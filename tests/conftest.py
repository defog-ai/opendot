"""Shared test fakes. Nothing here needs Docker, Slack, a model login or the network.

Fixtures: clock, state_root, config, store, fake_backend, fake_reviewer,
fake_docker, fake_channel, http_mock. The classes can also be imported directly:
    from conftest import FakeChannel, FakeDocker, FakeProcess, FrozenClock, HttpMock
"""

from __future__ import annotations

import io
import json as jsonlib
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from opendot.backends import CommandResult
from opendot.backends.fake import FakeBackend
from opendot.channels import ChannelError
from opendot.config import Config
from opendot.models import Destination, IncomingMessage
from opendot.store import Store, open_store

# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------

FROZEN_START = datetime(2026, 1, 5, 12, 0, 0, tzinfo=UTC)  # a Monday


class FrozenClock:
    """A clock that moves only when told to."""

    def __init__(self, start: datetime = FROZEN_START):
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("FrozenClock needs an aware datetime")
        self._now = value.astimezone(UTC)

    def advance(self, **delta: float) -> datetime:
        """advance(minutes=5), advance(hours=1, seconds=30), ... returns the new time."""
        self._now = self._now + timedelta(**delta)
        return self._now


# ---------------------------------------------------------------------------
# Docker / command runner
# ---------------------------------------------------------------------------


@dataclass
class FakeRun:
    args: list[str]
    input: str | None
    env: dict[str, str] | None
    timeout: float | None


class _RecordingText(io.StringIO):
    """A writable text stream that keeps its contents after close()."""

    def __init__(self) -> None:
        super().__init__()
        self.written = ""
        self.closed_by_caller = False

    def write(self, text: str) -> int:
        self.written += text
        return super().write(text)

    def close(self) -> None:
        self.closed_by_caller = True


class FakeProcess:
    """Stands in for subprocess.Popen in text mode.

    stdout yields the scripted lines; whatever the code writes to stdin is kept in
    stdin.written. wait() and poll() return the scripted returncode once stdout
    has been read to the end (or at once after kill/terminate).
    """

    def __init__(self, args: list[str], stdout_lines: Sequence[str], returncode: int, stderr: str):
        self.args = args
        self.stdin = _RecordingText()
        text = "".join(line if line.endswith("\n") else line + "\n" for line in stdout_lines)
        self.stdout = io.StringIO(text)
        self.stderr = io.StringIO(stderr)
        self._final = returncode
        self._stdout_len = len(text)
        self.returncode: int | None = None
        self.killed = False

    def poll(self) -> int | None:
        if self.returncode is None and self.stdout.tell() >= self._stdout_len:
            self.returncode = self._final
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = self._final
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        if self.returncode is None:
            self.returncode = -9

    def terminate(self) -> None:
        self.kill()


class FakeDocker:
    """A CommandRunner that records every argument list and returns scripted results.

    run(): returns the next result queued with script(), else CommandResult(args, 0, "", "").
    spawn(): returns the next FakeProcess queued with script_process(), else one that
    prints nothing and exits 0.
    """

    def __init__(self) -> None:
        self.runs: list[FakeRun] = []
        self.spawned: list[FakeProcess] = []
        self._results: deque[tuple[int, str, str]] = deque()
        self._processes: deque[tuple[list[str], int, str]] = deque()

    def script(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self._results.append((returncode, stdout, stderr))

    def script_process(
        self, stdout_lines: Sequence[str] = (), returncode: int = 0, stderr: str = ""
    ) -> None:
        self._processes.append((list(stdout_lines), returncode, stderr))

    def run(
        self,
        args: Sequence[str],
        *,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        args = list(args)
        self.runs.append(FakeRun(args, input, None if env is None else dict(env), timeout))
        returncode, stdout, stderr = self._results.popleft() if self._results else (0, "", "")
        return CommandResult(args, returncode, stdout, stderr)

    def spawn(self, args: Sequence[str], *, env: Mapping[str, str] | None = None) -> FakeProcess:
        lines, returncode, stderr = self._processes.popleft() if self._processes else ([], 0, "")
        process = FakeProcess(list(args), lines, returncode, stderr)
        process.env = None if env is None else dict(env)
        self.spawned.append(process)
        return process

    @property
    def all_args(self) -> list[list[str]]:
        """Argument lists of run() and spawn() calls, in call order within each kind."""
        return [r.args for r in self.runs] + [p.args for p in self.spawned]


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------


@dataclass
class FakePost:
    destination: Destination
    text: str
    external_id: str


@dataclass
class FakeUpload:
    destination: Destination
    filename: str
    content: str
    title: str
    external_id: str


class FakeChannel:
    """An in-memory Channel. receive() queues an incoming message; fetch_new() drains it.

    Message ids are "<conversation>:<n>". A top-level message's thread is its own "<n>".
    Set fail_posts = k to make the next k post() calls raise ChannelError.
    """

    def __init__(self, name: str = "fake"):
        self.name = name
        self.inbox: list[IncomingMessage] = []
        self.posts: list[FakePost] = []
        self.reactions: list[tuple[Destination, str, str]] = []
        self.uploads: list[FakeUpload] = []
        self.fail_posts = 0
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        return str(self._counter)

    def receive(
        self,
        text: str,
        *,
        author: str = "alice",
        conversation: str = "local",
        thread: str | None = None,
        received_at: datetime | None = None,
    ) -> IncomingMessage:
        ts = self._next_id()
        message = IncomingMessage(
            channel=self.name,
            external_id=f"{conversation}:{ts}",
            conversation=conversation,
            thread=thread or ts,
            author=author,
            text=text,
            is_reply=thread is not None,
            received_at=received_at,
        )
        self.inbox.append(message)
        return message

    def fetch_new(self) -> list[IncomingMessage]:
        messages, self.inbox = self.inbox, []
        return messages

    def post(self, destination: Destination, text: str) -> str:
        if self.fail_posts:
            self.fail_posts -= 1
            raise ChannelError("scripted post failure")
        external_id = f"{destination.conversation}:{self._next_id()}"
        self.posts.append(FakePost(destination, text, external_id))
        return external_id

    def react(self, destination: Destination, external_id: str, reaction: str) -> None:
        self.reactions.append((destination, external_id, reaction))

    def upload(self, destination: Destination, filename: str, content: str, title: str = "") -> str:
        external_id = f"file:{self._next_id()}"
        self.uploads.append(FakeUpload(destination, filename, content, title, external_id))
        return external_id

    @property
    def texts(self) -> list[str]:
        return [p.text for p in self.posts]


# ---------------------------------------------------------------------------
# HTTP (for the Slack channel and any other HTTP client)
# ---------------------------------------------------------------------------


class HttpMock:
    """Routes for httpx.MockTransport.

    add("POST", "/api/chat.postMessage", json={"ok": True, "ts": "1.0"}) queues a response
    for that method and path; several add() calls on one route are returned in order and
    the last one repeats. handler=callable(request) -> httpx.Response overrides json.
    A request to a path with no route fails the test. Every request is kept in .requests.
    """

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[Callable[[httpx.Request], httpx.Response]]] = {}
        self.requests: list[httpx.Request] = []

    def add(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
        handler: Callable[[httpx.Request], httpx.Response] | None = None,
    ) -> None:
        if handler is None:

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(status, json=json, headers=dict(headers or {}))

        self.routes.setdefault((method.upper(), path), []).append(handler)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        handlers = self.routes.get(key)
        if not handlers:
            raise AssertionError(f"no mock route for {request.method} {request.url.path}")
        handler = handlers.pop(0) if len(handlers) > 1 else handlers[0]
        return handler(request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def client(self, base_url: str = "https://mock.invalid") -> httpx.Client:
        return httpx.Client(transport=self.transport(), base_url=base_url)

    def json_bodies(self, path: str) -> list[Any]:
        """Parsed JSON bodies (or form fields as a dict) of requests to path."""
        bodies = []
        for request in self.requests:
            if request.url.path != path:
                continue
            content = request.content.decode()
            if request.headers.get("content-type", "").startswith("application/json"):
                bodies.append(jsonlib.loads(content))
            else:
                bodies.append(dict(httpx.QueryParams(content)))
        return bodies


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_saved_logins(tmp_path_factory: pytest.TempPathFactory, monkeypatch) -> None:
    """Point ~ at an empty folder, so a login saved on this machine never reaches a test."""
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    """A state root path under tmp_path. The config fixture creates it (mode 0700)."""
    return tmp_path / "state"


@pytest.fixture
def config(state_root: Path) -> Config:
    """Defaults, with state_root under tmp_path and the fake backend for both roles."""
    cfg = Config.from_dict(
        {
            "core": {"state_root": str(state_root)},
            "backend": {"worker": {"kind": "fake"}, "reviewer": {"kind": "fake"}},
        },
        env={},
    )
    cfg.ensure_directories()
    return cfg


@pytest.fixture
def store(config: Config, clock: FrozenClock) -> Iterator[Store]:
    """A migrated Store at state_root/opendot.db that reads time from the clock fixture."""
    opened = open_store(config, clock=clock)
    yield opened
    opened.close()


@pytest.fixture
def fake_backend() -> FakeBackend:
    """Worker backend. Script it with push(Step.WORK, {...}); inspect .calls."""
    return FakeBackend()


@pytest.fixture
def fake_reviewer() -> FakeBackend:
    """A second, independent FakeBackend for the review step."""
    return FakeBackend()


@pytest.fixture
def fake_docker() -> FakeDocker:
    return FakeDocker()


@pytest.fixture
def fake_channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def http_mock() -> HttpMock:
    return HttpMock()
