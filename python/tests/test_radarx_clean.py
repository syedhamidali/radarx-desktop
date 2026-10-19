"""radarx_clean: radarx echo mask + SNR gate -> cleaned fields and a flag map."""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from engine import plugins
from tests import radar_data

pytest.importorskip("radarx")
pytestmark = pytest.mark.filterwarnings("ignore")

N_AZ, N_RNG = 360, 240
AZ = np.arange(N_AZ, dtype=float)
RNG = 250.0 * (1 + np.arange(N_RNG))
CLEAN, LOW_SNR, NON_MET, SPECKLE = 1, 2, 3, 4


def _scene(with_snr=True):
    """Smooth rain in the east half, pure noise in the west half."""
    rng = np.random.default_rng(7)
    shape = (N_AZ, N_RNG)
    east = np.broadcast_to(((AZ > 20) & (AZ < 160))[:, None], shape)
    noisy = np.broadcast_to(((AZ > 200) & (AZ < 340))[:, None], shape)
    r_km = np.broadcast_to((RNG / 1000.0)[None, :], shape)
    z = np.where(east, 35 + rng.normal(0, 0.8, shape), np.nan)
    z = np.where(noisy, rng.uniform(-10, 15, shape), z)
    zdr = np.where(east, 1.0 + rng.normal(0, 0.08, shape), np.nan)
    zdr = np.where(noisy, rng.normal(0, 3.0, shape), zdr)
    rho = np.where(east, 0.985 + rng.normal(0, 0.004, shape), np.nan)
    rho = np.where(noisy, rng.uniform(0.3, 0.8, shape), rho)
    phi = np.where(east, 20 + 1.5 * r_km + rng.normal(0, 0.3, shape), np.nan)
    phi = np.where(noisy, rng.uniform(-180, 180, shape), phi)
    snr = np.where(east, 25.0, np.where(noisy, 1.5, np.nan))
    vel = np.where(east | noisy, 8.0, np.nan)
    data = {"DBZH": z, "ZDR": zdr, "RHOHV": rho, "PHIDP": phi, "VRADH": vel}
    if with_snr:
        data["SNR"] = snr
    ds = xr.Dataset(
        {k: (("azimuth", "range"), v) for k, v in data.items()},
        coords={"azimuth": AZ, "range": RNG},
    )
    return ds, east, noisy


def _run(ds, **params):
    plugins.discover_modules()
    tree = xr.DataTree.from_dict(
        {
            "/": xr.Dataset(
                {"latitude": 40.0, "longitude": -100.0, "altitude": 300.0},
            ),
            "/sweep_0": ds,
        }
    )
    return plugins.run_module(
        "radarx_clean", tree, 0, params, get_sweep_dataset=lambda i: ds, return_fields=True
    )


def test_noise_is_removed_and_smooth_rain_is_kept() -> None:
    ds, east, noisy = _scene()
    payload, fields = _run(ds)
    flags = fields["QC flags"]
    rain_kept = (flags[east] == CLEAN).mean()
    noise_kept = (flags[noisy] == CLEAN).mean()
    assert rain_kept > 0.9, rain_kept
    assert noise_kept < 0.1, noise_kept
    assert (flags[noisy] == LOW_SNR).mean() > 0.9  # SNR 1.5 dB < 3 dB: the specific reason wins
    clean = fields["Cleaned reflectivity"]
    assert np.isfinite(clean[east][flags[east] == CLEAN]).all()
    assert np.isnan(clean[noisy]).all()
    assert {"Cleaned ZDR", "Cleaned velocity", "Cleaned RhoHV", "QC flags"} <= set(fields)
    assert np.isnan(flags[~(east | noisy)]).all()  # no data stays no data
    json.dumps(payload, allow_nan=False)


def test_without_snr_the_echo_mask_still_separates_noise() -> None:
    ds, east, noisy = _scene(with_snr=False)
    payload, fields = _run(ds)
    flags = fields["QC flags"]
    assert (flags[east] == CLEAN).mean() > 0.9
    assert (flags[noisy] == CLEAN).mean() < 0.2
    assert not (flags == LOW_SNR).any()
    scalars = {
        i["label"]: i["value"]
        for b in payload["blocks"]
        if b["type"] == "scalars"
        for i in b["items"]
    }
    assert "SNR gate skipped" in scalars["Steps applied"]


def test_stricter_threshold_removes_at_least_as_much() -> None:
    ds, east, _ = _scene()
    _, loose = _run(ds, threshold=0.3)
    _, strict = _run(ds, threshold=0.9)
    assert (strict["QC flags"][east] == CLEAN).sum() <= (loose["QC flags"][east] == CLEAN).sum()


def test_noise_correction_outputs_are_opt_in() -> None:
    ds, _, _ = _scene()
    _, off = _run(ds)
    _, on = _run(ds, noise_correction=True)
    assert "Noise-corrected ZDR" not in off
    assert {"Noise-corrected ZDR", "Noise-corrected RhoHV"} <= set(on)
    assert on["Noise-corrected ZDR"].shape == ds["DBZH"].shape


def test_all_nodata_is_a_clear_error() -> None:
    ds, _, _ = _scene()
    ds["DBZH"][:] = np.nan
    with pytest.raises(plugins.ModuleError, match="no valid reflectivity"):
        _run(ds)


def test_listed_as_unavailable_when_radarx_is_missing(monkeypatch) -> None:
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda n, *a, **k: None if n == "radarx" else real(n, *a, **k)
    )
    plugins.discover_modules()
    info = next(m for m in plugins.list_modules()["modules"] if m["id"] == "radarx_clean")
    assert info["available"] is False and "radarx" in info["unavailable_reason"]


_COW = radar_data.example(
    "RADAR_CORRECTED/cfrad.20220322_220338.299_to_20220322_220506.977_COW1high_SUR.nc"
) or Path("/nonexistent/COW.nc")


@pytest.mark.skipif(
    not _COW.exists() or not os.environ.get("XRADAR_SLOW_TESTS"),
    reason="needs the real COW1 sample and XRADAR_SLOW_TESTS=1",
)
def test_real_cow_sweep_keeps_the_storm_and_drops_noise() -> None:
    from engine.reader import RadarReader

    reader = RadarReader()
    reader.open_file(str(_COW))
    plugins.discover_modules()
    payload, _fields = plugins.run_module(
        "radarx_clean",
        reader.datatree,
        0,
        {},
        get_sweep_dataset=reader.get_sweep_dataset,
        return_fields=True,
    )
    scalars = {
        i["label"]: i["value"]
        for b in payload["blocks"]
        if b["type"] == "scalars"
        for i in b["items"]
    }
    kept = float(scalars["Kept (clean)"].split()[0])
    assert 55.0 < kept < 80.0, kept


def _scene_with_hole():
    ds, east, noisy = _scene()
    snr = ds["SNR"].values.copy()
    snr[80:82, 100:103] = 0.0  # a tiny noisy patch inside the smooth rain
    ds["SNR"] = (("azimuth", "range"), snr)
    return ds, east, noisy


def test_small_gap_in_rain_is_filled_but_noise_region_stays_empty() -> None:
    ds, _, noisy = _scene_with_hole()
    _, fields = _run(ds, fill_radius_km=1.5)
    flags = fields["QC flags"]
    assert (flags[80:82, 100:103] == 5).all()  # FILLED
    filled = fields["Gap-filled reflectivity"][80:82, 100:103]
    assert np.isfinite(filled).all() and abs(float(filled.mean()) - 35.0) < 3.0
    assert np.isnan(fields["Cleaned reflectivity"][80:82, 100:103]).all()
    assert np.isnan(fields["Gap-filled reflectivity"][noisy]).mean() > 0.95  # nothing invented
    assert "Gap-filled velocity" not in fields  # velocity is never interpolated


def test_every_step_can_be_switched_off() -> None:
    ds, _, _ = _scene_with_hole()
    valid = np.isfinite(ds["DBZH"].values)
    _, off = _run(
        ds, use_snr_gate=False, use_echo_mask=False, remove_speckle=False, fill_gaps=False
    )
    assert (off["QC flags"][valid] == CLEAN).all()
    assert np.allclose(off["Cleaned reflectivity"], ds["DBZH"].values, atol=1e-4, equal_nan=True)
    assert not any(k.startswith("Gap-filled") for k in off)
    _, only_snr = _run(ds, use_echo_mask=False, remove_speckle=False, fill_gaps=False)
    assert (only_snr["QC flags"] == LOW_SNR).any() and not (only_snr["QC flags"] == NON_MET).any()
    _, no_snr = _run(ds, use_snr_gate=False, fill_gaps=False)
    assert not (no_snr["QC flags"] == LOW_SNR).any()


def test_smoothing_output_only_on_request() -> None:
    ds, _, _ = _scene_with_hole()
    _, default = _run(ds)
    _, smoothed = _run(ds, smooth=True)
    assert "Smoothed reflectivity" not in default and "Smoothed reflectivity" in smoothed


def test_mask_applies_to_every_polar_field_by_default() -> None:
    ds, east, noisy = _scene()
    shape = ds["DBZH"].shape
    ds["KDP"] = (("azimuth", "range"), np.where(east | noisy, 0.8, np.nan))
    ds["WIDTH"] = (("azimuth", "range"), np.where(east | noisy, 2.0, np.nan))
    _, fields = _run(ds, fill_gaps=False)
    flagged = fields["QC flags"] != CLEAN
    for name in ("KDP", "WIDTH"):
        out = fields[f"Cleaned {name}"]
        assert out.shape == shape
        assert np.isnan(out[flagged]).all(), name
        kept = ~flagged & np.isfinite(ds["DBZH"].values)
        assert np.isfinite(out[kept]).all(), name
    _, main_only = _run(ds, apply_to="main fields only", fill_gaps=False)
    assert "Cleaned KDP" not in main_only and "Cleaned reflectivity" in main_only
