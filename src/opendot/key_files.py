"""Saved logins: one file per variable name, readable only by you.

The Slack token, the GitHub token and the API keys of connectors are read from
host variables, such as OPENDOT_SLACK_BOT_TOKEN. Cron does not read a shell
profile, so a variable exported there is not set in the runs that cron starts.
`opendot login slack`, `opendot login github` and `opendot connectors login
<name>` save the value in core.keys_dir/<VARIABLE> with mode 0600. When an
opendot command loads its config, it sets each such variable that is not set
from its saved file. A variable that is set wins over the file.

The Claude Code token has its own file (see claude_login.py) and is not set
here, because the Claude Code backend reads that file itself.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import re
import stat
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opendot.config import Config

__all__ = [
    "KeyFileError",
    "check_key",
    "key_path",
    "key_source",
    "load_config",
    "needed_keys",
    "read_key_file",
    "save_key_file",
    "set_saved_keys",
]

VARIABLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class KeyFileError(Exception):
    pass


def check_key(value: str, what: str = "the key") -> str:
    """The value without surrounding space. Raises KeyFileError when it cannot be a key."""
    value = value.strip()
    if not value:
        raise KeyFileError(f"{what} is empty")
    if any(ch.isspace() for ch in value):
        raise KeyFileError(f"{what} has a space or a line break in it; paste it on one line")
    return value


def key_path(keys_dir: Path, variable: str) -> Path:
    """The file for one variable. Refuses a name that is not a plain variable name."""
    if not VARIABLE_PATTERN.match(variable):
        raise KeyFileError(f"{variable!r} is not a variable name, so it cannot name a key file")
    return keys_dir / variable


def read_key_file(path: Path, login_command: str = "") -> str | None:
    """The saved value, or None when the file does not exist.

    Raises KeyFileError when the file is not a regular file, belongs to another
    user, can be read by other users, or is empty.
    """
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise KeyFileError(f"{path} is not a regular file")
    if info.st_uid != os.getuid():
        raise KeyFileError(f"{path} belongs to another user")
    if info.st_mode & 0o077:
        mode = stat.S_IMODE(info.st_mode)
        raise KeyFileError(f"{path} has mode {mode:o}; run `chmod 600 {path}`")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        hint = f"; run `{login_command}`" if login_command else ""
        raise KeyFileError(f"{path} is empty{hint}")
    return value


def save_key_file(path: Path, value: str) -> None:
    """Write value to path with mode 0600, replacing any earlier value.

    The folder is created with mode 0700. The caller checks the value first.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def needed_keys(config: Config) -> list[tuple[str, str]]:
    """(variable, command that saves it) for each login the configured features use."""
    found: list[tuple[str, str]] = []
    if config.slack.enabled:
        found.append((config.slack.bot_token_env, "opendot login slack"))
    if config.github is not None and any(not r.plain_git for r in config.repositories):
        found.append((config.github.token_env, "opendot login github"))
    for server in config.connectors():
        if server.auth == "bearer_env" and server.auth_env:
            found.append((server.auth_env, f"opendot connectors login {server.name}"))
    return found


def _savable_names(config: Config) -> set[str]:
    return config.secret_env_names() - {config.claude_code.token_env}


def set_saved_keys(config: Config, environ: MutableMapping[str, str]) -> list[str]:
    """Set each login variable that is not set in environ from its saved file.

    Returns the names it set. A file that cannot be read is skipped here;
    `opendot doctor` reports it.
    """
    loaded: list[str] = []
    for name in sorted(_savable_names(config)):
        if environ.get(name):
            continue
        try:
            value = read_key_file(key_path(config.core.keys_dir, name))
        except (KeyFileError, OSError):
            continue
        if value:
            environ[name] = value
            loaded.append(name)
    return loaded


def load_config(path: Path | None = None) -> Config:
    """Config.load, then set the saved logins in this process's environment.

    Every command that runs work loads its config through this function, so a
    run that cron starts finds the logins that `opendot login` saved.
    """
    from opendot.config import Config

    config = Config.load(path)
    loaded = set_saved_keys(config, os.environ)
    return dataclasses.replace(config, keys_from_files=tuple(loaded))


def key_source(config: Config, variable: str, env: Mapping[str, str]) -> tuple[str, str]:
    """("variable" | "file" | "missing" | "error", detail) for one login variable."""
    try:
        path = key_path(config.core.keys_dir, variable)
    except KeyFileError as exc:
        return "error", str(exc)
    try:
        saved = read_key_file(path)
    except (KeyFileError, OSError) as exc:
        return "error", f"cannot read the saved {variable}: {exc}"
    if saved:
        return "file", f"{variable} is saved in {path}"
    if env.get(variable):
        return "variable", f"{variable} is set"
    return "missing", f"{variable} is not set and not saved"
