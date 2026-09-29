"""Remove secrets from text before it is written to a log, and keep paths inside a root.

redact() first replaces every known secret value it is given (a login token, the
strings inside a login file, allowlisted variable values), then anything that has
the shape of a common token. contained_path() refuses a path that leaves its root,
whether through ".." or through a symbolic link.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

MASK = "[REDACTED]"

# Known values shorter than this are not replaced; they would mask ordinary words.
MIN_SECRET_LENGTH = 8

_TOKEN_PATTERNS = [
    # Private key blocks.
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    # Anthropic and OpenAI style keys: sk-ant-..., sk-proj-..., sk-...
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    # Slack tokens.
    re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bxapp-[A-Za-z0-9\-]{10,}"),
    # GitHub tokens.
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    # AWS access key ids.
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    # Google API keys.
    re.compile(r"\bAIza[A-Za-z0-9_\-]{30,}"),
    # JSON web tokens.
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]*"),
]

# "Authorization: Bearer <value>" and "bearer <value>".
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/\-]{12,}=*")

# key=value or "key": "value" where the key names a secret.
_SECRET_KEY_WORDS = r"(?:access_token|refresh_token|id_token|api_key|apikey|secret|password|token)"
_KEY_VALUE = re.compile(
    r"(?i)(\"?[A-Za-z0-9_\-]*" + _SECRET_KEY_WORDS + r"\"?\s*[:=]\s*\"?)([^\s\",}]{8,})"
)

_SECRET_KEY = re.compile(r"(?i)" + _SECRET_KEY_WORDS + r"|authorization|credential|cookie")


class PathEscape(ValueError):
    """A path resolves outside the root it must stay in."""


def _known_values(secrets: Iterable[str | None]) -> list[str]:
    values = {s for s in secrets if s and len(s) >= MIN_SECRET_LENGTH}
    # Longest first, so a value that contains another is replaced whole.
    return sorted(values, key=len, reverse=True)


def redact(text: str, secrets: Iterable[str | None] = ()) -> str:
    """Return text with known secret values and token-shaped strings replaced by MASK."""
    for value in _known_values(secrets):
        text = text.replace(value, MASK)
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(MASK, text)
    text = _BEARER.sub(lambda m: m.group(1) + MASK, text)
    text = _KEY_VALUE.sub(lambda m: m.group(1) + MASK, text)
    return text


def redact_json(value: Any, secrets: Iterable[str | None] = ()) -> Any:
    """Redact a decoded JSON value. Values under secret-sounding keys are replaced whole."""
    known = _known_values(secrets)
    return _redact_value(value, known)


def _redact_value(value: Any, known: list[str]) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_KEY.search(key) and isinstance(item, str):
                result[key] = MASK if item else item
            else:
                result[key] = _redact_value(item, known)
        return result
    if isinstance(value, list):
        return [_redact_value(item, known) for item in value]
    if isinstance(value, str):
        return redact(value, known)
    return value


def secret_strings(data: Any, min_length: int = 16) -> list[str]:
    """Every string of at least min_length characters inside a decoded JSON value.

    Used on login files: each long string in them is treated as a secret.
    """
    found: list[str] = []
    if isinstance(data, dict):
        for item in data.values():
            found.extend(secret_strings(item, min_length))
    elif isinstance(data, list):
        for item in data:
            found.extend(secret_strings(item, min_length))
    elif isinstance(data, str) and len(data) >= min_length:
        found.append(data)
    return found


def contained_path(root: Path, candidate: Path | str) -> Path:
    """Resolve candidate (relative paths are taken from root) and require it inside root.

    Symbolic links are followed before the check, so a link that points outside
    the root is refused like a path with "..". Returns the resolved path.
    """
    root_resolved = Path(root).resolve()
    path = Path(candidate)
    if not path.is_absolute():
        path = root_resolved / path
    resolved = path.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise PathEscape(f"{candidate} is outside {root}")
    return resolved


def write_redacted(
    root: Path, relative: Path | str, text: str, secrets: Iterable[str | None] = ()
) -> Path:
    """Write redacted text to root/relative with mode 0600; the path must stay in root."""
    target = contained_path(root, relative)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(redact(text, secrets))
    return target


def dumps_redacted(value: Any, secrets: Iterable[str | None] = ()) -> str:
    """json.dumps of a redacted copy of value, on one line."""
    return json.dumps(redact_json(value, secrets), ensure_ascii=False, sort_keys=True)
