"""Check the overloaded HTTP response against durable SQLite acceptance."""

from collections.abc import Iterator
from importlib import import_module
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def controller(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[ModuleType]:
    """Load the route with an Odoo double, returning the configured route module."""
    addon: ModuleType = ModuleType("_test_summary_route_addon")
    addon.__path__ = [str(Path(__file__).resolve().parents[1])]
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
    for module in (addon, http, exceptions, tools):
        monkeypatch.setitem(sys.modules, module.__name__, module)

    summary: ModuleType = import_module(f"{addon.__name__}.controllers.summary")
    env: SimpleNamespace = SimpleNamespace(
        cr=SimpleNamespace(dbname="qa"), uid=7, su=False,
        user=SimpleNamespace(active=True, has_group=lambda group: group == "base.group_user"),
    )
    request: SimpleNamespace = SimpleNamespace(env=env, make_json_response=Mock())
    request.make_json_response.side_effect = lambda data, **options: (data, options)
    monkeypatch.setattr(summary, "request", request)
    monkeypatch.setattr(summary, "_company_scope", lambda: (3, 1))
    monkeypatch.setattr(summary, "_try_sync_slot", lambda _env: False)
    monkeypatch.setattr(summary, "readable_projects", lambda _env, project: project == 4)
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


def test_overload_never_returns_202_when_sqlite_cannot_accept(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return a neutral 503 without a status link when the durable write fails."""
    monkeypatch.setattr(controller, "SQLiteStateStore", Mock(side_effect=controller.StateStoreError()))
    data, options = controller.SummaryController().generate_summary("4")
    assert options["status"] == 503 and data == {"error": "generation_unavailable"}
    assert "Location" not in dict(options["headers"])
