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


class TestBasinOutputs:
    """Outlines for a GIS, the map figure, and the metadata of the CSV files."""

    @staticmethod
    def _basin(tmp_path):
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
        return gj

    def test_geojson_map_and_csv_metadata(self, capsys, volumes, tmp_path):
        gj = self._basin(tmp_path)
        code, _, _ = run(
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
            "-o",
            "out.geojson",
            "-o",
            "out.csv",
            "--map",
            "map.png",
            "-q",
        )
        assert code == 0
        fc = json.loads(Path("out.geojson").read_text())
        f = fc["features"][0]
        assert fc["provenance_type"] == "RETRIEVED" and fc["label"].startswith("an estimate")
        assert f["properties"]["id"] == "H1"
        assert f["properties"]["accumulation_mean_mm"] == pytest.approx(10 * 1200 / 3600, rel=0.03)
        assert Path("map.png").stat().st_size > 5000

        # the CSV files carry their provenance, units and uncertainty statement in # lines
        table = Path("out.csv").read_text().splitlines()
        meta = [ln for ln in table if ln.startswith("#")]
        assert any(ln.startswith("# columns:") and "accumulation_mean_mm [mm]" in ln for ln in meta)
        assert any(ln.startswith("# uncertainty:") for ln in meta)
        df = pd.read_csv("out.csv", comment="#")
        assert df.loc[0, "id"] == "H1"
        series = Path("out_H1_series.csv").read_text().splitlines()
        meta = [ln for ln in series if ln.startswith("#")]
        assert any("basin_area_km2" in ln for ln in meta)
        assert any(ln.startswith("# accumulation_1sigma_mm:") for ln in meta)
        assert any(ln.startswith("# columns:") and "cumulative_mm [mm]" in ln for ln in meta)
        ts = pd.read_csv("out_H1_series.csv", comment="#")
        assert list(ts.columns) == [
            "time",
            "mean_rate_mm_h",
            "peak_cell_rate_mm_h",
            "coverage",
            "cumulative_mm",
        ]
        assert ts["cumulative_mm"].iloc[-1] == pytest.approx(10 * 1200 / 3600, rel=0.03)

    def test_geojson_needs_basins_in_the_qpe_command(self, capsys, volumes):
        code, _, err = run(capsys, "qpe", "-i", *volumes, "-o", "x.geojson")
        assert code == 2 and "--basins" in err


class TestEnsembleCommand:
    """``--ensemble N`` on synthetic volumes (uniform 10 mm/h for 20 min = 3.33 mm everywhere)."""

    BASE = (
        "-P",
        "min_dbz=-30",
        "--resolution-m",
        "2000",
        "--max-range-km",
        "100",
    )
    TRUE_MM = 10 * 1200 / 3600

    def test_products_provenance_and_seed(self, capsys, volumes):
        import xarray as xr

        args = ("qpe", "-i", *volumes, *self.BASE, "--ensemble", "24", "--seed", "3")
        code, out, _ = run(capsys, *args, "--exceed", "2,3.3,6", "-o", "e1.nc", "--json")
        assert code == 0
        doc = json.loads(out)
        e = doc["ensemble"]
        assert e["n_members"] == 24 and e["seed"] == 3 and e["record"]["type"] == "qpe_ensemble"
        assert "ASSUMED" in e["disclaimer"]
        src = e["error_model"]["sources"]
        assert "ASSUMED DEFAULT" in src["corr_length_km"] and "ASSUMED DEFAULT" in src["shares"]
        assert "engine's own uncertainty" in src["sigma_ln"]
        ds = xr.open_dataset("e1.nc")
        mean = ds["ens_mean"].values
        wet = np.isfinite(mean) & (ds["accumulation"].values > 1)
        # the ensemble mean is the estimate (mean-preserving), up to the Monte Carlo error
        assert float(np.nanmean(mean[wet])) == pytest.approx(self.TRUE_MM, rel=0.1)
        assert ds["ens_mean"].attrs["provenance_type"] == "RETRIEVED"
        assert "ASSUMED" in ds["prob_ge_3.3mm"].attrs["ensemble_label"]
        p2, p33, p6 = (
            float(np.nanmean(ds[f"prob_ge_{t}mm"].values[wet])) for t in ("2", "3.3", "6")
        )
        assert p2 > p33 > p6 and p2 > 0.6 and p6 < 0.3
        rec = json.loads(ds["ens_std"].attrs["ensemble_record"])
        assert rec["seed"] == 3

        # same seed: the same fields; another seed: different ones
        run(capsys, *args, "--exceed", "2,3.3,6", "-o", "e2.nc", "-q")
        run(capsys, *args[:-2], "--seed", "4", "--exceed", "2,3.3,6", "-o", "e3.nc", "-q")
        a, b, c = (xr.open_dataset(f)["ens_std"].values for f in ("e1.nc", "e2.nc", "e3.nc"))
        assert np.array_equal(a, b, equal_nan=True)
        assert not np.array_equal(a, c, equal_nan=True)

    def test_options_need_an_ensemble_and_errors_are_usage_errors(self, capsys, volumes):
        code, _, err = run(capsys, "qpe", "-i", *volumes, *self.BASE, "--exceed", "5")
        assert code == 2 and "--ensemble" in err
        code, _, err = run(capsys, "qpe", "-i", *volumes, *self.BASE, "--ensemble", "1")
        assert code != 0 and "2 to 500" in err
        code, _, err = run(
            capsys,
            "qpe",
            "-i",
            *volumes,
            *self.BASE,
            "--ensemble",
            "5",
            "--error-shares",
            "0.5,0.5,0.5",
        )
        assert code != 0 and "add up to 1" in err

    def test_figure_field_and_basin_probabilities(self, capsys, volumes, tmp_path):
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
        code, out, _ = run(
            capsys,
            "qpe",
            "-i",
            *volumes,
            *self.BASE,
            "-b",
            gj,
            "--ensemble",
            "30",
            "--seed",
            "1",
            "--exceed",
            "1,3.3,10",
            "--field",
            "prob_ge_3.3mm",
            "-p",
            "prob.png",
            "--json",
        )
        assert code == 0 and Path("prob.png").stat().st_size > 3000
        row = json.loads(out)["ensemble"]["basins"][0]
        assert row["id"] == "H1"
        bm = row["basin_mean_mm"]
        assert bm["mean"] == pytest.approx(self.TRUE_MM, rel=0.1)
        # a basin mean is less variable than a cell: its 5-95 % range is narrower than a cell's
        cell_sd = json.loads(out)["ensemble"]["median_relative_spread"]
        assert (bm["p95"] - bm["p05"]) / (2 * 1.645) / bm["mean"] < cell_sd
        probs = row["prob_basin_mean_ge"]
        assert (
            probs["1"] > 0.95 and probs["10"] < 0.05 and probs["1"] >= probs["3.3"] >= probs["10"]
        )

    def test_gauges_give_the_model_and_a_reliability_check(self, capsys, volumes):
        # 6 x 6 gauges, each reading 5 mm where the radar says 3.33 mm: ratio 1.5
        lat = np.repeat(np.linspace(34.7, 35.3, 6), 6)
        lon = np.tile(np.linspace(-97.5, -96.5, 6), 6)
        pd.DataFrame(
            {
                "station": [f"G{i}" for i in range(36)],
                "lat": lat,
                "lon": lon,
                "time": "2024-05-01 20:20:00",
                "precip_mm": 5.0,
            }
        ).to_csv("gauges.csv", index=False)
        code, out, _ = run(
            capsys,
            "qpe",
            "-i",
            *volumes,
            *self.BASE,
            "--gauges",
            "gauges.csv",
            "--gauge-interval-s",
            "1200",
            "--adjust",
            "mfb",
            "--ensemble",
            "20",
            "--json",
        )
        assert code == 0
        e = json.loads(out)["ensemble"]
        gs = e["error_model"]["gauge_stats"]
        assert gs["used"] and gs["n_pairs"] >= 30
        assert gs["mean_ratio_gauge_over_radar"] == pytest.approx(1.5, rel=0.03)
        assert "gauges: root mean square" in e["error_model"]["sources"]["sigma_ln"]
        # the bias is entirely "common" error here (all gauges agree)
        assert e["error_model"]["shares"]["common"] > 0.9
        rel = e["reliability"]
        assert rel["validated"] and rel["model_from_the_same_gauges"] and rel["n_gauges"] >= 30
        assert "optimistic" in rel["note"]


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
