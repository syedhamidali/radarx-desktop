"""Plugin ("module") SDK for xradar-desktop.

A module is a single Python file that declares what it needs, what knobs it
exposes, and returns a :class:`Result`. The sidecar discovers it automatically
and the UI builds a form + result views from the declaration — no server or
frontend edits required.

Minimal example (drop into ``python/modules/`` or ``~/.xradar-desktop/modules/``)::

    from engine.plugins import module, param, Result

    @module(
        id="hot_gates",
        title="Hot Gates",
        description="Count gates above a threshold.",
        category="Demo",
        requires=["DBZH"],
        params=[param("threshold", "number", 50.0, "Threshold (dBZ)", min=0, max=80)],
    )
    def run(ctx, threshold):
        z = ctx.field("DBZH").values
        res = Result()
        res.scalars({"gates_above": int((z >= threshold).sum())})
        return res

Discovery order: built-in ``python/modules`` → ``~/.xradar-desktop/modules`` →
directories listed in ``XRADAR_MODULES_DIR`` (``os.pathsep`` separated). Later
sources override earlier ones with the same ``id``. Modules run with the
privileges of the sidecar, so only install modules you trust.
"""

from __future__ import annotations

import importlib.util
import logging
import math
import os
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

logger = logging.getLogger(__name__)

PARAM_TYPES = ("number", "int", "bool", "select", "string")
# "sweep": computed from one sweep, so it can also run on every sweep and become a derived
# variable. "volume": integrates/grids the whole volume and returns one product on a single
# sweep's grid, so running it per sweep makes no sense.
MODULE_SCOPES = ("sweep", "volume")

# Canonical variable aliases — a module asking for "ZDR" also matches
# "differential_reflectivity" etc. (same table the dual-pol tools use).
VAR_ALIASES: dict[str, list[str]] = {
    "DBZH": [
        "DBZH",
        "DBZ",
        "reflectivity",
        "Z",
        "corrected_reflectivity_horizontal",
        "DBZHCC",
        "DBZHC",  # NCAR/CSU-style clutter-corrected reflectivity
    ],
    "ZDR": ["ZDR", "differential_reflectivity", "Zdr", "ZDRC"],
    "PHIDP": ["PHIDP", "differential_phase", "PhiDP", "UPHIDP"],
    "KDP": ["KDP", "specific_differential_phase"],
    "RHOHV": ["RHOHV", "cross_correlation_ratio", "RhoHV", "CCORH"],
    "VRADH": ["VRADH", "velocity", "V", "radial_velocity", "VEL"],
    "SNR": ["SNR", "SNRH", "SNRHC", "SNRV", "SNRVC", "snr"],
    "NCP": ["NCP", "normalized_coherent_power", "SQI", "SQIH"],
    "WRADH": ["WRADH", "WIDTH", "spectrum_width"],
}


class ModuleError(Exception):
    """Raised by a module to report a user-facing problem (bad data, missing field…)."""


class ModuleTimeoutError(ModuleError):
    """A module run exceeded the wall-clock limit (``XRADAR_MODULE_TIMEOUT_S``)."""


class ModuleCancelled(Exception):  # noqa: N818 - control-flow signal, not an error condition
    """Raised inside a module (by ``ctx.progress`` / ``ctx.check_cancelled``) once its run was
    abandoned (timeout or the connection went away). Modules should let it propagate."""


# ---------------------------------------------------------------------------
# Parameter declaration
# ---------------------------------------------------------------------------


def param(
    name: str,
    type: str,  # noqa: A002 - mirrors the JSON schema field
    default: Any,
    label: str | None = None,
    *,
    min: float | None = None,  # noqa: A002
    max: float | None = None,  # noqa: A002
    step: float | None = None,
    options: list[Any] | None = None,
    help: str | None = None,  # noqa: A002
) -> dict[str, Any]:
    """Declare a module parameter. ``type`` is one of :data:`PARAM_TYPES`."""
    if type not in PARAM_TYPES:
        raise ValueError(f"param '{name}': type must be one of {PARAM_TYPES}, got {type!r}")
    if type == "select" and not options:
        raise ValueError(f"param '{name}': select params need 'options'")
    spec: dict[str, Any] = {
        "name": name,
        "type": type,
        "default": default,
        "label": label or name.replace("_", " ").capitalize(),
    }
    extras = (("min", min), ("max", max), ("step", step), ("options", options), ("help", help))
    for key, val in extras:
        if val is not None:
            spec[key] = val
    return spec


def _coerce_params(specs: list[dict[str, Any]], raw: dict[str, Any]) -> dict[str, Any]:
    """Validate/coerce user-supplied params against the declaration (defaults fill gaps)."""
    out: dict[str, Any] = {}
    for spec in specs:
        name, typ = spec["name"], spec["type"]
        val = raw.get(name, spec["default"])
        try:
            if typ == "bool":
                val = bool(val)
            elif typ in ("number", "int"):
                if isinstance(val, bool) or val is None:
                    raise TypeError("expected a number")
                val = float(val)
                if not math.isfinite(val):
                    raise ValueError("must be finite")
                if "min" in spec:
                    val = max(val, spec["min"])
                if "max" in spec:
                    val = min(val, spec["max"])
                if typ == "int":
                    val = round(val)
            elif typ == "select":
                if val not in spec["options"]:
                    raise ValueError(f"must be one of {spec['options']}")
            else:
                val = str(val)
        except (TypeError, ValueError) as exc:
            raise ModuleError(f"parameter '{name}': {exc}") from exc
        out[name] = val
    return out


# ---------------------------------------------------------------------------
# Context handed to a running module
# ---------------------------------------------------------------------------


class ModuleContext:
    """Everything a module needs to read data. Instances are created per run."""

    def __init__(
        self,
        datatree: xr.DataTree,
        sweep: int,
        *,
        get_sweep_dataset: Callable[[int], xr.Dataset],
        progress: Callable[[int, str], None] | None = None,
    ) -> None:
        self.datatree = datatree
        self.sweep = sweep
        self._get_ds = get_sweep_dataset
        self._progress = progress
        self._ds: xr.Dataset | None = None

    @property
    def dataset(self) -> xr.Dataset:
        """The current sweep as an ``xr.Dataset`` (loaded once, then cached)."""
        if self._ds is None:
            self._ds = self._get_ds(self.sweep)
        return self._ds

    def find(self, name: str) -> xr.DataArray | None:
        """Look up a variable by canonical name or alias; ``None`` if absent."""
        ds = self.dataset
        for alias in VAR_ALIASES.get(name, [name]):
            if alias in ds.data_vars:
                return ds[alias]
        return None

    def has(self, name: str) -> bool:
        return self.find(name) is not None

    def field(self, name: str) -> xr.DataArray:
        """Like :meth:`find` but raises :class:`ModuleError` if the variable is missing."""
        da = self.find(name)
        if da is None:
            avail = sorted(str(v) for v in self.dataset.data_vars)
            raise ModuleError(f"Sweep {self.sweep} has no '{name}' field (available: {avail})")
        return da

    def polar_coords(self) -> tuple[np.ndarray, np.ndarray]:
        """``(azimuth_deg, range_m)`` float64 arrays for the current sweep."""
        ds = self.dataset
        return (
            np.asarray(ds["azimuth"].values, dtype=np.float64),
            np.asarray(ds["range"].values, dtype=np.float64),
        )

    # Set by run_module; a module that loops for a long time can poll ``ctx.cancelled``
    # (or just call ``ctx.progress``, which raises ModuleCancelled) to stop early.
    _cancel_event: threading.Event | None = None

    @property
    def cancelled(self) -> bool:
        """True once this run was abandoned (timed out / client gone); its result is discarded."""
        return self._cancel_event is not None and self._cancel_event.is_set()

    def check_cancelled(self) -> None:
        if self.cancelled:
            raise ModuleCancelled()

    def progress(self, percent: int, message: str) -> None:
        self.check_cancelled()
        if self._progress is not None:
            self._progress(int(percent), message)


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


def _clean(obj: Any) -> Any:
    """Make *obj* strictly JSON-safe: numpy → python, NaN/Inf → None."""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return _clean(obj.tolist())
    if isinstance(obj, np.generic):
        return _clean(obj.item())
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    return obj


class Result:
    """Structured module output. The UI renders each block type generically."""

    def __init__(self) -> None:
        self.blocks: list[dict[str, Any]] = []
        # name -> (n_azimuth, n_range) float32 array; kept out of the JSON blocks and
        # sent separately as binary by the server.
        self.fields: dict[str, np.ndarray] = {}

    def scalars(
        self, values: dict[str, Any] | list[dict[str, Any]], title: str = "Summary"
    ) -> Result:
        """Key/value summary. Dict form: ``{"Cells": 4}``; list form: ``[{label, value, unit}]``."""
        if isinstance(values, dict):
            values = [{"label": k, "value": v} for k, v in values.items()]
        self.blocks.append({"type": "scalars", "title": title, "items": values})
        return self

    def table(
        self,
        title: str,
        columns: list[dict[str, Any] | str],
        rows: list[dict[str, Any]],
        *,
        id_key: str | None = None,
    ) -> Result:
        """Sortable table. ``columns`` entries are ``"key"`` or ``{key, label, unit, digits}``.

        If ``id_key`` is set, clicking a row highlights the overlay feature with that id.
        """
        cols = [c if isinstance(c, dict) else {"key": c, "label": c} for c in columns]
        self.blocks.append(
            {"type": "table", "title": title, "columns": cols, "rows": rows, "id_key": id_key}
        )
        return self

    def overlay(self, title: str, features: list[dict[str, Any]]) -> Result:
        """Map overlay drawn on the PPI. See :func:`polygon`, :func:`point`, :func:`line`."""
        self.blocks.append({"type": "overlay", "title": title, "features": features})
        return self

    def field(
        self,
        name: str,
        values: Any,
        *,
        units: str = "",
        vmin: float | None = None,
        vmax: float | None = None,
        cmap: str | None = None,
        categories: list[dict[str, Any]] | None = None,
        title: str | None = None,
    ) -> Result:
        """A 2-D gridded product on the sweep's ``(azimuth, range)`` grid, shown on the PPI.

        ``values`` is array-like of shape ``(n_azimuth, n_range)`` (NaN = no data). For a
        categorical product (e.g. hydrometeor class) pass
        ``categories=[{"value": 1, "label": "Rain", "color": "#3b82f6"}, ...]``; otherwise
        ``vmin``/``vmax`` set the colour scale (defaults to the 2nd-98th percentile) and
        ``cmap`` names a built-in colormap (``None`` = let the UI choose from ``units``).
        """
        if not name or not isinstance(name, str):
            raise ValueError("field name must be a non-empty string")
        arr = np.asarray(getattr(values, "values", values), dtype=np.float32)
        if arr.ndim != 2:
            raise ValueError(f"field '{name}' must be 2-D (azimuth, range), got shape {arr.shape}")
        finite = arr[np.isfinite(arr)]
        if categories is None and finite.size:
            if vmin is None:
                vmin = float(np.percentile(finite, 2))
            if vmax is None:
                vmax = float(np.percentile(finite, 98))
            if vmax <= vmin:
                vmax = vmin + 1.0
        self.fields[name] = arr
        self.blocks.append(
            {
                "type": "field",
                "name": name,
                "title": title or name,
                "units": units,
                "vmin": vmin,
                "vmax": vmax,
                "cmap": cmap,
                "categories": categories,
                "shape": [int(arr.shape[0]), int(arr.shape[1])],
                "valid_fraction": float(finite.size / arr.size) if arr.size else 0.0,
            }
        )
        return self

    def text(self, body: str, title: str = "Notes") -> Result:
        self.blocks.append({"type": "text", "title": title, "body": body})
        return self

    def to_dict(self) -> dict[str, Any]:
        return {"blocks": _clean(self.blocks)}


def polygon(
    fid: str,
    coords: list[list[float]],
    *,
    frame: str = "polar",
    label: str | None = None,
    color: str | None = None,
    fill: float = 0.12,
) -> dict[str, Any]:
    """Closed outline. ``frame='polar'`` → ``[azimuth_deg, range_m]``; ``'xy'`` → ``[x_m, y_m]``."""
    return {
        "id": fid,
        "kind": "polygon",
        "frame": frame,
        "coords": coords,
        "label": label,
        "color": color,
        "fill": fill,
    }


def point(
    fid: str,
    coord: list[float],
    *,
    frame: str = "polar",
    label: str | None = None,
    color: str | None = None,
) -> dict[str, Any]:
    return {
        "id": fid,
        "kind": "point",
        "frame": frame,
        "coords": [coord],
        "label": label,
        "color": color,
    }


def line(
    fid: str,
    coords: list[list[float]],
    *,
    frame: str = "polar",
    label: str | None = None,
    color: str | None = None,
) -> dict[str, Any]:
    return {
        "id": fid,
        "kind": "line",
        "frame": frame,
        "coords": coords,
        "label": label,
        "color": color,
    }


# ---------------------------------------------------------------------------
# Registry + decorator
# ---------------------------------------------------------------------------


@dataclass
class ModuleSpec:
    id: str
    title: str
    description: str
    category: str
    requires: list[str]
    params: list[dict[str, Any]]
    func: Callable[..., Result]
    source: str = "<unknown>"
    version: str = "1.0"
    optional: list[str] = field(default_factory=list)
    needs: list[str] = field(default_factory=list)
    scope: str = "sweep"

    @property
    def per_sweep(self) -> bool:
        """True if the module can be run independently on each sweep (``scope == "sweep"``)."""
        return self.scope == "sweep"

    def missing_packages(self) -> list[str]:
        """Names from ``needs`` that cannot be imported right now."""
        return [pkg for pkg in self.needs if importlib.util.find_spec(pkg) is None]

    def describe(self) -> dict[str, Any]:
        missing = self.missing_packages()
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "category": self.category,
            "requires": self.requires,
            "optional": self.optional,
            "params": self.params,
            "source": self.source,
            "version": self.version,
            "needs": self.needs,
            "scope": self.scope,
            "per_sweep": self.per_sweep,
            "available": not missing,
            "unavailable_reason": (
                f"Requires the Python package{'s' if len(missing) > 1 else ''} "
                f"{', '.join(missing)} (pip install {' '.join(missing)})"
                if missing
                else None
            ),
        }


MODULES: dict[str, ModuleSpec] = {}
LOAD_ERRORS: list[dict[str, str]] = []
_current_source: str = "<unknown>"


def module(
    *,
    id: str,  # noqa: A002
    title: str,
    description: str = "",
    category: str = "General",
    requires: list[str] | None = None,
    optional: list[str] | None = None,
    params: list[dict[str, Any]] | None = None,
    version: str = "1.0",
    needs: list[str] | None = None,
    scope: str = "sweep",
) -> Callable[[Callable[..., Result]], Callable[..., Result]]:
    """Register ``func(ctx, **params) -> Result`` as a module.

    ``needs`` lists optional Python packages (import names, e.g. ``["radarx"]``). The module
    is always listed, but is marked unavailable (and refuses to run) when one is missing, so
    import those packages *inside* the function, not at module top level.

    ``scope`` is ``"sweep"`` (default: the module works on the selected sweep alone, so it can
    also be run on all sweeps to build a derived variable) or ``"volume"`` (it uses the whole
    volume and returns one product on a single sweep's grid).
    """

    def decorator(func: Callable[..., Result]) -> Callable[..., Result]:
        if not id or not id.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"module id {id!r} must be alphanumeric/underscore/dash")
        if scope not in MODULE_SCOPES:
            raise ValueError(f"module '{id}': scope must be one of {MODULE_SCOPES}, got {scope!r}")
        names = [p["name"] for p in (params or [])]
        if len(names) != len(set(names)):
            raise ValueError(f"module '{id}' declares duplicate param names")
        if id in MODULES:
            logger.info("Module '%s' from %s overrides %s", id, _current_source, MODULES[id].source)
        MODULES[id] = ModuleSpec(
            id=id,
            title=title,
            description=description,
            category=category,
            requires=list(requires or []),
            optional=list(optional or []),
            params=list(params or []),
            func=func,
            source=_current_source,
            version=version,
            needs=list(needs or []),
            scope=scope,
        )
        return func

    return decorator


def module_dirs() -> list[Path]:
    dirs = [
        Path(__file__).resolve().parent.parent / "modules",
        Path.home() / ".xradar-desktop" / "modules",
    ]
    for extra in os.environ.get("XRADAR_MODULES_DIR", "").split(os.pathsep):
        if extra.strip():
            dirs.append(Path(extra.strip()).expanduser())
    return dirs


def discover_modules(extra_dirs: list[Path] | None = None) -> dict[str, ModuleSpec]:
    """(Re)scan module directories. A module that fails to import is skipped and recorded."""
    global _current_source
    MODULES.clear()
    LOAD_ERRORS.clear()
    for directory in [*module_dirs(), *(extra_dirs or [])]:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            _current_source = str(path)
            try:
                spec = importlib.util.spec_from_file_location(f"xradar_module_{path.stem}", path)
                if spec is None or spec.loader is None:
                    raise ImportError("cannot create import spec")
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            except Exception as exc:
                logger.error("Failed to load module %s: %s", path, exc)
                LOAD_ERRORS.append({"file": str(path), "error": f"{type(exc).__name__}: {exc}"})
    _current_source = "<unknown>"
    logger.info("Discovered %d modules (%d load errors)", len(MODULES), len(LOAD_ERRORS))
    return MODULES


def list_modules() -> dict[str, Any]:
    return {
        "modules": sorted(
            (m.describe() for m in MODULES.values()), key=lambda m: (m["category"], m["title"])
        ),
        "errors": list(LOAD_ERRORS),
        "dirs": [str(d) for d in module_dirs()],
    }


DEFAULT_MODULE_TIMEOUT_S = 300.0
# Runs that timed out but whose thread has not returned yet. Threads cannot be killed, so a
# module that ignores ctx.cancelled keeps running; cap how many we tolerate before refusing.
_MAX_ABANDONED_RUNS = 3
_abandoned_runs: list[threading.Thread] = []
_abandoned_lock = threading.Lock()


def module_timeout_s() -> float | None:
    """Wall-clock limit of one module run in seconds (``XRADAR_MODULE_TIMEOUT_S``, default 300).

    ``0`` or a negative value disables the limit; an unparsable value falls back to the default.
    """
    raw = os.environ.get("XRADAR_MODULE_TIMEOUT_S", "").strip()
    if not raw:
        return DEFAULT_MODULE_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MODULE_TIMEOUT_S
    if math.isnan(value):
        return DEFAULT_MODULE_TIMEOUT_S
    if value <= 0 or math.isinf(value):
        return None
    return value


def _live_abandoned() -> int:
    with _abandoned_lock:
        _abandoned_runs[:] = [t for t in _abandoned_runs if t.is_alive()]
        return len(_abandoned_runs)


def _abandon(thread: threading.Thread, cancel: threading.Event) -> None:
    cancel.set()
    with _abandoned_lock:
        _abandoned_runs.append(thread)


def _call_module(
    spec: ModuleSpec,
    ctx: ModuleContext,
    params: dict[str, Any],
    timeout_s: float | None,
    is_cancelled: Callable[[], bool] | None,
) -> Any:
    """Run ``spec.func`` in a dedicated daemon thread and wait for it, at most *timeout_s*.

    A Python thread cannot be killed, so on timeout (or caller cancellation) the cancel flag
    is set -- the module stops at its next ``ctx.progress`` / ``ctx.cancelled`` check -- and the
    caller is released at once; whatever the thread later returns is discarded. Running it in
    its own daemon thread (not the shared executor) keeps a stuck module from exhausting the
    pool or blocking interpreter exit.
    """
    if _live_abandoned() >= _MAX_ABANDONED_RUNS:
        raise ModuleError(
            f"{_MAX_ABANDONED_RUNS} earlier module runs timed out and are still busy in the "
            "background. Restart xradar-desktop to free them, then try again."
        )
    cancel = threading.Event()
    ctx._cancel_event = cancel
    done = threading.Event()
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["result"] = spec.func(ctx, **params)
        except BaseException as exc:  # handed back to (or dropped by) the waiting caller
            box["exc"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=target, name=f"module-{spec.id}", daemon=True)
    thread.start()
    deadline = None if timeout_s is None else time.monotonic() + timeout_s
    while not done.wait(0.1):
        if is_cancelled is not None and is_cancelled():
            _abandon(thread, cancel)
            raise ModuleCancelled()
        if deadline is not None and time.monotonic() >= deadline:
            _abandon(thread, cancel)
            logger.error("Module '%s' timed out after %g s", spec.id, timeout_s)
            raise ModuleTimeoutError(
                f"{spec.title} timed out after {timeout_s:g} s. Try a smaller or coarser input "
                "(another sweep, looser parameters), or raise the limit with the "
                "XRADAR_MODULE_TIMEOUT_S environment variable (seconds)."
            )
    if "exc" in box:
        raise box["exc"]
    return box["result"]


def _friendly_failure(spec: ModuleSpec, sweep: int, exc: BaseException) -> str:
    """Actionable one-line message for an unexpected exception inside a module."""
    kind = type(exc).__name__
    detail = str(exc).strip() or "no details"
    if isinstance(exc, MemoryError):
        hint = "It ran out of memory: close other open files, or choose a smaller sweep."
    elif isinstance(exc, (KeyError, IndexError)):
        hint = (
            f"The data in sweep {sweep} may not have what it expects: try another sweep or "
            "check that the required fields are present."
        )
    elif isinstance(exc, (ValueError, TypeError, ZeroDivisionError, FloatingPointError)):
        hint = (
            f"Check the parameter values and that sweep {sweep} holds valid data (not all "
            "no-echo), then try again."
        )
    elif isinstance(exc, ImportError):
        hint = "A Python package this module needs is missing or broken: reinstall it."
    else:
        hint = "Try another sweep or different parameters; the full traceback is in the log."
    return f"{spec.title} failed ({kind}: {detail}). {hint}"


def _validate_fields(spec: ModuleSpec, ctx: ModuleContext, result: Result) -> list[str]:
    """Check module output arrays against the sweep grid; return non-fatal warnings.

    Raises :class:`ModuleError` for a wrong shape/dtype or a field holding only infinities.
    +/-inf become NaN (no data) so colour scales and statistics stay finite.
    """
    if not result.fields:
        return []
    ds = ctx.dataset
    expected = (int(ds["azimuth"].size), int(ds["range"].size))
    warnings: list[str] = []
    for name, arr in list(result.fields.items()):
        arr = np.asarray(arr)
        if arr.shape != expected:
            raise ModuleError(
                f"Module '{spec.id}' produced field '{name}' with shape {tuple(arr.shape)}, but "
                f"the sweep grid is (n_azimuth, n_range) = {expected}. The module must return one "
                "value per gate; if this is a custom module, check its output shape."
            )
        if arr.dtype.kind not in "fiub":
            raise ModuleError(
                f"{spec.title} produced field '{name}' of non-numeric type {arr.dtype}; "
                "fields must be numbers (NaN = no data)."
            )
        if arr.dtype.kind != "f":
            continue
        finite = np.isfinite(arr)
        if not finite.all():
            inf = np.isinf(arr)
            if inf.any():
                if not (finite | np.isnan(arr)).any():
                    raise ModuleError(
                        f"{spec.title} produced field '{name}' containing only infinite "
                        "values, which usually means a division by zero. Check that the input "
                        "fields have valid data on this sweep and try different parameters."
                    )
                arr = np.where(inf, np.float32(np.nan), arr).astype(arr.dtype, copy=False)
                result.fields[name] = arr
                warnings.append(f"'{name}': infinite values were treated as no data")
            if not np.isfinite(arr).any():
                warnings.append(f"'{name}' has no valid gates on this sweep")
    return warnings


def run_module(
    module_id: str,
    datatree: xr.DataTree,
    sweep: int,
    raw_params: dict[str, Any],
    *,
    get_sweep_dataset: Callable[[int], xr.Dataset],
    progress: Callable[[int, str], None] | None = None,
    return_fields: bool = False,
    timeout_s: float | None | str = "env",
    is_cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any] | tuple[dict[str, Any], dict[str, np.ndarray]]:
    """Execute a module and return its JSON-safe result payload.

    With ``return_fields=True`` returns ``(payload, fields)`` where ``fields`` maps field
    names (see :meth:`Result.field`) to float32 arrays, for the caller to send as binary.

    The run is bounded by *timeout_s* (default :func:`module_timeout_s`; ``None`` = no limit):
    on expiry :class:`ModuleTimeoutError` is raised and the late result is discarded. If
    *is_cancelled* turns true the run is abandoned with :class:`ModuleCancelled`. Every other
    failure is a :class:`ModuleError` whose message says what to try.
    """
    spec = MODULES.get(module_id)
    if spec is None:
        raise KeyError(f"Unknown module '{module_id}'. Available: {sorted(MODULES)}")
    missing = spec.missing_packages()
    if missing:
        raise ModuleError(
            f"{spec.title} needs the Python package {', '.join(missing)} "
            f"(pip install {' '.join(missing)})"
        )
    params = _coerce_params(spec.params, raw_params or {})
    ctx = ModuleContext(datatree, sweep, get_sweep_dataset=get_sweep_dataset, progress=progress)
    for req in spec.requires:
        ctx.field(req)  # raises a clear ModuleError up-front
    limit = module_timeout_s() if timeout_s == "env" else timeout_s
    t0 = time.perf_counter()
    try:
        result = _call_module(spec, ctx, params, limit, is_cancelled)  # type: ignore[arg-type]
    except (ModuleError, ModuleCancelled):
        raise
    except Exception as exc:
        logger.error("Module '%s' crashed:\n%s", module_id, traceback.format_exc())
        raise ModuleError(_friendly_failure(spec, sweep, exc)) from exc
    if not isinstance(result, Result):
        raise ModuleError(
            f"{spec.title} must return a Result, got {type(result).__name__}. "
            "If this is a custom module, end its run() function with `return res`."
        )
    warnings = _validate_fields(spec, ctx, result)
    payload = result.to_dict()
    payload.update(
        module=module_id,
        sweep=sweep,
        params=params,
        elapsed_ms=round((time.perf_counter() - t0) * 1000.0, 1),
    )
    if warnings:
        payload["warnings"] = warnings
    return (payload, result.fields) if return_fields else payload
