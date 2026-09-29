"""The notify.post action: a scheduled run's result, sent to the schedule's destination."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opendot.actions import (
    KIND_NOTIFY,
    ActionContext,
    ActionResult,
    InvalidProposal,
    PreparedAction,
)
from opendot.models import Level, Schedule
from opendot.schedules import result_hash, should_notify

__all__ = ["ACTION_HANDLERS", "MAX_NOTIFY_CHARS", "NotifyHandler", "notify_target"]

MAX_NOTIFY_CHARS = 100_000


def notify_target(schedule: Schedule) -> str:
    destination = schedule.destination
    return f"{destination.channel}:{destination.conversation}:{destination.thread or '-'}"


class NotifyHandler:
    """Posts a scheduled run's result at the destination saved with the schedule.

    Fields: text (required). With the notify rule "changed", nothing is posted
    when the text matches the previous run's text.
    """

    kind = KIND_NOTIFY
    outward = True
    default_level = Level.ALLOW
    floor = Level.ALLOW
    description = (
        "Post the result of a scheduled run at the schedule's saved destination. "
        "Fields: text (required). Only available in scheduled runs."
    )

    def prepare(self, proposal: Mapping[str, Any], ctx: ActionContext) -> PreparedAction:
        if ctx.schedule is None:
            raise InvalidProposal("notify.post: only a scheduled run can notify")
        text = proposal.get("text")
        if not isinstance(text, str) or not text.strip():
            raise InvalidProposal("notify.post: text is required")
        if len(text) > MAX_NOTIFY_CHARS:
            raise InvalidProposal(f"notify.post: text is longer than {MAX_NOTIFY_CHARS} characters")
        text = text.strip()
        return PreparedAction(
            kind=self.kind,
            target=notify_target(ctx.schedule),
            payload={"text": text, "result_hash": result_hash(text)},
            outward=True,
        )

    def execute(self, action: PreparedAction, ctx: ActionContext) -> ActionResult:
        if ctx.schedule is None:
            return ActionResult(ok=False, detail={"error": "the task has no schedule"})
        schedule = ctx.store.get_schedule(ctx.schedule.id)
        if action.target != notify_target(schedule):
            return ActionResult(ok=False, detail={"error": "target is not the saved destination"})
        text = action.payload["text"]
        if not should_notify(ctx.store, schedule, text):
            return ActionResult(ok=True, detail={"skipped": "result unchanged"})
        item = ctx.store.enqueue_outbox(schedule.destination, text, task_id=ctx.task.id)
        return ActionResult(ok=True, detail={"outbox_id": item.id})


ACTION_HANDLERS: list = [NotifyHandler()]
