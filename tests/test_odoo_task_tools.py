"""Test scoped Odoo task tools using a mocked ORM, not a live Odoo database."""

import sys
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from types import ModuleType
from unittest.mock import MagicMock

import pytest
from adapters.odoo_task_tools import OdooTaskSource, _utc_midnight
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
    assert card.source == "/odoo/tasks/9"
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
        ReadStatus.AVAILABLE, Description("", "/odoo/tasks/9", False),
    )


def test_description_and_revoked_project(orm: tuple[MagicMock, dict[str, MagicMock]]) -> None:
    _, models = orm
    models["project.task"].search_read.return_value = [{"id": 9, "description": "<p>hello</p>"}]
    tools = TaskReadTools(source(orm))
    assert tools.read_task_description() == (ReadStatus.AVAILABLE, Description("hello", "/odoo/tasks/9", False))
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


def test_unavailable_tracking_is_incomplete_not_empty_verified(
    orm: tuple[MagicMock, dict[str, MagicMock]],
) -> None:
    env, models = orm
    del env.registry.models["mail.tracking.value"]
    assert source(orm).history(date(2026, 1, 1), date(2026, 1, 1)) == (
        ReadStatus.AVAILABLE, History((), False),
    )
    models["mail.message"].search_read.assert_not_called()


def test_denied_tracking_never_exposes_raw_exception(orm: tuple[MagicMock, dict[str, MagicMock]]) -> None:
    _, models = orm
    models["mail.message"].search_read.side_effect = AccessError("private")
    assert source(orm).history(date(2026, 1, 1), date(2026, 1, 1)) == (
        ReadStatus.AVAILABLE, History((), False),
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
