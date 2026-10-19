"""Tests for the optional ``radarx_*`` modules (modules/radarx_*.py).

Synthetic data with known features are used for the detection logic; the real NEXRAD KATX
volume is used for a smoke test when it is available. The whole file is skipped when the
``radarx`` package is not installed, except the "unavailable" test which fakes its absence.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from engine import plugins
from engine.plugins import ModuleError, run_module
from tests import radar_data

pytest.importorskip("radarx")

RADARX_IDS = [
    "radarx_couplets",
    "radarx_dealias",
    "radarx_echo_mask",
    "radarx_hail",
    "radarx_hid",
    "radarx_kdp",
    "radarx_qpe",
    "radarx_shear",
    "radarx_vil",
    "radarx_zdr_arc",
    "radarx_zdr_column",
]

N_AZ = 360
GATE_M = 250.0
RE_EFF = 4.0 / 3.0 * 6371e3
SITE = {"latitude": 35.0, "longitude": -97.0, "altitude": 100}
STORM_XY_KM = (20.0, 20.0)  # volume storm core, east/north of the radar

KATX = str(radar_data.katx()) if radar_data.katx() else None


# ---------------------------------------------------------------------------
# Fixtures: module discovery
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def discovered():
    """Discover the shipped modules; restore the global registry afterwards."""
    saved = (dict(plugins.MODULES), list(plugins.LOAD_ERRORS))
    plugins.discover_modules()
    yield
    plugins.MODULES.clear()
    plugins.MODULES.update(saved[0])
    plugins.LOAD_ERRORS[:] = saved[1]


def run(module_id, tree, sweep=0, params=None):
    """Run a module the way the server does and return ``(payload, fields)``."""

    def get(i):
        return tree[f"sweep_{i}"].to_dataset()  # like the reader: no site coords on sweeps

    return run_module(
        module_id, tree, sweep, params or {}, get_sweep_dataset=get, return_fields=True
    )


def block(payload, kind, title=None):
    found = [b for b in payload["blocks"] if b["type"] == kind]
    if title is not None:
        found = [b for b in found if title in b.get("title", "")]
    assert found, f"no {kind} block {title!r} in {[b['type'] for b in payload['blocks']]}"
    return found[0]


def scalar(payload, label):
    items = block(payload, "scalars")["items"]
    for item in items:
        if item["label"] == label:
            return item["value"]
    raise AssertionError(f"scalar {label!r} not in {[i['label'] for i in items]}")


# ---------------------------------------------------------------------------
# Synthetic data
# ---------------------------------------------------------------------------


def _geometry(azimuth, range_m, elevation_deg):
    """x, y (km east/north) and beam height (km above the radar) of every gate."""
    az = np.radians(azimuth)[:, None]
    r = range_m[None, :]
    el = np.radians(elevation_deg)
    s = r * np.cos(el)
    height = r * np.sin(el) + r**2 / (2 * RE_EFF)
    return s * np.sin(az) / 1e3, s * np.cos(az) / 1e3, height / 1e3


def _sweep(elevation, n_range, variables, fixed_angle=None):
    azimuth = np.arange(N_AZ) + 0.5
    range_m = GATE_M / 2 + GATE_M * np.arange(n_range)
    x, y, h = _geometry(azimuth, range_m, elevation)
    data = {
        name: (("azimuth", "range"), np.asarray(fn(x, y, h), dtype=np.float32))
        for name, fn in variables.items()
    }
    data["sweep_mode"] = ((), "azimuth_surveillance")
    data["sweep_fixed_angle"] = ((), float(elevation if fixed_angle is None else fixed_angle))
    return xr.Dataset(
        data,
        coords={
            "azimuth": ("azimuth", azimuth),
            "range": ("range", range_m.astype(np.float32)),
            "elevation": ("azimuth", np.full(N_AZ, elevation)),
            "time": (
                "azimuth",
                np.datetime64("2024-05-01T20:00:00") + np.arange(N_AZ).astype("timedelta64[ms]"),
            ),
        },
    )


def _tree(sweeps):
    nodes = {"/": xr.Dataset(coords={k: np.float64(v) for k, v in SITE.items()})}
    nodes.update({f"/sweep_{i}": ds for i, ds in enumerate(sweeps)})
    return xr.DataTree.from_dict(nodes)


def storm_volume(elevations=None, *, zdr_column=True):
    """15 sweeps over one convective core (radius 5 km) at 20 km east / 20 km north.

    * DBZH 50 dBZ from the ground to 9 km, NaN (no echo) elsewhere;
    * ZDR 2 dB between 3 km and 6.5 km above the radar (a ZDR column over a freezing level
      near 3 km), 0.3 dB elsewhere in the core; RHOHV 0.98.
    """
    elevations = elevations or [0.5, 1, 1.5, 2, 3, 4, 5, 6, 7, 8, 10, 12, 14, 16, 19.5]
    cx, cy = STORM_XY_KM

    def core(x, y):
        return np.hypot(x - cx, y - cy) < 5.0

    def dbzh(x, y, h):
        return np.where(core(x, y) & (h < 9.0), 50.0, np.nan)

    def zdr(x, y, h):
        col = (h >= 3.0) & (h < 6.5) if zdr_column else np.zeros_like(h, dtype=bool)
        return np.where(core(x, y) & (h < 9.0), np.where(col, 2.0, 0.3), np.nan)

    def rhohv(x, y, h):
        return np.where(core(x, y) & (h < 9.0), 0.98, np.nan)

    sweeps = []
    for i, el in enumerate(elevations):
        n_range = 400 if i % 2 == 0 else 360  # sweeps with different range counts
        sweeps.append(_sweep(el, n_range, {"DBZH": dbzh, "ZDR": zdr, "RHOHV": rhohv}))
    return _tree(sweeps)


def _gauss(x, y, cx, cy, sigma):
    return np.exp(-((x - cx) ** 2 + (y - cy) ** 2) / (2 * sigma**2))


def ppi_tree(*, keep_kdp=False, vortex=True, clutter=True):
    """One 0.5 degree PPI (100 km) with a storm, a ZDR arc, a KDP foot, a vortex and clutter.

    * storm: Gaussian blob, 55 dBZ peak, sigma 8 km at (30, 0) km (east of the radar);
    * ZDR arc: ZDR 4 dB crescent south of the storm centre, around (30, -12.5) km;
    * KDP foot: KDP 4 deg/km disc, radius 3 km, at (30, +4) km; PHIDP is built from KDP;
    * vortex: Rankine vortex (Vrot 25 m/s, core 1.5 km) at (-30, 30) km, Nyquist 30 m/s;
    * clutter: az 200-210 deg, 10-20 km: noisy 40 dBZ, RHOHV 0.5, noisy ZDR and PHIDP.
    """
    rng = np.random.default_rng(0)
    noise = rng.uniform(-3, 3, size=(N_AZ, 400))
    dbz_noise = rng.uniform(-10, 10, size=(N_AZ, 400))
    phase_noise = rng.uniform(0, 360, size=(N_AZ, 400))
    clutter_box = {"az": (200, 210), "r": (10e3, 20e3)}
    azimuth = np.arange(N_AZ) + 0.5
    range_m = GATE_M / 2 + GATE_M * np.arange(400)
    in_clutter = (
        (azimuth[:, None] >= clutter_box["az"][0])
        & (azimuth[:, None] < clutter_box["az"][1])
        & (range_m[None, :] >= clutter_box["r"][0])
        & (range_m[None, :] < clutter_box["r"][1])
        & clutter
    )

    def kdp_true(x, y, h):
        return np.where(np.hypot(x - 30, y - 4) < 3.0, 4.0, 0.0)

    def dbzh(x, y, h):
        z = 25.0 + 30.0 * _gauss(x, y, 30, 0, 8)
        return np.where(in_clutter, 40.0 + dbz_noise, np.where(np.hypot(x, y) < 95, z, np.nan))

    def zdr(x, y, h):
        d = np.hypot(x - 30, y + 7)
        ang = np.degrees(np.arctan2(y + 7, x - 30))
        arc = (d > 6) & (d < 8) & (ang > -150) & (ang < -30)
        out = np.where(arc, 4.0, 0.5)
        return np.where(in_clutter, noise, out)

    def rhohv(x, y, h):
        return np.where(in_clutter, 0.5, 0.98)

    def phidp(x, y, h):
        kdp = kdp_true(x, y, h)
        ramp = 30.0 + 2.0 * np.cumsum(kdp, axis=1) * GATE_M / 1e3
        return np.where(in_clutter, phase_noise, ramp)

    def vrad(x, y, h):
        vrot, core, nyq = 25.0, 1.5, 30.0
        dx, dy = x - (-30.0), y - 30.0
        rho = np.hypot(dx, dy)
        vt = np.where(rho < core, vrot * rho / core, vrot * core / np.maximum(rho, 1e-6))
        # counter-clockwise (cyclonic) tangential wind, projected on the beam (away = +)
        az = np.arctan2(x, y)
        ux, uy = np.sin(az), np.cos(az)
        tx, ty = -dy / np.maximum(rho, 1e-6), dx / np.maximum(rho, 1e-6)
        v = vt * (tx * ux + ty * uy)
        return np.where(np.hypot(dx, dy) < 12, v, np.nan).clip(-nyq, nyq)

    variables = {"DBZH": dbzh, "ZDR": zdr, "RHOHV": rhohv, "PHIDP": phidp}
    if vortex:
        variables["VRADH"] = vrad
    if keep_kdp:
        variables["KDP"] = kdp_true
    ds = _sweep(0.5, 400, variables)
    if vortex:
        ds["VRADH"].attrs["nyquist_velocity"] = 30.0
    return _tree([ds])


def vortex_volume(tilts=3, *, upper_only=False):
    """``tilts`` Doppler sweeps, each with the same Rankine vortex at (-30, 30) km."""
    sweeps = []
    for i, el in enumerate([0.5, 1.5, 2.5, 3.5, 4.5][:tilts]):
        keep = (not upper_only) or i == 0

        def vrad(x, y, h, keep=keep):
            dx, dy = x + 30.0, y - 30.0
            rho = np.hypot(dx, dy)
            vt = np.where(rho < 1.5, 25.0 * rho / 1.5, 25.0 * 1.5 / np.maximum(rho, 1e-6))
            az = np.arctan2(x, y)
            v = vt * ((-dy * np.sin(az) + dx * np.cos(az)) / np.maximum(rho, 1e-6))
            return np.where((rho < 12) & keep, v, np.nan).clip(-30, 30)

        def dbzh(x, y, h):
            return np.where(np.hypot(x + 30.0, y - 30.0) < 12, 40.0, np.nan)

        sweep = _sweep(el, 400, {"VRADH": vrad, "DBZH": dbzh})
        sweep["VRADH"].attrs["nyquist_velocity"] = 30.0
        sweeps.append(sweep)
    return _tree(sweeps)


def aliased_wind_tree(*, speed=40.0, nyquist=25.0):
    """Uniform wind blowing toward the east (from 270) folded into +-``nyquist``."""

    def true_v(x, y, h):
        az = np.arctan2(x, y)
        return speed * np.sin(az)

    def folded(x, y, h):
        v = true_v(x, y, h)
        return (v + nyquist) % (2 * nyquist) - nyquist

    def dbzh(x, y, h):
        return np.where(np.hypot(x, y) < 95, 30.0, np.nan)

    ds = _sweep(0.5, 400, {"VRADH": folded, "DBZH": dbzh})
    ds["VRADH"].attrs["nyquist_velocity"] = nyquist
    return _tree([ds]), true_v(*_geometry(ds["azimuth"].values, ds["range"].values, 0.5))


def rain_tree(*, dbz=40.0, zdr=1.0, rhohv=0.99, kdp=1.0):
    """Uniform moderate rain, 0-60 km, with a PHIDP ramp that has a constant KDP."""

    def inside(x, y):
        return np.hypot(x, y) < 60

    ds = _sweep(
        0.5,
        400,
        {
            "DBZH": lambda x, y, h: np.where(inside(x, y), dbz, np.nan),
            "ZDR": lambda x, y, h: np.where(inside(x, y), zdr, np.nan),
            "RHOHV": lambda x, y, h: np.where(inside(x, y), rhohv, np.nan),
            "PHIDP": lambda x, y, h: np.where(
                inside(x, y), 20.0 + 2.0 * kdp * np.hypot(x, y), np.nan
            ),
        },
    )
    return _tree([ds])


# ---------------------------------------------------------------------------
# Registration and availability
# ---------------------------------------------------------------------------


def test_all_radarx_modules_load_and_are_declared():
    assert plugins.LOAD_ERRORS == []
    for mid in RADARX_IDS:
        spec = plugins.MODULES[mid]
        assert spec.needs == ["radarx"], mid
        assert spec.category in {
            "Storm",
            "Dual-pol",
            "Hazards",
            "QC",
            "Precipitation",
            "Kinematics",
        }
        assert spec.description and len(spec.description) > 40, mid
        assert spec.missing_packages() == [], mid
        for p in spec.params:
            if p["type"] in ("number", "int"):
                assert "min" in p and "max" in p, (mid, p["name"])
                assert p["min"] <= p["default"] <= p["max"], (mid, p["name"])
            if p["type"] == "select":
                assert p["default"] in p["options"], (mid, p["name"])


def test_modules_unavailable_without_radarx(monkeypatch):
    real = importlib.util.find_spec

    def fake(name, *args, **kwargs):
        return None if name == "radarx" else real(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake)
    listing = {m["id"]: m for m in plugins.list_modules()["modules"]}
    for mid in RADARX_IDS:
        info = listing[mid]
        assert info["available"] is False, mid
        assert "radarx" in info["unavailable_reason"]
        with pytest.raises(ModuleError, match="radarx"):
            run(mid, storm_volume(), 0)
    # modules that do not need radarx are unaffected
    assert listing["zdr_arc"]["available"] is True


def test_importing_the_modules_does_not_import_radarx():
    """A module file must load without radarx (it is only imported inside the functions)."""
    code = (
        "import sys, importlib.abc\n"
        "class Block(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'radarx' or name.startswith('radarx.'):\n"
        "            raise ImportError('blocked')\n"
        "sys.meta_path.insert(0, Block())\n"
        "from engine import plugins\n"
        "plugins.discover_modules()\n"
        "ids = sorted(m for m in plugins.MODULES if m.startswith('radarx_'))\n"
        "assert len(ids) >= 10, ids\n"
        "assert not plugins.LOAD_ERRORS, plugins.LOAD_ERRORS\n"
        "assert 'radarx' not in sys.modules\n"
    )
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=root, capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stderr


# ---------------------------------------------------------------------------
# Per-sweep modules
# ---------------------------------------------------------------------------


def test_echo_mask_separates_clutter_from_rain():
    payload, fields = run("radarx_echo_mask", ppi_tree(), 0, {"include_score": True})
    cls = fields["Echo class"]
    assert cls.shape == (N_AZ, 400)
    block(payload, "field", "Echo class")
    cats = {c["value"] for c in block(payload, "field", "Echo class")["categories"]}
    assert cats == {1, 2, 3}
    az = np.arange(N_AZ) + 0.5
    clutter = cls[(az >= 200) & (az < 210)][:, 40:80]  # 10-20 km
    storm = cls[:5, :][:, :]  # azimuth ~0-5 deg, rain only
    assert np.nanmean(clutter == 2) > 0.5, "clutter patch should be non-meteorological"
    assert np.nanmean(storm[np.isfinite(storm)] == 1) > 0.7
    assert "Meteorological score" in fields
    assert scalar(payload, "Meteorological") > scalar(payload, "Non-meteorological")


def test_echo_mask_krause_method_runs():
    payload, fields = run("radarx_echo_mask", ppi_tree(), 0, {"method": "krause2016"})
    assert fields["Echo class"].shape == (N_AZ, 400)
    assert scalar(payload, "Gates with echo") > 0


def test_kdp_recovers_constant_kdp():
    payload, fields = run("radarx_kdp", rain_tree(kdp=1.0), 0)
    kdp = fields["KDP (radarx)"]
    assert kdp.shape == (N_AZ, 400)
    interior = kdp[:, 40:200]  # 10-50 km
    assert np.isfinite(interior).mean() > 0.9
    assert abs(np.nanmedian(interior) - 1.0) < 0.15
    assert scalar(payload, "Gates with KDP") > 10000
    # alternative outputs share the grid
    for out in ("PHIDP_processed", "KDP_UNCERTAINTY"):
        _, f = run("radarx_kdp", rain_tree(), 0, {"output": out, "method": "monotone"})
        assert next(iter(f.values())).shape == (N_AZ, 400)


def test_kdp_requires_phidp():
    tree = _tree([_sweep(0.5, 100, {"DBZH": lambda x, y, h: np.full_like(x, 30.0)})])
    with pytest.raises(ModuleError, match="PHIDP"):
        run("radarx_kdp", tree, 0)


def test_dealias_unfolds_with_reference_wind():
    tree, truth = aliased_wind_tree()
    payload, fields = run("radarx_dealias", tree, 0, {"wind_speed": 40, "wind_from": 270})
    out = fields["Dealiased velocity (radarx)"]
    assert out.shape == truth.shape
    ok = np.isfinite(out)
    assert ok.mean() > 0.9
    assert np.mean(np.abs(out[ok] - truth[ok]) < 1.0) > 0.97
    assert scalar(payload, "Nyquist source") == "file"
    assert scalar(payload, "Nyquist velocity") == 25.0
    assert scalar(payload, "Gates unfolded") > 1000
    # fold map has integer folds
    _, folds = run("radarx_dealias", tree, 0, {"output": "fold_number", "wind_speed": 40})
    arr = next(iter(folds.values()))
    assert set(np.unique(arr[np.isfinite(arr)])) <= {-1.0, 0.0, 1.0}


def test_dealias_estimates_nyquist_when_missing():
    tree = _tree([_sweep(0.5, 200, {"VRADH": lambda x, y, h: 15.0 * np.sin(np.arctan2(x, y))})])
    payload, _ = run("radarx_dealias", tree, 0)
    assert scalar(payload, "Nyquist source").startswith("estimated")
    assert scalar(payload, "Nyquist velocity") == pytest.approx(15.0, abs=0.5)


def test_shear_finds_the_vortex_and_signs():
    tree = ppi_tree()
    payload, fields = run("radarx_shear", tree, 0, {"dealias_first": False})
    shear = fields["Azimuthal shear (radarx)"]
    assert shear.shape == (N_AZ, 400)
    ia, ir = np.unravel_index(np.nanargmax(shear), shear.shape)
    az, rng = ia + 0.5, 125 + 250 * ir
    assert abs(az - 315.0) < 5.0 and abs(rng - 42426.0) < 3000.0, (az, rng)
    assert np.nanmax(shear) > 0.006  # cyclonic, well above the usual threshold
    assert scalar(payload, "Max (positive)") > scalar(payload, "Min (negative)")
    # radial divergence is available as well and has the same shape
    _, f2 = run("radarx_shear", tree, 0, {"output": "radial_divergence", "dealias_first": False})
    assert f2["Radial divergence (radarx)"].shape == (N_AZ, 400)


def test_couplets_detect_one_cyclonic_vortex():
    payload, _ = run("radarx_couplets", ppi_tree(), 0, {"dealias_first": False})
    table = block(payload, "table")
    assert len(table["rows"]) >= 1
    top = table["rows"][0]
    assert abs(top["azimuth"] - 315.0) < 5.0
    assert abs(top["range_km"] - 42.4) < 3.0
    assert top["vrot"] > 10.0 and top["shear"] > 0.006
    overlay = block(payload, "overlay")
    assert overlay["features"][0]["kind"] == "point"
    assert overlay["features"][0]["frame"] == "polar"
    # no anticyclonic vortex
    anti, _ = run("radarx_couplets", ppi_tree(), 0, {"rotation": "anticyclonic"})
    assert scalar(anti, "Couplets") == 0


def test_couplets_vertical_continuity():
    tree = vortex_volume(3)
    found, _ = run("radarx_couplets", tree, 0, {"min_tilts": 3, "dealias_first": False})
    rows = block(found, "table")["rows"]
    assert rows and rows[0]["n_tilts"] >= 3
    # a vortex on one tilt only is not vertically continuous
    only_low = vortex_volume(3, upper_only=True)
    none, _ = run("radarx_couplets", only_low, 0, {"min_tilts": 3, "dealias_first": False})
    assert scalar(none, "Couplets") == 0
    with pytest.raises(ModuleError, match="tilts"):
        run("radarx_couplets", vortex_volume(2), 0, {"min_tilts": 6, "dealias_first": False})


def test_hid_classifies_moderate_rain():
    payload, fields = run("radarx_hid", rain_tree(), 0, {"use_echo_mask": False})
    cls = fields["Hydrometeor class (radarx)"]
    assert cls.shape == (N_AZ, 400)
    cats = {c["label"]: c for c in block(payload, "field")["categories"]}
    assert "Light and moderate rain" in cats
    assert all(c["color"].startswith("#") for c in cats.values())
    values = np.unique(cls[np.isfinite(cls)])
    assert set(values) <= {c["value"] for c in cats.values()}
    rain = cats["Light and moderate rain"]["value"]
    assert np.mean(cls[np.isfinite(cls)] == rain) > 0.8
    assert scalar(payload, "Most common class") == "Light and moderate rain"
    rows = block(payload, "table")["rows"]
    assert rows[0]["class"] == "Light and moderate rain" and rows[0]["share"] > 80


def test_hid_other_methods_run():
    for params in ({"method": "dolan", "band": "C"}, {"method": "park", "freezing_level_m": 3000}):
        payload, fields = run("radarx_hid", rain_tree(), 0, params)
        assert fields["Hydrometeor class (radarx)"].shape == (N_AZ, 400)
        assert scalar(payload, "Classified gates") > 0


def test_hid_requires_zdr():
    tree = _tree([_sweep(0.5, 100, {"DBZH": lambda x, y, h: np.full_like(x, 30.0)})])
    with pytest.raises(ModuleError, match="ZDR"):
        run("radarx_hid", tree, 0)


def test_qpe_reflectivity_relation_matches_the_formula():
    payload, fields = run(
        "radarx_qpe", rain_tree(dbz=40.0), 0, {"relation": "z", "use_echo_mask": False}
    )
    rate = fields["Rain rate (radarx)"]
    assert rate.shape == (N_AZ, 400)
    expected = 0.017 * (10 ** (40.0 / 10)) ** 0.714  # S-band WSR-88D relation
    assert np.nanmedian(rate) == pytest.approx(expected, rel=0.02)
    assert scalar(payload, "Max rain rate") == pytest.approx(expected, rel=0.02)


def test_qpe_other_relations_and_blend():
    for relation in ("z_zdr", "kdp", "hid_blend"):
        payload, fields = run(
            "radarx_qpe", rain_tree(), 0, {"relation": relation, "use_echo_mask": False}
        )
        rate = fields["Rain rate (radarx)"]
        assert rate.shape == (N_AZ, 400)
        assert np.isfinite(rate).sum() > 1000, relation
        assert scalar(payload, "Max rain rate") > 1.0
    # KDP = 1 deg/km -> 44 * 1^0.822 = 44 mm/h in the interior
    _, f = run("radarx_qpe", rain_tree(kdp=1.0), 0, {"relation": "kdp", "use_echo_mask": False})
    assert np.nanmedian(f["Rain rate (radarx)"][:, 40:200]) == pytest.approx(44.0, rel=0.25)


def test_qpe_c_band_reflectivity_has_no_default():
    with pytest.raises(ModuleError, match="C band"):
        run("radarx_qpe", rain_tree(), 0, {"relation": "z", "band": "C"})


def test_qpe_auto_relation_follows_available_fields():
    tree = _tree([_sweep(0.5, 100, {"DBZH": lambda x, y, h: np.full_like(x, 35.0)})])
    payload, _ = run("radarx_qpe", tree, 0, {"use_echo_mask": False})
    assert scalar(payload, "Relation") == "R(Z) = a Z^b"
    full, _ = run("radarx_qpe", rain_tree(), 0, {"use_echo_mask": False})
    assert "hydrometeor class" in scalar(full, "Relation")


def test_alias_variable_names_are_understood():
    """Files with DBZ / differential_reflectivity / ... names work like canonical ones."""
    ds = (
        rain_tree()["sweep_0"]
        .to_dataset()
        .rename(
            {
                "DBZH": "DBZ",
                "ZDR": "differential_reflectivity",
                "RHOHV": "cross_correlation_ratio",
                "PHIDP": "differential_phase",
            }
        )
    )
    tree = _tree([ds])
    for module_id in ("radarx_echo_mask", "radarx_kdp", "radarx_hid", "radarx_qpe"):
        _, fields = run(module_id, tree, 0, {})
        assert next(iter(fields.values())).shape == (N_AZ, 400), module_id


# ---------------------------------------------------------------------------
# Gridded / object products
# ---------------------------------------------------------------------------


def test_zdr_arc_and_kdp_separation():
    tree = ppi_tree(keep_kdp=True, vortex=False)
    payload, fields = run(
        "radarx_zdr_arc",
        tree,
        0,
        {"ffd_direction": 0, "storm_motion": 0, "zdr_threshold": 3.25, "max_range_km": 90},
    )
    shown = fields["ZDR arc cells (radarx)"]
    assert shown.shape == (N_AZ, 400)
    assert scalar(payload, "Storm objects") >= 1
    rows = block(payload, "table")["rows"]
    storm = max(rows, key=lambda r: r["storm_area_km2"])
    assert abs(storm["azimuth"] - 90.0) < 4.0 and abs(storm["range_km"] - 30.0) < 4.0
    assert storm["arc_mean_zdr"] == pytest.approx(4.0, abs=0.3)
    assert storm["arc_area_km2"] > 5.0
    # KDP foot at (30, +4) km, arc centroid near (30, -12.5) km: the separation vector points
    # from the foot to the arc (south) and is about 16.5 km long
    assert 14.0 < storm["separation_km"] < 19.0
    assert storm["separation_angle"] > 150.0  # storm motion 0 (north) vs vector south
    # overlay uses the x/y frame; the sampled map is finite only near the arc
    overlay = block(payload, "overlay")
    frames = {f["frame"] for f in overlay["features"]}
    assert frames == {"xy"}
    az = np.arange(N_AZ) + 0.5
    near = np.isfinite(shown[(az > 95) & (az < 112)][:, 100:140]).sum()  # 25-35 km
    far = np.isfinite(shown[(az > 250) & (az < 300)]).sum()
    assert near > 0 and far == 0
    assert np.nanmin(shown[np.isfinite(shown)]) >= 3.25


def test_zdr_arc_without_kdp_still_reports_arcs():
    tree = ppi_tree(keep_kdp=False, vortex=False)
    # drop PHIDP so that KDP cannot be estimated either
    tree = _tree([tree["sweep_0"].to_dataset().drop_vars("PHIDP")])
    payload, _ = run("radarx_zdr_arc", tree, 0, {"ffd_direction": 0})
    assert scalar(payload, "Storm objects") >= 1
    assert all(r["separation_km"] is None for r in block(payload, "table")["rows"])
    assert "No KDP" in block(payload, "text", "About")["body"]


def test_zdr_column_depth_and_objects():
    tree = storm_volume()
    payload, fields = run("radarx_zdr_column", tree, 0, {"freezing_level_m": 3100, "min_dbz": 20})
    depth = fields["ZDR column depth (radarx)"]
    assert depth.shape == (N_AZ, 400)
    finite = depth[np.isfinite(depth)]
    assert finite.size > 20
    assert 1000 <= finite.max() <= 4500  # ~3.4 km from 3.1 km to 6.5 km
    # the column gates lie over the storm core (az 45 deg, range 28 km)
    ia, ir = np.nonzero(np.isfinite(depth))
    assert abs(np.mean(ia + 0.5) - 45.0) < 8.0
    assert abs(np.mean((125 + 250 * ir) / 1e3) - 28.3) < 7.0
    rows = block(payload, "table")["rows"]
    assert rows and abs(rows[0]["azimuth"] - 45.0) < 6.0 and rows[0]["max_depth_km"] > 1.0
    overlay = block(payload, "overlay")
    assert overlay["features"][0]["frame"] == "xy"
    assert scalar(payload, "Max column depth") == pytest.approx(finite.max(), abs=300)
    # selecting another sweep gives that sweep's grid
    _, f3 = run("radarx_zdr_column", tree, 3, {"freezing_level_m": 3100})
    assert next(iter(f3.values())).shape == (N_AZ, 360 if 3 % 2 else 400)
    # no column when ZDR is low everywhere
    flat, flat_fields = run(
        "radarx_zdr_column", storm_volume(zdr_column=False), 0, {"freezing_level_m": 3100}
    )
    assert scalar(flat, "Max column depth") == 0.0
    assert not np.isfinite(flat_fields["ZDR column depth (radarx)"]).any()


def test_zdr_column_needs_a_volume():
    with pytest.raises(ModuleError, match="several sweeps"):
        run("radarx_zdr_column", ppi_tree(), 0)


def test_vil_and_echo_top_over_the_storm():
    tree = storm_volume()
    cx, cy = STORM_XY_KM
    exp_az = float(np.degrees(np.arctan2(cx, cy)))  # 45 deg
    _, fields = run("radarx_vil", tree, 0)
    vil = fields["VIL (radarx)"]
    assert vil.shape == (N_AZ, 400)
    assert 8.0 < np.nanmax(vil) < 45.0
    ia, ir = np.unravel_index(np.nanargmax(vil), vil.shape)
    assert abs((ia + 0.5) - exp_az) < 8.0
    assert abs((125 + 250 * ir) / 1e3 - 28.3) < 7.0
    # outside the storm there is no VIL (zero VIL is masked)
    assert not np.isfinite(vil[200:260]).any()
    # a different sweep (different range count) is mapped onto its own grid
    _, f1 = run("radarx_vil", tree, 1)
    assert f1["VIL (radarx)"].shape == (N_AZ, 360)
    assert np.nanmax(f1["VIL (radarx)"]) > 8.0
    # echo top ~ 9 km above the sea level offset of the radar (km)
    top_payload, top = run("radarx_vil", tree, 0, {"product": "echo_top"})
    arr = top["Echo top (radarx)"]
    assert 6.0 < np.nanmax(arr) < 12.0
    assert block(top_payload, "field")["units"] == "km"
    _, d = run("radarx_vil", tree, 0, {"product": "vil_density"})
    assert d["VIL density (radarx)"].shape == (N_AZ, 400)


def test_vil_needs_a_volume():
    with pytest.raises(ModuleError, match="several sweeps"):
        run("radarx_vil", ppi_tree(), 0)


def test_hail_products_over_the_storm():
    tree = storm_volume()
    params = {"freezing_level_m": 3000, "minus20_level_m": 6500, "min_mesh_mm": 10}
    payload, fields = run("radarx_hail", tree, 0, params)
    mesh = fields["MESH (radarx)"]
    assert mesh.shape == (N_AZ, 400)
    assert np.nanmax(mesh) > 10.0
    assert scalar(payload, "Max MESH") >= np.nanmax(mesh) - 1e-3
    assert 0.0 <= scalar(payload, "Max POSH") <= 100.0
    rows = block(payload, "table")["rows"]
    assert rows and abs(rows[0]["azimuth"] - 45.0) < 8.0
    assert abs(rows[0]["range_km"] - 28.3) < 7.0
    features = block(payload, "overlay")["features"]
    assert features and all(f["frame"] == "polar" for f in features)
    # other products and another sweep
    for product in ("SHI", "POSH", "HKE"):
        _, f = run("radarx_hail", tree, 3, {**params, "product": product})
        assert next(iter(f.values())).shape == (N_AZ, 360)
    # a higher freezing level leaves less of the column above it: smaller hail
    high, _ = run("radarx_hail", tree, 0, {"freezing_level_m": 7000, "minus20_level_m": 11000})
    assert scalar(high, "Max MESH") < scalar(payload, "Max MESH")


def test_hail_validates_levels():
    with pytest.raises(ModuleError, match="-20"):
        run("radarx_hail", storm_volume(), 0, {"freezing_level_m": 5000, "minus20_level_m": 4000})
    with pytest.raises(ModuleError, match="several sweeps"):
        run("radarx_hail", ppi_tree(), 0)


# ---------------------------------------------------------------------------
# Adapter helpers
# ---------------------------------------------------------------------------


def _adapter():
    """The shared adapter module (loaded by path when the modules were discovered)."""
    return sys.modules["xradar_module_radarx_adapter"]


def test_adapter_remap_polar_identity_and_shift():
    adapter = _adapter()
    tree = storm_volume()
    ds = tree["sweep_0"].to_dataset().assign_coords(**{k: np.float64(v) for k, v in SITE.items()})
    az = ds["azimuth"].values
    ground = adapter.ground_range_m(ds["range"].values, 0.5, SITE["altitude"])
    values = np.random.default_rng(1).random((az.size, ds.sizes["range"])).astype(np.float32)
    same = adapter.remap_polar(values, az, ground, ds)
    assert np.array_equal(same, values)
    # a shorter target sweep takes the leading gates (same elevation => same ground range)
    short = (
        tree["sweep_1"].to_dataset().assign_coords(**{k: np.float64(v) for k, v in SITE.items()})
    )
    short = short.assign_coords(elevation=("azimuth", np.full(az.size, 0.5)))
    out = adapter.remap_polar(values, az, ground, short)
    assert out.shape == (az.size, short.sizes["range"])
    assert np.allclose(out[:, :50], values[:, :50])


def test_adapter_cartesian_roundtrip():
    adapter = _adapter()
    ds = ppi_tree()["sweep_0"].to_dataset()
    field = ds["DBZH"].values
    grid = adapter.sweep_to_cartesian({"DBZH": field}, ds, 500.0, 90e3)
    x, y = grid["x"].values, grid["y"].values
    # the storm at (30, 0) km: the grid maximum is within 2 km of it
    arr = grid["DBZH"].values
    iy, ix = np.unravel_index(np.nanargmax(arr), arr.shape)
    assert abs(x[ix] - 30e3) < 2500 and abs(y[iy]) < 2500
    back = adapter.sample_cartesian(arr, x, y, ds, flat=True)
    inside = np.isfinite(back) & np.isfinite(field)
    assert inside.sum() > 50000
    assert np.nanmedian(np.abs(back[inside] - field[inside])) < 1.0


# ---------------------------------------------------------------------------
# Real data: NEXRAD KATX (dual-pol, 16 sweeps; split cuts)
# ---------------------------------------------------------------------------

# module -> sweep to run it on: sweep 0 = surveillance cut (DBZH/ZDR/RHOHV/PHIDP),
# sweep 1 = Doppler cut of the same elevation (VRADH), sweep 5 = full-field cut.
KATX_RUNS = [
    ("radarx_echo_mask", 0),
    ("radarx_kdp", 0),
    ("radarx_hid", 0),
    ("radarx_qpe", 0),
    ("radarx_zdr_arc", 0),
    ("radarx_zdr_column", 0),
    ("radarx_vil", 0),
    ("radarx_hail", 0),
    ("radarx_dealias", 1),
    ("radarx_shear", 1),
    ("radarx_couplets", 1),
    ("radarx_hid", 5),
    ("radarx_vil", 5),
]


@pytest.fixture(scope="module")
def katx():
    from engine.reader import RadarReader

    reader = RadarReader()
    reader.open_file(KATX)
    return reader


@pytest.mark.skipif(KATX is None, reason="KATX20130717_195021_V06 not available")
@pytest.mark.parametrize(("module_id", "sweep"), KATX_RUNS)
def test_katx_modules_run(katx, module_id, sweep):
    payload, fields = run_module(
        module_id,
        katx.datatree,
        sweep,
        {},
        get_sweep_dataset=katx.get_sweep_dataset,
        return_fields=True,
    )
    shape = (
        katx.get_sweep_dataset(sweep).sizes["azimuth"],
        katx.get_sweep_dataset(sweep).sizes["range"],
    )
    assert payload["elapsed_ms"] < 10_000
    json.dumps(payload, allow_nan=False)  # strictly JSON-safe
    assert payload["blocks"]
    for name, arr in fields.items():
        assert arr.shape == shape, name
        assert arr.dtype == np.float32
    for b in payload["blocks"]:
        if b["type"] == "field":
            assert tuple(b["shape"]) == shape


@pytest.mark.skipif(KATX is None, reason="KATX20130717_195021_V06 not available")
def test_katx_missing_fields_give_module_errors(katx):
    def go(module_id, sweep):
        return run_module(
            module_id, katx.datatree, sweep, {}, get_sweep_dataset=katx.get_sweep_dataset
        )

    # the surveillance cut has no velocity, but the paired Doppler cut (same elevation) does:
    # dealiasing finds it there and says so
    res = go("radarx_dealias", 0)
    assert any("sweep 1" in n for n in res.get("notes", [])), res.get("notes")
    with pytest.raises(ModuleError, match="ZDR"):
        go("radarx_hid", 1)  # Doppler cut has no dual-pol
