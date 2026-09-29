"""Host-side action registry.

The agent's output only proposes actions. Each proposal is an object with a "kind"
field plus fields for that kind. The host looks the kind up in this registry:

- an unknown kind is refused (UnknownAction); the host has no code to run it;
- the handler, not the model, builds the target (for example the requester's own
  thread) and the payload that will be sent;
- the handler declares whether the action is outward and its fixed floor level,
  which user rules cannot relax.

Built-in handlers live in the modules listed in BUILTIN_ACTION_MODULES. Each of
those modules exposes a module-level list ACTION_HANDLERS.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from opendot.models import Level, Schedule, Task

if TYPE_CHECKING:
    from opendot.channels import Channel
    from opendot.config import Config
    from opendot.store import Store

__all__ = [
    "BUILTIN_ACTION_MODULES",
    "KIND_NOTE_WRITE",
    "KIND_NOTIFY",
    "KIND_REPLY",
    "KIND_SCHEDULE_CREATE",
    "ActionContext",
    "ActionError",
    "ActionHandler",
    "ActionRegistry",
    "ActionResult",
    "InvalidProposal",
    "PreparedAction",
    "UnknownAction",
    "default_registry",
    "payload_digest",
]

KIND_REPLY = "reply.post"  # post in the requester's own thread
KIND_NOTIFY = "notify.post"  # post a schedule result to the schedule's stored destination
KIND_NOTE_WRITE = "note.write"  # add, edit or delete a note in the requester's profile
KIND_SCHEDULE_CREATE = "schedule.create"  # save a new schedule

BUILTIN_ACTION_MODULES = (
    "opendot.actions.reply",
    "opendot.actions.notify",
    "opendot.notes",
    "opendot.schedules",
)


class ActionError(Exception):
    pass


class UnknownAction(ActionError):
    """The proposal names a kind the host has no handler for. Always refused."""


class InvalidProposal(ActionError):
    """The handler cannot build an action from the proposal's fields."""


def payload_digest(kind: str, target: str, payload: Mapping[str, Any]) -> str:
    """sha256 hex digest of the canonical JSON of kind, target and payload."""
    canonical = json.dumps(
        {"kind": kind, "target": target, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PreparedAction:
    """An action exactly as the host would run it. Approvals bind to its digest."""

    kind: str
    target: str  # host-built, e.g. "slack:C0123:1700000000.000100" or "note:<profile>"
    payload: dict[str, Any]  # exactly what execute() will send or write
    outward: bool  # True when it posts, sends or changes anything outside the store

    @property
    def digest(self) -> str:
        return payload_digest(self.kind, self.target, self.payload)


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)
    external_id: str | None = None  # e.g. the posted message id


@dataclass
class ActionContext:
    """What a handler may use. Handlers read the task; they never read the model's target."""

    task: Task
    store: Store
    config: Config
    channels: Mapping[str, Channel]
    schedule: Schedule | None = None  # set when the task was started by a schedule


class ActionHandler(Protocol):
    """One kind of action the host can run.

    Two optional members are read with getattr, so a handler may leave them out:
    - description: str, the text the agent sees for this kind in its instructions
      (the class docstring is used when it is missing);
    - default_level_for(action: PreparedAction, ctx: ActionContext) -> Level, a
      per-action default used instead of default_level when no rule matches. The
      floor still applies to what it returns.
    """

    kind: str
    outward: bool
    default_level: Level  # used when no user rule matches
    floor: Level  # the least strict level any rule may give this kind

    def prepare(self, proposal: Mapping[str, Any], ctx: ActionContext) -> PreparedAction:
        """Build the exact action from the proposal. Raise InvalidProposal on bad fields."""
        ...

    def execute(self, action: PreparedAction, ctx: ActionContext) -> ActionResult:
        """Carry out a prepared action. Called only after rules, review and approval."""
        ...


class ActionRegistry:
    def __init__(self, handlers: Iterable[ActionHandler] = ()):
        self._handlers: dict[str, ActionHandler] = {}
        for handler in handlers:
            self.register(handler)

    def register(self, handler: ActionHandler) -> None:
        if handler.kind in self._handlers:
            raise ValueError(f"action kind {handler.kind!r} is already registered")
        if Level(handler.default_level).rank < Level(handler.floor).rank:
            raise ValueError(f"{handler.kind}: default_level is less strict than floor")
        self._handlers[handler.kind] = handler

    def kinds(self) -> list[str]:
        return sorted(self._handlers)

    def __contains__(self, kind: object) -> bool:
        return kind in self._handlers

    def get(self, kind: str) -> ActionHandler:
        try:
            return self._handlers[kind]
        except (KeyError, TypeError):
            raise UnknownAction(f"no action handler for kind {kind!r}") from None

    def prepare(self, proposal: Mapping[str, Any], ctx: ActionContext) -> PreparedAction:
        """Look up proposal["kind"] and let its handler build the action.

        Raises UnknownAction for a missing or unregistered kind, InvalidProposal when
        the handler rejects the fields or returns an action of a different kind.
        """
        if not isinstance(proposal, Mapping):
            raise InvalidProposal("an action proposal must be an object")
        handler = self.get(proposal.get("kind"))  # type: ignore[arg-type]
        prepared = handler.prepare(proposal, ctx)
        if prepared.kind != handler.kind or prepared.outward != handler.outward:
            raise InvalidProposal(
                f"handler {handler.kind!r} returned kind {prepared.kind!r}; the host sets the kind"
            )
        return prepared

    def execute(self, action: PreparedAction, ctx: ActionContext) -> ActionResult:
        return self.get(action.kind).execute(action, ctx)


def default_registry() -> ActionRegistry:
    """A registry holding every handler from BUILTIN_ACTION_MODULES."""
    registry = ActionRegistry()
    for module_name in BUILTIN_ACTION_MODULES:
        module = importlib.import_module(module_name)
        for handler in module.ACTION_HANDLERS:
            registry.register(handler)
    return registry
