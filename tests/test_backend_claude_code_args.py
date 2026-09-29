from __future__ import annotations

import dataclasses
import json
import os
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from conftest import FakeDocker, FakeProcess
from opendot.backends import BackendError, StepInterrupted, StepLimits, StepTimedOut
from opendot.backends.anthropic_api import AnthropicApiBackend
from opendot.backends.claude_code import (
    WORK_TOOLS,
    ClaudeCodeBackend,
    claude_args,
    container_token_variable,
)
from opendot.config import Config, ConfigError
from opendot.models import Step

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}}
TOKEN = "sk-ant-oat01-" + "t" * 48


def make_config(
    tmp_path: Path, token_env: str = "CLAUDE_CODE_OAUTH_TOKEN", **sandbox: Any
) -> Config:
    cfg = Config.from_dict(
        {
            "core": {"state_root": str(tmp_path / "state")},
            "backend": {
                "reviewer": {"kind": "claude_code", "model": ""},
                "claude_code": {"token_env": token_env},
            },
            "sandbox": sandbox,
        },
        env={},
    )
    cfg.ensure_directories()
    return cfg


def backend(
    cfg: Config, docker: FakeDocker, host_env: Mapping[str, str] | None = None
) -> ClaudeCodeBackend:
    env = {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN} if host_env is None else host_env
    return ClaudeCodeBackend(
        cfg, "reviewer", docker, host_env=env, poll_seconds=0.01, exit_grace_seconds=0.1
    )


def stream(session_id: str, *, result: dict[str, Any] | None = None) -> list[str]:
    """A hand-written stream-json transcript with the fields the backend reads."""
    final = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 3,
        "result": '{"answer": "ok"}',
        "structured_output": {"answer": "ok"},
        "session_id": session_id,
        "usage": {
            "input_tokens": 10,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 3000,
            "output_tokens": 45,
        },
    }
    final.update(result or {})
    return [
        json.dumps({"type": "system", "subtype": "init", "session_id": session_id, "tools": []}),
        json.dumps(
            {
                "type": "assistant",
                "session_id": session_id,
                "message": {"role": "assistant", "content": [{"type": "text", "text": "working"}]},
            }
        ),
        json.dumps(final),
    ]


def after(args: Sequence[str], flag: str) -> str:
    return args[list(args).index(flag) + 1]


def command_of(process: FakeProcess) -> list[str]:
    args = process.args
    return args[args.index("claude") :]


def answer_with_chosen_session(docker: FakeDocker) -> None:
    """Make each spawn print a stream whose session id is the one the backend chose."""
    original_spawn = docker.spawn

    def spawn(args: Sequence[str], *, env: Mapping[str, str] | None = None) -> FakeProcess:
        flag = "--resume" if "--resume" in args else "--session-id"
        docker.script_process(stream(after(args, flag)))
        return original_spawn(args, env=env)

    docker.spawn = spawn  # type: ignore[method-assign]


def run_new(cfg: Config, docker: FakeDocker, step: Step = Step.REVIEW, **kwargs: Any) -> Any:
    answer_with_chosen_session(docker)
    return backend(cfg, docker, **kwargs).run_step(step, "review this", SCHEMA, {}, [])


def test_command_line_for_a_new_review_step() -> None:
    session_id = str(uuid.uuid4())
    args = claude_args(Step.REVIEW, SCHEMA, session_id=session_id, resume=False)
    assert args[:2] == ["claude", "-p"]
    assert after(args, "--output-format") == "stream-json"
    assert "--verbose" in args
    assert json.loads(after(args, "--json-schema")) == SCHEMA
    assert after(args, "--permission-mode") == "dontAsk"
    assert after(args, "--session-id") == session_id
    assert "--resume" not in args
    assert after(args, "--tools") == ""
    assert "--allowedTools" not in args
    assert "--model" not in args
    assert "--dangerously-skip-permissions" not in args


def test_command_line_for_a_resumed_work_step() -> None:
    args = claude_args(Step.WORK, SCHEMA, session_id="abc", resume=True, model="some-model")
    assert after(args, "--resume") == "abc"
    assert "--session-id" not in args
    assert after(args, "--allowedTools") == ",".join(WORK_TOOLS)
    assert "--tools" not in args
    assert after(args, "--model") == "some-model"


def test_token_variable_choice() -> None:
    assert container_token_variable("CLAUDE_CODE_OAUTH_TOKEN", TOKEN) == "CLAUDE_CODE_OAUTH_TOKEN"
    assert container_token_variable("MY_CLAUDE_LOGIN", TOKEN) == "CLAUDE_CODE_OAUTH_TOKEN"
    assert container_token_variable("ANTHROPIC_API_KEY", "anything") == "ANTHROPIC_API_KEY"
    assert container_token_variable("MY_KEY", "sk-ant-api03-abc") == "ANTHROPIC_API_KEY"


def test_happy_path(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = FakeDocker()
    result = run_new(cfg, docker)
    process = docker.spawned[0]
    session_id = after(process.args, "--session-id")

    assert result.output == {"answer": "ok"}
    assert result.thread_id == session_id
    assert result.usage == {"turns": 3, "input_tokens": 3210, "output_tokens": 45}

    assert process.stdin.written == "review this"
    assert process.stdin.closed_by_caller
    assert process.args[:3] == ["docker", "run", "--rm"]
    assert "CLAUDE_CONFIG_DIR=/opendot/cli" in process.args
    assert command_of(process)[:2] == ["claude", "-p"]
    # The login travels in the docker process environment, named but not valued in argv.
    assert process.env == {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN}
    assert "CLAUDE_CODE_OAUTH_TOKEN" in process.args
    assert not any(TOKEN in a for a in process.args)
    assert not any("OPENAI" in a or "auth.json" in a for a in process.args)
    mounts = [a for a in process.args if "dst=/work" in a]
    assert mounts and mounts[0].endswith(",readonly")
    assert (cfg.state_root / "sessions" / "claude_code" / session_id).is_dir()


def test_api_key_login(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, token_env="ANTHROPIC_API_KEY")
    docker = FakeDocker()
    run_new(cfg, docker, host_env={"ANTHROPIC_API_KEY": "sk-ant-api03-" + "k" * 40})
    process = docker.spawned[0]
    assert set(process.env) == {"ANTHROPIC_API_KEY"}
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in process.args


def test_missing_token_is_a_clear_error(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = FakeDocker()
    with pytest.raises(BackendError, match="CLAUDE_CODE_OAUTH_TOKEN is not set"):
        backend(cfg, docker, host_env={}).run_step(Step.REVIEW, "p", SCHEMA, {}, [])
    assert docker.spawned == []


def test_resume(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = FakeDocker()
    first = run_new(cfg, docker)
    second = backend(cfg, docker).run_step(
        Step.REVIEW, "again", SCHEMA, {}, [], resume_id=first.thread_id
    )
    process = docker.spawned[1]
    assert after(process.args, "--resume") == first.thread_id
    assert "--session-id" not in process.args
    assert second.thread_id == first.thread_id


def test_work_step_gets_tools_and_allowlisted_env(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, env_allowlist=["TOOL_SETTING"])
    docker = FakeDocker()
    answer_with_chosen_session(docker)
    runner = backend(cfg, docker)
    runner.run_step(Step.WORK, "work", SCHEMA, {"TOOL_SETTING": "value-of-setting-2"}, [])
    process = docker.spawned[0]
    assert after(process.args, "--allowedTools") == ",".join(WORK_TOOLS)
    assert process.env == {"TOOL_SETTING": "value-of-setting-2", "CLAUDE_CODE_OAUTH_TOKEN": TOKEN}
    assert not any("value-of-setting-2" in a for a in process.args)
    mounts = [a for a in process.args if "dst=/work" in a]
    assert mounts and not mounts[0].endswith(",readonly")


def test_error_result(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = FakeDocker()
    docker.script_process(
        stream("sid", result={"is_error": True, "subtype": "error_max_turns", "result": ""})
    )
    with pytest.raises(BackendError, match="error_max_turns"):
        backend(cfg, docker).run_step(Step.REVIEW, "p", SCHEMA, {}, [])
    assert list((cfg.state_root / "sessions" / "claude_code").iterdir()) == []


def test_result_text_is_used_without_structured_output(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = FakeDocker()
    lines = stream("sid", result={"structured_output": None, "result": '{"answer": "text"}'})
    docker.script_process(lines)
    result = backend(cfg, docker).run_step(Step.REVIEW, "p", SCHEMA, {}, [])
    assert result.output == {"answer": "text"}
    # The CLI reported another id than the one chosen; the session folder follows it.
    assert result.thread_id == "sid"
    assert (cfg.state_root / "sessions" / "claude_code" / "sid").is_dir()


def test_no_result_line_reports_redacted_stderr(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = FakeDocker()
    docker.script_process([], returncode=1, stderr=f"Invalid API key {TOKEN}\n")
    with pytest.raises(BackendError) as info:
        backend(cfg, docker).run_step(Step.REVIEW, "p", SCHEMA, {}, [])
    assert "Invalid API key" in str(info.value)
    assert TOKEN not in str(info.value)


def test_transcript_is_redacted(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = FakeDocker()
    lines = stream("sid")
    lines.insert(1, json.dumps({"type": "user", "echo": f"token was {TOKEN}"}))
    docker.script_process(lines)
    result = backend(cfg, docker).run_step(Step.REVIEW, "p", SCHEMA, {}, [])
    text = result.transcript_path.read_text()
    assert TOKEN not in text
    assert '"type": "result"' in text


def test_stop_kills_container(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = FakeDocker()
    docker.script_process(stream("sid"))
    calls = iter([False, True])
    with pytest.raises(StepInterrupted):
        backend(cfg, docker).run_step(
            Step.REVIEW, "p", SCHEMA, {}, [], should_stop=lambda: next(calls, True)
        )
    assert docker.spawned[0].killed
    assert docker.runs[0].args[:2] == ["docker", "kill"]


class _SilentDocker(FakeDocker):
    def spawn(self, args, *, env=None):  # type: ignore[override]
        process = super().spawn(args, env=env)
        read_fd, write_fd = os.pipe()
        self.open_fds = [read_fd, write_fd]
        process.stdout = os.fdopen(read_fd, "r")
        return process


def test_timeout_kills_container(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = _SilentDocker()
    try:
        with pytest.raises(StepTimedOut):
            backend(cfg, docker).run_step(
                Step.REVIEW, "p", SCHEMA, {}, [], limits=StepLimits(timeout_seconds=0.2)
            )
        assert docker.spawned[0].killed
        assert docker.runs[0].args[:2] == ["docker", "kill"]
    finally:
        os.close(docker.open_fds[1])


def test_review_step_refuses_env(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, env_allowlist=["TOOL_SETTING"])
    with pytest.raises(BackendError):
        backend(cfg, FakeDocker()).run_step(Step.REVIEW, "p", SCHEMA, {"TOOL_SETTING": "x"}, [])


def test_token_variable_cannot_be_passed_as_step_env(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        make_config(tmp_path, env_allowlist=["CLAUDE_CODE_OAUTH_TOKEN"])
    # The backend refuses it too, for a config built without the loader's check.
    cfg = make_config(tmp_path)
    cfg = dataclasses.replace(
        cfg, sandbox=dataclasses.replace(cfg.sandbox, env_allowlist=["CLAUDE_CODE_OAUTH_TOKEN"])
    )
    with pytest.raises(BackendError):
        backend(cfg, FakeDocker()).run_step(
            Step.WORK, "p", SCHEMA, {"CLAUDE_CODE_OAUTH_TOKEN": "x" * 20}, []
        )


def test_anthropic_api_backend_is_not_ready(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    with pytest.raises(NotImplementedError, match="v0.2"):
        AnthropicApiBackend.from_config(cfg, "worker")
    with pytest.raises(NotImplementedError, match="v0.2"):
        AnthropicApiBackend().run_step(Step.WORK, "p", SCHEMA, {}, [])
