"""Schema version 2: migration from a version 1 database and the new tables."""

import os
import sqlite3
import stat
import subprocess
from pathlib import Path

import pytest

from opendot.models import (
    GatewayCallStatus,
    GatewayMode,
    PublicationKind,
    PublicationState,
    RepoVisibility,
    Step,
    WorktreeStatus,
)
from opendot.store import SCHEMA_VERSION, NotFound, Store, StoreError


def _v1_schema() -> str:
    """The schema.sql shipped with v0.1, read from git history when available."""
    here = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        ["git", "show", "dd08de0:src/opendot/schema.sql"],
        cwd=here,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("v0.1 schema not available from git history")
    return result.stdout


def test_schema_version_is_two():
    assert SCHEMA_VERSION == 2


def test_migrate_version_one_database_keeps_rows(tmp_path, clock):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(_v1_schema())
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', '1') "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value"
    )
    conn.commit()
    conn.close()

    store = Store(path, clock=clock)
    assert store.schema_version() == 1
    task = store.create_task(
        text="old task", requester="alice", channel="cli", conversation="local"
    )
    store.migrate()
    assert store.schema_version() == 2
    assert store.get_task(task.id).text == "old task"
    names = {
        row["name"]
        for row in store._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "repositories",
        "worktrees",
        "github_publications",
        "gateway_calls",
        "connector_tokens",
    } <= names
    store.close()


def test_migrate_refuses_newer_database(tmp_path, clock):
    store = Store(tmp_path / "new.db", clock=clock)
    store.migrate()
    store._conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    with pytest.raises(StoreError):
        store.migrate()
    store.close()


def test_wal_and_shm_files_are_private(store):
    store.save_connector_token("factiq", "secret-token")
    for suffix in ("", "-wal", "-shm"):
        path = store.path.with_name(store.path.name + suffix)
        if path.exists():
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def _task(store):
    return store.create_task(text="t", requester="alice", channel="cli", conversation="local")


def test_repositories(store, tmp_path, clock):
    repo = store.upsert_repository(
        "app",
        remote="https://github.com/example/app.git",
        control_path=tmp_path / "repos" / "app",
        default_branch="main",
    )
    assert repo.visibility is RepoVisibility.UNKNOWN
    assert repo.visibility_checked_at is None
    store.set_repository_visibility("app", RepoVisibility.PUBLIC)
    clock.advance(minutes=1)
    store.mark_repository_fetched("app")
    again = store.upsert_repository(
        "app",
        remote="git@" + "github.com:example/app.git",
        control_path=tmp_path / "repos" / "app",
        default_branch="trunk",
    )
    assert again.visibility is RepoVisibility.PUBLIC
    assert again.default_branch == "trunk"
    assert again.last_fetched_at == clock.now()
    assert [r.name for r in store.list_repositories()] == ["app"]
    with pytest.raises(NotFound):
        store.get_repository("missing")
    with pytest.raises(NotFound):
        store.set_repository_visibility("missing", RepoVisibility.PRIVATE)


def test_worktrees(store, tmp_path):
    task = _task(store)
    tree = store.add_worktree(
        task.id,
        "app",
        path=tmp_path / "worktrees" / "task-1" / "app",
        base_ref="main",
        base_sha="a" * 40,
        branch="opendot/task-1",
    )
    assert tree.status is WorktreeStatus.ACTIVE
    assert store.get_worktree(task.id, "app") == tree
    assert store.get_worktree(task.id, "other") is None
    with pytest.raises(sqlite3.IntegrityError):
        store.add_worktree(
            task.id, "app", path=tmp_path, base_ref="main", base_sha="b" * 40, branch="x"
        )
    removed = store.mark_worktree_removed(tree.id)
    assert removed.status is WorktreeStatus.REMOVED
    assert removed.removed_at is not None
    assert store.list_worktrees(status=WorktreeStatus.ACTIVE) == []
    assert len(store.list_worktrees(task_id=task.id)) == 1


def test_publications_are_idempotent_on_marker(store):
    task = _task(store)
    first = store.start_publication(
        task.id, PublicationKind.PULL_REQUEST, "app", "marker-1", branch="opendot/task-1"
    )
    assert first.state is PublicationState.STARTED
    second = store.start_publication(task.id, PublicationKind.PULL_REQUEST, "app", "marker-1")
    assert second.id == first.id
    done = store.update_publication(
        first.id, state=PublicationState.PUBLISHED, number=7, url="https://example.com/pr/7"
    )
    assert done.state is PublicationState.PUBLISHED
    assert done.number == 7
    assert store.get_publication_by_marker("marker-1").url == "https://example.com/pr/7"
    assert store.get_publication_by_marker("nope") is None
    assert store.list_publications(states=[PublicationState.STARTED]) == []
    assert len(store.list_publications(task_id=task.id, kind=PublicationKind.PULL_REQUEST)) == 1
    with pytest.raises(ValueError):
        store.update_publication(first.id, marker="changed")
    with pytest.raises(NotFound):
        store.update_publication(999, state=PublicationState.FAILED)


def test_gateway_calls_link_to_attempt_later(store):
    task = _task(store)
    call = store.log_gateway_call(
        task.id,
        "step-abc",
        server="factiq",
        tool="search_series",
        mode=GatewayMode.READ,
        arguments={"query": "cpi"},
        status=GatewayCallStatus.OK,
        result_bytes=120,
        result_preview="{...}",
        duration_ms=45,
    )
    assert call.attempt_id is None
    assert call.arguments == {"query": "cpi"}
    store.log_gateway_call(
        task.id,
        "step-other",
        server="factiq",
        tool="send_feedback",
        mode=GatewayMode.WRITE,
        arguments={},
        status=GatewayCallStatus.REFUSED,
        error="write tools are actions",
    )
    attempt = store.start_attempt(task.id, Step.WORK, "fake")
    assert store.link_gateway_calls("step-abc", attempt.id) == 1
    calls = store.list_gateway_calls(task_id=task.id)
    assert [c.attempt_id for c in calls] == [attempt.id, None]
    assert len(store.list_gateway_calls(step_token="step-other")) == 1


def test_connector_tokens(store):
    assert store.get_connector_token("factiq") is None
    store.save_connector_token("factiq", "one", refresh_token="r", client_info={"id": "c"})
    store.save_connector_token("factiq", "two", scope="read")
    token = store.get_connector_token("factiq")
    assert token.access_token == "two"
    assert token.refresh_token is None
    assert token.scope == "read"
    assert store.delete_connector_token("factiq") is True
    assert store.delete_connector_token("factiq") is False
