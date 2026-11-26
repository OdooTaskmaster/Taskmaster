"""Convert explicit instants to dates on the project calendar."""

from datetime import date, datetime, tzinfo


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
