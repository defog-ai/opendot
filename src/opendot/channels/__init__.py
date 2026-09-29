"""Channels: where requests come from and where replies go.

A channel only moves text. It never decides what a message means for a task;
the host classifies messages and stores them (store.record_message).

Registry: each channel kind maps to "module:ClassName". The class must provide
    @classmethod
    def from_config(cls, config: Config) -> Channel
Modules load only when enabled_channels or create_channel asks for them.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from opendot.models import Destination, IncomingMessage

if TYPE_CHECKING:
    from opendot.config import Config

__all__ = [
    "Channel",
    "ChannelError",
    "create_channel",
    "enabled_channels",
    "known_channel_kinds",
    "register_channel",
]


class ChannelError(Exception):
    """Delivery or fetch failed. The outbox records the message and retries later."""


class Channel(Protocol):
    """Where messages come from and where replies go.

    Two optional members are read with getattr or hasattr, so a channel may leave
    them out:
    - store: a Store | None attribute; when it is None the orchestrator sets it,
      so the channel can keep its own cursors in the database;
    - acknowledge() -> None, called after the orchestrator has stored every
      message from the last fetch_new(). A channel that keeps cursors should
      move them only here, so a crash between fetching and storing loses nothing.
    """

    name: str  # the channel kind, e.g. "cli" or "slack"; equals Destination.channel

    def fetch_new(self) -> list[IncomingMessage]:
        """Return messages that arrived since the last call, oldest first.

        Only messages from allowed authors are returned. Each message's external_id
        is unique within this channel kind (Slack: "<conversation>:<ts>"), because
        the store keeps UNIQUE(channel, external_id).
        """
        ...

    def post(self, destination: Destination, text: str) -> str:
        """Post text; return the new message's external_id. Raises ChannelError."""
        ...

    def react(self, destination: Destination, external_id: str, reaction: str) -> None:
        """Add a reaction (a short name such as "eyes") to a message."""
        ...

    def upload(self, destination: Destination, filename: str, content: str, title: str = "") -> str:
        """Upload a text file (long reports) and return its external_id."""
        ...


ChannelFactory = Callable[["Config"], Channel]

_CHANNELS: dict[str, str | ChannelFactory] = {
    "cli": "opendot.channels.cli:CliChannel",
    "slack": "opendot.channels.slack:SlackChannel",
}


def known_channel_kinds() -> set[str]:
    return set(_CHANNELS)


def register_channel(kind: str, factory: str | ChannelFactory) -> None:
    """Add or replace a channel kind. factory is "module:Class" or factory(config) -> Channel."""
    _CHANNELS[kind] = factory


def create_channel(kind: str, config: Config) -> Channel:
    try:
        entry = _CHANNELS[kind]
    except KeyError:
        raise ChannelError(f"unknown channel kind {kind!r}") from None
    if isinstance(entry, str):
        module_name, _, attr = entry.partition(":")
        cls = getattr(importlib.import_module(module_name), attr)
        return cls.from_config(config)
    return entry(config)


def enabled_channels(config: Config) -> list[Channel]:
    """Channels switched on in the config: cli when channels.cli.enabled, slack when
    channels.slack.enabled."""
    kinds = []
    if config.cli.enabled:
        kinds.append("cli")
    if config.slack.enabled:
        kinds.append("slack")
    return [create_channel(kind, config) for kind in kinds]
