"""The `opendot` command line, run in-process against a temporary state folder.

Every test uses the scripted fake backend or a fake command runner, so nothing
here needs Docker, a model login, crontab or the network.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

import opendot.__main__ as cli
from opendot import cron, help
from opendot.backends import CommandResult
from opendot.config import Config
from opendot.models import TaskState
from opendot.store import WorkerLock, open_store


class FakeRunner:
    """Records commands and answers them from a table of (first args) -> result."""

    def __init__(self, answers: dict[tuple[str, ...], CommandResult] | None = None):
        self.answers = answers or {}
        self.calls: list[tuple[list[str], str | None]] = []

    def run(self, args, *, input=None, env=None, timeout=None) -> CommandResult:
        self.calls.append((list(args), input))
        for prefix, result in self.answers.items():
            if tuple(args[: len(prefix)]) == prefix:
                return result
        return CommandResult(list(args), 0, "", "")


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.delenv("OPENDOT_CONFIG", raising=False)
    for name in list(__import__("os").environ):
        if name.startswith("OPENDOT_"):
            monkeypatch.delenv(name, raising=False)
    return tmp_path


def run(home: Path, *argv: str) -> int:
    return cli.main(["--config", str(home / "opendot.toml"), *argv])


@pytest.fixture
def demo(home, capsys) -> Path:
    assert run(home, "init", "--demo", "--state-root", str(home / "state")) == 0
    capsys.readouterr()
    return home


def load(home: Path) -> Config:
    return Config.load(home / "opendot.toml")


# ---------------------------------------------------------------------------
# Help and parsing
# ---------------------------------------------------------------------------


def test_every_command_has_a_summary_and_a_parser():
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if a.dest == "command")
    assert set(sub.choices) == set(help.COMMANDS)
    assert len(help.COMMANDS) == 24


def test_help_lists_the_demo(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "init --demo" in out
    assert "install-cron" in out


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_init_writes_a_private_config_and_a_state_folder(home, capsys):
    assert run(home, "init", "--state-root", str(home / "state")) == 0
    path = home / "opendot.toml"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    config = load(home)
    assert config.worker_backend.kind == "codex"
    assert config.reviewer_backend.kind == "claude_code"
    assert config.state_root == (home / "state").resolve()
    assert config.db_path.is_file()
    assert "Wrote" in capsys.readouterr().out


def test_init_refuses_to_overwrite_without_force(home, capsys):
    assert run(home, "init", "--state-root", str(home / "state")) == 0
    assert run(home, "init", "--state-root", str(home / "state")) == 1
    assert "--force" in capsys.readouterr().err
    assert run(home, "init", "--force", "--demo", "--state-root", str(home / "state")) == 0
    assert load(home).worker_backend.kind == "fake"


def test_init_demo_writes_a_fake_script(demo):
    config = load(demo)
    assert config.worker_backend.kind == "fake"
    assert config.reviewer_backend.kind == "fake"
    script = json.loads(config.fake.script.read_text())
    assert script["work"][0]["output"]["status"] == "done"
    assert script["review"][0]["output"]["verdicts"][0]["verdict"] == "approve"


def test_init_uses_opendot_config_when_no_flag(home, monkeypatch, capsys):
    target = home / "elsewhere" / "cfg.toml"
    monkeypatch.setenv("OPENDOT_CONFIG", str(target))
    assert cli.main(["init", "--demo", "--state-root", str(home / "s")]) == 0
    assert target.is_file()


def test_missing_config_file_is_a_clean_error(home, capsys):
    assert run(home, "status") == 1
    assert "opendot:" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The demo path: task -> run-once -> status
# ---------------------------------------------------------------------------


def test_demo_task_runs_and_status_shows_the_answer(demo, capsys):
    assert run(demo, "task", "Say", "hello") == 0
    assert "Task 1 queued" in capsys.readouterr().out

    assert run(demo, "run-once") == 0
    out = capsys.readouterr().out
    assert "Hello. This answer comes from the scripted fake backend" in out
    assert "Task 1 is now done." in out

    assert run(demo, "status") == 0
    out = capsys.readouterr().out
    assert "Tasks: done 1" in out
    assert "#1 done" in out
    assert "answer: Hello." in out

    assert run(demo, "show", "1") == 0
    out = capsys.readouterr().out
    assert "reply.post" in out
    assert "work on fake: succeeded" in out
    assert "review on fake: succeeded" in out


def test_run_once_with_nothing_queued(demo, capsys):
    assert run(demo, "run-once") == 0
    assert "No queued task." in capsys.readouterr().out


def test_status_on_an_empty_database(demo, capsys):
    assert run(demo, "status") == 0
    assert "No tasks yet." in capsys.readouterr().out


def test_run_once_skips_when_another_worker_holds_the_lock(demo, capsys):
    run(demo, "task", "hello")
    with WorkerLock(load(demo).lock_path):
        assert run(demo, "run-once") == 0
    assert "Another OpenDot worker is running" in capsys.readouterr().err
    with open_store(load(demo)) as store:
        assert store.get_task(1).state is TaskState.QUEUED


def test_tick_runs_a_pass(demo, capsys):
    run(demo, "task", "hello")
    capsys.readouterr()
    assert run(demo, "tick") == 0
    out = capsys.readouterr().out
    assert "task_turns=1" in out
    with open_store(load(demo)) as store:
        assert store.get_task(1).state is TaskState.DONE


def test_ingest_with_only_the_cli_channel(demo, capsys):
    assert run(demo, "ingest") == 0
    assert "0 new message(s)." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Approvals through the CLI
# ---------------------------------------------------------------------------


def test_ask_rule_then_queue_then_approve(demo, capsys):
    assert run(demo, "rules", "add", "reply.post", "ask") == 0
    run(demo, "task", "draft it")
    assert run(demo, "run-once") == 0
    assert "awaiting_approval" in capsys.readouterr().out

    assert run(demo, "queue") == 0
    out = capsys.readouterr().out
    assert "approval 1 for task 1: reply.post" in out
    assert "Hello." in out

    assert run(demo, "approve", "1") == 0
    assert "Approval 1 granted." in capsys.readouterr().out
    assert run(demo, "run-once") == 0
    assert "Hello. This answer" in capsys.readouterr().out
    assert run(demo, "status") == 0
    assert "answer: Hello." in capsys.readouterr().out


def test_deny_keeps_the_reply_unsent(demo, capsys):
    run(demo, "rules", "add", "reply.post", "ask")
    run(demo, "task", "draft it")
    run(demo, "run-once")
    assert run(demo, "deny", "1") == 0
    run(demo, "run-once")
    capsys.readouterr()
    run(demo, "status")
    assert "answer:" not in capsys.readouterr().out


def test_unknown_approval_is_a_clean_error(demo, capsys):
    assert run(demo, "approve", "99") == 1
    assert "not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# retry, skip, stop
# ---------------------------------------------------------------------------


def test_skip_retry_and_stop(demo, capsys):
    run(demo, "task", "one")
    assert run(demo, "skip", "1") == 0
    assert run(demo, "skip", "1") == 1  # already finished
    assert run(demo, "retry", "1") == 0
    with open_store(load(demo)) as store:
        assert store.get_task(1).state is TaskState.QUEUED
    assert run(demo, "stop", "1") == 0
    out = capsys.readouterr().out
    assert "Task 1 is stopped." in out
    with open_store(load(demo)) as store:
        assert store.get_task(1).state is TaskState.STOPPED


def test_retry_refuses_a_done_or_running_task(demo, capsys):
    run(demo, "task", "one")
    assert run(demo, "retry", "1") == 1
    run(demo, "run-once")
    assert run(demo, "retry", "1") == 1
    assert "only a failed, stopped or skipped task" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# notes, schedules, rules
# ---------------------------------------------------------------------------


def test_notes_round_trip(demo, capsys):
    assert run(demo, "notes", "add", "prefers short answers", "--subject", "style") == 0
    assert run(demo, "notes", "list") == 0
    assert "(cli:operator, operator) [style] prefers short answers" in capsys.readouterr().out
    assert run(demo, "notes", "edit", "1", "--text", "prefers tables") == 0
    assert run(demo, "notes", "edit", "1") == 1
    run(demo, "notes", "list")
    assert "prefers tables" in capsys.readouterr().out
    assert run(demo, "notes", "rm", "1") == 0
    assert run(demo, "notes", "rm", "1") == 1
    run(demo, "notes", "list")
    assert "No notes." in capsys.readouterr().out


def test_schedules_round_trip(demo, capsys):
    args = ["--what", "weekly summary", "--cadence", "0 9 * * 1", "--tz", "Asia/Singapore"]
    assert run(demo, "schedules", "add", *args, "--until", "2099-01-01") == 0
    assert "Schedule 1 added" in capsys.readouterr().out
    run(demo, "schedules", "list")
    out = capsys.readouterr().out
    assert "active '0 9 * * 1' Asia/Singapore" in out
    assert "2098-12-31 16:00 UTC" in out  # the date-only end is read in the schedule's zone
    assert run(demo, "schedules", "pause", "1") == 0
    assert run(demo, "schedules", "resume", "1") == 0
    assert run(demo, "schedules", "rm", "1") == 0
    run(demo, "schedules", "list")
    assert "ended" in capsys.readouterr().out


def test_schedule_errors_are_clean(demo, capsys):
    assert run(demo, "schedules", "add", "--what", "x", "--cadence", "not cron") == 1
    assert (
        run(demo, "schedules", "add", "--what", "x", "--cadence", "0 9 * * *", "--until", "soon")
        == 1
    )
    err = capsys.readouterr().err
    assert err.count("opendot:") == 2


def test_rules_round_trip(demo, capsys):
    assert run(demo, "rules", "list") == 0
    assert "No rules." in capsys.readouterr().out
    assert run(demo, "rules", "add", "note.*", "preapproved") == 0
    run(demo, "rules", "list")
    assert "note.* target=* level=preapproved (active, from operator)" in capsys.readouterr().out
    assert run(demo, "rules", "rm", "1") == 0
    assert run(demo, "rules", "rm", "1") == 1


# ---------------------------------------------------------------------------
# doctor, images, cron
# ---------------------------------------------------------------------------


def test_doctor_passes_for_the_demo(demo, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_runner", lambda: FakeRunner())
    assert run(demo, "doctor") == 0
    out = capsys.readouterr().out
    assert "0 error(s)." in out
    assert "fake backend is in use" in out


def test_doctor_reports_missing_logins_and_image(home, capsys, monkeypatch):
    run(home, "init", "--state-root", str(home / "state"))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    config = load(home)
    runner = FakeRunner({("docker", "image", "inspect"): CommandResult([], 1, "", "no such")})
    findings = cli.doctor_findings(config, runner, env={})
    errors = [message for level, message in findings if level == "error"]
    assert any("Codex login file" in m for m in errors)
    assert any(config.claude_code.token_env in m for m in errors)
    assert any("build-image" in m for m in errors)
    assert any("network" in m for level, m in findings if level == "warn")


def test_build_image_sends_the_dockerfile_on_stdin(demo, capsys, monkeypatch):
    runner = FakeRunner()
    monkeypatch.setattr(cli, "_runner", lambda: runner)
    assert run(demo, "build-image") == 0
    [(args, stdin)] = runner.calls
    assert args[:2] == ["docker", "build"]
    assert stdin is not None and "FROM" in stdin


def test_build_image_failure(demo, capsys, monkeypatch):
    runner = FakeRunner({("docker",): CommandResult([], 2, "", "boom")})
    monkeypatch.setattr(cli, "_runner", lambda: runner)
    assert run(demo, "build-image") == 1


def test_verify_image_reports_problems(demo, capsys, monkeypatch):
    runner = FakeRunner({("docker",): CommandResult([], 0, "uid=0\n", "")})
    monkeypatch.setattr(cli, "_runner", lambda: runner)
    assert run(demo, "verify-image") == 1
    assert "failed the image check" in capsys.readouterr().err


def test_install_cron_print_changes_nothing(demo, capsys, monkeypatch):
    runner = FakeRunner()
    monkeypatch.setattr(cli, "_runner", lambda: runner)
    assert run(demo, "install-cron", "--print", "--every", "5") == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("*/5 * * * * ")
    assert line.endswith("tick >> " + str(load(demo).logs_dir / "tick.log") + " 2>&1")
    assert "--config" in line
    assert [args for args, _ in runner.calls] == [["crontab", "-l"]]


def test_install_cron_keeps_other_lines_and_can_be_removed(demo, capsys, monkeypatch):
    existing = "0 3 * * * backup.sh\n"
    runner = FakeRunner({("crontab", "-l"): CommandResult([], 0, existing, "")})
    monkeypatch.setattr(cli, "_runner", lambda: runner)
    assert run(demo, "install-cron") == 0
    written = runner.calls[-1][1]
    assert written.startswith(existing)
    assert cron.BLOCK_BEGIN in written and cron.BLOCK_END in written

    runner.answers = {("crontab", "-l"): CommandResult([], 0, written, "")}
    assert run(demo, "install-cron", "--remove") == 0
    assert runner.calls[-1][1] == existing


def test_install_cron_with_no_crontab_yet(demo, monkeypatch):
    runner = FakeRunner({("crontab", "-l"): CommandResult([], 1, "", "no crontab for user")})
    monkeypatch.setattr(cli, "_runner", lambda: runner)
    assert run(demo, "install-cron", "--every", "2") == 0
    assert runner.calls[-1][1].startswith(cron.BLOCK_BEGIN)


# ---------------------------------------------------------------------------
# cron helpers
# ---------------------------------------------------------------------------


def test_tick_line_quotes_paths_and_escapes_percent():
    line = cron.tick_line(
        ["/opt/my tools/opendot"],
        every_minutes=1,
        config_path=Path("/srv/a%b.toml"),
        log_path=Path("/var/log/opendot.log"),
    )
    assert line.startswith("* * * * * '/opt/my tools/opendot' --config /srv/a\\%b.toml tick")


def test_tick_line_rejects_bad_intervals():
    with pytest.raises(cron.CronError):
        cron.tick_line(["opendot"], every_minutes=0, config_path=None, log_path=Path("x"))


def test_remove_block_refuses_a_broken_block():
    with pytest.raises(cron.CronError):
        cron.remove_block(f"{cron.BLOCK_BEGIN}\n* * * * * x\n")


def test_with_block_replaces_an_old_block():
    first = cron.with_block("", "* * * * * old")
    second = cron.with_block(first, "* * * * * new")
    assert "old" not in second
    assert second.count(cron.BLOCK_BEGIN) == 1
