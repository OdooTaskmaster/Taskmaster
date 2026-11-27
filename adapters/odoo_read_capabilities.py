"""Probe reader metadata without loading message or timesheet records."""

from typing import TYPE_CHECKING

if __package__ and "." in __package__:
    from ..application.task_read_map import ReadTool  # noqa: TID252
else:
    from application.task_read_map import ReadTool

if TYPE_CHECKING:
    from odoo.api import Environment
    from odoo.orm.models import BaseModel


def _permitted(env: "Environment", model_name: str, fields: tuple[str, ...]) -> bool:
    """Check model and field access only; e.g. _permitted(env, 'mail.message', ('body',))."""
    if model_name not in env.registry.models:
        return False
    model: BaseModel = env[model_name]
    return model.has_access("read") and set(fields).issubset(
        model.fields_get(allfields=fields, attributes=("type",)),
    )


def readable_types(env: "Environment", project_id: int) -> tuple[ReadTool, ...]:
    """Probe types after selected-task authorization; e.g. readable_types(env, 4).

    Do not inspect source rows or infer presence from record rules. A permitted
    branch may yield no rows. The project read projects only timesheet enablement.
    """
    result: list[ReadTool] = [ReadTool.CARD]
    if _permitted(env, "project.task", ("description",)):
        result.append(ReadTool.DESCRIPTION)
    if (_permitted(env, "mail.message", ("id", "date"))
            and "mail.tracking.value" in env.registry.models
            and (_permitted(env, "project.task", ("stage_id",)) or _permitted(env, "project.task", ("state",)))):
        result.append(ReadTool.HISTORY)
    if _permitted(env, "mail.message", ("id", "date", "body")):
        result.append(ReadTool.COMMENTS)
    if (_permitted(env, "account.analytic.line", ("id", "date", "unit_amount", "product_uom_id", "task_id", "project_id"))
            and _permitted(env, "project.project", ("allow_timesheets", "company_id"))
            and _permitted(env, "uom.uom", ("id", "name"))):
        rows: list[dict[str, object]] = env["project.project"].search_read(
            [("id", "=", project_id)], fields=["allow_timesheets"], load=None, limit=1,
        )
        if rows and rows[0].get("allow_timesheets") is True:
            result.append(ReadTool.WORKLOG)
    return tuple(result)
