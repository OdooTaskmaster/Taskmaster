"""Check the Odoo worker runner, trusted scope and launcher loop."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from adapters import odoo_background as background
from adapters.sqlite_state_store import SQLiteStateStore
from application.operation_store import Operation, OperationState
from application.risk_summary import ProjectRiskSummary, SummaryStatus
from application.summary_store import SavedSummary

_SCRIPT: Path = Path(__file__).resolve().parents[1] / "scripts" / "run_background_worker.py"
_SPEC = importlib.util.spec_from_file_location("run_background_worker", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
worker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(worker)


@pytest.fixture
def execution_denied(monkeypatch: pytest.MonkeyPatch) -> type[Exception]:
    """Provide the application denial type, stubbing it until the parent module lands.

    Example: a test wraps a denied call with pytest.raises(execution_denied).
    """
    try:
        from application.background_operations import ExecutionDenied
    except ModuleNotFoundError:
        class ExecutionDenied(Exception):
            """Stand in for the application denial while its module is absent."""

        module: ModuleType = ModuleType("application.background_operations")
        module.ExecutionDenied = ExecutionDenied
        monkeypatch.setitem(sys.modules, "application.background_operations", module)
        return ExecutionDenied
    return ExecutionDenied


def _install_odoo(monkeypatch: pytest.MonkeyPatch, *, active: bool = True, internal: bool = True,
                  allowed: tuple[int, ...] = (1, 3)) -> SimpleNamespace:
    """Install a minimal Odoo module pair and return a trusted shell environment.

    Example: _install_odoo(monkeypatch) returns env for database 'pilot'.
    """
    odoo: ModuleType = ModuleType("odoo")
    odoo.SUPERUSER_ID = 1
    exceptions: ModuleType = ModuleType("odoo.exceptions")
    exceptions.AccessError = type("AccessError", (Exception,), {})
    exceptions.MissingError = type("MissingError", (Exception,), {})

    class _Cursor:
        def __init__(self, dbname: str) -> None:
            self.dbname: str = dbname

        def __enter__(self) -> "_Cursor":
            return self

        def __exit__(self, *_error: object) -> bool:
            return False

        def rollback(self) -> None:
            """Discard any accidental Odoo writes; rollback() returns None."""

    class _Registry:
        def __init__(self, dbname: str) -> None:
            self._dbname: str = dbname

        def cursor(self) -> _Cursor:
            return _Cursor(self._dbname)

    class _Environment:
        def __init__(self, cursor: _Cursor, uid: int, _context: dict[str, object], su: bool = False) -> None:
            self.cr: _Cursor = cursor
            self.uid: int = uid
            self.su: bool = su
            self.context: dict[str, object] = _context
            self.user = SimpleNamespace(
                active=active,
                has_group=lambda group: internal and group == "base.group_user",
            )

        @property
        def companies(self) -> SimpleNamespace:
            """Return selected IDs or raise if one is no longer authorized."""
            ids: list[int] = list(self.context.get("allowed_company_ids", allowed))
            if not set(ids).issubset(allowed):
                raise exceptions.AccessError()
            return SimpleNamespace(ids=ids)

    api: ModuleType = ModuleType("odoo.api")
    api.Environment = _Environment
    monkeypatch.setitem(sys.modules, "odoo", odoo)
    monkeypatch.setitem(sys.modules, "odoo.api", api)
    monkeypatch.setitem(sys.modules, "odoo.exceptions", exceptions)
    return SimpleNamespace(cr=SimpleNamespace(dbname="pilot"), registry=_Registry("pilot"))


def _open_recipient(recipient: object) -> object:
    """Return a context manager that yields the supplied fake recipient.

    Example: monkeypatch uses _open_recipient(SimpleNamespace()) as an opener.
    """
    @contextmanager
    def opened(_user_id: int) -> Iterator[object]:
        yield recipient

    return opened


def _operation(now: datetime, *, owner_id: int = 7, recipient_id: int = 7, project_id: int = 3) -> Operation:
    """Return a running operation with explicit scope and timestamps.

    Example: _operation(datetime(2026, 11, 27, tzinfo=UTC)) returns op-1.
    """
    return Operation("op-1", owner_id, project_id, recipient_id, OperationState.RUNNING, now, now)


def test_queue_name_is_stable_and_per_database() -> None:
    """Assert the queue name is the trusted database hash; return None on success."""
    assert background.queue_name("pilot") == f"taskmaster.{sha256(b'pilot').hexdigest()}"
    assert background.queue_name("pilot") == background.queue_name("pilot")
    assert background.queue_name("pilot") != background.queue_name("other")
    with pytest.raises(ValueError):
        background.queue_name("")


def test_database_state_path_uses_the_sync_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assert the worker shares controllers.summary.state_path; return None on success."""
    summary: ModuleType = ModuleType("controllers.summary")
    summary.state_path = lambda database: Path("shared") / f"{database}.sqlite3"
    controllers: ModuleType = ModuleType("controllers")
    controllers.summary = summary
    monkeypatch.setitem(sys.modules, "controllers", controllers)
    monkeypatch.setitem(sys.modules, "controllers.summary", summary)

    assert background.database_state_path("pilot") == Path("shared") / "pilot.sqlite3"
    with pytest.raises(ValueError):
        background.database_state_path("")


def test_runner_saves_summary_under_the_operation_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Assert a fresh non-elevated run saves one result scoped by operation ID; return None."""
    store: SQLiteStateStore = SQLiteStateStore(tmp_path / "state.sqlite3")
    now: datetime = datetime(2026, 11, 27, tzinfo=UTC)
    recipient: object = SimpleNamespace(uid=7)
    monkeypatch.setattr(background, "_recipient_environment", lambda _env, _db, uid, _companies: _open_recipient(recipient)(uid))
    monkeypatch.setattr(background, "readable_projects", Mock(return_value=object()))
    monkeypatch.setattr(background, "read_summary_tasks", Mock(return_value=()))
    env: SimpleNamespace = SimpleNamespace(cr=SimpleNamespace(dbname="pilot"))
    runner: background.OdooSummaryRunner = background.OdooSummaryRunner(
        store, env, "pilot", project_timezone=UTC, clock=lambda: now,
    )

    result_id: str = runner(_operation(now))
    saved: SavedSummary | None = store.get_summary(
        result_id, project_id=3, recipient_id=7, authorize=lambda _snapshot: True,
    )

    assert saved is not None
    assert saved.run_id == "op-1"
    assert saved.checked_at == now
    assert saved.summary.status is SummaryStatus.EMPTY


@pytest.mark.parametrize("manager", [True, False])
def test_runner_checks_role_and_timezone_for_scheduled_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, execution_denied: type[Exception], *, manager: bool,
) -> None:
    """Reject a revoked role or use the scheduled zone instead of the worker default."""
    store: SQLiteStateStore = SQLiteStateStore(tmp_path / "state.sqlite3")
    now: datetime = datetime(2026, 11, 27, 22, tzinfo=UTC)
    recipient: SimpleNamespace = SimpleNamespace(user=SimpleNamespace(has_group=Mock(return_value=manager)))
    monkeypatch.setattr(background, "_recipient_environment", lambda _env, _db, uid, _companies: _open_recipient(recipient)(uid))
    monkeypatch.setattr(background, "readable_projects", Mock(return_value=object()))
    monkeypatch.setattr(background, "read_summary_tasks", Mock(return_value=()))
    build: Mock = Mock(wraps=background.core.build_project_risk_summary)
    monkeypatch.setattr(background.core, "build_project_risk_summary", build)
    env: SimpleNamespace = SimpleNamespace(cr=SimpleNamespace(dbname="pilot"))
    runner: background.OdooSummaryRunner = background.OdooSummaryRunner(store, env, "pilot", project_timezone=UTC, clock=lambda: now)

    operation: Operation = replace(_operation(now), project_timezone="Asia/Yekaterinburg")
    if manager:
        runner(operation)
        assert build.call_args.kwargs["project_timezone"] == ZoneInfo("Asia/Yekaterinburg")
    else:
        with pytest.raises(execution_denied):
            runner(operation)
        build.assert_not_called()

    recipient.user.has_group.assert_called_once_with("project.group_project_manager")


def test_runner_denies_a_different_recipient(
    execution_denied: type[Exception], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assert only owner_id == recipient_id is accepted; return None on success."""
    opener: Mock = Mock(side_effect=AssertionError("the recipient environment must not open"))
    monkeypatch.setattr(background, "_recipient_environment", opener)
    env: SimpleNamespace = SimpleNamespace(cr=SimpleNamespace(dbname="pilot"))
    runner: background.OdooSummaryRunner = background.OdooSummaryRunner(Mock(), env, "pilot")

    with pytest.raises(execution_denied):
        runner(_operation(datetime(2026, 11, 27, tzinfo=UTC), recipient_id=8))
    opener.assert_not_called()


def test_runner_denies_a_project_the_recipient_cannot_read(
    execution_denied: type[Exception], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assert a denied project raises before any summary is saved; return None on success."""
    store: Mock = Mock()
    recipient: object = SimpleNamespace(uid=7)
    monkeypatch.setattr(background, "_recipient_environment", lambda _env, _db, uid, _companies: _open_recipient(recipient)(uid))
    monkeypatch.setattr(background, "readable_projects", Mock(return_value=None))
    env: SimpleNamespace = SimpleNamespace(cr=SimpleNamespace(dbname="pilot"))
    runner: background.OdooSummaryRunner = background.OdooSummaryRunner(store, env, "pilot")

    with pytest.raises(execution_denied):
        runner(_operation(datetime(2026, 11, 27, tzinfo=UTC)))
    store.save_summary.assert_not_called()


def test_runner_translates_a_failed_summary_to_a_generic_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assert FAILED summaries raise a safe error and never save; return None on success."""
    store: Mock = Mock()
    recipient: object = SimpleNamespace(uid=7)
    monkeypatch.setattr(background, "_recipient_environment", lambda _env, _db, uid, _companies: _open_recipient(recipient)(uid))
    monkeypatch.setattr(background, "readable_projects", Mock(return_value=object()))
    monkeypatch.setattr(background, "read_summary_tasks", Mock(return_value=()))
    monkeypatch.setattr(
        background.core, "build_project_risk_summary",
        Mock(return_value=ProjectRiskSummary(
            SummaryStatus.FAILED, (), (), ("RR-01", "RR-02", "RR-03", "RR-04"),
        )),
    )
    env: SimpleNamespace = SimpleNamespace(cr=SimpleNamespace(dbname="pilot"))
    runner: background.OdooSummaryRunner = background.OdooSummaryRunner(store, env, "pilot")

    with pytest.raises(background.SummaryExecutionFailed):
        runner(_operation(datetime(2026, 11, 27, tzinfo=UTC)))
    store.save_summary.assert_not_called()


def test_recipient_environment_is_fresh_non_elevated_and_internal(
    execution_denied: type[Exception], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assert the trusted actor checks and non-elevated context; return None on success."""
    env: SimpleNamespace = _install_odoo(monkeypatch)
    with background._recipient_environment(env, "pilot", 7) as recipient:
        assert recipient.uid == 7
        assert recipient.su is False

    for expected_db, user_id in (("other", 7), ("pilot", 1)):
        with pytest.raises(execution_denied):
            with background._recipient_environment(env, expected_db, user_id):
                pass

    inactive: SimpleNamespace = _install_odoo(monkeypatch, active=False)
    with pytest.raises(execution_denied):
        with background._recipient_environment(inactive, "pilot", 7):
            pass

    external: SimpleNamespace = _install_odoo(monkeypatch, internal=False)
    with pytest.raises(execution_denied):
        with background._recipient_environment(external, "pilot", 7):
            pass


def test_recipient_preserves_company_order_and_denies_a_revoked_company(
    execution_denied: type[Exception], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pass both selected companies in order, never widening or silently shrinking the scope."""
    env: SimpleNamespace = _install_odoo(monkeypatch)
    with background._recipient_environment(env, "pilot", 7, (3, 1)) as recipient:
        assert recipient.companies.ids == [3, 1]
    revoked: SimpleNamespace = _install_odoo(monkeypatch, allowed=(3,))
    with pytest.raises(execution_denied):
        with background._recipient_environment(revoked, "pilot", 7, (3, 1)):
            pytest.fail("must not start under only one of the requested companies")


def test_queue_max_length_requires_explicit_configuration() -> None:
    """Reject the missing limit and accept a positive configured value."""
    with pytest.raises(ValueError, match="TASKMASTER_QUEUE_MAX_LENGTH"):
        worker._queue_max_length({})
    assert worker._queue_max_length({worker.QUEUE_MAX_LENGTH_VARIABLE: "7"}) == 7
    assert worker._positive_length(7) == 7
    for raw in ("0", "-1", "x", "1.5"):
        with pytest.raises(ValueError):
            worker._queue_max_length({worker.QUEUE_MAX_LENGTH_VARIABLE: raw})
    for value in (0, -1):
        with pytest.raises(ValueError):
            worker._positive_length(value)


def test_run_requires_the_trusted_database_and_amqp_url() -> None:
    """Assert the launcher rejects a wrong shell and a missing URL; return None on success."""
    with pytest.raises(ValueError):
        worker.run(SimpleNamespace(cr=SimpleNamespace(dbname="other")), "pilot", environ={})
    with pytest.raises(ValueError):
        worker.run(SimpleNamespace(cr=SimpleNamespace(dbname="pilot")), "pilot", environ={})


def test_run_requires_supported_pika_before_accessing_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject an unsupported client before touching pending operations."""
    module: Mock = Mock(spec=[])
    loader: Mock = Mock(return_value=module)
    monkeypatch.setattr(worker, "import_module", loader)
    with pytest.raises(RuntimeError, match="requirements-worker.txt"):
        worker.run(SimpleNamespace(cr=SimpleNamespace(dbname="pilot")), "pilot", environ={
            worker.AMQP_URL_VARIABLE: "amqp://localhost/",
            worker.QUEUE_MAX_LENGTH_VARIABLE: "8",
        })
    loader.assert_called_once_with("pika")


def test_run_tick_recovers_before_receiving() -> None:
    """Assert each tick recovers and then delegates the message to execute; return None on success."""
    calls: list[str] = []
    background_operations: Mock = Mock()
    background_operations.recover.side_effect = lambda *, limit: calls.append("recover")
    queue: Mock = Mock()

    def receive(handle: object) -> bool:
        calls.append("receive")
        handle("op-1")
        return True

    queue.receive.side_effect = receive
    runner: Mock = Mock()

    assert worker.run_tick(background_operations, queue, runner) is True
    assert calls == ["recover", "receive"]
    background_operations.recover.assert_called_once_with(limit=1)
    background_operations.execute.assert_called_once_with("op-1", runner)


def test_serve_waits_bounded_after_idle_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assert idle and failure waits are bounded and skipped after a handled tick; return None."""
    class Failure(Exception):
        """Represent a recoverable broker failure."""

    monkeypatch.setattr(worker, "_failure_types", lambda: (Failure,))
    background_operations: Mock = Mock()
    background_operations.recover.side_effect = [Failure(), None, None]
    queue: Mock = Mock()
    queue.receive.return_value = False
    sleeps: list[float] = []

    ticks: int = worker.serve(
        background_operations, queue, Mock(), max_ticks=3, idle_seconds=0.25, failure_seconds=1.5,
        sleep=sleeps.append,
    )

    assert ticks == 3
    assert sleeps == [1.5, 0.25]


def test_serve_does_not_wait_after_a_handled_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assert a handled message proceeds without an idle wait; return None on success."""
    monkeypatch.setattr(worker, "_failure_types", lambda: ())
    background_operations: Mock = Mock()
    queue: Mock = Mock()
    queue.receive.return_value = True
    sleeps: list[float] = []

    assert worker.serve(background_operations, queue, Mock(), max_ticks=2, sleep=sleeps.append) == 2
    assert sleeps == []


def test_serve_bounds_consecutive_failure_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assert consecutive failures back off to the cap and never exceed it; return None."""
    class Failure(Exception):
        """Represent a recoverable broker failure."""

    monkeypatch.setattr(worker, "_failure_types", lambda: (Failure,))
    background_operations: Mock = Mock()
    background_operations.recover.side_effect = Failure()
    queue: Mock = Mock()
    sleeps: list[float] = []

    ticks: int = worker.serve(
        background_operations, queue, Mock(), max_ticks=5, failure_seconds=20.0, sleep=sleeps.append,
    )

    assert ticks == 5
    assert sleeps == [20.0, 40.0, 60.0, 60.0]


def test_serve_rejects_unbounded_or_empty_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assert invalid tick counts and waits fail before the loop; return None on success."""
    monkeypatch.setattr(worker, "_failure_types", lambda: ())
    for max_ticks in (0, -1):
        with pytest.raises(ValueError):
            worker.serve(Mock(), Mock(), Mock(), max_ticks=max_ticks, sleep=lambda _seconds: None)
    for idle_seconds in (float("inf"), -0.1):
        with pytest.raises(ValueError):
            worker.serve(Mock(), Mock(), Mock(), max_ticks=1, idle_seconds=idle_seconds, sleep=lambda _seconds: None)
