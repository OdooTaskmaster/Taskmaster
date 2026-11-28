"""Exercise the read-only graph with synthetic ports and the real orchestration runtime."""

from contextlib import nullcontext
from datetime import date
from unittest.mock import Mock

import pytest

from application.risk_analysis_graph import AnalysisStatus, analyze_risk
from application.model_port import ModelResponse
from application.risk_analysis_model import RiskAnalysisModel
from application.task_read_map import ReadBranch, ReadScenario, ReadTool, TaskReadMap, TaskReadingMap
from application.task_read_plan import PlanResult, ReadingContext, ReadObservation, ReadRunStatus, ReadStep, TaskReadRun
from application.task_read_tools import Comment, Comments, ReadStatus, TaskCard, TaskReadTools
from application.task_result_release import ModelDraft, ReleasedResult, ReleaseStatus, TaskResultRelease

pytest.importorskip("langgraph.graph")

_CARD: ReadingContext = ReadingContext((ReadObservation(ReadStep(ReadTool.CARD), b"{}", ()),))
_EXTRA: ReadingContext = ReadingContext((*_CARD.observations, ReadObservation(ReadStep(ReadTool.DESCRIPTION), b"{}", ())))
_DRAFT: ModelDraft = ModelDraft("Участник сообщил о препятствии.", ())


@pytest.fixture
def ports() -> tuple[Mock, Mock, Mock]:
    """Provide a minimal evidence run; ports() returns model, reading and release doubles."""
    model: Mock = Mock()
    model.assess.return_value = _DRAFT
    reading: Mock = Mock(context=_CARD)
    reading.prepare.return_value = (ReadRunStatus.READY, TaskReadingMap(
        ReadScenario.RISK_ANALYSIS, (ReadBranch(ReadTool.DESCRIPTION),),
    ))
    release: Mock = Mock()
    release.check.return_value = (ReleaseStatus.RELEASED, ReleasedResult(_DRAFT.text, ()))
    return model, reading, release


def test_enough_initial_context_never_reads_more(ports: tuple[Mock, Mock, Mock]) -> None:
    """Release the initial explanation without accessing a reading map."""
    model, reading, release = ports
    result = analyze_risk("", model=model, reading=reading, release=release)
    assert result.status is AnalysisStatus.ANSWERED and result.facts is _CARD
    reading.prepare.assert_not_called()
    reading.execute.assert_not_called()
    release.check.assert_called_once_with(_DRAFT, _CARD)


def test_additional_context_is_rechecked_before_release(ports: tuple[Mock, Mock, Mock]) -> None:
    """Use one plan and send the complete model context to the final barrier."""
    model, reading, release = ports
    model.assess.side_effect = [None, _DRAFT]
    model.plan.return_value = (ReadStep(ReadTool.DESCRIPTION),)

    def execute(_steps: tuple[ReadStep, ...]) -> PlanResult:
        """Admit the one synthetic extra observation; return the expanded context."""
        reading.context = _EXTRA
        return PlanResult(ReadRunStatus.FINISHED, _EXTRA)

    reading.execute.side_effect = execute
    result = analyze_risk("Почему?", model=model, reading=reading, release=release)
    assert result.status is AnalysisStatus.ANSWERED and result.facts is _EXTRA
    reading.execute.assert_called_once_with(model.plan.return_value)
    release.check.assert_called_once_with(_DRAFT, _EXTRA)


def test_no_new_data_has_a_distinct_status(ports: tuple[Mock, Mock, Mock]) -> None:
    """Return a server-written clarification instead of an invented explanation."""
    model, reading, release = ports
    model.assess.return_value = None
    model.plan.return_value = ()
    reading.execute.return_value = PlanResult(ReadRunStatus.NO_NEW_DATA, _CARD)
    release.check.side_effect = lambda draft, _context: (ReleaseStatus.RELEASED, ReleasedResult(draft.text, ()))
    result = analyze_risk("Почему?", model=model, reading=reading, release=release)
    assert result.status is AnalysisStatus.INSUFFICIENT
    assert result.explanation is not None and "Недостаточно" in result.explanation.text


def test_revocation_withholds_the_entire_response(ports: tuple[Mock, Mock, Mock]) -> None:
    """Suppress both the explanation and facts when the barrier cannot release them."""
    model, reading, release = ports
    release.check.return_value = (ReleaseStatus.UNAVAILABLE, None)
    result = analyze_risk("Почему?", model=model, reading=reading, release=release)
    assert result.status is AnalysisStatus.UNAVAILABLE and result.facts is None and result.explanation is None


def test_invalid_model_response_is_not_absence_of_risk(ports: tuple[Mock, Mock, Mock]) -> None:
    """Keep parsing failure separate from an evidence gap or a negative risk answer."""
    model, reading, release = ports
    model.assess.side_effect = ValueError("private response is invalid")
    result = analyze_risk("Почему?", model=model, reading=reading, release=release)
    assert result.status is AnalysisStatus.FAILED and result.facts is None
    release.check.assert_not_called()


@pytest.mark.parametrize("revoked", [False, True])
def test_real_plan_and_release_recheck_an_uncited_comment(revoked: bool) -> None:
    """Run the whole graph through typed readers and suppress stale uncited evidence on revocation."""
    day: date = date(2026, 11, 27)
    card: TaskCard = TaskCard("Demo task", None, None, False, None, "/odoo/project.task/9")
    comments: Comments = Comments((Comment(day, "Waiting for access", "/mail/message/17"),), False, False)
    source: Mock = Mock()
    source.read_types.return_value = (ReadStatus.AVAILABLE, (ReadTool.CARD, ReadTool.COMMENTS))
    source.comments.return_value = (ReadStatus.AVAILABLE, comments)
    fresh: Mock = Mock()
    fresh.card.return_value = (ReadStatus.AVAILABLE, card)
    fresh.comments.return_value = (ReadStatus.UNAVAILABLE, None) if revoked else (ReadStatus.AVAILABLE, comments)
    reading: TaskReadRun = TaskReadRun(
        TaskReadMap(source, scenario=ReadScenario.RISK_ANALYSIS), TaskReadTools(source),
        initial=((ReadStep(ReadTool.CARD), card),),
    )
    model: Mock = Mock()
    model.complete.side_effect = [ModelResponse(response, "synthetic") for response in (
        '{"need_data":true}',
        '{"steps":[{"tool":"read_task_comments","start":"2026-11-27","end":"2026-11-27","limit":1}]}',
        '{"text":"The participant reported a possible blocker, not a verified cause.","citations":["/odoo/project.task/9"]}',
    )]
    gate: TaskResultRelease = TaskResultRelease(lambda: nullcontext(TaskReadTools(fresh)))
    result = analyze_risk("Why?", model=RiskAnalysisModel(model), reading=reading, release=gate)
    source.comments.assert_called_once_with(day, day, 1)
    fresh.comments.assert_called_once_with(day, day, 1)
    if revoked:
        assert result.status is AnalysisStatus.UNAVAILABLE and result.facts is None and result.explanation is None
    else:
        assert result.status is AnalysisStatus.ANSWERED
        assert result.explanation is not None and result.explanation.links == ("/odoo/project.task/9",)
        assert result.facts is not None and len(result.facts.observations) == 2
