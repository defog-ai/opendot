"""The gateway step extension and the in-container bridge script."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from opendot.config import Config
from opendot.extensions import StepContext, StepExtensions
from opendot.gateway.step import GatewayExtension, prompt_note, step_extensions
from opendot.models import GatewayCallStatus, HostMountKind, Step
from opendot.sandbox import check_host_mounts
from opendot.store import open_store
from test_gateway_instructions import make_archive
from test_gateway_server import LineClient, fake_factory


def connector_config(state_root, **extra) -> Config:
    data = {
        "core": {"state_root": str(state_root)},
        "backend": {"worker": {"kind": "fake"}, "reviewer": {"kind": "fake"}},
        "mcp_servers": [
            {
                "name": "fake",
                "url": "https://mcp.example.com/mcp",
                "tools": [
                    {"name": "search", "mode": "read"},
                    {"name": "submit", "mode": "write"},
                ],
            }
        ],
    }
    data.update(extra)
    config = Config.from_dict(data, env={})
    config.ensure_directories()
    return config


@pytest.fixture
def gw_config(state_root):
    return connector_config(state_root)


@pytest.fixture
def gw_store(gw_config):
    store = open_store(gw_config)
    yield store
    store.close()


@pytest.fixture
def factiq_archive() -> bytes:
    return make_archive()


def _ctx(store, config):
    task = store.create_task(text="t", requester="alice", channel="cli", conversation="local")
    return StepContext(task=task, step=Step.WORK, store=store, config=config, backend_kind="fake")


def test_no_connectors_means_no_extension(config):
    assert step_extensions(config) == []


def test_step_extension_builds_a_valid_plan_and_cleans_up(gw_config, gw_store):
    ext = GatewayExtension(gw_config, upstream_factory=fake_factory())
    exts = StepExtensions([ext])
    ctx = _ctx(gw_store, gw_config)
    plan = exts.begin(ctx)
    assert plan is not None
    folder = plan.run_dir / "mcp"
    try:
        assert check_host_mounts(Step.WORK, plan.host_mounts, gw_config) == plan.host_mounts
        [mount] = plan.host_mounts
        assert mount.kind is HostMountKind.MCP
        assert mount.container == "/opendot/mcp"
        assert not mount.writable
        [spec] = plan.mcp_servers
        assert spec.name == "fake"
        assert spec.command == ("python3", "/opendot/mcp/bridge.py", "/opendot/mcp/fake.sock")
        assert spec.tools == ("search",)
        assert (folder / "bridge.py").is_file()
        assert (folder / "fake.sock").exists()
        assert "mcp.fake.submit" in plan.prompt_notes[-1]

        client = LineClient(folder / "fake.sock")
        result = client.call("search", {"query": "gdp"})
        assert result["content"][0]["text"] == "found gdp"
        client.close()
    finally:
        attempt = gw_store.start_attempt(ctx.task.id, Step.WORK, "fake", run_path=plan.run_dir)
        exts.finish(ctx, plan, attempt)
    assert not (folder / "fake.sock").exists()
    [row] = gw_store.list_gateway_calls(task_id=ctx.task.id)
    assert row.status == GatewayCallStatus.OK
    assert row.step_token == plan.step_token
    assert row.attempt_id == attempt.id


def test_factiq_instructions_are_mounted(state_root, factiq_archive):
    config = Config.from_dict(
        {
            "core": {"state_root": str(state_root)},
            "backend": {"worker": {"kind": "fake"}, "reviewer": {"kind": "fake"}},
            "factiq": {"enabled": True},
        },
        env={},
    )
    config.ensure_directories()
    store = open_store(config)
    fetched: list[str] = []

    def download(url: str) -> bytes:
        fetched.append(url)
        return factiq_archive

    ext = GatewayExtension(config, upstream_factory=fake_factory(), download=download)
    exts = StepExtensions([ext])
    ctx = _ctx(store, config)
    plan = exts.begin(ctx)
    try:
        assert len(fetched) == 1
        assert fetched[0].startswith("https://github.com/defog-ai/factiq-plugin/archive/")
        check_host_mounts(Step.WORK, plan.host_mounts, config)
        kinds = {m.kind: m for m in plan.host_mounts}
        instructions = kinds[HostMountKind.INSTRUCTIONS]
        assert instructions.container == "/opendot/instructions/factiq"
        assert (instructions.host / "skills" / "factiq" / "SKILL.md").is_file()
        assert (instructions.host / "SOURCE.md").is_file()
        note = plan.prompt_notes[-1]
        assert "/opendot/instructions/factiq/skills/factiq/SKILL.md" in note
        assert "LICENSE" in note
        [spec] = plan.mcp_servers
        assert "get_series" in spec.tools
        assert "send_feedback" not in spec.tools
    finally:
        exts.finish(ctx, plan, None)
    # A second step uses the files already there.
    plan = exts.begin(ctx)
    exts.finish(ctx, plan, None)
    assert len(fetched) == 1
    store.close()


def test_instructions_download_failure_skips_the_mount(state_root):
    config = Config.from_dict(
        {
            "core": {"state_root": str(state_root)},
            "backend": {"worker": {"kind": "fake"}, "reviewer": {"kind": "fake"}},
            "factiq": {"enabled": True},
        },
        env={},
    )
    config.ensure_directories()
    store = open_store(config)

    def download(url: str) -> bytes:
        raise OSError("network is down")

    exts = StepExtensions(
        [GatewayExtension(config, upstream_factory=fake_factory(), download=download)]
    )
    ctx = _ctx(store, config)
    plan = exts.begin(ctx)
    try:
        assert [m.kind for m in plan.host_mounts] == [HostMountKind.MCP]
        assert "/opendot/instructions" not in plan.prompt_notes[-1]
    finally:
        exts.finish(ctx, plan, None)
    events = [e.kind for e in store.list_events(task_id=ctx.task.id)]
    assert "connector.instructions_unavailable" in events
    store.close()


def test_prompt_note_names_tools_and_actions(gw_config):
    [server] = gw_config.connectors()
    note = prompt_note([server], {})
    assert "read tools search" in note
    assert "mcp.fake.submit" in note
    assert "/opendot/mcp/fake.write-tools.json" in note


def test_bridge_script_relays_json_rpc(gw_config, gw_store):
    ext = GatewayExtension(gw_config, upstream_factory=fake_factory())
    exts = StepExtensions([ext])
    ctx = _ctx(gw_store, gw_config)
    plan = exts.begin(ctx)
    folder = plan.run_dir / "mcp"
    try:
        lines = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "search", "arguments": {"query": "trade"}},
            },
        ]
        # A relative socket path keeps the name short; inside the container it is
        # /opendot/mcp/<name>.sock.
        proc = subprocess.run(
            [sys.executable, str(folder / "bridge.py"), "fake.sock"],
            input="".join(json.dumps(line) + "\n" for line in lines).encode(),
            cwd=folder,
            capture_output=True,
            timeout=60,
        )
    finally:
        exts.finish(ctx, plan, None)
    assert proc.returncode == 0, proc.stderr
    replies = {r["id"]: r for r in map(json.loads, proc.stdout.decode().splitlines())}
    assert replies[1]["result"]["serverInfo"]["name"] == "opendot-fake"
    assert [t["name"] for t in replies[2]["result"]["tools"]] == ["search"]
    assert replies[3]["result"]["content"][0]["text"] == "found trade"


def test_bridge_reports_a_missing_socket(tmp_path):
    from opendot.gateway import bridge

    proc = subprocess.run(
        [sys.executable, bridge.__file__, "missing.sock"],
        cwd=tmp_path,
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 1
    assert proc.stderr
    proc = subprocess.run([sys.executable, bridge.__file__], capture_output=True, timeout=30)
    assert proc.returncode == 2
