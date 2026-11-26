"""Verify that RR-03 requires a confirmed absence of assignees."""

from collections.abc import Sequence

import pytest

from application.assignee_risk import AssigneeRiskResult, evaluate_assignee_risk
from application.risk_status import RiskStatus


@pytest.mark.parametrize(
    ("active", "is_closed", "assignee_ids", "expected"),
    [
        pytest.param(True, False, [], RiskStatus.TRIGGERED, id="confirmed-empty"),
        pytest.param(True, False, (4,), RiskStatus.NOT_TRIGGERED, id="one-confirmed-assignee"),
        pytest.param(True, False, [4, 8], RiskStatus.NOT_TRIGGERED, id="multiple-assignees"),
        pytest.param(True, False, None, RiskStatus.NOT_EVALUATED, id="missing-or-incomplete-assignments"),
        pytest.param(True, True, (), RiskStatus.NOT_TRIGGERED, id="done-or-cancelled"),
        pytest.param(False, False, (), RiskStatus.NOT_TRIGGERED, id="archived"),
        pytest.param(False, None, None, RiskStatus.NOT_TRIGGERED, id="known-archived"),
        pytest.param(True, None, (), RiskStatus.NOT_EVALUATED, id="unknown-closed-flag"),
        pytest.param(None, False, (), RiskStatus.NOT_EVALUATED, id="unknown-active-flag"),
    ],
)
def test_assignee_risk(
    active: bool | None, is_closed: bool | None, assignee_ids: Sequence[int] | None, expected: RiskStatus,
) -> None:
    """Assert the expected RR-03 outcome for task flags and assignments.

    Example: test_assignee_risk(True, False, (), RiskStatus.TRIGGERED)
    completes without error.
    """
    result: AssigneeRiskResult = evaluate_assignee_risk(active=active, is_closed=is_closed, assignee_ids=assignee_ids)
    assert (result.rule_id, result.status) == ("RR-03", expected)
