"""Attack scoped comment and timesheet boundaries with an ORM double, not live ACLs."""

# ruff: noqa: F811 -- Pytest injects the imported shared ORM fixture by name.

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from adapters.odoo_task_tools import OdooTaskSource
from application.task_read_tools import (
    Comment,
    Comments,
    ReadStatus,
    TaskReadTools,
    Worklog,
    WorklogEntry,
    WorklogTotal,
)
from test_odoo_task_tools import AccessError, orm  # noqa: F401

START = date(2026, 1, 1)
END = date(2026, 1, 2)


@pytest.fixture
def activity(orm: tuple[MagicMock, dict[str, MagicMock]]) -> tuple[TaskReadTools, dict[str, MagicMock]]:
    """Build a readable optional timesheet source; e.g. tools, models = activity."""
    env, models = orm
    for name in ("account.analytic.line", "res.company", "uom.uom"):
        models[name] = MagicMock()
        models[name].fields_get.side_effect = lambda *, allfields, attributes: {field: {"type": "x"} for field in allfields}
    models["project.project"].search_read.return_value = [{"allow_timesheets": True, "company_id": 1}]
    models["res.company"].search_read.return_value = [{"project_time_mode_id": 5}]
    models["uom.uom"].search_read.return_value = [{"id": 5, "name": "Hours"}]
    models["account.analytic.line"].search_read.return_value = []
    models["mail.message"].search_read.return_value = []
    return TaskReadTools(OdooTaskSource(env, project_id=4, task_id=9, timezone=UTC)), models


@pytest.mark.parametrize("limit", [0, -1, 21, True, False, None, "2", 1.5])
def test_invalid_limit_rejected_before_source(limit: object) -> None:
    source = MagicMock()
    with pytest.raises(ValueError, match="Comment limit"):
        TaskReadTools(source).read_task_comments(START, END, limit)
    source.comments.assert_not_called()


@pytest.mark.parametrize("method", ["read_task_comments", "read_task_worklog"])
@pytest.mark.parametrize("start,end", [(END, START), (START, date(2026, 4, 1)), (None, END), (START, datetime(2026, 1, 2))])
def test_invalid_period_rejected_before_source(method: str, start: object, end: object) -> None:
    source = MagicMock()
    with pytest.raises(ValueError):
        getattr(TaskReadTools(source), method)(start, end)
    assert not source.mock_calls


def test_comment_query_source_normalization_and_truncation(activity: tuple[TaskReadTools, dict[str, MagicMock]]) -> None:
    tools, models = activity
    models["mail.message"].search_read.return_value = [
        {"id": 8, "date": datetime(2026, 1, 2), "body": "<p>A</p><script>secret</script><p>" + "x" * 2001 + "</p>"},
        {"id": 7, "date": datetime(2026, 1, 1), "body": "omitted"},
    ]
    status, result = tools.read_task_comments(START, END, 1)
    assert status == ReadStatus.AVAILABLE
    assert result is not None and result.truncated and not result.complete
    assert len(result.fragments) == 1
    item = result.fragments[0]
    assert (item.day, item.source, item.truncated) == (END, "/mail/message/8", True)
    assert item.text == "A\n" + "x" * 1998
    models["mail.message"].search_read.assert_called_once_with(
        [("model", "=", "project.task"), ("res_id", "=", 9), ("message_type", "=", "comment"),
         ("date", ">=", datetime(2026, 1, 1)), ("date", "<", datetime(2026, 1, 3))],
        fields=["id", "date", "body"], order="date desc, id desc", load=None, limit=2,
    )


@pytest.mark.parametrize("limit", [1, 20])
def test_exact_comment_limit_not_truncated(activity: tuple[TaskReadTools, dict[str, MagicMock]], limit: int) -> None:
    tools, models = activity
    models["mail.message"].search_read.return_value = [
        {"id": index + 1, "date": datetime(2026, 1, 1), "body": "x"} for index in range(limit)
    ]
    status, result = tools.read_task_comments(START, END, limit)
    assert status == ReadStatus.AVAILABLE and result is not None
    assert len(result.fragments) == limit and not result.truncated and not result.complete


def test_comment_project_timezone_dst(orm: tuple[MagicMock, dict[str, MagicMock]]) -> None:
    env, models = orm
    models["mail.message"].search_read.return_value = [{"id": 1, "date": datetime(2026, 3, 28, 23), "body": "x"}]
    tools = TaskReadTools(OdooTaskSource(env, project_id=4, task_id=9, timezone=ZoneInfo("Europe/Berlin")))
    status, result = tools.read_task_comments(date(2026, 3, 29), date(2026, 3, 29))
    assert status == ReadStatus.AVAILABLE and result.fragments[0].day == date(2026, 3, 29)
    domain = models["mail.message"].search_read.call_args.args[0]
    assert domain[-2:] == [("date", ">=", datetime(2026, 3, 28, 23)), ("date", "<", datetime(2026, 3, 29, 22))]


def test_known_sum_evidence_and_projection(activity: tuple[TaskReadTools, dict[str, MagicMock]]) -> None:
    tools, models = activity
    models["account.analytic.line"].search_read.return_value = [
        {"id": 10, "date": START, "unit_amount": 0.1, "product_uom_id": 5},
        {"id": 11, "date": "2026-01-02", "unit_amount": 0.2, "product_uom_id": 5},
        {"id": 12, "date": END, "unit_amount": -0.05, "product_uom_id": 5},
    ]
    status, result = tools.read_task_worklog(START, END)
    assert status == ReadStatus.AVAILABLE and result is not None
    assert result.totals == (WorklogTotal(5, "Hours", Decimal("0.25")),)
    assert result.entries == (
        WorklogEntry(START, Decimal("0.1"), 5, "/odoo/account.analytic.line/10"),
        WorklogEntry(END, Decimal("0.2"), 5, "/odoo/account.analytic.line/11"),
        WorklogEntry(END, Decimal("-0.05"), 5, "/odoo/account.analytic.line/12"),
    )
    assert not result.complete and not result.truncated
    assert (result.period_start, result.period_end) == (START, END)
    models["account.analytic.line"].search_read.assert_called_once_with(
        [("project_id", "=", 4), ("task_id", "=", 9), ("date", ">=", START), ("date", "<=", END)],
        fields=["id", "date", "unit_amount", "product_uom_id"], order="date, id", load=None, limit=1001,
    )


def test_mixed_actual_units_not_added(activity: tuple[TaskReadTools, dict[str, MagicMock]]) -> None:
    tools, models = activity
    models["account.analytic.line"].search_read.return_value = [
        {"id": 1, "date": START, "unit_amount": 2, "product_uom_id": 5},
        {"id": 2, "date": START, "unit_amount": 1, "product_uom_id": 6},
    ]
    models["uom.uom"].search_read.return_value = [{"id": 5, "name": "Hours"}, {"id": 6, "name": "Days"}]
    _, result = tools.read_task_worklog(START, END)
    assert result.totals == (WorklogTotal(5, "Hours", Decimal(2)), WorklogTotal(6, "Days", Decimal(1)))


@pytest.mark.parametrize("count,truncated", [(0, False), (1000, False), (1001, True)])
def test_worklog_cap_and_empty_visible_not_full_zero(
    activity: tuple[TaskReadTools, dict[str, MagicMock]], count: int, truncated: bool,
) -> None:
    tools, models = activity
    models["account.analytic.line"].search_read.return_value = [
        {"id": index + 1, "date": START, "unit_amount": 1, "product_uom_id": 5} for index in range(count)
    ]
    status, result = tools.read_task_worklog(START, END)
    assert status == ReadStatus.AVAILABLE and result is not None
    assert result.totals == (WorklogTotal(5, "Hours", Decimal(min(count, 1000))),)
    assert len(result.entries) == min(count, 1000) and result.truncated is truncated and not result.complete


@pytest.mark.parametrize("method,model", [("read_task_comments", "mail.message"), ("read_task_worklog", "account.analytic.line")])
def test_optional_model_absent_no_false_empty(
    activity: tuple[TaskReadTools, dict[str, MagicMock]], method: str, model: str,
) -> None:
    tools, models = activity
    del models[model]
    assert getattr(tools, method)(START, END) == (ReadStatus.UNAVAILABLE, None)


@pytest.mark.parametrize("method,model", [("read_task_comments", "mail.message"), ("read_task_worklog", "account.analytic.line")])
def test_source_acl_failure_not_empty(
    activity: tuple[TaskReadTools, dict[str, MagicMock]], method: str, model: str,
) -> None:
    tools, models = activity
    models[model].search_read.side_effect = AccessError("raw sensitive details")
    assert getattr(tools, method)(START, END) == (ReadStatus.FAILED, None)


@pytest.mark.parametrize("method,model", [("read_task_comments", "mail.message"), ("read_task_worklog", "account.analytic.line")])
def test_hidden_fields_not_empty(
    activity: tuple[TaskReadTools, dict[str, MagicMock]], method: str, model: str,
) -> None:
    tools, models = activity
    models[model].fields_get.side_effect = lambda **kwargs: {}
    assert getattr(tools, method)(START, END) == (ReadStatus.UNAVAILABLE, None)
    models[model].search_read.assert_not_called()


@pytest.mark.parametrize("method", ["read_task_comments", "read_task_worklog"])
def test_scope_rechecked_and_no_elevation(activity: tuple[TaskReadTools, dict[str, MagicMock]], method: str) -> None:
    tools, models = activity
    assert getattr(tools, method)(START, END)[0] == ReadStatus.AVAILABLE
    models["project.task"].search_read.return_value = []
    assert getattr(tools, method)(START, END) == (ReadStatus.UNAVAILABLE, None)
    for model in models.values():
        model.sudo.assert_not_called()


@pytest.mark.parametrize("amount", [True, None, "2", float("nan"), float("inf")])
def test_invalid_amount_fails_not_partial_sum(activity: tuple[TaskReadTools, dict[str, MagicMock]], amount: object) -> None:
    tools, models = activity
    models["account.analytic.line"].search_read.return_value = [
        {"id": 1, "date": START, "unit_amount": amount, "product_uom_id": 5},
    ]
    assert tools.read_task_worklog(START, END) == (ReadStatus.FAILED, None)


def test_disabled_timesheets_not_zero(activity: tuple[TaskReadTools, dict[str, MagicMock]]) -> None:
    tools, models = activity
    models["project.project"].search_read.return_value = [{"allow_timesheets": False, "company_id": 1}]
    assert tools.read_task_worklog(START, END) == (ReadStatus.UNAVAILABLE, None)
    models["account.analytic.line"].search_read.assert_not_called()


def test_hidden_unit_not_implicit_hours(activity: tuple[TaskReadTools, dict[str, MagicMock]]) -> None:
    tools, models = activity
    models["uom.uom"].search_read.return_value = []
    assert tools.read_task_worklog(START, END) == (ReadStatus.UNAVAILABLE, None)


@pytest.mark.parametrize("method", ["read_task_comments", "read_task_worklog"])
def test_application_discards_payload_on_nonavailable(method: str) -> None:
    source = MagicMock()
    getattr(source, "comments" if method.endswith("comments") else "worklog").return_value = (ReadStatus.UNAVAILABLE, "secret")
    assert getattr(TaskReadTools(source), method)(START, END) == (ReadStatus.UNAVAILABLE, None)


def test_application_rejects_excess_or_invalid_fragments() -> None:
    source = MagicMock()
    for result in (None, Comments((Comment(START, "x", ""),)), Comments((Comment(START, "x", "ref"),) * 2)):
        source.comments.return_value = (ReadStatus.AVAILABLE, result)
        assert TaskReadTools(source).read_task_comments(START, END, 1) == (ReadStatus.FAILED, None)


def test_application_preserves_worklog_partial_coverage() -> None:
    source = MagicMock()
    result = Worklog((), (), False, True, START, END)
    source.worklog.return_value = (ReadStatus.AVAILABLE, result)
    assert TaskReadTools(source).read_task_worklog(START, END) == (ReadStatus.AVAILABLE, result)


@pytest.mark.parametrize("field,value", [
    ("id", 0), ("date", None), ("date", date(2026, 1, 3)),
    ("product_uom_id", False), ("product_uom_id", (5, "Hours")),
])
def test_malformed_worklog_row_fails(
    activity: tuple[TaskReadTools, dict[str, MagicMock]], field: str, value: object,
) -> None:
    tools, models = activity
    row = {"id": 1, "date": START, "unit_amount": 2, "product_uom_id": 5}
    row[field] = value
    models["account.analytic.line"].search_read.return_value = [row]
    assert tools.read_task_worklog(START, END) == (ReadStatus.FAILED, None)


@pytest.mark.parametrize("field,value", [("id", 0), ("body", None), ("date", "bad"), ("date", datetime(2026, 1, 3))])
def test_malformed_comment_fails(
    activity: tuple[TaskReadTools, dict[str, MagicMock]], field: str, value: object,
) -> None:
    tools, models = activity
    row = {"id": 1, "date": datetime(2026, 1, 1), "body": "x"}
    row[field] = value
    models["mail.message"].search_read.return_value = [row]
    assert tools.read_task_comments(START, END) == (ReadStatus.FAILED, None)


def test_readable_empty_comment_not_failure(activity: tuple[TaskReadTools, dict[str, MagicMock]]) -> None:
    tools, models = activity
    models["mail.message"].search_read.return_value = [{"id": 1, "date": datetime(2026, 1, 1), "body": False}]
    status, result = tools.read_task_comments(START, END)
    assert status == ReadStatus.AVAILABLE and result.fragments == (Comment(START, "", "/mail/message/1"),)


@pytest.mark.parametrize("result", [
    Worklog((WorklogTotal(5, "h", Decimal(999)),), (WorklogEntry(START, Decimal(1), 5, "ref"),)),
    Worklog((), (WorklogEntry(START, Decimal(1), 5, "ref"),)),
    Worklog((), (WorklogEntry(START, Decimal(1), 5, ""),)),
    Worklog((), (WorklogEntry(START, Decimal("NaN"), 5, "ref"),)),
    Worklog((), (WorklogEntry(START, Decimal(1), 5, "ref"),) * 2),
    Worklog((), (WorklogEntry(START, Decimal(1), 5, "ref"),) * 1001),
    Worklog((WorklogTotal(5, "", Decimal(0)),), ()),
    Worklog((WorklogTotal(5, "h", Decimal(0)),) * 2, ()),
])
def test_application_rejects_unsupported_or_invalid_sum(result: Worklog) -> None:
    source = MagicMock()
    source.worklog.return_value = (ReadStatus.AVAILABLE, result)
    assert TaskReadTools(source).read_task_worklog(START, END) == (ReadStatus.FAILED, None)


@pytest.mark.parametrize("method", ["read_task_comments", "read_task_worklog"])
@pytest.mark.parametrize("argument,value", [("task_id", 10), ("project_id", 10), ("user_id", 8), ("timezone", UTC)])
def test_model_cannot_override_server_scope(
    activity: tuple[TaskReadTools, dict[str, MagicMock]], method: str, argument: str, value: object,
) -> None:
    tools, _ = activity
    with pytest.raises(TypeError):
        getattr(tools, method)(START, END, **{argument: value})
