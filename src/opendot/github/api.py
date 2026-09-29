"""A small GitHub REST client for the calls the host makes.

The token is sent in the Authorization header of each request and is never
logged. Tests pass an httpx.MockTransport as transport.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

from opendot.redact import redact

__all__ = ["GitHubClient", "GitHubError", "noreply_email"]

API_VERSION = "2022-11-28"
MAX_PAGES = 5


class GitHubError(RuntimeError):
    """A GitHub API call failed."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def noreply_email(user: dict[str, Any], host: str) -> str:
    """The GitHub no-reply address of a user, as GitHub shows it for web commits."""
    domain = "users.noreply.github.com" if host == "github.com" else f"users.noreply.{host}"
    return f"{user['id']}+{user['login']}@{domain}"


class GitHubClient:
    def __init__(
        self,
        api_url: str,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ):
        self._token = token
        self._client = httpx.Client(
            base_url=api_url.rstrip("/"),
            transport=transport,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "opendot",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- plumbing ----------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise GitHubError(redact(f"{method} {path}: {exc}", [self._token])) from None
        if response.status_code >= 400:
            try:
                message = response.json().get("message", "")
            except ValueError:
                message = response.text[:300]
            raise GitHubError(
                redact(f"{method} {path}: HTTP {response.status_code} {message}", [self._token]),
                status=response.status_code,
            )
        return response

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params).json()

    def _pages(self, path: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        params = {**params, "per_page": 100}
        for page in range(1, MAX_PAGES + 1):
            items = self._get(path, {**params, "page": page})
            if not isinstance(items, list):
                raise GitHubError(f"GET {path}: expected a list")
            yield from items
            if len(items) < 100:
                return

    # -- calls -------------------------------------------------------------

    def viewer(self) -> dict[str, Any]:
        """The user the token belongs to."""
        return self._get("/user")

    def repository(self, owner: str, repo: str) -> dict[str, Any]:
        return self._get(f"/repos/{owner}/{repo}")

    def open_pulls_for_head(self, owner: str, repo: str, branch: str) -> list[dict[str, Any]]:
        return self._get(
            f"/repos/{owner}/{repo}/pulls", {"state": "open", "head": f"{owner}:{branch}"}
        )

    def find_open_pull_with(self, owner: str, repo: str, text: str) -> dict[str, Any] | None:
        for pull in self._pages(f"/repos/{owner}/{repo}/pulls", {"state": "open"}):
            if text in (pull.get("body") or ""):
                return pull
        return None

    def create_pull(
        self, owner: str, repo: str, *, title: str, body: str, head: str, base: str, draft: bool
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json={"title": title, "body": body, "head": head, "base": base, "draft": draft},
        ).json()

    def pull(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        return self._get(f"/repos/{owner}/{repo}/pulls/{int(number)}")

    def issue(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        return self._get(f"/repos/{owner}/{repo}/issues/{int(number)}")

    def find_issue_with(
        self, owner: str, repo: str, text: str, *, creator: str, since: str
    ) -> dict[str, Any] | None:
        params = {"state": "all", "creator": creator, "since": since}
        for issue in self._pages(f"/repos/{owner}/{repo}/issues", params):
            if (
                "pull_request" not in issue
                and _login(issue) == creator.lower()
                and text in (issue.get("body") or "")
            ):
                return issue
        return None

    def create_issue(
        self, owner: str, repo: str, *, title: str, body: str, labels: list[str]
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        return self._request("POST", f"/repos/{owner}/{repo}/issues", json=payload).json()

    def find_comment_with(
        self, owner: str, repo: str, number: int, text: str, *, author: str, since: str
    ) -> dict[str, Any] | None:
        """The first comment by author that holds text. Anyone can post a comment with
        the same text, so a comment by another user never counts."""
        path = f"/repos/{owner}/{repo}/issues/{int(number)}/comments"
        for comment in self._pages(path, {"since": since}):
            if _login(comment) == author.lower() and text in (comment.get("body") or ""):
                return comment
        return None

    def create_comment(self, owner: str, repo: str, number: int, body: str) -> dict[str, Any]:
        return self._request(
            "POST", f"/repos/{owner}/{repo}/issues/{int(number)}/comments", json={"body": body}
        ).json()


def _login(item: dict[str, Any]) -> str:
    """The lower-case login of the user who made an issue, pull request or comment."""
    user = item.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    return login.lower() if isinstance(login, str) else ""


def own_pull(pull: dict[str, Any], branch: str, login: str) -> bool:
    """True when login opened the pull request from branch of the repository itself.

    A pull request from a fork, from another branch, or by another user is never taken
    over: anyone can open one whose head has the same branch name or whose body holds
    the same marker text."""
    head = pull.get("head")
    base = pull.get("base")
    if not isinstance(head, dict) or not isinstance(base, dict):
        return False
    head_repo = head.get("repo")
    base_repo = base.get("repo")
    if not isinstance(head_repo, dict) or not isinstance(base_repo, dict):
        return False
    return (
        head_repo.get("id") is not None
        and head_repo.get("id") == base_repo.get("id")
        and head.get("ref") == branch
        and _login(pull) == login.lower()
    )
