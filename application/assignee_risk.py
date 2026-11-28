"""Evaluate RR-03 without reading assignments from Odoo."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

from .risk_status import RiskStatus, task_is_open


@dataclass(frozen=True, slots=True)
class AssigneeRiskResult:
    """Report RR-03 with the supplied outcome and no deadline metrics.

    Example: AssigneeRiskResult(RiskStatus.TRIGGERED) returns an RR-03 signal.
    """

    rule_id: ClassVar[str] = "RR-03"
    status: RiskStatus


def evaluate_assignee_risk(
    *, active: bool | None, is_closed: bool | None, assignee_ids: Sequence[int] | None,
) -> AssigneeRiskResult:
    """Evaluate RR-03 from task flags and confirmed assignments.

    Use None for an unknown flag or for assignments that cannot be confirmed
    empty and contain no known assignee. An empty sequence must be confirmed
    complete. Return an RR-03 outcome without requiring any deadline.

    Example: evaluate_assignee_risk(active=True, is_closed=False, assignee_ids=())
    returns AssigneeRiskResult(RiskStatus.TRIGGERED).
    """
    is_open: bool | None = task_is_open(active, is_closed)
    if is_open is False or assignee_ids:
        return AssigneeRiskResult(RiskStatus.NOT_TRIGGERED)
    if is_open is None or assignee_ids is None:
        return AssigneeRiskResult(RiskStatus.NOT_EVALUATED)
    return AssigneeRiskResult(RiskStatus.TRIGGERED)
