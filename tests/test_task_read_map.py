"""Attack map scope, metadata-only probing and malformed capability contracts."""

# ruff: noqa: F811 -- Import the shared pytest ORM fixture by name.

from dataclasses import asdict
from datetime import UTC
from unittest.mock import MagicMock

import pytest
from adapters.odoo_task_tools import OdooTaskSource
from application.task_read_map import ReadScenario, ReadTool, TaskReadMap
from application.task_read_tools import ReadStatus
from test_odoo_task_tools import AccessError, orm  # noqa: F401


@pytest.fixture
def capability(orm: tuple[MagicMock, dict[str, MagicMock]]) -> tuple[OdooTaskSource, dict[str, MagicMock]]:
    """Build five permitted metadata branches; e.g. source, models = capability."""
    env, models = orm
    for name in ("account.analytic.line", "uom.uom"):
        models[name] = MagicMock()
        models[name].fields_get.side_effect = lambda *, allfields, attributes: {field: {"type": "x"} for field in allfields}
    for model in models.values():
        model.has_access.return_value = True
    models["project.project"].search_read.return_value = [{"allow_timesheets": True}]
    return OdooTaskSource(env, project_id=4, task_id=9, timezone=UTC), models


@pytest.mark.parametrize("scenario", list(ReadScenario))
def test_exact_map_and_limits_without_data(
    capability: tuple[OdooTaskSource, dict[str, MagicMock]], scenario: ReadScenario,
) -> None:
    source, models = capability
    status, result = TaskReadMap(source, scenario=scenario).build()
    assert status == ReadStatus.AVAILABLE and result is not None and result.scenario == scenario
    assert tuple(branch.tool for branch in result.branches) == tuple(ReadTool)
    card, description, history, comments, worklog = result.branches
    assert card.parameters == description.parameters == ()
    assert description.max_text_chars == 4000
    assert history.parameters == worklog.parameters == ("start", "end")
    assert history.max_period_days == comments.max_period_days == worklog.max_period_days == 90
    assert (history.max_rows, history.max_tracking_values_per_field) == (500, 1000)
    assert (comments.parameters, comments.max_fragments, comments.max_text_chars) == (("start", "end", "limit"), 20, 2000)
    assert worklog.max_rows == 1000
    for name in ("mail.message", "mail.tracking.value", "account.analytic.line", "uom.uom", "res.users"):
        models[name].search.assert_not_called()
        models[name].search_read.assert_not_called()
        models[name].search_count.assert_not_called()
    for model in models.values():
        model.sudo.assert_not_called()
    assert models["project.task"].search_read.call_args.kwargs["fields"] == ["id"]
    assert models["project.project"].search_read.call_args.kwargs["fields"] == ["allow_timesheets"]
    assert "source" not in repr(asdict(result))


@pytest.mark.parametrize("scenario", list(ReadScenario))
@pytest.mark.parametrize("policy", [frozenset(), frozenset({ReadTool.CARD, ReadTool.COMMENTS})])
def test_server_scenario_policy_can_only_narrow(
    capability: tuple[OdooTaskSource, dict[str, MagicMock]], scenario: ReadScenario, policy: frozenset[ReadTool],
) -> None:
    source, _ = capability
    status, result = TaskReadMap(source, scenario=scenario, allowed_tools=policy).build()
    assert status == ReadStatus.AVAILABLE and result is not None
    assert {branch.tool for branch in result.branches} == policy


@pytest.mark.parametrize("model,missing,removed", [
    ("project.task", "description", {ReadTool.DESCRIPTION}),
    ("mail.message", "body", {ReadTool.COMMENTS}),
    ("mail.message", "date", {ReadTool.COMMENTS, ReadTool.HISTORY}),
    ("account.analytic.line", "project_id", {ReadTool.WORKLOG}),
    ("account.analytic.line", "task_id", {ReadTool.WORKLOG}),
    ("project.project", "company_id", {ReadTool.WORKLOG}),
    ("uom.uom", "name", {ReadTool.WORKLOG}),
])
def test_hidden_required_fields_remove_exact_branches(
    capability: tuple[OdooTaskSource, dict[str, MagicMock]], model: str, missing: str, removed: set[ReadTool],
) -> None:
    source, models = capability
    models[model].fields_get.side_effect = lambda *, allfields, attributes: {
        field: {"type": "x"} for field in allfields if field != missing
    }
    status, tools = source.read_types()
    assert status == ReadStatus.AVAILABLE and set(tools) == set(ReadTool) - removed


@pytest.mark.parametrize("model,removed", [
    ("mail.message", {ReadTool.COMMENTS, ReadTool.HISTORY}),
    ("account.analytic.line", {ReadTool.WORKLOG}),
    ("uom.uom", {ReadTool.WORKLOG}),
])
def test_model_acl_denial_removes_branches(
    capability: tuple[OdooTaskSource, dict[str, MagicMock]], model: str, removed: set[ReadTool],
) -> None:
    source, models = capability
    models[model].has_access.return_value = False
    status, tools = source.read_types()
    assert status == ReadStatus.AVAILABLE and set(tools) == set(ReadTool) - removed
    models[model].fields_get.assert_not_called()


@pytest.mark.parametrize("model,removed", [
    ("mail.message", {ReadTool.COMMENTS, ReadTool.HISTORY}),
    ("mail.tracking.value", {ReadTool.HISTORY}),
    ("account.analytic.line", {ReadTool.WORKLOG}),
    ("uom.uom", {ReadTool.WORKLOG}),
])
def test_optional_models_absent(
    capability: tuple[OdooTaskSource, dict[str, MagicMock]], model: str, removed: set[ReadTool],
) -> None:
    source, models = capability
    del models[model]
    status, tools = source.read_types()
    assert status == ReadStatus.AVAILABLE and set(tools) == set(ReadTool) - removed


def test_disabled_timesheets_and_unreadable_tracking_fields(capability: tuple[OdooTaskSource, dict[str, MagicMock]]) -> None:
    source, models = capability
    models["project.project"].search_read.return_value = [{"allow_timesheets": False}]
    models["project.task"].fields_get.side_effect = lambda *, allfields, attributes: {
        field: {"type": "x"} for field in allfields if field not in {"stage_id", "state"}
    }
    assert source.read_types() == (ReadStatus.AVAILABLE, (ReadTool.CARD, ReadTool.DESCRIPTION, ReadTool.COMMENTS))


def test_history_available_with_only_state_readable(capability: tuple[OdooTaskSource, dict[str, MagicMock]]) -> None:
    source, models = capability
    models["project.task"].fields_get.side_effect = lambda *, allfields, attributes: {
        field: {"type": "x"} for field in allfields if field != "stage_id"
    }
    assert source.read_types() == (ReadStatus.AVAILABLE, tuple(ReadTool))


def test_existing_map_does_not_authorize_later_read(capability: tuple[OdooTaskSource, dict[str, MagicMock]]) -> None:
    source, models = capability
    assert TaskReadMap(source, scenario=ReadScenario.RISK_ANALYSIS).build()[0] == ReadStatus.AVAILABLE
    models["project.project"].search.return_value = False
    assert source.card() == (ReadStatus.UNAVAILABLE, None)


def test_scope_revoked_between_calls(capability: tuple[OdooTaskSource, dict[str, MagicMock]]) -> None:
    source, models = capability
    reader = TaskReadMap(source, scenario=ReadScenario.RISK_ANALYSIS)
    assert reader.build()[0] == ReadStatus.AVAILABLE
    models["project.project"].search.return_value = False
    assert reader.build() == (ReadStatus.UNAVAILABLE, None)


def test_missing_task_is_unavailable(capability: tuple[OdooTaskSource, dict[str, MagicMock]]) -> None:
    source, models = capability
    models["project.task"].search_read.return_value = []
    assert source.read_types() == (ReadStatus.UNAVAILABLE, None)


def test_metadata_failure_has_no_partial_map(capability: tuple[OdooTaskSource, dict[str, MagicMock]]) -> None:
    source, models = capability
    models["mail.message"].fields_get.side_effect = AccessError("sensitive")
    assert TaskReadMap(source, scenario=ReadScenario.DRAFT).build() == (ReadStatus.FAILED, None)


@pytest.mark.parametrize("value", [None, [], ("read_task_card",), (ReadTool.CARD, ReadTool.CARD), (42,)])
def test_application_rejects_malformed_source(value: object) -> None:
    source = MagicMock()
    source.read_types.return_value = (ReadStatus.AVAILABLE, value)
    assert TaskReadMap(source, scenario=ReadScenario.DRAFT).build() == (ReadStatus.FAILED, None)


@pytest.mark.parametrize("status", [ReadStatus.UNAVAILABLE, ReadStatus.FAILED])
def test_nonavailable_source_payload_discarded(status: ReadStatus) -> None:
    source = MagicMock()
    source.read_types.return_value = (status, (ReadTool.CARD,))
    assert TaskReadMap(source, scenario=ReadScenario.DRAFT).build() == (status, None)


def test_invalid_server_configuration_rejected_before_access() -> None:
    source = MagicMock()
    with pytest.raises(TypeError):
        TaskReadMap(source, scenario="draft")
    with pytest.raises(TypeError):
        TaskReadMap(source, scenario=ReadScenario.DRAFT, allowed_tools=frozenset({"read_task_card"}))
    source.read_types.assert_not_called()


@pytest.mark.parametrize("su,uid", [(True, 7), (False, 1)])
def test_elevated_environment_denied(orm: tuple[MagicMock, dict[str, MagicMock]], su: bool, uid: int) -> None:
    env, models = orm
    env.su, env.uid = su, uid
    assert OdooTaskSource(env, project_id=4, task_id=9, timezone=UTC).read_types() == (ReadStatus.UNAVAILABLE, None)
    models["project.project"].search.assert_not_called()


def test_model_cannot_override_scope_or_policy(capability: tuple[OdooTaskSource, dict[str, MagicMock]]) -> None:
    source, _ = capability
    with pytest.raises(TypeError):
        TaskReadMap(source, scenario=ReadScenario.DRAFT).build(task_id=42)
