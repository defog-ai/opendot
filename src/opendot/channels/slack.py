"""The Slack channel: polls the Slack Web API and posts with the bot token.

No public URL, Events API or Socket Mode is needed. Each fetch_new() call:

1. reads new top-level messages in every configured conversation
   (conversations.history, starting after a stored cursor);
2. reads new replies in the threads of recent tasks (conversations.replies),
   at most MAX_THREADS_PER_FETCH threads per call.

Only messages from users in channels.slack.allowed_users are returned; an empty
list allows nobody. Bot messages, the bot's own messages and system messages
are skipped. A mention of the bot is rewritten to MENTION_MARKER.

Cursors move only when the orchestrator calls acknowledge() after it has stored
the fetched messages, so a crash in between re-reads them (the store ignores a
message it already has).

Rate limits: Slack answers HTTP 429 with a Retry-After header. The channel then
stops the current poll, keeps the cursors it has not reached, and skips polling
until the wait is over. Slack gives apps outside the Slack Marketplace a low
limit on conversations.history and conversations.replies, so keep the number of
conversations small and the poll interval at a minute or more.

Long reports are uploaded as a file with files.getUploadURLExternal, a POST of
the content to the returned URL, and files.completeUploadExternal.
"""

from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx

from opendot.channels import ChannelError
from opendot.instructions import MENTION_MARKER
from opendot.models import TERMINAL_STATES, Destination, IncomingMessage

if TYPE_CHECKING:
    from opendot.config import Config
    from opendot.store import Store

__all__ = [
    "ALLOWED_ATTACHMENT_TYPES",
    "API_BASE",
    "MAX_THREADS_PER_FETCH",
    "SlackApiError",
    "SlackChannel",
    "SlackRateLimited",
]

API_BASE = "https://slack.com/api/"
MAX_THREADS_PER_FETCH = 20
THREAD_FOLLOW_DAYS = 1
PAGE_LIMIT = 100
MAX_PAGES = 10

# Attachment metadata is passed on only for these types. Files are never downloaded.
ALLOWED_ATTACHMENT_TYPES = frozenset(
    {
        "text/plain",
        "text/csv",
        "text/markdown",
        "application/json",
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/gif",
    }
)

_USER_SUBTYPES = {"file_share", "thread_broadcast"}


class SlackApiError(ChannelError):
    def __init__(self, method: str, error: str):
        super().__init__(f"slack {method} failed: {error}")
        self.method = method
        self.error = error


class SlackRateLimited(ChannelError):
    def __init__(self, method: str, retry_after: float):
        super().__init__(f"slack {method} is rate limited; retry after {retry_after:.0f}s")
        self.method = method
        self.retry_after = retry_after


def _ts_key(ts: str) -> tuple[int, int]:
    """Order Slack timestamps ("1700000000.000100") without float rounding."""
    seconds, _, fraction = ts.partition(".")
    return int(seconds), int((fraction or "0").ljust(6, "0")[:6])


def _ts_after(ts: str, cursor: str | None) -> bool:
    return cursor is None or _ts_key(ts) > _ts_key(cursor)


def _format_ts(moment: datetime) -> str:
    return f"{moment.timestamp():.6f}"


class SlackChannel:
    name = "slack"

    def __init__(
        self,
        config: Config,
        client: httpx.Client | None = None,
        store: Store | None = None,
        token: str | None = None,
    ):
        self.config = config
        self.slack = config.slack
        self.token = token if token is not None else self.slack.bot_token()
        if not self.token:
            raise ChannelError(
                "the Slack bot token is missing; run `opendot login slack` "
                f"or set the variable {self.slack.bot_token_env}"
            )
        self.client = client or httpx.Client(base_url=API_BASE, timeout=30.0)
        self.store = store
        self.bot_user_id: str | None = None
        self._pending_cursors: dict[str, str] = {}
        self._paused_until = 0.0  # time.monotonic() value

    @classmethod
    def from_config(cls, config: Config) -> SlackChannel:
        return cls(config)

    # -- HTTP --------------------------------------------------------------

    def _call(self, method: str, data: Mapping[str, Any]) -> dict[str, Any]:
        fields = {k: v for k, v in data.items() if v is not None}
        try:
            response = self.client.post(
                method, data=fields, headers={"Authorization": f"Bearer {self.token}"}
            )
        except httpx.HTTPError as exc:
            raise ChannelError(f"slack {method} request failed: {exc}") from exc
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "60") or 60)
            self._paused_until = time.monotonic() + retry_after
            raise SlackRateLimited(method, retry_after)
        if response.status_code >= 400:
            raise SlackApiError(method, f"HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise SlackApiError(method, "the response was not JSON") from None
        if not body.get("ok"):
            raise SlackApiError(method, str(body.get("error", "unknown error")))
        return body

    def _bot_id(self) -> str:
        if self.bot_user_id is None:
            self.bot_user_id = str(self._call("auth.test", {})["user_id"])
        return self.bot_user_id

    # -- reading -----------------------------------------------------------

    def _require_store(self) -> Store:
        if self.store is None:
            raise ChannelError("the Slack channel needs the store to keep its cursors")
        return self.store

    def _cursor(self, key: str, default: str) -> str:
        if key in self._pending_cursors:
            return self._pending_cursors[key]
        stored = self._require_store().get_cursor(self.name, key)
        return stored if stored is not None else default

    def _clean_text(self, text: str) -> str:
        bot = self._bot_id()
        text = re.sub(rf"<@{re.escape(bot)}(\|[^>]*)?>", MENTION_MARKER, text)
        return html.unescape(text)

    def _attachments(self, raw: Mapping[str, Any]) -> list[dict[str, Any]]:
        files = []
        for item in raw.get("files") or []:
            mimetype = str(item.get("mimetype", ""))
            if mimetype not in ALLOWED_ATTACHMENT_TYPES:
                continue
            files.append(
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "mimetype": mimetype,
                    "size": item.get("size"),
                }
            )
        return files

    def _to_message(self, conversation: str, raw: Mapping[str, Any]) -> IncomingMessage | None:
        if raw.get("subtype") and raw.get("subtype") not in _USER_SUBTYPES:
            return None
        if raw.get("bot_id") or raw.get("bot_profile"):
            return None
        user = raw.get("user")
        ts = raw.get("ts")
        if not user or not ts or user == self._bot_id():
            return None
        if not self.slack.is_allowed(str(user)):
            return None
        thread = str(raw.get("thread_ts") or ts)
        return IncomingMessage(
            channel=self.name,
            external_id=f"{conversation}:{ts}",
            conversation=conversation,
            thread=thread,
            author=str(user),
            text=self._clean_text(str(raw.get("text", ""))),
            is_reply=thread != ts,
            received_at=datetime.fromtimestamp(_ts_key(str(ts))[0], UTC),
            attachments=self._attachments(raw),
        )

    def _read_pages(
        self, method: str, params: dict[str, Any], cursor: str
    ) -> list[Mapping[str, Any]]:
        messages: list[Mapping[str, Any]] = []
        page_cursor: str | None = None
        for _ in range(MAX_PAGES):
            body = self._call(
                method, {**params, "oldest": cursor, "limit": PAGE_LIMIT, "cursor": page_cursor}
            )
            messages.extend(body.get("messages") or [])
            page_cursor = (body.get("response_metadata") or {}).get("next_cursor") or None
            if not body.get("has_more") or not page_cursor:
                break
        return messages

    def _followed_threads(self) -> list[tuple[str, str]]:
        """(conversation, thread) of recent Slack tasks, newest first."""
        store = self._require_store()
        since = store.clock.now() - timedelta(days=THREAD_FOLLOW_DAYS)
        allowed = set(self.slack.channels)
        threads: list[tuple[str, str]] = []
        for task in store.list_tasks(limit=200):
            if task.channel != self.name or not task.thread or task.conversation not in allowed:
                continue
            if task.state in TERMINAL_STATES and (task.finished_at or task.updated_at) < since:
                continue
            key = (task.conversation, task.thread)
            if key not in threads:
                threads.append(key)
            if len(threads) >= MAX_THREADS_PER_FETCH:
                break
        return threads

    def fetch_new(self) -> list[IncomingMessage]:
        """New messages from allowed users, oldest first. Stops early on a rate limit."""
        if time.monotonic() < self._paused_until:
            return []
        store = self._require_store()
        now_ts = _format_ts(store.clock.now())
        found: dict[str, IncomingMessage] = {}
        try:
            for conversation in self.slack.channels:
                cursor = self._cursor(conversation, now_ts)
                newest = cursor
                for raw in self._read_pages(
                    "conversations.history", {"channel": conversation}, cursor
                ):
                    ts = str(raw.get("ts", ""))
                    if not ts or not _ts_after(ts, cursor):
                        continue
                    if _ts_key(ts) > _ts_key(newest):
                        newest = ts
                    message = self._to_message(conversation, raw)
                    if message is not None:
                        found[message.external_id] = message
                self._pending_cursors[conversation] = newest
            for conversation, thread in self._followed_threads():
                key = f"{conversation}/{thread}"
                cursor = self._cursor(key, thread)
                newest = cursor
                replies = self._read_pages(
                    "conversations.replies", {"channel": conversation, "ts": thread}, cursor
                )
                for raw in replies:
                    ts = str(raw.get("ts", ""))
                    if not ts or ts == thread or not _ts_after(ts, cursor):
                        continue
                    if _ts_key(ts) > _ts_key(newest):
                        newest = ts
                    message = self._to_message(conversation, raw)
                    if message is not None:
                        found[message.external_id] = message
                self._pending_cursors[key] = newest
        except SlackRateLimited:
            pass  # keep what was read; the rest is read after the wait
        messages = list(found.values())
        messages.sort(key=lambda m: _ts_key(m.external_id.split(":", 1)[1]))
        return messages

    def acknowledge(self) -> None:
        """Store the cursors of the last fetch. Call after the messages are recorded."""
        store = self._require_store()
        for key, value in self._pending_cursors.items():
            store.set_cursor(self.name, key, value)
        self._pending_cursors.clear()

    # -- writing -----------------------------------------------------------

    def post(self, destination: Destination, text: str) -> str:
        body = self._call(
            "chat.postMessage",
            {
                "channel": destination.conversation,
                "thread_ts": destination.thread,
                "text": text,
                "unfurl_links": "false",
                "unfurl_media": "false",
            },
        )
        return f"{destination.conversation}:{body['ts']}"

    def react(self, destination: Destination, external_id: str, reaction: str) -> None:
        conversation, _, ts = external_id.partition(":")
        try:
            self._call(
                "reactions.add",
                {
                    "channel": conversation or destination.conversation,
                    "timestamp": ts,
                    "name": reaction,
                },
            )
        except SlackApiError as exc:
            if exc.error != "already_reacted":
                raise

    def upload(self, destination: Destination, filename: str, content: str, title: str = "") -> str:
        data = content.encode("utf-8")
        ticket = self._call(
            "files.getUploadURLExternal", {"filename": filename, "length": str(len(data))}
        )
        try:
            response = self.client.post(
                ticket["upload_url"],
                content=data,
                headers={"Content-Type": "application/octet-stream"},
            )
        except httpx.HTTPError as exc:
            raise ChannelError(f"slack file upload failed: {exc}") from exc
        if response.status_code >= 400:
            raise SlackApiError("file upload", f"HTTP {response.status_code}")
        done = self._call(
            "files.completeUploadExternal",
            {
                "files": json.dumps([{"id": ticket["file_id"], "title": title or filename}]),
                "channel_id": destination.conversation,
                "thread_ts": destination.thread,
            },
        )
        files = done.get("files") or [{"id": ticket["file_id"]}]
        return f"file:{files[0].get('id', ticket['file_id'])}"
