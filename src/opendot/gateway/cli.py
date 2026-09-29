"""Command line for connectors, and the gateway's doctor checks.

opendot connectors list
opendot connectors login <name> [--no-browser] [--paste] [--port N]
opendot connectors logout <name>
opendot connectors fetch-instructions [<name>] [--force]
opendot connectors test <name>
opendot connectors calls [--task N] [--limit N]
opendot init --with-factiq ...
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from opendot.config import FACTIQ_PLUGIN_COMMIT, Config

if TYPE_CHECKING:
    from opendot.config import McpServerConfig

__all__ = ["ConnectorCliError", "FACTIQ_INIT_BLOCK", "doctor_checks", "register_cli"]

FACTIQ_INIT_BLOCK = (
    "\n"
    "# FactIQ connector (https://api.factiq.com/mcp). Log in with an API key in\n"
    '# FACTIQ_API_KEY, or set auth = "oauth" and run `opendot connectors login factiq`.\n'
    "[factiq]\n"
    "enabled = true\n"
)


class ConnectorCliError(ValueError):
    """A problem to report with exit code 1 (opendot's main prints ValueError)."""


def _load(args: argparse.Namespace) -> Config:
    return Config.load(Path(args.config) if getattr(args, "config", None) else None)


def _server(config: Config, name: str) -> McpServerConfig:
    server = config.connector(name)
    if server is None:
        names = ", ".join(s.name for s in config.connectors()) or "none"
        raise ConnectorCliError(f"no connector named {name!r} (configured: {names})")
    return server


def _login_state(config: Config, server: McpServerConfig) -> tuple[bool, str]:
    if server.auth == "none":
        return True, "no login needed"
    if server.auth == "bearer_env":
        if os.environ.get(server.auth_env):
            return True, f"API key in {server.auth_env}"
        return False, f"set {server.auth_env} on the host"
    if _stored_token(config, server.name):
        return True, "OAuth token stored"
    return False, f"run `opendot connectors login {server.name}`"


def _stored_token(config: Config, name: str) -> bool:
    """Read-only look at connector_tokens; never creates the database."""
    if not config.db_path.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{config.db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute("SELECT 1 FROM connector_tokens WHERE server = ?", (name,)).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _source(server: McpServerConfig) -> str:
    return server.url or " ".join(server.command)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    config = _load(args)
    connectors = config.connectors()
    if not connectors:
        print("No connectors. Add [factiq] enabled = true or an [[mcp_servers]] entry.")
        return 0
    from opendot.gateway.instructions import installed_commit

    for server in connectors:
        ok, detail = _login_state(config, server)
        print(f"{server.name}  {_source(server)}")
        print(f"  login: {'ready' if ok else 'missing'} ({detail})")
        print(f"  read tools: {', '.join(server.read_tools) or 'none'}")
        if server.write_tools:
            kinds = ", ".join(f"mcp.{server.name}.{t}" for t in server.write_tools)
            print(f"  write tools (actions, always ask): {kinds}")
        if server.instructions is not None:
            commit = installed_commit(server.instructions)
            state = f"at {commit[:12]}" if commit else "not fetched yet"
            print(f"  instructions: {state}")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    config = _load(args)
    server = _server(config, args.name)
    from opendot.gateway.oauth import LoginError, login
    from opendot.store import open_store

    store = open_store(config)
    try:
        tools = login(
            server,
            store,
            open_browser=not args.no_browser,
            paste=args.paste,
            port=args.port,
        )
    except LoginError as exc:
        raise ConnectorCliError(f"sign-in failed: {exc}") from exc
    finally:
        store.close()
    print(f"Signed in to {server.name}. The token is kept in {config.db_path}.")
    print(f"The server lists {len(tools)} tools.")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    config = _load(args)
    server = _server(config, args.name)
    from opendot.store import open_store

    store = open_store(config)
    try:
        removed = store.delete_connector_token(server.name)
    finally:
        store.close()
    print(
        f"Removed the stored token for {server.name}."
        if removed
        else f"No token stored for {server.name}."
    )
    return 0


def cmd_fetch_instructions(args: argparse.Namespace) -> int:
    config = _load(args)
    server = _server(config, args.name)
    if server.preset != "factiq":
        raise ConnectorCliError(
            f"only the factiq connector has instructions to download, not {server.name}"
        )
    if server.instructions is None:
        raise ConnectorCliError("factiq.instructions is false in the config")
    from opendot.gateway.instructions import InstructionsError, fetch_factiq_instructions

    config.ensure_directories()
    try:
        folder = fetch_factiq_instructions(server.instructions, force=args.force)
    except InstructionsError as exc:
        raise ConnectorCliError(str(exc)) from exc
    print(f"FactIQ plugin files at commit {FACTIQ_PLUGIN_COMMIT[:12]} are in {folder}")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    config = _load(args)
    server = _server(config, args.name)
    import anyio

    from opendot.gateway.upstream import list_remote_tools
    from opendot.store import open_store

    store = open_store(config) if server.auth == "oauth" else None
    try:
        names = anyio.run(lambda: list_remote_tools(server, store=store))
    except Exception as exc:  # noqa: BLE001 - shown to the operator
        raise ConnectorCliError(f"cannot reach {server.name}: {exc}") from exc
    finally:
        if store is not None:
            store.close()
    listed = set(names)
    print(f"{server.name} lists {len(names)} tools.")
    missing = [t for t in server.read_tools + server.write_tools if t not in listed]
    for tool in server.read_tools:
        print(f"  read  {tool}{'' if tool in listed else '  (not listed by the server)'}")
    for tool in server.write_tools:
        print(f"  write {tool}{'' if tool in listed else '  (not listed by the server)'}")
    unused = sorted(listed - set(server.read_tools) - set(server.write_tools))
    if unused:
        print(f"  not allowed: {', '.join(unused)}")
    return 1 if missing else 0


def cmd_calls(args: argparse.Namespace) -> int:
    config = _load(args)
    from opendot.store import open_store

    store = open_store(config)
    try:
        calls = store.list_gateway_calls(task_id=args.task, limit=args.limit)
    finally:
        store.close()
    if not calls:
        print("No connector calls.")
        return 0
    for call in calls:
        print(
            f"{call.created_at.isoformat(timespec='seconds')}  task {call.task_id}  "
            f"{call.server}.{call.tool}  {call.mode.value}  {call.status.value}  "
            f"{call.result_bytes} B  {call.duration_ms} ms"
            + (f"  {call.error[:120]}" if call.error else "")
        )
    return 0


def _with_factiq(original):
    def handler(args: argparse.Namespace) -> int:
        code = original(args)
        if code != 0 or not getattr(args, "with_factiq", False):
            return code
        from opendot.__main__ import _config_path

        path = _config_path(args)
        with open(path, "a", encoding="utf-8") as out:
            out.write(FACTIQ_INIT_BLOCK)
        config = Config.load(path)
        server = config.connector("factiq")
        print("Turned on the FactIQ connector.")
        if server is not None and server.instructions is not None:
            from opendot.gateway.instructions import InstructionsError, fetch_factiq_instructions

            try:
                fetch_factiq_instructions(server.instructions)
                print("Downloaded the public FactIQ plugin files (MIT licence).")
            except InstructionsError as exc:
                print(f"Could not download the FactIQ plugin files yet ({exc}).")
                print("A work step will try again.")
        print("Next: create an API key at https://factiq.com/settings/security")
        print("and set FACTIQ_API_KEY on the host,")
        print('or set auth = "oauth" under [factiq] and run `opendot connectors login factiq`.')
        return 0

    return handler


def register_cli(subparsers: argparse._SubParsersAction) -> None:
    init = subparsers.choices.get("init")
    if init is not None:
        original = init.get_default("handler")
        if original is not None:
            init.add_argument(
                "--with-factiq",
                action="store_true",
                help="turn on the FactIQ connector (https://api.factiq.com/mcp)",
            )
            init.set_defaults(handler=_with_factiq(original))

    parser = subparsers.add_parser(
        "connectors",
        help="MCP connectors reached through the host gateway",
        description="MCP connectors reached through the host gateway",
    )
    commands = parser.add_subparsers(dest="connectors_command", metavar="ACTION", required=True)

    p = commands.add_parser("list", help="show connectors and their login state")
    p.set_defaults(handler=cmd_list)

    p = commands.add_parser("login", help="sign in to an oauth connector")
    p.add_argument("name")
    p.add_argument(
        "--no-browser", action="store_true", help="print the address; do not open a browser"
    )
    p.add_argument(
        "--paste",
        action="store_true",
        help="paste the address the browser ends on instead of waiting for it (no local browser)",
    )
    p.add_argument(
        "--port", type=int, default=0, help="local port for the redirect (default: any free port)"
    )
    p.set_defaults(handler=cmd_login)

    p = commands.add_parser("logout", help="delete a stored oauth token")
    p.add_argument("name")
    p.set_defaults(handler=cmd_logout)

    p = commands.add_parser("fetch-instructions", help="download the FactIQ plugin files")
    p.add_argument("name", nargs="?", default="factiq")
    p.add_argument("--force", action="store_true", help="download again even when present")
    p.set_defaults(handler=cmd_fetch_instructions)

    p = commands.add_parser("test", help="connect with the host login and list the tools")
    p.add_argument("name")
    p.set_defaults(handler=cmd_test)

    p = commands.add_parser("calls", help="show logged connector calls")
    p.add_argument("--task", type=int, default=None)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(handler=cmd_calls)


def doctor_checks(config: Config) -> list[tuple[str, bool, str]]:
    """Offline checks only; nothing is created or downloaded."""
    from opendot.gateway.instructions import installed_commit

    results: list[tuple[str, bool, str]] = []
    for server in config.connectors():
        ok, detail = _login_state(config, server)
        results.append((f"connector {server.name} login", ok, detail))
        if server.command:
            results.append(
                (
                    f"connector {server.name} runs on the host",
                    True,
                    f"{server.command[0]} runs as your user, outside the sandbox "
                    "(allow_host_command = true)",
                )
            )
            found = shutil.which(server.command[0])
            results.append(
                (
                    f"connector {server.name} command",
                    found is not None,
                    found or f"{server.command[0]} is not on PATH",
                )
            )
        if server.preset == "factiq" and server.instructions is not None:
            commit = installed_commit(server.instructions)
            if commit == FACTIQ_PLUGIN_COMMIT:
                results.append(("FactIQ plugin files", True, f"at {commit[:12]}"))
            else:
                results.append(
                    (
                        "FactIQ plugin files",
                        False,
                        "run `opendot connectors fetch-instructions` (a step also downloads them)",
                    )
                )
    return results
