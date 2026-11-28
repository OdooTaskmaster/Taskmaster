"""Exercise model proposals without contacting a provider or running tools."""

from datetime import UTC, date, datetime
import json
from unittest.mock import Mock

import pytest

from application.model_port import ModelResponse
from application.risk_analysis_model import RiskAnalysisModel
from application.risk_analysis_signal import RiskSignal
from application.task_read_map import ReadBranch, ReadScenario, ReadTool, TaskReadingMap
from application.task_read_plan import ReadingContext, ReadObservation, ReadStep
from application.task_result_release import ModelDraft

_CONTEXT: ReadingContext = ReadingContext((ReadObservation(
    ReadStep(ReadTool.CARD), b'{"data":{"source":"/odoo/project.task/7"}}',
    (("/odoo/project.task/7", "/odoo/project.task/7"),),
),))
_MAP: TaskReadingMap = TaskReadingMap(ReadScenario.RISK_ANALYSIS, (
    ReadBranch(ReadTool.COMMENTS, ("start", "end", "limit"), max_period_days=7, max_fragments=3),
))


def test_assessment_only_sends_admitted_evidence() -> None:
    """Check that initial assessment sends no reading map or server scope."""
    model: Mock = Mock()
    model.complete.return_value = ModelResponse('{"need_data":true}', "synthetic")
    adapter: RiskAnalysisModel = RiskAnalysisModel(model)
    assert adapter.assess("Explain this risk", _CONTEXT) is None
    request = model.complete.call_args.args[0]
    assert [message.role for message in request.messages] == ["system", "user"]
    assert json.loads(request.messages[1].content) == {
        "question": "Explain this risk", "evidence": json.loads(_CONTEXT.model_json),
    }
    assert request.max_tokens == 1024
    with pytest.raises(ValueError):
        adapter.assess("Explain this risk", _CONTEXT, final=True)


def test_assessment_returns_draft_not_an_authorized_result() -> None:
    """Keep model text and citation keys as a draft for the release gate."""
    model: Mock = Mock()
    model.complete.return_value = ModelResponse('{"text":"A possible cause","citations":["/odoo/project.task/7"]}', "synthetic")
    assert RiskAnalysisModel(model).assess("", _CONTEXT) == ModelDraft("A possible cause", ("/odoo/project.task/7",))


def test_saved_signal_is_separate_from_current_evidence() -> None:
    """Send historically observed data separately without inventing current values."""
    model: Mock = Mock()
    model.complete.return_value = ModelResponse('{"need_data":true}', "synthetic")
    signal: RiskSignal = RiskSignal(datetime(2026, 11, 27, tzinfo=UTC), "Asia/Tokyo", ("RR-01",), True,
                                    date(2026, 11, 26), None)
    assert RiskAnalysisModel(model, signal=signal).assess("", _CONTEXT) is None
    payload = json.loads(model.complete.call_args.args[0].messages[1].content)
    assert payload["signal_at_check"]["deadline"] == "2026-11-26"
    assert payload["signal_at_check"]["rules"] == ["RR-01"]
    assert payload["evidence"] == json.loads(_CONTEXT.model_json)


def test_plan_accepts_only_typed_branch_arguments() -> None:
    """Parse an in-scope request into a read step without executing it."""
    model: Mock = Mock()
    model.complete.return_value = ModelResponse(
        '{"steps":[{"tool":"read_task_comments","start":"2026-11-24","end":"2026-11-27","limit":2}]}', "synthetic",
    )
    assert RiskAnalysisModel(model).plan("", _CONTEXT, _MAP) == (
        ReadStep(ReadTool.COMMENTS, date(2026, 11, 24), date(2026, 11, 27), 2),
    )


@pytest.mark.parametrize("body", [
    '{"steps":[{"tool":"read_task_card"}]}',
    '{"steps":[{"tool":"read_task_comments","project_id":88}]}',
    '{"steps":[{"tool":"read_task_comments","start":"2026-11-24","end":"2026-11-27","limit":true}]}',
    '{"steps":[{"tool":"read_task_comments","start":"2026-11-24","end":"2026-11-27","limit":4}]}',
    '{"steps":[],"steps":[]}',
])
def test_bad_plan_is_not_forwarded(body: str) -> None:
    """Reject readers, arguments or responses outside the proposal contract."""
    model: Mock = Mock()
    model.complete.return_value = ModelResponse(body, "synthetic")
    with pytest.raises(ValueError):
        RiskAnalysisModel(model).plan("", _CONTEXT, _MAP)
