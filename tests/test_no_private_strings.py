"""The repository is public. Fail when a tracked text file holds something private.

The checks are generic patterns only: personal home-folder paths, email addresses
outside the reserved example domains, Slack-style member or channel ids, links to
private coding sessions and private key blocks. This file holds no real value to
look for, and it builds its patterns from parts so it does not match itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "build",
    "dist",
    "state",
}
SKIP_FILES = {"LICENSE", "NOTICE", "uv.lock", Path(__file__).name}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".db", ".sqlite", ".pyc"}

HOME_PATH = re.compile("/" + "home/" + r"[A-Za-z0-9._-]+|/" + "Users/" + r"[A-Za-z0-9._-]+")
# The last label must be letters, so npm pins such as tool@1.2.3 do not count.
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b")
SLACK_ID = re.compile(r"\b[UCW][A-Z0-9]{8,}\b")
SESSION_LINK = re.compile(re.escape("claude.ai/code/" + "session_"))
PRIVATE_KEY = re.compile("-----BEGIN [A-Z ]*" + "PRIVATE KEY-----")

ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "example.net")
ALLOWED_EMAILS = {"security@" + "defog.ai"}
ALLOWED_HOME_PATHS = {"/" + "home/opendot", "/" + "home/user", "/" + "Users/you"}


def _email_allowed(address: str) -> bool:
    address = address.lower()
    local, _, domain = address.partition("@")
    if address in ALLOWED_EMAILS:
        return True
    if domain in ALLOWED_EMAIL_DOMAINS or domain.endswith(
        tuple("." + d for d in ALLOWED_EMAIL_DOMAINS)
    ):
        return True
    return "noreply" in local or "no-reply" in local


def _slack_id_allowed(token: str) -> bool:
    # A real id mixes letters and digits; all-letter matches are ordinary capitalised
    # words, and ids that contain EXAMPLE are placeholders in docs.
    return not any(ch.isdigit() for ch in token) or "EXAMPLE" in token


def _text_files() -> list[Path]:
    found: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in relative.parts[:-1]) or (
            relative.parts == (".git",)
        ):
            continue
        if path.name in SKIP_FILES or path.suffix.lower() in BINARY_SUFFIXES:
            continue
        found.append(path)
    return found


def find_private_strings(text: str) -> list[str]:
    problems: list[str] = []
    for match in HOME_PATH.finditer(text):
        if match.group(0) not in ALLOWED_HOME_PATHS:
            problems.append(f"home folder path {match.group(0)!r}")
    for match in EMAIL.finditer(text):
        if not _email_allowed(match.group(0)):
            problems.append(f"email address {match.group(0)!r}")
    for match in SLACK_ID.finditer(text):
        if not _slack_id_allowed(match.group(0)):
            problems.append(f"Slack-style id {match.group(0)!r}")
    if SESSION_LINK.search(text):
        problems.append("a private coding-session link")
    if PRIVATE_KEY.search(text):
        problems.append("a private key block")
    return problems


def test_the_scan_sees_the_source_tree():
    names = {p.relative_to(ROOT).as_posix() for p in _text_files()}
    assert "README.md" in names
    assert "src/opendot/__main__.py" in names


@pytest.mark.parametrize(
    "text",
    [
        "see /" + "home/alice/project",
        "see /" + "Users/bob/notes",
        "mail " + "alice@" + "company.io",
        "channel " + "C0" + "12AB34CD",
        "https://" + "claude.ai/code/" + "session_abc",
        "-----BEGIN OPENSSH " + "PRIVATE KEY-----",
    ],
)
def test_the_checks_catch_each_pattern(text):
    assert find_private_strings(text)


@pytest.mark.parametrize(
    "text",
    [
        "write to " + "security@" + "defog.ai",
        "user@" + "example.com and ops@" + "mail.example.org",
        "Co-Authored-By: bot <" + "noreply@" + "vendor.test>",
        "WARRANTIES CONDITIONS CONTRIBUTING CHANGELOG",
        "profile slack:" + "U0" + "EXAMPLE",
        "the container's home is /" + "home/opendot",
        "npm install -g tool@1.2.3",
    ],
)
def test_the_checks_allow_public_placeholders(text):
    assert find_private_strings(text) == []


def test_no_private_strings_in_the_repository():
    problems: list[str] = []
    for path in _text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for problem in find_private_strings(text):
            problems.append(f"{path.relative_to(ROOT)}: {problem}")
    assert problems == []
