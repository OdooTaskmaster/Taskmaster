"""Convert explicit instants to dates on the project calendar."""

from datetime import UTC, date, datetime, tzinfo
from zoneinfo import ZoneInfo

DEFAULT_PROJECT_TIMEZONE: str = "Europe/Moscow"


def project_zone(name: str) -> ZoneInfo:
    """Load an explicit IANA zone; project_zone('Asia/Tokyo') uses that calendar.

    Reject host-local and alternate system aliases rather than inferring a zone.
    """
    if name in {"localtime", "posixrules"} or name.startswith(("posix/", "right/")):
        raise ValueError("An IANA time zone is required.")
    return ZoneInfo(name)


def project_day(instant: datetime, project_timezone: tzinfo) -> date:
    """Return the project date of an offset-aware instant in the given zone.

    Example: project_day(datetime(2026, 11, 25, tzinfo=UTC), UTC)
    returns date(2026, 11, 25).
    """
    if not isinstance(project_timezone, tzinfo):
        raise TypeError("A project time zone is required.")
    if instant.utcoffset() is None:
        raise ValueError("The check time must include a UTC offset.")
    return instant.astimezone(project_timezone).date()


def deadline_timing(
    deadline: date | datetime, checked_at: datetime, project_timezone: tzinfo, checked_day: date,
) -> tuple[bool, int]:
    """Return whether a deadline expired and its signed calendar-day offset.

    Supply a date-only or aware exact deadline, an aware check instant, the
    project zone and its local check date. Example: deadline_timing(date(2026,
    11, 26), datetime(2026, 11, 25, tzinfo=UTC), UTC, date(2026, 11, 25))
    returns (False, 1).
    """
    if isinstance(deadline, datetime):
        if deadline.utcoffset() is None:
            raise ValueError("An exact deadline must include a UTC offset.")
        # Compare instants, not wall times that may repeat during a clock change.
        expired: bool = checked_at.astimezone(UTC) > deadline.astimezone(UTC)
        return expired, (deadline.astimezone(project_timezone).date() - checked_day).days
    return checked_day > deadline, (deadline - checked_day).days
