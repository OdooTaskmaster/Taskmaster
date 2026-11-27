"""Attack scoped comment and timesheet boundaries with an ORM double, not live ACLs."""

# ruff: noqa: F811 -- Pytest injects the imported shared ORM fixture by name.

import json
from dataclasses import asdict
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
    models["uom.uom"].with_context.return_value = models["uom.uom"]
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
        [("model", "=", "project.task"), ("res_id", "=", 9),
         ("message_type", "in", ["comment", "email", "email_outgoing"]),
         ("date", ">=", datetime(2026, 1, 1)), ("date", "<", datetime(2026, 1, 3))],
        fields=["id", "date", "body"], order="date desc, id desc", load=None, limit=2,
    )


def test_comment_domain_covers_email_conversation(activity: tuple[TaskReadTools, dict[str, MagicMock]]) -> None:
    """Email in/out replies belong to the comment selection, not just mt_note notes."""
    tools, models = activity
    source = models["mail.message"]
    for message_type, body in (("comment", "note"), ("email", "reply"), ("email_outgoing", "sent")):
        source.search_read.return_value = [{"id": 1, "date": datetime(2026, 1, 1), "body": body}]
        status, result = tools.read_task_comments(START, END, 1)
        assert status == ReadStatus.AVAILABLE and result is not None
        assert result.fragments[0].text == body
    assert source.search_read.call_args.args[0][2] == ("message_type", "in", ["comment", "email", "email_outgoing"])


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
        WorklogEntry(START, Decimal("0.1"), 5, 1, ("/odoo/account.analytic.line/10",)),
        WorklogEntry(END, Decimal("0.15"), 5, 2, ("/odoo/account.analytic.line/11", "/odoo/account.analytic.line/12")),
    )
    assert not result.complete and not result.truncated
    assert (result.period_start, result.period_end) == (START, END)
    models["account.analytic.line"].search_read.assert_called_once_with(
        [("project_id", "=", 4), ("task_id", "=", 9), ("date", ">=", START), ("date", "<=", END)],
        fields=["id", "date", "unit_amount", "product_uom_id"], order="date desc, id desc", load=None, limit=1001,
    )


def test_same_day_rows_aggregate_into_one_entry(activity: tuple[TaskReadTools, dict[str, MagicMock]]) -> None:
    tools, models = activity
    models["account.analytic.line"].search_read.return_value = [
        {"id": 1, "date": START, "unit_amount": 1, "product_uom_id": 5},
        {"id": 2, "date": START, "unit_amount": 2, "product_uom_id": 5},
        {"id": 3, "date": START, "unit_amount": 0.5, "product_uom_id": 5},
    ]
    _, result = tools.read_task_worklog(START, END)
    assert result.entries == (
        WorklogEntry(START, Decimal("3.5"), 5, 3, ("/odoo/account.analytic.line/1",
                                                   "/odoo/account.analytic.line/2",
                                                   "/odoo/account.analytic.line/3")),
    )
    assert result.totals == (WorklogTotal(5, "Hours", Decimal("3.5")),)


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
    if count == 0:
        assert result.entries == () and not result.truncated and not result.complete
    else:
        assert len(result.entries) == 1 and result.entries[0].count == min(count, 1000)
        assert result.truncated is truncated and not result.complete


def test_archived_unit_still_readable(activity: tuple[TaskReadTools, dict[str, MagicMock]]) -> None:
    """An archived uom.uom must not make the whole worklog unavailable."""
    tools, models = activity
    models["account.analytic.line"].search_read.return_value = [
        {"id": 1, "date": START, "unit_amount": 2, "product_uom_id": 5},
    ]
    models["uom.uom"].search_read.return_value = [{"id": 5, "name": "Hours"}]
    status, result = tools.read_task_worklog(START, END)
    assert status == ReadStatus.AVAILABLE and result.totals == (WorklogTotal(5, "Hours", Decimal(2)),)
    models["uom.uom"].with_context.assert_called_once_with(active_test=False)


@pytest.mark.parametrize("config", [
    {"company_id": False}, {"company_id": None}, {"company_id": 0},
])
def test_empty_selection_without_company_not_zero(
    activity: tuple[TaskReadTools, dict[str, MagicMock]], config: dict[str, object],
) -> None:
    tools, models = activity
    models["project.project"].search_read.return_value = [{"allow_timesheets": True, **config}]
    assert tools.read_task_worklog(START, END) == (ReadStatus.UNAVAILABLE, None)


def test_default_unit_resolved_only_for_empty_selection(
    activity: tuple[TaskReadTools, dict[str, MagicMock]],
) -> None:
    """A cleared project time unit never fails a read that has valid rows."""
    tools, models = activity
    models["account.analytic.line"].search_read.return_value = [
        {"id": 1, "date": START, "unit_amount": 2, "product_uom_id": 5},
    ]
    models["res.company"].search_read.return_value = [{"project_time_mode_id": False}]
    status, result = tools.read_task_worklog(START, END)
    assert status == ReadStatus.AVAILABLE and result.totals == (WorklogTotal(5, "Hours", Decimal(2)),)
    models["res.company"].search_read.assert_not_called()


def test_empty_selection_cleared_default_unit_is_unavailable(
    activity: tuple[TaskReadTools, dict[str, MagicMock]],
) -> None:
    tools, models = activity
    models["res.company"].search_read.return_value = [{"project_time_mode_id": False}]
    assert tools.read_task_worklog(START, END) == (ReadStatus.UNAVAILABLE, None)


def test_empty_selection_missing_company_row_is_unavailable(
    activity: tuple[TaskReadTools, dict[str, MagicMock]],
) -> None:
    tools, models = activity
    models["res.company"].search_read.return_value = []
    assert tools.read_task_worklog(START, END) == (ReadStatus.UNAVAILABLE, None)


@pytest.mark.parametrize("method,model", [("read_task_comments", "mail.message"), ("read_task_worklog", "account.analytic.line")])
def test_optional_model_absent_no_false_empty(
    activity: tuple[TaskReadTools, dict[str, MagicMock]], method: str, model: str,
) -> None:
    tools, models = activity
    del models[model]
    assert getattr(tools, method)(START, END) == (ReadStatus.UNAVAILABLE, None)


@pytest.mark.parametrize("method,model,field", [
    ("read_task_worklog", "project.project", "allow_timesheets"),
    ("read_task_worklog", "res.company", "project_time_mode_id"),
    ("read_task_worklog", "uom.uom", "name"),
])
def test_hidden_auxiliary_field_not_empty(
    activity: tuple[TaskReadTools, dict[str, MagicMock]], method: str, model: str, field: str,
) -> None:
    tools, models = activity
    models[model].fields_get.side_effect = lambda *args, **kwargs: {}
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


def test_application_rejects_comment_complete_or_non_bool_flags() -> None:
    source = MagicMock()
    for result in (Comments((), True), Comments((), False, "yes"), Comment(START, "x", "ref", 1)):
        payload = result if isinstance(result, Comments) else Comments((result,))
        source.comments.return_value = (ReadStatus.AVAILABLE, payload)
        assert TaskReadTools(source).read_task_comments(START, END, 5) == (ReadStatus.FAILED, None)


def test_application_rejects_comment_out_of_period_day() -> None:
    source = MagicMock()
    source.comments.return_value = (ReadStatus.AVAILABLE, Comments((Comment(date(2025, 12, 31), "x", "ref"),)))
    assert TaskReadTools(source).read_task_comments(START, END, 5) == (ReadStatus.FAILED, None)


def test_application_skips_empty_comment_fragment() -> None:
    """Attachment-only or blank notes consume no slot and do not fail the read."""
    source = MagicMock()
    source.comments.return_value = (ReadStatus.AVAILABLE, Comments(
        (Comment(START, "", "mail.message:1"), Comment(START, "kept", "mail.message:2")),
    ))
    status, result = TaskReadTools(source).read_task_comments(START, END, 5)
    assert status == ReadStatus.AVAILABLE and result is not None
    assert result.fragments == (Comment(START, "kept", "mail.message:2", False),)
    assert not result.truncated and not result.complete


def test_application_dedupes_comment_sources() -> None:
    source = MagicMock()
    source.comments.return_value = (ReadStatus.AVAILABLE, Comments(
        (Comment(START, "a", "mail.message:1"), Comment(START, "b", "mail.message:1")),
    ))
    assert TaskReadTools(source).read_task_comments(START, END, 5) == (ReadStatus.FAILED, None)


def test_application_collection_truncation_is_union() -> None:
    source = MagicMock()
    source.comments.return_value = (ReadStatus.AVAILABLE, Comments(
        (Comment(START, "x" * 2000, "ref", True),), False, False,
    ))
    status, result = TaskReadTools(source).read_task_comments(START, END, 5)
    assert status == ReadStatus.AVAILABLE and result is not None and result.truncated


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


def test_worklog_entries_guess_unit_but_source_covers(
    activity: tuple[TaskReadTools, dict[str, MagicMock]],
) -> None:
    """A cleared project time unit does not hide rows that carry their own unit."""
    tools, models = activity
    models["res.company"].search_read.return_value = [{"project_time_mode_id": False}]
    models["account.analytic.line"].search_read.return_value = [
        {"id": 1, "date": START, "unit_amount": 1.5, "product_uom_id": 5},
    ]
    status, result = tools.read_task_worklog(START, END)
    assert status == ReadStatus.AVAILABLE and result is not None
    assert result.totals == (WorklogTotal(5, "Hours", Decimal("1.5")),)
    assert result.entries == (WorklogEntry(START, Decimal("1.5"), 5, 1, ("/odoo/account.analytic.line/1",)),)


def test_application_rejects_worklog_complete_or_non_bool_flags() -> None:
    source = MagicMock()
    for result in (Worklog((), (), True), Worklog((), (), False, "yes"),
                   Worklog(None, (), False, False), Worklog((), None, False, False)):
        source.worklog.return_value = (ReadStatus.AVAILABLE, result)
        assert TaskReadTools(source).read_task_worklog(START, END) == (ReadStatus.FAILED, None)


# Valid single entry reused across the invalid-shape checks.
def _valid_entry() -> WorklogEntry:
    return WorklogEntry(START, Decimal(1), 5, 1, ("ref",))


@pytest.mark.parametrize("result", [
    Worklog((WorklogTotal(5, "h", Decimal(999)),), (_valid_entry(),)),
    Worklog((), (_valid_entry(),)),
    Worklog((), (WorklogEntry(START, Decimal(1), 5, 0, ("ref",)),)),
    Worklog((), (WorklogEntry(START, Decimal(1), 5, 1, ()),)),
    Worklog((), (WorklogEntry(START, Decimal(1), 5, 1, ("ref", "ref")),)),
    Worklog((), (WorklogEntry(START, Decimal("NaN"), 5, 1, ("ref",)),)),
    Worklog((), (WorklogEntry(START, Decimal(1), 5, 1, ("ref",)), WorklogEntry(START, Decimal(1), 5, 1, ("ref2",)))),
    Worklog((WorklogTotal(5, "", Decimal(0)),), ()),
    Worklog((WorklogTotal(5, "h", Decimal(0)),) * 2, ()),
])
def test_application_rejects_unsupported_or_invalid_sum(result: Worklog) -> None:
    source = MagicMock()
    source.worklog.return_value = (ReadStatus.AVAILABLE, result)
    assert TaskReadTools(source).read_task_worklog(START, END) == (ReadStatus.FAILED, None)


def test_application_accepts_duplicate_rows_but_unique_sources() -> None:
    """Two same-day rows form one entry; the summed source list stays unique."""
    source = MagicMock()
    result = Worklog((WorklogTotal(5, "h", Decimal(3)),),
                     (WorklogEntry(START, Decimal(3), 5, 2, ("/a", "/b")),))
    source.worklog.return_value = (ReadStatus.AVAILABLE, result)
    status, returned = TaskReadTools(source).read_task_worklog(START, END)
    assert status == ReadStatus.AVAILABLE and returned is not None
    assert returned.period_start == START and returned.period_end == END


@pytest.mark.parametrize("method", ["read_task_comments", "read_task_worklog"])
@pytest.mark.parametrize("argument,value", [("task_id", 10), ("project_id", 10), ("user_id", 8), ("timezone", UTC)])
def test_model_cannot_override_server_scope(
    activity: tuple[TaskReadTools, dict[str, MagicMock]], method: str, argument: str, value: object,
) -> None:
    tools, _ = activity
    with pytest.raises(TypeError):
        getattr(tools, method)(START, END, **{argument: value})


@pytest.mark.parametrize("fragments", [None, [], iter(())])
def test_application_rejects_invalid_fragment_container(fragments: object) -> None:
    source: MagicMock = MagicMock()
    source.comments.return_value = (ReadStatus.AVAILABLE, Comments(fragments))
    assert TaskReadTools(source).read_task_comments(START, END) == (ReadStatus.FAILED, None)


def test_application_rejects_more_than_1000_aggregate_sources() -> None:
    source: MagicMock = MagicMock()
    entries: tuple[WorklogEntry, ...] = (
        WorklogEntry(START, Decimal(500), 5, 500, tuple(f"ref:{i}" for i in range(500))),
        WorklogEntry(END, Decimal(501), 5, 501, tuple(f"ref:{i}" for i in range(500, 1001))),
    )
    source.worklog.return_value = (ReadStatus.AVAILABLE, Worklog((WorklogTotal(5, "h", Decimal(1001)),), entries))
    assert TaskReadTools(source).read_task_worklog(START, END) == (ReadStatus.FAILED, None)


@pytest.mark.parametrize("limit", [1, 20])
def test_exact_comment_limit_not_truncated(activity: tuple[TaskReadTools, dict[str, MagicMock]], limit: int) -> None:
    tools, models = activity
    models["mail.message"].search_read.return_value = [
        {"id": i + 1, "date": datetime(2026, 1, 1), "body": "x"} for i in range(limit)
    ]
    status, result = tools.read_task_comments(START, END, limit)
    assert status == ReadStatus.AVAILABLE and result is not None
    assert len(result.fragments) == limit and result.truncated is False and result.complete is False


@pytest.mark.parametrize("field", ["project_id", "task_id"])
def test_missing_timesheet_link_field_is_unavailable(
    activity: tuple[TaskReadTools, dict[str, MagicMock]], field: str,
) -> None:
    tools, models = activity
    models["account.analytic.line"].fields_get.side_effect = (
        lambda *, allfields, attributes: {name: {"type": "x"} for name in allfields if name != field}
    )
    assert tools.read_task_worklog(START, END) == (ReadStatus.UNAVAILABLE, None)
    models["account.analytic.line"].search_read.assert_not_called()


def test_1000_rows_payload_keeps_only_daily_values_and_source_links(
    activity: tuple[TaskReadTools, dict[str, MagicMock]],
) -> None:
    """Bound a representative JSON projection; this is not a production serializer."""
    tools, models = activity
    models["account.analytic.line"].search_read.return_value = [
        {"id": i + 1, "date": START, "unit_amount": 1, "product_uom_id": 5} for i in range(1000)
    ]
    status, result = tools.read_task_worklog(START, END)
    assert status == ReadStatus.AVAILABLE and result is not None
    payload: bytes = json.dumps(asdict(result), default=str, separators=(",", ":")).encode("utf-8")
    assert len(result.entries) == 1 and result.entries[0].count == 1000
    assert len(payload) < 40000
