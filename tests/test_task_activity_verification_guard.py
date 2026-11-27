"""Reject accidental real-database targets before activity fixtures are created."""

from unittest.mock import MagicMock

import pytest
from scripts.verify_task_activity import DISPOSABLE_MARKER, _validate_target

NAME = "taskmaster_activity_probe_test"


@pytest.fixture
def target() -> tuple[MagicMock, dict[str, str]]:
    """Build a marked explicit disposable shell; e.g. env, values = target."""
    env = MagicMock(su=True)
    env.cr.dbname = NAME
    env.cr.fetchone.return_value = (DISPOSABLE_MARKER,)
    return env, {"TASKMASTER_VERIFY_DB": NAME, "TASKMASTER_VERIFY_CONFIRM": "DISPOSABLE_ROLLBACK_ONLY"}


def test_explicit_marked_database_allowed(target: tuple[MagicMock, dict[str, str]]) -> None:
    env, values = target
    _validate_target(env, NAME, values, None)
    env.cr.execute.assert_called_once()
    env.__getitem__.assert_not_called()


@pytest.mark.parametrize("key,value", [("TASKMASTER_VERIFY_DB", "other"), ("TASKMASTER_VERIFY_CONFIRM", "yes")])
def test_confirmation_rejected_before_sql(target: tuple[MagicMock, dict[str, str]], key: str, value: str) -> None:
    env, values = target
    values[key] = value
    with pytest.raises(ValueError):
        _validate_target(env, NAME, values, None)
    env.cr.execute.assert_not_called()


@pytest.mark.parametrize("marker", [None, (None,), (), ("real database",)])
def test_missing_or_wrong_marker_rejected(target: tuple[MagicMock, dict[str, str]], marker: object) -> None:
    env, values = target
    env.cr.fetchone.return_value = marker
    with pytest.raises(ValueError, match="marker"):
        _validate_target(env, NAME, values, None)


def test_real_database_name_rejected(target: tuple[MagicMock, dict[str, str]]) -> None:
    """A marker on a real-named database must still be rejected before SQL."""
    env, values = target
    env.cr.dbname = "production"
    with pytest.raises(ValueError):
        _validate_target(env, "production", values, None)
    env.cr.execute.assert_not_called()


@pytest.mark.parametrize("setting", ["su", "dbname", "dbfilter", "prefix"])
def test_unsafe_shell_rejected(target: tuple[MagicMock, dict[str, str]], setting: str) -> None:
    env, values = target
    name = NAME
    if setting == "su":
        env.su = False
    if setting == "dbname":
        env.cr.dbname = "other"
    if setting == "prefix":
        name = "production"
        env.cr.dbname = name
        values["TASKMASTER_VERIFY_DB"] = name
    with pytest.raises(ValueError):
        _validate_target(env, name, values, ".*" if setting == "dbfilter" else None)
    env.cr.execute.assert_not_called()
