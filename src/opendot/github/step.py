"""The step extension that gives each work step the task's repository copies.

Before a work step the host, for each configured repository:
1. fetches the control clone (repos/<name>, a bare clone of the default branch);
2. on the task's first step, clones it into worktrees/task-<id>/<name>, creates
   the branch <branch_prefix>task-<id> and runs the repository's prepare
   commands in a sandbox container;
3. mounts the copy at /opendot/repos/<name>, with its .git folder read-only, and
   marks that folder as a git safe.directory for the container.

Later steps of the same task reuse the copy, so the model's edits carry over.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from opendot.extensions import ExtensionError, StepContext
from opendot.github.containers import run_repository_commands, safe_directory_env
from opendot.github.git import GitError, auth_env, git, head_sha
from opendot.models import Attempt, HostMount, HostMountKind, StepPlan, WorktreeStatus
from opendot.sandbox import CONTAINER_REPOS, container_user

if TYPE_CHECKING:
    from opendot.backends import CommandRunner
    from opendot.config import Config, RepositoryConfig
    from opendot.store import Store

__all__ = [
    "RepositoryCopies",
    "copy_path",
    "ensure_control_clone",
    "git_url",
    "network_env",
    "step_extensions",
    "task_branch",
]

log = logging.getLogger(__name__)

FETCH_TIMEOUT = 900


def task_branch(config: Config, task_id: int) -> str:
    prefix = config.github.branch_prefix if config.github else "opendot/"
    return f"{prefix}task-{int(task_id)}"


def copy_path(config: Config, task_id: int, repository: str) -> Path:
    return config.worktrees_dir / f"task-{int(task_id)}" / repository


def git_url(repo: RepositoryConfig, overrides: Mapping[str, str] | None = None) -> str:
    """The address host git uses for the repository. Tests point it at a local folder."""
    if overrides and repo.name in overrides:
        return overrides[repo.name]
    return repo.remote


def network_env(config: Config, repo: RepositoryConfig, env: Mapping[str, str] | None) -> dict:
    """GIT_CONFIG_* values carrying the token, for an https remote on the GitHub host."""
    github = config.github
    if github is None or not repo.remote.startswith(f"https://{github.host}/"):
        return {}
    return auth_env(github.token(env), github.host)


def _token_list(config: Config, env: Mapping[str, str] | None) -> list[str]:
    token = config.github.token(env) if config.github else None
    return [token] if token else []


def ensure_control_clone(
    config: Config,
    store: Store,
    repo: RepositoryConfig,
    *,
    env: Mapping[str, str] | None = None,
    url_overrides: Mapping[str, str] | None = None,
) -> Path:
    """Clone or fetch repos/<name> (bare, default branch only) and record it."""
    control = config.repos_dir / repo.name
    url = git_url(repo, url_overrides)
    extra = network_env(config, repo, env)
    secrets_ = _token_list(config, env)
    branch = repo.default_branch
    if not (control / "HEAD").is_file():
        if control.exists():
            shutil.rmtree(control)
        git(
            None,
            "clone",
            "--bare",
            "--single-branch",
            "--no-tags",
            "--branch",
            branch,
            "--",
            url,
            str(control),
            env=extra,
            network=True,
            timeout=FETCH_TIMEOUT,
            secrets_=secrets_,
        )
    else:
        git(
            control,
            "fetch",
            "--no-tags",
            "--prune",
            "--",
            url,
            f"+refs/heads/{branch}:refs/heads/{branch}",
            env=extra,
            network=True,
            timeout=FETCH_TIMEOUT,
            secrets_=secrets_,
        )
    store.upsert_repository(
        repo.name, remote=repo.remote, control_path=control, default_branch=branch
    )
    store.mark_repository_fetched(repo.name)
    return control


def _hand_to_container_user(copy: Path) -> None:
    """When the host runs as root the container does not; give it the work files."""
    if os.getuid() != 0:
        return
    uid, gid = container_user()
    for root, dirs, files in os.walk(copy, followlinks=False):
        if Path(root) == copy:
            dirs[:] = [d for d in dirs if d != ".git"]
        for name in [*dirs, *files]:
            os.lchown(os.path.join(root, name), uid, gid)
    os.chown(copy, uid, gid)


class RepositoryCopies:
    """Makes and mounts the task's copy of every configured repository."""

    name = "github"

    def __init__(
        self,
        config: Config,
        *,
        runner: CommandRunner | None = None,
        env: Mapping[str, str] | None = None,
        url_overrides: Mapping[str, str] | None = None,
    ):
        if runner is None:
            from opendot.backends import SubprocessRunner

            runner = SubprocessRunner()
        self.config = config
        self.runner = runner
        self.env = env
        self.url_overrides = url_overrides

    def before_step(self, ctx: StepContext, plan: StepPlan) -> None:
        names = []
        for repo in self.config.repositories:
            path = self._copy(ctx, repo)
            plan.host_mounts.append(
                HostMount(HostMountKind.WORKTREE, path, f"{CONTAINER_REPOS}/{repo.name}", True)
            )
            names.append(f"{CONTAINER_REPOS}/{repo.name}")
        if names:
            plan.fixed_env = safe_directory_env(names, plan.fixed_env)
            plan.prompt_notes.append(
                "Repositories: your copy of each repository is at "
                + ", ".join(names)
                + ". Edit files there. The .git folder is read-only: git status, git diff "
                "and git log work, but git add, git commit and git push fail. The host "
                "commits your edits, runs the repository's checks and pushes only when you "
                "propose a github.push_branch or github.open_pr action."
            )
            plain = [repo.name for repo in self.config.repositories if repo.plain_git]
            if plain:
                plan.prompt_notes.append(
                    "These repositories are plain git remotes, not on GitHub: "
                    + ", ".join(plain)
                    + ". For them only github.push_branch works; pull requests, issues and "
                    "comments are refused."
                )

    def after_step(self, ctx: StepContext, plan: StepPlan, attempt: Attempt | None) -> None:
        return None

    def _copy(self, ctx: StepContext, repo: RepositoryConfig) -> Path:
        store = ctx.store
        task_id = ctx.task.id
        existing = store.get_worktree(task_id, repo.name)
        if existing is not None and existing.status is WorktreeStatus.ACTIVE:
            if (existing.path / ".git").is_dir():
                return existing.path
            raise ExtensionError(f"the copy of {repo.name} for task {task_id} is missing")
        if existing is not None:
            raise ExtensionError(f"the copy of {repo.name} for task {task_id} was removed")
        try:
            control = ensure_control_clone(
                self.config, store, repo, env=self.env, url_overrides=self.url_overrides
            )
        except GitError as exc:
            raise ExtensionError(f"could not fetch {repo.name}: {exc}") from None
        path = copy_path(self.config, task_id, repo.name)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        if path.exists():
            shutil.rmtree(path)  # a copy left by a step that stopped half way
        branch = task_branch(self.config, task_id)
        try:
            git(
                None,
                "clone",
                "--local",
                "--no-tags",
                "--single-branch",
                "--branch",
                repo.default_branch,
                "--",
                str(control),
                str(path),
            )
            git(path, "checkout", "--quiet", "-b", branch)
            base_sha = head_sha(path)
        except GitError as exc:
            shutil.rmtree(path, ignore_errors=True)
            raise ExtensionError(f"could not copy {repo.name}: {exc}") from None
        _hand_to_container_user(path)
        if repo.prepare:
            outcomes = run_repository_commands(
                self.config,
                self.runner,
                repository=repo.name,
                copy=path,
                commands=repo.prepare,
                network=self.config.sandbox.network,
                timeout_seconds=self.config.limits.step_minutes * 60,
                purpose="prepare",
            )
            failed = [o for o in outcomes if not o.ok]
            if failed:
                shutil.rmtree(path, ignore_errors=True)
                raise ExtensionError(
                    f"{repo.name}: prepare command {failed[0].command!r} exited with "
                    f"{failed[0].exit_code}: {failed[0].output_tail[-500:]}"
                )
        store.add_worktree(
            task_id,
            repo.name,
            path=path,
            base_ref=repo.default_branch,
            base_sha=base_sha,
            branch=branch,
        )
        store.log_event(
            "github.copy_made",
            {"repository": repo.name, "branch": branch, "base_sha": base_sha},
            task_id=task_id,
        )
        return path


def step_extensions(config: Config) -> list[RepositoryCopies]:
    if not config.repositories or config.github is None:
        return []
    return [RepositoryCopies(config)]
