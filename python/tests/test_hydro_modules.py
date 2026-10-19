"""The hydrology modules (``hydro_qpe``, ``hydro_basin_rate``) through the module machinery."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("radarx")

from engine import api
from engine.plugins import discover_modules
from engine.reader import RadarReader
from tests.hydro_synth import make_tree

LAT, LON = 35.0, -97.0


@pytest.fixture(autouse=True)
def hyd_files(monkeypatch):
    real_open = RadarReader.open_file

    def fake_open(self, path: str):
        if not path.endswith(".hyd"):
            return real_open(self, path)
        when, rate = Path(path).read_text().split()
        r = float(rate)
        tree = make_tree(LAT, LON, lambda x, y: np.full(np.broadcast(x, y).shape, r), when)
        self._commit(tree, path, "synthetic")
        return self.get_schema()

    monkeypatch.setattr(RadarReader, "open_file", fake_open)


@pytest.fixture()
def volume(tmp_path: Path):
    p = tmp_path / "v.hyd"
    p.write_text("2024-05-01T20:00:00 10.0")
    return p


def test_modules_are_listed_in_the_hydrology_category():
    mods = discover_modules()
    assert mods["hydro_qpe"].category == "Hydrology"
    assert mods["hydro_basin_rate"].category == "Hydrology"
    assert mods["hydro_basin_rate"].scope == "volume"
    assert mods["hydro_qpe"].requires == ["DBZH"]


def test_hydro_qpe_gives_rate_uncertainty_quality_and_relation(volume):
    res = api.open(volume).run("hydro_qpe", sweep=0)
    fields = {b["name"] for b in res.blocks if b["type"] == "field"}
    assert {
        "Rain rate (RETRIEVED)",
        "Rate uncertainty (1 sigma)",
        "Beam quality index",
        "Relation used",
    } <= fields
    text = json.dumps(res.blocks, default=str)
    assert "RETRIEVED" in text and "300" in text and "not an observation" in text
    arr = np.asarray(res.fields["Rain rate (RETRIEVED)"])
    # the sweep is Z = 300 R^1.4 of 10 mm/h: the default 5 dBZ floor and 53 dBZ cap do not touch it
    wet = np.isfinite(arr) & (arr > 0)
    assert wet.sum() > 10_000
    assert np.median(arr[wet]) == pytest.approx(10.0, rel=3e-3)
    sig = np.asarray(res.fields["Rate uncertainty (1 sigma)"])
    assert np.nanmedian(sig[wet]) == pytest.approx(48.0, abs=5.0)  # percent, R(Z) with 1 dB


def test_hydro_qpe_relation_and_input_order(volume):
    vol = api.open(volume)
    tropical = vol.run("hydro_qpe", sweep=0, relation="tropical")
    default = vol.run("hydro_qpe", sweep=0)
    a = np.asarray(tropical.fields["Rain rate (RETRIEVED)"])
    b = np.asarray(default.fields["Rain rate (RETRIEVED)"])
    assert a.shape == b.shape == (360, 400)
    assert np.nanmedian(a[np.isfinite(a) & (a > 0)]) > 1.05 * np.nanmedian(
        b[np.isfinite(b) & (b > 0)]
    )


def test_hydro_qpe_bad_input_is_a_clear_error(volume):
    with pytest.raises(Exception, match=r"snow relation|Z-S|snow"):
        api.open(volume).run("hydro_qpe", sweep=0, precip="rain", relation="snow_sekhon_srivastava")


def test_basin_rate_module(volume, tmp_path):
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
    res = api.open(volume).run("hydro_basin_rate", basins_file=str(gj), resolution_m=2000.0)
    table = next(b for b in res.blocks if b["type"] == "table")
    row = table["rows"][0]
    assert row["id"] == "H1"
    assert row["coverage"] == pytest.approx(100.0)
    assert row["mean"] == pytest.approx(10.0, rel=5e-3)
    assert row["above_10"] == pytest.approx(row["area"], rel=0.05)
    assert any(b["type"] == "overlay" for b in res.blocks)
    with pytest.raises(Exception, match=r"not found|Basin file"):
        api.open(volume).run("hydro_basin_rate", basins_file=str(tmp_path / "none.geojson"))
