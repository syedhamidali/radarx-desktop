"""The benchmark regression check decides correctly (scripts/perf/bench_check.py)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "perf" / "bench_check.py"

pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="repository scripts not available")


@pytest.fixture(scope="module")
def check():
    spec = importlib.util.spec_from_file_location("bench_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE = {
    "calibration_ms": 100.0,
    "metrics": {"ready_ms": 600.0, "libraries_at_ready": 120.0, "png_switch_cpu_ms": 170.0},
}


def _bad(rows):
    return {name for name, _, _, _, bad in rows if bad}


def test_the_reference_run_itself_passes(check) -> None:
    rows = check.compare(dict(BASE["metrics"]), BASE, 100.0, 2.5)
    assert len(rows) == 3 and not _bad(rows)


def test_a_slow_machine_is_not_a_regression(check) -> None:
    # twice the calibration time, twice every timing: fine; the library count does not scale
    slow = {"ready_ms": 1200.0, "libraries_at_ready": 120.0, "png_switch_cpu_ms": 340.0}
    assert not _bad(check.compare(slow, BASE, 200.0, 2.5))


def test_several_times_slower_on_the_same_machine_fails(check) -> None:
    slower = {"ready_ms": 600.0, "libraries_at_ready": 120.0, "png_switch_cpu_ms": 170.0 * 3.2}
    assert _bad(check.compare(slower, BASE, 100.0, 2.5)) == {"png_switch_cpu_ms"}


def test_noise_within_the_margin_passes(check) -> None:
    noisy = {"ready_ms": 600.0 * 1.4, "libraries_at_ready": 125.0, "png_switch_cpu_ms": 170 * 1.6}
    assert not _bad(check.compare(noisy, BASE, 100.0, 2.5))


def test_loading_many_more_libraries_at_start_fails(check) -> None:
    more = {"ready_ms": 600.0, "libraries_at_ready": 200.0, "png_switch_cpu_ms": 170.0}
    assert _bad(check.compare(more, BASE, 100.0, 2.5)) == {"libraries_at_ready"}


def test_the_committed_baseline_is_complete(check) -> None:
    base = json.loads(check.BASELINE.read_text())
    wanted = {name for name, _, _ in check.BENCH_ROWS} | {"ready_ms", "libraries_at_ready"}
    assert wanted <= set(base["metrics"])
    assert base["calibration_ms"] > 0
