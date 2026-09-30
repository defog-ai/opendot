"""The saved Claude Code login: one token in a file that only you can read.

`opendot login claude` runs `claude setup-token`, asks for the token it prints,
and saves it in backend.claude_code.token_file with mode 0600. The Claude Code
backend reads the variable named by backend.claude_code.token_env first, and
this file when the variable is not set. So the token needs no line in a shell
profile, and cron runs find it too.
"""

from __future__ import annotations

from pathlib import Path

from opendot.key_files import KeyFileError, read_key_file, save_key_file

__all__ = ["TokenFileError", "read_token_file", "save_token_file", "check_token"]

TokenFileError = KeyFileError


def check_token(token: str) -> str:
    """The token without surrounding space. Raises TokenFileError when it cannot be one."""
    token = token.strip()
    if not token:
        raise TokenFileError("the token is empty")
    if any(ch.isspace() for ch in token):
        raise TokenFileError("the token has a space or a line break in it; paste it on one line")
    if not token.startswith("sk-ant-"):
        raise TokenFileError(
            "this does not look like a Claude token; the token that `claude setup-token` "
            "prints starts with sk-ant-"
        )
    return token


def read_token_file(path: Path) -> str | None:
    """The saved token, or None when the file does not exist.

    Raises TokenFileError when the file is not a regular file, belongs to another
    user, can be read by other users, or holds no token.
    """
    return read_key_file(path, "opendot login claude")


def save_token_file(path: Path, token: str) -> None:
    """Write the token to path with mode 0600, replacing any earlier token."""
    save_key_file(path, check_token(token))
