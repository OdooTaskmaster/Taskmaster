"""Reject unsafe verification targets before any synthetic ORM writes."""

import sys
from runpy import run_path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

functions = run_path("scripts/verify_task_read_tools.py")
validate = functions["_validate_target"]


@pytest.mark.parametrize("change", ["missing-db", "wrong-db", "missing-confirm", "wrong-shell", "not-admin", "dbfilter"])
def test_explicit_confirmation_is_required_before_orm(change: str) -> None:
    env = MagicMock(su=True)
    env.cr.dbname = "taskmaster_prod"
    values = {"TASKMASTER_VERIFY_DB": "taskmaster_prod", "TASKMASTER_VERIFY_CONFIRM": "DISPOSABLE_ROLLBACK_ONLY"}
    dbfilter = None
    if change == "missing-db":
        values.pop("TASKMASTER_VERIFY_DB")
    elif change == "wrong-db":
        values["TASKMASTER_VERIFY_DB"] = "other"
    elif change == "missing-confirm":
        values.pop("TASKMASTER_VERIFY_CONFIRM")
    elif change == "wrong-shell":
        env.cr.dbname = "other"
    elif change == "not-admin":
        env.su = False
    else:
        dbfilter = ".*"
    with pytest.raises(ValueError, match="Explicit disposable"):
        validate(env, expected_database="taskmaster_prod", environment=values, dbfilter=dbfilter)
    env.__getitem__.assert_not_called()


@pytest.mark.parametrize("model", ["project.project", "project.task", "res.users", "res.company"])
def test_even_confirmed_target_rejects_non_bootstrap_business_records(model: str) -> None:
    models = {name: MagicMock() for name in ("ir.model.data", "project.project", "project.task", "res.users", "res.company")}
    for value in models.values():
        value.with_context.return_value = value
        value.search_count.return_value = 0
    models["ir.model.data"].search.return_value.mapped.return_value = [1]
    models[model].search_count.return_value = 1
    env = MagicMock(su=True)
    env.cr.dbname = "test"
    env.__getitem__.side_effect = models.__getitem__
    with pytest.raises(ValueError, match=f"model={model}, count=1"):
        validate(env, expected_database="test", environment={
            "TASKMASTER_VERIFY_DB": "test", "TASKMASTER_VERIFY_CONFIRM": "DISPOSABLE_ROLLBACK_ONLY",
        }, dbfilter=None)
    for value in models.values():
        value.create.assert_not_called()


def test_confirmed_bootstrap_database_is_allowed() -> None:
    env = MagicMock(su=True)
    env.cr.dbname = "test"
    env.__getitem__.return_value.with_context.return_value.search_count.return_value = 0
    validate(env, expected_database="test", environment={
        "TASKMASTER_VERIFY_DB": "test", "TASKMASTER_VERIFY_CONFIRM": "DISPOSABLE_ROLLBACK_ONLY",
    }, dbfilter=None)
    env.__getitem__.return_value.create.assert_not_called()
    for call in env.__getitem__.return_value.search.call_args_list:
        domain = call.args[0]
        assert ("module", "=", "base") in domain
        assert ("model", "=", "project.task") not in domain
        assert ("model", "=", "project.project") not in domain
        assert any(term[0] == "name" and term[1] == "in" for term in domain)


@pytest.mark.parametrize("identifier", ["0", "-1", "", "other", "1;2"])
def test_onboarding_exception_rejects_invalid_ids_before_orm(identifier: str) -> None:
    env = MagicMock()
    with pytest.raises(ValueError, match="Invalid explicit"):
        functions["_validate_onboarding_task"](env, identifier)
    env.__getitem__.assert_not_called()


@pytest.mark.parametrize("change", [None, "project", "parent", "creator", "owner", "title", "body", "template"])
def test_only_explicit_unmodified_onboarding_task_is_allowed(monkeypatch: pytest.MonkeyPatch, change: str | None) -> None:
    odoo = ModuleType("odoo")
    odoo.SUPERUSER_ID = 1
    tools = ModuleType("odoo.tools")
    tools.html_sanitize = lambda value: "sanitized " + value
    monkeypatch.setitem(sys.modules, "odoo", odoo)
    monkeypatch.setitem(sys.modules, "odoo.tools", tools)
    env = MagicMock()
    env.ref.return_value.ids = [2]
    task = env.__getitem__.return_value.browse.return_value.exists.return_value
    task.id = 1
    task.project_id = False
    task.parent_id = False
    task.create_uid.id = 1
    task.user_ids.ids = [2]
    task.name = "Welcome Administrator!"
    task.description = "sanitized body"
    env.return_value._.return_value = "Welcome Administrator!"
    env.return_value.__getitem__.return_value._render.return_value = "body"
    if change == "project":
        task.project_id = True
    elif change == "parent":
        task.parent_id = True
    elif change == "creator":
        task.create_uid.id = 2
    elif change == "owner":
        task.user_ids.ids = [3]
    elif change == "title":
        task.name = "Changed"
    elif change == "body":
        task.description = "Changed"
    elif change == "template":
        env.return_value.__getitem__.return_value._render.return_value = ""
    if change is None:
        assert functions["_validate_onboarding_task"](env, "1") == 1
    else:
        with pytest.raises(ValueError, match="not the unmodified"):
            functions["_validate_onboarding_task"](env, "1")
    task.write.assert_not_called()
    task.unlink.assert_not_called()
