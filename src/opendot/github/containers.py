"""Repository commands (prepare and checks) in a sandbox container.

Each command runs in its own `docker run` with the step image and the same
limits as a model step: no capabilities, a read-only root, a non-root user. The
container has no /work folder, no CLI login and no environment variables other than
the git safe.directory setting for the copy. The
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
from collections.abc import Mapping, Sequence
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

__all__ = ["CommandOutcome", "image_id", "run_repository_commands", "safe_directory_env"]

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


def safe_directory_env(
    directories: Sequence[str], existing: Mapping[str, str] | None = None
) -> dict[str, str]:
    """GIT_CONFIG_* values that mark each container repository folder as safe.

    When the host runs as root, the copy's files are handed to the container user
    but its read-only .git folder is not, so git in the container sees a repository
    owned by another user and refuses to run. Git reads safe.directory from these
    variables. Values already in existing are kept; the new ones follow them."""
    env = dict(existing or {})
    count = int(env.get("GIT_CONFIG_COUNT", "0") or "0")
    for directory in directories:
        env[f"GIT_CONFIG_KEY_{count}"] = "safe.directory"
        env[f"GIT_CONFIG_VALUE_{count}"] = directory
        count += 1
    if count:
        env["GIT_CONFIG_COUNT"] = str(count)
    return env


def _docker_env() -> dict[str, str]:
    return {name: os.environ[name] for name in DOCKER_CLIENT_VARIABLES if name in os.environ}


def image_id(config: Config, runner: CommandRunner) -> str | None:
    """The content id of the sandbox image (sha256:...), or None when Docker cannot say.

    The image name can point at new content after a rebuild or a pull, so a cached
    check result is tied to this id, not to the name."""
    args = [config.sandbox.docker, "image", "inspect", "--format", "{{.Id}}", config.sandbox.image]
    try:
        result = runner.run(args, env=_docker_env(), timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    ident = (result.stdout or "").strip()
    if result.returncode != 0 or not ident.startswith("sha256:") or len(ident.split()) != 1:
        return None
    return ident


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
            fixed_env=safe_directory_env([mount.container]),
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
