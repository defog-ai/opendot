"""Shared data types: states, steps, rule levels and the records the store returns."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current time as a timezone-aware datetime in UTC."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def to_iso(value: datetime) -> str:
    """Format a datetime as fixed-width UTC text, so text comparison matches time order."""
    if value.tzinfo is None:
        raise ValueError("naive datetimes are not accepted; attach a timezone")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def from_iso(value: str) -> datetime:
    """Parse text written by to_iso (or any ISO 8601 text with an offset) into UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp has no timezone: {value!r}")
    return parsed.astimezone(UTC)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class TaskState(StrEnum):
    QUEUED = "queued"  # ready for a worker to claim
    RUNNING = "running"  # claimed; a step or an action is in progress
    AWAITING_APPROVAL = "awaiting_approval"  # parked until the requester approves or denies
    AWAITING_REPLY = "awaiting_reply"  # the agent asked the requester a question
    WAITING = "waiting"  # parked until wait_until; tick requeues it
    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"  # a stop note, a budget or the denial limit ended it
    SKIPPED = "skipped"  # the operator skipped it

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATES


TERMINAL_STATES = frozenset(
    {TaskState.DONE, TaskState.FAILED, TaskState.STOPPED, TaskState.SKIPPED}
)


class Step(StrEnum):
    """The three model steps. Each maps to prompts/<NAME>.md and schemas/<name>.schema.json."""

    WORK = "work"
    REVIEW = "review"
    REFLECT = "reflect"


class AttemptStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # backend error
    SCHEMA_ERROR = "schema_error"  # output did not match the schema; never acted on
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"  # a stop note ended the step


class TaskSource(StrEnum):
    CLI = "cli"
    CHANNEL = "channel"
    SCHEDULE = "schedule"


class MessageKind(StrEnum):
    REQUEST = "request"  # starts a new task
    CLARIFY = "clarification"  # answer to a question the agent asked
    STEER = "steer"  # a note that changes the running task
    STOP = "stop"  # ends the current step and the task
    FOLLOW_UP = "follow_up"  # new work in the thread of a finished task
    COMMAND = "command"  # queue / pause / resume / help / approve / deny
    IGNORED = "ignored"


class Level(StrEnum):
    """Rule levels, from least to most strict. "Refuse" is not a level (see UnknownAction)."""

    ALLOW = "allow"  # run after the reviewer approves
    PREAPPROVED = "preapproved"  # run only with a matching stored approval
    ASK = "ask"  # ask the requester and park the task
    HAND_OFF = "hand_off"  # never run; give the prepared material to the requester

    @property
    def rank(self) -> int:
        return _LEVEL_ORDER.index(self)

    @staticmethod
    def strictest(*levels: Level) -> Level:
        if not levels:
            raise ValueError("strictest() needs at least one level")
        return max(levels, key=lambda level: level.rank)


_LEVEL_ORDER = [Level.ALLOW, Level.PREAPPROVED, Level.ASK, Level.HAND_OFF]


class ApprovalMode(StrEnum):
    SINGLE_USE = "single_use"  # covers one action with this exact payload digest
    UNTIL_TASK_END = "until_task_end"  # covers kind + target for the rest of the task


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"
    USED = "used"
    EXPIRED = "expired"


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    REFUSED = "refused"  # unknown kind or bad proposal; the host has no code to run it
    DENIED = "denied"  # the reviewer or the requester said no
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    EXECUTED = "executed"
    FAILED = "failed"
    HANDED_OFF = "handed_off"


class ReviewVerdict(StrEnum):
    APPROVE = "approve"
    DENY = "deny"
    ESCALATE_TO_USER = "escalate_to_user"


class RuleStatus(StrEnum):
    ACTIVE = "active"
    PENDING = "pending"  # drafted, waiting for the operator


class RuleSource(StrEnum):
    OPERATOR = "operator"  # added on the local command line
    CONFIG = "config"  # listed in the TOML file
    AGENT = "agent"  # drafted by the agent; always starts pending


class NotifyRule(StrEnum):
    ALWAYS = "always"
    CHANGED = "changed"  # only when the result hash differs from last_result_hash


class ScheduleStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ENDED = "ended"


class NoteSource(StrEnum):
    REQUESTER = "requester"
    AGENT = "agent"
    OPERATOR = "operator"


# ---------------------------------------------------------------------------
# Values passed between modules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Destination:
    """Where a message goes. channel is the channel kind ("cli", "slack")."""

    channel: str
    conversation: str  # Slack channel id, or "local" for the command line
    thread: str | None = None  # thread id inside the conversation; None = top level


@dataclass(frozen=True)
class IncomingMessage:
    """A message a channel fetched. external_id must be unique within the channel kind."""

    channel: str
    external_id: str
    conversation: str
    thread: str  # the thread root id; equals the message id for a top-level message
    author: str
    text: str
    is_reply: bool  # True when the message is inside an existing thread
    received_at: datetime | None = None
    attachments: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class Mount:
    host: Path
    container: str
    read_only: bool = True


@dataclass
class StepResult:
    output: dict[str, Any]
    thread_id: str
    transcript_path: Path | None = None
    # Keys used: turns, input_tokens, output_tokens.
    usage: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Rows returned by the store
# ---------------------------------------------------------------------------


@dataclass
class Task:
    id: int
    state: TaskState
    source: TaskSource
    channel: str
    conversation: str
    thread: str | None
    requester: str
    profile: str
    text: str
    schedule_id: int | None
    parent_task_id: int | None
    backend_thread_id: str | None
    wait_until: datetime | None
    summary: str | None
    last_error: str | None
    turns_used: int
    tokens_used: int
    active_seconds: float
    lease_owner: str | None
    lease_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None

    @property
    def destination(self) -> Destination:
        return Destination(self.channel, self.conversation, self.thread)


@dataclass
class Attempt:
    id: int
    task_id: int
    step: Step
    attempt_number: int
    status: AttemptStatus
    backend: str
    backend_thread_id: str | None
    run_path: Path | None
    output: dict[str, Any] | None
    usage: dict[str, Any] | None
    error: str | None
    started_at: datetime
    finished_at: datetime | None


@dataclass
class Message:
    id: int
    channel: str
    external_id: str
    conversation: str
    thread: str
    author: str
    text: str
    kind: MessageKind
    task_id: int | None
    received_at: datetime
    applied_at: datetime | None


@dataclass
class OutboxItem:
    id: int
    task_id: int | None
    channel: str
    conversation: str
    thread: str | None
    text: str
    reaction: str | None  # when set, react to reply_to instead of posting text
    reply_to: str | None  # external id of the message to react to
    attempt_count: int
    last_error: str | None
    external_id: str | None
    delivered_at: datetime | None
    created_at: datetime

    @property
    def destination(self) -> Destination:
        return Destination(self.channel, self.conversation, self.thread)


@dataclass
class ActionRecord:
    id: int
    task_id: int
    attempt_id: int | None
    kind: str
    target: str
    payload: dict[str, Any]
    payload_digest: str
    level: Level
    status: ActionStatus
    result: dict[str, Any] | None
    error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass
class Approval:
    id: int
    task_id: int
    action_id: int | None
    kind: str
    target: str
    payload_digest: str | None  # required for single_use; None only for until_task_end
    mode: ApprovalMode
    status: ApprovalStatus
    requested_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    expires_at: datetime | None
    used_at: datetime | None


@dataclass
class Rule:
    id: int
    kind: str  # an action kind, or a prefix ending in ".*", or "*"
    target: str  # exact target, or "*"
    level: Level
    status: RuleStatus
    source: RuleSource
    created_by: str
    created_at: datetime
    approved_at: datetime | None


@dataclass
class Schedule:
    id: int
    what: str  # the task text each run starts with
    cadence: str  # a five-field cron expression, read in tz
    tz: str  # IANA time zone name
    until: datetime | None
    notify_rule: NotifyRule
    channel: str
    conversation: str
    thread: str | None
    creator: str
    profile: str
    status: ScheduleStatus
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_result_hash: str | None
    last_task_id: int | None
    created_at: datetime
    updated_at: datetime

    @property
    def destination(self) -> Destination:
        return Destination(self.channel, self.conversation, self.thread)


@dataclass
class Note:
    id: int
    profile: str
    subject: str
    text: str
    source: NoteSource
    source_task_id: int | None
    created_at: datetime
    updated_at: datetime


@dataclass
class ReviewRecord:
    id: int
    task_id: int
    action_id: int | None
    verdict: ReviewVerdict
    reason: str
    created_at: datetime


@dataclass
class EventRecord:
    id: int
    task_id: int | None
    kind: str
    detail: dict[str, Any]
    created_at: datetime
