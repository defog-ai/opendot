"""Host git commands for control clones and task copies.

Every command:
- runs with the host's global and system git config switched off
  (GIT_CONFIG_GLOBAL=/dev/null, GIT_CONFIG_NOSYSTEM=1), so no credential helper,
  filter, alias or template from the host user's config applies;
- passes -c core.hooksPath=/dev/null and -c core.fsmonitor=false, so nothing in
  a repository can make the host run a program;
- never recurses into submodules;
- gets a scrubbed environment (opendot.proc.scrubbed_env).

The GitHub token never appears in an argument list or a remote address. For the
one fetch or push that needs it, it is passed as an http.extraheader value in
GIT_CONFIG_* variables of that process only, and only for https://<host>/.

Files in a task copy were written by the model. They may be symbolic links that
point at host paths, so size and type checks use lstat and git stores links as
links. A nested .git entry is refused, because `git add` would turn its folder
into a submodule link.
"""

from __future__ import annotations

import base64
import fnmatch
import os
import secrets
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from opendot.proc import scrubbed_env
from opendot.redact import redact

__all__ = [
    "EMPTY_TREE",
    "ChangedFile",
    "GitError",
    "Identity",
    "added_lines",
    "auth_env",
    "changed_files",
    "check_changed_files",
    "commit_messages",
    "commit_worktree",
    "diff_stat",
    "diff_text",
    "find_nested_git",
    "git",
    "head_sha",
    "head_tree",
    "worktree_tree",
]

# The hash of git's empty tree.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

SAFE_CONFIG = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "submodule.recurse=false",
    "-c",
    "protocol.ext.allow=never",
    "-c",
    "core.symlinks=true",
)

# Variables a network git command (fetch, push, clone from a remote) may inherit
# on top of scrubbed_env: the ssh agent, for ssh remotes the host user set up.
NETWORK_VARIABLES = ("SSH_AUTH_SOCK",)

MODE_SYMLINK = "120000"
MODE_GITLINK = "160000"


class GitError(RuntimeError):
    """A host git command failed, or a copy holds something the host refuses."""


def _base_env(extra: Mapping[str, str] | None = None, *, network: bool = False) -> dict[str, str]:
    env = scrubbed_env()
    if network:
        env.update({name: os.environ[name] for name in NETWORK_VARIABLES if name in os.environ})
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        }
    )
    env.update(extra or {})
    return env


def auth_env(token: str | None, host: str) -> dict[str, str]:
    """GIT_CONFIG_* values that send the token to https://<host>/ only. Empty without a token."""
    if not token:
        return {}
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.https://{host}/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }


def git(
    cwd: Path | None,
    *args: str,
    env: Mapping[str, str] | None = None,
    network: bool = False,
    timeout: float = 600,
    secrets_: Sequence[str] = (),
) -> str:
    """Run one host git command and return its stdout. Raises GitError."""
    argv = ["git", *SAFE_CONFIG]
    if cwd is not None:
        argv += ["-C", str(cwd)]
    argv += list(args)
    try:
        completed = subprocess.run(
            argv,
            env=_base_env(env, network=network),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0] if args else ''} took longer than {timeout:g} s") from None
    if completed.returncode != 0:
        detail = redact(completed.stderr.strip()[-2000:], secrets_)
        raise GitError(f"git {args[0] if args else ''} failed: {detail}")
    return completed.stdout


def head_sha(copy: Path) -> str:
    return git(copy, "rev-parse", "--verify", "HEAD^{commit}").strip()


def head_tree(copy: Path) -> str:
    return git(copy, "rev-parse", "--verify", "HEAD^{tree}").strip()


def find_nested_git(copy: Path) -> list[str]:
    """Paths (relative to the copy) of every .git entry below the top one."""
    found: list[str] = []
    top = copy / ".git"
    for root, dirs, files in os.walk(copy, followlinks=False):
        root_path = Path(root)
        if root_path == copy:
            dirs[:] = [d for d in dirs if d != ".git"]
        for name in [*dirs, *files]:
            if name == ".git" and root_path / name != top:
                found.append((root_path / name).relative_to(copy).as_posix())
        dirs[:] = [d for d in dirs if d != ".git"]
    return sorted(found)


def worktree_tree(copy: Path) -> str:
    """The tree hash of the copy's files as `git add -A` would stage them.

    Uses a temporary index inside the copy's .git folder (the host may write
    there; the container may not), so the copy's own index is left alone.
    Refuses a nested .git entry.
    """
    nested = find_nested_git(copy)
    if nested:
        raise GitError(f"the copy holds a nested .git entry, which the host refuses: {nested[:5]}")
    index = copy / ".git" / f"opendot-index-{secrets.token_hex(6)}"
    env = {"GIT_INDEX_FILE": str(index)}
    try:
        git(copy, "read-tree", "HEAD", env=env)
        git(copy, "add", "--all", "--", ".", env=env)
        return git(copy, "write-tree", env=env).strip()
    finally:
        index.unlink(missing_ok=True)


@dataclass(frozen=True)
class Identity:
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    signing_key: Path | None = None


def commit_worktree(copy: Path, branch: str, message: str, identity: Identity) -> tuple[str, bool]:
    """Commit the copy's files on branch. Returns (commit sha, made a new commit).

    No commit is made when the files already match HEAD. Uses commit-tree, so no
    hook can run, then moves the branch with update-ref (checking its old value)
    and resets the copy's index to the new commit.
    """
    tree = worktree_tree(copy)
    old = head_sha(copy)
    if tree == head_tree(copy):
        return old, False
    current = git(copy, "symbolic-ref", "--quiet", "HEAD").strip()
    if current != f"refs/heads/{branch}":
        raise GitError(f"the copy is on {current or 'a detached HEAD'}, not refs/heads/{branch}")
    env = {
        "GIT_AUTHOR_NAME": identity.author_name,
        "GIT_AUTHOR_EMAIL": identity.author_email,
        "GIT_COMMITTER_NAME": identity.committer_name,
        "GIT_COMMITTER_EMAIL": identity.committer_email,
    }
    sign: list[str] = []
    if identity.signing_key is not None:
        sign = ["-c", "gpg.format=ssh", "-c", f"user.signingkey={identity.signing_key}"]
    args = [*sign, "commit-tree", tree, "-p", old, "-F", "-"]
    if identity.signing_key is not None:
        args.insert(len(sign) + 1, "-S")
    commit = _git_with_input(copy, args, message, env).strip()
    git(copy, "update-ref", "-m", "opendot commit", f"refs/heads/{branch}", commit, old)
    git(copy, "reset", "--quiet", "--mixed", commit)
    return commit, True


def _git_with_input(copy: Path, args: Sequence[str], text: str, env: Mapping[str, str]) -> str:
    argv = ["git", *SAFE_CONFIG, "-C", str(copy), *args]
    completed = subprocess.run(
        argv,
        input=text,
        env=_base_env(env),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise GitError(f"git {args[0]} failed: {redact(completed.stderr.strip()[-2000:])}")
    return completed.stdout


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str  # A, M, D or T
    mode: str  # new mode, "000000" for a deleted file


def changed_files(copy: Path, base: str, head: str) -> list[ChangedFile]:
    """Files that differ between base and head, without rename detection."""
    out = git(copy, "diff-tree", "-r", "-z", "--no-renames", "--no-commit-id", base, head)
    fields = out.split("\0")
    result: list[ChangedFile] = []
    i = 0
    while i + 1 < len(fields):
        meta, path = fields[i], fields[i + 1]
        i += 2
        if not meta.startswith(":"):
            continue
        parts = meta[1:].split()
        result.append(ChangedFile(path=path, status=parts[4][0], mode=parts[1]))
    return result


def _forbidden(path: str, patterns: Sequence[str]) -> str | None:
    posix = PurePosixPath(path)
    for pattern in patterns:
        if pattern.endswith("/"):
            prefix = pattern.rstrip("/")
            if path == prefix or path.startswith(prefix + "/") or f"/{prefix}/" in f"/{path}":
                return pattern
            continue
        if fnmatch.fnmatchcase(posix.name, pattern) or fnmatch.fnmatchcase(path, pattern):
            return pattern
    return None


def check_changed_files(
    copy: Path,
    files: Sequence[ChangedFile],
    *,
    forbidden: Sequence[str],
    max_file_kib: int,
) -> list[str]:
    """Problems that stop a push: forbidden names, large files, submodule links and
    symbolic links that point outside the copy. Uses lstat, never follows links."""
    problems: list[str] = []
    limit = max_file_kib * 1024
    for item in files:
        if item.status == "D":
            continue
        if any(part == ".git" for part in PurePosixPath(item.path).parts):
            problems.append(f"{item.path}: a path inside a .git folder")
            continue
        pattern = _forbidden(item.path, forbidden)
        if pattern is not None:
            problems.append(f"{item.path}: matches github.forbidden_files pattern {pattern!r}")
        if item.mode == MODE_GITLINK:
            problems.append(f"{item.path}: a submodule link")
            continue
        full = copy / item.path
        try:
            info = os.lstat(full)
        except FileNotFoundError:
            problems.append(f"{item.path}: missing from the copy")
            continue
        if item.mode == MODE_SYMLINK:
            target = os.readlink(full)
            resolved = os.path.normpath(os.path.join(os.path.dirname(item.path), target))
            if os.path.isabs(target) or resolved == ".." or resolved.startswith("../"):
                problems.append(f"{item.path}: a symbolic link that points outside the repository")
            continue
        if info.st_size > limit:
            problems.append(
                f"{item.path}: {info.st_size // 1024} KiB, more than github.max_file_kib "
                f"({max_file_kib})"
            )
    return problems


def diff_text(copy: Path, base: str, head: str) -> str:
    """The full patch from base to head, binary files named only."""
    return git(copy, "diff", "--no-color", "--no-ext-diff", "--no-textconv", base, head)


def diff_stat(copy: Path, base: str, head: str) -> str:
    return git(copy, "diff", "--no-color", "--stat=100", base, head).strip()


def commit_messages(copy: Path, base: str, head: str) -> str:
    return git(copy, "log", "--format=%B", f"{base}..{head}")


def added_lines(patch: str) -> str:
    """Only the added lines of a patch, and the new file names."""
    lines = []
    for line in patch.splitlines():
        if line.startswith("+++ "):
            lines.append(line[4:])
        elif line.startswith("+"):
            lines.append(line[1:])
    return "\n".join(lines)
