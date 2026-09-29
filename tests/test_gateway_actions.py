"""Connector write tools as host actions: mcp.<server>.<tool>."""

from __future__ import annotations

import pytest

from opendot.actions import ActionContext, InvalidProposal, build_registry
from opendot.config import Config
from opendot.gateway.actions import McpWriteHandler, action_handlers
from opendot.models import GatewayCallStatus, GatewayMode, Level
from opendot.store import open_store
from test_gateway_server import failing_factory, fake_factory
from test_gateway_step import connector_config


@pytest.fixture
def setup(state_root):
    config = connector_config(state_root)
    store = open_store(config)
    task = store.create_task(text="t", requester="alice", channel="cli", conversation="local")
    ctx = ActionContext(task=task, store=store, config=config, channels={})
    yield config, store, ctx
    store.close()


def test_registry_gets_one_handler_per_write_tool(setup):
    config, _, _ = setup
    registry = build_registry(config)
    assert "mcp.fake.submit" in registry
    assert "mcp.fake.search" not in registry
    [handler] = action_handlers(config)
    assert handler.outward is True
    assert handler.floor is Level.ASK and handler.default_level is Level.ASK


def test_factiq_feedback_is_an_action_only_when_turned_on(state_root):
    base = {
        "core": {"state_root": str(state_root)},
        "backend": {"worker": {"kind": "fake"}, "reviewer": {"kind": "fake"}},
    }
    off = Config.from_dict({**base, "factiq": {"enabled": True}}, env={})
    assert action_handlers(off) == []
    on = Config.from_dict({**base, "factiq": {"enabled": True, "feedback": True}}, env={})
    assert [h.kind for h in action_handlers(on)] == ["mcp.factiq.send_feedback"]


def test_prepare_uses_the_fields_as_arguments(setup):
    config, _, ctx = setup
    [handler] = action_handlers(config)
    action = handler.prepare({"kind": "mcp.fake.submit", "title": "x", "body": "y"}, ctx)
    assert action.kind == "mcp.fake.submit"
    assert action.target == "mcp:fake:submit"
    assert action.outward is True
    assert action.payload == {
        "server": "fake",
        "tool": "submit",
        "destination": "https://mcp.example.com/mcp",
        "arguments": {"title": "x", "body": "y"},
    }
    wrapped = handler.prepare({"kind": "mcp.fake.submit", "arguments": {"title": "x"}}, ctx)
    assert wrapped.payload["arguments"] == {"title": "x"}


def test_prepare_refuses_large_or_odd_arguments(setup):
    config, _, ctx = setup
    [handler] = action_handlers(config)
    with pytest.raises(InvalidProposal, match="64 KiB"):
        handler.prepare({"kind": "mcp.fake.submit", "title": "x" * 70000}, ctx)
    with pytest.raises(InvalidProposal, match="plain JSON"):
        handler.prepare({"kind": "mcp.fake.submit", "title": {1, 2}}, ctx)


def test_execute_calls_the_write_tool_and_logs_it(setup):
    config, store, ctx = setup
    factory = fake_factory()
    [handler] = action_handlers(config, upstream_factory=factory)
    action = handler.prepare({"kind": "mcp.fake.submit", "title": "hello"}, ctx)
    result = handler.execute(action, ctx)
    assert result.ok is True
    assert result.detail["result"] == "submitted hello"
    assert factory.log.calls == [("submit", {"title": "hello", "body": ""})]
    [row] = store.list_gateway_calls(task_id=ctx.task.id)
    assert (row.tool, row.mode, row.status) == ("submit", GatewayMode.WRITE, GatewayCallStatus.OK)
    assert row.step_token == "action"
    assert row.arguments == {"title": "hello"}


def test_execute_refuses_a_payload_for_another_destination(setup):
    config, store, ctx = setup
    factory = fake_factory()
    [handler] = action_handlers(config, upstream_factory=factory)
    action = handler.prepare({"kind": "mcp.fake.submit", "title": "hello"}, ctx)
    action.payload["destination"] = "https://other.example.com/mcp"
    result = handler.execute(action, ctx)
    assert result.ok is False
    assert factory.log.calls == []
    assert store.list_gateway_calls(task_id=ctx.task.id) == []


def test_execute_reports_an_unreachable_server(setup):
    config, store, ctx = setup
    [server] = config.connectors()
    handler = McpWriteHandler(server, "submit", upstream_factory=failing_factory("refused"))
    action = handler.prepare({"kind": "mcp.fake.submit", "title": "hello"}, ctx)
    result = handler.execute(action, ctx)
    assert result.ok is False
    assert "refused" in result.detail["error"]
    [row] = store.list_gateway_calls(task_id=ctx.task.id)
    assert row.status == GatewayCallStatus.ERROR


def test_execute_without_the_api_key_fails_closed(state_root):
    config = Config.from_dict(
        {
            "core": {"state_root": str(state_root)},
            "backend": {"worker": {"kind": "fake"}, "reviewer": {"kind": "fake"}},
            "factiq": {"enabled": True, "feedback": True, "api_key_env": "OPENDOT_TEST_NO_KEY"},
        },
        env={},
    )
    store = open_store(config)
    task = store.create_task(text="t", requester="alice", channel="cli", conversation="local")
    ctx = ActionContext(task=task, store=store, config=config, channels={})
    [handler] = action_handlers(config)
    action = handler.prepare({"kind": "mcp.factiq.send_feedback", "message": "hi"}, ctx)
    result = handler.execute(action, ctx)
    assert result.ok is False
    assert "OPENDOT_TEST_NO_KEY" in result.detail["error"]
    store.close()
