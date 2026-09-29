from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from opendot.redact import (
    MASK,
    PathEscape,
    contained_path,
    dumps_redacted,
    redact,
    redact_json,
    secret_strings,
    write_redacted,
)


@pytest.mark.parametrize(
    "token",
    [
        "sk-ant-oat01-" + "a" * 40,
        "sk-proj-" + "B" * 30,
        "ghp_" + "c" * 36,
        "github_pat_" + "d" * 40,
        "xoxb-1234567890-abcdefghij",
        "AKIA" + "E" * 16,
        "AIza" + "f" * 35,
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJl",
    ],
)
def test_token_shapes_are_masked(token: str) -> None:
    out = redact(f"before {token} after")
    assert token not in out
    assert MASK in out
    assert out.startswith("before ") and out.endswith(" after")


def test_bearer_and_key_value() -> None:
    out = redact("Authorization: Bearer abcdefghijklmnop123")
    assert "abcdefghijklmnop123" not in out
    out = redact('{"refresh_token": "rt-abcdefgh12345"} password=hunter2hunter2')
    assert "rt-abcdefgh12345" not in out
    assert "hunter2hunter2" not in out


def test_private_key_block() -> None:
    label = "RSA PRIVATE" + " KEY"  # built from parts so the public-repo scan stays clean
    block = f"-----BEGIN {label}-----\nMIIabc\n-----END {label}-----"
    assert redact(f"x {block} y") == f"x {MASK} y"


def test_known_values_are_masked_and_short_ones_kept() -> None:
    out = redact("plain value opaque-login-value here", ["opaque-login-value", "value"])
    assert out == f"plain value {MASK} here"


def test_ordinary_text_is_unchanged() -> None:
    text = "The build passed on 2026-01-05; see the log for 42 tests."
    assert redact(text) == text


def test_redact_json_masks_secret_keys() -> None:
    data = {
        "tokens": {"access_token": "short", "id_token": "x"},
        "Authorization": "anything",
        "note": "uses sk-ant-api03-" + "z" * 30,
        "count": 3,
        "items": ["keep", "opaque-known-1"],
    }
    out = redact_json(data, ["opaque-known-1"])
    assert out["tokens"] == {"access_token": MASK, "id_token": MASK}
    assert out["Authorization"] == MASK
    assert "zzzz" not in out["note"]
    assert out["count"] == 3
    assert out["items"] == ["keep", MASK]
    assert json.loads(dumps_redacted(data))["Authorization"] == MASK


def test_secret_strings() -> None:
    data = {"a": "x" * 16, "b": ["short", {"c": "y" * 20}], "n": 5}
    assert sorted(secret_strings(data)) == ["x" * 16, "y" * 20]


def test_contained_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    assert contained_path(root, "a/b.txt") == (root / "a" / "b.txt").resolve()
    assert contained_path(root, root) == root.resolve()
    with pytest.raises(PathEscape):
        contained_path(root, "../outside.txt")
    with pytest.raises(PathEscape):
        contained_path(root, "a/../../outside.txt")
    with pytest.raises(PathEscape):
        contained_path(root, tmp_path / "other")


def test_contained_path_follows_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(PathEscape):
        contained_path(root, "link/file.txt")


def test_write_redacted(tmp_path: Path) -> None:
    path = write_redacted(tmp_path, "logs/out.txt", "token opaque-known-2", ["opaque-known-2"])
    assert path.read_text() == f"token {MASK}"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    with pytest.raises(PathEscape):
        write_redacted(tmp_path / "logs", "../../escape.txt", "x")
