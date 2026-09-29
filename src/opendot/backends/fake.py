"""A scripted backend for tests and dry runs. It never starts a container.

Script format (a JSON file, or the same structure passed in code):

    {
      "work":    [ {"output": {...}, "usage": {"turns": 1}, "thread_id": "t-1"},
                   {"error": "the CLI crashed"},
                   {"interrupt": true},
                   {"timeout": true} ],
      "review":  [ ... ],
      "reflect": [ ... ]
    }

Each call to run_step takes the next entry for that step. An entry has exactly one
of "output", "error", "interrupt" or "timeout"; "usage" and "thread_id" are
optional. Without "thread_id", a resumed call returns its resume_id and a new call
returns "fake-thread-<n>". An empty list for a step raises BackendError.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opendot.backends import (
    BackendError,
    CommandRunner,
    StepInterrupted,
    StepLimits,
    StepTimedOut,
)
from opendot.models import Mount, Step, StepResult

if TYPE_CHECKING:
    from opendot.config import Config

_OUTCOMES = {"output", "error", "interrupt", "timeout"}


@dataclass
class FakeCall:
    step: Step
    prompt: str
    output_schema: dict[str, Any]
    env: dict[str, str]
    mounts: list[Mount]
    resume_id: str | None
    limits: StepLimits | None


@dataclass
class FakeBackend:
    kind: str = "fake"
    script: dict[Step, list[dict[str, Any]]] = field(default_factory=dict)
    calls: list[FakeCall] = field(default_factory=list)
    _threads: int = 0

    @classmethod
    def from_config(
        cls, config: Config, role: str, runner: CommandRunner | None = None
    ) -> FakeBackend:
        path = config.fake.script
        if path is None:
            return cls()
        return cls.from_file(path)

    @classmethod
    def from_file(cls, path: Path) -> FakeBackend:
        return cls.from_script(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_script(cls, data: Mapping[str, list[dict[str, Any]]]) -> FakeBackend:
        backend = cls()
        for step_name, entries in data.items():
            for entry in entries:
                backend._push(Step(step_name), entry)
        return backend

    # -- scripting -----------------------------------------------------------

    def _push(self, step: Step, entry: dict[str, Any]) -> None:
        outcomes = _OUTCOMES & set(entry)
        if len(outcomes) != 1 or set(entry) - _OUTCOMES - {"usage", "thread_id"}:
            raise ValueError(f"bad fake script entry for {step.value}: {entry!r}")
        self.script.setdefault(Step(step), []).append(entry)

    def push(
        self,
        step: Step,
        output: dict[str, Any],
        *,
        usage: dict[str, Any] | None = None,
        thread_id: str | None = None,
    ) -> None:
        """Queue one successful output for the next call of `step`."""
        entry: dict[str, Any] = {"output": output}
        if usage is not None:
            entry["usage"] = usage
        if thread_id is not None:
            entry["thread_id"] = thread_id
        self._push(step, entry)

    def push_error(self, step: Step, message: str = "scripted failure") -> None:
        self._push(step, {"error": message})

    def push_interrupt(self, step: Step) -> None:
        self._push(step, {"interrupt": True})

    def push_timeout(self, step: Step) -> None:
        self._push(step, {"timeout": True})

    def remaining(self, step: Step) -> int:
        return len(self.script.get(Step(step), []))

    def calls_for(self, step: Step) -> list[FakeCall]:
        return [c for c in self.calls if c.step is Step(step)]

    # -- Backend -------------------------------------------------------------

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
    ) -> StepResult:
        step = Step(step)
        self.calls.append(
            FakeCall(
                step=step,
                prompt=prompt,
                output_schema=output_schema,
                env=dict(env),
                mounts=list(mounts),
                resume_id=resume_id,
                limits=limits,
            )
        )
        if should_stop is not None and should_stop():
            raise StepInterrupted(f"{step.value} step stopped before it started")
        queue = self.script.get(step, [])
        if not queue:
            raise BackendError(f"the fake backend has no scripted output left for {step.value}")
        entry = queue.pop(0)
        if "error" in entry:
            raise BackendError(str(entry["error"]))
        if "interrupt" in entry:
            raise StepInterrupted(f"{step.value} step interrupted")
        if "timeout" in entry:
            raise StepTimedOut(f"{step.value} step timed out")
        thread_id = entry.get("thread_id") or resume_id
        if thread_id is None:
            self._threads += 1
            thread_id = f"fake-thread-{self._threads}"
        return StepResult(
            output=copy.deepcopy(entry["output"]),
            thread_id=thread_id,
            transcript_path=None,
            usage=dict(entry.get("usage") or {}),
        )
