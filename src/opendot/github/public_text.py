"""The private-text check for text the host publishes in a public repository.

A push, pull request, issue or comment in a repository that GitHub reports as
public is refused when its text holds:
- a personal home-folder path;
- an email address outside the reserved example domains (no-reply addresses pass);
- text shaped like a key or token (the patterns opendot.redact masks);
- a link to a private coding session;
- any literal string from github.private_markers.

Only added lines of a diff are checked, so removing such text is allowed.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from opendot.redact import _BEARER, _TOKEN_PATTERNS

__all__ = ["find_private_text"]

# Built from parts so this file does not match its own check.
_HOME_PATH = re.compile("/" + "(?:home|Users)" + r"/(?!user\b|you\b|opendot\b)[A-Za-z0-9._-]+/")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b")
_ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "example.net")
_SESSION_LINK = re.compile(
    re.escape("claude.ai/code/" + "session_") + "|" + re.escape("chatgpt.com/" + "codex/tasks/")
)


def _email_allowed(address: str) -> bool:
    local, _, domain = address.lower().partition("@")
    if domain in _ALLOWED_EMAIL_DOMAINS or domain.endswith(
        tuple("." + d for d in _ALLOWED_EMAIL_DOMAINS)
    ):
        return True
    return any(word in part for word in ("noreply", "no-reply") for part in (local, domain))


def _short(value: str) -> str:
    return value if len(value) <= 6 else value[:6] + "..."


def find_private_text(text: str, markers: Sequence[str] = ()) -> list[str]:
    """Descriptions of every private-looking string in text. Values are shortened,
    so the result can be shown without repeating a secret."""
    problems: list[str] = []
    for match in _HOME_PATH.finditer(text):
        problems.append(f"a home-folder path ({_short(match.group(0))})")
    for match in _EMAIL.finditer(text):
        if not _email_allowed(match.group(0)):
            problems.append(f"an email address ({_short(match.group(0))})")
    for pattern in _TOKEN_PATTERNS:
        if pattern.search(text):
            problems.append("text shaped like a key or token")
            break
    else:
        if _BEARER.search(text):
            problems.append("text shaped like a bearer token")
    if _SESSION_LINK.search(text):
        problems.append("a link to a private coding session")
    for marker in markers:
        if marker and marker in text:
            problems.append(f"a github.private_markers entry ({_short(marker)})")
    seen: set[str] = set()
    unique = []
    for problem in problems:
        if problem not in seen:
            seen.add(problem)
            unique.append(problem)
    return unique
