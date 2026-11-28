"""Check HTTP handlers with a real SQLite store and Odoo request/ORM doubles."""

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, Mock
from urllib.parse import parse_qs, urlsplit

import pytest

if TYPE_CHECKING:
    from adapters.sqlite_state_store import SQLiteStateStore
    from application.risk_summary import ProjectRiskSummary
    from application.summary_store import SavedSummary


@pytest.fixture
def controller(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[ModuleType]:
    """Load the route with an Odoo double, returning the configured route module."""
    addon: ModuleType = ModuleType("_test_summary_route_addon")
    addon.__path__ = [str(Path(__file__).resolve().parents[1])]
    odoo: ModuleType = ModuleType("odoo")
    odoo.SUPERUSER_ID = 1
    http: ModuleType = ModuleType("odoo.http")
    http.Controller = object
    http.Response = object
    http.request = None
    http.route = lambda *_args, **_options: lambda method: method
    exceptions: ModuleType = ModuleType("odoo.exceptions")
    exceptions.AccessError = type("AccessError", (Exception,), {})
    exceptions.MissingError = type("MissingError", (Exception,), {})
    tools: ModuleType = ModuleType("odoo.tools")
    tools.config = {"data_dir": str(tmp_path)}
    for module in (addon, odoo, http, exceptions, tools):
        monkeypatch.setitem(sys.modules, module.__name__, module)

    summary: ModuleType = import_module(f"{addon.__name__}.controllers.summary")
    env: MagicMock = MagicMock(
        cr=SimpleNamespace(dbname="qa"), uid=7, su=False, context={"allowed_company_ids": [3, 1]},
        user=SimpleNamespace(active=True, has_group=lambda group: group == "base.group_user",
                             _get_company_ids=lambda: [1, 3]),
    )
    request: SimpleNamespace = SimpleNamespace(
        env=env, cookies={},
        update_context=Mock(side_effect=env.context.update),
        make_json_response=Mock(side_effect=lambda data, **options: (data, options)),
        make_response=Mock(side_effect=lambda body, **options: (body, options)),
        render=Mock(side_effect=lambda template, values, **options: (template, values, options)),
    )
    monkeypatch.setattr(summary, "request", request)
    monkeypatch.setattr(summary, "_try_sync_slot", lambda _env: False)
    monkeypatch.setattr(summary, "readable_projects", lambda _env, project: (
        SimpleNamespace(taskmaster_morning_timezone="Asia/Tokyo") if project == 4 else None
    ))
    monkeypatch.setattr(summary, "import_module", lambda _name: SimpleNamespace(Connection=object))
    monkeypatch.setenv("TASKMASTER_AMQP_URL", "amqp://localhost/")
    monkeypatch.setenv("TASKMASTER_QUEUE_MAX_LENGTH", "8")
    try:
        yield summary
    finally:
        for name in tuple(sys.modules):
            if name.startswith(f"{addon.__name__}."):
                sys.modules.pop(name)


def test_overload_returns_202_for_existing_operation(controller: ModuleType) -> None:
    """Check that the response identifies a row readable from a new store connection."""
    data, options = controller.SummaryController().generate_summary("4")
    key: str = data["operation_id"]
    assert options["status"] == 202 and set(data) == {"operation_id"}
    assert dict(options["headers"])["Location"] == f"/taskmaster/api/projects/4/operations/{key}"
    store = controller.SQLiteStateStore(controller.state_path("qa"), read_only=True)
    operation = store.get_operation(key, owner_id=7, project_id=4)
    assert operation is not None and operation.company_ids == (3, 1)
    assert operation.state is controller.OperationState.ACCEPTED
    assert operation.project_timezone == "Asia/Tokyo" and not operation.scheduled


@pytest.mark.parametrize("setting,expected", [("Asia/Tokyo", "Asia/Tokyo"), (False, "Europe/Moscow")])
def test_sync_summary_saves_the_calendar_it_uses(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, setting: str | bool, expected: str,
) -> None:
    """Keep the summary metadata and calculation in the same project calendar."""
    monkeypatch.setattr(controller, "_try_sync_slot", lambda _env: True)
    monkeypatch.setattr(controller, "readable_projects", lambda _env, _project: (
        SimpleNamespace(taskmaster_morning_timezone=setting)
    ))
    monkeypatch.setattr(controller, "read_summary_tasks", lambda _env, _project: ())
    build = Mock(wraps=controller.build_project_risk_summary)
    monkeypatch.setattr(controller, "build_project_risk_summary", build)
    controller.request.redirect = Mock(side_effect=lambda url, **_options: SimpleNamespace(location=url, headers={}))
    response = controller.SummaryController().generate_summary("4")
    key: str = parse_qs(urlsplit(response.location).query)["result_id"][0]
    store = controller.SQLiteStateStore(controller.state_path("qa"), read_only=True)
    saved = store.get_summary(key, project_id=4, recipient_id=7, authorize=lambda _: True)
    assert saved is not None and saved.project_timezone == expected
    assert build.call_args.kwargs["project_timezone"].key == expected


def test_overload_never_returns_202_when_sqlite_cannot_accept(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return a neutral 503 without a status link when the durable write fails."""
    monkeypatch.setattr(controller, "SQLiteStateStore", Mock(side_effect=controller.StateStoreError()))
    data, options = controller.SummaryController().generate_summary("4")
    assert options["status"] == 503 and data == {"error": "generation_unavailable"}
    assert "Location" not in dict(options["headers"])


@pytest.fixture
def risk_case(controller: ModuleType, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Prepare two saved runs and emulate project membership and task record rules."""
    package: str = controller.__package__.rsplit(".", 1)[0]
    core: ModuleType = import_module(f"{package}.application.risk_summary")
    checked_at: datetime = datetime(2026, 11, 25, 9, tzinfo=UTC)
    old_deadline: datetime = (checked_at - timedelta(days=2)).astimezone(timezone(timedelta(hours=3)))
    current_deadline: datetime = checked_at + timedelta(days=5)
    path: Path = controller.state_path("qa")
    path.parent.mkdir()
    store: SQLiteStateStore = controller.SQLiteStateStore(path)
    snapshot: ProjectRiskSummary = core.build_project_risk_summary(
        lambda: (
            core.SummaryTask(9, "Контрольная задача", core.DeadlineTaskSnapshot(True, False, old_deadline), ()),
            core.SummaryTask(10, "Без сохранённого срока", core.DeadlineTaskSnapshot(True, False, None, False), ()),
        ), checked_at=checked_at, project_timezone=UTC,
    )
    saved: SavedSummary = store.save_summary(
        "run-1", project_id=4, recipient_id=7, checked_at=checked_at, summary=snapshot, project_timezone="UTC",
    )
    latest: SavedSummary = store.save_summary("run-2", project_id=4, recipient_id=7,
                                              checked_at=checked_at + timedelta(hours=1), summary=snapshot)

    env = controller.request.env
    projects = Mock()
    projects.read.return_value = [{"id": 4, "name": "Demo"}]
    members: set[int] = {7, 11}
    tasks = Mock()
    visible_tasks: set[int] = {9, 10, 12}

    def search_projects(domain: list[tuple[str, str, object]], **_options: object) -> Mock | None:
        """Return the project only if its ID and requested membership match."""
        filters: dict[str, object] = {field: value for field, _operator, value in domain}
        if filters.get("id", 4) == 4 and filters.get("allowed_internal_user_ids") == [env.uid] and env.uid in members:
            return projects
        return None

    def search_tasks(
        domain: list[tuple[str, str, Sequence[int] | int]], *, fields: list[str], **_options: object,
    ) -> list[dict[str, object]]:
        """Return currently visible records in the projected fields and requested scope."""
        assert ("project_id", "=", 4) in domain
        requested: Sequence[int] | int = next(value for field, _operator, value in domain if field == "id")
        assert not isinstance(requested, int)
        return [
            {key: value for key, value in {
                "id": task_id, "name": "Текущее название", "active": True, "is_closed": False, "state": "01_in_progress",
                "date_deadline": current_deadline.strftime("%Y-%m-%d %H:%M:%S"), "user_ids": [],
            }.items() if key in fields}
            for task_id in requested if task_id in visible_tasks
        ]

    projects.search.side_effect = search_projects
    tasks.fields_get.side_effect = lambda *, allfields, **_options: dict.fromkeys(allfields)
    tasks.search_read.side_effect = search_tasks
    env.__getitem__.side_effect = {"project.project": projects, "project.task": tasks}.__getitem__
    reader: ModuleType = import_module(f"{package}.adapters.odoo_task_reader")
    monkeypatch.setattr(controller, "readable_projects", reader.readable_projects)
    for method in ("build_project_risk_summary", "read_summary_tasks", "_queue_summary"):
        monkeypatch.setattr(controller, method, Mock(side_effect=AssertionError("Detail must not generate a summary")))
    return SimpleNamespace(
        routes=controller.SummaryController(), saved=saved, latest=latest, env=env,
        old_deadline=old_deadline, current_deadline=current_deadline, checked_at=checked_at,
        members=members, visible_tasks=visible_tasks, tasks=tasks,
    )


def test_detail_reads_exact_result_and_separates_changed_deadline(risk_case: SimpleNamespace) -> None:
    """Follow the selected saved result despite a newer run and a rescheduled task."""
    _, summary, _ = risk_case.routes.risk_summary(project_id="4", expanded="1", result_id=risk_case.saved.result_id)
    assert summary["project_timezone"] == "UTC"
    query: dict[str, list[str]] = parse_qs(summary["detail_query"])
    assert query == {"result_id": [risk_case.saved.result_id], "expanded": ["1"]}
    assert risk_case.latest.result_id != risk_case.saved.result_id
    template, detail, options = risk_case.routes.risk_detail(4, 9, **{key: values[0] for key, values in query.items()})
    assert template.endswith(".risk_detail")
    assert detail["checked_at"] == risk_case.checked_at
    assert detail["project_timezone"] == "UTC"
    assert detail["row"].name == "Контрольная задача"
    assert detail["row"].deadline == risk_case.old_deadline
    assert detail["historical_deadline"].isoformat() == risk_case.old_deadline.astimezone(UTC).isoformat()
    assert detail["current_deadline"] == risk_case.current_deadline
    assert detail["row"].reasons[0].rule_id == "RR-01"
    assert detail["row"].reasons[0].overdue_days == 2
    assert [reason.rule_id for reason in detail["row"].reasons] == ["RR-01", "RR-03"]
    assert detail["task_url"] == "/odoo/project.task/9"
    back = urlsplit(detail["summary_url"])
    assert back.path == "/taskmaster/risk-summary"
    assert parse_qs(back.query) == query | {"project_id": ["4"]}
    assert dict(options["headers"])["Cache-Control"] == "private, no-store"


def test_analysis_route_is_opt_in_and_withholds_denied_output(
    risk_case: SimpleNamespace, controller: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise one authorized request without contacting a provider, then deny final release."""
    args: dict[str, object] = {"project_id": 4, "task_id": 9, "result_id": risk_case.saved.result_id, "question": "Что случилось?"}
    graph: Mock = Mock()
    monkeypatch.setattr(controller, "analyze_risk", graph)
    monkeypatch.delenv("TASKMASTER_ENABLE_RISK_ANALYSIS", raising=False)
    _, options = risk_case.routes.risk_analysis(**args)
    assert options["status"] == 404
    graph.assert_not_called()
    monkeypatch.setenv("TASKMASTER_ENABLE_RISK_ANALYSIS", "1")
    monkeypatch.setattr(controller, "_try_sync_slot", lambda _env: True)
    monkeypatch.setattr(controller, "prepare_risk_analysis", lambda *_a, **_kw: controller.PreparedRiskAnalysis(
        controller.PrepareStatus.READY, Mock(), Mock(), risk_case.saved, risk_case.saved.summary.rows[0],
    ))
    monkeypatch.setattr(controller, "load_settings", Mock())
    monkeypatch.setattr(controller, "OpenAICompatibleModelAdapter", Mock())
    explanation: SimpleNamespace = SimpleNamespace(text="Подтверждённый срок прошёл.", links=())
    graph.return_value = controller.RiskAnalysis(controller.AnalysisStatus.ANSWERED, explanation=explanation)
    _, detail, _ = risk_case.routes.risk_analysis(**args)
    assert detail["analysis"] is explanation
    assert detail["analysis_text"] == explanation.text
    assert graph.call_args.args == (args["question"],)
    graph.return_value = controller.RiskAnalysis(controller.AnalysisStatus.UNAVAILABLE)
    _, options = risk_case.routes.risk_analysis(**args)
    assert options["status"] == 404
    assert dict(options["headers"])["Cache-Control"] == "private, no-store"


def test_dialogue_keeps_failed_question_and_hides_unverified_answer(
    risk_case: SimpleNamespace, controller: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persist a failed question, then hide a prior answer before reuse and display."""
    for key, value in {"ENABLE_RISK_ANALYSIS": "1", "DIALOGUE_RETENTION_DAYS": "7", "DIALOGUE_WINDOW": "6"}.items():
        monkeypatch.setenv(f"TASKMASTER_{key}", value)
    risk_case.env.companies.ids = [3, 1]
    monkeypatch.setattr(controller, "_try_sync_slot", lambda _env: True)
    gate: Mock = Mock()
    gate.check_history.return_value = False
    monkeypatch.setattr(controller, "prepare_risk_analysis", lambda *_args, **_kwargs: controller.PreparedRiskAnalysis(
        controller.PrepareStatus.READY, Mock(), gate, risk_case.saved, risk_case.saved.summary.rows[0],
    ))
    monkeypatch.setattr(controller, "load_settings", Mock())
    monkeypatch.setattr(controller, "OpenAICompatibleModelAdapter", Mock())
    graph: Mock = Mock(return_value=controller.RiskAnalysis(controller.AnalysisStatus.FAILED))
    monkeypatch.setattr(controller, "analyze_risk", graph)
    key: str = risk_case.saved.result_id
    _, page, _ = risk_case.routes.risk_analysis(4, 9, result_id=key, question="Why?")
    dialogue_id: str = page["dialogue_id"]
    assert page["analysis_failed"] and page["dialogue_turns"] == [("Why?", None)]
    _, reopened, _ = risk_case.routes.risk_detail(4, 9, result_id=key, recover_dialogue="1")
    assert reopened["dialogue_id"] == dialogue_id and reopened["dialogue_turns"] == [("Why?", None)]
    assert reopened["analysis_failed"] and reopened["analysis_text"] is None
    store = controller.SQLiteDialogueStore(controller.state_path("qa").with_suffix(".dialogue.sqlite3"))
    scope = controller.DialogueScope(7, 4, 9, key, (3, 1))
    now: datetime = datetime.now(UTC)
    question, = store.recent_turns(dialogue_id, scope, now=now, window=6)
    store.record_answer(dialogue_id, question.turn_id, scope, answer="Previously visible evidence",
                        provenance='[1,[["read_task_card",null,null,null,"' + "a" * 64 + '"]]]',
                        now=now, ttl=timedelta(days=7))
    gate.check_history.return_value = True
    _, recovered, _ = risk_case.routes.risk_detail(4, 9, result_id=key, dialogue_id=dialogue_id)
    assert recovered["analysis_text"] == "Previously visible evidence"
    assert recovered["history_turns"] == []
    gate.check_history.return_value = False
    _, withheld, _ = risk_case.routes.risk_detail(4, 9, result_id=key, dialogue_id=dialogue_id)
    assert withheld["analysis_failed"] and withheld["analysis_text"] is None
    assert withheld["history_turns"] == []
    gate.check_history.return_value = True
    model_type: Mock = Mock()
    monkeypatch.setattr(controller, "RiskAnalysisModel", model_type)
    risk_case.routes.risk_analysis(4, 9, result_id=key, dialogue_id=dialogue_id, question="What changed?")
    assert model_type.call_args.kwargs["history"] == (("Why?", "Previously visible evidence"),)
    assert graph.call_args.kwargs["inherited"]
    gate.check_history.return_value = False
    gate.check_history.reset_mock()
    _, revoked, _ = risk_case.routes.risk_detail(4, 9, result_id=key, dialogue_id=dialogue_id)
    assert revoked["dialogue_turns"] == [("What changed?", None)]
    gate.check_history.assert_called_once()


def test_only_known_demo_answer_is_translated(controller: ModuleType) -> None:
    """Translate the exact placeholder without rewriting real model output."""
    assert controller._answer_text(controller._DEMO_ANSWER).startswith("Демонстрационный ответ")
    assert controller._answer_text("Реальный ответ") == "Реальный ответ"


def test_display_date_keeps_date_only_and_formats_utc(controller: ModuleType) -> None:
    """Keep a date distinct from an instant and avoid ISO text in the UI."""
    assert controller._display_date(date(2026, 11, 28)) == "28.11.2026"
    assert controller._display_date(datetime(2026, 11, 28, 14, 30, tzinfo=UTC)) == "28.11.2026 14:30 UTC"


def test_detail_does_not_substitute_current_deadline_for_missing_history(risk_case: SimpleNamespace) -> None:
    """Keep the unknown historical value even when Odoo has a current deadline."""
    _, detail, _ = risk_case.routes.risk_detail(4, 10, risk_case.saved.result_id)
    assert detail["row"].deadline is None and detail["row"].deadline_known is False
    assert detail["historical_deadline"] is None
    assert detail["current_deadline"] == risk_case.current_deadline and detail["current_deadline_known"] is True


@pytest.mark.parametrize("revoked", ["project", "task", "other_source", "deadline_field", "company", "recipient"])
def test_detail_after_revocation_matches_missing(
    controller: ModuleType, risk_case: SimpleNamespace, revoked: str,
) -> None:
    """Recheck the entire snapshot on reopen and never render a denied result."""
    routes = risk_case.routes
    result_id: str = risk_case.saved.result_id
    assert routes.risk_detail(4, 9, result_id)[0].endswith(".risk_detail")
    missing = routes.risk_detail(4, 9, "absent-result")
    assert routes.risk_detail(4, 9) == missing
    assert routes.risk_detail(5, 9, result_id) == missing
    assert routes.risk_detail(4, 12, result_id) == missing  # Visible task, but not a row in this snapshot.
    assert routes.risk_detail(4, 999, result_id) == missing
    if revoked == "project":
        risk_case.members.remove(7)
    elif revoked == "task":
        risk_case.visible_tasks.remove(9)
    elif revoked == "other_source":
        risk_case.visible_tasks.remove(10)
    elif revoked == "deadline_field":
        risk_case.tasks.fields_get.side_effect = lambda *, allfields, **_options: dict.fromkeys(
            field for field in allfields if field != "date_deadline"
        )
    elif revoked == "company":
        risk_case.env.user._get_company_ids = lambda: [1]
    else:
        risk_case.env.uid = 11
    controller.request.render.reset_mock()
    assert routes.risk_detail(4, 9, result_id) == missing
    assert missing == ("Карточка риска недоступна.", {
        "status": 404, "headers": (("Cache-Control", "private, no-store"), ("Vary", "Cookie")),
    })
    controller.request.render.assert_not_called()


def test_detail_storage_failure_keeps_404_and_logs_no_details(
    controller: ModuleType, risk_case: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Keep operational failures visible only as a sanitized warning."""
    missing = risk_case.routes.risk_detail(4, 9, "absent-result")
    failure: Exception = controller.StateStoreError()
    failure.__cause__ = OSError("private diagnostic")
    monkeypatch.setattr(controller, "_open_store", Mock(side_effect=failure))
    assert risk_case.routes.risk_detail(4, 9, risk_case.saved.result_id) == missing
    assert caplog.messages == ["Could not read or authorize the saved risk detail."]
    assert "private diagnostic" not in caplog.text
