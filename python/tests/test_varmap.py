"""Tests for the universal variable-mapping layer (engine.varmap) and its integrations."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from engine import dualpol, plugins, varmap
from engine.plugins import ModuleError
from tests.conftest import make_synthetic_datatree

KTLX = Path("~/Downloads/Others/XRADAR_EXAMPLES/KTLX20130520_201643_V06.gz").expanduser()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sweep(names, *, n_az=36, n_rng=20, attrs=None, angle=0.5, az=None, rng=None):
    """Sweep dataset with 2-D fields called *names* (a name or {name: attrs})."""
    az = np.arange(n_az) * (360.0 / n_az) if az is None else az
    rng = (np.arange(n_rng) + 1) * 1000.0 if rng is None else rng
    if not isinstance(names, dict):
        names = {n: {} for n in names}
    data = {
        n: (("azimuth", "range"), np.full((az.size, rng.size), float(i)), a)
        for i, (n, a) in enumerate(names.items())
    }
    return xr.Dataset(
        data,
        coords={"azimuth": az, "range": rng},
        attrs={"sweep_fixed_angle": angle, **(attrs or {})},
    )


def _tree(*sweeps):
    nodes = {"/": xr.Dataset()}
    nodes.update({f"/sweep_{i}": ds for i, ds in enumerate(sweeps)})
    return xr.DataTree.from_dict(nodes)


def _split_cut_tree():
    """NEXRAD-style split cut: sweep 0 surveillance (no velocity), sweep 1 Doppler (finer rays,
    shorter range), same fixed angle; sweep 2 is a different elevation without velocity."""
    surv = _sweep(["DBZH", "ZDR"], n_az=36, n_rng=40, angle=0.48)
    az1 = np.arange(72) * 5.0 + 0.4  # 5 deg spacing, offset from sweep 0's 10 deg grid
    dop = _sweep(["DBZH", "VRADH", "WRADH"], angle=0.48, az=az1, rng=(np.arange(25) + 1) * 1000.0)
    dop["VRADH"] = (("azimuth", "range"), np.repeat(az1[:, None], 25, axis=1))
    high = _sweep(["DBZH", "ZDR"], angle=1.5)
    return _tree(surv, dop, high)


# ---------------------------------------------------------------------------
# Registry / single-dataset resolution
# ---------------------------------------------------------------------------


def test_registry_has_every_role_with_cf_and_aliases():
    assert set(varmap.ROLES) == {
        "reflectivity",
        "zdr",
        "phidp",
        "kdp",
        "rhohv",
        "velocity",
        "width",
        "snr",
        "ncp",
    }
    for role in varmap.ROLES.values():
        assert role.standard_names and role.aliases and role.patterns and role.units


@pytest.mark.parametrize(
    ("variable", "role"),
    [
        ("DBZ", "reflectivity"),
        ("DBTH", "reflectivity"),
        ("corrected_reflectivity_horizontal", "reflectivity"),
        ("differential_reflectivity", "zdr"),
        ("UPHIDP", "phidp"),
        ("specific_differential_phase", "kdp"),
        ("CCORH", "rhohv"),
        ("radial_velocity", "velocity"),
        ("VEL", "velocity"),
        ("WIDTH", "width"),
        ("SNRHC", "snr"),
        ("SQI", "ncp"),
    ],
)
def test_alias_resolution(variable, role):
    m = varmap.resolve(_sweep([variable]), role)
    assert (m.variable, m.kind, m.role) == (variable, "alias", role)
    assert 0.8 < m.confidence < 1.0


def test_alias_is_case_insensitive_fallback():
    assert varmap.resolve(_sweep(["dbzh"]), "reflectivity").variable == "dbzh"


def test_cf_standard_name_beats_names():
    ds = _sweep(
        {
            "my_weird_name": {"standard_name": "radar_differential_reflectivity_hv", "units": "dB"},
            "ZDRX": {},
        }
    )
    m = varmap.resolve(ds, "zdr")
    assert (m.variable, m.kind) == ("my_weird_name", "cf")


def test_cf_units_must_be_compatible():
    ds = _sweep({"foo": {"standard_name": "radar_differential_reflectivity_hv", "units": "m/s"}})
    assert varmap.resolve(ds, "zdr") is None
    ok = _sweep({"foo": {"standard_name": "radar_differential_reflectivity_hv", "units": ""}})
    assert varmap.resolve(ok, "zdr").kind == "cf"


def test_cf_beats_exact_alias_when_both_exist():
    ds = _sweep({"DBZH": {}, "zh_filtered": {"standard_name": "equivalent_reflectivity_factor"}})
    m = varmap.resolve(ds, "reflectivity")
    assert (m.variable, m.kind) == ("zh_filtered", "cf")


def test_conflicting_standard_name_vetoes_alias():
    ds = _sweep({"V": {"standard_name": "radar_doppler_spectrum_width_h"}, "WRADH": {}})
    assert varmap.resolve(ds, "velocity") is None


@pytest.mark.parametrize(
    ("variable", "role"),
    [("vel_h", "velocity"), ("dbz_h", "reflectivity"), ("snr_hc", "snr"), ("rho_hv", "rhohv")],
)
def test_pattern_resolution(variable, role):
    m = varmap.resolve(_sweep([variable]), role)
    assert (m.variable, m.kind) == (variable, "pattern")
    assert m.confidence < 0.9


def test_flag_companions_never_match_a_pattern():
    assert varmap.resolve(_sweep(["DBZHC_F", "VEL_F"]), "reflectivity") is None
    assert varmap.resolve(_sweep(["VEL_F"]), "velocity") is None


def test_corrected_variants_only_as_fallback_unless_asked():
    both = _sweep(["DBZHCC", "DBZH"])
    assert varmap.resolve(both, "reflectivity").variable == "DBZH"
    asked = varmap.resolve(both, "reflectivity", prefer_corrected=True)
    assert (asked.variable, asked.corrected) == ("DBZHCC", True)
    only = varmap.resolve(_sweep(["DBZHCC", "DBZHC"]), "reflectivity")
    assert (only.variable, only.corrected) == ("DBZHCC", True)  # legacy preference preserved


def test_cow_style_names():
    ds = _sweep(["DBZHCC", "ZDRC", "ZDRM", "VEL", "VEL_F", "VS", "WIDTH", "SNRHC", "NCP", "KDP"])
    got = {k: m.variable for k, m in varmap.resolve_all(ds).items() if m}
    assert got["reflectivity"] == "DBZHCC"
    assert got["zdr"] == "ZDRC"  # alias (corrected) before a pattern hit on ZDRM
    assert got["velocity"] == "VEL"
    assert got["width"] == "WIDTH"
    assert got["snr"] == "SNRHC"
    assert "phidp" not in got and "rhohv" not in got


def test_unknown_names_resolve_exactly_or_not_at_all():
    ds = _sweep(["REFC"])
    assert varmap.resolve(ds, "REFC").variable == "REFC"
    assert varmap.resolve(ds, "nonexistent") is None


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------


def test_override_wins_over_cf_and_alias():
    ds = _sweep({"DBZH": {"standard_name": "equivalent_reflectivity_factor"}, "mine": {}})
    m = varmap.resolve(ds, "reflectivity", overrides={"reflectivity": "mine"})
    assert (m.variable, m.kind, m.confidence) == ("mine", "override", 1.0)
    # ... for any legacy name of the role too
    assert varmap.resolve(ds, "DBZH", overrides={"reflectivity": "mine"}).variable == "mine"


def test_override_for_absent_variable_falls_through():
    ds = _sweep(["DBZH"])
    m = varmap.resolve(ds, "reflectivity", overrides={"reflectivity": "ghost"})
    assert (m.variable, m.kind) == ("DBZH", "alias")


def test_normalize_overrides_accepts_legacy_names_and_drops_blanks():
    assert varmap.normalize_overrides({"VRADH": "VEL", "zdr": "", "kdp": "auto"}) == {
        "velocity": "VEL"
    }
    with pytest.raises(varmap.VarMapError, match="Unknown role"):
        varmap.normalize_overrides({"bogus": "X"})


def test_override_store_persists_per_path(tmp_path, monkeypatch):
    monkeypatch.setenv("XRADAR_VARMAP_FILE", str(tmp_path / "maps.json"))
    f = tmp_path / "radar.nc"
    store = varmap.OverrideStore()
    out = store.update("id1", str(f), {"velocity": "VEL", "zdr": "ZDRC"})
    assert out == {"velocity": "VEL", "zdr": "ZDRC"}
    saved = json.loads((tmp_path / "maps.json").read_text())
    assert saved[str(f.resolve())] == out
    # a fresh store (new process / reopened file) is seeded from disk
    again = varmap.OverrideStore().get("other-id", str(f))
    assert again == out
    # blank clears one role; replace=True swaps everything
    assert store.update("id1", str(f), {"zdr": ""}) == {"velocity": "VEL"}
    assert store.update("id1", str(f), {"kdp": "KDPX"}, replace=True) == {"kdp": "KDPX"}
    store.update("id1", str(f), {"kdp": ""})
    assert str(f.resolve()) not in json.loads((tmp_path / "maps.json").read_text())


def test_corrupt_store_is_ignored(tmp_path, monkeypatch):
    bad = tmp_path / "maps.json"
    bad.write_text("{not json")
    monkeypatch.setenv("XRADAR_VARMAP_FILE", str(bad))
    assert varmap.load_saved(str(tmp_path / "x.nc")) == {}


# ---------------------------------------------------------------------------
# Cross-sweep (paired) resolution
# ---------------------------------------------------------------------------


def test_paired_sweep_lookup_synthetic():
    tree = _split_cut_tree()
    here = varmap.resolve_in_tree(tree, 0, "velocity")
    assert here is None
    m = varmap.resolve_in_tree(tree, 0, "velocity", allow_paired=True)
    assert (m.variable, m.sweep) == ("VRADH", 1)
    assert m.angle_diff == pytest.approx(0.0)
    # symmetrical: the Doppler cut finds ZDR on the surveillance cut
    z = varmap.resolve_in_tree(tree, 1, "zdr", allow_paired=True)
    assert z.sweep == 0
    # a sweep at another elevation has no partner
    assert varmap.resolve_in_tree(tree, 2, "velocity", allow_paired=True) is None


def test_paired_sweep_respects_tolerance_and_prefers_nearest():
    a = _sweep(["DBZH"], angle=0.5)
    far = _sweep(["VRADH"], angle=0.9)
    near = _sweep(["VRADH"], angle=0.6)
    tree = _tree(a, far, near)
    assert varmap.find_paired_sweep(tree, 0, "velocity", tolerance=0.3).sweep == 2
    assert varmap.find_paired_sweep(tree, 0, "velocity", tolerance=0.05) is None
    assert varmap.find_paired_sweep(tree, 0, "velocity", tolerance=0.5).sweep == 2


def test_align_to_sweep_nearest_ray_and_gate():
    tree = _split_cut_tree()
    surv, dop = varmap.sweep_datasets(tree)[:2]
    out = varmap.align_to_sweep(dop["VRADH"], dop, surv)
    assert out.shape == (surv.sizes["azimuth"], surv.sizes["range"])
    # sweep-0 ray at 100 deg -> nearest Doppler ray (100.4 deg wins over 95.4)
    i = int(np.argmin(np.abs(surv["azimuth"].values - 100.0)))
    assert out.values[i, 0] == pytest.approx(100.4)
    # beyond the Doppler range coverage (25 km) -> NaN, inside -> data
    assert np.isnan(out.values[:, 30]).all()
    assert np.isfinite(out.values[:, :24]).all()
    assert out.dims == ("azimuth", "range") and out.name == "VRADH"


def test_align_handles_azimuth_wrap_and_sparse_rays():
    src = _sweep(["VRADH"], n_az=90, az=np.arange(90.0))  # a 0-89 deg sector, 1 deg rays
    tgt = _sweep(["DBZH"], n_az=5, az=np.array([359.5, 0.2, 45.0, 89.9, 200.0]))
    out = varmap.align_to_sweep(src["VRADH"], src, tgt)
    assert np.isfinite(out.values[:4]).all()  # incl. across the 0/360 seam
    assert np.isnan(out.values[4]).all()  # 200 deg is 110 deg from any source ray


# ---------------------------------------------------------------------------
# ModuleContext integration (legacy names keep working)
# ---------------------------------------------------------------------------


def _ctx(tree, sweep=0, **kw):
    names = sorted(tree.children, key=lambda n: int(n.split("_")[1]))
    return plugins.ModuleContext(
        tree, sweep, get_sweep_dataset=lambda i: tree[names[i]].to_dataset(), **kw
    )


@pytest.mark.parametrize("name", ["velocity", "VRADH", "VEL", "radial_velocity"])
def test_ctx_field_legacy_names_map_to_roles(name):
    tree = _tree(_sweep(["DBZ", "VEL"]))
    ctx = _ctx(tree)
    assert ctx.field(name).name == "VEL"
    assert ctx.field("DBZH").name == "DBZ"
    assert ctx.has("reflectivity") and not ctx.has("ZDR")
    assert ctx.find("KDP") is None


def test_ctx_field_exact_alias_still_returns_that_variable():
    ctx = _ctx(_tree(_sweep(["VRADH", "VEL"])))
    assert ctx.field("VRADH").name == "VRADH"
    assert ctx.field("VEL").name == "VEL"


def test_ctx_user_override_applies_to_every_name_of_the_role():
    ctx = _ctx(_tree(_sweep(["VRADH", "VEL"])), var_overrides={"VRADH": "VEL"})
    assert ctx.field("VRADH").name == "VEL"
    assert ctx.field("velocity").name == "VEL"
    assert ctx.match("velocity").kind == "override"


def test_ctx_paired_sweep_is_opt_in_and_aligned():
    tree = _split_cut_tree()
    ctx = _ctx(tree)
    with pytest.raises(ModuleError) as err:
        ctx.field("velocity")
    text = str(err.value)
    assert "No velocity field found on sweep 0" in text
    assert "DBZH, ZDR" in text  # what exists
    assert "Sweep 1" in text and "VRADH" in text  # where velocity actually is
    da = ctx.field("velocity", allow_paired_sweep=True)
    assert da.shape == ctx.dataset["DBZH"].shape
    assert any("taken from sweep 1" in n for n in ctx.notes)
    assert ctx.has("VRADH", allow_paired_sweep=True) and not ctx.has("VRADH")
    assert len(ctx.notes) == 1  # the note is not repeated on the second access


def test_run_module_paired_roles_and_notes(monkeypatch):
    tree = _split_cut_tree()
    seen = {}

    def run(ctx):
        v = ctx.field("VRADH")  # no allow_paired_sweep: the module declared paired=[...]
        seen["shape"] = v.shape
        res = plugins.Result()
        res.scalars({"cells": int(np.isfinite(v.values).sum())})
        return res

    spec_kw = {"title": "t", "requires": ["VRADH"]}
    monkeypatch.setattr(plugins, "MODULES", dict(plugins.MODULES))
    plugins.module(id="vm_paired", paired=["VRADH"], **spec_kw)(run)
    plugins.module(id="vm_strict", **spec_kw)(run)

    names = sorted(tree.children)
    load = lambda i: tree[names[i]].to_dataset()  # noqa: E731
    out = plugins.run_module("vm_paired", tree, 0, {}, get_sweep_dataset=load)
    assert seen["shape"] == (36, 40)
    assert any("velocity taken from sweep 1" in n for n in out["notes"])
    assert out["blocks"][-1]["type"] == "text" and "sweep 1" in out["blocks"][-1]["body"]
    with pytest.raises(ModuleError, match="Sweep 1"):
        plugins.run_module("vm_strict", tree, 0, {}, get_sweep_dataset=load)
    # on the Doppler cut itself there is nothing to pair: no note
    out1 = plugins.run_module("vm_paired", tree, 1, {}, get_sweep_dataset=load)
    assert "notes" not in out1


def test_run_module_override_changes_the_field_used(monkeypatch):
    tree = _tree(_sweep({"DBZH": {}, "DBZHCC": {}}))
    monkeypatch.setattr(plugins, "MODULES", dict(plugins.MODULES))

    @plugins.module(id="vm_which", title="t", requires=["DBZH"])
    def run(ctx):
        return plugins.Result().scalars({"name": ctx.field("DBZH").name})

    load = lambda i: tree["sweep_0"].to_dataset()  # noqa: E731
    base = plugins.run_module("vm_which", tree, 0, {}, get_sweep_dataset=load)
    assert base["blocks"][0]["items"][0]["value"] == "DBZH"
    ov = plugins.run_module(
        "vm_which", tree, 0, {}, get_sweep_dataset=load, var_overrides={"reflectivity": "DBZHCC"}
    )
    assert ov["blocks"][0]["items"][0]["value"] == "DBZHCC"


def test_var_aliases_shim_is_derived_from_varmap():
    assert plugins.VAR_ALIASES["DBZH"][0] == "DBZH"
    assert "DBZHCC" in plugins.VAR_ALIASES["DBZH"]
    assert "VEL" in plugins.VAR_ALIASES["VRADH"]
    assert set(plugins.VAR_ALIASES) == {r.legacy for r in varmap.ROLES.values()}


# ---------------------------------------------------------------------------
# Error text
# ---------------------------------------------------------------------------


def test_error_text_lists_role_search_and_existing_variables():
    ds = _sweep(["DBZH", "ZDR"])
    text = varmap.explain_missing("velocity", ds, sweep=0)
    assert text.startswith("No velocity field found on sweep 0")
    assert "radial_velocity_of_scatterers_away_from_instrument_h" in text
    assert "VRADH" in text  # searched names
    assert "Variables on sweep 0: DBZH, ZDR" in text
    assert "Variable mapping" in text


def test_error_text_mentions_other_sweeps_with_the_role():
    tree = _tree(_sweep(["DBZH"], angle=0.5), _sweep(["DBZH"], angle=1.5), _sweep(["VEL"], angle=3))
    text = varmap.explain_missing("velocity", varmap.sweep_datasets(tree)[0], sweep=0, tree=tree)
    assert "Other sweeps with velocity: 2 (VEL)" in text


def test_error_text_notes_a_missing_user_mapping():
    text = varmap.explain_missing("velocity", _sweep(["DBZH"]), overrides={"velocity": "ghost"})
    assert "your mapping 'ghost' is not on this sweep" in text


def test_missing_required_module_field_error_is_helpful():
    tree = make_synthetic_datatree(n_azimuth=6, n_range=8)
    ctx = _ctx(tree)
    with pytest.raises(ModuleError, match=r"No zdr|No differential reflectivity"):
        ctx.field("ZDR")


# ---------------------------------------------------------------------------
# dual-pol tools, retrievals, derived
# ---------------------------------------------------------------------------


def test_dualpol_uses_roles_overrides_and_pairing():
    tree = _split_cut_tree()
    # velocity histogram on the surveillance cut -> paired Doppler cut, with a note
    out = dualpol.histogram_data(tree, "VRADH", sweep=0, bins=5)
    assert out["n_valid"] > 0 and any("sweep 1" in n for n in out["notes"])
    # a plain alias still works, and an override redirects it
    t2 = _tree(_sweep(["DBZ", "DBZHCC"]))
    assert dualpol.histogram_data(t2, "DBZH", 0)["n_valid"] > 0
    with pytest.raises(ValueError, match=r"No (differential reflectivity|zdr)"):
        dualpol.histogram_data(t2, "ZDR", 0)
    s = dualpol.scatter_data(tree, "DBZH", "velocity", 0, overrides={"velocity": "VRADH"})
    assert s["n_valid"] > 0 and s["notes"]


def test_retrieval_error_names_the_missing_role():
    from engine.retrievals import run_retrieval

    tree = make_synthetic_datatree(n_azimuth=6, n_range=8)
    with pytest.raises(ValueError, match="differential phase"):
        run_retrieval(tree, "kdp_estimation", 0)


def test_derived_precheck_honours_cf_names_overrides_and_pairing(monkeypatch):
    from engine import derived

    monkeypatch.setattr(plugins, "MODULES", dict(plugins.MODULES))
    plugins.module(id="vm_v", title="v", requires=["VRADH"], paired=["VRADH"])(lambda ctx: None)
    plugins.module(id="vm_v_strict", title="v", requires=["VRADH"])(lambda ctx: None)
    spec, strict = plugins.MODULES["vm_v"], plugins.MODULES["vm_v_strict"]
    tree = _split_cut_tree()
    dss = varmap.sweep_datasets(tree)
    names = lambda i: {str(v) for v in dss[i].data_vars}  # noqa: E731
    kw = {"datatree": tree}
    assert (
        derived._missing_required(spec, names(1), load_dataset=lambda: dss[1], sweep=1, **kw)
        is None
    )
    assert (
        derived._missing_required(spec, names(0), load_dataset=lambda: dss[0], sweep=0, **kw)
        is None
    )
    reason = derived._missing_required(strict, names(0), load_dataset=lambda: dss[0], sweep=0, **kw)
    assert reason and "Sweep 1" in reason
    assert derived._missing_required(spec, names(2), load_dataset=lambda: dss[2], sweep=2, **kw)
    cf = _sweep({"x": {"standard_name": "radial_velocity_of_scatterers_away_from_instrument_h"}})
    assert derived._missing_required(strict, {"x"}, load_dataset=lambda: cf, sweep=0) is None
    ov = derived._missing_required(strict, {"mine"}, var_overrides={"velocity": "mine"})
    assert ov is None


# ---------------------------------------------------------------------------
# Server messages
# ---------------------------------------------------------------------------


def _server_handler():
    import asyncio
    from unittest.mock import AsyncMock

    import server

    ws = AsyncMock()
    ws.remote_address = ("127.0.0.1", 1)
    sent: list[dict] = []

    async def _send(raw):
        if isinstance(raw, str):
            sent.append(json.loads(raw))

    ws.send = _send
    handler = server.ConnectionHandler(ws)
    return server, handler, sent, asyncio


def test_server_get_and_set_variable_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv("XRADAR_VARMAP_FILE", str(tmp_path / "maps.json"))
    server, handler, sent, asyncio = _server_handler()
    from engine.reader import RadarReader

    reader = RadarReader()
    reader._dtree = _split_cut_tree()
    reader._format = "synthetic"
    reader._path = str(tmp_path / "radar.nc")
    file_id = server._register_reader(reader)

    async def go():
        await handler.handle_message(
            json.dumps({"type": "get_variable_mapping", "file_id": file_id, "sweep": 0})
        )
        await handler.handle_message(
            json.dumps(
                {
                    "type": "set_variable_mapping",
                    "file_id": file_id,
                    "sweep": 0,
                    "mapping": {"width": "WRADH", "velocity": "VRADH"},
                }
            )
        )
        await handler.handle_message(
            json.dumps({"type": "set_variable_mapping", "file_id": file_id, "mapping": {"x": "y"}})
        )

    try:
        asyncio.run(go())
    finally:
        server._file_readers.pop(file_id, None)
        server._var_overrides.drop(file_id)
    first, second, bad = sent[-3:]
    assert first["type"] == "variable_mapping" and first["overrides"] == {}
    roles = {r["role"]: r for r in first["roles"]}
    assert roles["velocity"]["variable"] == "VRADH" and roles["velocity"]["sweep"] == 1
    assert (
        roles["reflectivity"]["kind"] in ("cf", "alias") and roles["reflectivity"]["sweep"] is None
    )
    assert roles["kdp"]["variable"] is None
    assert {"DBZH", "VRADH", "ZDR"} <= set(first["variables"])
    assert second["overrides"] == {"width": "WRADH", "velocity": "VRADH"}
    by2 = {r["role"]: r for r in second["roles"]}
    assert by2["width"]["kind"] == "override" and by2["width"]["override"] == "WRADH"
    assert bad["type"] == "error" and "Unknown role" in bad["message"]
    saved = json.loads((tmp_path / "maps.json").read_text())
    assert saved[str((tmp_path / "radar.nc").resolve())] == {
        "width": "WRADH",
        "velocity": "VRADH",
    }


# ---------------------------------------------------------------------------
# Real data: KTLX split cuts
# ---------------------------------------------------------------------------

needs_ktlx = pytest.mark.skipif(not KTLX.exists(), reason="KTLX sample not available")


@pytest.fixture(scope="module")
def ktlx():
    from engine.reader import RadarReader

    reader = RadarReader()
    reader.open_file(str(KTLX))
    return reader


@needs_ktlx
def test_ktlx_velocity_is_found_on_the_paired_doppler_cut(ktlx):
    tree = ktlx.datatree
    assert varmap.resolve_in_tree(tree, 0, "velocity") is None  # surveillance cut
    m = varmap.resolve_in_tree(tree, 0, "VRADH", allow_paired=True)
    assert (m.variable, m.sweep) == ("VRADH", 1)
    assert m.angle_diff < 0.3
    # and the other way round: dual-pol lives on sweep 0
    z = varmap.resolve_in_tree(tree, 1, "ZDR", allow_paired=True)
    assert z.sweep == 0
    # sweep 2/3 are the next split cut
    assert varmap.resolve_in_tree(tree, 2, "velocity", allow_paired=True).sweep == 3


@needs_ktlx
def test_ktlx_error_text_and_aligned_field(ktlx):
    ctx = plugins.ModuleContext(ktlx.datatree, 0, get_sweep_dataset=ktlx.get_sweep_dataset)
    with pytest.raises(ModuleError) as err:
        ctx.field("velocity")
    text = str(err.value)
    assert "Variables on sweep 0: DBZH, ZDR, PHIDP, RHOHV" in text
    assert "Sweep 1" in text and "VRADH" in text
    v = ctx.field("VRADH", allow_paired_sweep=True)
    assert v.shape == ctx.dataset["DBZH"].shape
    assert np.isfinite(v.values).mean() > 0.05
    assert ctx.notes and "sweep 1" in ctx.notes[0]


@needs_ktlx
def test_ktlx_mapping_report(ktlx):
    rep = varmap.mapping_report(ktlx.datatree, 0)
    by = {r["role"]: r for r in rep["roles"]}
    assert by["velocity"]["sweep"] == 1 and by["velocity"]["variable"] == "VRADH"
    assert by["reflectivity"]["variable"] == "DBZH" and by["reflectivity"]["sweep"] is None
    assert "WRADH" in rep["variables"]


@needs_ktlx
def test_ktlx_dealias_module_runs_on_a_surveillance_cut(ktlx):
    pytest.importorskip("radarx")
    plugins.discover_modules()
    out = plugins.run_module(
        "radarx_dealias", ktlx.datatree, 0, {}, get_sweep_dataset=ktlx.get_sweep_dataset
    )
    assert any("velocity taken from sweep 1" in n for n in out["notes"])
    assert any(b["type"] == "field" for b in out["blocks"])


COW = Path(
    "~/Downloads/Others/XRADAR_EXAMPLES/RADAR_CORRECTED/"
    "cfrad.20220322_220338.299_to_20220322_220506.977_COW1high_SUR.nc"
).expanduser()


@pytest.mark.skipif(not COW.exists(), reason="COW1 sample not available")
def test_cow_cfradial_resolves_corrected_names_as_fallback():
    from engine.reader import RadarReader

    reader = RadarReader()
    reader.open_file(str(COW))
    by = {r["role"]: r for r in varmap.mapping_report(reader.datatree, 0)["roles"]}
    assert (by["reflectivity"]["variable"], by["reflectivity"]["corrected"]) == ("DBZHCC", True)
    assert by["zdr"]["variable"] == "ZDRC"
    assert by["velocity"]["variable"] == "VEL"  # not the VS / VL PRT variants or VEL_F
    assert by["snr"]["variable"] == "SNRHC"
    assert all(r["sweep"] is None for r in by.values())  # every role is on sweep 0 itself
