"""Verify the server-side assignee prerequisite without publishing anything."""

from contextlib import nullcontext
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from adapters import odoo_draft_recipient as gate
from application.task_read_tools import ReadStatus, TaskAssignee


@pytest.mark.parametrize(("assigned", "visible", "expected"), [
    (True, True, True), (True, False, False), (False, True, False),
])
def test_selected_assignee_requires_assignment_and_own_access(
    monkeypatch: pytest.MonkeyPatch, assigned: bool, visible: bool, expected: bool,
) -> None:
    """Require both checks and do not use a foreign user's view for an unassigned selection."""
    exceptions: ModuleType = ModuleType("odoo.exceptions")
    exceptions.AccessError = PermissionError
    exceptions.MissingError = LookupError
    monkeypatch.setitem(sys.modules, "odoo.exceptions", exceptions)
    env: SimpleNamespace = SimpleNamespace(su=False, uid=5, cr=SimpleNamespace(dbname="synthetic"),
                                           companies=SimpleNamespace(ids=[3, 1]))
    manager: SimpleNamespace = SimpleNamespace(user=SimpleNamespace(has_group=Mock(return_value=True)))
    recipient: object = object()
    fresh: Mock = Mock(side_effect=[nullcontext(manager), nullcontext(recipient)])
    monkeypatch.setattr(gate, "_recipient_environment", fresh)
    manager_source: Mock = Mock()
    manager_source.card.return_value = ReadStatus.AVAILABLE, SimpleNamespace(
        assignees=(TaskAssignee(7 if assigned else 8, None),),
    )
    recipient_source: Mock = Mock()
    recipient_source.card.return_value = (
        (ReadStatus.AVAILABLE, object()) if visible else (ReadStatus.UNAVAILABLE, None)
    )
    source: Mock = Mock(side_effect=[manager_source, recipient_source])
    monkeypatch.setattr(gate, "OdooTaskSource", source)
    assert gate.can_address_assignee(env, project_id=4, task_id=9, recipient_id=7) is expected
    assert [call.args[1:] for call in fresh.call_args_list] == (
        [("synthetic", 5, (3, 1)), ("synthetic", 7, (3, 1))] if assigned else [("synthetic", 5, (3, 1))]
    )
