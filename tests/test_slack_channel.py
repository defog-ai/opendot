"""The Slack channel against a mocked Web API (httpx.MockTransport)."""

from __future__ import annotations

import json

import httpx
import pytest

from opendot.channels import ChannelError
from opendot.channels.slack import SlackApiError, SlackChannel
from opendot.config import Config
from opendot.models import Destination

NOW_TS = "1767614400.000000"  # the conftest clock start, 2026-01-05 12:00 UTC
BASE = "https://mock.invalid/api/"


@pytest.fixture
def slack_config(state_root) -> Config:
    return Config.from_dict(
        {
            "core": {"state_root": str(state_root)},
            "backend": {"worker": {"kind": "fake"}},
            "channels": {
                "slack": {"enabled": True, "channels": ["C1"], "allowed_users": ["U_ALICE"]}
            },
        },
        env={},
    )


@pytest.fixture
def slack(slack_config, store, http_mock) -> SlackChannel:
    http_mock.add("POST", "/api/auth.test", json={"ok": True, "user_id": "U_BOT"})
    return SlackChannel(slack_config, client=http_mock.client(BASE), store=store, token="t-1")


def history(*messages: dict) -> dict:
    return {"ok": True, "messages": list(messages), "has_more": False}


def test_missing_token_is_an_error(slack_config):
    with pytest.raises(ChannelError):
        SlackChannel(slack_config, token="")


def test_fetch_keeps_allowed_users_and_rewrites_mentions(slack, http_mock, store):
    http_mock.add(
        "POST",
        "/api/conversations.history",
        json=history(
            {"ts": "1767614405.000200", "user": "U_EVE", "text": "not allowed"},
            {"ts": "1767614404.000100", "user": "U_ALICE", "text": "<@U_BOT|bot> hi &amp; bye"},
            {"ts": "1767614403.000000", "user": "U_ALICE", "subtype": "channel_join"},
            {"ts": "1767614402.000000", "bot_id": "B1", "text": "from a bot"},
            {"ts": "1767614401.000000", "user": "U_BOT", "text": "my own post"},
            {"ts": "1767614399.000000", "user": "U_ALICE", "text": "before the cursor"},
        ),
    )

    [message] = slack.fetch_new()

    assert message.text == "@opendot hi & bye"
    assert message.author == "U_ALICE"
    assert message.external_id == "C1:1767614404.000100"
    assert message.thread == "1767614404.000100"
    assert message.is_reply is False
    [body] = http_mock.json_bodies("/api/conversations.history")
    assert body["channel"] == "C1"
    assert body["oldest"] == NOW_TS
    assert http_mock.requests[0].headers["authorization"] == "Bearer t-1"

    assert store.get_cursor("slack", "C1") is None
    slack.acknowledge()
    assert store.get_cursor("slack", "C1") == "1767614405.000200"


def test_empty_allowlist_allows_nobody(state_root, store, http_mock):
    config = Config.from_dict(
        {
            "core": {"state_root": str(state_root)},
            "channels": {"slack": {"enabled": True, "channels": ["C1"]}},
        },
        env={},
    )
    http_mock.add("POST", "/api/auth.test", json={"ok": True, "user_id": "U_BOT"})
    http_mock.add(
        "POST",
        "/api/conversations.history",
        json=history({"ts": "1767614404.000100", "user": "U_ALICE", "text": "hi"}),
    )
    channel = SlackChannel(config, client=http_mock.client(BASE), store=store, token="t-1")

    assert channel.fetch_new() == []


def test_replies_in_task_threads_are_read_after_the_thread_cursor(slack, http_mock, store):
    root = "1767614401.000100"
    store.create_task(
        requester="U_ALICE", text="job", channel="slack", conversation="C1", thread=root
    )
    store.create_task(
        requester="U_ALICE", text="other", channel="slack", conversation="C9", thread="5.0"
    )
    http_mock.add("POST", "/api/conversations.history", json=history())
    http_mock.add(
        "POST",
        "/api/conversations.replies",
        json=history(
            {"ts": root, "thread_ts": root, "user": "U_ALICE", "text": "job"},
            {"ts": "1767614409.000000", "thread_ts": root, "user": "U_ALICE", "text": "stop"},
        ),
    )

    [reply] = slack.fetch_new()

    assert reply.is_reply is True
    assert reply.thread == root
    assert reply.text == "stop"
    [body] = http_mock.json_bodies("/api/conversations.replies")
    assert body == {"channel": "C1", "ts": root, "oldest": root, "limit": "100"}
    slack.acknowledge()
    assert store.get_cursor("slack", f"C1/{root}") == "1767614409.000000"


def test_rate_limit_stops_the_poll_and_pauses_later_polls(slack, http_mock, store):
    http_mock.add("POST", "/api/conversations.history", status=429, headers={"Retry-After": "30"})

    assert slack.fetch_new() == []
    history_calls = len(http_mock.json_bodies("/api/conversations.history"))
    assert slack.fetch_new() == []
    assert len(http_mock.json_bodies("/api/conversations.history")) == history_calls
    slack.acknowledge()
    assert store.get_cursor("slack", "C1") is None


def test_post_replies_in_the_thread(slack, http_mock):
    http_mock.add("POST", "/api/chat.postMessage", json={"ok": True, "ts": "1767614500.000100"})

    external_id = slack.post(Destination("slack", "C1", "1767614401.000100"), "hello")

    assert external_id == "C1:1767614500.000100"
    [body] = http_mock.json_bodies("/api/chat.postMessage")
    assert body["thread_ts"] == "1767614401.000100"
    assert body["text"] == "hello"
    assert body["unfurl_links"] == "false"


def test_api_error_raises(slack, http_mock):
    http_mock.add("POST", "/api/chat.postMessage", json={"ok": False, "error": "not_in_channel"})

    with pytest.raises(SlackApiError) as caught:
        slack.post(Destination("slack", "C1"), "hello")
    assert caught.value.error == "not_in_channel"


def test_react_ignores_already_reacted(slack, http_mock):
    http_mock.add("POST", "/api/reactions.add", json={"ok": False, "error": "already_reacted"})
    http_mock.add("POST", "/api/reactions.add", json={"ok": False, "error": "invalid_name"})
    where = Destination("slack", "C1", "1.0")

    slack.react(where, "C1:1767614401.000100", "eyes")
    with pytest.raises(SlackApiError):
        slack.react(where, "C1:1767614401.000100", "eyes")
    body = http_mock.json_bodies("/api/reactions.add")[0]
    assert body == {"channel": "C1", "timestamp": "1767614401.000100", "name": "eyes"}


def test_upload_uses_the_external_upload_flow(slack, http_mock):
    http_mock.add(
        "POST",
        "/api/files.getUploadURLExternal",
        json={"ok": True, "upload_url": "https://files.invalid/up/1", "file_id": "F1"},
    )
    http_mock.add("POST", "/up/1", handler=lambda request: httpx.Response(200, text="OK"))
    http_mock.add(
        "POST",
        "/api/files.completeUploadExternal",
        json={"ok": True, "files": [{"id": "F1"}]},
    )

    result = slack.upload(Destination("slack", "C1", "1.0"), "report.md", "long text", "Report")

    assert result == "file:F1"
    [ticket] = http_mock.json_bodies("/api/files.getUploadURLExternal")
    assert ticket == {"filename": "report.md", "length": str(len(b"long text"))}
    upload = next(r for r in http_mock.requests if r.url.path == "/up/1")
    assert upload.content == b"long text"
    [done] = http_mock.json_bodies("/api/files.completeUploadExternal")
    assert json.loads(done["files"]) == [{"id": "F1", "title": "Report"}]
    assert done["channel_id"] == "C1"
    assert done["thread_ts"] == "1.0"
