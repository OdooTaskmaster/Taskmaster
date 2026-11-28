"""Test scoped Odoo task tools using a mocked ORM, not a live Odoo database."""

import sys
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from types import ModuleType
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from adapters.odoo_task_reader import _read_task_projection
from adapters.odoo_task_tools import (
    HISTORY_MESSAGE_LIMIT,
    HISTORY_TRACKING_LIMIT,
    OdooTaskSource,
    _utc_midnight,
)
from application.task_read_tools import (
    Description,
    History,
    ReadStatus,
    TaskAssignee,
    TaskReadTools,
    Transition,
)


class AccessError(Exception):
    """Represent a mocked ORM access denial; AccessError() creates one."""


class MissingError(Exception):
    """Represent a mocked missing record; MissingError() creates one."""


@pytest.fixture
def orm(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, dict[str, MagicMock]]:
    """Build a recipient environment with readable fields and an authorized task.

    Example: a test using orm receives (environment, models).
    """
    odoo = ModuleType("odoo")
    errors = ModuleType("odoo.exceptions")
    monkeypatch.setattr(odoo, "SUPERUSER_ID", 1, raising=False)
    monkeypatch.setattr(errors, "AccessError", AccessError, raising=False)
    monkeypatch.setattr(errors, "MissingError", MissingError, raising=False)
    monkeypatch.setitem(sys.modules, "odoo", odoo)
    monkeypatch.setitem(sys.modules, "odoo.exceptions", errors)
    models = {name: MagicMock() for name in (
        "project.project", "project.task", "project.task.type", "res.users", "mail.message", "mail.tracking.value",
    )}
    for model in models.values():
        model.fields_get.side_effect = lambda *, allfields, attributes: {field: {"type": "x"} for field in allfields}
    models["mail.tracking.value"].sudo.return_value = models["mail.tracking.value"]
    models["project.task.type"].with_context.return_value = models["project.task.type"]
    models["project.task"].search_read.return_value = [{"id": 9}]
    models["res.users"].search_read.return_value = [{"id": 7, "name": "Demo user"}]
    models["project.task.type"].search_read.return_value = [{"id": 1}, {"id": 2}]
    env = MagicMock(su=False, uid=7)
    env.user.has_group.return_value = False
    env.__getitem__.side_effect = models.__getitem__
    env.registry.models = models
    return env, models


def source(orm: tuple[MagicMock, dict[str, MagicMock]]) -> OdooTaskSource:
    """Bind the fake environment to project 4, task 9 and UTC.

    Example: source(orm) returns the scoped fake-backed reader.
    """
    return OdooTaskSource(orm[0], project_id=4, task_id=9, timezone=UTC)


def test_pytz_style_timezone_uses_localize() -> None:
    class PytzStyle(tzinfo):
        def localize(self, value: datetime, is_dst: bool | None = None) -> datetime:
            assert is_dst is None
            return value.replace(tzinfo=timezone(timedelta(hours=3)))

    assert _utc_midnight(date(2026, 1, 2), PytzStyle()) == datetime(2026, 1, 1, 21)


def test_card_reads_only_requested_fields_with_recipient_scope(orm: tuple[MagicMock, dict[str, MagicMock]]) -> None:
    env, models = orm
    models["project.task"].search_read.return_value = [{
        "id": 9, "name": "Demo", "state": "01_in_progress", "date_deadline": False,
        "user_ids": [7], "description": "not requested",
    }]
    status, card = TaskReadTools(source(orm)).read_task_card()
    assert status == ReadStatus.AVAILABLE
    assert card is not None
    assert (card.name, card.state, card.deadline, card.deadline_known, card.assignees) == (
        "Demo", "01_in_progress", None, True, (TaskAssignee(7, "Demo user"),),
    )
    assert card.source == "/odoo/project.task/9"
    assert card.assignees_complete is False
    models["project.project"].search.assert_called_once_with(
        [("id", "=", 4), ("allowed_internal_user_ids", "in", [7])], limit=1,
    )
    models["project.task"].search_read.assert_called_once_with(
        [("project_id", "=", 4), ("id", "in", [9])],
        fields=["id", "name", "state", "date_deadline", "user_ids"], order="id", load=None,
    )
    assert not env.su


def test_missing_fields_are_unknown_not_empty(orm: tuple[MagicMock, dict[str, MagicMock]]) -> None:
    status, card = source(orm).card()
    assert status == ReadStatus.AVAILABLE
    assert card is not None
    assert (card.name, card.state, card.deadline_known, card.assignees) == (None, None, False, None)


def test_hidden_assignees_are_not_exposed(orm: tuple[MagicMock, dict[str, MagicMock]]) -> None:
    _, models = orm
    models["project.task"].search_read.return_value = [{"id": 9, "user_ids": [7, 8]}]
    models["res.users"].search_read.return_value = [{"id": 7}]
    status, card = source(orm).card()
    assert status == ReadStatus.AVAILABLE
    assert card is not None and card.assignees == (TaskAssignee(7, None),)


def test_empty_assignees_are_unknown_when_visibility_is_not_proven(
    orm: tuple[MagicMock, dict[str, MagicMock]],
) -> None:
    _, models = orm
    models["project.task"].search_read.return_value = [{"id": 9, "user_ids": []}]
    _, card = source(orm).card()
    assert card is not None and card.assignees is None


def test_unreadable_description_is_not_a_confirmed_empty_description(
    orm: tuple[MagicMock, dict[str, MagicMock]],
) -> None:
    assert TaskReadTools(source(orm)).read_task_description() == (ReadStatus.UNAVAILABLE, None)


def test_empty_description_is_confirmed(orm: tuple[MagicMock, dict[str, MagicMock]]) -> None:
    orm[1]["project.task"].search_read.return_value = [{"id": 9, "description": False}]
    assert TaskReadTools(source(orm)).read_task_description() == (
        ReadStatus.AVAILABLE, Description("", "/odoo/project.task/9", False),
    )


def test_description_and_revoked_project(orm: tuple[MagicMock, dict[str, MagicMock]]) -> None:
    _, models = orm
    models["project.task"].search_read.return_value = [{"id": 9, "description": "<p>hello</p>"}]
    tools = TaskReadTools(source(orm))
    assert tools.read_task_description() == (ReadStatus.AVAILABLE, Description("hello", "/odoo/project.task/9", False))
    models["project.project"].search.return_value = False
    assert tools.read_task_description() == (ReadStatus.UNAVAILABLE, None)
    assert models["project.task"].search_read.call_count == 1


@pytest.mark.parametrize("case", ["missing", "denied", "su"])
def test_hidden_and_missing_task_have_same_result(
    orm: tuple[MagicMock, dict[str, MagicMock]], case: str,
) -> None:
    env, models = orm
    if case == "missing":
        models["project.task"].search_read.return_value = []
    elif case == "denied":
        models["project.task"].search_read.side_effect = AccessError()
    else:
        env.su = True
    assert TaskReadTools(source(orm)).read_task_card() == (ReadStatus.UNAVAILABLE, None)


def test_history_reads_only_visible_message_ids_and_reports_incomplete(
    orm: tuple[MagicMock, dict[str, MagicMock]],
) -> None:
    _, models = orm
    models["mail.message"].search_read.return_value = [
        {"id": 10, "date": datetime(2026, 1, 1, 22)},
        {"id": 11, "date": datetime(2025, 12, 31, 8)},
    ]
    models["mail.tracking.value"].search_read.side_effect = [
        [{"mail_message_id": 10, "old_value_integer": 1, "new_value_integer": 2},
         {"mail_message_id": 11, "old_value_integer": 3, "new_value_integer": 4}],
        [{"mail_message_id": 10, "old_value_char": "Open", "new_value_char": "Done"},
         {"mail_message_id": 10, "old_value_char": "Same", "new_value_char": "Same"}],
    ]
    tools = TaskReadTools(OdooTaskSource(orm[0], project_id=4, task_id=9, timezone=timezone(timedelta(hours=3))))
    status, history = tools.read_task_stage_history(date(2026, 1, 2), date(2026, 1, 2))
    assert status == ReadStatus.AVAILABLE
    assert history == History((
        Transition(date(2026, 1, 2), "stage", 1, 2, "mail.message:10"),
        Transition(date(2026, 1, 2), "state", "Open", "Done", "mail.message:10"),
    ), False)
    args = models["mail.message"].search_read.call_args
    assert ("date", ">=", datetime(2026, 1, 1, 21)) in args.args[0]
    assert ("date", "<", datetime(2026, 1, 2, 21)) in args.args[0]
    for call in models["mail.tracking.value"].search_read.call_args_list:
        assert ("mail_message_id", "in", [10]) in call.args[0]


def test_unavailable_tracking_is_explicitly_unavailable(
    orm: tuple[MagicMock, dict[str, MagicMock]],
) -> None:
    env, models = orm
    del env.registry.models["mail.tracking.value"]
    assert source(orm).history(date(2026, 1, 1), date(2026, 1, 1)) == (
        ReadStatus.UNAVAILABLE, None,
    )
    models["mail.message"].search_read.assert_not_called()


def test_denied_tracking_never_exposes_raw_exception(orm: tuple[MagicMock, dict[str, MagicMock]]) -> None:
    _, models = orm
    models["mail.message"].search_read.side_effect = AccessError("private")
    assert source(orm).history(date(2026, 1, 1), date(2026, 1, 1)) == (
        ReadStatus.FAILED, None,
    )


def test_hidden_task_never_reads_history(orm: tuple[MagicMock, dict[str, MagicMock]]) -> None:
    _, models = orm
    models["project.task"].search_read.return_value = []
    assert source(orm).history(date(2026, 1, 1), date(2026, 1, 1)) == (ReadStatus.UNAVAILABLE, None)
    models["mail.message"].search_read.assert_not_called()


def test_invalid_history_period_fails_before_any_orm_call(orm: tuple[MagicMock, dict[str, MagicMock]]) -> None:
    env, _ = orm
    with pytest.raises(ValueError):
        source(orm).history(date(2026, 1, 2), date(2026, 1, 1))
    env.__getitem__.assert_not_called()


def test_source_failure_is_distinct_and_does_not_expose_error(orm: tuple[MagicMock, dict[str, MagicMock]]) -> None:
    orm[1]["project.project"].search.side_effect = RuntimeError("private details")
    assert source(orm).card() == (ReadStatus.FAILED, None)


def test_hidden_stage_ids_are_not_returned(orm: tuple[MagicMock, dict[str, MagicMock]]) -> None:
    _, models = orm
    models["mail.message"].search_read.return_value = [{"id": 10, "date": datetime(2026, 1, 1, 12)}]
    models["mail.tracking.value"].search_read.side_effect = [
        [{"mail_message_id": 10, "old_value_integer": 1, "new_value_integer": 2}], [],
    ]
    models["project.task.type"].search_read.return_value = [{"id": 1}]
    assert source(orm).history(date(2026, 1, 1), date(2026, 1, 1)) == (
        ReadStatus.AVAILABLE, History((), False),
    )


def test_restricted_stage_field_is_not_read_even_if_tracking_is_visible(
    orm: tuple[MagicMock, dict[str, MagicMock]],
) -> None:
    _, models = orm
    models["project.task"].fields_get.side_effect = (
        lambda *, allfields, attributes: {field: {} for field in allfields if field != "stage_id"}
    )
    models["mail.message"].search_read.return_value = [{"id": 10, "date": datetime(2026, 1, 1, 12)}]
    models["mail.tracking.value"].search_read.return_value = [
        {"mail_message_id": 10, "old_value_char": "Open", "new_value_char": "Done"},
    ]
    assert source(orm).history(date(2026, 1, 1), date(2026, 1, 1)) == (
        ReadStatus.AVAILABLE,
        History((Transition(date(2026, 1, 1), "state", "Open", "Done", "mail.message:10"),), False),
    )
    assert models["mail.tracking.value"].search_read.call_count == 1
    assert ("field_id.name", "=", "state") in models["mail.tracking.value"].search_read.call_args.args[0]


@pytest.mark.parametrize("raw, expected", [
    (datetime(2026, 1, 1, 12), datetime(2026, 1, 1, 12, tzinfo=UTC)),
    (datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=3))),
     datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=3)))),
    (date(2026, 1, 1), date(2026, 1, 1)),
    ("2026-01-01", date(2026, 1, 1)),
    ("2026-01-01 12:00:00", datetime(2026, 1, 1, 12, tzinfo=UTC)),
    ("2026-01-01T12:00:00+03:00", datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=3)))),
])
def test_deadline_preserves_value_and_precision(
    orm: tuple[MagicMock, dict[str, MagicMock]], raw: object, expected: date | datetime,
) -> None:
    orm[1]["project.task"].search_read.return_value = [{"id": 9, "date_deadline": raw}]
    status, card = source(orm).card()
    assert status == ReadStatus.AVAILABLE and card.deadline_known
    assert type(card.deadline) is type(expected)
    assert card.deadline == expected


@pytest.mark.parametrize("raw", ["invalid", "2026-99-99", 123, None])
def test_malformed_deadline_is_unknown(orm: tuple[MagicMock, dict[str, MagicMock]], raw: object) -> None:
    orm[1]["project.task"].search_read.return_value = [{"id": 9, "date_deadline": raw}]
    _, card = source(orm).card()
    assert card.deadline is None and not card.deadline_known


def test_zoneinfo_regular_midnight() -> None:
    assert _utc_midnight(date(2026, 1, 2), ZoneInfo("Europe/Berlin")) == datetime(2026, 1, 1, 23)


@pytest.mark.parametrize("day", [date(2020, 3, 8), date(2020, 11, 1)])
def test_zoneinfo_nonexistent_and_ambiguous_midnight_rejected(day: date) -> None:
    with pytest.raises(ValueError, match="Ambiguous or nonexistent"):
        _utc_midnight(day, ZoneInfo("America/Havana"))


def test_history_dst_failure_is_failed_not_empty(orm: tuple[MagicMock, dict[str, MagicMock]]) -> None:
    reader: OdooTaskSource = OdooTaskSource(orm[0], project_id=4, task_id=9, timezone=ZoneInfo("America/Havana"))
    assert reader.history(date(2020, 3, 8), date(2020, 3, 8)) == (ReadStatus.FAILED, None)
    orm[1]["mail.message"].search_read.assert_not_called()


@pytest.mark.parametrize("model, error", [
    ("mail.message", AccessError("private")),
    ("mail.tracking.value", AccessError("private")),
    ("mail.tracking.value", MissingError("private")),
    ("mail.tracking.value", KeyError("private")),
    ("mail.tracking.value", RuntimeError("private")),
])
def test_history_read_errors_are_failed(
    orm: tuple[MagicMock, dict[str, MagicMock]], model: str, error: Exception,
) -> None:
    orm[1]["mail.message"].search_read.return_value = [{"id": 9, "date": "2026-01-01 12:00:00"}]
    orm[1][model].search_read.side_effect = error
    assert source(orm).history(date(2026, 1, 1), date(2026, 1, 1)) == (ReadStatus.FAILED, None)


def test_history_uses_instant_then_numeric_message_id(orm: tuple[MagicMock, dict[str, MagicMock]]) -> None:
    models: dict[str, MagicMock] = orm[1]
    models["mail.message"].search_read.return_value = [
        {"id": 10, "date": "2026-01-01 12:00:00"},
        {"id": 9, "date": "2026-01-01 12:00:00"},
        {"id": 99, "date": "2026-01-01 11:00:00"},
    ]
    models["mail.tracking.value"].search_read.side_effect = [[], [
        {"mail_message_id": mid, "old_value_char": "Open", "new_value_char": "Done"}
        for mid in (10, 9, 99)
    ]]
    _, history = source(orm).history(date(2026, 1, 1), date(2026, 1, 1))
    assert [t.source for t in history.transitions] == ["mail.message:99", "mail.message:9", "mail.message:10"]


@pytest.mark.parametrize("old,new", [(0, 2), (-1, 2), (1, 0), (1, -1), (False, 2), (1, 1)])
def test_initial_invalid_and_noop_stages_are_excluded(
    orm: tuple[MagicMock, dict[str, MagicMock]], old: object, new: object,
) -> None:
    models: dict[str, MagicMock] = orm[1]
    models["mail.message"].search_read.return_value = [{"id": 9, "date": datetime(2026, 1, 1, 12)}]
    models["mail.tracking.value"].search_read.side_effect = [[{
        "mail_message_id": 9, "old_value_integer": old, "new_value_integer": new,
    }], []]
    assert source(orm).history(date(2026, 1, 1), date(2026, 1, 1)) == (ReadStatus.AVAILABLE, History((), False))
    models["project.task.type"].search_read.assert_not_called()


@pytest.mark.parametrize("message_extra,tracking_extra,truncated", [
    (0, 0, False), (1, 0, True), (0, 1, True), (1, 1, True),
])
def test_history_bounds_queries_and_batches_stage_visibility(
    orm: tuple[MagicMock, dict[str, MagicMock]], message_extra: int, tracking_extra: int, truncated: bool,
) -> None:
    models: dict[str, MagicMock] = orm[1]
    models["mail.message"].search_read.return_value = [
        {"id": mid, "date": datetime(2026, 1, 1, 12)}
        for mid in range(1, HISTORY_MESSAGE_LIMIT + message_extra + 1)
    ]
    models["mail.tracking.value"].search_read.side_effect = [[
        {"mail_message_id": 1, "old_value_integer": 1, "new_value_integer": 2}
        for _ in range(HISTORY_TRACKING_LIMIT + tracking_extra)
    ], []]
    _, history = source(orm).history(date(2026, 1, 1), date(2026, 1, 1))
    assert len(history.transitions) == HISTORY_TRACKING_LIMIT and not history.complete
    assert history.truncated is truncated
    assert models["mail.message"].search_read.call_args.kwargs["limit"] == HISTORY_MESSAGE_LIMIT + 1
    for call in models["mail.tracking.value"].search_read.call_args_list:
        assert call.kwargs["limit"] == HISTORY_TRACKING_LIMIT + 1
        assert len(call.args[0][0][2]) == HISTORY_MESSAGE_LIMIT
    models["project.task.type"].search_read.assert_called_once_with(
        [("id", "in", [1, 2])], fields=["id"], order="id", load=None,
    )


def test_projection_cannot_expand_global_allowlist(orm: tuple[MagicMock, dict[str, MagicMock]]) -> None:
    with pytest.raises(ValueError, match="Unsupported task projection"):
        _read_task_projection(orm[0], project_id=4, fields=("description", "password"),
                              task_ids=(9,), allowed_fields=frozenset({"description", "password"}))
    orm[0].__getitem__.assert_not_called()


@pytest.mark.parametrize("stage_extra,state_extra", [(0, 0), (1, 0), (0, 1), (1, 1)])
def test_tracking_limit_is_per_field_and_either_overflow_truncates_history(
    orm: tuple[MagicMock, dict[str, MagicMock]], stage_extra: int, state_extra: int,
) -> None:
    models: dict[str, MagicMock] = orm[1]
    models["mail.message"].search_read.return_value = [{"id": 9, "date": datetime(2026, 1, 1, 12)}]
    models["mail.tracking.value"].search_read.side_effect = [[
        {"mail_message_id": 9, "old_value_integer": 1, "new_value_integer": 2}
        for _ in range(HISTORY_TRACKING_LIMIT + stage_extra)
    ], [
        {"mail_message_id": 9, "old_value_char": "Open", "new_value_char": "Done"}
        for _ in range(HISTORY_TRACKING_LIMIT + state_extra)
    ]]
    status, history = source(orm).history(date(2026, 1, 1), date(2026, 1, 1))
    assert status == ReadStatus.AVAILABLE and history is not None
    assert len(history.transitions) == 2 * HISTORY_TRACKING_LIMIT
    assert sum(t.kind == "stage" for t in history.transitions) == HISTORY_TRACKING_LIMIT
    assert sum(t.kind == "state" for t in history.transitions) == HISTORY_TRACKING_LIMIT
    assert history.truncated is bool(stage_extra or state_extra)
    assert history.complete is False
    for call in models["mail.tracking.value"].search_read.call_args_list:
        assert call.kwargs["limit"] == HISTORY_TRACKING_LIMIT + 1


@pytest.mark.parametrize("visible", [True, False])
def test_archived_stage_lookup_disables_active_filter_but_keeps_recipient_rights(
    orm: tuple[MagicMock, dict[str, MagicMock]], visible: bool,
) -> None:
    models: dict[str, MagicMock] = orm[1]
    models["mail.message"].search_read.return_value = [{"id": 9, "date": datetime(2026, 1, 1, 12)}]
    models["mail.tracking.value"].search_read.side_effect = [[{
        "mail_message_id": 9, "old_value_integer": 1, "new_value_integer": 2,
    }], []]
    stages: MagicMock = models["project.task.type"]
    stages.search_read.return_value = [{"id": 1}, {"id": 2}] if visible else [{"id": 2}]
    _, history = source(orm).history(date(2026, 1, 1), date(2026, 1, 1))
    assert len(history.transitions) == int(visible)
    stages.with_context.assert_called_once_with(active_test=False)
    stages.sudo.assert_not_called()


@pytest.mark.parametrize("raw", [None, 12, [], {}])
def test_non_string_description_fails(orm: tuple[MagicMock, dict[str, MagicMock]], raw: object) -> None:
    orm[1]["project.task"].search_read.return_value = [{"id": 9, "description": raw}]
    assert TaskReadTools(source(orm)).read_task_description() == (ReadStatus.FAILED, None)


def test_description_source_error_fails(orm: tuple[MagicMock, dict[str, MagicMock]]) -> None:
    orm[1]["project.task"].search_read.side_effect = RuntimeError("private")
    assert TaskReadTools(source(orm)).read_task_description() == (ReadStatus.FAILED, None)


@pytest.mark.parametrize("start,end", [
    (date.min, date.min), (date.max, date.max), (date(2026, 1, 1), date(2026, 4, 1)),
])
def test_source_rejects_extreme_or_wide_range_before_orm(
    orm: tuple[MagicMock, dict[str, MagicMock]], start: date, end: date,
) -> None:
    with pytest.raises(ValueError):
        source(orm).history(start, end)
    orm[0].__getitem__.assert_not_called()


def test_empty_authorized_messages_do_not_read_privileged_tracking(orm: tuple[MagicMock, dict[str, MagicMock]]) -> None:
    orm[1]["mail.message"].search_read.return_value = []
    orm[1]["mail.tracking.value"].check_access.side_effect = AccessError("private")
    assert source(orm).history(date(2026, 1, 1), date(2026, 1, 1)) == (
        ReadStatus.AVAILABLE, History((), False),
    )
    orm[1]["mail.tracking.value"].sudo.assert_not_called()
    orm[1]["mail.tracking.value"].search_read.assert_not_called()


def test_restricted_tracking_fields_are_unavailable(orm: tuple[MagicMock, dict[str, MagicMock]]) -> None:
    orm[1]["mail.message"].search_read.return_value = [{"id": 9, "date": datetime(2026, 1, 1, 12)}]
    orm[1]["mail.tracking.value"].fields_get.return_value = {}
    orm[1]["mail.tracking.value"].fields_get.side_effect = None
    assert source(orm).history(date(2026, 1, 1), date(2026, 1, 1)) == (ReadStatus.UNAVAILABLE, None)
    orm[1]["mail.tracking.value"].search_read.assert_not_called()


def test_only_fixed_tracking_projection_is_elevated_after_authorization(
    orm: tuple[MagicMock, dict[str, MagicMock]],
) -> None:
    env, models = orm
    raw = models["mail.tracking.value"]
    raw.search_read.side_effect = AccessError("technical journal is system-only")
    elevated = MagicMock()
    elevated.fields_get.return_value = {field: {} for field in (
        "mail_message_id", "old_value_integer", "new_value_integer", "old_value_char", "new_value_char",
    )}
    raw.sudo.return_value = elevated
    models["mail.message"].search_read.return_value = [{"id": 10, "date": datetime(2026, 1, 1, 12)}]
    elevated.search_read.side_effect = [[{
        "mail_message_id": 10, "old_value_integer": 1, "new_value_integer": 2,
    }, {"mail_message_id": 999, "old_value_integer": 1, "new_value_integer": 2}], []]

    assert source(orm).history(date(2026, 1, 1), date(2026, 1, 1)) == (
        ReadStatus.AVAILABLE,
        History((Transition(date(2026, 1, 1), "stage", 1, 2, "mail.message:10"),), False),
    )
    raw.search_read.assert_not_called()
    raw.check_access.assert_not_called()
    raw.sudo.assert_called_once_with()
    for call, field in zip(elevated.search_read.call_args_list, ("stage_id", "state"), strict=True):
        assert call.args == ([
            ("mail_message_id", "in", [10]), ("field_id.name", "=", field),
            ("field_id.model", "=", "project.task"),
        ],)
        assert call.kwargs == {
            "fields": ["mail_message_id", "old_value_integer", "new_value_integer", "old_value_char", "new_value_char"],
            "order": "id", "load": None, "limit": HISTORY_TRACKING_LIMIT + 1,
        }
    for name, model in models.items():
        if name != "mail.tracking.value":
            model.sudo.assert_not_called()
    env.sudo.assert_not_called()
    assert env.su is False


@pytest.mark.parametrize("restriction,expected", [
    ("task", ReadStatus.UNAVAILABLE), ("messages", ReadStatus.FAILED), ("fields", ReadStatus.UNAVAILABLE),
])
def test_denied_scope_never_reaches_elevated_tracking(
    orm: tuple[MagicMock, dict[str, MagicMock]], restriction: str, expected: ReadStatus,
) -> None:
    _, models = orm
    if restriction == "task":
        models["project.task"].search_read.return_value = []
    elif restriction == "messages":
        models["mail.message"].search_read.side_effect = AccessError("private")
    else:
        models["project.task"].fields_get.side_effect = (
            lambda *, allfields, attributes: {field: {} for field in allfields if field == "id"}
        )
    status, result = source(orm).history(date(2026, 1, 1), date(2026, 1, 1))
    assert (status, result) == (expected, None)
    models["mail.tracking.value"].sudo.assert_not_called()
