"""Gauges: reading, QC, time handling, adjustment with a known bias, validation metrics."""

from __future__ import annotations

import json
import math
import textwrap

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("radarx")

from engine.hydro import accumulate as acc
from engine.hydro import basins as hb
from engine.hydro import gauges as gg
from engine.hydro import products
from tests.hydro_synth import make_reader

LAT, LON = 35.0, -97.0


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(textwrap.dedent(text).lstrip())
    return p


class TestReading:
    def test_generic_csv_with_aliases_and_units(self, tmp_path):
        p = _write(
            tmp_path,
            "g.csv",
            """
            site,lat,lon,datetime,precip_in
            A,35.1,-97.1,2024-05-01 21:00:00,0.10
            A,35.1,-97.1,2024-05-01 22:00:00,0.20
            B,35.2,-96.9,2024-05-01 21:00:00,0.00
            """,
        )
        gs = gg.read_gauges(p, units="in")
        assert len(gs) == 2
        r = gs.records.sort_values(["station", "time_end"])
        assert r["depth_mm"].tolist() == pytest.approx([2.54, 5.08, 0.0])
        assert (r["interval_s"] == 3600).all()  # inferred from the spacing of A's records

    def test_column_mapping_and_unknown_column(self, tmp_path):
        p = _write(
            tmp_path,
            "g.csv",
            """
            id,y,x,when,rr
            A,35.1,-97.1,2024-05-01T21:00:00Z,1.5
            """,
        )
        gs = gg.read_gauges(
            p,
            mapping={"station": "id", "lat": "y", "lon": "x", "time": "when", "value": "rr"},
            interval_s=3600,
        )
        assert gs.records["depth_mm"].iloc[0] == 1.5
        with pytest.raises(gg.GaugeError, match="not in the file"):
            gg.read_gauges(p, mapping={"value": "nope"})

    def test_time_zone_and_time_convention(self, tmp_path):
        p = _write(
            tmp_path,
            "g.csv",
            """
            station,lat,lon,time,mm
            A,35.1,-97.1,2024-01-10 12:00:00,2.0
            """,
        )
        end = gg.read_gauges(p, tz="America/Chicago", interval_s=3600)  # CST = UTC-6
        assert end.records["time_end"].iloc[0] == pd.Timestamp("2024-01-10 18:00:00")
        start = gg.read_gauges(p, tz="UTC", interval_s=3600, time_convention="start")
        assert start.records["time_end"].iloc[0] == pd.Timestamp("2024-01-10 13:00:00")
        centre = gg.read_gauges(p, tz="UTC", interval_s=3600, time_convention="centre")
        assert centre.records["time_end"].iloc[0] == pd.Timestamp("2024-01-10 12:30:00")
        with pytest.raises(gg.GaugeError, match="time zone"):
            gg.read_gauges(p, tz="Mars/Olympus", interval_s=3600)

    def test_noaa_hourly_hpd_with_trace_missing_and_accumulation_flags(self, tmp_path):
        p = _write(
            tmp_path,
            "hpd.csv",
            """
            STATION,STATION_NAME,ELEVATION,LATITUDE,LONGITUDE,DATE,HPCP,Measurement Flag,Quality\
 Flag
            COOP:340001,OKC AIRPORT,390,35.39,-97.60,2024-05-01 21:00,0.10,,
            COOP:340001,OKC AIRPORT,390,35.39,-97.60,2024-05-01 22:00,T,,
            COOP:340001,OKC AIRPORT,390,35.39,-97.60,2024-05-01 23:00,999.99,,
            COOP:340001,OKC AIRPORT,390,35.39,-97.60,2024-05-02 00:00,0.00,[,
            COOP:340001,OKC AIRPORT,390,35.39,-97.60,2024-05-02 01:00,0.50,],
            """,
        )
        gs = gg.read_gauges(p)
        assert gs.source == "hpd.csv"
        qc = gs.quality_control()
        flags = dict(zip(qc.records["time_end"].dt.hour, qc.records["flag"], strict=True))
        assert flags[21] == ""  # 0.10 in = 2.54 mm
        assert flags[22] == ""  # trace counted as zero
        assert flags[23] == "missing"  # 999.99 sentinel
        assert flags[0] == "accumulating" and flags[1] == "accumulated_total"
        assert qc.records.loc[qc.records["time_end"].dt.hour == 21, "depth_mm"].iloc[
            0
        ] == pytest.approx(2.54)
        assert any("trace" in n for n in gs.notes)

    def test_ghcn_daily(self, tmp_path):
        p = _write(
            tmp_path,
            "d.csv",
            """
            STATION,DATE,LATITUDE,LONGITUDE,ELEVATION,NAME,PRCP,PRCP_ATTRIBUTES
            USC00340179,2024-05-01,35.3,-97.5,370,NORMAN,12.7,",,7,0700"
            USC00340179,2024-05-02,35.3,-97.5,370,NORMAN,0.0,",,7,0700"
            """,
        )
        gs = gg.read_gauges(p)
        assert (gs.records["interval_s"] == 86400).all()
        assert gs.records["depth_mm"].tolist() == [12.7, 0.0]
        assert gs.records["time_end"].iloc[0] == pd.Timestamp(
            "2024-05-02 00:00:00"
        )  # day 1 ends at 24 h
        assert any("not aligned to UTC" in n for n in gs.notes)

    def test_metar_precip_group(self, tmp_path):
        st = _write(tmp_path, "st.csv", "station,lat,lon\nKOKC,35.39,-97.60\n")
        p = _write(
            tmp_path,
            "m.txt",
            """
            KOKC 011953Z 18010KT 10SM RA BKN040 18/16 A2985 RMK AO2 P0012 T01780156
            KOKC 012053Z 18010KT 10SM -RA BKN040 18/16 A2985 RMK AO2 P0025 T01780156
            KOKC 012153Z 18010KT 10SM CLR 18/16 A2985 RMK AO2 T01780156
            """,
        )
        with pytest.raises(gg.GaugeError, match="stations"):
            gg.read_gauges(p, fmt="metar", year=2024, month=5)
        gs = gg.read_gauges(p, fmt="metar", stations=st, year=2024, month=5)
        r = gs.records.sort_values("time_end")
        assert r["depth_mm"].tolist() == pytest.approx([0.12 * 25.4, 0.25 * 25.4, 0.0])
        assert r["time_end"].iloc[0] == pd.Timestamp("2024-05-01 20:00:00")  # rounded to the hour

    def test_json_records_and_geojson_points(self, tmp_path):
        recs = [
            {
                "station": "A",
                "lat": 35.1,
                "lon": -97.1,
                "time": "2024-05-01T21:00:00Z",
                "precip_mm": 3.0,
            },
            {
                "station": "A",
                "lat": 35.1,
                "lon": -97.1,
                "time": "2024-05-01T22:00:00Z",
                "precip_mm": 4.0,
            },
        ]
        p = tmp_path / "g.json"
        p.write_text(json.dumps(recs))
        assert len(gg.read_gauges(p).records) == 2
        fc = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [-97.1, 35.1]},
                    "properties": {"station": "A", "time": "2024-05-01T21:00:00Z", "precip": 1.0},
                }
            ],
        }
        q = tmp_path / "g.geojson"
        q.write_text(json.dumps(fc))
        gs = gg.read_gauges(q, interval_s=3600)
        assert gs.stations["lon"].iloc[0] == -97.1

    def test_missing_file_and_missing_coordinates_say_what_to_do(self, tmp_path):
        with pytest.raises(gg.GaugeError, match="not found"):
            gg.read_gauges(tmp_path / "nope.csv")
        p = _write(tmp_path, "nc.csv", "station,time,mm\nA,2024-05-01 21:00:00,1\n")
        with pytest.raises(gg.GaugeError, match="coordinates"):
            gg.read_gauges(p, interval_s=3600)


# ---------------------------------------------------------------------------
# Quality control
# ---------------------------------------------------------------------------


def _records(rows):
    stations = pd.DataFrame(
        {
            "station": ["A", "B"],
            "lat": [35.1, 35.2],
            "lon": [-97.1, -97.0],
            "name": ["A", "B"],
            "elevation_m": [0.0, 0.0],
        }
    )
    rec = pd.DataFrame(rows, columns=["station", "time_end", "interval_s", "depth_mm"])
    rec["time_end"] = pd.to_datetime(rec["time_end"])
    rec["flag"] = ""
    return gg.GaugeSet(stations, rec, "synthetic")


class TestQuality:
    def test_flags(self):
        t = pd.date_range("2024-05-01 00:00", periods=10, freq="h")
        rows = [
            ("A", t[0], 3600, 1.0),
            ("A", t[1], 3600, np.nan),
            ("A", t[2], 3600, -0.4),
            ("A", t[3], 3600, 2.0),
            ("A", t[3], 3600, 2.0),  # identical duplicate
            ("A", t[4], 3600, 5.0),
            ("A", t[4], 3600, 6.0),  # conflicting duplicate
            ("A", t[5], 3600, 450.0),
        ]  # 450 mm in an hour: implausible
        rows += [("B", t[i], 3600, 1.2) for i in range(7)]  # a stuck gauge: 7 identical values
        gs = _records(rows).quality_control(stuck_n=6)
        r = gs.records
        a = r[r["station"] == "A"]
        assert a.loc[a["time_end"] == t[1], "flag"].iloc[0] == "missing"
        assert a.loc[a["time_end"] == t[2], "flag"].iloc[0] == "negative"
        dups = a[a["time_end"] == t[3]]["flag"].tolist()
        assert sorted(dups) == ["", "duplicate"]
        assert set(a[a["time_end"] == t[4]]["flag"]) == {"duplicate_conflict"}
        assert a.loc[a["time_end"] == t[5], "flag"].iloc[0] == "implausible"
        assert (r[r["station"] == "B"]["flag"] == "stuck").all()
        s = gs.qc_summary()
        assert s["missing"] == 1 and s["stuck"] == 7 and s["duplicate_conflict"] == 2

    def test_zero_runs_are_not_stuck(self):
        t = pd.date_range("2024-05-01 00:00", periods=12, freq="h")
        gs = _records([("A", x, 3600, 0.0) for x in t]).quality_control()
        assert (gs.records["flag"] == "").all()

    def test_qc_is_idempotent(self):
        t = pd.date_range("2024-05-01 00:00", periods=3, freq="h")
        gs = _records([("A", t[0], 3600, 1.0), ("A", t[1], 3600, -1.0), ("A", t[2], 3600, 2.0)])
        once = gs.quality_control()
        twice = once.quality_control()
        assert once.records["flag"].tolist() == twice.records["flag"].tolist()


class TestTotals:
    def test_sum_inside_the_period_only(self):
        t = pd.date_range("2024-05-01 21:00", periods=6, freq="h")
        gs = _records([("A", x, 3600, float(i + 1)) for i, x in enumerate(t)]).quality_control()
        tot = gs.totals(np.datetime64("2024-05-01T20:00:00"), np.datetime64("2024-05-01T23:00:00"))
        # records ending 21:00, 22:00, 23:00 lie wholly inside [20:00, 23:00]
        assert tot["depth_mm"].iloc[0] == pytest.approx(1 + 2 + 3)
        assert tot["coverage"].iloc[0] == pytest.approx(1.0)

    def test_coverage_rule_and_prorating(self):
        t = pd.date_range("2024-05-01 21:00", periods=2, freq="h")
        gs = _records([("A", x, 3600, 2.0) for x in t]).quality_control()
        start, end = np.datetime64("2024-05-01T20:30:00"), np.datetime64("2024-05-01T22:00:00")
        assert gs.totals(
            start, end, min_coverage=0.9
        ).empty  # the 20:00-21:00 record straddles the start
        pro = gs.totals(start, end, min_coverage=0.9, prorate=True)
        assert pro["depth_mm"].iloc[0] == pytest.approx(
            2.0 * 0.5 + 2.0
        )  # an assumption, flagged in the docs

    def test_flagged_records_are_never_summed(self):
        t = pd.date_range("2024-05-01 21:00", periods=3, freq="h")
        gs = _records(
            [("A", t[0], 3600, 1.0), ("A", t[1], 3600, -5.0), ("A", t[2], 3600, 1.0)]
        ).quality_control()
        tot = gs.totals(
            np.datetime64("2024-05-01T20:00:00"),
            np.datetime64("2024-05-01T23:00:00"),
            min_coverage=0.5,
        )
        assert tot["depth_mm"].iloc[0] == pytest.approx(2.0)
        assert tot["coverage"].iloc[0] == pytest.approx(2 / 3, rel=1e-6)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class TestMetrics:
    def test_perfect_and_scaled(self):
        g = np.array([1.0, 2.0, 4.0, 8.0, 16.0])
        m = gg.metrics(g, g)
        assert m["bias_mm"] == 0 and m["rmse_mm"] == 0 and m["correlation"] == pytest.approx(1.0)
        assert m["kge"] == pytest.approx(1.0)
        m2 = gg.metrics(2.0 * g, g)
        assert m2["relative_bias"] == pytest.approx(1.0)
        assert m2["correlation"] == pytest.approx(1.0)
        # KGE of a pure x2 scale: r = 1, alpha = 2, beta = 2 -> 1 - sqrt(0 + 1 + 1)
        assert m2["kge"] == pytest.approx(1.0 - math.sqrt(2.0))
        assert m2["rmse_mm"] == pytest.approx(math.sqrt(np.mean(g**2)))

    def test_known_numbers(self):
        g = np.array([1.0, 2.0, 3.0, 4.0])
        r = np.array([1.5, 1.5, 3.5, 3.5])
        m = gg.metrics(r, g)
        assert m["bias_mm"] == pytest.approx(0.0)
        assert m["mae_mm"] == pytest.approx(0.5)
        assert m["rmse_mm"] == pytest.approx(0.5)
        assert m["correlation"] == pytest.approx(np.corrcoef(r, g)[0, 1])

    def test_stratification(self):
        n = 40
        p = gg.Pairs(
            station=np.array([f"s{i}" for i in range(n)]),
            lon=np.zeros(n),
            lat=np.zeros(n),
            x=np.zeros(n),
            y=np.zeros(n),
            gauge=np.linspace(0.5, 60, n),
            radar=np.linspace(0.5, 60, n) * 1.2,
            range_km=np.linspace(5, 220, n),
            coverage=np.ones(n),
        )
        v = gg.validate(p)
        assert v["overall"]["relative_bias"] == pytest.approx(0.2)
        assert len(v["by_amount_mm"]) >= 4 and len(v["by_range_km"]) == 4
        assert all(row["relative_bias"] == pytest.approx(0.2) for row in v["by_range_km"])
        assert len(v["points"]) == n and v["points"][0]["residual_mm"] == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# Adjustment with a known bias
# ---------------------------------------------------------------------------


def _pairs(n=24, bias=lambda x, y: 1.5, noise=0.0, seed=1):
    rng = np.random.default_rng(seed)
    x = rng.uniform(-60e3, 60e3, n)
    y = rng.uniform(-60e3, 60e3, n)
    truth = 20.0 + 10.0 * np.sin(x / 25e3) + 6.0 * np.cos(y / 30e3)
    radar = truth / bias(x, y)
    gauge = truth * np.exp(rng.normal(0, noise, n)) if noise else truth
    return gg.Pairs(
        station=np.array([f"s{i}" for i in range(n)]),
        lon=np.zeros(n),
        lat=np.zeros(n),
        x=x,
        y=y,
        gauge=gauge,
        radar=radar,
        range_km=np.hypot(x, y) / 1e3,
        coverage=np.ones(n),
    ), truth


class TestAdjustment:
    def test_mean_field_bias_recovers_the_factor(self):
        p, _ = _pairs(bias=lambda x, y: 1.5)
        ad = gg.Adjuster("mfb").fit(p)
        assert ad.mfb == pytest.approx(1.5, rel=1e-9)
        lo, hi = ad.params["mfb_ci90"]
        assert lo <= 1.5 <= hi
        assert ad.predict_points(p.x, p.y, p.radar) == pytest.approx(p.gauge)

    def test_mean_field_bias_with_noisy_gauges(self):
        p, _ = _pairs(n=60, bias=lambda x, y: 1.5, noise=0.15)
        ad = gg.Adjuster("mfb").fit(p)
        assert ad.mfb == pytest.approx(1.5, rel=0.06)

    def test_too_few_gauges_is_an_error_with_the_fix(self):
        p, _ = _pairs(n=2)
        with pytest.raises(gg.GaugeError, match="at least 3"):
            gg.Adjuster("mfb").fit(p)

    @pytest.mark.parametrize("method", ["brandes", "ked"])
    def test_spatial_methods_beat_the_mean_field_for_a_spatial_bias(self, method):
        """Radar bias grows from 1.0 in the west to 2.0 in the east: one factor cannot fix it."""

        def bias(x, y):
            return 1.0 + (x + 60e3) / 120e3

        p, _ = _pairs(n=40, bias=bias)
        loo_mfb = gg.cross_validate(p, "mfb")
        loo = gg.cross_validate(p, method)
        assert loo["adjusted"]["rmse_mm"] < loo["unadjusted"]["rmse_mm"]
        assert loo["adjusted"]["rmse_mm"] < 0.7 * loo_mfb["adjusted"]["rmse_mm"]

    def test_ked_exact_at_the_gauges_and_nonnegative(self):
        p, _ = _pairs(n=20, bias=lambda x, y: 1.0 + (x + 60e3) / 120e3)
        ad = gg.Adjuster("ked").fit(p)
        out = ad.predict_points(ad._x, ad._y, ad._r)
        assert out == pytest.approx(ad._g, rel=1e-6, abs=1e-6)  # kriging is an exact interpolator
        far = ad.predict_points(np.array([500e3]), np.array([500e3]), np.array([5.0]))
        assert far[0] >= 0

    def test_end_to_end_on_a_result_with_provenance(self, tmp_path):
        readers = [
            make_reader(LAT, LON, lambda x, y: np.full(np.broadcast(x, y).shape, 10.0), t)
            for t in ("2024-05-01T20:00:00", "2024-05-01T20:10:00", "2024-05-01T20:20:00")
        ]
        basin = hb.BasinSet.from_polygon(
            [(-97.2, 34.9), (-96.8, 34.9), (-96.8, 35.2), (-97.2, 35.2)], "wshed"
        )
        res = acc.accumulate(
            readers, {"min_dbz": -30.0}, resolution_m=2000.0, max_range_km=100.0, basins=basin
        )
        radar_total = 10.0 * 20 / 60  # 3.33 mm
        # gauges that measured 5.0 mm: the radar is biased low by 1.5; hourly records 20:00-21:00
        stations = pd.DataFrame(
            {
                "station": [f"G{i}" for i in range(6)],
                "lat": [35.05, 35.1, 34.95, 35.2, 35.0, 35.15],
                "lon": [-97.1, -96.9, -97.0, -97.05, -96.85, -96.95],
                "name": "g",
                "elevation_m": 0.0,
            }
        )
        rec = pd.DataFrame(
            {
                "station": stations["station"],
                "time_end": pd.Timestamp("2024-05-01 20:20:00"),
                "interval_s": 1200.0,
                "depth_mm": 5.0,
                "flag": "",
            }
        )
        gs = gg.GaugeSet(stations, rec, "synthetic.csv")
        report = gg.adjust(res, gs, method="mfb", min_amount=1.0)
        wet = np.isfinite(res.accumulation) & (res.accumulation > 1)
        assert res.accumulation[wet] == pytest.approx(radar_total, rel=3e-3)
        assert res.accumulation_adjusted[wet] == pytest.approx(5.0, rel=3e-3)
        assert res.adjusted["type"] == "ADJUSTED"
        assert res.adjusted["n_gauges"] == 6 and "G0" in res.adjusted["gauges_used"]
        assert res.provenance(adjusted=True)["type"] == "ADJUSTED"
        assert res.provenance(adjusted=True)["adjusted_from"] == "RETRIEVED"
        assert report["validation_before"]["overall"]["relative_bias"] == pytest.approx(
            radar_total / 5.0 - 1.0, rel=2e-3
        )
        assert report["validation_after_in_sample"]["overall"]["relative_bias"] == pytest.approx(
            0.0, abs=2e-3
        )
        # the basin series carries the adjustment as well
        bs = res.basin_series
        assert bs.adjusted is not None
        assert bs.cum_mean[0][-1] == pytest.approx(5.0, rel=5e-3)
        # NetCDF labels the adjusted field ADJUSTED and keeps the radar field RETRIEVED
        import xarray as xr

        ds = xr.open_dataset(products.write_netcdf(res, tmp_path / "adj.nc"))
        assert ds["accumulation"].attrs["provenance_type"] == "RETRIEVED"
        assert ds["accumulation_adjusted"].attrs["provenance_type"] == "ADJUSTED"
        assert ds["accumulation_adjusted"].attrs["adjustment_method"] == "mean-field bias"
        assert ds.attrs["provenance_type"] == "ADJUSTED"
        assert "G3" not in ds["accumulation_adjusted"].attrs.get("adjustment_gauge_source", "G3x")

    def test_no_gauge_in_the_period_is_an_error(self):
        readers = [
            make_reader(LAT, LON, lambda x, y: np.full(np.broadcast(x, y).shape, 5.0), t)
            for t in ("2024-05-01T20:00:00", "2024-05-01T20:10:00")
        ]
        res = acc.accumulate(readers, {"min_dbz": -30.0}, resolution_m=2000.0, max_range_km=80.0)
        stations = pd.DataFrame(
            {"station": ["A"], "lat": [35.0], "lon": [-97.0], "name": "a", "elevation_m": 0.0}
        )
        rec = pd.DataFrame(
            {
                "station": ["A"],
                "time_end": pd.Timestamp("2024-05-01 05:00:00"),
                "interval_s": 3600.0,
                "depth_mm": 1.0,
                "flag": "",
            }
        )
        with pytest.raises(gg.GaugeError, match="no gauge has valid records"):
            gg.adjust(res, gg.GaugeSet(stations, rec, "x"))


# ---------------------------------------------------------------------------
# Basin series on a real accumulation
# ---------------------------------------------------------------------------


class TestBasinSeries:
    def _res(self):
        readers = [
            make_reader(LAT, LON, lambda x, y: np.full(np.broadcast(x, y).shape, 12.0), t)
            for t in (
                "2024-05-01T20:00:00",
                "2024-05-01T20:15:00",
                "2024-05-01T20:30:00",
                "2024-05-01T21:00:00",
            )
        ]
        basin = hb.BasinSet.from_polygon(
            [(-97.2, 34.9), (-96.8, 34.9), (-96.8, 35.2), (-97.2, 35.2)], "w"
        )
        return acc.accumulate(
            readers,
            {"min_dbz": -30.0},
            resolution_m=2000.0,
            max_range_km=100.0,
            basins=basin,
            max_gap_s=2000.0,
        )

    def test_mean_rate_cumulative_and_hyetograph(self):
        res = self._res()
        bs = res.basin_series
        s = bs.summary()[0]
        assert s["area_km2"] == pytest.approx(bs.weights[0].geodesic_area_km2, rel=3e-3)
        assert s["coverage"] == pytest.approx(1.0)
        assert s["accumulation_mean_mm"] == pytest.approx(12.0, rel=3e-3)  # 12 mm/h for 1 h
        assert np.nanmax(bs.mean_rate[0]) == pytest.approx(12.0, rel=3e-3)
        assert bs.cum_mean[0][-1] == pytest.approx(12.0, rel=3e-3)
        hy = bs.hyetograph(0, bin_minutes=15)
        assert sum(hy["depth_mm"]) == pytest.approx(12.0, rel=3e-3)
        assert hy["depth_mm"][0] == pytest.approx(3.0, rel=5e-3)
        assert s["area_acc_ge_10mm_km2"] == pytest.approx(s["area_km2"], rel=0.02)
        assert s["area_acc_ge_25mm_km2"] == 0.0

    def test_exceedance_hook_uses_the_basin_mean_depth(self):
        res = self._res()
        table = hb.FrequencyTable(1.0, np.array([2.0, 10.0, 100.0]), np.array([8.0, 12.0, 20.0]))
        ex = hb.exceedance(res.basin_series, table)[0]
        assert ex["max_depth_mm"] == pytest.approx(12.0, rel=3e-3)
        assert ex["return_period_years"] == pytest.approx(10.0, rel=0.05)
        assert ex["duration_h"] == 1.0

    def test_csv_tables_carry_provenance(self):
        res = self._res()
        text = products.basin_table_csv(res)
        assert "provenance_type: RETRIEVED" in text
        assert "accumulation_mean_mm" in text
        series = products.basin_series_csv(res, 0)
        assert series.splitlines()[1].startswith("time,mean_rate_mm_h")
