"""v0.2 sandbox hooks: host mounts, no_new_privileges, MCP renderers, snap detection."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from opendot.config import Config
from opendot.models import HostMount, HostMountKind, McpServerSpec, Step
from opendot.sandbox import (
    CONTAINER_ARTIFACTS,
    CONTAINER_INSTRUCTIONS,
    CONTAINER_MCP,
    CONTAINER_REPOS,
    SandboxError,
    check_host_mounts,
    check_step_env,
    claude_mcp_allowed_tools,
    claude_mcp_config,
    codex_mcp_overrides,
    detect_snap_docker,
    docker_run_args,
    new_session,
    snap_docker_problems,
)


def make_config(tmp_path: Path, **sections: object) -> Config:
    data = {"core": {"state_root": str(tmp_path / "state")}, **sections}
    cfg = Config.from_dict(data, env={})
    cfg.ensure_directories()
    return cfg


def pairs(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, value in enumerate(args[:-1]) if value == flag]


def make_worktree(cfg: Config, name: str = "app") -> Path:
    path = cfg.worktrees_dir / "task-1" / name
    (path / ".git").mkdir(parents=True)
    return path


def good_mounts(cfg: Config) -> list[HostMount]:
    run_dir = cfg.runs_dir / "task-1" / "abc123"
    (run_dir / "artifacts").mkdir(parents=True)
    (run_dir / "mcp").mkdir()
    instructions = cfg.connectors_dir / "factiq" / "instructions"
    instructions.mkdir(parents=True)
    return [
        HostMount(HostMountKind.WORKTREE, make_worktree(cfg), f"{CONTAINER_REPOS}/app", True),
        HostMount(HostMountKind.ARTIFACTS, run_dir / "artifacts", CONTAINER_ARTIFACTS, True),
        HostMount(HostMountKind.MCP, run_dir / "mcp", CONTAINER_MCP),
        HostMount(HostMountKind.INSTRUCTIONS, instructions, f"{CONTAINER_INSTRUCTIONS}/factiq"),
    ]


def test_good_host_mounts_pass_and_render(tmp_path):
    cfg = make_config(tmp_path)
    mounts = check_host_mounts(Step.WORK, good_mounts(cfg), cfg)
    args = docker_run_args(
        cfg.sandbox,
        name="t",
        command=["true"],
        session=new_session(cfg, "codex"),
        work_writable=True,
        host_mounts=mounts,
    )
    specs = pairs(args, "--mount")
    tree = (cfg.worktrees_dir / "task-1" / "app").resolve()
    assert f"type=bind,src={tree},dst=/opendot/repos/app" in specs
    assert f"type=bind,src={tree / '.git'},dst=/opendot/repos/app/.git,readonly" in specs
    # The .git mount comes after the copy, so it lies on top of it.
    assert specs.index(f"type=bind,src={tree},dst=/opendot/repos/app") < specs.index(
        f"type=bind,src={tree / '.git'},dst=/opendot/repos/app/.git,readonly"
    )
    assert any(s.endswith(f"dst={CONTAINER_MCP},readonly") for s in specs)
    assert any(s.endswith(f"dst={CONTAINER_ARTIFACTS}") for s in specs)


def test_host_mounts_only_in_work_steps(tmp_path):
    cfg = make_config(tmp_path)
    mounts = good_mounts(cfg)
    for step in (Step.REFLECT,):
        with pytest.raises(SandboxError):
            check_host_mounts(step, mounts[:1], cfg)
    assert check_host_mounts(Step.REFLECT, [], cfg) == []


@pytest.mark.parametrize(
    "build",
    [
        # the state root itself, its database folder and the control clones
        lambda cfg: HostMount(HostMountKind.WORKTREE, cfg.state_root, "/opendot/repos/x", True),
        lambda cfg: HostMount(HostMountKind.ARTIFACTS, cfg.logs_dir, CONTAINER_ARTIFACTS, True),
        lambda cfg: HostMount(HostMountKind.WORKTREE, cfg.repos_dir, "/opendot/repos/x", True),
        lambda cfg: HostMount(HostMountKind.ARTIFACTS, cfg.runs_dir, CONTAINER_ARTIFACTS, True),
        # wrong container place
        lambda cfg: HostMount(HostMountKind.WORKTREE, make_worktree(cfg), "/work/app", True),
        lambda cfg: HostMount(HostMountKind.WORKTREE, make_worktree(cfg), "/opendot/repos/a/b"),
        lambda cfg: HostMount(HostMountKind.WORKTREE, make_worktree(cfg), "/opendot/repos/.."),
        # writable where only read-only is allowed
        lambda cfg: HostMount(
            HostMountKind.MCP, _mkdir(cfg.runs_dir / "task-1" / "m"), CONTAINER_MCP, True
        ),
        # a copy without .git
        lambda cfg: HostMount(
            HostMountKind.WORKTREE,
            _mkdir(cfg.worktrees_dir / "task-1" / "x"),
            "/opendot/repos/x",
            True,
        ),
        # missing folder
        lambda cfg: HostMount(
            HostMountKind.ARTIFACTS, cfg.runs_dir / "task-1" / "missing", CONTAINER_ARTIFACTS
        ),
        # not inside a task folder: the transcripts folder, or a copy outside task-<id>
        lambda cfg: HostMount(
            HostMountKind.ARTIFACTS, _mkdir(cfg.runs_dir / "transcripts"), CONTAINER_ARTIFACTS
        ),
        lambda cfg: HostMount(
            HostMountKind.MCP, _mkdir(cfg.runs_dir / "transcripts" / "x"), CONTAINER_MCP
        ),
        # relative host path
        lambda cfg: HostMount(HostMountKind.ARTIFACTS, Path("runs/t"), CONTAINER_ARTIFACTS),
        # escape with ..
        lambda cfg: HostMount(
            HostMountKind.ARTIFACTS, cfg.runs_dir / ".." / "logs", CONTAINER_ARTIFACTS
        ),
    ],
)
def test_bad_host_mounts_are_refused(tmp_path, build):
    cfg = make_config(tmp_path)
    with pytest.raises(SandboxError):
        check_host_mounts(Step.WORK, [build(cfg)], cfg)


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_symbolic_link_below_base_is_refused(tmp_path):
    cfg = make_config(tmp_path)
    outside = _mkdir(tmp_path / "elsewhere")
    link = cfg.runs_dir / "task-9"
    os.symlink(outside, link)
    _mkdir(outside / "artifacts")
    with pytest.raises(SandboxError):
        check_host_mounts(
            Step.WORK,
            [HostMount(HostMountKind.ARTIFACTS, link / "artifacts", CONTAINER_ARTIFACTS)],
            cfg,
        )


def test_state_root_reached_through_a_link_is_fine(tmp_path):
    real = _mkdir(tmp_path / "real")
    os.symlink(real, tmp_path / "linked")
    cfg = Config.from_dict({"core": {"state_root": str(tmp_path / "linked" / "state")}}, env={})
    cfg.ensure_directories()
    artifacts = _mkdir(cfg.runs_dir / "task-1" / "tok" / "artifacts")
    check_host_mounts(
        Step.WORK, [HostMount(HostMountKind.ARTIFACTS, artifacts, CONTAINER_ARTIFACTS, True)], cfg
    )


def test_duplicate_container_paths_are_refused(tmp_path):
    cfg = make_config(tmp_path)
    run = _mkdir(cfg.runs_dir / "task-1" / "tok" / "a")
    other = _mkdir(cfg.runs_dir / "task-1" / "tok" / "b")
    with pytest.raises(SandboxError):
        check_host_mounts(
            Step.WORK,
            [
                HostMount(HostMountKind.ARTIFACTS, run, CONTAINER_ARTIFACTS),
                HostMount(HostMountKind.ARTIFACTS, other, CONTAINER_ARTIFACTS),
            ],
            cfg,
        )


def test_no_new_privileges_off_and_shm_and_no_session(tmp_path):
    cfg = make_config(tmp_path, sandbox={"no_new_privileges": False})
    tree = make_worktree(cfg)
    args = docker_run_args(
        cfg.sandbox,
        name="checks",
        command=["sh", "-c", "true"],
        session=None,
        work_writable=False,
        host_mounts=[HostMount(HostMountKind.WORKTREE, tree, "/opendot/repos/app", True)],
        shm_size="1g",
        workdir="/opendot/repos/app",
    )
    assert pairs(args, "--security-opt") == []
    assert pairs(args, "--cap-drop") == ["ALL"]
    assert pairs(args, "--shm-size") == ["1g"]
    assert pairs(args, "--workdir") == ["/opendot/repos/app"]
    assert not any("dst=/work" in m or "dst=/opendot/cli" in m for m in pairs(args, "--mount"))


def test_step_env_refuses_every_host_login(tmp_path):
    cfg = make_config(tmp_path, factiq={"enabled": True})
    for name in ("OPENDOT_GITHUB_TOKEN", "FACTIQ_API_KEY"):
        with pytest.raises(SandboxError):
            check_step_env(Step.WORK, {name: "x"}, cfg)


SPECS = [
    McpServerSpec(
        name="browser",
        command=("playwright-mcp", "--headless", "--isolated"),
        tools=("browser_navigate", "browser_snapshot"),
    ),
    McpServerSpec(
        name="factiq",
        command=("python3", "/opendot/mcp/bridge.py", "/opendot/mcp/factiq.sock"),
        env={"PYTHONDONTWRITEBYTECODE": "1"},
    ),
]


def test_codex_overrides():
    args = codex_mcp_overrides(SPECS)
    values = [args[i + 1] for i, a in enumerate(args) if a == "-c"]
    assert 'mcp_servers.browser.command="playwright-mcp"' in values
    assert 'mcp_servers.browser.args=["--headless", "--isolated"]' in values
    assert 'mcp_servers.browser.enabled_tools=["browser_navigate", "browser_snapshot"]' in values
    assert 'mcp_servers.browser.default_tools_approval_mode="approve"' in values
    assert "mcp_servers.browser.startup_timeout_sec=30" in values
    assert 'mcp_servers.factiq.env={PYTHONDONTWRITEBYTECODE = "1"}' in values
    assert not any(v.startswith("mcp_servers.factiq.enabled_tools") for v in values)


def test_claude_config_and_allowed_tools():
    config = claude_mcp_config(SPECS)
    assert config["mcpServers"]["browser"] == {
        "type": "stdio",
        "command": "playwright-mcp",
        "args": ["--headless", "--isolated"],
    }
    assert config["mcpServers"]["factiq"]["env"] == {"PYTHONDONTWRITEBYTECODE": "1"}
    assert claude_mcp_allowed_tools(SPECS) == [
        "mcp__browser__browser_navigate",
        "mcp__browser__browser_snapshot",
        "mcp__factiq",
    ]


@pytest.mark.parametrize(
    "spec",
    [
        McpServerSpec(name="a.b", command=("x",)),
        McpServerSpec(name="a__b", command=("x",)),
        McpServerSpec(name="a", command=()),
        McpServerSpec(name="a", command=("x",), env={"HOME": "/"}),
        McpServerSpec(name="a", command=("x",), tools=("bad name",)),
    ],
)
def test_bad_specs_are_refused(spec):
    with pytest.raises(SandboxError):
        codex_mcp_overrides([spec])
    with pytest.raises(SandboxError):
        claude_mcp_config([spec])


def test_detect_snap_docker():
    assert detect_snap_docker("docker", which=lambda _: "/snap/bin/docker")
    assert detect_snap_docker(
        "docker", docker_root_dir="/var/snap/docker/common/var-lib-docker", which=lambda _: None
    )
    assert not detect_snap_docker(
        "docker", docker_root_dir="/var/lib/docker", which=lambda _: "/usr/bin/docker"
    )


def test_snap_docker_problems(tmp_path):
    cfg = make_config(tmp_path)
    assert snap_docker_problems(cfg, is_snap=False) == []
    problems = snap_docker_problems(cfg, is_snap=True)
    assert any("no_new_privileges" in p for p in problems)
    relaxed = make_config(tmp_path, sandbox={"no_new_privileges": False})
    assert [p for p in snap_docker_problems(relaxed, is_snap=True) if "no_new" in p] == []
