"""Decide and persist one morning risk check per project and local date."""

from collections.abc import Callable
from datetime import datetime, time
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .background_operations import BackgroundStore
from .operation_store import Operation


class ScheduleRefused(Exception):
    """Report a fixed configuration refusal code, e.g. ScheduleRefused('missing_timezone')."""


def schedule_morning_check(
    project_id: int, timezone_name: str | None, recipient_id: int | None, *, now: datetime,
    authorize: Callable[[int], tuple[int, ...] | None], open_store: Callable[[], BackgroundStore],
) -> Operation | None:
    """Accept the local 09:00 slot or return None outside its five-minute window.

    Supply an aware instant, an explicit IANA zone and recipient, and an authorization
    callback returning the recipient's allowed company scope. Invalid configuration
    raises ScheduleRefused. For example, 04:00 UTC in Asia/Yekaterinburg reserves
    the project's local date; a repeated tick reuses that slot's operation ID.
    """
    if now.utcoffset() is None:
        raise ValueError("The schedule clock must be timezone-aware.")
    if not timezone_name or not timezone_name.strip():
        raise ScheduleRefused("missing_timezone")
    if timezone_name in {"localtime", "posixrules"} or timezone_name.startswith(("posix/", "right/")):
        raise ScheduleRefused("invalid_timezone")
    try:
        zone: ZoneInfo = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError):
        raise ScheduleRefused("invalid_timezone") from None
    local: datetime = now.astimezone(zone)
    if not time(9) <= local.time() < time(9, 5):
        return None
    if type(recipient_id) is not int or recipient_id <= 0:
        raise ScheduleRefused("missing_recipient")
    companies: tuple[int, ...] | None = authorize(recipient_id)
    if companies is None:
        raise ScheduleRefused("recipient_unavailable")
    # The local date, not the recipient or current offset, identifies a project's slot.
    key: str = str(uuid5(NAMESPACE_URL, f"urn:taskmaster:morning:{project_id}:{local.date().isoformat()}"))
    return open_store().accept_background(
        key, owner_id=recipient_id, project_id=project_id, recipient_id=recipient_id,
        now=now, company_ids=companies, project_timezone=zone.key,
    )
