from __future__ import annotations

from datetime import UTC, datetime

import pytest

from opendot.actions import ActionContext, ActionRegistry, InvalidProposal
from opendot.approvals import consume_approval, decide, request_approval
from opendot.models import (
    Destination,
    Level,
    NotifyRule,
    ScheduleStatus,
    TaskSource,
    TaskState,
)
from opendot.rules import RuleEngine
from opendot.schedules import (
    ScheduleCreateHandler,
    ScheduleError,
    create_schedule,
    next_run,
    pause_schedule,
    result_hash,
    resume_schedule,
    run_due_schedules,
    should_notify,
    validate_cadence,
)

HOME = Destination("slack", "C1", "100.1")


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def make(store, cadence="0 9 * * *", tz="UTC", **kw):
    fields = dict(
        what="check the build",
        cadence=cadence,
        tz=tz,
        destination=HOME,
        creator="alice",
        profile="slack:alice",
    )
    fields.update(kw)
    return create_schedule(store, **fields)


# -- cadence ----------------------------------------------------------------


def test_cadence_needs_five_valid_fields():
    assert validate_cadence(" 0  9 * * 1-5 ") == "0 9 * * 1-5"
    for bad in ("0 9 * *", "0 9 * * * *", "@daily", "61 9 * * *", "", "every day"):
        with pytest.raises(ScheduleError):
            validate_cadence(bad)


def test_next_run_is_strictly_after():
    assert next_run("0 9 * * *", "UTC", utc(2026, 1, 5, 9)) == utc(2026, 1, 6, 9)
    assert next_run("0 9 * * *", "UTC", utc(2026, 1, 5, 8, 59)) == utc(2026, 1, 5, 9)


def test_next_run_follows_spring_forward():
    # New York moves from UTC-5 to UTC-4 on 2026-03-08.
    ny = "America/New_York"
    assert next_run("0 9 * * *", ny, utc(2026, 3, 7, 15)) == utc(2026, 3, 8, 13)
    assert next_run("0 9 * * *", ny, utc(2026, 3, 6, 15)) == utc(2026, 3, 7, 14)
    # 02:30 does not exist that day; the run happens at 03:30 local (07:30 UTC).
    assert next_run("30 2 * * *", ny, utc(2026, 3, 7, 12)) == utc(2026, 3, 8, 7, 30)


def test_next_run_follows_fall_back_and_runs_the_doubled_hour_once():
    ny = "America/New_York"
    # New York moves from UTC-4 to UTC-5 on 2026-11-01.
    assert next_run("0 9 * * *", ny, utc(2026, 10, 31, 14)) == utc(2026, 11, 1, 14)
    first = next_run("30 1 * * *", ny, utc(2026, 10, 31, 12))
    assert first == utc(2026, 11, 1, 5, 30)  # 01:30 EDT, the first occurrence
    assert next_run("30 1 * * *", ny, first) == utc(2026, 11, 2, 6, 30)  # not 01:30 EST again


def test_next_run_needs_aware_time_and_known_zone():
    with pytest.raises(ValueError):
        next_run("0 9 * * *", "UTC", datetime(2026, 1, 1))
    with pytest.raises(ScheduleError):
        next_run("0 9 * * *", "Mars/Olympus", utc(2026, 1, 1))


# -- create -----------------------------------------------------------------


def test_create_computes_first_run(store, clock):
    schedule = make(store, cadence="0 9 * * *", tz="Asia/Kolkata")
    # 12:00 UTC on Monday is 17:30 in Kolkata; the next 09:00 there is 03:30 UTC Tuesday.
    assert schedule.next_run_at == utc(2026, 1, 6, 3, 30)
    assert schedule.status is ScheduleStatus.ACTIVE
    assert schedule.destination == HOME


@pytest.mark.parametrize(
    "fields",
    [
        {"what": "  "},
        {"cadence": "* *"},
        {"tz": "Nowhere/Zone"},
        {"notify_rule": "sometimes"},
        {"until": datetime(2026, 2, 1)},
        {"until": utc(2026, 1, 5, 13)},  # before the first run
    ],
)
def test_create_rejects_bad_fields(store, fields):
    with pytest.raises(ScheduleError):
        make(store, **fields)


# -- running ----------------------------------------------------------------


def test_due_schedule_starts_a_task_owned_by_the_creator(store, clock):
    schedule = make(store)
    assert run_due_schedules(store) == []
    clock.set(utc(2026, 1, 6, 9, 0, 30))
    [task] = run_due_schedules(store)
    assert task.requester == "alice"
    assert task.profile == "slack:alice"
    assert task.source is TaskSource.SCHEDULE
    assert task.schedule_id == schedule.id
    assert task.destination == HOME
    assert task.text == "check the build"
    assert task.state is TaskState.QUEUED
    after = store.get_schedule(schedule.id)
    assert after.last_task_id == task.id
    assert after.last_run_at == clock.now()
    assert after.next_run_at == utc(2026, 1, 7, 9)
    assert run_due_schedules(store) == []


def test_missed_runs_are_not_made_up(store, clock):
    schedule = make(store)
    clock.set(utc(2026, 1, 10, 12))
    assert len(run_due_schedules(store)) == 1
    assert store.get_schedule(schedule.id).next_run_at == utc(2026, 1, 11, 9)


def test_run_is_skipped_while_the_previous_run_is_active(store, clock):
    schedule = make(store)
    clock.set(utc(2026, 1, 6, 9))
    [first] = run_due_schedules(store)
    clock.set(utc(2026, 1, 7, 9))
    assert run_due_schedules(store) == []
    assert store.get_schedule(schedule.id).next_run_at == utc(2026, 1, 8, 9)
    store.set_task_state(first.id, TaskState.DONE)
    clock.set(utc(2026, 1, 8, 9))
    assert len(run_due_schedules(store)) == 1


def test_until_stops_the_schedule(store, clock):
    schedule = make(store, until=utc(2026, 1, 7, 12))
    clock.set(utc(2026, 1, 6, 9))
    [first] = run_due_schedules(store)
    store.set_task_state(first.id, TaskState.DONE)
    clock.set(utc(2026, 1, 7, 9))
    assert len(run_due_schedules(store)) == 1  # the last run before until
    ended = store.get_schedule(schedule.id)
    assert ended.status is ScheduleStatus.ENDED
    assert ended.next_run_at is None
    clock.set(utc(2026, 1, 8, 9))
    assert run_due_schedules(store) == []


def test_schedule_past_until_ends_without_a_run(store, clock):
    schedule = make(store, until=utc(2026, 1, 6, 10))
    clock.set(utc(2026, 1, 6, 11))
    assert run_due_schedules(store) == []
    assert store.get_schedule(schedule.id).status is ScheduleStatus.ENDED


def test_paused_schedule_does_not_run_and_resumes_from_now(store, clock):
    schedule = make(store)
    pause_schedule(store, schedule.id)
    clock.set(utc(2026, 1, 9, 12))
    assert run_due_schedules(store) == []
    resumed = resume_schedule(store, schedule.id)
    assert resumed.status is ScheduleStatus.ACTIVE
    assert resumed.next_run_at == utc(2026, 1, 10, 9)


def test_scheduled_run_does_not_inherit_approvals(store, config, clock):
    registry = ActionRegistry([ScheduleCreateHandler()])
    creator_task = store.create_task(
        requester="alice", text="x", channel="slack", conversation="C1", thread="100.1"
    )
    ctx = ActionContext(task=creator_task, store=store, config=config, channels={})
    prepared = registry.prepare(
        {"kind": "schedule.create", "what": "w", "cadence": "0 9 * * *"}, ctx
    )
    approval = request_approval(store, creator_task, prepared)
    decide(store, config, approval.id, granted=True, decided_by="alice", channel="slack")
    make(store)
    clock.set(utc(2026, 1, 6, 9))
    [run] = run_due_schedules(store)
    assert store.list_approvals(run.id) == []
    assert consume_approval(store, run, prepared) is None


# -- notify rule ------------------------------------------------------------


def test_notify_always(store):
    schedule = make(store)
    assert should_notify(store, schedule, "same")
    assert should_notify(store, store.get_schedule(schedule.id), "same")


def test_notify_only_when_changed(store):
    schedule = make(store, notify_rule=NotifyRule.CHANGED)
    assert should_notify(store, schedule, "build is green")  # first result always posts
    schedule = store.get_schedule(schedule.id)
    assert schedule.last_result_hash == result_hash("build is green")
    assert not should_notify(store, schedule, "build is green\n")
    schedule = store.get_schedule(schedule.id)
    assert should_notify(store, schedule, "build is red")


# -- schedule.create action -------------------------------------------------


@pytest.fixture
def slack_task(store):
    return store.create_task(
        requester="alice", text="x", channel="slack", conversation="C1", thread="100.1"
    )


def test_schedule_create_needs_approval_and_targets_the_own_thread(store, config, slack_task):
    registry = ActionRegistry([ScheduleCreateHandler()])
    ctx = ActionContext(task=slack_task, store=store, config=config, channels={})
    prepared = registry.prepare(
        {
            "kind": "schedule.create",
            "what": "summarise the news",
            "cadence": "0 8 * * 1-5",
            "tz": "Europe/Berlin",
            "notify": "changed",
            "destination": {"channel": "slack", "conversation": "C999"},  # ignored
        },
        ctx,
    )
    assert prepared.outward
    assert prepared.target == "schedule:slack:C1:100.1"
    assert prepared.payload["destination"]["conversation"] == "C1"
    assert RuleEngine(registry, []).decide(prepared, ctx).level is Level.ASK

    result = registry.execute(prepared, ctx)
    assert result.ok
    saved = store.get_schedule(result.detail["schedule_id"])
    assert saved.destination == slack_task.destination
    assert saved.creator == "alice"
    assert saved.profile == slack_task.profile
    assert saved.notify_rule is NotifyRule.CHANGED
    assert saved.tz == "Europe/Berlin"


def test_schedule_create_uses_config_timezone_and_parses_until(store, config, slack_task):
    registry = ActionRegistry([ScheduleCreateHandler()])
    ctx = ActionContext(task=slack_task, store=store, config=config, channels={})
    prepared = registry.prepare(
        {
            "kind": "schedule.create",
            "what": "w",
            "cadence": "0 9 * * *",
            "until": "2026-02-01T00:00:00+01:00",
        },
        ctx,
    )
    assert prepared.payload["tz"] == config.core.timezone
    result = registry.execute(prepared, ctx)
    saved = store.get_schedule(result.detail["schedule_id"])
    assert saved.until == utc(2026, 1, 31, 23)


@pytest.mark.parametrize(
    "proposal",
    [
        {"kind": "schedule.create", "cadence": "0 9 * * *"},
        {"kind": "schedule.create", "what": "w"},
        {"kind": "schedule.create", "what": "w", "cadence": "@hourly"},
        {"kind": "schedule.create", "what": "w", "cadence": "0 9 * * *", "tz": "Nope/Nope"},
        {"kind": "schedule.create", "what": "w", "cadence": "0 9 * * *", "notify": "maybe"},
        {"kind": "schedule.create", "what": "w", "cadence": "0 9 * * *", "until": "soon"},
        {
            "kind": "schedule.create",
            "what": "w",
            "cadence": "0 9 * * *",
            "until": "2026-02-01T00:00:00",
        },
    ],
)
def test_schedule_create_rejects_bad_proposals(store, config, slack_task, proposal):
    registry = ActionRegistry([ScheduleCreateHandler()])
    ctx = ActionContext(task=slack_task, store=store, config=config, channels={})
    with pytest.raises(InvalidProposal):
        registry.prepare(proposal, ctx)


def test_rule_cannot_relax_schedule_create(store, config, slack_task):
    from opendot.models import RuleSource

    store.add_rule("schedule.create", Level.ALLOW, source=RuleSource.OPERATOR, created_by="op")
    registry = ActionRegistry([ScheduleCreateHandler()])
    ctx = ActionContext(task=slack_task, store=store, config=config, channels={})
    prepared = registry.prepare(
        {"kind": "schedule.create", "what": "w", "cadence": "0 9 * * *"}, ctx
    )
    assert RuleEngine.from_store(registry, store).decide(prepared, ctx).level is Level.ASK


def test_execute_refuses_a_destination_other_than_the_task_thread(store, config, slack_task):
    registry = ActionRegistry([ScheduleCreateHandler()])
    ctx = ActionContext(task=slack_task, store=store, config=config, channels={})
    prepared = registry.prepare(
        {"kind": "schedule.create", "what": "w", "cadence": "0 9 * * *"}, ctx
    )
    other = store.create_task(requester="alice", text="y", channel="slack", conversation="C2")
    other_ctx = ActionContext(task=other, store=store, config=config, channels={})
    assert not registry.execute(prepared, other_ctx).ok
    assert store.list_schedules() == []
