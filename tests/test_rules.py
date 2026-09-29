from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from opendot.actions import (
    ActionContext,
    ActionRegistry,
    ActionResult,
    PreparedAction,
    UnknownAction,
)
from opendot.config import RuleConfig
from opendot.models import Level, RuleSource, RuleStatus
from opendot.notes import NoteWriteHandler
from opendot.rules import (
    FIXED_FLOORS,
    NotOperator,
    RuleEngine,
    add_operator_rule,
    approve_operator_rule,
    fixed_floor,
    remove_operator_rule,
    sync_config_rules,
    validate_rule_kind,
)
from opendot.schedules import ScheduleCreateHandler


@dataclass
class StubHandler:
    kind: str
    default_level: Level = Level.ALLOW
    floor: Level = Level.ALLOW
    outward: bool = False

    def prepare(self, proposal, ctx):
        return PreparedAction(self.kind, str(proposal.get("target", "t")), {}, self.outward)

    def execute(self, action, ctx):
        return ActionResult(ok=True)


def action(kind: str, target: str = "t") -> PreparedAction:
    return PreparedAction(kind=kind, target=target, payload={}, outward=False)


@pytest.fixture
def registry() -> ActionRegistry:
    return ActionRegistry(
        [
            StubHandler("reply.post"),
            StubHandler("reply.react"),
            StubHandler("mail.send", default_level=Level.ASK, floor=Level.ASK, outward=True),
            StubHandler("payment.send", default_level=Level.HAND_OFF, floor=Level.HAND_OFF),
            ScheduleCreateHandler(),
        ]
    )


def engine(store, registry) -> RuleEngine:
    return RuleEngine.from_store(registry, store)


def test_default_levels_without_rules(store, registry):
    rules = engine(store, registry)
    assert rules.decide(action("reply.post")).level is Level.ALLOW
    assert rules.decide(action("mail.send")).level is Level.ASK
    assert rules.decide(action("payment.send")).level is Level.HAND_OFF
    assert rules.decide(action("schedule.create")).level is Level.ASK


def test_each_of_the_four_levels_can_come_from_a_rule(store, registry):
    for level in Level:
        store.add_rule("reply.post", level, source=RuleSource.OPERATOR, created_by="operator")
        decision = engine(store, registry).decide(action("reply.post"))
        assert decision.level is level
        for rule in store.list_rules():
            store.remove_rule(rule.id)


def test_rule_cannot_relax_the_floor(store, registry):
    store.add_rule("mail.send", Level.ALLOW, source=RuleSource.OPERATOR, created_by="operator")
    store.add_rule("payment.*", Level.ALLOW, source=RuleSource.OPERATOR, created_by="operator")
    store.add_rule("*", Level.ALLOW, source=RuleSource.OPERATOR, created_by="operator")
    rules = engine(store, registry)
    mail = rules.decide(action("mail.send"))
    assert mail.level is Level.ASK
    assert mail.raised_by_floor
    assert rules.decide(action("payment.send")).level is Level.HAND_OFF
    assert rules.decide(action("schedule.create")).level is Level.ASK


def test_fixed_floors_apply_even_when_the_handler_floor_is_loose(store):
    loose = ActionRegistry([StubHandler("credentials.rotate"), StubHandler("rule.create")])
    rules = RuleEngine(loose, [])
    assert rules.decide(action("credentials.rotate")).level is Level.HAND_OFF
    assert rules.decide(action("rule.create")).level is Level.ASK
    assert fixed_floor("reply.post") is Level.ALLOW
    assert "access.*" in FIXED_FLOORS


def test_rule_can_make_a_level_stricter(store, registry):
    store.add_rule("reply.post", Level.ASK, source=RuleSource.OPERATOR, created_by="operator")
    decision = engine(store, registry).decide(action("reply.post"))
    assert decision.level is Level.ASK
    assert decision.rule_level is Level.ASK
    assert not decision.raised_by_floor


def test_most_specific_rule_wins(store, registry):
    add = store.add_rule
    add("*", Level.HAND_OFF, source=RuleSource.OPERATOR, created_by="operator")
    add("reply.*", Level.ASK, source=RuleSource.OPERATOR, created_by="operator")
    exact = add("reply.post", Level.PREAPPROVED, source=RuleSource.OPERATOR, created_by="operator")
    targeted = add(
        "reply.post", Level.ALLOW, target="home", source=RuleSource.OPERATOR, created_by="operator"
    )
    rules = engine(store, registry)
    assert rules.decide(action("reply.post", "home")).rule_id == targeted.id
    assert rules.decide(action("reply.post", "elsewhere")).rule_id == exact.id
    assert rules.decide(action("reply.react")).level is Level.ASK  # prefix beats "*"
    assert rules.decide(action("mail.send")).level is Level.HAND_OFF  # only "*" matches


def test_equally_specific_rules_pick_the_stricter(store, registry):
    store.add_rule("reply.post", Level.ALLOW, source=RuleSource.OPERATOR, created_by="operator")
    store.add_rule("reply.post", Level.ASK, source=RuleSource.CONFIG, created_by="config")
    assert engine(store, registry).decide(action("reply.post")).level is Level.ASK


def test_prefix_does_not_match_a_longer_word(store, registry):
    store.add_rule("reply.*", Level.ASK, source=RuleSource.OPERATOR, created_by="operator")
    reg = ActionRegistry([StubHandler("replyall.post")])
    assert RuleEngine.from_store(reg, store).decide(action("replyall.post")).level is Level.ALLOW


def test_pending_rules_never_match(store, registry):
    pending = store.add_rule("reply.post", Level.ASK, source=RuleSource.AGENT, created_by="agent")
    assert pending.status is RuleStatus.PENDING
    assert engine(store, registry).decide(action("reply.post")).level is Level.ALLOW


def test_unknown_kind_is_refused(store, registry):
    store.add_rule("*", Level.ALLOW, source=RuleSource.OPERATOR, created_by="operator")
    with pytest.raises(UnknownAction):
        engine(store, registry).decide(action("money.move"))


def test_per_action_default_hook_is_used(store, config):
    task = store.create_task(requester="alice", text="hi", channel="cli", conversation="local")
    reg = ActionRegistry([NoteWriteHandler()])
    rules = RuleEngine(reg, [])
    ctx = ActionContext(task=task, store=store, config=config, channels={})
    prepared = reg.prepare({"kind": "note.write", "text": "likes tea"}, ctx)
    assert rules.decide(prepared, ctx).level is Level.ASK


def test_operator_on_cli_can_add_approve_and_remove(store, config):
    rule = add_operator_rule(store, config, "reply.*", "ask", channel="cli", actor=config.cli.user)
    assert rule.status is RuleStatus.ACTIVE
    assert rule.source is RuleSource.OPERATOR
    drafted = store.add_rule("reply.post", Level.ASK, source=RuleSource.AGENT, created_by="agent")
    approved = approve_operator_rule(
        store, config, drafted.id, channel="cli", actor=config.cli.user
    )
    assert approved.status is RuleStatus.ACTIVE
    assert remove_operator_rule(store, config, rule.id, channel="cli", actor=config.cli.user)


@pytest.mark.parametrize(
    ("channel", "actor"),
    [("slack", "operator"), ("slack", "alice"), ("cli", "alice"), ("fake", "operator")],
)
def test_nobody_else_can_change_rules(store, config, channel, actor):
    drafted = store.add_rule("reply.post", Level.ALLOW, source=RuleSource.AGENT, created_by="a")
    with pytest.raises(NotOperator):
        add_operator_rule(store, config, "*", Level.ALLOW, channel=channel, actor=actor)
    with pytest.raises(NotOperator):
        approve_operator_rule(store, config, drafted.id, channel=channel, actor=actor)
    with pytest.raises(NotOperator):
        remove_operator_rule(store, config, drafted.id, channel=channel, actor=actor)
    assert store.get_rule(drafted.id).status is RuleStatus.PENDING
    assert len(store.list_rules()) == 1
    assert len(store.list_events(kind="rule.change_refused")) == 3


@pytest.mark.parametrize("kind", ["*", "reply.post", "reply.*", "a.b.*"])
def test_valid_rule_kinds(kind):
    assert validate_rule_kind(kind) == kind


@pytest.mark.parametrize("kind", ["", " reply", "re*ply", "reply*", ".*", "a b", "*.post"])
def test_invalid_rule_kinds(kind):
    with pytest.raises(ValueError):
        validate_rule_kind(kind)


def test_add_operator_rule_rejects_a_bad_level(store, config):
    with pytest.raises(ValueError):
        add_operator_rule(store, config, "reply.post", "refuse", channel="cli", actor="operator")


def test_sync_config_rules_replaces_config_rows(store, config, registry):
    cfg = replace(config, rules=[RuleConfig("reply.post", "*", Level.ASK)])
    synced = sync_config_rules(store, cfg)
    assert [(r.kind, r.level) for r in synced] == [("reply.post", Level.ASK)]
    assert engine(store, registry).decide(action("reply.post")).level is Level.ASK
    empty = replace(config, rules=[])
    assert sync_config_rules(store, empty) == []
