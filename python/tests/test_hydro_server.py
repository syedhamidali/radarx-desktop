"""The QPE messages of the WebSocket server (engine/hydro/ws.py via ConnectionHandler)."""

from __future__ import annotations

import base64
import json

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("radarx")

import server
from engine.reader import RadarReader
from tests.hydro_synth import make_tree
from tests.test_server import _make_handler

LAT, LON = 35.0, -97.0


@pytest.fixture(autouse=True)
def _isolated_state():
    saved = (dict(server._file_readers), server._last_file_id, dict(server._generations))
    server._file_readers.clear()
    server._generations.clear()
    server._last_file_id = None
    from engine.hydro import ws

    ws.STORE.clear()
    yield
    server._file_readers.clear()
    server._file_readers.update(saved[0])
    server._last_file_id = saved[1]
    server._generations.clear()
    server._generations.update(saved[2])


def _open(times, rate=10.0) -> list[str]:
    ids = []
    for t in times:
        reader = RadarReader()
        reader._dtree = make_tree(
            LAT, LON, lambda x, y: np.full(np.broadcast(x, y).shape, rate), t, name="SYN"
        )
        reader._format = "synthetic"
        ids.append(server._register_reader(reader))
    return ids


async def _send(handler, msg):
    handler._sent.clear()
    await handler.handle_message(json.dumps(msg))
    return list(handler._sent)


def _last(sent, type_):
    found = [m for m in sent if m["type"] == type_]
    assert found, f"no {type_} in {[m['type'] for m in sent]}"
    return found[-1]


RUN = {
    "config": {"min_dbz": -30.0},
    "options": {"resolution_m": 3000.0, "max_range_km": 100.0},
}


@pytest.mark.asyncio
async def test_run_returns_layer_summary_and_registers_the_grid_for_the_probe():
    ids = _open(["2024-05-01T20:00:00", "2024-05-01T20:10:00", "2024-05-01T20:20:00"])
    handler, _ = _make_handler()
    sent = await _send(handler, {"type": "qpe_run", "request_id": "r1", "file_ids": ids, **RUN})
    res = _last(sent, "qpe_result")
    assert res["request_id"] == "r1" and res["qpe_id"]
    s = res["summary"]
    assert s["provenance"]["type"] == "RETRIEVED"
    assert s["stats"]["max_mm"] == pytest.approx(10 * 1200 / 3600, rel=0.02)
    assert {f["name"] for f in s["fields"]} >= {"accumulation", "accumulation_sigma", "quality"}
    layer = res["layer"]
    png = base64.b64decode(layer["png_b64"])
    assert png[:4] == b"\x89PNG" and layer["units"] == "mm" and len(layer["corners"]) == 4
    assert "RETRIEVED" in layer["variable"]
    assert sent[-1]["type"] == "progress" and sent[-1]["percent"] == 100
    # the map probe of the mosaic layer works on the QPE grid
    sent = await _send(
        handler,
        {
            "type": "mosaic_probe",
            "mosaic_id": layer["mosaic_id"],
            "lon": LON + 0.3,
            "lat": LAT + 0.1,
        },
    )
    probe = _last(sent, "mosaic_probe_result")
    assert probe["units"] == "mm" and probe["value"] == pytest.approx(10 * 1200 / 3600, rel=0.03)


@pytest.mark.asyncio
async def test_layer_switch_series_and_export(tmp_path):
    ids = _open(["2024-05-01T20:00:00", "2024-05-01T20:10:00"])
    gj = tmp_path / "b.geojson"
    ring = [[-97.2, 34.9], [-96.8, 34.9], [-96.8, 35.2], [-97.2, 35.2], [-97.2, 34.9]]
    gj.write_text(
        json.dumps(
            {
                "type": "Feature",
                "properties": {"huc12": "H1"},
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        )
    )
    handler, _ = _make_handler()
    sent = await _send(
        handler,
        {
            "type": "qpe_run",
            "request_id": "a",
            "file_ids": ids,
            **RUN,
            "basins": {"path": str(gj), "thresholds": [2, 5]},
        },
    )
    res = _last(sent, "qpe_result")
    qid = res["qpe_id"]
    assert res["summary"]["basins"][0]["id"] == "H1"
    fc = res["summary"]["basin_geojson"]
    assert fc["features"][0]["properties"]["id"] == "H1" and fc["provenance_type"] == "RETRIEVED"
    assert fc["features"][0]["properties"]["accumulation_mean_mm"] == pytest.approx(
        10 * 600 / 3600, rel=0.03
    )
    sent = await _send(
        handler,
        {"type": "qpe_layer", "request_id": "b", "qpe_id": qid, "field": "accumulation_sigma"},
    )
    lay = _last(sent, "qpe_layer_result")["layer"]
    assert lay["field"] == "accumulation_sigma" and lay["units"] == "mm"
    sent = await _send(
        handler,
        {"type": "qpe_series", "request_id": "c", "qpe_id": qid, "basin": "H1", "bin_minutes": 5},
    )
    ser = _last(sent, "qpe_series_result")
    assert ser["basin"] == "H1" and ser["provenance_type"] == "RETRIEVED"
    assert sum(ser["hyetograph"]["depth_mm"]) == pytest.approx(10 * 600 / 3600, rel=0.03)
    assert ser["rows"][-1]["cumulative_mm"] == pytest.approx(10 * 600 / 3600, rel=0.03)
    out = tmp_path / "x.nc"
    sent = await _send(
        handler, {"type": "qpe_export", "request_id": "d", "qpe_id": qid, "path": str(out)}
    )
    assert _last(sent, "file_saved")["path"] == str(out) and out.is_file()
    sent = await _send(
        handler, {"type": "qpe_export", "request_id": "e", "qpe_id": qid, "path": str(out)}
    )
    assert "already exists" in _last(sent, "qpe_error")["message"]


@pytest.mark.asyncio
async def test_adjust_an_existing_result_without_recomputing(tmp_path):
    ids = _open(["2024-05-01T20:00:00", "2024-05-01T20:10:00", "2024-05-01T20:20:00"])
    handler, _ = _make_handler()
    res = _last(
        await _send(handler, {"type": "qpe_run", "request_id": "a", "file_ids": ids, **RUN}),
        "qpe_result",
    )
    g = pd.DataFrame(
        {
            "station": [f"G{i}" for i in range(6)],
            "lat": [35.05, 35.1, 34.95, 35.2, 35.0, 35.15],
            "lon": [-97.1, -96.9, -97.0, -97.05, -96.85, -96.95],
            "time": "2024-05-01 20:20:00",
            "precip_mm": 5.0,
        }
    )
    path = tmp_path / "g.csv"
    g.to_csv(path, index=False)
    sent = await _send(
        handler,
        {
            "type": "qpe_adjust",
            "request_id": "b",
            "qpe_id": res["qpe_id"],
            "gauges": {"path": str(path), "adjust": "mfb", "interval_s": 1200},
        },
    )
    out = _last(sent, "qpe_adjust_result")
    assert out["summary"]["provenance"]["type"] == "ADJUSTED"
    assert out["layer"]["provenance_type"] == "ADJUSTED"
    assert out["validation"]["pairs"] == 6
    assert out["validation"]["validation_before"]["overall"]["n"] == 6
    # a second adjustment replaces the first (the basin series, if any, starts from the raw rates)
    sent = await _send(
        handler,
        {
            "type": "qpe_adjust",
            "request_id": "c",
            "qpe_id": res["qpe_id"],
            "gauges": {"path": str(path), "adjust": "mfb", "interval_s": 1200},
        },
    )
    assert _last(sent, "qpe_adjust_result")["summary"]["adjustment"]["n_gauges"] == 6


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "needle"),
    [
        ({"file_ids": []}, "file_ids"),
        ({"file_ids": ["nope"]}, "Unknown or closed"),
        ({"config": {"relation": "nope"}}, "unknown Z-R"),
        ({"config": {"bogus": 1}}, "unknown option"),
        ({"options": {"resolution_m": 10}}, "resolution_m"),
    ],
)
async def test_errors_are_qpe_errors(payload, needle):
    ids = _open(["2024-05-01T20:00:00"])
    handler, _ = _make_handler()
    msg = {"type": "qpe_run", "request_id": "q", "file_ids": ids, **RUN}
    msg.update(payload)
    sent = await _send(handler, msg)
    err = _last(sent, "qpe_error")
    assert err["request_id"] == "q" and needle in err["message"]
    assert not [m for m in sent if m["type"] == "error"]
    assert sent[-1]["type"] == "progress" and sent[-1]["percent"] == 100


@pytest.mark.asyncio
async def test_info_lists_relations_with_equations_and_references():
    handler, _ = _make_handler()
    sent = await _send(handler, {"type": "qpe_info", "request_id": "i"})
    info = _last(sent, "qpe_info_result")
    keys = {r["key"] for r in info["relations"]}
    assert {"wsr88d", "tropical", "marshall_palmer", "snow_sekhon_srivastava"} <= keys
    wsr = next(r for r in info["relations"] if r["key"] == "wsr88d")
    assert "300" in wsr["equation"] and "Fulton" in wsr["reference"]
    assert info["default_relation"]["rain"] == "wsr88d" and "hybrid" in info["methods"]


@pytest.mark.asyncio
async def test_unknown_result_id_and_web_mode(monkeypatch):
    handler, _ = _make_handler()
    sent = await _send(handler, {"type": "qpe_layer", "request_id": "x", "qpe_id": "gone"})
    assert "expired" in _last(sent, "qpe_error")["message"]
    monkeypatch.setattr(server.web, "is_web_mode", lambda: True)
    sent = await _send(handler, {"type": "qpe_run", "request_id": "w", "file_ids": ["a"]})
    assert "desktop app" in _last(sent, "qpe_error")["message"]
