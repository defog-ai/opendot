"""v0.2 action registry: feature modules and connector action kinds."""

import sys
import types

import pytest

import opendot.actions as actions
from opendot.actions import build_registry, default_registry, mcp_kind, parse_mcp_kind
from opendot.models import Level


class _Handler:
    kind = "github.push_branch"
    outward = True
    default_level = Level.ASK
    floor = Level.ASK

    def prepare(self, proposal, ctx):
        raise NotImplementedError

    def execute(self, action, ctx):
        raise NotImplementedError


def test_build_registry_without_feature_modules_matches_default(config):
    assert build_registry(config).kinds() == default_registry().kinds()


def test_build_registry_adds_feature_handlers(config, monkeypatch):
    module = types.ModuleType("opendot_test_feature")
    seen = []

    def action_handlers(cfg):
        seen.append(cfg)
        return [_Handler()]

    module.action_handlers = action_handlers
    monkeypatch.setitem(sys.modules, "opendot_test_feature", module)
    monkeypatch.setattr(
        actions, "FEATURE_ACTION_MODULES", ("opendot_test_feature", "opendot_missing_feature")
    )
    registry = build_registry(config)
    assert "github.push_branch" in registry
    assert seen == [config]


def test_build_registry_raises_on_broken_module(config, monkeypatch, tmp_path):
    (tmp_path / "opendot_broken_feature.py").write_text("import opendot_no_such_dependency\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(actions, "FEATURE_ACTION_MODULES", ("opendot_broken_feature",))
    with pytest.raises(ModuleNotFoundError):
        build_registry(config)


def test_mcp_kind_round_trip():
    kind = mcp_kind("factiq", "send_feedback")
    assert kind == "mcp.factiq.send_feedback"
    assert parse_mcp_kind(kind) == ("factiq", "send_feedback")


@pytest.mark.parametrize("kind", ["reply.post", "mcp.a", "mcp.a.b.c", "mcp.a b.c", "mcp.a__x.c"])
def test_parse_mcp_kind_refuses_other_shapes(kind):
    assert parse_mcp_kind(kind) is None


@pytest.mark.parametrize("server, tool", [("a.b", "c"), ("a", "b.c"), ("", "c"), ("a__b", "c")])
def test_mcp_kind_refuses_bad_names(server, tool):
    with pytest.raises(ValueError):
        mcp_kind(server, tool)
