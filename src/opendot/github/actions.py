"""The four GitHub action handlers.

prepare() does the host work that decides what will be published, and records
the exact result in the action's payload:
- push and pull request: commit the copy's files, check the changed files, run
  the repository's checks in a sandbox container, and record the commit, its
  tree hash, the change list, a bounded diff and the check results;
- all four: read the repository's visibility from GitHub and, for a public
  repository, refuse text that looks private (opendot.github.public_text).

execute() runs right after prepare(). It uses only the payload and the store.
Before it pushes it checks that the copy still holds the prepared commit and
tree. Each publication
carries a marker derived from its content; a retry finds the earlier result by
that marker in the store or on GitHub and does not publish twice.

Host-made remarks go under the payload key "host_notes". Everything else in the
payload that came from the model (title, body, commit message, diff) is data.

A repository with forge = "none" is a plain git remote. Only push_branch works
for it; its visibility comes from the configuration, and no GitHub call is made.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import httpx

from opendot.actions import (
    KIND_GITHUB_ISSUE,
    KIND_GITHUB_ISSUE_COMMENT,
    KIND_GITHUB_OPEN_PR,
    KIND_GITHUB_PUSH_BRANCH,
    ActionContext,
    ActionResult,
    InvalidProposal,
    PreparedAction,
)
from opendot.github import git as g
from opendot.github.api import GitHubClient, GitHubError, noreply_email, own_pull
from opendot.github.containers import image_id, run_repository_commands
from opendot.github.public_text import find_private_text
from opendot.github.step import git_url, network_env
from opendot.models import (
    PublicationKind,
    PublicationState,
    RepoVisibility,
    WorktreeStatus,
)
from opendot.redact import redact
from opendot.store import NotFound

if TYPE_CHECKING:
    from opendot.backends import CommandRunner
    from opendot.config import Config, RepositoryConfig
    from opendot.models import Publication, Worktree

__all__ = [
    "IssueCommentHandler",
    "IssueHandler",
    "OpenPullRequestHandler",
    "PushBranchHandler",
    "action_handlers",
    "marker_comment",
    "make_marker",
]

MAX_FILES_LISTED = 300
MAX_GITATTRIBUTES_CHARS = 4_000
MAX_TITLE_CHARS = 256
MAX_BODY_CHARS = 60_000
MAX_COMMIT_MESSAGE_CHARS = 5_000
MAX_LABELS = 10
SEARCH_MARGIN = timedelta(minutes=10)


_STATUS_WORDS = {"A": "added", "M": "changed", "D": "deleted", "T": "type changed"}


def make_marker(kind: str, task_id: int, *parts: object) -> str:
    digest = hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]
    return f"opendot:{kind}:task-{int(task_id)}:{digest}"


def marker_comment(marker: str) -> str:
    return f"<!-- opendot-marker: {marker} -->"


def _text(proposal: Mapping[str, Any], name: str, kind: str, limit: int, required: bool) -> str:
    value = proposal.get(name, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise InvalidProposal(f"{kind}: {name} must be text")
    value = value.strip()
    if required and not value:
        raise InvalidProposal(f"{kind}: {name} is required")
    if len(value) > limit:
        raise InvalidProposal(f"{kind}: {name} is longer than {limit} characters")
    return value


def _utc_iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _GitHubHandler:
    """Shared plumbing. Subclasses set kind, levels, description and the two steps."""

    kind = ""
    outward = True
    description = ""
    allows_plain_git = False  # True only where a forge = "none" repository makes sense

    def __init__(
        self,
        config: Config,
        *,
        runner: CommandRunner | None = None,
        env: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        url_overrides: Mapping[str, str] | None = None,
    ):
        self.config = config
        self._runner = runner
        self.env = env
        self.transport = transport
        self.url_overrides = url_overrides

    # -- helpers -----------------------------------------------------------

    @property
    def runner(self) -> CommandRunner:
        if self._runner is None:
            from opendot.backends import SubprocessRunner

            self._runner = SubprocessRunner()
        return self._runner

    def _token(self) -> str:
        token = self.config.github.token(self.env) if self.config.github else None
        if not token:
            name = self.config.github.token_env if self.config.github else "the token variable"
            raise InvalidProposal(
                f"{self.kind}: the host has no GitHub token ({name} is not set; "
                "run `opendot login github`)"
            )
        return token

    def _client(self) -> GitHubClient:
        return GitHubClient(self.config.github.api_url, self._token(), transport=self.transport)

    @contextlib.contextmanager
    def _client_for(self, repo: RepositoryConfig) -> Iterator[GitHubClient | None]:
        """A GitHub client, or None for a plain git repository (no token needed)."""
        if repo.plain_git:
            yield None
            return
        with self._client() as client:
            yield client

    def _repository(self, proposal: Mapping[str, Any]) -> tuple[RepositoryConfig, str, str]:
        name = proposal.get("repository")
        if not isinstance(name, str) or not name:
            raise InvalidProposal(f"{self.kind}: repository is required")
        repo = self.config.repository(name)
        if repo is None:
            known = ", ".join(r.name for r in self.config.repositories) or "none"
            raise InvalidProposal(f"{self.kind}: unknown repository {name!r} (configured: {known})")
        if repo.plain_git:
            if not self.allows_plain_git:
                raise InvalidProposal(
                    f"{self.kind}: {name} is a plain git remote (forge = 'none'); only "
                    f"{KIND_GITHUB_PUSH_BRANCH} works for it"
                )
            return repo, "git", repo.name
        slug = repo.github_slug(self.config.github.host)
        if slug is None:
            raise InvalidProposal(
                f"{self.kind}: the remote of {name} is not on {self.config.github.host}"
            )
        return repo, slug[0], slug[1]

    def _visibility(
        self,
        ctx: ActionContext,
        client: GitHubClient | None,
        repo: RepositoryConfig,
        owner: str,
        name: str,
    ) -> RepoVisibility:
        """Ask GitHub. Any failure or a missing field refuses (fail closed).

        A plain git repository has no service to ask; the operator's
        repositories.visibility setting is used instead."""
        if repo.plain_git:
            visibility = RepoVisibility(repo.visibility)
        elif client is None:
            raise GitHubError(f"no GitHub client to read the visibility of {owner}/{name}")
        else:
            data = client.repository(owner, name)
            private = data.get("private")
            if not isinstance(private, bool):
                raise GitHubError(f"GitHub did not say whether {owner}/{name} is private")
            visibility = RepoVisibility.PRIVATE if private else RepoVisibility.PUBLIC
        try:
            ctx.store.get_repository(repo.name)
        except NotFound:
            ctx.store.upsert_repository(
                repo.name,
                remote=repo.remote,
                control_path=self.config.repos_dir / repo.name,
                default_branch=repo.default_branch,
            )
        ctx.store.set_repository_visibility(repo.name, visibility)
        return visibility

    def _screen(
        self, repo: RepositoryConfig, visibility: RepoVisibility, text: str, notes: list[str]
    ) -> None:
        """Refuse private-looking text for a public repository; note markers otherwise."""
        markers = self.config.github.private_markers
        if visibility is RepoVisibility.PUBLIC:
            if not repo.public:
                where = "by its configuration" if repo.plain_git else "on GitHub"
                raise InvalidProposal(
                    f"{self.kind}: {repo.name} is public {where}, and the configuration does "
                    "not allow publishing to it while it is public (repositories.public = false)"
                )
            problems = find_private_text(text, markers)
            if problems:
                raise InvalidProposal(
                    f"{self.kind}: {repo.name} is public and the text holds "
                    + "; ".join(problems)
                    + ". Remove it and propose again."
                )
            notes.append(
                "The repository is public. The host found no private-looking text in the "
                "added lines, file names, commit messages, title or body."
            )
        else:
            problems = find_private_text(text, markers)
            if problems:
                notes.append(
                    "The repository is private. The text holds " + "; ".join(problems) + "."
                )

    def _check_visibility_unchanged(
        self,
        ctx: ActionContext,
        client: GitHubClient | None,
        repo: RepositoryConfig,
        payload: dict,
    ) -> None:
        owner, name = payload["slug"].split("/", 1)
        now = self._visibility(ctx, client, repo, owner, name)
        if now.value != payload["visibility"]:
            raise GitHubError(
                f"{payload['slug']} is now {now.value}, not {payload['visibility']} as when the "
                "action was prepared; prepare it again"
            )

    def _publication(
        self, ctx: ActionContext, kind: PublicationKind, payload: dict, branch: str | None = None
    ) -> Publication:
        return ctx.store.start_publication(
            ctx.task.id, kind, payload["slug"], payload["marker"], branch=branch
        )

    def _fail(
        self, ctx: ActionContext, publication: Publication | None, error: str
    ) -> ActionResult:
        error = redact(error, [self.config.github.token(self.env)])
        if publication is not None:
            ctx.store.update_publication(
                publication.id, state=PublicationState.FAILED, error=error[:2000]
            )
        return ActionResult(ok=False, detail={"error": error})

    # -- entry points ------------------------------------------------------

    def prepare(self, proposal: Mapping[str, Any], ctx: ActionContext) -> PreparedAction:
        try:
            return self._prepare(proposal, ctx)
        except (g.GitError, GitHubError, OSError) as exc:
            message = redact(str(exc), [self.config.github.token(self.env)])
            raise InvalidProposal(f"{self.kind}: {message}") from None

    def execute(self, action: PreparedAction, ctx: ActionContext) -> ActionResult:
        try:
            return self._execute(action, ctx)
        except (g.GitError, GitHubError, InvalidProposal, OSError, KeyError) as exc:
            return self._fail(ctx, None, f"{type(exc).__name__}: {exc}")

    def _prepare(self, proposal: Mapping[str, Any], ctx: ActionContext) -> PreparedAction:
        raise NotImplementedError

    def _execute(self, action: PreparedAction, ctx: ActionContext) -> ActionResult:
        raise NotImplementedError


class _BranchHandler(_GitHubHandler):
    """Commit, check and push the task's copy. Base of push_branch and open_pr."""

    def _worktree(self, ctx: ActionContext, repo: RepositoryConfig) -> Worktree:
        worktree = ctx.store.get_worktree(ctx.task.id, repo.name)
        if worktree is None or worktree.status is not WorktreeStatus.ACTIVE:
            raise InvalidProposal(f"{self.kind}: task {ctx.task.id} has no copy of {repo.name}")
        return worktree

    def _identity(self, client: GitHubClient | None) -> g.Identity:
        github = self.config.github
        email = github.author_email
        if not email:
            if client is None:
                raise InvalidProposal(f"{self.kind}: github.author_email is not set")
            email = noreply_email(client.viewer(), github.host)
        return g.Identity(
            author_name=github.author_name,
            author_email=email,
            committer_name=github.committer_name or github.author_name,
            committer_email=github.committer_email or email,
            signing_key=github.signing_key,
        )

    def _checks(self, repo: RepositoryConfig, copy: Path, tree: str) -> list[dict[str, Any]]:
        """Run the checks once per tree and image; the result is cached in the copy's .git
        folder. When Docker cannot report the image's content id, nothing is cached."""
        if not repo.checks:
            return []
        image = image_id(self.config, self.runner)
        cache: Path | None = None
        if image is not None:
            key = hashlib.sha256(
                json.dumps(
                    [repo.checks, repo.check_network, self.config.sandbox.image, image]
                ).encode()
            ).hexdigest()[:12]
            cache = copy / ".git" / f"opendot-checks-{tree}-{key}.json"
            if cache.is_file():
                return json.loads(cache.read_text())
        outcomes = run_repository_commands(
            self.config,
            self.runner,
            repository=repo.name,
            copy=copy,
            commands=repo.checks,
            network=repo.check_network,
            timeout_seconds=self.config.github.check_minutes * 60,
            purpose="check",
        )
        results = [o.as_dict() for o in outcomes]
        if cache is not None and all(o.ok for o in outcomes):
            cache.write_text(json.dumps(results))
        return results

    def _prepare_branch(
        self, proposal: Mapping[str, Any], ctx: ActionContext, default_message: str
    ) -> tuple[RepositoryConfig, dict[str, Any], list[str], str]:
        """Returns (repo, payload, host notes, text to screen)."""
        repo, owner, name = self._repository(proposal)
        worktree = self._worktree(ctx, repo)
        copy = Path(worktree.path)
        message = _text(proposal, "commit_message", self.kind, MAX_COMMIT_MESSAGE_CHARS, False)
        message = message or default_message
        if not message:
            raise InvalidProposal(f"{self.kind}: commit_message is required")
        github = self.config.github
        with self._client_for(repo) as client:
            visibility = self._visibility(ctx, client, repo, owner, name)
            commit, made = g.commit_worktree(copy, worktree.branch, message, self._identity(client))
        if commit == worktree.base_sha:
            raise InvalidProposal(f"{self.kind}: the copy of {repo.name} has no changes to push")
        tree = g.head_tree(copy)
        files = g.changed_files(copy, worktree.base_sha, commit)
        problems = g.check_changed_files(
            copy, files, forbidden=github.forbidden_files, max_file_kib=github.max_file_kib
        )
        if problems:
            raise InvalidProposal(
                f"{self.kind}: the host refuses these files: " + "; ".join(problems)
            )
        checks = self._checks(repo, copy, tree)
        failed = [c for c in checks if c["exit_code"] != 0]
        if failed:
            raise InvalidProposal(
                f"{self.kind}: check {failed[0]['command']!r} exited with "
                f"{failed[0]['exit_code']}. Output (end):\n{failed[0]['output_tail']}"
            )
        if g.worktree_tree(copy) != tree:
            raise InvalidProposal(
                f"{self.kind}: the checks changed files in the copy of {repo.name}. Look at "
                "git status in the copy, keep or undo the changes, and propose again."
            )
        notes: list[str] = []
        if not made:
            notes.append("No new commit: the files already matched the branch's last commit.")
        if not checks:
            notes.append("The repository has no checks configured.")
        blobs = g.blob_info(
            copy, [f.content_sha for f in files], read_limit=github.max_file_kib * 1024
        )
        binary = [f for f in files if f.content_sha in blobs and blobs[f.content_sha].binary]
        added_binary = [f for f in binary if f.status != "D"]
        if added_binary and visibility is RepoVisibility.PUBLIC and not github.allow_binary_public:
            names = ", ".join(f.path for f in added_binary[:20])
            raise InvalidProposal(
                f"{self.kind}: {repo.name} is public and the change adds or edits binary files, "
                f"which the diff cannot show: {names}. Remove them and propose again. The "
                "operator can allow binary files in public repositories with "
                "github.allow_binary_public = true."
            )
        if binary:
            notes.append(
                "These files are binary. The diff leaves them out; only their names and sizes "
                "are shown: "
                + "; ".join(
                    f"{f.path} ({_STATUS_WORDS.get(f.status, f.status)}, "
                    f"{blobs[f.content_sha].size} bytes)"
                    for f in binary[:50]
                )
            )
        # Every added line is screened as text, binary files included, whatever a
        # .gitattributes file in the copy says about them.
        full_patch = g.diff_text(copy, worktree.base_sha, commit)
        screen_text = "\n".join(
            [
                g.added_lines(full_patch),
                *(f.path for f in files),
                g.commit_messages(copy, worktree.base_sha, commit),
            ]
        )
        patch = (
            g.diff_text(copy, worktree.base_sha, commit, leave_out=[f.path for f in binary])
            if binary
            else full_patch
        )
        if len(patch) > github.max_diff_chars:
            raise InvalidProposal(
                f"{self.kind}: the diff is {len(patch)} characters, more than "
                f"github.max_diff_chars ({github.max_diff_chars}). The task log keeps the "
                "whole change, so the host does not cut it. Split the work into smaller "
                "pushes, or ask the operator to raise github.max_diff_chars."
            )
        attributes = {}
        for f in files:
            if PurePosixPath(f.path).name == ".gitattributes" and f.status != "D":
                content = g.git_bytes(copy, "cat-file", "blob", f.new_sha)
                attributes[f.path] = content.decode("utf-8", "replace")[:MAX_GITATTRIBUTES_CHARS]
        if attributes:
            notes.append(
                "The change adds or edits a .gitattributes file ("
                + ", ".join(attributes)
                + "). Such a file can change how git and GitHub treat other files, for "
                "example hide a file's diff on GitHub. Its full new content is under "
                "gitattributes."
            )
        payload: dict[str, Any] = {
            "repository": repo.name,
            "slug": f"{owner}/{name}",
            "visibility": visibility.value,
            "branch": worktree.branch,
            "base_branch": repo.default_branch,
            "base_sha": worktree.base_sha,
            "commit": commit,
            "tree": tree,
            "commit_message": message,
            "files": [
                {
                    "path": f.path,
                    "status": f.status,
                    "bytes": blobs[f.content_sha].size if f.content_sha in blobs else None,
                    "binary": f in binary,
                }
                for f in files[:MAX_FILES_LISTED]
            ],
            "files_total": len(files),
            "diff_stat": g.stat_from_patch(patch)[-4000:],
            "diff": patch,
            "diff_truncated": False,
            "checks": checks,
        }
        if attributes:
            payload["gitattributes"] = attributes
        return repo, payload, notes, screen_text

    def _push(self, ctx: ActionContext, repo: RepositoryConfig, payload: dict) -> Publication:
        """Push payload["commit"] to payload["branch"] once. Returns the branch publication."""
        worktree = self._worktree(ctx, repo)
        copy = Path(worktree.path)
        if worktree.branch != payload["branch"]:
            raise InvalidProposal("the copy is on a different branch than the prepared one")
        if payload["branch"] == repo.default_branch:
            raise InvalidProposal("the host never pushes to the default branch")
        if g.head_sha(copy) != payload["commit"] or g.worktree_tree(copy) != payload["tree"]:
            raise InvalidProposal(
                "the copy changed after this action was prepared; prepare it again"
            )
        marker = make_marker(
            "branch", ctx.task.id, payload["slug"], payload["branch"], payload["commit"]
        )
        publication = ctx.store.start_publication(
            ctx.task.id,
            PublicationKind.BRANCH,
            payload["slug"],
            marker,
            branch=payload["branch"],
        )
        if publication.state is PublicationState.PUBLISHED:
            return publication
        secrets_ = [] if repo.plain_git else [self._token()]
        try:
            g.git(
                copy,
                "push",
                "--porcelain",
                "--no-verify",
                "--",
                git_url(repo, self.url_overrides),
                f"{payload['commit']}:refs/heads/{payload['branch']}",
                env=network_env(self.config, repo, self.env),
                network=True,
                timeout=900,
                secrets_=secrets_,
            )
        except g.GitError as exc:
            ctx.store.update_publication(
                publication.id, state=PublicationState.FAILED, error=str(exc)[:2000]
            )
            raise
        host = self.config.github.host
        url = (
            None if repo.plain_git else f"https://{host}/{payload['slug']}/tree/{payload['branch']}"
        )
        return ctx.store.update_publication(
            publication.id,
            state=PublicationState.PUBLISHED,
            head_sha=payload["commit"],
            url=url,
            error=None,
        )


class PushBranchHandler(_BranchHandler):
    kind = KIND_GITHUB_PUSH_BRANCH
    allows_plain_git = True
    description = (
        "Push your edits in a repository copy to the task's own branch on the repository's "
        "remote (never the default branch, never a force push). The host commits the files, "
        "runs the repository's checks and refuses the push when a check fails. Fields: "
        "repository (the configured name, required), commit_message (required)."
    )

    def _prepare(self, proposal: Mapping[str, Any], ctx: ActionContext) -> PreparedAction:
        repo, payload, notes, text = self._prepare_branch(proposal, ctx, "")
        self._screen(repo, RepoVisibility(payload["visibility"]), text, notes)
        payload["host_notes"] = notes
        payload["marker"] = make_marker(
            "branch", ctx.task.id, payload["slug"], payload["branch"], payload["commit"]
        )
        prefix = f"git:{repo.name}" if repo.plain_git else f"github:{payload['slug']}"
        return PreparedAction(
            kind=self.kind,
            target=f"{prefix}:{payload['branch']}",
            payload=payload,
            outward=True,
        )

    def _execute(self, action: PreparedAction, ctx: ActionContext) -> ActionResult:
        payload = action.payload
        repo, _, _ = self._repository(payload)
        with self._client_for(repo) as client:
            self._check_visibility_unchanged(ctx, client, repo, payload)
        publication = self._push(ctx, repo, payload)
        return ActionResult(
            ok=True,
            detail={
                "branch": payload["branch"],
                "commit": payload["commit"],
                "url": publication.url,
            },
            external_id=payload["commit"],
        )


class OpenPullRequestHandler(_BranchHandler):
    kind = KIND_GITHUB_OPEN_PR
    description = (
        "Push your edits in a repository copy to the task's branch and open a pull request "
        "into the default branch. When the task already has an open pull request, the new "
        "commit is pushed to it. Fields: repository (required), title (required), body, "
        "commit_message (default: the title), draft (true or false, default false)."
    )

    def _prepare(self, proposal: Mapping[str, Any], ctx: ActionContext) -> PreparedAction:
        title = _text(proposal, "title", self.kind, MAX_TITLE_CHARS, True)
        body = _text(proposal, "body", self.kind, MAX_BODY_CHARS, False)
        draft = proposal.get("draft", False)
        if not isinstance(draft, bool):
            raise InvalidProposal(f"{self.kind}: draft must be true or false")
        repo, payload, notes, text = self._prepare_branch(proposal, ctx, title)
        self._screen(repo, RepoVisibility(payload["visibility"]), f"{text}\n{title}\n{body}", notes)
        marker = make_marker("pull_request", ctx.task.id, payload["slug"], payload["branch"])
        payload.update(
            {
                "title": title,
                "body": f"{body}\n\n{marker_comment(marker)}".lstrip(),
                "draft": draft,
                "host_notes": notes,
                "marker": marker,
            }
        )
        return PreparedAction(
            kind=self.kind,
            target=f"github:{payload['slug']}:{payload['branch']}",
            payload=payload,
            outward=True,
        )

    def _execute(self, action: PreparedAction, ctx: ActionContext) -> ActionResult:
        payload = action.payload
        repo, owner, name = self._repository(payload)
        with self._client() as client:
            self._check_visibility_unchanged(ctx, client, repo, payload)
            self._push(ctx, repo, payload)
            publication = self._publication(
                ctx, PublicationKind.PULL_REQUEST, payload, branch=payload["branch"]
            )
            if publication.state is PublicationState.PUBLISHED:
                return self._result(publication, payload, reused=True)
            try:
                login = str(client.viewer()["login"])
                branch = payload["branch"]
                found = client.open_pulls_for_head(owner, name, branch)
                ours = [p for p in found if own_pull(p, branch, login)]
                if found and not ours:
                    return self._fail(
                        ctx,
                        publication,
                        f"an open pull request from branch {branch} exists, but {login} did not "
                        f"open it from {payload['slug']}; the host does not take it over",
                    )
                pull = ours[0] if ours else None
                if pull is None:
                    marked = client.find_open_pull_with(owner, name, payload["marker"])
                    if marked is not None and own_pull(marked, branch, login):
                        pull = marked
                reused = pull is not None
                if pull is None:
                    pull = client.create_pull(
                        owner,
                        name,
                        title=payload["title"],
                        body=payload["body"],
                        head=payload["branch"],
                        base=payload["base_branch"],
                        draft=payload["draft"],
                    )
            except GitHubError as exc:
                return self._fail(ctx, publication, str(exc))
        publication = ctx.store.update_publication(
            publication.id,
            state=PublicationState.PUBLISHED,
            head_sha=payload["commit"],
            number=int(pull["number"]),
            url=pull.get("html_url"),
            external_id=str(pull.get("node_id") or pull.get("id") or ""),
            error=None,
        )
        return self._result(publication, payload, reused=reused)

    def _result(self, publication: Publication, payload: dict, *, reused: bool) -> ActionResult:
        return ActionResult(
            ok=True,
            detail={
                "number": publication.number,
                "url": publication.url,
                "commit": payload["commit"],
                "existing_pull_request": reused,
            },
            external_id=publication.external_id,
        )


class IssueHandler(_GitHubHandler):
    kind = KIND_GITHUB_ISSUE
    description = (
        "Open an issue in a configured repository. Fields: repository (required), title "
        "(required), body, labels (a list of existing label names)."
    )

    def _prepare(self, proposal: Mapping[str, Any], ctx: ActionContext) -> PreparedAction:
        repo, owner, name = self._repository(proposal)
        title = _text(proposal, "title", self.kind, MAX_TITLE_CHARS, True)
        body = _text(proposal, "body", self.kind, MAX_BODY_CHARS, False)
        labels = proposal.get("labels", []) or []
        if not isinstance(labels, list) or not all(
            isinstance(label, str) and label.strip() for label in labels
        ):
            raise InvalidProposal(f"{self.kind}: labels must be a list of label names")
        if len(labels) > MAX_LABELS:
            raise InvalidProposal(f"{self.kind}: at most {MAX_LABELS} labels")
        labels = [label.strip() for label in labels]
        slug = f"{owner}/{name}"
        with self._client() as client:
            visibility = self._visibility(ctx, client, repo, owner, name)
        notes: list[str] = []
        self._screen(repo, visibility, f"{title}\n{body}\n" + "\n".join(labels), notes)
        marker = make_marker("issue", ctx.task.id, slug, title, body)
        return PreparedAction(
            kind=self.kind,
            target=f"github:{slug}:issues",
            payload={
                "repository": repo.name,
                "slug": slug,
                "visibility": visibility.value,
                "title": title,
                "body": f"{body}\n\n{marker_comment(marker)}".lstrip(),
                "labels": labels,
                "host_notes": notes,
                "marker": marker,
            },
            outward=True,
        )

    def _execute(self, action: PreparedAction, ctx: ActionContext) -> ActionResult:
        payload = action.payload
        repo, owner, name = self._repository(payload)
        with self._client() as client:
            self._check_visibility_unchanged(ctx, client, repo, payload)
            publication = self._publication(ctx, PublicationKind.ISSUE, payload)
            if publication.state is PublicationState.PUBLISHED:
                return _issue_result(publication, reused=True)
            try:
                viewer = client.viewer()
                issue = client.find_issue_with(
                    owner,
                    name,
                    payload["marker"],
                    creator=viewer["login"],
                    since=_utc_iso(publication.created_at - SEARCH_MARGIN),
                )
                reused = issue is not None
                if issue is None:
                    issue = client.create_issue(
                        owner,
                        name,
                        title=payload["title"],
                        body=payload["body"],
                        labels=payload["labels"],
                    )
            except GitHubError as exc:
                return self._fail(ctx, publication, str(exc))
        publication = ctx.store.update_publication(
            publication.id,
            state=PublicationState.PUBLISHED,
            number=int(issue["number"]),
            url=issue.get("html_url"),
            external_id=str(issue.get("node_id") or issue.get("id") or ""),
            error=None,
        )
        return _issue_result(publication, reused=reused)


class IssueCommentHandler(_GitHubHandler):
    kind = KIND_GITHUB_ISSUE_COMMENT
    description = (
        "Comment on an issue or pull request in a configured repository. Fields: repository "
        "(required), number (the issue or pull request number, required), body (required)."
    )

    def _prepare(self, proposal: Mapping[str, Any], ctx: ActionContext) -> PreparedAction:
        repo, owner, name = self._repository(proposal)
        number = proposal.get("number")
        if isinstance(number, str) and number.strip().isdigit():
            number = int(number.strip())
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            raise InvalidProposal(f"{self.kind}: number must be a positive whole number")
        body = _text(proposal, "body", self.kind, MAX_BODY_CHARS, True)
        slug = f"{owner}/{name}"
        with self._client() as client:
            visibility = self._visibility(ctx, client, repo, owner, name)
            issue = client.issue(owner, name, number)
        notes = [
            f"Comment on {'pull request' if 'pull_request' in issue else 'issue'} #{number} "
            f"({issue.get('state', 'unknown')}), titled: {str(issue.get('title', ''))[:200]}"
        ]
        if issue.get("locked"):
            raise InvalidProposal(f"{self.kind}: #{number} is locked")
        self._screen(repo, visibility, body, notes)
        marker = make_marker("issue_comment", ctx.task.id, slug, number, body)
        return PreparedAction(
            kind=self.kind,
            target=f"github:{slug}#{number}",
            payload={
                "repository": repo.name,
                "slug": slug,
                "visibility": visibility.value,
                "number": number,
                "body": f"{body}\n\n{marker_comment(marker)}",
                "host_notes": notes,
                "marker": marker,
            },
            outward=True,
        )

    def _execute(self, action: PreparedAction, ctx: ActionContext) -> ActionResult:
        payload = action.payload
        repo, owner, name = self._repository(payload)
        with self._client() as client:
            self._check_visibility_unchanged(ctx, client, repo, payload)
            publication = self._publication(ctx, PublicationKind.ISSUE_COMMENT, payload)
            if publication.state is PublicationState.PUBLISHED:
                return _issue_result(publication, reused=True)
            try:
                comment = client.find_comment_with(
                    owner,
                    name,
                    payload["number"],
                    payload["marker"],
                    author=str(client.viewer()["login"]),
                    since=_utc_iso(publication.created_at - SEARCH_MARGIN),
                )
                reused = comment is not None
                if comment is None:
                    comment = client.create_comment(owner, name, payload["number"], payload["body"])
            except GitHubError as exc:
                return self._fail(ctx, publication, str(exc))
        publication = ctx.store.update_publication(
            publication.id,
            state=PublicationState.PUBLISHED,
            number=int(payload["number"]),
            url=comment.get("html_url"),
            external_id=str(comment.get("id") or comment.get("node_id") or ""),
            error=None,
        )
        return _issue_result(publication, reused=reused)


def _issue_result(publication: Publication, *, reused: bool) -> ActionResult:
    return ActionResult(
        ok=True,
        detail={"number": publication.number, "url": publication.url, "existing": reused},
        external_id=publication.external_id,
    )


def action_handlers(config: Config) -> list[_GitHubHandler]:
    """The four handlers, or none when no repository is configured."""
    if not config.repositories or config.github is None:
        return []
    return [
        PushBranchHandler(config),
        OpenPullRequestHandler(config),
        IssueHandler(config),
        IssueCommentHandler(config),
    ]
