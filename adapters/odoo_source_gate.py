"""Re-read full context under ordinary recipient rights in a fresh Odoo cursor."""

from datetime import tzinfo
from typing import TYPE_CHECKING

from .odoo_task_tools import OdooTaskSource

if __package__ and "." in __package__:
    from ..application.reading_plan import ContextItem, read_item  # noqa: TID252
    from ..application.task_read_tools import ReadStatus, TaskReadTools  # noqa: TID252
else:
    from application.reading_plan import ContextItem, read_item
    from application.task_read_tools import ReadStatus, TaskReadTools

if TYPE_CHECKING:
    from odoo.api import Environment
    from odoo.modules.registry import Registry


class OdooContextVerifier:
    """Bind a fresh-transaction verifier to server-established recipient scope.

    Example: OdooContextVerifier(env, project_id=4, task_id=9, timezone=UTC).
    Captures no project texts, logs or secrets. Receipts must be server-owned,
    not deserialized from an untrusted model/request. Re-reading is conservative:
    any changed value or visible selection denies the entire generated text.
    """

    def __init__(self, env: "Environment", *, project_id: int, task_id: int, timezone: tzinfo) -> None:
        """Capture only registry/user/company scope; e.g. OdooContextVerifier(env, project_id=4, task_id=9, timezone=UTC)."""
        if env.su or env.uid == 1 or any(type(value) is not int or value <= 0 for value in (project_id, task_id)):
            message: str = "An ordinary server-selected recipient scope required"
            raise ValueError(message)
        self._registry: Registry = env.registry
        self._uid: int = env.uid
        self._project: int = project_id
        self._task: int = task_id
        self._zone: tzinfo = timezone
        self._companies: tuple[int, ...] = tuple(env.companies.ids)
        self._context: dict[str, object] = {"allowed_company_ids": list(self._companies)}
        for key in ("lang", "tz"):
            value: object = env.context.get(key)
            if type(value) is str:
                self._context[key] = value

    def check(self, items: tuple[ContextItem, ...]) -> bool:
        """Re-read every receipt after earlier rights changes commit; e.g. verifier.check(run.items).

        Use an independent cursor/environment, never invalidate caches in the old
        REPEATABLE READ transaction and claim a fresh snapshot. No commit or write.
        Unsaved fixture/project data in another cursor is deliberately invisible.
        """
        from odoo.api import Environment  # noqa: PLC0415

        if type(items) is not tuple or not items or any(type(item) is not ContextItem for item in items):
            return False
        try:
            with self._registry.cursor() as cursor:
                recipient: Environment = Environment(
                    cursor, self._uid, self._context, su=False,
                )
                tools: TaskReadTools = TaskReadTools(OdooTaskSource(
                    recipient, project_id=self._project, task_id=self._task, timezone=self._zone,
                ))
                for item in items:
                    status, current = read_item(tools, item.step)
                    if status is not ReadStatus.AVAILABLE or current != item:
                        return False
                cursor.rollback()
            return True
        except Exception:  # noqa: BLE001 -- No partial authorization or internal details.
            return False
