"""A backend that calls the Anthropic Messages API directly. Planned for v0.2.

v0.1 runs every step through a CLI inside a container (Codex or Claude Code).
A direct API backend needs its own tool loop and sandboxed tool execution, which
are not built yet.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from opendot.backends import CommandRunner, StepLimits
from opendot.models import Mount, Step, StepResult

if TYPE_CHECKING:
    from opendot.config import Config

NOT_READY = (
    "the anthropic_api backend is planned for v0.2; use the codex or claude_code backend in v0.1"
)


class AnthropicApiBackend:
    kind = "anthropic_api"

    @classmethod
    def from_config(
        cls, config: Config, role: str, runner: CommandRunner | None = None
    ) -> AnthropicApiBackend:
        raise NotImplementedError(NOT_READY)

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
        raise NotImplementedError(NOT_READY)
