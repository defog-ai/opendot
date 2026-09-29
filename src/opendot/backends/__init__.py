"""Model backends and the command runner they share.

A backend runs one model step and returns its structured output. It never carries
out an outward action; the host checks the output against the step's schema and
decides what happens next.

Registry: each backend kind maps to "module:ClassName". The class must provide
    @classmethod
    def from_config(cls, config: Config, role: str, runner: CommandRunner | None = None) -> Backend
where role is "worker" or "reviewer". Modules load only when create_backend asks for them.
"""

from __future__ import annotations

import importlib
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING, Any, Protocol

from opendot.models import Mount, Step, StepPlan, StepResult

if TYPE_CHECKING:
    from opendot.config import Config

__all__ = [
    "Backend",
    "BackendError",
    "CommandResult",
    "CommandRunner",
    "Process",
    "StepInterrupted",
    "StepLimits",
    "StepTimedOut",
    "SubprocessRunner",
    "create_backend",
    "known_backend_kinds",
    "register_backend",
]


class BackendError(Exception):
    """The step failed: the CLI crashed, the login is missing, or the output was unreadable."""


class StepInterrupted(BackendError):
    """A stop note ended the step (the should_stop callback returned True)."""


class StepTimedOut(BackendError):
    """The step ran past StepLimits.timeout_seconds."""


@dataclass(frozen=True)
class StepLimits:
    timeout_seconds: float | None = None  # wall-clock limit for this step
    max_turns: int | None = None  # passed to the CLI when it supports a turn limit


class Backend(Protocol):
    kind: str  # the registry key, e.g. "codex"

    def run_step(
        self,
        step: Step,
        prompt: str,
        output_schema: dict[str, Any],
        env: Mapping[str, str],
        mounts: Sequence[Mount],
        resume_id: str | None = None,
        *,
        limits: StepLimits | None = None,
        should_stop: Callable[[], bool] | None = None,
        plan: StepPlan | None = None,
    ) -> StepResult:
        """Run one step and return its output.

        step: which step (work / review / reflect); selects the prompt file and schema.
        prompt: the full prompt text, already assembled by the host.
        output_schema: JSON schema the output must follow; pass it to the CLI.
        env: variables for the container. Review steps always get an empty mapping.
        mounts: extra bind mounts for the container.
        resume_id: a thread_id from an earlier StepResult; continue that session.
        limits: time and turn limits for this step.
        should_stop: polled while the step runs; when it returns True, stop the
            step and raise StepInterrupted.
        plan: what the step extensions prepared for a work step (task folders,
            MCP servers, fixed values, shared memory size), or None. A backend
            passes plan.host_mounts through check_host_mounts before it uses them.

        Returns StepResult(output, thread_id, transcript_path, usage). The backend
        does not validate output against the schema; the host does. usage uses the
        keys turns, input_tokens and output_tokens when the CLI reports them.
        Raises BackendError, StepInterrupted or StepTimedOut.
        """
        ...


# ---------------------------------------------------------------------------
# Command runner (docker and other host commands)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


class Process(Protocol):
    """The subset of subprocess.Popen (text mode) that backends use."""

    stdin: IO[str] | None
    stdout: IO[str] | None
    stderr: IO[str] | None
    returncode: int | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...

    def terminate(self) -> None: ...


class CommandRunner(Protocol):
    def run(
        self,
        args: Sequence[str],
        *,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        """Run to completion and capture text output. Does not raise on a non-zero exit."""
        ...

    def spawn(self, args: Sequence[str], *, env: Mapping[str, str] | None = None) -> Process:
        """Start a long-running process with text pipes on stdin, stdout and stderr."""
        ...


class SubprocessRunner:
    """The real CommandRunner. Children get only PATH, HOME and the given env."""

    def run(
        self,
        args: Sequence[str],
        *,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        completed = subprocess.run(
            list(args),
            input=input,
            env=_child_env(env),
            timeout=timeout,
            capture_output=True,
            text=True,
            check=False,
        )
        return CommandResult(list(args), completed.returncode, completed.stdout, completed.stderr)

    def spawn(self, args: Sequence[str], *, env: Mapping[str, str] | None = None) -> Process:
        return subprocess.Popen(
            list(args),
            env=_child_env(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )


def _child_env(env: Mapping[str, str] | None) -> dict[str, str]:
    base = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
    if "HOME" in os.environ:
        base["HOME"] = os.environ["HOME"]
    base.update(env or {})
    return base


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

BackendFactory = Callable[..., Backend]

_BACKENDS: dict[str, str | BackendFactory] = {
    "codex": "opendot.backends.codex:CodexBackend",
    "claude_code": "opendot.backends.claude_code:ClaudeCodeBackend",
    "anthropic_api": "opendot.backends.anthropic_api:AnthropicApiBackend",
    "fake": "opendot.backends.fake:FakeBackend",
}


def known_backend_kinds() -> set[str]:
    return set(_BACKENDS)


def register_backend(kind: str, factory: str | BackendFactory) -> None:
    """Add or replace a backend kind. factory is "module:Class" or a callable
    factory(config, role, runner=None) -> Backend."""
    _BACKENDS[kind] = factory


def create_backend(config: Config, role: str, runner: CommandRunner | None = None) -> Backend:
    """Build the backend configured for role ("worker" or "reviewer")."""
    kind = config.backend_choice(role).kind
    try:
        entry = _BACKENDS[kind]
    except KeyError:
        raise BackendError(f"unknown backend kind {kind!r}") from None
    if isinstance(entry, str):
        module_name, _, attr = entry.partition(":")
        cls = getattr(importlib.import_module(module_name), attr)
        return cls.from_config(config, role, runner=runner)
    return entry(config, role, runner=runner)
