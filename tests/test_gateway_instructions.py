"""Downloading and unpacking the public FactIQ plugin files."""

from __future__ import annotations

import io
import os
import stat
import tarfile

import pytest

from opendot.config import FACTIQ_PLUGIN_COMMIT
from opendot.gateway.instructions import (
    COMMIT_MARKER,
    InstructionsError,
    archive_url,
    extract_plugin_archive,
    fetch_factiq_instructions,
    installed_commit,
)

TOP = f"factiq-plugin-{FACTIQ_PLUGIN_COMMIT}"

DEFAULT_FILES = {
    "LICENSE": "MIT License\n",
    "README.md": "# plugin\n",
    "skills/factiq/SKILL.md": "---\nname: factiq\n---\nUse {plugin_root}/references.\n",
    "references/style.md": "style\n",
    "scripts/chart.py": "print('chart')\n",
    ".claude-plugin/plugin.json": "{}\n",
    "hooks/hooks.json": "{}\n",
}


def make_archive(files: dict[str, str] | None = None, *, top: str = TOP, extra=None) -> bytes:
    """A .tar.gz shaped like a GitHub archive: everything under one top folder."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        folder = tarfile.TarInfo(top)
        folder.type = tarfile.DIRTYPE
        archive.addfile(folder)
        for name, text in (DEFAULT_FILES if files is None else files).items():
            data = text.encode()
            info = tarfile.TarInfo(f"{top}/{name}" if top else name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for info in extra or []:
            archive.addfile(info)
    return buffer.getvalue()


@pytest.fixture
def factiq_archive() -> bytes:
    return make_archive()


def test_archive_url_points_at_the_pinned_commit():
    assert archive_url() == (
        f"https://github.com/defog-ai/factiq-plugin/archive/{FACTIQ_PLUGIN_COMMIT}.tar.gz"
    )


def test_extract_keeps_only_the_listed_files(tmp_path, factiq_archive):
    target = tmp_path / "connectors" / "factiq" / "instructions"
    extract_plugin_archive(factiq_archive, target, commit="abc123")
    kept = sorted(str(p.relative_to(target)) for p in target.rglob("*") if p.is_file())
    assert kept == [
        COMMIT_MARKER,
        "LICENSE",
        "README.md",
        "SOURCE.md",
        "references/style.md",
        "scripts/chart.py",
        "skills/factiq/SKILL.md",
    ]
    assert installed_commit(target) == "abc123"
    source = (target / "SOURCE.md").read_text()
    assert "https://github.com/defog-ai/factiq-plugin" in source
    assert "abc123" in source and "MIT" in source
    assert stat.S_IMODE((target / "LICENSE").stat().st_mode) == 0o644
    assert stat.S_IMODE((target / "skills").stat().st_mode) == 0o755


def test_extract_replaces_an_older_copy(tmp_path, factiq_archive):
    target = tmp_path / "instructions"
    extract_plugin_archive(factiq_archive, target, commit="old")
    (target / "stale.txt").write_text("x")
    files = dict(DEFAULT_FILES)
    files["skills/factiq/SKILL.md"] = "new\n"
    extract_plugin_archive(make_archive(files), target, commit="new")
    assert installed_commit(target) == "new"
    assert not (target / "stale.txt").exists()
    assert (target / "skills/factiq/SKILL.md").read_text() == "new\n"
    assert [p.name for p in tmp_path.iterdir()] == ["instructions"]


@pytest.mark.parametrize(
    "name",
    ["/etc/passwd", f"{TOP}/../escape", f"{TOP}/skills/..\\x"],
)
def test_unsafe_paths_are_refused(tmp_path, name):
    info = tarfile.TarInfo(name)
    info.size = 0
    data = make_archive(extra=[info])
    with pytest.raises(InstructionsError, match="unsafe path"):
        extract_plugin_archive(data, tmp_path / "t", commit="c")
    assert not (tmp_path / "t").exists()


def test_links_are_refused(tmp_path):
    link = tarfile.TarInfo(f"{TOP}/skills/evil")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    with pytest.raises(InstructionsError, match="link"):
        extract_plugin_archive(make_archive(extra=[link]), tmp_path / "t", commit="c")
    assert list(tmp_path.iterdir()) == []


def test_links_outside_the_kept_folders_are_ignored(tmp_path):
    link = tarfile.TarInfo(f"{TOP}/hooks/evil")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    target = tmp_path / "t"
    extract_plugin_archive(make_archive(extra=[link]), target, commit="c")
    assert not (target / "hooks").exists()


def test_missing_required_files_are_refused(tmp_path):
    files = {k: v for k, v in DEFAULT_FILES.items() if k != "LICENSE"}
    with pytest.raises(InstructionsError, match="LICENSE"):
        extract_plugin_archive(make_archive(files), tmp_path / "t", commit="c")


def test_oversized_archive_is_refused(tmp_path, monkeypatch):
    import opendot.gateway.instructions as module

    monkeypatch.setattr(module, "MAX_TOTAL_BYTES", 10)
    with pytest.raises(InstructionsError, match="larger"):
        extract_plugin_archive(make_archive(), tmp_path / "t", commit="c")


def test_fetch_downloads_once(tmp_path, factiq_archive):
    urls: list[str] = []

    def download(url: str) -> bytes:
        urls.append(url)
        return factiq_archive

    target = tmp_path / "instructions"
    assert fetch_factiq_instructions(target, download=download) == target
    assert installed_commit(target) == FACTIQ_PLUGIN_COMMIT
    fetch_factiq_instructions(target, download=download)
    assert len(urls) == 1
    fetch_factiq_instructions(target, download=download, force=True)
    assert len(urls) == 2


def test_fetch_wraps_network_errors(tmp_path):
    def download(url: str) -> bytes:
        raise OSError("connection refused")

    with pytest.raises(InstructionsError, match="connection refused"):
        fetch_factiq_instructions(tmp_path / "t", download=download)


@pytest.mark.skipif(
    not os.environ.get("OPENDOT_NETWORK_TESTS"), reason="set OPENDOT_NETWORK_TESTS=1"
)
def test_real_download_of_the_pinned_commit(tmp_path):
    target = fetch_factiq_instructions(tmp_path / "instructions")
    assert (target / "skills" / "factiq" / "SKILL.md").is_file()
    assert "MIT" in (target / "LICENSE").read_text()
