"""Processing pipelines ("recipes"): chain cleaning / processing steps into ONE variable.

A *recipe* is an ordered list of steps applied to one target variable, e.g. "de-alias the
velocity, remove ground clutter, remove biological echo, despeckle". Instead of creating one
derived variable per step, the steps share a **working state** (the fields of one sweep plus a
keep/remove mask) and only the final result is stored, as a single derived variable
``pipeline:<recipe_slug>_<target>`` that downstream modules and exports can use like any other.

Two kinds of step (see :mod:`engine.pipeline_steps` for the catalog):

* **filters** only shrink the mask (``True`` = keep): SNR gate, echo mask, clutter, biology ...
* **transforms** change values: de-aliasing (changes the velocity field), smoothing, gap fill
  (re-admits gates), noise correction ...

Order matters: a filter before a transform restricts what the transform sees (clutter removed
before de-aliasing gives radarx a cleaner field); gap filling before a filter is undone by it.

Pure data flow, no I/O: :func:`run_recipe_on_sweep` (preview) and
:func:`run_recipe_all_sweeps` (one derived variable) take callables for dataset access exactly
like :func:`engine.derived.run_all_sweeps`. Persistence and the script export are in this module
too (:func:`save_recipe`, :func:`recipe_to_script`).
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from engine import derived, plugins
from engine.derived import RunCancelledError
from engine.plugins import ModuleError

logger = logging.getLogger(__name__)

RECIPE_VERSION = 1
MAX_STEPS = 30
MAX_NAME_LEN = 80

# Colour defaults per canonical role: (units, vmin, vmax, cmap). Roles not listed get the pooled
# 2nd-98th percentile range and the viewer's default colormap.
ROLE_STYLE: dict[str, tuple[str, float | None, float | None, str | None]] = {
    "DBZH": ("dBZ", -20.0, 70.0, "NWSRef"),
    "VRADH": ("m/s", None, None, "NWSVel"),
    "ZDR": ("dB", -3.0, 6.0, None),
    "RHOHV": ("", 0.5, 1.0, None),
    "WRADH": ("m/s", 0.0, 10.0, None),
}


# ---------------------------------------------------------------------------
# Variable lookup (THE single swap point for the variable-mapping work)
# ---------------------------------------------------------------------------


def canonical_role(name: str) -> str:
    """``"velocity"`` / ``"vel"`` / ``"VRADH"`` -> ``"VRADH"``; unknown names are returned as is.

    Matching is case-insensitive over :data:`engine.plugins.VAR_ALIASES`.
    """
    low = str(name).strip().lower()
    for canon, aliases in plugins.VAR_ALIASES.items():
        if low == canon.lower() or low in (a.lower() for a in aliases):
            return canon
    return str(name).strip()


def find_variable(ds: Any, role: str) -> str | None:
    """Name of the data variable of *ds* standing for *role*, or ``None``.

    This is the ONE place the pipeline resolves a role to a file variable. Once
    ``engine/varmap.py`` (user-editable variable mapping) exists, replace the body with a call to
    it (for example ``return varmap.resolve(ds, role)``); nothing else in the pipeline looks at
    variable names.
    """
    present = {str(v) for v in ds.data_vars}
    if role in present:
        return role
    for alias in plugins.VAR_ALIASES.get(canonical_role(role), []):
        if alias in present:
            return alias
    return None


def polar_array(da: Any) -> np.ndarray:
    """A DataArray as float32 ``(n_azimuth, n_range)`` (extra dims: first index)."""
    extra = [d for d in da.dims if d not in ("azimuth", "range")]
    if extra:
        da = da.isel(dict.fromkeys(extra, 0))
    return np.asarray(da.transpose("azimuth", "range").values, dtype=np.float32)


# ---------------------------------------------------------------------------
# Step declaration + registry
# ---------------------------------------------------------------------------


class StepSkippedError(Exception):
    """Raised by a step that does not apply to this sweep (e.g. no SNR): noted, not an error."""


@dataclass
class StepOutcome:
    """What a step reports back besides the state it changed."""

    notes: list[str] = field(default_factory=list)
    modified: int = 0  # gates whose value was changed (transforms)
    filled: int = 0  # gates re-admitted by the step (gap fill)
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepSpec:
    id: str
    title: str
    description: str
    category: str
    kind: str  # "filter" (mask only) or "transform" (changes values)
    func: Callable[..., StepOutcome | None]
    params: list[dict[str, Any]]
    needs: list[str] = field(
        default_factory=list
    )  # roles that must exist, else the sweep is skipped
    optional: list[str] = field(default_factory=list)  # roles used when present
    transforms: str | None = None  # role changed by a transform: "target" or a role name
    packages: list[str] = field(default_factory=list)  # optional pip packages (better results)
    kernels: list[Callable[..., Any]] = field(default_factory=list)  # embedded in exported scripts
    script: str = ""  # code snippet for the exported script (see recipe_to_script)

    def describe(self) -> dict[str, Any]:
        import importlib.util

        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "category": self.category,
            "kind": self.kind,
            "needs": self.needs,
            "optional": self.optional,
            "transforms": self.transforms,
            "params": self.params,
            "packages": self.packages,
            "uses_packages": [p for p in self.packages if importlib.util.find_spec(p) is not None],
        }


STEPS: dict[str, StepSpec] = {}


def step(
    *,
    id: str,  # noqa: A002
    title: str,
    description: str = "",
    category: str = "Filter",
    kind: str = "filter",
    needs: list[str] | None = None,
    optional: list[str] | None = None,
    transforms: str | None = None,
    params: list[dict[str, Any]] | None = None,
    packages: list[str] | None = None,
    kernels: list[Callable[..., Any]] | None = None,
    script: str = "",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register ``func(state, **params) -> StepOutcome | None`` in the step catalog."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if kind not in ("filter", "transform"):
            raise ValueError(f"step '{id}': kind must be 'filter' or 'transform'")
        if not re.fullmatch(r"[a-z0-9_]+", id):
            raise ValueError(f"step id {id!r} must be lowercase alphanumeric/underscore")
        names = [p["name"] for p in (params or [])]
        if len(names) != len(set(names)):
            raise ValueError(f"step '{id}' declares duplicate param names")
        STEPS[id] = StepSpec(
            id=id,
            title=title,
            description=description,
            category=category,
            kind=kind,
            func=func,
            params=list(params or []),
            needs=list(needs or []),
            optional=list(optional or []),
            transforms=transforms,
            packages=list(packages or []),
            kernels=list(kernels or []),
            script=script,
        )
        return func

    return decorator


def _ensure_steps() -> None:
    if not STEPS:
        from engine import pipeline_steps  # noqa: F401  (registers the catalog)


def get_step(step_id: str) -> StepSpec:
    _ensure_steps()
    spec = STEPS.get(step_id)
    if spec is None:
        raise ModuleError(f"Unknown pipeline step '{step_id}'. Available: {sorted(STEPS)}")
    return spec


def list_steps() -> list[dict[str, Any]]:
    """The step catalog for the UI, grouped by category then title."""
    _ensure_steps()
    return sorted((s.describe() for s in STEPS.values()), key=lambda s: (s["category"], s["title"]))


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------


@dataclass
class Recipe:
    name: str
    target: str
    steps: list[dict[str, Any]]  # {"step_id", "params", "enabled"}
    description: str = ""
    apply_to_all: bool = (
        False  # also store every other loaded field, masked ("apply to all fields")
    )
    builtin: bool = False

    @property
    def slug(self) -> str:
        return derived.slug(self.name)[:40].strip("_") or "recipe"

    @property
    def target_role(self) -> str:
        return canonical_role(self.target)

    def enabled_steps(self) -> list[dict[str, Any]]:
        return [s for s in self.steps if s.get("enabled", True)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": RECIPE_VERSION,
            "name": self.name,
            "description": self.description,
            "target": self.target,
            "apply_to_all": self.apply_to_all,
            "steps": [
                {
                    "step_id": s["step_id"],
                    "params": dict(s.get("params") or {}),
                    "enabled": bool(s.get("enabled", True)),
                }
                for s in self.steps
            ],
            "builtin": self.builtin,
        }


def parse_recipe(raw: Any, *, strict_name: bool = True) -> Recipe:
    """Validate and normalise a recipe dict (from JSON / the UI). Raises :class:`ModuleError`.

    Every step's params are validated and filled with defaults via the step's declaration.
    """
    _ensure_steps()
    if not isinstance(raw, dict):
        raise ModuleError("A recipe must be an object")
    version = raw.get("version", RECIPE_VERSION)
    if isinstance(version, int) and not isinstance(version, bool) and version > RECIPE_VERSION:
        raise ModuleError(
            f"This recipe was saved by a newer xradar-desktop (recipe format {version}; this "
            f"version reads up to {RECIPE_VERSION})"
        )
    schema = raw.get("schema")
    if schema is not None and schema != RECIPE_SCHEMA:
        raise ModuleError(f"Not an xradar-desktop recipe (schema {schema!r})")
    name = raw.get("name")
    if strict_name and (not isinstance(name, str) or not name.strip()):
        raise ModuleError("A recipe needs a name")
    name = str(name or "Untitled recipe").strip()
    if len(name) > MAX_NAME_LEN:
        raise ModuleError(f"Recipe name is longer than {MAX_NAME_LEN} characters")
    target = raw.get("target")
    if not isinstance(target, str) or not target.strip() or len(target) > 64:
        raise ModuleError("A recipe needs a target variable (e.g. 'VRADH')")
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ModuleError("A recipe needs at least one step")
    if len(steps_raw) > MAX_STEPS:
        raise ModuleError(f"A recipe can have at most {MAX_STEPS} steps")
    steps: list[dict[str, Any]] = []
    for i, item in enumerate(steps_raw):
        if not isinstance(item, dict) or not isinstance(item.get("step_id"), str):
            raise ModuleError(f"Step {i + 1}: expected an object with a 'step_id'")
        spec = get_step(item["step_id"])
        params = item.get("params") or {}
        if not isinstance(params, dict):
            raise ModuleError(f"Step {i + 1} ({spec.title}): 'params' must be an object")
        try:
            clean = plugins._coerce_params(spec.params, params)
        except ModuleError as exc:
            raise ModuleError(f"Step {i + 1} ({spec.title}): {exc}") from exc
        steps.append(
            {"step_id": spec.id, "params": clean, "enabled": bool(item.get("enabled", True))}
        )
    return Recipe(
        name=name,
        target=target.strip(),
        steps=steps,
        description=str(raw.get("description") or "")[:500],
        apply_to_all=bool(raw.get("apply_to_all", False)),
        builtin=bool(raw.get("builtin", False)),
    )


def required_roles(recipe: Recipe) -> list[str]:
    """Roles a sweep must have for the recipe to run (target + hard needs of enabled steps)."""
    roles = [recipe.target_role]
    for s in recipe.enabled_steps():
        for r in get_step(s["step_id"]).needs:
            if r not in roles:
                roles.append(r)
    return roles


def optional_roles(recipe: Recipe) -> list[str]:
    have = set(required_roles(recipe))
    out: list[str] = []
    for s in recipe.enabled_steps():
        for r in get_step(s["step_id"]).optional:
            if r not in have and r not in out:
                out.append(r)
    return out


# ---------------------------------------------------------------------------
# Working state
# ---------------------------------------------------------------------------


@dataclass
class WorkingState:
    """Fields of one sweep + the keep mask that the steps of a recipe share."""

    ctx: Any  # engine.plugins.ModuleContext of the sweep (steps use it for radarx calls)
    target: str  # key of the target in ``fields``
    fields: dict[str, np.ndarray]  # role -> float32 (n_az, n_range); NaN = no data
    mask: np.ndarray  # bool; True = keep
    azimuth: np.ndarray
    range_m: np.ndarray
    units: dict[str, str] = field(default_factory=dict)
    var_names: dict[str, str] = field(default_factory=dict)  # role -> variable name in the file
    borrowed: dict[str, int] = field(default_factory=dict)  # role -> companion sweep it came from

    @property
    def shape(self) -> tuple[int, int]:
        return self.mask.shape  # type: ignore[return-value]

    @property
    def values(self) -> np.ndarray:
        """The (current) target values, NaN where there is no data. The mask is NOT applied."""
        return self.fields[self.target]

    def valid(self) -> np.ndarray:
        """Gates that are kept and have a target value."""
        return self.mask & np.isfinite(self.values)

    def has(self, role: str) -> bool:
        return role in self.fields

    def get(self, role: str) -> np.ndarray:
        """Field for *role* (raises :class:`StepSkippedError` if the sweep does not have it)."""
        key = self.target if role == "target" else role
        arr = self.fields.get(key)
        if arr is None:
            raise StepSkippedError(f"no {role} field on this sweep")
        return arr

    def masked(self, role: str) -> np.ndarray:
        """Field with removed gates set to NaN."""
        return np.where(self.mask, self.get(role), np.nan).astype(np.float32)

    def restrict(self, keep: np.ndarray) -> int:
        """AND *keep* into the mask; returns how many target-valid gates that removed."""
        before = self.valid()
        self.mask &= np.asarray(keep, dtype=bool)
        return int((before & ~self.mask).sum())


@dataclass
class SweepRun:
    """Outcome of one recipe on one sweep (see :func:`run_recipe_on_sweep`)."""

    sweep: int
    before: np.ndarray  # raw target, mask not applied
    after: np.ndarray  # final target (mask applied), float32 with NaN
    mask: np.ndarray
    removed_by: np.ndarray  # float32; 1-based index of the step that removed a gate, NaN if kept
    steps: list[dict[str, Any]]
    fields: dict[str, np.ndarray]  # every loaded role, mask applied (for "apply to all fields")
    units: dict[str, str]
    elapsed_ms: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def n_valid_before(self) -> int:
        return int(np.isfinite(self.before).sum())

    @property
    def n_valid_after(self) -> int:
        return int(np.isfinite(self.after).sum())


def _load_state(
    recipe: Recipe, ctx: Any, *, roles_needed: list[str], roles_optional: list[str]
) -> WorkingState:
    ds = ctx.dataset
    if "azimuth" not in ds.coords or "range" not in ds.coords:
        raise ModuleError("This sweep has no azimuth/range coordinates (not a polar PPI)")
    fields: dict[str, np.ndarray] = {}
    units: dict[str, str] = {}
    names: dict[str, str] = {}
    shape = (ds.sizes["azimuth"], ds.sizes["range"])
    for role in [*roles_needed, *roles_optional]:
        name = find_variable(ds, role)
        if name is None:
            if role in roles_needed:
                have = sorted(str(v) for v in ds.data_vars)
                raise ModuleError(f"no {role} field on this sweep (has {', '.join(have)})")
            continue
        arr = polar_array(ds[name])
        if arr.shape != shape:
            continue
        fields[role] = arr
        names[role] = name
        units[role] = str(ds[name].attrs.get("units", ""))
    target = roles_needed[0]
    return WorkingState(
        ctx=ctx,
        target=target,
        fields=fields,
        mask=np.ones(shape, dtype=bool),
        azimuth=np.asarray(ds["azimuth"].values, dtype=np.float64),
        range_m=np.asarray(ds["range"].values, dtype=np.float64),
        units=units,
        var_names=names,
    )


def run_recipe_on_sweep(
    recipe: Recipe,
    ctx: Any,
    *,
    progress: Callable[[int, str], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    get_sweep_dataset: Callable[[int], Any] | None = None,
) -> SweepRun:
    """Run *recipe* on the sweep of a :class:`~engine.plugins.ModuleContext`.

    A sweep lacking a required role raises :class:`ModuleError`. A step that does not apply
    (:class:`StepSkippedError`) is recorded and the pipeline carries on; a step that fails aborts
    the
    sweep with an error naming the step.
    """
    _ensure_steps()
    t_start = time.perf_counter()
    specs = [(s, get_step(s["step_id"])) for s in recipe.steps]
    needed = required_roles(recipe)
    optional = optional_roles(recipe)
    state = _load_state(recipe, ctx, roles_needed=needed, roles_optional=optional)
    loader = get_sweep_dataset or getattr(ctx, "_get_ds", None)
    notes: list[str] = []
    if loader is not None:
        from engine import pipeline_steps

        notes = pipeline_steps.borrow_split_cut_fields(
            state, optional, ctx.datatree, int(getattr(ctx, "sweep", 0)), loader
        )
    before = state.values.copy()
    n0 = int(np.isfinite(before).sum())
    removed_by = np.full(state.shape, np.nan, dtype=np.float32)
    stats: list[dict[str, Any]] = []
    enabled_idx = 0
    n_enabled = max(1, len(recipe.enabled_steps()))

    for pos, (item, spec) in enumerate(specs):
        row: dict[str, Any] = {
            "index": pos,
            "step_id": spec.id,
            "title": spec.title,
            "enabled": bool(item.get("enabled", True)),
            "status": "disabled",
            "reason": "",
            "removed": 0,
            "modified": 0,
            "filled": 0,
            "valid": int(state.valid().sum()),
            "valid_pct": 100.0 * int(state.valid().sum()) / n0 if n0 else 0.0,
            "ms": 0.0,
            "notes": [],
        }
        stats.append(row)
        if not row["enabled"]:
            continue
        if is_cancelled is not None and is_cancelled():
            raise RunCancelledError()
        if progress is not None:
            progress(int(100 * enabled_idx / n_enabled), spec.title)
        enabled_idx += 1
        mask_before = state.mask.copy()
        valid_before = state.valid()
        t0 = time.perf_counter()
        try:
            outcome = spec.func(state, **item["params"]) or StepOutcome()
            row["status"] = "ok"
        except StepSkippedError as exc:
            outcome = StepOutcome()
            row["status"] = "skipped"
            row["reason"] = str(exc)
        except ModuleError as exc:
            raise ModuleError(f"Step {pos + 1} ({spec.title}): {exc}") from exc
        except RunCancelledError:
            raise
        except Exception as exc:
            logger.warning("Pipeline step %s failed", spec.id, exc_info=True)
            raise ModuleError(
                f"Step {pos + 1} ({spec.title}): {type(exc).__name__}: {exc}"
            ) from exc
        row["ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        gone = valid_before & ~state.valid()
        newly = mask_before & ~state.mask & np.isfinite(before)
        removed_by[np.isnan(removed_by) & newly] = pos + 1
        row["removed"] = int(gone.sum())
        row["modified"] = int(outcome.modified)
        row["filled"] = int(outcome.filled)
        row["valid"] = int(state.valid().sum())
        row["valid_pct"] = 100.0 * row["valid"] / n0 if n0 else 0.0
        row["notes"] = list(outcome.notes)
        row["metrics"] = outcome.metrics

    removed_by[state.mask] = np.nan  # gates re-admitted by a later gap fill are not "removed"
    final = {
        r: np.where(state.mask, a, np.nan).astype(np.float32)
        for r, a in state.fields.items()
        if r not in state.borrowed
    }
    return SweepRun(
        sweep=int(getattr(ctx, "sweep", 0)),
        before=before,
        after=final[state.target],
        mask=state.mask,
        removed_by=removed_by,
        steps=stats,
        fields=final,
        units=dict(state.units),
        elapsed_ms=round((time.perf_counter() - t_start) * 1000.0, 1),
        notes=notes,
    )


# ---------------------------------------------------------------------------
# All sweeps -> ONE derived variable
# ---------------------------------------------------------------------------


@dataclass
class PipelineVolumeRun:
    recipe: Recipe
    elapsed_ms: float = 0.0
    arrays: dict[str, dict[int, np.ndarray]] = field(default_factory=dict)  # role -> sweep -> array
    units: dict[str, str] = field(default_factory=dict)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    per_sweep: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)  # aggregated over sweeps
    per_sweep_ms: dict[int, float] = field(default_factory=dict)


def _aggregate(per_sweep: list[list[dict[str, Any]]], valid0: int) -> list[dict[str, Any]]:
    """Sum the per-step stats of all sweeps into one table."""
    if not per_sweep:
        return []
    out: list[dict[str, Any]] = []
    for i in range(len(per_sweep[0])):
        rows = [sw[i] for sw in per_sweep]
        first = rows[0]
        statuses = {r["status"] for r in rows}
        status = (
            "disabled"
            if statuses == {"disabled"}
            else "skipped"
            if statuses <= {"skipped", "disabled"}
            else "ok"
        )
        reasons = sorted({r["reason"] for r in rows if r["reason"]})
        valid = sum(r["valid"] for r in rows)
        out.append(
            {
                "index": first["index"],
                "step_id": first["step_id"],
                "title": first["title"],
                "enabled": first["enabled"],
                "status": status,
                "reason": "; ".join(reasons),
                "removed": sum(r["removed"] for r in rows),
                "modified": sum(r["modified"] for r in rows),
                "filled": sum(r["filled"] for r in rows),
                "valid": valid,
                "valid_pct": 100.0 * valid / valid0 if valid0 else 0.0,
                "ms": round(sum(r["ms"] for r in rows), 1),
                "notes": sorted({n for r in rows for n in r["notes"]}),
            }
        )
    return out


def run_recipe_all_sweeps(
    recipe: Recipe,
    datatree: Any,
    n_sweeps: int,
    *,
    get_sweep_dataset: Callable[[int], Any],
    get_sweep_variables: Callable[[int], set[str]],
    progress: Callable[[int, str], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    sweeps: list[int] | None = None,
) -> PipelineVolumeRun:
    """Run *recipe* on sweeps ``0..n_sweeps-1`` (or just ``sweeps``); sweeps lacking a required
    role are skipped.

    Raises :class:`ModuleError` when no sweep could be processed (message quotes the first
    reason) and :class:`~engine.derived.RunCancelledError` on cancellation.
    """
    _ensure_steps()
    if n_sweeps <= 0:
        raise ModuleError("The file has no sweeps")
    run = PipelineVolumeRun(recipe=recipe)
    need = required_roles(recipe)
    t_start = time.perf_counter()
    last_pct = -1
    stats_by_sweep: list[list[dict[str, Any]]] = []
    valid0 = 0
    todo = list(range(n_sweeps)) if sweeps is None else [s for s in sweeps if 0 <= s < n_sweeps]
    if not todo:
        raise ModuleError("No such sweep")

    for pos, sweep in enumerate(todo):
        if is_cancelled is not None and is_cancelled():
            raise RunCancelledError()
        label = f"{recipe.name}: sweep {pos + 1}/{len(todo)}"

        def report(inner: int, msg: str, _s: int = pos, _label: str = label) -> None:
            nonlocal last_pct
            if progress is None:
                return
            pct = min(95, 2 + int((_s + min(max(inner, 0), 100) / 100.0) / len(todo) * 93))
            if pct != last_pct:
                last_pct = pct
                progress(pct, f"{_label} - {msg}" if msg else _label)

        report(0, "")
        variables = get_sweep_variables(sweep)
        lacking = [
            r
            for r in need
            if not any(a in variables for a in plugins.VAR_ALIASES.get(r, [r]))
            and r not in variables
        ]
        if lacking:
            have = ", ".join(sorted(variables)) or "no fields"
            run.skipped.append(
                {
                    "sweep": sweep,
                    "reason": f"no {', '.join(lacking)} field on this sweep (has {have})",
                }
            )
            continue
        t0 = time.perf_counter()
        try:
            ctx = plugins.ModuleContext(datatree, sweep, get_sweep_dataset=get_sweep_dataset)
            sw = run_recipe_on_sweep(
                recipe,
                ctx,
                progress=report,
                is_cancelled=is_cancelled,
                get_sweep_dataset=get_sweep_dataset,
            )
        except ModuleError as exc:
            run.skipped.append({"sweep": sweep, "reason": str(exc)})
            continue
        except RunCancelledError:
            raise
        except Exception as exc:  # one bad sweep must not lose the others
            logger.warning("Pipeline sweep %d failed: %s", sweep, exc, exc_info=True)
            run.skipped.append({"sweep": sweep, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        run.per_sweep_ms[sweep] = round((time.perf_counter() - t0) * 1000.0, 1)
        valid0 += sw.n_valid_before
        stats_by_sweep.append(sw.steps)
        run.per_sweep.append(
            {
                "sweep": sweep,
                "valid_before": sw.n_valid_before,
                "valid_after": sw.n_valid_after,
                "steps": [
                    {
                        k: r[k]
                        for k in ("step_id", "status", "removed", "modified", "filled", "valid")
                    }
                    for r in sw.steps
                ],
            }
        )
        keep_roles = list(sw.fields) if recipe.apply_to_all else [recipe.target_role]
        for role in keep_roles:
            arr = sw.fields.get(role)
            if arr is not None:
                run.arrays.setdefault(role, {})[sweep] = np.array(arr, dtype=np.float32, copy=True)
                run.units.setdefault(role, sw.units.get(role, ""))

    if not run.arrays:
        first = run.skipped[0]["reason"] if run.skipped else "no sweep produced data"
        raise ModuleError(f"{recipe.name} could not be applied to any sweep ({first})")
    run.steps = _aggregate(stats_by_sweep, valid0)
    run.elapsed_ms = round((time.perf_counter() - t_start) * 1000.0, 1)
    return run


def derived_name(recipe: Recipe, role: str) -> str:
    """``pipeline:<recipe_slug>_<role>`` (a derived name allows a single colon)."""
    return derived.make_name("pipeline", f"{recipe.slug}_{derived.slug(role)}")


def _range_for(role: str, arrays: dict[int, np.ndarray]) -> tuple[float, float]:
    _, lo, hi, _ = ROLE_STYLE.get(role, ("", None, None, None))
    if lo is not None and hi is not None:
        return lo, hi
    pooled = derived._pooled_percentiles(list(arrays.values()))
    if role == "VRADH":
        lim = max(abs(v) for v in pooled) if pooled else 1.0
        return -max(lim, 1.0), max(lim, 1.0)
    if pooled is None:
        return 0.0, 1.0
    lo2, hi2 = pooled
    return lo2, hi2 if hi2 > lo2 else lo2 + 1.0


def build_variables(
    run: PipelineVolumeRun, computed_at: float | None = None
) -> list[tuple[str, dict[int, np.ndarray], dict[str, Any]]]:
    """``[(name, {sweep: array}, meta)]``: exactly one entry unless ``apply_to_all`` is set.

    The metadata carries the recipe (steps + parameters) as provenance: it is listed in the
    derived-variable ``params`` and written to the ``comment`` attribute of exported files.
    """
    recipe = run.recipe
    stamp = time.time() if computed_at is None else computed_at
    provenance = {
        "recipe": recipe.name,
        "target": recipe.target,
        "steps": [
            {"step": s["step_id"], "params": s["params"]}
            for s in recipe.steps
            if s.get("enabled", True)
        ],
    }
    out = []
    roles = [recipe.target_role] + [r for r in run.arrays if r != recipe.target_role]
    for role in roles:
        arrays = run.arrays.get(role)
        if not arrays:
            continue
        units, _, _, cmap = ROLE_STYLE.get(role, ("", None, None, None))
        vmin, vmax = _range_for(role, arrays)
        main = role == recipe.target_role
        meta = {
            "label": f"{recipe.name} [{recipe.target}]" if main else f"{recipe.name} [{role}]",
            "units": run.units.get(role) or units,
            "vmin": vmin,
            "vmax": vmax,
            "cmap": cmap,
            "categories": None,
            "module": "pipeline",
            "module_title": "Pipeline",
            "field": role,
            "params": provenance,
            "computed_at": stamp,
        }
        out.append((derived_name(recipe, role), arrays, meta))
    return out


STEP_COLORS = [
    "#ef4444",
    "#f59e0b",
    "#8b5cf6",
    "#06b6d4",
    "#ec4899",
    "#84cc16",
    "#3b82f6",
    "#f97316",
    "#14b8a6",
    "#a855f7",
]


def preview_fields(
    run: SweepRun, recipe: Recipe
) -> tuple[list[dict[str, Any]], dict[str, np.ndarray]]:
    """Field blocks + arrays for the preview: ``Before``, ``After`` and ``Removed by step``.

    The blocks have the shape of a module result's ``field`` block, so the UI shows them on the PPI
    with the same machinery as module fields. "Removed by step" is categorical (one class per
    step that removed gates) so you can see which step took out what.
    """
    role = recipe.target_role
    units, _, _, cmap = ROLE_STYLE.get(role, ("", None, None, None))
    units = run.units.get(role) or units
    vmin, vmax = _range_for(role, {0: run.before})

    def block(name: str, title: str, arr: np.ndarray, **kw: Any) -> dict[str, Any]:
        finite = int(np.isfinite(arr).sum())
        return {
            "type": "field",
            "name": name,
            "title": title,
            "units": kw.get("units", units),
            "vmin": kw.get("vmin", vmin),
            "vmax": kw.get("vmax", vmax),
            "cmap": kw.get("cmap", cmap),
            "categories": kw.get("categories"),
            "shape": [int(arr.shape[0]), int(arr.shape[1])],
            "valid_fraction": finite / arr.size if arr.size else 0.0,
        }

    cats = [
        {
            "value": int(r["index"]) + 1,
            "label": f"{int(r['index']) + 1}. {r['title']}",
            "color": STEP_COLORS[int(r["index"]) % len(STEP_COLORS)],
        }
        for r in run.steps
        if r.get("removed", 0) > 0
    ]
    blocks = [
        block("Before", f"Before: {recipe.target}", run.before),
        block("After", f"After: {recipe.name}", run.after),
    ]
    fields = {"Before": run.before, "After": run.after}
    if cats:
        blocks.append(
            block("Removed by step", "Removed by step", run.removed_by, units="", categories=cats)
        )
        fields["Removed by step"] = run.removed_by
    return blocks, fields


# ---------------------------------------------------------------------------
# Downstream use: substitute a role by a derived variable
# ---------------------------------------------------------------------------


def module_overrides(
    store: derived.DerivedStore | None, inputs: Any
) -> dict[str, Callable[[int], np.ndarray]] | None:
    """Turn ``{"velocity": "pipeline:..."}`` into the override map of ``ModuleContext``.

    Keys are roles (canonical names or aliases such as ``velocity``/``reflectivity``); values are
    derived-variable names of the file. The returned callables give the array for a sweep and
    raise :class:`ModuleError` if the derived variable does not exist on that sweep, so an
    all-sweeps module run reports it as a skipped sweep instead of silently using raw data.
    """
    if inputs in (None, {}):
        return None
    if not isinstance(inputs, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in inputs.items()
    ):
        raise ModuleError("'inputs' must map a variable role to a derived variable name")
    overrides: dict[str, Callable[[int], np.ndarray]] = {}
    for role, name in inputs.items():
        if store is None or not store.has(name):
            raise ModuleError(f"Derived variable '{name}' (input '{role}') does not exist")

        def lookup(sweep: int, _name: str = name) -> np.ndarray:
            got = store.get(sweep, _name) if store is not None else None
            if got is None:
                raise ModuleError(f"Derived variable '{_name}' is not available on sweep {sweep}")
            return got[0]

        overrides[canonical_role(role)] = lookup
    return overrides


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _web_mode() -> bool:
    from engine import web

    return web.is_web_mode()


#: Web mode: the calling session's private recipes folder (set per request by the server).
SESSION_RECIPES_DIR: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "xradar_session_recipes_dir", default=None
)


def recipes_dir() -> Path:
    from engine import web

    if web.is_web_mode():
        # Never ~/.xradar-desktop in a public service: recipes live in the session folder.
        folder = SESSION_RECIPES_DIR.get()
        if folder is None:
            raise ModuleError("Recipes are not available outside a web session")
        return folder
    env = os.environ.get("XRADAR_RECIPES_DIR")
    return Path(env).expanduser() if env else Path.home() / ".xradar-desktop" / "recipes"


def builtin_recipes() -> list[Recipe]:
    """Example recipes that ship with the app (read-only)."""
    raw: list[dict[str, Any]] = [
        {
            "name": "Doppler velocity: dealias + clutter + bio filter",
            "description": (
                "Removes ground clutter and biological echo, de-aliases the velocity, drops "
                "isolated gates and fills tiny holes. Feed the result to a wind analysis."
            ),
            "target": "VRADH",
            "steps": [
                {"step_id": "snr_gate", "params": {"min_snr": 3.0}},
                {"step_id": "clutter_filter", "params": {}},
                {"step_id": "biological_filter", "params": {}},
                {"step_id": "dealias", "params": {}},
                {"step_id": "speckle", "params": {"min_size": 12}},
                {"step_id": "gap_fill", "params": {"radius_km": 0.6}, "enabled": False},
            ],
        },
        {
            "name": "Reflectivity QC",
            "description": "SNR gate, meteorological echo mask, clutter and speckle removal.",
            "target": "DBZH",
            "steps": [
                {"step_id": "snr_gate", "params": {"min_snr": 3.0}},
                {"step_id": "echo_mask", "params": {}},
                {"step_id": "clutter_filter", "params": {}},
                {"step_id": "speckle", "params": {"min_size": 10}},
                {"step_id": "gap_fill", "params": {"radius_km": 0.6}},
            ],
        },
        {
            "name": "Dual-pol QC",
            "description": (
                "ZDR cleaning: RhoHV gate, echo mask, biological filter, speckle, noise "
                "correction and light smoothing."
            ),
            "target": "ZDR",
            "steps": [
                {"step_id": "snr_gate", "params": {"min_snr": 5.0}},
                {"step_id": "rhohv_gate", "params": {"min_rhohv": 0.85}},
                {"step_id": "echo_mask", "params": {}},
                {"step_id": "biological_filter", "params": {}},
                {"step_id": "speckle", "params": {"min_size": 10}},
                {"step_id": "noise_correction", "params": {}, "enabled": False},
                {
                    "step_id": "smooth",
                    "params": {"method": "median", "window": 3},
                    "enabled": False,
                },
            ],
        },
    ]
    return [replace_builtin(parse_recipe(r)) for r in raw]


def replace_builtin(recipe: Recipe) -> Recipe:
    recipe.builtin = True
    return recipe


def _path_for(name: str) -> Path:
    return recipes_dir() / f"{derived.slug(name)[:60]}.json"


RECIPE_SCHEMA = "xradar-desktop/recipe"


def recipe_file_dict(recipe: Recipe) -> dict[str, Any]:
    """What a saved recipe file contains: the recipe, a schema tag and who/when/with what made it.

    ``version`` is the schema version (:data:`RECIPE_VERSION`); readers refuse a newer one.
    ``created_with`` is informational and ignored when the recipe is loaded.
    """
    from engine import provenance

    body = recipe.to_dict()
    body.pop("builtin", None)
    return {
        "schema": RECIPE_SCHEMA,
        **body,
        "created_with": provenance.json_provenance(action="save recipe"),
    }


def save_recipe(raw: Any) -> Recipe:
    """Validate and write a recipe as ``<recipes_dir>/<slug>.json`` (atomic); returns it."""
    recipe = parse_recipe(raw)
    if recipe.slug in {derived.slug(b.name)[:40].strip("_") for b in builtin_recipes()}:
        raise ModuleError(f"'{recipe.name}' is a built-in recipe; save it under another name")
    recipe.builtin = False
    path = _path_for(recipe.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if _web_mode() and not path.exists() and len(list(path.parent.glob("*.json"))) >= 50:
        raise ModuleError("Too many saved recipes in this session (limit 50); delete some first")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(recipe_file_dict(recipe), indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return recipe


def load_user_recipes() -> tuple[list[Recipe], list[dict[str, str]]]:
    """``(recipes, errors)`` from the recipes folder; unreadable files are reported, not fatal."""
    recipes: list[Recipe] = []
    errors: list[dict[str, str]] = []
    folder = recipes_dir()
    if not folder.is_dir():
        return recipes, errors
    for path in sorted(folder.glob("*.json")):
        try:
            recipes.append(parse_recipe(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, ModuleError) as exc:
            errors.append({"file": str(path), "error": str(exc)})
    return recipes, errors


def list_recipes() -> dict[str, Any]:
    user, errors = load_user_recipes()
    return {
        "recipes": [r.to_dict() for r in [*builtin_recipes(), *user]],
        "errors": errors,
        "dir": "" if _web_mode() else str(recipes_dir()),
    }


def delete_recipe(name: str) -> bool:
    """Delete a user recipe by name; returns False if it does not exist. Built-ins are refused."""
    if not isinstance(name, str) or not name.strip():
        raise ModuleError("A recipe name is required")
    if any(b.name == name for b in builtin_recipes()):
        raise ModuleError("Built-in recipes cannot be deleted")
    path = _path_for(name)
    if not path.is_file():
        return False
    path.unlink()
    return True


# ---------------------------------------------------------------------------
# Script export
# ---------------------------------------------------------------------------


def recipe_to_script(recipe: Recipe) -> str:
    """A standalone Python script (numpy / scipy / xarray, optional radarx) that applies the recipe.

    The script defines ``process_sweep(ds)`` for one sweep dataset (as opened by xradar), returning
    the processed target as a DataArray (NaN = removed); run as a script it opens a file given on
    the command line, processes every sweep that has the target and saves a NetCDF. The step
    kernels are copied verbatim from xradar-desktop, so the numbers match the app.
    """
    import inspect
    import textwrap

    _ensure_steps()
    chosen = [(s, get_step(s["step_id"])) for s in recipe.steps if s.get("enabled", True)]
    kernels: list[Callable[..., Any]] = []
    for _, spec in chosen:
        for k in spec.kernels:
            if k not in kernels:
                kernels.append(k)
    body: list[str] = []
    for i, (item, spec) in enumerate(chosen, start=1):
        body.append(f"    # --- step {i}: {spec.title}")
        rendered = textwrap.dedent(spec.script).strip("\n") if spec.script else "pass"
        for key, val in item["params"].items():
            rendered = rendered.replace("{" + key + "}", repr(val))
        body.extend(textwrap.indent(rendered, "    ").splitlines())
        body.append("")
    kernel_src = "\n\n".join(inspect.getsource(k).rstrip() + "\n" for k in kernels)
    from engine import provenance

    return _SCRIPT_TEMPLATE.format(
        software=provenance.software_string(),
        software_repr=repr(provenance.software_string()),
        name_repr=repr(recipe.name),
        titles_repr=repr(", ".join(spec.title for _, spec in chosen)),
        generated=provenance.utc_stamp(),
        recipe_version=RECIPE_VERSION,
        libraries=provenance.libraries_string(),
        name=recipe.name.replace('"', "'"),
        target=recipe.target,
        titles=", ".join(spec.title for _, spec in chosen),
        aliases=json.dumps(plugins.VAR_ALIASES, indent=4),
        role=recipe.target_role,
        kernels=kernel_src,
        body="\n".join(body),
    )


_SCRIPT_TEMPLATE = '''#!/usr/bin/env python
"""Pipeline "{name}" exported from xradar-desktop.

Target: {target}.  Steps: {titles}.
Generated by {software} on {generated} (recipe format {recipe_version}; {libraries}).
Needs numpy, scipy, xarray and xradar. radarx is optional (better de-aliasing / echo masks).
Usage: python this_script.py INPUT_FILE OUTPUT.nc
"""

from __future__ import annotations

import json
import os
import sys
import warnings
from datetime import datetime, timezone

import numpy as np
import xarray as xr
from scipy import ndimage

try:
    import radarx.retrieve as rt
except ImportError:  # steps fall back to their built-in numpy versions
    rt = None

ROLE = "{role}"
ALIASES = json.loads("""{aliases}""")
RECIPE_NAME = {name_repr}
STEPS = {titles_repr}
GENERATED_BY = {software_repr}


def find(ds, role):
    """The sweep variable standing for ``role`` (canonical name or an alias) as (az, range)."""
    for name in ALIASES.get(role, [role]):
        if name in ds.data_vars:
            return ds[name].transpose("azimuth", "range").values.astype("float32")
    return None


def nyquist_of(ds, vel):
    """Nyquist velocity: from the file, else the largest valid |velocity| (rounded up)."""
    if "nyquist_velocity" in ds:
        return float(np.nanmedian(ds["nyquist_velocity"].values))
    return float(np.ceil(np.nanmax(np.abs(vel)) * 2) / 2)


def _rt_dataset(ds, fields, keep):
    """The sweep with canonical names and removed gates set to NaN, for radarx."""
    out = ds
    for name, arr in fields.items():
        out = out.assign({{name: (("azimuth", "range"), np.where(keep, arr, np.nan))}})
    return out


# ----- kernels copied from xradar-desktop (pure numpy / scipy) -----


{kernels}

def process_sweep(ds):
    """Apply the recipe to one sweep dataset; returns the processed target (NaN = removed)."""
    azimuth = np.asarray(ds["azimuth"].values, dtype=float)
    range_m = np.asarray(ds["range"].values, dtype=float)
    role = ROLE
    fields = {{r: find(ds, r) for r in ALIASES}}
    fields = {{r: a for r, a in fields.items() if a is not None}}
    if role not in fields:
        fields[role] = find(ds, role)
    if fields[role] is None:
        raise KeyError(f"sweep has no {{role}} field")
    keep = np.ones(fields[role].shape, dtype=bool)

{body}
    out = np.where(keep, fields[role], np.nan).astype("float32")
    units = ""
    for name in ALIASES.get(role, [role]):
        if name in ds.data_vars:
            units = str(ds[name].attrs.get("units", ""))
            break
    return xr.DataArray(
        out,
        dims=("azimuth", "range"),
        coords={{"azimuth": ds["azimuth"], "range": ds["range"]}},
        attrs={{
            "long_name": f"{{ROLE}} after pipeline '{{RECIPE_NAME}}' (NaN = removed)",
            "units": units or "1",
            "comment": f"steps: {{STEPS}}",
        }},
    )


def main(src, dst):
    import xradar as xd

    tree = xd.io.open_cfradial1_datatree(src) if src.endswith(".nc") else xr.open_datatree(src)
    outputs = {{}}
    for name, node in tree.children.items():
        if not name.startswith("sweep"):
            continue
        try:
            outputs[name] = process_sweep(node.to_dataset()).to_dataset(name=ROLE + "_pipeline")
        except KeyError as exc:
            print(f"skip {{name}}: {{exc}}")
    result = xr.DataTree.from_dict(outputs)
    result.attrs = {{
        "Conventions": "CF-1.8",
        "title": f"Pipeline '{{RECIPE_NAME}}' applied to {{os.path.basename(src)}}",
        "source": os.path.basename(src),
        "history": (
            f"{{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}}: {{GENERATED_BY}} pipeline script: "
            f"{{STEPS}}"
        ),
        "references": "CF Conventions https://cfconventions.org/",
        "comment": "Removed gates are NaN (_FillValue).",
    }}
    result.to_netcdf(dst)


if __name__ == "__main__":
    main(*sys.argv[1:3])
'''
