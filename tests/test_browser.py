from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import FakeDocker
from opendot import browser
from opendot.config import DEFAULT_BROWSER_TOOLS, Config
from opendot.container_contract import (
    BROWSER_CHECK_URL,
    BROWSER_SMOKE_SCRIPT,
    PLAYWRIGHT_BROWSERS_PATH,
    PLAYWRIGHT_MCP_TOOLS,
    PLAYWRIGHT_MCP_VERSION,
    REQUIRED_DIRECTORIES,
    REQUIRED_EXECUTABLES,
    check_verify_output,
    dockerfile_path,
    playwright_mcp_args,
    verify_image_args,
)
from opendot.extensions import StepContext, StepExtensions
from opendot.models import HostMountKind, Step
from opendot.sandbox import (
    CONTAINER_ARTIFACTS,
    check_host_mounts,
    claude_mcp_allowed_tools,
    claude_mcp_config,
    codex_mcp_overrides,
    docker_run_args,
)


def make_config(
    tmp_path: Path, browser_section: dict[str, Any] | None = None, **sandbox: Any
) -> Config:
    data: dict[str, Any] = {
        "core": {"state_root": str(tmp_path / "state")},
        "backend": {"worker": {"kind": "fake"}, "reviewer": {"kind": "fake"}},
        "sandbox": sandbox,
    }
    if browser_section is not None:
        data["browser"] = browser_section
    cfg = Config.from_dict(data, env={})
    cfg.ensure_directories()
    return cfg


class RecordingStore:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any], int | None]] = []
        self.linked: list[tuple[str, int]] = []

    def log_event(
        self, kind: str, detail: dict[str, Any] | None = None, *, task_id: int | None = None
    ):
        self.events.append((kind, detail or {}, task_id))

    def link_gateway_calls(self, step_token: str, attempt_id: int) -> None:
        self.linked.append((step_token, attempt_id))


def context(
    cfg: Config, store: RecordingStore | None = None, step: Step = Step.WORK
) -> StepContext:
    return StepContext(SimpleNamespace(id=7), step, store or RecordingStore(), cfg, "codex")


def pairs(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, value in enumerate(args) if value == flag]


# ---------------------------------------------------------------------------
# Step extension
# ---------------------------------------------------------------------------


def test_browser_is_off_by_default(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    assert browser.step_extensions(cfg) == []
    assert browser.doctor_checks(cfg) == []
    assert StepExtensions([*browser.step_extensions(cfg)]).begin(context(cfg)) is None


def test_enabled_browser_adds_server_mount_and_shared_memory(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, {"enabled": True, "viewport": "1024x700", "shm_size": "2g"})
    extensions = StepExtensions(browser.step_extensions(cfg))
    ctx = context(cfg)
    plan = extensions.begin(ctx)
    assert plan is not None

    [mount] = plan.host_mounts
    assert mount.kind is HostMountKind.ARTIFACTS
    assert mount.container == CONTAINER_ARTIFACTS and mount.writable
    assert mount.host == plan.run_dir / "artifacts"
    assert mount.host.is_dir() and (mount.host.stat().st_mode & 0o777) == 0o700
    assert check_host_mounts(Step.WORK, plan.host_mounts, cfg) == plan.host_mounts

    [spec] = plan.mcp_servers
    assert spec.name == "browser"
    assert spec.command[0] == "playwright-mcp"
    assert "--headless" in spec.command and "--isolated" in spec.command
    assert "--no-webmcp" in spec.command
    assert pairs(list(spec.command), "--viewport-size") == ["1024x700"]
    assert pairs(list(spec.command), "--output-dir") == [CONTAINER_ARTIFACTS]
    assert "--allowed-origins" not in spec.command
    assert spec.env == {"PLAYWRIGHT_BROWSERS_PATH": PLAYWRIGHT_BROWSERS_PATH}
    assert spec.tools == tuple(DEFAULT_BROWSER_TOOLS)
    assert plan.shm_size == "2g"
    assert any(CONTAINER_ARTIFACTS in note for note in plan.prompt_notes)
    extensions.finish(ctx, plan, None)


def test_review_steps_get_no_browser(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, {"enabled": True})
    assert (
        StepExtensions(browser.step_extensions(cfg)).begin(context(cfg, step=Step.REVIEW)) is None
    )


def test_allowed_origins_are_joined_for_playwright(tmp_path: Path) -> None:
    cfg = make_config(
        tmp_path,
        {"enabled": True, "allowed_origins": ["https://example.com", "https://example.org"]},
    )
    spec = browser.browser_server_spec(cfg)
    assert pairs(list(spec.command), "--allowed-origins") == [
        "https://example.com;https://example.org"
    ]
    assert "https://example.org" in browser.browser_prompt_note(cfg)


def test_spec_works_with_both_cli_formats(tmp_path: Path) -> None:
    cfg = make_config(
        tmp_path, {"enabled": True, "tools": ["browser_navigate", "browser_snapshot"]}
    )
    spec = browser.browser_server_spec(cfg)
    overrides = codex_mcp_overrides([spec])
    assert 'mcp_servers.browser.command="playwright-mcp"' in overrides
    assert 'mcp_servers.browser.enabled_tools=["browser_navigate", "browser_snapshot"]' in overrides
    assert 'mcp_servers.browser.default_tools_approval_mode="approve"' in overrides
    assert claude_mcp_allowed_tools([spec]) == [
        "mcp__browser__browser_navigate",
        "mcp__browser__browser_snapshot",
    ]
    entry = claude_mcp_config([spec])["mcpServers"]["browser"]
    assert entry["command"] == "playwright-mcp"
    assert entry["env"] == {"PLAYWRIGHT_BROWSERS_PATH": PLAYWRIGHT_BROWSERS_PATH}


def test_plan_reaches_the_docker_command(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, {"enabled": True})
    ctx = context(cfg)
    extensions = StepExtensions(browser.step_extensions(cfg))
    plan = extensions.begin(ctx)
    assert plan is not None
    args = docker_run_args(
        cfg.sandbox,
        name="opendot-test",
        command=["true"],
        session=None,
        work_writable=False,
        host_mounts=plan.host_mounts,
        shm_size=plan.shm_size,
        workdir="/opendot/home",
    )
    assert pairs(args, "--shm-size") == ["1g"]
    mount = f"type=bind,src={(plan.run_dir / 'artifacts').resolve()},dst={CONTAINER_ARTIFACTS}"
    assert mount in pairs(args, "--mount")
    extensions.finish(ctx, plan, None)


def test_after_step_records_the_saved_files(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, {"enabled": True})
    store = RecordingStore()
    ctx = context(cfg, store)
    extensions = StepExtensions(browser.step_extensions(cfg))
    plan = extensions.begin(ctx)
    assert plan is not None
    (plan.run_dir / "artifacts" / "page.png").write_bytes(b"png")
    extensions.finish(ctx, plan, SimpleNamespace(id=3))
    assert store.linked == [(plan.step_token, 3)]
    [(kind, detail, task_id)] = store.events
    assert kind == "browser.artifacts" and task_id == 7
    assert detail["files"] == ["page.png"]
    assert (plan.run_dir / "artifacts" / "page.png").exists()


def test_after_step_with_no_files_logs_nothing(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, {"enabled": True})
    store = RecordingStore()
    ctx = context(cfg, store)
    extensions = StepExtensions(browser.step_extensions(cfg))
    plan = extensions.begin(ctx)
    extensions.finish(ctx, plan, None)
    assert store.events == []


# ---------------------------------------------------------------------------
# Doctor and the check command
# ---------------------------------------------------------------------------


def test_doctor_checks_pass_for_defaults(tmp_path: Path) -> None:
    results = browser.doctor_checks(make_config(tmp_path, {"enabled": True}))
    assert results and all(ok for _, ok, _ in results)


def test_doctor_flags_unknown_tools_and_no_network(tmp_path: Path) -> None:
    cfg = make_config(
        tmp_path,
        {"enabled": True, "tools": ["browser_navigate", "browser_teleport"]},
        network="none",
    )
    failed = {name: detail for name, ok, detail in browser.doctor_checks(cfg) if not ok}
    assert "browser_teleport" in failed["browser tools"]
    assert "none" in failed["browser network"]


def test_default_tools_are_offered_by_the_pinned_server() -> None:
    assert set(DEFAULT_BROWSER_TOOLS) <= PLAYWRIGHT_MCP_TOOLS
    for risky in ("browser_evaluate", "browser_run_code_unsafe", "browser_file_upload"):
        assert risky not in DEFAULT_BROWSER_TOOLS


def test_check_args_follow_the_sandbox_config(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, {"enabled": True}, no_new_privileges=False, image="example/step:1")
    args = browser.browser_check_args(cfg, "https://example.com")
    assert "--security-opt" not in args
    assert pairs(args, "--shm-size") == ["1g"]
    assert pairs(args, "--cap-drop") == ["ALL"] and "--read-only" in args
    assert not any("dst=/work" in m for m in pairs(args, "--mount"))
    image_at = args.index("example/step:1")
    assert args[image_at + 1 : image_at + 3] == ["sh", "-c"]
    assert "https://example.com" in args[image_at + 3]
    hardened = browser.browser_check_args(make_config(tmp_path / "b", {"enabled": True}), "x")
    assert pairs(hardened, "--security-opt") == ["no-new-privileges"]


def test_run_browser_check_reads_the_result_line(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, {"enabled": True})
    docker = FakeDocker()
    docker.script(0, "browser ok Example Domain\n")
    ok, message = browser.run_browser_check(cfg, "https://example.com", docker)
    assert ok and "Example Domain" in message
    assert docker.runs[0].input == ""
    docker.script(0, "browser failed net::ERR_NAME_NOT_RESOLVED\n")
    ok, message = browser.run_browser_check(cfg, "https://example.com", docker)
    assert not ok and "ERR_NAME_NOT_RESOLVED" in message
    docker.script(125, "", "docker: Error response from daemon")
    ok, message = browser.run_browser_check(cfg, "https://example.com", docker)
    assert not ok and "125" in message


def test_cli_registers_browser_check(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg_path = tmp_path / "opendot.toml"
    cfg_path.write_text(f'[core]\nstate_root = "{tmp_path / "state"}"\n[browser]\nenabled = true\n')
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    browser.register_cli(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        ["--config", str(cfg_path), "browser", "check", "--url", "https://example.org"]
    )
    assert args.url == "https://example.org"
    docker = FakeDocker()
    docker.script(0, "browser ok Example Domain\n")
    monkeypatch.setattr(browser, "_runner", lambda: docker)
    assert args.handler(args) == 0
    assert "Example Domain" in capsys.readouterr().out
    docker.script(0, "browser failed timeout\n")
    assert args.handler(args) == 1


# ---------------------------------------------------------------------------
# Image contract
# ---------------------------------------------------------------------------


def test_dockerfile_pins_the_browser_and_strips_setuid() -> None:
    text = dockerfile_path().read_text()
    assert f"@playwright/mcp@{PLAYWRIGHT_MCP_VERSION}" in text
    assert f"PLAYWRIGHT_BROWSERS_PATH={PLAYWRIGHT_BROWSERS_PATH}" in text
    assert "find / -xdev -perm /6000 -type f -exec chmod a-s {} +" in text
    last_user = [line for line in text.splitlines() if line.startswith("USER ")][-1]
    assert last_user == "USER 10001:10001"
    assert text.index("chmod a-s") < text.index(last_user)
    assert "playwright-mcp" in REQUIRED_EXECUTABLES


def test_playwright_args_skip_blank_origins() -> None:
    args = playwright_mcp_args(viewport="800x600", allowed_origins=[" ", ""], output_dir="/o")
    assert "--allowed-origins" not in args


def test_smoke_script_is_valid_python() -> None:
    compile(BROWSER_SMOKE_SCRIPT, "smoke", "exec")


def test_verify_image_runs_the_browser_offline() -> None:
    args = verify_image_args("docker", "img:1")
    assert pairs(args, "--network") == ["none"]
    assert "--security-opt" not in args
    assert any(value.startswith("/tmp:") for value in pairs(args, "--tmpfs"))
    assert "--shm-size" in args
    assert BROWSER_CHECK_URL.split("<")[0] in args[-1]
    assert "-perm /6000" in args[-1]


def good_output(*extra: str) -> str:
    lines = ["uid=10001"]
    lines += [f"tool {t} /usr/bin/{t}" for t in REQUIRED_EXECUTABLES]
    lines += [f"dir {d} ok" for d in REQUIRED_DIRECTORIES]
    return "\n".join([*lines, *extra])


def test_verify_output_browser_and_setuid_lines() -> None:
    assert check_verify_output(0, good_output("browser ok t"), require_browser=True).ok
    missing = check_verify_output(0, good_output(), require_browser=True)
    assert not missing.ok and any("did not report" in p for p in missing.problems)
    assert check_verify_output(0, good_output()).ok
    failed = check_verify_output(0, good_output("browser failed no chromium"))
    assert not failed.ok and any("no chromium" in p for p in failed.problems)
    setuid = check_verify_output(0, good_output("setuid /usr/bin/su", "browser ok t"))
    assert not setuid.ok and any("/usr/bin/su" in p for p in setuid.problems)


# ---------------------------------------------------------------------------
# Live check against a built image (skipped unless OPENDOT_TEST_IMAGE is set)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("OPENDOT_TEST_IMAGE") or shutil.which("docker") is None,
    reason="set OPENDOT_TEST_IMAGE to a built step image to run the live browser check",
)
def test_live_image_opens_a_page_offline() -> None:
    args = verify_image_args("docker", os.environ["OPENDOT_TEST_IMAGE"])
    result = subprocess.run(args, capture_output=True, text=True, timeout=300, check=False)
    check = check_verify_output(result.returncode, result.stdout, require_browser=True)
    assert check.ok, check.problems
