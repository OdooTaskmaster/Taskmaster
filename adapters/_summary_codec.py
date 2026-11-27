"""Encode typed summaries as versioned JSON, never executable serialized objects."""

from dataclasses import asdict
from datetime import date, datetime
import json
from typing import Any

from ._core import StateStoreError, risk_summary as core


def _nonnegative(value: object) -> bool:
    """Check an integer metric, excluding bool; _nonnegative(0) returns True."""
    return type(value) is int and value >= 0


def _validate(summary: core.ProjectRiskSummary) -> None:
    """Reject inconsistent types, coverage, statuses or metrics in a decoded result.

    Example: _validate(result) returns None for a well-formed summary.
    """
    rules: tuple[str, ...] = ("RR-01", "RR-02", "RR-03", "RR-04")
    checked: tuple[str, ...] = summary.checked_rules
    unchecked: tuple[str, ...] = summary.unchecked_rules
    if (not isinstance(summary.status, core.SummaryStatus)
            or checked != tuple(rule for rule in rules if rule in checked)
            or unchecked != tuple(rule for rule in rules if rule in unchecked)
            or set(checked) & set(unchecked) or set(checked) | set(unchecked) != set(rules)):
        raise StateStoreError()
    if (summary.status is core.SummaryStatus.COMPLETE and (not summary.rows or unchecked)
            or summary.status is core.SummaryStatus.EMPTY and (summary.rows or unchecked)
            or summary.status is core.SummaryStatus.PARTIAL and not unchecked
            or summary.status is core.SummaryStatus.FAILED and (summary.rows or checked)):
        raise StateStoreError()
    seen: set[int] = set()
    for row in summary.rows:
        if (type(row.task_id) is not int or row.task_id <= 0 or row.task_id in seen
                or row.name is not None and not isinstance(row.name, str)
                or type(row.deadline_known) is not bool
                or not row.deadline_known and row.deadline is not None
                or row.task_url != f"/odoo/tasks/{row.task_id}"):
            raise StateStoreError()
        seen.add(row.task_id)
        if row.assignees is not None:
            ids: set[int] = set()
            for assignee in row.assignees:
                if (type(assignee.user_id) is not int or assignee.user_id <= 0 or assignee.user_id in ids
                        or assignee.name is not None and not isinstance(assignee.name, str)):
                    raise StateStoreError()
                ids.add(assignee.user_id)
        if row.deadline is not None and not isinstance(row.deadline, date):
            raise StateStoreError()
        if isinstance(row.deadline, datetime) and row.deadline.utcoffset() is None:
            raise StateStoreError()
        if (row.deadline is None) != (row.deviation is None):
            raise StateStoreError()
        if row.deviation is not None and (type(row.deviation.overdue) is not bool or not _nonnegative(row.deviation.days)):
            raise StateStoreError()
        reason_ids: tuple[str, ...] = tuple(reason.rule_id for reason in row.reasons)
        if not reason_ids or reason_ids != tuple(rule for rule in rules if rule in reason_ids):
            raise StateStoreError()
        for reason in row.reasons:
            if reason.status is not core.RiskStatus.TRIGGERED:
                raise StateStoreError()
            if isinstance(reason, core.DeadlineRiskResult):
                if (not _nonnegative(reason.overdue_days) or row.deviation is None
                        or not row.deviation.overdue or row.deviation.days != reason.overdue_days):
                    raise StateStoreError()
            elif isinstance(reason, core.StalledDeadlineRiskResult):
                if (not _nonnegative(reason.days_until_deadline) or reason.days_until_deadline > 3
                        or not _nonnegative(reason.days_without_movement) or reason.days_without_movement < 2
                        or row.deviation is None or row.deviation.overdue
                        or row.deviation.days != reason.days_until_deadline):
                    raise StateStoreError()
            elif isinstance(reason, core.AssigneeRiskResult):
                if row.assignees != ():
                    raise StateStoreError()
            elif isinstance(reason, core.InactivityRiskResult):
                if not _nonnegative(reason.days_without_activity) or reason.days_without_activity < 7:
                    raise StateStoreError()
            else:
                raise StateStoreError()


def encode_summary(summary: core.ProjectRiskSummary) -> str:
    """Return JSON preserving date precision, rule types and display fields.

    Example: encode_summary(result) returns a version-1 JSON string.
    """
    _validate(summary)
    rows: list[dict[str, Any]] = []
    for row in summary.rows:
        value: dict[str, Any] = asdict(row)
        value["deadline"] = (
            None if row.deadline is None else {
                "kind": "datetime" if isinstance(row.deadline, datetime) else "date",
                "value": row.deadline.isoformat(),
            }
        )
        value["reasons"] = [{"rule_id": reason.rule_id, **asdict(reason)} for reason in row.reasons]
        rows.append(value)
    return json.dumps({
        "version": 1, "status": summary.status.value, "rows": rows,
        "checked_rules": summary.checked_rules, "unchecked_rules": summary.unchecked_rules,
    }, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def decode_summary(payload: str) -> core.ProjectRiskSummary:
    """Rebuild an immutable result or raise a fixed error for corrupt storage.

    Example: decode_summary(encode_summary(result)) returns an equal result.
    """
    try:
        data: dict[str, Any] = json.loads(payload)
        if type(data["version"]) is not int or data["version"] != 1:
            raise ValueError
        rows: list[core.SummaryRow] = []
        factories: dict[str, type] = {
            "RR-01": core.DeadlineRiskResult, "RR-02": core.StalledDeadlineRiskResult,
            "RR-03": core.AssigneeRiskResult, "RR-04": core.InactivityRiskResult,
        }
        for row in data["rows"]:
            deadline: date | datetime | None = None
            if row["deadline"] is not None:
                kind: str = row["deadline"]["kind"]
                value: str = row["deadline"]["value"]
                if kind == "datetime":
                    deadline = datetime.fromisoformat(value)
                    if deadline.utcoffset() is None:
                        raise ValueError
                elif kind == "date":
                    deadline = date.fromisoformat(value)
                else:
                    raise ValueError
            assignees: tuple[core.SummaryAssignee, ...] | None = (
                None if row["assignees"] is None else tuple(core.SummaryAssignee(**item) for item in row["assignees"])
            )
            reasons: list[core.RiskReason] = []
            for reason in row["reasons"]:
                args: dict[str, Any] = {key: value for key, value in reason.items() if key != "rule_id"}
                args["status"] = core.RiskStatus(args["status"])
                if args["status"] is not core.RiskStatus.TRIGGERED:
                    raise ValueError
                reasons.append(factories[reason["rule_id"]](**args))
            if not reasons or type(row["task_id"]) is not int or row["task_id"] <= 0:
                raise ValueError
            if row["task_url"] != f"/odoo/tasks/{row['task_id']}":
                raise ValueError
            if not row["deadline_known"] and deadline is not None:
                raise ValueError
            deviation: core.DeadlineDeviation | None = (
                None if row["deviation"] is None else core.DeadlineDeviation(**row["deviation"])
            )
            if deadline is None and deviation is not None:
                raise ValueError
            rows.append(core.SummaryRow(
                row["task_id"], row["name"], assignees, deadline, row["deadline_known"],
                deviation, row["task_url"], tuple(reasons),
            ))
        summary: core.ProjectRiskSummary = core.ProjectRiskSummary(
            core.SummaryStatus(data["status"]), tuple(rows), tuple(data["checked_rules"]), tuple(data["unchecked_rules"]),
        )
        _validate(summary)
        return summary
    except (ValueError, KeyError, TypeError, AttributeError):
        raise StateStoreError() from None
