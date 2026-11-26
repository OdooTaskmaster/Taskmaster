"""Define the outcomes shared by the independent risk rules."""

from enum import StrEnum


class RiskStatus(StrEnum):
    """Identify an emitted signal, a negative check, or an incomplete check.

    A serialized outcome maps to the corresponding enum member.
    Example: RiskStatus("not_evaluated") returns RiskStatus.NOT_EVALUATED.
    """

    TRIGGERED = "triggered"
    NOT_TRIGGERED = "not_triggered"
    NOT_EVALUATED = "not_evaluated"


def task_is_open(active: bool | None, is_closed: bool | None) -> bool | None:
    """Combine the archive and closed flags, using None for unknown inputs.

    Return False for an archived or closed task, True for a confirmed open
    task, and None otherwise. Example: task_is_open(True, False) returns True.
    """
    if active is False or is_closed is True:
        return False
    if active is None or is_closed is None:
        return None
    return True
