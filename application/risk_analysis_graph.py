"""Compose the read-only risk workflow without checkpoints or routing by model text."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, TypedDict

from .dialogue_provenance import Evidence
from .model_port import ModelFailure
from .risk_analysis_model import RiskAnalysisModel
from .task_read_plan import ReadingContext, ReadRunStatus, TaskReadRun
from .task_result_release import ModelDraft, ReleasedResult, ReleaseStatus, TaskResultRelease


class AnalysisStatus(StrEnum):
    """Separate a grounded reply, missing evidence, denied access and failure."""

    ANSWERED = "answered"
    INSUFFICIENT = "insufficient"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RiskAnalysis:
    """Return server-held facts separately from the released explanation."""

    status: AnalysisStatus
    facts: ReadingContext | None = None
    explanation: ReleasedResult | None = None


class _State(TypedDict, total=False):
    """Track one invocation's decision, draft and output without persisting them."""

    branch: Literal["read", "release", "stop"]
    draft: ModelDraft
    status: AnalysisStatus
    result: RiskAnalysis


def analyze_risk(question: str, *, model: RiskAnalysisModel, reading: TaskReadRun,
                 release: TaskResultRelease, inherited: tuple[Evidence, ...] = ()) -> RiskAnalysis:
    """Run one read-only graph with server-scoped readers and a fresh release gate.

    Example: analyze_risk('', model=model, reading=run, release=gate) defaults
    to an explanation and returns no facts if final authorization fails.
    """
    try:
        from langgraph.graph import END, START, StateGraph
        from langsmith import tracing_context
    except ImportError:
        return RiskAnalysis(AnalysisStatus.FAILED)

    def assess(_state: _State) -> _State:
        """Assess initial evidence; return a fixed branch and any proposed draft."""
        context: ReadingContext | None = reading.context
        if context is None or not context.observations:
            return {"branch": "stop", "result": RiskAnalysis(AnalysisStatus.UNAVAILABLE)}
        draft: ModelDraft | None = model.assess(question, context)
        if draft is None:
            return {"branch": "read"}
        return {"branch": "release", "draft": draft, "status": AnalysisStatus.ANSWERED}

    def read(_state: _State) -> _State:
        """Run at most one bounded, validated plan; return an answer or an explicit gap."""
        context: ReadingContext | None = reading.context
        map_status, reading_map = reading.prepare()
        if context is None or map_status is ReadRunStatus.UNAVAILABLE:
            return {"branch": "stop", "result": RiskAnalysis(AnalysisStatus.UNAVAILABLE)}
        if map_status is ReadRunStatus.FAILED:
            return {"branch": "stop", "result": RiskAnalysis(AnalysisStatus.FAILED)}
        if reading_map is not None and reading_map.branches:
            result = reading.execute(model.plan(question, context, reading_map))
            if result.status is ReadRunStatus.UNAVAILABLE:
                return {"branch": "stop", "result": RiskAnalysis(AnalysisStatus.UNAVAILABLE)}
            if result.status in (ReadRunStatus.FAILED, ReadRunStatus.INVALID_PLAN):
                return {"branch": "stop", "result": RiskAnalysis(AnalysisStatus.FAILED)}
            if result.context is not None and result.context != context:
                draft: ModelDraft | None = model.assess(question, result.context)
                if draft is not None:
                    return {"branch": "release", "draft": draft, "status": AnalysisStatus.ANSWERED}
        return {
            "branch": "release", "status": AnalysisStatus.INSUFFICIENT,
            "draft": ModelDraft("Недостаточно данных. Уточните, какое событие или утверждение нужно проверить.", ()),
        }

    def verify(state: _State) -> _State:
        """Return facts only if the draft and every supplied projection pass the gate."""
        verdict, output = (release.check(state["draft"], reading.context, inherited=inherited) if inherited
                           else release.check(state["draft"], reading.context))
        if verdict is ReleaseStatus.RELEASED and output is not None:
            return {"result": RiskAnalysis(state["status"], reading.context, output)}
        status: AnalysisStatus = AnalysisStatus.UNAVAILABLE if verdict is ReleaseStatus.UNAVAILABLE else AnalysisStatus.FAILED
        return {"result": RiskAnalysis(status)}

    def route(state: _State) -> Literal["read", "release", "stop"]:
        """Choose a server branch; route({'branch': 'stop'}) ends the graph."""
        return state["branch"]

    graph = StateGraph(_State)
    graph.add_node("assess", assess)
    graph.add_node("read", read)
    graph.add_node("verify", verify)
    graph.add_edge(START, "assess")
    graph.add_conditional_edges("assess", route, {"read": "read", "release": "verify", "stop": END})
    graph.add_conditional_edges("read", route, {"release": "verify", "stop": END})
    graph.add_edge("verify", END)
    try:
        with tracing_context(enabled=False, parent=False):
            return graph.compile().invoke({"branch": "stop"})["result"]
    except (ModelFailure, ValueError, UnicodeError, RecursionError):
        return RiskAnalysis(AnalysisStatus.FAILED)
