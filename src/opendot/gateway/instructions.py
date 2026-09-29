"""The public FactIQ plugin's skill files, shared with the model read-only.

The files come from the public repository FACTIQ_PLUGIN_REPO at the pinned
commit FACTIQ_PLUGIN_COMMIT and are MIT licensed; the LICENSE file is kept next
to them and a SOURCE.md file says where they came from. They are stored at
<state root>/connectors/factiq/instructions and mounted at
/opendot/instructions/factiq.

Only LICENSE, README.md, skills/, references/ and scripts/ are kept. The archive
is read with strict rules: regular files and folders only, no absolute paths, no
"..", no links, and a size limit.
"""

from __future__ import annotations

import io
import os
import shutil
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from opendot.config import FACTIQ_PLUGIN_COMMIT, FACTIQ_PLUGIN_REPO

__all__ = [
    "COMMIT_MARKER",
    "InstructionsError",
    "KEEP",
    "archive_url",
    "extract_plugin_archive",
    "fetch_factiq_instructions",
    "installed_commit",
]

KEEP_FILES = ("LICENSE", "README.md")
KEEP_DIRS = ("skills", "references", "scripts")
KEEP = KEEP_FILES + KEEP_DIRS
REQUIRED = ("LICENSE", "skills/factiq/SKILL.md")
COMMIT_MARKER = ".opendot-commit"
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_FILES = 2000
DOWNLOAD_TIMEOUT_SECONDS = 60.0


class InstructionsError(RuntimeError):
    pass


def archive_url(commit: str = FACTIQ_PLUGIN_COMMIT, repo: str = FACTIQ_PLUGIN_REPO) -> str:
    return f"{repo}/archive/{commit}.tar.gz"


def installed_commit(folder: Path) -> str | None:
    marker = folder / COMMIT_MARKER
    try:
        return marker.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _kept_path(name: str) -> PurePosixPath | None:
    """The member's path below the archive's top folder when it is kept, else None.
    Raises InstructionsError for a path that is not safe."""
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise InstructionsError(f"unsafe path in the archive: {name!r}")
    parts = path.parts[1:]  # drop <repo>-<commit>/
    if not parts:
        return None
    if parts[0] in KEEP_DIRS or (len(parts) == 1 and parts[0] in KEEP_FILES):
        return PurePosixPath(*parts)
    return None


def extract_plugin_archive(
    data: bytes, target: Path, *, commit: str, repo: str = FACTIQ_PLUGIN_REPO
) -> Path:
    """Unpack the kept files of a .tar.gz into target, replacing what is there.

    The files are unpacked into a new folder next to target and then renamed into
    place, so a reader never sees half a folder."""
    target = Path(target)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".instructions-", dir=target.parent))
    try:
        total = 0
        count = 0
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive:
                kept = _kept_path(member.name)
                if kept is None:
                    continue
                if not (member.isfile() or member.isdir()):
                    raise InstructionsError(f"the archive holds a link or device: {member.name!r}")
                dest = staging.joinpath(*kept.parts)
                if member.isdir():
                    dest.mkdir(mode=0o755, parents=True, exist_ok=True)
                    continue
                count += 1
                total += member.size
                if count > MAX_FILES or total > MAX_TOTAL_BYTES:
                    raise InstructionsError("the archive is larger than expected")
                source = archive.extractfile(member)
                if source is None:
                    raise InstructionsError(f"cannot read {member.name!r}")
                dest.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                with source, open(dest, "wb") as out:
                    shutil.copyfileobj(source, out)
                os.chmod(dest, 0o644)
        for required in REQUIRED:
            if not (staging / required).is_file():
                raise InstructionsError(f"the archive has no {required}")
        (staging / "SOURCE.md").write_text(
            "# Source of these files\n\n"
            f"Copied by OpenDot from {repo} at commit {commit}.\n"
            "They are covered by the MIT licence in LICENSE, which applies to them and not\n"
            "to OpenDot. Only LICENSE, README.md, skills/, references/ and scripts/ are kept.\n",
            encoding="utf-8",
        )
        (staging / COMMIT_MARKER).write_text(commit + "\n", encoding="utf-8")
        for path in (staging / "SOURCE.md", staging / COMMIT_MARKER):
            os.chmod(path, 0o644)
        for root, dirs, _files in os.walk(staging):
            for d in dirs:
                os.chmod(Path(root) / d, 0o755)
        os.chmod(staging, 0o755)
        old: Path | None = None
        if target.exists() or target.is_symlink():
            old = target.parent / f".instructions-old-{os.getpid()}"
            if old.exists():
                shutil.rmtree(old)
            os.rename(target, old)
        os.rename(staging, target)
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
        return target
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _download(url: str) -> bytes:
    import httpx

    with httpx.Client(follow_redirects=True, timeout=DOWNLOAD_TIMEOUT_SECONDS) as client:
        with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise InstructionsError(f"download failed: HTTP {response.status_code} for {url}")
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_TOTAL_BYTES:
                    raise InstructionsError("the download is larger than expected")
                chunks.append(chunk)
    return b"".join(chunks)


def fetch_factiq_instructions(
    target: Path,
    *,
    commit: str = FACTIQ_PLUGIN_COMMIT,
    download: Callable[[str], bytes] | None = None,
    force: bool = False,
) -> Path:
    """Make sure target holds the plugin files at commit; download them if not."""
    target = Path(target)
    if (
        not force
        and installed_commit(target) == commit
        and (target / "skills/factiq/SKILL.md").is_file()
    ):
        return target
    try:
        data = (download or _download)(archive_url(commit))
    except InstructionsError:
        raise
    except Exception as exc:  # noqa: BLE001 - network errors of any kind
        raise InstructionsError(f"cannot download the FactIQ plugin files: {exc}") from exc
    return extract_plugin_archive(data, target, commit=commit)
