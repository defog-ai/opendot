"""The command-line channel: tasks typed by the local operator, answers printed.

The command line has no inbox to poll. `opendot` commands build a message with
message() and hand it to the orchestrator directly. Posts are printed and also
appended to logs/cli.log, so answers produced by a background worker can be read
later.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from opendot.models import Destination, IncomingMessage

if TYPE_CHECKING:
    from opendot.config import Config

__all__ = ["LOCAL", "CliChannel"]

LOCAL = "local"


class CliChannel:
    name = "cli"

    def __init__(self, user: str, log_path: Path | None = None, out: TextIO | None = None):
        self.user = user
        self.log_path = log_path
        self.out = out

    @classmethod
    def from_config(cls, config: Config) -> CliChannel:
        return cls(config.cli.user, config.logs_dir / "cli.log")

    def message(self, text: str, thread: str | None = None) -> IncomingMessage:
        """A message from the operator. With thread, it is a reply in that task's thread."""
        message_id = uuid.uuid4().hex
        return IncomingMessage(
            channel=self.name,
            external_id=f"{LOCAL}:{message_id}",
            conversation=LOCAL,
            thread=thread or message_id,
            author=self.user,
            text=text,
            is_reply=thread is not None,
            received_at=datetime.now(UTC),
        )

    def fetch_new(self) -> list[IncomingMessage]:
        return []

    def _write(self, destination: Destination, text: str) -> None:
        header = f"[{destination.thread or '-'}]"
        out = self.out or sys.stdout
        print(f"{header} {text}", file=out, flush=True)
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            with self.log_path.open("a", encoding="utf-8") as log:
                log.write(f"{stamp} {header} {text}\n")

    def post(self, destination: Destination, text: str) -> str:
        self._write(destination, text)
        return f"{LOCAL}:{uuid.uuid4().hex}"

    def react(self, destination: Destination, external_id: str, reaction: str) -> None:
        return None

    def upload(self, destination: Destination, filename: str, content: str, title: str = "") -> str:
        folder = (self.log_path.parent if self.log_path else Path.cwd()) / "reports"
        folder.mkdir(parents=True, exist_ok=True)
        safe_name = Path(filename).name or "report.txt"
        path = folder / f"{uuid.uuid4().hex[:8]}-{safe_name}"
        path.write_text(content, encoding="utf-8")
        label = title or safe_name
        self._write(destination, f"{label}: {len(content)} characters, saved to {path}")
        return f"file:{path.name}"
