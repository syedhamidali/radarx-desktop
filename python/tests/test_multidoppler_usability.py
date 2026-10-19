"""Usability features of the multi-Doppler retrieval: automatic dealiasing, the automatic lowest
level and vertical boundary, the output quality control, the per-level coverage and the wind
figures (engine/windfig.py). Synthetic volumes from ``tests.test_multidoppler``; the real-case
smoke tests at the end skip when the NEXRAD files are not on this machine.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import ClassVar

import numpy as np
import pytest
import xarray as xr

from engine import multidoppler as mdop
from tests.test_multidoppler import (
    GRID,
    SEPARATION_KM,
    field,
    make_reader,
    make_wind_volume,
    site_east_of_origin,
)

HAS_RADARX = importlib.util.find_spec("radarx") is not None
needs_radarx = pytest.mark.skipif(not HAS_RADARX, reason="radarx is not installed")
NO_FALL = {"fall_speed_correction": False}


@pytest.fixture(autouse=True)
def _fresh_cache():
    mdop.WIND_RESULTS.clear()
    yield
    mdop.WIND_RESULTS.clear()


def fast_wind(x, y, z):
    """Strong uniform + shear flow (radial velocities up to ~35 m/s) that folds at 12 m/s."""
    zero = 0.0 * x
    return 24.0 + 3.0 * z + zero, -12.0 + 1.5 * z + zero, zero


def fold(tree: xr.DataTree, nyquist: float, *, store: bool) -> xr.DataTree:
    """Wrap every sweep's VRADH into [-nyquist, nyquist) like a radar with that Nyquist velocity."""
    nodes = {"/": tree.root.to_dataset()}
    for key, child in tree.children.items():
        ds = child.to_dataset()
        vr = ds["VRADH"].values.astype(np.float64)
        wrapped = ((vr + nyquist) % (2 * nyquist)) - nyquist
        ds = ds.assign(VRADH=(ds["VRADH"].dims, wrapped.astype(np.float32), ds["VRADH"].attrs))
        if store:
            ds = ds.assign(nyquist_velocity=((), float(nyquist)))
        nodes[f"/{key}"] = ds
    return xr.DataTree.from_dict(nodes)


def folded_pair(nyquist: float = 12.0, *, store: bool = True):
    a = site_east_of_origin(-SEPARATION_KM / 2)
    b = site_east_of_origin(SEPARATION_KM / 2)
    return [
        (
            "fa",
            make_reader(fold(make_wind_volume(*a, fast_wind, name="WESTR"), nyquist, store=store)),
        ),
        (
            "fb",
            make_reader(fold(make_wind_volume(*b, fast_wind, name="EASTR"), nyquist, store=store)),
        ),
    ]


def rms_error(rec: mdop.WindRecord) -> float:
    zz, yy, xx = np.meshgrid(rec.z / 1e3, rec.y / 1e3, rec.x / 1e3, indexing="ij")
    ut, vt, _ = fast_wind(xx, yy, zz)
    ok = (
        np.isfinite(field(rec, "u"))
        & np.isfinite(field(rec, "v"))
        & (field(rec, "crossing_angle") >= 40)
    )
    assert ok.sum() > 100
    return float(
        np.sqrt(np.mean((field(rec, "u") - ut)[ok] ** 2 + (field(rec, "v") - vt)[ok] ** 2))
    )


# ---------------------------------------------------------------------------
# Dealiasing
# ---------------------------------------------------------------------------


@needs_radarx
class TestDealias:
    def test_auto_recovers_a_folded_wind(self) -> None:
        folded = mdop.run_multidoppler(
            folded_pair(), GRID, {**NO_FALL, "dealias": "never", "max_speed": None}, cache=None
        )
        fixed = mdop.run_multidoppler(folded_pair(), GRID, NO_FALL, cache=None)
        assert rms_error(fixed) < 1.5
        assert rms_error(folded) > 4 * rms_error(fixed)
        # what was done is reported per radar
        info = fixed.summary["dealias"]
        assert info["mode"] == "auto" and len(info["radars"]) == 2
        for r in info["radars"]:
            assert r["dealiased"] > 0 and r["nyquist_source"] == "file"
            assert r["nyquist_ms"] == [12.0, 12.0]
            assert r["jump_after_pct"] < r["jump_before_pct"]
        assert any("dealiased" in n for n in fixed.summary["notes"])
        assert fixed.radars[0]["dealias"]["dealiased"] > 0

    def test_nyquist_is_estimated_when_the_file_has_none(self) -> None:
        rec = mdop.run_multidoppler(folded_pair(store=False), GRID, NO_FALL, cache=None)
        assert rms_error(rec) < 1.5
        for r in rec.summary["dealias"]["radars"]:
            assert r["nyquist_source"] == "estimated from max |V|"
            assert 11.5 <= r["nyquist_ms"][0] <= 12.5

    def test_never_warns_that_velocities_look_aliased(self) -> None:
        rec = mdop.run_multidoppler(
            folded_pair(), GRID, {**NO_FALL, "dealias": "never", "max_speed": None}, cache=None
        )
        assert any("look aliased" in w for w in rec.warnings)
        assert all(r["dealiased"] == 0 for r in rec.summary["dealias"]["radars"])

    def test_auto_leaves_unaliased_velocities_alone(self, shear_pair) -> None:
        rec = mdop.run_multidoppler(shear_pair, GRID, NO_FALL, cache=None)
        for r in rec.summary["dealias"]["radars"]:
            assert r["dealiased"] == 0 and r["looks_aliased"] == 0
        assert not any("aliased" in w for w in rec.warnings)

    def test_always_runs_on_every_low_sweep_without_changing_clean_data(self, shear_pair) -> None:
        auto = mdop.run_multidoppler(shear_pair, GRID, NO_FALL, cache=None)
        always = mdop.run_multidoppler(
            shear_pair, GRID, {**NO_FALL, "dealias": "always"}, cache=None
        )
        assert any(r["dealiased"] > 0 for r in always.summary["dealias"]["radars"]) or any(
            r["nyquist_ms"] is None for r in always.summary["dealias"]["radars"]
        )
        both = np.isfinite(field(auto, "u")) & np.isfinite(field(always, "u"))
        diff = np.abs(field(auto, "u") - field(always, "u"))[both]
        assert float(np.median(diff)) < 0.5

    def test_the_opened_file_is_not_modified(self) -> None:
        readers = folded_pair()
        before = readers[0][1].datatree["sweep_0"].to_dataset()["VRADH"].values.copy()
        mdop.run_multidoppler(readers, GRID, NO_FALL, cache=None)
        after = readers[0][1].datatree["sweep_0"].to_dataset()["VRADH"].values
        np.testing.assert_array_equal(before, after)

    def test_option_parsing(self) -> None:
        assert mdop.Options.from_dict({}).dealias == "auto"
        assert mdop.Options.from_dict({"dealias": False}).dealias == "never"
        assert mdop.Options.from_dict({"dealias": True}).dealias == "always"
        with pytest.raises(mdop.MultiDopplerError, match="dealias"):
            mdop.Options.from_dict({"dealias": "sometimes"})
        with pytest.raises(mdop.MultiDopplerError, match="dealias_max_elevation"):
            mdop.Options.from_dict({"dealias_max_elevation": 0})


# ---------------------------------------------------------------------------
# Automatic lowest level and vertical boundary
# ---------------------------------------------------------------------------


def _fake_volume(name: str, x_km: float, min_el: float = 0.5) -> mdop._Volume:
    lat, lon = site_east_of_origin(x_km)
    return mdop._Volume(
        file_id=None,
        tree=None,
        name=name,
        latitude=lat,
        longitude=lon,
        altitude_m=300.0,
        time=np.datetime64("NaT", "ns"),
        max_range_m=230e3,
        has_dbz=True,
        n_vel_sweeps=1,
        min_elevation_deg=min_el,
    )


class TestAutoLevels:
    def _plan(self, half_sep_km: float, box_km: float, **spec_kw):
        vols = [_fake_volume("A", -half_sep_km), _fake_volume("B", half_sep_km)]
        spec = mdop.GridSpec.from_dict(
            {"dx_m": 1000, "dz_m": 500, "z_max_m": 10000, "max_range_km": 230, **spec_kw}
        )
        origin = mdop._choose_origin(vols, spec)
        xy = mdop._radar_xy(vols, origin)
        ax = np.arange(-box_km, box_km + 1, 1.0) * 1000.0
        return mdop._plan_levels(spec, vols, xy, ax, ax, 30.0), vols, xy, ax

    def test_far_radars_still_get_a_lowest_level(self) -> None:
        # radars 190 km apart watching a 200 km box centred between them: the old rule (every
        # column seen) left no levels below the top; the new one needs 5 % of the columns
        (z, note), vols, xy, ax = self._plan(95.0, 100.0)
        assert note and "chosen automatically" in note and "5 %" in note
        assert z.size > 3
        # at the chosen level >= 5 % of the columns have a usable crossing; one level lower fewer
        reach = np.full(2, 230e3)
        xx, yy = np.meshgrid(ax, ax)
        need = [
            mdop._lowest_beam_height(v, np.hypot(xx - rx, yy - ry))
            for v, (rx, ry) in zip(vols, xy, strict=True)
        ]
        _, good = mdop._dual_mask(xy, reach, ax, ax, 30.0)

        def fraction(level: float) -> float:
            return float((good & (need[0] <= level) & (need[1] <= level)).mean())

        assert fraction(float(z[0])) >= mdop.Z_MIN_COLUMN_FRACTION
        assert fraction(float(z[0]) - 500.0) < mdop.Z_MIN_COLUMN_FRACTION
        assert z[0] > 1500  # the far radars cannot see the low levels

    def test_close_radars_start_low(self) -> None:
        (z, _), *_ = self._plan(35.0, 60.0)
        assert z[0] <= 1500

    def test_manual_z_min_wins_and_has_no_note(self) -> None:
        (z, note), *_ = self._plan(95.0, 100.0, z_min_m=1000)
        assert z[0] == 1000 and note is None

    def test_impossible_geometry_says_so(self) -> None:
        vols = [_fake_volume("A", -95.0), _fake_volume("B", 95.0)]
        spec = mdop.GridSpec.from_dict({"z_max_m": 1000, "max_range_km": 230})
        origin = mdop._choose_origin(vols, spec)
        xy = mdop._radar_xy(vols, origin)
        ax = np.arange(-100, 101, 1.0) * 1000.0
        with pytest.raises(mdop.MultiDopplerError, match="At no height"):
            mdop._plan_levels(spec, vols, xy, ax, ax, 30.0)

    @needs_radarx
    def test_run_reports_the_chosen_level(self, shear_pair) -> None:
        rec = mdop.run_multidoppler(shear_pair, GRID, NO_FALL, cache=None)
        assert any("Lowest analysis level" in n for n in rec.summary["notes"])


def _gridded(vals_by_level: list[float], nz: int = 6) -> xr.Dataset:
    """Gridded VRADH ``(radar, z, y, x)`` of 2 radars; ``vals_by_level`` = share of columns seen."""
    rng = np.random.default_rng(0)
    ny = nx = 20
    data = np.full((2, nz, ny, nx), np.nan, dtype=np.float32)
    for k, share in enumerate(vals_by_level):
        seen = rng.random((ny, nx)) < share
        data[:, k][:, seen] = 1.0
    data[:, len(vals_by_level) :] = 1.0  # upper levels fully covered
    return xr.Dataset(
        {"VRADH": (("radar", "z", "y", "x"), data)},
        coords={"z": 500.0 + 500.0 * np.arange(nz), "y": np.arange(ny), "x": np.arange(nx)},
    )


class TestAutoBoundary:
    vols: ClassVar[list[mdop._Volume]] = [_fake_volume("A", -30.0), _fake_volume("B", 30.0)]

    def test_explicit_boundary_is_kept(self) -> None:
        opts = mdop.Options.from_dict({"boundary": "obrien"})
        assert mdop._resolve_boundary(opts, _gridded([1.0, 1.0]), self.vols) == ("obrien", None)

    def test_good_low_coverage_integrates_upward(self) -> None:
        opts = mdop.Options.from_dict({})
        b, note = mdop._resolve_boundary(opts, _gridded([1.0, 1.0]), self.vols)
        assert b == "upward" and "upward" in note

    def test_poor_low_coverage_integrates_downward(self) -> None:
        opts = mdop.Options.from_dict({})
        b, note = mdop._resolve_boundary(opts, _gridded([0.05, 0.1]), self.vols)
        assert b == "downward" and "downward" in note and "covered by two radars" in note

    def test_variational_avoids_a_bottom_level_far_above_the_radar(self) -> None:
        # z[0] = 500 m is 200 m above radars at 300 m: allowed; with radars at 0 m ASL it is not
        opts = mdop.Options.from_dict({"method": "variational"})
        g = _gridded([1.0, 1.0])
        assert mdop._resolve_boundary(opts, g, self.vols)[0] == "upward"
        g = g.assign_coords(z=g["z"] + 1000.0)
        b, note = mdop._resolve_boundary(opts, g, self.vols)
        assert b == "downward" and "above the highest radar" in note

    def test_boundary_warning_is_tagged(self) -> None:
        out = mdop._tag_boundary_warnings(["w_boundary='bottom' sets w = 0 at z = 500 m", "other"])
        assert out[0].startswith("Vertical boundary warning:") and out[1] == "other"

    @needs_radarx
    def test_run_resolves_auto_and_reports_it(self, shear_pair) -> None:
        rec = mdop.run_multidoppler(shear_pair, GRID, NO_FALL, cache=None)
        assert rec.summary["boundary_requested"] == "auto"
        assert rec.summary["boundary"] in ("upward", "downward")
        assert any(n.startswith("Vertical boundary:") for n in rec.summary["notes"])
        rec2 = mdop.run_multidoppler(
            shear_pair, GRID, {**NO_FALL, "boundary": "downward"}, cache=None
        )
        assert rec2.summary["boundary"] == "downward"
        assert not any(n.startswith("Vertical boundary:") for n in rec2.summary["notes"])


# ---------------------------------------------------------------------------
# Output quality control
# ---------------------------------------------------------------------------


def _fake_result(nz: int = 3, ny: int = 6, nx: int = 6) -> tuple[xr.Dataset, xr.Dataset]:
    shape = (nz, ny, nx)
    dims = ("z", "y", "x")
    u = np.full(shape, 10.0, dtype=np.float32)
    v = np.full(shape, 5.0, dtype=np.float32)
    w = np.full(shape, 1.0, dtype=np.float32)
    u[0, 0, 0] = 150.0  # 150 m/s: impossible
    u[0, 0, 1] = 120.0
    w[1, 1, 1] = 55.0  # |w| 55
    w[1, 1, 2] = -70.0
    ue = np.full(shape, 0.5, dtype=np.float32)
    ue[2, 3, 3] = 9.0  # very uncertain
    res = xr.Dataset(
        {
            "u": (dims, u),
            "v": (dims, v),
            "w": (dims, w),
            "u_error": (dims, ue),
            "v_error": (dims, np.full(shape, 0.5, dtype=np.float32)),
            "n_radars": (dims, np.full(shape, 2.0, dtype=np.float32)),
            "beam_crossing_angle": (dims, np.full(shape, 60.0, dtype=np.float32)),
        },
        coords={"z": np.arange(nz) * 500.0, "y": np.arange(ny), "x": np.arange(nx)},
    )
    g = xr.Dataset(
        {"VRADH": (("radar", *dims), np.ones((2, *shape), dtype=np.float32))},
        coords=res.coords,
    )
    return res, g


class TestQualityControl:
    x = np.arange(6) * 1000.0

    def test_counts_and_masks(self) -> None:
        res, g = _fake_result()
        opts = mdop.Options.from_dict({})
        fields, has_unc, qc, raw = mdop._assemble_fields(res, g, opts, self.x, self.x)
        assert has_unc and raw is None
        assert qc["wind_cells"] == 3 * 6 * 6
        assert qc["speed_masked"] == 2 and qc["masked_cells"] == 2
        assert qc["w_masked"] == 2 and qc["w_cells"] == 3 * 36 - 2
        assert qc["masked_pct"] == pytest.approx(100 * 2 / 108)
        assert np.isnan(fields["u"][0, 0, 0]) and np.isnan(fields["w"][0, 0, 0])
        assert np.isnan(fields["speed"][0, 0, 1])
        assert np.isnan(fields["w"][1, 1, 1]) and np.isfinite(fields["u"][1, 1, 1])
        assert np.nanmax(fields["speed"]) <= 100 and np.nanmax(np.abs(fields["w"])) <= 40
        assert any("faster than 100" in w for w in qc["warnings"])
        assert any("|w| above 40" in w for w in qc["warnings"])

    def test_derived_fields_do_not_use_masked_cells(self) -> None:
        res, g = _fake_result()
        fields, *_ = mdop._assemble_fields(res, g, mdop.Options.from_dict({}), self.x, self.x)
        # the masked 150 m/s cell would put a huge divergence on its neighbours
        assert np.nanmax(np.abs(fields["divergence"])) < 20

    def test_uncertainty_threshold(self) -> None:
        res, g = _fake_result()
        opts = mdop.Options.from_dict({"max_uncertainty": 5.0})
        fields, _, qc, _ = mdop._assemble_fields(res, g, opts, self.x, self.x)
        assert qc["uncertainty_masked"] == 1 and qc["masked_cells"] == 3
        assert np.isnan(fields["u"][2, 3, 3]) and np.isnan(fields["uncertainty"][2, 3, 3])

    def test_limits_can_be_switched_off_and_raw_kept(self) -> None:
        res, g = _fake_result()
        opts = mdop.Options.from_dict({"max_speed": None, "max_abs_w": None, "keep_raw": True})
        fields, _, qc, raw = mdop._assemble_fields(res, g, opts, self.x, self.x)
        assert qc["masked_cells"] == 0 and qc["w_masked"] == 0 and qc["warnings"] == []
        assert fields["u"][0, 0, 0] == 150.0
        assert raw is not None and raw["u"][0, 0, 0] == 150.0

    def test_raw_fields_survive_the_masking(self) -> None:
        res, g = _fake_result()
        opts = mdop.Options.from_dict({"keep_raw": True})
        fields, _, _, raw = mdop._assemble_fields(res, g, opts, self.x, self.x)
        assert np.isnan(fields["w"][1, 1, 1]) and raw["w"][1, 1, 1] == 55.0
        assert raw["u"][0, 0, 0] == 150.0 and np.isnan(fields["u"][0, 0, 0])

    def test_option_parsing(self) -> None:
        o = mdop.Options.from_dict({})
        assert (o.max_speed, o.max_abs_w, o.max_uncertainty, o.keep_raw) == (
            100.0,
            40.0,
            None,
            False,
        )
        o = mdop.Options.from_dict({"max_speed": None, "max_abs_w": 25, "max_uncertainty": 3})
        assert (o.max_speed, o.max_abs_w, o.max_uncertainty) == (None, 25.0, 3.0)
        with pytest.raises(mdop.MultiDopplerError, match="max_speed"):
            mdop.Options.from_dict({"max_speed": -5})

    @needs_radarx
    def test_run_reports_the_mask_and_keeps_raw_on_request(self, shear_pair, tmp_path) -> None:
        rec = mdop.run_multidoppler(
            shear_pair, GRID, {**NO_FALL, "max_speed": 12.0, "keep_raw": True}, cache=None
        )
        qc = rec.summary["qc"]
        assert qc["max_speed"] == 12.0 and qc["speed_masked"] > 0 and qc["masked_pct"] > 0
        assert np.nanmax(field(rec, "speed")) <= 12.0
        assert any("Quality control" in w for w in rec.warnings)
        assert rec.raw is not None and np.nanmax(np.hypot(rec.raw["u"], rec.raw["v"])) > 12.0
        out = tmp_path / "wind.nc"
        mdop.write_netcdf(rec, out)
        with xr.open_dataset(out) as ds:
            assert {"u_raw", "v_raw", "w_raw"} <= set(ds.data_vars)
        # default: no raw copy
        plain = mdop.run_multidoppler(shear_pair, GRID, NO_FALL, cache=None)
        assert plain.raw is None and plain.summary["qc"]["raw_kept"] is False

    @needs_radarx
    def test_everything_masked_is_explained(self, shear_pair) -> None:
        with pytest.raises(mdop.MultiDopplerError, match="Quality control removed every"):
            mdop.run_multidoppler(shear_pair, GRID, {**NO_FALL, "max_speed": 1.0}, cache=None)


# ---------------------------------------------------------------------------
# Per-level coverage
# ---------------------------------------------------------------------------


@needs_radarx
def test_level_coverage_table(shear_pair) -> None:
    rec = mdop.run_multidoppler(shear_pair, GRID, NO_FALL, cache=None)
    table = rec.summary["level_coverage"]
    assert [r["z_m"] for r in table] == [float(z) for z in rec.z]
    cells = rec.fields.shape[2] * rec.fields.shape[3]
    for k, row in enumerate(table):
        assert 0 <= row["w_pct"] <= row["wind_pct"] <= row["dual_pct"] <= 100
        wind = np.isfinite(field(rec, "u")[k]) & np.isfinite(field(rec, "v")[k])
        assert row["wind_pct"] == pytest.approx(100.0 * wind.sum() / cells)
    assert max(r["wind_pct"] for r in table) > 5
    # the table travels with the header sent to the UI
    assert rec.header()["level_coverage"] == table
    assert "qc" in rec.header() and "notes" in rec.header()


def test_level_coverage_function() -> None:
    n = np.zeros((2, 2, 2), dtype=np.float32)
    n[0] = [[2, 2], [1, 0]]
    n[1] = [[2, 2], [2, 2]]
    u = np.where(n >= 2, 1.0, np.nan).astype(np.float32)
    w = u.copy()
    w[1] = np.nan
    table = mdop._level_coverage({"u": u, "v": u, "w": w, "n_radars": n}, np.array([500.0, 1000.0]))
    assert table[0] == {"z_m": 500.0, "dual_pct": 50.0, "wind_pct": 50.0, "w_pct": 50.0}
    assert table[1] == {"z_m": 1000.0, "dual_pct": 100.0, "wind_pct": 100.0, "w_pct": 0.0}


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def shear_pair():
    a = site_east_of_origin(-SEPARATION_KM / 2)
    b = site_east_of_origin(SEPARATION_KM / 2)
    from tests.test_multidoppler import shear_wind

    return [
        ("fa", make_reader(make_wind_volume(*a, shear_wind, name="WESTR", fall_speed=None))),
        ("fb", make_reader(make_wind_volume(*b, shear_wind, name="EASTR", fall_speed=None))),
    ]


def _quivers(fig):
    from matplotlib.quiver import Quiver

    return [c for ax in fig.axes for c in ax.collections if isinstance(c, Quiver)]


def _lum(rgba) -> float:
    r, g, b = rgba[:3]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


@needs_radarx
class TestWindFigure:
    @pytest.fixture(scope="class")
    def record(self, shear_pair):
        return mdop.run_multidoppler(shear_pair, GRID, NO_FALL, cache=None)

    @pytest.mark.parametrize("theme", ["dark", "light"])
    def test_arrows_contrast_with_the_background_and_have_opposite_edges(
        self, record, theme
    ) -> None:
        from engine import windfig

        fig = windfig.plot_wind(record, 3.0, theme=theme)
        quivers = _quivers(fig)
        assert quivers, "no wind arrows were drawn"
        for q in quivers:
            fill = q.get_facecolor()[0]
            edge = q.get_edgecolor()[0]
            assert q.pivot == "middle"  # pivot="mid"
            assert 0.35 <= q.get_linewidth()[0] <= 0.7
            if theme == "dark":
                assert _lum(fill) > 0.9 and _lum(edge) < 0.2  # white arrow, dark edge
            else:
                assert _lum(fill) < 0.2 and _lum(edge) > 0.9  # dark arrow, light edge
        assert windfig.arrow_colors("dark") != windfig.arrow_colors("light")

    def test_diagnostic_has_six_panels_and_publication_three(self, record, tmp_path) -> None:
        from engine import windfig

        diag = windfig.plot_wind(record, 3.0, tmp_path / "d.png")
        pub = windfig.plot_wind(record, 3.0, tmp_path / "p.png", theme="light", kind="publication")
        content = lambda fig: [a for a in fig.axes if a.get_visible() and a.get_title(loc="left")]  # noqa: E731
        assert len(content(diag)) == 6 and len(content(pub)) == 3
        assert (tmp_path / "d.png").stat().st_size > 5000
        assert (tmp_path / "p.png").stat().st_size > 5000

    def test_reflectivity_uses_a_cmweather_colormap(self, record) -> None:
        from engine import windfig

        fig = windfig.plot_wind(record, 3.0)
        names = {im.get_cmap().name for ax in fig.axes for im in ax.images}
        assert "ChaseSpectral" in names

    def test_dataset_input_and_errors(self, record) -> None:
        from engine import windfig

        ds = mdop.to_dataset(record)
        assert [n for n, _, _ in windfig._radar_xy(ds)] == ["WESTR", "EASTR"]
        fig = windfig.plot_wind(ds, 2.0, kind="publication")
        assert fig is not None
        with pytest.raises(ValueError, match="theme"):
            windfig.plot_wind(ds, 2.0, theme="blue")
        with pytest.raises(ValueError, match="unknown panel"):
            windfig.plot_wind(ds, 2.0, panels=["nope"])
        with pytest.raises(TypeError):
            windfig.plot_wind(42, 2.0)


# ---------------------------------------------------------------------------
# Real NEXRAD cases (skipped when the files are not on this machine)
# ---------------------------------------------------------------------------

CASES = Path(os.environ.get("XRADAR_SEVERE_CASES", "/Users/syed44/Downloads/SEVERE_CASES_TEST"))
MAYFIELD = [
    "mayfield/KHPX/KHPX20211211_020457_V06",
    "mayfield/KNQA/KNQA20211211_020148_V06",
    "mayfield/KPAH/KPAH20211211_020547_V06",
    "mayfield/KVWX/KVWX20211211_020418_V06",
]
ELRENO = [
    "elreno/KFDR/KFDR20130531_231524_V06",
    "elreno/KTLX/KTLX20130531_231434_V06",
    "elreno/KVNX/KVNX20130531_231437_V06",
]


def _open_case(rel: list[str]):
    from engine.reader import RadarReader

    readers = []
    for r in rel:
        reader = RadarReader()
        reader.open_file(str(CASES / r))
        readers.append((Path(r).name[:4], reader))
    return readers


@needs_radarx
@pytest.mark.parametrize(
    ("rel", "origin"),
    [(MAYFIELD, (36.74, -88.64)), (ELRENO, (35.55, -98.0))],
    ids=["mayfield", "elreno"],
)
def test_real_case_defaults_give_a_sensible_wind(rel, origin) -> None:
    if not all((CASES / r).exists() for r in rel):
        pytest.skip("the severe-case NEXRAD files are not on this machine")
    grid = {
        "origin": {"lat": origin[0], "lon": origin[1]},
        "extent": {"x_min_km": -100, "x_max_km": 100, "y_min_km": -100, "y_max_km": 100},
        "dx_m": 1000,
        "dz_m": 500,
        "z_max_m": 9000,
        "max_range_km": 230,
    }
    rec = mdop.run_multidoppler(_open_case(rel), grid, {}, cache=None)
    s = rec.summary
    assert s["dealias"]["mode"] == "auto"
    assert all(r["dealiased"] > 0 for r in s["dealias"]["radars"] if r["sweeps"] > 4)
    assert s["field_stats"]["speed"]["max"] <= 100.0
    assert s["field_stats"]["w"]["max"] <= 40.0 and s["field_stats"]["w"]["min"] >= -40.0
    assert 5 < s["field_stats"]["speed"]["mean"] < 60  # was up to 106 m/s with aliased input
    assert s["boundary_requested"] == "auto" and s["boundary"] in ("upward", "downward")
    assert len(s["level_coverage"]) == rec.z.size
    assert max(r["dual_pct"] for r in s["level_coverage"]) > 5
    assert s["coverage_pct"] > 5


@needs_radarx
def test_a_sweep_off_by_a_nyquist_interval_is_shifted_back() -> None:
    readers = folded_pair()
    vols = [mdop._prepare_volume(f, r, i, dealias="auto") for i, (f, r) in enumerate(readers)]
    spec = mdop.GridSpec.from_dict(GRID)
    z = np.arange(500.0, 8001.0, 500.0)
    assert mdop._check_absolute_folds(vols, z, spec) == []  # a clean unfolding stays as it is
    key = sorted(vols[0].tree.children)[0]
    node = vols[0].tree[key]
    ds = node.to_dataset()
    good = ds["VRADH"].values.copy()
    node.dataset = ds.assign(VRADH=(ds["VRADH"].dims, good + 24.0, ds["VRADH"].attrs))
    notes = mdop._check_absolute_folds(vols, z, spec)
    assert notes and "shifted" in notes[0]
    np.testing.assert_allclose(vols[0].tree[key].to_dataset()["VRADH"].values, good, atol=1e-4)
    assert vols[0].dealias["refolded"] == 1
