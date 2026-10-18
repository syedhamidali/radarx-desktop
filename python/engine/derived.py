"""Derived variables: module output fields registered as variables of an open file.

A plugin module normally computes one 2-D field on one sweep. Running it on every sweep
(:func:`run_all_sweeps`) yields one field per sweep that is kept in a per-file
:class:`DerivedStore` and served through the same path as a normal variable, so the 2-D and
3-D viewers need no special handling.

Naming: ``"<module_id>:<field_slug>"`` (see :func:`make_name`), e.g.
``radarx_hid:hydrometeor_class``.
Names always contain a colon, so they can never collide with a variable that comes from a file.

Memory: a global byte budget (``XRADAR_DERIVED_MAX_MB``, default 1024) is shared by all
variables of one store. When a new variable does not fit, whole least-recently-used variables
are evicted (never some of a variable's sweeps), and their names are returned to the caller.
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
import weakref
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import xarray as xr

from engine import plugins, varmap

logger = logging.getLogger(__name__)

DEFAULT_MAX_MB = 1024
_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+:[A-Za-z0-9_\-]+$")
# Samples per sweep that feed the pooled colour range (keeps the percentile cheap).
_RANGE_SAMPLES_PER_SWEEP = 200_000


def default_budget_bytes() -> int:
    """Byte budget from ``XRADAR_DERIVED_MAX_MB`` (falls back to 1 GB on a bad value)."""
    raw = os.environ.get("XRADAR_DERIVED_MAX_MB")
    if raw:
        try:
            mb = float(raw)
            if math.isfinite(mb) and mb > 0:
                return int(mb * 1024 * 1024)
        except ValueError:
            pass
        logger.warning("Ignoring invalid XRADAR_DERIVED_MAX_MB=%r", raw)
    return DEFAULT_MAX_MB * 1024 * 1024


class DerivedError(ValueError):
    """A derived variable cannot be stored (bad name, larger than the whole budget...)."""


def slug(text: str) -> str:
    """``"Hydrometeor class (radarx)"`` -> ``"hydrometeor_class"`` (lower case, ``[a-z0-9_]``)."""
    text = re.sub(r"\(\s*radarx\s*\)", " ", str(text), flags=re.IGNORECASE)
    out = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return out or "field"


def make_name(module_id: str, field_name: str) -> str:
    """The derived-variable name for a module's field."""
    name = f"{module_id}:{slug(field_name)}"
    validate_name(name)
    return name


def validate_name(name: Any) -> str:
    """Return *name* if it is a well-formed derived name, else raise :class:`DerivedError`."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise DerivedError(f"Invalid derived variable name {name!r} (expected '<module>:<field>')")
    return name


@dataclass
class _Variable:
    sweeps: dict[int, np.ndarray] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def nbytes(self) -> int:
        return sum(int(a.nbytes) for a in self.sweeps.values())


class DerivedStore:
    """Thread-safe store of derived variables for one open file.

    ``(sweep, name) -> (float32 array (n_az, n_range), meta)``. Variables are kept in
    least-recently-used order (``get``/``put``/``has`` with ``touch=True`` refresh them).
    """

    def __init__(self, max_bytes: int | None = None) -> None:
        self.max_bytes = int(max_bytes) if max_bytes is not None else default_budget_bytes()
        self._vars: OrderedDict[str, _Variable] = OrderedDict()
        self._lock = threading.RLock()
        self._tree_ref: Callable[[], Any] | None = None
        self._bound = False

    # -- identity of the data the results were computed from -----------------

    def bind(self, tree: Any) -> bool:
        """Tie the store to *tree*; contents are dropped if it is a different tree than before.

        Returns True if something was dropped. Covers every way a reader's tree can be replaced
        (re-open, ``process``, close) without each of them having to know about derived data.
        """
        with self._lock:
            if self._bound and (self._tree_ref() if self._tree_ref else None) is tree:
                return False
            had = bool(self._vars)
            self._vars.clear()
            try:
                self._tree_ref = weakref.ref(tree) if tree is not None else (lambda: None)
            except TypeError:
                self._tree_ref = lambda: tree
            self._bound = True
            return had

    # -- writing --------------------------------------------------------------

    def put_variable(
        self, name: str, sweeps: dict[int, np.ndarray], meta: dict[str, Any]
    ) -> list[str]:
        """Store (or atomically replace) *name* for all of *sweeps*; returns evicted names.

        The previous version of *name* is only dropped once the new one is stored, and a
        variable that cannot fit even in an empty store raises :class:`DerivedError` without
        touching anything.
        """
        validate_name(name)
        if not sweeps:
            raise DerivedError(f"Derived variable '{name}' has no sweeps")
        arrays: dict[int, np.ndarray] = {}
        for sweep, arr in sweeps.items():
            if isinstance(sweep, bool) or not isinstance(sweep, (int, np.integer)) or sweep < 0:
                raise DerivedError(f"Invalid sweep index {sweep!r} for '{name}'")
            a = np.asarray(arr)
            if a.ndim != 2:
                raise DerivedError(f"'{name}' sweep {sweep} must be 2-D, got shape {a.shape}")
            arrays[int(sweep)] = np.ascontiguousarray(a, dtype=np.float32)
        var = _Variable(arrays, dict(meta))
        need = var.nbytes
        if need > self.max_bytes:
            raise DerivedError(
                f"'{name}' needs {need / 1e6:.0f} MB, more than the derived-data budget of "
                f"{self.max_bytes / 1e6:.0f} MB (raise XRADAR_DERIVED_MAX_MB)"
            )
        evicted: list[str] = []
        with self._lock:
            old = self._vars.pop(name, None)  # replaced, not "evicted"
            used = self._nbytes_locked()
            while used + need > self.max_bytes and self._vars:
                victim, v = self._vars.popitem(last=False)  # least recently used, whole variable
                used -= v.nbytes
                evicted.append(victim)
            if used + need > self.max_bytes:  # pragma: no cover - guarded by the check above
                if old is not None:
                    self._vars[name] = old
                raise DerivedError(f"'{name}' does not fit in the derived-data budget")
            self._vars[name] = var
        if evicted:
            logger.info("Derived store evicted %s to fit '%s'", evicted, name)
        return evicted

    def put(self, sweep: int, name: str, array: np.ndarray, meta: dict[str, Any]) -> list[str]:
        """Add or replace one sweep of *name* (``meta`` replaces the variable's metadata)."""
        validate_name(name)
        with self._lock:
            current = self._vars.get(name)
            sweeps = dict(current.sweeps) if current is not None else {}
            sweeps[int(sweep)] = array
            return self.put_variable(name, sweeps, meta)

    # -- reading --------------------------------------------------------------

    def get(self, sweep: int, name: str) -> tuple[np.ndarray, dict[str, Any]] | None:
        """``(array, meta)`` for one sweep of *name*, or ``None``. Marks it recently used."""
        with self._lock:
            var = self._vars.get(name)
            if var is None or sweep not in var.sweeps:
                return None
            self._vars.move_to_end(name)
            return var.sweeps[sweep], dict(var.meta)

    def has(self, name: str, sweep: int | None = None) -> bool:
        with self._lock:
            var = self._vars.get(name)
            return var is not None and (sweep is None or sweep in var.sweeps)

    def names_on_sweep(self, sweep: int) -> list[str]:
        with self._lock:
            return sorted(n for n, v in self._vars.items() if sweep in v.sweeps)

    def variables(self) -> list[dict[str, Any]]:
        """Description of every stored variable (no array data), oldest-used first."""
        with self._lock:
            out = []
            for name, var in self._vars.items():
                m = var.meta
                out.append(
                    {
                        "name": name,
                        "label": m.get("label", name),
                        "units": m.get("units", ""),
                        "vmin": m.get("vmin"),
                        "vmax": m.get("vmax"),
                        "cmap": m.get("cmap"),
                        "categories": m.get("categories"),
                        "module": m.get("module"),
                        "field": m.get("field"),
                        "params": m.get("params"),
                        "computed_at": m.get("computed_at"),
                        "sweeps": sorted(var.sweeps),
                        "nbytes": var.nbytes,
                    }
                )
            return out

    def drop(self, name: str | None = None) -> list[str]:
        """Remove *name* (or everything when ``None``); returns the names removed."""
        with self._lock:
            if name is None:
                dropped = list(self._vars)
                self._vars.clear()
            else:
                dropped = [name] if self._vars.pop(name, None) is not None else []
        return dropped

    clear = drop

    @property
    def nbytes(self) -> int:
        with self._lock:
            return self._nbytes_locked()

    def _nbytes_locked(self) -> int:
        return sum(v.nbytes for v in self._vars.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._vars)


_attach_lock = threading.Lock()


def store_for(reader: Any, *, create: bool = True) -> DerivedStore | None:
    """The reader's :class:`DerivedStore` (attached as ``reader.derived``), bound to its tree.

    ``create=False`` returns ``None`` when the reader has never had one. A store whose tree is no
    longer the reader's tree is emptied, so results of a previous incarnation of the file are
    never served.
    """
    with _attach_lock:
        store = getattr(reader, "derived", None)
        if store is None:
            if not create:
                return None
            store = DerivedStore()
            reader.derived = store
    store.bind(getattr(reader, "_dtree", None))
    return store


# ---------------------------------------------------------------------------
# Running a module on every sweep
# ---------------------------------------------------------------------------


@dataclass
class VolumeRun:
    """Outcome of :func:`run_all_sweeps`."""

    module: str
    params: dict[str, Any]
    elapsed_ms: float
    # field name -> {sweep -> float32 array}
    arrays: dict[str, dict[int, np.ndarray]] = field(default_factory=dict)
    # field name -> {sweep -> the module's "field" block for that sweep}
    blocks: dict[str, dict[int, dict[str, Any]]] = field(default_factory=dict)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    summaries: list[dict[str, Any]] = field(default_factory=list)
    per_sweep_ms: dict[int, float] = field(default_factory=dict)


class RunCancelledError(Exception):
    """The caller (connection) went away; stop after the current sweep."""


def _missing_required(
    spec: plugins.ModuleSpec,
    variables: set[str],
    *,
    load_dataset: Callable[[], Any] | None = None,
    datatree: Any = None,
    sweep: int | None = None,
    var_overrides: dict[str, str] | None = None,
) -> str | None:
    """Reason string if the sweep lacks a required field, else ``None``.

    Cheap check first (role aliases / patterns on the variable *names*); only when that misses
    is the sweep dataset consulted (CF ``standard_name``, user overrides, paired sweep).
    """
    ds = None
    for req in spec.requires:
        role = varmap.role_for_name(req)
        if role is None:
            if req not in variables:
                have = ", ".join(sorted(variables)) or "no fields"
                return f"no {req} field on this sweep (has {have})"
            continue
        override = (var_overrides or {}).get(role.key)
        if override and override in variables:
            continue
        stub = xr.Dataset({v: xr.DataArray(np.zeros((1, 1))) for v in variables})
        if varmap.resolve(stub, req) is not None:
            continue
        if load_dataset is None:
            return varmap.explain_missing(req, stub, sweep=sweep)
        if ds is None:
            ds = load_dataset()
        paired_ok = role.key in {r.key for n in spec.paired if (r := varmap.role_for_name(n))}
        if varmap.resolve(ds, req, overrides=var_overrides) is not None:
            continue
        if (
            paired_ok
            and sweep is not None
            and datatree is not None
            and varmap.find_paired_sweep(datatree, sweep, req, overrides=var_overrides)
        ):
            continue
        return varmap.explain_missing(req, ds, sweep=sweep, tree=datatree, overrides=var_overrides)
    return None


def scalar_summary(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """``{label: value}`` of every scalars block of a result (tables/overlays/fields dropped)."""
    out: dict[str, Any] = {}
    for block in blocks:
        if block.get("type") != "scalars":
            continue
        for item in block.get("items") or []:
            if isinstance(item, dict) and "label" in item:
                out[str(item["label"])] = item.get("value")
    return out


def run_all_sweeps(
    module_id: str,
    datatree: Any,
    n_sweeps: int,
    raw_params: dict[str, Any],
    *,
    get_sweep_dataset: Callable[[int], Any],
    get_sweep_variables: Callable[[int], set[str]],
    get_sweep_grid: Callable[[int], tuple[int, int]],
    progress: Callable[[int, str], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    var_overrides: dict[str, str] | None = None,
) -> VolumeRun:
    """Run a per-sweep module on sweeps ``0..n_sweeps-1``, one at a time.

    Sweeps lacking a required field, sweeps where the module raises, and fields whose shape
    is not the sweep's grid are skipped with a recorded reason. Raises
    :class:`plugins.ModuleError` if the module is not per-sweep or if *every* sweep failed
    (the message then quotes the first reason), and :class:`RunCancelledError` on cancellation.
    """
    spec = plugins.MODULES.get(module_id)
    if spec is None:
        raise KeyError(f"Unknown module '{module_id}'. Available: {sorted(plugins.MODULES)}")
    if not spec.per_sweep:
        raise plugins.ModuleError(
            "This module computes a whole-volume product and cannot be run per sweep"
        )
    if n_sweeps <= 0:
        raise plugins.ModuleError("The file has no sweeps")

    run = VolumeRun(module=module_id, params={}, elapsed_ms=0.0)
    t_start = time.perf_counter()
    last_pct = -1

    for sweep in range(n_sweeps):
        if is_cancelled is not None and is_cancelled():
            raise RunCancelledError()
        label = f"{spec.title.split(' (')[0]}: sweep {sweep + 1}/{n_sweeps}"

        def report(inner: int, _msg: str, _sweep: int = sweep, _label: str = label) -> None:
            nonlocal last_pct
            if progress is None:
                return
            frac = (_sweep + min(max(inner, 0), 100) / 100.0) / n_sweeps
            pct = min(95, 2 + int(frac * 93))
            if pct != last_pct:
                last_pct = pct
                progress(pct, _label)

        report(0, "")
        reason = _missing_required(
            spec,
            get_sweep_variables(sweep),
            load_dataset=lambda _i=sweep: get_sweep_dataset(_i),
            datatree=datatree,
            sweep=sweep,
            var_overrides=var_overrides,
        )
        if reason is not None:
            run.skipped.append({"sweep": sweep, "reason": reason})
            continue
        t0 = time.perf_counter()
        try:
            payload, fields = plugins.run_module(
                module_id,
                datatree,
                sweep,
                raw_params,
                get_sweep_dataset=get_sweep_dataset,
                progress=report,
                return_fields=True,
                var_overrides=var_overrides,
            )
            expected = tuple(get_sweep_grid(sweep))
            for name, arr in fields.items():
                shape = tuple(np.shape(arr))
                if shape != expected:
                    raise plugins.ModuleError(
                        f"Module '{module_id}' field '{name}' has shape {shape} but the sweep "
                        f"grid is (n_azimuth, n_range) = {expected}"
                    )
        except plugins.ModuleError as exc:
            run.skipped.append({"sweep": sweep, "reason": str(exc)})
            continue
        except Exception as exc:  # a crash on one sweep must not lose the other sweeps
            logger.warning("Module '%s' sweep %d failed: %s", module_id, sweep, exc, exc_info=True)
            run.skipped.append({"sweep": sweep, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        run.params = payload.get("params", run.params)
        run.per_sweep_ms[sweep] = round((time.perf_counter() - t0) * 1000.0, 1)
        blocks = payload.get("blocks", [])
        run.summaries.append({"sweep": sweep, "blocks_scalars": scalar_summary(blocks)})
        by_name = {b.get("name"): b for b in blocks if b.get("type") == "field"}
        for name, arr in fields.items():
            run.arrays.setdefault(name, {})[sweep] = np.array(arr, dtype=np.float32, copy=True)
            run.blocks.setdefault(name, {})[sweep] = by_name.get(name, {})

    if not run.arrays:
        first = run.skipped[0]["reason"] if run.skipped else "the module produced no gridded field"
        if run.skipped:
            msg = f"{spec.title} could not be computed on any sweep ({first})"
        else:
            msg = f"{spec.title} produced no gridded field to register as a variable"
        raise plugins.ModuleError(msg)
    run.elapsed_ms = round((time.perf_counter() - t_start) * 1000.0, 1)
    return run


def _pooled_percentiles(arrays: list[np.ndarray]) -> tuple[float, float] | None:
    parts = []
    for arr in arrays:
        flat = arr.reshape(-1)
        if flat.size > _RANGE_SAMPLES_PER_SWEEP:
            flat = flat[:: -(-flat.size // _RANGE_SAMPLES_PER_SWEEP)]
        parts.append(flat[np.isfinite(flat)])
    pooled = np.concatenate(parts) if parts else np.empty(0, dtype=np.float32)
    if pooled.size == 0:
        return None
    return float(np.percentile(pooled, 2)), float(np.percentile(pooled, 98))


def consistent_range(
    arrays: dict[int, np.ndarray], blocks: dict[int, dict[str, Any]], categories: list | None
) -> tuple[float | None, float | None]:
    """One ``(vmin, vmax)`` for all sweeps of a field.

    Categorical fields use their category values. For continuous fields a bound that every
    sweep declared identically is kept (e.g. rain rate always starts at 0, ZDR arc is 0..6);
    symmetric ranges (``vmin == -vmax`` on every sweep, e.g. shear) stay symmetric using the
    largest limit; otherwise the bound is the 2nd/98th percentile of the pooled finite values.
    """
    if categories:
        values = [c["value"] for c in categories if isinstance(c.get("value"), (int, float))]
        if values:
            return float(min(values)), float(max(values))
    bl = [blocks.get(s, {}) for s in arrays]
    vmins = [b.get("vmin") for b in bl]
    vmaxs = [b.get("vmax") for b in bl]
    if all(isinstance(v, (int, float)) for v in vmins + vmaxs) and all(
        math.isclose(lo, -hi, rel_tol=1e-6, abs_tol=1e-12) and hi > 0
        for lo, hi in zip(vmins, vmaxs, strict=True)
    ):
        lim = max(float(v) for v in vmaxs)
        return -lim, lim

    def agreed(vals: list) -> float | None:
        if vals and all(isinstance(v, (int, float)) for v in vals) and len(set(vals)) == 1:
            return float(vals[0])
        return None

    vmin, vmax = agreed(vmins), agreed(vmaxs)
    if vmin is None or vmax is None:
        pooled = _pooled_percentiles(list(arrays.values()))
        if pooled is not None:
            vmin = pooled[0] if vmin is None else vmin
            vmax = pooled[1] if vmax is None else vmax
    if vmin is None or vmax is None:
        return (vmin if vmin is not None else 0.0), (vmax if vmax is not None else 1.0)
    if vmax <= vmin:
        vmax = vmin + 1.0
    return vmin, vmax


def build_variables(
    run: VolumeRun, computed_at: float | None = None
) -> list[tuple[str, dict[int, np.ndarray], dict[str, Any]]]:
    """``[(derived_name, {sweep: array}, meta), ...]`` for every field the run produced."""
    stamp = time.time() if computed_at is None else computed_at
    spec = plugins.MODULES.get(run.module)
    out: list[tuple[str, dict[int, np.ndarray], dict[str, Any]]] = []
    used: set[str] = set()
    for field_name, arrays in run.arrays.items():
        blocks = run.blocks.get(field_name, {})
        first = blocks.get(min(blocks), {}) if blocks else {}
        categories = next(
            (b.get("categories") for b in blocks.values() if b.get("categories")), None
        )
        vmin, vmax = consistent_range(arrays, blocks, categories)
        meta = {
            "label": str(first.get("title") or field_name),
            "units": str(first.get("units") or ""),
            "vmin": vmin,
            "vmax": vmax,
            "cmap": first.get("cmap"),
            "categories": categories,
            "module": run.module,
            "module_title": spec.title if spec else run.module,
            "field": field_name,
            "params": run.params,
            "computed_at": stamp,
        }
        name = make_name(run.module, field_name)
        n = 2
        while name in used:  # two field names that slug to the same text
            name = f"{make_name(run.module, field_name)}_{n}"
            n += 1
        used.add(name)
        out.append((name, arrays, meta))
    return out


# ---------------------------------------------------------------------------
# Writing derived variables into the data that gets exported
# ---------------------------------------------------------------------------


def export_name(name: str) -> str:
    """NetCDF/Zarr-safe name: ``"qc:cleaned_zdr"`` -> ``"qc__cleaned_zdr"``."""
    return name.replace(":", "__")


def tree_with_derived(
    datatree: Any,
    store: DerivedStore,
    *,
    names: list[str] | None = None,
    include_original: bool = True,
) -> Any:
    """A shallow copy of *datatree* with derived variables added to every sweep.

    Each derived variable becomes a ``(azimuth, range)`` float32 data variable called
    :func:`export_name` with CF-style attributes (``long_name``, ``units``, ``comment`` naming the
    module and its parameters; categorical ones also get ``flag_values`` / ``flag_meanings``).
    A sweep a variable was not computed for (e.g. no dual-pol on a Doppler cut) gets an all-NaN
    array so every sweep of the file stays the same shape. With ``include_original=False`` the 2-D
    moment variables of the file are dropped, leaving a file with the derived fields (plus the
    1-D metadata) only. The reader's own tree is never modified.
    """
    import xarray as xr

    from engine.reader import sorted_sweep_names

    available = {v["name"]: v for v in store.variables()}
    chosen = list(available) if names is None else [n for n in names if n in available]
    missing = [n for n in (names or []) if n not in available]
    if missing:
        raise DerivedError(f"Unknown derived variable(s): {', '.join(missing)}")
    if not chosen and not include_original:
        raise DerivedError(
            "Nothing to export: no derived variables and original variables excluded"
        )

    out = datatree.copy(deep=False)
    for index, node_name in enumerate(sorted_sweep_names(out)):
        ds = out[node_name].to_dataset().copy(deep=False)
        shape = (ds.sizes.get("azimuth"), ds.sizes.get("range"))
        if not include_original:
            drop = [n for n, v in ds.data_vars.items() if "azimuth" in v.dims and "range" in v.dims]
            ds = ds.drop_vars(drop)
        new = {}
        for name in chosen:
            info = available[name]
            got = store.get(index, name)
            arr = got[0] if got is not None else np.full(shape, np.nan, dtype=np.float32)
            if tuple(arr.shape) != shape:
                raise DerivedError(
                    f"Derived variable {name!r} has shape {arr.shape} on sweep {index} but the "
                    f"sweep is {shape}"
                )
            attrs: dict[str, Any] = {
                "long_name": str(info.get("label") or name),
                "units": str(info.get("units") or ""),
                "comment": (
                    f"derived by xradar-desktop module {info.get('module')} "
                    f"(field {info.get('field')}); params {info.get('params')}"
                ),
            }
            cats = info.get("categories")
            if cats:
                attrs["flag_values"] = np.array([int(c["value"]) for c in cats], dtype=np.int32)
                attrs["flag_meanings"] = " ".join(
                    re.sub(r"[^A-Za-z0-9_]+", "_", str(c["label"])).strip("_") for c in cats
                )
            new[export_name(name)] = xr.DataArray(
                np.asarray(arr, dtype=np.float32), dims=("azimuth", "range"), attrs=attrs
            )
        out[node_name].dataset = ds.assign(new) if new else ds
    return out
