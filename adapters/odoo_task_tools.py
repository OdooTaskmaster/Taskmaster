"""Read scoped task-tool data with the recipient's Odoo environment."""

from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING

from .odoo_task_reader import _read_task_projection

if __package__ and "." in __package__:
    from ..application.task_read_tools import (  # noqa: TID252
        Comments,
        History,
        ReadStatus,
        TaskAssignee,
        TaskCard,
        Transition,
        Worklog,
        validate_comment_limit,
        validate_history_period,
    )
else:
    from application.task_read_tools import (
        Comments,
        History,
        ReadStatus,
        TaskAssignee,
        TaskCard,
        Transition,
        Worklog,
        validate_comment_limit,
        validate_history_period,
    )

if TYPE_CHECKING:
    from odoo.api import Environment
    from odoo.orm.models import BaseModel

_FIELDS: frozenset[str] = frozenset({"id", "name", "state", "date_deadline", "user_ids", "description"})
HISTORY_MESSAGE_LIMIT: int = 500
# Per readable field, not per response: at most 2000 transitions for stage + state.
HISTORY_TRACKING_LIMIT: int = 1000


def _utc_midnight(day: date, zone: tzinfo) -> datetime:
    """Convert a project-local midnight to naive UTC for an Odoo domain.

    Accept stdlib zones and Odoo's pytz zones. Ambiguous or missing midnight
    raises rather than guessing (zone-specific exceptions for pytz).
    Conversion can raise OverflowError at datetime bounds.
    Example: _utc_midnight(day, UTC).
    """
    local: datetime = datetime.combine(day, time.min)
    localize = getattr(zone, "localize", None)
    if callable(localize):
        aware: datetime = localize(local, is_dst=None)
    else:
        aware = local.replace(tzinfo=zone)
        # Some zones skip or repeat midnight; don't silently choose an instant.
        alternate: datetime = aware.replace(fold=1)
        if aware.utcoffset() != alternate.utcoffset():
            message: str = "Ambiguous or nonexistent local midnight"
            raise ValueError(message)
    return aware.astimezone(UTC).replace(tzinfo=None)


class OdooTaskSource:
    """Bind task tools to a server-selected recipient, project, task and time zone.

    Supply an unprivileged current-user environment, positive IDs and a project
    time zone. Example: OdooTaskSource(env, project_id=4, task_id=9, timezone=UTC).
    Each operation repeats the project and task permission checks.
    """

    def __init__(self, env: "Environment", *, project_id: int, task_id: int, timezone: tzinfo) -> None:
        if type(project_id) is not int or type(task_id) is not int or project_id <= 0 or task_id <= 0:
            message: str = "Positive project and task IDs required"
            raise ValueError(message)
        if not isinstance(timezone, tzinfo):
            message = "Project time zone required"
            raise TypeError(message)
        self._env: Environment = env
        self._project_id: int = project_id
        self._task_id: int = task_id
        self._timezone: tzinfo = timezone

    def _read(self, fields: tuple[str, ...]) -> tuple[ReadStatus, dict[str, object] | None]:
        try:
            rows: tuple[dict[str, object], ...] | None = _read_task_projection(
                self._env, project_id=self._project_id, fields=("id", *fields),
                task_ids=(self._task_id,), allowed_fields=_FIELDS,
            )
        except Exception:  # noqa: BLE001
            return ReadStatus.FAILED, None
        if rows is None:
            return ReadStatus.UNAVAILABLE, None
        if not rows or rows[0].get("id") != self._task_id:
            return ReadStatus.UNAVAILABLE, None
        return ReadStatus.AVAILABLE, rows[0]

    def card(self) -> tuple[ReadStatus, TaskCard | None]:
        """Return the visible card fields and a server-generated task reference.

        Example: source.card() returns (ReadStatus.UNAVAILABLE, None) for a hidden task.
        """
        status, row = self._read(("name", "state", "date_deadline", "user_ids"))
        if row is None:
            return status, None
        name: object = row.get("name")
        state: object = row.get("state")
        raw_deadline: object = row.get("date_deadline")
        deadline: date | datetime | None = None
        deadline_known: bool = "date_deadline" in row and raw_deadline is False
        if isinstance(raw_deadline, datetime):
            deadline = raw_deadline.replace(tzinfo=UTC) if raw_deadline.tzinfo is None else raw_deadline
            deadline_known = True
        elif type(raw_deadline) is date:
            deadline = raw_deadline
            deadline_known = True
        elif isinstance(raw_deadline, str):
            try:
                if "T" in raw_deadline or " " in raw_deadline:
                    deadline = datetime.fromisoformat(raw_deadline)
                    if deadline.tzinfo is None:
                        deadline = deadline.replace(tzinfo=UTC)
                    deadline_known = True
                else:
                    deadline = date.fromisoformat(raw_deadline)
                    deadline_known = True
            except ValueError:
                pass
        raw_users: object = row.get("user_ids")
        assignees: tuple[TaskAssignee, ...] | None = None
        if isinstance(raw_users, (list, tuple)) and all(type(item) is int and item > 0 for item in raw_users):
            # An empty relational read may be filtered by Odoo record rules.
            if raw_users:
                from odoo.exceptions import AccessError, MissingError  # noqa: PLC0415

                try:
                    users: BaseModel = self._env["res.users"]
                    user_fields: list[str] = ["id"]
                    if "name" in users.fields_get(allfields=("name",), attributes=("type",)):
                        user_fields.append("name")
                    visible: list[dict[str, object]] = users.search_read(
                        [("id", "in", list(raw_users))], fields=user_fields, order="id", load=None,
                    )
                    visible_by_id: dict[int, dict[str, object]] = {
                        item["id"]: item for item in visible if type(item.get("id")) is int
                    }
                    assignees = tuple(
                        TaskAssignee(item, visible_by_id[item].get("name") if isinstance(
                            visible_by_id[item].get("name"), str,
                        ) else None)
                        for item in raw_users if item in visible_by_id
                    ) or None
                except (AccessError, MissingError):
                    pass
                except Exception:  # noqa: BLE001
                    return ReadStatus.FAILED, None
        return status, TaskCard(
            name if isinstance(name, str) else None,
            state if isinstance(state, str) else None,
            deadline, deadline_known, assignees, self._task_ref(),
        )

    def description(self) -> tuple[ReadStatus, str | None, str | None]:
        """Return authorized HTML; the application tool converts it to plain text.

        Example: source.description() returns a status, HTML and task reference.
        """
        status, row = self._read(("description",))
        if row is None:
            return status, None, None
        if "description" not in row:
            return ReadStatus.UNAVAILABLE, None, None
        value: object = row["description"]
        if value is False:
            return status, "", self._task_ref()
        if not isinstance(value, str):
            return ReadStatus.FAILED, None, None
        return status, value, self._task_ref()

    def _task_ref(self) -> str:
        # The /tasks action requires active_id; use the router's direct model path.
        return f"/odoo/project.task/{self._task_id}"

    def comments(self, start: date, end: date, limit: int) -> tuple[ReadStatus, Comments | None]:
        """Read recipient-visible comments after scope validation; e.g. source.comments(start, end, 5)."""
        from .odoo_task_activity import _comments  # noqa: PLC0415

        validate_history_period(start, end)
        validate_comment_limit(limit)
        status, row = self._read(())
        if row is None:
            return status, None
        return _comments(self._env, self._task_id, self._timezone, start, end, limit)

    def worklog(self, start: date, end: date) -> tuple[ReadStatus, Worklog | None]:
        """Read recipient-visible work-date totals after scope validation; e.g. source.worklog(start, end)."""
        from .odoo_task_activity import _worklog  # noqa: PLC0415

        validate_history_period(start, end)
        status, row = self._read(())
        if row is None:
            return status, None
        return _worklog(self._env, self._project_id, self._task_id, start, end)

    def history(self, start: date, end: date) -> tuple[ReadStatus, History | None]:
        """Read only visible structured tracking for the selected task and period.

        Example: source.history(date(2026, 1, 1), date(2026, 1, 2)).
        Authorize task, messages and fields as the recipient before reading a
        fixed tracking projection with elevated rights. Never elevate the
        recipient environment. Read at most HISTORY_TRACKING_LIMIT values per
        field (stage_id and state), up to 2 * HISTORY_TRACKING_LIMIT transitions.
        Any per-field overflow marks the entire history truncated. Include
        archived stages only when ordinary recipient rights allow them.
        Coverage remains unconfirmed.
        """
        from odoo.exceptions import AccessError, MissingError  # noqa: PLC0415

        validate_history_period(start, end)
        status, row = self._read(())
        if row is None:
            return status, None
        if "mail.tracking.value" not in self._env.registry.models:
            return ReadStatus.UNAVAILABLE, None
        try:
            start_utc: datetime = _utc_midnight(start, self._timezone)
            end_utc: datetime = _utc_midnight(end + timedelta(days=1), self._timezone)
            task_model: BaseModel = self._env["project.task"]
            tracked_fields: tuple[str, ...] = ("stage_id", "state")
            readable_fields: dict[str, object] = task_model.fields_get(allfields=tracked_fields, attributes=("type",))
            fields: tuple[str, ...] = (
                "mail_message_id", "old_value_integer", "new_value_integer", "old_value_char", "new_value_char",
            )
            if not set(tracked_fields).intersection(readable_fields):
                return ReadStatus.UNAVAILABLE, None
            messages: BaseModel = self._env["mail.message"]
            message_fields: tuple[str, ...] = ("id", "date")
            if not set(message_fields).issubset(messages.fields_get(allfields=message_fields, attributes=("type",))):
                return ReadStatus.UNAVAILABLE, None
            rows: list[dict[str, object]] = messages.search_read(
                [("model", "=", "project.task"), ("res_id", "=", self._task_id),
                 ("date", ">=", start_utc), ("date", "<", end_utc)],
                fields=list(message_fields), order="date, id", load=None, limit=HISTORY_MESSAGE_LIMIT + 1,
            )
            truncated: bool = len(rows) > HISTORY_MESSAGE_LIMIT
            days: dict[int, date] = {}
            instants: dict[int, datetime] = {}
            for message in rows[:HISTORY_MESSAGE_LIMIT]:
                message_id: object = message.get("id")
                raw_date: object = message.get("date")
                if type(message_id) is not int:
                    continue
                try:
                    if isinstance(raw_date, datetime):
                        instant: datetime = raw_date
                    elif isinstance(raw_date, str) and ("T" in raw_date or " " in raw_date):
                        instant = datetime.fromisoformat(raw_date)
                    else:
                        continue
                    if instant.tzinfo is None:
                        instant = instant.replace(tzinfo=UTC)
                    day: date = instant.astimezone(self._timezone).date()
                except (ValueError, OverflowError):
                    continue
                if start <= day <= end:
                    days[message_id] = day
                    instants[message_id] = instant.astimezone(UTC)
            if not days:
                return status, History((), False, truncated)
            # Only these messages and task fields were authorized above. Raw
            # tracking is system-only in Odoo 20; never expose its general API.
            tracking: BaseModel = self._env["mail.tracking.value"].sudo()
            if not set(fields).issubset(tracking.fields_get(allfields=fields, attributes=("type",))):
                return ReadStatus.UNAVAILABLE, None
            pending: list[tuple[int, str, str | int, str | int]] = []
            for field, kind in (("stage_id", "stage"), ("state", "state")):
                if field not in readable_fields:
                    continue
                values: list[dict[str, object]] = tracking.search_read(
                    [("mail_message_id", "in", list(days)), ("field_id.name", "=", field),
                     ("field_id.model", "=", "project.task")], fields=list(fields), order="id", load=None,
                    limit=HISTORY_TRACKING_LIMIT + 1,
                )
                truncated = truncated or len(values) > HISTORY_TRACKING_LIMIT
                for value in values[:HISTORY_TRACKING_LIMIT]:
                    mid: object = value.get("mail_message_id")
                    if type(mid) is not int or mid not in days:
                        continue
                    old: object = value.get("old_value_integer" if field == "stage_id" else "old_value_char")
                    new: object = value.get("new_value_integer" if field == "stage_id" else "new_value_char")
                    expected: type = int if field == "stage_id" else str
                    if type(old) is not expected or type(new) is not expected or old == new:
                        continue
                    if field == "stage_id" and (old <= 0 or new <= 0):
                        continue
                    if field == "state" and (not old or not new):
                        continue
                    pending.append((mid, kind, old, new))
            stage_ids: list[int] = sorted({
                value for _, kind, old, new in pending if kind == "stage"
                for value in (old, new) if type(value) is int
            })
            visible_ids: set[int] = set()
            if stage_ids:
                visible_stages: list[dict[str, object]] = self._env["project.task.type"].with_context(active_test=False).search_read(
                    [("id", "in", stage_ids)], fields=["id"], order="id", load=None,
                )
                visible_ids = {item["id"] for item in visible_stages if type(item.get("id")) is int}
            pending.sort(key=lambda item: (instants[item[0]], item[0], item[1]))
            transitions: tuple[Transition, ...] = tuple(
                Transition(days[mid], kind, old, new, f"mail.message:{mid}")
                for mid, kind, old, new in pending
                if kind != "stage" or (old in visible_ids and new in visible_ids)
            )
            return status, History(transitions, False, truncated)
        except (AccessError, MissingError):
            return ReadStatus.FAILED, None
        except Exception:  # noqa: BLE001
            return ReadStatus.FAILED, None
