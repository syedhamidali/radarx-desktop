"""Tests for the second batch of ``radarx_*`` modules.

KDP column, VVP/VAD winds, beam blockage, tornado probability, melting layer, QVP and ZDR
calibration. Synthetic data with known features check the logic; the real KTLX (NEXRAD,
20 May 2013) and COW (CfRadial) volumes are used for smoke tests when the files exist. Tests of
functions that the installed radarx lacks (``kdp_column``, ``vvp_winds``,
``melting_layer_gradient``) are skipped, never faked.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from engine import derived, plugins
from engine.plugins import ModuleError, run_module

pytest.importorskip("radarx")
import radarx.retrieve as rxr

from tests.test_radarx_modules import (
    GATE_M,
    N_AZ,
    SITE,
    _sweep,
    _tree,
    block,
    ppi_tree,
    rain_tree,
    run,
    scalar,
    storm_volume,
    vortex_volume,
)

pytestmark = pytest.mark.filterwarnings("ignore")

EXTRA_IDS = [
    "radarx_kdp_column",
    "radarx_vvp_winds",
    "radarx_beam_blockage",
    "radarx_tornado_prob",
    "radarx_melting_layer",
    "radarx_qvp",
    "radarx_zdr_calibration",
]
CATEGORIES = {"Storm", "Dual-pol", "Hazards", "QC", "Precipitation", "Kinematics"}

needs_kdp_column = pytest.mark.skipif(
    not hasattr(rxr, "kdp_column"), reason="installed radarx has no kdp_column"
)
needs_vvp = pytest.mark.skipif(
    not hasattr(rxr, "vvp_winds"), reason="installed radarx has no vvp_winds"
)
needs_ml_gradient = pytest.mark.skipif(
    not hasattr(rxr, "melting_layer_gradient"),
    reason="installed radarx has no melting_layer_gradient",
)

KTLX = next(
    (
        p
        for p in (
            os.environ.get("XRADAR_TEST_KTLX", ""),
            str(Path.home() / "Downloads/Others/XRADAR_EXAMPLES/KTLX20130520_201643_V06.gz"),
        )
        if p and os.path.exists(p)
    ),
    None,
)
COW = next(
    (
        p
        for p in (
            os.environ.get("XRADAR_TEST_COW", ""),
            "/Users/syed44/Downloads/Others/XRADAR_EXAMPLES/RADAR_CORRECTED/"
            "cfrad.20220322_220338.299_to_20220322_220506.977_COW1high_SUR.nc",
        )
        if p and os.path.exists(p)
    ),
    None,
)


@pytest.fixture(scope="module", autouse=True)
def discovered():
    """Discover the shipped modules; restore the global registry afterwards."""
    saved = (dict(plugins.MODULES), list(plugins.LOAD_ERRORS))
    plugins.discover_modules()
    yield
    plugins.MODULES.clear()
    plugins.MODULES.update(saved[0])
    plugins.LOAD_ERRORS[:] = saved[1]


def check_json(payload):
    json.dumps(payload, allow_nan=False)  # strictly JSON-safe


# ---------------------------------------------------------------------------
# Synthetic volumes
# ---------------------------------------------------------------------------

ALT = float(SITE["altitude"])
ML_BOTTOM, ML_TOP = 2300.0, 3100.0  # m ASL


def stratiform_volume(elevations=None, *, bright_band=True, n_range=400):
    """Horizontally uniform stratiform precipitation with a bright band at 2.3-3.1 km ASL.

    Z 30 dBZ (36 in the layer, 25 above), ZDR 0.5 dB (1.5 in the layer, 0.2 above), RhoHV
    0.985 (0.93 in the layer, 0.99 above). Same fields on every sweep, a function of height.
    """
    elevations = elevations or [0.5, 1.5, 3.0, 4.5, 6.0, 8.0, 10.0, 14.0, 19.5]

    def asl(h):
        return h * 1000.0 + ALT

    def inside(h):
        return (asl(h) >= ML_BOTTOM) & (asl(h) <= ML_TOP) & bright_band

    def dbz(x, y, h):
        return np.broadcast_to(
            np.where(inside(h), 36.0, np.where(asl(h) > ML_TOP, 25.0, 30.0)), x.shape
        )

    def zdr(x, y, h):
        return np.broadcast_to(
            np.where(inside(h), 1.5, np.where(asl(h) > ML_TOP, 0.2, 0.5)), x.shape
        )

    def rho(x, y, h):
        return np.broadcast_to(
            np.where(inside(h), 0.93, np.where(asl(h) > ML_TOP, 0.99, 0.985)), x.shape
        )

    return _tree(
        [
            _sweep(el, n_range, {"DBZH": dbz, "ZDR": zdr, "RHOHV": rho}, fixed_angle=el)
            for el in elevations
        ]
    )


def wind_volume(elevations=None):
    """Doppler volume of a horizontally uniform wind u = 5 + 2 h (m/s, h km above the radar),
    v = -3 m/s, echo everywhere (dBZ 30). Within +-30 m/s so nothing folds."""
    elevations = elevations or [0.5, 1.5, 2.5, 3.5, 4.5, 6.0, 8.0, 10.0]

    def make(el):
        def vrad(x, y, h):
            u, v = 5.0 + 2.0 * h, -3.0 + 0.0 * h
            az = np.arctan2(x, y)
            return np.cos(np.radians(el)) * (u * np.sin(az) + v * np.cos(az))

        def dbz(x, y, h):
            return np.full_like(x, 30.0)

        sweep = _sweep(el, 400, {"VRADH": vrad, "DBZH": dbz})
        sweep["VRADH"].attrs["nyquist_velocity"] = 30.0
        return sweep

    return _tree([make(el) for el in elevations])


def wind_at(h_km):
    u, v = 5.0 + 2.0 * h_km, -3.0
    return float(np.hypot(u, v)), float(np.degrees(np.arctan2(-u, -v)) % 360.0)


def kdp_volume(*, with_kdp=True, column=True):
    """Storm core (radius 5 km at 20 km E / 20 km N) with KDP 2 deg/km from 3.2 to 7 km ASL."""
    elevations = [0.5, 1, 1.5, 2, 3, 4, 5, 6, 7, 8, 10, 12, 14, 16, 19.5]
    cx, cy = 20.0, 20.0

    def core(x, y):
        return np.hypot(x - cx, y - cy) < 5.0

    def dbz(x, y, h):
        return np.where(core(x, y) & (h < 9.0), 50.0, np.nan)

    def rho(x, y, h):
        return np.where(core(x, y) & (h < 9.0), 0.98, np.nan)

    def kdp(x, y, h):
        col = (h * 1000.0 + ALT >= 3200.0) & (h * 1000.0 + ALT < 7000.0) & column
        return np.where(core(x, y) & (h < 9.0), np.where(col, 2.0, 0.0), np.nan)

    variables = {"DBZH": dbz, "RHOHV": rho}
    if with_kdp:
        variables["KDP"] = kdp
    return _tree(
        [_sweep(el, 400 if i % 2 == 0 else 360, variables) for i, el in enumerate(elevations)]
    )


def write_ridge_dem(path: Path, *, fmt="nc"):
    """DEM with a N-S ridge (1200 m, 6 km wide) about 18 km east of the synthetic radar."""
    lat = np.round(np.arange(34.0, 36.0001, 0.01), 4)
    lon = np.round(np.arange(-98.2, -95.7999, 0.01), 4)
    ridge_lon = SITE["longitude"] + 0.2
    terrain = 1200.0 * np.exp(-(((lon - ridge_lon) / 0.03) ** 2))
    grid = np.tile(terrain, (lat.size, 1))
    if fmt == "nc":
        xr.DataArray(
            grid, dims=("lat", "lon"), coords={"lat": lat, "lon": lon}, name="elevation"
        ).to_dataset().to_netcdf(path)
    else:
        with open(path, "w") as fh:
            fh.write(f"ncols {lon.size}\nnrows {lat.size}\n")
            fh.write(f"xllcorner {lon[0] - 0.005}\nyllcorner {lat[0] - 0.005}\n")
            fh.write("cellsize 0.01\nNODATA_value -9999\n")
            for row in grid[::-1]:
                fh.write(" ".join(f"{v:.1f}" for v in row) + "\n")
    return path


# ---------------------------------------------------------------------------
# Registration and availability
# ---------------------------------------------------------------------------


def test_extra_modules_load_and_are_declared():
    assert plugins.LOAD_ERRORS == []
    listing = {m["id"]: m for m in plugins.list_modules()["modules"]}
    for mid in EXTRA_IDS:
        spec = plugins.MODULES[mid]
        assert spec.needs == ["radarx"], mid
        assert spec.category in CATEGORIES, mid
        assert spec.description and len(spec.description) > 40, mid
        assert listing[mid]["available"] is True, mid
        assert spec.missing_packages() == [], mid
        for p in spec.params:
            assert p.get("help") or p["type"] == "bool" or p["name"] in {"method"}, (mid, p)
            if p["type"] in ("number", "int"):
                assert "min" in p and "max" in p, (mid, p["name"])
                assert p["min"] <= p["default"] <= p["max"], (mid, p["name"])
            if p["type"] == "select":
                assert p["default"] in p["options"], (mid, p["name"])
    volume = {
        "radarx_kdp_column",
        "radarx_vvp_winds",
        "radarx_tornado_prob",
        "radarx_melting_layer",
        "radarx_qvp",
    }
    for mid in EXTRA_IDS:
        assert plugins.MODULES[mid].scope == ("volume" if mid in volume else "sweep"), mid


def test_extra_modules_unavailable_without_radarx(monkeypatch):
    real = importlib.util.find_spec

    def fake(name, *args, **kwargs):
        return None if name == "radarx" else real(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake)
    listing = {m["id"]: m for m in plugins.list_modules()["modules"]}
    for mid in EXTRA_IDS:
        assert listing[mid]["available"] is False, mid
        assert "radarx" in listing[mid]["unavailable_reason"]
        with pytest.raises(ModuleError, match="radarx"):
            run(mid, storm_volume(), 0)


def test_importing_the_extra_modules_does_not_import_radarx():
    code = (
        "import sys, importlib.abc\n"
        "class Block(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'radarx' or name.startswith('radarx.'):\n"
        "            raise ImportError('blocked')\n"
        "sys.meta_path.insert(0, Block())\n"
        "from engine import plugins\n"
        "plugins.discover_modules()\n"
        f"for mid in {EXTRA_IDS!r}:\n"
        "    assert mid in plugins.MODULES, mid\n"
        "assert not plugins.LOAD_ERRORS, plugins.LOAD_ERRORS\n"
        "assert 'radarx' not in sys.modules\n"
    )
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=root, capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stderr


@pytest.mark.parametrize(
    ("module_id", "attr"),
    [
        ("radarx_kdp_column", "kdp_column"),
        ("radarx_melting_layer", "melting_layer_ppi"),
        ("radarx_qvp", "qvp"),
        ("radarx_beam_blockage", "beam_blockage"),
        ("radarx_zdr_calibration", "zdr_offset"),
    ],
)
def test_old_radarx_gives_an_upgrade_message(monkeypatch, module_id, attr):
    """A radarx without the function must give a clear ModuleError, not a crash."""
    monkeypatch.delattr(rxr, attr, raising=False)
    trees = {
        "radarx_kdp_column": kdp_volume(),
        "radarx_melting_layer": stratiform_volume(),
        "radarx_qvp": stratiform_volume(),
        "radarx_beam_blockage": ppi_tree(),
        "radarx_zdr_calibration": rain_tree(dbz=22.0),
    }
    params = {"dem_path": "x.nc"} if module_id == "radarx_beam_blockage" else {}
    with pytest.raises(ModuleError, match=rf"{attr}.*(Upgrade|upgrade)"):
        run(module_id, trees[module_id], 0, params)


# ---------------------------------------------------------------------------
# KDP column
# ---------------------------------------------------------------------------


@needs_kdp_column
def test_kdp_column_finds_the_synthetic_column():
    payload, fields = run(
        "radarx_kdp_column",
        kdp_volume(),
        0,
        {"freezing_level_m": 3100, "min_area_km2": 2, "min_depth_m": 500, "max_range_km": 60},
    )
    check_json(payload)
    assert scalar(payload, "KDP columns") >= 1
    assert scalar(payload, "Highest column top") > 3500
    rows = block(payload, "table")["rows"]
    assert rows
    top = rows[0]
    assert abs(top["azimuth"] - 45.0) < 8.0
    assert abs(top["range_km"] - 28.3) < 7.0
    assert top["above_fl_km"] > 1.0
    overlay = block(payload, "overlay")
    assert overlay["features"][0]["frame"] == "xy"
    (arr,) = fields.values()
    assert arr.shape == (N_AZ, 400)
    # no KDP anywhere above the freezing level: no column
    flat, _ = run(
        "radarx_kdp_column",
        kdp_volume(column=False),
        0,
        {"freezing_level_m": 3100, "min_area_km2": 2, "max_range_km": 60},
    )
    assert scalar(flat, "KDP columns") == 0


@needs_kdp_column
def test_kdp_column_other_displays():
    for show in ("slab_integral", "slab_depth"):
        payload, fields = run(
            "radarx_kdp_column",
            kdp_volume(),
            3,
            {"freezing_level_m": 3100, "show": show, "max_range_km": 60},
        )
        assert block(payload, "field")["name"].startswith("KDP slab")
        assert next(iter(fields.values())).shape == (N_AZ, 360)


def test_kdp_column_needs_kdp_or_phidp_and_a_volume():
    if hasattr(rxr, "kdp_column"):
        with pytest.raises(ModuleError, match="KDP"):
            run("radarx_kdp_column", storm_volume(), 0)
        with pytest.raises(ModuleError, match="several sweeps"):
            run("radarx_kdp_column", ppi_tree(), 0)


# ---------------------------------------------------------------------------
# Wind profile (VAD / VVP)
# ---------------------------------------------------------------------------


def _profile_row(payload, height_km):
    rows = block(payload, "table", "Wind profile")["rows"]
    return min(rows, key=lambda r: abs(r["height_km"] - height_km))


@pytest.mark.parametrize("dealias", [False, True])
def test_vad_recovers_the_synthetic_wind_profile(dealias):
    payload, fields = run(
        "radarx_vvp_winds",
        wind_volume(),
        0,
        {"method": "vad", "dealias_first": dealias, "nyquist_velocity": 30.0},
    )
    check_json(payload)
    assert fields == {}
    for h in (1.25, 2.25, 3.25):
        row = _profile_row(payload, h)
        speed, direction = wind_at(row["height_km"])
        assert abs(row["speed"] - speed) < 1.2, row
        diff = abs((row["direction"] - direction + 180.0) % 360.0 - 180.0)
        assert diff < 6.0, row
    assert "Levels with a wind" in {i["label"] for i in block(payload, "scalars")["items"]}
    assert scalar(payload, "Bulk shear 0-3 km") > 3.0  # u grows by 6 m/s over 3 km
    assert "from" in block(payload, "text", "Summary")["body"]


@needs_vvp
@pytest.mark.parametrize("model", ["uniform", "linear"])
def test_vvp_recovers_the_synthetic_wind_profile(model):
    payload, _ = run(
        "radarx_vvp_winds",
        wind_volume(),
        0,
        {"method": "vvp", "model": model, "dealias_first": False, "radius_km": 12},
    )
    row = _profile_row(payload, 2.25)
    speed, direction = wind_at(row["height_km"])
    assert abs(row["speed"] - speed) < 2.0, row
    assert abs((row["direction"] - direction + 180.0) % 360.0 - 180.0) < 10.0, row


def test_wind_profile_errors():
    if not hasattr(rxr, "vvp_winds"):
        with pytest.raises(ModuleError, match=r"vvp_winds.*[Uu]pgrade"):
            run("radarx_vvp_winds", wind_volume(), 0, {"method": "vvp"})
    with pytest.raises(ModuleError, match="VRADH"):
        run("radarx_vvp_winds", ppi_tree(vortex=False), 0)
    # echo only in a small sector: a VAD profile needs echo all around the radar
    with pytest.raises(ModuleError, match="No height has a valid wind"):
        run("radarx_vvp_winds", vortex_volume(), 0, {"method": "vad", "dealias_first": False})


# ---------------------------------------------------------------------------
# Beam blockage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["nc", "asc"])
def test_beam_blockage_finds_the_ridge(tmp_path, fmt):
    dem = write_ridge_dem(tmp_path / f"dem.{fmt}", fmt=fmt)
    payload, fields = run(
        "radarx_beam_blockage", ppi_tree(), 0, {"dem_path": str(dem), "beamwidth_deg": 1.0}
    )
    check_json(payload)
    cbb = fields["Cumulative beam blockage (radarx)"]
    assert cbb.shape == (N_AZ, 400)
    east = slice(80, 100)  # towards the ridge (az 90)
    west = slice(260, 280)
    r_km = (GATE_M / 2 + GATE_M * np.arange(400)) / 1000.0
    behind = r_km > 25.0
    assert np.nanmin(cbb[east][:, behind]) > 0.9
    assert np.nanmax(cbb[west]) < 0.05
    assert scalar(payload, "Max PBB") > 0.9
    rows = block(payload, "table")["rows"]
    assert rows and 14 < rows[0]["first_km"] < 22
    assert any("080" in r["sector"] or "090" in r["sector"] for r in rows)
    # other displays
    _, f2 = run(
        "radarx_beam_blockage", ppi_tree(), 0, {"dem_path": str(dem), "output": "TERRAIN_HEIGHT"}
    )
    assert np.nanmax(f2["Terrain height under the beam (radarx)"]) > 1000
    run(
        "radarx_beam_blockage",
        ppi_tree(),
        0,
        {"dem_path": str(dem), "output": "TERRAIN_CLUTTER", "pattern": "uniform"},
    )


def test_beam_blockage_without_dem_explains_and_never_downloads(monkeypatch, tmp_path):
    import socket

    def refuse(*_a, **_k):  # pragma: no cover - must not be called
        raise AssertionError("beam blockage must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    with pytest.raises(ModuleError, match="never downloads"):
        run("radarx_beam_blockage", ppi_tree(), 0)
    with pytest.raises(ModuleError, match="not found"):
        run("radarx_beam_blockage", ppi_tree(), 0, {"dem_path": str(tmp_path / "nope.nc")})
    odd = tmp_path / "dem.xyz"
    odd.write_text("1 2 3")
    with pytest.raises(ModuleError, match="Unsupported DEM format"):
        run("radarx_beam_blockage", ppi_tree(), 0, {"dem_path": str(odd)})
    # a DEM that does not contain the radar
    far = tmp_path / "far.nc"
    xr.DataArray(
        np.zeros((20, 20)),
        dims=("lat", "lon"),
        coords={"lat": np.linspace(10, 11, 20), "lon": np.linspace(10, 11, 20)},
        name="elevation",
    ).to_dataset().to_netcdf(far)
    with pytest.raises(ModuleError, match="Beam blockage"):
        run("radarx_beam_blockage", ppi_tree(), 0, {"dem_path": str(far)})


@pytest.mark.skipif(
    importlib.util.find_spec("rioxarray") or importlib.util.find_spec("rasterio"),
    reason="a GeoTIFF reader is installed",
)
def test_beam_blockage_geotiff_needs_a_reader(tmp_path):
    tif = tmp_path / "dem.tif"
    tif.write_bytes(b"II*\x00")
    with pytest.raises(ModuleError, match="rioxarray or rasterio"):
        run("radarx_beam_blockage", ppi_tree(), 0, {"dem_path": str(tif)})


def test_beam_blockage_runs_on_all_sweeps_as_a_derived_variable(tmp_path):
    dem = write_ridge_dem(tmp_path / "dem.nc")
    tree = storm_volume(elevations=[0.5, 1.0, 2.0, 4.0])

    def ds(i):
        return tree[f"sweep_{i}"].to_dataset()

    out = derived.run_all_sweeps(
        "radarx_beam_blockage",
        tree,
        4,
        {"dem_path": str(dem)},
        get_sweep_dataset=ds,
        get_sweep_variables=lambda i: {str(v) for v in ds(i).data_vars},
        get_sweep_grid=lambda i: (ds(i)["azimuth"].size, ds(i)["range"].size),
    )
    assert sorted(out.arrays["Cumulative beam blockage (radarx)"]) == [0, 1, 2, 3]
    low = out.arrays["Cumulative beam blockage (radarx)"][0]
    high = out.arrays["Cumulative beam blockage (radarx)"][3]
    assert np.nanmean(low) > np.nanmean(high)  # steeper beams clear the ridge


# ---------------------------------------------------------------------------
# Tornado probability
# ---------------------------------------------------------------------------


def _globals_of(module_id):
    return plugins.MODULES[module_id].func.__globals__


def test_tornado_heuristic_finds_the_synthetic_vortex():
    payload, fields = run(
        "radarx_tornado_prob", ppi_tree(), 0, {"method": "heuristic", "min_dbz": -30}
    )
    check_json(payload)
    score = fields["Rotation score (heuristic, not a probability)"]
    assert score.shape == (N_AZ, 400)
    assert np.nanmax(score) > 0.5
    rows = block(payload, "table")["rows"]
    assert rows
    best = rows[0]  # vortex at (-30, 30) km: azimuth 315, range 42 km
    assert abs(best["azimuth"] - 315.0) < 8.0
    assert abs(best["range_km"] - 42.4) < 5.0
    assert best["vrot"] > 15 and best["score"] > 0.5
    assert scalar(payload, "Method") == "rotation heuristic (no ML)"
    body = block(payload, "text", "About")["body"]
    assert "NOT the machine-learning" in body
    assert block(payload, "overlay")["features"]


def test_tornado_ml_unavailable_is_explained(monkeypatch):
    g = _globals_of("radarx_tornado_prob")
    monkeypatch.setitem(g, "ml_status", lambda _p="": (None, "the onnxruntime package is missing"))
    with pytest.raises(ModuleError, match=r"TorNet CNN cannot run.*onnxruntime"):
        run("radarx_tornado_prob", ppi_tree(), 0, {"method": "ml"})
    # auto falls back and says why
    payload, _ = run("radarx_tornado_prob", ppi_tree(), 0, {"method": "auto"})
    body = block(payload, "text", "About")["body"]
    assert "could not run: the onnxruntime package is missing" in body


def test_ml_status_reports_missing_onnxruntime_and_model(monkeypatch, tmp_path):
    status = _globals_of("radarx_tornado_prob")["ml_status"]
    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda n, *a, **k: None if n == "onnxruntime" else real(n, *a, **k),
    )
    assert status("")[0] is None and "onnxruntime" in status("")[1]
    monkeypatch.setattr(importlib.util, "find_spec", lambda n, *a, **k: object())
    monkeypatch.setenv("RADARX_CACHE_DIR", str(tmp_path))
    path, reason = status("")
    assert path is None and "never does" in reason and "download" in reason
    assert status(str(tmp_path / "missing.onnx"))[0] is None
    model = tmp_path / "m.onnx"
    model.write_bytes(b"x")
    assert status(str(model)) == (str(model), "")
    cached = tmp_path / "models" / "tornet-baseline-v1.onnx"
    cached.parent.mkdir()
    cached.write_bytes(b"x")
    assert status("") == (str(cached), "")


def test_tornado_ml_path_wiring(monkeypatch):
    """The ML path with a stand-in network: remapping, objects and no-download arguments."""
    g = _globals_of("radarx_tornado_prob")
    monkeypatch.setitem(g, "ml_status", lambda _p="": ("/local/tornet.onnx", ""))
    seen = {}

    def fake(tree, **kwargs):
        seen.update(kwargs)
        az = np.arange(0.25, 360.0, 0.5)
        rng = np.arange(2125.0, 60000.0, 250.0)
        prob = np.full((az.size, rng.size), 0.01, dtype=np.float32)
        ia = int(np.argmin(np.abs(az - 315.0)))
        ir = int(np.argmin(np.abs(rng - 42000.0)))
        prob[ia - 6 : ia + 6, ir - 8 : ir + 8] = 0.8
        return xr.Dataset(
            {
                "tornado_probability": (("azimuth", "range"), prob),
                "chip_probability": ("chip", np.array([0.8, 0.1])),
            },
            coords={"azimuth": az, "range": rng},
        )

    monkeypatch.setattr(rxr, "tornado_probability", fake)
    payload, fields = run("radarx_tornado_prob", ppi_tree(), 0, {"method": "auto"})
    check_json(payload)
    assert seen["model"] == "/local/tornet.onnx"  # a file path: radarx cannot download
    assert scalar(payload, "Method") == "TorNet CNN (ML)"
    assert scalar(payload, "Max probability") == pytest.approx(0.8)
    shown = fields["Tornado probability (TorNet CNN)"]
    assert shown.shape == (N_AZ, 400)
    ia, ir = np.unravel_index(np.nanargmax(shown), shown.shape)
    assert abs(ia + 0.5 - 315.0) < 8 and abs((GATE_M / 2 + GATE_M * ir) / 1000.0 - 42.0) < 4
    rows = block(payload, "table")["rows"]
    assert rows and abs(rows[0]["azimuth"] - 315.0) < 5
    assert "TorNet" in block(payload, "text", "About")["body"]


def test_tornado_needs_velocity():
    with pytest.raises(ModuleError, match="VRADH"):
        run("radarx_tornado_prob", storm_volume(), 0)


# ---------------------------------------------------------------------------
# Melting layer
# ---------------------------------------------------------------------------


def test_melting_layer_ppi_finds_the_bright_band():
    tree = stratiform_volume()
    # the 8 degree sweep (index 5) crosses the layer, so all three classes appear
    payload, fields = run("radarx_melting_layer", tree, 5, {"method": "ppi"})
    check_json(payload)
    assert scalar(payload, "Melting layer") == "detected"
    top, bottom = scalar(payload, "Top"), scalar(payload, "Bottom")
    assert ML_BOTTOM - 100 <= bottom <= ML_BOTTOM + 500
    assert ML_TOP - 500 <= top <= ML_TOP + 100
    cls = fields["Melting layer position (radarx)"]
    assert set(np.unique(cls[np.isfinite(cls)])) == {1.0, 2.0, 3.0}
    assert len(block(payload, "table")["rows"]) == 12


def test_melting_layer_profile_methods():
    tree = stratiform_volume()
    payload, _ = run(
        "radarx_melting_layer", tree, 0, {"method": "profile", "freezing_level_m": 3100}
    )
    assert scalar(payload, "Melting layer") == "detected"
    assert abs(scalar(payload, "Top") - ML_TOP) < 500
    assert abs(scalar(payload, "Bottom") - ML_BOTTOM) < 500
    assert scalar(payload, "Top minus freezing level") == pytest.approx(
        scalar(payload, "Top") - 3100
    )
    prof = block(payload, "table", "Profile used")["rows"]
    assert any(r["layer"] == "yes" for r in prof)
    if hasattr(rxr, "melting_layer_gradient"):
        p2, _ = run("radarx_melting_layer", tree, 0, {"method": "profile_gradient"})
        assert scalar(p2, "Melting layer") == "detected"
        assert abs(scalar(p2, "Top") - ML_TOP) < 700


def test_melting_layer_not_detected_without_a_bright_band():
    tree = stratiform_volume(bright_band=False)
    payload, fields = run("radarx_melting_layer", tree, 0, {"method": "profile"})
    assert scalar(payload, "Melting layer") == "not detected"
    assert fields == {}
    assert "No melting layer was detected" in block(payload, "text", "About")["body"]


def test_melting_layer_errors():
    with pytest.raises(ModuleError, match="ZDR"):
        run("radarx_melting_layer", vortex_volume(), 0)
    low = stratiform_volume(elevations=[0.5, 1.0, 1.5])
    with pytest.raises(ModuleError, match="between 4° and 10°"):
        run("radarx_melting_layer", low, 0, {"method": "ppi"})
    with pytest.raises(ModuleError, match="Search to"):
        run("radarx_melting_layer", low, 0, {"search_min_m": 3000, "search_max_m": 2000})
    if not hasattr(rxr, "melting_layer_gradient"):
        with pytest.raises(ModuleError, match=r"melting_layer_gradient.*[Uu]pgrade"):
            run("radarx_melting_layer", low, 0, {"method": "profile_gradient"})


# ---------------------------------------------------------------------------
# QVP
# ---------------------------------------------------------------------------


def test_qvp_profile_and_bright_band_signature():
    payload, fields = run("radarx_qvp", stratiform_volume(), 0, {"dz_m": 250})
    check_json(payload)
    assert fields == {}
    assert scalar(payload, "Profile type") == "QVP (single sweep)"  # auto: 19.5 deg sweep
    rows = block(payload, "table")["rows"]
    assert len(rows) > 10
    in_layer = [r for r in rows if ML_BOTTOM <= r["height_asl_km"] * 1000 <= ML_TOP]
    above = [r for r in rows if r["height_asl_km"] * 1000 > ML_TOP + 500]
    assert in_layer and above
    assert max(r["ZDR"] for r in in_layer) > 1.2 > max(r["ZDR"] for r in above)
    assert min(r["RHOHV"] for r in in_layer) < 0.95
    assert ML_BOTTOM - 300 < scalar(payload, "Height of max ZDR") < ML_TOP + 300
    assert scalar(payload, "Bright-band signature").startswith("yes")
    assert scalar(payload, "Max Z") == pytest.approx(36.0, abs=1.0)
    assert "Strongest reflectivity" in block(payload, "text", "Summary")["body"]


def test_qvp_rd_qvp_method_and_no_bright_band():
    payload, _ = run("radarx_qvp", stratiform_volume(), 0, {"method": "rd_qvp", "rmax_km": 60})
    assert scalar(payload, "Profile type") == "RD-QVP"
    assert scalar(payload, "Bright-band signature").startswith("yes")
    flat, _ = run("radarx_qvp", stratiform_volume(bright_band=False), 0, {"method": "rd_qvp"})
    assert scalar(flat, "Bright-band signature") == "not found"
    low = stratiform_volume(elevations=[0.5, 1.5, 3.0])  # auto falls back to RD-QVP
    p3, _ = run("radarx_qvp", low, 0)
    assert scalar(p3, "Profile type") == "RD-QVP"


def test_qvp_errors():
    with pytest.raises(ModuleError, match="ZDR"):
        run("radarx_qvp", vortex_volume(), 0)
    with pytest.raises(ModuleError, match="no valid height"):
        run("radarx_qvp", stratiform_volume(), 0, {"min_rhohv": 1.0, "min_dbz": 30})


# ---------------------------------------------------------------------------
# ZDR calibration
# ---------------------------------------------------------------------------


def test_zdr_calibration_recovers_a_known_bias():
    base, _ = run("radarx_zdr_calibration", rain_tree(dbz=22.0, zdr=0.35, rhohv=0.99), 0)
    biased, fields = run("radarx_zdr_calibration", rain_tree(dbz=22.0, zdr=0.85, rhohv=0.99), 0)
    check_json(biased)
    # the same rain with 0.5 dB more ZDR: the offset rises by exactly that
    assert scalar(biased, "ZDR offset (bias)") - scalar(base, "ZDR offset (bias)") == pytest.approx(
        0.5, abs=0.01
    )
    # light-rain reference at 22 dBZ is about 0.3-0.4 dB
    assert abs(scalar(base, "ZDR offset (bias)")) < 0.15
    assert scalar(biased, "Gates used") > 1000
    corrected = fields["ZDR corrected (radarx offset)"]
    assert np.nanmedian(corrected) == pytest.approx(
        0.85 - scalar(biased, "ZDR offset (bias)"), abs=0.01
    )
    assert scalar(biased, "Standard error") < 0.01
    assert "significant" in scalar(biased, "Assessment") or "bias" in scalar(biased, "Assessment")
    assert "Corrected ZDR" in block(biased, "text", "Summary")["body"]


def test_zdr_calibration_vertical_and_dry_snow():
    vertical, _ = run(
        "radarx_zdr_calibration",
        rain_tree(dbz=30.0, zdr=0.4, rhohv=0.995),
        0,
        {"method": "vertical", "statistic": "mean"},
    )
    assert scalar(vertical, "ZDR offset (bias)") == pytest.approx(0.4, abs=0.02)
    # dry snow: a mask above 1 km ASL keeps only the farther gates of the 0.5 deg beam
    snow, _ = run(
        "radarx_zdr_calibration",
        rain_tree(dbz=15.0, zdr=0.6, rhohv=0.995),
        0,
        {"method": "dry_snow", "snow_above_m": 600, "min_count": 50},
    )
    assert scalar(snow, "ZDR offset (bias)") == pytest.approx(0.6 - 0.2, abs=0.03)


def test_zdr_calibration_errors():
    tree = rain_tree(dbz=22.0, zdr=0.5, rhohv=0.99)
    with pytest.raises(ModuleError, match="Snow above"):
        run("radarx_zdr_calibration", tree, 0, {"method": "dry_snow"})
    with pytest.raises(ModuleError, match="gates passed the selection"):
        run("radarx_zdr_calibration", rain_tree(dbz=22.0, rhohv=0.9), 0)
    with pytest.raises(ModuleError, match="both"):
        run("radarx_zdr_calibration", tree, 0, {"z_low_dbz": 20})
    with pytest.raises(ModuleError, match="DBZH"):
        run("radarx_zdr_calibration", vortex_volume(), 0)


def test_zdr_calibration_all_sweeps():
    tree = stratiform_volume(elevations=[0.5, 1.5, 3.0])

    def ds(i):
        return tree[f"sweep_{i}"].to_dataset()

    out = derived.run_all_sweeps(
        "radarx_zdr_calibration",
        tree,
        3,
        {"z_low_dbz": 25, "z_high_dbz": 35, "min_rhohv": 0.95, "min_count": 50},
        get_sweep_dataset=ds,
        get_sweep_variables=lambda i: {str(v) for v in ds(i).data_vars},
        get_sweep_grid=lambda i: (ds(i)["azimuth"].size, ds(i)["range"].size),
    )
    assert sorted(out.arrays["ZDR corrected (radarx offset)"]) == [0, 1, 2]


# ---------------------------------------------------------------------------
# Real data: KTLX (NEXRAD, dual-pol on sweep 0, velocity on sweep 1) and COW (CfRadial)
# ---------------------------------------------------------------------------

REAL_RUNS = [
    # (file, module, sweep, params, needs)
    ("ktlx", "radarx_zdr_calibration", 0, {}, None),
    ("ktlx", "radarx_melting_layer", 0, {"method": "ppi"}, None),
    ("ktlx", "radarx_melting_layer", 0, {"method": "profile"}, None),
    ("ktlx", "radarx_qvp", 0, {}, None),
    ("ktlx", "radarx_qvp", 0, {"method": "rd_qvp"}, None),
    ("ktlx", "radarx_vvp_winds", 1, {"method": "vad"}, None),
    ("ktlx", "radarx_vvp_winds", 1, {"method": "vvp"}, "vvp_winds"),
    ("ktlx", "radarx_tornado_prob", 1, {"method": "heuristic"}, None),
    ("ktlx", "radarx_kdp_column", 0, {"freezing_level_m": 4200}, "kdp_column"),
    ("cow", "radarx_zdr_calibration", 0, {}, None),
    ("cow", "radarx_melting_layer", 0, {"method": "ppi"}, None),
    ("cow", "radarx_qvp", 0, {}, None),
    ("cow", "radarx_vvp_winds", 0, {"method": "vad"}, None),
    ("cow", "radarx_tornado_prob", 0, {"method": "heuristic"}, None),
]
FILES = {"ktlx": KTLX, "cow": COW}


@pytest.fixture(scope="module")
def readers():
    from engine.reader import RadarReader

    cache = {}

    def get(which):
        if which not in cache:
            reader = RadarReader()
            reader.open_file(FILES[which])
            cache[which] = reader
        return cache[which]

    return get


@pytest.mark.parametrize(
    ("which", "module_id", "sweep", "params", "needs"),
    REAL_RUNS,
    ids=[f"{r[0]}-{r[1].removeprefix('radarx_')}-{r[3].get('method', '')}" for r in REAL_RUNS],
)
def test_real_data_modules_run(readers, which, module_id, sweep, params, needs):
    if FILES[which] is None:
        pytest.skip(f"{which} example file not available")
    if needs and not hasattr(rxr, needs):
        pytest.skip(f"installed radarx has no {needs}")
    reader = readers(which)
    payload, fields = run_module(
        module_id,
        reader.datatree,
        sweep,
        params,
        get_sweep_dataset=reader.get_sweep_dataset,
        return_fields=True,
    )
    check_json(payload)
    assert payload["elapsed_ms"] < 60_000
    assert payload["blocks"]
    ds = reader.get_sweep_dataset(sweep)
    shape = (ds.sizes["azimuth"], ds.sizes["range"])
    for name, arr in fields.items():
        assert arr.shape == shape and arr.dtype == np.float32, name
    for b in payload["blocks"]:
        if b["type"] == "field":
            assert tuple(b["shape"]) == shape


@pytest.mark.skipif(KTLX is None, reason="KTLX example file not available")
def test_ktlx_results_are_physically_sensible(readers):
    reader = readers("ktlx")

    def go(module_id, sweep, params=None):
        return run_module(
            module_id,
            reader.datatree,
            sweep,
            params or {},
            get_sweep_dataset=reader.get_sweep_dataset,
        )

    cal = go("radarx_zdr_calibration", 0)
    assert abs(scalar(cal, "ZDR offset (bias)")) < 0.5 and scalar(cal, "Gates used") > 100
    ml = go("radarx_melting_layer", 0, {"method": "ppi"})
    assert scalar(ml, "Melting layer") == "detected"
    assert 3000 < scalar(ml, "Top") < 5500 and scalar(ml, "Bottom") < scalar(ml, "Top")
    # 20 May 2013: south-easterly low-level, strong westerly/south-westerly flow aloft
    wind = go("radarx_vvp_winds", 1, {"method": "vad"})
    row_low = min(block(wind, "table")["rows"], key=lambda r: abs(r["height_km"] - 0.75))
    row_high = min(block(wind, "table")["rows"], key=lambda r: abs(r["height_km"] - 5.25))
    assert 90 < row_low["direction"] < 230 and row_low["speed"] < 20
    assert 200 < row_high["direction"] < 290 and row_high["speed"] > 15
    assert scalar(wind, "Bulk shear 0-6 km") > 15
    # the Newcastle-Moore EF5 vortex is ~22 km WSW of KTLX
    tor = go("radarx_tornado_prob", 1, {"method": "heuristic"})
    best = block(tor, "table")["rows"][0]
    assert 250 < best["azimuth"] < 280 and 12 < best["range_km"] < 32 and best["vrot"] > 25


@pytest.mark.skipif(KTLX is None, reason="KTLX example file not available")
def test_ktlx_missing_fields_give_module_errors(readers):
    reader = readers("ktlx")

    def go(module_id, sweep, params=None):
        return run_module(
            module_id,
            reader.datatree,
            sweep,
            params or {},
            get_sweep_dataset=reader.get_sweep_dataset,
        )

    with pytest.raises(ModuleError, match="VRADH"):
        go("radarx_tornado_prob", 0)  # surveillance cut has no velocity
    with pytest.raises(ModuleError, match="VRADH"):
        go("radarx_vvp_winds", 0)
    with pytest.raises(ModuleError, match="ZDR"):
        go("radarx_qvp", 1)  # Doppler cut has no dual-pol
    with pytest.raises(ModuleError, match="terrain model"):
        go("radarx_beam_blockage", 0)
