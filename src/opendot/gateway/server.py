"""The gateway thread: one unix socket per connector, answering MCP over JSON lines.

The model's CLI talks MCP to bridge.py over stdio; the bridge copies the bytes to
<run folder>/mcp/<connector>.sock. This module answers on that socket. It speaks
the few MCP requests a tool client needs (initialize, ping, tools/list,
tools/call) and nothing else, so the container can reach only what is listed here.

Rules for tools/call:
- a tool in the connector's read list is forwarded with the host's login, with a
  time limit (gateway.call_timeout_seconds) and a size limit on the result
  (gateway.max_result_kib);
- any other tool is refused with a tool error. A write tool's refusal names the
  action kind mcp.<server>.<tool> the model can propose instead;
- every call, refused or not, is one row in gateway_calls.

The thread opens its own Store, because a sqlite3 connection belongs to the
thread that opened it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from opendot import __version__
from opendot.models import GatewayCallStatus, GatewayMode
from opendot.redact import redact, secret_strings
from opendot.sandbox import hand_to_container

if TYPE_CHECKING:
    from opendot.config import McpServerConfig
    from opendot.gateway.upstream import UpstreamFactory
    from opendot.store import Store

__all__ = [
    "Gateway",
    "GatewayError",
    "HANDSHAKE_VERSIONS",
    "MAX_REQUEST_BYTES",
    "bind_unix_socket",
    "cut_result",
    "text_of",
    "write_tools_file",
]

log = logging.getLogger(__name__)

# Protocol versions with the initialize handshake. Kept here so this module does
# not import the mcp package at load time; test_gateway_server checks the list
# against mcp_types.version.
HANDSHAKE_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")

MAX_REQUEST_BYTES = 1024 * 1024  # a larger request line is refused
READER_LIMIT = 4 * 1024 * 1024  # asyncio's buffer for one line
SOCKET_PATH_LIMIT = 107  # sun_path is 108 bytes with the closing NUL
PREVIEW_BYTES = 4096  # result text kept in gateway_calls.result_preview
CONNECT_WAIT_SECONDS = 20.0  # tools/list waits this long for the connector
INITIALIZE_WAIT_SECONDS = 5.0  # initialize waits this long for the server's instructions
RECONNECT_AFTER_SECONDS = 10.0  # a failed connection is tried again after this
STOP_WAIT_SECONDS = 10.0


class GatewayError(RuntimeError):
    """The gateway could not start."""


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def bind_unix_socket(path: Path) -> socket.socket:
    """A listening unix socket at path, mode 0600. A path longer than the kernel
    allows is bound through /proc/self/fd/<folder fd>/<name>."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        if len(os.fsencode(str(path))) <= SOCKET_PATH_LIMIT:
            sock.bind(str(path))
        else:
            dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                short = f"/proc/self/fd/{dir_fd}/{path.name}"
                if len(os.fsencode(short)) > SOCKET_PATH_LIMIT:
                    raise GatewayError(f"socket name too long: {path.name}")
                sock.bind(short)
            finally:
                os.close(dir_fd)
        os.chmod(path, 0o600)
        hand_to_container(path)
        sock.listen(8)
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise


def text_of(content: list[dict[str, Any]]) -> tuple[str, int]:
    """The text parts joined, and the number of parts that are not text."""
    texts: list[str] = []
    other = 0
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            texts.append(str(item.get("text", "")))
        else:
            other += 1
    return "\n".join(texts), other


def cut_result(result: dict[str, Any], max_bytes: int) -> tuple[dict[str, Any], int, bool]:
    """(result to send, its full size in bytes, whether it was cut).

    A result above max_bytes keeps only the start of its text, with a note, and
    loses structuredContent and non-text parts."""
    full = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    if full <= max_bytes:
        return result, full, False
    text, other = text_of(result.get("content") or [])
    note_budget = 400
    head = text.encode("utf-8")[: max(0, max_bytes - note_budget)].decode("utf-8", "ignore")
    note = (
        f"\n\n[OpenDot cut this result: it was {full // 1024} KiB and the limit is "
        f"{max_bytes // 1024} KiB (gateway.max_result_kib)."
    )
    if other:
        note += f" {other} non-text part(s) were left out."
    note += " Ask for less, for example with a narrower query or a smaller limit.]"
    cut: dict[str, Any] = {"content": [{"type": "text", "text": head + note}]}
    if result.get("isError"):
        cut["isError"] = True
    return cut, full, True


def _tool_error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _wire_tool(tool: Any) -> dict[str, Any]:
    data = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
    return {
        k: data[k]
        for k in ("name", "title", "description", "inputSchema", "annotations")
        if k in data
    }


def write_tools_file(folder: Path, server: McpServerConfig, tools: list[Any]) -> Path | None:
    """<folder>/<server>.write-tools.json: the write tools' descriptions and input
    schemas, so the model can build an mcp.<server>.<tool> proposal."""
    if not server.write_tools:
        return None
    wanted = set(server.write_tools)
    items = [_wire_tool(t) for t in tools if t.name in wanted]
    path = folder / f"{server.name}.write-tools.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"server": server.name, "tools": items}, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    hand_to_container(tmp)
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# One connector's upstream session
# ---------------------------------------------------------------------------


class _Upstream:
    """Owns one connection. The task that enters the SDK's context managers must
    also leave them, so one long-lived task holds the session and others only call
    it."""

    def __init__(self, server: McpServerConfig, factory: UpstreamFactory):
        self.server = server
        self.factory = factory
        self.session: Any = None
        self.error: BaseException | None = None
        self.tools: list[Any] | None = None
        self.ready = asyncio.Event()
        self.stopping = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.started_at = 0.0
        self.lock = asyncio.Lock()

    def start(self) -> None:
        self.ready = asyncio.Event()
        self.error = None
        self.session = None
        self.started_at = time.monotonic()
        self.task = asyncio.create_task(self._run(), name=f"upstream-{self.server.name}")

    async def _run(self) -> None:
        try:
            async with self.factory(self.server) as session:
                self.session = session
                self.ready.set()
                await self.stopping.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - reported to the caller as a tool error
            self.error = exc
            log.warning("connector %s: %s", self.server.name, exc)
        finally:
            self.session = None
            self.ready.set()

    async def get(self, wait: float) -> Any:
        """The session, starting or restarting the connection when needed."""
        if self.task is None or (
            self.task.done() and time.monotonic() - self.started_at >= RECONNECT_AFTER_SECONDS
        ):
            self.start()
        try:
            await asyncio.wait_for(self.ready.wait(), wait)
        except TimeoutError as exc:
            raise ConnectionError(
                f"connector {self.server.name} did not answer within {wait:.0f} s"
            ) from exc
        if self.session is None:
            raise ConnectionError(f"connector {self.server.name} is not reachable: {self.error}")
        return self.session

    async def list_tools(self, wait: float) -> list[Any]:
        from opendot.gateway.upstream import list_all_tools

        async with self.lock:
            if self.tools is None:
                session = await self.get(wait)
                self.tools = await asyncio.wait_for(list_all_tools(session), wait)
            return self.tools

    async def close(self) -> None:
        self.stopping.set()
        if self.task is not None and not self.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.task), STOP_WAIT_SECONDS)
            except (TimeoutError, asyncio.CancelledError):
                self.task.cancel()
                with contextlib.suppress(BaseException):
                    await self.task


# ---------------------------------------------------------------------------
# The gateway
# ---------------------------------------------------------------------------


class Gateway:
    """One step's gateway: a thread with an asyncio loop and one socket per connector.

    start() binds the sockets in the calling thread (so a bind error is raised
    there) and returns once the thread serves them. stop() ends the thread and
    removes the socket files.
    """

    def __init__(
        self,
        connectors: list[McpServerConfig],
        *,
        task_id: int,
        step_token: str,
        folder: Path,
        store_factory: Callable[[], Store],
        upstream_factory: UpstreamFactory | None = None,
        call_timeout_seconds: float = 120,
        max_result_kib: int = 512,
        env: Mapping[str, str] | None = None,
    ):
        self.connectors = {c.name: c for c in connectors}
        self.task_id = task_id
        self.step_token = step_token
        self.folder = Path(folder)
        self.store_factory = store_factory
        self.call_timeout = float(call_timeout_seconds)
        self.max_bytes = int(max_result_kib) * 1024
        self.env = env
        self._factory = upstream_factory
        self._sockets: dict[str, socket.socket] = {}
        self.socket_paths: dict[str, Path] = {}
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._failure: BaseException | None = None
        self._store: Store | None = None
        self._upstreams: dict[str, _Upstream] = {}

    # -- lifecycle -----------------------------------------------------------

    def start(self, timeout: float = 30.0) -> None:
        try:
            for name in self.connectors:
                path = self.folder / f"{name}.sock"
                self._sockets[name] = bind_unix_socket(path)
                self.socket_paths[name] = path
        except OSError as exc:
            self._close_sockets()
            raise GatewayError(f"cannot open the gateway socket: {exc}") from exc
        self._thread = threading.Thread(
            target=self._thread_main, name=f"opendot-gateway-{self.step_token}", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            self.stop()
            raise GatewayError("the gateway thread did not start")
        if self._failure is not None:
            failure = self._failure
            self.stop()
            raise GatewayError(f"the gateway could not start: {failure}") from failure

    def stop(self) -> None:
        loop, stop = self._loop, self._stop
        if loop is not None and stop is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(stop.set)
        if self._thread is not None:
            self._thread.join(STOP_WAIT_SECONDS * 2)
        self._close_sockets()

    def _close_sockets(self) -> None:
        for sock in self._sockets.values():
            with contextlib.suppress(OSError):
                sock.close()
        self._sockets.clear()
        for path in self.socket_paths.values():
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    def _thread_main(self) -> None:
        try:
            self._store = self.store_factory()
        except BaseException as exc:  # noqa: BLE001 - reported by start()
            self._failure = exc
            self._ready.set()
            return
        try:
            asyncio.run(self._main())
        except BaseException as exc:  # noqa: BLE001
            if not self._ready.is_set():
                self._failure = exc
            log.warning("gateway stopped with an error: %s", exc)
        finally:
            self._ready.set()
            self._store.close()
            self._store = None

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        factory = self._factory or self._default_factory()
        for name, server in self.connectors.items():
            self._upstreams[name] = _Upstream(server, factory)
        servers = []
        handlers: set[asyncio.Task] = set()
        for name, sock in self._sockets.items():

            async def on_connect(reader, writer, _name=name) -> None:
                task = asyncio.current_task()
                if task is not None:
                    handlers.add(task)
                try:
                    await self._serve(_name, reader, writer)
                finally:
                    if task is not None:
                        handlers.discard(task)

            servers.append(
                await asyncio.start_unix_server(on_connect, sock=sock, limit=READER_LIMIT)
            )
        for upstream in self._upstreams.values():
            upstream.start()
        self._ready.set()
        try:
            await self._stop.wait()
        finally:
            for srv in servers:
                srv.close()
            for task in list(handlers):
                task.cancel()
            for task in list(handlers):
                with contextlib.suppress(BaseException):
                    await task
            for upstream in self._upstreams.values():
                await upstream.close()

    def _default_factory(self) -> UpstreamFactory:
        from opendot.gateway.upstream import open_session

        def factory(server: McpServerConfig):
            return open_session(server, store=self._store, env=self.env)

        return factory

    # -- JSON-RPC over lines ---------------------------------------------------

    async def _serve(
        self, name: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        write_lock = asyncio.Lock()
        running: dict[Any, asyncio.Task] = {}  # request id -> task, for cancel
        pending: set[asyncio.Task] = set()  # every request or batch still running

        async def send(message: Any) -> None:
            data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            async with write_lock:
                writer.write(data + b"\n")
                await writer.drain()

        async def answer(message: dict[str, Any]) -> dict[str, Any] | None:
            method = message.get("method")
            msg_id = message.get("id")
            try:
                result = await self._request(name, method, message.get("params") or {})
                return {"jsonrpc": "2.0", "id": msg_id, "result": result}
            except _RpcError as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": exc.code, "message": exc.message},
                }
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad call must not end the connection
                message_text = self._scrub(self.connectors[name], str(exc))
                log.warning("gateway %s: %s failed: %s", name, method, message_text)
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32603, "message": message_text[:500]},
                }

        async def run_one(message: dict[str, Any]) -> None:
            try:
                response = await answer(message)
            except asyncio.CancelledError:
                return
            finally:
                running.pop(message.get("id"), None)
            if response is not None:
                with contextlib.suppress(ConnectionError):
                    await send(response)

        async def run_batch(items: list[Any]) -> None:
            responses = []
            for item in items:
                if not isinstance(item, dict) or "method" not in item:
                    continue
                if "id" not in item:
                    self._notification(item, running)
                    continue
                response = await answer(item)
                if response is not None:
                    responses.append(response)
            if responses:
                with contextlib.suppress(ConnectionError):
                    await send(responses)

        try:
            while True:
                try:
                    line = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    await send(_rpc_error(None, -32600, "request too large"))
                    break
                if not line:
                    break
                if len(line) > MAX_REQUEST_BYTES:
                    await send(_rpc_error(None, -32600, "request too large"))
                    continue
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    await send(_rpc_error(None, -32700, "not valid JSON"))
                    continue
                if isinstance(message, list):
                    batch = asyncio.create_task(run_batch(message))
                    pending.add(batch)
                    batch.add_done_callback(pending.discard)
                    continue
                if not isinstance(message, dict) or "method" not in message:
                    continue  # a response to a request we never send, or junk
                if "id" not in message:
                    self._notification(message, running)
                    continue
                task = asyncio.create_task(run_one(message))
                running[message["id"]] = task
                pending.add(task)
                task.add_done_callback(pending.discard)
            # The client closed its side: answer what it already asked, then close.
            if pending:
                await asyncio.gather(*list(pending), return_exceptions=True)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            for task in list(pending):
                task.cancel()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def _notification(self, message: dict[str, Any], running: dict[Any, asyncio.Task]) -> None:
        if message.get("method") == "notifications/cancelled":
            params = message.get("params") or {}
            task = running.get(params.get("requestId"))
            if task is not None:
                task.cancel()

    async def _request(self, name: str, method: Any, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise _RpcError(-32602, "params must be an object")
        if method == "initialize":
            return await self._initialize(name, params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": await self._list_tools(name)}
        if method == "tools/call":
            return await self._call_tool(name, params)
        raise _RpcError(-32601, f"method not available: {method}")

    async def _initialize(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        asked = params.get("protocolVersion")
        version = asked if asked in HANDSHAKE_VERSIONS else HANDSHAKE_VERSIONS[-1]
        server = self.connectors[name]
        text = (
            f"OpenDot forwards the read tools of connector {name} from the host. "
            "Write tools are not listed here; propose them as actions."
        )
        upstream = self._upstreams[name]
        with contextlib.suppress(Exception):
            session = await upstream.get(INITIALIZE_WAIT_SECONDS)
            result = getattr(session, "initialize_result", None)
            upstream_text = getattr(result, "instructions", None)
            if isinstance(upstream_text, str) and upstream_text:
                text = f"{text}\n\n{self._scrub(server, upstream_text)}"
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": f"opendot-{server.name}", "version": __version__},
            "instructions": text,
        }

    async def _list_tools(self, name: str) -> list[dict[str, Any]]:
        server = self.connectors[name]
        upstream = self._upstreams[name]
        started = time.monotonic()
        try:
            tools = await upstream.list_tools(CONNECT_WAIT_SECONDS)
        except Exception as exc:  # noqa: BLE001 - the CLI still starts, with no tools
            self._log(
                server,
                "tools/list",
                GatewayMode.READ,
                {},
                GatewayCallStatus.ERROR,
                error=self._scrub(server, str(exc))[:1000],
                started=started,
            )
            return []
        with contextlib.suppress(OSError):
            write_tools_file(self.folder, server, tools)
        allowed = set(server.read_tools)
        return [_wire_tool(t) for t in tools if t.name in allowed]

    async def _call_tool(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        server = self.connectors[name]
        tool = params.get("name")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(tool, str) or not isinstance(arguments, dict):
            raise _RpcError(-32602, "tools/call needs a tool name and an arguments object")
        started = time.monotonic()
        if tool not in server.read_tools:
            if tool in server.write_tools:
                mode = GatewayMode.WRITE
                text = (
                    f"{tool} changes something outside this step, so it is not a tool here. "
                    f"Propose it as the action kind mcp.{server.name}.{tool} with the tool's "
                    "arguments as the action's fields; the host asks the person before it runs."
                )
            else:
                mode = GatewayMode.READ
                text = f"{tool} is not on the allowlist for connector {server.name}."
            self._log(
                server,
                tool,
                mode,
                arguments,
                GatewayCallStatus.REFUSED,
                error=text,
                started=started,
            )
            return _tool_error(text)
        try:
            session = await self._upstreams[name].get(CONNECT_WAIT_SECONDS)
            result = await asyncio.wait_for(
                session.call_tool(tool, arguments, read_timeout_seconds=self.call_timeout),
                self.call_timeout + 5,
            )
        except TimeoutError:
            text = f"{tool} timed out after {self.call_timeout:.0f} s"
            self._log(
                server,
                tool,
                GatewayMode.READ,
                arguments,
                GatewayCallStatus.ERROR,
                error=text,
                started=started,
            )
            return _tool_error(text)
        except asyncio.CancelledError:
            self._log(
                server,
                tool,
                GatewayMode.READ,
                arguments,
                GatewayCallStatus.ERROR,
                error="cancelled by the client",
                started=started,
            )
            raise
        except Exception as exc:  # noqa: BLE001 - reported to the model as a tool error
            text = (
                f"connector {server.name} failed on {tool}: {self._scrub(server, str(exc))[:500]}"
            )
            self._log(
                server,
                tool,
                GatewayMode.READ,
                arguments,
                GatewayCallStatus.ERROR,
                error=text,
                started=started,
            )
            return _tool_error(text)
        data = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        wire = {k: data[k] for k in ("content", "structuredContent", "isError") if k in data}
        wire.setdefault("content", [])
        sent, size, _ = cut_result(wire, self.max_bytes)
        preview, _ = text_of(wire.get("content") or [])
        is_error = bool(wire.get("isError"))
        self._log(
            server,
            tool,
            GatewayMode.READ,
            arguments,
            GatewayCallStatus.ERROR if is_error else GatewayCallStatus.OK,
            result_bytes=size,
            preview=preview,
            error=preview[:1000] if is_error else None,
            started=started,
        )
        return sent

    def _scrub(self, server: McpServerConfig, text: str) -> str:
        """Remove the connector's credentials from text the model or the call log sees.

        An upstream error can repeat the request: the address with its query
        values, or a header. The bearer token, the stored OAuth tokens and client
        secret, and the address's query values and password are masked, and the
        address is shown without its query."""
        secrets: list[str | None] = []
        if server.auth == "bearer_env":
            secrets.append(server.token(self.env))
        if self._store is not None:
            with contextlib.suppress(Exception):
                token = self._store.get_connector_token(server.name)
                if token is not None:
                    secrets += [token.access_token, token.refresh_token]
                    secrets += secret_strings(token.client_info)
        if server.url:
            parts = urlsplit(server.url)
            secrets += [value for _, value in parse_qsl(parts.query)]
            secrets.append(parts.password)
            if parts.query or parts.fragment or parts.password:
                host = parts.hostname or ""
                if ":" in host:
                    host = f"[{host}]"
                if parts.port is not None:
                    host = f"{host}:{parts.port}"
                bare = urlunsplit((parts.scheme, host, parts.path, "", ""))
                text = text.replace(server.url, bare)
        return redact(text, secrets)

    def _log(
        self,
        server: McpServerConfig,
        tool: str,
        mode: GatewayMode,
        arguments: dict[str, Any],
        status: GatewayCallStatus,
        *,
        result_bytes: int = 0,
        preview: str = "",
        error: str | None = None,
        started: float,
    ) -> None:
        assert self._store is not None
        self._store.log_gateway_call(
            self.task_id,
            self.step_token,
            server=server.name,
            tool=tool,
            mode=mode,
            arguments=arguments,
            status=status,
            result_bytes=result_bytes,
            result_preview=preview.encode("utf-8")[:PREVIEW_BYTES].decode("utf-8", "ignore"),
            error=error,
            duration_ms=int((time.monotonic() - started) * 1000),
        )


def _rpc_error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
