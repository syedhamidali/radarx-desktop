"""Opt-in fold reference of the velocity dealiasing (issue #152), against known answers.

Known answer: a wind that is uniform at each range ring gives a radial velocity that is a first
harmonic of azimuth (Browning and Wexler 1968), so the truth is known exactly at every gate and
its mean over a full circle is zero.
"""

from __future__ import annotations

import importlib.util
from typing import Any

import numpy as np
import pytest
import xarray as xr

from engine import pipeline, plugins, qc
from engine.dealias_reference import fold_reference_shift

HAS_RADARX = importlib.util.find_spec("radarx") is not None
needs_radarx = pytest.mark.skipif(not HAS_RADARX, reason="radarx not installed")

AZ = np.arange(360.0) + 0.5
RNG = 250.0 * np.arange(1, 201)


def fold(v: np.ndarray, nyq: float) -> np.ndarray:
    return (v + nyq) % (2.0 * nyq) - nyq


def uniform_wind(speed: float, from_deg: float, az: np.ndarray = AZ) -> np.ndarray:
    """Radial velocity (positive away) of a uniform wind blowing *from* ``from_deg``."""
    a = np.radians(az)[:, None]
    return -speed * np.cos(a - np.radians(from_deg)) + np.zeros((1, RNG.size))


def sheared_wind(seed: int = 1) -> np.ndarray:
    """Speed 5 -> 25 m/s and direction veering 25 deg from near to far range, plus 0.5 m/s noise."""
    a = np.radians(AZ)[:, None]
    km = RNG[None, :] / 1000.0
    speed = 5.0 + 0.4 * km
    to_dir = np.radians(200.0 + 0.5 * km)
    noise = np.random.default_rng(seed).normal(0.0, 0.5, (AZ.size, RNG.size))
    return speed * np.cos(a - to_dir) + noise


def as_da(values: np.ndarray, az: np.ndarray = AZ) -> xr.DataArray:
    return xr.DataArray(
        values, dims=("azimuth", "range"), coords={"azimuth": az, "range": RNG[: values.shape[1]]}
    )


# ---------------------------------------------------------------------------
# qc.velocity_dealiasing
# ---------------------------------------------------------------------------


class TestQcReference:
    NYQ = 10.0

    def test_issue_152_case_is_off_without_reference(self) -> None:
        """Documented limit of the default: 25 m/s with Vn 10 is off by one interval (20 m/s)."""
        truth = uniform_wind(25.0, 70.0)
        out = qc.velocity_dealiasing(as_da(fold(truth, self.NYQ)), self.NYQ).values
        err = out - truth
        assert np.allclose(err, err.flat[0], atol=1e-9)  # one uniform offset ...
        assert abs(err.flat[0]) == pytest.approx(2 * self.NYQ)  # ... of 2 x Nyquist

    def test_default_is_unchanged(self) -> None:
        da = as_da(fold(uniform_wind(25.0, 70.0), self.NYQ))
        a = qc.velocity_dealiasing(da, self.NYQ).values
        b = qc.velocity_dealiasing(da, self.NYQ, reference="none").values
        assert np.array_equal(a, b)

    @pytest.mark.parametrize("reference", ["zero", "vad"])
    @pytest.mark.parametrize("from_deg", [0.0, 70.0, 250.0])
    def test_reference_recovers_the_truth(self, reference: str, from_deg: float) -> None:
        truth = uniform_wind(25.0, from_deg)
        out = qc.velocity_dealiasing(as_da(fold(truth, self.NYQ)), self.NYQ, reference=reference)
        np.testing.assert_allclose(out.values, truth, atol=1e-9)
        assert f"reference {reference}" in out.attrs["qc_dealiased"]

    @pytest.mark.parametrize("reference", ["none", "zero", "vad"])
    def test_sheared_full_circle(self, reference: str) -> None:
        """A realistic profile (speed and direction change with range) stays right."""
        truth = sheared_wind()
        out = qc.velocity_dealiasing(as_da(fold(truth, 8.0)), 8.0, reference=reference).values
        assert np.mean(np.abs(out - truth) < 1e-6) > 0.99

    def test_sector_scan(self) -> None:
        """A 90-degree sector: the zero-mean assumption fails and the VAD is undefined: refused."""
        az = np.arange(30.0, 120.0) + 0.5
        da = as_da(fold(uniform_wind(25.0, 70.0, az), self.NYQ), az)
        for reference in ("zero", "vad"):
            with pytest.raises(ValueError, match=f"reference '{reference}'"):
                qc.velocity_dealiasing(da, self.NYQ, reference=reference)

    def test_vad_allows_a_gap_zero_does_not(self) -> None:
        """Echo on 270 degrees only: the zero mean is refused, the VAD fit still finds the fold."""
        truth = uniform_wind(25.0, 70.0)
        obs = fold(truth, self.NYQ)
        obs[90:180] = np.nan
        with pytest.raises(ValueError, match="reference 'zero'"):
            qc.velocity_dealiasing(as_da(obs), self.NYQ, reference="zero")
        out = qc.velocity_dealiasing(as_da(obs), self.NYQ, reference="vad").values
        ok = np.isfinite(obs)
        np.testing.assert_allclose(out[ok], truth[ok], atol=1e-9)

    def test_bad_reference(self) -> None:
        with pytest.raises(ValueError, match="reference must be"):
            qc.velocity_dealiasing(as_da(np.zeros((360, 5))), 10.0, reference="sounding")

    def test_registry_offers_the_option(self) -> None:
        p = qc.QC_REGISTRY["velocity_dealiasing"]["params"]["reference"]
        assert p["default"] == "none" and p["options"] == ["none", "zero", "vad"]


def test_kernel_shift_is_a_whole_interval() -> None:
    truth = uniform_wind(25.0, 70.0)
    k, note = fold_reference_shift(truth - 40.0, AZ, 10.0, "zero")
    assert k == 2 and "+40" in note
    assert fold_reference_shift(truth, AZ, 10.0, "none") == (0, "")


# ---------------------------------------------------------------------------
# The pipeline step
# ---------------------------------------------------------------------------


def _tree(vel: np.ndarray, nyq: float, az: np.ndarray = AZ) -> xr.DataTree:
    ds = xr.Dataset(
        {"VRADH": (("azimuth", "range"), vel.astype(np.float32), {"units": "m/s"})},
        coords={
            "azimuth": az,
            "range": RNG,
            "elevation": ("azimuth", np.full(az.size, 0.5, dtype=np.float32)),
        },
    )
    ds["sweep_fixed_angle"] = ((), 0.5)
    ds["nyquist_velocity"] = ((), nyq)
    root = xr.Dataset(coords={"latitude": 35.0, "longitude": -97.0, "altitude": 300.0})
    return xr.DataTree.from_dict({"/": root, "/sweep_0": ds})


def _run(tree: xr.DataTree, **params: Any):
    rec = pipeline.parse_recipe(
        {"name": "t", "target": "VRADH", "steps": [{"step_id": "dealias", "params": params}]}
    )
    ctx = plugins.ModuleContext(tree, 0, get_sweep_dataset=lambda i: tree["sweep_0"].to_dataset())
    return pipeline.run_recipe_on_sweep(rec, ctx)


def test_step_declares_reference_defaulting_to_none() -> None:
    spec = next(s for s in pipeline.list_steps() if s["id"] == "dealias")
    p = next(p for p in spec["params"] if p["name"] == "reference")
    assert p["default"] == "none" and p["options"] == ["none", "zero", "vad"]


@needs_radarx
@pytest.mark.parametrize("reference", ["zero", "vad"])
def test_step_region_with_reference(reference: str) -> None:
    truth = uniform_wind(25.0, 70.0)
    out = _run(_tree(fold(truth, 10.0), 10.0), method="region", reference=reference)
    np.testing.assert_allclose(out.after, truth, atol=1e-4)
    assert any(f"reference {reference}" in n for n in out.steps[0]["notes"])


def test_step_sector_scan_is_left_as_is_with_a_note() -> None:
    az = np.arange(30.0, 120.0) + 0.5
    tree = _tree(fold(uniform_wind(25.0, 70.0, az), 10.0), 10.0, az)
    plain = _run(tree, method="builtin")
    refd = _run(tree, method="builtin", reference="zero")
    np.testing.assert_array_equal(plain.after, refd.after)
    assert any("reference not applied" in n for n in refd.steps[0]["notes"])


def test_exported_script_applies_the_reference() -> None:
    truth = uniform_wind(25.0, 70.0)
    tree = _tree(fold(truth, 10.0), 10.0)
    rec = pipeline.parse_recipe(
        {
            "name": "t",
            "target": "VRADH",
            "steps": [{"step_id": "dealias", "params": {"method": "builtin", "reference": "vad"}}],
        }
    )
    ns: dict[str, Any] = {"__name__": "recipe_script"}
    exec(compile(pipeline.recipe_to_script(rec), "<recipe_script>", "exec"), ns)
    ns["rt"] = None
    got = ns["process_sweep"](tree["sweep_0"].to_dataset()).values
    ctx = plugins.ModuleContext(tree, 0, get_sweep_dataset=lambda i: tree["sweep_0"].to_dataset())
    want = pipeline.run_recipe_on_sweep(rec, ctx).after
    np.testing.assert_allclose(got, want, atol=1e-4)
    np.testing.assert_allclose(got, truth, atol=1e-4)


def test_command_line_setting_reaches_the_step() -> None:
    """``-P dealias.reference=zero`` on ``rxd pipeline run`` sets the step parameter."""
    from engine import api

    name = "Doppler velocity: dealias + clutter + bio filter"
    rec = api._resolve_recipe_arg(name, None, {"dealias.reference": "zero"})
    step = next(s for s in rec.steps if s["step_id"] == "dealias")
    assert step["params"]["reference"] == "zero"
    with pytest.raises(api.XrdArgumentError):
        api._resolve_recipe_arg(name, None, {"dealias.reference": "sounding"})
