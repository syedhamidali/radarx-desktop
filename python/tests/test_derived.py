"""Tests for engine.derived: the derived-variable store and the all-sweeps runner."""

from __future__ import annotations

import threading

import numpy as np
import pytest

from engine import derived, plugins
from engine.derived import DerivedError, DerivedStore
from tests.conftest import make_synthetic_datatree

MB = 1024 * 1024


def _arr(n_az: int = 10, n_rng: int = 10, fill: float = 1.0) -> np.ndarray:
    return np.full((n_az, n_rng), fill, dtype=np.float32)


META = {"label": "L", "units": "u", "vmin": 0.0, "vmax": 1.0, "module": "m", "field": "f"}


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module_id", "field_name", "expected"),
    [
        ("radarx_hid", "Hydrometeor class (radarx)", "radarx_hid:hydrometeor_class"),
        ("radarx_qpe", "Rain rate (radarx)", "radarx_qpe:rain_rate"),
        ("m", "Echo class", "m:echo_class"),
        ("m", "  ZDR / KDP  separation!! ", "m:zdr_kdp_separation"),
        ("m", "(radarx)", "m:field"),
        ("my-mod", "Ünïcode ≥ 3", "my-mod:n_code_3"),
    ],
)
def test_make_name(module_id, field_name, expected):
    assert derived.make_name(module_id, field_name) == expected


@pytest.mark.parametrize("bad", ["", "nocolon", ":x", "x:", "a:b:c", "a b:c", "a:b c", None, 3])
def test_validate_name_rejects(bad):
    with pytest.raises(DerivedError):
        derived.validate_name(bad)


def test_store_rejects_bad_name_and_bad_arrays():
    store = DerivedStore(max_bytes=MB)
    with pytest.raises(DerivedError):
        store.put_variable("plain", {0: _arr()}, META)
    with pytest.raises(DerivedError, match="no sweeps"):
        store.put_variable("m:f", {}, META)
    with pytest.raises(DerivedError, match="2-D"):
        store.put_variable("m:f", {0: np.zeros(5)}, META)
    with pytest.raises(DerivedError, match="sweep"):
        store.put_variable("m:f", {-1: _arr()}, META)
    assert len(store) == 0


# ---------------------------------------------------------------------------
# Store basics
# ---------------------------------------------------------------------------


def test_put_get_has_variables_drop():
    store = DerivedStore(max_bytes=MB)
    meta = {**META, "cmap": "turbo", "categories": None, "computed_at": 12.5, "params": {"a": 1}}
    store.put_variable("m:f", {2: _arr(fill=2), 0: _arr(fill=0)}, meta)
    assert store.has("m:f") and store.has("m:f", 2) and not store.has("m:f", 1)
    arr, got_meta = store.get(2, "m:f")
    assert arr.dtype == np.float32 and arr[0, 0] == 2 and got_meta["cmap"] == "turbo"
    assert store.get(1, "m:f") is None and store.get(0, "x:y") is None
    (var,) = store.variables()
    assert var["name"] == "m:f" and var["sweeps"] == [0, 2]
    assert {"label", "units", "vmin", "vmax", "cmap", "categories", "module", "field"} <= set(var)
    assert store.nbytes == 2 * 10 * 10 * 4
    assert store.names_on_sweep(0) == ["m:f"] and store.names_on_sweep(1) == []
    assert store.drop("nope") == []
    assert store.drop("m:f") == ["m:f"]
    assert store.nbytes == 0 and store.variables() == []


def test_put_converts_to_float32_and_drop_all():
    store = DerivedStore(max_bytes=MB)
    store.put(0, "m:a", np.ones((3, 3), dtype=np.float64), META)
    store.put(1, "m:a", np.zeros((3, 3), dtype=np.int16), META)  # adds a sweep to "a"
    store.put(0, "m:b", np.ones((3, 3)), META)
    arr, _ = store.get(1, "m:a")
    assert arr.dtype == np.float32
    assert {v["name"]: v["sweeps"] for v in store.variables()}["m:a"] == [0, 1]
    assert sorted(store.drop()) == ["m:a", "m:b"] and len(store) == 0


def test_replace_is_atomic_for_all_sweeps():
    store = DerivedStore(max_bytes=MB)
    store.put_variable("m:f", {0: _arr(fill=1), 1: _arr(fill=1), 2: _arr(fill=1)}, META)
    # The new version has fewer sweeps: the old sweep 2 must not survive.
    evicted = store.put_variable("m:f", {0: _arr(fill=9)}, META)
    assert evicted == []  # replacing is not an eviction
    assert store.variables()[0]["sweeps"] == [0]
    assert store.get(0, "m:f")[0][0, 0] == 9 and store.get(2, "m:f") is None


# ---------------------------------------------------------------------------
# Budget + LRU
# ---------------------------------------------------------------------------


def test_lru_evicts_whole_variables_oldest_first():
    one = 100 * 100 * 4  # one sweep
    store = DerivedStore(max_bytes=one * 6)
    for name in ("m:a", "m:b"):
        store.put_variable(name, {i: _arr(100, 100) for i in range(3)}, META)  # 3 sweeps each
    assert store.nbytes == one * 6
    store.get(0, "m:a")  # "a" is now more recently used than "b"
    evicted = store.put_variable("m:c", {i: _arr(100, 100) for i in range(3)}, META)
    assert evicted == ["m:b"]
    names = {v["name"]: v["sweeps"] for v in store.variables()}
    assert names == {"m:a": [0, 1, 2], "m:c": [0, 1, 2]}  # never half a variable
    assert store.nbytes <= store.max_bytes


def test_big_put_evicts_several_and_report_is_ordered():
    one = 50 * 50 * 4
    store = DerivedStore(max_bytes=one * 5)
    for name in ("m:a", "m:b", "m:c"):
        store.put_variable(name, {0: _arr(50, 50)}, META)
    evicted = store.put_variable("m:d", {i: _arr(50, 50) for i in range(4)}, META)
    assert evicted == ["m:a", "m:b"]  # least recently used first, only as many as needed
    assert [v["name"] for v in store.variables()] == ["m:c", "m:d"]


def test_variable_larger_than_budget_is_refused_and_changes_nothing():
    store = DerivedStore(max_bytes=10 * 10 * 4 * 2)
    store.put_variable("m:keep", {0: _arr()}, META)
    with pytest.raises(DerivedError, match="budget"):
        store.put_variable("m:huge", {i: _arr() for i in range(3)}, META)
    with pytest.raises(DerivedError, match="budget"):
        store.put_variable("m:keep", {i: _arr() for i in range(3)}, META)  # replacement too
    assert [v["name"] for v in store.variables()] == ["m:keep"]
    assert store.variables()[0]["sweeps"] == [0]


def test_get_refreshes_recency_but_has_does_not():
    one = 10 * 10 * 4
    store = DerivedStore(max_bytes=one * 2)
    store.put_variable("m:a", {0: _arr()}, META)
    store.put_variable("m:b", {0: _arr()}, META)
    store.has("m:a")  # does not count as use
    assert store.put_variable("m:c", {0: _arr()}, META) == ["m:a"]


def test_budget_from_environment(monkeypatch):
    monkeypatch.setenv("XRADAR_DERIVED_MAX_MB", "2")
    assert DerivedStore().max_bytes == 2 * MB
    monkeypatch.setenv("XRADAR_DERIVED_MAX_MB", "garbage")
    assert DerivedStore().max_bytes == 1024 * MB
    monkeypatch.setenv("XRADAR_DERIVED_MAX_MB", "-5")
    assert DerivedStore().max_bytes == 1024 * MB
    monkeypatch.delenv("XRADAR_DERIVED_MAX_MB")
    assert DerivedStore().max_bytes == 1024 * MB


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_concurrent_puts_gets_stay_consistent():
    one = 20 * 20 * 4
    store = DerivedStore(max_bytes=one * 12)
    errors: list[BaseException] = []

    def worker(k: int) -> None:
        try:
            for i in range(60):
                name = f"m:v{(k + i) % 7}"
                store.put_variable(name, {s: _arr(20, 20, fill=k) for s in range(3)}, META)
                got = store.get(1, name)
                if got is not None:
                    assert got[0].shape == (20, 20)
                for v in store.variables():
                    assert v["sweeps"] == [0, 1, 2]  # a variable is always complete
                assert store.nbytes <= store.max_bytes
                if i % 10 == 0:
                    store.drop(f"m:v{k % 7}")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    assert store.nbytes <= store.max_bytes


# ---------------------------------------------------------------------------
# Reader attachment
# ---------------------------------------------------------------------------


class _FakeReader:
    def __init__(self, tree):
        self._dtree = tree


def test_store_for_attaches_lazily_and_resets_when_tree_changes():
    tree1, tree2 = make_synthetic_datatree(4, 5), make_synthetic_datatree(4, 5)
    reader = _FakeReader(tree1)
    assert derived.store_for(reader, create=False) is None
    store = derived.store_for(reader)
    assert reader.derived is store and derived.store_for(reader) is store
    store.put_variable("m:f", {0: _arr()}, META)
    assert len(derived.store_for(reader)) == 1  # same tree: kept
    reader._dtree = tree2  # e.g. `process` swapped the tree
    assert len(derived.store_for(reader)) == 0
    reader._dtree = None  # closed
    assert len(derived.store_for(reader)) == 0


# ---------------------------------------------------------------------------
# consistent_range
# ---------------------------------------------------------------------------


def test_consistent_range_cases():
    a = {0: np.array([[0.0, 1.0, 2.0]]), 1: np.array([[10.0, 20.0, np.nan]])}
    # categories win
    cats = [{"value": 1, "label": "x"}, {"value": 4, "label": "y"}]
    assert derived.consistent_range(a, {}, cats) == (1.0, 4.0)
    # identical declared bounds are kept
    same = {0: {"vmin": 0.0, "vmax": 5.0}, 1: {"vmin": 0.0, "vmax": 5.0}}
    assert derived.consistent_range(a, same, None) == (0.0, 5.0)
    # symmetric per-sweep limits stay symmetric (max limit)
    sym = {0: {"vmin": -2.0, "vmax": 2.0}, 1: {"vmin": -5.0, "vmax": 5.0}}
    assert derived.consistent_range(a, sym, None) == (-5.0, 5.0)
    # shared vmin but differing vmax: vmin kept, vmax from pooled 98th percentile
    mix = {0: {"vmin": 0.0, "vmax": 5.0}, 1: {"vmin": 0.0, "vmax": 9.0}}
    lo, hi = derived.consistent_range(a, mix, None)
    pooled = np.array([0.0, 1.0, 2.0, 10.0, 20.0])
    assert lo == 0.0 and hi == pytest.approx(np.percentile(pooled, 98))
    # nothing declared: pooled 2nd-98th percentile
    lo, hi = derived.consistent_range(a, {}, None)
    assert (lo, hi) == pytest.approx((np.percentile(pooled, 2), np.percentile(pooled, 98)))
    # all NaN / degenerate
    nan = {0: np.full((2, 2), np.nan)}
    assert derived.consistent_range(nan, {}, None) == (0.0, 1.0)
    const = {0: np.full((2, 2), 3.0)}
    assert derived.consistent_range(const, {}, None) == (3.0, 4.0)


# ---------------------------------------------------------------------------
# run_all_sweeps
# ---------------------------------------------------------------------------


@pytest.fixture()
def registered():
    from engine.plugins import ModuleError, Result, module

    before = dict(plugins.MODULES)

    @module(id="d_test_scale", title="Scale (test)", requires=["DBZH"])
    def scale(ctx):
        ctx.progress(50, "half")
        if ctx.sweep == 3:
            raise ModuleError("sweep three is cursed")
        dbz = ctx.field("DBZH").values.astype(np.float32)
        res = Result().scalars({"sweep": ctx.sweep, "mean": float(np.nanmean(dbz))})
        res.field("Scaled (radarx)", dbz * (ctx.sweep + 1), units="dBZ")
        res.table("t", ["a"], [{"a": 1}])
        return res

    @module(id="d_test_bad_shape", title="Bad shape", requires=["DBZH"])
    def bad_shape(ctx):
        return Result().field("oops", np.zeros((2, 2)))

    @module(id="d_test_vol", title="Whole volume", scope="volume")
    def vol(ctx):
        return Result()

    @module(id="d_test_nofield", title="No field")
    def nofield(ctx):
        return Result().scalars({"a": 1})

    yield
    plugins.MODULES.clear()
    plugins.MODULES.update(before)


ELEV = [0.5, 1.5, 2.5, 3.5, 4.5, 6.0]


def _runner_args(tree, n=None):
    from engine.reader import sorted_sweep_names

    names = sorted_sweep_names(tree)

    def ds(i, mask=True):
        return tree[names[i]].to_dataset()

    return dict(  # noqa: C408
        n_sweeps=len(names) if n is None else n,
        get_sweep_dataset=ds,
        get_sweep_variables=lambda i: {str(v) for v in ds(i).data_vars},
        get_sweep_grid=lambda i: (ds(i)["azimuth"].size, ds(i)["range"].size),
    )


def test_run_all_sweeps_collects_fields_skips_and_summaries(registered):
    tree = make_synthetic_datatree(n_azimuth=6, n_range=8, elevations=ELEV)
    a = _runner_args(tree)
    n = a.pop("n_sweeps")
    seen: list[tuple[int, str]] = []
    run = derived.run_all_sweeps(
        "d_test_scale", tree, n, {}, progress=lambda p, m: seen.append((p, m)), **a
    )
    assert n == 6
    assert [s["sweep"] for s in run.skipped] == [3]
    assert "cursed" in run.skipped[0]["reason"]
    assert sorted(run.arrays["Scaled (radarx)"]) == [i for i in range(n) if i != 3]
    assert [s["sweep"] for s in run.summaries] == [i for i in range(n) if i != 3]
    assert set(run.summaries[0]["blocks_scalars"]) == {"sweep", "mean"}  # no tables / fields
    # progress: monotone, labelled per sweep, never reaching 100 itself
    pcts = [p for p, _ in seen]
    assert pcts == sorted(pcts) and pcts[-1] < 100
    assert seen[0][1] == f"Scale: sweep 1/{n}" and seen[-1][1].endswith(f"sweep {n}/{n}")
    built = derived.build_variables(run, computed_at=7.0)
    ((name, _arrays, meta),) = built
    assert name == "d_test_scale:scaled"
    assert meta["label"] == "Scaled (radarx)" and meta["units"] == "dBZ"
    assert meta["computed_at"] == 7.0 and meta["module"] == "d_test_scale"
    assert meta["field"] == "Scaled (radarx)" and meta["vmin"] < meta["vmax"]


def test_run_all_sweeps_skips_sweeps_lacking_required_field(registered):
    tree = make_synthetic_datatree(n_azimuth=6, n_range=8, elevations=ELEV)
    tree["sweep_1"] = tree["sweep_1"].to_dataset().drop_vars("DBZH")
    a = _runner_args(tree)
    n = a.pop("n_sweeps")
    run = derived.run_all_sweeps("d_test_scale", tree, n, {}, **a)
    reasons = {s["sweep"]: s["reason"] for s in run.skipped}
    assert "no reflectivity field" in reasons[1].lower() and "VRADH" in reasons[1]
    assert 1 not in run.arrays["Scaled (radarx)"]


def test_run_all_sweeps_errors(registered):
    tree = make_synthetic_datatree(n_azimuth=6, n_range=8, elevations=ELEV)
    a = _runner_args(tree)
    n = a.pop("n_sweeps")
    with pytest.raises(plugins.ModuleError, match="whole-volume"):
        derived.run_all_sweeps("d_test_vol", tree, n, {}, **a)
    with pytest.raises(plugins.ModuleError, match="on any sweep"):
        derived.run_all_sweeps("d_test_bad_shape", tree, n, {}, **a)
    with pytest.raises(plugins.ModuleError, match="no gridded field"):
        derived.run_all_sweeps("d_test_nofield", tree, n, {}, **a)
    with pytest.raises(KeyError):
        derived.run_all_sweeps("missing_module", tree, n, {}, **a)


def test_run_all_sweeps_cancellation_stops_between_sweeps(registered):
    tree = make_synthetic_datatree(n_azimuth=6, n_range=8, elevations=ELEV)
    a = _runner_args(tree)
    n = a.pop("n_sweeps")
    calls = {"n": 0}

    def cancelled() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    with pytest.raises(derived.RunCancelledError):
        derived.run_all_sweeps("d_test_scale", tree, n, {}, is_cancelled=cancelled, **a)
    assert calls["n"] == 3
