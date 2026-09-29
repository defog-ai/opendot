from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from conftest import FakeDocker, FakeProcess
from opendot.backends import BackendError, StepInterrupted, StepLimits, StepTimedOut
from opendot.backends.codex import CodexBackend
from opendot.config import Config
from opendot.models import Step

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}}
ACCESS_VALUE = "opaque-access-value-0123456789"
LOGIN = {"auth_mode": "chatgpt", "tokens": {"access_token": ACCESS_VALUE, "account_id": "acct-1"}}


def make_config(tmp_path: Path, env_allowlist: Sequence[str] = ()) -> Config:
    auth = tmp_path / "codex" / "auth.json"
    auth.parent.mkdir(exist_ok=True)
    auth.write_text(json.dumps(LOGIN))
    cfg = Config.from_dict(
        {
            "core": {"state_root": str(tmp_path / "state")},
            "backend": {
                "worker": {"kind": "codex", "model": ""},
                "codex": {"auth_file": str(auth)},
            },
            "sandbox": {"env_allowlist": list(env_allowlist)},
        },
        env={},
    )
    cfg.ensure_directories()
    return cfg


def backend(cfg: Config, docker: FakeDocker) -> CodexBackend:
    return CodexBackend(
        cfg,
        "worker",
        docker,
        poll_seconds=0.01,
        interrupt_grace_seconds=0.2,
        exit_grace_seconds=0.1,
    )


def line(message: dict[str, Any]) -> str:
    return json.dumps(message)


def happy_lines(
    thread_id: str = "thr-1",
    *,
    final_text: str = '{"answer": "done"}',
    status: str = "completed",
    extra: Sequence[dict[str, Any]] = (),
) -> list[str]:
    usage_first = {
        "total": {"inputTokens": 1100, "outputTokens": 60},
        "last": {"inputTokens": 100, "outputTokens": 10},
    }
    usage_last = {
        "total": {"inputTokens": 1500, "outputTokens": 90},
        "last": {"inputTokens": 400, "outputTokens": 30},
    }
    return [
        line({"id": 1, "result": {"userAgent": "codex"}}),
        line({"id": 2, "result": {"thread": {"id": thread_id}}}),
        line({"id": 3, "result": {"turn": {"id": "turn-1", "status": "inProgress"}}}),
        line(
            {"method": "turn/started", "params": {"threadId": thread_id, "turn": {"id": "turn-1"}}}
        ),
        *[line(m) for m in extra],
        line(
            {
                "method": "thread/tokenUsage/updated",
                "params": {"threadId": thread_id, "turnId": "turn-1", "tokenUsage": usage_first},
            }
        ),
        line(
            {
                "method": "item/completed",
                "params": {
                    "threadId": thread_id,
                    "turnId": "turn-1",
                    "item": {"type": "agentMessage", "id": "m1", "text": "thinking out loud"},
                },
            }
        ),
        line(
            {
                "method": "item/completed",
                "params": {
                    "threadId": thread_id,
                    "turnId": "turn-1",
                    "item": {"type": "agentMessage", "id": "m2", "text": final_text},
                },
            }
        ),
        line(
            {
                "method": "thread/tokenUsage/updated",
                "params": {"threadId": thread_id, "turnId": "turn-1", "tokenUsage": usage_last},
            }
        ),
        line(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turn": {
                        "id": "turn-1",
                        "status": status,
                        "items": [],
                        "error": {"message": "model overloaded"} if status == "failed" else None,
                    },
                },
            }
        ),
    ]


def sent(process: FakeProcess) -> list[dict[str, Any]]:
    return [json.loads(x) for x in process.stdin.written.splitlines() if x.strip()]


def test_happy_path_protocol(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = FakeDocker()
    docker.script_process(happy_lines())
    result = backend(cfg, docker).run_step(Step.WORK, "do the thing", SCHEMA, {}, [])

    assert result.output == {"answer": "done"}
    assert result.thread_id == "thr-1"
    assert result.usage == {"turns": 1, "input_tokens": 500, "output_tokens": 40}

    process = docker.spawned[0]
    messages = sent(process)
    methods = [m.get("method") for m in messages]
    assert methods[:4] == ["initialize", "initialized", "thread/start", "turn/start"]
    assert "jsonrpc" not in messages[0]
    assert messages[0]["params"]["clientInfo"]["name"] == "opendot"
    assert "id" not in messages[1]
    start = messages[2]["params"]
    assert start["approvalPolicy"] == "never"
    assert start["cwd"] == "/work"
    assert "model" not in start
    turn = messages[3]["params"]
    assert turn["threadId"] == "thr-1"
    assert turn["outputSchema"] == SCHEMA
    assert turn["input"] == [{"type": "text", "text": "do the thing"}]
    assert process.stdin.closed_by_caller

    args = process.args
    assert args[:3] == ["docker", "run", "--rm"]
    assert args[-2:] == ["codex", "app-server"]
    assert "CODEX_HOME=/opendot/cli" in args
    assert process.env == {}

    session = cfg.state_root / "sessions" / "codex" / "thr-1"
    assert session.is_dir()
    assert not (session / "cli" / "auth.json").exists()
    assert result.transcript_path is not None
    text = result.transcript_path.read_text()
    assert "turn/start" in text


def test_server_requests_are_answered(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = FakeDocker()
    requests = [
        {"id": "s1", "method": "item/commandExecution/requestApproval", "params": {}},
        {"id": "s2", "method": "item/fileChange/requestApproval", "params": {}},
        {"id": "s3", "method": "execCommandApproval", "params": {}},
        {"id": "s4", "method": "applyPatchApproval", "params": {}},
        {"id": "s5", "method": "item/tool/requestUserInput", "params": {}},
        {"id": "s6", "method": "mcpServer/elicitation/request", "params": {}},
        {"id": "s7", "method": "item/tool/call", "params": {}},
    ]
    docker.script_process(happy_lines(extra=requests))
    backend(cfg, docker).run_step(Step.WORK, "p", SCHEMA, {}, [])
    answers = {m["id"]: m for m in sent(docker.spawned[0]) if isinstance(m.get("id"), str)}
    assert answers["s1"]["result"] == {"decision": "accept"}
    assert answers["s2"]["result"] == {"decision": "accept"}
    assert answers["s3"]["result"] == {"decision": "approved"}
    assert answers["s4"]["result"] == {"decision": "approved"}
    assert answers["s5"]["result"] == {"answers": {}}
    assert answers["s6"]["result"] == {"action": "decline"}
    assert "error" in answers["s7"] and "result" not in answers["s7"]


def test_resume_uses_thread_resume(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = FakeDocker()
    docker.script_process(happy_lines("thr-1"))
    docker.script_process(happy_lines("thr-1"))
    runner = backend(cfg, docker)
    first = runner.run_step(Step.WORK, "one", SCHEMA, {}, [])
    second = runner.run_step(Step.WORK, "two", SCHEMA, {}, [], resume_id=first.thread_id)
    assert second.thread_id == "thr-1"
    messages = sent(docker.spawned[1])
    assert messages[2]["method"] == "thread/resume"
    assert messages[2]["params"]["threadId"] == "thr-1"


def test_resume_of_unknown_session_fails(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    with pytest.raises(BackendError):
        backend(cfg, FakeDocker()).run_step(Step.WORK, "p", SCHEMA, {}, [], resume_id="nope")


def test_model_is_passed_only_when_configured(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = FakeDocker()
    docker.script_process(happy_lines())
    runner = backend(cfg, docker)
    runner.model = "some-model"
    runner.run_step(Step.WORK, "p", SCHEMA, {}, [])
    assert sent(docker.spawned[0])[2]["params"]["model"] == "some-model"


def test_failed_turn_raises_and_cleans_up(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = FakeDocker()
    docker.script_process(happy_lines(status="failed"))
    with pytest.raises(BackendError, match="model overloaded"):
        backend(cfg, docker).run_step(Step.WORK, "p", SCHEMA, {}, [])
    assert docker.spawned[0].killed
    assert ["docker", "kill"] == docker.runs[0].args[:2]
    assert list((cfg.state_root / "sessions" / "codex").iterdir()) == []


@pytest.mark.parametrize("text", ["not json", "[1, 2]"])
def test_bad_final_message(tmp_path: Path, text: str) -> None:
    cfg = make_config(tmp_path)
    docker = FakeDocker()
    docker.script_process(happy_lines(final_text=text))
    with pytest.raises(BackendError):
        backend(cfg, docker).run_step(Step.WORK, "p", SCHEMA, {}, [])


def test_request_error_is_reported(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = FakeDocker()
    docker.script_process(
        [
            line({"id": 1, "result": {}}),
            line({"id": 2, "error": {"code": -32600, "message": "bad cwd"}}),
        ]
    )
    with pytest.raises(BackendError, match="bad cwd"):
        backend(cfg, docker).run_step(Step.WORK, "p", SCHEMA, {}, [])


def test_early_exit_includes_redacted_stderr(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = FakeDocker()
    docker.script_process([], returncode=1, stderr=f"login failed for {ACCESS_VALUE}\n")
    with pytest.raises(BackendError) as info:
        backend(cfg, docker).run_step(Step.WORK, "p", SCHEMA, {}, [])
    assert "login failed" in str(info.value)
    assert ACCESS_VALUE not in str(info.value)


def test_stop_sends_turn_interrupt(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = FakeDocker()
    lines = happy_lines()[:4] + [
        line(
            {
                "method": "turn/completed",
                "params": {"threadId": "thr-1", "turn": {"id": "turn-1", "status": "interrupted"}},
            }
        )
    ]
    docker.script_process(lines)

    def should_stop() -> bool:
        return bool(docker.spawned) and "turn/start" in docker.spawned[0].stdin.written

    with pytest.raises(StepInterrupted):
        backend(cfg, docker).run_step(Step.WORK, "p", SCHEMA, {}, [], should_stop=should_stop)
    interrupt = [m for m in sent(docker.spawned[0]) if m.get("method") == "turn/interrupt"]
    assert interrupt and interrupt[0]["params"] == {"threadId": "thr-1", "turnId": "turn-1"}
    assert docker.spawned[0].killed
    assert docker.runs[0].args[:2] == ["docker", "kill"]


def test_stop_before_start_spawns_nothing(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = FakeDocker()
    with pytest.raises(StepInterrupted):
        backend(cfg, docker).run_step(Step.WORK, "p", SCHEMA, {}, [], should_stop=lambda: True)
    assert docker.spawned == []


class _SilentDocker(FakeDocker):
    """Spawns a process whose stdout stays open and never sends anything."""

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
                Step.WORK, "p", SCHEMA, {}, [], limits=StepLimits(timeout_seconds=0.2)
            )
        assert docker.spawned[0].killed
        assert docker.runs[0].args[:2] == ["docker", "kill"]
    finally:
        os.close(docker.open_fds[1])


def test_login_is_staged_only_during_the_step(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = FakeDocker()
    docker.script_process(happy_lines())
    seen: list[dict[str, Any]] = []

    def should_stop() -> bool:
        for staged in (cfg.state_root / "sessions" / "codex").glob("*/cli/auth.json"):
            seen.append(json.loads(staged.read_text()))
            assert oct(staged.stat().st_mode & 0o777) == "0o600"
        return False

    backend(cfg, docker).run_step(Step.WORK, "p", SCHEMA, {}, [], should_stop=should_stop)
    assert seen and seen[0] == LOGIN
    assert list((cfg.state_root / "sessions").rglob("auth.json")) == []
    # Nothing but the login file and the session folders is given to the container.
    args = docker.spawned[0].args
    assert not any(str(cfg.codex.auth_file) in a for a in args)


def test_refreshed_login_is_copied_back(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = FakeDocker()
    docker.script_process(happy_lines())
    refreshed = {
        "auth_mode": "chatgpt",
        "tokens": {"access_token": "opaque-new-value-987654321", "account_id": "acct-1"},
    }

    def should_stop() -> bool:
        for staged in (cfg.state_root / "sessions" / "codex").glob("*/cli/auth.json"):
            staged.write_text(json.dumps(refreshed))
        return False

    backend(cfg, docker).run_step(Step.WORK, "p", SCHEMA, {}, [], should_stop=should_stop)
    assert json.loads(cfg.codex.auth_file.read_text()) == refreshed
    assert oct(cfg.codex.auth_file.stat().st_mode & 0o777) == "0o600"


def test_login_for_another_account_is_not_copied_back(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = FakeDocker()
    docker.script_process(happy_lines())
    other = {
        "auth_mode": "chatgpt",
        "tokens": {"access_token": "opaque-other-value-555555555", "account_id": "acct-2"},
    }

    def should_stop() -> bool:
        for staged in (cfg.state_root / "sessions" / "codex").glob("*/cli/auth.json"):
            staged.write_text(json.dumps(other))
        return False

    backend(cfg, docker).run_step(Step.WORK, "p", SCHEMA, {}, [], should_stop=should_stop)
    assert json.loads(cfg.codex.auth_file.read_text()) == LOGIN


def test_refresh_does_not_overwrite_a_host_login_changed_during_the_step(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = FakeDocker()
    docker.script_process(happy_lines())
    relogin = {
        "auth_mode": "chatgpt",
        "tokens": {"access_token": "host-new", "account_id": "acct-1"},
    }
    refreshed = {
        "auth_mode": "chatgpt",
        "tokens": {"access_token": "step-new", "account_id": "acct-1"},
    }

    def should_stop() -> bool:
        for staged in (cfg.state_root / "sessions" / "codex").glob("*/cli/auth.json"):
            staged.write_text(json.dumps(refreshed))
            cfg.codex.auth_file.write_text(json.dumps(relogin))
        return False

    backend(cfg, docker).run_step(Step.WORK, "p", SCHEMA, {}, [], should_stop=should_stop)
    assert json.loads(cfg.codex.auth_file.read_text()) == relogin


def test_broken_login_is_not_copied_back(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    docker = FakeDocker()
    docker.script_process(happy_lines())

    def should_stop() -> bool:
        for staged in (cfg.state_root / "sessions" / "codex").glob("*/cli/auth.json"):
            staged.write_text("{not json")
        return False

    backend(cfg, docker).run_step(Step.WORK, "p", SCHEMA, {}, [], should_stop=should_stop)
    assert json.loads(cfg.codex.auth_file.read_text()) == LOGIN


def test_missing_login_is_a_clear_error(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.codex.auth_file.unlink()
    docker = FakeDocker()
    with pytest.raises(BackendError, match="codex login"):
        backend(cfg, docker).run_step(Step.WORK, "p", SCHEMA, {}, [])
    assert docker.spawned == []


def test_transcript_is_redacted(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, env_allowlist=["TOOL_SETTING"])
    docker = FakeDocker()
    leak = {
        "method": "item/completed",
        "params": {
            "threadId": "thr-1",
            "turnId": "turn-1",
            "item": {
                "type": "commandExecution",
                "aggregatedOutput": f"{ACCESS_VALUE} value-of-setting-1",
            },
        },
    }
    docker.script_process(happy_lines(extra=[leak]))
    result = backend(cfg, docker).run_step(
        Step.WORK, "p", SCHEMA, {"TOOL_SETTING": "value-of-setting-1"}, []
    )
    text = result.transcript_path.read_text()
    assert ACCESS_VALUE not in text
    assert "value-of-setting-1" not in text
    process = docker.spawned[0]
    assert process.env == {"TOOL_SETTING": "value-of-setting-1"}
    assert "TOOL_SETTING" in process.args
    assert not any("value-of-setting-1" in a for a in process.args)


def test_review_step_refuses_env_and_mounts_read_only_work(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, env_allowlist=["TOOL_SETTING"])
    docker = FakeDocker()
    with pytest.raises(BackendError):
        backend(cfg, docker).run_step(Step.REVIEW, "p", SCHEMA, {"TOOL_SETTING": "x"}, [])
    docker.script_process(happy_lines())
    backend(cfg, docker).run_step(Step.REVIEW, "p", SCHEMA, {}, [])
    mounts = [a for a in docker.spawned[0].args if "dst=/work" in a]
    assert mounts and mounts[0].endswith(",readonly")
