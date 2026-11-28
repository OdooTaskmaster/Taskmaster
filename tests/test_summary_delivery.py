"""Check the authorization and read-only storage used by the summary routes."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from adapters import odoo_summary_access as access
from adapters import odoo_summary_source as source
from adapters.sqlite_state_store import SQLiteStateStore
from application.operation_store import StateStoreError
from application.risk_summary import ProjectRiskSummary, SummaryReadError, SummaryStatus, SummaryTask, build_project_risk_summary
from application.summary_store import SavedSummary

_NOW: datetime = datetime(2026, 11, 27, tzinfo=UTC)
_RULES: tuple[str, ...] = ("RR-01", "RR-02", "RR-03", "RR-04")
_EMPTY: ProjectRiskSummary = ProjectRiskSummary(SummaryStatus.EMPTY, (), _RULES, (), ())


def test_saved_result_cannot_outlive_access_to_a_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assert reauthorization of non-row sources and fields; return None on success."""
    monkeypatch.setitem(sys.modules, "odoo.exceptions", SimpleNamespace(AccessError=ValueError, MissingError=KeyError))
    env: Mock = Mock(uid=7)
    project: Mock = Mock(return_value=True)
    task: dict[str, object] = dict.fromkeys(access._FIELDS, False)
    task["id"] = 12
    projection: Mock = Mock(return_value=(task,))
    monkeypatch.setattr(access, "readable_projects", project)
    monkeypatch.setattr(access, "_read_task_projection", projection)
    saved: SavedSummary = SavedSummary("result", "run", 3, 7, _NOW, replace(_EMPTY, source_task_ids=(12,)))

    assert access.can_read_summary(env, saved)
    assert projection.call_args.kwargs["task_ids"] == (12,)
    del task["date_deadline"]
    assert not access.can_read_summary(env, saved)
    projection.return_value = ()
    assert not access.can_read_summary(env, saved)
    project.return_value = False
    assert not access.can_read_summary(env, replace(saved, summary=_EMPTY))
    project.reset_mock()
    assert not access.can_read_summary(env, replace(saved, summary=replace(_EMPTY, source_task_ids=None)))
    assert not access.can_read_summary(env, replace(saved, recipient_id=8))
    project.assert_not_called()


def test_latest_summary_never_falls_back_or_crosses_scope(tmp_path: Path) -> None:
    """Select and authorize only the newest in-scope record; return None on success."""
    path: Path = tmp_path / "state.sqlite3"
    store: SQLiteStateStore = SQLiteStateStore(path)
    old: SavedSummary = store.save_summary("old", project_id=3, recipient_id=7, checked_at=_NOW, summary=_EMPTY)
    new: SavedSummary = store.save_summary("new", project_id=3, recipient_id=7, checked_at=_NOW + timedelta(days=1), summary=_EMPTY)
    store.save_summary("other", project_id=3, recipient_id=8, checked_at=_NOW + timedelta(days=2), summary=_EMPTY)
    reader: SQLiteStateStore = SQLiteStateStore(path, read_only=True)
    check: Mock = Mock(return_value=False)

    assert reader.get_latest_summary(project_id=3, recipient_id=7, authorize=check) is None
    check.assert_called_once_with(new)
    assert reader.get_summary(old.result_id, project_id=3, recipient_id=7, authorize=lambda _: True) == old
    with pytest.raises(StateStoreError):
        reader.accept("op", owner_id=7, project_id=3, recipient_id=7)


def test_read_only_store_does_not_create_missing_file(tmp_path: Path) -> None:
    """Assert an absent file stays absent after a failed read; return None on success."""
    path: Path = tmp_path / "missing.sqlite3"
    reader: SQLiteStateStore = SQLiteStateStore(path, read_only=True)
    with pytest.raises(StateStoreError):
        reader.get_operation("op", owner_id=7, project_id=3)
    assert not path.exists()


def test_generation_keeps_unknown_assignments_distinct_from_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assert a filtered relation is not treated as empty; return None on success."""
    monkeypatch.setitem(sys.modules, "odoo.exceptions", SimpleNamespace(AccessError=ValueError, MissingError=KeyError))
    monkeypatch.setattr(source, "readable_projects", Mock(return_value=True))
    rows: tuple[dict[str, object], ...] = tuple(
        {"id": task_id, "name": "Demo", "active": True, "state": "01_in_progress", "is_closed": False,
         "date_deadline": "2026-11-25 17:00:00", "user_ids": []}
        for task_id in (12, 13)
    )
    projection: Mock = Mock(return_value=rows)
    monkeypatch.setattr(source, "_read_task_projection", projection)
    search: Mock = Mock(return_value=SimpleNamespace(ids=[12]))
    env: dict[str, SimpleNamespace] = {"project.task": SimpleNamespace(search=search)}

    snapshots: tuple[SummaryTask, ...] = source.read_summary_tasks(env, 3)
    search.assert_called_once_with([("project_id", "=", 3), ("id", "in", [12, 13]), ("user_ids", "=", False)])
    assert snapshots[0].assignees == () and snapshots[1].assignees is None
    assert snapshots[0].snapshot.deadline == datetime(2026, 11, 25, 17, tzinfo=UTC)
    summary: ProjectRiskSummary = build_project_risk_summary(lambda: snapshots, checked_at=_NOW, project_timezone=UTC)
    assert summary.status is SummaryStatus.PARTIAL
    assert summary.source_task_ids == (12, 13)
    assert [[reason.rule_id for reason in row.reasons] for row in summary.rows] == [["RR-01", "RR-03"], ["RR-01"]]


def test_generation_denies_project_before_task_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assert a denied project never reaches the task projection; return None on success."""
    monkeypatch.setitem(sys.modules, "odoo.exceptions", SimpleNamespace(AccessError=ValueError, MissingError=KeyError))
    monkeypatch.setattr(source, "readable_projects", Mock(return_value=False))
    projection: Mock = Mock()
    monkeypatch.setattr(source, "_read_task_projection", projection)
    with pytest.raises(SummaryReadError):
        source.read_summary_tasks(Mock(), 3)
    projection.assert_not_called()
