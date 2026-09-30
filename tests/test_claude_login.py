from __future__ import annotations

import os
from pathlib import Path

import pytest

from opendot.claude_login import TokenFileError, check_token, read_token_file, save_token_file

TOKEN = "sk-ant-oat01-" + "t" * 48


def test_check_token_strips_the_line_break():
    assert check_token(f"  {TOKEN}\n") == TOKEN


@pytest.mark.parametrize("text", ["", "   ", "sk-ant-oat01 abc", "ghp_" + "x" * 36])
def test_check_token_refuses_what_cannot_be_a_token(text):
    with pytest.raises(TokenFileError):
        check_token(text)


def test_save_then_read(tmp_path: Path):
    path = tmp_path / "config" / "opendot" / "claude-token"
    save_token_file(path, TOKEN)
    assert read_token_file(path) == TOKEN
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["claude-token"]


def test_save_replaces_an_earlier_token(tmp_path: Path):
    path = tmp_path / "claude-token"
    save_token_file(path, "sk-ant-oat01-old")
    save_token_file(path, TOKEN)
    assert read_token_file(path) == TOKEN


def test_a_missing_file_is_no_login(tmp_path: Path):
    assert read_token_file(tmp_path / "claude-token") is None


def test_a_file_others_can_read_is_refused(tmp_path: Path):
    path = tmp_path / "claude-token"
    path.write_text(TOKEN)
    path.chmod(0o640)
    with pytest.raises(TokenFileError, match="chmod 600"):
        read_token_file(path)


def test_a_symlink_is_refused(tmp_path: Path):
    target = tmp_path / "real"
    save_token_file(target, TOKEN)
    link = tmp_path / "claude-token"
    link.symlink_to(target)
    with pytest.raises(TokenFileError, match="not a regular file"):
        read_token_file(link)


def test_an_empty_file_is_refused(tmp_path: Path):
    path = tmp_path / "claude-token"
    path.write_text("\n")
    path.chmod(0o600)
    with pytest.raises(TokenFileError, match="empty"):
        read_token_file(path)
