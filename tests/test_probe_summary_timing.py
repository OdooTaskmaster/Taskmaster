"""Check the opt-in summary timing probe boundaries and labels."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

_SCRIPT: Path = Path(__file__).resolve().parents[1] / "scripts" / "probe_summary_timing.py"
_SPEC = spec_from_file_location("probe_summary_timing", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
probe: ModuleType = module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)


def test_measure_times_only_the_worker_calls() -> None:
    """Assert the clock wraps each supplied call exactly once; return None on success."""
    ticks: list[float] = [10.0, 10.25, 20.0, 20.5, 30.0, 30.1]
    calls: list[int] = []

    samples: tuple[float, ...] = probe._measure(
        lambda index: calls.append(index), 3, clock=lambda: ticks.pop(0),
    )

    assert calls == [0, 1, 2]
    assert samples == pytest.approx((250.0, 500.0, 100.0))


def test_measure_has_no_hidden_warmup() -> None:
    """Assert a single round runs one call and yields one sample; return None on success."""
    calls: list[int] = []
    assert probe._measure(lambda index: calls.append(index), 1, clock=lambda: 5.0) == (0.0,)
    assert calls == [0]


def test_run_requires_an_explicit_fixture_and_run_count() -> None:
    """Assert missing scope or timing arguments fail before any Odoo access; return None."""
    env: SimpleNamespace = SimpleNamespace(cr=SimpleNamespace(dbname="probe"))
    for arguments in (
        {"expected_db": "other", "user_id": 7, "project_id": 3, "run_count": 5, "expected_task_count": 24},
        {"expected_db": "probe", "user_id": 0, "project_id": 3, "run_count": 5, "expected_task_count": 24},
        {"expected_db": "probe", "user_id": 7, "project_id": 0, "run_count": 5, "expected_task_count": 24},
        {"expected_db": "probe", "user_id": 7, "project_id": 3, "run_count": 0, "expected_task_count": 24},
        {"expected_db": "probe", "user_id": 7, "project_id": 3, "run_count": 5, "expected_task_count": -1},
    ):
        with pytest.raises(ValueError):
            probe.run(env, arguments.pop("expected_db"), **arguments)
