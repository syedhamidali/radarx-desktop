"""``rxd qpe`` and ``rxd basin``: the command line, the Python API and the QPE module.

Volumes are in-memory synthetic ones (uniform rain of a given rate, see ``hydro_synth``): files
named ``*.hyd`` hold ``<ISO time> <rate mm/h>`` and open as such through the real reader.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("radarx")

from engine import api, cli
from engine.reader import RadarReader
from tests.hydro_synth import make_tree

LAT, LON = 35.0, -97.0


@pytest.fixture(autouse=True)
def _in_tmp(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def hyd_files(monkeypatch):
    real_open = RadarReader.open_file

    def fake_open(self, path: str):
        if not path.endswith(".hyd"):
            return real_open(self, path)
        when, rate = Path(path).read_text().split()
        r = float(rate)
        tree = make_tree(
            LAT, LON, lambda x, y: np.full(np.broadcast(x, y).shape, r), when, name="SYN"
        )
        self._commit(tree, path, "synthetic")
        return self.get_schema()

    monkeypatch.setattr(RadarReader, "open_file", fake_open)


@pytest.fixture()
def volumes(tmp_path: Path) -> list[Path]:
    out = []
    for k, t in enumerate(("20:00:00", "20:10:00", "20:20:00")):
        p = tmp_path / f"v{k}.hyd"
        p.write_text(f"2024-05-01T{t} 10.0")
        out.append(p)
    return out


def run(capsys, *argv):
    code = cli.main([str(a) for a in argv])
    c = capsys.readouterr()
    return code, c.out, c.err


class TestQpeCommand:
    def test_list_relations(self, capsys):
        code, out, _ = run(capsys, "qpe", "--list-relations")
        assert code == 0 and "wsr88d" in out and "Z = 300 R^1.4" in out and "tropical" in out
        code, out, _ = run(capsys, "qpe", "--list-relations", "--json")
        keys = [r["key"] for r in json.loads(out)]
        assert "snow_sekhon_srivastava" in keys and "marshall_palmer" in keys

    def test_accumulation_files_json_and_provenance(self, capsys, volumes, tmp_path):
        code, out, _ = run(
            capsys,
            "qpe",
            "-i",
            *volumes,
            "-P",
            "min_dbz=-30",
            "--resolution-m",
            "2000",
            "--max-range-km",
            "100",
            "-o",
            "rain.nc",
            "-p",
            "rain.png",
            "--json",
        )
        assert code == 0
        doc = json.loads(out)
        assert doc["provenance"]["type"] == "RETRIEVED"
        assert doc["duration_s"] == pytest.approx(1200.0, abs=60)
        assert doc["stats"]["max_mm"] == pytest.approx(10.0 * 1200 / 3600, rel=0.02)
        assert {Path(f).name for f in doc["files"]} == {"rain.nc", "rain.png"}
        assert (tmp_path / "rain.nc").is_file() and (tmp_path / "rain.png").stat().st_size > 5000

    def test_text_summary_says_what_it_is(self, capsys, volumes):
        code, out, _ = run(
            capsys,
            "qpe",
            "-i",
            *volumes,
            "-P",
            "min_dbz=-30",
            "-o",
            "r.nc",
            "--resolution-m",
            "2000",
        )
        assert code == 0
        assert (
            "rainfall accumulation" in out and "RETRIEVED: an estimate, not an observation" in out
        )
        assert "R(Z)" in out and "uncertainty" in out
        assert "wrote r.nc" in out

    def test_default_outputs_are_named_and_announced(self, capsys, volumes, tmp_path):
        code, out, _ = run(
            capsys, "qpe", "-i", *volumes, "-P", "min_dbz=-30", "--resolution-m", "3000"
        )
        assert code == 0 and "as the outputs" in out
        assert any(p.name.startswith("qpe_") and p.suffix == ".nc" for p in tmp_path.iterdir())

    def test_relation_changes_the_estimate(self, capsys, volumes):
        code, _, _ = run(
            capsys,
            "qpe",
            "-i",
            *volumes,
            "--relation",
            "tropical",
            "-P",
            "min_dbz=-30",
            "--resolution-m",
            "3000",
            "-o",
            "t.json",
            "-q",
        )
        assert code == 0
        doc = json.loads(Path("t.json").read_text())
        # Z = 300 R^1.4 data read with Z = 250 R^1.2: R = (Z/250)^(1/1.2) > the true 10 mm/h
        assert "250" in str(doc["provenance"]["method"])
        assert doc["stats"]["max_mm"] > 10.0 * 1200 / 3600 * 1.05

    def test_custom_zr_and_no_cap(self, capsys, volumes):
        code, _, _ = run(
            capsys,
            "qpe",
            "-i",
            *volumes,
            "-P",
            "zr_a=300",
            "-P",
            "zr_b=1.4",
            "--no-z-cap",
            "-P",
            "min_dbz=-30",
            "--resolution-m",
            "3000",
            "-o",
            "c.json",
            "-q",
        )
        assert code == 0
        doc = json.loads(Path("c.json").read_text())
        assert doc["stats"]["max_mm"] == pytest.approx(10.0 * 1200 / 3600, rel=0.02)

    def test_errors_are_usage_errors_with_the_fix(self, capsys, volumes):
        code, _, err = run(capsys, "qpe", "-i", *volumes, "--relation", "nope")
        assert code == 2 and "unknown Z-R relation" in err and "wsr88d" in err
        code, _, err = run(capsys, "qpe", "-i", *volumes, "-P", "zrr_a=3")
        assert code == 2 and "unknown option" in err
        code, _, err = run(capsys, "qpe", "-i", *volumes, "-o", "x.txt")
        assert code == 2 and ".nc" in err
        code, _, err = run(capsys, "qpe", "-i", *volumes, "--adjust", "ked")
        assert code == 2 and "--gauges" in err
        code, _, err = run(capsys, "qpe", "-i", *volumes, "--method", "magic")
        assert code == 2 and "method must be" in err
        code, _, err = run(capsys, "qpe", "-o", "x.nc")
        assert code == 2 and "needs a radar file" in err

    def test_basins_and_threshold_table(self, capsys, volumes, tmp_path):
        gj = tmp_path / "b.geojson"
        gj.write_text(
            json.dumps(
                {
                    "type": "Feature",
                    "properties": {"name": "Test Creek"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [-97.2, 34.9],
                                [-96.8, 34.9],
                                [-96.8, 35.2],
                                [-97.2, 35.2],
                                [-97.2, 34.9],
                            ]
                        ],
                    },
                }
            )
        )
        code, out, _ = run(
            capsys,
            "qpe",
            "-i",
            *volumes,
            "-P",
            "min_dbz=-30",
            "--resolution-m",
            "2000",
            "--max-range-km",
            "100",
            "-b",
            gj,
            "--thresholds",
            "2,5",
            "-o",
            "tab.csv",
            "-o",
            "tab.json",
        )
        assert (code == 0 and "Test Creek" in out) or "basin_1" in out
        rows = [ln for ln in Path("tab.csv").read_text().splitlines() if not ln.startswith("#")]
        header = rows[0].split(",")
        values = dict(zip(header, rows[1].split(","), strict=True))
        assert float(values["accumulation_mean_mm"]) == pytest.approx(10.0 * 1200 / 3600, rel=0.03)
        assert float(values["area_acc_ge_2mm_km2"]) == pytest.approx(
            float(values["area_km2"]), rel=0.02
        )
        assert float(values["area_acc_ge_5mm_km2"]) == 0.0
        series = list(tmp_path.glob("tab_*_series.csv"))
        assert len(series) == 1
        assert Path("tab.json").is_file()

    def test_gauge_adjustment_is_labelled_and_validated(self, capsys, volumes, tmp_path):
        g = pd.DataFrame(
            {
                "station": [f"G{i}" for i in range(6)],
                "lat": [35.05, 35.1, 34.95, 35.2, 35.0, 35.15],
                "lon": [-97.1, -96.9, -97.0, -97.05, -96.85, -96.95],
                "time": "2024-05-01 20:20:00",
                "precip_mm": 5.0,
            }
        )
        g.to_csv("gauges.csv", index=False)
        code, out, _ = run(
            capsys,
            "qpe",
            "-i",
            *volumes,
            "-P",
            "min_dbz=-30",
            "--resolution-m",
            "2000",
            "--max-range-km",
            "100",
            "--gauges",
            "gauges.csv",
            "--gauge-interval-s",
            "1200",
            "--adjust",
            "mfb",
            "-o",
            "adj.nc",
            "--json",
        )
        assert code == 0
        doc = json.loads(out)
        assert doc["provenance"]["type"] == "ADJUSTED"
        assert doc["adjustment"]["n_gauges"] == 6
        before = doc["gauge_validation"]["validation_before"]["overall"]
        after = doc["gauge_validation"]["validation_after_in_sample"]["overall"]
        assert before["relative_bias"] == pytest.approx(10 * 1200 / 3600 / 5 - 1, abs=0.02)
        assert abs(after["relative_bias"]) < 0.01
        import xarray as xr

        ds = xr.open_dataset("adj.nc")
        assert ds["accumulation_adjusted"].attrs["provenance_type"] == "ADJUSTED"


class TestBasinCommand:
    def test_requires_basins_and_writes_table_and_figure(self, capsys, volumes, tmp_path):
        code, _, err = run(capsys, "basin", "-i", *volumes)
        assert code == 2 and "--basins" in err
        gj = tmp_path / "b.geojson"
        gj.write_text(
            json.dumps(
                {
                    "type": "Feature",
                    "properties": {"huc12": "H1"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [-97.2, 34.9],
                                [-96.8, 34.9],
                                [-96.8, 35.2],
                                [-97.2, 35.2],
                                [-97.2, 34.9],
                            ]
                        ],
                    },
                }
            )
        )
        code, out, _ = run(
            capsys,
            "basin",
            "-i",
            *volumes,
            "-b",
            gj,
            "-P",
            "min_dbz=-30",
            "--resolution-m",
            "2000",
            "--max-range-km",
            "100",
            "--bin-minutes",
            "10",
        )
        assert code == 0 and "H1" in out and "as the outputs" in out
        names = {p.name for p in tmp_path.iterdir()}
        assert any(n.startswith("basin_") and n.endswith(".csv") for n in names)
        assert any(n.startswith("basin_") and n.endswith(".png") for n in names)

    def test_frequency_table_adds_a_return_period(self, capsys, volumes, tmp_path):
        gj = tmp_path / "b.geojson"
        gj.write_text(
            json.dumps(
                {
                    "type": "Feature",
                    "properties": {"huc12": "H1"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [-97.2, 34.9],
                                [-96.8, 34.9],
                                [-96.8, 35.2],
                                [-97.2, 35.2],
                                [-97.2, 34.9],
                            ]
                        ],
                    },
                }
            )
        )
        freq = tmp_path / "f.json"
        freq.write_text(
            json.dumps({"duration_hours": 0.25, "depth_mm": {"2": 2.0, "10": 4.0, "100": 8.0}})
        )
        code, out, _ = run(
            capsys,
            "basin",
            "-i",
            *volumes,
            "-b",
            gj,
            "--frequency",
            freq,
            "-P",
            "min_dbz=-30",
            "--resolution-m",
            "2000",
            "--max-range-km",
            "100",
            "-o",
            "t.json",
            "-q",
            "--json",
        )
        assert code == 0
        doc = json.loads(out)
        ex = doc["exceedance"][0]
        assert ex["max_depth_mm"] == pytest.approx(10 * 0.25, rel=0.03)  # 10 mm/h for 15 min
        assert 2 < ex["return_period_years"] < 10


class TestIndicatorsCommand:
    """``rxd indicators`` on synthetic volumes (uniform 10 mm/h for 20 min = 3.33 mm everywhere)."""

    BASE = ("-P", "min_dbz=-30", "--resolution-m", "2000", "--max-range-km", "100")

    @staticmethod
    def _grid_file(path, value, units="mm"):
        import xarray as xr

        lat = np.linspace(33.0, 37.0, 41)
        lon = np.linspace(-99.0, -95.0, 41)
        ds = xr.Dataset(
            {"depth": (("lat", "lon"), np.full((41, 41), value, "f4"), {"units": units})},
            coords={"lat": lat, "lon": lon},
        )
        ds.to_netcdf(path)
        return path

    def test_ratio_to_guidance_label_vintage_and_files(self, capsys, volumes):
        import xarray as xr

        self._grid_file("ffg.nc", 5.0 / 25.4, units="in")  # 5 mm in inches
        code, out, _ = run(
            capsys, "indicators", "-i", *volumes, *self.BASE, "--guidance", "ffg.nc",
            "--vintage", "FFG test 2024-05-01 18Z", "-o", "ind.nc", "-o", "ind.json",
            "-p", "ind.png",
            "--json",
        )  # fmt: skip
        assert code == 0
        doc = json.loads(out)
        assert doc["label"] == "RETRIEVED INDICATOR"
        assert "not an official warning" in doc["product_text"]
        assert "Antecedent soil moisture" in doc["not_modelled"]
        lay = doc["layers"]["ffg_ratio_1h"]
        assert lay["provenance"]["guidance"]["vintage"] == "FFG test 2024-05-01 18Z"
        assert lay["provenance"]["guidance"]["units_in_file"] == "in"
        assert lay["provenance"]["lower_bound"] is True  # 20 min of a 1 h guidance
        assert any("lower bounds" in n for n in doc["notes"])
        # 3.33 mm / 5 mm = 0.67: the class "50 to 75 %" holds the cells with rain
        cls = lay["classes"]
        assert cls["50 to 75 %"]["cells"] > 1000
        assert cls["150 % or more of guidance"]["cells"] == 0
        ds = xr.open_dataset("ind.nc")
        assert float(ds["ffg_ratio_1h"].where(ds["rainfall"] > 1).mean()) == pytest.approx(
            10 * 1200 / 3600 / 5.0, rel=0.03
        )
        assert ds["ffg_ratio_1h"].attrs["product_label"] == "RETRIEVED INDICATOR"
        assert "not an official warning" in ds.attrs["comment"]
        assert json.loads(Path("ind.json").read_text())["layers"]
        assert Path("ind_ffg_ratio_1h.png").stat().st_size > 3000

    def test_return_period_and_relative_indicators(self, capsys, volumes, tmp_path):
        for years, depth in ((2, 2.0), (10, 4.0), (100, 8.0)):
            self._grid_file(f"f{years}.nc", depth)
        self._grid_file("clim.nc", 6.0)
        code, out, _ = run(
            capsys, "indicators", "-i", *volumes, *self.BASE, "--duration-h", "0.35",
            "--frequency-grid", "2=f2.nc", "--frequency-grid", "10=f10.nc",
            "--frequency-grid", "100=f100.nc", "--climatology", "clim.nc", "-o", "i.json", "-q",
            "--json",
        )  # fmt: skip
        assert code == 0
        doc = json.loads(out)
        rp = doc["layers"]["return_period_0.35h"]
        # 3.33 mm lies between the 2 year (2 mm) and 10 year (4 mm) depths: log-log gives 6.5 years
        assert rp["classes"]["5 to 10 years"]["cells"] > 1000
        rel = doc["layers"]["relative_1h"]
        # 3.33 / 6 = 0.56: between half the reference and the reference
        assert rel["classes"]["half to the reference"]["cells"] > 1000
        assert "did not beat this rule" in rel["provenance"]["lab_result"]

    def test_usage_errors(self, capsys, volumes):
        code, _, err = run(capsys, "indicators", "-i", *volumes, *self.BASE)
        assert code == 2 and "at least one of" in err
        self._grid_file("g.nc", 5.0)
        code, _, err = run(
            capsys,
            "indicators",
            "-i",
            *volumes,
            *self.BASE,
            "--guidance",
            "g.nc",
            "--duration-h",
            "0.1",
        )
        assert code != 0 and "longer than" in err
        code, _, err = run(
            capsys, "indicators", "-i", *volumes, *self.BASE, "--frequency-grid", "ten=a.nc"
        )
        assert code == 2 and "YEARS=FILE" in err
        code, _, err = run(
            capsys, "indicators", "-i", *volumes, *self.BASE, "--frequency-grid", "10=a.nc"
        )
        assert code == 2 and "at least two" in err

    def test_api(self, volumes):
        res = api.qpe(volumes, min_dbz=-30.0, resolution_m=2000.0, max_range_km=100.0)
        self._grid_file("g.nc", 10.0)
        ind = api.flood_indicators(res, guidance="g.nc", vintage="v")
        assert float(np.nanmax(ind.layers["ffg_ratio_1h"].values)) < 0.5
        assert "flood_indicators" in api.__all__


class TestApiAndModule:
    def test_api_qpe_volume_and_list(self, volumes, tmp_path):
        res = api.qpe(
            volumes, min_dbz=-30.0, resolution_m=2000.0, max_range_km=100.0, out=tmp_path / "a.nc"
        )
        assert (tmp_path / "a.nc").is_file()
        assert res.provenance()["type"] == "RETRIEVED"
        vol = api.open(volumes[0])
        inst = vol.qpe(min_dbz=-30.0, resolution_m=2000.0, max_range_km=100.0)
        wet = np.isfinite(inst.rate_last) & (inst.range_km > 10)
        assert inst.rate_last[wet] == pytest.approx(10.0, rel=3e-3)
        with pytest.raises(api.XrdArgumentError, match="Did you mean"):
            api.qpe(volumes, relaton="tropical")
        with pytest.raises(api.XrdArgumentError, match="unknown Z-R"):
            api.qpe(volumes, relation="nope")

    def test_one_volume_with_gauges_is_an_error(self, volumes, tmp_path):
        (tmp_path / "g.csv").write_text("station,lat,lon,time,mm\nA,35,-97,2024-05-01 21:00,1\n")
        from engine.hydro.qpe import QpeError

        with pytest.raises(QpeError, match="needs an accumulation"):
            api.qpe(volumes[:1], gauges=tmp_path / "g.csv", gauge_options={"interval_s": 3600})
