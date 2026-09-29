"""Repository commands (prepare and checks) in a sandbox container.

Each command runs in its own `docker run` with the step image and the same
limits as a model step: no capabilities, a read-only root, a non-root user. The
container has no /work folder, no CLI login and no environment variables. The
task's copy is its only mount: /opendot/repos/<name>, writable, with the copy's
.git folder read-only on top. The network is the one the caller names:
sandbox.network for prepare commands, the repository's check_network for checks.
"""

from __future__ import annotations

import dataclasses
import os
import secrets
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from opendot.models import HostMount, HostMountKind, Step
from opendot.proc import Deadline
from opendot.redact import redact
from opendot.sandbox import CONTAINER_REPOS, check_host_mounts, docker_run_args, stop_container

if TYPE_CHECKING:
    from opendot.backends import CommandRunner
    from opendot.config import Config

__all__ = ["CommandOutcome", "run_repository_commands"]

OUTPUT_TAIL_CHARS = 2000
DOCKER_CLIENT_VARIABLES = ("DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT")


@dataclass(frozen=True)
class CommandOutcome:
    command: str
    exit_code: int  # 124 when the time limit ended it
    seconds: float
    output_tail: str  # the last characters of stdout and stderr, redacted

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def as_dict(self) -> dict[str, object]:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "seconds": round(self.seconds, 1),
            "output_tail": self.output_tail,
        }


def _docker_env() -> dict[str, str]:
    return {name: os.environ[name] for name in DOCKER_CLIENT_VARIABLES if name in os.environ}


def run_repository_commands(
    config: Config,
    runner: CommandRunner,
    *,
    repository: str,
    copy: Path,
    commands: Sequence[str],
    network: str,
    timeout_seconds: float,
    purpose: str,
) -> list[CommandOutcome]:
    """Run commands one by one in fresh containers; stop at the first failure.

    timeout_seconds is shared by all commands. A command still running at the
    deadline is killed with `docker kill` and gets exit code 124.
    """
    mount = HostMount(HostMountKind.WORKTREE, copy, f"{CONTAINER_REPOS}/{repository}", True)
    check_host_mounts(Step.WORK, [mount], config)
    sandbox = dataclasses.replace(config.sandbox, network=network)
    deadline = Deadline(timeout_seconds)
    outcomes: list[CommandOutcome] = []
    for command in commands:
        remaining = deadline.remaining()
        name = f"opendot-{purpose}-{secrets.token_hex(6)}"
        if remaining is not None and remaining <= 0:
            outcomes.append(CommandOutcome(command, 124, 0.0, "not started: out of time"))
            break
        args = docker_run_args(
            sandbox,
            name=name,
            command=["sh", "-c", command],
            session=None,
            work_writable=False,
            host_mounts=[mount],
            workdir=f"{CONTAINER_REPOS}/{repository}",
        )
        started = time.monotonic()
        try:
            result = runner.run(args, env=_docker_env(), timeout=remaining)
            code, text = result.returncode, (result.stdout or "") + (result.stderr or "")
        except subprocess.TimeoutExpired as exc:
            stop_container(runner, config.sandbox.docker, name)
            partial = exc.stdout if isinstance(exc.stdout, str) else ""
            code, text = 124, partial + "\n[stopped: the time limit was reached]"
        outcome = CommandOutcome(
            command=command,
            exit_code=code,
            seconds=time.monotonic() - started,
            output_tail=redact(text[-OUTPUT_TAIL_CHARS:]),
        )
        outcomes.append(outcome)
        if not outcome.ok:
            break
    return outcomes
