"""Verify durable dispatch and at-most-once execution against local SQLite."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
from threading import Lock
from uuid import uuid4

import pytest

from adapters.sqlite_state_store import SQLiteStateStore
from application.background_operations import BackgroundOperations, ExecutionDenied
from application.operation_store import Operation, OperationError, OperationState, StateConflict, StateStoreError
from application.risk_summary import build_project_risk_summary
from application.summary_store import SavedSummary

_START: datetime = datetime(2026, 11, 27, 9, tzinfo=UTC)
_DELAY: timedelta = timedelta(seconds=10)


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStateStore:
    """Build an isolated store; requesting the fixture returns a fresh SQLite file."""
    return SQLiteStateStore(tmp_path / "state.sqlite3")


def _submit(background: BackgroundOperations, key: str = "op-1") -> Operation:
    """Submit the common synthetic scope; _submit(background) returns its operation."""
    return background.submit(key, owner_id=7, project_id=3, recipient_id=7)


def test_confirm_after_durable_acceptance(store: SQLiteStateStore) -> None:
    """Confirm only an existing operation, and never send it again after confirmation."""
    sent: list[str] = []

    def publish(key: str) -> bool:
        """Check the saved key during publication and return True as the broker would."""
        operation: Operation | None = store.get_background_operation(key)
        assert operation is not None and operation.state is OperationState.ACCEPTED
        sent.append(key)
        return True

    background: BackgroundOperations = BackgroundOperations(store, publish, retry_delay=_DELAY, clock=lambda: _START)
    assert _submit(background).state is OperationState.ACCEPTED
    assert _submit(background).operation_id == "op-1"
    assert store.due_dispatches(_START + 9 * _DELAY, limit=10) == ()
    assert sent == ["op-1"]
    assert store.get_operation("op-1", owner_id=8, project_id=3) is None


def test_submission_keeps_ordered_company_scope(store: SQLiteStateStore) -> None:
    """Persist ordered companies before dispatch and reject same-ID scope changes."""
    def publish(key: str) -> bool:
        """Confirm the opaque ID only after asserting the exact saved scope."""
        stored: Operation | None = store.get_background_operation(key)
        assert stored is not None and stored.company_ids == (5, 2)
        return True

    background: BackgroundOperations = BackgroundOperations(store, publish)
    operation: Operation = background.submit("op-1", owner_id=7, project_id=3, recipient_id=7, company_ids=(5, 2))
    assert operation.company_ids == (5, 2)
    with pytest.raises(StateConflict):
        background.submit("op-1", owner_id=7, project_id=3, recipient_id=7, company_ids=(2, 5))
    with pytest.raises(ValueError):
        background.submit("op-2", owner_id=7, project_id=3, recipient_id=7, company_ids=())


def test_unsent_operations_survive_store_restart(tmp_path: Path) -> None:
    """Recover the crash window between durable acceptance and the first publication."""
    path: Path = tmp_path / "background.sqlite3"
    SQLiteStateStore(path).accept_background("op-1", owner_id=7, project_id=3, recipient_id=7, now=_START)
    sent: list[str] = []

    def publish(key: str) -> bool:
        """Append the recovered ID and simulate a confirmed publication."""
        sent.append(key)
        return True

    background: BackgroundOperations = BackgroundOperations(SQLiteStateStore(path), publish, clock=lambda: _START)
    assert background.recover() == 1
    assert background.recover() == 0
    assert sent == ["op-1"]


@pytest.mark.parametrize("first_arrives", [False, True])
def test_recover_same_id_and_execute_only_once(store: SQLiteStateStore, first_arrives: bool) -> None:
    """Recover rejected or unconfirmed sends with the same ID and at most one run."""
    now: datetime = _START
    delivered: list[str] = []
    sent: list[str] = []
    runs: list[Operation] = []

    def publish(key: str) -> bool:
        """Lose the first confirmation; optionally keep its delivery in the fake broker."""
        sent.append(key)
        if first_arrives or len(sent) == 2:
            delivered.append(key)
        return len(sent) == 2

    def run(operation: Operation) -> str:
        """Record the trusted operation passed after claim and return its fake saved result ID."""
        runs.append(operation)
        return "result-1"

    background: BackgroundOperations = BackgroundOperations(store, publish, retry_delay=_DELAY, clock=lambda: now)
    _submit(background)
    assert background.recover() == 0
    now += _DELAY
    assert background.recover() == 1
    for key in delivered:
        background.execute(key, run)
    assert sent == ["op-1", "op-1"]
    assert len(runs) == 1 and runs[0].state is OperationState.RUNNING
    operation: Operation | None = store.get_background_operation("op-1")
    assert operation is not None and operation.state is OperationState.SUCCEEDED and operation.result_id == "result-1"


def test_failed_dispatch_budget_is_terminal(store: SQLiteStateStore) -> None:
    """Fail an unconfirmed operation after its last retry window and ignore late messages."""
    now: datetime = _START
    attempts: list[str] = []

    def publish(key: str) -> bool:
        """Append the attempted ID but return no broker confirmation."""
        attempts.append(key)
        return False

    background: BackgroundOperations = BackgroundOperations(
        store, publish, retry_delay=_DELAY, max_attempts=2, clock=lambda: now,
    )
    _submit(background)
    now += _DELAY
    assert background.recover() == 0
    operation: Operation | None = store.get_background_operation("op-1")
    assert operation is not None and operation.state is OperationState.ACCEPTED
    now += _DELAY
    assert background.recover() == 0
    assert store.due_dispatches(now, limit=10) == ()
    operation = store.get_background_operation("op-1")
    assert operation is not None and operation.state is OperationState.FAILED
    assert operation.error is OperationError.DISPATCH_FAILED and operation.result_id is None
    background.execute("op-1", lambda _: pytest.fail("late delivery must not execute"))
    assert attempts == ["op-1", "op-1"]


def test_submit_returns_failure_after_exhausting_dispatch_budget(store: SQLiteStateStore) -> None:
    """Return the same terminal status that the repeated submission writes to SQLite."""
    now: datetime = _START
    publisher_calls: list[str] = []

    def reject(key: str) -> bool:
        """Record the attempted ID and return an unconfirmed publication."""
        publisher_calls.append(key)
        return False

    background: BackgroundOperations = BackgroundOperations(
        store, reject, retry_delay=_DELAY, max_attempts=1, clock=lambda: now,
    )
    assert _submit(background).state is OperationState.ACCEPTED
    now += _DELAY
    failed: Operation = _submit(background)
    assert failed.state is OperationState.FAILED and failed.error is OperationError.DISPATCH_FAILED
    assert failed == store.get_background_operation("op-1") == _submit(background)
    assert publisher_calls == ["op-1"]


def test_recover_confirmation_that_could_not_be_saved(store: SQLiteStateStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Recover the same ID when the broker confirmed it but writing confirmation failed."""
    now: datetime = _START
    sent: list[str] = []

    def publish(key: str) -> bool:
        """Record the published opaque key and return a confirmation."""
        sent.append(key)
        return True

    def unavailable(key: str) -> None:
        """Fail the local write for the confirmed key without changing stored dispatch state."""
        assert key == "op-1"
        raise StateStoreError()

    background: BackgroundOperations = BackgroundOperations(store, publish, retry_delay=_DELAY, clock=lambda: now)
    with monkeypatch.context() as patch:
        patch.setattr(store, "confirm_dispatch", unavailable)
        with pytest.raises(StateStoreError):
            _submit(background)
    now += _DELAY
    assert background.recover() == 1
    assert sent == ["op-1", "op-1"]


def test_initial_submission_and_recovery_share_the_attempt_budget(store: SQLiteStateStore) -> None:
    """Make competing dispatchers reserve at most one attempt for the same due ID."""
    store.accept_background("op-1", owner_id=7, project_id=3, recipient_id=7, now=_START)
    sent: list[str] = []

    def publish(key: str) -> bool:
        """Record a send and leave the first attempt unconfirmed for the retry window."""
        sent.append(key)
        return False

    background: BackgroundOperations = BackgroundOperations(store, publish, retry_delay=_DELAY, clock=lambda: _START)
    with ThreadPoolExecutor(max_workers=2) as executor:
        submission = executor.submit(_submit, background)
        recovery = executor.submit(background.recover)
        assert submission.result().state is OperationState.ACCEPTED
        assert recovery.result() == 0
    assert sent == ["op-1"]
    assert store.due_dispatches(_START, limit=10) == ()


@pytest.mark.parametrize(("failure", "error"), [
    (ExecutionDenied(), OperationError.SOURCE_UNAVAILABLE),
    (RuntimeError("private source text"), OperationError.EXECUTION_FAILED),
])
def test_worker_failure_never_returns_source_details(store: SQLiteStateStore, failure: Exception,
                                                     error: OperationError) -> None:
    """Persist only safe failure categories for denied or failed executions."""
    def run(operation: Operation) -> str:
        """Raise the supplied failure after verifying the stored actor and project."""
        assert (operation.owner_id, operation.project_id, operation.recipient_id) == (7, 3, 7)
        raise failure

    background: BackgroundOperations = BackgroundOperations(store, lambda _: True)
    _submit(background)
    background.execute("op-1", run)
    background.execute("op-1", lambda _: pytest.fail("failed operations are terminal"))
    operation: Operation | None = store.get_background_operation("op-1")
    assert operation is not None and operation.state is OperationState.FAILED
    assert operation.error is error and operation.result_id is None


def test_concurrent_delivery_and_lost_execution(store: SQLiteStateStore) -> None:
    """Leave a lost claim in running and do not execute a concurrent or later duplicate."""
    count: int = 0
    lock: Lock = Lock()

    def run(operation: Operation) -> str:
        """Count starts and simulate process loss after the persistent claim."""
        nonlocal count
        assert operation.state is OperationState.RUNNING
        with lock:
            count += 1
        raise KeyboardInterrupt

    background: BackgroundOperations = BackgroundOperations(store, lambda _: True)
    _submit(background)
    with ThreadPoolExecutor(max_workers=2) as executor:
        deliveries = [executor.submit(background.execute, "op-1", run) for _ in range(2)]
        lost: int = 0
        for item in deliveries:
            try:
                item.result()
            except KeyboardInterrupt:
                lost += 1
    assert count == lost == 1
    background.execute("op-1", lambda _: pytest.fail("the running attempt cannot restart"))
    operation: Operation | None = store.get_background_operation("op-1")
    assert operation is not None and operation.state is OperationState.RUNNING


def test_redelivery_recovers_saved_work_after_failed_terminal_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Recover a committed snapshot after a lost terminal write without rerunning the scenario."""
    path: Path = tmp_path / "state.sqlite3"
    store: SQLiteStateStore = SQLiteStateStore(path)
    transition: Callable[..., Operation] = store.transition
    saved: list[SavedSummary] = []

    def intermittent(key: str, *, expected: OperationState, target: OperationState,
                     result_id: str | None = None, error: OperationError | None = None) -> Operation:
        """Commit the claim but simulate a storage outage while publishing the result reference."""
        if target is OperationState.SUCCEEDED:
            raise StateStoreError()
        return transition(key, expected=expected, target=target, result_id=result_id, error=error)

    def run(operation: Operation) -> str:
        """Persist a real, empty snapshot for the claimed scope and return its ID."""
        snapshot: SavedSummary = store.save_summary(
            operation.operation_id, project_id=operation.project_id, recipient_id=operation.recipient_id,
            checked_at=_START, summary=build_project_risk_summary(lambda: (), checked_at=_START, project_timezone=UTC),
        )
        saved.append(snapshot)
        return snapshot.result_id

    background: BackgroundOperations = BackgroundOperations(store, lambda _: True)
    _submit(background)
    with monkeypatch.context() as patch:
        patch.setattr(store, "transition", intermittent)
        with pytest.raises(StateStoreError):
            background.execute("op-1", run)
    operation: Operation | None = store.get_background_operation("op-1")
    assert operation is not None and operation.state is OperationState.RUNNING and operation.result_id is None
    assert len(saved) == 1

    reopened: SQLiteStateStore = SQLiteStateStore(path)
    restarted: BackgroundOperations = BackgroundOperations(reopened, lambda _: True)
    restarted.execute("op-1", run)
    succeeded: Operation | None = reopened.get_background_operation("op-1")
    assert succeeded is not None and succeeded.state is OperationState.SUCCEEDED
    assert succeeded.result_id == saved[0].result_id and succeeded.error is None
    restarted.execute("op-1", run)
    assert len(saved) == 1 and reopened.get_background_operation("op-1") == succeeded
    assert reopened.get_summary(succeeded.result_id, project_id=3, recipient_id=7, authorize=lambda _: False) is None


def test_redelivery_does_not_link_summary_from_another_scope(store: SQLiteStateStore) -> None:
    """Ignore persisted results that differ by run ID, project or recipient."""
    background: BackgroundOperations = BackgroundOperations(store, lambda _: True)
    _submit(background)
    running: Operation = store.transition("op-1", expected=OperationState.ACCEPTED, target=OperationState.RUNNING)
    for key, project_id, recipient_id in (("op-2", 3, 7), ("op-1", 4, 7), ("op-1", 3, 8)):
        store.save_summary(key, project_id=project_id, recipient_id=recipient_id, checked_at=_START,
                           summary=build_project_risk_summary(lambda: (), checked_at=_START, project_timezone=UTC))
    background.execute("op-1", lambda _: pytest.fail("a running job must not call the scenario again"))
    assert store.get_background_operation("op-1") == running


def test_plain_operations_are_not_enrolled_in_recovery(store: SQLiteStateStore) -> None:
    """Keep old or plain jobs out of new dispatch and trust only validated keys."""
    store.accept("plain", owner_id=7, project_id=3, recipient_id=7)
    unexpected: Callable[[str], bool] = lambda _: pytest.fail("plain and invalid IDs cannot publish")
    background: BackgroundOperations = BackgroundOperations(store, unexpected)
    assert background.recover() == 0
    with pytest.raises(StateConflict):
        _submit(background, "plain")
    with pytest.raises(ValueError):
        _submit(background, "../invalid")
    background.execute("plain", lambda _: pytest.fail("plain operation cannot execute"))


def test_existing_store_upgrade_keeps_status_and_does_not_schedule_old_rows(tmp_path: Path) -> None:
    """Open the pre-background schema without enrolling or changing existing operations."""
    path: Path = tmp_path / "legacy.sqlite3"
    key: str = str(uuid4())
    connection: sqlite3.Connection = sqlite3.connect(path)
    try:
        with connection:
            connection.execute("CREATE TABLE operation (operation_id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, "
                               "project_id INTEGER NOT NULL, recipient_id INTEGER NOT NULL, state TEXT NOT NULL, "
                               "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, result_id TEXT, error TEXT)")
            connection.execute("INSERT INTO operation VALUES (?, 7, 3, 7, 'accepted', ?, ?, NULL, NULL)",
                               (key, _START.isoformat(), _START.isoformat()))
    finally:
        connection.close()
    original: Operation | None = SQLiteStateStore(path, read_only=True).get_operation(key, owner_id=7, project_id=3)
    upgraded: SQLiteStateStore = SQLiteStateStore(path)
    assert original is not None and upgraded.get_operation(key, owner_id=7, project_id=3) == original
    assert upgraded.get_background_operation(key) is None
    assert upgraded.due_dispatches(_START, limit=10) == ()
    assert upgraded.accept_background("new", owner_id=7, project_id=3, recipient_id=7, now=_START).state is OperationState.ACCEPTED
    assert upgraded.due_dispatches(_START, limit=10) == ("new",)
