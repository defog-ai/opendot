"""The saved Claude Code login: one token in a file that only you can read.

`opendot login claude` runs `claude setup-token`, asks for the token it prints,
and saves it in backend.claude_code.token_file with mode 0600. The Claude Code
backend reads the variable named by backend.claude_code.token_env first, and
this file when the variable is not set. So the token needs no line in a shell
profile, and cron runs find it too.
"""

from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path

__all__ = ["TokenFileError", "read_token_file", "save_token_file", "check_token"]


class TokenFileError(Exception):
    pass


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
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise TokenFileError(f"{path} is not a regular file")
    if info.st_uid != os.getuid():
        raise TokenFileError(f"{path} belongs to another user")
    if info.st_mode & 0o077:
        raise TokenFileError(
            f"{path} has mode {stat.S_IMODE(info.st_mode):o}; run `chmod 600 {path}`"
        )
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise TokenFileError(f"{path} is empty; run `opendot login claude`")
    return token


def save_token_file(path: Path, token: str) -> None:
    """Write the token to path with mode 0600, replacing any earlier token."""
    token = check_token(token)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
