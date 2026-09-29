"""Approvals: a person's permission for one exact action, tied to one task.

An approval row holds the task id, the action kind, the target and the sha256
digest of the exact payload the host will send (opendot.actions.payload_digest).

- single_use covers one action whose digest equals the stored digest. It is used
  up by that action.
- until_task_end covers every action of that kind and target for the rest of the
  task, whatever the payload. Because it does not look at the content, it never
  covers an outward action (one that posts or sends something); those always need
  a single-use approval of the exact text.

Approvals belong to one task. A follow-up task, a retry that makes a new task or a
scheduled run starts with none. A task that ends expires its approvals (the store
does this when the task reaches a terminal state).

Only two people can decide an approval: the task's requester, on the channel the
task came from, and the operator on the local command line. Decisions from
anyone else are ignored and logged.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from opendot.actions import PreparedAction, payload_digest
from opendot.models import (
    ActionRecord,
    Approval,
    ApprovalMode,
    ApprovalStatus,
    Task,
)
from opendot.rules import is_operator

if TYPE_CHECKING:
    from opendot.config import Config
    from opendot.store import Store

__all__ = [
    "ApprovalMismatch",
    "NotAllowedToDecide",
    "action_matches_approval",
    "can_decide",
    "consume_approval",
    "covering_approval",
    "covers",
    "decide",
    "request_approval",
]


class NotAllowedToDecide(PermissionError):
    """The person is neither the task's requester nor the local operator."""


class ApprovalMismatch(Exception):
    """The action about to run is not the one the person approved."""


def covers(approval: Approval, action: PreparedAction, task_id: int) -> bool:
    """True when a granted approval covers this exact action on this task."""
    if approval.status is not ApprovalStatus.GRANTED:
        return False
    if approval.task_id != task_id:
        return False
    if approval.kind != action.kind or approval.target != action.target:
        return False
    if approval.mode is ApprovalMode.SINGLE_USE:
        return approval.payload_digest is not None and approval.payload_digest == action.digest
    # until_task_end: kind and target only, so never for outward content.
    return not action.outward


def request_approval(
    store: Store,
    task: Task,
    action: PreparedAction,
    *,
    mode: ApprovalMode = ApprovalMode.SINGLE_USE,
    action_id: int | None = None,
    expires_at: datetime | None = None,
) -> Approval:
    """Store a pending approval for an action. Its digest is always recorded.

    Raises ValueError when until_task_end is asked for an outward action or for a
    task that has already ended.
    """
    mode = ApprovalMode(mode)
    if mode is ApprovalMode.UNTIL_TASK_END and action.outward:
        raise ValueError(
            f"{action.kind} sends content outside the store; only a single-use approval "
            "of the exact payload can cover it"
        )
    if task.state.is_terminal:
        raise ValueError(f"task {task.id} has ended; it cannot ask for approvals")
    approval = store.create_approval(
        task.id,
        kind=action.kind,
        target=action.target,
        mode=mode,
        payload_digest=action.digest,
        action_id=action_id,
        expires_at=expires_at,
    )
    store.log_event(
        "approval.requested",
        {
            "approval_id": approval.id,
            "kind": action.kind,
            "target": action.target,
            "mode": mode.value,
            "action_id": action_id,
        },
        task_id=task.id,
    )
    return approval


def can_decide(config: Config, task: Task, *, decided_by: str, channel: str) -> bool:
    """The requester on the task's own channel, or the operator on the command line."""
    if channel == task.channel and decided_by == task.requester:
        return True
    return is_operator(config, channel=channel, actor=decided_by)


def decide(
    store: Store,
    config: Config,
    approval_id: int,
    *,
    granted: bool,
    decided_by: str,
    channel: str,
) -> Approval:
    """Grant or deny a pending approval.

    Raises NotAllowedToDecide (after logging it) when the person may not decide,
    and StoreError when the approval is no longer pending.
    """
    approval = store.get_approval(approval_id)
    task = store.get_task(approval.task_id)
    if not can_decide(config, task, decided_by=decided_by, channel=channel):
        store.log_event(
            "approval.decision_ignored",
            {"approval_id": approval_id, "by": decided_by, "channel": channel},
            task_id=task.id,
        )
        raise NotAllowedToDecide(
            f"approval {approval_id} can be decided only by the task's requester "
            "or the local operator"
        )
    decided = store.decide_approval(approval_id, granted=granted, decided_by=decided_by)
    store.log_event(
        "approval.granted" if granted else "approval.denied",
        {"approval_id": approval_id, "by": decided_by, "channel": channel},
        task_id=task.id,
    )
    return decided


def covering_approval(store: Store, task: Task, action: PreparedAction) -> Approval | None:
    """The oldest granted approval of this task that covers the exact action, if any.

    Single-use approvals are preferred over until-task-end ones, so a broad
    approval is not spent where a narrow one exists.
    """
    if task.state.is_terminal:
        return None
    candidates = [
        a
        for a in store.granted_approvals(task.id, action.kind, action.target)
        if covers(a, action, task.id)
    ]
    candidates.sort(key=lambda a: (a.mode is not ApprovalMode.SINGLE_USE, a.id))
    return candidates[0] if candidates else None


def consume_approval(store: Store, task: Task, action: PreparedAction) -> Approval | None:
    """Find the covering approval and mark it used. None when nothing covers the action."""
    approval = covering_approval(store, task, action)
    if approval is None:
        return None
    used = store.mark_approval_used(approval.id)
    store.log_event(
        "approval.used",
        {"approval_id": approval.id, "kind": action.kind, "target": action.target},
        task_id=task.id,
    )
    return used


def action_matches_approval(approval: Approval, record: ActionRecord, *, outward: bool) -> None:
    """Check a stored action against the approval granted for it, right before it runs.

    outward is the handler's flag for the action kind. Raises ApprovalMismatch when
    the approval is not granted, belongs to another task, was given for a different
    kind, target or payload, cannot cover outward content, or when the stored
    payload no longer matches the stored digest.
    """
    if approval.status is not ApprovalStatus.GRANTED:
        raise ApprovalMismatch(f"approval {approval.id} is {approval.status.value}")
    if approval.task_id != record.task_id:
        raise ApprovalMismatch(f"approval {approval.id} belongs to task {approval.task_id}")
    if approval.kind != record.kind or approval.target != record.target:
        raise ApprovalMismatch(f"approval {approval.id} was given for another kind or target")
    if payload_digest(record.kind, record.target, record.payload) != record.payload_digest:
        raise ApprovalMismatch(f"action {record.id} payload does not match its digest")
    if approval.mode is ApprovalMode.SINGLE_USE:
        if approval.payload_digest != record.payload_digest:
            raise ApprovalMismatch(f"approval {approval.id} was given for a different payload")
    elif outward:
        raise ApprovalMismatch(f"approval {approval.id} cannot cover outward content")
