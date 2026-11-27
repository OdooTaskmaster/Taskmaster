"""Check a selected draft recipient independently of model output."""

from datetime import UTC
from typing import TYPE_CHECKING

from .odoo_background import _recipient_environment
from .odoo_task_tools import OdooTaskSource

if __package__ and "." in __package__:
    from ..application.background_operations import ExecutionDenied
    from ..application.task_read_tools import ReadStatus
else:
    from application.background_operations import ExecutionDenied
    from application.task_read_tools import ReadStatus

if TYPE_CHECKING:
    from odoo.api import Environment


def can_address_assignee(env: "Environment", *, project_id: int, task_id: int, recipient_id: int) -> bool:
    """Check the manager's selected assignee, not the audience of a future comment.

    Use a fresh, non-elevated view for each actor in the selected companies.
    Example: can_address_assignee(env, project_id=4, task_id=9, recipient_id=7)
    returns False if the assignment or either actor's access is unavailable.
    This is a prerequisite, not a policy for choosing recipients or publishing.
    """
    from odoo.exceptions import AccessError, MissingError

    if env.su or any(type(value) is not int or value <= 0 for value in (project_id, task_id, recipient_id)):
        return False
    try:
        database: str = env.cr.dbname
        companies: tuple[int, ...] = tuple(env.companies.ids)
        if not companies:
            return False
        with _recipient_environment(env, database, env.uid, companies) as manager:
            if not manager.user.has_group("project.group_project_manager"):
                return False
            status, card = OdooTaskSource(manager, project_id=project_id, task_id=task_id, timezone=UTC).card()
            if status is not ReadStatus.AVAILABLE or card is None or not any(
                assignee.user_id == recipient_id for assignee in (card.assignees or ())
            ):
                return False
        with _recipient_environment(env, database, recipient_id, companies) as recipient:
            status, visible = OdooTaskSource(recipient, project_id=project_id, task_id=task_id, timezone=UTC).card()
            return status is ReadStatus.AVAILABLE and visible is not None
    except (AccessError, MissingError, ExecutionDenied):
        return False
