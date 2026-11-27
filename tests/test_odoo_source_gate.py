"""Verify fresh cursor wiring without presenting mocked rights as real Odoo ACL checks."""

import sys
from datetime import UTC
from types import ModuleType
from unittest.mock import MagicMock

import pytest
from adapters import odoo_source_gate as adapter
from application.reading_plan import ContextItem, ReadStep
from application.task_read_map import ReadTool
from application.task_read_tools import ReadStatus
from scripts.verify_reading_source_gate import _validate_commit_confirmation

ITEM: ContextItem = ContextItem(ReadStep(ReadTool.CARD), "{}", ())


@pytest.fixture
def fresh(monkeypatch: pytest.MonkeyPatch) -> tuple:
    """Replace only Odoo environment construction and narrow reading; e.g. verifier, env, factory, read = fresh."""
    api: ModuleType = ModuleType("odoo.api")
    factory: MagicMock = MagicMock()
    api.Environment = factory
    monkeypatch.setitem(sys.modules, "odoo.api", api)
    env: MagicMock = MagicMock(su=False, uid=7)
    env.companies.ids = [2]
    env.registry.cursor.return_value.__enter__.return_value = MagicMock()
    reader: MagicMock = MagicMock(return_value=(ReadStatus.AVAILABLE, ITEM))
    monkeypatch.setattr(adapter, "read_item", reader)
    monkeypatch.setattr(adapter, "OdooTaskSource", MagicMock())
    verifier = adapter.OdooContextVerifier(env, project_id=4, task_id=9, timezone=UTC)
    return verifier, env, factory, reader


def test_independent_cursor_environment_and_all_receipts(fresh: tuple) -> None:
    verifier, env, factory, reader = fresh
    assert verifier.check((ITEM, ITEM)) is True
    cursor = env.registry.cursor.return_value.__enter__.return_value
    factory.assert_called_once_with(cursor, 7, {"allowed_company_ids": [2]}, su=False)
    assert factory.call_args.args[0] is not env.cr
    assert reader.call_count == 2
    cursor.rollback.assert_called_once()
    adapter.OdooTaskSource.assert_called_once_with(factory.return_value, project_id=4, task_id=9, timezone=UTC)


@pytest.mark.parametrize("status,current", [
    (ReadStatus.UNAVAILABLE, None), (ReadStatus.FAILED, None),
    (ReadStatus.AVAILABLE, ContextItem(ITEM.step, '{"changed":true}', ())),
])
def test_revoked_or_changed_receipt_denies_entire_batch(fresh: tuple, status: ReadStatus, current: object) -> None:
    verifier, _, _, reader = fresh
    reader.side_effect = [(ReadStatus.AVAILABLE, ITEM), (status, current)]
    assert verifier.check((ITEM, ITEM)) is False
    assert reader.call_count == 2


def test_cursor_error_never_authorizes(fresh: tuple) -> None:
    verifier, env, _, _ = fresh
    env.registry.cursor.side_effect = RuntimeError("sensitive")
    assert verifier.check((ITEM,)) is False


@pytest.mark.parametrize("items", [(), [], None, ("untrusted",)])
def test_invalid_provenance_never_opens_cursor(fresh: tuple, items: object) -> None:
    verifier, env, _, _ = fresh
    assert verifier.check(items) is False
    env.registry.cursor.assert_not_called()


@pytest.mark.parametrize("setting", ["su", "root", "project", "task"])
def test_elevated_or_invalid_scope_rejected(setting: str) -> None:
    env: MagicMock = MagicMock(su=setting == "su", uid=1 if setting == "root" else 7)
    with pytest.raises(ValueError):
        adapter.OdooContextVerifier(env, project_id=True if setting == "project" else 4,
                                   task_id=0 if setting == "task" else 9, timezone=UTC)


def test_only_safe_language_and_timezone_context_retained(fresh: tuple) -> None:
    _, env, factory, _ = fresh
    env.context = {"lang": "ru_RU", "tz": "Europe/Moscow", "unsafe": "not copied"}
    verifier = adapter.OdooContextVerifier(env, project_id=4, task_id=9, timezone=UTC)
    assert verifier.check((ITEM,)) is True
    assert factory.call_args.args[2] == {"allowed_company_ids": [2], "lang": "ru_RU", "tz": "Europe/Moscow"}


@pytest.mark.parametrize("confirmation", [None, "DISPOSABLE_ROLLBACK_ONLY", "yes"])
def test_commit_verifier_requires_additional_explicit_consent(confirmation: object) -> None:
    with pytest.raises(ValueError, match="synthetic commits"):
        _validate_commit_confirmation({"TASKMASTER_VERIFY_COMMIT_CONFIRM": confirmation})


def test_explicit_synthetic_commit_confirmation_allowed() -> None:
    _validate_commit_confirmation({"TASKMASTER_VERIFY_COMMIT_CONFIRM": "DISPOSABLE_SYNTHETIC_COMMIT_AND_CLEANUP"})
