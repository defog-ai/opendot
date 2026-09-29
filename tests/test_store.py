import os
import sqlite3
import stat
from datetime import timedelta

import pytest

from opendot.models import (
    ApprovalMode,
    ApprovalStatus,
    AttemptStatus,
    Destination,
    IncomingMessage,
    Level,
    MessageKind,
    NoteSource,
    ReviewVerdict,
    RuleSource,
    RuleStatus,
    ScheduleStatus,
    Step,
    TaskState,
)
from opendot.store import LeaseLost, LockBusy, NotFound, Store, StoreError, WorkerLock


def make_task(store: Store, text: str = "summarise the report", **kw):
    kw.setdefault("requester", "alice")
    kw.setdefault("channel", "cli")
    kw.setdefault("conversation", "local")
    return store.create_task(text=text, **kw)


def test_migrate_from_empty_and_twice(tmp_path, clock):
    path = tmp_path / "fresh.db"
    store = Store(path, clock=clock)
    assert store.schema_version() is None
    store.migrate()
    assert store.schema_version() == 1
    task = make_task(store)
    store.migrate()
    assert store.get_task(task.id).text == "summarise the report"
    store.close()


def test_database_file_is_private(store):
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


def test_create_task_defaults(store, clock):
    task = make_task(store)
    assert task.state is TaskState.QUEUED
    assert task.profile == "cli:alice"
    assert task.created_at == clock.now()
    assert task.destination == Destination("cli", "local", None)
    with pytest.raises(NotFound):
        store.get_task(999)


def test_claim_lease_expiry_and_requeue(store, clock):
    first = make_task(store, "one")
    make_task(store, "two")
    claimed = store.claim_next("w1", lease_seconds=60)
    assert claimed.id == first.id
    assert claimed.state is TaskState.RUNNING
    assert claimed.lease_owner == "w1"

    store.renew_lease(first.id, "w1", 60)
    with pytest.raises(LeaseLost):
        store.renew_lease(first.id, "w2", 60)

    clock.advance(seconds=61)
    # The expired task goes back to the queue and, being oldest, is claimed again.
    again = store.claim_next("w2", lease_seconds=60)
    assert again.id == first.id
    assert again.lease_owner == "w2"
    with pytest.raises(LeaseLost):
        store.release_task(first.id, "w1", TaskState.DONE)

    done = store.release_task(first.id, "w2", TaskState.DONE, summary="ok")
    assert done.state is TaskState.DONE
    assert done.lease_owner is None
    assert done.finished_at == clock.now()
    assert done.summary == "ok"


def test_two_stores_never_claim_the_same_task(store, clock):
    other = Store(store.path, clock=clock)
    try:
        for i in range(3):
            make_task(store, f"task {i}")
        claimed = []
        for owner, db in [("a", store), ("b", other)] * 3:
            task = db.claim_next(owner, 60)
            if task is not None:
                claimed.append(task.id)
        assert len(claimed) == 3
        assert len(set(claimed)) == 3
        assert store.claim_next("a", 60) is None
        assert other.claim_next("b", 60) is None
    finally:
        other.close()


def test_claim_task_only_when_queued(store):
    task = make_task(store)
    assert store.claim_task(task.id, "w", 60).state is TaskState.RUNNING
    assert store.claim_task(task.id, "w", 60) is None


def test_worker_lock_is_exclusive(tmp_path):
    path = tmp_path / "worker.lock"
    with WorkerLock(path) as first:
        assert first.held
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        with pytest.raises(LockBusy):
            WorkerLock(path).acquire()
    second = WorkerLock(path)
    second.acquire()
    assert second.held
    second.release()
    assert not second.held


def test_waiting_and_wake_due(store, clock):
    task = make_task(store)
    with pytest.raises(ValueError):
        store.set_task_state(task.id, TaskState.WAITING)
    store.set_task_state(task.id, TaskState.WAITING, wait_until=clock.now() + timedelta(hours=1))
    assert store.wake_due() == []
    clock.advance(hours=1)
    woken = store.wake_due()
    assert [t.id for t in woken] == [task.id]
    assert woken[0].state is TaskState.QUEUED
    assert woken[0].wait_until is None


def test_update_task_and_usage(store):
    task = make_task(store)
    updated = store.update_task(task.id, backend_thread_id="th-1", thread="42")
    assert updated.backend_thread_id == "th-1"
    assert updated.thread == "42"
    with pytest.raises(ValueError):
        store.update_task(task.id, state="done")
    store.add_usage(task.id, turns=3, tokens=100, seconds=1.5)
    task = store.add_usage(task.id, turns=1, tokens=50, seconds=0.5)
    assert (task.turns_used, task.tokens_used, task.active_seconds) == (4, 150, 2.0)


def test_find_thread_task(store):
    old = make_task(store, thread="7")
    store.set_task_state(old.id, TaskState.DONE)
    new = make_task(store, thread="7")
    assert store.find_thread_task("cli", "local", "7").id == new.id
    store.set_task_state(new.id, TaskState.STOPPED)
    assert store.find_thread_task("cli", "local", "7", active_only=True) is None
    assert store.find_thread_task("cli", "local", "8") is None


def test_attempts_are_numbered_per_task(store):
    task = make_task(store)
    first = store.start_attempt(task.id, Step.WORK, "fake")
    second = store.start_attempt(task.id, Step.REVIEW, "fake")
    assert (first.attempt_number, second.attempt_number) == (1, 2)
    done = store.finish_attempt(
        first.id,
        AttemptStatus.SUCCEEDED,
        output={"summary": "x"},
        usage={"turns": 2},
        backend_thread_id="th",
    )
    assert done.output == {"summary": "x"}
    assert done.usage == {"turns": 2}
    assert done.backend_thread_id == "th"
    assert [a.step for a in store.list_attempts(task.id)] == [Step.WORK, Step.REVIEW]


def _incoming(external_id="local:1", text="hello"):
    return IncomingMessage(
        channel="fake",
        external_id=external_id,
        conversation="local",
        thread="1",
        author="alice",
        text=text,
        is_reply=False,
    )


def test_message_intake_drops_duplicates(store):
    first = store.record_message(_incoming(), MessageKind.REQUEST)
    assert first is not None
    assert store.record_message(_incoming(text="again"), MessageKind.REQUEST) is None
    assert store.has_message("fake", "local:1")
    assert store.get_message(first.id).text == "hello"


def test_pending_messages_and_applied(store):
    task = make_task(store)
    steer = store.record_message(_incoming("local:2"), MessageKind.STEER, task_id=task.id)
    stop = store.record_message(_incoming("local:3"), MessageKind.STOP)
    store.set_message_task(stop.id, task.id)
    assert [m.id for m in store.pending_messages(task.id)] == [steer.id, stop.id]
    assert [m.id for m in store.pending_messages(task.id, [MessageKind.STOP])] == [stop.id]
    store.mark_messages_applied([steer.id])
    assert [m.id for m in store.pending_messages(task.id)] == [stop.id]


def test_cursors_and_thread_pauses(store):
    assert store.get_cursor("slack", "C1") is None
    store.set_cursor("slack", "C1", "100.1")
    store.set_cursor("slack", "C1", "200.2")
    assert store.get_cursor("slack", "C1") == "200.2"
    store.pause_thread("slack", "C1", "5", "alice")
    store.pause_thread("slack", "C1", "5", "alice")
    assert store.is_thread_paused("slack", "C1", "5")
    assert store.resume_thread("slack", "C1", "5")
    assert not store.resume_thread("slack", "C1", "5")


def test_outbox_retry_and_delivery(store):
    dest = Destination("fake", "local", "1")
    item = store.enqueue_outbox(dest, "hi")
    with pytest.raises(ValueError):
        store.enqueue_outbox(dest, "", reaction="eyes")
    failed = store.mark_outbox_failed(item.id, "timeout")
    assert failed.attempt_count == 1
    assert failed.last_error == "timeout"
    assert [i.id for i in store.pending_outbox()] == [item.id]
    assert store.pending_outbox(max_attempts=1) == []
    delivered = store.mark_delivered(item.id, "local:9")
    assert delivered.delivered_at is not None
    assert delivered.external_id == "local:9"
    assert delivered.last_error is None
    assert delivered.destination == dest
    assert store.pending_outbox() == []


def test_actions(store):
    task = make_task(store)
    action = store.record_action(
        task.id,
        kind="reply.post",
        target="cli:local:1",
        payload={"text": "hi"},
        payload_digest="d1",
        level=Level.ALLOW,
    )
    assert action.payload == {"text": "hi"}
    updated = store.set_action_status(action.id, "executed", result={"id": "x"})
    assert updated.result == {"id": "x"}
    assert [a.id for a in store.list_actions(task.id, status="executed")] == [action.id]


def test_single_use_approval_needs_digest_and_is_used_once(store):
    task = make_task(store)
    with pytest.raises(ValueError):
        store.create_approval(
            task.id,
            kind="note.write",
            target="t",
            mode=ApprovalMode.SINGLE_USE,
            payload_digest=None,
        )
    with pytest.raises(sqlite3.IntegrityError):
        # The table enforces the same rule.
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO approvals (task_id, kind, target, mode, status, requested_at) "
                "VALUES (?, 'k', 't', 'single_use', 'pending', 'x')",
                (task.id,),
            )
    approval = store.create_approval(
        task.id, kind="note.write", target="t", mode=ApprovalMode.SINGLE_USE, payload_digest="abc"
    )
    assert approval.status is ApprovalStatus.PENDING
    assert store.granted_approvals(task.id, "note.write", "t") == []
    granted = store.decide_approval(approval.id, granted=True, decided_by="alice")
    assert granted.status is ApprovalStatus.GRANTED
    assert granted.decided_by == "alice"
    with pytest.raises(StoreError):
        store.decide_approval(approval.id, granted=False, decided_by="alice")
    assert [a.id for a in store.granted_approvals(task.id, "note.write", "t")] == [approval.id]
    used = store.mark_approval_used(approval.id)
    assert used.status is ApprovalStatus.USED
    assert store.granted_approvals(task.id, "note.write", "t") == []
    with pytest.raises(StoreError):
        store.mark_approval_used(approval.id)


def test_until_task_end_approval_stays_granted(store):
    task = make_task(store)
    approval = store.create_approval(
        task.id,
        kind="notify.post",
        target="t",
        mode=ApprovalMode.UNTIL_TASK_END,
        payload_digest=None,
    )
    store.decide_approval(approval.id, granted=True, decided_by="alice")
    used = store.mark_approval_used(approval.id)
    assert used.status is ApprovalStatus.GRANTED
    assert used.used_at is not None


def test_approval_expiry(store, clock):
    task = make_task(store)
    approval = store.create_approval(
        task.id,
        kind="k",
        target="t",
        mode=ApprovalMode.SINGLE_USE,
        payload_digest="abc",
        expires_at=clock.now() + timedelta(minutes=10),
    )
    clock.advance(minutes=10)
    assert store.expire_approvals() == 1
    assert store.get_approval(approval.id).status is ApprovalStatus.EXPIRED
    with pytest.raises(StoreError):
        store.decide_approval(approval.id, granted=True, decided_by="alice")


def test_terminal_state_expires_open_approvals(store):
    task = make_task(store)
    pending = store.create_approval(
        task.id, kind="k", target="t", mode=ApprovalMode.SINGLE_USE, payload_digest="a"
    )
    granted = store.create_approval(
        task.id, kind="k", target="t", mode=ApprovalMode.UNTIL_TASK_END, payload_digest=None
    )
    denied = store.create_approval(
        task.id, kind="k", target="t", mode=ApprovalMode.SINGLE_USE, payload_digest="b"
    )
    store.decide_approval(granted.id, granted=True, decided_by="alice")
    store.decide_approval(denied.id, granted=False, decided_by="alice")
    store.set_task_state(task.id, TaskState.STOPPED)
    statuses = {a.id: a.status for a in store.list_approvals(task_id=task.id)}
    assert statuses == {
        pending.id: ApprovalStatus.EXPIRED,
        granted.id: ApprovalStatus.EXPIRED,
        denied.id: ApprovalStatus.DENIED,
    }


def test_rules(store):
    agent = store.add_rule("note.write", Level.ALLOW, source=RuleSource.AGENT, created_by="task:1")
    operator = store.add_rule(
        "reply.post", Level.ASK, target="cli:local", source=RuleSource.OPERATOR, created_by="op"
    )
    assert agent.status is RuleStatus.PENDING
    assert agent.approved_at is None
    assert operator.status is RuleStatus.ACTIVE
    assert [r.id for r in store.list_rules(RuleStatus.ACTIVE)] == [operator.id]
    assert store.approve_rule(agent.id).status is RuleStatus.ACTIVE

    synced = store.sync_config_rules([("schedule.create", "*", Level.ASK)])
    assert [(r.kind, r.source) for r in synced] == [("schedule.create", RuleSource.CONFIG)]
    synced = store.sync_config_rules([("note.*", "*", Level.HAND_OFF)])
    assert [r.kind for r in synced] == ["note.*"]
    assert len(store.list_rules()) == 3
    assert store.remove_rule(operator.id)
    assert not store.remove_rule(operator.id)


def test_schedules(store, clock):
    dest = Destination("slack", "C1", None)
    sched = store.create_schedule(
        what="check the news",
        cadence="0 9 * * 1-5",
        tz="Europe/Paris",
        destination=dest,
        creator="alice",
        profile="slack:alice",
        next_run_at=clock.now() + timedelta(hours=1),
    )
    assert sched.status is ScheduleStatus.ACTIVE
    assert sched.destination == dest
    assert store.due_schedules() == []
    clock.advance(hours=1)
    assert [s.id for s in store.due_schedules()] == [sched.id]

    task = make_task(store, source="schedule", schedule_id=sched.id)
    updated = store.update_schedule(
        sched.id,
        last_run_at=clock.now(),
        next_run_at=clock.now() + timedelta(days=1),
        last_result_hash="h1",
        last_task_id=task.id,
    )
    assert updated.last_result_hash == "h1"
    assert updated.last_task_id == task.id
    assert store.due_schedules() == []
    with pytest.raises(ValueError):
        store.update_schedule(sched.id, creator="mallory")

    store.update_schedule(sched.id, status=ScheduleStatus.PAUSED, next_run_at=clock.now())
    assert store.due_schedules() == []
    assert [s.id for s in store.list_schedules(creator="alice")] == [sched.id]
    assert store.list_schedules(status=ScheduleStatus.ACTIVE) == []
    assert store.delete_schedule(sched.id)
    assert not store.delete_schedule(sched.id)


def test_notes_are_kept_per_profile(store):
    a = store.add_note("slack:alice", "prefers metric units", source=NoteSource.REQUESTER)
    store.add_note("slack:bob", "works in Berlin", source=NoteSource.AGENT, subject="location")
    assert [n.text for n in store.list_notes("slack:alice")] == ["prefers metric units"]
    assert len(store.list_notes()) == 2
    edited = store.update_note(a.id, text="prefers imperial units")
    assert edited.text == "prefers imperial units"
    assert edited.subject == ""
    assert store.delete_note(a.id)
    assert store.list_notes("slack:alice") == []


def test_denial_counts(store):
    task = make_task(store)
    for verdict in ["deny", "approve", "deny", "deny"]:
        store.record_review(task.id, verdict, reason="r")
    counts = store.denial_counts(task.id)
    assert (counts.in_a_row, counts.in_window) == (2, 3)
    assert store.denial_counts(task.id, window=2).in_window == 2
    store.record_review(task.id, ReviewVerdict.ESCALATE_TO_USER)
    assert store.denial_counts(task.id).in_a_row == 0
    assert store.list_reviews(task.id)[0].verdict is ReviewVerdict.ESCALATE_TO_USER


def test_events(store):
    task = make_task(store)
    store.log_event("task.created", {"by": "alice"}, task_id=task.id)
    store.log_event("worker.started")
    assert [e.kind for e in store.list_events()] == ["worker.started", "task.created"]
    assert store.list_events(task_id=task.id)[0].detail == {"by": "alice"}


def test_transaction_rolls_back(store):
    with pytest.raises(RuntimeError):
        with store.transaction():
            make_task(store)
            raise RuntimeError("boom")
    assert store.list_tasks() == []


def test_fractional_seconds_keep_time_order(store, clock):
    task = make_task(store)
    store.set_task_state(task.id, TaskState.WAITING, wait_until=clock.now())
    clock.advance(seconds=-0.5)
    assert store.wake_due() == []
    clock.advance(seconds=0.25)
    assert store.wake_due() == []
    clock.advance(seconds=0.25)
    assert [t.id for t in store.wake_due()] == [task.id]


def test_deleting_a_schedule_keeps_its_tasks(store):
    sched = store.create_schedule(
        what="w",
        cadence="@daily",
        tz="UTC",
        destination=Destination("cli", "local", None),
        creator="alice",
        profile="cli:alice",
    )
    task = make_task(store, source="schedule", schedule_id=sched.id)
    assert store.delete_schedule(sched.id)
    assert store.get_task(task.id).schedule_id is None
    with pytest.raises(sqlite3.IntegrityError):
        make_task(store, schedule_id=9999)
