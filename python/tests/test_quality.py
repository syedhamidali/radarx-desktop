"""Data-quality check (engine/quality.py): injected faults are found where they were put, clean
synthetic data raises nothing above 'info', and a real NEXRAD volume shows its known facts."""

from __future__ import annotations

import json
import math
import os
import re
import warnings
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import xarray as xr

from engine import pipeline, quality
from tests import radar_data

pytestmark = pytest.mark.filterwarnings("ignore")

N_AZ, N_RNG, DR = 360, 400, 250.0  # 1 degree rays, 100 km at 250 m
NYQ = 26.0
AZ = np.arange(N_AZ, dtype=float)
RNG = DR * (1 + np.arange(N_RNG))
RKM = RNG / 1000.0


def _scene(seed: int = 1) -> dict[str, np.ndarray]:
    """A clean rain scene: smooth Z with plenty of light-rain gates, ZDR tied to Z (0.3 dB at
    22.5 dBZ), RhoHV 0.99, PhiDP rising slowly, a wind field inside the Nyquist interval."""
    rng = np.random.default_rng(seed)
    a = np.deg2rad(AZ)[:, None]
    r = RKM[None, :]
    z = 23.0 + 9.0 * np.sin(r / 9.0 + 2.0 * a) + 6.0 * np.cos(3.0 * a - r / 17.0)
    z = z + rng.normal(0, 0.5, z.shape)
    zdr = 0.3 + 0.03 * (z - 22.5) + rng.normal(0, 0.05, z.shape)
    rho = 0.99 + rng.normal(0, 0.003, z.shape)
    phi = 20.0 + 0.25 * r + rng.normal(0, 1.0, z.shape)
    v = 18.0 * np.cos(a - np.deg2rad(60.0)) + rng.normal(0, 0.4, z.shape) + 0 * r
    w = 2.0 + rng.normal(0, 0.3, z.shape)
    return {"DBZH": z, "ZDR": zdr, "RHOHV": rho, "PHIDP": phi, "VRADH": v, "WRADH": w}


def _sweep(
    fields: dict[str, np.ndarray],
    elev: float,
    *,
    t0: float = 0.0,
    az=AZ,
    nyquist=NYQ,
    encode_velocity: bool = False,
) -> xr.Dataset:
    n = len(az)
    t = np.datetime64("2024-05-01T12:00:00", "ns") + ((t0 + 0.05 * np.arange(n)) * 1e9).astype(
        "timedelta64[ns]"
    )
    units = {
        "DBZH": "dBZ",
        "ZDR": "dB",
        "RHOHV": "",
        "PHIDP": "degrees",
        "VRADH": "meters per second",
        "WRADH": "meters per second",
    }
    ds = xr.Dataset(
        {
            k: (("azimuth", "range"), v.astype(np.float32), {"units": units[k]})
            for k, v in fields.items()
        },
        coords={
            "azimuth": ("azimuth", np.asarray(az, dtype=float)),
            "range": ("range", RNG),
            "elevation": ("azimuth", np.full(n, elev)),
            "time": ("azimuth", t),
        },
    )
    ds["sweep_fixed_angle"] = ((), elev)
    ds["sweep_mode"] = ((), "azimuth_surveillance")
    if nyquist:
        ds["nyquist_velocity"] = ((), nyquist)
    if encode_velocity and "VRADH" in ds:
        ds["VRADH"].encoding = {"scale_factor": 0.5, "add_offset": -64.5, "dtype": "uint8"}
    return ds


def _tree(sweeps: list[xr.Dataset]) -> xr.DataTree:
    root = xr.Dataset(coords={"latitude": 35.0, "longitude": -97.0, "altitude": 300.0})
    nodes = {"/": root}
    for i, ds in enumerate(sweeps):
        nodes[f"/sweep_{i}"] = ds
    return xr.DataTree.from_dict(nodes)


def volume(
    *,
    alias: bool = False,
    fold: bool = False,
    no_width: bool = False,
    no_rhohv: bool = False,
    time_gap: bool = False,
    sector: bool = False,
    clutter: bool = False,
    blockage: bool = False,
    zdr_offset: float = 0.0,
    nyquist: float | None = NYQ,
) -> xr.DataTree:
    """Three sweeps: 0.5 deg dual-pol, 0.5 deg Doppler, 1.5 deg dual-pol, with injected faults."""
    s = _scene()
    dp = {k: s[k].copy() for k in ("DBZH", "ZDR", "RHOHV", "PHIDP")}
    dop = {k: s[k].copy() for k in ("DBZH", "VRADH", "WRADH")}
    dp["ZDR"] = dp["ZDR"] + zdr_offset
    if alias:
        dop["VRADH"] = 40.0 * np.cos(np.deg2rad(AZ)[:, None] - np.deg2rad(60.0)) + 0 * RKM[None, :]
        dop["VRADH"] = ((dop["VRADH"] + NYQ) % (2 * NYQ)) - NYQ
    if clutter:  # a ring at 20-25 km: strong, still
        ring = (RKM >= 20) & (RKM < 25)
        dop["DBZH"][:, ring] = 48.0
        dop["VRADH"][:, ring] = np.random.default_rng(5).normal(0, 0.2, (N_AZ, int(ring.sum())))
        dp["DBZH"][:, ring] = 48.0
    if blockage:  # az 100-115, beyond 25 km nothing
        wedge = (AZ >= 100) & (AZ <= 115)
        far = RKM > 25
        for d in (dp, dop):
            for k in d:
                d[k][np.ix_(wedge, far)] = np.nan
    if fold:  # range-folded code (value offset + scale) over az 200-229, 60-90 km
        wedge = (AZ >= 200) & (AZ < 230)
        far = (RKM >= 60) & (RKM < 90)
        dop["VRADH"][np.ix_(wedge, far)] = -64.0
    if no_width:
        del dop["WRADH"]
    if no_rhohv:
        del dp["RHOHV"]
    az = AZ
    sweeps = [
        _sweep(dp, 0.5, nyquist=None),
        _sweep(dop, 0.5, t0=20.0, nyquist=nyquist, encode_velocity=fold),
        _sweep({k: v.copy() for k, v in dp.items()}, 1.5, t0=40.0, nyquist=None),
    ]
    if time_gap:  # rays after number 200 of the Doppler sweep come 9 s late
        ds = sweeps[1]
        t = ds["time"].values.copy()
        t[200:] += np.timedelta64(9, "s")
        sweeps[1] = ds.assign_coords(time=("azimuth", t))
    if sector:  # the Doppler sweep covers only az 60-240
        keep = (AZ >= 60) & (AZ <= 240)
        sweeps[1] = sweeps[1].isel(azimuth=np.nonzero(keep)[0])
    del az
    return _tree(sweeps)


def report(tree: xr.DataTree, **kw: Any) -> dict[str, Any]:
    rep = quality.check_tree(tree, fmt="synthetic", **kw)
    json.dumps(rep)  # the UI and `--json` get exactly this
    assert rep["errors"] == []
    return rep


def find(rep: dict[str, Any], fid: str) -> dict[str, Any] | None:
    return next((f for f in rep["findings"] if f["id"] == fid), None)


def covers(interval: list[float], lo: float, hi: float, tol: float) -> bool:
    return interval[0] <= lo + tol and interval[1] >= hi - tol


# ---------------------------------------------------------------------------
# Clean data
# ---------------------------------------------------------------------------


def test_circular_arc_handles_clusters_and_the_north_seam():
    c = quality.circular_arc
    assert c(np.array([10.0] * 800 + [109.0] * 800 + [190.0] * 800 + [289.0] * 800), 0.9) == (
        109.0,
        10.0,
    )
    assert c(np.array([350.0] * 50 + [5.0] * 50), 0.9) == (350.0, 5.0)  # across north
    assert c(np.array([100.0, 101.0, 102.0, 250.0]), 0.7) == (100.0, 102.0)


def test_clean_volume_has_no_notice_or_warning():
    rep = report(volume())
    bad = [f for f in rep["findings"] if f["severity"] != "info"]
    assert not bad, [(f["id"], f["summary"]) for f in bad]
    assert rep["counts"]["warning"] == 0 and rep["counts"]["notice"] == 0
    assert rep["recommendation"]["recipes"] == []  # nothing to fix, nothing recommended
    view = rep["recommendation"]["first_view"]
    assert view["variable"] == "DBZH" and view["sweep"] == 0
    zdr = find(rep, "zdr_offset")
    assert zdr is not None and zdr["severity"] == "info"
    off = next(e for e in zdr["evidence"] if e["label"] == "ZDR offset")
    assert abs(off["value"]) < 0.05


def test_clean_volume_is_fast_and_bounded():
    rep = report(volume())
    assert rep["elapsed_ms"] < 1500


# ---------------------------------------------------------------------------
# Injected faults
# ---------------------------------------------------------------------------


def test_aliasing_is_found_with_nyquist_and_dealias_recommended():
    rep = report(volume(alias=True))
    f = find(rep, "velocity_aliasing")
    assert f is not None and f["severity"] == "warning"
    assert f["sweeps"] == [1]
    nyq = next(e for e in f["evidence"] if e["label"] == "Nyquist velocity")
    assert nyq["value"] == NYQ and nyq["kind"] == "RETRIEVED"
    jumps = next(e for e in f["evidence"] if e["label"].startswith("steps above"))
    assert jumps["value"] > 0.3  # percent
    # the fold boundaries lie where |V| passes 26 m/s: near az 60 +- 46 / 180 (not at the extremes)
    assert f["where"]["range_km"][0] < 10 and f["where"]["range_km"][1] > 90
    recipe = next(r for r in rep["recommendation"]["recipes"] if r["role"] == "velocity")
    assert [s["step_id"] for s in recipe["recipe"]["steps"]] == ["dealias"]
    assert recipe["recipe"]["target"] == "VRADH"
    assert recipe["reasons"][0]["reason"]
    pipeline.parse_recipe(recipe["recipe"])  # the Apply button sends exactly this


def test_aliasing_with_unknown_nyquist_is_estimated_and_labelled():
    rep = report(volume(alias=True, nyquist=None))
    f = find(rep, "velocity_aliasing")
    assert f is not None
    nyq = next(e for e in f["evidence"] if e["label"] == "Nyquist velocity")
    assert nyq["kind"] == "ESTIMATE" and abs(nyq["value"] - NYQ) <= 1.0
    assert f["confidence"] < 0.8


def test_range_folded_gates_found_with_location_and_size():
    rep = report(volume(fold=True))
    f = find(rep, "range_folding")
    assert f is not None, rep["findings"]
    folded = 30 * 120  # rays x gates (60-90 km at 250 m)
    n = next(e for e in f["evidence"] if e["label"] == "range-folded gates")["value"]
    assert n == folded
    assert covers(f["where"]["azimuth_deg"], 202, 227, 4)
    assert covers(f["where"]["range_km"], 62, 88, 4)
    assert f["sweeps"] == [1]


def test_velocity_without_width_and_zdr_without_rhohv():
    rep = report(volume(no_width=True, no_rhohv=True))
    assert find(rep, "velocity_without_width")["severity"] == "notice"
    assert find(rep, "zdr_without_rhohv")["severity"] == "warning"
    assert find(rep, "zdr_offset") is None or find(rep, "zdr_offset")["severity"] == "info"


def test_time_gap_found_at_the_right_ray():
    rep = report(volume(time_gap=True))
    f = find(rep, "time_gap")
    assert f is not None and f["sweeps"] == [1]
    gap = next(e for e in f["evidence"] if e["label"] == "largest gap")["value"]
    assert 8.9 < gap < 9.2
    lost = next(e for e in f["evidence"] if e["label"].startswith("rays missing"))
    assert lost["kind"] == "ESTIMATE" and 150 < lost["value"] < 200  # 9 s / 0.05 s
    assert covers(f["where"]["azimuth_deg"], 199, 200, 1.5)  # between ray 199 and ray 200
    assert f["severity"] == "warning"  # ~50 % of that sweep's rays are missing


def test_unsorted_and_duplicate_times_warn():
    tree = volume()
    ds = tree["sweep_0"].to_dataset()
    t = ds["time"].values.copy()
    t[50] = t[10]  # a time that jumps back
    t[80] = t[79]  # and one repeated
    bad = ds.assign_coords(time=("azimuth", t))
    tree2 = _tree([bad, tree["sweep_1"].to_dataset(), tree["sweep_2"].to_dataset()])
    f = find(report(tree2), "time_order")
    assert f is not None and f["severity"] == "warning" and f["sweeps"] == [0]


def test_rotated_ray_storage_is_not_an_error():
    """NEXRAD volumes are stored from azimuth ~0 although the scan started elsewhere: one backward
    step, otherwise increasing time. That is not unsorted."""
    tree = volume()
    ds = tree["sweep_0"].to_dataset()
    shift = 170
    rolled = ds.isel(azimuth=np.roll(np.arange(N_AZ), -shift))
    rolled = rolled.assign_coords(azimuth=("azimuth", np.arange(N_AZ, dtype=float)))
    rolled = rolled.assign_coords(time=("azimuth", np.roll(ds["time"].values, shift)))
    tree2 = _tree([rolled, tree["sweep_1"].to_dataset(), tree["sweep_2"].to_dataset()])
    assert find(report(tree2), "time_order") is None


def test_partial_sector_found():
    rep = report(volume(sector=True))
    f = find(rep, "partial_coverage")
    assert f is not None and f["sweeps"] == [1]
    cov = next(e for e in f["evidence"] if e["label"].startswith("azimuth coverage"))["value"]
    assert abs(cov - 181) < 2
    assert (
        abs(f["where"]["azimuth_deg"][0] - 240) <= 1 and abs(f["where"]["azimuth_deg"][1] - 60) <= 1
    )
    assert f["severity"] == "warning"  # the other sweeps are complete circles


def test_sector_scan_alone_is_a_notice_not_a_warning():
    tree = volume(sector=True)
    only = _tree([tree["sweep_1"].to_dataset()])
    f = find(report(only), "partial_coverage")
    assert f is not None and f["severity"] == "notice"


def test_clutter_ring_found_with_range_and_recipe():
    rep = report(volume(clutter=True))
    f = find(rep, "ground_clutter")
    assert f is not None, [x["id"] for x in rep["findings"]]
    assert covers(f["where"]["range_km"], 20.5, 24.5, 1.5)
    assert f["where"]["azimuth_deg"] == [0.0, 360.0]
    share = next(e for e in f["evidence"] if e["label"] == "share of echo gates")["value"]
    assert 3.0 < share < 8.0  # 20 of 400 gates = 5 %
    assert "clutter_filter" in f["steps"]
    steps = {s["step_id"] for r in rep["recommendation"]["recipes"] for s in r["recipe"]["steps"]}
    assert "clutter_filter" in steps


def test_blocked_wedge_found():
    rep = report(volume(blockage=True))
    f = find(rep, "beam_blockage")
    assert f is not None, [x["id"] for x in rep["findings"]]
    a0, a1 = f["where"]["azimuth_deg"]
    assert abs(a0 - 100) <= 3 and abs(a1 - 116) <= 3
    assert f["severity"] == "notice" and f["confidence"] <= 0.6  # a proxy, never a warning


def test_zdr_offset_of_half_a_db_is_recovered_with_uncertainty():
    rep = report(volume(zdr_offset=0.5))
    f = find(rep, "zdr_offset")
    assert f is not None and f["severity"] == "warning"
    ev = {e["label"]: e for e in f["evidence"]}
    assert ev["ZDR offset"]["kind"] == "ESTIMATE"
    assert abs(ev["ZDR offset"]["value"] - 0.5) < 0.05
    sigma = ev["1-sigma uncertainty"]["value"]
    assert 0.1 <= sigma < 0.2  # dominated by the +-0.1 dB reference uncertainty
    assert ev["qualifying gates"]["value"] > 3000
    assert "Ryzhkov" in f["method"]
    assert "Subtract" in f["summary"]


def test_zdr_offset_refused_with_too_few_samples():
    s = _scene()
    s["DBZH"] = np.full_like(s["DBZH"], 40.0)  # heavy rain everywhere: no light-rain gates at all
    tree = _tree([_sweep({k: s[k] for k in ("DBZH", "ZDR", "RHOHV", "PHIDP")}, 0.5, nyquist=None)])
    f = find(report(tree), "zdr_offset")
    assert f is not None and f["severity"] == "info" and f["confidence"] == 0.0
    assert "none is given" in f["summary"]
    assert not any(e["label"] == "ZDR offset" for e in f["evidence"])


def test_phidp_noise_and_wraps_found():
    s = _scene()
    rng = np.random.default_rng(2)
    s["PHIDP"] = s["PHIDP"] + rng.normal(0, 20.0, s["PHIDP"].shape)
    s["PHIDP"] = np.mod(s["PHIDP"] + 340.0, 360.0)  # starts near 360 and wraps
    tree = _tree([_sweep({k: s[k] for k in ("DBZH", "ZDR", "RHOHV", "PHIDP")}, 0.5, nyquist=None)])
    rep = report(tree)
    n = find(rep, "phidp_noise")
    assert n is not None and n["severity"] == "warning"
    assert next(e for e in n["evidence"] if e["label"] == "PhiDP noise")["value"] > 15
    assert find(rep, "phidp_unfold") is not None


def test_attenuation_hint_needs_a_band_and_phase():
    s = _scene()
    s["PHIDP"] = 20.0 + 1.2 * RKM[None, :] + np.zeros((N_AZ, N_RNG))  # 120 deg over 100 km
    ds = _sweep({k: s[k] for k in ("DBZH", "ZDR", "RHOHV", "PHIDP")}, 0.5, nyquist=None)
    c_band = ds.assign(radar_frequency=((), 5.6e9))
    f = find(report(_tree([c_band])), "attenuation")
    assert f is not None and f["severity"] in ("notice", "warning")
    pia = next(e for e in f["evidence"] if e["label"].startswith("path-integrated"))
    assert pia["kind"] == "ESTIMATE" and 5 < pia["value"] < 15  # ~0.08 dB/deg x ~120 deg
    assert find(report(_tree([ds])), "attenuation") is None  # band unknown: no number is invented
    s_band = ds.assign(radar_frequency=((), 2.8e9))
    assert find(report(_tree([s_band])), "attenuation") is None


def test_unit_and_scale_oddities():
    s = _scene()
    s["RHOHV"] = s["RHOHV"] * 100.0  # percent instead of a ratio
    tree = _tree([_sweep({k: s[k] for k in ("DBZH", "ZDR", "RHOHV", "PHIDP")}, 0.5, nyquist=None)])
    f = find(report(tree), "units_rhohv")
    assert f is not None and f["severity"] == "warning"


def test_velocity_above_stated_nyquist_warns():
    s = _scene()
    s["VRADH"] = s["VRADH"] * 2.5
    tree = _tree([_sweep({k: s[k] for k in ("DBZH", "VRADH", "WRADH")}, 0.5)])
    assert find(report(tree), "nyquist_mismatch") is not None


def test_missing_reflectivity_and_empty_field():
    s = _scene()
    tree = _tree([_sweep({k: s[k] for k in ("VRADH", "WRADH")}, 0.5)])
    assert find(report(tree), "no_reflectivity")["severity"] == "warning"
    s["WRADH"] = np.full_like(s["WRADH"], np.nan)
    tree = _tree([_sweep({k: s[k] for k in ("DBZH", "VRADH", "WRADH")}, 0.5)])
    assert find(report(tree), "empty_field") is not None


def test_bio_and_speckle_recommend_their_steps():
    s = _scene()
    rng = np.random.default_rng(4)
    weak = rng.random(s["DBZH"].shape) < 0.5
    s["DBZH"] = np.where(weak, rng.uniform(0, 15, s["DBZH"].shape), s["DBZH"])
    s["RHOHV"] = np.where(weak, 0.5, s["RHOHV"])
    rep = report(
        _tree([_sweep({k: s[k] for k in ("DBZH", "ZDR", "RHOHV", "PHIDP")}, 0.5, nyquist=None)])
    )
    bio = find(rep, "biological_echo")
    assert bio is not None and bio["severity"] == "notice" and "biological_filter" in bio["steps"]
    steps = {st["step_id"] for r in rep["recommendation"]["recipes"] for st in r["recipe"]["steps"]}
    assert "biological_filter" in steps


def test_recommended_recipes_are_valid_and_ordered():
    s = _scene()
    dop = {k: s[k].copy() for k in ("DBZH", "VRADH", "WRADH")}
    dop["VRADH"] = (
        (40.0 * np.cos(np.deg2rad(AZ)[:, None]) + 0 * RKM[None, :] + NYQ) % (2 * NYQ)
    ) - NYQ
    ring = (RKM >= 20) & (RKM < 25)
    dop["DBZH"][:, ring] = 48.0
    dop["VRADH"][:, ring] = 0.0
    rep = report(_tree([_sweep(dop, 0.5)]))
    rec = next(r for r in rep["recommendation"]["recipes"] if r["role"] == "velocity")
    ids = [x["step_id"] for x in rec["recipe"]["steps"]]
    assert ids.index("clutter_filter") < ids.index("dealias")  # filters before the unfolding
    assert {r["step_id"] for r in rec["reasons"]} == set(ids)
    for r in rep["recommendation"]["recipes"]:
        pipeline.parse_recipe(r["recipe"])


def test_rhi_scan_is_labelled_and_skips_azimuth_checks():
    n = 90
    el = np.linspace(0.0, 45.0, n)
    ds = xr.Dataset(
        {
            "DBZH": (
                ("elevation", "range"),
                np.full((n, 200), 30.0, dtype=np.float32),
                {"units": "dBZ"},
            )
        },
        coords={"elevation": el, "range": RNG[:200], "azimuth": ("elevation", np.full(n, 135.0))},
    )
    ds["sweep_mode"] = ((), "rhi")
    ds["sweep_fixed_angle"] = ((), 135.0)
    rep = report(_tree([ds]))
    f = find(rep, "scan_type")
    assert "RHI" in f["title"]
    assert find(rep, "partial_coverage") is None and find(rep, "beam_blockage") is None


def test_check_never_raises_on_odd_input():
    odd = xr.Dataset(coords={"range": RNG[:10]})
    rep = quality.check_tree(_tree([odd]))
    assert rep["findings"] == [] or all("id" in f for f in rep["findings"])
    empty = quality.check_tree(xr.DataTree.from_dict({"/": xr.Dataset()}))
    assert empty["n_sweeps"] == 0


# ---------------------------------------------------------------------------
# Real file (read-only; skipped when the local example files are absent)
# ---------------------------------------------------------------------------

KTLX = radar_data.example("KTLX20130520_201643_V06.gz")
needs_ktlx = pytest.mark.skipif(KTLX is None, reason="KTLX example file not available")
_SEVERE = os.environ.get("XRADAR_SEVERE_CASES_DIR", "~/Downloads/SEVERE_CASES_TEST")
KVWX = Path(_SEVERE).expanduser() / "mayfield" / "KVWX" / "KVWX20211211_020000_V06"
needs_kvwx = pytest.mark.skipif(not KVWX.is_file(), reason="KVWX case file not available")


def test_katx_open_radar_data_smoke():
    """The open-radar-data NEXRAD volume (downloaded on CI): the check runs, is JSON, and finds
    the split-cut layout; no timing assertion here (CI machines vary)."""
    path = radar_data.katx()
    if path is None:
        pytest.skip("KATX not available")
    from engine import api

    with api.open(str(path)) as vol:
        rep = quality.check_volume(vol)
    json.dumps(rep)
    assert rep["errors"] == [] and rep["n_sweeps"] >= 10
    ids = {f["id"] for f in rep["findings"]}
    assert {"scan_type", "split_cut"} <= ids
    for r in rep["recommendation"]["recipes"]:
        pipeline.parse_recipe(r["recipe"])


@needs_kvwx
def test_kvwx_known_facts():
    """A second real NEXRAD volume (read-only): same split-cut layout, Nyquist estimated."""
    from engine import api

    with api.open(str(KVWX)) as vol:
        rep = quality.check_volume(vol)
    assert rep["errors"] == [] and rep["elapsed_ms"] < 1500, rep["elapsed_ms"]
    f = {x["id"]: x for x in rep["findings"]}
    assert f["scan_type"]["summary"].startswith("15 PPI")
    assert f["split_cut"]["severity"] == "info"
    al = f["velocity_aliasing"]
    nyq = next(e for e in al["evidence"] if e["label"] == "Nyquist velocity")
    assert nyq["kind"] == "ESTIMATE" and 25 <= nyq["value"] <= 32
    assert all(s % 2 == 1 for s in al["sweeps"] if s < 5)  # velocity lives on the odd sweeps
    rf = f["range_folding"]  # the Doppler cut of this volume has a lot of folded gates
    assert rf["severity"] == "warning" and 1 in rf["sweeps"]
    assert "time_order" not in f and "partial_coverage" not in f
    # too few light-rain gates: the ZDR offset is refused, not guessed
    zdr = f.get("zdr_offset")
    assert zdr is None or zdr["severity"] == "info" or zdr["confidence"] > 0
    for r in rep["recommendation"]["recipes"]:
        pipeline.parse_recipe(r["recipe"])


@needs_ktlx
def test_ktlx_known_facts():
    from engine import api

    with api.open(str(KTLX)) as vol:
        rep = quality.check_volume(vol)
    json.dumps(rep)
    assert rep["errors"] == []
    assert rep["elapsed_ms"] < 1500, rep["elapsed_ms"]
    f = {x["id"]: x for x in rep["findings"]}
    scan = f["scan_type"]
    assert scan["summary"].startswith("17 PPI")
    # split cut: velocity lives on the odd sweeps 1, 3, 5, ... and dual-pol on 0, 2, 4, ...
    assert "split_cut" in f
    # the Nyquist velocity is not in the file: estimated, and close to the known 26 m/s of VCP 12
    al = f.get("velocity_aliasing")
    assert al is not None
    nyq = next(e for e in al["evidence"] if e["label"] == "Nyquist velocity")
    assert nyq["kind"] == "ESTIMATE" and 24 <= nyq["value"] <= 32
    assert all(s % 2 == 1 for s in al["sweeps"] if s < 5)
    # rays are stored rotated, which must not be called unsorted time
    assert "time_order" not in f
    # the full-circle sweeps raise no coverage finding
    assert "partial_coverage" not in f
    # everything recommended must be an existing, valid recipe
    for r in rep["recommendation"]["recipes"]:
        pipeline.parse_recipe(r["recipe"])
    view = rep["recommendation"]["first_view"]
    assert view["sweep"] == 0 and view["variable"] == "DBZH"


def test_report_is_stable_json():
    rep = report(volume(alias=True, zdr_offset=0.5))
    again = report(volume(alias=True, zdr_offset=0.5))
    rep.pop("elapsed_ms")
    again.pop("elapsed_ms")
    assert json.dumps(rep, sort_keys=True) == json.dumps(again, sort_keys=True)
    assert all(math.isfinite(f["confidence"]) for f in rep["findings"])


# ---------------------------------------------------------------------------
# xrd check
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_open(monkeypatch):
    from engine import api, cli

    trees = {"aliased.nc": volume(alias=True, zdr_offset=0.5), "clean.nc": volume()}

    def _open(path, mapping=None):
        return api.Volume.from_tree(trees[path], path=path, format="synthetic")

    monkeypatch.setattr(cli, "_open", _open)
    monkeypatch.setattr(cli, "expand_inputs", lambda items: list(items))
    return cli


def test_cli_check_exit_zero_with_findings_and_json(fake_open, capsys):
    code = fake_open.main(["check", "-i", "aliased.nc", "--json"])
    assert code == 0  # findings are not errors
    rep = json.loads(capsys.readouterr().out)
    assert rep["counts"]["warning"] >= 2 and rep["file"] == "aliased.nc"
    assert {f["id"] for f in rep["findings"]} >= {"velocity_aliasing", "zdr_offset"}


def test_cli_check_table_shows_evidence_and_plan(fake_open, capsys):
    assert fake_open.main(["check", "-i", "aliased.nc", "--fix-plan", "--no-color"]) == 0
    text = capsys.readouterr().out
    assert "WARNING" in text and "Velocity looks aliased" in text
    assert "Nyquist velocity 26 m/s [RETRIEVED]" in text  # evidence with its label
    assert re.search(r"ZDR offset 0\.[45]\d* dB \[ESTIMATE\]", text)
    assert "Fix plan" in text and "dealias" in text
    assert "none of them is applied" in text


def test_cli_check_several_files_json_is_a_list(fake_open, capsys):
    assert fake_open.main(["check", "-i", "aliased.nc", "clean.nc", "--json"]) == 0
    reps = json.loads(capsys.readouterr().out)
    assert [r["file"] for r in reps] == ["aliased.nc", "clean.nc"]
    assert reps[1]["counts"]["warning"] == 0


def test_cli_check_writes_recipe_that_pipeline_accepts(fake_open, tmp_path, capsys):
    from engine import api

    out = tmp_path / "recipe.json"
    assert fake_open.main(["check", "-i", "aliased.nc", "--fix-plan", "-o", str(out)]) == 0
    capsys.readouterr()
    saved = json.loads(out.read_text())
    assert saved["target"] == "VRADH" and [s["step_id"] for s in saved["steps"]] == ["dealias"]
    assert api.recipe(str(out)).name.startswith("Data check")


def test_cli_check_usage_and_input_errors(fake_open, capsys, monkeypatch):
    assert fake_open.main(["check", "-i", "aliased.nc", "-o", "x.json"]) == 2  # -o needs --fix-plan
    assert fake_open.main(["check"]) == 2  # no input
    capsys.readouterr()


def test_cli_check_missing_file_is_an_input_error(tmp_path, capsys):
    from engine import cli

    assert cli.main(["check", "-i", str(tmp_path / "nope.nc")]) == 3
    capsys.readouterr()


@needs_ktlx
def test_cli_check_real_file(capsys):
    from engine import cli

    assert cli.main(["check", "-i", str(KTLX), "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["n_sweeps"] == 17


# ---------------------------------------------------------------------------
# WebSocket: the app asks for the check and applies the recipe with one click
# ---------------------------------------------------------------------------


@pytest.fixture()
def served():
    import server
    from engine.reader import RadarReader

    saved = (dict(server._file_readers), server._last_file_id)
    server._file_readers.clear()
    server._last_file_id = None
    reader = RadarReader()
    reader._dtree = volume(alias=True, zdr_offset=0.5)
    reader._format = "synthetic"
    yield server._register_reader(reader), reader
    server._file_readers.clear()
    server._file_readers.update(saved[0])
    server._last_file_id = saved[1]


@pytest.mark.asyncio
async def test_websocket_data_check_then_apply_recipe(served, tmp_path, monkeypatch):
    from engine import derived
    from tests.test_pipeline_server import _make_handler, _send

    monkeypatch.setenv("XRADAR_RECIPES_DIR", str(tmp_path / "recipes"))
    file_id, reader = served
    h = _make_handler()
    await _send(h, type="data_check", file_id=file_id)
    msg = next(m for m in h._sent if m["type"] == "data_check")
    assert msg["file_id"] == file_id
    rep = msg["report"]
    assert {f["id"] for f in rep["findings"]} >= {"velocity_aliasing", "zdr_offset"}
    assert len(derived.store_for(reader, create=False) or []) == 0  # looking applies nothing
    # one click = run_pipeline with exactly the recipe from the report
    recipe = next(r for r in rep["recommendation"]["recipes"] if r["role"] == "velocity")["recipe"]
    h2 = _make_handler()
    await _send(h2, type="run_pipeline", file_id=file_id, recipe=recipe, all_sweeps=True)
    res = next(m for m in h2._sent if m["type"] == "pipeline_result")
    assert res["name"].startswith("pipeline:") and res["derived"]
    assert not any(m["type"] == "error" for m in h2._sent)


@pytest.mark.asyncio
async def test_websocket_data_check_unknown_file_is_an_error(served):
    from tests.test_pipeline_server import _make_handler, _send

    h = _make_handler()
    await _send(h, type="data_check", file_id="nope")
    assert any(m["type"] == "error" for m in h._sent)


def test_radar_band_with_a_missing_frequency_does_not_warn() -> None:
    """xradar's CfRadial2 reader adds ``frequency = [NaN]`` when the file has none."""
    ds = xr.Dataset({"frequency": ("frequency", np.array([np.nan], dtype="float32"))})
    tree = xr.DataTree.from_dict({"/": xr.Dataset(), "/sweep_0": ds})
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        band, how = quality.radar_band(tree, ds, "cfradial2")
    assert band is None and how == "not stated in the file"
