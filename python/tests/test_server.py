"""Tests for WebSocket server message routing and error handling."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

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
        assert handler._sent[-1]["type"] == "render_result"

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
        assert handler._sent[-1]["type"] == "module_error"
        assert "no file" in handler._sent[-1]["message"].lower()

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
        assert handler._sent[-1]["type"] == "module_error"


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
