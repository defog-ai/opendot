"""Step extensions: the v0.2 features that add folders and tools to a work step.

A step extension prepares something on the host before a work step and cleans up
after it. Examples: the task's copy of a repository, the built-in browser, the
connector gateway. Each feature module listed in FEATURE_STEP_MODULES exposes

    def step_extensions(config: Config) -> list[StepExtension]

and returns an empty list when the feature is off in the config. A module that is
not installed is skipped.

The orchestrator uses StepExtensions around each work step:

    plan = self.extensions.begin(ctx)          # None when no extension is active
    attempt = None
    try:
        prompt = with_prompt_notes(prompt, plan)
        attempt, output = self._run_model(..., plan=plan)
    finally:
        self.extensions.finish(ctx, plan, attempt)

Review and reflect steps get no plan. Extensions only change the StepPlan; they
never start the model, and they never send anything outward. Outward work is an
action (opendot.actions) and goes through rules, review and approval.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Protocol

from opendot.models import Attempt, Step, StepPlan, Task

if TYPE_CHECKING:
    from opendot.config import Config
    from opendot.store import Store

__all__ = [
    "FEATURE_CLI_MODULES",
    "FEATURE_STEP_MODULES",
    "ExtensionError",
    "StepContext",
    "StepExtension",
    "StepExtensions",
    "doctor_checks",
    "load_feature_modules",
    "load_step_extensions",
    "register_feature_cli",
    "new_step_token",
    "step_run_dir",
    "with_prompt_notes",
]

log = logging.getLogger(__name__)

# Order matters: repositories are copied first, then the browser and the gateway
# add their tools.
FEATURE_STEP_MODULES = (
    "opendot.github.step",
    "opendot.browser",
    "opendot.gateway.step",
)

# Each module exposes register_cli(subparsers) and doctor_checks(config), where
# doctor_checks returns a list of (name, ok, detail) tuples.
FEATURE_CLI_MODULES = (
    "opendot.github.cli",
    "opendot.browser",
    "opendot.gateway.cli",
)


class ExtensionError(RuntimeError):
    """An extension could not prepare the step. The step does not run."""


@dataclass(frozen=True)
class StepContext:
    task: Task
    step: Step
    store: Store
    config: Config
    backend_kind: str  # "codex", "claude_code", "opencode" or "fake"


class StepExtension(Protocol):
    name: str

    def before_step(self, ctx: StepContext, plan: StepPlan) -> None:
        """Add host mounts, MCP servers, fixed env values and prompt notes to plan.

        Create folders only inside plan.run_dir or the folders check_host_mounts
        allows. Raise ExtensionError to stop the step.
        """
        ...

    def after_step(self, ctx: StepContext, plan: StepPlan, attempt: Attempt | None) -> None:
        """Clean up. Always called once before_step returned, even when the step
        failed. attempt is None when the model never started."""
        ...


def new_step_token() -> str:
    """A short random id for one step. Short, because the gateway's unix socket
    path under the run folder must stay under the 108-byte limit."""
    return secrets.token_hex(6)


def step_run_dir(config: Config, task_id: int, step_token: str) -> Path:
    """runs_dir/task-<id>/<step_token>, created with mode 0700."""
    path = config.runs_dir / f"task-{int(task_id)}" / step_token
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    path.chmod(0o700)
    path.parent.chmod(0o700)
    return path


def with_prompt_notes(prompt: str, plan: StepPlan | None) -> str:
    """The work prompt with the plan's notes appended under one heading."""
    if plan is None or not plan.prompt_notes:
        return prompt
    notes = "\n\n".join(note.strip() for note in plan.prompt_notes if note.strip())
    return f"{prompt.rstrip()}\n\n## Tools and folders for this step\n\n{notes}\n"


def load_feature_modules(names: Sequence[str], attribute: str) -> list[ModuleType]:
    """The modules in names that exist and define attribute, in order. A module
    that is not installed is skipped; any other import error is raised."""
    found: list[ModuleType] = []
    for module_name in names:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name is not None and module_name.startswith(exc.name):
                continue
            raise
        if hasattr(module, attribute):
            found.append(module)
    return found


def load_step_extensions(config: Config) -> list[StepExtension]:
    """The active extensions from FEATURE_STEP_MODULES, in order."""
    found: list[StepExtension] = []
    for module in load_feature_modules(FEATURE_STEP_MODULES, "step_extensions"):
        found.extend(module.step_extensions(config))
    return found


def register_feature_cli(subparsers: argparse._SubParsersAction) -> None:
    """Let each feature module in FEATURE_CLI_MODULES add its subcommands."""
    for module in load_feature_modules(FEATURE_CLI_MODULES, "register_cli"):
        module.register_cli(subparsers)


def doctor_checks(config: Config) -> list[tuple[str, bool, str]]:
    """Every feature module's doctor checks as (name, ok, detail) tuples."""
    results: list[tuple[str, bool, str]] = []
    for module in load_feature_modules(FEATURE_CLI_MODULES, "doctor_checks"):
        results.extend(module.doctor_checks(config))
    return results


class StepExtensions:
    def __init__(self, extensions: Sequence[StepExtension]):
        self.extensions = list(extensions)

    @classmethod
    def from_config(cls, config: Config) -> StepExtensions:
        return cls(load_step_extensions(config))

    def begin(self, ctx: StepContext) -> StepPlan | None:
        """Build the plan for a work step. None when no extension is active or the
        step is not a work step. If one extension fails, those that already ran
        are finished in reverse order and the error is raised."""
        if not self.extensions or Step(ctx.step) is not Step.WORK:
            return None
        token = new_step_token()
        plan = StepPlan(step_token=token, run_dir=step_run_dir(ctx.config, ctx.task.id, token))
        started: list[StepExtension] = []
        try:
            for extension in self.extensions:
                extension.before_step(ctx, plan)
                started.append(extension)
        except BaseException:
            self._finish(started, ctx, plan, None)
            raise
        return plan

    def finish(self, ctx: StepContext, plan: StepPlan | None, attempt: Attempt | None) -> None:
        """Run every extension's after_step in reverse order. Errors are logged as
        task events and do not stop the others."""
        if plan is None:
            return
        if attempt is not None:
            ctx.store.link_gateway_calls(plan.step_token, attempt.id)
        self._finish(self.extensions, ctx, plan, attempt)

    def _finish(
        self,
        extensions: Sequence[StepExtension],
        ctx: StepContext,
        plan: StepPlan,
        attempt: Attempt | None,
    ) -> None:
        for extension in reversed(extensions):
            try:
                extension.after_step(ctx, plan, attempt)
            except Exception as exc:  # noqa: BLE001 - one failed cleanup must not skip the rest
                log.warning("step extension %s failed to clean up: %s", extension.name, exc)
                ctx.store.log_event(
                    "extension.cleanup_failed",
                    {"extension": extension.name, "error": str(exc)[:500]},
                    task_id=ctx.task.id,
                )
