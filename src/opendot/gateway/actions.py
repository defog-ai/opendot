"""Connector write tools as actions: mcp.<server>.<tool>.

A tool marked mode = "write" is never callable through the gateway. The model
proposes it as an action instead, and the host runs it like any other outward
action, so each call is recorded as an action of the task.

Fields: the tool's arguments, as the action's fields. A proposal that has only
the field "arguments" (an object) uses that object as the arguments.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opendot.actions import ActionContext, ActionResult, InvalidProposal, PreparedAction, mcp_kind
from opendot.models import GatewayCallStatus, GatewayMode

if TYPE_CHECKING:
    from opendot.config import Config, McpServerConfig

__all__ = ["MAX_ARGUMENT_BYTES", "McpWriteHandler", "action_handlers"]

MAX_ARGUMENT_BYTES = 64 * 1024
RESULT_PREVIEW_BYTES = 4096
WRITE_STEP_TOKEN = "action"  # gateway_calls.step_token for calls made by an action


def _destination(server: McpServerConfig) -> str:
    return server.url or (server.command[0] if server.command else "")


class McpWriteHandler:
    """One connector write tool, run by the host."""

    outward = True

    def __init__(
        self,
        server: McpServerConfig,
        tool: str,
        *,
        db_path: Path | None = None,
        timeout: float = 120.0,
        upstream_factory: Any = None,
    ):
        self.server = server
        self.tool = tool
        self.db_path = db_path
        self.timeout = float(timeout)
        self.kind = mcp_kind(server.name, tool)
        self.upstream_factory = upstream_factory
        self.description = (
            f"Call the write tool {tool} of connector {server.name} ({_destination(server)}). "
            "Fields: the tool's arguments. The host runs it after this step, without asking."
        )

    def _arguments(self, proposal: Mapping[str, Any]) -> dict[str, Any]:
        fields = {k: v for k, v in proposal.items() if k != "kind"}
        if set(fields) == {"arguments"} and isinstance(fields["arguments"], dict):
            fields = dict(fields["arguments"])
        return fields

    def prepare(self, proposal: Mapping[str, Any], ctx: ActionContext) -> PreparedAction:
        arguments = self._arguments(proposal)
        try:
            size = len(json.dumps(arguments, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise InvalidProposal(f"{self.kind}: the arguments are not plain JSON") from exc
        if size > MAX_ARGUMENT_BYTES:
            raise InvalidProposal(f"{self.kind}: the arguments are larger than 64 KiB")
        return PreparedAction(
            kind=self.kind,
            target=f"mcp:{self.server.name}:{self.tool}",
            payload={
                "server": self.server.name,
                "tool": self.tool,
                "destination": _destination(self.server),
                "arguments": arguments,
            },
            outward=True,
        )

    def execute(self, action: PreparedAction, ctx: ActionContext) -> ActionResult:
        payload = action.payload
        if (
            action.kind != self.kind
            or action.target != f"mcp:{self.server.name}:{self.tool}"
            or payload.get("destination") != _destination(self.server)
        ):
            return ActionResult(
                ok=False, detail={"error": "the action does not match this connector"}
            )
        arguments = payload.get("arguments")
        if not isinstance(arguments, dict):
            return ActionResult(ok=False, detail={"error": "arguments must be an object"})
        started = time.monotonic()
        outcome = self._call(arguments)
        duration = int((time.monotonic() - started) * 1000)
        if "error" in outcome:
            ctx.store.log_gateway_call(
                ctx.task.id,
                WRITE_STEP_TOKEN,
                server=self.server.name,
                tool=self.tool,
                mode=GatewayMode.WRITE,
                arguments=arguments,
                status=GatewayCallStatus.ERROR,
                error=outcome["error"][:1000],
                duration_ms=duration,
            )
            return ActionResult(ok=False, detail={"error": outcome["error"]})
        text = outcome["text"]
        preview = text.encode("utf-8")[:RESULT_PREVIEW_BYTES].decode("utf-8", "ignore")
        ok = not outcome["is_error"]
        ctx.store.log_gateway_call(
            ctx.task.id,
            WRITE_STEP_TOKEN,
            server=self.server.name,
            tool=self.tool,
            mode=GatewayMode.WRITE,
            arguments=arguments,
            status=GatewayCallStatus.OK if ok else GatewayCallStatus.ERROR,
            result_bytes=outcome["bytes"],
            result_preview=preview,
            error=None if ok else preview[:1000],
            duration_ms=duration,
        )
        detail = {"result": preview, "result_bytes": outcome["bytes"]}
        if not ok:
            detail["error"] = preview[:1000]
        return ActionResult(ok=ok, detail=detail)

    def _call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run the call on a fresh event loop in a helper thread. The helper opens
        its own Store when the login is an OAuth token; ctx.store stays in the
        calling thread."""
        box: dict[str, Any] = {}

        def run() -> None:
            import anyio

            try:
                box.update(anyio.run(self._call_async, arguments))
            except BaseException as exc:  # noqa: BLE001 - reported as the action's result
                box["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=run, name=f"opendot-{self.kind}", daemon=True)
        thread.start()
        thread.join()
        return box or {"error": "the call ended without a result"}

    async def _call_async(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from opendot.gateway.server import text_of

        store = None
        try:
            if self.upstream_factory is not None:
                opener = self.upstream_factory(self.server)
            else:
                from opendot.gateway.upstream import open_session

                if self.server.auth == "oauth":
                    from opendot.store import Store

                    store = Store(self.db_path)
                opener = open_session(self.server, store=store)
            async with opener as session:
                result = await session.call_tool(
                    self.tool, arguments, read_timeout_seconds=self.timeout
                )
            data = result.model_dump(mode="json", by_alias=True, exclude_none=True)
            text, _ = text_of(data.get("content") or [])
            return {
                "text": text,
                "is_error": bool(data.get("isError")),
                "bytes": len(json.dumps(data, ensure_ascii=False).encode("utf-8")),
            }
        finally:
            if store is not None:
                store.close()


def action_handlers(config: Config, *, upstream_factory: Any = None) -> list[McpWriteHandler]:
    """One handler per write tool of every configured connector."""
    handlers: list[McpWriteHandler] = []
    timeout = config.gateway.call_timeout_seconds if config.gateway is not None else 120
    for server in config.connectors():
        for tool in server.write_tools:
            handlers.append(
                McpWriteHandler(
                    server,
                    tool,
                    db_path=config.db_path,
                    timeout=timeout,
                    upstream_factory=upstream_factory,
                )
            )
    return handlers
