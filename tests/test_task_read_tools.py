"""Check task tool contracts using a fake, server-scoped source."""

from datetime import date, timedelta
from unittest.mock import Mock

import pytest
from application.task_read_tools import (
    DESCRIPTION_LIMIT,
    Description,
    History,
    ReadStatus,
    TaskCard,
    TaskReadTools,
)


def test_card_is_bound_to_source_and_has_only_contract_fields() -> None:
    source = Mock()
    card = TaskCard("Demo", "in_progress", None, True, (), "/odoo/tasks/9")
    source.card.return_value = (ReadStatus.AVAILABLE, card)
    tools = TaskReadTools(source)
    assert tools.read_task_card() == (ReadStatus.AVAILABLE, card)
    source.card.assert_called_once_with()
    assert set(card.__dataclass_fields__) == {
        "name", "state", "deadline", "deadline_known", "assignees", "source", "assignees_complete",
    }
    with pytest.raises(TypeError):
        tools.read_task_card(task_id=10)


@pytest.mark.parametrize("status", [ReadStatus.UNAVAILABLE, ReadStatus.FAILED])
def test_description_does_not_return_inaccessible_text(status: ReadStatus) -> None:
    source = Mock()
    source.description.return_value = (status, "hidden", "hidden-ref")
    assert TaskReadTools(source).read_task_description() == (status, None)


def test_description_removes_markup_and_executable_content() -> None:
    source = Mock()
    source.description.return_value = (
        ReadStatus.AVAILABLE, "<p>Hello &amp; bye</p><script>secret()</script>", "ref",
    )
    assert TaskReadTools(source).read_task_description() == (
        ReadStatus.AVAILABLE, Description("Hello & bye", "ref", False),
    )


@pytest.mark.parametrize("length", [0, DESCRIPTION_LIMIT, DESCRIPTION_LIMIT + 1])
def test_description_limit(length: int) -> None:
    source = Mock()
    source.description.return_value = (ReadStatus.AVAILABLE, "x" * length, "ref")
    status, result = TaskReadTools(source).read_task_description()
    assert status == ReadStatus.AVAILABLE
    assert result == Description("x" * min(length, DESCRIPTION_LIMIT), "ref", length > DESCRIPTION_LIMIT)


def test_inconsistent_description_fails_closed() -> None:
    source = Mock()
    source.description.return_value = (ReadStatus.AVAILABLE, None, "ref")
    assert TaskReadTools(source).read_task_description() == (ReadStatus.FAILED, None)


@pytest.mark.parametrize("days", [-1, 90])
def test_invalid_period_never_reads(days: int) -> None:
    source = Mock()
    start = date(2026, 1, 1)
    with pytest.raises(ValueError):
        TaskReadTools(source).read_task_stage_history(start, start + timedelta(days=days))
    source.history.assert_not_called()


def test_90_day_period_and_coverage_are_preserved() -> None:
    source = Mock()
    source.history.side_effect = [
        (ReadStatus.AVAILABLE, History((), False)),
        (ReadStatus.AVAILABLE, History((), True)),
    ]
    tools = TaskReadTools(source)
    start = date(2026, 1, 1)
    end = start + timedelta(days=89)
    assert tools.read_task_stage_history(start, end) == (ReadStatus.AVAILABLE, History((), False))
    assert tools.read_task_stage_history(start, end) == (ReadStatus.AVAILABLE, History((), True))
    assert source.history.call_count == 2
