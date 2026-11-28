"""Check project-local scheduling against a real SQLite store and controlled instants."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from adapters.sqlite_state_store import SQLiteStateStore
from application.morning_schedule import ScheduleRefused, schedule_morning_check
from application.operation_store import Operation, OperationState, StateConflict

_DUE: datetime = datetime(2026, 11, 28, 4, tzinfo=UTC)


@pytest.mark.parametrize(("zone", "utc_due"), [
    ("Asia/Yekaterinburg", _DUE),
    ("Pacific/Kiritimati", datetime(2026, 11, 27, 19, tzinfo=UTC)),
    ("Europe/Berlin", datetime(2026, 7, 1, 7, tzinfo=UTC)),
    ("Europe/Berlin", datetime(2026, 12, 1, 8, tzinfo=UTC)),
])
def test_local_window_and_repeated_tick(tmp_path: Path, zone: str, utc_due: datetime) -> None:
    """Test local 08:59:59, 09:00, retry, 09:05 and the following local day."""
    path: Path = tmp_path / "state.sqlite3"
    authorize: Mock = Mock(return_value=(8,))

    def tick(now: datetime) -> Operation | None:
        """Run one controlled tick; tick(utc_due) accepts the local slot."""
        return schedule_morning_check(3, zone, 7, now=now, authorize=authorize,
                                      open_store=lambda: SQLiteStateStore(path))

    assert tick(utc_due - timedelta(seconds=1)) is None
    assert tick(utc_due + timedelta(minutes=5)) is None
    authorize.assert_not_called()
    assert not path.exists()
    operation: Operation | None = tick(utc_due)
    assert operation is not None
    assert operation.state is OperationState.ACCEPTED
    assert (operation.owner_id, operation.project_id, operation.recipient_id) == (7, 3, 7)
    assert operation.company_ids == (8,)
    assert operation.project_timezone == zone
    assert operation.scheduled
    assert tick(utc_due) == tick(utc_due + timedelta(minutes=4, seconds=59)) == operation
    assert SQLiteStateStore(path).due_dispatches(utc_due, limit=10) == (operation.operation_id,)
    tomorrow: Operation | None = tick(utc_due + timedelta(days=1))
    assert tomorrow is not None and tomorrow.operation_id != operation.operation_id


@pytest.mark.parametrize(("zone", "recipient", "reason"), [
    (None, 7, "missing_timezone"), (" ", 7, "missing_timezone"),
    ("Not/AZone", 7, "invalid_timezone"), ("/UTC", 7, "invalid_timezone"), ("localtime", 7, "invalid_timezone"),
    ("Asia/Yekaterinburg", None, "missing_recipient"), ("Asia/Yekaterinburg", True, "missing_recipient"),
    ("Asia/Yekaterinburg", 7, "recipient_unavailable"),
])
def test_refusals_do_not_open_state(zone: str | None, recipient: int | None, reason: str) -> None:
    """Reject incomplete configuration and unavailable recipients before opening SQLite."""
    open_store: Mock = Mock()
    with pytest.raises(ScheduleRefused, match=f"^{reason}$"):
        schedule_morning_check(3, zone, recipient, now=_DUE, authorize=lambda _: None, open_store=open_store)
    open_store.assert_not_called()


def test_naive_clock_is_refused() -> None:
    """Reject a clock without a time zone instead of implicitly using the host time."""
    open_store: Mock = Mock()
    with pytest.raises(ValueError, match="timezone-aware"):
        schedule_morning_check(3, "UTC", 7, now=_DUE.replace(tzinfo=None),
                               authorize=lambda _: (8,), open_store=open_store)
    open_store.assert_not_called()


def test_missing_recipient_is_reported_only_in_the_local_window() -> None:
    """Outside the slot avoid repeated recipient warnings and do not open SQLite."""
    open_store: Mock = Mock()
    assert schedule_morning_check(3, "Asia/Yekaterinburg", None, now=_DUE - timedelta(seconds=1),
                                  authorize=lambda _: None, open_store=open_store) is None
    open_store.assert_not_called()


def test_concurrent_and_changed_recipient_ticks_do_not_duplicate(tmp_path: Path) -> None:
    """Reserve the same canonical slot from independent writers, even if the scope changes."""
    path: Path = tmp_path / "state.sqlite3"

    def tick(recipient: int, project: int = 3) -> Operation | None:
        """Attempt the same local slot as this recipient for the selected project."""
        return schedule_morning_check(project, "Asia/Yekaterinburg", recipient, now=_DUE,
                                      authorize=lambda _: (8,), open_store=lambda: SQLiteStateStore(path))

    with ThreadPoolExecutor(2) as workers:
        first, second = workers.map(tick, (7, 7))
    assert first is not None and first == second
    with pytest.raises(StateConflict):
        tick(9)
    store: SQLiteStateStore = SQLiteStateStore(path)
    assert store.due_dispatches(_DUE, limit=10) == (first.operation_id,)
    assert store.get_background_operation(first.operation_id) == first
    other: Operation | None = tick(7, project=4)
    assert other is not None and other.operation_id != first.operation_id


def test_timezone_change_across_utc_dates_cannot_create_another_local_slot(tmp_path: Path) -> None:
    """Use the local day for identity even when two 09:00 instants have different UTC dates."""
    store: SQLiteStateStore = SQLiteStateStore(tmp_path / "state.sqlite3")
    earlier: datetime = datetime(2026, 11, 27, 19, tzinfo=UTC)
    first: Operation | None = schedule_morning_check(
        3, "Pacific/Kiritimati", 7, now=earlier, authorize=lambda _: (8,), open_store=lambda: store,
    )
    assert first is not None
    with pytest.raises(StateConflict):
        schedule_morning_check(3, "Asia/Yekaterinburg", 7, now=_DUE, authorize=lambda _: (8,), open_store=lambda: store)
    assert store.due_dispatches(_DUE, limit=10) == (first.operation_id,)
