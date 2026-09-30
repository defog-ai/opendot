"""The GitHub step extension, the four action handlers and the CLI.

Uses a real git repository with a bare remote on disk, FakeDocker for the
check containers and httpx.MockTransport for the GitHub API.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from conftest import FakeDocker
from opendot.actions import ActionContext, ActionRegistry, InvalidProposal, build_registry
from opendot.backends import CommandResult
from opendot.config import Config
from opendot.extensions import ExtensionError, StepContext
from opendot.github import cli as github_cli
from opendot.github import git as g
from opendot.github.actions import (
    IssueCommentHandler,
    IssueHandler,
    OpenPullRequestHandler,
    PushBranchHandler,
    action_handlers,
    marker_comment,
)
from opendot.github.containers import safe_directory_env
from opendot.github.step import RepositoryCopies, step_extensions
from opendot.models import PublicationKind, PublicationState, Step, StepPlan, TaskState
from opendot.sandbox import check_plan_env
from opendot.store import open_store

TOKEN = "tok-" + "0123456789abcdef"
ENV = {"OPENDOT_GITHUB_TOKEN": TOKEN}
REMOTE = "https://github.com/acme/widget.git"


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
    ).stdout.strip()


class ImageDocker(FakeDocker):
    """FakeDocker that answers `docker image inspect` with image_id and records the
    question in inspects, not in runs, so scripted results stay for the containers."""

    def __init__(self) -> None:
        super().__init__()
        self.image_id: str | None = "sha256:" + "a" * 64
        self.inspects = 0

    def run(self, args, *, input=None, env=None, timeout=None):
        if list(args[1:3]) == ["image", "inspect"]:
            self.inspects += 1
            if self.image_id is None:
                return CommandResult(list(args), 1, "", "No such image")
            return CommandResult(list(args), 0, self.image_id + "\n", "")
        return super().run(args, input=input, env=env, timeout=timeout)


def pull_fields(branch: str, *, owner: str = "acme", repo_id: int = 1) -> dict[str, Any]:
    """The head and base of a pull request on acme/widget (repository id 1)."""
    return {
        "label": f"{owner}:{branch}",
        "head": {"ref": branch, "repo": {"id": repo_id, "full_name": f"{owner}/widget"}},
        "base": {"ref": "main", "repo": {"id": 1, "full_name": "acme/widget"}},
    }


class FakeGitHub:
    """A small in-memory GitHub for one repository, acme/widget."""

    def __init__(self) -> None:
        self.private = False
        self.repo_status = 200
        self.pulls: list[dict[str, Any]] = []
        self.issues: list[dict[str, Any]] = []
        self.comments: list[dict[str, Any]] = []
        self.fail_next_create = 0
        self.requests: list[httpx.Request] = []

    def posts(self) -> list[str]:
        return [r.url.path for r in self.requests if r.method == "POST"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        path = request.url.path
        params = request.url.params
        if path == "/user":
            return httpx.Response(200, json={"id": 7, "login": "opendot-bot"})
        if path == "/repos/acme/widget":
            if self.repo_status != 200:
                return httpx.Response(self.repo_status, json={"message": "nope"})
            return httpx.Response(200, json={"private": self.private})
        if request.method == "POST" and self.fail_next_create:
            self.fail_next_create -= 1
            # GitHub made it, but the answer was lost.
            self._create(path, json.loads(request.content))
            return httpx.Response(502, json={"message": "bad gateway"})
        if path == "/repos/acme/widget/pulls":
            if request.method == "POST":
                return httpx.Response(201, json=self._create(path, json.loads(request.content)))
            if params.get("head"):
                label = params["head"]
                return httpx.Response(200, json=[p for p in self.pulls if p["label"] == label])
            return httpx.Response(200, json=self.pulls)
        if path == "/repos/acme/widget/issues":
            if request.method == "POST":
                return httpx.Response(201, json=self._create(path, json.loads(request.content)))
            return httpx.Response(200, json=self.issues)
        if path.startswith("/repos/acme/widget/issues/") and path.endswith("/comments"):
            if request.method == "POST":
                return httpx.Response(201, json=self._create(path, json.loads(request.content)))
            return httpx.Response(200, json=self.comments)
        if path.startswith("/repos/acme/widget/issues/"):
            number = int(path.rsplit("/", 1)[1])
            return httpx.Response(
                200, json={"number": number, "title": "Crash on start", "state": "open"}
            )
        return httpx.Response(404, json={"message": f"no route {path}"})

    def _create(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        number = 100 + len(self.pulls) + len(self.issues) + len(self.comments)
        item = {
            "id": number,
            "node_id": f"N{number}",
            "number": number,
            "body": body.get("body"),
            "html_url": f"https://github.com/acme/widget/x/{number}",
        }
        item["user"] = {"login": "opendot-bot"}
        if path.endswith("/pulls"):
            item.update(pull_fields(body["head"]))
            self.pulls.append(item)
        elif path.endswith("/comments"):
            self.comments.append(item)
        else:
            self.issues.append(item)
        return item


class Env:
    """One configured repository, a task with its copy, and the handlers."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        checks: list[str],
        public: bool = True,
        plain_visibility: str = "",
        **github,
    ):
        self.tmp_path = tmp_path
        source = tmp_path / "source"
        source.mkdir()
        plain_git(source, "init", "--quiet", "--initial-branch", "main")
        (source / "app.py").write_text("print('hi')\n")
        plain_git(source, "add", "app.py")
        plain_git(source, "commit", "--quiet", "-m", "first")
        self.remote = tmp_path / "remote.git"
        plain_git(tmp_path, "clone", "--quiet", "--bare", str(source), str(self.remote))
        repository: dict[str, Any] = {
            "name": "widget",
            "remote": REMOTE,
            "checks": checks,
            "public": public,
        }
        if plain_visibility:
            # A plain git remote: the bare repository on disk, no GitHub at all.
            repository.update(
                {"remote": str(self.remote), "forge": "none", "visibility": plain_visibility}
            )
        self.config = Config.from_dict(
            {
                "core": {"state_root": str(tmp_path / "state")},
                "backend": {"worker": {"kind": "fake"}},
                "github": {"author_email": "bot@example.com", **github},
                "repositories": [repository],
            },
            env={},
        )
        self.config.ensure_directories()
        self.store = open_store(self.config)
        self.docker = ImageDocker()
        self.github = FakeGitHub()
        self.overrides = {"widget": str(self.remote)}
        self.task = self.store.create_task(
            requester="cli:tester", text="fix it", channel="cli", conversation="c1"
        )
        self.ctx = ActionContext(task=self.task, store=self.store, config=self.config, channels={})

    def handler(self, cls, env: dict[str, str] | None = None):
        return cls(
            self.config,
            runner=self.docker,
            env=ENV if env is None else env,
            transport=httpx.MockTransport(self.github.handler),
            url_overrides=self.overrides,
        )

    def make_copy(self) -> tuple[StepPlan, Path]:
        ext = RepositoryCopies(
            self.config, runner=self.docker, env=ENV, url_overrides=self.overrides
        )
        plan = StepPlan(step_token="abc", run_dir=self.tmp_path / "run")
        ext.before_step(StepContext(self.task, Step.WORK, self.store, self.config, "fake"), plan)
        return plan, self.config.worktrees_dir / f"task-{self.task.id}" / "widget"

    def remote_sha(self, branch: str) -> str:
        return plain_git(self.remote, "rev-parse", f"refs/heads/{branch}")


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path, checks=["make test"])


# -- the step extension -----------------------------------------------------


def test_step_makes_copy_mount_and_note(env: Env) -> None:
    plan, copy = env.make_copy()
    assert (copy / "app.py").read_text() == "print('hi')\n"
    assert plain_git(copy, "symbolic-ref", "HEAD") == f"refs/heads/opendot/task-{env.task.id}"
    [mount] = plan.host_mounts
    assert (mount.host, mount.container, mount.writable) == (copy, "/opendot/repos/widget", True)
    assert "read-only" in plan.prompt_notes[0]
    worktree = env.store.get_worktree(env.task.id, "widget")
    assert worktree.base_sha == env.remote_sha("main")
    assert env.store.get_repository("widget").last_fetched_at is not None
    (copy / "app.py").write_text("changed\n")
    plan2, copy2 = env.make_copy()  # a later step reuses the copy
    assert copy2 == copy and (copy / "app.py").read_text() == "changed\n"


def test_containers_trust_the_repository_folder(tmp_path: Path) -> None:
    # A root host hands the work files to the container user but not the read-only
    # .git folder; without safe.directory git in the container refuses the copy.
    env = Env(tmp_path, checks=[])
    env.config.repositories[0].prepare.append("npm ci")
    plan, _ = env.make_copy()
    assert plan.fixed_env == {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "/opendot/repos/widget",
    }
    assert check_plan_env(plan.fixed_env) == plan.fixed_env
    [prepare] = [r.args for r in env.docker.runs if "npm ci" in r.args]
    assert "GIT_CONFIG_VALUE_0=/opendot/repos/widget" in prepare
    assert "GIT_CONFIG_KEY_0=safe.directory" in prepare


def test_safe_directory_env_keeps_values_already_set() -> None:
    existing = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "a.b", "GIT_CONFIG_VALUE_0": "c"}
    env = safe_directory_env(["/opendot/repos/x", "/opendot/repos/y"], existing)
    assert env["GIT_CONFIG_COUNT"] == "3"
    assert (env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == ("a.b", "c")
    assert env["GIT_CONFIG_VALUE_1"] == "/opendot/repos/x"
    assert env["GIT_CONFIG_VALUE_2"] == "/opendot/repos/y"
    assert safe_directory_env([]) == {}


def test_step_prepare_failure_removes_copy(tmp_path: Path) -> None:
    env = Env(tmp_path, checks=[])
    env.config.repositories[0].prepare.append("npm ci")
    env.docker.script(1, "", "npm failed")
    with pytest.raises(ExtensionError, match="npm failed"):
        env.make_copy()
    assert not (env.config.worktrees_dir / f"task-{env.task.id}" / "widget").exists()
    assert env.store.get_worktree(env.task.id, "widget") is None


def test_feature_functions_return_nothing_without_repositories(tmp_path: Path) -> None:
    cfg = Config.from_dict({"core": {"state_root": str(tmp_path / "s")}}, env={})
    assert action_handlers(cfg) == []
    assert step_extensions(cfg) == []
    assert github_cli.doctor_checks(cfg) == []


def test_handlers_register(env: Env) -> None:
    registry = ActionRegistry(action_handlers(env.config))
    assert registry.kinds() == [
        "github.issue",
        "github.issue_comment",
        "github.open_pr",
        "github.push_branch",
    ]
    assert "github.open_pr" in build_registry(env.config)


# -- push and pull request --------------------------------------------------


def test_push_branch_prepare_and_execute(env: Env, monkeypatch) -> None:
    _, copy = env.make_copy()
    (copy / "app.py").write_text("print('fixed')\n")
    handler = env.handler(PushBranchHandler)
    action = handler.prepare(
        {"kind": "github.push_branch", "repository": "widget", "commit_message": "Fix it"},
        env.ctx,
    )
    payload = action.payload
    assert action.target == f"github:acme/widget:opendot/task-{env.task.id}"
    assert payload["files"] == [{"path": "app.py", "status": "M", "bytes": 15, "binary": False}]
    assert payload["checks"][0]["command"] == "make test"
    assert payload["visibility"] == "public"
    assert "+print('fixed')" in payload["diff"]
    check_args = env.docker.runs[0].args
    assert check_args[check_args.index("--network") + 1] == "none"
    assert plain_git(copy, "log", "-1", "--format=%s") == "Fix it"

    seen: list[tuple[list[str], dict[str, str]]] = []
    real_run = subprocess.run

    def spy(argv, *args, **kwargs):
        seen.append((list(argv), dict(kwargs.get("env") or {})))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(g.subprocess, "run", spy)
    result = handler.execute(action, env.ctx)
    assert result.ok, result.detail
    assert env.remote_sha(payload["branch"]) == payload["commit"]
    push = [(argv, e) for argv, e in seen if "push" in argv]
    assert len(push) == 1
    assert "--force" not in push[0][0] and "-f" not in push[0][0]
    assert all(TOKEN not in arg for argv, _ in seen for arg in argv)
    [pub] = env.store.list_publications(task_id=env.task.id)
    assert (pub.kind, pub.state) == (PublicationKind.BRANCH, PublicationState.PUBLISHED)

    seen.clear()
    again = handler.execute(action, env.ctx)  # a retry does not push again
    assert again.ok and not [argv for argv, _ in seen if "push" in argv]


def test_https_push_carries_token_in_env_only(env: Env, monkeypatch) -> None:
    from opendot.github.step import network_env

    repo = env.config.repositories[0]
    extra = network_env(env.config, repo, ENV)
    assert extra["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert TOKEN not in json.dumps(extra)


def test_prepare_refuses_without_changes(env: Env) -> None:
    env.make_copy()
    with pytest.raises(InvalidProposal, match="no changes"):
        env.handler(PushBranchHandler).prepare(
            {"repository": "widget", "commit_message": "x"}, env.ctx
        )


def test_prepare_refuses_without_token(env: Env) -> None:
    _, copy = env.make_copy()
    (copy / "app.py").write_text("x\n")
    with pytest.raises(InvalidProposal, match="OPENDOT_GITHUB_TOKEN is not set"):
        env.handler(PushBranchHandler, env={}).prepare(
            {"repository": "widget", "commit_message": "x"}, env.ctx
        )


def test_prepare_refuses_unknown_repository(env: Env) -> None:
    with pytest.raises(InvalidProposal, match="unknown repository"):
        env.handler(IssueHandler).prepare({"repository": "other", "title": "t"}, env.ctx)


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (lambda c: (c / "lib" / ".git").mkdir(parents=True), "nested .git"),
        (lambda c: (c / ".env").write_text("X=1\n"), "github.forbidden_files"),
        (lambda c: (c / "big.bin").write_bytes(b"x" * 600 * 1024), "max_file_kib"),
        (lambda c: os.symlink("/etc/passwd", c / "link"), "points outside"),
    ],
)
def test_prepare_refuses_bad_files(env: Env, setup, message: str) -> None:
    _, copy = env.make_copy()
    setup(copy)
    with pytest.raises(InvalidProposal, match=message):
        env.handler(PushBranchHandler).prepare(
            {"repository": "widget", "commit_message": "x"}, env.ctx
        )


def test_prepare_refuses_failed_check(env: Env) -> None:
    _, copy = env.make_copy()
    (copy / "app.py").write_text("x\n")
    env.docker.script(2, "", "1 test failed")
    with pytest.raises(InvalidProposal, match="1 test failed"):
        env.handler(PushBranchHandler).prepare(
            {"repository": "widget", "commit_message": "x"}, env.ctx
        )


def test_prepare_refuses_check_that_changes_files(env: Env) -> None:
    _, copy = env.make_copy()
    (copy / "app.py").write_text("x\n")

    class Formatter(FakeDocker):
        def run(self, args, *, input=None, env=None, timeout=None):
            (copy / "app.py").write_text("x  # formatted\n")
            return super().run(args, input=input, env=env, timeout=timeout)

    handler = env.handler(PushBranchHandler)
    handler._runner = Formatter()
    with pytest.raises(InvalidProposal, match="the checks changed files"):
        handler.prepare({"repository": "widget", "commit_message": "x"}, env.ctx)


def test_checks_are_cached_per_tree(env: Env) -> None:
    _, copy = env.make_copy()
    (copy / "app.py").write_text("x\n")
    handler = env.handler(PushBranchHandler)
    handler.prepare({"repository": "widget", "commit_message": "x"}, env.ctx)
    handler.prepare({"repository": "widget", "commit_message": "x"}, env.ctx)
    assert len(env.docker.runs) == 1


def test_check_cache_is_tied_to_the_image_content(env: Env) -> None:
    _, copy = env.make_copy()
    (copy / "app.py").write_text("x\n")
    handler = env.handler(PushBranchHandler)
    handler.prepare({"repository": "widget", "commit_message": "x"}, env.ctx)
    env.docker.image_id = "sha256:" + "b" * 64  # the image was rebuilt under the same name
    handler.prepare({"repository": "widget", "commit_message": "x"}, env.ctx)
    assert len(env.docker.runs) == 2


def test_checks_are_not_cached_when_the_image_id_is_unknown(env: Env) -> None:
    _, copy = env.make_copy()
    (copy / "app.py").write_text("x\n")
    env.docker.image_id = None
    handler = env.handler(PushBranchHandler)
    handler.prepare({"repository": "widget", "commit_message": "x"}, env.ctx)
    handler.prepare({"repository": "widget", "commit_message": "x"}, env.ctx)
    assert len(env.docker.runs) == 2
    assert not list((copy / ".git").glob("opendot-checks-*"))


PRIVATE_PATH = "/" + "home/alice/secret-project"


@pytest.mark.parametrize("attributes", ["* -diff\n", "*.txt binary\n"])
def test_public_push_screens_text_that_gitattributes_marks_binary(
    tmp_path: Path, attributes: str
) -> None:
    env = Env(tmp_path, checks=[])
    _, copy = env.make_copy()
    (copy / ".gitattributes").write_text(attributes)
    (copy / "notes.txt").write_text(f"see {PRIVATE_PATH}\n")
    with pytest.raises(InvalidProposal, match="home-folder path"):
        env.handler(PushBranchHandler).prepare(
            {"repository": "widget", "commit_message": "x"}, env.ctx
        )


def test_public_push_screens_file_with_nul_byte(tmp_path: Path) -> None:
    env = Env(tmp_path, checks=[], allow_binary_public=True)
    _, copy = env.make_copy()
    (copy / "notes.txt").write_bytes(b"\0\0" + f"see {PRIVATE_PATH}\n".encode())
    with pytest.raises(InvalidProposal, match="home-folder path"):
        env.handler(PushBranchHandler).prepare(
            {"repository": "widget", "commit_message": "x"}, env.ctx
        )


def test_public_push_refuses_binary_files_by_default(tmp_path: Path) -> None:
    env = Env(tmp_path, checks=[])
    _, copy = env.make_copy()
    (copy / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR")
    with pytest.raises(InvalidProposal, match="binary files.*logo.png.*allow_binary_public"):
        env.handler(PushBranchHandler).prepare(
            {"repository": "widget", "commit_message": "x"}, env.ctx
        )


def test_binary_files_are_listed_with_sizes_and_left_out_of_the_diff(tmp_path: Path) -> None:
    env = Env(tmp_path, checks=[])
    env.github.private = True
    _, copy = env.make_copy()
    (copy / "logo.png").write_bytes(b"\x89PNG\0\0\0" + b"z" * 100)
    (copy / "app.py").write_text("print('fixed')\n")
    action = env.handler(PushBranchHandler).prepare(
        {"repository": "widget", "commit_message": "x"}, env.ctx
    )
    payload = action.payload
    assert {"path": "logo.png", "status": "A", "bytes": 107, "binary": True} in payload["files"]
    assert "logo.png" not in payload["diff"]
    assert "print('fixed')" in payload["diff"]
    assert any("logo.png (added, 107 bytes)" in note for note in payload["host_notes"])


def test_gitattributes_cannot_hide_a_diff(tmp_path: Path) -> None:
    env = Env(tmp_path, checks=[])
    _, copy = env.make_copy()
    (copy / ".gitattributes").write_text("*.py -diff\n")
    (copy / "app.py").write_text("print('changed')\n")
    action = env.handler(PushBranchHandler).prepare(
        {"repository": "widget", "commit_message": "x"}, env.ctx
    )
    payload = action.payload
    assert "+print('changed')" in payload["diff"]
    assert "Binary files" not in payload["diff"]
    assert "app.py | +1 -1" in payload["diff_stat"]
    assert payload["gitattributes"] == {".gitattributes": "*.py -diff\n"}
    assert any(".gitattributes" in note for note in payload["host_notes"])


def test_a_diff_longer_than_the_limit_is_refused_not_cut(tmp_path: Path) -> None:
    env = Env(tmp_path, checks=[], max_diff_chars=300)
    _, copy = env.make_copy()
    (copy / "app.py").write_text("print('x')\n" * 100)
    with pytest.raises(InvalidProposal, match="more than github.max_diff_chars"):
        env.handler(PushBranchHandler).prepare(
            {"repository": "widget", "commit_message": "x"}, env.ctx
        )


def test_diff_commands_never_run_a_configured_diff_program(env: Env) -> None:
    _, copy = env.make_copy()
    marker = env.tmp_path / "ran"
    plain_git(copy, "config", "diff.external", f"touch {marker}")
    plain_git(copy, "config", "diff.show.textconv", f"touch {marker}; cat")
    (copy / ".gitattributes").write_text("* diff=show\n")
    (copy / "app.py").write_text("print('changed')\n")
    commit, _ = g.commit_worktree(
        copy,
        f"opendot/task-{env.task.id}",
        "x",
        g.Identity("a", "a@example.com", "a", "a@example.com"),
    )
    base = env.store.get_worktree(env.task.id, "widget").base_sha
    assert "app.py" in g.diff_stat(copy, base, commit)
    assert "+print('changed')" in g.diff_text(copy, base, commit)
    assert not marker.exists()


def test_public_repository_refuses_private_text(env: Env) -> None:
    _, copy = env.make_copy()
    (copy / "notes.txt").write_text("see /" + "home/alice/secret-project\n")
    with pytest.raises(InvalidProposal, match="home-folder path"):
        env.handler(PushBranchHandler).prepare(
            {"repository": "widget", "commit_message": "x"}, env.ctx
        )
    assert env.store.get_repository("widget").visibility.value == "public"


def test_public_repository_refused_when_not_allowed(tmp_path: Path) -> None:
    env = Env(tmp_path, checks=[], public=False)
    with pytest.raises(InvalidProposal, match="repositories.public = false"):
        env.handler(IssueHandler).prepare({"repository": "widget", "title": "t"}, env.ctx)
    env.github.private = True
    action = env.handler(IssueHandler).prepare({"repository": "widget", "title": "t"}, env.ctx)
    assert action.payload["visibility"] == "private"


def test_private_repository_notes_markers(tmp_path: Path) -> None:
    env = Env(tmp_path, checks=[], private_markers=["Falcon"])
    env.github.private = True
    action = env.handler(IssueHandler).prepare(
        {"repository": "widget", "title": "Falcon bug", "body": "details"}, env.ctx
    )
    assert "private_markers" in action.payload["host_notes"][0]


def test_visibility_lookup_failure_refuses(env: Env) -> None:
    env.github.repo_status = 500
    with pytest.raises(InvalidProposal, match="HTTP 500"):
        env.handler(IssueHandler).prepare({"repository": "widget", "title": "t"}, env.ctx)


def test_execute_refuses_when_copy_changed(env: Env) -> None:
    _, copy = env.make_copy()
    (copy / "app.py").write_text("x\n")
    handler = env.handler(PushBranchHandler)
    action = handler.prepare({"repository": "widget", "commit_message": "x"}, env.ctx)
    (copy / "app.py").write_text("sneaky\n")
    result = handler.execute(action, env.ctx)
    assert not result.ok and "prepare it again" in result.detail["error"]
    assert plain_git(env.remote, "branch", "--list", "opendot/*") == ""


def test_execute_refuses_when_visibility_changed(env: Env) -> None:
    env.github.private = True
    _, copy = env.make_copy()
    (copy / "app.py").write_text("x\n")
    handler = env.handler(PushBranchHandler)
    action = handler.prepare({"repository": "widget", "commit_message": "x"}, env.ctx)
    env.github.private = False
    result = handler.execute(action, env.ctx)
    assert not result.ok and "now public" in result.detail["error"]


def test_open_pr_creates_once_and_later_commits_reuse_it(env: Env) -> None:
    _, copy = env.make_copy()
    (copy / "app.py").write_text("v1\n")
    handler = env.handler(OpenPullRequestHandler)
    proposal = {"repository": "widget", "title": "Fix start", "body": "Details."}
    first = handler.prepare(proposal, env.ctx)
    assert first.payload["commit_message"] == "Fix start"
    assert first.payload["body"].endswith(marker_comment(first.payload["marker"]))
    result = handler.execute(first, env.ctx)
    assert result.ok and result.detail["number"] == 100
    assert not result.detail["existing_pull_request"]
    assert env.github.posts() == ["/repos/acme/widget/pulls"]

    (copy / "app.py").write_text("v2\n")
    second = handler.prepare(proposal, env.ctx)
    assert second.payload["marker"] == first.payload["marker"]
    result = handler.execute(second, env.ctx)
    assert result.ok and result.detail["number"] == 100
    assert env.remote_sha(second.payload["branch"]) == second.payload["commit"]
    assert env.github.posts() == ["/repos/acme/widget/pulls"]


def test_open_pr_finds_pull_made_before_a_lost_answer(env: Env) -> None:
    _, copy = env.make_copy()
    (copy / "app.py").write_text("v1\n")
    handler = env.handler(OpenPullRequestHandler)
    action = handler.prepare({"repository": "widget", "title": "Fix"}, env.ctx)
    env.github.fail_next_create = 1
    assert not handler.execute(action, env.ctx).ok
    result = handler.execute(action, env.ctx)
    assert result.ok and result.detail["existing_pull_request"]
    assert len(env.github.pulls) == 1


def test_open_pr_finds_pull_by_marker(env: Env) -> None:
    _, copy = env.make_copy()
    (copy / "app.py").write_text("v1\n")
    handler = env.handler(OpenPullRequestHandler)
    action = handler.prepare({"repository": "widget", "title": "Fix"}, env.ctx)
    # The head search misses it (the owner was renamed), the marker search finds it.
    env.github.pulls.append(
        {
            "id": 5,
            "node_id": "N5",
            "number": 5,
            "body": action.payload["body"],
            "user": {"login": "opendot-bot"},
            **pull_fields(action.payload["branch"], owner="old-name"),
        }
    )
    result = handler.execute(action, env.ctx)
    assert result.ok and result.detail["number"] == 5
    assert env.github.posts() == []


def test_open_pr_refuses_pull_on_the_branch_by_another_user(env: Env) -> None:
    _, copy = env.make_copy()
    (copy / "app.py").write_text("v1\n")
    handler = env.handler(OpenPullRequestHandler)
    action = handler.prepare({"repository": "widget", "title": "Fix"}, env.ctx)
    env.github.pulls.append(
        {
            "id": 5,
            "number": 5,
            "body": "mine now",
            "user": {"login": "mallory"},
            **pull_fields(action.payload["branch"]),
        }
    )
    result = handler.execute(action, env.ctx)
    assert not result.ok and "does not take it over" in result.detail["error"]
    assert env.github.posts() == []


@pytest.mark.parametrize(
    ("login", "repo_id", "branch"),
    [("mallory", 1, None), ("opendot-bot", 2, None), ("opendot-bot", 1, "other")],
)
def test_open_pr_ignores_marked_pull_it_did_not_open(
    env: Env, login: str, repo_id: int, branch: str | None
) -> None:
    _, copy = env.make_copy()
    (copy / "app.py").write_text("v1\n")
    handler = env.handler(OpenPullRequestHandler)
    action = handler.prepare({"repository": "widget", "title": "Fix"}, env.ctx)
    env.github.pulls.append(
        {
            "id": 5,
            "number": 5,
            "body": action.payload["body"],  # a copy of the marker
            "user": {"login": login},
            **pull_fields(branch or action.payload["branch"], owner="fork", repo_id=repo_id),
        }
    )
    result = handler.execute(action, env.ctx)
    assert result.ok and result.detail["number"] != 5
    assert not result.detail["existing_pull_request"]
    assert env.github.posts() == ["/repos/acme/widget/pulls"]


# -- issues and comments ----------------------------------------------------


def test_issue_retry_after_lost_answer_does_not_duplicate(env: Env) -> None:
    handler = env.handler(IssueHandler)
    action = handler.prepare(
        {"repository": "widget", "title": "Crash", "body": "Steps.", "labels": ["bug"]}, env.ctx
    )
    assert action.target == "github:acme/widget:issues"
    env.github.fail_next_create = 1
    failed = handler.execute(action, env.ctx)
    assert not failed.ok and "HTTP 502" in failed.detail["error"]
    result = handler.execute(action, env.ctx)
    assert result.ok and result.detail["existing"]
    assert len(env.github.issues) == 1
    again = handler.execute(action, env.ctx)
    assert again.ok and len(env.github.posts()) == 1
    search = [
        r for r in env.github.requests if r.url.path.endswith("/issues") and r.method == "GET"
    ]
    assert search[0].url.params["creator"] == "opendot-bot"


def test_issue_made_by_another_user_with_the_marker_is_not_reused(env: Env) -> None:
    handler = env.handler(IssueHandler)
    action = handler.prepare({"repository": "widget", "title": "Crash"}, env.ctx)
    env.github.issues.append(
        {"id": 5, "number": 5, "body": action.payload["body"], "user": {"login": "mallory"}}
    )
    result = handler.execute(action, env.ctx)
    assert result.ok and not result.detail["existing"] and result.detail["number"] != 5
    assert env.github.posts() == ["/repos/acme/widget/issues"]


def test_comment_made_by_another_user_with_the_marker_is_not_reused(env: Env) -> None:
    handler = env.handler(IssueCommentHandler)
    action = handler.prepare({"repository": "widget", "number": 12, "body": "Fixed."}, env.ctx)
    env.github.comments.append(
        {"id": 5, "body": action.payload["body"], "user": {"login": "mallory"}}
    )
    result = handler.execute(action, env.ctx)
    assert result.ok and not result.detail["existing"]
    assert env.github.posts() == ["/repos/acme/widget/issues/12/comments"]


def test_issue_comment(env: Env) -> None:
    handler = env.handler(IssueCommentHandler)
    action = handler.prepare({"repository": "widget", "number": "12", "body": "Fixed."}, env.ctx)
    assert action.target == "github:acme/widget#12"
    assert "Crash on start" in action.payload["host_notes"][0]
    result = handler.execute(action, env.ctx)
    assert result.ok and not result.detail["existing"]
    assert handler.execute(action, env.ctx).detail["existing"]
    assert env.github.posts() == ["/repos/acme/widget/issues/12/comments"]
    with pytest.raises(InvalidProposal, match="positive whole number"):
        handler.prepare({"repository": "widget", "number": 0, "body": "x"}, env.ctx)


def test_token_never_in_payload(env: Env) -> None:
    action = env.handler(IssueHandler).prepare({"repository": "widget", "title": "t"}, env.ctx)
    assert TOKEN not in json.dumps(action.payload)


# -- CLI --------------------------------------------------------------------


def test_cli_prune_removes_copies_of_finished_tasks(env: Env, monkeypatch, capsys) -> None:
    _, copy = env.make_copy()
    monkeypatch.setattr(github_cli, "_open", lambda args: (env.config, env.store))
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    github_cli.register_cli(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["github", "prune"])
    assert args.handler(args) == 0
    assert copy.exists()  # the task is still queued
    env.store.set_task_state(env.task.id, TaskState.DONE)
    assert args.handler(args) == 0
    assert not copy.exists()
    assert env.store.list_worktrees(task_id=env.task.id)[0].status.value == "removed"
    args = parser.parse_args(["github", "copies", "--all"])
    assert args.handler(args) == 0
    assert "removed" in capsys.readouterr().out


def test_doctor_checks(env: Env, monkeypatch) -> None:
    # The token is checked by `opendot doctor` with the other saved logins.
    monkeypatch.delenv("OPENDOT_GITHUB_TOKEN", raising=False)
    checks = {name: ok for name, ok, _ in github_cli.doctor_checks(env.config)}
    assert checks == {
        "github: git": True,
        "github: widget remote": True,
    }


# -- plain git remotes (forge = "none") --------------------------------------


def no_github(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"a plain git repository must not call GitHub: {request.url}")


def plain_handler(env: Env, cls):
    return cls(
        env.config,
        runner=env.docker,
        env={},  # no GitHub token at all
        transport=httpx.MockTransport(no_github),
    )


def test_plain_git_push_branch_needs_no_github(tmp_path: Path) -> None:
    env = Env(tmp_path, checks=[], plain_visibility="private")
    plan, copy = env.make_copy()
    assert "plain git remotes, not on GitHub: widget" in plan.prompt_notes[-1]
    (copy / "app.py").write_text("print('fixed')\n")
    handler = plain_handler(env, PushBranchHandler)
    action = handler.prepare({"repository": "widget", "commit_message": "Fix it"}, env.ctx)
    payload = action.payload
    assert action.target == f"git:widget:opendot/task-{env.task.id}"
    assert (payload["slug"], payload["visibility"]) == ("git/widget", "private")
    assert plain_git(copy, "log", "-1", "--format=%ae") == "bot@example.com"
    result = handler.execute(action, env.ctx)
    assert result.ok, result.detail
    assert env.remote_sha(payload["branch"]) == payload["commit"]
    assert result.detail["url"] is None
    [pub] = env.store.list_publications(task_id=env.task.id)
    assert (pub.kind, pub.state) == (PublicationKind.BRANCH, PublicationState.PUBLISHED)


@pytest.mark.parametrize("cls", [OpenPullRequestHandler, IssueHandler, IssueCommentHandler])
def test_plain_git_refuses_github_only_actions(tmp_path: Path, cls) -> None:
    env = Env(tmp_path, checks=[], plain_visibility="private")
    env.make_copy()
    proposal = {"repository": "widget", "title": "t", "body": "b", "number": 1}
    with pytest.raises(InvalidProposal, match="forge = 'none'"):
        plain_handler(env, cls).prepare(proposal, env.ctx)


def test_plain_git_public_follows_the_public_setting(tmp_path: Path) -> None:
    env = Env(tmp_path, checks=[], public=False, plain_visibility="public")
    _, copy = env.make_copy()
    (copy / "app.py").write_text("x\n")
    with pytest.raises(InvalidProposal, match="public by its configuration"):
        plain_handler(env, PushBranchHandler).prepare(
            {"repository": "widget", "commit_message": "x"}, env.ctx
        )


def test_plain_git_execute_refuses_when_stated_visibility_changed(tmp_path: Path) -> None:
    env = Env(tmp_path, checks=[], plain_visibility="private")
    _, copy = env.make_copy()
    (copy / "app.py").write_text("x\n")
    handler = plain_handler(env, PushBranchHandler)
    action = handler.prepare({"repository": "widget", "commit_message": "x"}, env.ctx)
    action.payload["visibility"] = "public"
    result = handler.execute(action, env.ctx)
    assert not result.ok and "now private" in result.detail["error"]


def test_plain_git_doctor_needs_no_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENDOT_GITHUB_TOKEN", raising=False)
    env = Env(tmp_path, checks=[], plain_visibility="private")
    checks = {name: ok for name, ok, _ in github_cli.doctor_checks(env.config)}
    assert checks == {"github: git": True, "github: widget remote": True}


@pytest.mark.parametrize(
    ("repo", "github", "message"),
    [
        ({"forge": "gitlab"}, {"author_email": "a@example.com"}, "forge must be"),
        ({"forge": "none"}, {"author_email": "a@example.com"}, "visibility must be"),
        ({"visibility": "private"}, {}, "only for forge"),
        ({"forge": "none", "visibility": "private"}, {}, "author_email must be set"),
    ],
)
def test_plain_git_config_is_checked(tmp_path: Path, repo, github, message) -> None:
    from opendot.config import ConfigError

    with pytest.raises(ConfigError, match=message):
        Config.from_dict(
            {
                "core": {"state_root": str(tmp_path / "state")},
                "github": github,
                "repositories": [{"name": "widget", "remote": str(tmp_path / "r.git"), **repo}],
            },
            env={},
        )
