"""Flash-flood indicators: ratio, return period, relative-to-climatology, the files you supply."""

from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pytest
import xarray as xr

from engine.hydro import grid as hgrid
from engine.hydro import indicators as hi

GRID = hgrid.make_grid((-88.9, 35.4, -87.1, 36.6), 2000.0)


def _result(acc, duration_s=3600.0, adjusted=None):
    t0 = np.datetime64("2024-05-01T20:00:00", "ns")
    return SimpleNamespace(
        accumulation=np.asarray(acc, dtype=np.float32),
        accumulation_adjusted=adjusted,
        duration_s=duration_s,
        start=t0,
        end=t0 + np.timedelta64(int(duration_s * 1e9), "ns"),
        grid=GRID,
        source_files=["a_V06"],
    )


def _flat(value):
    return np.full(GRID.shape, value, dtype=np.float64)


class TestRatioAndClasses:
    def test_ratio_is_exact_and_missing_stays_missing(self):
        rain = np.array([[10.0, 30.0], [np.nan, 5.0]])
        guid = np.array([[20.0, 20.0], [20.0, 0.0]])
        r = hi.ratio_to_guidance(rain, guid)
        assert r[0, 0] == 0.5 and r[0, 1] == 1.5
        assert np.isnan(r[1, 0]) and np.isnan(r[1, 1])  # missing rain; guidance 0

    def test_classes_follow_the_edges(self):
        v = np.array([0.1, 0.5, 0.74, 0.75, 0.999, 1.0, 1.49, 1.5, 3.0, np.nan])
        c = hi.classify(v, hi.RATIO_EDGES)
        assert list(c) == [0, 1, 1, 2, 2, 3, 3, 4, 4, -1]
        assert len(hi.RATIO_LABELS) == len(hi.RATIO_EDGES) + 1

    def test_no_label_says_warning(self):
        for labels in (hi.RATIO_LABELS, hi.RETURN_LABELS, hi.REL_LABELS):
            assert not any("warning" in s.lower() for s in labels)


class TestReturnPeriod:
    periods = (2.0, 10.0, 100.0)

    def _stack(self):
        # point depths for 2, 10, 100 years: 40, 80, 160 mm everywhere
        return [_flat(40.0), _flat(80.0), _flat(160.0)]

    def test_log_log_interpolation_known_answer(self):
        # depth 60 mm: between 40 (2 y) and 80 (10 y): ln-linear -> T = 2 * (10/2)^(ln(60/40)/ln(2))
        want = math.exp(
            math.log(2.0)
            + (math.log(60 / 40) / math.log(80 / 40)) * (math.log(10.0) - math.log(2.0))
        )
        y, flag = hi.return_period_grid(_flat(60.0), self.periods, self._stack())
        assert y[3, 3] == pytest.approx(want, rel=1e-5)  # radarx returns float32
        assert flag[3, 3] == 0
        # exact table values give the table periods
        for d, t in ((40.0, 2.0), (80.0, 10.0), (160.0, 100.0)):
            yy, _ = hi.return_period_grid(_flat(d), self.periods, self._stack())
            assert yy[0, 0] == pytest.approx(t, rel=1e-5)

    def test_outside_the_table_is_flagged_not_extrapolated(self):
        y, f = hi.return_period_grid(_flat(20.0), self.periods, self._stack())
        assert y[0, 0] == 2.0 and f[0, 0] == -1
        y, f = hi.return_period_grid(_flat(500.0), self.periods, self._stack())
        assert y[0, 0] == 100.0 and f[0, 0] == 1

    def test_missing_or_nonsense_inputs_give_no_value(self):
        st = self._stack()
        st[1][0, 0] = np.nan
        d = _flat(60.0)
        d[1, 1] = np.nan
        y, _ = hi.return_period_grid(d, self.periods, st)
        assert np.isnan(y[0, 0]) and np.isnan(y[1, 1]) and np.isfinite(y[2, 2])
        # depth that falls with the period is not a frequency table
        bad = [_flat(80.0), _flat(40.0), _flat(160.0)]
        y, _ = hi.return_period_grid(_flat(60.0), self.periods, bad)
        assert np.isnan(y).all()
        with pytest.raises(hi.IndicatorError, match="increasing"):
            hi.return_period_grid(_flat(1.0), [10, 2], st[:2])


class TestComputeAndLabels:
    def test_ratio_layer_and_provenance(self):
        res = _result(_flat(30.0))
        ind = hi.compute(
            res, duration_h=1.0, guidance=(_flat(40.0), {"file": "ffg.nc", "vintage": "x"})
        )
        lay = ind.layers["ffg_ratio_1h"]
        assert np.allclose(lay.values, 0.75)
        assert (lay.classes == 2).all()  # 75 to 100 %
        p = lay.provenance
        assert p["class"] == "RETRIEVED" and p["label"] == "RETRIEVED INDICATOR"
        assert "not an official warning" in p["product_text"]
        assert p["guidance"]["file"] == "ffg.nc" and p["period"][0].startswith("2024-05-01T20")
        s = ind.summary()
        assert "Antecedent soil moisture" in s["not_modelled"]
        assert (
            s["layers"]["ffg_ratio_1h"]["classes"][hi.RATIO_LABELS[2]]["cells"]
            == GRID.shape[0] * GRID.shape[1]
        )

    def test_forecast_class_is_carried(self):
        ind = hi.compute(_result(_flat(10.0)), guidance=(_flat(20.0), {}), input_class="FORECAST")
        assert ind.layers["ffg_ratio_1h"].provenance["label"] == "FORECAST INDICATOR"
        with pytest.raises(hi.IndicatorError, match="RETRIEVED"):
            hi.compute(_result(_flat(10.0)), guidance=(_flat(20.0), {}), input_class="OBSERVED")

    def test_short_period_is_a_lower_bound_long_period_is_refused(self):
        short = hi.compute(_result(_flat(10.0), 1200.0), guidance=(_flat(20.0), {}))
        assert short.layers["ffg_ratio_1h"].provenance["lower_bound"] is True
        assert any("lower bounds" in n for n in short.notes)
        with pytest.raises(hi.IndicatorError, match="longer than"):
            hi.compute(_result(_flat(10.0), 7200.0), guidance=(_flat(20.0), {}))
        with pytest.raises(hi.IndicatorError, match="not one rate"):
            hi.compute(_result(_flat(10.0), 0.0), guidance=(_flat(20.0), {}))

    def test_adjusted_rain_is_used_and_named(self):
        res = _result(_flat(10.0), adjusted=_flat(20.0).astype(np.float32))
        ind = hi.compute(res, guidance=(_flat(40.0), {}))
        assert np.allclose(ind.layers["ffg_ratio_1h"].values, 0.5)
        assert ind.layers["ffg_ratio_1h"].provenance["rain_provenance"].startswith("ADJUSTED")

    def test_needs_an_input(self):
        with pytest.raises(hi.IndicatorError, match="at least one"):
            hi.compute(_result(_flat(1.0)))

    def test_relative_indicator_is_the_lab_reference_rule(self):
        # rain / the cell's own climatological reference; 1 = at the reference
        ref = _flat(25.0)
        ref[:, :5] = 50.0
        res = _result(_flat(30.0))
        ind = hi.compute(res, climatology=(ref, {"file": "clim.nc"}))
        lay = ind.layers["relative_1h"]
        assert lay.values[0, 10] == pytest.approx(1.2) and lay.values[0, 2] == pytest.approx(0.6)
        assert lay.classes[0, 10] == 2 and lay.classes[0, 2] == 1
        assert "did not beat this rule" in lay.provenance["lab_result"]
        # a 6 h period is not the one-hour indicator: said
        six = hi.compute(_result(_flat(30.0), 6 * 3600.0), duration_h=6.0, climatology=(ref, {}))
        assert any("one-hour" in n for n in six.notes)

    def test_climatology_from_a_record_needs_enough_hours(self):
        rng = np.random.default_rng(0)
        stack = [rng.gamma(0.5, 2.0, (4, 4)) for _ in range(2000)]
        c = hi.climatology_from_stack(stack, 0.995)
        want = np.quantile(np.stack(stack), 0.995, axis=0)
        assert np.allclose(c, want)
        few = hi.climatology_from_stack(stack[:10], 0.995)
        assert np.isnan(few).all()
        with pytest.raises(hi.IndicatorError, match="between"):
            hi.climatology_from_stack(stack, 0.2)


class TestReadingFiles:
    def _nc(
        self,
        tmp_path,
        name="ffg.nc",
        units="in",
        fill=0.5,
        flip=False,
        vintage="issued 2024-05-01 18Z",
    ):
        lat = np.linspace(35.0, 37.0, 21)
        lon = np.linspace(-89.5, -86.5, 31)
        v = np.full((lat.size, lon.size), fill, dtype=np.float32)
        v += (lon[None, :] + 89.5).astype(np.float32) * 0.1  # a ramp in longitude
        if flip:
            lat, v = lat[::-1], v[::-1]
        ds = xr.Dataset(
            {"ffg": (("lat", "lon"), v, {"units": units})},
            coords={"lat": lat, "lon": lon},
            attrs={"vintage": vintage, "source": "test file"},
        )
        p = tmp_path / name
        ds.to_netcdf(p)
        return p

    def test_netcdf_units_regrid_and_vintage(self, tmp_path):
        p = self._nc(tmp_path)
        kw = hi.load_inputs(GRID, guidance=p)
        arr, meta = kw["guidance"]
        lon, _ = GRID.lonlat()
        want = (0.5 + (lon + 89.5) * 0.1) * 25.4  # inches -> mm, bilinear of a linear ramp is exact
        ok = np.isfinite(arr)
        assert ok.mean() > 0.95
        assert np.allclose(arr[ok], want[ok], rtol=1e-4)
        assert meta["vintage"] == "issued 2024-05-01 18Z" and meta["units_in_file"] == "in"
        assert meta["file"] == "ffg.nc" and "bilinear" in meta["regridded"]

    def test_descending_latitudes_give_the_same_answer(self, tmp_path):
        a = hi.load_inputs(GRID, guidance=self._nc(tmp_path, "a.nc"))["guidance"][0]
        b = hi.load_inputs(GRID, guidance=self._nc(tmp_path, "b.nc", flip=True))["guidance"][0]
        assert np.allclose(a, b, equal_nan=True)

    def test_user_vintage_overrides_and_missing_is_stated(self, tmp_path):
        p = self._nc(tmp_path, vintage="")
        assert hi.load_inputs(GRID, guidance=p)["guidance"][1]["vintage"] == "not given"
        m = hi.load_inputs(GRID, guidance=p, vintage="my note")["guidance"][1]
        assert m["vintage"] == "my note"

    def test_unknown_units_and_several_variables_are_errors(self, tmp_path):
        p = self._nc(tmp_path, units="furlongs")
        with pytest.raises(hi.IndicatorError, match="unknown depth unit"):
            hi.load_inputs(GRID, guidance=p)
        ds = xr.Dataset(
            {"a": (("lat", "lon"), np.ones((3, 3))), "b": (("lat", "lon"), np.ones((3, 3)))},
            coords={"lat": [1.0, 2, 3], "lon": [1.0, 2, 3]},
        )
        q = tmp_path / "two.nc"
        ds.to_netcdf(q)
        with pytest.raises(hi.IndicatorError, match="several 2-D variables"):
            hi.read_netcdf(q)
        assert hi.read_netcdf(q, "a").values.shape == (3, 3)

    def test_csv_points_nearest_within_distance(self, tmp_path):
        p = tmp_path / "zones.csv"
        p.write_text("zone,lat,lon,ffg_mm\nA,35.9,-88.5,40\nB,36.3,-87.5,60\n")
        kw = hi.load_inputs(GRID, guidance=p, max_distance_km=500.0)
        arr, _ = kw["guidance"]
        lon, lat = GRID.lonlat()
        near_a = np.hypot((lat - 35.9) * 111.2, (lon + 88.5) * 111.2 * np.cos(np.radians(35.9)))
        near_b = np.hypot((lat - 36.3) * 111.2, (lon + 87.5) * 111.2 * np.cos(np.radians(36.3)))
        a_is_nearer = near_a < near_b
        assert np.all(arr[a_is_nearer] == 40.0) and np.all(arr[~a_is_nearer] == 60.0)
        far, far_meta = hi.load_inputs(GRID, guidance=p, max_distance_km=5.0)["guidance"]
        assert np.isnan(far).mean() > 0.9 and "within 5 km" in far_meta["regridded"]

    def test_geotiff_with_tags(self, tmp_path):
        from PIL import Image

        ny, nx = 12, 18
        data = np.arange(ny * nx, dtype=np.float32).reshape(ny, nx)
        im = Image.fromarray(data, mode="F")
        # lon -89.4 .. -86.4 (0.1667 deg), lat top 36.9 going down
        dx = dy = 0.2
        tags = {
            33550: (dx, dy, 0.0),
            33922: (0.0, 0.0, 0.0, -89.5, 37.0, 0.0),
        }
        p = tmp_path / "g.tif"
        from PIL import TiffImagePlugin

        ifd = TiffImagePlugin.ImageFileDirectory_v2()
        for k, v in tags.items():
            ifd[k] = v
        im.save(p, tiffinfo=ifd)
        src = hi.read_geotiff(p, units="mm")
        assert src.values.shape == (ny, nx)
        assert src.lon[0] == pytest.approx(-89.5 + dx / 2) and src.lat[-1] == pytest.approx(
            37.0 - dy / 2
        )
        # north-up file: the first image row is the highest latitude; after the flip the last row is
        assert src.values[-1, 0] == data[0, 0] and src.values[0, 0] == data[-1, 0]
        bare = tmp_path / "bare.tif"
        Image.fromarray(data, mode="F").save(bare)
        with pytest.raises(hi.IndicatorError, match="georeferencing"):
            hi.read_geotiff(bare)

    def test_unknown_extension_and_missing_file(self, tmp_path):
        with pytest.raises(hi.IndicatorError, match="NetCDF"):
            hi.read_field(tmp_path / "x.grb")
        with pytest.raises(hi.IndicatorError, match="not found"):
            hi.read_field(tmp_path / "x.nc")


class TestOutputs:
    def test_netcdf_carries_labels_and_inputs(self, tmp_path):
        res = _result(_flat(30.0))
        ind = hi.compute(
            res,
            guidance=(_flat(40.0), {"file": "ffg.nc", "vintage": "v1"}),
            frequency=([2.0, 10.0], [_flat(20.0), _flat(40.0)], {"file": "f.nc", "vintage": "v2"}),
        )
        ds = hi.to_dataset(ind)
        assert ds["ffg_ratio_1h"].attrs["product_label"] == "RETRIEVED INDICATOR"
        assert "not an official warning" in ds.attrs["comment"]
        assert "ffg.nc" in ds.attrs["source"] and "ffg_ratio_1h_class" in ds
        assert "return_period_1h_flag" in ds
        p = tmp_path / "i.nc"
        ds.to_netcdf(p)
        back = xr.open_dataset(p)
        assert float(back["ffg_ratio_1h"][0, 0]) == pytest.approx(0.75)
        assert "vintage" in back["ffg_ratio_1h"].attrs["indicator_parameters"]

    def test_figure_says_what_it_is(self):
        ind = hi.compute(
            _result(_flat(30.0)), guidance=(_flat(40.0), {"file": "ffg.nc", "vintage": "v1"})
        )
        fig = hi.figure(ind, "ffg_ratio_1h")
        txt = " ".join(t.get_text() for t in fig.findobj(lambda o: hasattr(o, "get_text")))
        assert (
            "RETRIEVED INDICATOR" in txt
            and "not an official warning" in txt
            and "vintage: v1" in txt
        )
        import matplotlib.pyplot as plt

        plt.close(fig)


class TestRadarxParity:
    """The algorithms live in radarx; the built-in copies are a transitional fallback.

    Skipped when the installed radarx lacks them."""

    @staticmethod
    def _need(name):
        if hi._radarx(name) is None:
            pytest.skip(f"the installed radarx has no {name} yet")

    def test_return_period_agrees(self, monkeypatch):
        self._need("return_period")
        rng = np.random.default_rng(3)
        base = rng.uniform(20, 60, (30, 30))
        stack = [base, base * 1.6, base * 2.9]
        depth = rng.uniform(10, 200, (30, 30))
        depth[3, 3] = np.nan
        stack[1][5, 5] = np.nan
        a, fa = hi.return_period_grid(depth, [2, 10, 100], stack)
        monkeypatch.setattr(hi, "_radarx", lambda name: None)
        b, fb = hi.return_period_grid(depth, [2, 10, 100], stack)
        assert np.allclose(a, b, rtol=1e-5, equal_nan=True) and np.array_equal(fa, fb)

    def test_climatology_agrees(self, monkeypatch):
        self._need("climatological_reference")
        rng = np.random.default_rng(1)
        stack = [rng.gamma(0.5, 2.0, (5, 6)) for _ in range(500)]
        stack[0][0, 0] = np.nan
        a = hi.climatology_from_stack(stack, 0.99)
        monkeypatch.setattr(hi, "_radarx", lambda name: None)
        b = hi.climatology_from_stack(stack, 0.99)
        assert np.allclose(a, b, rtol=1e-5, equal_nan=True)

    def test_uses_radarx_says_which(self):
        u = hi.uses_radarx()
        assert set(u) == {"return_period", "climatological_reference"}
        assert all(isinstance(v, bool) for v in u.values())
