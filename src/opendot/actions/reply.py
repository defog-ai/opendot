"""The reply.post action: a message in the requester's own thread."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opendot.actions import (
    KIND_REPLY,
    ActionContext,
    ActionResult,
    InvalidProposal,
    PreparedAction,
)
from opendot.models import Task

__all__ = ["ACTION_HANDLERS", "MAX_REPLY_CHARS", "ReplyHandler", "reply_target"]

MAX_REPLY_CHARS = 100_000


def reply_target(task: Task) -> str:
    return f"{task.channel}:{task.conversation}:{task.thread or '-'}"


class ReplyHandler:
    """Posts text in the thread where the requester asked.

    Fields: text (required). The destination is always the task's own thread.
    Scheduled runs use notify.post instead.
    """

    kind = KIND_REPLY
    outward = True
    description = (
        "Post a message in the requester's own thread. Fields: text (required, "
        "plain text or simple Markdown). Not available in scheduled runs."
    )

    def prepare(self, proposal: Mapping[str, Any], ctx: ActionContext) -> PreparedAction:
        if ctx.task.schedule_id is not None:
            raise InvalidProposal("reply.post: a scheduled run posts with notify.post")
        text = proposal.get("text")
        if not isinstance(text, str) or not text.strip():
            raise InvalidProposal("reply.post: text is required")
        if len(text) > MAX_REPLY_CHARS:
            raise InvalidProposal(f"reply.post: text is longer than {MAX_REPLY_CHARS} characters")
        return PreparedAction(
            kind=self.kind,
            target=reply_target(ctx.task),
            payload={"text": text.strip()},
            outward=True,
        )

    def execute(self, action: PreparedAction, ctx: ActionContext) -> ActionResult:
        if action.target != reply_target(ctx.task):
            return ActionResult(ok=False, detail={"error": "target is not the task's thread"})
        item = ctx.store.enqueue_outbox(
            ctx.task.destination, action.payload["text"], task_id=ctx.task.id
        )
        return ActionResult(ok=True, detail={"outbox_id": item.id})


ACTION_HANDLERS: list = [ReplyHandler()]
