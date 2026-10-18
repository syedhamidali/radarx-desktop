"""Module-run safety: timeout, cancellation, output validation, actionable errors."""

from __future__ import annotations

import json
import threading
import time

import numpy as np
import pytest

import server
from engine import plugins
from engine.plugins import Result, module
from tests.conftest import make_synthetic_datatree
from tests.test_server import _make_handler, _reader_with_tree


@pytest.fixture()
def safe_modules():
    before = dict(plugins.MODULES)
    state: dict = {"release": threading.Event(), "finished": threading.Event()}

    @module(id="rel_hang", title="Hang", category="Test")
    def hang(ctx):
        state["release"].wait(30)  # ignores cancellation: only returns when released
        state["finished"].set()
        return Result().scalars({"x": 1})

    @module(id="rel_coop", title="Coop", category="Test")
    def coop(ctx):
        for i in range(10_000):
            ctx.progress(i % 100, "working")  # raises ModuleCancelled once abandoned
            state["release"].wait(0.01)
        return Result()

    @module(id="rel_crash", title="Crash", category="Test")
    def crash(ctx):
        raise ValueError("bad thing")

    @module(id="rel_badshape", title="Bad Shape", category="Test")
    def badshape(ctx):
        return Result().field("f", np.zeros((2, 2)))

    @module(id="rel_allinf", title="All Inf", category="Test")
    def allinf(ctx):
        n_az, n_rng = ctx.dataset["azimuth"].size, ctx.dataset["range"].size
        return Result().field("f", np.full((n_az, n_rng), np.inf))

    @module(id="rel_someinf", title="Some Inf", category="Test")
    def someinf(ctx):
        n_az, n_rng = ctx.dataset["azimuth"].size, ctx.dataset["range"].size
        arr = np.ones((n_az, n_rng))
        arr[0, 0] = np.inf
        return Result().field("f", arr)

    @module(id="rel_nonresult", title="Non Result", category="Test")
    def nonresult(ctx):
        return {"not": "a result"}

    yield state
    state["release"].set()
    plugins.MODULES.clear()
    plugins.MODULES.update(before)
    with plugins._abandoned_lock:
        plugins._abandoned_runs.clear()


def _run(module_id: str, **kw):
    tree = make_synthetic_datatree(n_azimuth=20, n_range=30)
    return plugins.run_module(
        module_id,
        tree,
        0,
        {},
        get_sweep_dataset=lambda i: tree["sweep_0"].to_dataset(),
        **kw,
    )


class TestModuleSafety:
    def test_timeout_returns_clear_error_and_discards_late_result(self, safe_modules) -> None:
        t0 = time.monotonic()
        with pytest.raises(plugins.ModuleTimeoutError, match=r"Hang timed out after 0\.3 s"):
            _run("rel_hang", timeout_s=0.3)
        assert time.monotonic() - t0 < 5  # caller released promptly
        safe_modules["release"].set()  # the stuck thread now finishes: its result is dropped
        assert safe_modules["finished"].wait(5)

    def test_cooperative_module_stops_after_timeout(self, safe_modules) -> None:
        with pytest.raises(plugins.ModuleTimeoutError):
            _run("rel_coop", timeout_s=0.3)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and plugins._live_abandoned():
            time.sleep(0.05)
        assert plugins._live_abandoned() == 0  # the thread exited: nothing leaked
        assert not any(t.name == "module-rel_coop" for t in threading.enumerate())

    def test_env_timeout(self, monkeypatch) -> None:
        monkeypatch.delenv("XRADAR_MODULE_TIMEOUT_S", raising=False)
        assert plugins.module_timeout_s() == 300.0
        monkeypatch.setenv("XRADAR_MODULE_TIMEOUT_S", "12.5")
        assert plugins.module_timeout_s() == 12.5
        monkeypatch.setenv("XRADAR_MODULE_TIMEOUT_S", "garbage")
        assert plugins.module_timeout_s() == 300.0
        monkeypatch.setenv("XRADAR_MODULE_TIMEOUT_S", "0")
        assert plugins.module_timeout_s() is None

    def test_too_many_stuck_runs_refuse_new_ones(self, safe_modules) -> None:
        for _ in range(plugins._MAX_ABANDONED_RUNS):
            with pytest.raises(plugins.ModuleTimeoutError):
                _run("rel_hang", timeout_s=0.1)
        with pytest.raises(plugins.ModuleError, match="Restart"):
            _run("rel_hang", timeout_s=0.1)

    def test_caller_cancel(self, safe_modules) -> None:
        with pytest.raises(plugins.ModuleCancelled):
            _run("rel_hang", timeout_s=None, is_cancelled=lambda: True)

    def test_crash_becomes_actionable_module_error(self, safe_modules) -> None:
        with pytest.raises(plugins.ModuleError) as ei:
            _run("rel_crash")
        text = str(ei.value)
        assert "Crash failed" in text
        assert "ValueError: bad thing" in text
        assert "Check the parameter values" in text
        assert "Traceback" not in text

    def test_non_result_message(self, safe_modules) -> None:
        with pytest.raises(plugins.ModuleError, match="return res"):
            _run("rel_nonresult")

    def test_bad_shape_rejected(self, safe_modules) -> None:
        with pytest.raises(plugins.ModuleError, match=r"shape \(2, 2\).*\(20, 30\)"):
            _run("rel_badshape")

    def test_all_inf_rejected_some_inf_sanitised(self, safe_modules) -> None:
        with pytest.raises(plugins.ModuleError, match="division by zero"):
            _run("rel_allinf")
        payload, fields = _run("rel_someinf", return_fields=True)
        assert np.isnan(fields["f"][0, 0])
        assert np.isfinite(fields["f"][1, 1])
        assert any("infinite" in w for w in payload["warnings"])


@pytest.fixture()
def _server_state():
    saved = (dict(server._file_readers), server._last_file_id)
    server._file_readers.clear()
    yield
    server._file_readers.clear()
    server._file_readers.update(saved[0])
    server._last_file_id = saved[1]


class TestModuleSafetyServer:
    @pytest.mark.asyncio
    async def test_timeout_is_module_error_and_server_stays_responsive(
        self, safe_modules, _server_state, monkeypatch
    ) -> None:
        monkeypatch.setenv("XRADAR_MODULE_TIMEOUT_S", "0.3")
        server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        try:
            await handler.handle_message(json.dumps({"type": "run_module", "module": "rel_hang"}))
            errors = [m for m in handler._sent if m["type"] == "module_error"]
            assert len(errors) == 1
            assert "timed out after 0.3 s" in errors[0]["message"]
            assert not any(m["type"] == "module_result" for m in handler._sent)
            await handler.handle_message(json.dumps({"type": "ping"}))
            assert any(m["type"] == "pong" for m in handler._sent)
        finally:
            safe_modules["release"].set()

    @pytest.mark.asyncio
    async def test_timeout_aborts_all_sweeps_run(
        self, safe_modules, _server_state, monkeypatch
    ) -> None:
        monkeypatch.setenv("XRADAR_MODULE_TIMEOUT_S", "0.3")
        server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        try:
            await handler.handle_message(
                json.dumps({"type": "run_module", "module": "rel_hang", "all_sweeps": True})
            )
            errors = [m for m in handler._sent if m["type"] == "module_error"]
            assert len(errors) == 1
            assert "timed out" in errors[0]["message"]
        finally:
            safe_modules["release"].set()

    @pytest.mark.asyncio
    async def test_crash_message_is_actionable_not_traceback(
        self, safe_modules, _server_state
    ) -> None:
        server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        await handler.handle_message(json.dumps({"type": "run_module", "module": "rel_crash"}))
        msg = next(m for m in handler._sent if m["type"] == "module_error")
        assert "bad thing" in msg["message"]
        assert "Traceback" not in msg["message"]
        assert "Check the parameter values" in msg["message"]
