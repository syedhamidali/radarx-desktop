"""The always-on fold check of the velocity dealiasing (issue #152), against known answers.

Known answers: a uniform wind seen by a radar is a first harmonic of azimuth on every range ring
(Browning and Wexler 1968), so its ring means are zero and a wrong whole-sweep interval shows up
as a ring mean of exactly ``k x 2 x Vn``. A Rankine vortex has a known velocity everywhere and a
mean that is small on any ring that does not cut through its core.
"""

from __future__ import annotations

import importlib.util
import warnings

import numpy as np
import pytest
import xarray as xr

from engine import pipeline, plugins, qc
from engine.dealias_reference import fold_check

from . import radar_data

HAS_RADARX = importlib.util.find_spec("radarx") is not None
needs_radarx = pytest.mark.skipif(not HAS_RADARX, reason="radarx not installed")

AZ = np.arange(360.0) + 0.5
RNG = 250.0 * np.arange(1, 201)  # 0.25 .. 50 km: five 10 km rings
NYQ = 10.0


def fold(v: np.ndarray, nyq: float = NYQ) -> np.ndarray:
    return (v + nyq) % (2.0 * nyq) - nyq


def uniform_wind(speed: float, from_deg: float, az: np.ndarray = AZ) -> np.ndarray:
    a = np.radians(az)[:, None]
    return -speed * np.cos(a - np.radians(from_deg)) + np.zeros((1, RNG.size))


def rankine(
    v_max: float, r_core_m: float, x0: float, y0: float, background: np.ndarray
) -> np.ndarray:
    """Radial velocity of a Rankine vortex (core radius, peak tangential speed) at (x0, y0) metres
    from the radar, added to a background radial-velocity field. Counter-clockwise rotation."""
    a = np.radians(AZ)[:, None]
    x = RNG[None, :] * np.sin(a)  # east
    y = RNG[None, :] * np.cos(a)  # north
    dx, dy = x - x0, y - y0
    r = np.hypot(dx, dy)
    vt = np.where(r < r_core_m, v_max * r / r_core_m, v_max * r_core_m / np.maximum(r, 1e-9))
    # counter-clockwise tangential flow (-dy, dx) / r, projected on the beam
    u = -vt * dy / np.maximum(r, 1e-9)
    v = vt * dx / np.maximum(r, 1e-9)
    return background + u * np.sin(a) + v * np.cos(a)


def as_da(values: np.ndarray, az: np.ndarray = AZ) -> xr.DataArray:
    return xr.DataArray(
        values, dims=("azimuth", "range"), coords={"azimuth": az, "range": RNG[: values.shape[1]]}
    )


# ---------------------------------------------------------------------------
# The kernel
# ---------------------------------------------------------------------------


class TestKernel:
    def test_truth_passes(self) -> None:
        c = fold_check(uniform_wind(25.0, 70.0), AZ, NYQ, RNG)
        assert c["status"] == "consistent" and c["k"] == 0 and c["rings"] == 6
        assert c["off_share"] == 0.0

    @pytest.mark.parametrize("k", [1, -1, 2])
    def test_whole_sweep_offset_is_found_with_its_multiple(self, k: int) -> None:
        """Truth shifted by ``-k`` intervals: the check says add ``k x 2 x Vn``."""
        c = fold_check(uniform_wind(25.0, 70.0) - k * 2 * NYQ, AZ, NYQ, RNG)
        assert c["status"] == "whole_field_offset"
        assert c["k"] == k and c["shift_ms"] == pytest.approx(k * 2 * NYQ)
        assert "FAILED" in c["message"]

    def test_offset_with_noise(self) -> None:
        rng = np.random.default_rng(3)
        noisy = uniform_wind(25.0, 250.0) + rng.normal(0.0, 2.0, (AZ.size, RNG.size))
        assert fold_check(noisy, AZ, NYQ, RNG)["status"] == "consistent"
        c = fold_check(noisy + 2 * NYQ, AZ, NYQ, RNG)
        assert c["status"] == "whole_field_offset" and c["k"] == -1

    def test_offset_after_correcting_passes(self) -> None:
        c = fold_check(uniform_wind(25.0, 70.0) + 20.0, AZ, NYQ, RNG)
        fixed = uniform_wind(25.0, 70.0) + 20.0 + c["shift_ms"]
        assert fold_check(fixed, AZ, NYQ, RNG)["status"] == "consistent"

    def test_parts_in_different_intervals_are_inconsistent(self) -> None:
        """Outer rings one interval up, middle rings one down: no single shift can fix it."""
        v = uniform_wind(25.0, 70.0)
        v[:, 80:140] += 2 * NYQ
        v[:, 140:] -= 2 * NYQ
        c = fold_check(v, AZ, NYQ, RNG)
        assert c["status"] == "inconsistent" and c["k"] == 0
        assert 0.4 < c["off_share"] < 0.7

    def test_only_part_of_the_rings_off_is_inconsistent_not_whole_field(self) -> None:
        v = uniform_wind(25.0, 70.0)
        v[:, 100:] += 2 * NYQ  # outer half of the range only
        c = fold_check(v, AZ, NYQ, RNG)
        assert c["status"] == "inconsistent"

    def test_a_minority_of_wrong_gates_is_not_a_whole_sweep_offset(self) -> None:
        """Real radarx output on KHPX had a quarter of its gates unfolded one interval too high:
        their mean moves the rings by more than Vn, but the typical gate is right and shifting the
        sweep would make the other three quarters wrong. The check must not call it a whole-sweep
        offset (it uses the median of each sector)."""
        rng = np.random.default_rng(5)
        v = uniform_wind(25.0, 70.0)
        wrong = rng.random(v.shape) < 0.25
        c = fold_check(np.where(wrong, v + 2 * NYQ, v), AZ, NYQ, RNG)
        assert c["status"] == "consistent" and c["k"] == 0

    def test_rings_that_cover_less_than_half_the_gates_cannot_claim_a_whole_sweep(self) -> None:
        """Radarx output on KHPX had complete rings only at close range (11-20 % of the gates);
        a wrong interval there is reported, but a shift of the whole sweep is not suggested."""
        v = uniform_wind(25.0, 70.0) - 20.0
        v[150:, 39:] = np.nan  # beyond 10 km only 150 degrees of echo: those rings cannot be tested
        c = fold_check(v, AZ, NYQ, RNG)
        assert c["status"] == "inconsistent" and c["k"] == 0 and c["shift_ms"] == 0.0
        assert c["usable_share"] < 0.5

    def test_small_affected_share_passes(self) -> None:
        v = uniform_wind(25.0, 70.0)
        v[:, 190:] += 2 * NYQ  # the outermost ring only (~5 % of the gates)
        assert fold_check(v, AZ, NYQ, RNG)["status"] == "consistent"

    def test_sector_scan_is_undetermined(self) -> None:
        az = np.arange(30.0, 120.0) + 0.5
        c = fold_check(uniform_wind(25.0, 70.0, az) + 20.0, az, NYQ, RNG)
        assert c["status"] == "undetermined" and c["k"] == 0

    def test_vortex_far_from_the_ring_centres_passes(self) -> None:
        """A strong compact vortex (peak 30 m/s, 1 km core, 20 km from the radar, so Vn 10 is
        exceeded; background wind 8 m/s) does not make a correct field look off: only a few gates
        of its ring carry it."""
        truth = rankine(30.0, 1000.0, 14000.0, 14000.0, uniform_wind(8.0, 200.0))
        assert fold_check(truth, AZ, NYQ, RNG)["status"] == "consistent"

    def test_nan_gates_and_bad_input(self) -> None:
        v = uniform_wind(25.0, 70.0)
        v[:, ::2] = np.nan
        assert fold_check(v, AZ, NYQ, RNG)["status"] == "consistent"
        assert fold_check(v, AZ, -1.0, RNG)["status"] == "undetermined"
        assert fold_check(np.full_like(v, np.nan), AZ, NYQ, RNG)["status"] == "undetermined"
        assert fold_check(v[0], AZ, NYQ, RNG)["status"] == "undetermined"

    def test_without_ranges(self) -> None:
        c = fold_check(uniform_wind(25.0, 70.0) + 20.0, AZ, NYQ)
        assert c["status"] == "whole_field_offset"


# ---------------------------------------------------------------------------
# The QC function: always on, warns, records, never changes the data
# ---------------------------------------------------------------------------


class TestQcAlwaysOn:
    def test_issue_152_case_warns_and_records(self) -> None:
        truth = uniform_wind(25.0, 70.0)
        with pytest.warns(RuntimeWarning, match="fold check FAILED"):
            out = qc.velocity_dealiasing(as_da(fold(truth)), NYQ, engine="builtin")
        assert out.attrs["qc_fold_check"].startswith("whole_field_offset")
        # the check reports, it does not change the data: still off by exactly one interval
        assert np.allclose(np.abs(out.values - truth), 2 * NYQ)

    @pytest.mark.parametrize("reference", ["zero", "vad"])
    def test_with_a_reference_the_check_passes_silently(self, reference: str) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            out = qc.velocity_dealiasing(
                as_da(fold(uniform_wind(25.0, 70.0))), NYQ, reference=reference
            )
        assert out.attrs["qc_fold_check"].startswith("consistent")

    def test_vortex_in_wind(self) -> None:
        truth = rankine(30.0, 1000.0, 14000.0, 14000.0, uniform_wind(8.0, 200.0))
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            out = qc.velocity_dealiasing(as_da(fold(truth)), NYQ)
        assert out.attrs["qc_fold_check"].startswith("consistent")
        assert np.mean(np.abs(out.values - truth) < 1e-6) > 0.99  # the vortex is unfolded too

    def test_sector_scan_does_not_warn(self) -> None:
        az = np.arange(30.0, 120.0) + 0.5
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            out = qc.velocity_dealiasing(as_da(fold(uniform_wind(25.0, 70.0, az)), az), NYQ)
        assert out.attrs["qc_fold_check"].startswith("undetermined")


# ---------------------------------------------------------------------------
# The pipeline step: notes and metrics in the run record
# ---------------------------------------------------------------------------


def _tree(vel: np.ndarray, nyq: float) -> xr.DataTree:
    ds = xr.Dataset(
        {"VRADH": (("azimuth", "range"), vel.astype(np.float32), {"units": "m/s"})},
        coords={
            "azimuth": AZ,
            "range": RNG,
            "elevation": ("azimuth", np.full(AZ.size, 0.5, dtype=np.float32)),
        },
    )
    ds["sweep_fixed_angle"] = ((), 0.5)
    ds["nyquist_velocity"] = ((), nyq)
    root = xr.Dataset(coords={"latitude": 35.0, "longitude": -97.0, "altitude": 300.0})
    return xr.DataTree.from_dict({"/": root, "/sweep_0": ds})


def _run(tree: xr.DataTree, **params):
    rec = pipeline.parse_recipe(
        {"name": "t", "target": "VRADH", "steps": [{"step_id": "dealias", "params": params}]}
    )
    ctx = plugins.ModuleContext(tree, 0, get_sweep_dataset=lambda i: tree["sweep_0"].to_dataset())
    return pipeline.run_recipe_on_sweep(rec, ctx)


class TestPipelineStep:
    def test_correct_unfolding_is_recorded_as_passed(self) -> None:
        truth = uniform_wind(25.0, 70.0)
        out = _run(_tree(fold(truth), NYQ), method="builtin")
        step = out.steps[0]
        assert any(n.startswith("fold check passed") for n in step["notes"])
        assert step["metrics"]["fold_check"]["status"] == "consistent"

    def test_offset_is_recorded_and_warned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unfolding that comes out one interval off (forced here) is reported in the notes and
        the metrics of the run record, and warned about."""
        from engine import pipeline_steps

        truth = uniform_wind(25.0, 70.0)
        monkeypatch.setattr(pipeline_steps, "unfold_velocity", lambda vel, nyq, wrap: truth - 20.0)
        with pytest.warns(RuntimeWarning, match="fold check FAILED"):
            out = _run(_tree(fold(truth), NYQ), method="builtin")
        step = out.steps[0]
        assert any(n.startswith("fold check FAILED") for n in step["notes"])
        assert step["metrics"]["fold_check"]["status"] == "whole_field_offset"
        assert step["metrics"]["fold_check"]["k"] == 1

    def test_exported_script_carries_the_check(self) -> None:
        rec = pipeline.parse_recipe(
            {
                "name": "t",
                "target": "VRADH",
                "steps": [{"step_id": "dealias", "params": {"method": "builtin"}}],
            }
        )
        src = pipeline.recipe_to_script(rec)
        assert "def fold_check" in src
        ns: dict = {"__name__": "recipe_script"}
        exec(compile(src, "<recipe_script>", "exec"), ns)
        ns["rt"] = None
        tree = _tree(fold(uniform_wind(25.0, 70.0)), NYQ)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            got = ns["process_sweep"](tree["sweep_0"].to_dataset()).values
        np.testing.assert_allclose(got, uniform_wind(25.0, 70.0), atol=1e-4)


# ---------------------------------------------------------------------------
# Real volumes
# ---------------------------------------------------------------------------


def _real_sweep(path, name: str):
    """(velocity with no-echo codes masked, azimuth, range, Nyquist estimated like the engine)."""
    import xradar as xd

    from engine.reader import mask_variable

    ds = xd.io.open_nexradlevel2_datatree(str(path), sweep=[int(name.split("_")[1])])[
        name
    ].to_dataset()
    vel = mask_variable(ds["VRADH"]).values.astype(np.float64)
    nyq = float(np.ceil(np.nanmax(np.abs(vel)) * 2.0) / 2.0)
    return vel, ds["azimuth"].values, ds["range"].values, nyq


def _real_cases():
    cases = []
    # (label, file, sweep, verdict for a sweep moved by one interval): the KNQA echo is patchy, only
    # 13 % of its gates sit in complete rings, so a planted offset is reported as inconsistent
    # (rings off) but not as a whole-sweep shift; KILX has complete rings
    for label, path, name, verdict in (
        (
            "KNQA 2021-12-11 03:01",
            radar_data.event_volume("KNQA", "030113"),
            "sweep_1",
            "inconsistent",
        ),
        ("KILX 2023-06-29", radar_data.get(radar_data.KILX), "sweep_1", "whole_field_offset"),
    ):
        if path is not None:
            cases.append(pytest.param(path, name, verdict, id=label))
    if not cases:
        cases.append(pytest.param(None, None, None, id="no real volume available"))
    return cases


@pytest.mark.parametrize(("path", "name", "verdict"), _real_cases())
def test_real_sweep_region_unfolding_passes_and_a_planted_offset_is_found(
    path, name, verdict
) -> None:
    """Real NEXRAD velocities: the region unfolding of the QC panel passes the check; the same
    field moved by one 2 x Vn interval (the failure of issue 152) is flagged, and where the rings
    cover most of the sweep, with the multiple that undoes it (the check passes again after it)."""
    if path is None:
        pytest.skip("no real NEXRAD volume available (see tests/radar_data.py)")
    vel, az, rng, nyq = _real_sweep(path, name)
    da = xr.DataArray(vel, dims=("azimuth", "range"), coords={"azimuth": az, "range": rng})
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        out = qc.velocity_dealiasing(da, nyq, engine="builtin").values
    assert fold_check(out, az, nyq, rng)["status"] == "consistent"
    planted = fold_check(out + 2.0 * nyq, az, nyq, rng)
    assert planted["status"] == verdict
    if verdict == "whole_field_offset":
        assert planted["k"] == -1
        assert (
            fold_check(out + 2.0 * nyq + planted["shift_ms"], az, nyq, rng)["status"]
            == "consistent"
        )
    else:
        assert planted["k"] == 0 and planted["usable_share"] < 0.5


@needs_radarx
def test_real_case_radarx_region_sweep_with_a_wrong_interval_is_flagged() -> None:
    """KNQA 2021-12-11 03:01 UTC, 0.5 degree velocity cut (Vn 34 m/s): radarx 0.5.1 'region'
    unfolds the largest group (174 degrees of azimuth) to the wrong interval; the mean along-beam
    wind of the 0-40 km rings is about -130 m/s. Measured, not a statement about radarx at large:
    the check must flag this result."""
    import radarx

    path = radar_data.event_volume("KNQA", "030113")
    if path is None or not radarx.__version__.startswith("0.5."):
        pytest.skip("needs the tutorial KNQA volume and radarx 0.5.x")
    vel, az, rng, nyq = _real_sweep(path, "sweep_1")
    ds = xr.Dataset({"VRADH": (("azimuth", "range"), vel)}, coords={"azimuth": az, "range": rng})
    ds["elevation"] = ("azimuth", np.full(az.size, 0.48))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        out = radarx.retrieve.dealias_velocity(ds, "VRADH", nyq, method="region", threshold=0.3)
    check = fold_check(out.transpose("azimuth", "range").values, az, nyq, rng)
    assert check["status"] in ("inconsistent", "whole_field_offset")
    assert check["off_share"] > 0.5


# ---------------------------------------------------------------------------
# The unfolding engine: radarx when installed, the built-in method otherwise
# ---------------------------------------------------------------------------


class TestEngine:
    @needs_radarx
    def test_radarx_gets_the_issue_152_case_right(self) -> None:
        """25 m/s with Vn 10 over a full circle: radarx anchors the absolute fold at zero mean and
        returns the truth, where the built-in method is a whole interval off."""
        truth = uniform_wind(25.0, 70.0)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            out = qc.velocity_dealiasing(as_da(fold(truth)), NYQ)  # engine auto
        np.testing.assert_allclose(out.values, truth, atol=1e-9)
        assert "radarx region method" in out.attrs["qc_dealiased"]
        assert out.attrs["qc_fold_check"].startswith("consistent")

    @needs_radarx
    def test_engine_radarx_and_builtin_are_selectable(self) -> None:
        da = as_da(fold(uniform_wind(8.0, 200.0)))
        a = qc.velocity_dealiasing(da, NYQ, engine="radarx")
        b = qc.velocity_dealiasing(da, NYQ, engine="builtin")
        assert "radarx" in a.attrs["qc_dealiased"] and "built-in" in b.attrs["qc_dealiased"]

    def test_without_radarx_auto_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        monkeypatch.setitem(sys.modules, "radarx", None)
        monkeypatch.setitem(sys.modules, "radarx.retrieve", None)
        out = qc.velocity_dealiasing(as_da(fold(uniform_wind(8.0, 200.0))), NYQ)
        assert "built-in" in out.attrs["qc_dealiased"]
        with pytest.raises(ValueError, match="needs radarx"):
            qc.velocity_dealiasing(as_da(fold(uniform_wind(8.0, 200.0))), NYQ, engine="radarx")

    def test_bad_engine(self) -> None:
        with pytest.raises(ValueError, match="engine must be"):
            qc.velocity_dealiasing(as_da(np.zeros((360, 5))), NYQ, engine="magic")

    def test_registry_offers_the_option(self) -> None:
        p = qc.QC_REGISTRY["velocity_dealiasing"]["params"]["engine"]
        assert p["default"] == "auto" and p["options"] == ["auto", "builtin", "radarx"]


class TestImplausibleGuard:
    """A few gates with an extreme fold (radarx issue 350) never reach the result."""

    def test_kernel(self) -> None:
        from engine.dealias_reference import implausible_velocity

        v = np.array([0.0, 149.0, -151.0, 523294.5, np.nan, 160.0])
        assert implausible_velocity(v, 10.0).tolist() == [False, False, True, True, False, True]
        assert implausible_velocity(np.array([160.0]), 40.0).tolist() == [False]  # 5 Vn = 200

    def test_extreme_fold_from_the_engine_is_removed_counted_and_warned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        truth = uniform_wind(8.0, 200.0)
        bad = truth.copy()
        bad[100, 50:54] += 9873 * 2 * NYQ  # four gates, as on the real KVWX sweep

        monkeypatch.setattr(qc, "_radarx_unfold", lambda data, nyq: bad.copy())
        with pytest.warns(RuntimeWarning, match="4 gate"):
            out = qc.velocity_dealiasing(as_da(fold(truth)), NYQ)
        assert np.isnan(out.values[100, 50:54]).all()
        assert out.attrs["qc_implausible_gates"] == 4
        assert "set to missing" in out.attrs["qc_dealiased"]
        rest = np.isfinite(out.values)
        np.testing.assert_allclose(out.values[rest], truth[rest], atol=1e-9)

    def test_clean_result_has_no_flag(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            out = qc.velocity_dealiasing(as_da(fold(uniform_wind(8.0, 200.0))), NYQ)
        assert out.attrs["qc_implausible_gates"] == 0

    def test_pipeline_step_removes_the_gates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine import pipeline_steps

        truth = uniform_wind(8.0, 200.0)
        bad = truth.copy()
        bad[100, 50:54] += 9873 * 2 * NYQ
        monkeypatch.setattr(pipeline_steps, "unfold_velocity", lambda vel, nyq, wrap: bad.copy())
        with pytest.warns(RuntimeWarning, match="beyond a plausible velocity"):
            out = _run(_tree(fold(truth), NYQ), method="builtin")
        assert np.isnan(out.after[100, 50:54]).all()
        assert any("beyond a plausible velocity" in n for n in out.steps[0]["notes"])

    @needs_radarx
    def test_real_kvwx_sweep_with_radarx_gives_no_absurd_velocity(self) -> None:
        """KVWX 2021-12-11 02:56:51, sweep_1 (Vn 26.5): radarx 0.5.1 gives four gates a fold of
        9873 (523 294 m/s). With the default engine none of that reaches the result."""
        path = radar_data.event_volume("KVWX", "025651")
        if path is None:
            pytest.skip("the tutorial KVWX volume is not available (tests/radar_data.py)")
        vel, az, rng, nyq = _real_sweep(path, "sweep_1")
        da = xr.DataArray(vel, dims=("azimuth", "range"), coords={"azimuth": az, "range": rng})
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            out = qc.velocity_dealiasing(da, nyq)
        assert float(np.nanmax(np.abs(out.values))) <= max(150.0, 5 * nyq)
        if "radarx" in out.attrs["qc_dealiased"] and out.attrs["qc_implausible_gates"]:
            assert (
                out.attrs["qc_implausible_gates"] == 4
            )  # radarx 0.5.1: the four gates of issue 350
