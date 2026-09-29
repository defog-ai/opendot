"""The crontab entry that runs `opendot tick` on a timer.

OpenDot owns one block of the user's crontab, between BLOCK_BEGIN and BLOCK_END.
Installing replaces that block and leaves every other line alone; removing
deletes only that block. Overlapping runs are harmless: `tick` takes the worker
lock and exits when another run holds it.
"""

from __future__ import annotations

import shlex
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from opendot.backends import CommandRunner

BLOCK_BEGIN = "# >>> opendot tick (managed by `opendot install-cron`) >>>"
BLOCK_END = "# <<< opendot tick <<<"


class CronError(RuntimeError):
    pass


def opendot_command() -> list[str]:
    """How cron should start opendot: the installed script, else this interpreter."""
    found = shutil.which("opendot")
    if found:
        return [str(Path(found).resolve())]
    return [sys.executable, "-m", "opendot"]


def _cron_quote(value: str) -> str:
    # cron turns an unescaped % into a newline.
    return shlex.quote(value).replace("%", "\\%")


def tick_line(
    command: Sequence[str],
    *,
    every_minutes: int,
    config_path: Path | None,
    log_path: Path,
) -> str:
    """One crontab line that runs tick every `every_minutes` minutes."""
    if not 1 <= every_minutes <= 59:
        raise CronError("every_minutes must be between 1 and 59")
    minute = "*" if every_minutes == 1 else f"*/{every_minutes}"
    parts = [_cron_quote(part) for part in command]
    if config_path is not None:
        parts += ["--config", _cron_quote(str(config_path))]
    parts.append("tick")
    return f"{minute} * * * * {' '.join(parts)} >> {_cron_quote(str(log_path))} 2>&1"


def remove_block(crontab: str) -> str:
    """The crontab without OpenDot's block."""
    kept: list[str] = []
    inside = False
    for line in crontab.splitlines():
        if line.strip() == BLOCK_BEGIN:
            inside = True
            continue
        if inside:
            if line.strip() == BLOCK_END:
                inside = False
            continue
        kept.append(line)
    if inside:
        raise CronError("the crontab has an OpenDot start marker without an end marker")
    text = "\n".join(kept).rstrip("\n")
    return text + "\n" if text else ""


def with_block(crontab: str, line: str) -> str:
    """The crontab with OpenDot's block replaced by one holding `line`."""
    base = remove_block(crontab)
    block = f"{BLOCK_BEGIN}\n{line}\n{BLOCK_END}\n"
    return f"{base}{block}" if not base else f"{base}\n{block}"


def read_crontab(runner: CommandRunner) -> str:
    """The current user's crontab. A user with no crontab gets an empty string."""
    result = runner.run(["crontab", "-l"])
    if result.returncode == 0:
        return result.stdout
    if "no crontab" in result.stderr.lower():
        return ""
    raise CronError(f"crontab -l failed: {result.stderr.strip()}")


def write_crontab(runner: CommandRunner, text: str) -> None:
    result = runner.run(["crontab", "-"], input=text)
    if result.returncode != 0:
        raise CronError(f"crontab - failed: {result.stderr.strip()}")
