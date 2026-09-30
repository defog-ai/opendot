"""The gateway step extension: connectors for one work step.

Before the step:
1. make <run folder>/mcp and copy bridge.py into it;
2. start a gateway (server.Gateway) with one socket per connector;
3. mount that folder read-only at /opendot/mcp;
4. give the model's CLI one stdio MCP server per connector: python3 bridge.py
   <socket>, with the connector's read tools as the allowed tools;
5. mount the FactIQ plugin files read-only at /opendot/instructions/factiq,
   downloading them first when they are missing;
6. add a prompt note that says what is available.

After the step the gateway stops and the sockets are removed. The orchestrator
attaches the step's gateway_calls rows to the attempt (StepExtensions.finish).
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from opendot.extensions import ExtensionError, StepContext
from opendot.models import Attempt, HostMount, HostMountKind, McpServerSpec, StepPlan
from opendot.sandbox import CONTAINER_INSTRUCTIONS, CONTAINER_MCP, hand_to_container

if TYPE_CHECKING:
    from opendot.config import Config, McpServerConfig
    from opendot.gateway.server import Gateway
    from opendot.gateway.upstream import UpstreamFactory
    from opendot.store import Store

__all__ = ["GatewayExtension", "prompt_note", "step_extensions"]

log = logging.getLogger(__name__)

BRIDGE_SOURCE = Path(__file__).with_name("bridge.py")
DEFAULT_CALL_TIMEOUT = 120
DEFAULT_MAX_RESULT_KIB = 512


def _limits(config: Config) -> tuple[int, int]:
    gateway = config.gateway
    if gateway is None:
        return DEFAULT_CALL_TIMEOUT, DEFAULT_MAX_RESULT_KIB
    return gateway.call_timeout_seconds, gateway.max_result_kib


def prompt_note(servers: list[McpServerConfig], instructions: dict[str, str]) -> str:
    lines = [
        "Connectors. These MCP servers are reached through the host, which adds the "
        "login; you never see or need a key."
    ]
    for server in servers:
        reads = ", ".join(server.read_tools) or "none"
        lines.append(f"- {server.name}: read tools {reads}.")
        if server.write_tools:
            kinds = ", ".join(f"mcp.{server.name}.{t}" for t in server.write_tools)
            lines.append(
                f"  Write tools are not callable. Propose them as actions ({kinds}); put the "
                "tool's arguments in the action's fields. The host asks the person first. "
                f"Their input schemas are in {CONTAINER_MCP}/{server.name}.write-tools.json "
                "once the server's tools have been listed."
            )
        place = instructions.get(server.name)
        if place:
            lines.append(
                f"  Read-only instructions for {server.name} are in {place}. "
                f"Start with {place}/skills/{server.name}/SKILL.md when it exists. "
                f"Where those files say {{plugin_root}} or ${{CLAUDE_PLUGIN_ROOT}}, "
                f"use {place}. Copy a script into your work folder before you change or run "
                "it; the folder is read-only. See LICENSE and SOURCE.md there for their licence."
            )
    return "\n".join(lines)


class GatewayExtension:
    name = "gateway"

    def __init__(
        self,
        config: Config,
        *,
        upstream_factory: UpstreamFactory | None = None,
        store_factory: Callable[[], Store] | None = None,
        download: Callable[[str], bytes] | None = None,
        env: Mapping[str, str] | None = None,
    ):
        self.config = config
        self.upstream_factory = upstream_factory
        self.store_factory = store_factory
        self.download = download
        self.env = env
        self._gateways: dict[str, Gateway] = {}
        self._lock = threading.Lock()

    def _open_store(self) -> Store:
        if self.store_factory is not None:
            return self.store_factory()
        from opendot.store import Store

        return Store(self.config.db_path)

    def _instructions(self, ctx: StepContext, server: McpServerConfig) -> Path | None:
        folder = server.instructions
        if folder is None:
            return None
        if server.preset == "factiq":
            from opendot.gateway.instructions import InstructionsError, fetch_factiq_instructions

            try:
                return fetch_factiq_instructions(folder, download=self.download)
            except (InstructionsError, OSError) as exc:
                ctx.store.log_event(
                    "connector.instructions_unavailable",
                    {"connector": server.name, "error": str(exc)[:500]},
                    task_id=ctx.task.id,
                )
                return None
        if folder.is_dir() and not folder.is_symlink():
            return folder
        return None

    def before_step(self, ctx: StepContext, plan: StepPlan) -> None:
        from opendot.gateway.server import Gateway, GatewayError

        connectors = ctx.config.connectors()
        if not connectors:
            return
        folder = plan.run_dir / "mcp"
        folder.mkdir(mode=0o700, exist_ok=True)
        os.chmod(folder, 0o700)
        hand_to_container(folder)
        bridge = folder / "bridge.py"
        shutil.copyfile(BRIDGE_SOURCE, bridge)
        os.chmod(bridge, 0o644)
        hand_to_container(bridge)

        timeout, max_kib = _limits(ctx.config)
        gateway = Gateway(
            connectors,
            task_id=ctx.task.id,
            step_token=plan.step_token,
            folder=folder,
            store_factory=self._open_store,
            upstream_factory=self.upstream_factory,
            call_timeout_seconds=timeout,
            max_result_kib=max_kib,
            env=self.env,
        )
        try:
            gateway.start()
        except GatewayError as exc:
            raise ExtensionError(str(exc)) from exc
        with self._lock:
            self._gateways[plan.step_token] = gateway

        try:
            self._describe(ctx, plan, connectors, folder, timeout)
        except BaseException:
            self.after_step(ctx, plan, None)
            raise

    def _describe(
        self,
        ctx: StepContext,
        plan: StepPlan,
        connectors: list[McpServerConfig],
        folder: Path,
        timeout: int,
    ) -> None:
        plan.host_mounts.append(HostMount(HostMountKind.MCP, folder, CONTAINER_MCP))
        places: dict[str, str] = {}
        for server in connectors:
            plan.mcp_servers.append(
                McpServerSpec(
                    name=server.name,
                    command=(
                        "python3",
                        f"{CONTAINER_MCP}/bridge.py",
                        f"{CONTAINER_MCP}/{server.name}.sock",
                    ),
                    tools=tuple(server.read_tools),
                    tool_timeout_seconds=timeout + 15,
                )
            )
            instructions = self._instructions(ctx, server)
            if instructions is not None:
                place = f"{CONTAINER_INSTRUCTIONS}/{server.name}"
                plan.host_mounts.append(HostMount(HostMountKind.INSTRUCTIONS, instructions, place))
                places[server.name] = place
        plan.prompt_notes.append(prompt_note(connectors, places))

    def after_step(self, ctx: StepContext, plan: StepPlan, attempt: Attempt | None) -> None:
        with self._lock:
            gateway = self._gateways.pop(plan.step_token, None)
        if gateway is not None:
            gateway.stop()


def step_extensions(config: Config) -> list[GatewayExtension]:
    """One gateway extension when any connector is configured, else none."""
    if not config.connectors():
        return []
    return [GatewayExtension(config)]
