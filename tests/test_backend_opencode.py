from __future__ import annotations

import json
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

import opendot.__main__ as cli
from conftest import FakeDocker, FakeProcess
from opendot.backends import BackendError, StepInterrupted
from opendot.backends.opencode import (
    OpencodeBackend,
    last_json_object,
    opencode_args,
    opencode_settings,
    prompt_with_schema,
    provider_of,
)
from opendot.config import Config, ConfigError
from opendot.models import McpServerSpec, Step, StepPlan

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}}
MODEL = "someprovider/some-model"
API_KEY = "sk-or-v1-" + "k" * 48
OTHER_KEY = "sk-other-" + "o" * 48
SESSION = "ses_0123456789abcdefABCDEF"


def make_config(tmp_path: Path, logins: dict[str, Any] | None = None, **sandbox: Any) -> Config:
    auth = tmp_path / "auth.json"
    if logins is None:
        logins = {
            "someprovider": {"type": "api", "key": API_KEY},
            "otherprovider": {"type": "api", "key": OTHER_KEY},
        }
    auth.write_text(json.dumps(logins))
    cfg = Config.from_dict(
        {
            "core": {"state_root": str(tmp_path / "state")},
            "backend": {
                "worker": {"kind": "opencode", "model": MODEL},
                "opencode": {"auth_file": str(auth)},
            },
            "sandbox": sandbox,
        },
        env={},
    )
    cfg.ensure_directories()
    return cfg


def backend(cfg: Config, docker: FakeDocker, role: str = "worker") -> OpencodeBackend:
    return OpencodeBackend(cfg, role, docker, poll_seconds=0.01, exit_grace_seconds=0.1)


def events(
    session_id: str = SESSION, answer: str = '{"answer": "ok"}', *, before: str = ""
) -> list[str]:
    """A hand-written `opencode run --format json` transcript: two model steps."""

    def event(kind: str, part: dict[str, Any]) -> str:
        return json.dumps({"type": kind, "sessionID": session_id, "part": part})

    finish = {"tokens": {"input": 100, "output": 20, "reasoning": 5, "cache": {"read": 1000}}}
    return [
        event("step_start", {}),
        event("text", {"text": "Let me look. {not json"}),
        event("tool_use", {"tool": "bash", "state": {"status": "completed"}}),
        event("step_finish", finish),
        event("step_start", {}),
        event("text", {"text": f"{before}Done.\n```json\n{answer}\n```"}),
        event("step_finish", finish),
    ]


def after(args: Sequence[str], flag: str) -> str:
    return args[list(args).index(flag) + 1]


def command_of(process: FakeProcess) -> list[str]:
    return process.args[process.args.index("opencode") :]


def env_value(args: Sequence[str], name: str) -> str:
    for index, arg in enumerate(args):
        if arg == "--env" and args[index + 1].startswith(f"{name}="):
            return args[index + 1].split("=", 1)[1]
    raise AssertionError(f"{name} not set")


def cli_folder(args: Sequence[str]) -> Path:
    for arg in args:
        if arg.endswith(",dst=/opendot/cli"):
            return Path(arg.split("src=", 1)[1].split(",dst=", 1)[0])
    raise AssertionError("no CLI folder mount")


class _WatchingDocker(FakeDocker):
    """Records the staged login at spawn time, and can let the step rewrite it."""

    def __init__(self, rewrite: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.rewrite = rewrite
        self.staged: list[dict[str, Any]] = []
        self.staged_modes: list[int] = []

    def spawn(self, args: Sequence[str], *, env: Mapping[str, str] | None = None) -> FakeProcess:
        login = cli_folder(args) / "data" / "opencode" / "auth.json"
        self.staged.append(json.loads(login.read_text()))
        self.staged_modes.append(stat.S_IMODE(login.stat().st_mode))
        if self.rewrite is not None:
            login.write_text(json.dumps(self.rewrite))
        return super().spawn(args, env=env)


# -- pure helpers -------------------------------------------------------------


def test_provider_of() -> None:
    assert provider_of("opencode-go/deepseek-v4-flash") == "opencode-go"
    assert provider_of("openrouter/anthropic/claude-x") == "openrouter"
    for bad in ("", "gpt-5", "/model", "provider/"):
        with pytest.raises(BackendError, match="provider/model"):
            provider_of(bad)


def test_settings_allow_tools_only_for_work() -> None:
    assert opencode_settings(Step.WORK)["permission"] == {"*": "allow"}
    assert opencode_settings(Step.REFLECT)["permission"] == {"*": "deny"}
    assert opencode_settings(Step.REFLECT)["permission"] == {"*": "deny"}
    settings = opencode_settings(Step.WORK)
    assert settings["share"] == "disabled"
    assert settings["autoupdate"] is False


BROWSER = McpServerSpec(
    name="browser", command=("playwright-mcp", "--headless"), env={}, tools=("browser_navigate",)
)
GATEWAY = McpServerSpec(
    name="gateway",
    command=("python3", "/opendot/mcp/bridge.py"),
    env={"OPENDOT_GATEWAY": "/opendot/mcp/gateway.sock"},
    tools=(),
    tool_timeout_seconds=90,
)


def test_settings_give_mcp_servers_and_hide_unlisted_tools() -> None:
    settings = opencode_settings(Step.WORK, [BROWSER, GATEWAY])
    assert settings["mcp"] == {
        "browser": {
            "type": "local",
            "command": ["playwright-mcp", "--headless"],
            "enabled": True,
            "timeout": 120000,
        },
        "gateway": {
            "type": "local",
            "command": ["python3", "/opendot/mcp/bridge.py"],
            "enabled": True,
            "timeout": 90000,
            "environment": {"OPENDOT_GATEWAY": "/opendot/mcp/gateway.sock"},
        },
    }
    # opencode applies the last matching rule, so the deny must come before the allow.
    assert list(settings["permission"].items()) == [
        ("*", "allow"),
        ("browser_*", "deny"),
        ("browser_browser_navigate", "allow"),
    ]


def test_settings_refuse_mcp_outside_work_steps_and_overlapping_names() -> None:
    with pytest.raises(BackendError, match="no MCP servers"):
        opencode_settings(Step.REFLECT, [BROWSER])
    overlap = McpServerSpec(name="browser_extra", command=("x",), env={}, tools=())
    with pytest.raises(BackendError, match="overlap"):
        opencode_settings(Step.WORK, [BROWSER, overlap])
    bad = McpServerSpec(name="bad name", command=("x",), env={}, tools=())
    with pytest.raises(BackendError, match="bad MCP server name"):
        opencode_settings(Step.WORK, [bad])


def test_work_step_uses_the_step_plan(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = _WatchingDocker()
    docker.script_process(events())
    plan = StepPlan(
        step_token="abc123abc123",
        run_dir=tmp_path / "run",
        host_mounts=[],
        mcp_servers=[BROWSER],
        fixed_env={"PLAYWRIGHT_BROWSERS_PATH": "/opt/ms-playwright"},
        prompt_notes=[],
        shm_size="1g",
    )
    backend(cfg, docker, "worker").run_step(Step.WORK, "work", SCHEMA, {}, [], plan=plan)
    process = docker.spawned[0]
    settings = json.loads(env_value(process.args, "OPENCODE_CONFIG_CONTENT"))
    assert settings["mcp"]["browser"]["command"] == ["playwright-mcp", "--headless"]
    assert env_value(process.args, "PLAYWRIGHT_BROWSERS_PATH") == "/opt/ms-playwright"
    assert after(process.args, "--shm-size") == "1g"


def test_command_line() -> None:
    args = opencode_args(MODEL)
    assert args[:2] == ["opencode", "run"]
    assert after(args, "--format") == "json"
    assert "--pure" in args
    assert after(args, "--model") == MODEL
    assert after(args, "--dir") == "/work"
    assert "--session" not in args
    assert after(opencode_args(MODEL, SESSION), "--session") == SESSION


def test_prompt_carries_the_schema() -> None:
    text = prompt_with_schema("Do the thing.\n", SCHEMA)
    assert text.startswith("Do the thing.\n\n## Output format")
    fenced = text.split("```json\n", 1)[1].split("```", 1)[0]
    assert json.loads(fenced) == SCHEMA


def test_last_json_object() -> None:
    assert last_json_object('first {"a": 1} then {"b": {"c": 2}} end') == {"b": {"c": 2}}
    assert last_json_object('```json\n{"answer": "x"}\n```') == {"answer": "x"}
    assert last_json_object("a { broken and [1, 2]") is None
    assert last_json_object('{"a": 1} [3]') == {"a": 1}


# -- run_step -----------------------------------------------------------------


def test_happy_path(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = _WatchingDocker()
    docker.script_process(events())
    result = backend(cfg, docker).run_step(Step.REFLECT, "reflect on this", SCHEMA, {}, [])
    process = docker.spawned[0]

    assert result.output == {"answer": "ok"}
    assert result.thread_id == SESSION
    assert result.usage == {"turns": 2, "input_tokens": 2200, "output_tokens": 50}

    assert process.stdin.written.startswith("reflect on this\n\n## Output format")
    assert process.stdin.closed_by_caller
    assert command_of(process) == opencode_args(MODEL)
    settings = json.loads(env_value(process.args, "OPENCODE_CONFIG_CONTENT"))
    assert settings["permission"] == {"*": "deny"}
    assert env_value(process.args, "XDG_DATA_HOME") == "/opendot/cli/data"
    # Only the login of the model's provider reaches the container, as a 0600 file.
    assert docker.staged == [{"someprovider": {"type": "api", "key": API_KEY}}]
    assert docker.staged_modes == [0o600]
    assert process.env == {}
    assert not any(API_KEY in a or OTHER_KEY in a for a in process.args)
    mounts = [a for a in process.args if "dst=/work" in a]
    assert mounts and not mounts[0].endswith(",readonly")
    # The session folder is named after opencode's id and holds no login afterwards.
    session = cfg.state_root / "sessions" / "opencode" / SESSION
    assert session.is_dir()
    assert not (session / "cli" / "data" / "opencode" / "auth.json").exists()


def test_resume_passes_the_session(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = _WatchingDocker()
    docker.script_process(events())
    docker.script_process(events())
    first = backend(cfg, docker).run_step(Step.REFLECT, "p", SCHEMA, {}, [])
    second = backend(cfg, docker).run_step(
        Step.REFLECT, "again", SCHEMA, {}, [], resume_id=first.thread_id
    )
    assert after(docker.spawned[1].args, "--session") == SESSION
    assert cli_folder(docker.spawned[1].args).parent.name == SESSION
    assert second.thread_id == SESSION


def test_work_step_gets_tools_env_and_a_writable_folder(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, env_allowlist=["TOOL_SETTING"])
    docker = _WatchingDocker()
    docker.script_process(events())
    backend(cfg, docker, "worker").run_step(
        Step.WORK, "work", SCHEMA, {"TOOL_SETTING": "value-of-setting-2"}, []
    )
    process = docker.spawned[0]
    settings = json.loads(env_value(process.args, "OPENCODE_CONFIG_CONTENT"))
    assert settings["permission"] == {"*": "allow"}
    assert process.env == {"TOOL_SETTING": "value-of-setting-2"}
    assert not any("value-of-setting-2" in a for a in process.args)
    mounts = [a for a in process.args if "dst=/work" in a]
    assert mounts and not mounts[0].endswith(",readonly")


def test_only_the_final_step_text_is_the_answer(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = _WatchingDocker()
    docker.script_process(events(answer='{"answer": "final"}', before='{"answer": "draft"} '))
    result = backend(cfg, docker).run_step(Step.REFLECT, "p", SCHEMA, {}, [])
    assert result.output == {"answer": "final"}


def test_error_event_fails_and_is_redacted(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = _WatchingDocker()
    error = {
        "type": "error",
        "sessionID": SESSION,
        "error": {"name": "APIError", "data": {"message": f"bad key {API_KEY}"}},
    }
    docker.script_process([json.dumps(error)], returncode=1)
    with pytest.raises(BackendError) as info:
        backend(cfg, docker).run_step(Step.REFLECT, "p", SCHEMA, {}, [])
    assert "bad key" in str(info.value)
    assert API_KEY not in str(info.value)
    assert list((cfg.state_root / "sessions" / "opencode").iterdir()) == []


def test_answer_without_json_fails(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = _WatchingDocker()
    docker.script_process(events(answer="no object here"))
    with pytest.raises(BackendError, match="JSON object"):
        backend(cfg, docker).run_step(Step.REFLECT, "p", SCHEMA, {}, [])


def test_no_output_reports_stderr(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = _WatchingDocker()
    docker.script_process([], returncode=1, stderr="model not found\n")
    with pytest.raises(BackendError, match="model not found"):
        backend(cfg, docker).run_step(Step.REFLECT, "p", SCHEMA, {}, [])


def test_transcript_is_redacted(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = _WatchingDocker()
    lines = events()
    lines.insert(1, json.dumps({"type": "text", "part": {"text": f"key is {API_KEY}"}}))
    docker.script_process(lines)
    result = backend(cfg, docker).run_step(Step.REFLECT, "p", SCHEMA, {}, [])
    text = result.transcript_path.read_text()
    assert API_KEY not in text
    assert "step_finish" in text


def test_missing_login_file(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.opencode.auth_file.unlink()
    docker = FakeDocker()
    with pytest.raises(BackendError, match="opencode auth login"):
        backend(cfg, docker).run_step(Step.REFLECT, "p", SCHEMA, {}, [])
    assert docker.spawned == []


def test_missing_provider_login(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, logins={"otherprovider": {"type": "api", "key": OTHER_KEY}})
    docker = FakeDocker()
    with pytest.raises(BackendError, match="no login for 'someprovider'"):
        backend(cfg, docker).run_step(Step.REFLECT, "p", SCHEMA, {}, [])
    assert docker.spawned == []


def oauth(access: str) -> dict[str, Any]:
    return {"type": "oauth", "access": access, "refresh": "r" * 40, "expires": 1}


def test_refreshed_oauth_login_is_written_back(tmp_path: Path) -> None:
    cfg = make_config(
        tmp_path,
        logins={
            "someprovider": oauth("a" * 40),
            "otherprovider": {"type": "api", "key": OTHER_KEY},
        },
    )
    new_access = "n" * 40
    docker = _WatchingDocker(rewrite={"someprovider": oauth(new_access)})
    docker.script_process(events())
    backend(cfg, docker).run_step(Step.REFLECT, "p", SCHEMA, {}, [])
    host = json.loads(cfg.opencode.auth_file.read_text())
    assert host["someprovider"]["access"] == new_access
    assert host["otherprovider"] == {"type": "api", "key": OTHER_KEY}
    assert stat.S_IMODE(cfg.opencode.auth_file.stat().st_mode) == 0o600


def test_changed_api_key_is_not_written_back(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    before = cfg.opencode.auth_file.read_text()
    docker = _WatchingDocker(rewrite={"someprovider": {"type": "api", "key": "x" * 40}})
    docker.script_process(events())
    backend(cfg, docker).run_step(Step.REFLECT, "p", SCHEMA, {}, [])
    assert cfg.opencode.auth_file.read_text() == before


def test_oauth_entry_with_new_keys_is_not_written_back(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, logins={"someprovider": oauth("a" * 40)})
    before = cfg.opencode.auth_file.read_text()
    changed = {**oauth("n" * 40), "extra": "planted-value"}
    docker = _WatchingDocker(rewrite={"someprovider": changed})
    docker.script_process(events())
    backend(cfg, docker).run_step(Step.REFLECT, "p", SCHEMA, {}, [])
    assert cfg.opencode.auth_file.read_text() == before


def test_host_login_changed_during_the_step_is_kept(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, logins={"someprovider": oauth("a" * 40)})
    docker = _WatchingDocker(rewrite={"someprovider": oauth("n" * 40)})
    docker.script_process(events())
    original_spawn = docker.spawn

    def spawn(args: Sequence[str], *, env: Mapping[str, str] | None = None) -> FakeProcess:
        process = original_spawn(args, env=env)
        # The person logs in again on the host while the step runs.
        cfg.opencode.auth_file.write_text(json.dumps({"someprovider": oauth("h" * 40)}))
        return process

    docker.spawn = spawn  # type: ignore[method-assign]
    backend(cfg, docker).run_step(Step.REFLECT, "p", SCHEMA, {}, [])
    host = json.loads(cfg.opencode.auth_file.read_text())
    assert host["someprovider"]["access"] == "h" * 40


def test_stop_before_start_leaves_nothing(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = FakeDocker()
    with pytest.raises(StepInterrupted):
        backend(cfg, docker).run_step(Step.REFLECT, "p", SCHEMA, {}, [], should_stop=lambda: True)
    assert docker.spawned == []
    assert list((cfg.state_root / "sessions" / "opencode").iterdir()) == []


def test_stop_kills_the_container_and_removes_the_login(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = _WatchingDocker()
    docker.script_process(events())
    calls = iter([False, True])
    with pytest.raises(StepInterrupted):
        backend(cfg, docker).run_step(
            Step.REFLECT, "p", SCHEMA, {}, [], should_stop=lambda: next(calls, True)
        )
    assert docker.spawned[0].killed
    assert docker.runs[0].args[:2] == ["docker", "kill"]
    assert not list((cfg.state_root / "sessions" / "opencode").rglob("auth.json"))


# -- config and init ------------------------------------------------------------


def test_config_needs_provider_and_model(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="provider/model"):
        Config.from_dict(
            {
                "core": {"state_root": str(tmp_path / "state")},
                "backend": {"worker": {"kind": "opencode", "model": "just-a-model"}},
            },
            env={},
        )


@pytest.mark.parametrize("name", ["XDG_DATA_HOME", "OPENCODE_CONFIG_CONTENT"])
def test_opencode_variables_cannot_be_step_env(tmp_path: Path, name: str) -> None:
    cfg = make_config(tmp_path, env_allowlist=[name])
    docker = FakeDocker()
    with pytest.raises(BackendError, match="reserved"):
        backend(cfg, docker, "worker").run_step(Step.WORK, "p", SCHEMA, {name: "/elsewhere"}, [])
    assert docker.spawned == []


def test_init_writes_opencode_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENDOT_CONFIG", raising=False)
    path = tmp_path / "opendot.toml"
    argv = ["--config", str(path), "init", "--state-root", str(tmp_path / "state")]
    assert cli.main([*argv, "--worker", "opencode"]) == 1
    assert not path.exists()
    assert (
        cli.main(
            [
                *argv,
                "--worker",
                "opencode",
                "--worker-model",
                MODEL,
            ]
        )
        == 0
    )
    config = Config.load(path)
    assert config.worker_backend.kind == "opencode"
    assert config.worker_backend.model == MODEL
