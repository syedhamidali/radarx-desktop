"""qc_clean: multi-field quality mask (SNR / NCP / RhoHV / clutter / speckle)."""

from __future__ import annotations

import json

import numpy as np
import pytest
import xarray as xr

from engine import plugins

pytestmark = pytest.mark.filterwarnings("ignore")

N_AZ, N_RNG = 360, 100
AZ = np.arange(N_AZ, dtype=float)
RNG = 500.0 * (1 + np.arange(N_RNG))  # 0.5 .. 50 km
CLEAN, LOW_SNR, LOW_NCP, LOW_RHO, CLUTTER, SPECKLE = 1, 2, 3, 4, 5, 6


def _sweep(**extra):
    shape = (N_AZ, N_RNG)
    data = {
        "DBZH": np.full(shape, 30.0),
        "SNRHC": np.full(shape, 20.0),
        "NCP": np.full(shape, 0.9),
        "RHOHV": np.full(shape, 0.98),
        "VEL": np.full(shape, 8.0),
        "ZDRC": np.full(shape, 0.5),
    }
    data.update(extra)
    return xr.Dataset(
        {k: (("azimuth", "range"), v) for k, v in data.items() if v is not None},
        coords={"azimuth": AZ, "range": RNG},
    )


def _run(ds, **params):
    plugins.discover_modules()
    tree = xr.DataTree.from_dict({"/": xr.Dataset(), "/sweep_0": ds})
    payload, fields = plugins.run_module(
        "qc_clean", tree, 0, params, get_sweep_dataset=lambda i: ds, return_fields=True
    )
    return payload, fields


def test_each_rule_flags_its_planted_gates_with_priority_order() -> None:
    ds = _sweep()
    z = ds["DBZH"].values.copy()
    snr, ncp, rho, vel = (ds[n].values.copy() for n in ("SNRHC", "NCP", "RHOHV", "VEL"))
    snr[10:20, 60:70] = 1.0  # low SNR
    ncp[30:40, 60:70] = 0.1  # low NCP
    rho[50:60, 60:70] = 0.5  # low RhoHV
    snr[50:60, 60:70] = 1.0  # also low SNR -> SNR wins (first rule)
    vel[100:110, 10:20] = 0.0  # ground clutter: near radar, |V|~0, Z>15, RhoHV<0.95
    rho[100:110, 10:20] = 0.9
    z[200, 80] = 25.0  # isolated speckle: surround it with no data
    z[199:202, 79:82] = np.nan
    z[200, 80] = 25.0
    ds = _sweep(DBZH=z, SNRHC=snr, NCP=ncp, RHOHV=rho, VEL=vel)
    payload, fields = _run(ds)
    flags = fields["QC flags"]
    assert (flags[10:20, 60:70] == LOW_SNR).all()
    assert (flags[30:40, 60:70] == LOW_NCP).all()
    assert (flags[50:60, 60:70] == LOW_SNR).all()  # priority: SNR before RhoHV
    assert (flags[100:110, 10:20] == CLUTTER).all()
    assert flags[200, 80] == SPECKLE
    assert flags[0, 0] == CLEAN
    assert np.isnan(flags[199, 79])  # no data stays no data
    clean = fields["Cleaned reflectivity"]
    assert np.isnan(clean[10:20, 60:70]).all() and clean[0, 0] == 30.0
    assert fields["Cleaned ZDR"][0, 0] == 0.5 and np.isnan(fields["Cleaned ZDR"][10, 65])
    json.dumps(payload, allow_nan=False)  # strict-JSON-safe


def test_missing_optional_fields_skip_those_tests_and_say_so() -> None:
    payload, fields = _run(_sweep(SNRHC=None, NCP=None, RHOHV=None, VEL=None, ZDRC=None))
    scalars = {
        i["label"]: i["value"]
        for b in payload["blocks"]
        if b["type"] == "scalars"
        for i in b["items"]
    }
    assert "SNR" in scalars["Rules skipped (missing field)"]
    assert "speckle" in scalars["Rules applied"]
    assert "Cleaned ZDR" not in fields and "Cleaned velocity" not in fields
    assert "QC flags" in fields


def test_blob_across_the_north_seam_is_not_despeckled_away() -> None:
    z = np.full((N_AZ, N_RNG), np.nan)
    z[[358, 359, 0, 1], 40:44] = 30.0  # a 4x4 echo straddling 359 -> 0
    _, fields = _run(_sweep(DBZH=z))
    assert (fields["QC flags"][[358, 359, 0, 1], 40:44] == CLEAN).all()


def test_parameters_are_respected_and_clamped() -> None:
    ds = _sweep()
    snr = ds["SNRHC"].values.copy()
    snr[:, :] = 7.0
    _, strict = _run(_sweep(SNRHC=snr), snr_min=10.0)
    _, lenient = _run(_sweep(SNRHC=snr), snr_min=3.0)
    assert (strict["QC flags"] == LOW_SNR).all()
    assert (lenient["QC flags"] == CLEAN).all()
    payload, _ = _run(ds, rhohv_min=5.0)  # clamped to the declared max (1.0)
    assert payload["params"]["rhohv_min"] == 1.0


def test_all_nodata_sweep_is_a_clear_error() -> None:
    with pytest.raises(plugins.ModuleError, match="no valid reflectivity"):
        _run(_sweep(DBZH=np.full((N_AZ, N_RNG), np.nan)))


def test_aliases_find_cow_style_names() -> None:
    ds = _sweep()
    ds = ds.rename({"DBZH": "DBZHCC", "VEL": "VEL"})  # DBZHCC / SNRHC / VEL are COW names
    _, fields = _run(ds)
    assert "Cleaned reflectivity" in fields and "Cleaned velocity" in fields


def _rain_with_holes():
    """Uniform rain; a small 2x3 bad patch (filled) and a big 60x40 bad block (must stay empty)."""
    ds = _sweep()
    snr = ds["SNRHC"].values.copy()
    snr[100:102, 50:53] = 0.0  # tiny hole well inside good data
    snr[200:260, 30:70] = 0.0  # large noisy region
    return _sweep(SNRHC=snr)


def test_small_holes_are_filled_large_regions_are_not() -> None:
    # 500 m gates here: a 2.5 km radius spans the 2x3-gate hole but not the big block
    payload, fields = _run(_rain_with_holes(), despeckle_min=0, fill_radius_km=2.5)
    flags = fields["QC flags"]
    assert (flags[100:102, 50:53] == 7).all()  # FILLED
    filled = fields["Gap-filled reflectivity"]
    assert np.allclose(filled[100:102, 50:53], 30.0, atol=0.5)  # interpolated from rain at 30 dBZ
    assert np.isnan(fields["Cleaned reflectivity"][100:102, 50:53]).all()  # plain field untouched
    assert (flags[210:250, 40:60] == LOW_SNR).all()  # the core of the big block stays removed
    assert np.isnan(filled[220:240, 45:55]).all()
    scalars = {
        i["label"]: i["value"]
        for b in payload["blocks"]
        if b["type"] == "scalars"
        for i in b["items"]
    }
    assert float(scalars["Filled (interpolated)"].split()[0]) > 0
    json.dumps(payload, allow_nan=False)


def test_every_rule_can_be_switched_off() -> None:
    ds = _rain_with_holes()
    _, off = _run(
        ds,
        use_snr=False,
        use_ncp=False,
        use_rhohv=False,
        remove_clutter=False,
        despeckle_min=0,
        fill_gaps=False,
    )
    assert (off["QC flags"] == CLEAN).all()
    assert np.allclose(off["Cleaned reflectivity"], ds["DBZH"].values, atol=1e-4, equal_nan=True)
    assert not any(k.startswith("Gap-filled") for k in off)
    _, no_snr = _run(ds, use_snr=False, despeckle_min=0, fill_gaps=False)
    assert not (no_snr["QC flags"] == LOW_SNR).any()


def test_fill_off_and_smooth_on_outputs() -> None:
    ds = _rain_with_holes()
    _, nofill = _run(ds, fill_gaps=False)
    assert not any(k.startswith("Gap-filled") for k in nofill)
    _, sm = _run(ds, smooth=True, fill_gaps=False)
    smoothed = sm["Smoothed reflectivity"]
    assert np.isnan(smoothed[100:102, 50:53]).all()  # smoothing only touches clean gates
    assert np.isfinite(smoothed[0, 0])
