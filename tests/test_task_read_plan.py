import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pytest

from application.task_read_map import ReadScenario, ReadTool, TaskReadMap
from application.task_read_plan import ReadBudget, ReadRunStatus, ReadStep, StepStatus, TaskReadRun
from application.task_read_tools import Comment, Comments, History, ReadStatus, TaskCard, TaskReadTools

DAY = date(2026, 11, 27)
CARD = ReadStep(ReadTool.CARD)
DESCRIPTION = ReadStep(ReadTool.DESCRIPTION)
HISTORY = ReadStep(ReadTool.HISTORY, DAY, DAY)
COMMENTS = ReadStep(ReadTool.COMMENTS, DAY, DAY)
TASK = "/odoo/project.task/9"


@dataclass
class Reader:
    calls: list[str] = field(default_factory=list)
    now: float = 0.0
    map_duration: float = 0.0
    card_duration: float = 0.0
    revoke_after_card: bool = False
    denied: bool = False

    def clock(self):
        return self.now

    def read_types(self):
        self.calls.append("map")
        self.now += self.map_duration
        return ReadStatus.AVAILABLE, tuple(ReadTool)

    def card(self):
        self.calls.append("card")
        self.now += self.card_duration
        self.denied = self.revoke_after_card
        return ReadStatus.AVAILABLE, TaskCard("Demo", None, None, False, None, TASK)

    def description(self):
        self.calls.append("description")
        if self.denied:
            return ReadStatus.UNAVAILABLE, None, None
        return ReadStatus.AVAILABLE, "\u0442\u0435\u043a\u0441\u0442 \U0001f600", TASK

    def history(self, start, end):
        self.calls.append("history")
        return ReadStatus.AVAILABLE, History((), False)

    def comments(self, start, end, limit):
        self.calls.append(f"comments:{limit}")
        return ReadStatus.AVAILABLE, Comments((Comment(start, "Demo comment", "/mail/message/5"),), False, False)


def make_run(reader, *, budget=ReadBudget(), allowed=frozenset(ReadTool), initial=()):
    reading_map = TaskReadMap(reader, scenario=ReadScenario.RISK_ANALYSIS, allowed_tools=allowed)
    return TaskReadRun(reading_map, TaskReadTools(reader), budget=budget, clock=reader.clock, initial=initial)


def test_plan_double_uses_existing_map_and_executes_in_order():
    reader = Reader()
    run = make_run(reader)
    status, reading_map = run.prepare()
    assert status is ReadRunStatus.READY
    model_double = lambda branches: (ReadStep(branches[0].tool), COMMENTS, DESCRIPTION)

    result = run.execute(model_double(reading_map.branches))

    assert result.status is ReadRunStatus.FINISHED
    assert result.attempts == 3
    assert result.steps == (StepStatus.ADMITTED,) * 3
    assert reader.calls == ["map", "card", "comments:20", "description"]
    assert [record["step"]["tool"] for record in json.loads(result.context.model_json)] == [
        CARD.tool, COMMENTS.tool, DESCRIPTION.tool,
    ]
    assert run.execute((CARD,)).status is ReadRunStatus.INVALID_PLAN
    assert reader.calls == ["map", "card", "comments:20", "description"]


@pytest.mark.parametrize("bad", [
    {"tool": ReadTool.CARD, "task_id": 1},
    ReadStep("sudo"),
    ReadStep(ReadTool.CARD, DAY, DAY),
    ReadStep(ReadTool.HISTORY, DAY, DAY - timedelta(days=1)),
    ReadStep(ReadTool.HISTORY, DAY, DAY + timedelta(days=90)),
    ReadStep(ReadTool.HISTORY, datetime(2026, 11, 27), DAY),
    ReadStep(ReadTool.HISTORY, date.min, DAY),
    ReadStep(ReadTool.COMMENTS, DAY, DAY, 21),
    ReadStep(ReadTool.COMMENTS, DAY, DAY, True),
    ReadStep(ReadTool.WORKLOG, DAY, DAY, 1),
])
def test_invalid_plan_never_starts_a_reader_even_with_a_valid_prefix(bad):
    reader = Reader()
    result = make_run(reader).execute((CARD, bad))
    assert (result.status, result.attempts, reader.calls) == (ReadRunStatus.INVALID_PLAN, 0, ["map"])
    assert result.context.model_json == b"[]"


def test_map_only_allows_server_policy():
    reader = Reader()
    result = make_run(reader, allowed=frozenset({ReadTool.CARD})).execute((CARD, DESCRIPTION))
    assert (result.status, result.attempts, reader.calls) == (ReadRunStatus.INVALID_PLAN, 0, ["map"])


def test_skip_canonical_duplicates_and_stop_when_sufficient():
    reader = Reader()
    result = make_run(reader).execute(
        (COMMENTS, ReadStep(ReadTool.COMMENTS, DAY, DAY, 20), CARD, DESCRIPTION),
        sufficient=lambda ctx: len(ctx.observations) >= 2,
    )
    assert result.status is ReadRunStatus.SUFFICIENT
    assert result.steps == (StepStatus.ADMITTED, StepStatus.DUPLICATE, StepStatus.ADMITTED)
    assert reader.calls == ["map", "comments:20", "card"]


def test_empty_incomplete_read_consumes_attempt_and_never_becomes_a_fact():
    reader = Reader()
    result = make_run(reader, budget=ReadBudget(max_calls=1)).execute((HISTORY, CARD))
    assert (result.status, result.attempts, reader.calls) == (ReadRunStatus.CALL_LIMIT, 1, ["map", "history"])
    assert result.steps == (StepStatus.NO_NEW_DATA,)
    assert result.context.model_json == b"[]"

    run = make_run(Reader())
    no_evidence = run.execute((HISTORY, HISTORY))
    assert (no_evidence.status, no_evidence.attempts) == (ReadRunStatus.NO_NEW_DATA, 1)
    assert no_evidence.steps == (StepStatus.NO_NEW_DATA, StepStatus.DUPLICATE)


def test_project_access_revoked_between_steps_blocks_read_and_stops_plan():
    reader = Reader(revoke_after_card=True)
    result = make_run(reader).execute((CARD, DESCRIPTION, COMMENTS))
    assert (result.status, result.attempts) == (ReadRunStatus.UNAVAILABLE, 2)
    assert reader.calls == ["map", "card", "description"]
    assert [item.step for item in result.context.observations] == [CARD]


@pytest.mark.parametrize("stage,calls", [
    ("map", ["map"]), ("planning", ["map"]), ("read", ["map", "card"]),
])
def test_shared_deadline_denies_late_map_plan_or_projection(stage, calls):
    reader = Reader(map_duration=5.0 if stage == "map" else 0.0, card_duration=5.0 if stage == "read" else 0.0)
    run = make_run(reader)
    if stage == "planning":
        assert run.prepare()[0] is ReadRunStatus.READY
        reader.now += 5.0
    result = run.execute((CARD, DESCRIPTION))
    assert result.status is ReadRunStatus.TIME_LIMIT
    assert reader.calls == calls
    assert result.context.model_json == b"[]"


def test_context_budget_includes_initial_json_and_utf8_bytes_without_partial_payload():
    card = TaskCard("Initial", None, None, False, None, TASK)
    initial = ((CARD, card),)
    reader = Reader()
    full = make_run(reader, initial=initial).execute((CARD, DESCRIPTION)).context.model_json
    assert len(full) > len(full.decode("utf-8"))

    limited_reader = Reader()
    limited = make_run(limited_reader, initial=initial, budget=ReadBudget(max_context_bytes=len(full) - 1))
    before = limited.context.model_json
    result = limited.execute((CARD, DESCRIPTION, COMMENTS))
    assert result.status is ReadRunStatus.VOLUME_LIMIT
    assert result.steps == (StepStatus.DUPLICATE,)
    assert result.context.model_json == before
    assert limited_reader.calls == ["map", "description"]

    exact_reader = Reader()
    exact = make_run(exact_reader, initial=initial, budget=ReadBudget(max_context_bytes=len(full)))
    exact_result = exact.execute((DESCRIPTION, COMMENTS))
    assert exact_result.status is ReadRunStatus.VOLUME_LIMIT
    assert exact_result.context.model_json == full
    assert exact_reader.calls == ["map", "description"]


def test_oversized_initial_context_or_plan_stops_before_map():
    reader = Reader()
    initial = ((CARD, TaskCard("Large", None, None, False, None, TASK)),)
    result = make_run(reader, initial=initial, budget=ReadBudget(max_context_bytes=2)).execute((DESCRIPTION,))
    assert result.status is ReadRunStatus.VOLUME_LIMIT
    assert result.context is None
    assert make_run(reader).execute((CARD,) * 17).status is ReadRunStatus.INVALID_PLAN
    assert reader.calls == []
