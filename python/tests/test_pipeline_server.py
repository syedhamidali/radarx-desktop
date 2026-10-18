"""WebSocket handlers of the processing-pipeline feature (preview, run, recipes, module inputs)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import numpy as np
import pytest

import server
from engine import derived, plugins
from engine.plugins import Result
from engine.reader import RadarReader
from server import ConnectionHandler
from tests.test_pipeline import CLEAN_STEPS, N_AZ, N_RNG, make_scene, make_tree

pytestmark = pytest.mark.filterwarnings("ignore")

RECIPE = {
    "name": "Server test recipe",
    "target": "VRADH",
    "steps": [{"step_id": step, "params": params, "enabled": True} for step, params in CLEAN_STEPS],
}


def _make_handler() -> ConnectionHandler:
    ws = AsyncMock()
    ws.remote_address = ("127.0.0.1", 9999)
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
    handler._sent = sent
    handler._frames = frames
    return handler


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XRADAR_RECIPES_DIR", str(tmp_path / "recipes"))
    saved = (dict(server._file_readers), server._last_file_id)
    server._file_readers.clear()
    server._last_file_id = None
    yield
    server._file_readers.clear()
    server._file_readers.update(saved[0])
    server._last_file_id = saved[1]


@pytest.fixture()
def opened():
    """A 3-sweep volume: sweeps 0 and 2 have velocity, sweep 1 does not."""
    s0, s2 = make_scene(seed=1)["ds"], make_scene(seed=2)["ds"]
    tree = make_tree([s0, s0.drop_vars("VRADH"), s2])
    reader = RadarReader()
    reader._dtree = tree
    reader._format = "synthetic"
    return server._register_reader(reader), reader


def _types(handler):
    return [m["type"] for m in handler._sent if m["type"] != "progress"]


async def _send(handler, **msg):
    await handler.handle_message(json.dumps(msg))


@pytest.mark.asyncio
async def test_catalog_and_recipe_crud():
    h = _make_handler()
    await _send(h, type="list_pipeline_steps")
    steps = h._sent[-1]["steps"]
    assert h._sent[-1]["type"] == "pipeline_steps" and {s["id"] for s in steps} >= {"dealias"}

    await _send(h, type="list_recipes")
    first = h._sent[-1]
    assert first["type"] == "recipes_list" and sum(r["builtin"] for r in first["recipes"]) >= 3

    await _send(h, type="save_recipe", recipe=RECIPE)
    assert [m["type"] for m in h._sent[-2:]] == ["recipe_saved", "recipes_list"]
    assert any(r["name"] == RECIPE["name"] for r in h._sent[-1]["recipes"])

    await _send(h, type="export_recipe_script", recipe=RECIPE)
    script = h._sent[-1]
    assert script["type"] == "recipe_script" and "def process_sweep" in script["script"]
    assert script["filename"] == "server_test_recipe.py"

    await _send(h, type="delete_recipe", name=RECIPE["name"])
    assert h._sent[-2]["type"] == "recipe_deleted" and h._sent[-2]["existed"] is True
    assert all(r["name"] != RECIPE["name"] for r in h._sent[-1]["recipes"])

    for bad in ({"name": "x", "target": "DBZH", "steps": []}, "nope"):
        await _send(h, type="save_recipe", recipe=bad)
        assert h._sent[-1]["type"] == "pipeline_error"
    await _send(h, type="delete_recipe", name=pipeline_builtin_name())
    assert h._sent[-1]["type"] == "pipeline_error"


def pipeline_builtin_name() -> str:
    from engine import pipeline

    return pipeline.builtin_recipes()[0].name


@pytest.mark.asyncio
async def test_preview_sends_stats_then_three_binary_fields(opened):
    file_id, _ = opened
    h = _make_handler()
    await _send(h, type="preview_pipeline", file_id=file_id, sweep=0, recipe=RECIPE)
    assert _types(h) == ["pipeline_preview", "module_field", "module_field", "module_field"]
    prev = next(m for m in h._sent if m["type"] == "pipeline_preview")
    assert prev["sweep"] == 0 and prev["valid_after"] < prev["valid_before"]
    assert [s["step_id"] for s in prev["steps"]] == [s for s, _ in CLEAN_STEPS]
    assert [b["name"] for b in prev["blocks"]] == ["Before", "After", "Removed by step"]
    fields = [m for m in h._sent if m["type"] == "module_field"]
    assert [f["name"] for f in fields] == ["Before", "After", "Removed by step"]
    assert all(f["module"] == "pipeline" and f["file_id"] == file_id for f in fields)
    idx = [i for i, (k, f) in enumerate(h._frames) if k == "json" and f["type"] == "module_field"]
    after = fields[1]
    blob, pos = [], idx[1] + 1
    while pos < len(h._frames) and h._frames[pos][0] == "bin":
        blob.append(h._frames[pos][1])
        pos += 1
    raw = b"".join(blob)
    assert len(raw) == after["azimuth_bytes"] + after["range_bytes"] + after["values_bytes"]
    vals = np.frombuffer(raw[after["azimuth_bytes"] + after["range_bytes"] :], np.float32)
    assert vals.shape == (N_AZ * N_RNG,)


@pytest.mark.asyncio
async def test_preview_errors_become_pipeline_error(opened):
    file_id, _ = opened
    h = _make_handler()
    await _send(h, type="preview_pipeline", file_id=file_id, sweep=1, recipe=RECIPE)  # no velocity
    assert h._sent[-2]["type"] == "pipeline_error" and "VRADH" in h._sent[-2]["message"]
    assert h._sent[-1]["type"] == "progress" and h._sent[-1]["percent"] == 100
    h2 = _make_handler()
    await _send(h2, type="preview_pipeline", file_id=file_id, sweep=-1, recipe=RECIPE)
    assert h2._sent[-2]["type"] == "pipeline_error"
    h3 = _make_handler()
    await _send(h3, type="preview_pipeline", file_id="nope", sweep=0, recipe=RECIPE)
    assert "pipeline_error" in _types(h3)


@pytest.mark.asyncio
async def test_run_pipeline_stores_one_derived_variable(opened):
    file_id, reader = opened
    h = _make_handler()
    await _send(h, type="run_pipeline", file_id=file_id, recipe=RECIPE, all_sweeps=True)
    assert _types(h) == ["pipeline_result", "derived_variables"]
    res = h._sent[[m["type"] for m in h._sent].index("pipeline_result")]
    assert res["name"] == "pipeline:server_test_recipe_vradh"
    assert [d["name"] for d in res["derived"]] == [res["name"]]
    assert res["derived"][0]["sweeps"] == [0, 2]
    assert [s["sweep"] for s in res["skipped"]] == [1]
    assert [s["step_id"] for s in res["steps"]] == [s for s, _ in CLEAN_STEPS]
    assert res["params"]["recipe"] == RECIPE["name"]
    dv = next(m for m in h._sent if m["type"] == "derived_variables")
    assert [v["name"] for v in dv["variables"]] == [res["name"]]  # exactly one
    assert dv["variables"][0]["module"] == "pipeline"
    store = derived.store_for(reader, create=False)
    assert len(store) == 1 and store.has(res["name"], 0) and not store.has(res["name"], 1)

    # the derived variable is served like any other variable
    start = len(h._frames)
    await _send(h, type="get_sweep_data", variable=res["name"], sweep=0, file_id=file_id)
    hdr = next(f for k, f in h._frames[start:] if k == "json" and f["type"] == "sweep_data")
    assert hdr["derived"] is True and hdr["n_az"] == N_AZ

    # running again replaces it, it does not add a second one
    await _send(h, type="run_pipeline", file_id=file_id, recipe=RECIPE)
    assert len(store) == 1


@pytest.mark.asyncio
async def test_run_pipeline_single_sweep_and_errors(opened):
    file_id, _reader = opened
    h = _make_handler()
    await _send(h, type="run_pipeline", file_id=file_id, recipe=RECIPE, all_sweeps=False, sweep=2)
    res = next(m for m in h._sent if m["type"] == "pipeline_result")
    assert res["derived"][0]["sweeps"] == [2]
    h2 = _make_handler()
    await _send(h2, type="run_pipeline", file_id=file_id, recipe=RECIPE, all_sweeps=False, sweep=1)
    err = next(m for m in h2._sent if m["type"] == "pipeline_error")
    assert "could not be applied" in err["message"]
    h3 = _make_handler()
    await _send(h3, type="run_pipeline", file_id=file_id, recipe="bad")
    assert "pipeline_error" in _types(h3)
    h4 = _make_handler()
    await _send(h4, type="run_pipeline", file_id=file_id, recipe=RECIPE, all_sweeps="yes")
    assert "pipeline_error" in _types(h4)


@pytest.mark.asyncio
async def test_cancel_pipeline_stops_the_run(opened):
    file_id, _ = opened
    h = _make_handler()
    original = server.pipeline.run_recipe_all_sweeps

    def cancelling(*args, **kw):
        server._PIPELINE_CANCELS[str(file_id)].set()  # as a cancel_pipeline message would
        return original(*args, **kw)

    server.pipeline.run_recipe_all_sweeps = cancelling
    try:
        await _send(h, type="run_pipeline", file_id=file_id, recipe=RECIPE)
    finally:
        server.pipeline.run_recipe_all_sweeps = original
    err = next(m for m in h._sent if m["type"] == "pipeline_error")
    assert err["cancelled"] is True
    assert str(file_id) not in server._PIPELINE_CANCELS
    await _send(h, type="cancel_pipeline", file_id=file_id)  # nothing running: harmless


@pytest.fixture()
def probe():
    @plugins.module(id="srv_pipeline_probe", title="Probe", requires=["VRADH"])
    def probe_run(ctx):
        v = ctx.field("VRADH").values
        return Result().scalars({"valid": int(np.isfinite(v).sum())}).field("v", v)

    yield
    plugins.MODULES.pop("srv_pipeline_probe", None)


@pytest.mark.asyncio
async def test_run_module_inputs_substitute_pipeline_output(opened, probe):
    file_id, _ = opened
    h = _make_handler()
    await _send(h, type="run_pipeline", file_id=file_id, recipe=RECIPE)
    name = next(m for m in h._sent if m["type"] == "pipeline_result")["name"]

    async def valid_gates(**extra):
        hh = _make_handler()
        await _send(
            hh, type="run_module", module="srv_pipeline_probe", sweep=0, file_id=file_id, **extra
        )
        res = next(m for m in hh._sent if m["type"] in ("module_result", "module_error"))
        assert res["type"] == "module_result", res
        return res["blocks"][0]["items"][0]["value"]

    raw = await valid_gates()
    cleaned = await valid_gates(inputs={"velocity": name})
    assert cleaned < raw
    # all-sweeps run: sweep 1 has no velocity anyway, the others read the derived variable
    hv = _make_handler()
    await _send(
        hv,
        type="run_module",
        module="srv_pipeline_probe",
        all_sweeps=True,
        file_id=file_id,
        inputs={"VRADH": name},
    )
    assert "module_error" not in _types(hv)
    # unknown derived input is a clear module_error
    he = _make_handler()
    await _send(
        he,
        type="run_module",
        module="srv_pipeline_probe",
        file_id=file_id,
        inputs={"velocity": "x:y"},
    )
    err = next(m for m in he._sent if m["type"] == "module_error")
    assert "does not exist" in err["message"]
