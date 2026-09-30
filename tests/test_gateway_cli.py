"""`opendot connectors ...`, `opendot init --with-factiq` and the gateway's doctor checks."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import opendot.__main__ as main_module
import opendot.gateway.instructions as instructions_module
from opendot.config import FACTIQ_PLUGIN_COMMIT, Config, ConfigError
from opendot.gateway import cli as gateway_cli
from opendot.models import GatewayCallStatus, GatewayMode
from opendot.store import open_store
from test_gateway_instructions import make_archive
from test_gateway_server import fake_factory


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    for name in list(os.environ):
        if name.startswith("OPENDOT_") or name == "FACTIQ_API_KEY":
            monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.fixture
def downloads(monkeypatch) -> list[str]:
    urls: list[str] = []

    def download(url: str) -> bytes:
        urls.append(url)
        return make_archive()

    monkeypatch.setattr(instructions_module, "_download", download)
    return urls


def run(home: Path, *argv: str) -> int:
    """Like opendot.__main__.main; build_parser adds the gateway's commands."""
    parser = main_module.build_parser()
    args = parser.parse_args(["--config", str(home / "opendot.toml"), *argv])
    try:
        return args.handler(args)
    except (ConfigError, ValueError) as exc:
        print(f"opendot: {exc}")
        return 1


def init(home: Path, *extra: str) -> int:
    return run(home, "init", "--demo", "--state-root", str(home / "state"), *extra)


def load(home: Path) -> Config:
    return Config.load(home / "opendot.toml")


def test_init_with_factiq_turns_on_the_preset(home, downloads, capsys):
    assert init(home, "--with-factiq") == 0
    out = capsys.readouterr().out
    assert "factiq.com/settings/security" in out
    assert "opendot connectors login factiq" in out
    config = load(home)
    [server] = config.connectors()
    assert server.name == "factiq" and server.url == "https://api.factiq.com/mcp"
    assert server.auth == "bearer_env" and server.auth_env == "FACTIQ_API_KEY"
    assert len(downloads) == 1
    assert instructions_module.installed_commit(server.instructions) == FACTIQ_PLUGIN_COMMIT


def test_init_with_factiq_still_works_offline(home, monkeypatch, capsys):
    def offline(url: str) -> bytes:
        raise OSError("network is unreachable")

    monkeypatch.setattr(instructions_module, "_download", offline)
    assert init(home, "--with-factiq") == 0
    assert "Could not download" in capsys.readouterr().out
    assert load(home).connector("factiq") is not None


def test_init_without_the_flag_adds_no_connector(home, downloads):
    assert init(home) == 0
    assert load(home).connectors() == []
    assert downloads == []


def test_connectors_list_and_doctor(home, downloads, monkeypatch, capsys):
    assert init(home, "--with-factiq") == 0
    capsys.readouterr()
    assert run(home, "connectors", "list") == 0
    out = capsys.readouterr().out
    assert "factiq  https://api.factiq.com/mcp" in out
    assert "login: missing (set FACTIQ_API_KEY on the host)" in out
    assert f"instructions: at {FACTIQ_PLUGIN_COMMIT[:12]}" in out

    checks = {name: (ok, detail) for name, ok, detail in gateway_cli.doctor_checks(load(home))}
    assert checks["connector factiq login"][0] is False
    assert checks["FactIQ plugin files"][0] is True
    monkeypatch.setenv("FACTIQ_API_KEY", "fiq-test-key")
    checks = {name: ok for name, ok, _ in gateway_cli.doctor_checks(load(home))}
    assert checks["connector factiq login"] is True


def test_doctor_checks_a_stdio_command_and_an_oauth_token(home):
    (home / "opendot.toml").write_text(
        f"""
[core]
state_root = "{home / "state"}"

[[mcp_servers]]
name = "local"
command = ["opendot-no-such-command-xyz", "--stdio"]
allow_host_command = true
tools = [{{ name = "search", mode = "read" }}]

[[mcp_servers]]
name = "remote"
url = "https://mcp.example.com/mcp"
auth = "oauth"
tools = [{{ name = "search", mode = "read" }}]
""",
        encoding="utf-8",
    )
    config = load(home)
    checks = {name: (ok, detail) for name, ok, detail in gateway_cli.doctor_checks(config)}
    assert checks["connector local command"][0] is False
    assert checks["connector local runs on the host"][0] is True
    assert "outside the sandbox" in checks["connector local runs on the host"][1]
    assert checks["connector local login"][0] is True
    assert checks["connector remote login"] == (False, "run `opendot connectors login remote`")
    assert not config.db_path.exists()  # doctor creates nothing

    store = open_store(config)
    store.save_connector_token("remote", "token")
    store.close()
    checks = {name: ok for name, ok, _ in gateway_cli.doctor_checks(config)}
    assert checks["connector remote login"] is True

    assert run(home, "connectors", "logout", "remote") == 0
    checks = {name: ok for name, ok, _ in gateway_cli.doctor_checks(config)}
    assert checks["connector remote login"] is False


def test_unknown_connector_is_an_error(home, capsys):
    assert init(home) == 0
    capsys.readouterr()
    assert run(home, "connectors", "logout", "nope") == 1
    assert "no connector named 'nope'" in capsys.readouterr().out


def test_fetch_instructions_command(home, downloads, capsys):
    assert init(home, "--with-factiq") == 0
    assert run(home, "connectors", "fetch-instructions") == 0
    assert len(downloads) == 1  # already there
    assert run(home, "connectors", "fetch-instructions", "--force") == 0
    assert len(downloads) == 2


def test_test_command_lists_tools(home, monkeypatch, capsys):
    (home / "opendot.toml").write_text(
        f"""
[core]
state_root = "{home / "state"}"

[[mcp_servers]]
name = "fake"
url = "https://mcp.example.com/mcp"
tools = [{{ name = "search", mode = "read" }}, {{ name = "submit", mode = "write" }}]
""",
        encoding="utf-8",
    )
    factory = fake_factory()

    async def fake_list(server, auth=None, *, store=None, env=None):
        async with factory(server) as session:
            result = await session.list_tools()
            return [t.name for t in result.tools]

    import opendot.gateway.upstream as upstream_module

    monkeypatch.setattr(upstream_module, "list_remote_tools", fake_list)
    assert run(home, "connectors", "test", "fake") == 0
    out = capsys.readouterr().out
    assert "fake lists 6 tools." in out
    assert "not allowed: big, boom, hidden, slow" in out


def test_calls_command(home, capsys):
    assert init(home) == 0
    store = open_store(load(home))
    task = store.create_task(text="t", requester="alice", channel="cli", conversation="local")
    store.log_gateway_call(
        task.id,
        "tok",
        server="fake",
        tool="search",
        mode=GatewayMode.READ,
        arguments={"query": "x"},
        status=GatewayCallStatus.OK,
        result_bytes=12,
        result_preview="found x",
        error=None,
        duration_ms=5,
    )
    store.close()
    capsys.readouterr()
    assert run(home, "connectors", "calls", "--task", str(task.id)) == 0
    out = capsys.readouterr().out
    assert "fake.search  read  ok  12 B  5 ms" in out
