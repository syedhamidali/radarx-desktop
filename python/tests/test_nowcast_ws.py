"""The ``nowcast_run`` message end to end on synthetic volumes (needs radarx for the gridding)."""

from __future__ import annotations

import numpy as np
import pytest

from engine.nowcast import ws as nowcast_ws
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


def test_nowcast_run_message_returns_layers_with_forecast_provenance(readers, tmp_path) -> None:
    progress = []
    msg = {
        "type": "nowcast_run",
        "request_id": "r1",
        "options": {
            "lead_min": 20,
            "step_min": 5,
            "n_members": 4,
            "resolution_m": 2000,
            "max_range_km": 120,
        },
    }
    out = nowcast_ws.run(msg, readers, lambda p, m: progress.append((p, m)), lambda: False)
    assert out["field"] == "reflectivity" and out["units"] == "dBZ"
    assert out["leads_min"] == [5.0, 10.0, 15.0, 20.0]
    assert out["provenance"]["kind"] == "forecast"
    assert "not an official forecast" in out["provenance"]["uncertainty"]
    assert out["record"]["type"] == "FORECAST"
    kinds = {(lyr["kind"], lyr["lead_min"]) for lyr in out["layers"]}
    assert ("deterministic", 20.0) in kinds and ("probability", 20.0) in kinds
    assert len(out["corners"]) == 4
    assert all(lyr["png_b64"] for lyr in out["layers"])
    # the motion is eastward at about the speed of the synthetic storm
    assert out["motion"]["u_mean_ms"] == pytest.approx(SPEED_MS, abs=4.0)
    assert abs(out["motion"]["v_mean_ms"]) < 4.0
    assert progress and progress[-1][0] >= 96
    # export the stored result
    path = nowcast_ws.export({"nowcast_id": out["nowcast_id"], "path": str(tmp_path / "n.nc")})
    assert path.exists() and path.suffix == ".nc"


def test_nowcast_run_refuses_bad_options(readers) -> None:
    from engine.nowcast.run import NowcastError

    with pytest.raises(NowcastError):
        nowcast_ws.run({"options": {"lead_min": "soon"}}, readers, lambda p, m: None)
    with pytest.raises(NowcastError):
        nowcast_ws.run({"options": "fast"}, readers, lambda p, m: None)
    with pytest.raises(NowcastError):
        nowcast_ws.export({"nowcast_id": "nope", "path": "x.nc"})
