"""Authorize persisted risk summaries in the active Odoo request snapshot."""

from typing import TYPE_CHECKING

from ._core import summary_store
from .odoo_task_reader import _read_task_projection, readable_projects

if TYPE_CHECKING:
    from odoo.api import Environment

_FIELDS: tuple[str, ...] = ("id", "name", "active", "state", "is_closed", "date_deadline", "user_ids")


def can_read_summary(env: "Environment", saved: summary_store.SavedSummary) -> bool:
    """Check the recipient, project and all recorded source records and fields.

    Unknown provenance fails closed, including when the snapshot has no rows.
    Example: can_read_summary(env, saved) returns False after membership revocation.
    """
    from odoo.exceptions import AccessError, MissingError  # noqa: PLC0415

    snapshot = saved.summary
    sources: tuple[int, ...] | None = snapshot.source_task_ids
    if env.uid != saved.recipient_id or sources is None or not readable_projects(env, saved.project_id):
        return False
    if sources:
        rows: tuple[dict[str, object], ...] | None = _read_task_projection(
            env, project_id=saved.project_id, task_ids=sources, fields=_FIELDS, allowed_fields=frozenset(_FIELDS),
        )
        if rows is None or {row.get("id") for row in rows} != set(sources):
            return False
        if any(not all(field in row for field in _FIELDS) for row in rows):
            return False
    users: tuple[int, ...] = snapshot.source_assignee_ids
    if users:
        try:
            model = env["res.users"]
            if "name" not in model.fields_get(allfields=("name",), attributes=("type",)):
                return False
            visible: list[dict[str, object]] = model.search_read(
                [("id", "in", list(users))], fields=["id", "name"], load=None,
            )
            return {row.get("id") for row in visible if "name" in row} == set(users)
        except (AccessError, MissingError):
            return False
    return True
