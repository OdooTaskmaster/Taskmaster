"""Verify the Odoo reader boundary without importing a live Odoo runtime."""

import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from adapters.odoo_task_reader import read_project_tasks


class AccessError(Exception):
    """Represent fake ORM denial; AccessError('denied') constructs a test error."""


class MissingError(Exception):
    """Represent fake missing records; MissingError('gone') constructs a test error."""


@pytest.fixture
def orm(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Supply a mocked recipient ORM with a visible project and readable names.

    Example: a test declaring orm receives (env, projects, tasks).
    """
    odoo: ModuleType = ModuleType("odoo")
    exceptions: ModuleType = ModuleType("odoo.exceptions")
    monkeypatch.setattr(odoo, "SUPERUSER_ID", 1, raising=False)
    monkeypatch.setattr(exceptions, "AccessError", AccessError, raising=False)
    monkeypatch.setattr(exceptions, "MissingError", MissingError, raising=False)
    monkeypatch.setitem(sys.modules, "odoo", odoo)
    monkeypatch.setitem(sys.modules, "odoo.exceptions", exceptions)

    projects: MagicMock = MagicMock()
    tasks: MagicMock = MagicMock()
    tasks.fields_get.return_value = {"name": {"type": "char"}}
    env: MagicMock = MagicMock(su=False, uid=7)
    env.user.has_group.return_value = False
    models: dict[str, MagicMock] = {"project.project": projects, "project.task": tasks}
    env.__getitem__.side_effect = models.__getitem__
    return env, projects, tasks


def test_member_project_and_minimal_fields(orm: tuple[MagicMock, MagicMock, MagicMock]) -> None:
    """Assert the exact ORM scope and response projection for the supplied fake ORM.

    Example: pytest injects orm; this test returns None on success.
    """
    env, projects, tasks = orm
    tasks.search_read.return_value = [{"id": 11, "name": "Demo", "description": "Not requested"}]

    rows: tuple[dict[str, object], ...] | None = read_project_tasks(
        env, project_id=4, fields=("name", "user_ids", "description", "name"),
        task_ids=(11, 12, 13, 14, 99),
    )

    env.user.has_group.assert_called_once_with("project.group_project_manager")
    projects.search.assert_called_once_with([("id", "=", 4), ("allowed_internal_user_ids", "in", [7])], limit=1)
    tasks.fields_get.assert_called_once_with(allfields=("name", "user_ids"), attributes=("type",))
    tasks.search_read.assert_called_once_with(
        [("project_id", "=", 4), ("id", "in", [11, 12, 13, 14, 99])], fields=["name"], order="id", load=None,
    )
    assert rows == ({"name": "Demo"},)


def test_hidden_missing_and_revoked_tasks(orm: tuple[MagicMock, MagicMock, MagicMock]) -> None:
    """Assert fresh authorization and indistinguishable missing and hidden IDs.

    Example: pytest injects orm; this test returns None on success.
    """
    env, projects, tasks = orm
    tasks.search_read.side_effect = ([{"id": 11, "name": "Demo"}], [], [])

    assert read_project_tasks(env, project_id=4, fields=("name",), task_ids=(11,)) == ({"name": "Demo"},)
    assert read_project_tasks(env, project_id=4, fields=("name",), task_ids=(12,)) == ()
    assert read_project_tasks(env, project_id=4, fields=("name",), task_ids=(99,)) == ()
    projects.search.return_value = False
    assert read_project_tasks(env, project_id=4, fields=("name",), task_ids=(11,)) == ()
    assert projects.search.call_count == 4
    assert tasks.search_read.call_count == 3


@pytest.mark.parametrize(("su", "uid"), [(True, 7), (False, 1)])
def test_reject_privileged_environment(orm: tuple[MagicMock, MagicMock, MagicMock], su: bool, uid: int) -> None:
    """Reject elevation or superuser identity on a fake environment.

    Example: test_reject_privileged_environment(orm, True, 7) returns None on success.
    """
    env, _, _ = orm
    env.su, env.uid = su, uid
    assert read_project_tasks(env, project_id=4, fields=("name",)) is None
    env.__getitem__.assert_not_called()
    env.user.has_group.assert_not_called()


@pytest.mark.parametrize("error", [AccessError, MissingError, None])
def test_unavailable_project_has_no_rows(
    orm: tuple[MagicMock, MagicMock, MagicMock], error: type[Exception] | None,
) -> None:
    """Return no rows for an empty project search or a missing/denied project.

    Example: test_unavailable_project_has_no_rows(orm, AccessError) returns None on success.
    """
    env, projects, tasks = orm
    projects.search.side_effect = error
    projects.search.return_value = False
    assert read_project_tasks(env, project_id=4, fields=("name",)) == ()
    tasks.search_read.assert_not_called()


@pytest.mark.parametrize("project_visible", [True, False])
def test_manager_skips_membership_not_project_access(
    orm: tuple[MagicMock, MagicMock, MagicMock], project_visible: bool,
) -> None:
    """Test the manager branch for a visible or ORM-hidden project.

    Example: test_manager_skips_membership_not_project_access(orm, False)
    returns None on success without querying tasks.
    """
    env, projects, tasks = orm
    env.user.has_group.return_value = True
    projects.search.return_value = project_visible
    tasks.search_read.return_value = [{"name": "Demo"}]
    expected: tuple[dict[str, object], ...] = ({"name": "Demo"},) if project_visible else ()
    assert read_project_tasks(env, project_id=4, fields=("name",)) == expected
    projects.search.assert_called_once_with([("id", "=", 4)], limit=1)
    if project_visible:
        tasks.search_read.assert_called_once_with([("project_id", "=", 4)], fields=["name"], order="id", load=None)
    else:
        tasks.search_read.assert_not_called()


def test_no_readable_fields_never_fetches_all_fields(orm: tuple[MagicMock, MagicMock, MagicMock]) -> None:
    """Stop before search_read when no requested field survives field checks.

    Example: pytest injects orm; this test returns None on success.
    """
    env, _, tasks = orm
    assert read_project_tasks(env, project_id=4, fields=("user_ids",)) == ()
    tasks.search_read.assert_not_called()


@pytest.mark.parametrize("error", [AccessError, MissingError])
def test_task_read_failure_returns_no_partial_rows(
    orm: tuple[MagicMock, MagicMock, MagicMock], error: type[Exception],
) -> None:
    """Return unavailable for either ORM denial or disappearance during reading.

    Example: test_task_read_failure_returns_no_partial_rows(orm, AccessError) returns None on success.
    """
    env, _, tasks = orm
    tasks.search_read.side_effect = error
    assert read_project_tasks(env, project_id=4, fields=("name",)) is None
