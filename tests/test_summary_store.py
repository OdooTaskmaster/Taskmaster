"""Check summary round trips, immutable run keys and current read authorization."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
from unittest.mock import Mock
from typing import Any

import pytest

from adapters.sqlite_state_store import SQLiteStateStore
from application.assignee_risk import AssigneeRiskResult
from application.deadline_risk import DeadlineRiskResult, StalledDeadlineRiskResult
from application.inactivity_risk import InactivityRiskResult
from application.operation_store import OperationState, StateConflict, StateStoreError
from application.risk_status import RiskStatus
from application.risk_summary import (
    DeadlineDeviation, ProjectRiskSummary, SummaryAssignee, SummaryReadError, SummaryRow, SummaryStatus,
)
from application.summary_store import SavedSummary


_CHECKED: datetime = datetime(2026, 11, 25, 6, tzinfo=UTC)
_SUMMARY: ProjectRiskSummary = ProjectRiskSummary(
    SummaryStatus.COMPLETE,
    (
        SummaryRow(
            11, "Overdue task", (SummaryAssignee(7, "Demo member"),), date(2026, 11, 24), True,
            DeadlineDeviation(True, 1), "/odoo/tasks/11",
            (DeadlineRiskResult(RiskStatus.TRIGGERED, 1), InactivityRiskResult(RiskStatus.TRIGGERED, 8)),
        ),
        SummaryRow(
            12, "Close deadline", (), datetime(2026, 11, 28, 17, tzinfo=timezone(timedelta(hours=3))), True,
            DeadlineDeviation(False, 3), "/odoo/tasks/12",
            (StalledDeadlineRiskResult(RiskStatus.TRIGGERED, 3, 2), AssigneeRiskResult(RiskStatus.TRIGGERED)),
        ),
        SummaryRow(13, None, None, None, False, None, "/odoo/tasks/13", (InactivityRiskResult(RiskStatus.TRIGGERED, 9),)),
    ),
    ("RR-01", "RR-02", "RR-03", "RR-04"), (),
)


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStateStore:
    """Create the isolated adapter; store(tmp_path) returns a SQLite test store."""
    return SQLiteStateStore(tmp_path / "state.sqlite3")


def _save(store: SQLiteStateStore, summary: ProjectRiskSummary = _SUMMARY) -> SavedSummary:
    """Save a known run and recipient; _save(store) returns its stable saved result."""
    return store.save_summary("run-1", project_id=3, recipient_id=7, checked_at=_CHECKED, summary=summary)


def test_round_trip_keeps_typed_reasons_precision_and_result_reference(store: SQLiteStateStore, tmp_path: Path) -> None:
    """Reopen the file and link an operation to its exact result; returns None on success."""
    saved: SavedSummary = _save(store)
    store.accept("op", owner_id=7, project_id=3, recipient_id=7)
    store.transition("op", expected=OperationState.ACCEPTED, target=OperationState.RUNNING)
    store.transition("op", expected=OperationState.RUNNING, target=OperationState.SUCCEEDED, result_id=saved.result_id)
    reopened: SQLiteStateStore = SQLiteStateStore(tmp_path / "state.sqlite3")
    authorize: Mock = Mock(return_value=True)
    restored: SavedSummary | None = reopened.get_summary(
        saved.result_id, project_id=3, recipient_id=7, authorize=authorize,
    )
    assert restored == saved
    authorize.assert_called_once_with(saved)
    assert restored.summary == _SUMMARY
    assert type(restored.summary.rows[0].deadline) is date
    assert type(restored.summary.rows[1].deadline) is datetime
    assert type(restored.summary.rows[0].reasons[1]) is InactivityRiskResult
    operation = reopened.get_operation("op", owner_id=7, project_id=3)
    assert operation is not None and operation.result_id == restored.result_id


def test_identical_retry_and_conflicting_replacement(store: SQLiteStateStore, tmp_path: Path) -> None:
    """Keep one immutable row per run/project/recipient; returns None on success."""
    saved: SavedSummary = _save(store)
    assert _save(store) == saved
    assert _save(SQLiteStateStore(tmp_path / "state.sqlite3")) == saved
    for checked, summary in ((_CHECKED + timedelta(seconds=1), _SUMMARY), (_CHECKED, replace(_SUMMARY, rows=_SUMMARY.rows[:2]))):
        with pytest.raises(StateConflict):
            store.save_summary("run-1", project_id=3, recipient_id=7, checked_at=checked, summary=summary)
    with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM summary").fetchone() == (1,)


def test_concurrent_summary_retries_return_one_id(store: SQLiteStateStore, tmp_path: Path) -> None:
    """Race identical writes; test_concurrent_summary_retries_return_one_id(store, tmp_path) returns None."""
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(_save, store)
        second = pool.submit(_save, store)
        assert first.result() == second.result()
    with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM summary").fetchone() == (1,)


def test_concurrent_conflicting_snapshots_have_one_winner(store: SQLiteStateStore, tmp_path: Path) -> None:
    """Race two different results for one key; returns None when one wins and one conflicts."""
    def save(summary: ProjectRiskSummary) -> SavedSummary | None:
        """Return the saved summary or None on conflict; save(result) attempts the shared key."""
        try:
            return _save(store, summary)
        except StateConflict:
            return None

    with ThreadPoolExecutor(2) as pool:
        results: list[SavedSummary | None] = list(pool.map(save, (_SUMMARY, replace(_SUMMARY, rows=_SUMMARY.rows[:2]))))
    assert sum(result is not None for result in results) == 1
    with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM summary").fetchone() == (1,)


@pytest.mark.parametrize(("project", "recipient", "result"), [(4, 7, None), (3, 8, None), (3, 7, "missing")])
def test_scope_and_missing_are_indistinguishable(
    store: SQLiteStateStore, project: int, recipient: int, result: str | None,
) -> None:
    """Read the supplied scope or unknown ID without exposing existence; returns None on success."""
    saved: SavedSummary = _save(store)
    authorize: Mock = Mock(return_value=True)
    assert store.get_summary(result or saved.result_id, project_id=project, recipient_id=recipient, authorize=authorize) is None
    authorize.assert_not_called()


@pytest.mark.parametrize("lost_access", ["project", "task", "field", "assignee", "evidence"])
def test_revoked_access_hides_whole_result(store: SQLiteStateStore, lost_access: str) -> None:
    """Recheck the supplied permission after reading, denying every row and count; returns None."""
    saved: SavedSummary = _save(store)
    permissions: dict[str, bool] = dict.fromkeys(("project", "task", "field", "assignee", "evidence"), True)

    def authorize(result: SavedSummary) -> bool:
        """Validate every permission for this recipient; authorize(saved) returns the current verdict."""
        return result.project_id == 3 and result.recipient_id == 7 and all(permissions.values())

    assert store.get_summary(saved.result_id, project_id=3, recipient_id=7, authorize=authorize) == saved
    permissions[lost_access] = False
    denied: SavedSummary | None = store.get_summary(saved.result_id, project_id=3, recipient_id=7, authorize=authorize)
    assert denied is None  # No old rows, old status, coverage, counts, links or private names.


@pytest.mark.parametrize("status", list(SummaryStatus))
def test_all_summary_outcomes_require_current_access(store: SQLiteStateStore, status: SummaryStatus) -> None:
    """Round-trip the supplied outcome and deny it when access fails; returns None."""
    summary: ProjectRiskSummary = replace(
        _SUMMARY, status=status,
        rows=_SUMMARY.rows if status in (SummaryStatus.COMPLETE, SummaryStatus.PARTIAL) else (),
        checked_rules=_SUMMARY.checked_rules if status in (SummaryStatus.COMPLETE, SummaryStatus.EMPTY) else (),
        unchecked_rules=() if status in (SummaryStatus.COMPLETE, SummaryStatus.EMPTY) else _SUMMARY.checked_rules,
    )
    saved: SavedSummary = _save(store, summary)
    assert store.get_summary(saved.result_id, project_id=3, recipient_id=7, authorize=lambda _: True) == saved
    assert store.get_summary(saved.result_id, project_id=3, recipient_id=7, authorize=lambda _: False) is None


def test_unavailable_authorization_source_fails_closed(store: SQLiteStateStore) -> None:
    """Suppress a source-error message and result; test_unavailable_authorization_source_fails_closed(store) returns None."""
    saved: SavedSummary = _save(store)
    authorize: Mock = Mock(side_effect=SummaryReadError("raw source text and secret"))
    assert store.get_summary(saved.result_id, project_id=3, recipient_id=7, authorize=authorize) is None


def test_other_recipient_and_run_have_separate_keys(store: SQLiteStateStore) -> None:
    """Save distinct runs and recipients without collisions; returns None on success."""
    first: SavedSummary = _save(store)
    second: SavedSummary = store.save_summary("run-1", project_id=3, recipient_id=8, checked_at=_CHECKED, summary=_SUMMARY)
    third: SavedSummary = store.save_summary("run-2", project_id=3, recipient_id=7, checked_at=_CHECKED, summary=_SUMMARY)
    assert len({first.result_id, second.result_id, third.result_id}) == 3


@pytest.mark.parametrize("payload", ['{"version":99}', '{"version":1,"rows":[{}]}', 'private corrupt content'])
def test_corrupt_payload_never_reaches_authorizer(store: SQLiteStateStore, tmp_path: Path, payload: str) -> None:
    """Corrupt the file with the supplied payload and assert a fixed error; returns None."""
    saved: SavedSummary = _save(store)
    with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
        connection.execute("UPDATE summary SET payload = ?", (payload,))
    authorize: Mock = Mock(return_value=True)
    with pytest.raises(StateStoreError) as raised:
        store.get_summary(saved.result_id, project_id=3, recipient_id=7, authorize=authorize)
    assert "private" not in str(raised.value)
    authorize.assert_not_called()


def test_equal_instant_retry_is_idempotent(store: SQLiteStateStore) -> None:
    """Save equal instants in different zones without a conflict; returns None on success."""
    saved: SavedSummary = _save(store)
    row: SummaryRow = _SUMMARY.rows[1]
    assert isinstance(row.deadline, datetime)
    equivalent: ProjectRiskSummary = replace(_SUMMARY, rows=(
        _SUMMARY.rows[0], replace(row, deadline=row.deadline.astimezone(UTC)), _SUMMARY.rows[2],
    ))
    assert equivalent == _SUMMARY
    assert _save(store, equivalent) == saved


def test_unexpected_authorizer_error_is_sanitized(store: SQLiteStateStore) -> None:
    """Suppress an unexpected source error; returns None when a fixed error is raised."""
    saved: SavedSummary = _save(store)
    authorize: Mock = Mock(side_effect=RuntimeError("private backend secret"))
    with pytest.raises(StateStoreError) as raised:
        store.get_summary(saved.result_id, project_id=3, recipient_id=7, authorize=authorize)
    assert "private" not in str(raised.value)


def test_no_permissive_authorization_default(store: SQLiteStateStore) -> None:
    """Reject a missing or truthy non-bool authorizer; returns None on success."""
    saved: SavedSummary = _save(store)
    with pytest.raises(TypeError):
        store.get_summary(saved.result_id, project_id=3, recipient_id=7)  # type: ignore[call-arg]
    assert store.get_summary(saved.result_id, project_id=3, recipient_id=7, authorize=lambda _: [True]) is None


@pytest.mark.parametrize("corruption", ["coverage", "metric", "negative", "duplicate", "name", "status", "flag"])
def test_semantic_corruption_never_reaches_authorizer(
    store: SQLiteStateStore, tmp_path: Path, corruption: str,
) -> None:
    """Inject the supplied semantic corruption and assert a safe failure; returns None."""
    saved: SavedSummary = _save(store)
    with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
        data: dict[str, Any] = json.loads(connection.execute("SELECT payload FROM summary").fetchone()[0])
        if corruption == "coverage":
            data["checked_rules"] = ["RR-99"]
        elif corruption == "metric":
            data["rows"][0]["reasons"][0]["overdue_days"] = None
        elif corruption == "negative":
            data["rows"][0]["deviation"]["days"] = -1
        elif corruption == "duplicate":
            data["rows"].append(data["rows"][0])
        elif corruption == "name":
            data["rows"][0]["name"] = {"private": "unexpected object"}
        elif corruption == "status":
            data["status"] = "empty"
        else:
            data["rows"][0]["deadline_known"] = 1
        connection.execute("UPDATE summary SET payload = ?", (json.dumps(data),))
    authorize: Mock = Mock(return_value=True)
    with pytest.raises(StateStoreError):
        store.get_summary(saved.result_id, project_id=3, recipient_id=7, authorize=authorize)
    authorize.assert_not_called()
