"""The `opendot` command line.

Commands that only read or change stored records (status, queue, show, notes,
schedules, rules, retry, skip) open the store and nothing else. Commands that
run model steps (run-once, tick) build the configured backends and take the
worker lock, so only one worker runs at a time. The local user of every command
here is the operator: channel "cli", actor config.cli.user.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from opendot import __version__, cron, help
from opendot.actions import KIND_NOTIFY, KIND_REPLY, build_registry
from opendot.approvals import NotAllowedToDecide
from opendot.backends import BackendError, CommandRunner, SubprocessRunner
from opendot.channels import ChannelError, enabled_channels
from opendot.claude_login import TokenFileError, read_token_file, save_token_file
from opendot.config import CONFIG_ENV, DEFAULT_CONFIG_PATH, Config, ConfigError
from opendot.container_contract import (
    build_image_args,
    check_verify_output,
    dockerfile_path,
    verify_image_args,
)
from opendot.extensions import doctor_checks as feature_doctor_checks
from opendot.extensions import register_feature_cli
from opendot.models import (
    ActionStatus,
    ApprovalStatus,
    Destination,
    Level,
    NoteSource,
    NotifyRule,
    RuleStatus,
    ScheduleStatus,
    Step,
    StepResult,
    Task,
    TaskState,
)
from opendot.orchestrator import Orchestrator
from opendot.rules import (
    NotOperator,
    add_operator_rule,
    approve_operator_rule,
    remove_operator_rule,
)
from opendot.sandbox import SNAP_DOCKER_ADVICE, detect_snap_docker, snap_docker_problems
from opendot.schedules import (
    ScheduleError,
    create_schedule,
    end_schedule,
    pause_schedule,
    resume_schedule,
)
from opendot.store import LockBusy, NotFound, Store, StoreError, WorkerLock, open_store

UNFINISHED = [
    TaskState.QUEUED,
    TaskState.RUNNING,
    TaskState.AWAITING_APPROVAL,
    TaskState.AWAITING_REPLY,
    TaskState.WAITING,
]
DEMO_SCRIPT_NAME = "demo-script.json"
DEMO_REPLY = (
    "Hello. This answer comes from the scripted fake backend, so no model ran. "
    "Switch backend.worker.kind and backend.reviewer.kind to codex, claude_code or opencode "
    "for real answers."
)


class CliError(Exception):
    """A problem to report to the user with exit code 1 and no traceback."""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _runner() -> CommandRunner:
    """The command runner for docker and crontab. Tests replace this function."""
    return SubprocessRunner()


def _config_path(args: argparse.Namespace) -> Path:
    if args.config:
        return Path(args.config).expanduser()
    if os.environ.get(CONFIG_ENV):
        return Path(os.environ[CONFIG_ENV]).expanduser()
    return DEFAULT_CONFIG_PATH.expanduser()


def _load(args: argparse.Namespace) -> Config:
    return Config.load(Path(args.config) if args.config else None)


def _store(config: Config) -> Store:
    return open_store(config)


class _NoSteps:
    """A placeholder backend for commands that never run a model step."""

    kind = "none"

    def run_step(self, step: Step, *args: Any, **kwargs: Any) -> StepResult:
        raise BackendError("this command does not run model steps")


def _orchestrator(config: Config, store: Store, *, run_steps: bool) -> Orchestrator:
    if run_steps:
        return Orchestrator.from_config(config, store)
    return Orchestrator(
        store,
        config,
        worker=_NoSteps(),
        reviewer=_NoSteps(),
        channels={c.name: c for c in enabled_channels(config)},
        # The full registry: an approval given here runs the approved action in
        # this process, and that may be a GitHub or connector action.
        registry=build_registry(config),
    )


def _short(text: str | None, limit: int = 70) -> str:
    one_line = " ".join((text or "").split())
    return one_line if len(one_line) <= limit else one_line[: limit - 3] + "..."


def _when(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else "-"


def _answers(store: Store, task_id: int) -> list[str]:
    """Texts of the replies the task actually sent."""
    return [
        str(action.payload.get("text", ""))
        for action in store.list_actions(task_id, ActionStatus.EXECUTED)
        if action.kind in (KIND_REPLY, KIND_NOTIFY)
    ]


def _task_line(task: Task) -> str:
    return f"#{task.id} {task.state.value:<17} {task.requester}: {_short(task.text)}"


def _with_lock(config: Config, work: Callable[[], int]) -> int:
    config.ensure_directories()
    try:
        with WorkerLock(config.lock_path):
            return work()
    except LockBusy:
        print("Another OpenDot worker is running; nothing to do.", file=sys.stderr)
        return 0


# ---------------------------------------------------------------------------
# Setup commands
# ---------------------------------------------------------------------------


def _toml_string(value: str) -> str:
    return json.dumps(value)  # a JSON string is a valid TOML basic string


def cmd_init(args: argparse.Namespace) -> int:
    path = _config_path(args)
    if path.exists() and not args.force:
        raise CliError(f"{path} already exists; pass --force to replace it")
    worker, reviewer = args.worker, args.reviewer
    models = {"worker": args.worker_model, "reviewer": args.reviewer_model}
    if args.demo:
        worker = reviewer = "fake"
        models = {"worker": "", "reviewer": ""}
    for role, kind in (("worker", worker), ("reviewer", reviewer)):
        if kind == "opencode" and "/" not in models[role].strip("/"):
            raise CliError(
                f"the opencode backend needs --{role}-model provider/model; "
                "run `opencode models` to list them"
            )
    state_root = Path(args.state_root).expanduser().resolve() if args.state_root else None

    lines = [
        "# Written by `opendot init`. opendot.example.toml in the repository lists every key.",
        "",
    ]
    if state_root is not None:
        lines += ["[core]", f"state_root = {_toml_string(str(state_root))}", ""]
    lines += [
        "[backend.worker]",
        f"kind = {_toml_string(worker)}",
        f"model = {_toml_string(models['worker'])}",
        "",
        "[backend.reviewer]",
        f"kind = {_toml_string(reviewer)}",
        f"model = {_toml_string(models['reviewer'])}",
        "",
    ]
    if args.demo:
        script_root = state_root or Config.from_dict({}).state_root
        lines += [
            "[backend.fake]",
            f"script = {_toml_string(str(script_root / DEMO_SCRIPT_NAME))}",
            "",
        ]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    os.chmod(path, 0o600)

    config = Config.load(path)
    store = _store(config)
    store.close()
    print(f"Wrote {path}")
    print(f"State folder: {config.state_root}")
    if args.demo:
        script = {
            "work": [
                {
                    "output": {
                        "status": "done",
                        "summary": "Answered with the scripted demo reply.",
                        "reply": DEMO_REPLY,
                        "wait_until": None,
                        "actions": [],
                    },
                    "usage": {"turns": 1},
                }
            ],
            "review": [
                {
                    "output": {
                        "verdicts": [
                            {
                                "action_id": 1,
                                "verdict": "approve",
                                "reason": "A reply in the requester's own thread.",
                            }
                        ]
                    }
                }
            ],
        }
        script_path = config.fake.script
        assert script_path is not None
        script_path.write_text(json.dumps(script, indent=2) + "\n", encoding="utf-8")
        print(f"Demo script: {script_path} (it answers the first task only)")
    if args.config:
        print(f"Next: opendot --config {args.config} doctor")
    else:
        print("Next: opendot doctor")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    config = _load(args)
    store = _store(config)
    print(f"Database {config.db_path} is at schema version {store.schema_version()}.")
    store.close()
    return 0


def _check_image(config: Config, runner: CommandRunner) -> tuple[bool, str]:
    result = runner.run([config.sandbox.docker, "image", "inspect", config.sandbox.image])
    if result.returncode != 0:
        return False, f"image {config.sandbox.image} is not built; run `opendot build-image`"
    return True, f"image {config.sandbox.image} is present"


def doctor_findings(
    config: Config, runner: CommandRunner, env: dict[str, str] | None = None
) -> list[tuple[str, str]]:
    """(level, message) pairs; level is ok, warn or error."""
    env = dict(os.environ) if env is None else env
    found: list[tuple[str, str]] = []
    found.append(("ok", f"config: {config.source_path or 'built-in defaults'}"))

    root = config.state_root
    if not root.is_dir():
        found.append(("error", f"state folder {root} does not exist; run `opendot init`"))
    else:
        mode = root.stat().st_mode & 0o777
        if mode & 0o077:
            found.append(("error", f"state folder {root} has mode {mode:o}; it must be 700"))
        else:
            found.append(("ok", f"state folder {root} (mode 700)"))

    kinds = {config.worker_backend.kind, config.reviewer_backend.kind}
    if config.worker_backend.kind == config.reviewer_backend.kind and "fake" not in kinds:
        found.append(
            (
                "warn",
                "the worker and the reviewer use the same backend; a different vendor "
                "for the reviewer gives a more independent check",
            )
        )
    if "anthropic_api" in kinds:
        found.append(("error", "the anthropic_api backend is not ready yet"))
    if "codex" in kinds:
        auth = config.codex.auth_file
        if auth.is_file():
            found.append(("ok", f"Codex login file {auth}"))
        else:
            found.append(("error", f"Codex login file {auth} is missing; run `codex login`"))
    if "claude_code" in kinds:
        found.append(_claude_login_finding(config, env))
    if "opencode" in kinds:
        auth = config.opencode.auth_file
        providers = sorted(
            {
                choice.model.partition("/")[0]
                for choice in (config.worker_backend, config.reviewer_backend)
                if choice.kind == "opencode"
            }
        )
        try:
            logins = json.loads(auth.read_bytes())
        except (OSError, ValueError):
            logins = None
        if not isinstance(logins, dict):
            found.append(
                ("error", f"opencode login file {auth} is missing; run `opencode auth login`")
            )
        else:
            for provider in providers:
                if isinstance(logins.get(provider), dict):
                    found.append(("ok", f"opencode login for {provider} in {auth}"))
                else:
                    found.append(
                        (
                            "error",
                            f"opencode login file {auth} has no login for {provider}; "
                            f"run `opencode auth login` and choose {provider}",
                        )
                    )
    if "fake" in kinds:
        script = config.fake.script
        if script is not None and not script.is_file():
            found.append(("error", f"fake backend script {script} is missing"))
        else:
            found.append(("warn", "the fake backend is in use; no model will run"))

    if kinds & {"codex", "claude_code", "opencode"}:
        try:
            ok, message = _check_image(config, runner)
        except OSError:
            ok, message = False, f"cannot run {config.sandbox.docker}; is Docker installed?"
        found.append(("ok" if ok else "error", message))
        if config.sandbox.network == "none":
            found.append(
                ("warn", "sandbox.network is none, so the model CLIs cannot reach their API")
            )
        else:
            found.append(
                (
                    "warn",
                    f"step containers use Docker network {config.sandbox.network!r}; they "
                    "can reach any host it allows (see SECURITY.md)",
                )
            )

    if kinds & {"codex", "claude_code"}:
        found += _snap_findings(config, runner)
    for name, ok, detail in feature_doctor_checks(config):
        found.append(("ok" if ok else "error", f"{name}: {detail}"))

    slack = config.slack
    if slack.enabled:
        if slack.bot_token(env):
            found.append(("ok", f"Slack token variable {slack.bot_token_env} is set"))
        else:
            found.append(("error", f"Slack is enabled but {slack.bot_token_env} is not set"))
        if not slack.channels:
            found.append(("warn", "channels.slack.channels is empty; nothing will be polled"))
        if not slack.allowed_users:
            found.append(
                ("warn", "channels.slack.allowed_users is empty, so nobody can give work in Slack")
            )
    if not config.cli.enabled and not slack.enabled:
        found.append(("error", "no channel is enabled"))
    return found


def _snap_findings(config: Config, runner: CommandRunner) -> list[tuple[str, str]]:
    """Findings for Docker installed as a snap package. Empty for other Docker installs."""
    root_dir: str | None = None
    try:
        result = runner.run(
            [config.sandbox.docker, "info", "--format", "{{.DockerRootDir}}"], timeout=30
        )
        if result.returncode == 0:
            root_dir = result.stdout.strip() or None
    except OSError:
        return []
    if not detect_snap_docker(config.sandbox.docker, root_dir):
        return []
    problems = snap_docker_problems(config, True)
    if not problems:
        return [
            (
                "ok",
                "Docker is the snap package; sandbox.no_new_privileges is false and no "
                "mounted folder is under /tmp (see SECURITY.md for the trade-off)",
            )
        ]
    return [("error", f"snap Docker: {problem}") for problem in problems]


def _claude_login_finding(config: Config, env: Mapping[str, str]) -> tuple[str, str]:
    name = config.claude_code.token_env
    token_file = config.claude_code.token_file
    try:
        saved = read_token_file(token_file)
    except (TokenFileError, OSError) as exc:
        return ("error", f"cannot read the saved Claude login: {exc}")
    if env.get(name):
        return ("ok", f"Claude Code token variable {name} is set")
    if saved:
        return ("ok", f"Claude Code login saved in {token_file}")
    return ("error", "no Claude Code login; run `opendot login claude`")


def cmd_login(args: argparse.Namespace) -> int:
    """Save a Claude Code token in backend.claude_code.token_file.

    With a terminal, run `claude setup-token` so you can sign in, then ask for the
    token it printed. Without a terminal, read the token from standard input.
    """
    config = _load(args)
    token_file = config.claude_code.token_file
    if sys.stdin.isatty():
        if not args.skip_setup:
            print("Running `claude setup-token`. Sign in, then copy the token it prints.")
            try:
                code = subprocess.run(["claude", "setup-token"], check=False).returncode
            except FileNotFoundError:
                raise CliError(
                    "the `claude` command is not installed on this machine; install Claude "
                    "Code, or make a token on another machine and run "
                    "`opendot login claude --skip-setup`"
                ) from None
            if code != 0:
                raise CliError(f"`claude setup-token` ended with exit code {code}")
        token = getpass.getpass("Paste the token (it is not shown): ")
    else:
        token = sys.stdin.read()
    try:
        save_token_file(token_file, token)
    except TokenFileError as exc:
        raise CliError(str(exc)) from None
    print(f"Saved the Claude login in {token_file} (only you can read it).")
    print("Every opendot command uses it, including the runs that cron starts.")
    if os.environ.get(config.claude_code.token_env):
        print(
            f"{config.claude_code.token_env} is also set in this shell; when it is set, "
            "it is used instead of the saved file."
        )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = _load(args)
    findings = doctor_findings(config, _runner())
    for level, message in findings:
        print(f"[{level}] {message}")
    errors = sum(1 for level, _ in findings if level == "error")
    if any(message.startswith("snap Docker:") for _, message in findings):
        print()
        print(SNAP_DOCKER_ADVICE)
        print()
    print(f"{errors} error(s).")
    return 1 if errors else 0


def cmd_build_image(args: argparse.Namespace) -> int:
    config = _load(args)
    dockerfile = dockerfile_path().read_text(encoding="utf-8")
    command = build_image_args(config.sandbox.docker, config.sandbox.image)
    print(f"Building {config.sandbox.image} (this downloads packages and takes a few minutes)")
    result = _runner().run(command, input=dockerfile)
    if result.returncode != 0:
        print(result.stdout[-4000:], end="")
        print(result.stderr[-4000:], end="", file=sys.stderr)
        raise CliError(f"docker build failed with exit code {result.returncode}")
    print(f"Built {config.sandbox.image}. Next: opendot verify-image")
    return 0


def cmd_verify_image(args: argparse.Namespace) -> int:
    config = _load(args)
    command = verify_image_args(config.sandbox.docker, config.sandbox.image)
    result = _runner().run(command, timeout=300)
    check = check_verify_output(result.returncode, result.stdout, require_browser=True)
    if check.ok:
        print(f"{config.sandbox.image} has every required tool and runs as a non-root user.")
        return 0
    for problem in check.problems:
        print(f"- {problem}")
    if result.stderr.strip():
        print(result.stderr.strip()[-2000:], file=sys.stderr)
    raise CliError(f"{config.sandbox.image} failed the image check")


# ---------------------------------------------------------------------------
# Running work
# ---------------------------------------------------------------------------


def cmd_task(args: argparse.Namespace) -> int:
    config = _load(args)
    store = _store(config)
    text = " ".join(args.text).strip()
    if not text:
        raise CliError("the task text is empty")
    orchestrator = _orchestrator(config, store, run_steps=False)
    try:
        message = orchestrator.submit(text, thread=args.thread)
    except ChannelError as exc:
        raise CliError(str(exc)) from None
    if message is None:
        raise CliError("the message was already recorded")
    if message.task_id is None:
        print(f"Recorded as {message.kind.value}.")
    elif message.kind.value in ("request", "follow_up"):
        print(f"Task {message.task_id} queued (thread {message.thread}).")
    else:
        print(f"Added to task {message.task_id} as {message.kind.value}.")
    orchestrator.flush_outbox()
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    config = _load(args)
    store = _store(config)

    def work() -> int:
        orchestrator = _orchestrator(config, store, run_steps=False)
        messages = orchestrator.ingest()
        orchestrator.flush_outbox()
        print(f"{len(messages)} new message(s).")
        return 0

    return _with_lock(config, work)


def cmd_run_once(args: argparse.Namespace) -> int:
    config = _load(args)
    store = _store(config)

    def work() -> int:
        orchestrator = _orchestrator(config, store, run_steps=True)
        store.expire_approvals()
        store.wake_due()
        task = orchestrator.run_once()
        orchestrator.flush_outbox()
        if task is None:
            print("No queued task.")
        else:
            print(f"Task {task.id} is now {task.state.value}.")
        return 0

    return _with_lock(config, work)


def cmd_tick(args: argparse.Namespace) -> int:
    config = _load(args)
    store = _store(config)

    def one_pass(orchestrator: Orchestrator) -> None:
        counts = orchestrator.tick(max_tasks=args.max_tasks)
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        print(stamp, " ".join(f"{k}={v}" for k, v in counts.items()), flush=True)

    def work() -> int:
        orchestrator = _orchestrator(config, store, run_steps=True)
        if not args.loop:
            one_pass(orchestrator)
            return 0
        try:
            while True:
                one_pass(orchestrator)
                time.sleep(args.loop)
        except KeyboardInterrupt:
            return 0

    return _with_lock(config, work)


# ---------------------------------------------------------------------------
# Looking at the state
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    config = _load(args)
    store = _store(config)
    counts = store.count_tasks_by_state()
    if not counts:
        print("No tasks yet.")
        return 0
    print("Tasks: " + ", ".join(f"{state.value} {n}" for state, n in sorted(counts.items())))
    for task in store.list_tasks(limit=args.limit):
        print(_task_line(task))
        for answer in _answers(store, task.id):
            print(f"    answer: {answer}")
        if task.last_error and task.state in (TaskState.FAILED, TaskState.STOPPED):
            print(f"    error: {_short(task.last_error, 200)}")
    pending = store.list_approvals(status=ApprovalStatus.PENDING)
    if pending:
        print(f"{len(pending)} approval(s) waiting; see `opendot queue`.")
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    config = _load(args)
    store = _store(config)
    tasks = sorted(store.list_tasks(UNFINISHED, limit=200), key=lambda t: t.id)
    if not tasks:
        print("No unfinished tasks.")
    for task in tasks:
        line = _task_line(task)
        if task.state is TaskState.WAITING:
            line += f" (until {_when(task.wait_until)})"
        print(line)
    pending = store.list_approvals(status=ApprovalStatus.PENDING)
    for approval in pending:
        text = ""
        if approval.action_id is not None:
            action = store.get_action(approval.action_id)
            text = _short(json.dumps(action.payload, ensure_ascii=False), 90)
        print(
            f"approval {approval.id} for task {approval.task_id}: {approval.kind} "
            f"-> {approval.target} {text}"
        )
    if pending:
        print("Decide with `opendot approve N` or `opendot deny N`.")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    config = _load(args)
    store = _store(config)
    task = store.get_task(args.task_id)
    print(f"Task {task.id}: {task.state.value}")
    print(f"  requester: {task.requester} (profile {task.profile})")
    print(f"  where:     {task.channel} {task.conversation} {task.thread or '-'}")
    print(f"  created:   {_when(task.created_at)}   finished: {_when(task.finished_at)}")
    print(
        f"  used:      {task.active_seconds:.1f}s, {task.turns_used} turns, "
        f"{task.tokens_used} tokens"
    )
    if task.schedule_id is not None:
        print(f"  schedule:  {task.schedule_id}")
    if task.parent_task_id is not None:
        print(f"  follows:   task {task.parent_task_id}")
    print(f"  text:      {task.text}")
    if task.summary:
        print(f"  summary:   {task.summary}")
    if task.last_error:
        print(f"  error:     {task.last_error}")
    attempts = store.list_attempts(task.id)
    if attempts:
        print("Steps:")
        for attempt in attempts:
            error = f" ({_short(attempt.error, 100)})" if attempt.error else ""
            print(
                f"  {attempt.attempt_number}. {attempt.step.value} on {attempt.backend}: "
                f"{attempt.status.value}{error}"
            )
    actions = store.list_actions(task.id)
    if actions:
        print("Actions:")
        for action in actions:
            print(
                f"  {action.id}. {action.kind} -> {action.target or '-'} "
                f"[level {action.level.value}, {action.status.value}]"
            )
            print(f"     {_short(json.dumps(action.payload, ensure_ascii=False), 200)}")
            if action.error:
                print(f"     error: {_short(action.error, 200)}")
    approvals = store.list_approvals(task.id)
    if approvals:
        print("Approvals:")
        for approval in approvals:
            print(
                f"  {approval.id}. {approval.kind} {approval.mode.value}: {approval.status.value}"
                + (f" by {approval.decided_by}" if approval.decided_by else "")
            )
    events = list(reversed(store.list_events(task.id, limit=args.events)))
    if events:
        print("Recent events:")
        for event in events:
            detail = _short(json.dumps(event.detail, ensure_ascii=False), 120)
            print(f"  {_when(event.created_at)} {event.kind} {detail}")
    return 0


# ---------------------------------------------------------------------------
# Operator decisions
# ---------------------------------------------------------------------------


def _decide(args: argparse.Namespace, granted: bool) -> int:
    config = _load(args)
    store = _store(config)
    orchestrator = _orchestrator(config, store, run_steps=False)
    try:
        approval = orchestrator.decide(
            args.approval_id, granted=granted, decided_by=config.cli.user, channel="cli"
        )
    except NotAllowedToDecide as exc:
        raise CliError(str(exc)) from None
    word = "granted" if granted else "denied"
    task = store.get_task(approval.task_id)
    print(f"Approval {approval.id} {word}. Task {task.id} is {task.state.value}.")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    return _decide(args, True)


def cmd_deny(args: argparse.Namespace) -> int:
    return _decide(args, False)


def cmd_retry(args: argparse.Namespace) -> int:
    config = _load(args)
    store = _store(config)
    task = store.get_task(args.task_id)
    if not task.state.is_terminal or task.state is TaskState.DONE:
        raise CliError(
            f"task {task.id} is {task.state.value}; only a failed, stopped or "
            "skipped task can be retried"
        )
    store.set_task_state(task.id, TaskState.QUEUED)
    store.log_event("task.retried", {"by": config.cli.user}, task_id=task.id)
    print(f"Task {task.id} queued again.")
    return 0


def cmd_skip(args: argparse.Namespace) -> int:
    config = _load(args)
    store = _store(config)
    task = store.get_task(args.task_id)
    if task.state.is_terminal:
        raise CliError(f"task {task.id} is already {task.state.value}")
    store.set_task_state(task.id, TaskState.SKIPPED, error=f"skipped by {config.cli.user}")
    store.log_event("task.skipped", {"by": config.cli.user}, task_id=task.id)
    print(f"Task {task.id} skipped.")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    config = _load(args)
    store = _store(config)
    orchestrator = _orchestrator(config, store, run_steps=False)
    task = orchestrator.stop_task(args.task_id, by=config.cli.user)
    orchestrator.flush_outbox()
    print(f"Task {task.id} is {task.state.value}.")
    return 0


# ---------------------------------------------------------------------------
# Notes, schedules and rules
# ---------------------------------------------------------------------------


def _operator_profile(config: Config) -> str:
    return f"cli:{config.cli.user}"


def cmd_notes(args: argparse.Namespace) -> int:
    config = _load(args)
    store = _store(config)
    if args.notes_command == "list":
        notes = store.list_notes(args.profile)
        if not notes:
            print("No notes.")
        for note in notes:
            subject = f"[{note.subject}] " if note.subject else ""
            print(f"{note.id}. ({note.profile}, {note.source.value}) {subject}{note.text}")
    elif args.notes_command == "add":
        note = store.add_note(
            args.profile or _operator_profile(config),
            args.text,
            source=NoteSource.OPERATOR,
            subject=args.subject or "",
        )
        store.log_event("note.added", {"note_id": note.id, "by": config.cli.user})
        print(f"Note {note.id} added to {note.profile}.")
    elif args.notes_command == "edit":
        if args.text is None and args.subject is None:
            raise CliError("give --text or --subject")
        note = store.update_note(args.note_id, text=args.text, subject=args.subject)
        store.log_event("note.edited", {"note_id": note.id, "by": config.cli.user})
        print(f"Note {note.id} updated.")
    elif args.notes_command == "rm":
        if not store.delete_note(args.note_id):
            raise CliError(f"note {args.note_id} not found")
        store.log_event("note.removed", {"note_id": args.note_id, "by": config.cli.user})
        print(f"Note {args.note_id} removed.")
    return 0


def _aware(value: str, tz_name: str) -> datetime:
    from zoneinfo import ZoneInfo

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise CliError(f"--until must be an ISO date or time, got {value!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))
    return parsed


def cmd_schedules(args: argparse.Namespace) -> int:
    config = _load(args)
    store = _store(config)
    sub = args.schedules_command
    if sub == "list":
        schedules = store.list_schedules()
        if not schedules:
            print("No schedules.")
        for s in schedules:
            print(
                f"{s.id}. {s.status.value:<6} '{s.cadence}' {s.tz} notify={s.notify_rule.value} "
                f"next={_when(s.next_run_at)} until={_when(s.until)}: {_short(s.what)}"
            )
    elif sub == "add":
        tz = args.tz or config.core.timezone
        until = _aware(args.until, tz) if args.until else None
        destination = Destination("cli", "local", uuid.uuid4().hex)
        schedule = create_schedule(
            store,
            what=args.what,
            cadence=args.cadence,
            tz=tz,
            destination=destination,
            creator=config.cli.user,
            profile=_operator_profile(config),
            notify_rule=NotifyRule(args.notify),
            until=until,
        )
        print(f"Schedule {schedule.id} added; first run {_when(schedule.next_run_at)}.")
    elif sub == "pause":
        print(f"Schedule {pause_schedule(store, args.schedule_id).id} paused.")
    elif sub == "resume":
        schedule = resume_schedule(store, args.schedule_id)
        print(f"Schedule {schedule.id} is {schedule.status.value}.")
    elif sub == "rm":
        schedule = store.get_schedule(args.schedule_id)
        if schedule.status is not ScheduleStatus.ENDED:
            end_schedule(store, schedule.id)
        print(f"Schedule {schedule.id} ended.")
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    config = _load(args)
    store = _store(config)
    sub = args.rules_command
    who = {"channel": "cli", "actor": config.cli.user}
    try:
        if sub == "list":
            rules = store.list_rules()
            if not rules:
                print("No rules. Built-in defaults and fixed floors apply.")
            for rule in rules:
                print(
                    f"{rule.id}. {rule.kind} target={rule.target} level={rule.level.value} "
                    f"({rule.status.value}, from {rule.source.value})"
                )
            if any(r.status is RuleStatus.PENDING for r in rules):
                print("Activate a pending rule with `opendot rules approve N`.")
        elif sub == "add":
            rule = add_operator_rule(
                store, config, args.kind, Level(args.level), target=args.target, **who
            )
            print(f"Rule {rule.id} added: {rule.kind} -> {rule.level.value}.")
        elif sub == "approve":
            rule = approve_operator_rule(store, config, args.rule_id, **who)
            print(f"Rule {rule.id} is active.")
        elif sub == "rm":
            if not remove_operator_rule(store, config, args.rule_id, **who):
                raise CliError(f"rule {args.rule_id} not found")
            print(f"Rule {args.rule_id} removed.")
    except NotOperator as exc:
        raise CliError(str(exc)) from None
    return 0


def cmd_install_cron(args: argparse.Namespace) -> int:
    runner = _runner()
    current = cron.read_crontab(runner)
    if args.remove:
        cron.write_crontab(runner, cron.remove_block(current))
        print("Removed the OpenDot crontab entry.")
        return 0
    config = _load(args)
    config.ensure_directories()
    config_path = config.source_path.resolve() if config.source_path else None
    line = cron.tick_line(
        cron.opendot_command(),
        every_minutes=args.every,
        config_path=config_path,
        log_path=config.logs_dir / "tick.log",
    )
    if args.print_only:
        print(line)
        return 0
    cron.write_crontab(runner, cron.with_block(current, line))
    print("Installed:")
    print(line)
    kinds = {config.worker_backend.kind, config.reviewer_backend.kind}
    if "claude_code" in kinds and _claude_login_finding(config, {})[0] != "ok":
        print(
            "Note: cron does not read your shell profile, so it cannot see "
            f"{config.claude_code.token_env}. Run `opendot login claude` to save the login."
        )
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opendot",
        description=help.DESCRIPTION,
        epilog=help.GETTING_STARTED,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"opendot {__version__}")
    parser.add_argument(
        "--config", help=f"config file (default: ${CONFIG_ENV} or {DEFAULT_CONFIG_PATH})"
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    def add(name: str, handler: Callable[[argparse.Namespace], int]) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help.summary(name), description=help.summary(name))
        sub.set_defaults(handler=handler)
        return sub

    backend_kinds = ["codex", "claude_code", "opencode", "fake"]
    p = add("init", cmd_init)
    p.add_argument("--state-root", help="folder for the database, runs and logs")
    p.add_argument("--worker", choices=backend_kinds, default="codex")
    p.add_argument("--reviewer", choices=backend_kinds, default="claude_code")
    p.add_argument(
        "--worker-model", default="", help="model for the worker; opencode needs provider/model"
    )
    p.add_argument(
        "--reviewer-model", default="", help="model for the reviewer; opencode needs provider/model"
    )
    p.add_argument(
        "--demo",
        action="store_true",
        help="use the fake backend with a scripted answer; needs no login or Docker",
    )
    p.add_argument("--force", action="store_true", help="replace an existing config file")

    add("migrate", cmd_migrate)
    p = add("login", cmd_login)
    p.add_argument("service", choices=["claude"], help="the login to save")
    p.add_argument(
        "--skip-setup",
        action="store_true",
        help="do not run `claude setup-token`; only ask for a token you already have",
    )
    add("doctor", cmd_doctor)
    add("build-image", cmd_build_image)
    add("verify-image", cmd_verify_image)

    p = add("task", cmd_task)
    p.add_argument("text", nargs="+", help="what you want done")
    p.add_argument("--thread", help="reply in the thread of an earlier task instead")

    add("ingest", cmd_ingest)
    add("run-once", cmd_run_once)
    p = add("tick", cmd_tick)
    p.add_argument("--max-tasks", type=int, default=5, help="task turns per pass (default 5)")
    p.add_argument(
        "--loop",
        type=float,
        metavar="SECONDS",
        help="keep running, pausing this many seconds between passes",
    )

    p = add("status", cmd_status)
    p.add_argument("--limit", type=int, default=10, help="how many recent tasks to list")
    add("queue", cmd_queue)
    p = add("show", cmd_show)
    p.add_argument("task_id", type=int)
    p.add_argument("--events", type=int, default=20, help="how many recent events to list")

    for name, handler in (("approve", cmd_approve), ("deny", cmd_deny)):
        add(name, handler).add_argument("approval_id", type=int)
    for name, handler in (("retry", cmd_retry), ("skip", cmd_skip), ("stop", cmd_stop)):
        add(name, handler).add_argument("task_id", type=int)

    p = add("notes", cmd_notes)
    notes = p.add_subparsers(dest="notes_command", required=True)
    n = notes.add_parser("list", help="list notes")
    n.add_argument("--profile", help="only this profile, for example slack:U0EXAMPLE")
    n = notes.add_parser("add", help="add a note")
    n.add_argument("text")
    n.add_argument("--subject")
    n.add_argument("--profile", help="default: cli:<channels.cli.user>")
    n = notes.add_parser("edit", help="change a note")
    n.add_argument("note_id", type=int)
    n.add_argument("--text")
    n.add_argument("--subject")
    n = notes.add_parser("rm", help="delete a note")
    n.add_argument("note_id", type=int)

    p = add("schedules", cmd_schedules)
    schedules = p.add_subparsers(dest="schedules_command", required=True)
    schedules.add_parser("list", help="list schedules")
    s = schedules.add_parser("add", help="add a schedule whose results print on this machine")
    s.add_argument("--what", required=True, help="the task text for each run")
    s.add_argument("--cadence", required=True, help='five-field cron expression, e.g. "0 9 * * 1"')
    s.add_argument("--tz", help="IANA time zone (default: core.timezone)")
    s.add_argument("--until", help="ISO date or time after which the schedule ends")
    s.add_argument("--notify", choices=[r.value for r in NotifyRule], default="always")
    for name in ("pause", "resume", "rm"):
        schedules.add_parser(name, help=f"{name} a schedule").add_argument("schedule_id", type=int)

    p = add("rules", cmd_rules)
    rules = p.add_subparsers(dest="rules_command", required=True)
    rules.add_parser("list", help="list rules")
    r = rules.add_parser("add", help="add an active rule")
    r.add_argument("kind", help='an action kind, a prefix such as "note.*", or "*"')
    r.add_argument("level", choices=[level.value for level in Level])
    r.add_argument("--target", default="*", help='exact target, or "*" (default)')
    r = rules.add_parser("approve", help="activate a pending rule")
    r.add_argument("rule_id", type=int)
    r = rules.add_parser("rm", help="delete a rule")
    r.add_argument("rule_id", type=int)

    p = add("install-cron", cmd_install_cron)
    p.add_argument("--every", type=int, default=1, help="minutes between runs (default 1)")
    p.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="print the crontab line and change nothing",
    )
    p.add_argument("--remove", action="store_true", help="remove the OpenDot entry")

    # github, browser and connectors, plus `init --with-factiq`.
    register_feature_cli(commands)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except NotFound as exc:
        print(f"opendot: not found: {exc}", file=sys.stderr)
        return 1
    except (CliError, ConfigError, StoreError, ScheduleError, cron.CronError) as exc:
        print(f"opendot: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"opendot: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
