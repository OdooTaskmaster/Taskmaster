"""Read current Odoo task facts for an on-demand risk summary."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ._core import risk_summary as core
from .odoo_summary_access import _FIELDS
from .odoo_task_reader import _read_task_projection, readable_projects

if TYPE_CHECKING:
    from odoo.api import Environment
    from odoo.orm.models import BaseModel


def read_summary_tasks(env: "Environment", project_id: int) -> tuple[core.SummaryTask, ...]:
    """Return normalized visible task facts or raise a neutral read error.

    Do not assert completeness of activity history. Verify whether a seemingly
    empty assignment relation is actually empty before passing it to RR-03.
    Example: read_summary_tasks(env, 4) returns () for a readable empty project.
    """
    from odoo.exceptions import AccessError, MissingError  # noqa: PLC0415

    if not readable_projects(env, project_id):
        raise core.SummaryReadError()
    rows: tuple[dict[str, object], ...] | None = _read_task_projection(
        env, project_id=project_id, fields=_FIELDS, task_ids=None, allowed_fields=frozenset(_FIELDS),
    )
    if rows is None or any(not all(field in row for field in _FIELDS) for row in rows):
        raise core.SummaryReadError()

    empty_candidates: list[int] = []
    user_ids: set[int] = set()
    for row in rows:
        task_id: object = row["id"]
        assigned: object = row["user_ids"]
        if type(task_id) is not int or task_id <= 0:
            raise core.SummaryReadError()
        if isinstance(assigned, (list, tuple)) and all(type(uid) is int and uid > 0 for uid in assigned):
            if assigned:
                user_ids.update(assigned)
            else:
                empty_candidates.append(task_id)

    empty_ids: set[int] = set()
    names: dict[int, str | None] = {}
    try:
        if empty_candidates:
            # Odoo 20's x2many = False checks the relation, not the filtered IDs from read().
            tasks: BaseModel = env["project.task"]
            empty_ids = set(tasks.search([
                ("project_id", "=", project_id), ("id", "in", empty_candidates), ("user_ids", "=", False),
            ]).ids)
        if user_ids:
            users: BaseModel = env["res.users"]
            if "name" in users.fields_get(allfields=("name",), attributes=("type",)):
                records: list[dict[str, object]] = users.search_read(
                    [("id", "in", sorted(user_ids))], fields=["id", "name"], load=None,
                )
                names = {item["id"]: item["name"] for item in records
                         if type(item.get("id")) is int and isinstance(item.get("name"), str)}
    except (AccessError, MissingError):
        # Missing evidence does not prevent evaluating independent rules.
        pass

    snapshots: list[core.SummaryTask] = []
    for row in rows:
        task_id = row["id"]
        assigned = row["user_ids"]
        assignees: tuple[core.SummaryAssignee, ...] | None = () if task_id in empty_ids else None
        if isinstance(assigned, (list, tuple)) and assigned:
            assignees = tuple(core.SummaryAssignee(uid, names[uid]) for uid in assigned if type(uid) is int and uid in names) or None
        value: object = row["date_deadline"]
        deadline: datetime | None = None
        if isinstance(value, datetime):
            deadline = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        elif isinstance(value, str) and (" " in value or "T" in value):
            try:
                parsed: datetime = datetime.fromisoformat(value)
                deadline = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
            except ValueError:
                pass
        active: object = row["active"]
        closed: object = row["is_closed"]
        name: object = row["name"]
        snapshots.append(core.SummaryTask(
            task_id, name if isinstance(name, str) else None,
            core.DeadlineTaskSnapshot(active if type(active) is bool else None,
                                      closed if type(closed) is bool else None,
                                      deadline, value is False or deadline is not None),
            assignees,
        ))
    return tuple(snapshots)
