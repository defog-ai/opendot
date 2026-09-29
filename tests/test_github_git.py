"""Host git helpers, the private-text check and repository command containers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from conftest import FakeDocker
from opendot.config import Config
from opendot.github import git as g
from opendot.github.containers import run_repository_commands
from opendot.github.public_text import find_private_text

IDENTITY = g.Identity("Bot", "bot@example.com", "Bot", "bot@example.com")


def plain_git(cwd: Path, *args: str) -> str:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(cwd),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    ident = ["-c", "user.name=Test", "-c", "user.email=test@example.com"]
    return subprocess.run(
        ["git", *ident, "-C", str(cwd), *args],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    plain_git(path, "init", "--quiet", "--initial-branch", "main")
    (path / "README.md").write_text("hello\n")
    plain_git(path, "add", "README.md")
    plain_git(path, "commit", "--quiet", "-m", "first")
    plain_git(path, "checkout", "--quiet", "-b", "work")
    return path


def test_commit_worktree_commits_edits_and_skips_when_unchanged(repo: Path) -> None:
    base = g.head_sha(repo)
    (repo / "README.md").write_text("hello\nworld\n")
    (repo / "new.txt").write_text("new\n")
    sha, made = g.commit_worktree(repo, "work", "Add a line", IDENTITY)
    assert made and sha != base
    assert plain_git(repo, "log", "-1", "--format=%an <%ae>%n%s").split("\n")[:2] == [
        "Bot <bot@example.com>",
        "Add a line",
    ]
    assert plain_git(repo, "status", "--porcelain") == ""
    files = {f.path: f.status for f in g.changed_files(repo, base, sha)}
    assert files == {"README.md": "M", "new.txt": "A"}
    again, made_again = g.commit_worktree(repo, "work", "Nothing", IDENTITY)
    assert (again, made_again) == (sha, False)


def test_commit_worktree_runs_no_hooks(repo: Path) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    flag = repo.parent / "hook-ran"
    hook.write_text(f"#!/bin/sh\ntouch {flag}\n")
    hook.chmod(0o755)
    (repo / "a.txt").write_text("a\n")
    g.commit_worktree(repo, "work", "msg", IDENTITY)
    assert not flag.exists()


def test_commit_worktree_refuses_wrong_branch(repo: Path) -> None:
    (repo / "a.txt").write_text("a\n")
    with pytest.raises(g.GitError, match="not refs/heads/other"):
        g.commit_worktree(repo, "other", "msg", IDENTITY)


def test_worktree_tree_refuses_nested_git(repo: Path) -> None:
    (repo / "vendor" / ".git").mkdir(parents=True)
    with pytest.raises(g.GitError, match="nested .git"):
        g.worktree_tree(repo)
    assert g.find_nested_git(repo) == ["vendor/.git"]


def test_worktree_tree_leaves_the_index_alone(repo: Path) -> None:
    (repo / "a.txt").write_text("a\n")
    before = plain_git(repo, "status", "--porcelain")
    tree = g.worktree_tree(repo)
    assert tree != g.head_tree(repo)
    assert plain_git(repo, "status", "--porcelain") == before
    assert not list((repo / ".git").glob("opendot-index-*"))


def test_check_changed_files(repo: Path) -> None:
    base = g.head_sha(repo)
    (repo / ".env").write_text("X=1\n")
    (repo / "big.bin").write_bytes(b"x" * 3000)
    (repo / "keys").mkdir()
    (repo / "keys" / "server.pem").write_text("pem\n")
    os.symlink("/etc/passwd", repo / "outside")
    os.symlink("../../x", repo / "keys" / "up")
    os.symlink("README.md", repo / "inside")
    sha, _ = g.commit_worktree(repo, "work", "stuff", IDENTITY)
    files = g.changed_files(repo, base, sha)
    problems = g.check_changed_files(
        repo, files, forbidden=[".env", "*.pem", "secrets/"], max_file_kib=2
    )
    text = "\n".join(problems)
    assert ".env: matches" in text
    assert "keys/server.pem: matches" in text
    assert "big.bin: 2 KiB, more than" in text
    assert "outside: a symbolic link" in text
    assert "keys/up: a symbolic link" in text
    assert "inside" not in text.replace("outside", "")
    assert len(problems) == 5


def test_forbidden_folder_pattern() -> None:
    assert g._forbidden("a/secrets/x.txt", ["secrets/"]) == "secrets/"
    assert g._forbidden("secrets/x.txt", ["secrets/"]) == "secrets/"
    assert g._forbidden("mysecrets/x.txt", ["secrets/"]) is None


def test_auth_env_keeps_token_out_of_arguments() -> None:
    env = g.auth_env("tok123", "github.com")
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert "tok123" not in env["GIT_CONFIG_VALUE_0"]  # base64 of x-access-token:tok123
    assert g.auth_env(None, "github.com") == {}


def test_added_lines_only_new_text() -> None:
    patch = "--- a/f\n+++ b/f\n-old secret\n+new line\n context\n"
    assert g.added_lines(patch) == "b/f\nnew line"


# -- private text ----------------------------------------------------------


def test_find_private_text() -> None:
    home = "/" + "home/alice/project"
    assert find_private_text(f"path {home}") == ["a home-folder path (/" + "home/...)"]
    assert find_private_text("/" + "home/user/x and /" + "Users/you/y") == []
    assert find_private_text("mail " + "alice" + "@" + "corp.io") == [
        "an email address (alice@...)"
    ]
    assert find_private_text("a@example.com 1+bot" + "@users.noreply.github.com") == []
    assert find_private_text("token " + "ghp_" + "a" * 36) == ["text shaped like a key or token"]
    link = "https://" + "claude.ai/code/" + "session_" + "abc"
    assert find_private_text(link) == ["a link to a private coding session"]
    assert find_private_text("Project Falcon notes", ["Falcon"]) == [
        "a github.private_markers entry (Falcon)"
    ]
    assert find_private_text("plain text") == []


# -- containers ------------------------------------------------------------


def make_config(tmp_path: Path, **sections: object) -> Config:
    data = {"core": {"state_root": str(tmp_path / "state")}, **sections}
    cfg = Config.from_dict(data, env={})
    cfg.ensure_directories()
    return cfg


def make_copy(cfg: Config) -> Path:
    copy = cfg.worktrees_dir / "task-1" / "widget"
    (copy / ".git").mkdir(parents=True)
    return copy


def test_run_repository_commands_arguments(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    copy = make_copy(cfg)
    docker = FakeDocker()
    docker.script(0, "ok\n", "")
    docker.script(3, "", "tests failed\n")
    outcomes = run_repository_commands(
        cfg,
        docker,
        repository="widget",
        copy=copy,
        commands=["make lint", "make test", "never"],
        network="none",
        timeout_seconds=60,
        purpose="check",
    )
    assert [(o.command, o.exit_code) for o in outcomes] == [("make lint", 0), ("make test", 3)]
    assert "tests failed" in outcomes[1].output_tail
    args = docker.runs[0].args
    assert args[args.index("--network") + 1] == "none"
    assert args[args.index("--workdir") + 1] == "/opendot/repos/widget"
    assert args[-3:] == ["sh", "-c", "make lint"]
    mounts = " ".join(a for a in args if a.startswith("type=bind"))
    assert f"src={copy},dst=/opendot/repos/widget" in mounts
    assert f"src={copy / '.git'},dst=/opendot/repos/widget/.git,readonly" in mounts
    assert "/work" not in args
    envs = [args[i + 1] for i, a in enumerate(args) if a == "--env"]
    assert envs == ["HOME=/opendot/home"]


def test_run_repository_commands_stops_container_on_timeout(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    copy = make_copy(cfg)

    class SlowDocker(FakeDocker):
        def run(self, args, *, input=None, env=None, timeout=None):
            result = super().run(args, input=input, env=env, timeout=timeout)
            if "run" in args[:2]:
                raise subprocess.TimeoutExpired(args, timeout or 0)
            return result

    docker = SlowDocker()
    outcomes = run_repository_commands(
        cfg,
        docker,
        repository="widget",
        copy=copy,
        commands=["sleep 999"],
        network="none",
        timeout_seconds=1,
        purpose="check",
    )
    assert outcomes[0].exit_code == 124
    name = docker.runs[0].args[docker.runs[0].args.index("--name") + 1]
    assert docker.runs[1].args[-2:] == ["kill", name]


def test_run_repository_commands_refuses_copy_outside_worktrees(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / ".git").mkdir(parents=True)
    with pytest.raises(Exception, match="inside"):
        run_repository_commands(
            cfg,
            FakeDocker(),
            repository="widget",
            copy=elsewhere,
            commands=["true"],
            network="none",
            timeout_seconds=5,
            purpose="check",
        )
