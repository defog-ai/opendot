"""Small helpers for running host commands.

Host commands (docker build, docker kill, image checks) get a scrubbed
environment: only the few variables a command needs to find its tools, never the
tokens the host process holds. Error messages are redacted before they are raised.
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import IO

from opendot.redact import redact

# Variables a host command may inherit. Everything else is dropped.
SAFE_HOST_VARIABLES = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "DOCKER_HOST", "DOCKER_CONFIG")


class CommandFailed(RuntimeError):
    """A host command exited with a non-zero status."""

    def __init__(self, args: Sequence[str], returncode: int, stderr: str):
        self.args_list = list(args)
        self.returncode = returncode
        self.stderr = stderr
        tail = stderr.strip()[-2000:]
        super().__init__(f"{args[0] if args else 'command'} exited with {returncode}: {tail}")


def scrubbed_env(
    extra: Mapping[str, str] | None = None,
    *,
    keep: Iterable[str] = SAFE_HOST_VARIABLES,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The variables in `keep` that are set in `source` (default os.environ), plus `extra`."""
    source = os.environ if source is None else source
    env = {name: source[name] for name in keep if name in source}
    if "PATH" not in env:
        env["PATH"] = "/usr/local/bin:/usr/bin:/bin"
    env.update(extra or {})
    return env


def run_checked(
    args: Sequence[str],
    *,
    input: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    secrets: Iterable[str] = (),
) -> str:
    """Run a host command with a scrubbed environment and return its stdout.

    Raises CommandFailed on a non-zero exit. The stderr in the error is redacted,
    with `secrets` removed as well as anything that looks like a token.
    """
    completed = subprocess.run(
        list(args),
        input=input,
        env=scrubbed_env(env),
        timeout=timeout,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise CommandFailed(args, completed.returncode, redact(completed.stderr, secrets))
    return completed.stdout


class LineReader:
    """Reads lines from a text stream on a background thread.

    get() waits up to `timeout` seconds and returns the next line, None when no
    line arrived in time, or EOF once the stream has ended. Loops that must also
    check a deadline or a stop request use this instead of blocking on readline().
    """

    EOF = object()

    def __init__(self, stream: IO[str]):
        self._queue: queue.Queue[object] = queue.Queue()
        self._thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        self._thread.start()

    def _pump(self, stream: IO[str]) -> None:
        try:
            for line in stream:
                self._queue.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._queue.put(self.EOF)

    def get(self, timeout: float) -> object:
        try:
            return self._queue.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            return None


class Deadline:
    """A wall-clock limit. seconds=None means no limit."""

    def __init__(self, seconds: float | None):
        self._end = None if seconds is None else time.monotonic() + seconds

    def remaining(self) -> float | None:
        if self._end is None:
            return None
        return self._end - time.monotonic()

    @property
    def passed(self) -> bool:
        remaining = self.remaining()
        return remaining is not None and remaining <= 0
