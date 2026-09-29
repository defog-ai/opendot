"""`opendot github ...` commands and the offline doctor checks."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from opendot.github.api import GitHubClient, GitHubError
from opendot.github.git import GitError
from opendot.github.step import ensure_control_clone
from opendot.models import PublicationKind, PublicationState, WorktreeStatus

if TYPE_CHECKING:
    from opendot.config import Config

__all__ = ["doctor_checks", "register_cli"]


def _load(args: argparse.Namespace) -> Config:
    from opendot.config import Config

    return Config.load(Path(args.config) if getattr(args, "config", None) else None)


def _open(args: argparse.Namespace):
    from opendot.store import open_store

    config = _load(args)
    return config, open_store(config)


def cmd_repos(args: argparse.Namespace) -> int:
    config, store = _open(args)
    known = {r.name: r for r in store.list_repositories()}
    if not config.repositories:
        print("no repositories configured")
    for repo in config.repositories:
        record = known.get(repo.name)
        visibility = record.visibility.value if record else "unknown"
        fetched = (
            record.last_fetched_at.isoformat() if record and record.last_fetched_at else "never"
        )
        allowed = "allowed" if repo.public else "refused"
        print(
            f"{repo.name}\t{repo.remote}\tbranch={repo.default_branch}\tvisibility={visibility}\t"
            f"push-while-public={allowed}\tfetched={fetched}"
        )
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    config, store = _open(args)
    code = 0
    for repo in config.repositories:
        if args.repository and repo.name != args.repository:
            continue
        try:
            path = ensure_control_clone(config, store, repo)
            print(f"{repo.name}: fetched into {path}")
        except GitError as exc:
            print(f"{repo.name}: {exc}")
            code = 1
    return code


def cmd_copies(args: argparse.Namespace) -> int:
    _, store = _open(args)
    status = None if args.all else WorktreeStatus.ACTIVE
    for item in store.list_worktrees(task_id=args.task, status=status):
        print(
            f"task {item.task_id}\t{item.repository}\t{item.branch}\t{item.status.value}\t"
            f"base={item.base_sha[:12]}\t{item.path}"
        )
    return 0


def cmd_publications(args: argparse.Namespace) -> int:
    _, store = _open(args)
    for item in store.list_publications(task_id=args.task):
        where = f"#{item.number}" if item.number else (item.branch or "")
        print(
            f"{item.id}\ttask {item.task_id}\t{item.kind.value}\t{item.repository}{where}\t"
            f"{item.state.value}\t{item.url or ''}\t{item.error or ''}"
        )
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    """Delete the copies of finished tasks."""
    config, store = _open(args)
    removed = 0
    for item in store.list_worktrees(status=WorktreeStatus.ACTIVE):
        task = store.get_task(item.task_id)
        if not task.state.is_terminal:
            continue
        path = Path(item.path)
        base = config.worktrees_dir.resolve()
        if path.exists() and base not in path.resolve().parents:
            print(f"skipped {path}: not inside {base}")
            continue
        if args.dry_run:
            print(f"would remove {path}")
            continue
        shutil.rmtree(path, ignore_errors=True)
        store.mark_worktree_removed(item.id)
        removed += 1
        print(f"removed {path}")
    if not args.dry_run:
        print(f"{removed} copies removed")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    """Record pull requests and issues that were merged or closed on GitHub."""
    config, store = _open(args)
    github = config.github
    token = github.token() if github else None
    if not token:
        print(f"{github.token_env if github else 'the GitHub token'} is not set")
        return 1
    kinds = {PublicationKind.PULL_REQUEST, PublicationKind.ISSUE}
    changed = 0
    with GitHubClient(github.api_url, token) as client:
        for item in store.list_publications(states=[PublicationState.PUBLISHED]):
            if item.kind not in kinds or not item.number:
                continue
            owner, name = item.repository.split("/", 1)
            try:
                if item.kind is PublicationKind.PULL_REQUEST:
                    data = client.pull(owner, name, item.number)
                    state = (
                        PublicationState.MERGED
                        if data.get("merged")
                        else PublicationState.CLOSED
                        if data.get("state") == "closed"
                        else None
                    )
                else:
                    data = client.issue(owner, name, item.number)
                    state = PublicationState.CLOSED if data.get("state") == "closed" else None
            except GitHubError as exc:
                print(f"{item.repository}#{item.number}: {exc}")
                continue
            if state is not None:
                store.update_publication(item.id, state=state)
                changed += 1
                print(f"{item.repository}#{item.number}: {state.value}")
    print(f"{changed} publications updated")
    return 0


def _dispatch(args: argparse.Namespace) -> int:
    return args.github_handler(args)


def register_cli(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "github", help="repositories, task copies and what the host published on GitHub"
    )
    parser.set_defaults(handler=_dispatch)
    commands = parser.add_subparsers(dest="github_command", required=True)

    def add(name: str, handler, help_text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text)
        sub.set_defaults(github_handler=handler)
        return sub

    add("repos", cmd_repos, "list configured repositories")
    add("fetch", cmd_fetch, "clone or fetch the control clones").add_argument(
        "repository", nargs="?"
    )
    sub = add("copies", cmd_copies, "list task copies")
    sub.add_argument("--task", type=int)
    sub.add_argument("--all", action="store_true", help="include removed copies")
    add(
        "publications", cmd_publications, "list pushes, pull requests, issues and comments"
    ).add_argument("--task", type=int)
    add("prune", cmd_prune, "delete the copies of finished tasks").add_argument(
        "--dry-run", action="store_true"
    )
    add("reconcile", cmd_reconcile, "record merged or closed pull requests and issues")


def doctor_checks(config: Config) -> list[tuple[str, bool, str]]:
    """Offline checks. None when no repository is configured."""
    if not config.repositories or config.github is None:
        return []
    github = config.github
    results: list[tuple[str, bool, str]] = []
    git_path = shutil.which("git")
    results.append(("github: git", git_path is not None, git_path or "git is not on PATH"))
    token = github.token()
    results.append(
        (
            "github: token",
            token is not None,
            f"{github.token_env} is set" if token else f"{github.token_env} is not set",
        )
    )
    if github.signing_key is not None:
        ok = Path(github.signing_key).expanduser().is_file()
        results.append(("github: signing key", ok, str(github.signing_key)))
    for repo in config.repositories:
        slug = repo.github_slug(github.host)
        results.append(
            (
                f"github: {repo.name} remote",
                slug is not None,
                f"{slug[0]}/{slug[1]} on {github.host}"
                if slug
                else f"{repo.remote} is not a remote on {github.host}",
            )
        )
    return results
