"""Read a minimal task projection under project membership and Odoo permissions."""

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from odoo.api import Environment
    from odoo.orm.models import BaseModel

_TASK_FIELDS: frozenset[str] = frozenset({
    "id", "name", "active", "is_closed", "date_deadline", "user_ids", "create_date",
})
_PROJECT_MANAGER_GROUP: str = "project.group_project_manager"
_PROJECTION_FIELDS: frozenset[str] = _TASK_FIELDS | {"state", "description"}


def read_project_tasks(
    env: "Environment",
    *,
    project_id: int,
    fields: Sequence[str],
    task_ids: Sequence[int] | None = None,
) -> tuple[dict[str, object], ...] | None:
    """Return only requested, allowed fields of visible tasks in verified scope.

    Take the server-established recipient environment, selected project,
    requested fields and optional task IDs. Require current project membership
    unless the recipient belongs to the Project administrator group. Both roles
    remain subject to Odoo's project, company, task and field access checks.
    Return None for an elevated environment or an unavailable task read.
    Missing and inaccessible projects or task IDs yield no rows.
    Omit fields outside the allowlist or unavailable to the recipient.

    Example: read_project_tasks(env, project_id=4, fields=("name",),
    task_ids=(9,)) returns
    ({"name": "Demo task"},) if task 9 is visible and in scope.
    """
    return _read_task_projection(
        env, project_id=project_id, fields=fields, task_ids=task_ids, allowed_fields=_TASK_FIELDS,
    )


def _read_task_projection(
    env: "Environment", *, project_id: int, fields: Sequence[str],
    task_ids: Sequence[int] | None, allowed_fields: frozenset[str],
) -> tuple[dict[str, object], ...] | None:
    """Authorize a projection restricted by the module's fixed field union.

    Example: _read_task_projection(env, project_id=4, fields=("name",),
    task_ids=(9,), allowed_fields=frozenset({"name"})).
    """
    from odoo import SUPERUSER_ID  # noqa: PLC0415
    from odoo.exceptions import AccessError, MissingError  # noqa: PLC0415

    if not allowed_fields.issubset(_PROJECTION_FIELDS):
        message: str = "Unsupported task projection"
        raise ValueError(message)
    if env.su or env.uid == SUPERUSER_ID:
        return None

    requested: tuple[str, ...] = tuple(dict.fromkeys(field for field in fields if field in allowed_fields))
    if not requested or (task_ids is not None and not task_ids):
        return ()

    try:
        project_domain: list[tuple[str, str, int | list[int]]] = [("id", "=", project_id)]
        if not env.user.has_group(_PROJECT_MANAGER_GROUP):
            project_domain.append(("allowed_internal_user_ids", "in", [env.uid]))
        project: BaseModel = env["project.project"].search(project_domain, limit=1)
    except (AccessError, MissingError):
        return ()
    if not project:
        return ()

    try:
        task_domain: list[tuple[str, str, int | list[int]]] = [("project_id", "=", project_id)]
        if task_ids is not None:
            task_domain.append(("id", "in", list(task_ids)))

        tasks: BaseModel = env["project.task"]
        readable: dict[str, object] = tasks.fields_get(allfields=requested, attributes=("type",))
        selected: list[str] = [field for field in requested if field in readable]
        if not selected:
            return ()

        rows: list[dict[str, object]] = tasks.search_read(
            task_domain, fields=selected, order="id", load=None,
        )
    except (AccessError, MissingError):
        return None
    # Odoo can include id in read results even when it was not requested.
    return tuple({field: row[field] for field in selected if field in row} for row in rows)
