"""Execute one bounded, server-scoped plan over the existing task readers."""

import json
import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from math import isfinite
from time import monotonic

from .audit import LOGGER_NAME
from .task_read_map import ReadBranch, ReadTool, TaskReadingMap, TaskReadMap
from .task_read_tools import (
    COMMENTS_LIMIT,
    Comments,
    Description,
    History,
    ReadStatus,
    TaskCard,
    TaskReadTools,
    Worklog,
    validate_comment_limit,
    validate_history_period,
)

Projection = TaskCard | Description | History | Comments | Worklog
_SOURCE_ROUTES: dict[ReadTool, tuple[str, str]] = {
    ReadTool.CARD: (r"/odoo/project\.task/", "/odoo/project.task/"),
    ReadTool.DESCRIPTION: (r"/odoo/project\.task/", "/odoo/project.task/"),
    ReadTool.HISTORY: (r"mail\.message:", "/mail/message/"),
    ReadTool.COMMENTS: (r"/mail/message/", "/mail/message/"),
    ReadTool.WORKLOG: (r"/odoo/account\.analytic\.line/", "/odoo/account.analytic.line/"),
}


@dataclass(frozen=True, slots=True)
class ReadBudget:
    """Set server limits for one run; e.g. ReadBudget(max_calls=3)."""

    seconds: float = 5.0
    max_calls: int = 8
    max_context_bytes: int = 128 * 1024

    def __post_init__(self) -> None:
        """Reject nonfinite or nonpositive limits; e.g. ReadBudget(seconds=0)."""
        if (type(self.seconds) not in (int, float) or not isfinite(self.seconds) or self.seconds <= 0
                or type(self.max_calls) is not int or self.max_calls < 1
                or type(self.max_context_bytes) is not int or self.max_context_bytes < 2):
            raise ValueError("Invalid read budget")


@dataclass(frozen=True, slots=True)
class ReadStep:
    """Describe untrusted plan arguments; e.g. ReadStep(ReadTool.CARD)."""

    tool: ReadTool
    start: date | None = None
    end: date | None = None
    limit: int | None = None

    def checked(self, branch: ReadBranch | None = None) -> "ReadStep":
        """Validate and canonicalize arguments, optionally against a server branch."""
        if type(self.tool) is not ReadTool or (branch is not None and self.tool is not branch.tool):
            raise ValueError("Invalid reader")
        if self.tool in (ReadTool.CARD, ReadTool.DESCRIPTION):
            if self.start is not None or self.end is not None or self.limit is not None:
                raise ValueError("Unexpected reader arguments")
            return self

        start, end = self.start, self.end
        if type(start) is not date or type(end) is not date:
            raise ValueError("Calendar dates required")
        validate_history_period(start, end)
        if branch is not None and (branch.max_period_days is None
                                   or (end - start).days + 1 > branch.max_period_days):
            raise ValueError("Period outside branch")
        if self.tool is ReadTool.COMMENTS:
            limit: int = COMMENTS_LIMIT if self.limit is None else self.limit
            validate_comment_limit(limit)
            if branch is not None and (branch.max_fragments is None or limit > branch.max_fragments):
                raise ValueError("Limit outside branch")
            return replace(self, limit=limit)
        if self.limit is not None:
            raise ValueError("Unexpected reader arguments")
        return self

    def read(self, tools: TaskReadTools) -> tuple[ReadStatus, Projection | None]:
        """Dispatch a checked step through a fixed allowlist, not a model attribute."""
        if self.tool is ReadTool.CARD:
            return tools.read_task_card()
        if self.tool is ReadTool.DESCRIPTION:
            return tools.read_task_description()
        if self.start is None or self.end is None:
            raise ValueError("Calendar dates required")
        if self.tool is ReadTool.HISTORY:
            return tools.read_task_stage_history(self.start, self.end)
        if self.tool is ReadTool.COMMENTS and self.limit is not None:
            return tools.read_task_comments(self.start, self.end, self.limit)
        if self.tool is ReadTool.WORKLOG:
            return tools.read_task_worklog(self.start, self.end)
        raise ValueError("Invalid reader")


def _json_value(value: object) -> str:
    """Encode a reader's temporal or decimal projection value without losing precision."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal) and value.is_finite():
        return str(value)
    raise TypeError("Unsupported projection value")


@dataclass(frozen=True, slots=True)
class ReadObservation:
    """Snapshot one read with replay arguments and server-derived citation routes."""

    step: ReadStep
    encoded: bytes
    citations: tuple[tuple[str, str], ...]

    @classmethod
    def capture(cls, step: ReadStep, value: Projection | None) -> "ReadObservation | None":
        """Capture admitted evidence, or None for an incomplete empty selection."""
        sources: tuple[str, ...]
        match step.tool, value:
            case (ReadTool.CARD, TaskCard()) | (ReadTool.DESCRIPTION, Description()):
                sources = (value.source,)
            case ReadTool.HISTORY, History():
                if not value.transitions and not value.complete:
                    return None
                sources = tuple(item.source for item in value.transitions)
            case ReadTool.COMMENTS, Comments():
                if not value.fragments and not value.complete:
                    return None
                sources = tuple(item.source for item in value.fragments)
            case ReadTool.WORKLOG, Worklog():
                if not value.entries and not value.complete:
                    return None
                sources = tuple(source for item in value.entries for source in item.sources)
            case _:
                raise ValueError("Invalid reader projection")

        pattern, route = _SOURCE_ROUTES[step.tool]
        citations: dict[str, str] = {}
        for source in sources:
            match_id: re.Match[str] | None = (
                re.fullmatch(pattern + r"([1-9][0-9]*)", source) if type(source) is str else None
            )
            if match_id is None:
                raise ValueError("Invalid source locator")
            citations[source] = route + match_id[1]

        encoded: bytes = json.dumps(
            {"step": asdict(step), "data": asdict(value)}, default=_json_value,
            ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")
        return cls(step, encoded, tuple(citations.items()))


@dataclass(frozen=True, slots=True)
class ReadingContext:
    """Carry immutable provenance for the exact JSON bytes given to the model."""

    observations: tuple[ReadObservation, ...] = ()

    @property
    def model_json(self) -> bytes:
        """Return the budgeted payload, not routing metadata or gap diagnostics."""
        return b"[" + b",".join(item.encoded for item in self.observations) + b"]"


class ReadRunStatus(StrEnum):
    """Report plan disposition without implying fact or collection completeness."""

    READY = "ready"
    FINISHED = "finished"
    SUFFICIENT = "sufficient"
    INVALID_PLAN = "invalid_plan"
    NO_NEW_DATA = "no_new_data"
    CALL_LIMIT = "call_limit"
    TIME_LIMIT = "time_limit"
    VOLUME_LIMIT = "volume_limit"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class StepStatus(StrEnum):
    """Distinguish accepted evidence, missing evidence and skipped repeats."""

    ADMITTED = "admitted"
    NO_NEW_DATA = "no_new_data"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class PlanResult:
    """Return admitted context and non-disclosing diagnostics to the caller."""

    status: ReadRunStatus
    context: ReadingContext | None
    attempts: int = 0
    steps: tuple[StepStatus, ...] = ()


class TaskReadRun:
    """Prepare one map and execute one plan for one server-selected task.

    Bind the map, tools and initial projections to the same recipient and scope.
    Initial projections must come from these same reader contracts. Pass only
    context.model_json to the model; keep that context for the release gate.
    The deadline covers preparation, plan submission and all reading. A late
    synchronous call cannot be interrupted here, but its result is never admitted.
    """

    def __init__(
        self, reading_map: TaskReadMap, tools: TaskReadTools, *, budget: ReadBudget = ReadBudget(),
        initial: tuple[tuple[ReadStep, Projection], ...] = (), clock: Callable[[], float] = monotonic,
    ) -> None:
        """Bind server dependencies and admit initial evidence atomically; e.g. TaskReadRun(map, tools)."""
        self._reading_map: TaskReadMap = reading_map
        self._tools: TaskReadTools = tools
        self._budget: ReadBudget = budget
        self._clock: Callable[[], float] = clock
        self._deadline: float = clock() + budget.seconds
        self._context: ReadingContext | None = None
        self._map_result: tuple[ReadRunStatus, TaskReadingMap | None] | None = None
        self._executed: bool = False
        self._initial_status: ReadRunStatus = ReadRunStatus.READY
        try:
            # Initial facts do not spend calls, but must not create an unbounded replay batch.
            if len(initial) > budget.max_calls:
                self._initial_status = ReadRunStatus.VOLUME_LIMIT
                return
            context: ReadingContext = ReadingContext()
            seen: set[ReadStep] = set()
            for step, value in initial:
                checked: ReadStep = step.checked()
                if checked in seen:
                    raise ValueError("Duplicate initial observation")
                seen.add(checked)
                item: ReadObservation | None = ReadObservation.capture(checked, value)
                if item is not None:
                    context = ReadingContext((*context.observations, item))
                if len(context.model_json) > budget.max_context_bytes:
                    self._initial_status = ReadRunStatus.VOLUME_LIMIT
                    return
            if self._expired():
                self._initial_status = ReadRunStatus.TIME_LIMIT
                return
            self._context = context
            self._seen: set[ReadStep] = seen
        except (ValueError, TypeError, AttributeError):
            self._initial_status = ReadRunStatus.FAILED

    @property
    def context(self) -> ReadingContext | None:
        """Expose the current admitted context, including before a map is needed."""
        return self._context

    def _expired(self) -> bool:
        """Check the shared monotonic deadline before and after external work."""
        return self._clock() >= self._deadline

    def prepare(self) -> tuple[ReadRunStatus, TaskReadingMap | None]:
        """Build the map at most once; e.g. status, branches = run.prepare()."""
        if self._initial_status is not ReadRunStatus.READY:
            return self._initial_status, None
        if self._expired():
            return ReadRunStatus.TIME_LIMIT, None
        if self._map_result is not None:
            return self._map_result
        self._map_result = ReadRunStatus.FAILED, None
        try:
            status, reading_map = self._reading_map.build()
            if self._expired():
                self._map_result = ReadRunStatus.TIME_LIMIT, None
            elif status is ReadStatus.AVAILABLE and isinstance(reading_map, TaskReadingMap):
                self._map_result = ReadRunStatus.READY, reading_map
            elif status is ReadStatus.UNAVAILABLE:
                self._map_result = ReadRunStatus.UNAVAILABLE, None
        except Exception:  # noqa: BLE001
            pass  # Keep the sanitized failure, not the reader's exception.
        return self._map_result

    def execute(
        self, plan: tuple[ReadStep, ...], *, sufficient: Callable[[ReadingContext], bool] | None = None,
    ) -> PlanResult:
        """Validate a typed plan and run its affordable prefix in order; e.g. run.execute((step,))."""
        result: PlanResult = self._execute(plan, sufficient)
        logging.getLogger(LOGGER_NAME).info("task_read_plan status=%s attempts=%d", result.status, result.attempts)
        return result

    def _execute(
        self, plan: tuple[ReadStep, ...], sufficient: Callable[[ReadingContext], bool] | None,
    ) -> PlanResult:
        """Execute one plan without logging the payload or diagnostics from readers."""
        if self._executed:
            return PlanResult(ReadRunStatus.INVALID_PLAN, self._context)
        self._executed = True
        if self._context is None:
            return PlanResult(self._initial_status, None)
        if self._expired():
            return PlanResult(ReadRunStatus.TIME_LIMIT, self._context)
        if type(plan) is tuple and not plan:
            return PlanResult(ReadRunStatus.NO_NEW_DATA, self._context)
        # Separate the bounded proposal size from the number of reads that may run.
        if type(plan) is not tuple or len(plan) > 2 * self._budget.max_calls:
            return PlanResult(ReadRunStatus.INVALID_PLAN, self._context)
        status, reading_map = self.prepare()
        if reading_map is None:
            return PlanResult(status, self._context)
        branches: dict[ReadTool, ReadBranch] = {branch.tool: branch for branch in reading_map.branches}
        checked: list[ReadStep] = []
        try:
            for step in plan:
                if type(step) is not ReadStep or type(step.tool) is not ReadTool or step.tool not in branches:
                    raise ValueError("Reader not on the map")
                checked.append(step.checked(branches[step.tool]))
        except (TypeError, ValueError):
            return PlanResult(ReadRunStatus.INVALID_PLAN, self._context)

        attempts: int = 0
        reports: list[StepStatus] = []
        status = ReadRunStatus.NO_NEW_DATA
        for step in checked:
            try:
                if self._expired():
                    status = ReadRunStatus.TIME_LIMIT
                    break
                if sufficient is not None:
                    enough: bool = sufficient(self._context)
                    if self._expired():
                        status = ReadRunStatus.TIME_LIMIT
                        break
                    if enough is True:
                        status = ReadRunStatus.SUFFICIENT
                        break
                if step in self._seen:
                    reports.append(StepStatus.DUPLICATE)
                    continue
                if attempts >= self._budget.max_calls:
                    status = ReadRunStatus.CALL_LIMIT
                    break
                if len(self._context.model_json) >= self._budget.max_context_bytes:
                    status = ReadRunStatus.VOLUME_LIMIT
                    break
                if self._expired():
                    status = ReadRunStatus.TIME_LIMIT
                    break

                attempts += 1
                self._seen.add(step)
                read_status, value = step.read(self._tools)
                if self._expired():
                    status = ReadRunStatus.TIME_LIMIT
                    break
                if read_status is not ReadStatus.AVAILABLE:
                    status = (ReadRunStatus.UNAVAILABLE if read_status is ReadStatus.UNAVAILABLE
                              else ReadRunStatus.FAILED)
                    break
                evidence: ReadObservation | None = ReadObservation.capture(step, value)
                if self._expired():
                    status = ReadRunStatus.TIME_LIMIT
                    break
                if evidence is None:
                    reports.append(StepStatus.NO_NEW_DATA)
                    continue
                candidate: ReadingContext = ReadingContext((*self._context.observations, evidence))
                fits: bool = len(candidate.model_json) <= self._budget.max_context_bytes
                if self._expired():
                    status = ReadRunStatus.TIME_LIMIT
                    break
                if not fits:
                    status = ReadRunStatus.VOLUME_LIMIT
                    break
                self._context = candidate
                reports.append(StepStatus.ADMITTED)
                status = ReadRunStatus.FINISHED
            except Exception:  # noqa: BLE001
                status = ReadRunStatus.FAILED
                break
        return PlanResult(status, self._context, attempts, tuple(reports))
