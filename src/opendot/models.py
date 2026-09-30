"""Shared data types: states, steps and the records the store returns."""

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
    """The model steps. Each maps to prompts/<NAME>.md and schemas/<name>.schema.json."""

    WORK = "work"
    REVIEW = "review"  # no longer run; kept so attempts recorded before 0.3 still read
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
    COMMAND = "command"  # queue / pause / resume / help
    IGNORED = "ignored"


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    REFUSED = "refused"  # unknown kind or bad proposal; the host has no code to run it
    DENIED = "denied"  # only on actions recorded before 0.3, which had approvals
    APPROVED = "approved"  # only on actions recorded before 0.3
    EXECUTED = "executed"
    FAILED = "failed"
    HANDED_OFF = "handed_off"  # only on actions recorded before 0.3


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
# Step plan (v0.2): what the host adds to one work step container
# ---------------------------------------------------------------------------


class HostMountKind(StrEnum):
    """Folders the host creates and mounts. Each kind has one fixed place in the container."""

    WORKTREE = "worktree"  # a task's copy of a repository: /opendot/repos/<name>, writable
    ARTIFACTS = "artifacts"  # files the step leaves for the host: /opendot/run/artifacts, writable
    MCP = "mcp"  # gateway socket and bridge script: /opendot/mcp, read-only
    INSTRUCTIONS = "instructions"  # connector instructions: /opendot/instructions/<name>, read-only


@dataclass(frozen=True)
class HostMount:
    """A mount of a folder the host made under the state root.

    Operator mounts (Mount) may never come from the state root. Host mounts may,
    but only from the folders sandbox.check_host_mounts allows, and only onto the
    container paths it allows. A WORKTREE mount always gets its .git folder
    mounted read-only on top; the sandbox adds that mount itself.
    """

    kind: HostMountKind
    host: Path
    container: str
    writable: bool = False


@dataclass(frozen=True)
class McpServerSpec:
    """An MCP server the model's CLI starts inside the container, over stdio.

    Two uses in v0.2: the browser (command = the Playwright MCP server) and the
    host gateway (command = the bridge script that forwards to the gateway socket).
    Remote servers are never given to the CLI directly; the gateway reaches them.

    tools: the tool names the CLI may call. An empty tuple means every tool the
    server lists; the gateway lists only allowlisted read tools, so it may use ().
    env: fixed, non-secret values only. Secret values never enter a container.
    """

    name: str
    command: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    tools: tuple[str, ...] = ()
    startup_timeout_seconds: int = 30
    tool_timeout_seconds: int = 120


@dataclass
class StepPlan:
    """Everything the v0.2 features add to one work step.

    Built by the step extensions (opendot.extensions) before the step runs and
    passed to Backend.run_step(plan=...). Reflect steps get no plan.

    step_token: a host-made id for this step; the run folder and the gateway log
        are keyed on it because the attempt row does not exist yet.
    run_dir: runs_dir/task-<task id>/<step_token>; the host owns it.
    prompt_notes: plain text paragraphs the host adds to the work prompt, for
        example where the repositories are mounted.
    shm_size: a --shm-size value for the container ("" = Docker's default).
    """

    step_token: str
    run_dir: Path
    host_mounts: list[HostMount] = field(default_factory=list)
    mcp_servers: list[McpServerSpec] = field(default_factory=list)
    fixed_env: dict[str, str] = field(default_factory=dict)
    prompt_notes: list[str] = field(default_factory=list)
    shm_size: str = ""


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
    status: ActionStatus
    result: dict[str, Any] | None
    error: str | None
    created_at: datetime
    updated_at: datetime


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
class EventRecord:
    id: int
    task_id: int | None
    kind: str
    detail: dict[str, Any]
    created_at: datetime


# ---------------------------------------------------------------------------
# v0.2 rows: repositories, task worktrees, GitHub publications, gateway log,
# connector tokens
# ---------------------------------------------------------------------------


class RepoVisibility(StrEnum):
    UNKNOWN = "unknown"
    PUBLIC = "public"
    PRIVATE = "private"


class WorktreeStatus(StrEnum):
    ACTIVE = "active"
    REMOVED = "removed"


class PublicationKind(StrEnum):
    BRANCH = "branch"  # github.push_branch
    PULL_REQUEST = "pull_request"  # github.open_pr
    ISSUE = "issue"  # github.issue
    ISSUE_COMMENT = "issue_comment"  # github.issue_comment


class PublicationState(StrEnum):
    STARTED = "started"  # the host began; a retry must look for the marker first
    PUBLISHED = "published"  # pushed, or created on GitHub
    MERGED = "merged"  # pull requests only, set by reconcile
    CLOSED = "closed"  # pull requests and issues, set by reconcile
    FAILED = "failed"


class GatewayMode(StrEnum):
    READ = "read"  # exposed to the model through the gateway
    WRITE = "write"  # never exposed; becomes the action kind mcp.<server>.<tool>


class GatewayCallStatus(StrEnum):
    OK = "ok"
    ERROR = "error"  # the remote server returned an error or could not be reached
    REFUSED = "refused"  # not on the allowlist, or not a read tool


@dataclass
class RepositoryRecord:
    """Host state for one configured repository and its control clone."""

    name: str
    remote: str
    control_path: Path
    default_branch: str
    visibility: RepoVisibility
    visibility_checked_at: datetime | None
    last_fetched_at: datetime | None
    updated_at: datetime


@dataclass
class Worktree:
    """A task's own copy of a repository (a local clone made from the control clone)."""

    id: int
    task_id: int
    repository: str
    path: Path
    base_ref: str
    base_sha: str
    branch: str  # the branch the host will push, e.g. opendot/task-12-fix-typo
    status: WorktreeStatus
    created_at: datetime
    removed_at: datetime | None


@dataclass
class Publication:
    """Something the host published on GitHub. marker makes retries idempotent."""

    id: int
    task_id: int
    action_id: int | None
    kind: PublicationKind
    repository: str  # "owner/name"
    marker: str  # hidden text put in the body; also used to find an earlier copy
    state: PublicationState
    branch: str | None
    head_sha: str | None
    number: int | None  # PR or issue number
    url: str | None
    external_id: str | None  # GitHub node or comment id
    error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass
class GatewayCall:
    id: int
    task_id: int
    step_token: str
    attempt_id: int | None
    server: str
    tool: str
    mode: GatewayMode
    arguments: dict[str, Any]
    status: GatewayCallStatus
    result_bytes: int
    result_preview: str  # the first few KiB of the result text, for the audit log
    error: str | None
    duration_ms: int
    created_at: datetime


@dataclass
class ConnectorToken:
    """An OAuth token for one MCP server, held by the host only."""

    server: str
    token_type: str
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scope: str
    client_info: dict[str, Any]
    updated_at: datetime
