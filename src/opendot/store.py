"""SQLite state store: tasks and leases, attempts, intake, outbox, approvals, rules,
schedules, notes, the review log and the audit log. Also the single-worker lock file."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opendot.models import (
    TERMINAL_STATES,
    ActionRecord,
    ActionStatus,
    Approval,
    ApprovalMode,
    ApprovalStatus,
    Attempt,
    AttemptStatus,
    Clock,
    Destination,
    EventRecord,
    IncomingMessage,
    Level,
    Message,
    MessageKind,
    Note,
    NoteSource,
    NotifyRule,
    OutboxItem,
    ReviewRecord,
    ReviewVerdict,
    Rule,
    RuleSource,
    RuleStatus,
    Schedule,
    ScheduleStatus,
    Step,
    SystemClock,
    Task,
    TaskSource,
    TaskState,
    from_iso,
    to_iso,
)

if TYPE_CHECKING:
    from opendot.config import Config

SCHEMA_VERSION = 1
DB_FILENAME = "opendot.db"


class StoreError(Exception):
    pass


class NotFound(StoreError, LookupError):
    pass


class LeaseLost(StoreError):
    """The caller no longer holds the lease on the task."""


class LockBusy(StoreError):
    """Another worker process holds the lock file."""


@dataclass(frozen=True)
class DenialCounts:
    in_a_row: int  # denials since the most recent non-deny verdict
    in_window: int  # denials among the most recent `window` verdicts
    window: int


# ---------------------------------------------------------------------------
# Lock file: one worker at a time
# ---------------------------------------------------------------------------


class WorkerLock:
    """An exclusive, non-blocking flock on a file. Use as a context manager."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise LockBusy(f"another worker holds {self.path}") from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> WorkerLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


# ---------------------------------------------------------------------------
# Conversions
# ---------------------------------------------------------------------------


def _ts(value: datetime | None) -> str | None:
    return None if value is None else to_iso(value)


def _dt(value: str | None) -> datetime | None:
    return None if value is None else from_iso(value)


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _loads(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        state=TaskState(row["state"]),
        source=TaskSource(row["source"]),
        channel=row["channel"],
        conversation=row["conversation"],
        thread=row["thread"],
        requester=row["requester"],
        profile=row["profile"],
        text=row["text"],
        schedule_id=row["schedule_id"],
        parent_task_id=row["parent_task_id"],
        backend_thread_id=row["backend_thread_id"],
        wait_until=_dt(row["wait_until"]),
        summary=row["summary"],
        last_error=row["last_error"],
        turns_used=row["turns_used"],
        tokens_used=row["tokens_used"],
        active_seconds=row["active_seconds"],
        lease_owner=row["lease_owner"],
        lease_expires_at=_dt(row["lease_expires_at"]),
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
        finished_at=_dt(row["finished_at"]),
    )


def _attempt(row: sqlite3.Row) -> Attempt:
    return Attempt(
        id=row["id"],
        task_id=row["task_id"],
        step=Step(row["step"]),
        attempt_number=row["attempt_number"],
        status=AttemptStatus(row["status"]),
        backend=row["backend"],
        backend_thread_id=row["backend_thread_id"],
        run_path=None if row["run_path"] is None else Path(row["run_path"]),
        output=_loads(row["output"]),
        usage=_loads(row["usage"]),
        error=row["error"],
        started_at=from_iso(row["started_at"]),
        finished_at=_dt(row["finished_at"]),
    )


def _message(row: sqlite3.Row) -> Message:
    return Message(
        id=row["id"],
        channel=row["channel"],
        external_id=row["external_id"],
        conversation=row["conversation"],
        thread=row["thread"],
        author=row["author"],
        text=row["text"],
        kind=MessageKind(row["kind"]),
        task_id=row["task_id"],
        received_at=from_iso(row["received_at"]),
        applied_at=_dt(row["applied_at"]),
    )


def _outbox(row: sqlite3.Row) -> OutboxItem:
    return OutboxItem(
        id=row["id"],
        task_id=row["task_id"],
        channel=row["channel"],
        conversation=row["conversation"],
        thread=row["thread"],
        text=row["text"],
        reaction=row["reaction"],
        reply_to=row["reply_to"],
        attempt_count=row["attempt_count"],
        last_error=row["last_error"],
        external_id=row["external_id"],
        delivered_at=_dt(row["delivered_at"]),
        created_at=from_iso(row["created_at"]),
    )


def _action(row: sqlite3.Row) -> ActionRecord:
    return ActionRecord(
        id=row["id"],
        task_id=row["task_id"],
        attempt_id=row["attempt_id"],
        kind=row["kind"],
        target=row["target"],
        payload=_loads(row["payload"]),
        payload_digest=row["payload_digest"],
        level=Level(row["level"]),
        status=ActionStatus(row["status"]),
        result=_loads(row["result"]),
        error=row["error"],
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
    )


def _approval(row: sqlite3.Row) -> Approval:
    return Approval(
        id=row["id"],
        task_id=row["task_id"],
        action_id=row["action_id"],
        kind=row["kind"],
        target=row["target"],
        payload_digest=row["payload_digest"],
        mode=ApprovalMode(row["mode"]),
        status=ApprovalStatus(row["status"]),
        requested_at=from_iso(row["requested_at"]),
        decided_by=row["decided_by"],
        decided_at=_dt(row["decided_at"]),
        expires_at=_dt(row["expires_at"]),
        used_at=_dt(row["used_at"]),
    )


def _rule(row: sqlite3.Row) -> Rule:
    return Rule(
        id=row["id"],
        kind=row["kind"],
        target=row["target"],
        level=Level(row["level"]),
        status=RuleStatus(row["status"]),
        source=RuleSource(row["source"]),
        created_by=row["created_by"],
        created_at=from_iso(row["created_at"]),
        approved_at=_dt(row["approved_at"]),
    )


def _schedule(row: sqlite3.Row) -> Schedule:
    return Schedule(
        id=row["id"],
        what=row["what"],
        cadence=row["cadence"],
        tz=row["tz"],
        until=_dt(row["until"]),
        notify_rule=NotifyRule(row["notify_rule"]),
        channel=row["channel"],
        conversation=row["conversation"],
        thread=row["thread"],
        creator=row["creator"],
        profile=row["profile"],
        status=ScheduleStatus(row["status"]),
        next_run_at=_dt(row["next_run_at"]),
        last_run_at=_dt(row["last_run_at"]),
        last_result_hash=row["last_result_hash"],
        last_task_id=row["last_task_id"],
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
    )


def _note(row: sqlite3.Row) -> Note:
    return Note(
        id=row["id"],
        profile=row["profile"],
        subject=row["subject"],
        text=row["text"],
        source=NoteSource(row["source"]),
        source_task_id=row["source_task_id"],
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
    )


def _review(row: sqlite3.Row) -> ReviewRecord:
    return ReviewRecord(
        id=row["id"],
        task_id=row["task_id"],
        action_id=row["action_id"],
        verdict=ReviewVerdict(row["verdict"]),
        reason=row["reason"],
        created_at=from_iso(row["created_at"]),
    )


def _event(row: sqlite3.Row) -> EventRecord:
    return EventRecord(
        id=row["id"],
        task_id=row["task_id"],
        kind=row["kind"],
        detail=_loads(row["detail"]),
        created_at=from_iso(row["created_at"]),
    )


# Columns update_task / update_schedule may change, with the converter for each.
_TASK_FIELDS = {
    "backend_thread_id": lambda v: v,
    "wait_until": _ts,
    "summary": lambda v: v,
    "last_error": lambda v: v,
    "thread": lambda v: v,
}
_SCHEDULE_FIELDS = {
    "what": lambda v: v,
    "cadence": lambda v: v,
    "tz": lambda v: v,
    "until": _ts,
    "notify_rule": lambda v: NotifyRule(v).value,
    "channel": lambda v: v,
    "conversation": lambda v: v,
    "thread": lambda v: v,
    "status": lambda v: ScheduleStatus(v).value,
    "next_run_at": _ts,
    "last_run_at": _ts,
    "last_result_hash": lambda v: v,
    "last_task_id": lambda v: v,
}


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class Store:
    def __init__(self, path: Path, clock: Clock | None = None):
        self.path = Path(path)
        self.clock: Clock = clock or SystemClock()
        if not self.path.exists():
            os.close(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600))
        os.chmod(self.path, 0o600)
        self._conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._depth = 0

    # -- lifecycle ---------------------------------------------------------

    def migrate(self) -> None:
        """Create every table that does not exist yet. Safe to run repeatedly."""
        schema = resources.files("opendot").joinpath("schema.sql").read_text(encoding="utf-8")
        self._conn.executescript(schema)
        with self.transaction():
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def schema_version(self) -> int | None:
        try:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return None if row is None else int(row["value"])

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A write transaction (BEGIN IMMEDIATE). Nested calls join the outer one."""
        if self._depth:
            self._depth += 1
            try:
                yield self._conn
            finally:
                self._depth -= 1
            return
        self._conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")
        finally:
            self._depth = 0

    def _now(self) -> str:
        return to_iso(self.clock.now())

    def _one(self, sql: str, params: Iterable[Any], what: str) -> sqlite3.Row:
        row = self._conn.execute(sql, tuple(params)).fetchone()
        if row is None:
            raise NotFound(what)
        return row

    # -- tasks -------------------------------------------------------------

    def create_task(
        self,
        *,
        requester: str,
        text: str,
        channel: str,
        conversation: str,
        thread: str | None = None,
        profile: str | None = None,
        source: TaskSource = TaskSource.CHANNEL,
        schedule_id: int | None = None,
        parent_task_id: int | None = None,
        state: TaskState = TaskState.QUEUED,
    ) -> Task:
        """Add a task. profile defaults to "<channel>:<requester>"."""
        now = self._now()
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO tasks (state, source, channel, conversation, thread, requester, "
                "profile, text, schedule_id, parent_task_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    TaskState(state).value,
                    TaskSource(source).value,
                    channel,
                    conversation,
                    thread,
                    requester,
                    profile or f"{channel}:{requester}",
                    text,
                    schedule_id,
                    parent_task_id,
                    now,
                    now,
                ),
            )
            return self.get_task(cur.lastrowid)

    def get_task(self, task_id: int) -> Task:
        return _task(self._one("SELECT * FROM tasks WHERE id = ?", (task_id,), f"task {task_id}"))

    def list_tasks(self, states: Iterable[TaskState] | None = None, limit: int = 50) -> list[Task]:
        """Newest first."""
        if states is None:
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            values = [TaskState(s).value for s in states]
            marks = ",".join("?" * len(values))
            rows = self._conn.execute(
                f"SELECT * FROM tasks WHERE state IN ({marks}) ORDER BY id DESC LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [_task(r) for r in rows]

    def count_tasks_by_state(self) -> dict[TaskState, int]:
        rows = self._conn.execute(
            "SELECT state, COUNT(*) AS n FROM tasks GROUP BY state"
        ).fetchall()
        return {TaskState(r["state"]): int(r["n"]) for r in rows}

    def find_thread_task(
        self, channel: str, conversation: str, thread: str, *, active_only: bool = False
    ) -> Task | None:
        """The newest task started in this thread (active_only: not in a terminal state)."""
        sql = "SELECT * FROM tasks WHERE channel = ? AND conversation = ? AND thread = ?"
        params: list[Any] = [channel, conversation, thread]
        if active_only:
            marks = ",".join("?" * len(TERMINAL_STATES))
            sql += f" AND state NOT IN ({marks})"
            params.extend(s.value for s in TERMINAL_STATES)
        row = self._conn.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()
        return None if row is None else _task(row)

    def _requeue_expired_leases(self, conn: sqlite3.Connection, now: str) -> None:
        conn.execute(
            "UPDATE tasks SET state = 'queued', lease_owner = NULL, lease_expires_at = NULL, "
            "updated_at = ? WHERE state = 'running' AND lease_expires_at <= ?",
            (now, now),
        )

    def claim_next(self, owner: str, lease_seconds: int) -> Task | None:
        """Requeue tasks whose lease expired, then lease the oldest queued task.

        The claimed task moves to state running with lease_owner = owner.
        """
        now_dt = self.clock.now()
        now = to_iso(now_dt)
        expires = to_iso(now_dt + timedelta(seconds=lease_seconds))
        with self.transaction() as conn:
            self._requeue_expired_leases(conn, now)
            row = conn.execute(
                "SELECT id FROM tasks WHERE state = 'queued' ORDER BY created_at, id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE tasks SET state = 'running', lease_owner = ?, lease_expires_at = ?, "
                "updated_at = ? WHERE id = ?",
                (owner, expires, now, row["id"]),
            )
            return self.get_task(row["id"])

    def claim_task(self, task_id: int, owner: str, lease_seconds: int) -> Task | None:
        """Lease one specific task if it is queued. Returns None otherwise."""
        now_dt = self.clock.now()
        now = to_iso(now_dt)
        expires = to_iso(now_dt + timedelta(seconds=lease_seconds))
        with self.transaction() as conn:
            self._requeue_expired_leases(conn, now)
            cur = conn.execute(
                "UPDATE tasks SET state = 'running', lease_owner = ?, lease_expires_at = ?, "
                "updated_at = ? WHERE id = ? AND state = 'queued'",
                (owner, expires, now, task_id),
            )
            return self.get_task(task_id) if cur.rowcount else None

    def renew_lease(self, task_id: int, owner: str, lease_seconds: int) -> None:
        """Extend the lease. Raises LeaseLost if owner no longer holds it."""
        now_dt = self.clock.now()
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE tasks SET lease_expires_at = ?, updated_at = ? "
                "WHERE id = ? AND state = 'running' AND lease_owner = ?",
                (
                    to_iso(now_dt + timedelta(seconds=lease_seconds)),
                    to_iso(now_dt),
                    task_id,
                    owner,
                ),
            )
            if not cur.rowcount:
                raise LeaseLost(f"task {task_id} is not leased by {owner}")

    def release_task(
        self,
        task_id: int,
        owner: str,
        state: TaskState,
        *,
        error: str | None = None,
        summary: str | None = None,
        wait_until: datetime | None = None,
    ) -> Task:
        """End a lease and move the task to `state`. Raises LeaseLost if not held."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT lease_owner, state FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"task {task_id}")
            if row["state"] != TaskState.RUNNING or row["lease_owner"] != owner:
                raise LeaseLost(f"task {task_id} is not leased by {owner}")
            return self.set_task_state(
                task_id, state, error=error, summary=summary, wait_until=wait_until
            )

    def set_task_state(
        self,
        task_id: int,
        state: TaskState,
        *,
        error: str | None = None,
        summary: str | None = None,
        wait_until: datetime | None = None,
    ) -> Task:
        """Move a task to `state` without a lease check (operator commands use this).

        Leaving running clears the lease. A terminal state sets finished_at and
        expires every pending or granted approval of the task. error / summary are
        written only when given; wait_until is written for state waiting and cleared
        otherwise.
        """
        state = TaskState(state)
        if state is TaskState.WAITING and wait_until is None:
            raise ValueError("state waiting needs wait_until")
        now = self._now()
        with self.transaction() as conn:
            self.get_task(task_id)
            sets = ["state = ?", "updated_at = ?", "wait_until = ?"]
            params: list[Any] = [
                state.value,
                now,
                _ts(wait_until) if state is TaskState.WAITING else None,
            ]
            if state is not TaskState.RUNNING:
                sets += ["lease_owner = NULL", "lease_expires_at = NULL"]
            if error is not None:
                sets.append("last_error = ?")
                params.append(error)
            if summary is not None:
                sets.append("summary = ?")
                params.append(summary)
            if state in TERMINAL_STATES:
                sets.append("finished_at = ?")
                params.append(now)
                conn.execute(
                    "UPDATE approvals SET status = 'expired' "
                    "WHERE task_id = ? AND status IN ('pending', 'granted')",
                    (task_id,),
                )
            conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", (*params, task_id))
            return self.get_task(task_id)

    def update_task(self, task_id: int, **fields: Any) -> Task:
        """Change backend_thread_id, wait_until, summary, last_error or thread."""
        unknown = set(fields) - set(_TASK_FIELDS)
        if unknown:
            raise ValueError(f"update_task cannot change {sorted(unknown)}")
        if not fields:
            return self.get_task(task_id)
        sets = [f"{name} = ?" for name in fields]
        params = [_TASK_FIELDS[name](value) for name, value in fields.items()]
        with self.transaction() as conn:
            self.get_task(task_id)
            conn.execute(
                f"UPDATE tasks SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
                (*params, self._now(), task_id),
            )
            return self.get_task(task_id)

    def add_usage(
        self, task_id: int, *, turns: int = 0, tokens: int = 0, seconds: float = 0.0
    ) -> Task:
        """Add to the task's budget counters."""
        with self.transaction() as conn:
            self.get_task(task_id)
            conn.execute(
                "UPDATE tasks SET turns_used = turns_used + ?, tokens_used = tokens_used + ?, "
                "active_seconds = active_seconds + ?, updated_at = ? WHERE id = ?",
                (turns, tokens, seconds, self._now(), task_id),
            )
            return self.get_task(task_id)

    def wake_due(self) -> list[Task]:
        """Move waiting tasks whose wait_until has passed back to queued."""
        now = self._now()
        with self.transaction() as conn:
            ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM tasks WHERE state = 'waiting' AND wait_until <= ? ORDER BY id",
                    (now,),
                ).fetchall()
            ]
            for task_id in ids:
                conn.execute(
                    "UPDATE tasks SET state = 'queued', wait_until = NULL, updated_at = ? "
                    "WHERE id = ?",
                    (now, task_id),
                )
            return [self.get_task(i) for i in ids]

    # -- attempts ----------------------------------------------------------

    def start_attempt(
        self, task_id: int, step: Step, backend: str, *, run_path: Path | None = None
    ) -> Attempt:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) + 1 AS n FROM attempts WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            cur = conn.execute(
                "INSERT INTO attempts (task_id, step, attempt_number, status, backend, run_path, "
                "started_at) VALUES (?, ?, ?, 'running', ?, ?, ?)",
                (
                    task_id,
                    Step(step).value,
                    row["n"],
                    backend,
                    None if run_path is None else str(run_path),
                    self._now(),
                ),
            )
            return self.get_attempt(cur.lastrowid)

    def finish_attempt(
        self,
        attempt_id: int,
        status: AttemptStatus,
        *,
        output: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        backend_thread_id: str | None = None,
        error: str | None = None,
    ) -> Attempt:
        with self.transaction() as conn:
            self.get_attempt(attempt_id)
            conn.execute(
                "UPDATE attempts SET status = ?, output = ?, usage = ?, "
                "backend_thread_id = COALESCE(?, backend_thread_id), error = ?, finished_at = ? "
                "WHERE id = ?",
                (
                    AttemptStatus(status).value,
                    None if output is None else _dumps(output),
                    None if usage is None else _dumps(usage),
                    backend_thread_id,
                    error,
                    self._now(),
                    attempt_id,
                ),
            )
            return self.get_attempt(attempt_id)

    def get_attempt(self, attempt_id: int) -> Attempt:
        return _attempt(
            self._one("SELECT * FROM attempts WHERE id = ?", (attempt_id,), f"attempt {attempt_id}")
        )

    def list_attempts(self, task_id: int) -> list[Attempt]:
        rows = self._conn.execute(
            "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_number", (task_id,)
        ).fetchall()
        return [_attempt(r) for r in rows]

    # -- intake ------------------------------------------------------------

    def record_message(
        self, message: IncomingMessage, kind: MessageKind, *, task_id: int | None = None
    ) -> Message | None:
        """Store an incoming message. Returns None if (channel, external_id) is already stored."""
        received = message.received_at or self.clock.now()
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO messages (channel, external_id, conversation, thread, author, text, "
                "kind, task_id, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (channel, external_id) DO NOTHING",
                (
                    message.channel,
                    message.external_id,
                    message.conversation,
                    message.thread,
                    message.author,
                    message.text,
                    MessageKind(kind).value,
                    task_id,
                    to_iso(received),
                ),
            )
            if not cur.rowcount:
                return None
            return self.get_message(cur.lastrowid)

    def has_message(self, channel: str, external_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM messages WHERE channel = ? AND external_id = ?", (channel, external_id)
        ).fetchone()
        return row is not None

    def get_message(self, message_id: int) -> Message:
        return _message(
            self._one("SELECT * FROM messages WHERE id = ?", (message_id,), f"message {message_id}")
        )

    def set_message_task(self, message_id: int, task_id: int) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE messages SET task_id = ? WHERE id = ?", (task_id, message_id))

    def pending_messages(
        self, task_id: int, kinds: Iterable[MessageKind] | None = None
    ) -> list[Message]:
        """Messages linked to the task that have not been applied yet, oldest first."""
        sql = "SELECT * FROM messages WHERE task_id = ? AND applied_at IS NULL"
        params: list[Any] = [task_id]
        if kinds is not None:
            values = [MessageKind(k).value for k in kinds]
            sql += f" AND kind IN ({','.join('?' * len(values))})"
            params.extend(values)
        rows = self._conn.execute(sql + " ORDER BY id", params).fetchall()
        return [_message(r) for r in rows]

    def mark_messages_applied(self, message_ids: Iterable[int]) -> None:
        now = self._now()
        with self.transaction() as conn:
            for message_id in message_ids:
                conn.execute(
                    "UPDATE messages SET applied_at = ? WHERE id = ? AND applied_at IS NULL",
                    (now, message_id),
                )

    def get_cursor(self, channel: str, conversation: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM cursors WHERE channel = ? AND conversation = ?",
            (channel, conversation),
        ).fetchone()
        return None if row is None else row["value"]

    def set_cursor(self, channel: str, conversation: str, value: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO cursors (channel, conversation, value, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (channel, conversation) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (channel, conversation, value, self._now()),
            )

    def pause_thread(self, channel: str, conversation: str, thread: str, paused_by: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO thread_pauses (channel, conversation, thread, paused_by, paused_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (channel, conversation, thread, paused_by, self._now()),
            )

    def resume_thread(self, channel: str, conversation: str, thread: str) -> bool:
        """Returns True if the thread was paused."""
        with self.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM thread_pauses WHERE channel = ? AND conversation = ? AND thread = ?",
                (channel, conversation, thread),
            )
            return bool(cur.rowcount)

    def is_thread_paused(self, channel: str, conversation: str, thread: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM thread_pauses WHERE channel = ? AND conversation = ? AND thread = ?",
            (channel, conversation, thread),
        ).fetchone()
        return row is not None

    # -- outbox ------------------------------------------------------------

    def enqueue_outbox(
        self,
        destination: Destination,
        text: str,
        *,
        task_id: int | None = None,
        reaction: str | None = None,
        reply_to: str | None = None,
    ) -> OutboxItem:
        """Queue a post (or, with reaction + reply_to, a reaction) for delivery."""
        if reaction is not None and reply_to is None:
            raise ValueError("a reaction needs reply_to")
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO outbox (task_id, channel, conversation, thread, text, reaction, "
                "reply_to, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    destination.channel,
                    destination.conversation,
                    destination.thread,
                    text,
                    reaction,
                    reply_to,
                    self._now(),
                ),
            )
            return self.get_outbox_item(cur.lastrowid)

    def get_outbox_item(self, item_id: int) -> OutboxItem:
        return _outbox(
            self._one("SELECT * FROM outbox WHERE id = ?", (item_id,), f"outbox item {item_id}")
        )

    def pending_outbox(
        self, channel: str | None = None, *, max_attempts: int = 5, limit: int = 50
    ) -> list[OutboxItem]:
        """Undelivered items with fewer than max_attempts tries, oldest first."""
        sql = "SELECT * FROM outbox WHERE delivered_at IS NULL AND attempt_count < ?"
        params: list[Any] = [max_attempts]
        if channel is not None:
            sql += " AND channel = ?"
            params.append(channel)
        rows = self._conn.execute(sql + " ORDER BY id LIMIT ?", (*params, limit)).fetchall()
        return [_outbox(r) for r in rows]

    def mark_delivered(self, item_id: int, external_id: str | None = None) -> OutboxItem:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE outbox SET delivered_at = ?, external_id = ?, "
                "attempt_count = attempt_count + 1, last_error = NULL WHERE id = ?",
                (self._now(), external_id, item_id),
            )
            return self.get_outbox_item(item_id)

    def mark_outbox_failed(self, item_id: int, error: str) -> OutboxItem:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE outbox SET attempt_count = attempt_count + 1, last_error = ? WHERE id = ?",
                (error, item_id),
            )
            return self.get_outbox_item(item_id)

    # -- actions -----------------------------------------------------------

    def record_action(
        self,
        task_id: int,
        *,
        kind: str,
        target: str,
        payload: dict[str, Any],
        payload_digest: str,
        level: Level,
        attempt_id: int | None = None,
        status: ActionStatus = ActionStatus.PROPOSED,
    ) -> ActionRecord:
        now = self._now()
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO actions (task_id, attempt_id, kind, target, payload, payload_digest, "
                "level, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    attempt_id,
                    kind,
                    target,
                    _dumps(payload),
                    payload_digest,
                    Level(level).value,
                    ActionStatus(status).value,
                    now,
                    now,
                ),
            )
            return self.get_action(cur.lastrowid)

    def get_action(self, action_id: int) -> ActionRecord:
        return _action(
            self._one("SELECT * FROM actions WHERE id = ?", (action_id,), f"action {action_id}")
        )

    def set_action_status(
        self,
        action_id: int,
        status: ActionStatus,
        *,
        level: Level | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> ActionRecord:
        with self.transaction() as conn:
            self.get_action(action_id)
            conn.execute(
                "UPDATE actions SET status = ?, level = COALESCE(?, level), "
                "result = COALESCE(?, result), error = COALESCE(?, error), updated_at = ? "
                "WHERE id = ?",
                (
                    ActionStatus(status).value,
                    None if level is None else Level(level).value,
                    None if result is None else _dumps(result),
                    error,
                    self._now(),
                    action_id,
                ),
            )
            return self.get_action(action_id)

    def list_actions(self, task_id: int, status: ActionStatus | None = None) -> list[ActionRecord]:
        sql = "SELECT * FROM actions WHERE task_id = ?"
        params: list[Any] = [task_id]
        if status is not None:
            sql += " AND status = ?"
            params.append(ActionStatus(status).value)
        rows = self._conn.execute(sql + " ORDER BY id", params).fetchall()
        return [_action(r) for r in rows]

    # -- approvals ---------------------------------------------------------

    def create_approval(
        self,
        task_id: int,
        *,
        kind: str,
        target: str,
        mode: ApprovalMode,
        payload_digest: str | None,
        action_id: int | None = None,
        expires_at: datetime | None = None,
    ) -> Approval:
        """Add a pending approval request. single_use requires payload_digest."""
        mode = ApprovalMode(mode)
        if mode is ApprovalMode.SINGLE_USE and not payload_digest:
            raise ValueError("a single-use approval needs the payload digest")
        with self.transaction() as conn:
            self.get_task(task_id)
            cur = conn.execute(
                "INSERT INTO approvals (task_id, action_id, kind, target, payload_digest, mode, "
                "status, requested_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    task_id,
                    action_id,
                    kind,
                    target,
                    payload_digest,
                    mode.value,
                    self._now(),
                    _ts(expires_at),
                ),
            )
            return self.get_approval(cur.lastrowid)

    def get_approval(self, approval_id: int) -> Approval:
        return _approval(
            self._one(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,), f"approval {approval_id}"
            )
        )

    def decide_approval(self, approval_id: int, *, granted: bool, decided_by: str) -> Approval:
        """Grant or deny a pending approval. Raises StoreError if it is not pending.

        This does not check who decided; approvals.py must check that decided_by
        is the task's requester (or the local operator) before calling it.
        """
        self.expire_approvals()
        with self.transaction() as conn:
            approval = self.get_approval(approval_id)
            if approval.status is not ApprovalStatus.PENDING:
                raise StoreError(f"approval {approval_id} is {approval.status.value}, not pending")
            conn.execute(
                "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?",
                ("granted" if granted else "denied", decided_by, self._now(), approval_id),
            )
            return self.get_approval(approval_id)

    def list_approvals(
        self, task_id: int | None = None, status: ApprovalStatus | None = None
    ) -> list[Approval]:
        sql = "SELECT * FROM approvals WHERE 1 = 1"
        params: list[Any] = []
        if task_id is not None:
            sql += " AND task_id = ?"
            params.append(task_id)
        if status is not None:
            sql += " AND status = ?"
            params.append(ApprovalStatus(status).value)
        rows = self._conn.execute(sql + " ORDER BY id", params).fetchall()
        return [_approval(r) for r in rows]

    def granted_approvals(self, task_id: int, kind: str, target: str) -> list[Approval]:
        """Granted, unexpired approvals for exactly this task, kind and target.

        The caller (approvals.py) still has to compare payload_digest and mode.
        """
        self.expire_approvals()
        rows = self._conn.execute(
            "SELECT * FROM approvals WHERE task_id = ? AND kind = ? AND target = ? "
            "AND status = 'granted' ORDER BY id",
            (task_id, kind, target),
        ).fetchall()
        return [_approval(r) for r in rows]

    def mark_approval_used(self, approval_id: int) -> Approval:
        """Mark a granted single-use approval used. Until-task-end approvals stay granted."""
        with self.transaction() as conn:
            approval = self.get_approval(approval_id)
            if approval.status is not ApprovalStatus.GRANTED:
                raise StoreError(f"approval {approval_id} is {approval.status.value}, not granted")
            if approval.mode is ApprovalMode.SINGLE_USE:
                conn.execute(
                    "UPDATE approvals SET status = 'used', used_at = ? WHERE id = ?",
                    (self._now(), approval_id),
                )
            else:
                conn.execute(
                    "UPDATE approvals SET used_at = ? WHERE id = ?", (self._now(), approval_id)
                )
            return self.get_approval(approval_id)

    def expire_approvals(self) -> int:
        """Expire pending or granted approvals whose expires_at has passed."""
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE approvals SET status = 'expired' WHERE status IN ('pending', 'granted') "
                "AND expires_at IS NOT NULL AND expires_at <= ?",
                (self._now(),),
            )
            return cur.rowcount

    # -- rules -------------------------------------------------------------

    def add_rule(
        self,
        kind: str,
        level: Level,
        *,
        target: str = "*",
        source: RuleSource,
        created_by: str,
    ) -> Rule:
        """Add a rule. Agent-drafted rules start pending; the others start active."""
        source = RuleSource(source)
        status = RuleStatus.PENDING if source is RuleSource.AGENT else RuleStatus.ACTIVE
        now = self._now()
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO rules (kind, target, level, status, source, created_by, created_at, "
                "approved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    kind,
                    target,
                    Level(level).value,
                    status.value,
                    source.value,
                    created_by,
                    now,
                    None if status is RuleStatus.PENDING else now,
                ),
            )
            return self.get_rule(cur.lastrowid)

    def get_rule(self, rule_id: int) -> Rule:
        return _rule(self._one("SELECT * FROM rules WHERE id = ?", (rule_id,), f"rule {rule_id}"))

    def approve_rule(self, rule_id: int) -> Rule:
        """Activate a pending rule. Only the local operator command may call this."""
        with self.transaction() as conn:
            self.get_rule(rule_id)
            conn.execute(
                "UPDATE rules SET status = 'active', approved_at = ? WHERE id = ?",
                (self._now(), rule_id),
            )
            return self.get_rule(rule_id)

    def remove_rule(self, rule_id: int) -> bool:
        with self.transaction() as conn:
            return bool(conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,)).rowcount)

    def list_rules(self, status: RuleStatus | None = None) -> list[Rule]:
        if status is None:
            rows = self._conn.execute("SELECT * FROM rules ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM rules WHERE status = ? ORDER BY id", (RuleStatus(status).value,)
            ).fetchall()
        return [_rule(r) for r in rows]

    def sync_config_rules(self, rules: Iterable[tuple[str, str, Level]]) -> list[Rule]:
        """Replace every source=config rule with (kind, target, level) triples from the config."""
        with self.transaction() as conn:
            conn.execute("DELETE FROM rules WHERE source = 'config'")
            for kind, target, level in rules:
                self.add_rule(
                    kind, level, target=target, source=RuleSource.CONFIG, created_by="config"
                )
        return [r for r in self.list_rules() if r.source is RuleSource.CONFIG]

    # -- schedules ---------------------------------------------------------

    def create_schedule(
        self,
        *,
        what: str,
        cadence: str,
        tz: str,
        destination: Destination,
        creator: str,
        profile: str,
        notify_rule: NotifyRule = NotifyRule.ALWAYS,
        until: datetime | None = None,
        next_run_at: datetime | None = None,
        status: ScheduleStatus = ScheduleStatus.ACTIVE,
    ) -> Schedule:
        """Store a schedule. The caller computes next_run_at (schedules.py owns cadence)."""
        now = self._now()
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO schedules (what, cadence, tz, until, notify_rule, channel, "
                "conversation, thread, creator, profile, status, next_run_at, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    what,
                    cadence,
                    tz,
                    _ts(until),
                    NotifyRule(notify_rule).value,
                    destination.channel,
                    destination.conversation,
                    destination.thread,
                    creator,
                    profile,
                    ScheduleStatus(status).value,
                    _ts(next_run_at),
                    now,
                    now,
                ),
            )
            return self.get_schedule(cur.lastrowid)

    def get_schedule(self, schedule_id: int) -> Schedule:
        return _schedule(
            self._one(
                "SELECT * FROM schedules WHERE id = ?", (schedule_id,), f"schedule {schedule_id}"
            )
        )

    def list_schedules(
        self, status: ScheduleStatus | None = None, creator: str | None = None
    ) -> list[Schedule]:
        sql = "SELECT * FROM schedules WHERE 1 = 1"
        params: list[Any] = []
        if status is not None:
            sql += " AND status = ?"
            params.append(ScheduleStatus(status).value)
        if creator is not None:
            sql += " AND creator = ?"
            params.append(creator)
        rows = self._conn.execute(sql + " ORDER BY id", params).fetchall()
        return [_schedule(r) for r in rows]

    def update_schedule(self, schedule_id: int, **fields: Any) -> Schedule:
        """Change any of: what, cadence, tz, until, notify_rule, channel, conversation,
        thread, status, next_run_at, last_run_at, last_result_hash, last_task_id."""
        unknown = set(fields) - set(_SCHEDULE_FIELDS)
        if unknown:
            raise ValueError(f"update_schedule cannot change {sorted(unknown)}")
        if not fields:
            return self.get_schedule(schedule_id)
        sets = [f"{name} = ?" for name in fields]
        params = [_SCHEDULE_FIELDS[name](value) for name, value in fields.items()]
        with self.transaction() as conn:
            self.get_schedule(schedule_id)
            conn.execute(
                f"UPDATE schedules SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
                (*params, self._now(), schedule_id),
            )
            return self.get_schedule(schedule_id)

    def delete_schedule(self, schedule_id: int) -> bool:
        with self.transaction() as conn:
            return bool(conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,)).rowcount)

    def due_schedules(self) -> list[Schedule]:
        """Active schedules whose next_run_at has passed, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM schedules WHERE status = 'active' AND next_run_at IS NOT NULL "
            "AND next_run_at <= ? ORDER BY next_run_at, id",
            (self._now(),),
        ).fetchall()
        return [_schedule(r) for r in rows]

    # -- notes -------------------------------------------------------------

    def add_note(
        self,
        profile: str,
        text: str,
        *,
        source: NoteSource,
        subject: str = "",
        source_task_id: int | None = None,
    ) -> Note:
        now = self._now()
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO notes (profile, subject, text, source, source_task_id, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (profile, subject, text, NoteSource(source).value, source_task_id, now, now),
            )
            return self.get_note(cur.lastrowid)

    def get_note(self, note_id: int) -> Note:
        return _note(self._one("SELECT * FROM notes WHERE id = ?", (note_id,), f"note {note_id}"))

    def update_note(
        self, note_id: int, *, text: str | None = None, subject: str | None = None
    ) -> Note:
        with self.transaction() as conn:
            self.get_note(note_id)
            conn.execute(
                "UPDATE notes SET text = COALESCE(?, text), subject = COALESCE(?, subject), "
                "updated_at = ? WHERE id = ?",
                (text, subject, self._now(), note_id),
            )
            return self.get_note(note_id)

    def delete_note(self, note_id: int) -> bool:
        with self.transaction() as conn:
            return bool(conn.execute("DELETE FROM notes WHERE id = ?", (note_id,)).rowcount)

    def list_notes(self, profile: str | None = None) -> list[Note]:
        """Oldest first. profile=None lists every profile (operator view)."""
        if profile is None:
            rows = self._conn.execute("SELECT * FROM notes ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM notes WHERE profile = ? ORDER BY id", (profile,)
            ).fetchall()
        return [_note(r) for r in rows]

    # -- review log --------------------------------------------------------

    def record_review(
        self,
        task_id: int,
        verdict: ReviewVerdict,
        *,
        reason: str = "",
        action_id: int | None = None,
    ) -> ReviewRecord:
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO reviews (task_id, action_id, verdict, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, action_id, ReviewVerdict(verdict).value, reason, self._now()),
            )
            row = conn.execute("SELECT * FROM reviews WHERE id = ?", (cur.lastrowid,)).fetchone()
            return _review(row)

    def list_reviews(self, task_id: int, limit: int = 50) -> list[ReviewRecord]:
        """Newest first."""
        rows = self._conn.execute(
            "SELECT * FROM reviews WHERE task_id = ? ORDER BY id DESC LIMIT ?", (task_id, limit)
        ).fetchall()
        return [_review(r) for r in rows]

    def denial_counts(self, task_id: int, window: int = 50) -> DenialCounts:
        """Count denials in a row and within the last `window` verdicts for a task."""
        recent = self.list_reviews(task_id, limit=window)
        in_a_row = 0
        for review in recent:
            if review.verdict is not ReviewVerdict.DENY:
                break
            in_a_row += 1
        in_window = sum(1 for r in recent if r.verdict is ReviewVerdict.DENY)
        return DenialCounts(in_a_row=in_a_row, in_window=in_window, window=window)

    # -- audit log ---------------------------------------------------------

    def log_event(
        self, kind: str, detail: dict[str, Any] | None = None, *, task_id: int | None = None
    ) -> EventRecord:
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO events (task_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
                (task_id, kind, _dumps(detail or {}), self._now()),
            )
            row = conn.execute("SELECT * FROM events WHERE id = ?", (cur.lastrowid,)).fetchone()
            return _event(row)

    def list_events(
        self, task_id: int | None = None, kind: str | None = None, limit: int = 100
    ) -> list[EventRecord]:
        """Newest first."""
        sql = "SELECT * FROM events WHERE 1 = 1"
        params: list[Any] = []
        if task_id is not None:
            sql += " AND task_id = ?"
            params.append(task_id)
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        rows = self._conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
        return [_event(r) for r in rows]


def open_store(config: Config, clock: Clock | None = None) -> Store:
    """Create the state directories, open state_root/opendot.db and migrate it."""
    config.ensure_directories()
    store = Store(config.db_path, clock=clock)
    store.migrate()
    return store
