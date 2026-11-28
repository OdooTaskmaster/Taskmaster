"""Attack typed plans, cumulative budgets and full-context output authorization."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from application.reading_plan import (
    PlanStop,
    ReadBudget,
    ReadingRun,
    ReadPlan,
    ReadStep,
    SourceRef,
    context_json,
    read_item,
)
from application.source_gate import Candidate, GateStatus, SourceGate
from application.task_read_map import ReadBranch, ReadScenario, ReadTool, TaskReadingMap
from application.task_read_tools import (
    Comment,
    Comments,
    Description,
    History,
    ReadStatus,
    TaskAssignee,
    TaskCard,
    TaskReadTools,
    Transition,
    Worklog,
    WorklogEntry,
    WorklogTotal,
)

DAY: date = date(2026, 1, 1)
CARD: ReadStep = ReadStep(ReadTool.CARD)
DESCRIPTION: ReadStep = ReadStep(ReadTool.DESCRIPTION)
COMMENTS: ReadStep = ReadStep(ReadTool.COMMENTS, DAY, DAY, 2)
REF: SourceRef = SourceRef("project.task", 9)
MESSAGE: SourceRef = SourceRef("mail.message", 5)


@pytest.fixture
def setup_run() -> tuple[ReadingRun, MagicMock, MagicMock, list[float]]:
    """Build typed readers with a controllable monotonic clock; e.g. run, tools, mapping, clock = setup_run."""
    tools: MagicMock = MagicMock(spec=TaskReadTools)
    tools.read_task_card.return_value = (
        ReadStatus.AVAILABLE, TaskCard("Task", "open", DAY, True, (TaskAssignee(3, "Member"),), REF.link()),
    )
    tools.read_task_description.return_value = ReadStatus.AVAILABLE, Description("Facts", REF.link(), False)
    tools.read_task_comments.return_value = ReadStatus.AVAILABLE, Comments((Comment(DAY, "Note", MESSAGE.link()),))
    tools.read_task_stage_history.return_value = ReadStatus.AVAILABLE, History((Transition(DAY, "state", "A", "B", "mail.message:5"),), False)
    tools.read_task_worklog.return_value = ReadStatus.AVAILABLE, Worklog(
        (WorklogTotal(1, "Hours", Decimal("1.5")),),
        (WorklogEntry(DAY, Decimal("1.5"), 1, 1, ("/odoo/account.analytic.line/8",)),),
        period_start=DAY, period_end=DAY,
    )
    mapping: MagicMock = MagicMock()
    mapping.build.return_value = ReadStatus.AVAILABLE, TaskReadingMap(
        ReadScenario.RISK_ANALYSIS, tuple(ReadBranch(tool) for tool in ReadTool),
    )
    clock: list[float] = [0.0]
    return ReadingRun(tools, mapping, clock=lambda: clock[0]), tools, mapping, clock


def test_order_and_complete_source_ledger(setup_run: tuple) -> None:
    run, tools, mapping, _ = setup_run
    steps: tuple[ReadStep, ...] = (CARD, DESCRIPTION, COMMENTS, ReadStep(ReadTool.HISTORY, DAY, DAY), ReadStep(ReadTool.WORKLOG, DAY, DAY))
    result = run.execute(ReadPlan(steps))
    assert result.stop is PlanStop.DONE
    assert tuple(item.step for item in result.items) == steps
    assert [call[0] for call in tools.method_calls] == [step.tool.value for step in steps]
    assert mapping.build.call_count == 5
    assert {ref for item in run.items for ref in item.sources} == {REF, MESSAGE, SourceRef("account.analytic.line", 8)}
    assert '"amount":"1.5"' in result.model_context
    assert "mail.message:5" not in result.model_context


@pytest.mark.parametrize("plan", [
    None, {}, ReadPlan([]), ReadPlan([CARD]), ReadPlan((None,)), ReadPlan((ReadStep("read_task_card"),)),
    ReadPlan((ReadStep(ReadTool.CARD, DAY),)), ReadPlan((ReadStep(ReadTool.DESCRIPTION, limit=1),)),
    ReadPlan((ReadStep(ReadTool.COMMENTS, DAY, DAY, True),)),
    ReadPlan((ReadStep(ReadTool.COMMENTS, DAY, DAY, 21),)),
    ReadPlan((ReadStep(ReadTool.HISTORY, DAY, DAY, 1),)),
    ReadPlan((ReadStep(ReadTool.WORKLOG, DAY, None),)),
    ReadPlan((ReadStep(ReadTool.HISTORY, DAY, date(2026, 4, 1)),)),
    ReadPlan((CARD,) * 7), ReadPlan((CARD, ReadStep("sudo"))),
])
def test_invalid_entire_plan_never_reads(setup_run: tuple, plan: object) -> None:
    run, tools, mapping, _ = setup_run
    result = run.execute(plan)
    assert result.stop is PlanStop.INVALID and not result.items
    assert not tools.mock_calls and not mapping.mock_calls


def test_empty_plan_is_valid_and_does_not_read(setup_run: tuple) -> None:
    run, tools, _, _ = setup_run
    assert run.execute(ReadPlan(())).stop is PlanStop.DONE
    assert not tools.mock_calls


def test_duplicates_share_state_across_plans(setup_run: tuple) -> None:
    run, tools, _, _ = setup_run
    assert run.execute(ReadPlan((CARD, CARD))).stop is PlanStop.DUPLICATE
    assert run.execute(ReadPlan((CARD,))).stop is PlanStop.DUPLICATE
    assert tools.read_task_card.call_count == 1


def test_call_budget_is_cumulative_including_failed_attempts(setup_run: tuple) -> None:
    _, tools, mapping, clock = setup_run
    run = ReadingRun(tools, mapping, budget=ReadBudget(max_calls=1), clock=lambda: clock[0])
    tools.read_task_card.return_value = ReadStatus.UNAVAILABLE, None
    assert run.execute(ReadPlan((CARD,))).stop is PlanStop.UNAVAILABLE
    assert run.execute(ReadPlan((DESCRIPTION,))).stop is PlanStop.CALL_LIMIT
    tools.read_task_description.assert_not_called()


@pytest.mark.parametrize("at,expected", [(19.999, PlanStop.DONE), (20.0, PlanStop.TIME_LIMIT), (30.0, PlanStop.TIME_LIMIT)])
def test_read_deadline_reserves_ten_seconds(setup_run: tuple, at: float, expected: PlanStop) -> None:
    run, tools, _, clock = setup_run
    clock[0] = at
    assert run.execute(ReadPlan((CARD,))).stop is expected
    assert tools.read_task_card.call_count == (1 if expected is PlanStop.DONE else 0)


@pytest.mark.parametrize("phase", ["map", "read"])
def test_overrunning_sync_call_is_not_exposed(setup_run: tuple, phase: str) -> None:
    run, tools, mapping, clock = setup_run
    target = mapping.build if phase == "map" else tools.read_task_card
    original = target.return_value

    def late() -> object:
        clock[0] = 20.0
        return original

    target.side_effect = late
    result = run.execute(ReadPlan((CARD, DESCRIPTION)))
    assert result.stop is PlanStop.TIME_LIMIT and not result.items
    tools.read_task_description.assert_not_called()
    assert tools.read_task_card.call_count == (phase == "read")


@pytest.mark.parametrize("delta,expected", [(-1, PlanStop.VOLUME_LIMIT), (0, PlanStop.DONE), (1, PlanStop.DONE)])
def test_exact_utf8_envelope_boundary(setup_run: tuple, delta: int, expected: PlanStop) -> None:
    _, tools, mapping, clock = setup_run
    tools.read_task_description.return_value = ReadStatus.AVAILABLE, Description("Я🙂" * 5, REF.link(), False)
    _, item = read_item(tools, DESCRIPTION)
    size: int = len(context_json((item,)).encode("utf-8"))
    run = ReadingRun(tools, mapping, budget=ReadBudget(max_bytes=size + delta), clock=lambda: clock[0])
    result = run.execute(ReadPlan((DESCRIPTION,)))
    assert result.stop is expected
    assert bool(result.items) is (expected is PlanStop.DONE)
    if result.items:
        assert len(result.model_context.encode("utf-8")) == size


def test_cumulative_volume_does_not_leak_overflow_item(setup_run: tuple) -> None:
    _, tools, mapping, _ = setup_run
    _, item = read_item(tools, CARD)
    run = ReadingRun(tools, mapping, budget=ReadBudget(max_bytes=len(context_json((item,)).encode("utf-8"))))
    result = run.execute(ReadPlan((CARD, DESCRIPTION)))
    assert result.stop is PlanStop.VOLUME_LIMIT
    assert len(result.items) == 1 and "Facts" not in result.model_context


def test_permission_map_rebuilt_between_steps(setup_run: tuple) -> None:
    run, tools, mapping, _ = setup_run
    mapping.build.side_effect = [mapping.build.return_value, (ReadStatus.UNAVAILABLE, None)]
    result = run.execute(ReadPlan((CARD, DESCRIPTION)))
    assert result.stop is PlanStop.UNAVAILABLE and len(result.items) == 1
    tools.read_task_description.assert_not_called()


def test_forbidden_tool_does_not_read(setup_run: tuple) -> None:
    run, tools, mapping, _ = setup_run
    mapping.build.return_value = ReadStatus.AVAILABLE, TaskReadingMap(ReadScenario.DRAFT, ())
    assert run.execute(ReadPlan((CARD,))).stop is PlanStop.FORBIDDEN
    assert not tools.mock_calls


def test_sufficient_data_skips_remaining_steps(setup_run: tuple) -> None:
    _, tools, mapping, _ = setup_run
    run = ReadingRun(tools, mapping, sufficient=bool)
    assert run.execute(ReadPlan((CARD, DESCRIPTION))).stop is PlanStop.SUFFICIENT
    tools.read_task_description.assert_not_called()


def test_same_data_in_different_period_does_not_consume_context_twice(setup_run: tuple) -> None:
    run, tools, _, _ = setup_run
    next_step: ReadStep = replace(COMMENTS, end=date(2026, 1, 2))
    result = run.execute(ReadPlan((COMMENTS, next_step, CARD)))
    assert result.stop is PlanStop.NO_NEW_DATA and len(result.items) == 1
    assert tools.read_task_comments.call_count == 2
    tools.read_task_card.assert_not_called()


@pytest.mark.parametrize("phase", ["map", "read", "encode"])
def test_failures_do_not_expose_exception_text(setup_run: tuple, phase: str) -> None:
    run, tools, mapping, _ = setup_run
    if phase == "map":
        mapping.build.side_effect = RuntimeError("SENSITIVE")
    elif phase == "read":
        tools.read_task_card.side_effect = RuntimeError("SENSITIVE")
    else:
        tools.read_task_card.return_value = ReadStatus.AVAILABLE, TaskCard(None, None, None, False, None, "https://SENSITIVE")
    result = run.execute(ReadPlan((CARD,)))
    assert result.stop is PlanStop.FAILED and result.model_context == "[]"


@pytest.mark.parametrize("kwargs", [
    {"max_calls": 0}, {"max_calls": True}, {"max_bytes": -1}, {"max_bytes": 1.5},
    {"total_seconds": float("inf")}, {"total_seconds": float("nan")},
    {"reserve_seconds": 0}, {"reserve_seconds": 30}, {"reserve_seconds": True},
])
def test_invalid_budgets_rejected(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        ReadBudget(**kwargs)


def test_gate_rechecks_uncited_context_and_builds_links(setup_run: tuple) -> None:
    run, _, _, _ = setup_run
    run.execute(ReadPlan((CARD, COMMENTS)))
    verifier: MagicMock = MagicMock()
    verifier.check.return_value = True
    gate: SourceGate = SourceGate(run, verifier)
    status, answer = gate.validate(Candidate("Interpretation, not a proven fact", (REF,)))
    assert status is GateStatus.AVAILABLE
    assert answer.links == ("/odoo/project.task/9",)
    assert answer.provenance == run.items
    assert MESSAGE in {ref for item in answer.provenance for ref in item.sources}
    verifier.check.assert_called_once_with(run.items)
    verifier.check.return_value = False
    assert gate.validate(Candidate("Now secret derived from unquoted note", (REF,))) == (GateStatus.DENIED, None)


@pytest.mark.parametrize("candidate", [
    None, {}, Candidate("", ()), Candidate("x" * 8001, ()), Candidate("x", []),
    Candidate("x", ("/mail/message/5",)), Candidate("x", (REF, REF)),
    Candidate("x", (SourceRef("mail.message", 99),)), Candidate("x https://bad", ()),
    Candidate("x /odoo/project.task/99", ()),
])
def test_invalid_candidate_never_rechecks_or_returns_text(setup_run: tuple, candidate: object) -> None:
    run, _, _, _ = setup_run
    run.execute(ReadPlan((CARD,)))
    verifier: MagicMock = MagicMock()
    assert SourceGate(run, verifier).validate(candidate) == (GateStatus.INVALID, None)
    verifier.check.assert_not_called()


@pytest.mark.parametrize("result", [False, None, 1, "yes"])
def test_nontrue_final_authorization_withholds_everything(setup_run: tuple, result: object) -> None:
    run, _, _, _ = setup_run
    run.execute(ReadPlan((CARD,)))
    verifier: MagicMock = MagicMock()
    verifier.check.return_value = result
    assert SourceGate(run, verifier).validate(Candidate("private", ())) == (GateStatus.DENIED, None)


def test_gate_exception_has_no_partial_output(setup_run: tuple) -> None:
    run, _, _, _ = setup_run
    run.execute(ReadPlan((CARD,)))
    verifier: MagicMock = MagicMock()
    verifier.check.side_effect = RuntimeError("SENSITIVE")
    assert SourceGate(run, verifier).validate(Candidate("private", (REF,))) == (GateStatus.FAILED, None)


@pytest.mark.parametrize("phase", ["before", "during"])
def test_gate_enforces_whole_run_deadline(setup_run: tuple, phase: str) -> None:
    run, _, _, clock = setup_run
    run.execute(ReadPlan((CARD,)))
    verifier: MagicMock = MagicMock()

    def verify(items: tuple) -> bool:
        clock[0] = 30.0
        return True

    if phase == "before":
        clock[0] = 30.0
    else:
        verifier.check.side_effect = verify
    assert SourceGate(run, verifier).validate(Candidate("private", ())) == (GateStatus.EXPIRED, None)
    assert verifier.check.call_count == (phase == "during")


def test_no_context_cannot_authorize_model_text(setup_run: tuple) -> None:
    run, _, _, _ = setup_run
    assert SourceGate(run, MagicMock()).validate(Candidate("unsupported", ())) == (GateStatus.INVALID, None)


@pytest.mark.parametrize("model,rid", [("res.users", 1), ("project.task", True), ("mail.message", 0)])
def test_source_reference_rejects_unknown_or_false_ids(model: str, rid: object) -> None:
    with pytest.raises(ValueError):
        SourceRef(model, rid)


def test_input_cannot_override_server_scope(setup_run: tuple) -> None:
    run, _, _, _ = setup_run
    with pytest.raises(TypeError):
        run.execute(ReadPlan((CARD,)), task_id=55)


def test_actual_application_readers_and_legacy_history_links(setup_run: tuple) -> None:
    _, _, mapping, _ = setup_run
    source: MagicMock = MagicMock()
    source.description.return_value = ReadStatus.AVAILABLE, "<p>Visible</p>", REF.link()
    source.history.return_value = ReadStatus.AVAILABLE, History((Transition(DAY, "state", "A", "B", "mail.message:5"),), False)
    run = ReadingRun(TaskReadTools(source), mapping)
    result = run.execute(ReadPlan((DESCRIPTION, ReadStep(ReadTool.HISTORY, DAY, DAY))))
    assert result.stop is PlanStop.DONE
    assert "<p>" not in result.model_context and MESSAGE in result.items[1].sources


def test_initial_server_sufficiency_skips_all_reads(setup_run: tuple) -> None:
    _, tools, mapping, _ = setup_run
    run = ReadingRun(tools, mapping, sufficient=lambda items: True)
    assert run.execute(ReadPlan((CARD,))).stop is PlanStop.SUFFICIENT
    assert not tools.mock_calls and not mapping.mock_calls


def test_empty_visible_selection_retains_receipt_but_stops(setup_run: tuple) -> None:
    run, tools, _, _ = setup_run
    tools.read_task_comments.return_value = ReadStatus.AVAILABLE, Comments((), False, False)
    result = run.execute(ReadPlan((COMMENTS, CARD)))
    assert result.stop is PlanStop.NO_NEW_DATA and len(result.items) == 1
    assert '"complete":false' in result.model_context
    tools.read_task_card.assert_not_called()
    verifier: MagicMock = MagicMock()
    verifier.check.return_value = False
    assert SourceGate(run, verifier).validate(Candidate("Interpretation of empty selection", ())) == (GateStatus.DENIED, None)
    verifier.check.assert_called_once_with(run.items)


def test_six_calls_then_seventh_is_blocked_across_plans(setup_run: tuple) -> None:
    run, tools, _, _ = setup_run
    tools.read_task_comments.side_effect = [
        (ReadStatus.AVAILABLE, Comments((Comment(DAY, str(index), f"/mail/message/{index + 1}"),)))
        for index in range(6)
    ]
    steps: tuple[ReadStep, ...] = tuple(ReadStep(ReadTool.COMMENTS, DAY, date(2026, 1, index + 1), 1) for index in range(6))
    assert run.execute(ReadPlan(steps)).stop is PlanStop.DONE
    assert run.execute(ReadPlan((ReadStep(ReadTool.COMMENTS, DAY, date(2026, 1, 7), 1),))).stop is PlanStop.CALL_LIMIT
    assert tools.read_task_comments.call_count == 6


def test_full_comment_budget_cannot_overflow_default_context(setup_run: tuple) -> None:
    run, tools, _, _ = setup_run
    tools.read_task_comments.return_value = ReadStatus.AVAILABLE, Comments(tuple(
        Comment(DAY, "я" * 2000, f"/mail/message/{index + 1}") for index in range(20)
    ))
    result = run.execute(ReadPlan((replace(COMMENTS, limit=20),)))
    assert result.stop is PlanStop.VOLUME_LIMIT and result.model_context == "[]"


@pytest.mark.parametrize("value", [1, 0, "yes", [], None])
def test_nonbool_sufficiency_fails_before_reading(setup_run: tuple, value: object) -> None:
    _, tools, mapping, _ = setup_run
    run = ReadingRun(tools, mapping, sufficient=lambda items: value)
    assert run.execute(ReadPlan((CARD,))).stop is PlanStop.FAILED
    assert not tools.mock_calls and not mapping.mock_calls


@pytest.mark.parametrize("value", [0.1, float("nan"), float("inf"), object()])
def test_unknown_reader_types_are_fail_closed(setup_run: tuple, value: object) -> None:
    run, tools, _, _ = setup_run
    tools.read_task_card.return_value = ReadStatus.AVAILABLE, value
    result = run.execute(ReadPlan((CARD, DESCRIPTION)))
    assert result.stop is PlanStop.FAILED and result.model_context == "[]"
    tools.read_task_description.assert_not_called()


def test_tool_removed_from_fresh_map_blocks_second_read(setup_run: tuple) -> None:
    run, tools, mapping, _ = setup_run
    mapping.build.side_effect = [mapping.build.return_value, (
        ReadStatus.AVAILABLE, TaskReadingMap(ReadScenario.RISK_ANALYSIS, (ReadBranch(ReadTool.CARD),)),
    )]
    result = run.execute(ReadPlan((CARD, DESCRIPTION)))
    assert result.stop is PlanStop.FORBIDDEN and len(result.items) == 1
    tools.read_task_card.assert_called_once_with()
    tools.read_task_description.assert_not_called()


def test_server_supplies_visible_sources_when_model_does_not_cite(setup_run: tuple) -> None:
    run, _, _, _ = setup_run
    run.execute(ReadPlan((CARD, COMMENTS)))
    verifier: MagicMock = MagicMock()
    verifier.check.return_value = True
    status, answer = SourceGate(run, verifier).validate(Candidate("Interpretation", ()))
    assert status is GateStatus.AVAILABLE
    assert answer.links == (MESSAGE.link(), REF.link())
    assert answer.provenance == run.items


def test_authorized_empty_selection_has_no_invented_links(setup_run: tuple) -> None:
    run, tools, _, _ = setup_run
    tools.read_task_comments.return_value = ReadStatus.AVAILABLE, Comments((), False, False)
    run.execute(ReadPlan((COMMENTS,)))
    verifier: MagicMock = MagicMock()
    verifier.check.return_value = True
    status, answer = SourceGate(run, verifier).validate(Candidate("Visible selection is incomplete and empty", ()))
    assert status is GateStatus.AVAILABLE and answer.links == ()
    assert answer.provenance == run.items
    verifier.check.assert_called_once_with(run.items)
