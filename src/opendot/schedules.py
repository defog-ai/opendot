"""Saved schedules: recurring work that runs as new tasks.

A schedule holds:
    what         the task text each run starts with
    cadence      a five-field cron expression ("0 9 * * 1-5"), read in tz
    tz           an IANA time zone name; runs follow its daylight-saving changes
    until        optional end time; no run starts after it
    notify_rule  "always", or "changed" to post only when the result differs
                 from the previous run's result (compared by sha256 hash)
    destination  where results go; fixed when the schedule is created

Each run is a new task owned by the schedule's creator, in the creator's notes
profile, with no approvals carried over from any earlier task. Results go only
to the stored destination, which the person approved when the schedule was made.
Saving a schedule is the action kind "schedule.create", which always needs a
person's confirmation.

Cadence and daylight saving: the cron expression is matched against wall-clock
time in tz. A time that does not exist on the day clocks go forward (02:30 in
New York in March) runs at the same moment one hour later on the clock. A time
that happens twice on the day clocks go back runs once, at the first occurrence.
A run missed while nothing was ticking is not made up; the schedule moves to the
next time after now.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from opendot.actions import (
    KIND_SCHEDULE_CREATE,
    ActionContext,
    ActionResult,
    InvalidProposal,
    PreparedAction,
)
from opendot.models import (
    Destination,
    Level,
    NotifyRule,
    Schedule,
    ScheduleStatus,
    Task,
    TaskSource,
    from_iso,
)
from opendot.store import NotFound

if TYPE_CHECKING:
    from opendot.store import Store

__all__ = [
    "ACTION_HANDLERS",
    "MAX_WHAT_CHARS",
    "ScheduleCreateHandler",
    "ScheduleError",
    "create_schedule",
    "end_schedule",
    "next_run",
    "pause_schedule",
    "result_hash",
    "resume_schedule",
    "run_due_schedules",
    "schedule_target",
    "should_notify",
    "validate_cadence",
    "validate_tz",
]

MAX_WHAT_CHARS = 2000
_MAX_CRON_STEPS = 100_000


class ScheduleError(ValueError):
    """A schedule field is invalid."""


# ---------------------------------------------------------------------------
# Cadence and time zone
# ---------------------------------------------------------------------------


def validate_cadence(cadence: str) -> str:
    """Accept exactly five cron fields (minute hour day-of-month month day-of-week)."""
    if not isinstance(cadence, str):
        raise ScheduleError("cadence must be text")
    fields = cadence.split()
    if len(fields) != 5:
        raise ScheduleError(
            f"cadence must have five fields (minute hour day month weekday): {cadence!r}"
        )
    normalized = " ".join(fields)
    if not croniter.is_valid(normalized):
        raise ScheduleError(f"cadence is not a valid cron expression: {cadence!r}")
    return normalized


def validate_tz(tz: str) -> ZoneInfo:
    if not isinstance(tz, str) or not tz:
        raise ScheduleError("tz must be an IANA time zone name")
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleError(f"unknown time zone: {tz!r}") from None


def next_run(cadence: str, tz: str, after: datetime) -> datetime:
    """The first run time strictly after `after`, as an aware UTC datetime."""
    if after.tzinfo is None:
        raise ValueError("after must be timezone-aware")
    zone = validate_tz(tz)
    after_utc = after.astimezone(UTC)
    # Walk wall-clock times without a zone, then attach the zone. croniter given an
    # aware start time repeats the doubled hour when clocks go back.
    wall = after_utc.astimezone(zone).replace(tzinfo=None)
    times = croniter(validate_cadence(cadence), wall)
    for _ in range(_MAX_CRON_STEPS):
        candidate = times.get_next(datetime).replace(tzinfo=zone)  # fold=0: first occurrence
        candidate_utc = candidate.astimezone(UTC)
        if candidate_utc > after_utc:
            return candidate_utc
    raise ScheduleError(f"cadence {cadence!r} has no run time after {after_utc.isoformat()}")


# ---------------------------------------------------------------------------
# Creating and changing schedules
# ---------------------------------------------------------------------------


def create_schedule(
    store: Store,
    *,
    what: str,
    cadence: str,
    tz: str,
    destination: Destination,
    creator: str,
    profile: str,
    notify_rule: NotifyRule | str = NotifyRule.ALWAYS,
    until: datetime | None = None,
) -> Schedule:
    """Validate the fields, compute the first run and store an active schedule."""
    if not isinstance(what, str) or not what.strip():
        raise ScheduleError("what must say what each run should do")
    if len(what) > MAX_WHAT_CHARS:
        raise ScheduleError(f"what is longer than {MAX_WHAT_CHARS} characters")
    cadence = validate_cadence(cadence)
    validate_tz(tz)
    try:
        notify_rule = NotifyRule(notify_rule)
    except ValueError:
        raise ScheduleError(f"notify_rule must be 'always' or 'changed': {notify_rule!r}") from None
    if until is not None and until.tzinfo is None:
        raise ScheduleError("until must include a time zone")
    now = store.clock.now()
    first = next_run(cadence, tz, now)
    if until is not None and first > until:
        raise ScheduleError("the schedule would never run: its first run is after until")
    schedule = store.create_schedule(
        what=what.strip(),
        cadence=cadence,
        tz=tz,
        destination=destination,
        creator=creator,
        profile=profile,
        notify_rule=notify_rule,
        until=until,
        next_run_at=first,
    )
    store.log_event(
        "schedule.created",
        {"schedule_id": schedule.id, "cadence": cadence, "tz": tz, "creator": creator},
    )
    return schedule


def pause_schedule(store: Store, schedule_id: int) -> Schedule:
    return store.update_schedule(schedule_id, status=ScheduleStatus.PAUSED)


def resume_schedule(store: Store, schedule_id: int) -> Schedule:
    """Make a paused schedule active again, with its next run computed from now."""
    schedule = store.get_schedule(schedule_id)
    if schedule.status is ScheduleStatus.ENDED:
        raise ScheduleError(f"schedule {schedule_id} has ended")
    now = store.clock.now()
    upcoming = next_run(schedule.cadence, schedule.tz, now)
    if schedule.until is not None and upcoming > schedule.until:
        return end_schedule(store, schedule_id)
    return store.update_schedule(schedule_id, status=ScheduleStatus.ACTIVE, next_run_at=upcoming)


def end_schedule(store: Store, schedule_id: int) -> Schedule:
    schedule = store.update_schedule(schedule_id, status=ScheduleStatus.ENDED, next_run_at=None)
    store.log_event("schedule.ended", {"schedule_id": schedule_id})
    return schedule


# ---------------------------------------------------------------------------
# Running due schedules
# ---------------------------------------------------------------------------


def run_due_schedules(store: Store) -> list[Task]:
    """Start one new task for each active schedule whose next run has come.

    A schedule past its until time ends without a run. A schedule whose previous
    run is still in progress skips this run. Missed runs are not made up.
    """
    now = store.clock.now()
    started: list[Task] = []
    for schedule in store.due_schedules():
        if schedule.until is not None and now > schedule.until:
            end_schedule(store, schedule.id)
            continue
        upcoming = next_run(schedule.cadence, schedule.tz, now)
        ends = schedule.until is not None and upcoming > schedule.until

        if schedule.last_task_id is not None and _still_running(store, schedule.last_task_id):
            store.log_event(
                "schedule.run_skipped",
                {"schedule_id": schedule.id, "running_task_id": schedule.last_task_id},
            )
            if ends:
                end_schedule(store, schedule.id)
            else:
                store.update_schedule(schedule.id, next_run_at=upcoming)
            continue

        with store.transaction():
            task = store.create_task(
                requester=schedule.creator,
                text=schedule.what,
                channel=schedule.channel,
                conversation=schedule.conversation,
                thread=schedule.thread,
                profile=schedule.profile,
                source=TaskSource.SCHEDULE,
                schedule_id=schedule.id,
            )
            store.update_schedule(
                schedule.id,
                last_run_at=now,
                last_task_id=task.id,
                next_run_at=None if ends else upcoming,
                status=ScheduleStatus.ENDED if ends else ScheduleStatus.ACTIVE,
            )
            store.log_event("schedule.run_started", {"schedule_id": schedule.id}, task_id=task.id)
        started.append(task)
    return started


def _still_running(store: Store, task_id: int) -> bool:
    try:
        return not store.get_task(task_id).state.is_terminal
    except NotFound:
        return False


# ---------------------------------------------------------------------------
# Notify rule
# ---------------------------------------------------------------------------


def result_hash(text: str) -> str:
    """sha256 hex of the result text with surrounding whitespace removed."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def should_notify(store: Store, schedule: Schedule, result_text: str) -> bool:
    """Record this run's result hash and say whether to post the result.

    notify_rule "always": always True. "changed": True only when the hash differs
    from the previous run's (the first run always posts).
    """
    digest = result_hash(result_text)
    changed = digest != schedule.last_result_hash
    store.update_schedule(schedule.id, last_result_hash=digest)
    if schedule.notify_rule is NotifyRule.ALWAYS:
        return True
    return changed


# ---------------------------------------------------------------------------
# The schedule.create action
# ---------------------------------------------------------------------------


def schedule_target(destination: Destination) -> str:
    return f"schedule:{destination.channel}:{destination.conversation}:{destination.thread or '-'}"


class ScheduleCreateHandler:
    """Saves a schedule whose results go to the requester's own thread.

    Proposal fields:
        what      text each run starts with (required)
        cadence   five-field cron expression (required)
        tz        IANA time zone (default: core.timezone from the config)
        until     optional ISO 8601 end time with an offset
        notify    "always" (default) or "changed"

    The destination is always the proposing task's own thread; the proposal
    cannot name another one. The action counts as outward because every later
    run posts to that destination, so only a single-use approval of the exact
    fields can cover it.
    """

    kind = KIND_SCHEDULE_CREATE
    outward = True
    default_level = Level.ASK
    floor = Level.ASK

    def prepare(self, proposal: Mapping[str, Any], ctx: ActionContext) -> PreparedAction:
        what = proposal.get("what")
        if not isinstance(what, str) or not what.strip():
            raise InvalidProposal("schedule.create: what is required")
        if len(what) > MAX_WHAT_CHARS:
            raise InvalidProposal(f"schedule.create: what is longer than {MAX_WHAT_CHARS}")
        tz = proposal.get("tz") or ctx.config.core.timezone
        notify = proposal.get("notify", NotifyRule.ALWAYS.value)
        until_text = proposal.get("until")
        try:
            cadence = validate_cadence(proposal.get("cadence"))  # type: ignore[arg-type]
            validate_tz(tz)
            notify = NotifyRule(notify).value
        except ValueError as exc:
            raise InvalidProposal(f"schedule.create: {exc}") from None
        until: str | None = None
        if until_text is not None:
            try:
                until = from_iso(str(until_text)).isoformat()
            except ValueError:
                raise InvalidProposal(
                    "schedule.create: until must be ISO 8601 text with a time zone offset"
                ) from None
        destination = ctx.task.destination
        payload = {
            "what": what.strip(),
            "cadence": cadence,
            "tz": tz,
            "until": until,
            "notify": notify,
            "destination": {
                "channel": destination.channel,
                "conversation": destination.conversation,
                "thread": destination.thread,
            },
        }
        return PreparedAction(
            kind=self.kind, target=schedule_target(destination), payload=payload, outward=True
        )

    def execute(self, action: PreparedAction, ctx: ActionContext) -> ActionResult:
        payload = action.payload
        destination = Destination(**payload["destination"])
        if destination != ctx.task.destination:
            return ActionResult(ok=False, detail={"error": "destination is not the task's thread"})
        try:
            schedule = create_schedule(
                ctx.store,
                what=payload["what"],
                cadence=payload["cadence"],
                tz=payload["tz"],
                destination=destination,
                creator=ctx.task.requester,
                profile=ctx.task.profile,
                notify_rule=payload["notify"],
                until=from_iso(payload["until"]) if payload["until"] else None,
            )
        except ScheduleError as exc:
            return ActionResult(ok=False, detail={"error": str(exc)})
        return ActionResult(
            ok=True,
            detail={"schedule_id": schedule.id, "next_run_at": schedule.next_run_at.isoformat()},
        )


ACTION_HANDLERS: list = [ScheduleCreateHandler()]
