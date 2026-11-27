"""Read scoped task-tool data with the recipient's Odoo environment."""

from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING

from .odoo_task_reader import _read_task_projection

if __package__ and "." in __package__:
    from ..application.task_read_tools import (  # noqa: TID252
        History,
        ReadStatus,
        TaskAssignee,
        TaskCard,
        Transition,
        validate_history_period,
    )
else:
    from application.task_read_tools import (
        History,
        ReadStatus,
        TaskAssignee,
        TaskCard,
        Transition,
        validate_history_period,
    )

if TYPE_CHECKING:
    from odoo.api import Environment

_FIELDS: frozenset[str] = frozenset({"id", "name", "state", "date_deadline", "user_ids", "description"})


def _utc_midnight(day: date, zone: tzinfo) -> datetime:
    """Convert a project-local midnight to naive UTC for an Odoo domain.

    Accept stdlib zones and Odoo's pytz zones. Ambiguous or missing midnight
    in a pytz zone raises rather than guessing. Example: _utc_midnight(day, UTC).
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
        deadline: datetime | None = None
        deadline_known: bool = "date_deadline" in row and raw_deadline is False
        if isinstance(raw_deadline, datetime):
            deadline = raw_deadline.replace(tzinfo=UTC) if raw_deadline.tzinfo is None else raw_deadline
            deadline_known = True
        elif isinstance(raw_deadline, str):
            try:
                if "T" in raw_deadline or " " in raw_deadline:
                    deadline = datetime.fromisoformat(raw_deadline)
                    if deadline.tzinfo is None:
                        deadline = deadline.replace(tzinfo=UTC)
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
                    users = self._env["res.users"]
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
        return f"/odoo/tasks/{self._task_id}"

    def history(self, start: date, end: date) -> tuple[ReadStatus, History | None]:
        """Read only visible structured tracking for the selected task and period.

        Example: source.history(date(2026, 1, 1), date(2026, 1, 2)).
        Coverage is always unconfirmed: a readable log cannot prove no gaps.
        """
        from odoo.exceptions import AccessError, MissingError  # noqa: PLC0415

        validate_history_period(start, end)
        status, row = self._read(())
        if row is None:
            return status, None
        if "mail.tracking.value" not in self._env.registry.models:
            return status, History((), False)
        try:
            start_utc: datetime = _utc_midnight(start, self._timezone)
            end_utc: datetime = _utc_midnight(end + timedelta(days=1), self._timezone)
            messages = self._env["mail.message"]
            message_fields: tuple[str, ...] = ("id", "date")
            if not set(message_fields).issubset(messages.fields_get(allfields=message_fields, attributes=("type",))):
                return status, History((), False)
            rows: list[dict[str, object]] = messages.search_read(
                [("model", "=", "project.task"), ("res_id", "=", self._task_id),
                 ("date", ">=", start_utc), ("date", "<", end_utc)],
                fields=list(message_fields), order="date, id", load=None,
            )
            days: dict[int, date] = {}
            for message in rows:
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
            if not days:
                return status, History((), False)
            tracking = self._env["mail.tracking.value"]
            task_model = self._env["project.task"]
            tracked_fields: tuple[str, ...] = ("stage_id", "state")
            readable_fields: dict[str, object] = task_model.fields_get(allfields=tracked_fields, attributes=("type",))
            fields: tuple[str, ...] = (
                "mail_message_id", "old_value_integer", "new_value_integer", "old_value_char", "new_value_char",
            )
            if not set(fields).issubset(tracking.fields_get(allfields=fields, attributes=("type",))):
                return status, History((), False)
            transitions: list[Transition] = []
            for field, kind in (("stage_id", "stage"), ("state", "state")):
                if field not in readable_fields:
                    continue
                values: list[dict[str, object]] = tracking.search_read(
                    [("mail_message_id", "in", list(days)), ("field_id.name", "=", field),
                     ("field_id.model", "=", "project.task")], fields=list(fields), order="id", load=None,
                )
                for value in values:
                    mid: object = value.get("mail_message_id")
                    if type(mid) is not int or mid not in days:
                        continue
                    old: object = value.get("old_value_integer" if field == "stage_id" else "old_value_char")
                    new: object = value.get("new_value_integer" if field == "stage_id" else "new_value_char")
                    expected: type = int if field == "stage_id" else str
                    if type(old) is not expected or type(new) is not expected or old == new:
                        continue
                    if field == "stage_id":
                        stage_ids: list[int] = [stage for stage in (old, new) if isinstance(stage, int) and stage > 0]
                        try:
                            visible_stages: list[dict[str, object]] = self._env["project.task.type"].search_read(
                                [("id", "in", stage_ids)], fields=["id"], order="id", load=None,
                            )
                        except (AccessError, MissingError):
                            continue
                        if {item.get("id") for item in visible_stages} != set(stage_ids):
                            continue
                    transitions.append(Transition(days[mid], kind, old, new, f"mail.message:{mid}"))
            transitions.sort(key=lambda item: (item.day, item.source, item.kind))
            return status, History(tuple(transitions), False)
        except (AccessError, MissingError, KeyError):
            return status, History((), False)
        except Exception:  # noqa: BLE001
            return ReadStatus.FAILED, None
