"""Fault injection against the message handler: hostile input, broken modules, bad data.

Each test is a failure that used to crash the engine, hang a request or send the user a
message they could not act on (see docs/STABILITY.md).
"""

from __future__ import annotations

import errno
import json
import logging
import sys

import numpy as np
import pytest
import xarray as xr

import server
from engine import plugins
from engine.plugins import Result, module
from engine.reader import RadarReader
from tests.conftest import make_synthetic_datatree
from tests.test_server import _binary, _make_handler, _reader_with_tree


@pytest.fixture(autouse=True)
def _isolated_state():
    saved = (dict(server._file_readers), server._last_file_id, dict(server._generations))
    server._file_readers.clear()
    server._generations.clear()
    server._last_file_id = None
    yield
    server._file_readers.clear()
    server._file_readers.update(saved[0])
    server._last_file_id = saved[1]
    server._generations.clear()
    server._generations.update(saved[2])


def _errors(handler) -> list[str]:
    return [m["message"] for m in handler._sent if m["type"] == "error"]


async def _send(handler, msg: dict | str) -> None:
    await handler.handle_message(msg if isinstance(msg, str) else json.dumps(msg))


class TestHostileJson:
    @pytest.mark.asyncio
    async def test_thousand_digit_integer_gets_an_error_reply(self) -> None:
        handler, _ = _make_handler()
        await _send(handler, '{"type": "render", "sweep": ' + "9" * 5000 + "}")
        assert len(_errors(handler)) == 1
        assert "Invalid JSON" in _errors(handler)[0]

    @pytest.mark.asyncio
    async def test_deeply_nested_json_gets_an_error_reply(self) -> None:
        handler, _ = _make_handler()
        await _send(handler, '{"type": "ping", "x": ' + "[" * 200_000 + "]" * 200_000 + "}")
        assert len(_errors(handler)) == 1
        assert "Invalid JSON" in _errors(handler)[0]

    @pytest.mark.asyncio
    async def test_engine_still_answers_after_the_garbage(self) -> None:
        handler, _ = _make_handler()
        await _send(handler, "[" * 100_000)
        await _send(handler, '{"type": "ping"}')
        assert handler._sent[-1]["type"] == "pong"


class TestModulesCannotStopTheEngine:
    @pytest.fixture()
    def bad_modules(self):
        before = dict(plugins.MODULES)

        @module(id="stab_exit", title="Exit", category="Test")
        def stab_exit(ctx):
            sys.exit(3)

        @module(id="stab_kbd", title="Kbd", category="Test")
        def stab_kbd(ctx):
            raise KeyboardInterrupt

        @module(id="stab_mem", title="Mem", category="Test")
        def stab_mem(ctx):
            raise MemoryError

        yield
        plugins.MODULES.clear()
        plugins.MODULES.update(before)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mod", ["stab_exit", "stab_kbd"])
    async def test_system_exit_in_a_module_is_a_module_error(self, bad_modules, mod: str) -> None:
        server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        await _send(handler, {"type": "run_module", "module": mod})
        errs = [m for m in handler._sent if m["type"] == "module_error"]
        assert len(errs) == 1
        assert "tried to stop the program" in errs[0]["message"]
        assert "Traceback" not in errs[0]["message"]
        await _send(handler, {"type": "ping"})
        assert handler._sent[-1]["type"] == "pong"

    @pytest.mark.asyncio
    async def test_all_sweeps_run_survives_it_too(self, bad_modules) -> None:
        server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        await _send(handler, {"type": "run_module", "module": "stab_exit", "all_sweeps": True})
        assert any(m["type"] == "module_error" for m in handler._sent)

    @pytest.mark.asyncio
    async def test_memory_error_in_a_module_says_what_to_do(self, bad_modules) -> None:
        server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        await _send(handler, {"type": "run_module", "module": "stab_mem"})
        msg = next(m for m in handler._sent if m["type"] == "module_error")["message"]
        assert "out of memory" in msg

    @pytest.mark.asyncio
    async def test_safety_net_for_any_other_handler(self, monkeypatch) -> None:
        async def boom(self, msg):
            raise SystemExit(1)

        monkeypatch.setattr(server.ConnectionHandler, "_handle_list_retrievals", boom)
        handler, _ = _make_handler()
        await _send(handler, {"type": "list_retrievals"})
        assert "tried to stop the program" in _errors(handler)[0]
        await _send(handler, {"type": "ping"})
        assert handler._sent[-1]["type"] == "pong"


def _empty_sweep_tree(n_az: int, n_rng: int, values: np.ndarray | None = None) -> xr.DataTree:
    az = np.linspace(0, 359, n_az)
    rng = np.linspace(500, 50_000, n_rng)
    data = np.zeros((n_az, n_rng), np.float32) if values is None else values
    ds = xr.Dataset(
        {"DBZH": (("azimuth", "range"), data)},
        coords={"azimuth": az, "range": rng},
        attrs={"sweep_fixed_angle": 0.5},
    )
    return xr.DataTree.from_dict({"/": xr.Dataset(), "/sweep_0": ds})


class TestDegenerateSweeps:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("shape", [(0, 30), (30, 0), (0, 0)])
    async def test_empty_sweep_is_a_clear_error(self, shape) -> None:
        server._register_reader(_reader_with_tree(_empty_sweep_tree(*shape)))
        handler, _ = _make_handler()
        await _send(handler, {"type": "get_sweep_data", "variable": "DBZH", "sweep": 0})
        errs = _errors(handler)
        assert len(errs) == 1
        assert "no DBZH data" in errs[0]
        assert "memoryview" not in errs[0]
        assert not _binary(handler)

    def test_zero_length_arrays_have_no_frames(self) -> None:
        views = server._byte_views([np.zeros((0, 4), np.float32), np.arange(3, dtype=np.float32)])
        assert [len(v) for v in views] == [12]

    @pytest.mark.asyncio
    async def test_float32_extremes_do_not_break_statistics(self) -> None:
        vals = np.random.default_rng(0).uniform(-10, 10, (20, 30)).astype(np.float32)
        vals[1, 1], vals[2, 2], vals[3, 3] = 3.0e38, -3.0e38, np.inf
        server._register_reader(_reader_with_tree(_empty_sweep_tree(20, 30, vals)))
        handler, _ = _make_handler()
        await _send(handler, {"type": "get_statistics", "variable": "DBZH", "sweep": 0})
        assert not _errors(handler)
        result = next(m for m in handler._sent if m["type"] == "statistics_result")
        assert sum(result["counts"]) == 20 * 30 - 1  # the infinity is not a measurement

    @pytest.mark.asyncio
    async def test_absurd_bin_counts_are_clamped(self) -> None:
        server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        await _send(
            handler,
            {"type": "get_statistics", "variable": "DBZH", "params": {"n_bins": 2_000_000_000}},
        )
        result = next(m for m in handler._sent if m["type"] == "statistics_result")
        assert len(result["counts"]) <= 10_000
        await _send(handler, {"type": "get_histogram", "variable": "DBZH", "bins": 2_000_000_000})
        result = next(m for m in handler._sent if m["type"] == "histogram_result")
        assert len(result["counts"]) <= 10_000

    @pytest.mark.asyncio
    async def test_infinite_numbers_are_invalid_input_not_unexpected(self) -> None:
        server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        await _send(
            handler,
            '{"type": "get_data_table", "variable": "DBZH", "sweep": 0, "az_end": Infinity}',
        )
        errs = _errors(handler)
        assert errs
        assert "Unexpected" not in errs[0]


class TestFailureText:
    def test_disk_full(self) -> None:
        text = server._failure_text("export", OSError(errno.ENOSPC, "No space left", "/x/a.png"))[1]
        assert text.startswith("I/O error in export:")
        assert "disk is full" in text and "a.png" in text
        assert "/x/" not in text  # only the file name

    def test_permission_and_read_only(self) -> None:
        denied = server._failure_text("export", PermissionError(errno.EACCES, "denied", "/r/a"))[1]
        assert "permission denied" in denied and "allowed to write" in denied
        ro = server._failure_text("export", OSError(errno.EROFS, "Read-only", "/r/a"))[1]
        assert "read-only" in ro

    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError("NetCDF: HDF error"),
            OSError("Can't synchronously read data (filter returned failure during read)"),
            ValueError("Unexpected file end detected at record 12"),
        ],
    )
    def test_damaged_data_says_to_redownload(self, exc) -> None:
        level, text = server._failure_text("get_sweep_data", exc)
        assert level == logging.WARNING
        assert "get_sweep_data" in text  # the UI matches a failure to its request by this name
        assert "truncated or damaged" in text and "Re-download" in text

    def test_memory_error(self) -> None:
        text = server._failure_text("render", MemoryError())[1]
        assert "Not enough memory in render" in text

    def test_unexpected_has_no_traceback_and_a_next_step(self) -> None:
        text = server._failure_text("render", ZeroDivisionError("division by zero"))[1]
        assert text.startswith("Unexpected error in render: division by zero")
        assert "restart" in text and "Traceback" not in text

    def test_long_details_are_cut(self) -> None:
        text = server._failure_text("render", RuntimeError("x" * 5000))[1]
        assert len(text) < 700

    def test_existing_prefixes_the_ui_relies_on(self) -> None:
        assert server._failure_text("a", ValueError("v"))[1] == "Invalid input for a: v"
        assert server._failure_text("a", KeyError("k"))[1] == "Not found: 'k'"
        assert (
            server._failure_text("a", FileNotFoundError("gone"))
            .__getitem__(1)
            .startswith("File not found: gone")
        )


class TestDamagedDataOnRead:
    @pytest.mark.asyncio
    async def test_lazy_read_failure_is_reported_as_damaged_file(self, monkeypatch) -> None:
        server._register_reader(_reader_with_tree())

        def broken(self, sweep, variable):
            raise OSError("Can't synchronously read data (filter returned failure during read)")

        monkeypatch.setattr(RadarReader, "get_sweep_data", broken)
        handler, _ = _make_handler()
        await _send(handler, {"type": "get_sweep_data", "variable": "DBZH", "sweep": 0})
        text = _errors(handler)[0]
        assert "get_sweep_data" in text and "Re-download" in text
        assert "I/O error" not in text


def test_synthetic_tree_helper_is_sane() -> None:
    assert len(make_synthetic_datatree(n_azimuth=4, n_range=4).children) == 3
    assert Result is not None
