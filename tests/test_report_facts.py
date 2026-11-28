"""Verify scoped report data and the difference between empty and incomplete."""

from datetime import UTC, date, datetime
from decimal import Decimal
import sys
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from adapters import odoo_report_facts as adapter
from application import report_facts as core
from application.risk_summary import ProjectRiskSummary, SummaryStatus
from application.summary_store import SavedSummary

_DAY: date = date(2026, 11, 27)
_PERIOD: core.ReportPeriod = core.ReportPeriod(_DAY, _DAY)
_NOW: datetime = datetime(2026, 11, 27, 12, tzinfo=UTC)
_ZONE: ZoneInfo = ZoneInfo("Europe/Moscow")


class FakeEnv(dict):
    """Expose the selected ORM projections; FakeEnv() has no project rows."""

    uid: int = 7
    su: bool = False
    registry: SimpleNamespace = SimpleNamespace(
        _init_modules={"hr_timesheet"}, models={"account.analytic.line": object()},
    )
    ref: Mock


def _model(rows: list[dict[str, object]]) -> Mock:
    """Build an ORM double with the requested fields; _model([]) reads no rows."""
    model: Mock = Mock()
    model.fields_get.side_effect = lambda *, allfields, attributes: dict.fromkeys(allfields)
    model.search_read.return_value = rows
    model.with_context.return_value = model
    return model


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> FakeEnv:
    """Bind an ordinary recipient and mock Odoo module; the fixture authorizes project 4."""
    monkeypatch.setitem(sys.modules, "odoo", SimpleNamespace(SUPERUSER_ID=1))
    monkeypatch.setitem(sys.modules, "odoo.exceptions", SimpleNamespace(AccessError=PermissionError, MissingError=LookupError))
    project: Mock = _model([])
    project.allow_timesheets = True
    monkeypatch.setattr(adapter, "readable_projects", Mock(return_value=project))
    env: FakeEnv = FakeEnv({name: _model([]) for name in ("project.task", "mail.message", "account.analytic.line", "uom.uom")})
    env["uom.uom"].search.return_value = SimpleNamespace(ids=[1])
    env["project.task"].search.return_value = SimpleNamespace(ids=[])
    env.ref = Mock(return_value=SimpleNamespace(id=14))
    return env


def _read(env: FakeEnv, **extra: object) -> core.ReportFacts:
    """Collect the fixture scope; _read(env) reads project 4 for Moscow-local dates."""
    return adapter.read_report_facts(env, project_id=4, period=_PERIOD, project_timezone=_ZONE, observed_at=_NOW, **extra)


@pytest.mark.parametrize("start,end", [(date(2026, 11, 28), _DAY), (date(2026, 1, 1), _DAY),
                                       (date.min, date.min), (_DAY, date.max)])
def test_invalid_period(start: date, end: date) -> None:
    """Reject inverted and unbounded periods before hitting a source."""
    with pytest.raises(ValueError):
        core.ReportPeriod(start, end)


def test_empty_work_is_distinct_from_unavailable_project_or_source(env: FakeEnv) -> None:
    """Show checked empty visible work without claiming a missing plan was empty."""
    result: core.ReportFacts = _read(env)
    assert result.work_status is core.ReportStatus.EMPTY and result.status is core.ReportStatus.PARTIAL
    assert dict(result.sections)[core.ReportSection.PLAN].coverage is core.FactCoverage.UNAVAILABLE
    env["project.task"].search_read.side_effect = PermissionError("internal detail")
    assert _read(env).work_status is core.ReportStatus.PARTIAL
    adapter.readable_projects.return_value = False
    env["project.task"].search_read.reset_mock()
    denied: core.ReportFacts = _read(env)
    assert denied.status is core.ReportStatus.UNAVAILABLE and not denied.sections
    env["project.task"].search_read.assert_not_called()


def test_period_boundaries_visible_tasks_and_worklog_units(env: FakeEnv) -> None:
    """Use local boundaries and discard out-of-scope, unavailable and future evidence."""
    env["project.task"].search_read.return_value = [
        {"id": 8, "create_date": "2026-11-26 21:00:00", "active": True, "is_closed": False, "state": "01_in_progress"},
        {"id": 9, "create_date": "2026-11-26 20:59:59", "active": True, "is_closed": True, "state": "1_done"},
    ]
    env["project.task"].search.return_value = SimpleNamespace(ids=[8, 9])
    env["mail.message"].search_read.return_value = [
        {"id": 11, "res_id": 9, "date": "2026-11-26 21:00:00"},
        {"id": 12, "res_id": 9, "date": "2026-11-27 21:00:00"},
        {"id": 13, "res_id": 404, "date": "2026-11-27 08:00:00"},
    ]
    env["account.analytic.line"].search_read.return_value = [
        {"id": 20, "task_id": 8, "date": _DAY, "unit_amount": 1.25, "product_uom_id": 1},
        {"id": 21, "task_id": 404, "date": _DAY, "unit_amount": 99, "product_uom_id": 1},
        {"id": 22, "task_id": 8, "date": _DAY, "unit_amount": 99, "product_uom_id": 2},
    ]
    sections: dict[core.ReportSection, core.SectionFacts] = dict(_read(env).sections)
    assert [fact.task_id for fact in sections[core.ReportSection.CREATED].facts] == [8]
    assert [fact.current_state for fact in sections[core.ReportSection.CURRENT].facts] == ["01_in_progress"]
    assert [fact.sources[-1] for fact in sections[core.ReportSection.COMPLETED].facts] == ["/mail/message/11"]
    assert [fact.quantity for fact in sections[core.ReportSection.WORKLOG].facts] == [Decimal("1.25")]
    assert sections[core.ReportSection.COMPLETED].coverage is core.FactCoverage.INCOMPLETE
    assert sections[core.ReportSection.WORKLOG].coverage is core.FactCoverage.INCOMPLETE
    assert all(("project_id", "=", 4) in call.args[0] for call in env["project.task"].search_read.call_args_list)
    assert ("res_id", "in", [8, 9]) in env["mail.message"].search_read.call_args.args[0]
    assert ("message_type", "=", "tracking") in env["mail.message"].search_read.call_args.args[0]
    assert ("subtype_id", "=", 14) in env["mail.message"].search_read.call_args.args[0]
    assert ("project_id", "=", 4) in env["account.analytic.line"].search_read.call_args.args[0]


def test_historical_period_does_not_pass_current_state_as_historical(env: FakeEnv) -> None:
    """Keep current state out of a past report even if the task existed at the time."""
    env["project.task"].search_read.return_value = [
        {"id": 8, "create_date": "2026-11-26 10:00:00", "active": True, "is_closed": False, "state": "01_in_progress"},
    ]
    period: core.ReportPeriod = core.ReportPeriod(date(2026, 11, 26), date(2026, 11, 26))
    result: core.ReportFacts = adapter.read_report_facts(env, project_id=4, period=period, project_timezone=UTC, observed_at=_NOW)
    assert dict(result.sections)[core.ReportSection.CURRENT] == core.SectionFacts(core.FactCoverage.NOT_APPLICABLE)
    assert dict(result.sections)[core.ReportSection.CREATED].facts[0].task_id == 8
    assert env["project.task"].search_read.call_args.kwargs["fields"] == ["id", "create_date"]


def test_created_query_is_independent_of_history_cap(env: FakeEnv) -> None:
    """Query created tasks inside the period even when historical evidence is capped."""
    tasks: Mock = env["project.task"]
    tasks.search.return_value = SimpleNamespace(ids=list(range(1, 502)))
    tasks.search_read.return_value = [{"id": 602, "create_date": "2026-11-27 09:00:00"}]
    sections: dict[core.ReportSection, core.SectionFacts] = dict(_read(env).sections)
    assert sections[core.ReportSection.CREATED].facts[0].task_id == 602
    assert sections[core.ReportSection.COMPLETED].coverage is core.FactCoverage.INCOMPLETE
    selection = tasks.search_read.call_args_list[0].args[0]
    assert ("create_date", ">=", datetime(2026, 11, 26, 21)) in selection
    assert ("create_date", "<", datetime(2026, 11, 27, 21)) in selection


def test_disabled_worklog_and_failed_completion_do_not_hide_created_facts(env: FakeEnv) -> None:
    """Keep independent evidence when one source fails and another is disabled."""
    adapter.readable_projects.return_value.allow_timesheets = False
    env["project.task"].search.return_value = SimpleNamespace(ids=[8])
    env["project.task"].search_read.return_value = [{"id": 8, "create_date": "2026-11-27 09:00:00"}]
    env["mail.message"].search_read.side_effect = ValueError("source details")
    sections: dict[core.ReportSection, core.SectionFacts] = dict(_read(env).sections)
    assert sections[core.ReportSection.CREATED].facts[0].task_id == 8
    assert sections[core.ReportSection.COMPLETED].coverage is core.FactCoverage.UNAVAILABLE
    assert sections[core.ReportSection.WORKLOG].coverage is core.FactCoverage.NOT_APPLICABLE
    env["account.analytic.line"].search_read.assert_not_called()


def test_risk_selection_deduplicates_ids_and_checks_access_and_period(env: FakeEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """Recheck each distinct requested result and reject an out-of-period snapshot."""
    empty: ProjectRiskSummary = ProjectRiskSummary(SummaryStatus.EMPTY, (), ("RR-01",), ())
    store: Mock = Mock()
    auth: Mock = Mock(side_effect=[True, True, False])
    monkeypatch.setattr(adapter, "can_read_summary", auth)

    def get(key: str, *, project_id: int, recipient_id: int, authorize: object) -> SavedSummary | None:
        """Return a synthetic accessible snapshot or suppress it, as the store contract requires."""
        assert (project_id, recipient_id) == (4, 7)
        saved: SavedSummary = SavedSummary(key, key, 4, 7, _NOW if key != "old" else _NOW.replace(day=25), empty)
        return saved if authorize(saved) else None

    store.get_summary.side_effect = get
    result: core.ReportFacts = _read(env, summary_store=store, risk_result_ids=("valid", "valid", "old", "denied"))
    assert [call.args[0] for call in store.get_summary.call_args_list] == ["valid", "old", "denied"]
    assert dict(result.sections)[core.ReportSection.RISKS] == core.SectionFacts(core.FactCoverage.INCOMPLETE)

    auth.side_effect = None
    auth.return_value = True
    result = _read(env, summary_store=store, risk_result_ids=("valid",))
    assert dict(result.sections)[core.ReportSection.RISKS] == core.SectionFacts(core.FactCoverage.INCOMPLETE)
