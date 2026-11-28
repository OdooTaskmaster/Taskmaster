"""Check immutable operation scope, persisted transitions and concurrent updates."""

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import UTC, datetime
import multiprocessing
from multiprocessing.synchronize import Barrier
from pathlib import Path
import sqlite3

import pytest

from adapters.sqlite_state_store import SQLiteStateStore
from application.operation_store import Operation, OperationError, OperationState, StateConflict, StateStoreError

_START: datetime = datetime(2026, 11, 25, 4, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStateStore:
    """Create an isolated file; store(tmp_path) returns a SQLite test adapter."""
    return SQLiteStateStore(tmp_path / "state.sqlite3")


def test_reopen_and_idempotent_accept(store: SQLiteStateStore, tmp_path: Path) -> None:
    """Persist scope and time, then reopen; this test returns None on success."""
    operation: Operation = store.accept("op-1", owner_id=7, project_id=3, recipient_id=8)
    assert operation.state is OperationState.ACCEPTED
    assert operation.created_at.utcoffset() is not None
    reopened: SQLiteStateStore = SQLiteStateStore(tmp_path / "state.sqlite3")
    assert reopened.accept("op-1", owner_id=7, project_id=3, recipient_id=8) == operation
    assert reopened.get_operation("op-1", owner_id=7, project_id=3) == operation
    for owner, project in ((8, 3), (7, 4)):
        assert reopened.get_operation("op-1", owner_id=owner, project_id=project) is None
    assert reopened.get_operation("absent", owner_id=7, project_id=3) is None
    with pytest.raises(StateConflict):
        store.accept("op-1", owner_id=8, project_id=3, recipient_id=8)


@pytest.mark.parametrize("terminal", [OperationState.SUCCEEDED, OperationState.FAILED])
def test_terminal_state_never_regresses(store: SQLiteStateStore, tmp_path: Path, terminal: OperationState) -> None:
    """Advance the supplied terminal outcome and reject stale updates; return None."""
    store.accept("op", owner_id=7, project_id=3, recipient_id=7)
    running: Operation = store.transition("op", expected=OperationState.ACCEPTED, target=OperationState.RUNNING)
    assert running.state is OperationState.RUNNING
    with pytest.raises(StateConflict):
        store.transition("op", expected=OperationState.ACCEPTED, target=OperationState.RUNNING)
    result_id: str | None = "summary-1" if terminal is OperationState.SUCCEEDED else None
    error: OperationError | None = OperationError.EXECUTION_FAILED if terminal is OperationState.FAILED else None
    completed: Operation = store.transition("op", expected=OperationState.RUNNING, target=terminal, result_id=result_id, error=error)
    assert store.transition("op", expected=OperationState.RUNNING, target=terminal, result_id=result_id, error=error) == completed
    reopened: SQLiteStateStore = SQLiteStateStore(tmp_path / "state.sqlite3")
    assert reopened.get_operation("op", owner_id=7, project_id=3) == completed
    assert reopened.accept("op", owner_id=7, project_id=3, recipient_id=7) == completed
    for target in (OperationState.ACCEPTED, OperationState.RUNNING):
        with pytest.raises(StateConflict):
            store.transition("op", expected=terminal, target=target)
    if result_id:
        with pytest.raises(StateConflict):
            store.transition("op", expected=terminal, target=terminal, result_id="replacement")
        with pytest.raises(StateConflict):
            store.transition("op", expected=OperationState.ACCEPTED, target=terminal, result_id=result_id)


def test_failed_before_running_and_invalid_transitions(store: SQLiteStateStore) -> None:
    """Fail a rejected start, preserving the error category; returns None on success."""
    store.accept("op", owner_id=7, project_id=3, recipient_id=7)
    with pytest.raises(StateConflict):
        store.transition("op", expected=OperationState.ACCEPTED, target=OperationState.SUCCEEDED, result_id="r")
    with pytest.raises(StateConflict):
        store.transition("op", expected=OperationState.RUNNING, target=OperationState.SUCCEEDED, result_id="r")
    with pytest.raises(ValueError):
        store.transition("op", expected=OperationState.ACCEPTED, target=OperationState.FAILED, error="raw secret")
    failed: Operation = store.transition(
        "op", expected=OperationState.ACCEPTED, target=OperationState.FAILED, error=OperationError.SOURCE_UNAVAILABLE,
    )
    assert failed.error is OperationError.SOURCE_UNAVAILABLE


def test_concurrent_terminal_updates_have_one_winner(store: SQLiteStateStore) -> None:
    """Race two result IDs; test_concurrent_terminal_updates_have_one_winner(store) returns None."""
    store.accept("op", owner_id=7, project_id=3, recipient_id=7)
    store.transition("op", expected=OperationState.ACCEPTED, target=OperationState.RUNNING)

    def complete(result_id: str) -> Operation | None:
        """Return the winning operation or None for conflict; complete('a') attempts result a."""
        try:
            return store.transition("op", expected=OperationState.RUNNING, target=OperationState.SUCCEEDED, result_id=result_id)
        except StateConflict:
            return None

    with ThreadPoolExecutor(2) as pool:
        results: list[Operation | None] = list(pool.map(complete, ("a", "b")))
    assert sum(result is not None for result in results) == 1


def test_storage_errors_hide_path_and_sql(tmp_path: Path) -> None:
    """Open a missing parent and corrupt file, exposing no raw details; return None."""
    for path in (tmp_path / "private-name" / "state.sqlite3", tmp_path / "corrupt.sqlite3"):
        if path.parent == tmp_path:
            path.write_bytes(b"private raw data")
        with pytest.raises(StateStoreError) as raised:
            SQLiteStateStore(path)
        assert "private" not in str(raised.value) and "private" not in repr(raised.value)


def test_sqlite_mode_and_busy_failure(store: SQLiteStateStore, tmp_path: Path) -> None:
    """Verify DELETE and bounded lock failure; this test returns None on success."""
    with sqlite3.connect(tmp_path / "state.sqlite3") as lock:
        assert lock.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        lock.execute("BEGIN IMMEDIATE")
        with pytest.raises(StateStoreError):
            store.accept("op", owner_id=7, project_id=3, recipient_id=7)
        lock.rollback()
    assert store.get_operation("op", owner_id=7, project_id=3) is None


def _claim_file(path: Path) -> bool:
    """Attempt the one worker claim in path; _claim_file(path) returns True only for the winner."""
    store: SQLiteStateStore = SQLiteStateStore(path)
    try:
        store.transition("op", expected=OperationState.ACCEPTED, target=OperationState.RUNNING)
        return True
    except StateConflict:
        return False


def _wait_start(barrier: Barrier) -> None:
    """Synchronize process starts; _wait_start(barrier) returns None when both are ready."""
    barrier.wait(timeout=15)


def test_two_process_claim_has_one_winner(store: SQLiteStateStore, tmp_path: Path) -> None:
    """Claim from two spawned processes; this test returns None with exactly one winner."""
    store.accept("op", owner_id=7, project_id=3, recipient_id=7)
    context = multiprocessing.get_context("spawn")
    barrier: Barrier = context.Barrier(2)
    with ProcessPoolExecutor(2, mp_context=context, initializer=_wait_start, initargs=(barrier,)) as pool:
        results: list[bool] = list(pool.map(_claim_file, [tmp_path / "state.sqlite3"] * 2))
    assert sorted(results) == [False, True]


@pytest.mark.parametrize(("owner", "project", "recipient"), [(8, 3, 7), (7, 4, 7), (7, 3, 8)])
def test_all_scope_components_are_immutable(store: SQLiteStateStore, owner: int, project: int, recipient: int) -> None:
    """Reject the supplied changed scope for a known key; returns None on success."""
    store.accept("op", owner_id=7, project_id=3, recipient_id=7)
    with pytest.raises(StateConflict):
        store.accept("op", owner_id=owner, project_id=project, recipient_id=recipient)


@pytest.mark.parametrize("owner", [True, 1.5, 0, -1])
def test_invalid_scope_ids(store: SQLiteStateStore, owner: int) -> None:
    """Reject the supplied invalid owner before SQL; returns None on success."""
    with pytest.raises(ValueError):
        store.accept("op", owner_id=owner, project_id=3, recipient_id=7)


def test_corrupt_operation_time_fails_safely(store: SQLiteStateStore, tmp_path: Path) -> None:
    """Reject a naive persisted timestamp; returns None when no invalid operation escapes."""
    store.accept("op", owner_id=7, project_id=3, recipient_id=7)
    with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
        connection.execute("UPDATE operation SET updated_at = '2026-11-25T06:00:00'")
    with pytest.raises(StateStoreError):
        store.get_operation("op", owner_id=7, project_id=3)


def test_existing_wal_file_is_not_silently_changed(tmp_path: Path) -> None:
    """Reject WAL without changing its mode; returns None on success."""
    path: Path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        with pytest.raises(StateStoreError):
            SQLiteStateStore(path)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)


def test_scheduled_flag_round_trips_and_defaults_false(store: SQLiteStateStore, tmp_path: Path) -> None:
    """Persist explicit scheduled and manual jobs; returns None on success."""
    manual: Operation = store.accept_background(
        "manual", owner_id=7, project_id=3, recipient_id=7, now=_START, project_timezone="Asia/Tokyo",
    )
    scheduled: Operation = store.accept_background(
        "scheduled", owner_id=7, project_id=3, recipient_id=7, now=_START,
        project_timezone="Asia/Tokyo", scheduled=True,
    )
    plain: Operation = store.accept("plain", owner_id=7, project_id=3, recipient_id=7)
    assert manual.scheduled is False and manual.project_timezone == "Asia/Tokyo"
    assert scheduled.scheduled is True and scheduled.project_timezone == "Asia/Tokyo"
    assert plain.scheduled is False and plain.project_timezone is None
    reopened: SQLiteStateStore = SQLiteStateStore(tmp_path / "state.sqlite3")
    assert reopened.get_operation("manual", owner_id=7, project_id=3) == manual
    assert reopened.get_background_operation("scheduled") == scheduled
    assert reopened.get_operation("plain", owner_id=7, project_id=3) == plain


def test_scheduled_flag_is_immutable_for_same_key(store: SQLiteStateStore) -> None:
    """Reject a changed scheduled flag for a known key; returns None on success."""
    accepted: Operation = store.accept_background(
        "op", owner_id=7, project_id=3, recipient_id=7, now=_START, scheduled=True,
    )
    assert store.accept_background(
        "op", owner_id=7, project_id=3, recipient_id=7, now=_START, scheduled=True,
    ) == accepted
    with pytest.raises(StateConflict):
        store.accept_background("op", owner_id=7, project_id=3, recipient_id=7, now=_START, scheduled=False)


@pytest.mark.parametrize("scheduled", [1, 0, "yes", None])
def test_invalid_scheduled_flag_is_rejected(store: SQLiteStateStore, scheduled: object) -> None:
    """Reject a non-boolean scheduled flag before SQL; returns None on success."""
    with pytest.raises(ValueError):
        store.accept_background(
            "op", owner_id=7, project_id=3, recipient_id=7, now=_START, scheduled=scheduled,  # type: ignore[arg-type]
        )


def _legacy_operation_database(path: Path) -> None:
    """Create the pre-scheduled schema with one zoned and one plain row; returns None."""
    created: str = _START.isoformat()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE operation (operation_id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, "
            "project_id INTEGER NOT NULL, recipient_id INTEGER NOT NULL, state TEXT NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, result_id TEXT, error TEXT, "
            "dispatch_due_at REAL, dispatch_attempts INTEGER NOT NULL DEFAULT 0, "
            "dispatch_confirmed INTEGER NOT NULL DEFAULT 0, company_ids TEXT, project_timezone TEXT)"
        )
        connection.execute(
            "INSERT INTO operation VALUES ('scheduled', 7, 3, 7, 'accepted', ?, ?, NULL, NULL, ?, 0, 0, NULL, "
            "'Asia/Tokyo')",
            (created, created, _START.timestamp()),
        )
        connection.execute(
            "INSERT INTO operation VALUES ('manual', 7, 3, 7, 'accepted', ?, ?, NULL, NULL, ?, 0, 0, NULL, NULL)",
            (created, created, _START.timestamp()),
        )


def test_legacy_rows_infer_scheduled_from_project_timezone(tmp_path: Path) -> None:
    """Read and upgrade a pre-scheduled file without changing its meaning; returns None."""
    path: Path = tmp_path / "legacy.sqlite3"
    _legacy_operation_database(path)
    legacy: SQLiteStateStore = SQLiteStateStore(path, read_only=True)
    zoned: Operation | None = legacy.get_operation("scheduled", owner_id=7, project_id=3)
    plain: Operation | None = legacy.get_operation("manual", owner_id=7, project_id=3)
    assert zoned is not None and zoned.scheduled is True
    assert plain is not None and plain.scheduled is False
    upgraded: SQLiteStateStore = SQLiteStateStore(path)
    assert upgraded.get_operation("scheduled", owner_id=7, project_id=3) == zoned
    assert upgraded.get_operation("manual", owner_id=7, project_id=3) == plain
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT operation_id, scheduled FROM operation ORDER BY operation_id",
        ).fetchall() == [("manual", 0), ("scheduled", 1)]


def test_new_manual_job_with_timezone_is_not_scheduled(store: SQLiteStateStore, tmp_path: Path) -> None:
    """Keep project_timezone for a manual job without inferring scheduled; returns None."""
    manual: Operation = store.accept_background(
        "manual", owner_id=7, project_id=3, recipient_id=7, now=_START,
        project_timezone="Europe/Moscow", scheduled=False,
    )
    reopened: SQLiteStateStore = SQLiteStateStore(tmp_path / "state.sqlite3")
    restored: Operation | None = reopened.get_operation("manual", owner_id=7, project_id=3)
    assert restored == manual and restored.scheduled is False and restored.project_timezone == "Europe/Moscow"
