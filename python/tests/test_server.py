"""Tests for WebSocket server message routing and error handling."""

from __future__ import annotations

import asyncio
import json
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
import xarray as xr

import server
from engine import plugins
from engine.reader import RadarReader

# We test ConnectionHandler in isolation — no real WebSocket needed.
from server import ConnectionHandler
from tests.conftest import make_synthetic_datatree

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_handler() -> tuple[ConnectionHandler, AsyncMock]:
    """Create a ConnectionHandler with a mock WebSocket that captures sends."""
    ws = AsyncMock()
    ws.remote_address = ("127.0.0.1", 9999)

    # Collect all messages sent back
    sent: list[dict] = []
    frames: list[tuple[str, object]] = []

    async def _fake_send(raw) -> None:
        if isinstance(raw, str):
            msg = json.loads(raw)
            sent.append(msg)
            frames.append(("json", msg))
        else:
            frames.append(("bin", bytes(raw)))

    ws.send = _fake_send
    handler = ConnectionHandler(ws)
    handler._sent = sent  # stash for assertions
    handler._frames = frames  # ordered JSON + binary frames
    return handler, ws


@pytest.fixture(autouse=True)
def _isolated_state():
    """Each test starts and ends with no open files."""
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


def _reader_with_tree(tree=None) -> RadarReader:
    reader = RadarReader()
    reader._dtree = tree if tree is not None else make_synthetic_datatree(n_azimuth=20, n_range=30)
    reader._format = "synthetic"
    return reader


def _binary(handler: ConnectionHandler) -> bytes:
    return b"".join(f for kind, f in handler._frames if kind == "bin")


# ---------------------------------------------------------------------------
# Ping / Pong
# ---------------------------------------------------------------------------


class TestPingPong:
    @pytest.mark.asyncio
    async def test_ping_returns_pong(self) -> None:
        handler, _ = _make_handler()
        await handler.handle_message(json.dumps({"type": "ping"}))
        assert len(handler._sent) == 1
        msg = handler._sent[0]
        assert msg["type"] == "pong"
        assert msg["status"] == "ok"
        assert "file_open" in msg

    @pytest.mark.asyncio
    async def test_ping_reports_no_file_open(self) -> None:
        handler, _ = _make_handler()
        with patch("server._shared_reader") as mock_reader:
            mock_reader.datatree = None
            await handler.handle_message(json.dumps({"type": "ping"}))
        assert handler._sent[0]["file_open"] is False


# ---------------------------------------------------------------------------
# Invalid messages
# ---------------------------------------------------------------------------


class TestInvalidMessages:
    @pytest.mark.asyncio
    async def test_invalid_json(self) -> None:
        handler, _ = _make_handler()
        await handler.handle_message("not json at all {{{")
        assert handler._sent[0]["type"] == "error"
        assert "Invalid JSON" in handler._sent[0]["message"]

    @pytest.mark.asyncio
    async def test_non_object_message(self) -> None:
        handler, _ = _make_handler()
        await handler.handle_message(json.dumps([1, 2, 3]))
        assert handler._sent[0]["type"] == "error"
        assert "JSON object" in handler._sent[0]["message"]

    @pytest.mark.asyncio
    async def test_missing_type_field(self) -> None:
        handler, _ = _make_handler()
        await handler.handle_message(json.dumps({"data": "hello"}))
        assert handler._sent[0]["type"] == "error"
        assert "type" in handler._sent[0]["message"]

    @pytest.mark.asyncio
    async def test_unknown_message_type(self) -> None:
        handler, _ = _make_handler()
        await handler.handle_message(json.dumps({"type": "foobar"}))
        assert handler._sent[0]["type"] == "error"
        assert "Unknown message type" in handler._sent[0]["message"]


# ---------------------------------------------------------------------------
# open_file routing
# ---------------------------------------------------------------------------


class TestOpenFileRouting:
    @pytest.mark.asyncio
    async def test_open_file_missing_path(self) -> None:
        handler, _ = _make_handler()
        await handler.handle_message(json.dumps({"type": "open_file"}))
        assert handler._sent[0]["type"] == "error"
        assert "missing" in handler._sent[0]["message"].lower()

    @pytest.mark.asyncio
    async def test_open_file_path_not_string(self) -> None:
        handler, _ = _make_handler()
        await handler.handle_message(json.dumps({"type": "open_file", "path": 123}))
        assert handler._sent[0]["type"] == "error"

    @pytest.mark.asyncio
    async def test_open_file_nonexistent(self) -> None:
        handler, _ = _make_handler()
        await handler.handle_message(json.dumps({"type": "open_file", "path": "/no/such/file.nc"}))
        # Should get progress then error
        errors = [m for m in handler._sent if m["type"] == "error"]
        assert len(errors) >= 1
        assert "not found" in errors[0]["message"].lower() or "No such file" in errors[0]["message"]


# ---------------------------------------------------------------------------
# render routing — validation only (no actual datashader)
# ---------------------------------------------------------------------------


class TestRenderRouting:
    @pytest.mark.asyncio
    async def test_render_missing_variable(self) -> None:
        handler, _ = _make_handler()
        await handler.handle_message(json.dumps({"type": "render"}))
        assert handler._sent[0]["type"] == "error"
        assert "variable" in handler._sent[0]["message"].lower()


# ---------------------------------------------------------------------------
# export routing — validation
# ---------------------------------------------------------------------------


class TestExportRouting:
    @pytest.mark.asyncio
    async def test_export_no_file_open(self) -> None:
        handler, _ = _make_handler()
        with patch("server._shared_reader") as mock_reader:
            mock_reader.datatree = None
            await handler.handle_message(json.dumps({"type": "export", "format": "png"}))
        errors = [m for m in handler._sent if m["type"] == "error"]
        assert len(errors) >= 1
        assert "no file" in errors[0]["message"].lower()


# ---------------------------------------------------------------------------
# process routing — validation
# ---------------------------------------------------------------------------


class TestProcessRouting:
    @pytest.mark.asyncio
    async def test_process_invalid_pipeline(self) -> None:
        handler, _ = _make_handler()
        await handler.handle_message(json.dumps({"type": "process", "pipeline": "not_a_dict"}))
        assert handler._sent[0]["type"] == "error"

    @pytest.mark.asyncio
    async def test_process_no_file_open(self) -> None:
        handler, _ = _make_handler()
        with patch("server._shared_reader") as mock_reader:
            mock_reader.datatree = None
            await handler.handle_message(json.dumps({"type": "process", "pipeline": {}}))
        errors = [m for m in handler._sent if m["type"] == "error"]
        assert len(errors) >= 1


# ---------------------------------------------------------------------------
# Multi-file isolation and file_id resolution
# ---------------------------------------------------------------------------


class TestMultiFile:
    @pytest.mark.asyncio
    async def test_cloud_opens_get_independent_readers(self) -> None:
        trees = [
            make_synthetic_datatree(n_azimuth=10, n_range=12),
            make_synthetic_datatree(n_azimuth=14, n_range=16),
        ]

        def fake_open_s3(self: RadarReader, path: str) -> dict:
            self._dtree = trees[0] if path.endswith("a") else trees[1]
            return {"variables": ["DBZH"], "sweeps": [], "attributes": {}, "dimensions": {}}

        handler, _ = _make_handler()
        with patch.object(RadarReader, "open_s3_file", fake_open_s3):
            await handler.handle_message(json.dumps({"type": "open_nexrad_l2", "path": "s3://x/a"}))
            await handler.handle_message(json.dumps({"type": "open_nexrad_l2", "path": "s3://x/b"}))
        ids = [m["file_id"] for m in handler._sent if m["type"] == "file_opened"]
        assert len(ids) == 2 and ids[0] != ids[1]
        r1, r2 = server._file_readers[ids[0]], server._file_readers[ids[1]]
        assert r1 is not r2
        assert r1.datatree is trees[0] and r2.datatree is trees[1]

        # Closing the second must not disturb the first; active file falls back to it.
        await handler.handle_message(json.dumps({"type": "close_file", "file_id": ids[1]}))
        assert server._file_readers[ids[0]].datatree is trees[0]
        assert server._get_reader(None) is r1

    @pytest.mark.asyncio
    async def test_unknown_file_id_is_an_error_not_another_files_data(self) -> None:
        server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        await handler.handle_message(
            json.dumps({"type": "get_sweep_data", "variable": "DBZH", "file_id": "nope"})
        )
        assert handler._sent[-1]["type"] == "error"
        assert "nope" in handler._sent[-1]["message"]

    @pytest.mark.asyncio
    async def test_missing_file_id_uses_most_recent_file(self) -> None:
        server._register_reader(_reader_with_tree())
        newest = _reader_with_tree()
        server._register_reader(newest)
        assert server._get_reader(None) is newest

    @pytest.mark.asyncio
    async def test_ping_with_unknown_file_id_reports_closed(self) -> None:
        handler, _ = _make_handler()
        await handler.handle_message(json.dumps({"type": "ping", "file_id": "gone"}))
        assert handler._sent[0]["type"] == "pong"
        assert handler._sent[0]["file_open"] is False


# ---------------------------------------------------------------------------
# JSON robustness
# ---------------------------------------------------------------------------


class TestJsonSend:
    @pytest.mark.asyncio
    async def test_nan_and_inf_become_null(self) -> None:
        handler, _ = _make_handler()
        await handler.send(
            {"type": "x", "a": float("nan"), "b": [1.0, float("inf")], "c": np.array([np.nan, 2.0])}
        )
        assert handler._sent == [{"type": "x", "a": None, "b": [1.0, None], "c": [None, 2.0]}]

    @pytest.mark.asyncio
    async def test_unserializable_payload_still_answers(self) -> None:
        handler, _ = _make_handler()
        await handler.send({"type": "x", "bad": object()})
        assert handler._sent[0]["type"] == "error"


# ---------------------------------------------------------------------------
# Binary framing
# ---------------------------------------------------------------------------


class TestBinaryFraming:
    def test_chunks_reassemble_exactly(self) -> None:
        arrays = [
            np.arange(1000, dtype=np.float32),
            np.arange(5000, dtype=np.float32) * 2,
            np.arange(300_000, dtype=np.float32),  # > one 900 KB frame
        ]
        expected = b"".join(a.tobytes() for a in arrays)
        chunks = [bytes(c) for c in server._iter_chunks(server._byte_views(arrays))]
        assert b"".join(chunks) == expected
        assert all(len(c) == server.CHUNK_SIZE for c in chunks[:-1])
        assert len(chunks) == -(-len(expected) // server.CHUNK_SIZE)

    def test_non_contiguous_array_is_made_contiguous(self) -> None:
        arr = np.arange(12, dtype=np.float32).reshape(3, 4)[:, ::2]
        assert b"".join(bytes(c) for c in server._iter_chunks(server._byte_views([arr]))) == (
            np.ascontiguousarray(arr).tobytes()
        )

    def test_finite_range(self) -> None:
        assert server._finite_range(np.array([np.nan, 2.0, -1.0], dtype=np.float32)) == (-1.0, 2.0)
        assert server._finite_range(np.full(4, np.nan, dtype=np.float32)) is None
        assert server._finite_range(np.array([1.0, np.inf], dtype=np.float32)) == (1.0, 1.0)
        assert server._finite_range(np.empty(0, dtype=np.float32)) is None

    @pytest.mark.asyncio
    async def test_get_sweep_data_wire_format(self) -> None:
        reader = _reader_with_tree(make_synthetic_datatree(n_azimuth=20, n_range=30))
        data = reader.get_sweep_data(0, "DBZH")
        data.values[0, 0] = np.nan
        file_id = server._register_reader(reader)
        handler, _ = _make_handler()
        await handler.handle_message(
            json.dumps(
                {"type": "get_sweep_data", "variable": "DBZH", "sweep": 0, "file_id": file_id}
            )
        )
        header = handler._sent[0]
        assert header["type"] == "sweep_data"
        assert header["n_az"] == 20 and header["n_range"] == 30
        buf = _binary(handler)
        assert header["chunks"] == len([1 for k, _ in handler._frames if k == "bin"])
        assert len(buf) == header["azimuth_bytes"] + header["range_bytes"] + header["values_bytes"]
        values = np.frombuffer(buf, dtype=np.float32, offset=(20 + 30) * 4)
        assert values[0] == np.float32(-9999.0)  # NaN -> sentinel
        assert np.isfinite(header["vmin"]) and np.isfinite(header["vmax"])
        # source data untouched by the in-place sentinel fill
        assert np.isnan(data.values[0, 0])

    @pytest.mark.asyncio
    async def test_all_nan_sweep_does_not_hang_or_raise(self) -> None:
        tree = make_synthetic_datatree(n_azimuth=8, n_range=9)
        for node in tree.children.values():
            node["VRADH"].values[:] = np.nan
        file_id = server._register_reader(_reader_with_tree(tree))
        handler, _ = _make_handler()
        await handler.handle_message(
            json.dumps({"type": "get_sweep_data", "variable": "VRADH", "file_id": file_id})
        )
        header = handler._sent[0]
        assert header["type"] == "sweep_data"
        assert header["vmin"] is not None and header["vmax"] is not None

    @pytest.mark.asyncio
    async def test_concurrent_binary_responses_do_not_interleave(self) -> None:
        # Large enough for several frames each; two requests race on one connection.
        tree = make_synthetic_datatree(n_azimuth=360, n_range=700)
        file_id = server._register_reader(_reader_with_tree(tree))
        handler, _ = _make_handler()
        msgs = [
            json.dumps({"type": "get_sweep_data", "variable": v, "file_id": file_id})
            for v in ("DBZH", "VRADH")
        ]
        await asyncio.gather(*(handler.handle_message(m) for m in msgs))
        kinds = [k for k, _ in handler._frames]
        headers = [
            i
            for i, (k, f) in enumerate(handler._frames)
            if k == "json" and f.get("type") == "sweep_data"
        ]
        assert len(headers) == 2
        first = handler._frames[headers[0]][1]
        # every frame between header 1 and header 2 is binary and exactly header 1's frames
        assert headers[1] - headers[0] - 1 == first["chunks"]
        assert all(k == "bin" for k in kinds[headers[0] + 1 : headers[1]])

    @pytest.mark.asyncio
    async def test_difference_header_carries_chunk_count(self) -> None:
        r1, r2 = _reader_with_tree(), _reader_with_tree()
        id1, id2 = server._register_reader(r1), server._register_reader(r2)
        n = 300_000  # float32 values -> 1.2 MB -> 2 frames
        fake = {
            "diff_values": np.zeros(n, dtype=np.float32).tobytes(),
            "azimuth": np.zeros(4, dtype=np.float32).tobytes(),
            "range": np.zeros(4, dtype=np.float32).tobytes(),
            "n_az": 1,
            "n_range": n,
        }
        handler, _ = _make_handler()
        with patch("server.compute_difference", return_value=fake):
            await handler.handle_message(
                json.dumps(
                    {
                        "type": "get_difference",
                        "variable": "DBZH",
                        "file_id_1": id1,
                        "file_id_2": id2,
                    }
                )
            )
        header = next(m for m in handler._sent if m["type"] == "difference_result")
        assert header["chunks"] == 2
        assert len(_binary(handler)) == n * 4 + 32


# ---------------------------------------------------------------------------
# Render cache identity and concurrency
# ---------------------------------------------------------------------------


class TestRenderCacheToken:
    @pytest.mark.asyncio
    async def test_render_passes_file_and_generation_token(self) -> None:
        file_id = server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        renderer = MagicMock()
        renderer.render_sweep.return_value = {"image": "x", "metadata": {}}
        with (
            patch.object(server, "_shared_renderer", renderer),
            patch.object(server, "_RENDER_ACCEPTS_TOKEN", True),
        ):
            await handler.handle_message(
                json.dumps({"type": "render", "variable": "DBZH", "file_id": file_id})
            )
            first = renderer.render_sweep.call_args.kwargs["cache_token"]
            assert first == (file_id, 0)

            server._generations[file_id] += 1  # what `process` does after swapping data
            await handler.handle_message(
                json.dumps({"type": "render", "variable": "DBZH", "file_id": file_id})
            )
            assert renderer.render_sweep.call_args.kwargs["cache_token"] == (file_id, 1)
        assert [m for m in handler._sent if m["type"] == "render_result"]

    @pytest.mark.asyncio
    async def test_process_bumps_generation(self) -> None:
        reader = _reader_with_tree()
        file_id = server._register_reader(reader)
        new_tree = make_synthetic_datatree(n_azimuth=4, n_range=5)
        handler, _ = _make_handler()
        with patch.object(server._shared_processor, "process", return_value=new_tree):
            await handler.handle_message(
                json.dumps({"type": "process", "pipeline": {}, "file_id": file_id})
            )
        assert reader.datatree is new_tree
        assert server._generations[file_id] == 1


class TestConcurrentDispatch:
    @pytest.mark.asyncio
    async def test_ping_is_answered_while_slow_request_runs(self) -> None:
        release = asyncio.Event()

        async def slow(self, msg):
            await release.wait()
            await self.send({"type": "slow_done"})

        handler, ws = _make_handler()

        async def _messages():
            yield json.dumps({"type": "list_nexrad_l2_files"})
            yield json.dumps({"type": "ping"})
            while not any(m["type"] == "pong" for m in handler._sent):
                await asyncio.sleep(0.01)
            release.set()
            await asyncio.sleep(0.05)

        ws.__aiter__ = lambda self=None: _messages()
        ws.request = None
        ws.close = AsyncMock()
        with (
            patch.object(ConnectionHandler, "_handle_list_nexrad_l2_files", slow),
            patch.object(server, "ConnectionHandler", lambda w: handler),
        ):
            await server._handle_connection(ws)
        types = [m["type"] for m in handler._sent]
        assert types.index("pong") < types.index("slow_done")

    @pytest.mark.asyncio
    async def test_track_cells_requires_identify_first(self) -> None:
        file_id = server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        await handler.handle_message(
            json.dumps({"type": "track_cells", "variable": "DBZH", "file_id": file_id})
        )
        assert handler._sent[-1]["type"] == "error"
        assert "identify_cells" in handler._sent[-1]["message"]


# ---------------------------------------------------------------------------
# Plugin modules
# ---------------------------------------------------------------------------


@pytest.fixture()
def test_module():
    """Register throw-away modules in the global plugin registry."""
    from engine.plugins import ModuleError, Result, module, param

    before = dict(plugins.MODULES)

    @module(
        id="srv_test_count",
        title="Count",
        category="Test",
        requires=["DBZH"],
        params=[param("threshold", "number", 10.0, "Threshold", min=-50, max=80)],
    )
    def count(ctx, threshold):
        ctx.progress(50, "halfway")
        z = ctx.field("DBZH").values
        return Result().scalars({"above": int((z >= threshold).sum()), "sweep": ctx.sweep})

    @module(id="srv_test_fail", title="Fail", category="Test")
    def fail(ctx):
        raise ModuleError("nope, user-facing")

    @module(id="srv_test_crash", title="Crash", category="Test")
    def crash(ctx):
        raise RuntimeError("boom")

    @module(id="srv_test_needs_kdp", title="Needs KDP", category="Test", requires=["KDP"])
    def needs_kdp(ctx):
        return Result()

    yield
    plugins.MODULES.clear()
    plugins.MODULES.update(before)


class TestModules:
    @pytest.mark.asyncio
    async def test_list_modules(self, test_module) -> None:
        handler, _ = _make_handler()
        await handler.handle_message(json.dumps({"type": "list_modules"}))
        msg = handler._sent[0]
        assert msg["type"] == "modules_list"
        ids = {m["id"] for m in msg["modules"]}
        assert {"srv_test_count", "srv_test_fail"} <= ids
        assert isinstance(msg["errors"], list) and isinstance(msg["dirs"], list)

    @pytest.mark.asyncio
    async def test_list_modules_reload_rediscovers(self, test_module) -> None:
        handler, _ = _make_handler()
        with patch.object(plugins, "discover_modules") as discover:
            await handler.handle_message(json.dumps({"type": "list_modules", "reload": True}))
            discover.assert_called_once()
            await handler.handle_message(json.dumps({"type": "list_modules"}))
            discover.assert_called_once()  # no reload without the flag

    @pytest.mark.asyncio
    async def test_run_module_success(self, test_module) -> None:
        tree = make_synthetic_datatree(n_azimuth=20, n_range=30)
        file_id = server._register_reader(_reader_with_tree(tree))
        expected = int((tree["sweep_1"]["DBZH"].values >= 30.0).sum())
        handler, _ = _make_handler()
        await handler.handle_message(
            json.dumps(
                {
                    "type": "run_module",
                    "module": "srv_test_count",
                    "sweep": 1,
                    "params": {"threshold": 30},
                    "file_id": file_id,
                }
            )
        )
        result = [m for m in handler._sent if m["type"] == "module_result"]
        assert len(result) == 1
        msg = result[0]
        assert msg["module"] == "srv_test_count" and msg["sweep"] == 1
        assert msg["params"] == {"threshold": 30.0}
        assert msg["file_id"] == file_id
        assert "elapsed_ms" in msg
        items = {i["label"]: i["value"] for i in msg["blocks"][0]["items"]}
        assert items == {"above": expected, "sweep": 1}
        assert any(m["type"] == "progress" and m["message"] == "halfway" for m in handler._sent)

    @pytest.mark.asyncio
    async def test_run_module_defaults_use_most_recent_file(self, test_module) -> None:
        server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        await handler.handle_message(json.dumps({"type": "run_module", "module": "srv_test_count"}))
        msg = next(m for m in handler._sent if m["type"] == "module_result")
        assert msg["sweep"] == 0 and msg["params"] == {"threshold": 10.0}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("payload", "needle"),
        [
            ({"module": "does_not_exist"}, "Unknown module"),
            ({"module": "srv_test_fail"}, "nope, user-facing"),
            ({"module": "srv_test_crash"}, "boom"),
            ({"module": "srv_test_needs_kdp"}, "KDP"),
            ({"module": "srv_test_count", "sweep": -1}, "sweep"),
            ({"module": "srv_test_count", "sweep": 99}, "out of range"),
            ({"module": "srv_test_count", "params": "x"}, "params"),
            ({"module": "srv_test_count", "params": {"threshold": "abc"}}, "threshold"),
            ({"module": "srv_test_count", "file_id": "gone"}, "gone"),
            ({}, "module"),
        ],
    )
    async def test_module_error(self, test_module, payload, needle) -> None:
        server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        await handler.handle_message(json.dumps({"type": "run_module", **payload}))
        errors = [m for m in handler._sent if m["type"] == "module_error"]
        assert len(errors) == 1, handler._sent
        assert needle in errors[0]["message"]
        assert errors[0]["module"] == payload.get("module")
        assert not any(m["type"] in ("module_result", "error") for m in handler._sent)

    @pytest.mark.asyncio
    async def test_module_error_when_no_file_open(self, test_module) -> None:
        handler, _ = _make_handler()
        with patch.object(server, "_shared_reader", RadarReader()):
            await handler.handle_message(
                json.dumps({"type": "run_module", "module": "srv_test_count"})
            )
        errors = [m for m in handler._sent if m["type"] == "module_error"]
        assert len(errors) == 1
        assert "no file" in errors[0]["message"].lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("module", ["srv_test_count", "does_not_exist"])
    async def test_run_module_always_closes_progress(self, test_module, module) -> None:
        """Success and failure both end with progress=100 so the UI status bar clears."""
        server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        await handler.handle_message(json.dumps({"type": "run_module", "module": module}))
        progress = [m for m in handler._sent if m["type"] == "progress"]
        assert progress and progress[-1]["percent"] == 100
        assert handler._sent[-1] is progress[-1]

    @pytest.mark.asyncio
    async def test_arco_placeholder_sweep_loads_on_demand(self, test_module) -> None:
        import xarray as xr

        tree = make_synthetic_datatree(n_azimuth=6, n_range=7)
        reader = _reader_with_tree(tree)
        real = tree["sweep_1"].to_dataset()
        # Turn sweep_1 into an attrs-only placeholder like the lazy ARCO opener does.
        reader._dtree = xr.DataTree.from_dict(
            {
                "sweep_0": tree["sweep_0"].to_dataset(),
                "sweep_1": xr.Dataset(attrs={"_sweep_variables": ["DBZH"]}),
            }
        )
        reader._format = "nexrad_arco"
        reader._load_arco_sweep = lambda i: real  # type: ignore[method-assign]
        file_id = server._register_reader(reader)
        handler, _ = _make_handler()
        await handler.handle_message(
            json.dumps(
                {"type": "run_module", "module": "srv_test_count", "sweep": 1, "file_id": file_id}
            )
        )
        assert any(m["type"] == "module_result" for m in handler._sent), handler._sent


# ---------------------------------------------------------------------------
# Module fields (gridded products) + save_text_file
# ---------------------------------------------------------------------------


@pytest.fixture()
def field_module():
    from engine.plugins import Result, module

    before = dict(plugins.MODULES)

    @module(id="srv_test_field", title="Field", category="Test", requires=["DBZH"])
    def field(ctx):
        z = ctx.field("DBZH").values.astype(np.float32).copy()
        z[0, 0] = np.nan
        cls = np.where(z > 10, 2.0, 1.0).astype(np.float32)
        cls[0, 0] = np.nan
        res = Result().scalars({"n": int(z.size)})
        res.field("dbz2", z, units="dBZ", vmin=-10, vmax=60, cmap="turbo")
        res.field(
            "cls",
            cls,
            categories=[
                {"value": 1, "label": "Low", "color": "#3b82f6"},
                {"value": 2, "label": "High", "color": "#ef4444"},
            ],
        )
        return res

    @module(id="srv_test_badfield", title="Bad Field", category="Test")
    def badfield(ctx):
        return Result().field("oops", np.zeros((3, 4), dtype=np.float32))

    yield
    plugins.MODULES.clear()
    plugins.MODULES.update(before)


class TestModuleFields:
    @pytest.mark.asyncio
    async def test_field_sent_as_json_then_binary(self, field_module) -> None:
        tree = make_synthetic_datatree(n_azimuth=20, n_range=30)
        file_id = server._register_reader(_reader_with_tree(tree))
        handler, _ = _make_handler()
        await handler.handle_message(
            json.dumps(
                {"type": "run_module", "module": "srv_test_field", "sweep": 1, "file_id": file_id}
            )
        )
        kinds = [(k, f["type"] if k == "json" else None) for k, f in handler._frames]
        types = [t for k, t in kinds if k == "json" and t != "progress"]
        assert types == ["module_result", "module_field", "module_field"]
        result = next(m for m in handler._sent if m["type"] == "module_result")
        assert [b["type"] for b in result["blocks"]] == ["scalars", "field", "field"]
        fields = [m for m in handler._sent if m["type"] == "module_field"]
        assert [f["name"] for f in fields] == ["dbz2", "cls"]
        # Each header is immediately followed by exactly its frames.
        idx = [
            i
            for i, (k, f) in enumerate(handler._frames)
            if k == "json" and f["type"] == "module_field"
        ]
        assert idx[0] < idx[1]
        for pos, hdr in zip(idx, fields, strict=True):
            total = hdr["azimuth_bytes"] + hdr["range_bytes"] + hdr["values_bytes"]
            frames = []
            for k, f in handler._frames[pos + 1 :]:
                if k != "bin":
                    break
                frames.append(f)
            assert len(frames) == hdr["chunks"]
            blob = b"".join(frames)
            assert len(blob) == total
            assert hdr["module"] == "srv_test_field" and hdr["sweep"] == 1
            assert hdr["file_id"] == file_id
            assert (hdr["n_az"], hdr["n_range"]) == (20, 30)
            assert hdr["azimuth_bytes"] == 20 * 4 and hdr["range_bytes"] == 30 * 4
            assert hdr["values_bytes"] == 20 * 30 * 4
            values = np.frombuffer(blob[hdr["azimuth_bytes"] + hdr["range_bytes"] :], np.float32)
            assert values[0] == -9999.0  # NaN -> sentinel
            assert np.isfinite(values).all()
        assert (fields[0]["vmin"], fields[0]["vmax"], fields[0]["units"]) == (-10, 60, "dBZ")
        # categorical: vmin/vmax come from the observed values
        assert (fields[1]["vmin"], fields[1]["vmax"]) == (1.0, 2.0)

    @pytest.mark.asyncio
    async def test_field_shape_mismatch_is_module_error(self, field_module) -> None:
        server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        await handler.handle_message(
            json.dumps({"type": "run_module", "module": "srv_test_badfield"})
        )
        errors = [m for m in handler._sent if m["type"] == "module_error"]
        assert len(errors) == 1
        assert errors[0]["module"] == "srv_test_badfield"
        assert "srv_test_badfield" in errors[0]["message"] and "oops" in errors[0]["message"]
        assert not any(m["type"] in ("module_result", "module_field") for m in handler._sent)
        assert _binary(handler) == b""

    @pytest.mark.asyncio
    async def test_list_modules_has_availability(self, test_module) -> None:
        @plugins.module(
            id="srv_test_needs_pkg", title="Needs pkg", needs=["definitely_not_a_pkg_xyz"]
        )
        def needs_pkg(ctx):
            return plugins.Result()

        handler, _ = _make_handler()
        await handler.handle_message(json.dumps({"type": "list_modules"}))
        mods = {m["id"]: m for m in handler._sent[0]["modules"]}
        assert mods["srv_test_count"]["available"] is True
        assert mods["srv_test_count"]["unavailable_reason"] is None
        assert mods["srv_test_needs_pkg"]["available"] is False
        assert (
            "pip install definitely_not_a_pkg_xyz"
            in mods["srv_test_needs_pkg"]["unavailable_reason"]
        )
        await handler.handle_message(
            json.dumps({"type": "run_module", "module": "srv_test_needs_pkg"})
        )
        assert [m["type"] for m in handler._sent if m["type"] == "module_error"] == ["module_error"]


class TestSaveTextFile:
    @pytest.mark.asyncio
    async def test_save_ok(self, tmp_path) -> None:
        handler, _ = _make_handler()
        target = tmp_path / "out.csv"
        await handler.handle_message(
            json.dumps({"type": "save_text_file", "path": str(target), "content": "a,b\n1,é\n"})
        )
        assert handler._sent[-1] == {"type": "file_saved", "path": str(target)}
        assert target.read_text(encoding="utf-8") == "a,b\n1,é\n"
        assert [p.name for p in tmp_path.iterdir()] == ["out.csv"]  # no temp left behind

    @pytest.mark.asyncio
    async def test_refuse_overwrite_unless_flag(self, tmp_path) -> None:
        handler, _ = _make_handler()
        target = tmp_path / "out.csv"
        target.write_text("old")
        await handler.handle_message(
            json.dumps({"type": "save_text_file", "path": str(target), "content": "new"})
        )
        assert handler._sent[-1]["type"] == "error"
        assert "already exists" in handler._sent[-1]["message"]
        assert target.read_text() == "old"
        await handler.handle_message(
            json.dumps(
                {"type": "save_text_file", "path": str(target), "content": "new", "overwrite": True}
            )
        )
        assert handler._sent[-1]["type"] == "file_saved"
        assert target.read_text() == "new"

    @pytest.mark.asyncio
    async def test_oversize_rejected(self, tmp_path) -> None:
        handler, _ = _make_handler()
        target = tmp_path / "big.txt"
        content = "x" * (server.SAVE_TEXT_MAX_BYTES + 1)
        await handler.handle_message(
            json.dumps({"type": "save_text_file", "path": str(target), "content": content})
        )
        assert handler._sent[-1]["type"] == "error"
        assert "MB" in handler._sent[-1]["message"]
        assert not target.exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload",
        [
            {"path": 5, "content": "x"},
            {"path": "", "content": "x"},
            {"path": "/tmp/x.txt", "content": 5},
            {"path": "/definitely/not/a/dir/x.txt", "content": "x"},
        ],
    )
    async def test_invalid_inputs(self, payload) -> None:
        handler, _ = _make_handler()
        await handler.handle_message(json.dumps({"type": "save_text_file", **payload}))
        assert handler._sent[-1]["type"] == "error"


# ---------------------------------------------------------------------------
# Multi-radar mosaics
# ---------------------------------------------------------------------------


class TestMosaic:
    GRID: ClassVar[dict] = {
        "resolution_m": 1000,
        "max_range_km": 80,
        "z_max_m": 12000,
        "z_step_m": 1000,
    }

    @pytest.fixture(autouse=True)
    def _fresh_cache(self):
        from engine import mosaic

        mosaic.MOSAICS.clear()
        yield
        mosaic.MOSAICS.clear()

    @staticmethod
    def _open_two() -> tuple[str, str]:
        from tests.test_mosaic import A, B, flat, make_reader, make_volume

        fa = server._register_reader(make_reader(make_volume(*A, flat(20.0), name="LOW")))
        fb = server._register_reader(make_reader(make_volume(*B, flat(40.0), name="HIGH")))
        return fa, fb

    @staticmethod
    async def _create(handler, file_ids, **extra) -> dict:
        await handler.handle_message(
            json.dumps(
                {
                    "type": "create_mosaic",
                    "request_id": "r1",
                    "file_ids": file_ids,
                    "variable": "DBZH",
                    "product": "cappi",
                    "height_m": 2000,
                    "grid": TestMosaic.GRID,
                    "merge": {"enabled": True},
                    **extra,
                }
            )
        )
        return next(
            (m for m in handler._sent if m["type"] in ("mosaic_result", "mosaic_error")), {}
        )

    @pytest.mark.asyncio
    async def test_create_streams_png_contiguously(self) -> None:
        pytest.importorskip("radarx")
        fa, fb = self._open_two()
        handler, _ = _make_handler()
        msg = await self._create(handler, [fa, fb])
        assert msg["type"] == "mosaic_result" and msg["request_id"] == "r1"
        for key in (
            "mosaic_id",
            "product",
            "variable",
            "units",
            "height_m",
            "grid",
            "corners",
            "vmin",
            "vmax",
            "cmap",
            "legend",
            "stats",
            "radars",
            "overlap_fraction",
            "elapsed_ms",
            "png_bytes",
            "chunks",
        ):
            assert key in msg, key
        assert msg["product"] == "cappi" and msg["units"] == "dBZ" and msg["height_m"] == 2000
        assert [r["file_id"] for r in msg["radars"]] == [fa, fb]
        assert [r["name"] for r in msg["radars"]] == ["LOW", "HIGH"]
        assert len(msg["corners"]) == 4 and len(msg["legend"]) == 16
        # frames: header immediately followed by exactly `chunks` binary frames
        kinds = [k for k, _ in handler._frames if k == "bin" or _.get("type") == "mosaic_result"]
        i = next(n for n, (k, f) in enumerate(handler._frames) if k == "json" and f is msg)
        assert [k for k, _ in handler._frames[i + 1 : i + 1 + msg["chunks"]]] == ["bin"] * msg[
            "chunks"
        ]
        assert kinds.count("bin") == msg["chunks"]
        png = _binary(handler)
        assert len(png) == msg["png_bytes"] and png[:8] == b"\x89PNG\r\n\x1a\n"
        assert all(len(f) <= server.CHUNK_SIZE for k, f in handler._frames if k == "bin")
        progress = [m for m in handler._sent if m["type"] == "progress"]
        assert progress and progress[-1]["percent"] == 100
        assert handler._sent[-1] is progress[-1]

    @pytest.mark.asyncio
    async def test_large_png_is_chunked(self, monkeypatch) -> None:
        pytest.importorskip("radarx")
        fa, fb = self._open_two()
        monkeypatch.setattr(server, "CHUNK_SIZE", 2000)
        handler, _ = _make_handler()
        msg = await self._create(handler, [fa, fb])
        assert msg["chunks"] > 1
        assert len(_binary(handler)) == msg["png_bytes"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("payload", "needle"),
        [
            ({"file_ids": []}, "file_ids"),
            ({"file_ids": "abc"}, "file_ids"),
            ({"file_ids": ["nope"]}, "Unknown or closed"),
            ({"product": "weird"}, "product"),
            ({"grid": {"resolution_m": 10}}, "resolution_m"),
            ({"grid": "big"}, "'grid' must be an object"),
            ({"variable": "ZDR"}, "ZDR"),
        ],
    )
    async def test_errors_are_mosaic_errors(self, payload, needle) -> None:
        pytest.importorskip("radarx")
        fa, _ = self._open_two()
        handler, _ = _make_handler()
        msg = {"file_ids": [fa], **payload}
        await handler.handle_message(
            json.dumps({"type": "create_mosaic", "request_id": "q", "variable": "DBZH", **msg})
        )
        errors = [m for m in handler._sent if m["type"] == "mosaic_error"]
        assert len(errors) == 1 and errors[0]["request_id"] == "q"
        assert needle in errors[0]["message"]
        assert not [m for m in handler._sent if m["type"] == "error"]
        assert handler._sent[-1]["type"] == "progress" and handler._sent[-1]["percent"] == 100

    @pytest.mark.asyncio
    async def test_missing_radarx_message(self, monkeypatch) -> None:
        from engine import mosaic

        def _raise():
            raise mosaic.MosaicError(
                "Multi-radar mosaics need the Python package radarx: pip install radarx"
            )

        monkeypatch.setattr(mosaic, "_radarx", _raise)
        fid = server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        msg = await self._create(handler, [fid])
        assert msg["type"] == "mosaic_error" and "pip install radarx" in msg["message"]

    @pytest.mark.asyncio
    async def test_probe(self) -> None:
        pytest.importorskip("radarx")
        fa, fb = self._open_two()
        handler, _ = _make_handler()
        created = await self._create(handler, [fa, fb])
        handler._sent.clear()
        await handler.handle_message(
            json.dumps(
                {
                    "type": "mosaic_probe",
                    "mosaic_id": created["mosaic_id"],
                    "lon": -97.0,
                    "lat": 35.3,
                }
            )
        )
        res = handler._sent[-1]
        assert res["type"] == "mosaic_probe_result" and res["mosaic_id"] == created["mosaic_id"]
        assert res["value"] == pytest.approx(20.0, abs=1.5) and res["units"] == "dBZ"
        assert [r["name"] for r in res["radars"]] == ["LOW", "HIGH"]
        assert {"value", "range_km", "elevation_deg"} <= set(res["radars"][0])
        await handler.handle_message(
            json.dumps(
                {
                    "type": "mosaic_probe",
                    "mosaic_id": created["mosaic_id"],
                    "lon": 10.0,
                    "lat": 10.0,
                }
            )
        )
        assert handler._sent[-1]["value"] is None

    @pytest.mark.asyncio
    async def test_mosaic_volume_message(self) -> None:
        pytest.importorskip("radarx")
        fa, fb = self._open_two()
        handler, _ = _make_handler()
        created = await self._create(handler, [fa, fb], product="composite")
        assert created["has_volume"] is True and created["volume_shape"][0] == 12
        handler._sent.clear()
        handler._frames.clear()
        await handler.handle_message(
            json.dumps(
                {"type": "get_mosaic_volume", "request_id": "v1", "mosaic_id": created["mosaic_id"]}
            )
        )
        msg = next(m for m in handler._sent if m["type"] == "mosaic_volume")
        nz, ny, nx = msg["grid"]["nz"], msg["grid"]["ny"], msg["grid"]["nx"]
        assert msg["request_id"] == "v1" and msg["dtype"] == "uint8"
        assert msg["byte_length"] == nz * ny * nx and msg["chunks"] >= 1
        assert [r["name"] for r in msg["radars"]] == ["LOW", "HIGH"]
        assert all({"x_m", "y_m", "latitude", "longitude"} <= set(r) for r in msg["radars"])
        data = np.frombuffer(_binary(handler), dtype=np.uint8)
        assert data.size == msg["byte_length"] and data.any()
        step, off = msg["scale"]["step"], msg["scale"]["offset"]
        vals = off + (data[data > 0].astype(float) - 1.0) * step
        assert vals.min() > 15.0 and vals.max() < 45.0

    @pytest.mark.asyncio
    async def test_mosaic_volume_unavailable_is_an_error(self) -> None:
        pytest.importorskip("radarx")
        fa, fb = self._open_two()
        handler, _ = _make_handler()
        created = await self._create(handler, [fa, fb], keep_volume=False)
        assert created["has_volume"] is False
        handler._sent.clear()
        await handler.handle_message(
            json.dumps(
                {"type": "get_mosaic_volume", "request_id": "v2", "mosaic_id": created["mosaic_id"]}
            )
        )
        err = handler._sent[-1]
        assert err["type"] == "mosaic_error" and err["request_id"] == "v2"
        assert "not kept" in err["message"]

    @pytest.mark.asyncio
    async def test_probe_unknown_mosaic(self) -> None:
        handler, _ = _make_handler()
        await handler.handle_message(
            json.dumps({"type": "mosaic_probe", "mosaic_id": "zzz", "lon": 0, "lat": 0})
        )
        assert handler._sent[-1]["type"] == "mosaic_error"
        assert "Unknown or expired" in handler._sent[-1]["message"]

    @pytest.mark.asyncio
    async def test_export_netcdf_and_png(self, tmp_path) -> None:
        pytest.importorskip("radarx")
        import xarray as xr

        fa, fb = self._open_two()
        handler, _ = _make_handler()
        created = await self._create(handler, [fa, fb])
        mid = created["mosaic_id"]
        nc = tmp_path / "mosaic.nc"
        await handler.handle_message(
            json.dumps(
                {"type": "export_mosaic", "mosaic_id": mid, "format": "netcdf", "path": str(nc)}
            )
        )
        assert handler._sent[-1] == {"type": "file_saved", "path": str(nc)}
        with xr.open_dataset(nc) as ds:
            assert "DBZH_cappi" in ds and ds["DBZH_cappi"].attrs["units"] == "dBZ"
            assert ds.sizes["lon"] == created["grid"]["nx"]
        assert not list(tmp_path.glob(".*.tmp"))
        # existing file: refused until overwrite=true
        await handler.handle_message(
            json.dumps(
                {"type": "export_mosaic", "mosaic_id": mid, "format": "netcdf", "path": str(nc)}
            )
        )
        assert handler._sent[-1]["type"] == "mosaic_error"
        assert "already exists" in handler._sent[-1]["message"]
        await handler.handle_message(
            json.dumps(
                {
                    "type": "export_mosaic",
                    "mosaic_id": mid,
                    "format": "netcdf",
                    "path": str(nc),
                    "overwrite": True,
                }
            )
        )
        assert handler._sent[-1]["type"] == "file_saved"
        png = tmp_path / "mosaic.png"
        await handler.handle_message(
            json.dumps(
                {"type": "export_mosaic", "mosaic_id": mid, "format": "png", "path": str(png)}
            )
        )
        assert handler._sent[-1] == {"type": "file_saved", "path": str(png)}
        assert png.read_bytes()[:4] == b"\x89PNG"
        assert len((tmp_path / "mosaic.pgw").read_text().split()) == 6
        assert (tmp_path / "mosaic.prj").exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload",
        [
            {"mosaic_id": "zzz", "format": "netcdf", "path": "/tmp/x.nc"},
            {"mosaic_id": "", "format": "netcdf", "path": "/tmp/x.nc"},
            {"mosaic_id": "zzz", "format": "gif", "path": "/tmp/x.gif"},
            {"mosaic_id": "zzz", "format": "png", "path": ""},
        ],
    )
    async def test_export_errors(self, payload) -> None:
        handler, _ = _make_handler()
        await handler.handle_message(json.dumps({"type": "export_mosaic", **payload}))
        assert handler._sent[-1]["type"] == "mosaic_error"

    @pytest.mark.asyncio
    async def test_export_bad_folder(self) -> None:
        pytest.importorskip("radarx")
        fa, fb = self._open_two()
        handler, _ = _make_handler()
        created = await self._create(handler, [fa, fb])
        await handler.handle_message(
            json.dumps(
                {
                    "type": "export_mosaic",
                    "mosaic_id": created["mosaic_id"],
                    "format": "png",
                    "path": "/definitely/not/here/x.png",
                }
            )
        )
        assert handler._sent[-1]["type"] == "mosaic_error"
        assert "folder does not exist" in handler._sent[-1]["message"]


class TestAnalysisReplies:
    """Replies of the analysis handlers carry what the UI needs to match them to a request."""

    @pytest.mark.asyncio
    async def test_scatter_histogram_region_stats_echo_the_sweep(self) -> None:
        file_id = server._register_reader(
            _reader_with_tree(make_synthetic_datatree(n_azimuth=20, n_range=30))
        )
        for msg, reply in (
            ({"type": "get_scatter_data", "var1": "DBZH", "var2": "VRADH"}, "scatter_data_result"),
            ({"type": "get_histogram", "variable": "DBZH"}, "histogram_result"),
            ({"type": "get_region_stats", "variable": "DBZH"}, "region_stats_result"),
        ):
            handler, _ = _make_handler()
            await handler.handle_message(json.dumps({**msg, "sweep": 1, "file_id": file_id}))
            out = handler._sent[-1]
            assert out["type"] == reply
            assert out["sweep"] == 1

    @pytest.mark.asyncio
    async def test_vertical_profile_echoes_the_probe_location(self) -> None:
        file_id = server._register_reader(
            _reader_with_tree(make_synthetic_datatree(n_azimuth=20, n_range=30))
        )
        handler, _ = _make_handler()
        await handler.handle_message(
            json.dumps(
                {
                    "type": "get_vertical_profile",
                    "lat": 10.5,
                    "lon": 20.25,
                    "variable": "DBZH",
                    "file_id": file_id,
                }
            )
        )
        out = next(m for m in handler._sent if m["type"] == "vertical_profile_result")
        assert out["lat"] == 10.5 and out["lon"] == 20.25

    @pytest.mark.asyncio
    async def test_run_qc_clutter_uses_rhohv_when_the_sweep_has_it(self) -> None:
        tree = make_synthetic_datatree(n_azimuth=20, n_range=30)
        sweep0 = tree["sweep_0"].to_dataset()
        rhv = np.full(sweep0["DBZH"].shape, 0.95, dtype=np.float32)
        rhv[:, :5] = 0.2  # non-meteorological gates
        sweep0["RHOHV"] = (("azimuth", "range"), rhv)
        tree["sweep_0"] = xr.DataTree(sweep0)
        file_id = server._register_reader(_reader_with_tree(tree))

        async def _run(sweep: int) -> np.ndarray:
            handler, _ = _make_handler()
            await handler.handle_message(
                json.dumps(
                    {
                        "type": "run_qc",
                        "variable": "DBZH",
                        "sweep": sweep,
                        "file_id": file_id,
                        "steps": [
                            {
                                "name": "remove_clutter",
                                "params": {"texture_threshold": 1.0, "rhohv_threshold": 0.7},
                            }
                        ],
                    }
                )
            )
            header = next(m for m in handler._sent if m.get("type") == "qc_result")
            buf = _binary(handler)
            offset = header["azimuth_bytes"] + header["range_bytes"]
            return np.frombuffer(buf, dtype=np.float32, offset=offset)

        with_rhv = (await _run(0)).reshape(20, 30)
        without = (await _run(1)).reshape(20, 30)  # sweep 1 has no RHOHV: texture only
        assert (with_rhv[:, :5] == np.float32(-9999.0)).all()
        assert (without > -9998).all()


class TestProgressAlwaysCloses:
    """Handlers that announce '10 %' and never finish used to leave the status bar stuck."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload",
        [
            {"type": "get_vertical_profile", "lat": 45.0, "lon": -100.0, "variable": "DBZH"},
            {"type": "get_vertical_profile", "lat": 45.0, "lon": -100.0, "variable": "NOPE"},
        ],
    )
    async def test_progress_is_closed_after_any_handler(self, payload) -> None:
        server._register_reader(_reader_with_tree())
        handler, _ = _make_handler()
        await handler.handle_message(json.dumps(payload))
        progress = [m for m in handler._sent if m["type"] == "progress"]
        assert progress, handler._sent
        assert progress[0]["percent"] < 100  # the handler did announce an unfinished run...
        assert progress[-1]["percent"] == 100  # ...and it is closed (success or error)

    @pytest.mark.asyncio
    async def test_no_extra_progress_when_handler_already_finished_it(self) -> None:
        handler, _ = _make_handler()
        await handler.handle_message(json.dumps({"type": "ping"}))
        assert not [m for m in handler._sent if m["type"] == "progress"]


# ---------------------------------------------------------------------------
# Derived variables (run a module on all sweeps)
# ---------------------------------------------------------------------------

ELEV4 = [0.5, 1.5, 2.5, 3.5]
CATS = [
    {"value": 1, "label": "Low", "color": "#3b82f6"},
    {"value": 2, "label": "High", "color": "#ef4444"},
]


@pytest.fixture()
def derived_modules():
    from engine.plugins import ModuleError, Result, module, param

    before = dict(plugins.MODULES)
    calls: list[int] = []

    @module(
        id="srv_derived_scale",
        title="Scale (test)",
        category="Test",
        requires=["DBZH"],
        params=[param("factor", "number", 1.0, "Factor", min=0, max=100)],
    )
    def scale(ctx, factor):
        calls.append(ctx.sweep)
        ctx.progress(50, "half")
        z = ctx.field("DBZH").values.astype(np.float32).copy()
        z[0, 0] = np.nan
        res = Result().scalars({"sweep": ctx.sweep})
        res.table("t", ["a"], [{"a": 1}])
        res.field("Scaled (radarx)", z * factor, units="dBZ", vmin=-10, vmax=60, cmap="turbo")
        res.field("Class", np.where(z > 10, 2.0, 1.0), categories=CATS)
        return res

    @module(id="srv_derived_other", title="Other (test)", category="Test", requires=["DBZH"])
    def other(ctx):
        return Result().field("Other", ctx.field("DBZH").values * 0.0 + ctx.sweep, units="x")

    @module(id="srv_derived_picky", title="Picky (test)", category="Test", requires=["DBZH"])
    def picky(ctx):
        if ctx.sweep == 2:
            raise ModuleError("sweep two is cursed")
        return Result().field("P", ctx.field("DBZH").values, units="dBZ")

    @module(id="srv_derived_vol", title="Volume (test)", category="Test", scope="volume")
    def vol(ctx):
        return Result()

    @module(id="srv_derived_badfield", title="Bad (test)", category="Test", requires=["DBZH"])
    def bad(ctx):
        return Result().field("oops", np.zeros((3, 4), dtype=np.float32))

    yield calls
    plugins.MODULES.clear()
    plugins.MODULES.update(before)


def _open_synthetic(elevations=None, n_az: int = 20, n_range: int = 30):
    tree = make_synthetic_datatree(n_azimuth=n_az, n_range=n_range, elevations=elevations or ELEV4)
    reader = _reader_with_tree(tree)
    return server._register_reader(reader), reader


async def _run_all(handler, file_id, module="srv_derived_scale", **extra):
    await handler.handle_message(
        json.dumps(
            {
                "type": "run_module",
                "module": module,
                "all_sweeps": True,
                "file_id": file_id,
                **extra,
            }
        )
    )


def _types(handler, *, skip_progress: bool = True) -> list[str]:
    return [m["type"] for m in handler._sent if not (skip_progress and m["type"] == "progress")]


async def _fetch_sweep(handler, file_id, variable, sweep):
    """get_sweep_data -> (header, azimuth, range, values) or (error_message,)."""
    start = len(handler._frames)
    await handler.handle_message(
        json.dumps(
            {"type": "get_sweep_data", "variable": variable, "sweep": sweep, "file_id": file_id}
        )
    )
    new = handler._frames[start:]
    errors = [f for k, f in new if k == "json" and f["type"] == "error"]
    if errors:
        return (errors[0]["message"],)
    hdr = next(f for k, f in new if k == "json" and f["type"] == "sweep_data")
    blob = b"".join(f for k, f in new if k == "bin")
    assert len(blob) == hdr["azimuth_bytes"] + hdr["range_bytes"] + hdr["values_bytes"]
    az = np.frombuffer(blob[: hdr["azimuth_bytes"]], np.float32)
    rng = np.frombuffer(
        blob[hdr["azimuth_bytes"] : hdr["azimuth_bytes"] + hdr["range_bytes"]], np.float32
    )
    vals = np.frombuffer(blob[hdr["azimuth_bytes"] + hdr["range_bytes"] :], np.float32)
    return hdr, az, rng, vals.reshape(hdr["n_az"], hdr["n_range"])


class TestDerivedVariables:
    @pytest.mark.asyncio
    async def test_all_sweeps_run_registers_and_lists_derived_variables(self, derived_modules):
        file_id, reader = _open_synthetic()
        handler, _ = _make_handler()
        await _run_all(handler, file_id, params={"factor": 2})
        assert _types(handler) == ["module_volume_result", "derived_variables"]
        assert derived_modules == [0, 1, 2, 3]  # one sweep at a time, in order

        res = handler._sent[[m["type"] for m in handler._sent].index("module_volume_result")]
        assert res["module"] == "srv_derived_scale" and res["file_id"] == file_id
        assert res["params"] == {"factor": 2.0} and res["evicted"] == []
        assert res["elapsed_ms"] >= 0
        by_name = {d["name"]: d for d in res["derived"]}
        assert set(by_name) == {"srv_derived_scale:scaled", "srv_derived_scale:class"}
        scaled = by_name["srv_derived_scale:scaled"]
        assert scaled["label"] == "Scaled (radarx)" and scaled["field"] == "Scaled (radarx)"
        assert (scaled["units"], scaled["vmin"], scaled["vmax"], scaled["cmap"]) == (
            "dBZ",
            -10,
            60,
            "turbo",
        )
        assert scaled["sweeps"] == [0, 1, 2, 3] and scaled["skipped"] == []
        assert scaled["categories"] is None
        cls = by_name["srv_derived_scale:class"]
        assert cls["categories"] == CATS and (cls["vmin"], cls["vmax"]) == (1.0, 2.0)
        # summaries are only the scalar blocks, per sweep
        assert [s["sweep"] for s in res["summaries"]] == [0, 1, 2, 3]
        assert res["summaries"][2]["blocks_scalars"] == {"sweep": 2}

        listing = next(m for m in handler._sent if m["type"] == "derived_variables")
        assert listing["file_id"] == file_id
        assert {v["name"] for v in listing["variables"]} == set(by_name)
        v = next(v for v in listing["variables"] if v["name"] == "srv_derived_scale:scaled")
        assert v["sweeps"] == [0, 1, 2, 3] and v["module"] == "srv_derived_scale"
        assert reader.derived.nbytes == 2 * 4 * 20 * 30 * 4

        # list_derived gives the same listing on request
        handler._sent.clear()
        await handler.handle_message(json.dumps({"type": "list_derived", "file_id": file_id}))
        assert handler._sent == [listing]

    @pytest.mark.asyncio
    async def test_progress_is_per_sweep_and_closes_at_100(self, derived_modules):
        file_id, _ = _open_synthetic()
        handler, _ = _make_handler()
        await _run_all(handler, file_id)
        progress = [m for m in handler._sent if m["type"] == "progress"]
        assert progress[-1]["percent"] == 100 and handler._sent[-1] is progress[-1]
        assert all(p["percent"] < 100 for p in progress[:-1])
        pcts = [p["percent"] for p in progress]
        assert pcts == sorted(pcts)
        msgs = {p["message"] for p in progress}
        assert {f"Scale: sweep {i}/4" for i in (1, 2, 3, 4)} <= msgs

    @pytest.mark.asyncio
    async def test_get_sweep_data_serves_derived_with_extra_header_keys(self, derived_modules):
        file_id, reader = _open_synthetic()
        handler, _ = _make_handler()
        await _run_all(handler, file_id, params={"factor": 2})
        name = "srv_derived_scale:scaled"
        hdr, az, rng, vals = await _fetch_sweep(handler, file_id, name, 2)
        assert hdr["type"] == "sweep_data" and hdr["variable"] == name and hdr["sweep"] == 2
        assert hdr["derived"] is True and hdr["label"] == "Scaled (radarx)"
        assert (hdr["units"], hdr["vmin"], hdr["vmax"], hdr["cmap"], hdr["categories"]) == (
            "dBZ",
            -10.0,
            60.0,
            "turbo",
            None,
        )
        assert (hdr["n_az"], hdr["n_range"]) == (20, 30) and hdr["max_range"] > 0
        ds = reader.get_sweep_dataset(2, mask=False)
        np.testing.assert_allclose(az, ds["azimuth"].values.astype(np.float32))
        np.testing.assert_allclose(rng, ds["range"].values.astype(np.float32))
        expected = ds["DBZH"].values.astype(np.float32) * 2
        assert vals[0, 0] == -9999.0  # NaN -> sentinel
        np.testing.assert_allclose(vals.ravel()[1:], expected.ravel()[1:], rtol=1e-6)

        hdr, *_ = await _fetch_sweep(handler, file_id, "srv_derived_scale:class", 0)
        assert hdr["categories"] == CATS and hdr["cmap"] is None and hdr["derived"] is True
        assert (hdr["vmin"], hdr["vmax"]) == (1.0, 2.0)

        # a normal variable keeps its normal header
        hdr, *_ = await _fetch_sweep(handler, file_id, "DBZH", 0)
        assert "derived" not in hdr and "label" not in hdr

    @pytest.mark.asyncio
    async def test_derived_name_missing_on_sweep_and_unknown_names(self, derived_modules):
        file_id, _ = _open_synthetic()
        handler, _ = _make_handler()
        await _run_all(handler, file_id, module="srv_derived_picky")
        res = next(m for m in handler._sent if m["type"] == "module_volume_result")
        (entry,) = res["derived"]
        assert entry["name"] == "srv_derived_picky:p" and entry["sweeps"] == [0, 1, 3]
        assert [s["sweep"] for s in entry["skipped"]] == [2]
        assert "cursed" in entry["skipped"][0]["reason"]

        (msg,) = await _fetch_sweep(handler, file_id, "srv_derived_picky:p", 2)
        assert "Variable 'srv_derived_picky:p' not in sweep 2" in msg and "Available:" in msg
        # derived names that DO exist on that sweep are offered
        await _run_all(handler, file_id, module="srv_derived_other")
        (msg,) = await _fetch_sweep(handler, file_id, "srv_derived_picky:p", 2)
        assert "srv_derived_other:other" in msg and "DBZH" in msg
        # unknown names behave as before
        (msg,) = await _fetch_sweep(handler, file_id, "nope:nope", 0)
        assert "Variable 'nope:nope' not in sweep 0" in msg
        assert "srv_derived_other" not in msg  # exactly the message an unknown name always had
        (msg,) = await _fetch_sweep(handler, file_id, "NOPE", 0)
        assert "Variable 'NOPE' not in sweep 0" in msg

    @pytest.mark.asyncio
    async def test_split_cut_sweeps_are_skipped_with_a_reason(self, derived_modules):
        tree = make_synthetic_datatree(n_azimuth=20, n_range=30, elevations=ELEV4)
        # NEXRAD split cuts: odd sweeps are Doppler-only.
        for i in (1, 3):
            tree[f"sweep_{i}"] = tree[f"sweep_{i}"].to_dataset().drop_vars("DBZH")
        file_id = server._register_reader(_reader_with_tree(tree))
        handler, _ = _make_handler()
        await _run_all(handler, file_id)
        res = next(m for m in handler._sent if m["type"] == "module_volume_result")
        for entry in res["derived"]:
            assert entry["sweeps"] == [0, 2]
            assert [s["sweep"] for s in entry["skipped"]] == [1, 3]
            for s in entry["skipped"]:
                assert "DBZH" in s["reason"] and "VRADH" in s["reason"]
        assert [s["sweep"] for s in res["summaries"]] == [0, 2]
        (msg,) = await _fetch_sweep(handler, file_id, "srv_derived_scale:scaled", 1)
        assert "not in sweep 1" in msg

    @pytest.mark.asyncio
    async def test_every_sweep_failing_is_a_module_error(self, derived_modules):
        file_id, reader = _open_synthetic()
        handler, _ = _make_handler()
        await _run_all(handler, file_id, module="srv_derived_badfield")
        errors = [m for m in handler._sent if m["type"] == "module_error"]
        assert len(errors) == 1 and errors[0]["module"] == "srv_derived_badfield"
        assert "any sweep" in errors[0]["message"] and "oops" in errors[0]["message"]
        assert "module_volume_result" not in _types(handler)
        assert _types(handler)[-1:] == ["module_error"]
        assert handler._sent[-1]["type"] == "progress" and handler._sent[-1]["percent"] == 100
        assert getattr(reader, "derived", None) is None or reader.derived.variables() == []

    @pytest.mark.asyncio
    async def test_volume_scope_module_is_rejected(self, derived_modules):
        file_id, _ = _open_synthetic()
        handler, _ = _make_handler()
        await _run_all(handler, file_id, module="srv_derived_vol")
        errors = [m for m in handler._sent if m["type"] == "module_error"]
        assert [e["message"] for e in errors] == [
            "This module computes a whole-volume product and cannot be run per sweep"
        ]
        assert errors[0]["module"] == "srv_derived_vol"
        assert "module_volume_result" not in _types(handler)
        assert handler._sent[-1] == {
            "type": "progress",
            "percent": 100,
            "message": "Module finished",
        }
        # ...but it still runs normally on one sweep
        handler._sent.clear()
        await handler.handle_message(
            json.dumps({"type": "run_module", "module": "srv_derived_vol", "file_id": file_id})
        )
        assert "module_result" in _types(handler)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("extra", "needle"),
        [
            ({"all_sweeps": "yes"}, "all_sweeps"),
            ({"all_sweeps": True, "params": {"factor": "abc"}}, "factor"),
            ({"all_sweeps": True, "module": "does_not_exist"}, "Unknown module"),
            ({"all_sweeps": True, "file_id": "gone"}, "gone"),
        ],
    )
    async def test_all_sweeps_validation_errors(self, derived_modules, extra, needle):
        file_id, _ = _open_synthetic()
        handler, _ = _make_handler()
        payload = {"type": "run_module", "module": "srv_derived_scale", "file_id": file_id, **extra}
        await handler.handle_message(json.dumps(payload))
        errors = [m for m in handler._sent if m["type"] == "module_error"]
        assert len(errors) == 1 and needle in errors[0]["message"]
        assert handler._sent[-1]["percent"] == 100

    @pytest.mark.asyncio
    async def test_all_sweeps_with_no_file_open(self, derived_modules):
        handler, _ = _make_handler()
        await handler.handle_message(
            json.dumps({"type": "run_module", "module": "srv_derived_scale", "all_sweeps": True})
        )
        errors = [m for m in handler._sent if m["type"] == "module_error"]
        assert len(errors) == 1 and "No file is open" in errors[0]["message"]

    @pytest.mark.asyncio
    async def test_rerun_replaces_variable_for_all_sweeps(self, derived_modules):
        file_id, reader = _open_synthetic()
        handler, _ = _make_handler()
        await _run_all(handler, file_id, module="srv_derived_picky")
        assert reader.derived.variables()[0]["sweeps"] == [0, 1, 3]
        # Same variable name; this time sweep 2 is also computed by a re-registered module.
        plugins.MODULES["srv_derived_picky"].func = lambda ctx: plugins.Result().field(
            "P", ctx.field("DBZH").values * 0 + 7, units="dBZ"
        )
        await _run_all(handler, file_id, module="srv_derived_picky")
        (var,) = reader.derived.variables()
        assert var["sweeps"] == [0, 1, 2, 3]
        _, _, _, vals = await _fetch_sweep(handler, file_id, "srv_derived_picky:p", 1)
        assert (vals == 7).all()
        listing = [m for m in handler._sent if m["type"] == "derived_variables"][-1]
        assert listing["variables"][0]["sweeps"] == [0, 1, 2, 3]

    @pytest.mark.asyncio
    async def test_clear_derived_one_or_all(self, derived_modules):
        file_id, reader = _open_synthetic()
        handler, _ = _make_handler()
        await _run_all(handler, file_id)  # two variables
        handler._sent.clear()
        await handler.handle_message(
            json.dumps(
                {"type": "clear_derived", "file_id": file_id, "name": "srv_derived_scale:class"}
            )
        )
        (msg,) = handler._sent
        assert msg["type"] == "derived_variables" and msg["file_id"] == file_id
        assert [v["name"] for v in msg["variables"]] == ["srv_derived_scale:scaled"]
        (err,) = await _fetch_sweep(handler, file_id, "srv_derived_scale:class", 0)
        assert "not in sweep 0" in err
        handler._sent.clear()
        await handler.handle_message(json.dumps({"type": "clear_derived", "file_id": file_id}))
        assert handler._sent[0]["variables"] == [] and reader.derived.nbytes == 0
        handler._sent.clear()
        await handler.handle_message(
            json.dumps({"type": "clear_derived", "file_id": file_id, "name": 5})
        )
        assert handler._sent[0]["type"] == "error"

    @pytest.mark.asyncio
    async def test_derived_dropped_on_close_file(self, derived_modules):
        file_id, reader = _open_synthetic()
        other_id, other = _open_synthetic()
        handler, _ = _make_handler()
        await _run_all(handler, file_id)
        await _run_all(handler, other_id)
        store = reader.derived
        assert store.nbytes > 0
        await handler.handle_message(json.dumps({"type": "close_file", "file_id": file_id}))
        assert store.nbytes == 0 and store.variables() == []
        assert other.derived.nbytes > 0  # another file is untouched
        handler._sent.clear()
        await handler.handle_message(json.dumps({"type": "list_derived", "file_id": file_id}))
        assert handler._sent[0]["type"] == "error"
        (err,) = await _fetch_sweep(handler, file_id, "srv_derived_scale:scaled", 0)
        assert "file_id" in err

    @pytest.mark.asyncio
    async def test_derived_not_served_after_the_tree_is_replaced(self, derived_modules):
        file_id, reader = _open_synthetic()
        handler, _ = _make_handler()
        await _run_all(handler, file_id)
        # what `process` / a re-open does: swap in a different tree
        reader._dtree = make_synthetic_datatree(n_azimuth=20, n_range=30, elevations=ELEV4)
        (err,) = await _fetch_sweep(handler, file_id, "srv_derived_scale:scaled", 0)
        assert "not in sweep 0" in err
        handler._sent.clear()
        await handler.handle_message(json.dumps({"type": "list_derived", "file_id": file_id}))
        assert handler._sent[0]["variables"] == []

    @pytest.mark.asyncio
    async def test_process_drops_derived_and_notifies(self, derived_modules):
        file_id, reader = _open_synthetic()
        handler, _ = _make_handler()
        await _run_all(handler, file_id)
        handler._sent.clear()
        with patch.object(server._shared_processor, "process", lambda tree, pipeline, cb: tree):
            await handler.handle_message(
                json.dumps({"type": "process", "pipeline": {}, "file_id": file_id})
            )
        notice = next(m for m in handler._sent if m["type"] == "derived_variables")
        assert notice["variables"] == [] and notice["file_id"] == file_id
        assert sorted(notice["dropped"]) == ["srv_derived_scale:class", "srv_derived_scale:scaled"]
        assert reader.derived.nbytes == 0

    @pytest.mark.asyncio
    async def test_eviction_is_reported_and_whole_variables_only(
        self, derived_modules, monkeypatch
    ):
        one_variable = 4 * 20 * 30 * 4
        # room for exactly one variable (a bit more than one, less than two)
        monkeypatch.setenv("XRADAR_DERIVED_MAX_MB", str(one_variable * 1.5 / (1024 * 1024)))
        file_id, _ = _open_synthetic()
        handler, _ = _make_handler()
        await _run_all(handler, file_id, module="srv_derived_other")
        handler._sent.clear()
        await _run_all(handler, file_id, module="srv_derived_picky")
        res = next(m for m in handler._sent if m["type"] == "module_volume_result")
        assert res["evicted"] == ["srv_derived_other:other"]
        listing = next(m for m in handler._sent if m["type"] == "derived_variables")
        assert [v["name"] for v in listing["variables"]] == ["srv_derived_picky:p"]
        assert listing["evicted"] == ["srv_derived_other:other"]
        (err,) = await _fetch_sweep(handler, file_id, "srv_derived_other:other", 0)
        assert "not in sweep 0" in err

    @pytest.mark.asyncio
    async def test_variable_larger_than_budget_is_a_module_error(
        self, derived_modules, monkeypatch
    ):
        monkeypatch.setenv("XRADAR_DERIVED_MAX_MB", "0.001")
        file_id, reader = _open_synthetic()
        handler, _ = _make_handler()
        await _run_all(handler, file_id, module="srv_derived_other")
        errors = [m for m in handler._sent if m["type"] == "module_error"]
        assert len(errors) == 1 and "budget" in errors[0]["message"]
        assert "module_volume_result" not in _types(handler)
        assert reader.derived.variables() == []

    @pytest.mark.asyncio
    async def test_result_discarded_if_file_closed_during_the_run(self, derived_modules):
        file_id, reader = _open_synthetic()
        handler, _ = _make_handler()

        def closing(ctx):
            server._file_readers.pop(file_id, None)  # closed from another connection
            return plugins.Result().field("Z", ctx.field("DBZH").values, units="dBZ")

        plugins.MODULES["srv_derived_other"].func = closing
        await _run_all(handler, file_id, module="srv_derived_other")
        errors = [m for m in handler._sent if m["type"] == "module_error"]
        assert len(errors) == 1 and "closed or changed" in errors[0]["message"]
        assert getattr(reader, "derived", None) is None or reader.derived.variables() == []

    @pytest.mark.asyncio
    async def test_cancelled_run_stops_after_the_current_sweep(self, derived_modules):
        import threading

        file_id, reader = _open_synthetic()
        handler, _ = _make_handler()
        started, release = threading.Event(), threading.Event()
        seen: list[int] = []

        def slow(ctx):
            seen.append(ctx.sweep)
            started.set()
            release.wait(10)
            return plugins.Result().field("Z", ctx.field("DBZH").values, units="dBZ")

        plugins.MODULES["srv_derived_other"].func = slow
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(
            handler.handle_message(
                json.dumps(
                    {
                        "type": "run_module",
                        "module": "srv_derived_other",
                        "all_sweeps": True,
                        "file_id": file_id,
                    }
                )
            )
        )
        await loop.run_in_executor(None, started.wait, 10)
        task.cancel()  # the connection went away
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        for _ in range(100):  # let the worker thread notice
            await asyncio.sleep(0.02)
            if seen == [0]:
                break
        await asyncio.sleep(0.1)
        assert seen == [0]  # sweeps 1..3 never ran
        assert getattr(reader, "derived", None) is None or reader.derived.variables() == []


# ---------------------------------------------------------------------------
# 3-D wind volume (get_wind_volume)
# ---------------------------------------------------------------------------


class TestWindVolume:
    @staticmethod
    def _record():
        from engine import multidoppler as mdop

        nz, ny, nx = 3, 4, 5
        rng = np.random.default_rng(1)
        fields = rng.normal(size=(len(mdop.FIELD_ORDER), nz, ny, nx)).astype(np.float32)
        rec = mdop.WindRecord(
            wind_id="w-test",
            fields=fields,
            x=np.arange(nx) * 1000.0 - 2000.0,
            y=np.arange(ny) * 1000.0 - 1500.0,
            z=np.array([1000.0, 2000.0, 3000.0]),
            origin=(35.0, -97.0),
            radars=[{"name": "A", "latitude": 35.0, "longitude": -97.0, "altitude_m": 100.0}],
            summary={"method": "analytic", "analysis_time": None},
            warnings=[],
        )
        mdop.WIND_RESULTS.put(rec)
        return rec

    @pytest.mark.asyncio
    async def test_streams_selected_fields(self) -> None:
        from engine import multidoppler as mdop

        rec = self._record()
        handler, _ = _make_handler()
        await handler.handle_message(
            json.dumps({"type": "get_wind_volume", "request_id": "w1", "wind_id": rec.wind_id})
        )
        msg = next(m for m in handler._sent if m["type"] == "wind_volume")
        names = [f["name"] for f in msg["fields"]]
        assert names[:4] == ["u", "v", "w", "speed"]
        assert "dbz" in names and "crossing_angle" in names
        assert msg["shape"] == [len(names), 3, 4, 5] and msg["grid"]["nz"] == 3
        assert msg["radars"][0]["x_m"] == pytest.approx(0.0, abs=1e-3)
        data = np.frombuffer(_binary(handler), dtype=np.float32).reshape(msg["shape"])
        assert msg["byte_length"] == data.nbytes
        for i, n in enumerate(names):
            np.testing.assert_array_equal(data[i], rec.fields[mdop.FIELD_ORDER.index(n)])

    @pytest.mark.asyncio
    async def test_unknown_wind_is_an_error(self) -> None:
        handler, _ = _make_handler()
        await handler.handle_message(
            json.dumps({"type": "get_wind_volume", "request_id": "w2", "wind_id": "nope"})
        )
        err = handler._sent[-1]
        assert err["type"] == "multidoppler_error" and err["request_id"] == "w2"
