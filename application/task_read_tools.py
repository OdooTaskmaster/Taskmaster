"""Define scoped task read contracts independent of the Odoo runtime."""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from html.parser import HTMLParser
from typing import Protocol

DESCRIPTION_LIMIT: int = 4000
DESCRIPTION_INPUT_LIMIT: int = 262144
HISTORY_MAX_DAYS: int = 90


class ReadStatus(StrEnum):
    """Distinguish available, unavailable and failed reads.

    Example: ReadStatus("unavailable") hides whether a task exists.
    """

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TaskAssignee:
    """Identify a visible assignee and an optional readable display name.

    Example: TaskAssignee(7, "Demo user").
    """

    user_id: int
    name: str | None


@dataclass(frozen=True, slots=True)
class TaskCard:
    """Carry authorized card fields; None means the field was not readable.

    The deadline may itself be None when confirmed empty. Assignees are references;
    an empty tuple is confirmed empty and None is unknown. Completeness is
    explicit, since a non-empty list can also be filtered. Example:
    TaskCard("Example", "in_progress", None, True, (), "/odoo/tasks/9").
    """

    name: str | None
    state: str | None
    deadline: date | datetime | None
    deadline_known: bool
    assignees: tuple[TaskAssignee, ...] | None
    source: str
    assignees_complete: bool = False


@dataclass(frozen=True, slots=True)
class Description:
    """Carry a plain-text description, source and truncation marker.

    Example: Description("text", "/odoo/tasks/9", False).
    """

    text: str
    source: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class Transition:
    """Carry an authorized observed transition and its source.

    State values are localized tracking labels, not TaskCard.state keys.
    Stage values are positive IDs. Ordering uses original event instants;
    changes from the same message are simultaneous, with a deterministic tie.
    Example: Transition(date(2026, 1, 1), "stage", 1, 2, "ref").
    """

    day: date
    kind: str
    old: str | int | None
    new: str | int | None
    source: str


@dataclass(frozen=True, slots=True)
class History:
    """Carry observed transitions and whether period coverage is confirmed.

    Example: History((), False) represents an incomplete empty observation.
    """

    transitions: tuple[Transition, ...]
    complete: bool
    truncated: bool = False


class ScopedTaskSource(Protocol):
    """Read one server-selected task under the current recipient's rights.

    Implementations must recheck authorization on every call. Example:
    tools = TaskReadTools(source), with source supplied by the server.
    """

    def card(self) -> tuple[ReadStatus, TaskCard | None]:
        """Read authorized fields or return a non-disclosing status."""
        ...

    def description(self) -> tuple[ReadStatus, str | None, str | None]:
        """Read authorized HTML and server-generated source."""
        ...

    def history(self, start: date, end: date) -> tuple[ReadStatus, History | None]:
        """Read authorized transitions for inclusive project-calendar dates."""
        ...


class _PlainText(HTMLParser):
    """Extract text while dropping executable or embedded HTML content.

    Example: parser = _PlainText(); parser.feed('<b>ok</b>').
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored: int = 0

    _BLOCKS: frozenset[str] = frozenset({
        "p", "div", "br", "li", "tr", "td", "th", "table", "ul", "ol",
        "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
    })

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "iframe", "object", "template"}:
            self._ignored += 1
        elif not self._ignored and tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self._ignored and tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "iframe", "object", "template"} and self._ignored:
            self._ignored -= 1
        elif not self._ignored and tag in self._BLOCKS - {"br"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored:
            self.parts.append(data)


class TaskReadTools:
    """Expose narrow tools bound to a server-selected source.

    The model cannot supply task, project, recipient or description limit.
    Example: TaskReadTools(server_source).read_task_card().
    """

    def __init__(self, source: ScopedTaskSource) -> None:
        self._source: ScopedTaskSource = source

    def read_task_card(self) -> tuple[ReadStatus, TaskCard | None]:
        """Return the scoped card, or a non-disclosing unavailable status."""
        status, card = self._source.card()
        if status != ReadStatus.AVAILABLE:
            return status, None
        return (status, card) if isinstance(card, TaskCard) else (ReadStatus.FAILED, None)

    def read_task_description(self) -> tuple[ReadStatus, Description | None]:
        """Return plain text limited by the server, without executable markup."""
        status, raw, source = self._source.description()
        if status != ReadStatus.AVAILABLE:
            return status, None
        if not isinstance(raw, str) or not isinstance(source, str) or not source:
            return ReadStatus.FAILED, None
        parser: _PlainText = _PlainText()
        parser.feed(raw[:DESCRIPTION_INPUT_LIMIT])
        parser.close()
        lines: list[str] = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
        text: str = "\n".join(line for line in lines if line)
        truncated: bool = len(raw) > DESCRIPTION_INPUT_LIMIT or len(text) > DESCRIPTION_LIMIT
        return status, Description(text[:DESCRIPTION_LIMIT], source, truncated)

    def read_task_stage_history(self, start: date, end: date) -> tuple[ReadStatus, History | None]:
        """Read an inclusive period of at most 90 project-calendar days.

        Example: read_task_stage_history(date(2026, 1, 1), date(2026, 1, 2)).
        Invalid ranges raise ValueError before accessing the source.
        """
        validate_history_period(start, end)
        status, history = self._source.history(start, end)
        if status != ReadStatus.AVAILABLE:
            return status, None
        return (status, history) if isinstance(history, History) else (ReadStatus.FAILED, None)


def validate_history_period(start: date, end: date) -> None:
    """Reject invalid or excessive inclusive calendar periods before any read.

    Example: validate_history_period(date(2026, 1, 1), date(2026, 1, 2)).
    """
    if type(start) is not date or type(end) is not date:
        message: str = "Calendar dates required"
        raise ValueError(message)
    if start == date.min or end == date.max or end < start:
        message: str = "Invalid date range"
        raise ValueError(message)
    if (end - start).days + 1 > HISTORY_MAX_DAYS:
        message = "Date range is too wide"
        raise ValueError(message)
