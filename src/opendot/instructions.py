"""Prompt and schema loading, and the assembly of the work and reflect prompts.

Each step has a policy file (prompts/<STEP>.md) and an output schema
(schemas/<step>.schema.json) shipped inside the package. The host adds the
current time, the action kinds it can run, and the requester's material.

Requester material is always placed inside <untrusted source="..."> blocks, one
JSON object per line, with "<" and ">" escaped so that no text can close the
block early. The review prompt is assembled by opendot.reviewer from the same
policy file.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from functools import cache
from importlib import resources
from typing import TYPE_CHECKING, Any

from opendot.models import Step, to_iso

if TYPE_CHECKING:
    from opendot.actions import ActionRegistry
    from opendot.models import Task

__all__ = [
    "MENTION_MARKER",
    "action_kinds_section",
    "json_line",
    "load_prompt",
    "load_schema",
    "reflect_prompt",
    "untrusted",
    "work_prompt",
]

# Channels replace a mention of the assistant with this text, so the host can
# tell whether a reply in a finished thread asks for new work.
MENTION_MARKER = "@opendot"


@cache
def load_prompt(step: Step | str) -> str:
    """The policy text for a step, from prompts/<STEP>.md."""
    name = f"{Step(step).value.upper()}.md"
    return resources.files("opendot").joinpath("prompts", name).read_text(encoding="utf-8")


@cache
def _schema_text(step: Step) -> str:
    name = f"{step.value}.schema.json"
    return resources.files("opendot").joinpath("schemas", name).read_text(encoding="utf-8")


def load_schema(step: Step | str) -> dict[str, Any]:
    """The output schema for a step, as a fresh dict the caller may change."""
    return json.loads(_schema_text(Step(step)))


def json_line(value: Any) -> str:
    """One JSON object on one line, with "<" and ">" escaped."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return text.replace("<", "\\u003c").replace(">", "\\u003e")


def untrusted(source: str, items: Iterable[Mapping[str, Any]]) -> str:
    """A block of requester material. source is a host-chosen label such as "request"."""
    lines = [json_line(dict(item)) for item in items]
    return "\n".join([f'<untrusted source="{source}">', *lines, "</untrusted>"])


def action_kinds_section(registry: ActionRegistry, kinds: Sequence[str] | None = None) -> str:
    """The action kinds the host can run, with each handler's description."""
    lines = ["## Action kinds the host can run", ""]
    chosen = registry.kinds() if kinds is None else [k for k in registry.kinds() if k in kinds]
    for kind in chosen:
        handler = registry.get(kind)
        text = getattr(handler, "description", "") or inspect.getdoc(type(handler)) or ""
        lines.append(f"### {kind}")
        lines.append("")
        lines.append(text.strip() or "(no description)")
        lines.append("")
    if not chosen:
        lines.append("(none)")
        lines.append("")
    lines.append(
        "Any other kind is refused. The host builds the target (for example the "
        "requester's own thread); you cannot choose it."
    )
    return "\n".join(lines)


def _time_section(now: datetime) -> str:
    return f"## Current time\n\n{to_iso(now)} (UTC)"


def _message_item(message: Any) -> dict[str, Any]:
    return {
        "message_id": message.id,
        "kind": message.kind.value,
        "author": message.author,
        "text": message.text,
    }


def work_prompt(
    task: Task,
    *,
    now: datetime,
    registry: ActionRegistry,
    new_session: bool,
    notes_snapshot: str = "",
    messages: Sequence[Any] = (),
    action_results: Sequence[Mapping[str, Any]] = (),
) -> str:
    """The prompt for one work step.

    new_session: True when the backend starts a fresh session, so the request and
    the notes are included; a resumed session already has them.
    messages: stored messages not yet shown to the model (answers, steering notes).
    action_results: what happened to the actions proposed in earlier steps.
    """
    parts = [load_prompt(Step.WORK).strip(), "", _time_section(now), ""]
    parts += [action_kinds_section(registry), ""]
    about = [f"Task {task.id}."]
    if task.schedule_id is not None:
        about.append(
            "This is a scheduled run. Your reply is posted to the schedule's saved "
            "destination; nobody can answer questions in this run."
        )
    if task.parent_task_id is not None:
        about.append(f"It follows up on task {task.parent_task_id} in the same thread.")
    parts += ["## Task", "", " ".join(about), ""]
    if new_session or task.parent_task_id is not None:
        if new_session and notes_snapshot:
            parts += [notes_snapshot.strip(), ""]
        parts += [
            "## The request (user-provided data, one JSON object per line)",
            "",
            untrusted("request", [{"author": task.requester, "text": task.text}]),
            "",
        ]
    if messages:
        parts += [
            "## New messages from the thread (user-provided data, one JSON object per line)",
            "",
            untrusted("thread", [_message_item(m) for m in messages]),
            "",
        ]
    if action_results:
        parts += [
            "## What happened to your earlier proposals (written by the host)",
            "",
            "<action-results>",
            *(json_line(dict(r)) for r in action_results),
            "</action-results>",
            "",
        ]
    return "\n".join(parts).rstrip() + "\n"


def reflect_prompt(
    task: Task,
    *,
    now: datetime,
    notes_snapshot: str,
    feedback: Sequence[Mapping[str, Any]],
    final_reply: str = "",
) -> str:
    """The prompt for the reflect step.

    feedback: the requester's answers and corrections, each with message_id,
    kind and text.
    """
    parts = [load_prompt(Step.REFLECT).strip(), "", _time_section(now), ""]
    parts += [
        "## Saved notes",
        "",
        notes_snapshot.strip() or "There are no saved notes for this requester yet.",
        "",
        "## The request (user-provided data, one JSON object per line)",
        "",
        untrusted("request", [{"author": task.requester, "text": task.text}]),
        "",
        "## The requester's feedback (user-provided data, one JSON object per line)",
        "",
        untrusted("feedback", feedback),
        "",
    ]
    if final_reply:
        parts += [
            "## The final reply the assistant proposed (written by the model)",
            "",
            untrusted("reply", [{"text": final_reply}]),
            "",
        ]
    return "\n".join(parts).rstrip() + "\n"
