from __future__ import annotations

from datetime import timedelta

import pytest

from opendot.actions import PreparedAction
from opendot.approvals import (
    ApprovalMismatch,
    NotAllowedToDecide,
    action_matches_approval,
    can_decide,
    consume_approval,
    covering_approval,
    covers,
    decide,
    request_approval,
)
from opendot.models import ApprovalMode, ApprovalStatus, Level, TaskState
from opendot.store import StoreError


def make_task(store, requester="alice", channel="slack", **kw):
    return store.create_task(
        requester=requester, text="do it", channel=channel, conversation="C1", thread="1", **kw
    )


def reply(text="hello", target="slack:C1:1") -> PreparedAction:
    return PreparedAction("reply.post", target, {"text": text}, outward=True)


def note(text="likes tea") -> PreparedAction:
    return PreparedAction("note.write", "note:slack:alice", {"text": text}, outward=False)


def granted(store, config, task, action, mode=ApprovalMode.SINGLE_USE, **kw):
    approval = request_approval(store, task, action, mode=mode, **kw)
    return decide(
        store, config, approval.id, granted=True, decided_by=task.requester, channel=task.channel
    )


def test_single_use_covers_exact_payload_once(store, config):
    task = make_task(store)
    approval = granted(store, config, task, reply())
    assert approval.status is ApprovalStatus.GRANTED
    assert approval.payload_digest == reply().digest
    used = consume_approval(store, task, reply())
    assert used is not None and used.status is ApprovalStatus.USED
    assert consume_approval(store, task, reply()) is None


def test_payload_change_after_approval_is_not_covered(store, config):
    task = make_task(store)
    granted(store, config, task, reply("hello"))
    assert covering_approval(store, task, reply("hello, and here is my token")) is None
    assert covering_approval(store, task, reply("hello", target="slack:C2:1")) is None
    assert covering_approval(store, task, reply("hello")) is not None


def test_until_task_end_covers_kind_and_target_for_non_outward(store, config):
    task = make_task(store)
    granted(store, config, task, note("a"), mode=ApprovalMode.UNTIL_TASK_END)
    for text in ("a", "b", "c"):
        used = consume_approval(store, task, note(text))
        assert used is not None and used.status is ApprovalStatus.GRANTED
        assert used.used_at is not None


def test_until_task_end_is_refused_for_outward_actions(store, config):
    task = make_task(store)
    with pytest.raises(ValueError):
        request_approval(store, task, reply(), mode=ApprovalMode.UNTIL_TASK_END)


def test_until_task_end_row_never_covers_outward_content(store, config):
    """Even a row made outside request_approval (for example by hand) does not match."""
    task = make_task(store)
    row = store.create_approval(
        task.id,
        kind="reply.post",
        target="slack:C1:1",
        mode=ApprovalMode.UNTIL_TASK_END,
        payload_digest=None,
    )
    store.decide_approval(row.id, granted=True, decided_by="alice")
    assert covering_approval(store, task, reply("anything")) is None
    assert not covers(store.get_approval(row.id), reply("anything"), task.id)


def test_single_use_preferred_over_until_task_end(store, config):
    task = make_task(store)
    broad = granted(store, config, task, note("x"), mode=ApprovalMode.UNTIL_TASK_END)
    narrow = granted(store, config, task, note("x"))
    assert consume_approval(store, task, note("x")).id == narrow.id
    assert consume_approval(store, task, note("x")).id == broad.id


def test_approval_is_not_carried_to_a_follow_up_task(store, config):
    first = make_task(store)
    granted(store, config, first, reply())
    granted(store, config, first, note(), mode=ApprovalMode.UNTIL_TASK_END)
    follow_up = make_task(store, parent_task_id=first.id)
    assert covering_approval(store, follow_up, reply()) is None
    assert covering_approval(store, follow_up, note()) is None
    assert covering_approval(store, first, reply()) is not None


def test_expired_approval_is_refused(store, config, clock):
    task = make_task(store)
    granted(store, config, task, reply(), expires_at=clock.now() + timedelta(minutes=10))
    clock.advance(minutes=11)
    assert covering_approval(store, task, reply()) is None
    assert store.list_approvals(task.id)[0].status is ApprovalStatus.EXPIRED


def test_pending_approval_expires_before_a_decision(store, config, clock):
    task = make_task(store)
    approval = request_approval(store, task, reply(), expires_at=clock.now() + timedelta(minutes=1))
    clock.advance(minutes=2)
    with pytest.raises(StoreError):
        decide(store, config, approval.id, granted=True, decided_by="alice", channel="slack")


def test_ended_task_has_no_approvals(store, config):
    task = make_task(store)
    granted(store, config, task, note(), mode=ApprovalMode.UNTIL_TASK_END)
    ended = store.set_task_state(task.id, TaskState.DONE)
    assert covering_approval(store, ended, note()) is None
    with pytest.raises(ValueError):
        request_approval(store, ended, note())


def test_denied_approval_covers_nothing(store, config):
    task = make_task(store)
    approval = request_approval(store, task, reply())
    denied = decide(store, config, approval.id, granted=False, decided_by="alice", channel="slack")
    assert denied.status is ApprovalStatus.DENIED
    assert covering_approval(store, task, reply()) is None
    assert store.list_events(task_id=task.id, kind="approval.denied")


@pytest.mark.parametrize(
    ("decided_by", "channel"),
    [
        ("mallory", "slack"),  # another allowed user in the thread
        ("alice", "cli"),  # the requester's name, but on another channel
        ("operator", "slack"),  # the operator name, but not on the command line
    ],
)
def test_decision_by_someone_else_is_ignored_and_logged(store, config, decided_by, channel):
    task = make_task(store)
    approval = request_approval(store, task, reply())
    with pytest.raises(NotAllowedToDecide):
        decide(store, config, approval.id, granted=True, decided_by=decided_by, channel=channel)
    assert store.get_approval(approval.id).status is ApprovalStatus.PENDING
    events = store.list_events(task_id=task.id, kind="approval.decision_ignored")
    assert events[0].detail["by"] == decided_by


def test_local_operator_can_decide(store, config):
    task = make_task(store)
    approval = request_approval(store, task, reply())
    assert can_decide(config, task, decided_by=config.cli.user, channel="cli")
    decided = decide(
        store, config, approval.id, granted=True, decided_by=config.cli.user, channel="cli"
    )
    assert decided.decided_by == config.cli.user


def test_decision_twice_is_refused(store, config):
    task = make_task(store)
    approval = granted(store, config, task, reply())
    with pytest.raises(StoreError):
        decide(store, config, approval.id, granted=False, decided_by="alice", channel="slack")


def record_for(store, task, action):
    return store.record_action(
        task.id,
        kind=action.kind,
        target=action.target,
        payload=action.payload,
        payload_digest=action.digest,
        level=Level.ASK,
    )


def test_stored_action_is_checked_against_its_approval(store, config):
    task = make_task(store)
    record = record_for(store, task, reply("hello"))
    approval = granted(store, config, task, reply("hello"), action_id=record.id)
    action_matches_approval(approval, record, outward=True)

    other = record_for(store, task, reply("changed"))
    with pytest.raises(ApprovalMismatch):
        action_matches_approval(approval, other, outward=True)


def test_stored_payload_that_no_longer_matches_its_digest_is_refused(store, config):
    task = make_task(store)
    record = record_for(store, task, reply("hello"))
    approval = granted(store, config, task, reply("hello"), action_id=record.id)
    tampered = record.__class__(**{**record.__dict__, "payload": {"text": "something else"}})
    with pytest.raises(ApprovalMismatch):
        action_matches_approval(approval, tampered, outward=True)


def test_stored_action_of_another_task_is_refused(store, config):
    task = make_task(store)
    other_task = make_task(store)
    approval = granted(store, config, task, reply())
    with pytest.raises(ApprovalMismatch):
        action_matches_approval(approval, record_for(store, other_task, reply()), outward=True)


def test_until_task_end_does_not_pass_outward_stored_action(store, config):
    task = make_task(store)
    approval = granted(store, config, task, note(), mode=ApprovalMode.UNTIL_TASK_END)
    record = record_for(store, task, note())
    action_matches_approval(approval, record, outward=False)
    with pytest.raises(ApprovalMismatch):
        action_matches_approval(approval, record, outward=True)
