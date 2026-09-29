"""The rule engine: decides the level of each prepared action.

Every action the agent proposes is first built by its handler in the action
registry (opendot.actions). The registry refuses unknown kinds. This module then
picks one of four levels for the prepared action:

    allow        run after the reviewer approves it
    preapproved  run only when a stored approval covers it
    ask          ask the requester and park the task
    hand_off     never run; give the prepared material to the requester

How the level is chosen:

1. The most specific active rule that matches the kind and target wins. An exact
   kind is more specific than a prefix ("reply.*"), a longer prefix is more
   specific than a shorter one, and any prefix is more specific than "*". Inside
   the same kind match, an exact target beats "*". When two rules are equally
   specific, the stricter one wins.
2. With no matching rule, the handler's default level applies. A handler may
   also define default_level_for(action, ctx) to give a per-action default; the
   note handler uses it to allow notes taken from the requester's own message.
3. The result is raised to the fixed floor: the handler's floor, and the fixed
   floors in FIXED_FLOORS. No rule can make an action less strict than its floor.

Rules are global, so only the operator on the local command line may add or
approve them (add_operator_rule, approve_operator_rule). Rules listed in the
config file are copied into the store by sync_config_rules.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from opendot.actions import ActionContext, ActionHandler, ActionRegistry, PreparedAction
from opendot.models import Level, Rule, RuleSource, RuleStatus

if TYPE_CHECKING:
    from opendot.config import Config
    from opendot.store import Store

__all__ = [
    "FIXED_FLOORS",
    "OPERATOR_CHANNEL",
    "LevelDecision",
    "NotOperator",
    "RuleEngine",
    "add_operator_rule",
    "approve_operator_rule",
    "fixed_floor",
    "is_operator",
    "remove_operator_rule",
    "rule_matches",
    "rule_specificity",
    "sync_config_rules",
    "validate_rule_kind",
]

OPERATOR_CHANNEL = "cli"

# Floors that hold for any handler whose kind matches, in addition to the
# handler's own floor. Changing credentials, moving money and granting access are
# always handed back to the person. Saving a schedule or changing a rule always
# needs a person's confirmation. v0.1 registers no handler for most of these
# kinds; the entries make sure a handler added later cannot start out looser.
FIXED_FLOORS: dict[str, Level] = {
    "credentials.*": Level.HAND_OFF,
    "payment.*": Level.HAND_OFF,
    "purchase.*": Level.HAND_OFF,
    "access.*": Level.HAND_OFF,
    "rule.*": Level.ASK,
    "schedule.*": Level.ASK,
}


class NotOperator(PermissionError):
    """Only the operator on the local command line may change rules."""


def validate_rule_kind(kind: str) -> str:
    """Accept an exact action kind, a prefix ending in ".*", or "*". Returns the kind."""
    if not isinstance(kind, str) or not kind.strip() or kind != kind.strip():
        raise ValueError(f"rule kind must be non-empty text without spaces: {kind!r}")
    if kind == "*":
        return kind
    if "*" in kind[:-1] or (kind.endswith("*") and not kind.endswith(".*")):
        raise ValueError(f"rule kind may only end in '.*' or be '*': {kind!r}")
    if kind.endswith(".*") and len(kind) == 2:
        raise ValueError("rule kind '.*' has no prefix; use '*' to match every kind")
    if any(ch.isspace() for ch in kind):
        raise ValueError(f"rule kind must not contain spaces: {kind!r}")
    return kind


def _kind_rank(pattern: str, kind: str) -> int | None:
    """How specifically pattern matches kind: None when it does not match.

    "*" ranks 0, a prefix ranks by its length (at least 1), an exact kind ranks
    above every prefix.
    """
    if pattern == "*":
        return 0
    if pattern.endswith(".*"):
        prefix = pattern[:-1]  # keeps the dot, so "reply.*" does not match "replyx"
        return len(prefix) if kind.startswith(prefix) else None
    return 10_000 if pattern == kind else None


def rule_matches(rule: Rule, kind: str, target: str) -> bool:
    if _kind_rank(rule.kind, kind) is None:
        return False
    return rule.target == "*" or rule.target == target


def rule_specificity(rule: Rule, kind: str, target: str) -> tuple[int, int]:
    """Sort key for a matching rule; larger is more specific."""
    kind_rank = _kind_rank(rule.kind, kind)
    if kind_rank is None:
        raise ValueError(f"rule {rule.id} does not match kind {kind!r}")
    return kind_rank, 0 if rule.target == "*" else 1


def fixed_floor(kind: str) -> Level:
    """The strictest entry of FIXED_FLOORS that matches kind, or allow when none does."""
    levels = [
        level for pattern, level in FIXED_FLOORS.items() if _kind_rank(pattern, kind) is not None
    ]
    return Level.strictest(Level.ALLOW, *levels)


@dataclass(frozen=True)
class LevelDecision:
    level: Level  # the level the host must apply
    rule_id: int | None  # the rule that matched, if any
    rule_level: Level | None  # that rule's own level, before the floor
    default_level: Level  # the handler's default for this action
    floor: Level  # the least strict level allowed for this kind

    @property
    def raised_by_floor(self) -> bool:
        """True when a rule asked for a looser level than the floor permits."""
        return self.rule_level is not None and self.rule_level.rank < self.floor.rank


class RuleEngine:
    """Chooses levels from the active rules in a list (normally store.list_rules())."""

    def __init__(self, registry: ActionRegistry, rules: Iterable[Rule]):
        self.registry = registry
        self.rules = [r for r in rules if r.status is RuleStatus.ACTIVE]

    @classmethod
    def from_store(cls, registry: ActionRegistry, store: Store) -> RuleEngine:
        return cls(registry, store.list_rules(RuleStatus.ACTIVE))

    def matching_rule(self, kind: str, target: str) -> Rule | None:
        matches = [r for r in self.rules if rule_matches(r, kind, target)]
        if not matches:
            return None
        return max(matches, key=lambda r: (rule_specificity(r, kind, target), r.level.rank))

    def floor_for(self, handler: ActionHandler) -> Level:
        return Level.strictest(Level(handler.floor), fixed_floor(handler.kind))

    def default_for(
        self, handler: ActionHandler, action: PreparedAction, ctx: ActionContext | None
    ) -> Level:
        per_action = getattr(handler, "default_level_for", None)
        if per_action is not None and ctx is not None:
            return Level(per_action(action, ctx))
        return Level(handler.default_level)

    def decide(self, action: PreparedAction, ctx: ActionContext | None = None) -> LevelDecision:
        """The level for a prepared action. Raises UnknownAction for an unregistered kind."""
        handler = self.registry.get(action.kind)
        floor = self.floor_for(handler)
        default = self.default_for(handler, action, ctx)
        rule = self.matching_rule(action.kind, action.target)
        chosen = rule.level if rule is not None else default
        return LevelDecision(
            level=Level.strictest(chosen, floor),
            rule_id=rule.id if rule is not None else None,
            rule_level=rule.level if rule is not None else None,
            default_level=default,
            floor=floor,
        )


# ---------------------------------------------------------------------------
# Rule changes: operator only
# ---------------------------------------------------------------------------


def is_operator(config: Config, *, channel: str, actor: str) -> bool:
    """True only for the configured command-line user on the local command line."""
    return channel == OPERATOR_CHANNEL and actor == config.cli.user


def _require_operator(store: Store, config: Config, channel: str, actor: str, what: str) -> None:
    if not is_operator(config, channel=channel, actor=actor):
        store.log_event("rule.change_refused", {"what": what, "channel": channel, "actor": actor})
        raise NotOperator(f"{what}: only the operator on the local command line may do this")


def add_operator_rule(
    store: Store,
    config: Config,
    kind: str,
    level: Level | str,
    *,
    target: str = "*",
    channel: str,
    actor: str,
) -> Rule:
    """Add an active rule. Refused (NotOperator) unless the caller is the local operator."""
    _require_operator(store, config, channel, actor, "add rule")
    validate_rule_kind(kind)
    if not target:
        raise ValueError("rule target must be an exact value or '*'")
    rule = store.add_rule(
        kind, Level(level), target=target, source=RuleSource.OPERATOR, created_by=actor
    )
    store.log_event(
        "rule.added",
        {"rule_id": rule.id, "kind": kind, "target": target, "level": rule.level.value},
    )
    return rule


def approve_operator_rule(
    store: Store, config: Config, rule_id: int, *, channel: str, actor: str
) -> Rule:
    """Activate a pending rule. Refused (NotOperator) unless the caller is the local operator."""
    _require_operator(store, config, channel, actor, "approve rule")
    rule = store.approve_rule(rule_id)
    store.log_event("rule.approved", {"rule_id": rule.id, "by": actor})
    return rule


def remove_operator_rule(
    store: Store, config: Config, rule_id: int, *, channel: str, actor: str
) -> bool:
    """Delete a rule. Refused (NotOperator) unless the caller is the local operator."""
    _require_operator(store, config, channel, actor, "remove rule")
    removed = store.remove_rule(rule_id)
    if removed:
        store.log_event("rule.removed", {"rule_id": rule_id, "by": actor})
    return removed


def sync_config_rules(store: Store, config: Config) -> list[Rule]:
    """Replace the stored config rules with the [[rules]] entries of the config file."""
    for rule in config.rules:
        validate_rule_kind(rule.kind)
    return store.sync_config_rules((r.kind, r.target, Level(r.level)) for r in config.rules)
