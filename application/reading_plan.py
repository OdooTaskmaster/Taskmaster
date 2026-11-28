"""Execute a typed, scoped reading plan within one non-resumable run budget."""

import json
import math
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from time import monotonic

from .task_read_map import ReadTool, TaskReadMap
from .task_read_tools import (
    ReadStatus,
    TaskReadTools,
    validate_comment_limit,
    validate_history_period,
)


@dataclass(frozen=True, slots=True)
class ReadBudget:
    """Set server limits for one run; e.g. ReadBudget() reserves 10 of 30 seconds."""

    max_calls: int = 6
    max_bytes: int = 32768
    total_seconds: float = 30.0
    reserve_seconds: float = 10.0

    def __post_init__(self) -> None:
        """Reject infinite, nonpositive or contradictory limits; e.g. ReadBudget(max_calls=0) raises."""
        if any(type(value) is not int or value <= 0 for value in (self.max_calls, self.max_bytes)):
            message: str = "Positive integer budgets required"
            raise ValueError(message)
        if self.max_bytes < 2:
            message = "Context budget must fit the empty JSON array"
            raise ValueError(message)
        if (any(type(value) not in (int, float) or not math.isfinite(value)
                for value in (self.total_seconds, self.reserve_seconds))
                or not 0 < self.reserve_seconds < self.total_seconds):
            message = "Finite time budget and a smaller positive reserve required"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class ReadStep:
    """Request one fixed reader, not scope or ORM arguments; e.g. ReadStep(ReadTool.CARD)."""

    tool: ReadTool
    start: date | None = None
    end: date | None = None
    limit: int | None = None


@dataclass(frozen=True, slots=True)
class ReadPlan:
    """Carry an ordered translated model plan; e.g. ReadPlan((ReadStep(ReadTool.CARD),))."""

    steps: tuple[ReadStep, ...]


@dataclass(frozen=True, slots=True, order=True)
class SourceRef:
    """Identify server-observed evidence; e.g. SourceRef('mail.message', 5)."""

    model: str
    record_id: int

    def __post_init__(self) -> None:
        """Restrict references to supported routes; e.g. SourceRef('res.users', 1) raises."""
        if (self.model not in {"project.task", "mail.message", "account.analytic.line"}
                or type(self.record_id) is not int or self.record_id <= 0):
            message: str = "A supported positive source reference required"
            raise ValueError(message)

    def link(self) -> str:
        """Build a canonical link without model text; e.g. SourceRef('mail.message', 5).link()."""
        return (f"/mail/message/{self.record_id}" if self.model == "mail.message"
                else f"/odoo/{self.model}/{self.record_id}")


def _reference(value: str) -> SourceRef:
    """Parse only fixed reader references; e.g. _reference('mail.message:5')."""
    match: re.Match[str] | None = re.fullmatch(
        r"(?:/odoo/(project\.task|account\.analytic\.line)/|/mail/(message)/|(mail\.message):)([1-9][0-9]*)", value,
    )
    if match is None:
        message: str = "Unsupported source reference"
        raise ValueError(message)
    return SourceRef(match[1] or "mail.message", int(match[4]))


def _json_value(value: object, sources: set[SourceRef], *, field: str = "") -> object:
    """Encode current reader types, rejecting floats; e.g. _json_value(card, refs).

    Work amounts use finite Decimal, not floats. Adding a new reader field type
    requires an explicit encoder contract rather than silently stringifying it.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value), sources)
    if type(value) is dict:
        return {key: _json_value(item, sources, field=key) for key, item in value.items()}
    if type(value) in (tuple, list):
        return [_json_value(item, sources, field=field) for item in value]
    if type(value) is str:
        if field in {"source", "sources"}:
            reference: SourceRef = _reference(value)
            sources.add(reference)
            return reference.link()
        return value
    if type(value) in (date, datetime):
        return value.isoformat()
    if type(value) is Decimal and value.is_finite():
        return str(value)
    if value is None or type(value) in (int, bool):
        return value
    message: str = "Unsupported reader data"
    raise ValueError(message)


def encode(value: object) -> str:
    """Produce canonical compact UTF-8 JSON text; e.g. encode({'x': 1})."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True, slots=True)
class ContextItem:
    """Retain a trusted exact read receipt, not model claims; e.g. ContextItem(step, json, refs)."""

    step: ReadStep
    data_json: str
    sources: tuple[SourceRef, ...]


def read_item(tools: TaskReadTools, step: ReadStep) -> tuple[ReadStatus, ContextItem | None]:
    """Execute one validated fixed reader and encode its receipt; e.g. read_item(tools, step)."""
    if step.tool in (ReadTool.CARD, ReadTool.DESCRIPTION):
        status, data = getattr(tools, step.tool.value)()
    elif step.tool is ReadTool.COMMENTS:
        status, data = tools.read_task_comments(step.start, step.end, step.limit)
    else:
        status, data = getattr(tools, step.tool.value)(step.start, step.end)
    if type(status) is not ReadStatus or status is not ReadStatus.AVAILABLE:
        return status if type(status) is ReadStatus else ReadStatus.FAILED, None
    if data is None:
        return ReadStatus.FAILED, None
    sources: set[SourceRef] = set()
    data_json: str = encode(_json_value(data, sources))
    return status, ContextItem(step, data_json, tuple(sorted(sources)))


def context_json(items: tuple[ContextItem, ...]) -> str:
    """Serialize exactly the model-visible data envelope; e.g. context_json(()) == '[]'."""
    return encode([{
        "tool": item.step.tool.value,
        "arguments": {"start": item.step.start.isoformat() if item.step.start else None,
                      "end": item.step.end.isoformat() if item.step.end else None, "limit": item.step.limit},
        "data": json.loads(item.data_json),
    } for item in items])


class PlanStop(StrEnum):
    """Distinguish missing information from facts; e.g. PlanStop.NO_NEW_DATA."""

    DONE = "done"
    SUFFICIENT = "sufficient"
    INVALID = "invalid_plan"
    FORBIDDEN = "forbidden"
    DUPLICATE = "duplicate"
    CALL_LIMIT = "call_limit"
    TIME_LIMIT = "time_limit"
    VOLUME_LIMIT = "volume_limit"
    NO_NEW_DATA = "no_new_data"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PlanResult:
    """Return cumulative receipts and an explicit stopping condition; e.g. PlanResult((), PlanStop.DONE)."""

    items: tuple[ContextItem, ...]
    stop: PlanStop

    @property
    def model_context(self) -> str:
        """Return the sole data envelope to pass to the model; e.g. result.model_context."""
        return context_json(self.items)


def _valid_step(step: ReadStep) -> bool:
    """Validate exact parameters before any read; e.g. _valid_step(ReadStep(ReadTool.CARD))."""
    if type(step) is not ReadStep or type(step.tool) is not ReadTool:
        return False
    if step.tool in (ReadTool.CARD, ReadTool.DESCRIPTION):
        return step.start is None and step.end is None and step.limit is None
    try:
        validate_history_period(step.start, step.end)
        if step.tool is ReadTool.COMMENTS:
            validate_comment_limit(step.limit)
        elif step.limit is not None:
            return False
    except (ValueError, TypeError):
        return False
    return True


class ReadingRun:
    """Share finite budgets, duplicates and provenance across plans in one run.

    Build on the server with scoped tools/map. Example: run.execute(ReadPlan((step,))).
    A synchronous read cannot be interrupted: check deadlines before and after it,
    discard late data, and configure real adapter timeouts separately.
    """

    def __init__(
        self, tools: TaskReadTools, reading_map: TaskReadMap, *, budget: ReadBudget = ReadBudget(),
        clock: Callable[[], float] = monotonic,
        sufficient: Callable[[tuple[ContextItem, ...]], bool] = lambda items: False,
    ) -> None:
        """Bind readers and an exactly-bool stop predicate; e.g. ReadingRun(tools, reading_map).

        A non-bool predicate result fails the plan before any further read.
        """
        if type(budget) is not ReadBudget:
            message: str = "Typed server budget required"
            raise ValueError(message)
        self._tools: TaskReadTools = tools
        self._map: TaskReadMap = reading_map
        self.budget: ReadBudget = budget
        self._clock: Callable[[], float] = clock
        self._sufficient: Callable[[tuple[ContextItem, ...]], bool] = sufficient
        self._started: float = clock()
        self._calls: int = 0
        self._seen: set[ReadStep] = set()
        self._items: tuple[ContextItem, ...] = ()

    @property
    def items(self) -> tuple[ContextItem, ...]:
        """Return cumulative provenance, including unquoted evidence; e.g. run.items."""
        return self._items

    def expired(self, *, reserve: bool = False) -> bool:
        """Check the cooperative deadline; e.g. run.expired(reserve=True) before reading."""
        limit: float = self.budget.total_seconds - (self.budget.reserve_seconds if reserve else 0)
        return self._clock() - self._started >= limit

    def execute(self, plan: ReadPlan) -> PlanResult:
        """Validate the whole plan then read in order; e.g. run.execute(ReadPlan((step,)))."""
        stop: PlanStop = PlanStop.DONE
        if (type(plan) is not ReadPlan or type(plan.steps) is not tuple
                or len(plan.steps) > self.budget.max_calls or not all(_valid_step(step) for step in plan.steps)):
            return PlanResult(self._items, PlanStop.INVALID)
        try:
            for step in plan.steps:
                enough: object = self._sufficient(self._items)
                if type(enough) is not bool:
                    stop = PlanStop.FAILED
                    break
                if enough:
                    stop = PlanStop.SUFFICIENT
                    break
                if self.expired(reserve=True):
                    stop = PlanStop.TIME_LIMIT
                    break
                if step in self._seen:
                    stop = PlanStop.DUPLICATE
                    break
                if self._calls >= self.budget.max_calls:
                    stop = PlanStop.CALL_LIMIT
                    break
                status, current = self._map.build()
                if status is not ReadStatus.AVAILABLE or current is None:
                    stop = PlanStop.FAILED if status is ReadStatus.FAILED else PlanStop.UNAVAILABLE
                    break
                if step.tool not in {branch.tool for branch in current.branches}:
                    stop = PlanStop.FORBIDDEN
                    break
                if self.expired(reserve=True):
                    stop = PlanStop.TIME_LIMIT
                    break
                self._seen.add(step)
                self._calls += 1
                status, item = read_item(self._tools, step)
                if self.expired(reserve=True):
                    stop = PlanStop.TIME_LIMIT
                    break
                if item is None:
                    stop = PlanStop.FAILED if status is ReadStatus.FAILED else PlanStop.UNAVAILABLE
                    break
                if any(old.step.tool is step.tool and old.data_json == item.data_json for old in self._items):
                    stop = PlanStop.NO_NEW_DATA
                    break
                proposed: tuple[ContextItem, ...] = (*self._items, item)
                if len(context_json(proposed).encode("utf-8")) > self.budget.max_bytes:
                    stop = PlanStop.VOLUME_LIMIT
                    break
                self._items = proposed
                if not item.sources:
                    stop = PlanStop.NO_NEW_DATA
                    break
        except Exception:  # noqa: BLE001 -- Expose no reader data or diagnostic text on failure.
            stop = PlanStop.FAILED
        return PlanResult(self._items, stop)
