"""The host gateway: allowlist, refusals, cut results, timeouts, cancel and the call log.

The upstream MCP server is a real SDK server run in-process over memory streams.
"""

from __future__ import annotations

import dataclasses
import json
import os
import socket
import stat
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import pytest

from opendot.config import McpServerConfig, McpToolConfig
from opendot.gateway.server import (
    HANDSHAKE_VERSIONS,
    Gateway,
    GatewayError,
    bind_unix_socket,
    cut_result,
    text_of,
)
from opendot.models import GatewayCallStatus, GatewayMode
from opendot.store import Store

# ---------------------------------------------------------------------------
# A fake upstream server, shared with the other gateway test files
# ---------------------------------------------------------------------------


class UpstreamLog:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.opened = 0
        self.slow_started = threading.Event()


def make_fake_server(log: UpstreamLog):
    from mcp.server.mcpserver import MCPServer

    srv = MCPServer("fake-upstream", instructions="Fake upstream instructions.")

    @srv.tool()
    def search(query: str) -> str:
        """Search the fake catalog."""
        log.calls.append(("search", {"query": query}))
        return f"found {query}"

    @srv.tool()
    def big(size: int) -> str:
        """Return size characters."""
        log.calls.append(("big", {"size": size}))
        return "x" * size

    @srv.tool()
    async def slow(seconds: float) -> str:
        """Sleep, then answer."""
        log.calls.append(("slow", {"seconds": seconds}))
        log.slow_started.set()
        await anyio.sleep(seconds)
        return "done"

    @srv.tool()
    def boom() -> str:
        """Fail."""
        from mcp.server.mcpserver.exceptions import ToolError

        raise ToolError("upstream broke")

    @srv.tool()
    def submit(title: str, body: str = "") -> str:
        """Send something outward."""
        log.calls.append(("submit", {"title": title, "body": body}))
        return f"submitted {title}"

    @srv.tool()
    def hidden() -> str:
        """Not on any allowlist."""
        log.calls.append(("hidden", {}))
        return "should never run"

    return srv


def fake_factory(log: UpstreamLog | None = None):
    """An UpstreamFactory that serves the fake server in-process."""
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    log = log or UpstreamLog()

    @asynccontextmanager
    async def factory(server: McpServerConfig):
        srv = make_fake_server(log)
        low = srv._lowlevel_server
        log.opened += 1
        async with create_client_server_memory_streams() as (client, served):
            async with anyio.create_task_group() as tg:

                async def run_server() -> None:
                    await low.run(served[0], served[1], low.create_initialization_options())

                tg.start_soon(run_server)
                async with ClientSession(client[0], client[1]) as session:
                    await session.initialize()
                    yield session
                tg.cancel_scope.cancel()

    factory.log = log  # type: ignore[attr-defined]
    return factory


def failing_factory(message: str = "no route to host"):
    @asynccontextmanager
    async def factory(server: McpServerConfig):
        raise ConnectionError(message)
        yield  # pragma: no cover

    return factory


def fake_connector(name: str = "fake") -> McpServerConfig:
    return McpServerConfig(
        name=name,
        url="https://mcp.example.com/mcp",
        command=[],
        auth="none",
        auth_env="",
        tools=[
            McpToolConfig("search", "read"),
            McpToolConfig("big", "read"),
            McpToolConfig("slow", "read"),
            McpToolConfig("boom", "read"),
            McpToolConfig("submit", "write"),
        ],
    )


def connect_unix(sock: socket.socket, path: Path) -> None:
    """Connect, going through /proc/self/fd when the path is too long."""
    if len(str(path)) <= 107:
        sock.connect(str(path))
        return
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        sock.connect(f"/proc/self/fd/{dir_fd}/{path.name}")
    finally:
        os.close(dir_fd)


class LineClient:
    """A newline JSON-RPC client over the gateway's unix socket."""

    def __init__(self, path: Path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(30)
        connect_unix(self.sock, path)
        self.buffer = b""
        self.next_id = 0

    def send(self, message: Any) -> None:
        self.sock.sendall(json.dumps(message).encode() + b"\n")

    def send_raw(self, data: bytes) -> None:
        self.sock.sendall(data)

    def read(self) -> Any:
        while b"\n" not in self.buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("gateway closed the connection")
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}})
        return self.read()

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        reply = self.request("tools/call", {"name": tool, "arguments": arguments or {}})
        return reply["result"]

    def close(self) -> None:
        self.sock.close()


def result_text(result: dict[str, Any]) -> str:
    return text_of(result["content"])[0]


@pytest.fixture
def task(store):
    return store.create_task(text="t", requester="alice", channel="cli", conversation="local")


@pytest.fixture
def run_gateway(config, task):
    started: list[Gateway] = []

    def start(connectors=None, factory=None, **kwargs) -> Gateway:
        folder = config.runs_dir / f"task-{task.id}" / "tok" / "mcp"
        folder.mkdir(parents=True, exist_ok=True)
        gateway = Gateway(
            connectors or [fake_connector()],
            task_id=task.id,
            step_token="tok",
            folder=folder,
            store_factory=lambda: Store(config.db_path),
            upstream_factory=factory or fake_factory(),
            **kwargs,
        )
        gateway.start()
        started.append(gateway)
        return gateway

    yield start
    for gateway in started:
        gateway.stop()


def calls_of(store, task):
    return store.list_gateway_calls(task_id=task.id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_handshake_versions_match_the_sdk():
    from mcp_types import version

    assert set(HANDSHAKE_VERSIONS) <= set(version.SUPPORTED_PROTOCOL_VERSIONS)
    assert HANDSHAKE_VERSIONS[-1] == version.LATEST_HANDSHAKE_VERSION


def test_initialize_echoes_a_known_version_and_adds_upstream_instructions(run_gateway):
    gateway = run_gateway()
    client = LineClient(gateway.socket_paths["fake"])
    reply = client.request("initialize", {"protocolVersion": "2025-06-18"})
    result = reply["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "opendot-fake"
    assert "Fake upstream instructions." in result["instructions"]
    reply = client.request("initialize", {"protocolVersion": "1999-01-01"})
    assert reply["result"]["protocolVersion"] == HANDSHAKE_VERSIONS[-1]
    assert client.request("ping")["result"] == {}
    assert client.request("resources/list")["error"]["code"] == -32601
    client.close()


def test_tools_list_shows_only_read_tools_and_writes_the_schemas_file(run_gateway):
    gateway = run_gateway()
    client = LineClient(gateway.socket_paths["fake"])
    tools = client.request("tools/list")["result"]["tools"]
    assert sorted(t["name"] for t in tools) == ["big", "boom", "search", "slow"]
    assert all("inputSchema" in t for t in tools)
    schemas = json.loads((gateway.folder / "fake.write-tools.json").read_text())
    assert [t["name"] for t in schemas["tools"]] == ["submit"]
    assert "title" in schemas["tools"][0]["inputSchema"]["properties"]
    client.close()


def test_read_tool_is_proxied_and_logged(run_gateway, store, task):
    gateway = run_gateway()
    client = LineClient(gateway.socket_paths["fake"])
    result = client.call("search", {"query": "cpi"})
    assert result_text(result) == "found cpi"
    assert not result.get("isError")
    client.close()
    gateway.stop()
    [row] = calls_of(store, task)
    assert (row.server, row.tool, row.mode, row.status) == (
        "fake",
        "search",
        GatewayMode.READ,
        GatewayCallStatus.OK,
    )
    assert row.arguments == {"query": "cpi"}
    assert row.step_token == "tok"
    assert row.result_preview == "found cpi"
    assert row.result_bytes > 0


def test_write_and_unknown_tools_are_refused_without_reaching_upstream(run_gateway, store, task):
    factory = fake_factory()
    gateway = run_gateway(factory=factory)
    client = LineClient(gateway.socket_paths["fake"])
    result = client.call("submit", {"title": "hello"})
    assert result["isError"] is True
    assert "mcp.fake.submit" in result_text(result)
    result = client.call("hidden")
    assert result["isError"] is True
    assert "not on the allowlist" in result_text(result)
    client.close()
    gateway.stop()
    assert [name for name, _ in factory.log.calls] == []
    rows = calls_of(store, task)
    assert [(r.tool, r.mode, r.status) for r in rows] == [
        ("submit", GatewayMode.WRITE, GatewayCallStatus.REFUSED),
        ("hidden", GatewayMode.READ, GatewayCallStatus.REFUSED),
    ]


def test_large_result_is_cut_with_a_note(run_gateway, store, task):
    gateway = run_gateway(max_result_kib=4)
    client = LineClient(gateway.socket_paths["fake"])
    result = client.call("big", {"size": 20000})
    text = result_text(result)
    assert len(text.encode()) <= 4 * 1024
    assert "OpenDot cut this result" in text
    assert "structuredContent" not in result
    client.close()
    gateway.stop()
    [row] = calls_of(store, task)
    assert row.result_bytes > 20000
    assert len(row.result_preview.encode()) <= 4096


def test_cut_result_keeps_small_results_and_marks_errors():
    small = {"content": [{"type": "text", "text": "hi"}]}
    assert cut_result(small, 1024) == (small, len(json.dumps(small).encode()), False)
    large = {
        "content": [{"type": "text", "text": "y" * 5000}, {"type": "image", "data": "zz"}],
        "structuredContent": {"a": 1},
        "isError": True,
    }
    sent, size, was_cut = cut_result(large, 2048)
    assert was_cut and size > 5000
    assert sent["isError"] is True
    assert "1 non-text part(s)" in sent["content"][0]["text"]
    assert len(json.dumps(sent).encode()) <= 2048 + 200


def test_upstream_tool_error_is_passed_on_and_logged(run_gateway, store, task):
    gateway = run_gateway()
    client = LineClient(gateway.socket_paths["fake"])
    result = client.call("boom")
    assert result["isError"] is True
    assert "upstream broke" in result_text(result)
    client.close()
    gateway.stop()
    [row] = calls_of(store, task)
    assert row.status == GatewayCallStatus.ERROR


def test_slow_tool_times_out(run_gateway, store, task):
    gateway = run_gateway(call_timeout_seconds=0.5)
    client = LineClient(gateway.socket_paths["fake"])
    started = time.monotonic()
    result = client.call("slow", {"seconds": 30})
    assert time.monotonic() - started < 15
    assert result["isError"] is True
    client.close()
    gateway.stop()
    [row] = calls_of(store, task)
    assert row.status == GatewayCallStatus.ERROR


def test_cancel_notification_stops_the_call(run_gateway, store, task):
    factory = fake_factory()
    gateway = run_gateway(factory=factory)
    client = LineClient(gateway.socket_paths["fake"])
    client.send(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "slow", "arguments": {"seconds": 30}},
        }
    )
    assert factory.log.slow_started.wait(10)
    client.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 7}})
    # The cancelled call sends nothing; the next request is answered normally.
    reply = client.request("ping")
    assert reply == {"jsonrpc": "2.0", "id": 1, "result": {}}
    client.close()
    gateway.stop()
    [row] = calls_of(store, task)
    assert row.status == GatewayCallStatus.ERROR
    assert row.error == "cancelled by the client"


def test_bad_lines_get_json_rpc_errors(run_gateway):
    gateway = run_gateway()
    client = LineClient(gateway.socket_paths["fake"])
    client.send_raw(b"{not json\n")
    assert client.read()["error"]["code"] == -32700
    reply = client.request("tools/call", {"name": 5})
    assert reply["error"]["code"] == -32602
    client.send(
        [
            {"jsonrpc": "2.0", "id": "a", "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": "b", "method": "ping"},
        ]
    )
    batch = client.read()
    assert [item["id"] for item in batch] == ["a", "b"]
    client.close()


def test_unreachable_upstream_gives_empty_list_and_tool_errors(run_gateway, store, task):
    gateway = run_gateway(factory=failing_factory())
    client = LineClient(gateway.socket_paths["fake"])
    assert client.request("initialize", {})["result"]["serverInfo"]["name"] == "opendot-fake"
    assert client.request("tools/list")["result"]["tools"] == []
    result = client.call("search", {"query": "x"})
    assert result["isError"] is True
    assert "no route to host" in result_text(result)
    client.close()
    gateway.stop()
    rows = calls_of(store, task)
    assert [(r.tool, r.status) for r in rows] == [
        ("tools/list", GatewayCallStatus.ERROR),
        ("search", GatewayCallStatus.ERROR),
    ]


def test_socket_files_are_private_and_removed_on_stop(run_gateway):
    gateway = run_gateway(connectors=[fake_connector("one"), fake_connector("two")])
    paths = dict(gateway.socket_paths)
    assert sorted(paths) == ["one", "two"]
    for path in paths.values():
        mode = path.stat().st_mode
        assert stat.S_ISSOCK(mode)
        assert stat.S_IMODE(mode) == 0o600
    gateway.stop()
    assert not any(p.exists() for p in paths.values())


def test_long_socket_path_is_bound_through_proc(tmp_path):
    folder = tmp_path / ("d" * 60) / ("e" * 60)
    folder.mkdir(parents=True)
    path = folder / "connector.sock"
    assert len(str(path)) > 107
    sock = bind_unix_socket(path)
    try:
        assert stat.S_ISSOCK(path.stat().st_mode)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connect_unix(client, path)
        client.close()
    finally:
        sock.close()


def test_start_fails_cleanly_when_the_store_cannot_open(config, task):
    folder = config.runs_dir / "x" / "mcp"
    folder.mkdir(parents=True)

    def broken_store():
        raise RuntimeError("database is locked")

    gateway = Gateway(
        [fake_connector()],
        task_id=task.id,
        step_token="tok",
        folder=folder,
        store_factory=broken_store,
        upstream_factory=fake_factory(),
    )
    with pytest.raises(GatewayError, match="database is locked"):
        gateway.start()
    assert not (folder / "fake.sock").exists()


BEARER = "bearer-" + "5f0c9a1e7d3b2468ace0"
URL_KEY = "urlkey-" + "8d1b3f5a7c9e0246bdf1"
ACCESS = "access-" + "2c4e6a8b0d1f3579ace2"


@pytest.mark.parametrize("auth", ["bearer_env", "oauth"])
def test_connector_errors_never_show_its_credentials(run_gateway, store, task, auth):
    url = f"https://mcp.example.com/mcp?key={URL_KEY}&v=1"
    server = dataclasses.replace(
        fake_connector(),
        url=url,
        auth=auth,
        auth_env="FAKE_MCP_TOKEN" if auth == "bearer_env" else "",
    )
    store.save_connector_token("fake", ACCESS, refresh_token="refresh-" + "7b9d1f3e5a0c2468")
    message = (
        f"POST {url} failed; headers: Authorization: Bearer {BEARER}, "
        f"echo {BEARER} {ACCESS} {URL_KEY}"
    )
    gateway = run_gateway(
        connectors=[server], factory=failing_factory(message), env={"FAKE_MCP_TOKEN": BEARER}
    )
    client = LineClient(gateway.socket_paths["fake"])
    client.request("initialize", {})
    client.request("tools/list")
    shown = result_text(client.call("search", {"query": "x"}))
    client.close()
    gateway.stop()
    logged = " ".join(r.error or "" for r in calls_of(store, task))
    assert "https://mcp.example.com/mcp failed" in shown
    for text in (shown, logged):
        assert URL_KEY not in text
        assert ACCESS not in text
        if auth == "bearer_env":
            assert BEARER not in text
