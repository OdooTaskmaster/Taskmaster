"""Describe scenario-scoped reading without enumerating source records."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .task_read_tools import (
    COMMENT_TEXT_LIMIT,
    COMMENTS_LIMIT,
    DESCRIPTION_LIMIT,
    HISTORY_MAX_DAYS,
    HISTORY_MESSAGE_LIMIT,
    HISTORY_TRACKING_LIMIT,
    WORKLOG_LIMIT,
    ReadStatus,
)


class ReadScenario(StrEnum):
    """Identify a server-selected scenario; e.g. ReadScenario.RISK_ANALYSIS."""

    RISK_ANALYSIS = "risk_analysis"
    DRAFT = "draft"


class ReadTool(StrEnum):
    """Restrict branches to implemented readers; e.g. ReadTool.CARD."""

    CARD = "read_task_card"
    DESCRIPTION = "read_task_description"
    HISTORY = "read_task_stage_history"
    COMMENTS = "read_task_comments"
    WORKLOG = "read_task_worklog"


@dataclass(frozen=True, slots=True)
class ReadBranch:
    """Describe one reader's bounds, not available rows; e.g. ReadBranch(ReadTool.CARD)."""

    tool: ReadTool
    parameters: tuple[str, ...] = ()
    max_period_days: int | None = None
    max_fragments: int | None = None
    max_text_chars: int | None = None
    max_rows: int | None = None
    max_tracking_values_per_field: int | None = None


@dataclass(frozen=True, slots=True)
class TaskReadingMap:
    """Describe the selected project's selected task without IDs or text.

    Example: TaskReadingMap(ReadScenario.DRAFT, (ReadBranch(ReadTool.CARD),)).
    The structural path is always selected project -> selected task -> branches.
    No branch proves that matching records exist or authorizes subsequent reading.
    """

    scenario: ReadScenario
    branches: tuple[ReadBranch, ...]


class CapabilitySource(Protocol):
    """Probe current reading capabilities; e.g. source.read_types()."""

    def read_types(self) -> tuple[ReadStatus, tuple[ReadTool, ...] | None]:
        """Return permitted types without enumerating records; e.g. (AVAILABLE, ())."""
        ...


_BRANCHES: tuple[ReadBranch, ...] = (
    ReadBranch(ReadTool.CARD),
    ReadBranch(ReadTool.DESCRIPTION, max_text_chars=DESCRIPTION_LIMIT),
    ReadBranch(ReadTool.HISTORY, ("start", "end"), max_period_days=HISTORY_MAX_DAYS,
               max_rows=HISTORY_MESSAGE_LIMIT, max_tracking_values_per_field=HISTORY_TRACKING_LIMIT),
    ReadBranch(ReadTool.COMMENTS, ("start", "end", "limit"), HISTORY_MAX_DAYS, COMMENTS_LIMIT, COMMENT_TEXT_LIMIT),
    ReadBranch(ReadTool.WORKLOG, ("start", "end"), max_period_days=HISTORY_MAX_DAYS, max_rows=WORKLOG_LIMIT),
)


class TaskReadMap:
    """Bind a map to server scope, scenario and a narrowing policy.

    Both approved scenarios may use all five types; the server can narrow them.
    Example: TaskReadMap(source, scenario=ReadScenario.RISK_ANALYSIS).build().
    Draft maps describe current-user reading, not publication audience clearance.
    """

    def __init__(
        self, source: CapabilitySource, *, scenario: ReadScenario,
        allowed_tools: frozenset[ReadTool] = frozenset(ReadTool),
    ) -> None:
        if not isinstance(scenario, ReadScenario):
            message: str = "Server-selected read scenario required"
            raise TypeError(message)
        if not isinstance(allowed_tools, frozenset) or any(not isinstance(tool, ReadTool) for tool in allowed_tools):
            message = "Fixed server read policy required"
            raise TypeError(message)
        self._source: CapabilitySource = source
        self._scenario: ReadScenario = scenario
        self._allowed: frozenset[ReadTool] = allowed_tools

    def build(self) -> tuple[ReadStatus, TaskReadingMap | None]:
        """Rebuild permitted branches without model arguments; e.g. reader.build().

        Reject malformed source output rather than widening the policy. Source
        failures return no partial map and no internal diagnostic information.
        """
        status: ReadStatus
        tools: tuple[ReadTool, ...] | None
        status, tools = self._source.read_types()
        if status != ReadStatus.AVAILABLE:
            return status, None
        if (not isinstance(tools, tuple) or any(not isinstance(tool, ReadTool) for tool in tools)
                or len(set(tools)) != len(tools)):
            return ReadStatus.FAILED, None
        branches: tuple[ReadBranch, ...] = tuple(
            branch for branch in _BRANCHES if branch.tool in tools and branch.tool in self._allowed
        )
        return status, TaskReadingMap(self._scenario, branches)
