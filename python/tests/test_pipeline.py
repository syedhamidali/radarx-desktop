"""Processing pipelines (recipes): step catalog, mask accumulation, one derived variable, reuse."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import xarray as xr

from engine import derived, pipeline, plugins
from engine.plugins import ModuleError, Result, param

pytestmark = pytest.mark.filterwarnings("ignore")

HAS_RADARX = importlib.util.find_spec("radarx") is not None
needs_radarx = pytest.mark.skipif(not HAS_RADARX, reason="radarx not installed")

N_AZ, N_RNG, DR, NYQ = 360, 320, 250.0, 26.0
AZ = np.arange(N_AZ, dtype=float)
RNG = DR * (1 + np.arange(N_RNG))


# ---------------------------------------------------------------------------
# Synthetic scene with known truth
# ---------------------------------------------------------------------------


def make_scene(seed: int = 3, speckle: int = 60, wind: float = 36.0) -> dict[str, Any]:
    """Uniform wind (aliased, Nyquist 26 m/s) + a clutter patch + isolated speckle.

    ``truth`` is the velocity a perfect pipeline returns: the dealiased wind on the weather echo,
    NaN on the clutter patch and on the speckle gates.
    """
    rng = np.random.default_rng(seed)
    a = np.deg2rad(AZ)[:, None]
    r = RNG[None, :]
    echo = np.broadcast_to((r > 3000) & (r < 60000), (N_AZ, N_RNG)).copy()
    dbz = 25.0 + 8.0 * np.sin(r / 9000.0) + 4.0 * np.cos(2 * a) + np.zeros((N_AZ, N_RNG))
    true_v = wind * np.cos(a - np.deg2rad(60.0)) * np.cos(np.deg2rad(0.5)) + np.zeros((N_AZ, N_RNG))
    clutter = np.zeros((N_AZ, N_RNG), dtype=bool)
    clutter[100:130, 16:40] = True  # r = 4-10 km
    dbz_obs = np.where(echo, dbz, np.nan)
    v_true = np.where(echo, true_v, np.nan)
    v_obs = np.where(echo, true_v, np.nan)
    dbz_obs[clutter] = rng.uniform(35, 60, clutter.sum())
    v_obs[clutter] = rng.normal(0.0, 0.3, clutter.sum())
    v_true[clutter] = np.nan
    spk = np.zeros((N_AZ, N_RNG), dtype=bool)
    placed = 0
    while placed < speckle:
        i, j = int(rng.integers(5, N_AZ - 5)), int(rng.integers(250, N_RNG - 5))
        if spk[i - 3 : i + 4, j - 3 : j + 4].any():
            continue
        spk[i, j] = True
        placed += 1
    dbz_obs[spk] = rng.uniform(0, 20, speckle)
    v_obs[spk] = rng.uniform(-20, 20, speckle)
    v_true[spk] = np.nan
    aliased = np.where(np.isfinite(v_obs), ((v_obs + NYQ) % (2 * NYQ)) - NYQ, np.nan)
    ds = xr.Dataset(
        {
            "DBZH": (("azimuth", "range"), dbz_obs.astype(np.float32), {"units": "dBZ"}),
            "VRADH": (("azimuth", "range"), aliased.astype(np.float32), {"units": "m/s"}),
        },
        coords={
            "azimuth": AZ,
            "range": RNG,
            "elevation": ("azimuth", np.full(N_AZ, 0.5, dtype=np.float32)),
        },
    )
    ds["sweep_fixed_angle"] = ((), 0.5)
    ds["nyquist_velocity"] = ((), NYQ)
    return {
        "ds": ds,
        "truth": v_true.astype(np.float32),
        "clutter": clutter,
        "speckle": spk,
        "echo": echo & ~clutter,
    }


def make_tree(sweeps: list[xr.Dataset]) -> xr.DataTree:
    root = xr.Dataset(coords={"latitude": 35.0, "longitude": -97.0, "altitude": 300.0})
    nodes = {"/": root}
    for i, ds in enumerate(sweeps):
        nodes[f"/sweep_{i}"] = ds
    return xr.DataTree.from_dict(nodes)


def ctx_for(tree: xr.DataTree, sweep: int = 0) -> plugins.ModuleContext:
    names = sorted(n for n in tree.children if n.startswith("sweep"))
    return plugins.ModuleContext(
        tree, sweep, get_sweep_dataset=lambda i: tree[names[i]].to_dataset()
    )


def recipe(target: str, *steps: tuple[str, dict[str, Any]] | tuple[str, dict, bool], **kw: Any):
    items = [
        {"step_id": s[0], "params": s[1], "enabled": s[2] if len(s) > 2 else True} for s in steps
    ]
    return pipeline.parse_recipe({"name": "Test recipe", "target": target, "steps": items, **kw})


CLEAN_STEPS = (
    ("clutter_filter", {"rule": "either", "tdbz_max": 50.0}),
    ("dealias", {"method": "region", "nyquist_velocity": NYQ}),
    ("speckle", {"min_size": 8}),
)


@pytest.fixture()
def scene():
    return make_scene()


@pytest.fixture()
def recipes_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XRADAR_RECIPES_DIR", str(tmp_path / "recipes"))
    return tmp_path / "recipes"


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def test_step_catalog_schema():
    steps = pipeline.list_steps()
    ids = {s["id"] for s in steps}
    assert {
        "dealias",
        "snr_gate",
        "rhohv_gate",
        "echo_mask",
        "clutter_filter",
        "biological_filter",
        "speckle",
        "despike",
        "smooth",
        "gap_fill",
        "range_clip",
        "noise_correction",
    } <= ids
    json.dumps(steps)  # the UI gets it as JSON
    for s in steps:
        assert s["kind"] in ("filter", "transform")
        assert s["title"] and s["description"] and s["category"]
        for role in [*s["needs"], *s["optional"]]:
            assert role in plugins.VAR_ALIASES, (s["id"], role)
        if s["kind"] == "transform":
            assert s["transforms"]
        names = [p["name"] for p in s["params"]]
        assert len(names) == len(set(names))
        for p in s["params"]:
            assert p["type"] in plugins.PARAM_TYPES and "default" in p and p["label"]
            if p["type"] == "select":
                assert p["default"] in p["options"]
        # defaults must validate and survive the coercion unchanged
        coerced = plugins._coerce_params(s["params"], {})
        assert coerced == {p["name"]: p["default"] for p in s["params"]} or all(
            coerced[p["name"]] == p["default"] or isinstance(p["default"], float)
            for p in s["params"]
        )
        spec = pipeline.get_step(s["id"])
        assert spec.script, f"{s['id']} has no export snippet"


def test_param_helper_reused():
    assert param("x", "number", 1.0)["type"] == "number"  # steps use the module SDK helper


def test_builtin_recipes_valid():
    built = pipeline.builtin_recipes()
    assert len(built) >= 3 and all(r.builtin for r in built)
    assert {"VRADH", "DBZH", "ZDR"} <= {r.target for r in built}
    for r in built:
        assert len(r.steps) >= 3
        pipeline.recipe_to_script(r)  # every shipped recipe exports


def test_parse_recipe_validation():
    ok = {"name": "x", "target": "DBZH", "steps": [{"step_id": "speckle", "params": {}}]}
    assert pipeline.parse_recipe(ok).steps[0]["params"] == {"min_size": 10}
    for bad in (
        {**ok, "name": ""},
        {**ok, "target": ""},
        {**ok, "steps": []},
        {**ok, "steps": [{"step_id": "nope"}]},
        {**ok, "steps": [{"step_id": "speckle", "params": {"min_size": "abc"}}]},
        {**ok, "steps": [{"step_id": "speckle"}] * 31},
        "not a dict",
    ):
        with pytest.raises(ModuleError):
            pipeline.parse_recipe(bad)
    clamped = pipeline.parse_recipe(
        {**ok, "steps": [{"step_id": "speckle", "params": {"min_size": 99999}}]}
    )
    assert clamped.steps[0]["params"]["min_size"] == 2000


def test_role_lookup_helpers():
    assert pipeline.canonical_role("velocity") == "VRADH"
    assert pipeline.canonical_role("dbz") == "DBZH"
    assert pipeline.canonical_role("MyField") == "MyField"
    ds = xr.Dataset({"VEL": (("azimuth", "range"), np.zeros((2, 2)))})
    assert pipeline.find_variable(ds, "VRADH") == "VEL"
    assert pipeline.find_variable(ds, "velocity") == "VEL"
    assert pipeline.find_variable(ds, "DBZH") is None


# ---------------------------------------------------------------------------
# Mask accumulation and ordering
# ---------------------------------------------------------------------------


def test_mask_accumulates_and_stats_add_up(scene):
    tree = make_tree([scene["ds"]])
    rec = recipe(
        "DBZH",
        ("range_clip", {"max_range_km": 40.0}),
        ("range_clip", {"min_value": 24.0}),
    )
    out = pipeline.run_recipe_on_sweep(rec, ctx_for(tree))
    z = scene["ds"]["DBZH"].values
    expected = np.isfinite(z) & (RNG[None, :] <= 40000.0) & (z >= 24.0)
    assert np.array_equal(np.isfinite(out.after), expected)
    removed = sum(s["removed"] for s in out.steps)
    assert removed == out.n_valid_before - out.n_valid_after
    assert out.steps[-1]["valid"] == out.n_valid_after
    # first step to remove a gate is the one credited
    first = out.removed_by == 1
    assert first.sum() == out.steps[0]["removed"]


def test_order_matters_filter_then_fill_vs_fill_then_filter(scene):
    tree = make_tree([scene["ds"]])
    punch = ("range_clip", {"min_value": 24.0})  # removes the low-reflectivity part
    fill = ("gap_fill", {"radius_km": 1.5, "min_coverage": 0.3})
    a = pipeline.run_recipe_on_sweep(recipe("DBZH", punch, fill), ctx_for(tree))
    b = pipeline.run_recipe_on_sweep(recipe("DBZH", fill, punch), ctx_for(tree))
    assert a.steps[1]["filled"] > 0  # filter first: the fill re-admits gates next to kept data
    assert b.steps[0]["filled"] == 0  # nothing removed yet: nothing to fill
    assert a.n_valid_after > b.n_valid_after
    assert not np.array_equal(np.isfinite(a.after), np.isfinite(b.after))
    # gates re-admitted by the fill are not reported as removed
    assert np.isnan(a.removed_by[np.isfinite(a.after)]).all()


def test_disabled_and_skipped_steps(scene):
    tree = make_tree([scene["ds"]])
    rec = recipe(
        "DBZH",
        ("snr_gate", {}),  # no SNR on this sweep: skipped, not an error
        ("range_clip", {"max_range_km": 20.0}, False),  # disabled
        ("speckle", {"min_size": 4}),
    )
    out = pipeline.run_recipe_on_sweep(rec, ctx_for(tree))
    assert [s["status"] for s in out.steps] == ["skipped", "disabled", "ok"]
    assert "SNR" in out.steps[0]["reason"]
    assert out.steps[1]["removed"] == 0


def test_required_role_missing_raises(scene):
    tree = make_tree([scene["ds"].drop_vars("VRADH")])
    with pytest.raises(ModuleError, match="VRADH"):
        pipeline.run_recipe_on_sweep(recipe("VRADH", *CLEAN_STEPS), ctx_for(tree))


# ---------------------------------------------------------------------------
# Truth recovery: aliased velocity + clutter + speckle
# ---------------------------------------------------------------------------


def _score(out, scene, tol=1.5):
    truth, after = scene["truth"], out.after
    good = np.isfinite(truth)
    recovered = good & np.isfinite(after)
    within = recovered & (np.abs(after - truth) <= tol)
    return {
        "completeness": recovered.sum() / good.sum(),
        "accuracy": within.sum() / max(recovered.sum(), 1),
        "clutter_left": (np.isfinite(after) & scene["clutter"]).sum() / scene["clutter"].sum(),
        "speckle_left": (np.isfinite(after) & scene["speckle"]).sum() / scene["speckle"].sum(),
    }


@needs_radarx
def test_chain_recovers_truth_with_radarx(scene):
    tree = make_tree([scene["ds"]])
    out = pipeline.run_recipe_on_sweep(recipe("VRADH", *CLEAN_STEPS), ctx_for(tree))
    s = _score(out, scene)
    assert s["clutter_left"] < 0.02, s
    assert s["speckle_left"] < 0.02, s
    assert s["completeness"] > 0.97, s
    assert s["accuracy"] > 0.98, s
    dealias = next(r for r in out.steps if r["step_id"] == "dealias")
    assert dealias["modified"] > 1000  # a lot of the field really was folded
    # raw velocity was far off before the pipeline
    raw_err = np.nanmean(np.abs(scene["ds"]["VRADH"].values - scene["truth"]) > 1.5)
    assert raw_err > 0.15


def test_chain_recovers_truth_builtin_dealias(scene):
    tree = make_tree([scene["ds"]])
    steps = (
        CLEAN_STEPS[0],
        ("dealias", {"method": "builtin", "nyquist_velocity": NYQ}),
        CLEAN_STEPS[2],
    )
    out = pipeline.run_recipe_on_sweep(recipe("VRADH", *steps), ctx_for(tree))
    s = _score(out, scene)
    assert s["clutter_left"] < 0.02 and s["speckle_left"] < 0.02, s
    assert s["completeness"] > 0.97, s
    assert s["accuracy"] > 0.95, s


def test_clutter_first_helps_dealias(scene):
    """Order matters for transforms too: clutter gates (V~0) confuse folding if left in."""
    tree = make_tree([scene["ds"]])
    dealias = ("dealias", {"method": "builtin", "nyquist_velocity": NYQ})
    good = pipeline.run_recipe_on_sweep(
        recipe("VRADH", CLEAN_STEPS[0], dealias, CLEAN_STEPS[2]), ctx_for(tree)
    )
    late = pipeline.run_recipe_on_sweep(
        recipe("VRADH", dealias, CLEAN_STEPS[0], CLEAN_STEPS[2]), ctx_for(tree)
    )
    assert _score(good, scene)["accuracy"] >= _score(late, scene)["accuracy"] - 1e-9


def test_despike_smooth_and_range_clip():
    rng = np.random.default_rng(0)
    smooth_field = 30 + 5 * np.sin(RNG[None, :] / 4000.0) + np.zeros((N_AZ, N_RNG))
    field = smooth_field.copy()
    spikes = rng.random(field.shape) < 0.002
    field[spikes] += 40
    ds = xr.Dataset(
        {"DBZH": (("azimuth", "range"), field.astype(np.float32))},
        coords={"azimuth": AZ, "range": RNG},
    )
    tree = make_tree([ds])
    out = pipeline.run_recipe_on_sweep(recipe("DBZH", ("despike", {})), ctx_for(tree))
    assert (np.isfinite(out.after) & spikes).sum() < 0.1 * spikes.sum()
    assert np.isfinite(out.after).mean() > 0.995

    noisy = (smooth_field + rng.normal(0, 3, field.shape)).astype(np.float32)
    ds2 = ds.assign(DBZH=(("azimuth", "range"), noisy))
    sm = pipeline.run_recipe_on_sweep(
        recipe("DBZH", ("smooth", {"method": "median", "window": 5})), ctx_for(make_tree([ds2]))
    )
    err_before = np.abs(noisy - smooth_field).mean()
    err_after = np.nanmean(np.abs(sm.after - smooth_field))
    assert err_after < 0.6 * err_before
    assert sm.steps[0]["modified"] > 0.9 * noisy.size
    g = pipeline.run_recipe_on_sweep(
        recipe("DBZH", ("smooth", {"method": "gaussian", "radius_km": 1.0})),
        ctx_for(make_tree([ds2])),
    )
    assert np.nanmean(np.abs(g.after - smooth_field)) < err_before


def test_biological_filter_rules():
    shape = (N_AZ, N_RNG)
    z = np.full(shape, 35.0)
    z[:, 100:150] = 12.0  # weak echo
    rho = np.full(shape, 0.98)
    rho[:, 100:125] = 0.5  # weak + low RhoHV -> biology
    zdr = np.full(shape, 0.5)
    ds = xr.Dataset(
        {
            "DBZH": (("azimuth", "range"), z.astype(np.float32)),
            "RHOHV": (("azimuth", "range"), rho.astype(np.float32)),
            "ZDR": (("azimuth", "range"), zdr.astype(np.float32)),
        },
        coords={"azimuth": AZ, "range": RNG},
    )
    out = pipeline.run_recipe_on_sweep(
        recipe("DBZH", ("biological_filter", {})), ctx_for(make_tree([ds]))
    )
    assert np.isnan(out.after[:, 100:125]).all()
    assert np.isfinite(out.after[:, 125:150]).all()  # weak but RhoHV is high: rain, kept
    assert np.isfinite(out.after[:, :100]).all()


# ---------------------------------------------------------------------------
# All sweeps -> exactly one derived variable
# ---------------------------------------------------------------------------


def _volume(scene):
    no_vel = scene["ds"].drop_vars("VRADH")
    return make_tree([scene["ds"], no_vel, make_scene(seed=5)["ds"]])


def _run_all(tree, rec, **kw):
    names = sorted(n for n in tree.children if n.startswith("sweep"))
    return pipeline.run_recipe_all_sweeps(
        rec,
        tree,
        len(names),
        get_sweep_dataset=lambda i: tree[names[i]].to_dataset(),
        get_sweep_variables=lambda i: {
            str(k) for k, v in tree[names[i]].to_dataset().data_vars.items() if v.ndim == 2
        },
        **kw,
    )


def test_one_derived_variable_for_a_multi_step_run(scene):
    tree = _volume(scene)
    rec = recipe("VRADH", *CLEAN_STEPS)
    msgs: list[tuple[int, str]] = []
    run = _run_all(tree, rec, progress=lambda p, m: msgs.append((p, m)))
    assert [s["sweep"] for s in run.skipped] == [1]
    assert "VRADH" in run.skipped[0]["reason"]
    assert sorted(run.arrays["VRADH"]) == [0, 2]
    assert msgs and msgs[-1][0] <= 95
    assert [s["step_id"] for s in run.steps] == ["clutter_filter", "dealias", "speckle"]
    assert run.steps[0]["removed"] > 0 and run.steps[2]["removed"] > 0

    built = pipeline.build_variables(run)
    assert len(built) == 1  # ONE variable, however many steps ran
    name, _sweeps, meta = built[0]
    assert name == "pipeline:test_recipe_vradh"
    derived.validate_name(name)
    assert meta["module"] == "pipeline" and meta["field"] == "VRADH" and meta["cmap"] == "NWSVel"
    assert meta["vmin"] == -meta["vmax"] and meta["units"] == "m/s"
    prov = meta["params"]
    assert prov["recipe"] == "Test recipe"
    assert [s["step"] for s in prov["steps"]] == ["clutter_filter", "dealias", "speckle"]
    assert prov["steps"][1]["params"]["nyquist_velocity"] == NYQ

    store = derived.DerivedStore(max_bytes=64 * 1024 * 1024)
    for n, arrays, m in built:
        store.put_variable(n, arrays, m)
    assert len(store) == 1
    listed = store.variables()[0]
    assert listed["module"] == "pipeline" and listed["sweeps"] == [0, 2]
    # re-running replaces it (still one variable)
    store.put_variable(*built[0])
    assert len(store) == 1


def test_apply_to_all_option_stores_masked_companions(scene):
    tree = _volume(scene)
    rec = recipe("VRADH", *CLEAN_STEPS, apply_to_all=True)
    run = _run_all(tree, rec)
    built = pipeline.build_variables(run)
    assert [b[0] for b in built] == [
        "pipeline:test_recipe_vradh",
        "pipeline:test_recipe_dbzh",
    ]
    v, z = built[0][1][0], built[1][1][0]
    assert np.array_equal(np.isfinite(v), np.isfinite(z) | (np.isfinite(v) & ~np.isfinite(z)))


def test_all_sweeps_every_sweep_fails_is_an_error(scene):
    tree = make_tree([scene["ds"].drop_vars("VRADH")] * 2)
    with pytest.raises(ModuleError, match="could not be applied"):
        _run_all(tree, recipe("VRADH", *CLEAN_STEPS))


def test_all_sweeps_cancel(scene):
    tree = _volume(scene)
    with pytest.raises(derived.RunCancelledError):
        _run_all(tree, recipe("VRADH", *CLEAN_STEPS), is_cancelled=lambda: True)


def test_subset_of_sweeps(scene):
    tree = _volume(scene)
    run = _run_all(tree, recipe("VRADH", *CLEAN_STEPS), sweeps=[2])
    assert sorted(run.arrays["VRADH"]) == [2] and not run.skipped


def test_preview_fields_blocks(scene):
    tree = make_tree([scene["ds"]])
    rec = recipe("VRADH", *CLEAN_STEPS)
    out = pipeline.run_recipe_on_sweep(rec, ctx_for(tree))
    blocks, fields = pipeline.preview_fields(out, rec)
    assert [b["name"] for b in blocks] == ["Before", "After", "Removed by step"]
    assert set(fields) == {b["name"] for b in blocks}
    cats = blocks[2]["categories"]
    assert {c["value"] for c in cats} == {1, 3}  # clutter (step 1) and speckle (step 3)
    json.dumps(blocks)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_recipe_json_roundtrip(recipes_dir):
    rec = recipe("VRADH", *CLEAN_STEPS, description="d", apply_to_all=True)
    rec.name = "My velocity cleaner"
    saved = pipeline.save_recipe(rec.to_dict())
    files = list(recipes_dir.glob("*.json"))
    assert [f.name for f in files] == ["my_velocity_cleaner.json"]
    on_disk = json.loads(files[0].read_text())
    assert on_disk["steps"][1]["params"]["nyquist_velocity"] == NYQ
    listing = pipeline.list_recipes()
    user = [r for r in listing["recipes"] if not r["builtin"]]
    assert len(user) == 1 and user[0] == saved.to_dict()
    again = pipeline.parse_recipe(user[0])
    assert again.to_dict() == saved.to_dict()
    assert any(r["builtin"] for r in listing["recipes"])
    assert pipeline.delete_recipe("My velocity cleaner") is True
    assert pipeline.delete_recipe("My velocity cleaner") is False
    assert not list(recipes_dir.glob("*.json"))


def test_builtins_are_protected_and_bad_files_reported(recipes_dir):
    builtin = pipeline.builtin_recipes()[0]
    with pytest.raises(ModuleError, match="built-in"):
        pipeline.save_recipe(builtin.to_dict())
    with pytest.raises(ModuleError, match="Built-in"):
        pipeline.delete_recipe(builtin.name)
    recipes_dir.mkdir(parents=True)
    (recipes_dir / "broken.json").write_text("{not json")
    (recipes_dir / "wrong.json").write_text(
        json.dumps({"name": "x", "target": "DBZH", "steps": []})
    )
    listing = pipeline.list_recipes()
    assert len(listing["errors"]) == 2
    assert len(listing["recipes"]) == len(pipeline.builtin_recipes())


def test_recipes_dir_default(monkeypatch):
    monkeypatch.delenv("XRADAR_RECIPES_DIR", raising=False)
    assert pipeline.recipes_dir() == Path.home() / ".xradar-desktop" / "recipes"


# ---------------------------------------------------------------------------
# Downstream use: a module consumes the pipeline output through overrides
# ---------------------------------------------------------------------------


@pytest.fixture()
def probe_module():
    @plugins.module(id="pipeline_probe", title="Probe", requires=["VRADH"], scope="sweep")
    def probe(ctx):
        v = ctx.field("VRADH").values
        res = Result()
        res.scalars({"valid": int(np.isfinite(v).sum()), "mean": float(np.nanmean(v))})
        res.field("v", v, units="m/s")
        return res

    yield "pipeline_probe"
    plugins.MODULES.pop("pipeline_probe", None)


def _stored(scene):
    tree = make_tree([scene["ds"], make_scene(seed=5)["ds"]])
    run = _run_all(tree, recipe("VRADH", *CLEAN_STEPS))
    store = derived.DerivedStore(max_bytes=64 * 1024 * 1024)
    for built in pipeline.build_variables(run):
        store.put_variable(*built)
    return tree, store, pipeline.build_variables(run)[0][0]


def test_module_consumes_pipeline_output_via_overrides(scene, probe_module):
    tree, store, name = _stored(scene)
    getter = lambda i: tree[f"sweep_{i}"].to_dataset()  # noqa: E731
    plain = plugins.run_module(probe_module, tree, 0, {}, get_sweep_dataset=getter)
    ov = pipeline.module_overrides(store, {"velocity": name})
    assert set(ov) == {"VRADH"}
    out = plugins.run_module(probe_module, tree, 0, {}, get_sweep_dataset=getter, overrides=ov)
    scal = {i["label"]: i["value"] for i in out["blocks"][0]["items"]}
    base = {i["label"]: i["value"] for i in plain["blocks"][0]["items"]}
    assert scal["valid"] < base["valid"]  # clutter + speckle gates are gone
    stored_v = store.get(0, name)[0]
    assert scal["valid"] == int(np.isfinite(stored_v).sum())
    assert abs(scal["mean"] - float(np.nanmean(stored_v))) < 1e-4


def test_overrides_in_all_sweeps_run_and_missing_sweep_is_skipped(scene, probe_module):
    tree, store, name = _stored(scene)
    # remove sweep 1 of the derived variable: that sweep must be skipped, never silently raw
    var = store._vars[name]
    del var.sweeps[1]
    getter = lambda i: tree[f"sweep_{i}"].to_dataset()  # noqa: E731
    run = derived.run_all_sweeps(
        probe_module,
        tree,
        2,
        {},
        get_sweep_dataset=getter,
        get_sweep_variables=lambda i: {"DBZH", "VRADH"},
        get_sweep_grid=lambda i: (N_AZ, N_RNG),
        overrides=pipeline.module_overrides(store, {"VRADH": name}),
    )
    assert sorted(run.arrays["v"]) == [0]
    assert (
        run.skipped and run.skipped[0]["sweep"] == 1 and "not available" in run.skipped[0]["reason"]
    )


def test_module_overrides_validation(scene):
    _, store, name = _stored(scene)
    assert pipeline.module_overrides(store, None) is None
    assert pipeline.module_overrides(store, {}) is None
    with pytest.raises(ModuleError, match="does not exist"):
        pipeline.module_overrides(store, {"velocity": "pipeline:nope"})
    with pytest.raises(ModuleError, match="inputs"):
        pipeline.module_overrides(store, {"velocity": 3})
    with pytest.raises(ModuleError):
        pipeline.module_overrides(None, {"velocity": name})


def test_override_shape_mismatch_is_an_error(scene, probe_module):
    tree, _, _ = _stored(scene)
    getter = lambda i: tree[f"sweep_{i}"].to_dataset()  # noqa: E731
    with pytest.raises(ModuleError, match="shape"):
        plugins.run_module(
            probe_module,
            tree,
            0,
            {},
            get_sweep_dataset=getter,
            overrides={"VRADH": lambda s: np.zeros((3, 3), dtype=np.float32)},
        )


def test_derived_export_carries_provenance(scene):
    tree, store, name = _stored(scene)
    out = derived.tree_with_derived(tree, store, names=[name])
    var = out["sweep_0"].to_dataset()[derived.export_name(name)]
    assert "pipeline" in var.attrs["comment"] and "clutter_filter" in var.attrs["comment"]
    assert var.attrs["units"] == "m/s"


# ---------------------------------------------------------------------------
# Script export
# ---------------------------------------------------------------------------


def test_exported_script_matches_engine(scene):
    steps = (
        ("range_clip", {"min_range_km": 2.0}),
        CLEAN_STEPS[0],
        ("dealias", {"method": "builtin", "nyquist_velocity": NYQ}),
        ("despike", {}),
        ("speckle", {"min_size": 8}),
        ("smooth", {"method": "median", "window": 3}),
        ("gap_fill", {"radius_km": 0.6}),
    )
    rec = recipe("VRADH", *steps)
    text = pipeline.recipe_to_script(rec)
    compile(text, "<recipe_script>", "exec")
    assert "def process_sweep" in text and "unfold_velocity" in text
    assert "xradar_module" not in text and "from engine" not in text  # standalone
    ns: dict[str, Any] = {"__name__": "recipe_script"}
    exec(compile(text, "<recipe_script>", "exec"), ns)
    ns["rt"] = None  # the numpy path, identical to the engine's builtin dealias
    got = ns["process_sweep"](scene["ds"]).values
    tree = make_tree([scene["ds"]])
    want = pipeline.run_recipe_on_sweep(rec, ctx_for(tree)).after
    assert np.array_equal(np.isfinite(got), np.isfinite(want))
    assert np.allclose(got[np.isfinite(got)], want[np.isfinite(want)], atol=1e-4)


@needs_radarx
def test_exported_script_with_radarx_matches_engine(scene):
    rec = recipe("VRADH", *CLEAN_STEPS)
    ns: dict[str, Any] = {"__name__": "recipe_script"}
    exec(compile(pipeline.recipe_to_script(rec), "<recipe_script>", "exec"), ns)
    assert ns["rt"] is not None
    got = ns["process_sweep"](scene["ds"]).values
    want = pipeline.run_recipe_on_sweep(rec, ctx_for(make_tree([scene["ds"]]))).after
    both = np.isfinite(got) & np.isfinite(want)
    assert np.array_equal(np.isfinite(got), np.isfinite(want))
    assert np.allclose(got[both], want[both], atol=1e-4)


def test_script_for_every_step_compiles():
    items = [{"step_id": s["id"], "params": {}} for s in pipeline.list_steps()]
    rec = pipeline.parse_recipe({"name": "all", "target": "DBZH", "steps": items})
    compile(pipeline.recipe_to_script(rec), "<all>", "exec")


# ---------------------------------------------------------------------------
# Real files (skipped when absent)
# ---------------------------------------------------------------------------

KTLX = Path(os.path.expanduser("~/Downloads/Others/XRADAR_EXAMPLES/KTLX20130520_201643_V06.gz"))
COW = Path(
    "/Users/syed44/Downloads/Others/XRADAR_EXAMPLES/RADAR_CORRECTED/"
    "cfrad.20220322_220338.299_to_20220322_220506.977_COW1high_SUR.nc"
)


def _real_run(reader, rec, sweeps=None):
    from engine.reader import sorted_sweep_names

    tree = reader.datatree
    n = len(sorted_sweep_names(tree))
    return pipeline.run_recipe_all_sweeps(
        rec,
        tree,
        n,
        get_sweep_dataset=lambda i: reader.get_sweep_dataset(i),
        get_sweep_variables=lambda i: {
            str(k)
            for k, v in reader.get_sweep_dataset(i, mask=False).data_vars.items()
            if v.ndim >= 2
        },
        sweeps=sweeps,
    )


@pytest.mark.skipif(not KTLX.exists(), reason="KTLX example file not available")
def test_ktlx_velocity_recipe_all_sweeps():
    from engine.reader import RadarReader

    reader = RadarReader()
    reader.open_file(str(KTLX))
    rec = pipeline.builtin_recipes()[0]
    run = _real_run(reader, rec)
    odd = sorted(run.arrays["VRADH"])
    assert odd and all(s % 2 == 1 for s in odd[:3])  # Doppler cuts are the odd sweeps
    assert any(s["sweep"] == 0 for s in run.skipped)  # dual-pol cut has no velocity
    assert all("VRADH" in s["reason"] for s in run.skipped)
    built = pipeline.build_variables(run)
    assert len(built) == 1
    ctx = plugins.ModuleContext(
        reader.datatree, 1, get_sweep_dataset=lambda i: reader.get_sweep_dataset(i)
    )
    sw = pipeline.run_recipe_on_sweep(rec, ctx)
    ids = [r["step_id"] for r in sw.steps]
    assert ids[:5] == ["snr_gate", "clutter_filter", "biological_filter", "dealias", "speckle"]
    bio = sw.steps[2]
    assert bio["status"] == "ok"  # RhoHV/ZDR borrowed from the dual-pol cut at the same elevation
    assert any("sweep 0" in n for n in sw.notes)
    assert 0.3 < sw.n_valid_after / sw.n_valid_before < 1.0
    assert sw.steps[3]["modified"] > 0  # real folded velocities were unfolded


@pytest.mark.skipif(not KTLX.exists(), reason="KTLX example file not available")
def test_ktlx_dualpol_recipes_sweep0():
    from engine.reader import RadarReader

    reader = RadarReader()
    reader.open_file(str(KTLX))
    for rec in pipeline.builtin_recipes()[1:]:
        run = _real_run(reader, rec, sweeps=[0])
        assert sorted(run.arrays[rec.target_role]) == [0]
        step_rows = {r["step_id"]: r for r in run.steps}
        assert step_rows["speckle"]["status"] == "ok"
        assert run.steps[-1]["valid_pct"] < 100.0


@pytest.mark.skipif(not COW.exists(), reason="COW example file not available")
def test_cow_cfradial_recipes():
    from engine.reader import RadarReader

    reader = RadarReader()
    reader.open_file(str(COW))
    for rec in pipeline.builtin_recipes():
        run = _real_run(reader, rec, sweeps=[0, 1])
        assert sorted(run.arrays[rec.target_role]) == [0, 1]
        assert len(pipeline.build_variables(run)) == 1
        assert run.steps[0]["status"] in ("ok", "skipped")
    # velocity target resolved through the file's own alias (VEL) and its nyquist_velocity
    run = _real_run(reader, pipeline.builtin_recipes()[0], sweeps=[0])
    dealias = next(r for r in run.steps if r["step_id"] == "dealias")
    assert any("(file)" in n for n in dealias["notes"])
