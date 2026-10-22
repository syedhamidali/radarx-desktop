"""The nowcast messages beyond the first run: product layers on demand, and the other sources of
frames (the engine's recent composites, a gridded frames file). Synthetic volumes, needs radarx."""

from __future__ import annotations

import numpy as np
import pytest

from engine.nowcast import ws as nowcast_ws
from engine.nowcast.run import NowcastError
from tests.hydro_synth import make_reader

pytest.importorskip("radarx")

LAT, LON = 36.0, -88.0
SPEED_MS = 15.0  # eastward


def _rate(shift_m: float):
    def fn(x, y):
        return 20.0 * np.exp(
            -((x - 20_000.0 - shift_m) ** 2 + (y - 10_000.0) ** 2) / (2 * 8_000.0**2)
        )

    return fn


@pytest.fixture(scope="module")
def readers():
    out = []
    for k in range(3):
        t = np.datetime64("2021-12-11T02:00:00") + np.timedelta64(300 * k, "s")
        rd = make_reader(LAT, LON, _rate(SPEED_MS * 300.0 * k), str(t), name="SYN")
        out.append((f"syn_{k}", rd))
    return out


def _options(**extra):
    return {
        "lead_min": 20,
        "step_min": 5,
        "n_members": 4,
        "resolution_m": 2000,
        "max_range_km": 120,
        **extra,
    }


def _quiet(_p, _m) -> None:
    return None


def test_run_can_send_only_the_deterministic_layers_and_names_its_thresholds(readers) -> None:
    out = nowcast_ws.run(
        {"options": _options(layers="deterministic")}, readers, _quiet, lambda: False
    )
    assert {lyr["kind"] for lyr in out["layers"]} == {"deterministic"}
    assert len(out["layers"]) == 4
    assert out["n_members"] == 4
    assert out["thresholds"] == [20.0, 35.0, 45.0]
    assert out["rate_thresholds"] == [0.5, 2.0, 10.0]
    assert out["grid"]["bbox"] and len(out["grid"]["bbox"]) == 4
    assert out["record"]["guidance"] == "Experimental guidance, not an official forecast."
    assert out["record"]["skill"]
    # every layer says it is a forecast
    assert all("[FORECAST]" in lyr["label"] for lyr in out["layers"])


def test_nowcast_layers_message_gives_every_lead_of_one_product(readers) -> None:
    out = nowcast_ws.run(
        {"options": _options(layers="deterministic")}, readers, _quiet, lambda: False
    )
    nid = out["nowcast_id"]
    prob = nowcast_ws.layers({"nowcast_id": nid, "kind": "probability", "threshold": 20})
    assert [lyr["lead_min"] for lyr in prob["layers"]] == [5.0, 10.0, 15.0, 20.0]
    assert {lyr["threshold"] for lyr in prob["layers"]} == {20.0}
    assert all("[FORECAST]" in lyr["label"] for lyr in prob["layers"])
    assert len(prob["corners"]) == 4
    rate = nowcast_ws.layers(
        {"nowcast_id": nid, "kind": "probability", "threshold": 2, "rate": True}
    )
    assert "mm/h" in rate["layers"][0]["label"] and rate["rate"] is True
    det = nowcast_ws.layers({"nowcast_id": nid, "kind": "deterministic"})
    assert det["layers"][0]["kind"] == "deterministic"
    with pytest.raises(NowcastError):
        nowcast_ws.layers({"nowcast_id": nid, "kind": "probability", "threshold": 7})
    with pytest.raises(NowcastError):
        nowcast_ws.layers({"nowcast_id": nid, "kind": "mean"})
    with pytest.raises(NowcastError):
        nowcast_ws.layers({"nowcast_id": "gone", "kind": "deterministic"})


def test_run_from_recent_composites_follows_the_same_motion(readers) -> None:
    from engine import mosaic as me

    records = [
        me.build_mosaic(
            [(n, rd)],
            "DBZH",
            "composite",
            {"resolution_m": 2000.0, "max_range_km": 120.0, "extent": "auto"},
            {"enabled": True},
            cache=None,
            keep_volume=False,
        )
        for n, rd in readers
    ]
    out = nowcast_ws.run(
        {"options": _options(layers="deterministic")}, [], _quiet, lambda: False, records
    )
    assert out["leads_min"] == [5.0, 10.0, 15.0, 20.0]
    assert out["provenance"]["kind"] == "forecast"
    assert out["motion"]["u_mean_ms"] == pytest.approx(SPEED_MS, abs=4.0)
    assert out["layers"] and out["corners"]
    # a composite that is not reflectivity is refused
    records[1].variable = "VRADH"
    with pytest.raises(NowcastError):
        nowcast_ws.run({"options": _options()}, [], _quiet, None, records)


def test_run_from_a_gridded_frames_file(readers, tmp_path) -> None:
    seq = nowcast_ws.build_frames(readers, _options(), _quiet, None)
    path = seq.to_netcdf(tmp_path / "frames.nc")
    out = nowcast_ws.run(
        {"frames_path": str(path), "options": _options(layers="deterministic")}, [], _quiet, None
    )
    assert out["leads_min"] == [5.0, 10.0, 15.0, 20.0]
    assert len(out["corners"]) == 4 and out["layers"]
    with pytest.raises(NowcastError):
        nowcast_ws.run({"frames_path": str(tmp_path / "no.nc")}, [], _quiet)
    with pytest.raises(NowcastError):
        nowcast_ws.run({"options": {}}, [], _quiet)
