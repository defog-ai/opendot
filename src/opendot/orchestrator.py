"""The orchestrator: takes messages in, runs steps, checks actions, and reports.

One task moves through these stages:

1. intake: a channel message becomes a task, or a note on an existing task;
2. work: the worker backend runs one step in its container;
3. validation: the output must match schemas/work.schema.json; output that does
   not match is recorded and never acted on;
4. preparation: the host builds each proposed action with its own handler;
   an unknown kind or bad fields are refused;
5. review: an independent reviewer approves, denies or escalates each action;
6. rules: the operator's rules and fixed floors give each action a level;
7. approval: an action at level ask parks the task until the requester answers;
8. act: allowed and approved actions run;
9. report: replies and host notices go out through the outbox;
10. reflect (optional): after a task with feedback, a reflect step proposes
    note changes, which go through the same checks.

Order detail: the rules give each action its level before the review, because
the reviewer is told that level, and a verdict can only make it stricter.

A task asks to wait with status "wait" and wait_until. It is parked in state
waiting; tick() wakes it when the time comes and the next step resumes the same
backend session. Budgets limit a task's active time, steps, turns and tokens.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

import jsonschema

from opendot import instructions
from opendot.actions import (
    KIND_NOTE_WRITE,
    KIND_NOTIFY,
    KIND_REPLY,
    ActionContext,
    ActionError,
    ActionRegistry,
    PreparedAction,
    payload_digest,
)
from opendot.approvals import (
    ApprovalMismatch,
    NotAllowedToDecide,
    action_matches_approval,
    consume_approval,
    request_approval,
)
from opendot.approvals import decide as decide_approval
from opendot.backends import (
    Backend,
    BackendError,
    StepInterrupted,
    StepLimits,
    StepTimedOut,
)
from opendot.channels import Channel, ChannelError
from opendot.extensions import ExtensionError, StepContext, StepExtensions, with_prompt_notes
from opendot.models import (
    ActionRecord,
    ActionStatus,
    Approval,
    ApprovalStatus,
    Attempt,
    AttemptStatus,
    Destination,
    IncomingMessage,
    Level,
    Message,
    MessageKind,
    Mount,
    RuleStatus,
    Step,
    StepPlan,
    Task,
    TaskSource,
    TaskState,
    from_iso,
)
from opendot.notes import render_snapshot, snapshot_for_task
from opendot.reviewer import ReviewItem, review_actions
from opendot.rules import RuleEngine, is_operator
from opendot.store import LeaseLost, NotFound, StoreError

if TYPE_CHECKING:
    from opendot.config import Config
    from opendot.store import Store

__all__ = [
    "HELP_TEXT",
    "LONG_TEXT_CHARS",
    "Command",
    "Orchestrator",
    "parse_command",
]

LONG_TEXT_CHARS = 12_000  # longer outbox texts are uploaded as a file
OUTBOX_MAX_ATTEMPTS = 5
ACK_REACTION = "eyes"
FEEDBACK_EVENT = "feedback.received"

HELP_TEXT = (
    "Start a task with a new message. In a task's thread you can reply with:\n"
    "- an answer or a correction, which the task reads before its next step;\n"
    "- stop (or cancel): end the task;\n"
    "- approve N / deny N: decide on approval request N;\n"
    "- pause / resume: ignore or follow this thread again;\n"
    "- queue: list unfinished tasks;\n"
    "- help: show this text.\n"
    f"To start new work in the thread of a finished task, mention {instructions.MENTION_MARKER}."
)

_APPROVAL_RE = re.compile(r"^(approve|deny)\s+#?(\d+)$")
_SIMPLE_COMMANDS = {
    "stop": "stop",
    "cancel": "stop",
    "queue": "queue",
    "status": "queue",
    "pause": "pause",
    "resume": "resume",
    "help": "help",
}


class Command:
    """A parsed thread command. name is stop, queue, pause, resume, help, approve or deny."""

    def __init__(self, name: str, approval_id: int | None = None):
        self.name = name
        self.approval_id = approval_id

    def __repr__(self) -> str:
        return f"Command({self.name!r}, {self.approval_id!r})"


def _strip_marker(text: str) -> str:
    return " ".join(text.replace(instructions.MENTION_MARKER, " ").split())


def parse_command(text: str) -> Command | None:
    """A command when the whole message is one, else None."""
    words = _strip_marker(text).lower().strip(" .!")
    if words in _SIMPLE_COMMANDS:
        return Command(_SIMPLE_COMMANDS[words])
    match = _APPROVAL_RE.match(words)
    if match:
        return Command(match.group(1), int(match.group(2)))
    return None


def _default_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class Orchestrator:
    """Runs tasks for one store. One process runs one Orchestrator at a time.

    worker, reviewer: the backends for the work (and reflect) step and the review step.
    channels: channel objects by kind ("cli", "slack").
    registry: the host's action handlers; opendot.actions.build_registry(config)
        in production.
    extensions: the step extensions that prepare each work step (repository
        copies, the browser, the connector gateway); none by default.
    """

    def __init__(
        self,
        store: Store,
        config: Config,
        *,
        worker: Backend,
        reviewer: Backend,
        channels: Mapping[str, Channel],
        registry: ActionRegistry,
        owner: str | None = None,
        host_env: Mapping[str, str] | None = None,
        poll_seconds: float = 30.0,
        extensions: StepExtensions | None = None,
    ):
        self.store = store
        self.config = config
        self.worker = worker
        self.reviewer = reviewer
        self.channels = dict(channels)
        self.registry = registry
        self.owner = owner or _default_owner()
        self.host_env = os.environ if host_env is None else host_env
        self.poll_seconds = poll_seconds
        self.extensions = extensions or StepExtensions([])
        self._ingesting = False
        for channel in self.channels.values():
            if hasattr(channel, "store") and channel.store is None:
                channel.store = store

    @classmethod
    def from_config(cls, config: Config, store: Store) -> Orchestrator:
        """The production set-up: configured backends, enabled channels, the
        built-in actions plus those of the configured features, and the step
        extensions of the configured features."""
        from opendot.actions import build_registry
        from opendot.backends import create_backend
        from opendot.channels import enabled_channels
        from opendot.rules import sync_config_rules

        sync_config_rules(store, config)
        return cls(
            store,
            config,
            worker=create_backend(config, "worker"),
            reviewer=create_backend(config, "reviewer"),
            channels={c.name: c for c in enabled_channels(config)},
            registry=build_registry(config),
            extensions=StepExtensions.from_config(config),
        )

    # -- helpers ---------------------------------------------------------------

    @property
    def lease_seconds(self) -> int:
        return self.config.core.lease_minutes * 60

    def notice(self, destination: Destination, text: str, *, task_id: int | None = None) -> None:
        """A host-written message. It goes straight to the outbox, without review."""
        self.store.enqueue_outbox(destination, text, task_id=task_id)

    def _context(self, task: Task) -> ActionContext:
        schedule = None
        if task.schedule_id is not None:
            try:
                schedule = self.store.get_schedule(task.schedule_id)
            except NotFound:
                schedule = None
        return ActionContext(
            task=task,
            store=self.store,
            config=self.config,
            channels=self.channels,
            schedule=schedule,
        )

    def _still_running(self, task_id: int) -> bool:
        task = self.store.get_task(task_id)
        return task.state is TaskState.RUNNING and task.lease_owner == self.owner

    def _finish(
        self,
        task: Task,
        state: TaskState,
        *,
        error: str | None = None,
        summary: str | None = None,
        wait_until: Any = None,
    ) -> Task | None:
        """Release the lease into `state`. None when someone else already moved the task."""
        try:
            return self.store.release_task(
                task.id, self.owner, state, error=error, summary=summary, wait_until=wait_until
            )
        except LeaseLost:
            self.store.log_event("task.lease_lost", {"state": state.value}, task_id=task.id)
            return None

    # -- intake ----------------------------------------------------------------

    def ingest(self) -> list[Message]:
        """Fetch new messages from every channel and take each one in."""
        if self._ingesting:
            return []
        self._ingesting = True
        recorded: list[Message] = []
        try:
            for channel in self.channels.values():
                try:
                    incoming = channel.fetch_new()
                except ChannelError as exc:
                    self.store.log_event(
                        "channel.fetch_failed", {"channel": channel.name, "error": str(exc)}
                    )
                    continue
                for message in incoming:
                    stored = self.intake(message)
                    if stored is not None:
                        recorded.append(stored)
                acknowledge = getattr(channel, "acknowledge", None)
                if acknowledge is not None:
                    acknowledge()
        finally:
            self._ingesting = False
        return recorded

    def submit(self, text: str, *, thread: str | None = None) -> Message | None:
        """Take in a message typed on the command line. Returns the stored message."""
        channel = self.channels.get("cli")
        if channel is None or not hasattr(channel, "message"):
            raise ChannelError("the command-line channel is not enabled")
        return self.intake(channel.message(text, thread=thread))

    def intake(self, message: IncomingMessage) -> Message | None:
        """Classify one message, store it, and apply it. None for a duplicate."""
        if self.store.has_message(message.channel, message.external_id):
            return None
        text = message.text.strip()
        if message.attachments:
            names = [
                f"{a.get('name')} ({a.get('mimetype')}, {a.get('size')} bytes)"
                for a in message.attachments
            ]
            text += "\n[attached: " + "; ".join(names) + "]"
        message = IncomingMessage(
            channel=message.channel,
            external_id=message.external_id,
            conversation=message.conversation,
            thread=message.thread,
            author=message.author,
            text=text,
            is_reply=message.is_reply,
            received_at=message.received_at,
            attachments=message.attachments,
        )
        where = Destination(message.channel, message.conversation, message.thread)
        command = parse_command(text)

        if self.store.is_thread_paused(message.channel, message.conversation, message.thread):
            if command is None or command.name not in ("resume", "help"):
                return self.store.record_message(message, MessageKind.IGNORED)

        if command is not None:
            return self._command(message, command, where)

        if not message.is_reply:
            return self._new_task(message, MessageKind.REQUEST)

        active = self.store.find_thread_task(
            message.channel, message.conversation, message.thread, active_only=True
        )
        if active is not None:
            if message.author != active.requester:
                return self.store.record_message(message, MessageKind.IGNORED, task_id=active.id)
            return self._feedback(message, active)

        if instructions.MENTION_MARKER not in text:
            return self.store.record_message(message, MessageKind.IGNORED)
        previous = self.store.find_thread_task(
            message.channel, message.conversation, message.thread
        )
        if previous is None:
            return self._new_task(message, MessageKind.REQUEST)
        return self._new_task(message, MessageKind.FOLLOW_UP, parent=previous)

    def _new_task(
        self, message: IncomingMessage, kind: MessageKind, *, parent: Task | None = None
    ) -> Message | None:
        text = message.text.replace(instructions.MENTION_MARKER, "").strip() or message.text
        with self.store.transaction():
            stored = self.store.record_message(message, kind)
            if stored is None:
                return None
            task = self.store.create_task(
                requester=message.author,
                text=text,
                channel=message.channel,
                conversation=message.conversation,
                thread=message.thread,
                source=TaskSource.CLI if message.channel == "cli" else TaskSource.CHANNEL,
                parent_task_id=parent.id if parent is not None else None,
            )
            self.store.set_message_task(stored.id, task.id)
            self.store.enqueue_outbox(
                Destination(message.channel, message.conversation, message.thread),
                "",
                task_id=task.id,
                reaction=ACK_REACTION,
                reply_to=message.external_id,
            )
            self.store.log_event(
                "task.created",
                {"message_id": stored.id, "parent_task_id": task.parent_task_id},
                task_id=task.id,
            )
        return self.store.get_message(stored.id)

    def _feedback(self, message: IncomingMessage, task: Task) -> Message | None:
        kind = MessageKind.CLARIFY if task.state is TaskState.AWAITING_REPLY else MessageKind.STEER
        stored = self.store.record_message(message, kind, task_id=task.id)
        if stored is None:
            return None
        self.store.log_event(
            FEEDBACK_EVENT,
            {"message_id": stored.id, "kind": kind.value, "text": message.text},
            task_id=task.id,
        )
        if task.state in (TaskState.AWAITING_REPLY, TaskState.WAITING):
            self.store.set_task_state(task.id, TaskState.QUEUED)
        return stored

    def _command(
        self, message: IncomingMessage, command: Command, where: Destination
    ) -> Message | None:
        active = None
        if message.is_reply:
            active = self.store.find_thread_task(
                message.channel, message.conversation, message.thread, active_only=True
            )
        stored = self.store.record_message(
            message, MessageKind.COMMAND, task_id=active.id if active else None
        )
        if stored is None:
            return None
        self.store.mark_messages_applied([stored.id])
        name = command.name

        if name == "help":
            self.notice(where, HELP_TEXT)
        elif name == "queue":
            self.notice(where, self._queue_text())
        elif name == "pause":
            self.store.pause_thread(
                message.channel, message.conversation, message.thread, message.author
            )
            self.notice(where, "Paused. I will ignore this thread until someone says resume.")
        elif name == "resume":
            if self.store.resume_thread(message.channel, message.conversation, message.thread):
                self.notice(where, "Resumed. I am following this thread again.")
        elif name == "stop":
            if active is None:
                self.notice(where, "There is no unfinished task in this thread.")
            elif message.author == active.requester or is_operator(
                self.config, channel=message.channel, actor=message.author
            ):
                self.stop_task(active.id, by=message.author)
            else:
                self.store.log_event("task.stop_ignored", {"by": message.author}, task_id=active.id)
        elif command.approval_id is not None:
            self._decide_from_message(message, command, where)
        return self.store.get_message(stored.id)

    def _decide_from_message(
        self, message: IncomingMessage, command: Command, where: Destination
    ) -> None:
        approval_id = command.approval_id
        granted = command.name == "approve"
        try:
            self.decide(
                approval_id, granted=granted, decided_by=message.author, channel=message.channel
            )
        except NotAllowedToDecide:
            self.notice(where, f"Only the requester can decide approval {approval_id}.")
            return
        except (NotFound, StoreError) as exc:
            self.notice(where, f"Approval {approval_id} cannot be decided: {exc}")
            return
        self.notice(where, f"Approval {approval_id} {'granted' if granted else 'denied'}.")

    def _queue_text(self) -> str:
        unfinished = [
            TaskState.QUEUED,
            TaskState.RUNNING,
            TaskState.AWAITING_APPROVAL,
            TaskState.AWAITING_REPLY,
            TaskState.WAITING,
        ]
        tasks = self.store.list_tasks(unfinished, limit=20)
        if not tasks:
            return "No unfinished tasks."
        lines = [f"{len(tasks)} unfinished task(s):"]
        for task in sorted(tasks, key=lambda t: t.id):
            lines.append(f"- task {task.id}: {task.state.value}")
        return "\n".join(lines)

    # -- operator and requester decisions --------------------------------------

    def decide(self, approval_id: int, *, granted: bool, decided_by: str, channel: str) -> Approval:
        """Record a decision; requeue the task when no approval is left pending."""
        approval = decide_approval(
            self.store,
            self.config,
            approval_id,
            granted=granted,
            decided_by=decided_by,
            channel=channel,
        )
        task = self.store.get_task(approval.task_id)
        pending = self.store.list_approvals(task.id, ApprovalStatus.PENDING)
        if task.state is TaskState.AWAITING_APPROVAL and not pending:
            self.store.set_task_state(task.id, TaskState.QUEUED)
        return approval

    def stop_task(self, task_id: int, *, by: str) -> Task:
        """End a task now. A step in progress sees the change and stops."""
        task = self.store.get_task(task_id)
        if task.state.is_terminal:
            return task
        stopped = self.store.set_task_state(task_id, TaskState.STOPPED, error=f"stopped by {by}")
        self.store.log_event("task.stopped", {"by": by}, task_id=task_id)
        self.notice(task.destination, f"Task {task_id} stopped.", task_id=task_id)
        return stopped

    # -- the loop --------------------------------------------------------------

    def tick(self, max_tasks: int = 5) -> dict[str, int]:
        """One pass: expire approvals, wake tasks, start schedules, read channels,
        run up to max_tasks task turns, and deliver the outbox."""
        from opendot.schedules import run_due_schedules

        expired = self.store.expire_approvals()
        woken = len(self.store.wake_due())
        started = len(run_due_schedules(self.store))
        received = len(self.ingest())
        ran = 0
        for _ in range(max_tasks):
            if self.run_once() is None:
                break
            ran += 1
        delivered = self.flush_outbox()
        return {
            "approvals_expired": expired,
            "woken": woken,
            "schedules_started": started,
            "messages": received,
            "task_turns": ran,
            "delivered": delivered,
        }

    def run_once(self) -> Task | None:
        """Claim the oldest queued task and move it forward one turn."""
        task = self.store.claim_next(self.owner, self.lease_seconds)
        if task is None:
            return None
        try:
            self._advance(task)
        except Exception as exc:
            self.store.log_event(
                "task.crashed", {"error": f"{type(exc).__name__}: {exc}"}, task_id=task.id
            )
            if self._finish(task, TaskState.FAILED, error=f"{type(exc).__name__}: {exc}"):
                self.notice(task.destination, f"Task {task.id} failed.", task_id=task.id)
        return self.store.get_task(task.id)

    def _advance(self, task: Task) -> None:
        waiting = self.store.list_actions(task.id, ActionStatus.AWAITING_APPROVAL)
        if waiting:
            if not self._resolve_approvals(task, waiting):
                self._finish(task, TaskState.AWAITING_APPROVAL)
                return
            self._after_actions(task)
            return

        budget = self._budget_exceeded(task)
        if budget is not None:
            self._stop_for(task, f"budget reached: {budget}")
            return
        attempt = self._work_step(task)
        if attempt is None:
            return
        self._after_proposals(task, attempt)

    def _after_proposals(self, task: Task, attempt: Attempt) -> None:
        if not self._still_running(task.id):
            return
        if self.store.list_actions(task.id, ActionStatus.AWAITING_APPROVAL):
            if self.store.list_approvals(task.id, ApprovalStatus.PENDING):
                self._finish(task, TaskState.AWAITING_APPROVAL)
            else:
                self._finish(task, TaskState.QUEUED)
            return
        self._after_actions(task)

    def _last_step(self, task: Task) -> Attempt | None:
        for attempt in reversed(self.store.list_attempts(task.id)):
            if attempt.step in (Step.WORK, Step.REFLECT) and attempt.status is (
                AttemptStatus.SUCCEEDED
            ):
                return attempt
        return None

    def _after_actions(self, task: Task) -> None:
        """Choose the next state from the last successful work or reflect output."""
        if not self._still_running(task.id):
            return
        attempt = self._last_step(task)
        if attempt is None or attempt.output is None:
            self._fail(task, "no step output to continue from")
            return
        output = attempt.output
        summary = output.get("summary") or None
        if attempt.step is Step.REFLECT:
            self._done(task, summary=None)
            return
        status = output["status"]
        if status == "done":
            if self._should_reflect(task):
                reflect = self._reflect_step(task, output)
                if reflect is not None:
                    self._after_proposals(task, reflect)
                return
            self._done(task, summary=summary)
        elif status == "needs_input":
            if task.schedule_id is not None:
                self._done(task, summary=summary)
            else:
                self._finish(task, TaskState.AWAITING_REPLY, summary=summary)
        elif status == "wait":
            self._wait(task, output.get("wait_until"), summary)
        else:  # continue
            self._finish(task, TaskState.QUEUED, summary=summary)

    def _wait(self, task: Task, wait_until: Any, summary: str | None) -> None:
        try:
            when = from_iso(str(wait_until))
        except (TypeError, ValueError):
            # The value is model output, and failure notices are not reviewed, so it
            # goes only into the event log.
            self.store.log_event(
                "task.bad_wait_until", {"value": str(wait_until)[:200]}, task_id=task.id
            )
            self._fail(task, "wait_until is not an ISO 8601 time with a time zone")
            return
        if when <= self.store.clock.now():
            self._finish(task, TaskState.QUEUED, summary=summary)
            return
        self._finish(task, TaskState.WAITING, summary=summary, wait_until=when)
        self.store.log_event("task.waiting", {"wait_until": when.isoformat()}, task_id=task.id)

    def _done(self, task: Task, *, summary: str | None) -> None:
        sent = [
            a
            for a in self.store.list_actions(task.id, ActionStatus.EXECUTED)
            if a.kind in (KIND_REPLY, KIND_NOTIFY)
        ]
        if self._finish(task, TaskState.DONE, summary=summary) is None:
            return
        if not sent and task.schedule_id is None:
            self.notice(
                task.destination,
                f"Task {task.id} finished without a reply. Ask me in this thread if you "
                "need the result.",
                task_id=task.id,
            )

    def _fail(self, task: Task, error: str) -> None:
        if self._finish(task, TaskState.FAILED, error=error) is not None:
            self.notice(task.destination, f"Task {task.id} failed: {error}", task_id=task.id)

    def _stop_for(self, task: Task, reason: str) -> None:
        if self._finish(task, TaskState.STOPPED, error=reason) is not None:
            self.store.log_event("task.stopped", {"reason": reason}, task_id=task.id)
            self.notice(task.destination, f"Task {task.id} stopped: {reason}.", task_id=task.id)

    # -- budgets ---------------------------------------------------------------

    def _budget_exceeded(self, task: Task) -> str | None:
        limits = self.config.limits
        steps = sum(1 for a in self.store.list_attempts(task.id) if a.step is Step.WORK)
        if limits.max_steps_per_task and steps >= limits.max_steps_per_task:
            return f"{steps} work steps"
        if limits.task_minutes and task.active_seconds >= limits.task_minutes * 60:
            return f"{limits.task_minutes} minutes of active time"
        if limits.max_turns_per_task and task.turns_used >= limits.max_turns_per_task:
            return f"{task.turns_used} model turns"
        if limits.max_tokens_per_task and task.tokens_used >= limits.max_tokens_per_task:
            return f"{task.tokens_used} tokens"
        return None

    def _step_limits(self, task: Task) -> StepLimits:
        limits = self.config.limits
        timeouts = []
        if limits.step_minutes:
            timeouts.append(limits.step_minutes * 60.0)
        if limits.task_minutes:
            timeouts.append(max(1.0, limits.task_minutes * 60.0 - task.active_seconds))
        max_turns = None
        if limits.max_turns_per_task:
            max_turns = max(1, limits.max_turns_per_task - task.turns_used)
        return StepLimits(timeout_seconds=min(timeouts) if timeouts else None, max_turns=max_turns)

    def _should_stop(self, task_id: int) -> Callable[[], bool]:
        """Polled by the backend while a step runs. Reads channels now and then, so a
        stop note in a thread reaches a running step, and keeps the lease alive."""
        last_poll = time.monotonic()
        interval = min(self.poll_seconds, self.lease_seconds / 3)

        def should_stop() -> bool:
            nonlocal last_poll
            if time.monotonic() - last_poll >= interval:
                last_poll = time.monotonic()
                try:
                    self.ingest()
                except Exception as exc:  # reading channels must not end the step
                    self.store.log_event("channel.poll_failed", {"error": str(exc)})
                try:
                    self.store.renew_lease(task_id, self.owner, self.lease_seconds)
                except LeaseLost:
                    return True
            return not self._still_running(task_id)

        return should_stop

    # -- model steps -----------------------------------------------------------

    def _env(self) -> dict[str, str]:
        return {
            name: self.host_env[name]
            for name in self.config.sandbox.env_allowlist
            if name in self.host_env
        }

    def _mounts(self) -> list[Mount]:
        return [
            Mount(host=m.host, container=m.container, read_only=True)
            for m in self.config.sandbox.readonly_mounts
        ]

    def _resume_id(self, task: Task) -> str | None:
        if task.backend_thread_id:
            return task.backend_thread_id
        if task.parent_task_id is not None:
            try:
                parent = self.store.get_task(task.parent_task_id)
            except NotFound:
                return None
            # The parent's session holds the parent requester's notes. A follow-up
            # from someone else starts a new session with that person's own notes.
            if parent.profile != task.profile:
                return None
            return parent.backend_thread_id
        return None

    def _action_results(self, task: Task) -> list[dict[str, Any]]:
        """Outcomes of actions from the latest step, for the next prompt."""
        work = [a for a in self.store.list_attempts(task.id) if a.step is Step.WORK]
        if not work:
            return []
        previous = work[-1].id
        results = []
        for action in self.store.list_actions(task.id):
            if action.attempt_id != previous:
                continue
            results.append(
                {
                    "action_id": action.id,
                    "kind": action.kind,
                    "status": action.status.value,
                    "detail": action.result or {},
                    "error": action.error,
                }
            )
        return results

    def _run_model(
        self,
        task: Task,
        step: Step,
        backend: Backend,
        prompt: str,
        *,
        env: Mapping[str, str],
        mounts: list[Mount],
        resume_id: str | None,
        plan: StepPlan | None = None,
    ) -> tuple[Attempt, dict[str, Any] | None]:
        """Run one step and validate it. Returns the attempt and the output when valid."""
        schema = instructions.load_schema(step)
        attempt = self.store.start_attempt(task.id, step, backend.kind)
        started = time.monotonic()
        output: dict[str, Any] | None = None
        usage: dict[str, Any] = {}
        thread_id: str | None = None
        status = AttemptStatus.SUCCEEDED
        error: str | None = None
        try:
            result = backend.run_step(
                step,
                prompt,
                schema,
                env,
                mounts,
                resume_id,
                limits=self._step_limits(task),
                should_stop=self._should_stop(task.id),
                plan=plan,
            )
            usage = result.usage or {}
            thread_id = result.thread_id
            output = result.output if isinstance(result.output, dict) else None
            jsonschema.validate(result.output, schema)
        except StepInterrupted as exc:
            status, error = AttemptStatus.INTERRUPTED, str(exc)
        except StepTimedOut as exc:
            status, error = AttemptStatus.TIMED_OUT, str(exc)
        except BackendError as exc:
            status, error = AttemptStatus.FAILED, str(exc)
        except jsonschema.ValidationError as exc:
            status, error = AttemptStatus.SCHEMA_ERROR, exc.message
        elapsed = time.monotonic() - started
        attempt = self.store.finish_attempt(
            attempt.id,
            status,
            output=output,
            usage=usage or None,
            backend_thread_id=thread_id,
            error=error,
        )
        self.store.add_usage(
            task.id,
            turns=int(usage.get("turns") or 0),
            tokens=int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0),
            seconds=elapsed,
        )
        return attempt, output if status is AttemptStatus.SUCCEEDED else None

    def _step_failed(self, task: Task, attempt: Attempt) -> None:
        if attempt.status is AttemptStatus.INTERRUPTED:
            if self._still_running(task.id):
                self._stop_for(task, "stopped during a step")
            return
        if not self._still_running(task.id):
            return
        reasons = {
            AttemptStatus.TIMED_OUT: "the step ran out of time",
            AttemptStatus.SCHEMA_ERROR: "the model's output did not match the expected format",
            AttemptStatus.FAILED: "the model step failed",
        }
        self.store.update_task(task.id, last_error=attempt.error)
        self._fail(task, reasons.get(attempt.status, "the model step failed"))

    def _work_step(self, task: Task) -> Attempt | None:
        stops = self.store.pending_messages(task.id, [MessageKind.STOP])
        if stops:
            self.store.mark_messages_applied([m.id for m in stops])
            self._stop_for(task, "stopped by the requester")
            return None
        messages = self.store.pending_messages(
            task.id,
            [MessageKind.REQUEST, MessageKind.FOLLOW_UP, MessageKind.CLARIFY, MessageKind.STEER],
        )
        shown = [m for m in messages if m.kind in (MessageKind.CLARIFY, MessageKind.STEER)]
        resume_id = self._resume_id(task)
        prompt = instructions.work_prompt(
            task,
            now=self.store.clock.now(),
            registry=self.registry,
            new_session=resume_id is None,
            notes_snapshot=snapshot_for_task(self.store, task) if resume_id is None else "",
            messages=shown,
            action_results=self._action_results(task),
        )
        step_ctx = StepContext(
            task=task,
            step=Step.WORK,
            store=self.store,
            config=self.config,
            backend_kind=self.worker.kind,
        )
        try:
            plan = self.extensions.begin(step_ctx)
        except ExtensionError as exc:
            self.store.log_event("step.prepare_failed", {"error": str(exc)}, task_id=task.id)
            self.store.update_task(task.id, last_error=str(exc))
            self._fail(task, f"the host could not prepare the work step: {exc}")
            return None
        attempt: Attempt | None = None
        try:
            attempt, output = self._run_model(
                task,
                Step.WORK,
                self.worker,
                with_prompt_notes(prompt, plan),
                env=self._env(),
                mounts=self._mounts(),
                resume_id=resume_id,
                plan=plan,
            )
        finally:
            self.extensions.finish(step_ctx, plan, attempt)
        if output is None:
            self._step_failed(task, attempt)
            return None
        if attempt.backend_thread_id:
            self.store.update_task(task.id, backend_thread_id=attempt.backend_thread_id)
        self.store.mark_messages_applied([m.id for m in messages])
        if not self._still_running(task.id):
            return None
        task = self.store.get_task(task.id)

        proposals: list[tuple[dict[str, Any] | None, str, str]] = []
        for item in output["actions"]:
            raw = item["arguments_json"]
            try:
                fields = json.loads(raw) if raw.strip() else {}
            except ValueError:
                fields = None
            if not isinstance(fields, dict):
                proposals.append((None, item["kind"], raw))
            else:
                proposals.append(({**fields, "kind": item["kind"]}, item["kind"], raw))
        reply = output["reply"].strip()
        if reply:
            kind = KIND_NOTIFY if task.schedule_id is not None else KIND_REPLY
            proposals.append(({"kind": kind, "text": reply}, kind, ""))
        self._handle_proposals(task, attempt, proposals, shown)
        return attempt

    def _should_reflect(self, task: Task) -> bool:
        if KIND_NOTE_WRITE not in self.registry:
            return False
        if any(a.step is Step.REFLECT for a in self.store.list_attempts(task.id)):
            return False
        if task.parent_task_id is not None:
            return True
        return bool(self.store.list_events(task.id, kind=FEEDBACK_EVENT, limit=1))

    def _reflect_step(self, task: Task, work_output: Mapping[str, Any]) -> Attempt | None:
        feedback = []
        for event in reversed(self.store.list_events(task.id, kind=FEEDBACK_EVENT, limit=50)):
            feedback.append(
                {
                    "message_id": event.detail.get("message_id"),
                    "kind": event.detail.get("kind"),
                    "author": task.requester,
                    "text": event.detail.get("text", ""),
                }
            )
        prompt = instructions.reflect_prompt(
            task,
            now=self.store.clock.now(),
            notes_snapshot=render_snapshot(self.store.list_notes(task.profile)),
            feedback=feedback,
            final_reply=str(work_output.get("reply") or ""),
        )
        attempt, output = self._run_model(
            task, Step.REFLECT, self.worker, prompt, env={}, mounts=[], resume_id=None
        )
        if output is None:
            # Reflection is optional: a failed reflect step does not fail the task.
            self.store.log_event("reflect.failed", {"error": attempt.error}, task_id=task.id)
            if self._still_running(task.id):
                self._done(task, summary=None)
            return None
        proposals = []
        for note in output["notes"]:
            fields = {k: v for k, v in note.items() if v is not None}
            proposals.append(({**fields, "kind": KIND_NOTE_WRITE}, KIND_NOTE_WRITE, ""))
        if not self._still_running(task.id):
            return None
        self._handle_proposals(self.store.get_task(task.id), attempt, proposals, [])
        return attempt

    # -- actions ---------------------------------------------------------------

    def _refuse(self, task: Task, attempt: Attempt, kind: str, raw: str, reason: str) -> None:
        payload = {"arguments_json": raw}
        record = self.store.record_action(
            task.id,
            kind=str(kind),
            target="",
            payload=payload,
            payload_digest=payload_digest(str(kind), "", payload),
            level=Level.HAND_OFF,
            attempt_id=attempt.id,
            status=ActionStatus.REFUSED,
        )
        self.store.set_action_status(record.id, ActionStatus.REFUSED, error=reason)
        self.store.log_event(
            "action.refused",
            {"action_id": record.id, "kind": kind, "reason": reason},
            task_id=task.id,
        )

    def _handle_proposals(
        self,
        task: Task,
        attempt: Attempt,
        proposals: list[tuple[dict[str, Any] | None, str, str]],
        messages: list[Message],
    ) -> None:
        ctx = self._context(task)
        engine = RuleEngine.from_store(self.registry, self.store)
        items: list[ReviewItem] = []
        for proposal, kind, raw in proposals:
            if proposal is None:
                self._refuse(task, attempt, kind, raw, "arguments_json is not a JSON object")
                continue
            try:
                prepared = self.registry.prepare(proposal, ctx)
            except ActionError as exc:
                self._refuse(task, attempt, kind, raw or json.dumps(proposal), str(exc))
                continue
            level = engine.decide(prepared, ctx).level
            record = self.store.record_action(
                task.id,
                kind=prepared.kind,
                target=prepared.target,
                payload=prepared.payload,
                payload_digest=prepared.digest,
                level=level,
                attempt_id=attempt.id,
            )
            items.append(ReviewItem(action_id=record.id, action=prepared, level=level))
        if not items:
            return

        review = review_actions(
            self.store,
            self.reviewer,
            self.config,
            task,
            items,
            policy_text=instructions.load_prompt(Step.REVIEW),
            messages=messages,
            rules=self.store.list_rules(RuleStatus.ACTIVE),
            output_schema=instructions.load_schema(Step.REVIEW),
        )
        if review.stop_reason:
            for item in items:
                outcome = review.outcome_for(item.action_id)
                self.store.set_action_status(
                    item.action_id,
                    ActionStatus.DENIED,
                    error=outcome.reason if outcome.denied else "the task was stopped",
                )
            self._stop_for(task, review.stop_reason)
            return
        if not self._still_running(task.id):
            return

        for item in items:
            outcome = review.outcome_for(item.action_id)
            if outcome.denied:
                self.store.set_action_status(
                    item.action_id, ActionStatus.DENIED, error=outcome.reason
                )
                continue
            self._apply_level(task, ctx, item.action_id, item.action, outcome.level)

    def _apply_level(
        self, task: Task, ctx: ActionContext, action_id: int, action: PreparedAction, level: Level
    ) -> None:
        if level is Level.ALLOW:
            self.store.set_action_status(action_id, ActionStatus.APPROVED, level=level)
            self._execute(ctx, action_id, action)
            return
        if level is Level.HAND_OFF:
            self.store.set_action_status(action_id, ActionStatus.HANDED_OFF, level=level)
            material = json.dumps(action.payload, ensure_ascii=False, indent=2)
            self.notice(
                task.destination,
                f"Task {task.id} prepared an action that I do not carry out myself "
                f"({action.kind}, target {action.target}). Here is the material if you "
                f"want to do it yourself:\n{material}",
                task_id=task.id,
            )
            return
        # preapproved and ask: run only with an approval that covers this exact action.
        if consume_approval(self.store, task, action) is not None:
            self.store.set_action_status(action_id, ActionStatus.APPROVED, level=level)
            self._execute(ctx, action_id, action)
            return
        approval = request_approval(self.store, task, action, action_id=action_id)
        self.store.set_action_status(action_id, ActionStatus.AWAITING_APPROVAL, level=level)
        material = json.dumps(action.payload, ensure_ascii=False, indent=2)
        self.notice(
            task.destination,
            f"Task {task.id} wants to run {action.kind} (target {action.target}):\n"
            f"{material}\n"
            f'Reply "approve {approval.id}" or "deny {approval.id}".',
            task_id=task.id,
        )

    def _execute(self, ctx: ActionContext, action_id: int, action: PreparedAction) -> None:
        try:
            result = self.registry.execute(action, ctx)
        except Exception as exc:
            self.store.set_action_status(
                action_id, ActionStatus.FAILED, error=f"{type(exc).__name__}: {exc}"
            )
            return
        if result.ok:
            self.store.set_action_status(action_id, ActionStatus.EXECUTED, result=result.detail)
        else:
            self.store.set_action_status(
                action_id,
                ActionStatus.FAILED,
                result=result.detail,
                error=str(result.detail.get("error", "the action failed")),
            )
        self.store.log_event(
            "action.executed" if result.ok else "action.failed",
            {"action_id": action_id, "kind": action.kind},
            task_id=ctx.task.id,
        )

    def _resolve_approvals(self, task: Task, waiting: list[ActionRecord]) -> bool:
        """Run or deny parked actions whose approval was decided. False while any is pending."""
        approvals = {a.action_id: a for a in self.store.list_approvals(task.id)}
        if any(
            approvals.get(r.id) is not None and approvals[r.id].status is ApprovalStatus.PENDING
            for r in waiting
        ):
            return False
        ctx = self._context(task)
        for record in waiting:
            approval = approvals.get(record.id)
            if approval is None or approval.status is not ApprovalStatus.GRANTED:
                status = approval.status.value if approval else "missing"
                self.store.set_action_status(
                    record.id, ActionStatus.DENIED, error=f"approval {status}"
                )
                continue
            try:
                handler = self.registry.get(record.kind)
                action_matches_approval(approval, record, outward=handler.outward)
            except (ApprovalMismatch, ActionError) as exc:
                self.store.set_action_status(record.id, ActionStatus.DENIED, error=str(exc))
                continue
            self.store.mark_approval_used(approval.id)
            self.store.set_action_status(record.id, ActionStatus.APPROVED)
            prepared = PreparedAction(record.kind, record.target, record.payload, handler.outward)
            self._execute(ctx, record.id, prepared)
        return True

    # -- outbox ----------------------------------------------------------------

    def flush_outbox(self) -> int:
        """Deliver queued posts and reactions. Returns how many were delivered."""
        delivered = 0
        blocked: set[str] = set()
        for item in self.store.pending_outbox(max_attempts=OUTBOX_MAX_ATTEMPTS):
            channel = self.channels.get(item.channel)
            if channel is None or item.channel in blocked:
                continue
            try:
                if item.reaction:
                    channel.react(item.destination, item.reply_to or "", item.reaction)
                    external_id = None
                elif len(item.text) > LONG_TEXT_CHARS:
                    name = f"task-{item.task_id}-report.md" if item.task_id else "report.md"
                    title = f"Task {item.task_id} report" if item.task_id else "Report"
                    external_id = channel.upload(item.destination, name, item.text, title=title)
                else:
                    external_id = channel.post(item.destination, item.text)
            except ChannelError as exc:
                failed = self.store.mark_outbox_failed(item.id, str(exc))
                if getattr(exc, "retry_after", None) is not None:
                    blocked.add(item.channel)
                if failed.attempt_count >= OUTBOX_MAX_ATTEMPTS:
                    self.store.log_event(
                        "outbox.gave_up",
                        {"outbox_id": item.id, "error": str(exc)},
                        task_id=item.task_id,
                    )
                continue
            self.store.mark_delivered(item.id, external_id)
            delivered += 1
        return delivered
