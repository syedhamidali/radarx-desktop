"""Tests for the module (plugin) SDK and the built-in modules."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from engine import plugins
from engine.dualpol import _get_sweep_ds
from engine.plugins import ModuleError, run_module

N_AZ, N_RNG, GATE_M = 360, 500, 250.0


# ---------------------------------------------------------------------------
# Synthetic data
# ---------------------------------------------------------------------------


def _xy_grid(azimuth, range_m):
    az = np.radians(azimuth)[:, None]
    r = range_m[None, :]
    return r * np.sin(az) / 1000.0, r * np.cos(az) / 1000.0  # km east, km north


def make_storm_tree(*, rhohv: bool = True, zdr: bool = True, nan_patch: bool = True) -> xr.DataTree:
    """360 x 500 sweep (250 m gates) with known features.

    * Blob A  : az 90, 60 km, sigma 10 km, peak 50 dBZ; a ZDR=3 dB crescent on its south edge.
    * Blob B  : centred on az 0 (straddles the 0/360 seam) at 40 km, sigma 8 km; a ZDR=3 dB
                N-S strip along the seam; RHOHV is lowered to 0.7 on the strip.
    * Blob C  : small (about 20 km2 above 35 dBZ) at az 200, 80 km, with a *round* 3 dB ZDR spot.
    * A NaN wedge at az 300-310.
    """
    azimuth = np.arange(N_AZ, dtype=np.float64)
    range_m = np.arange(N_RNG) * GATE_M + GATE_M / 2
    x, y = _xy_grid(azimuth, range_m)

    def blob(cx, cy, sigma):
        return np.exp(-((x - cx) ** 2 + (y - cy) ** 2) / (2 * sigma**2))

    dbzh = 50.0 * np.maximum.reduce([blob(60, 0, 10), blob(0, 40, 8), blob(-27.4, -75.2, 3)])
    zdr_arr = np.full_like(dbzh, 0.5)

    d = np.hypot(x - 60, y)
    ang = np.degrees(np.arctan2(y, x - 60))
    zdr_arr[(d > 7) & (d < 9.5) & (ang > -150) & (ang < -30)] = 3.0
    strip = (np.abs(x) < 1.5) & (y > 34) & (y < 46)
    zdr_arr[strip] = 3.0
    zdr_arr[np.hypot(x + 27.4, y + 75.2) < 2.0] = 3.0

    rho = np.full_like(dbzh, 0.98)
    rho[strip] = 0.7

    if nan_patch:
        dbzh[300:311, :] = np.nan
        zdr_arr[300:311, :] = np.nan

    data = {"DBZH": (["azimuth", "range"], dbzh.astype(np.float32))}
    if zdr:
        data["ZDR"] = (["azimuth", "range"], zdr_arr.astype(np.float32))
    if rhohv:
        data["RHOHV"] = (["azimuth", "range"], rho.astype(np.float32))
    ds = xr.Dataset(
        data,
        coords={
            "azimuth": azimuth,
            "range": range_m,
            "elevation": ("azimuth", np.full(N_AZ, 0.5, dtype=np.float32)),
        },
    )
    return xr.DataTree.from_dict({"/": xr.Dataset(), "/sweep_0": ds})


def run(module_id: str, tree: xr.DataTree, **params):
    return run_module(
        module_id, tree, 0, params, get_sweep_dataset=lambda s: _get_sweep_ds(tree, s)
    )


def blocks_by_type(payload: dict) -> dict[str, dict]:
    return {b["type"]: b for b in payload["blocks"]}


def ang_diff(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    """Fresh discovery with an isolated HOME and no extra module dirs."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XRADAR_MODULES_DIR", raising=False)
    plugins.discover_modules()
    yield plugins
    monkeypatch.delenv("XRADAR_MODULES_DIR", raising=False)
    plugins.discover_modules()


@pytest.fixture(scope="module")
def tree() -> xr.DataTree:
    return make_storm_tree()


# ---------------------------------------------------------------------------
# Discovery / SDK behaviour
# ---------------------------------------------------------------------------


def test_builtin_modules_discovered(registry):
    assert not registry.LOAD_ERRORS
    expected = (("storm_detection", "Storm"), ("cell_size_sort", "Storm"), ("zdr_arc", "Dual-pol"))
    for mid, cat in expected:
        assert mid in registry.MODULES
        assert registry.MODULES[mid].category == cat
    assert "_radar_utils" not in " ".join(registry.MODULES)  # underscore files are not modules
    listing = registry.list_modules()
    json.dumps(listing, allow_nan=False)
    assert {m["id"] for m in listing["modules"]} >= {"storm_detection", "cell_size_sort", "zdr_arc"}
    assert registry.MODULES["zdr_arc"].requires == ["DBZH", "ZDR"]
    assert registry.MODULES["zdr_arc"].optional == ["RHOHV"]


def test_param_defaults_and_clamping(registry, tree):
    out = run("cell_size_sort", tree)
    assert out["params"] == {
        "threshold_dbz": 35.0,
        "min_size_km2": 10.0,
        "sort_by": "area_km2",
        "descending": True,
        "top_n": 10,
    }
    out = run("cell_size_sort", tree, threshold_dbz=1e6, min_size_km2=-5, top_n=1000)
    assert out["params"]["threshold_dbz"] == 75
    assert out["params"]["min_size_km2"] == 0
    assert out["params"]["top_n"] == 100 and isinstance(out["params"]["top_n"], int)
    assert run("cell_size_sort", tree, top_n=0)["params"]["top_n"] == 1
    # numeric strings coerce and round
    assert run("cell_size_sort", tree, top_n="7.6")["params"]["top_n"] == 8


@pytest.mark.parametrize(
    "bad",
    [
        {"sort_by": "bogus"},
        {"threshold_dbz": "abc"},
        {"threshold_dbz": float("nan")},
        {"threshold_dbz": None},
        {"top_n": True},
    ],
)
def test_bad_param_values_raise(registry, tree, bad):
    with pytest.raises(ModuleError, match="parameter"):
        run("cell_size_sort", tree, **bad)


def test_unknown_module(registry, tree):
    with pytest.raises(KeyError):
        run("does_not_exist", tree)


@pytest.mark.parametrize("module_id", ["zdr_arc"])
def test_missing_field_raises_module_error(registry, module_id):
    no_zdr = make_storm_tree(zdr=False)
    with pytest.raises(ModuleError, match="ZDR"):
        run(module_id, no_zdr)


def test_missing_dbzh_raises(registry):
    ds = make_storm_tree().children["sweep_0"].to_dataset().drop_vars("DBZH")
    t = xr.DataTree.from_dict({"/": xr.Dataset(), "/sweep_0": ds})
    for mid in ("storm_detection", "cell_size_sort", "zdr_arc"):
        with pytest.raises(ModuleError, match="DBZH"):
            run(mid, t)


def test_field_aliases(registry):
    ds = make_storm_tree().children["sweep_0"].to_dataset()
    ds = ds.rename({"ZDR": "differential_reflectivity"})
    t = xr.DataTree.from_dict({"/": xr.Dataset(), "/sweep_0": ds})
    assert run("zdr_arc", t)["blocks"]


# ---------------------------------------------------------------------------
# storm_detection
# ---------------------------------------------------------------------------


def test_storm_detection_structure(registry, tree):
    out = run("storm_detection", tree)
    blocks = blocks_by_type(out)
    assert [b["type"] for b in out["blocks"]] == ["scalars", "table", "overlay"]
    table, overlay = blocks["table"], blocks["overlay"]
    assert table["id_key"] == "id"
    assert {c["key"] for c in table["columns"]} >= {
        "id",
        "centroid_az",
        "centroid_range_km",
        "area_km2",
        "max_dbz",
        "mean_dbz",
    }
    rows = table["rows"]
    assert len(rows) >= 3
    scal = {i["label"]: i["value"] for i in blocks["scalars"]["items"]}
    assert scal["Cells"] == len(rows)
    assert scal["Largest cell"] == pytest.approx(max(r["area_km2"] for r in rows), abs=0.1)
    assert scal["Max reflectivity"] == pytest.approx(50.0, abs=1.0)
    # overlay ids line up with table ids, polygons are well-formed and polar
    assert {f["id"] for f in overlay["features"]} == {r["id"] for r in rows}
    for f in overlay["features"]:
        assert f["frame"] == "polar"
        if f["kind"] == "polygon":
            assert 3 <= len(f["coords"]) <= 100
            assert all(0 <= az < 360 and rng > 0 for az, rng in f["coords"])
        assert f["color"].startswith("#")
    # blob A: az 90, 60 km
    a = [r for r in rows if ang_diff(r["centroid_az"], 90) < 3]
    assert a and a[0]["centroid_range_km"] == pytest.approx(60, abs=3)
    assert a[0]["max_dbz"] == pytest.approx(50, abs=0.5)


def test_storm_detection_thresholds_filter(registry, tree):
    low = run("storm_detection", tree, threshold_dbz=35, min_size_km2=10)
    big = run("storm_detection", tree, threshold_dbz=35, min_size_km2=180)
    none = run("storm_detection", tree, threshold_dbz=70)
    n = lambda o: len(blocks_by_type(o)["table"]["rows"])  # noqa: E731
    assert n(big) < n(low)
    assert n(none) == 0
    assert blocks_by_type(none)["overlay"]["features"] == []
    json.dumps(none, allow_nan=False)  # empty scalars (None max) must stay JSON-safe


# ---------------------------------------------------------------------------
# cell_size_sort
# ---------------------------------------------------------------------------


def test_cell_size_sort_ranking_and_labels(registry, tree):
    out = run("cell_size_sort", tree, sort_by="area_km2", descending=True, top_n=2)
    blocks = blocks_by_type(out)
    rows = blocks["table"]["rows"]
    assert [r["rank"] for r in rows] == [1, 2]
    assert rows[0]["area_km2"] >= rows[1]["area_km2"]
    assert ang_diff(rows[0]["centroid_az"], 90) < 3  # blob A is the largest
    feats = blocks["overlay"]["features"]
    assert [f["label"] for f in feats] == ["#1", "#2"]
    assert [f["id"] for f in feats] == [r["id"] for r in rows]
    assert blocks["table"]["id_key"] == "id"


@pytest.mark.parametrize("key", ["area_km2", "max_dbz", "mean_dbz", "centroid_range"])
@pytest.mark.parametrize("descending", [True, False])
def test_cell_size_sort_orders(registry, tree, key, descending):
    out = run("cell_size_sort", tree, sort_by=key, descending=descending, top_n=100)
    rows = blocks_by_type(out)["table"]["rows"]
    col = "centroid_range_km" if key == "centroid_range" else key
    vals = [r[col] for r in rows]
    assert len(vals) >= 3
    assert vals == sorted(vals, reverse=descending)


def test_cell_size_sort_top_n_limits(registry, tree):
    out = run("cell_size_sort", tree, top_n=1)
    blocks = blocks_by_type(out)
    assert len(blocks["table"]["rows"]) == 1 and len(blocks["overlay"]["features"]) == 1


# ---------------------------------------------------------------------------
# zdr_arc
# ---------------------------------------------------------------------------


def test_zdr_arc_structure_and_detection(registry, tree):
    out = run("zdr_arc", tree)
    assert [b["type"] for b in out["blocks"]] == ["scalars", "table", "overlay", "text"]
    blocks = blocks_by_type(out)
    assert "heuristic" in blocks["text"]["body"].lower()
    assert "heuristic" in registry.MODULES["zdr_arc"].description.lower()
    rows = blocks["table"]["rows"]
    # crescent on blob A is found; RHOHV (0.7) screens out the seam strip; round spot rejected
    assert len(rows) == 1
    arc = rows[0]
    assert ang_diff(arc["centroid_az"], 90) < 15
    assert 50 < arc["centroid_range_km"] < 65
    assert arc["elongation"] >= 2.0
    assert arc["mean_zdr"] == pytest.approx(3.0, abs=0.05)
    assert arc["area_km2"] >= 5
    assert 8 < arc["length_km"] < 25  # ~120 degrees of a ring of radius ~8 km (>=13 km)
    assert "mean_rhohv" in arc
    feat = blocks["overlay"]["features"][0]
    assert feat["id"] == arc["id"] and feat["kind"] == "polygon" and feat["frame"] == "polar"
    assert 3 <= len(feat["coords"]) <= 100


def test_zdr_arc_seam_wrap_and_circular_centroid(registry):
    t = make_storm_tree(rhohv=False)  # no RHOHV -> seam strip no longer screened
    out = run("zdr_arc", t)
    blocks = blocks_by_type(out)
    rows = blocks["table"]["rows"]
    seam = [r for r in rows if ang_diff(r["centroid_az"], 0) < 5]
    assert len(seam) == 1, rows  # one arc, not two halves split at 359/0
    assert seam[0]["elongation"] >= 3
    assert 8 < seam[0]["length_km"] < 16
    assert "mean_rhohv" not in seam[0]
    scal = {i["label"]: i["value"] for i in blocks["scalars"]["items"]}
    assert scal["RHOHV screening"].startswith("off")
    # polygon for the seam arc crosses 0/360: points on both sides of north
    feat = next(f for f in blocks["overlay"]["features"] if f["id"] == seam[0]["id"])
    azs = [c[0] for c in feat["coords"]]
    assert any(a < 5 for a in azs) and any(a > 355 for a in azs)


def test_zdr_arc_elongation_and_area_filters(registry):
    t = make_storm_tree(rhohv=False)
    n = lambda **p: len(blocks_by_type(run("zdr_arc", t, **p))["table"]["rows"])  # noqa: E731
    default = n()
    assert n(min_elongation=1.0) > default  # round spot in blob C now qualifies
    assert n(min_elongation=20) == 0
    assert n(min_area_km2=1000) == 0
    assert n(zdr_min=5) == 0
    assert n(z_min=70) == 0


def test_zdr_arc_no_candidates_is_json_safe(registry, tree):
    out = run("zdr_arc", tree, zdr_min=8)
    json.dumps(out, allow_nan=False)
    blocks = blocks_by_type(out)
    assert blocks["table"]["rows"] == [] and blocks["overlay"]["features"] == []


# ---------------------------------------------------------------------------
# JSON safety, helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_id", ["storm_detection", "cell_size_sort", "zdr_arc"])
def test_results_are_strict_json(registry, tree, module_id):
    out = run(module_id, tree)  # tree contains NaN wedges in DBZH/ZDR
    json.dumps(out, allow_nan=False)
    assert out["module"] == module_id and out["sweep"] == 0 and out["elapsed_ms"] >= 0


def test_result_clean_converts_nan_and_numpy():
    res = plugins.Result()
    res.scalars({"a": np.float32("nan"), "b": np.int64(3), "c": float("inf")})
    res.table("t", ["x"], [{"x": np.array([1.0, np.nan])}])
    payload = res.to_dict()
    text = json.dumps(payload, allow_nan=False)
    assert '"value": null' in text and '"b"' in text


def test_order_polygon_helper(registry):
    utils = sys.modules["xradar_module_radar_utils"]
    rng = np.random.default_rng(0)
    for centre_az in (90.0, 0.0):  # second one straddles the seam
        theta = rng.permutation(np.linspace(0, 2 * np.pi, 400, endpoint=False))
        cx, cy = utils.polar_to_xy(np.array([centre_az]), np.array([50_000.0]))
        px, py = cx[0] + 5000 * np.cos(theta), cy[0] + 5000 * np.sin(theta)
        az = np.degrees(np.arctan2(px, py)) % 360
        r = np.hypot(px, py)
        poly = utils.order_polygon(az, r, max_points=100)
        assert 90 <= len(poly) <= 100
        x, y = utils.polar_to_xy(np.array([p[0] for p in poly]), np.array([p[1] for p in poly]))
        steps = np.hypot(np.diff(np.r_[x, x[0]]), np.diff(np.r_[y, y[0]]))
        # ring is traversed in order: perimeter of the polyline ~ circumference
        assert steps.sum() == pytest.approx(2 * np.pi * 5000, rel=0.02)
    assert utils.order_polygon([10, 10], [1000, 2000]) == []  # degenerate input
    assert ang_diff(utils.circular_mean_deg(np.array([359.0, 1.0])), 0.0) < 1e-6


# ---------------------------------------------------------------------------
# Plugin loading (user directories)
# ---------------------------------------------------------------------------

GOOD_PLUGIN = textwrap.dedent(
    """
    from engine.plugins import module, param, Result

    @module(id="{mid}", title="{title}", category="Test",
            requires=["DBZH"], params=[param("k", "number", 1.0)])
    def run(ctx, k):
        return Result().scalars({{"k": k, "n": int(ctx.field("DBZH").size)}})
    """
)


def test_broken_plugin_reported_without_breaking_others(registry, tmp_path, monkeypatch, tree):
    d = tmp_path / "user_modules"
    d.mkdir()
    (d / "a_good.py").write_text(GOOD_PLUGIN.format(mid="tmp_good", title="Good"))
    (d / "b_raises.py").write_text("raise RuntimeError('boom on import')\n")
    (d / "c_syntax.py").write_text("def broken(:\n")
    (d / "d_badparam.py").write_text(
        "from engine.plugins import module, param\n"
        '@module(id="bad_param", title="x", params=[param("p", "select", 1)])\n'
        "def run(ctx, p): ...\n"
    )
    (d / "_helper.py").write_text("raise RuntimeError('should never be imported')\n")
    monkeypatch.setenv("XRADAR_MODULES_DIR", str(d))
    plugins.discover_modules()

    failed = {Path(e["file"]).name: e["error"] for e in plugins.LOAD_ERRORS}
    assert set(failed) == {"b_raises.py", "c_syntax.py", "d_badparam.py"}
    assert "boom on import" in failed["b_raises.py"] and "RuntimeError" in failed["b_raises.py"]
    assert "SyntaxError" in failed["c_syntax.py"]
    assert "tmp_good" in plugins.MODULES and "storm_detection" in plugins.MODULES
    assert "bad_param" not in plugins.MODULES
    assert plugins.list_modules()["errors"] == plugins.LOAD_ERRORS
    out = run("tmp_good", tree, k=3)
    assert blocks_by_type(out)["scalars"]["items"][0] == {"label": "k", "value": 3.0}


def test_user_plugin_overrides_builtin_by_id(registry, tmp_path, monkeypatch, tree):
    d = tmp_path / "override"
    d.mkdir()
    (d / "my_storms.py").write_text(GOOD_PLUGIN.format(mid="storm_detection", title="My Storms"))
    monkeypatch.setenv("XRADAR_MODULES_DIR", str(d))
    plugins.discover_modules()
    spec = plugins.MODULES["storm_detection"]
    assert spec.title == "My Storms" and spec.source == str(d / "my_storms.py")
    assert plugins.MODULES["cell_size_sort"].title != "My Storms"
    assert not plugins.LOAD_ERRORS
    assert run("storm_detection", tree)["blocks"][0]["type"] == "scalars"


def test_plugin_exception_becomes_module_error(registry, tmp_path, monkeypatch, tree):
    d = tmp_path / "crash"
    d.mkdir()
    (d / "crash.py").write_text(
        "from engine.plugins import module\n"
        '@module(id="crashy", title="Crashy")\n'
        "def run(ctx):\n    return 1 / 0\n"
    )
    (d / "wrong_return.py").write_text(
        "from engine.plugins import module\n"
        '@module(id="wrong_return", title="Wrong")\n'
        'def run(ctx):\n    return {"not": "a Result"}\n'
    )
    monkeypatch.setenv("XRADAR_MODULES_DIR", str(d))
    plugins.discover_modules()
    with pytest.raises(ModuleError, match="ZeroDivisionError"):
        run("crashy", tree)
    with pytest.raises(ModuleError, match="must return a Result"):
        run("wrong_return", tree)


# ---------------------------------------------------------------------------
# field blocks and optional-package availability
# ---------------------------------------------------------------------------


def test_result_field_block_and_array_are_separate():
    import numpy as np

    from engine.plugins import Result

    arr = np.arange(12, dtype=float).reshape(3, 4)
    arr[0, 0] = np.nan
    res = Result().field("demo", arr, units="dBZ")
    block = res.to_dict()["blocks"][0]
    assert block["type"] == "field" and block["shape"] == [3, 4]
    assert block["vmin"] < block["vmax"]
    json.dumps(res.to_dict(), allow_nan=False)  # no arrays / NaN in the JSON part
    assert res.fields["demo"].dtype == np.float32 and res.fields["demo"].shape == (3, 4)


def test_result_field_rejects_non_2d():
    import numpy as np
    import pytest

    from engine.plugins import Result

    with pytest.raises(ValueError):
        Result().field("bad", np.zeros(5))


def test_module_needs_missing_package_is_listed_but_refuses_to_run(tmp_path, monkeypatch):
    from engine import plugins

    (tmp_path / "needy.py").write_text(
        "from engine.plugins import module, Result\n"
        "@module(id='needy', title='Needy', needs=['definitely_not_installed_pkg'])\n"
        "def run(ctx):\n    return Result()\n"
    )
    monkeypatch.setenv("XRADAR_MODULES_DIR", str(tmp_path))
    plugins.discover_modules()
    try:
        info = next(m for m in plugins.list_modules()["modules"] if m["id"] == "needy")
        assert info["available"] is False
        assert "definitely_not_installed_pkg" in info["unavailable_reason"]
        assert plugins.LOAD_ERRORS == []
        with pytest.raises(plugins.ModuleError, match="definitely_not_installed_pkg"):
            plugins.run_module("needy", None, 0, {}, get_sweep_dataset=lambda i: None)
    finally:
        monkeypatch.delenv("XRADAR_MODULES_DIR")
        plugins.discover_modules()


# ---------------------------------------------------------------------------
# Module scope (per-sweep vs whole-volume)
# ---------------------------------------------------------------------------


def test_scope_defaults_to_sweep_and_is_described():
    before = dict(plugins.MODULES)
    try:

        @plugins.module(id="scope_default", title="Default")
        def _a(ctx):
            return plugins.Result()

        @plugins.module(id="scope_volume", title="Volume", scope="volume")
        def _b(ctx):
            return plugins.Result()

        info = {m["id"]: m for m in plugins.list_modules()["modules"]}
        assert (info["scope_default"]["scope"], info["scope_default"]["per_sweep"]) == (
            "sweep",
            True,
        )
        assert (info["scope_volume"]["scope"], info["scope_volume"]["per_sweep"]) == (
            "volume",
            False,
        )
        json.dumps(info["scope_volume"], allow_nan=False)
    finally:
        plugins.MODULES.clear()
        plugins.MODULES.update(before)


def test_invalid_scope_is_rejected():
    with pytest.raises(ValueError, match="scope"):

        @plugins.module(id="scope_bad", title="Bad", scope="galaxy")
        def _c(ctx):  # pragma: no cover - the decorator raises
            return plugins.Result()

    assert "scope_bad" not in plugins.MODULES


def test_builtin_volume_modules_are_marked_volume(registry):
    volume = {
        "radarx_vil",
        "radarx_hail",
        "radarx_zdr_column",
        "radarx_zdr_arc",
        "radarx_kdp_column",
        "radarx_vvp_winds",
        "radarx_tornado_prob",
        "radarx_melting_layer",
        "radarx_qvp",
        "hydro_basin_rate",
    }
    for mid, spec in registry.MODULES.items():
        if mid in volume:
            assert spec.scope == "volume" and not spec.per_sweep, mid
        else:
            assert spec.scope == "sweep" and spec.per_sweep, mid
