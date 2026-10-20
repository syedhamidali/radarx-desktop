"""Public Python API of radarx-desktop: everything the app does, from a script or a notebook.

The desktop app is a thin shell around this engine; this module exposes the same functions
without the WebSocket server, so a workflow you built in the GUI can be repeated unattended.
Every step has the same name here as on the command line (``rxd info`` is :func:`info`,
``rxd run`` is :func:`run`, and ``rxd mosaic -i F1 F2 -o out.nc`` is
``api.mosaic([F1, F2], out="out.nc")``).

Look
    :func:`open` (a file -> :class:`Volume`), :func:`info`, :func:`plot`, ``Volume.field``
Clean
    :func:`pipeline` / ``Volume.pipeline`` (QC recipes), :func:`recipes`, :func:`recipe`
Analyse
    :func:`modules`, :func:`module`, :func:`run` / ``Volume.run``, :func:`cells`, :func:`track`
Combine
    :func:`mosaic` (several radars on one map), :func:`winds` (multi-Doppler)
Hydrology
    :func:`qpe` (rain rate and accumulation with uncertainty, gauge adjustment, basin rainfall)
Save
    :func:`export` / ``Volume.export``; every result has ``.save(...)`` and ``.plot()``

Quick tour::

    from engine import api            # or:  import rxd

    vol = api.open("KTLX20130520_201643_V06.gz")        # -> Volume
    vol.info()                                          # format, site, time, sweeps, nyquist ...
    api.modules()                                       # the plug-in algorithms

    res = vol.run("radarx_hid")                         # one sweep  -> ModuleResult
    vol.run("radarx_hid", all_sweeps=True)              # every sweep -> derived variable
    vol.pipeline("Reflectivity QC", all_sweeps=True)    # a QC recipe -> derived variable
    vol.plot("DBZH").savefig("ppi.png")                 # publication-style figure
    vol.export("netcdf", "out.nc", include_derived=True)

    mos = api.mosaic([f1, f2, f3], out="mosaic.nc")     # multi-radar mosaic
    rain = api.qpe([f1, f2, f3], out="rain.nc")         # rainfall accumulation (docs/HYDROLOGY.md)
    w = api.winds([f1, f2], out="winds.nc")             # multi-Doppler winds
    trk = api.track([t1, t2, t3])                       # storm-cell tracking

One-liners that open the file for you: ``api.plot(FILE)``, ``api.run(MODULE, FILE)``,
``api.pipeline(RECIPE, FILE)``, ``api.cells(FILE)``, ``api.export(FILE, out="x.nc")``.

Conventions
-----------
* Argument names match the command line: ``files`` (``-i``), ``out`` (``-o``), ``plot`` (``-p``),
  ``variable`` (``-V``), ``sweep`` (``-s``), ``params`` / keyword parameters (``-P``).
* Anywhere a volume is expected you may pass a file path or a :class:`Volume`.
* Variable names go through the engine's variable mapping (:mod:`engine.varmap`), so
  ``"velocity"``, ``"VRADH"`` and ``"VEL"`` all find the radial-velocity field.
* ``sweep=None`` means "the lowest sweep that has what is needed" (velocity is on sweep 1 of a
  NEXRAD split-cut volume); give a number to be explicit.
* A :class:`Volume` owns its derived variables; there is no global state between volumes.
* Plots are matplotlib figures drawn with the non-interactive Agg backend (headless safe).
* Problems you can fix (unknown module, bad parameter, missing variable) raise
  :class:`XrdArgumentError`; a missing or unreadable file raises :class:`XrdInputError`; a
  missing optional package :class:`XrdDependencyError`; everything else that goes wrong while
  computing raises :class:`XrdError` (or the engine's own error, see :func:`user_errors`).
"""

from __future__ import annotations

import dataclasses
import difflib
import inspect
import json
import math
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import xarray as xr

from engine import derived, plugins, varmap
from engine.api_results import (
    CellsResult,
    ModuleResult,
    Mosaic,
    PipelineResult,
    TrackingResult,
    Winds,
    XrdArgumentError,
    XrdDependencyError,
    XrdError,
    XrdInputError,
    plot_ppi,
    save_figure,
)

if TYPE_CHECKING:
    import matplotlib.pyplot as plt

    from engine.pipeline import Recipe

__all__ = [
    "EXPORT_FORMATS",
    "CellsResult",
    "ModuleResult",
    "Mosaic",
    "PipelineResult",
    "TrackingResult",
    "Volume",
    "Winds",
    "XrdArgumentError",
    "XrdDependencyError",
    "XrdError",
    "XrdInputError",
    "cells",
    "check_params",
    "coerce_user_params",
    "export",
    "find_module",
    "format_module",
    "info",
    "module",
    "modules",
    "mosaic",
    "open",
    "pipeline",
    "pipeline_steps",
    "plot",
    "qpe",
    "recipe",
    "recipe_script",
    "recipes",
    "reload_modules",
    "run",
    "sounding",
    "track",
    "user_errors",
    "winds",
]

ProgressFn = Callable[[int, str], None]

EXPORT_FORMATS = ("netcdf", "cfradial2", "zarr", "csv", "png", "svg", "pdf")
_FORMAT_ALIASES = {"nc": "netcdf", "cfradial1": "netcdf", "cf1": "netcdf", "cf2": "cfradial2"}
_EXPORT_EXT = {
    "netcdf": ".nc",
    "cfradial2": ".nc",
    "zarr": ".zarr",
    "csv": ".csv",
    "png": ".png",
    "svg": ".svg",
    "pdf": ".pdf",
}


def simplify_reason(text: str) -> str:
    """An engine message about a missing field without its GUI-only wording.

    Drops the list of names that were searched, the hint to select the paired sweep and the
    pointer to the "Variable mapping" panel (the caller adds the command-line fix instead).
    """
    text = text.replace(" You can map a variable to this role under 'Variable mapping'.", "")
    text = re.sub(r"\s*\(searched .*? and name patterns\)", "", text)
    text = re.sub(
        r"\s*Sweep \d+ \(same elevation[^)]*\) has [^:]*: select that sweep[^.]*\.", "", text
    )
    text = re.sub(r"\s*Other sweeps with [^:]*: [^.]*\.", "", text)
    return " ".join(text.split())


def user_errors() -> tuple[type[BaseException], ...]:
    """Exception types whose message is written for the user (the CLI prints them plainly)."""
    from engine import mosaic as mosaic_engine
    from engine import multidoppler as mdop_engine
    from engine.hydro.qpe import QpeError

    return (
        XrdError,
        QpeError,
        plugins.ModuleError,
        derived.DerivedError,
        mosaic_engine.MosaicError,
        mdop_engine.MultiDopplerError,
        varmap.VarMapError,
        FileNotFoundError,
        PermissionError,
        IsADirectoryError,
        NotImplementedError,
        ValueError,
        KeyError,
        IndexError,
        ImportError,
    )


def _message(exc: BaseException) -> str:
    """The text of an exception, without the quotes ``KeyError`` puts around its message."""
    if isinstance(exc, KeyError) and exc.args:
        return str(exc.args[0])
    return str(exc)


def error_message(exc: BaseException) -> str:
    """User-facing text of *exc* (see :func:`user_errors`)."""
    return _message(exc)


# ---------------------------------------------------------------------------
# Module registry
# ---------------------------------------------------------------------------


class ModuleTable(list):
    """List of module description dicts that prints as a table (and renders in Jupyter)."""

    def __repr__(self) -> str:
        if not self:
            return "(no modules)"
        rows = [
            (
                m["id"],
                m["category"],
                m["scope"],
                ", ".join(m["requires"]) or "-",
                ", ".join(p["name"] for p in m["params"][:4])
                + (" ..." if len(m["params"]) > 4 else ""),
                "yes" if m["available"] else "no",
            )
            for m in self
        ]
        head = ("id", "category", "scope", "requires", "parameters", "usable")
        widths = [max(len(str(r[i])) for r in [head, *rows]) for i in range(len(head))]
        lines = ["  ".join(str(c).ljust(w) for c, w in zip(head, widths, strict=True))]
        lines.append("  ".join("-" * w for w in widths))
        lines += ["  ".join(str(c).ljust(w) for c, w in zip(r, widths, strict=True)) for r in rows]
        return "\n".join(lines)

    def _repr_html_(self) -> str:
        head = "".join(f"<th>{h}</th>" for h in ("id", "title", "category", "requires", "scope"))
        body = "".join(
            f"<tr><td><code>{m['id']}</code></td><td>{m['title']}</td><td>{m['category']}</td>"
            f"<td>{', '.join(m['requires']) or '-'}</td><td>{m['scope']}</td></tr>"
            for m in self
        )
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


class ModuleInfo(dict):
    """Description of one module (see :func:`module`); prints its parameters."""

    def __repr__(self) -> str:
        return format_module(self)


def format_module(info: Mapping[str, Any]) -> str:
    """Multi-line text description of a module: purpose, inputs and every parameter."""
    lines = [
        f"{info['id']}  -  {info['title']}",
        f"  category : {info['category']}    scope: {info['scope']}    version: {info['version']}",
    ]
    if info.get("description"):
        lines.append(f"  {info['description']}")
    lines.append(f"  requires : {', '.join(info['requires']) or '-'}")
    if info.get("optional"):
        lines.append(f"  optional : {', '.join(info['optional'])}")
    if info.get("paired"):
        lines.append(
            f"  paired   : {', '.join(info['paired'])} (may be taken from the paired sweep)"
        )
    if info.get("needs"):
        state = "" if info["available"] else "   <-- NOT INSTALLED"
        lines.append(f"  python   : {', '.join(info['needs'])}{state}")
    if not info["available"] and info.get("unavailable_reason"):
        lines.append(f"  note     : {info['unavailable_reason']}")
    params = info.get("params") or []
    if params:
        lines.append("  parameters (CLI: -P name=value; Python: vol.run(..., name=value)):")
        for p in params:
            extra = []
            if "options" in p:
                extra.append("one of " + ", ".join(str(o) for o in p["options"]))
            if "min" in p or "max" in p:
                extra.append(f"range {p.get('min', '-inf')}..{p.get('max', 'inf')}")
            tail = f"  [{'; '.join(extra)}]" if extra else ""
            lines.append(f"    {p['name']:<24} {p['type']:<7} default {p['default']!r}{tail}")
            if p.get("help"):
                lines.append(f"        {p['help']}")
    else:
        lines.append("  parameters: none")
    return "\n".join(lines)


def _ensure_modules() -> dict[str, plugins.ModuleSpec]:
    if not plugins.MODULES:
        plugins.discover_modules()
    return plugins.MODULES


def reload_modules(extra_dirs: Sequence[str | Path] | None = None) -> int:
    """Re-scan the module folders (after editing or adding a module file); returns the count.

    Modules are loaded from ``python/modules`` (shipped), ``~/.xradar-desktop/modules`` and the
    folders in ``$XRADAR_MODULES_DIR``. ``extra_dirs`` adds more for this scan.
    """
    plugins.discover_modules([Path(d).expanduser() for d in extra_dirs or []])
    return len(plugins.MODULES)


def modules(category: str | None = None, *, available_only: bool = False) -> ModuleTable:
    """List the plug-in algorithms.

    Parameters
    ----------
    category : str, optional
        Only modules of this category (case-insensitive), e.g. ``"Dual-pol"``.
    available_only : bool
        Hide modules whose optional Python package (e.g. radarx) is not installed.

    Returns
    -------
    ModuleTable
        A list of dicts (``id``, ``title``, ``description``, ``category``, ``requires``,
        ``optional``, ``params``, ``scope``, ``available`` ...) that prints as a table.

    Examples
    --------
    >>> api.modules("Dual-pol")                  # doctest: +SKIP
    >>> [m["id"] for m in api.modules()]         # doctest: +SKIP
    """
    _ensure_modules()
    listing = plugins.list_modules()["modules"]
    if category:
        listing = [m for m in listing if m["category"].lower() == category.lower()]
        if not listing:
            cats = sorted({m["category"] for m in plugins.list_modules()["modules"]})
            raise XrdArgumentError(
                f"No module category '{category}'. Categories: {', '.join(cats)}"
            )
    if available_only:
        listing = [m for m in listing if m["available"]]
    return ModuleTable(listing)


def find_module(module_id: str) -> plugins.ModuleSpec:
    """The :class:`~engine.plugins.ModuleSpec` of *module_id* (``"hid"`` finds ``radarx_hid``)."""
    mods = _ensure_modules()
    if module_id in mods:
        return mods[module_id]
    for candidate in (f"radarx_{module_id}", module_id.replace("-", "_")):
        if candidate in mods:
            return mods[candidate]
    close = difflib.get_close_matches(module_id, list(mods), n=3, cutoff=0.5)
    hint = f" Did you mean {', '.join(close)}?" if close else ""
    raise XrdArgumentError(
        f"Unknown module '{module_id}'.{hint} List them with api.modules() or `rxd modules`."
    )


def module(module_id: str) -> ModuleInfo:
    """Describe one module: purpose, required fields and every parameter with its default.

    Examples
    --------
    >>> api.module("radarx_hid")          # doctest: +SKIP
    """
    return ModuleInfo(find_module(module_id).describe())


def _parse_bool(value: Any) -> bool:
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"1", "true", "yes", "y", "on"}:
            return True
        if low in {"0", "false", "no", "n", "off"}:
            return False
        raise ValueError(f"expected true/false, got '{value}'")
    return bool(value)


def coerce_user_params(
    specs: Sequence[Mapping[str, Any]], raw: Mapping[str, Any], label: str
) -> dict[str, Any]:
    """Validate user parameters against parameter declarations; strings are converted.

    Unknown names, wrong types and values outside a ``select`` raise
    :class:`XrdArgumentError` naming the parameter and the valid choices. Returns only the
    parameters the caller gave (the engine fills in defaults). *label* names the owner in
    messages (``"Module 'radarx_hid'"``).
    """
    known = {p["name"]: p for p in specs}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        p = known.get(key)
        if p is None:
            close = difflib.get_close_matches(key, list(known), n=2, cutoff=0.5)
            hint = f" Did you mean {' or '.join(repr(c) for c in close)}?" if close else ""
            valid = ", ".join(known) or "none"
            raise XrdArgumentError(f"{label} has no parameter '{key}'.{hint} Parameters: {valid}.")
        try:
            if p["type"] == "bool":
                value = _parse_bool(value)
            elif p["type"] in ("number", "int"):
                if isinstance(value, bool):
                    raise ValueError("expected a number")
                value = float(value)
                if not math.isfinite(value):
                    raise ValueError("must be finite")
            elif p["type"] == "select":
                options = p["options"]
                if value not in options:
                    match = [o for o in options if str(o) == str(value)]
                    if not match:
                        raise ValueError(f"must be one of {', '.join(str(o) for o in options)}")
                    value = match[0]
        except (TypeError, ValueError) as exc:
            raise XrdArgumentError(f"{label}: parameter '{key}': {exc} (got {raw[key]!r})") from exc
        out[key] = value
    return out


def check_params(spec: plugins.ModuleSpec, raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a module's user parameters (see :func:`coerce_user_params`)."""
    return coerce_user_params(spec.params, raw, f"Module '{spec.id}'")


# ---------------------------------------------------------------------------
# Recipes (pipelines)
# ---------------------------------------------------------------------------


class RecipeTable(list):
    """List of recipe dicts that prints as a table."""

    def __repr__(self) -> str:
        rows = [
            (
                r["name"],
                r["target"],
                "built-in" if r.get("builtin") else "saved",
                " > ".join(s["step_id"] for s in r["steps"] if s.get("enabled", True)),
            )
            for r in self
        ]
        head = ("name", "target", "kind", "steps")
        widths = [max(len(str(x[i])) for x in [head, *rows]) for i in range(4)]
        lines = ["  ".join(str(c).ljust(w) for c, w in zip(head, widths, strict=True))]
        lines.append("  ".join("-" * w for w in widths))
        lines += ["  ".join(str(c).ljust(w) for c, w in zip(r, widths, strict=True)) for r in rows]
        return "\n".join(lines)


def recipes() -> RecipeTable:
    """The built-in recipes plus the ones saved from the app (``~/.xradar-desktop/recipes``).

    Examples
    --------
    >>> api.recipes()          # doctest: +SKIP
    """
    from engine import pipeline

    user, _errors = pipeline.load_user_recipes()
    return RecipeTable(r.to_dict() for r in [*pipeline.builtin_recipes(), *user])


def pipeline_steps() -> list[dict[str, Any]]:
    """The building blocks recipes are made of (id, title, parameters, needed fields)."""
    from engine import pipeline

    return pipeline.list_steps()


def recipe(spec: Any) -> Recipe:
    """Resolve a recipe from a name, a JSON file path, a dict or a :class:`Recipe`.

    A name matches (case-insensitively) a built-in or saved recipe by its full name, its slug
    or a unique part of it, so ``"reflectivity qc"`` finds *Reflectivity QC*.

    Examples
    --------
    >>> api.recipe("dual-pol")                          # doctest: +SKIP
    >>> api.recipe("my_recipe.json")                    # doctest: +SKIP
    """
    from engine import pipeline

    if isinstance(spec, pipeline.Recipe):
        return spec
    if isinstance(spec, Mapping):
        return _parse_recipe(dict(spec))
    if isinstance(spec, os.PathLike) or (
        isinstance(spec, str)
        and (spec.lower().endswith(".json") or os.sep in spec or Path(spec).is_file())
    ):
        path = Path(spec).expanduser()
        if not path.is_file():
            raise XrdArgumentError(
                f"Recipe file not found: {path}. Give a recipe name (see `rxd pipeline list`) "
                "or the path of a JSON file saved from the app."
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise XrdArgumentError(f"Cannot read recipe file {path}: {exc}") from exc
        return _parse_recipe(raw)
    if not isinstance(spec, str):
        raise XrdArgumentError("A recipe is a name, a JSON file path, a dict or a Recipe")
    user, _errors = pipeline.load_user_recipes()
    pool = [*pipeline.builtin_recipes(), *user]
    key = spec.strip().lower()
    for finder in (
        lambda r: r.name.lower() == key,
        lambda r: r.slug == derived.slug(spec)[:40].strip("_"),
        lambda r: key in r.name.lower() or key.replace("-", "_") in r.slug,
    ):
        hits = [r for r in pool if finder(r)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            names = ", ".join(repr(r.name) for r in hits)
            raise XrdArgumentError(f"Recipe '{spec}' is ambiguous: {names}. Use the full name.")
    names = ", ".join(repr(r.name) for r in pool)
    raise XrdArgumentError(
        f"Unknown recipe '{spec}'. Known recipes: {names}. You can also give the path of a "
        "recipe JSON file."
    )


def _parse_recipe(raw: Any) -> Recipe:
    from engine import pipeline

    try:
        return pipeline.parse_recipe(raw, strict_name=False)
    except plugins.ModuleError as exc:
        raise XrdArgumentError(f"Invalid recipe: {exc}") from exc


def recipe_script(spec: Any) -> str:
    """A standalone Python script (numpy / scipy / xarray) that applies the recipe to a file."""
    from engine import pipeline

    return pipeline.recipe_to_script(recipe(spec))


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------


def _iso(value: Any) -> str | None:
    try:
        arr = np.ravel(np.asarray(value)).astype("datetime64[ns]")
        arr = arr[~np.isnat(arr)]
        if arr.size == 0:
            return None
        return str(np.datetime_as_string(arr.min().astype("datetime64[s]"), unit="s")) + "Z"
    except (TypeError, ValueError):
        return None


class Volume:
    """One opened radar file (a full volume scan) with its own derived variables.

    Create it with :func:`open`. All methods that compute something accept variable names in
    any spelling the variable mapping knows (``"velocity"``, ``"VRADH"``, ``"VEL"`` ...).

    Parameters
    ----------
    reader : engine.reader.RadarReader
        An opened reader (use :func:`open` / :meth:`from_tree` instead of calling this).
    mapping : dict, optional
        ``{role: variable}`` overrides for the variable mapping, e.g. ``{"velocity": "V"}``.

    Attributes
    ----------
    tree : xarray.DataTree
        The volume as xradar opened it (one ``sweep_N`` node per sweep).
    path : str or None
        File the volume was read from.
    format : str or None
        Detected format (``nexrad_level2``, ``cfradial1``, ``odim`` ...).
    sweeps : list[dict]
        ``{"index", "elevation", "variables"}`` per sweep.

    Examples
    --------
    >>> from engine import api
    >>> vol = api.open("KTLX20130520_201643_V06.gz")          # doctest: +SKIP
    >>> vol.sweeps[0]["elevation"]                            # doctest: +SKIP
    0.4833984375
    >>> vol.field("velocity", sweep=1)                        # doctest: +SKIP
    """

    def __init__(self, reader: Any, *, mapping: Mapping[str, str] | None = None) -> None:
        self._reader = reader
        self.mapping: dict[str, str] = varmap.normalize_overrides(mapping)

    # -- construction --------------------------------------------------------------------

    @classmethod
    def from_tree(
        cls,
        tree: xr.DataTree,
        *,
        path: str | None = None,
        format: str = "datatree",  # noqa: A002 - mirrors the attribute name
        mapping: Mapping[str, str] | None = None,
    ) -> Volume:
        """Wrap an in-memory ``xarray.DataTree`` (for example one you built or processed)."""
        from engine.reader import RadarReader

        reader = RadarReader()
        reader._commit(tree, path or "", format)
        return cls(reader, mapping=mapping)

    # -- basic properties ----------------------------------------------------------------

    @property
    def tree(self) -> xr.DataTree:
        tree = self._reader.datatree
        if tree is None:
            raise XrdError("This volume has been closed")
        return tree

    @property
    def path(self) -> str | None:
        return self._reader._path or None

    @property
    def format(self) -> str | None:
        return self._reader._format

    @property
    def name(self) -> str:
        """Short label: the radar's instrument name, else the file name."""
        attrs = {**self.tree.attrs, **self.tree.to_dataset().attrs}
        inst = attrs.get("instrument_name")
        if inst and str(inst) != "None":
            return str(inst)
        return Path(self.path).name.split(".")[0] if self.path else "radar"

    @property
    def sweeps(self) -> list[dict[str, Any]]:
        return list(self._reader.get_schema()["sweeps"])

    @property
    def n_sweeps(self) -> int:
        from engine.reader import sorted_sweep_names

        return len(sorted_sweep_names(self.tree))

    def __repr__(self) -> str:
        if self._reader.datatree is None:
            return "<Volume (closed)>"
        label = Path(self.path).name if self.path else self.name
        n_derived = len(self.derived)
        return f"<Volume {label} | {self.format} | {self.n_sweeps} sweeps | {n_derived} derived>"

    def __enter__(self) -> Volume:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the data (derived variables are dropped with it)."""
        store = derived.store_for(self._reader, create=False)
        if store is not None:
            store.drop()
        self._reader.close()

    # -- variables -----------------------------------------------------------------------

    def variables(self, sweep: int | None = None, *, include_derived: bool = True) -> list[str]:
        """Names of the radar moments (all sweeps, or one sweep), plus derived variables."""
        schema = self._reader.get_schema()
        if sweep is None:
            names = list(schema["variables"])
        else:
            self._check_sweep(sweep)
            names = list(schema["sweeps"][sweep].get("variables") or [])
        if include_derived:
            store = self._store
            if sweep is None:
                names += [v["name"] for v in store.variables()]
            else:
                names += store.names_on_sweep(sweep)
        return names

    def _check_sweep(self, sweep: int) -> int:
        n = self.n_sweeps
        if isinstance(sweep, bool) or not isinstance(sweep, (int, np.integer)):
            raise XrdArgumentError(f"sweep must be an integer, got {sweep!r}")
        if sweep < 0 or sweep >= n:
            raise XrdArgumentError(
                f"sweep {sweep} is out of range: this volume has sweeps 0..{n - 1}"
            )
        return int(sweep)

    def dataset(self, sweep: int = 0, *, mask: bool = True) -> xr.Dataset:
        """The full ``xarray.Dataset`` of one sweep (no-echo codes are NaN when ``mask=True``)."""
        return self._reader.get_sweep_dataset(self._check_sweep(sweep), mask=mask)

    def resolve(self, variable: str, sweep: int = 0) -> str:
        """The name a variable has in the file (or the derived-variable name) on *sweep*."""
        sweep = self._check_sweep(sweep)
        if self._store.has(variable, sweep):
            return variable
        ds = self._reader.get_sweep_dataset(sweep, mask=False)
        match = varmap.resolve(ds, variable, overrides=self.mapping)
        if match is not None:
            return match.variable
        raise XrdArgumentError(self._missing_message(variable, sweep, ds))

    def _missing_message(self, variable: str, sweep: int, ds: xr.Dataset) -> str:
        if self._store.has(variable):
            return (
                f"Derived variable '{variable}' was not computed on sweep {sweep} "
                f"(it exists on sweeps {self.derived_meta(variable)['sweeps']})."
            )
        try:
            text = varmap.explain_missing(
                variable, ds, sweep=sweep, tree=self.tree, overrides=self.mapping
            )
        except Exception:  # an explanation must never hide the real error
            text = f"No variable '{variable}' on sweep {sweep}."
        derived_here = self._store.names_on_sweep(sweep)
        if derived_here:
            text += f" Derived variables here: {', '.join(derived_here)}."
        text = simplify_reason(text)  # our own sentence below gives the fix
        elsewhere = self.sweeps_with([variable])
        if elsewhere:
            first = elsewhere[0]
            more = f" (+{len(elsewhere) - 8} more)" if len(elsewhere) > 8 else ""
            shown = ", ".join(str(i) for i in elsewhere[:8]) + more
            text += (
                f" Sweeps that have it: {shown}. Use sweep={first} in Python or -s {first} on the "
                "command line."
            )
        return text

    def sweeps_with(self, variables: Sequence[str]) -> list[int]:
        """Indices of the sweeps on which every variable in *variables* can be found."""
        found = []
        for index in range(self.n_sweeps):
            ds = self._reader.get_sweep_dataset(index, mask=False)
            if all(
                self._store.has(v, index) or varmap.resolve(ds, v, overrides=self.mapping)
                for v in variables
            ):
                found.append(index)
        return found

    def default_sweep(self, variables: Sequence[str] = ()) -> int:
        """The sweep a command should use when you give none: the lowest sweep that has all of
        *variables* (e.g. velocity lives on sweep 1 of a NEXRAD split-cut volume); 0 if none do.
        """
        wanted = [v for v in variables if v]
        if not wanted:
            return 0
        found = self.sweeps_with(wanted)
        return found[0] if found else 0

    def field(self, variable: str, sweep: int = 0, *, mask: bool = True) -> xr.DataArray:
        """One variable of one sweep as an ``xarray.DataArray`` ``(azimuth, range)``.

        Works for file variables (any name the variable mapping knows) and for derived
        variables created by :meth:`run` / :meth:`pipeline`.

        Examples
        --------
        >>> vol.field("reflectivity", sweep=0).max()          # doctest: +SKIP
        >>> vol.field("radarx_hid:hydrometeor_class", 2)      # doctest: +SKIP
        """
        sweep = self._check_sweep(sweep)
        name = self.resolve(variable, sweep)
        if self._store.has(name, sweep):
            got = self._store.get(sweep, name)
            assert got is not None
            values, meta = got
            ds = self._reader.get_sweep_dataset(sweep, mask=False)
            return xr.DataArray(
                values,
                dims=("azimuth", "range"),
                coords={"azimuth": ds["azimuth"].values, "range": ds["range"].values},
                name=name,
                attrs={"units": meta.get("units", ""), "long_name": meta.get("label", name)},
            )
        return (
            self._reader.get_sweep_data(sweep, name)
            if mask
            else self.dataset(sweep, mask=False)[name]
        )

    # -- derived variables ---------------------------------------------------------------

    @property
    def _store(self) -> derived.DerivedStore:
        store = derived.store_for(self._reader)
        assert store is not None
        return store

    @property
    def derived(self) -> list[dict[str, Any]]:
        """Derived variables of this volume: ``name``, ``label``, ``units``, ``sweeps`` ..."""
        if self._reader.datatree is None:
            return []
        return self._store.variables()

    def derived_meta(self, name: str) -> dict[str, Any]:
        for entry in self._store.variables():
            if entry["name"] == name:
                return entry
        raise XrdArgumentError(
            f"No derived variable '{name}'. Derived: {[v['name'] for v in self.derived]}"
        )

    def drop_derived(self, name: str | None = None) -> list[str]:
        """Forget one derived variable (or all); returns the names removed."""
        return self._store.drop(name)

    def add_derived(
        self,
        name: str,
        data: Mapping[int, Any] | Any,
        *,
        units: str = "",
        label: str | None = None,
        vmin: float | None = None,
        vmax: float | None = None,
        cmap: str | None = None,
        categories: list[dict[str, Any]] | None = None,
    ) -> str:
        """Register your own array(s) as a derived variable of this volume.

        Parameters
        ----------
        name : str
            ``"group:name"``; ``"user:"`` is prepended when there is no colon.
        data : dict or array
            ``{sweep: 2-D array}`` or a single ``(azimuth, range)`` array for sweep 0.
        units, label, vmin, vmax, cmap, categories
            Display metadata used by :meth:`plot` and kept in exports.

        Returns
        -------
        str
            The derived-variable name.

        Examples
        --------
        >>> vol.add_derived("user:plus5", {0: vol.field("DBZH", 0) + 5})  # doctest: +SKIP
        """
        if ":" not in name:
            name = f"user:{name}"
        arrays = (
            {int(k): np.asarray(getattr(v, "values", v)) for k, v in data.items()}
            if isinstance(data, Mapping)
            else {0: np.asarray(getattr(data, "values", data))}
        )
        for sweep, arr in arrays.items():
            ds = self._reader.get_sweep_dataset(self._check_sweep(sweep), mask=False)
            if arr.shape != (ds.sizes["azimuth"], ds.sizes["range"]):
                raise XrdArgumentError(
                    f"Array for sweep {sweep} has shape {arr.shape}; the sweep grid is "
                    f"(azimuth, range) = {(ds.sizes['azimuth'], ds.sizes['range'])}"
                )
        finite = np.concatenate([a[np.isfinite(a)].ravel() for a in arrays.values()])
        if vmin is None:
            vmin = float(np.percentile(finite, 2)) if finite.size else 0.0
        if vmax is None:
            vmax = float(np.percentile(finite, 98)) if finite.size else 1.0
        meta = {
            "label": label or name,
            "units": units,
            "vmin": vmin,
            "vmax": vmax if vmax > vmin else vmin + 1.0,
            "cmap": cmap,
            "categories": categories,
            "module": "user",
            "module_title": "User",
            "field": name.split(":", 1)[1],
            "params": {},
            "computed_at": None,
        }
        try:
            self._store.put_variable(name, arrays, meta)
        except derived.DerivedError as exc:
            raise XrdArgumentError(str(exc)) from exc
        return name

    # -- info ----------------------------------------------------------------------------

    def info(self) -> dict[str, Any]:
        """Everything worth knowing about the file as a plain (JSON-able) dict.

        Keys: ``path``, ``name``, ``format``, ``size_bytes``, ``memory_bytes``, ``radar``,
        ``latitude``, ``longitude``, ``altitude_m``, ``time``, ``n_sweeps``, ``variables``,
        ``sweeps`` (index, elevation, mode, rays, gates, max range, nyquist, variables),
        ``derived``.

        Examples
        --------
        >>> vol.info()["sweeps"][1]["nyquist_ms"]          # doctest: +SKIP
        """
        from engine import cells as cell_engine
        from engine.multidoppler import _nyquist

        tree = self.tree
        schema = self._reader.get_schema()
        lat, lon = cell_engine.site_lat_lon(tree)
        attrs = schema["attributes"]
        alt = None
        root = tree.to_dataset()
        if "altitude" in root.variables:
            try:
                alt = float(np.ravel(root["altitude"].values)[0])
            except (TypeError, ValueError, IndexError):
                alt = None
        sweeps = []
        start = None
        for entry in schema["sweeps"]:
            ds = self._reader.get_sweep_dataset(entry["index"], mask=False)
            t0 = _iso(ds["time"].values) if "time" in ds.variables else None
            start = start or t0
            mode = ""
            if "sweep_mode" in ds.variables:
                mode = str(np.ravel(ds["sweep_mode"].values)[0])
            rng = ds["range"].values if "range" in ds.variables else np.array([])
            sweeps.append(
                {
                    "index": entry["index"],
                    "elevation": entry["elevation"],
                    "mode": mode,
                    "time": t0,
                    "n_rays": int(ds["azimuth"].size) if "azimuth" in ds.variables else 0,
                    "n_gates": int(rng.size),
                    "max_range_km": round(float(np.nanmax(rng)) / 1000.0, 1) if rng.size else None,
                    "nyquist_ms": _nyquist(ds),
                    "nyquist_estimated_ms": self._estimate_nyquist(entry["index"], ds),
                    "variables": entry.get("variables", []),
                }
            )
        size = None
        if self.path and os.path.isfile(self.path):
            size = os.path.getsize(self.path)
        return {
            "path": self.path,
            "name": self.name,
            "format": self.format,
            "size_bytes": size,
            "memory_bytes": int(self._reader.memory_bytes()),
            "radar": self.name,
            "latitude": lat if lat is not None else _num(attrs.get("latitude")),
            "longitude": lon if lon is not None else _num(attrs.get("longitude")),
            "altitude_m": alt,
            "time": start,
            "n_sweeps": len(sweeps),
            "variables": list(schema["variables"]),
            "sweeps": sweeps,
            "derived": [v["name"] for v in self.derived],
        }

    def _estimate_nyquist(self, index: int, ds: xr.Dataset) -> float | None:
        """Largest valid |velocity| of the sweep (rounded up to 0.5 m/s) when the file has no
        Nyquist velocity; ``None`` without a velocity field. An estimate: it equals the Nyquist
        velocity for aliased data and under-estimates it otherwise."""
        from engine.multidoppler import _nyquist

        if _nyquist(ds) is not None:
            return None
        match = varmap.resolve(ds, "VRADH", overrides=self.mapping)
        if match is None:
            return None
        try:
            vel = np.abs(np.asarray(self._reader.get_sweep_data(index, match.variable).values))
        except Exception:  # info must never fail over a statistic
            return None
        vel = vel[np.isfinite(vel)]
        if vel.size < 100 or float(vel.max()) < 5.0:
            return None
        return math.ceil(float(vel.max()) * 2.0) / 2.0

    # -- modules -------------------------------------------------------------------------

    def run(
        self,
        module: str,
        sweep: int | None = None,
        *,
        all_sweeps: bool = False,
        params: Mapping[str, Any] | None = None,
        inputs: Mapping[str, str] | None = None,
        out: str | os.PathLike[str] | Sequence[str | os.PathLike[str]] | None = None,
        formats: Sequence[str] | None = None,
        plot: str | os.PathLike[str] | None = None,
        progress: ProgressFn | None = None,
        timeout_s: float | str | None = "env",
        **module_params: Any,
    ) -> ModuleResult:
        """Run a plug-in module (see :func:`modules`) on one sweep or on all sweeps.

        Parameters
        ----------
        module : str
            Module id, e.g. ``"radarx_hid"`` (``"hid"`` also works for ``radarx_*`` modules).
        sweep : int, optional
            Sweep index (ignored when ``all_sweeps=True``). Default: the lowest sweep that has
            every field the module needs (see :meth:`default_sweep`).
        all_sweeps : bool
            Run on every sweep and store the result(s) as ONE derived variable per field
            (``vol.derived``); sweeps lacking a required field are skipped with a reason.
        params : dict, optional
            Module parameters as a dict (alternative to keyword arguments).
        inputs : dict, optional
            ``{role: derived variable}``: let the module read e.g. the velocity from a pipeline
            result: ``inputs={"velocity": "pipeline:doppler_velocity_..._vradh"}``.
        out : path or list of paths, optional
            Also save the result (CLI: ``-o``): a file (``.json`` summary, ``.csv`` table,
            ``.nc`` NetCDF, ``.png`` figure) or a folder (json, csv and nc); see
            :meth:`ModuleResult.save`.
        formats : list of str, optional
            With a folder ``out``: which of ``json``, ``csv``, ``nc``, ``png`` to write.
        plot : path, optional
            Also save a figure of the first field (CLI: ``-p``).
        progress : callable, optional
            ``progress(percent, message)``.
        timeout_s : float or None
            Wall-clock limit (default: ``$XRADAR_MODULE_TIMEOUT_S`` or 300 s; ``None`` = none).
        **module_params
            The module's parameters, see ``api.module(id)``. Strings are converted
            (``"true"`` for bools, ``"3.5"`` for numbers).

        Returns
        -------
        ModuleResult
            Tables as DataFrames, fields as DataArrays, scalars, text and overlays.

        Raises
        ------
        XrdArgumentError
            Unknown module, unknown parameter, bad value, sweep out of range.
        engine.plugins.ModuleError
            The module could not run on this data (message says what is missing).

        Examples
        --------
        >>> res = vol.run("radarx_hid", sweep=0, method="linear")      # doctest: +SKIP
        >>> res.fields                                                 # doctest: +SKIP
        >>> vol.run("radarx_hid", all_sweeps=True).derived             # doctest: +SKIP
        """
        spec = find_module(module)
        merged = {**(params or {}), **module_params}
        coerced = check_params(spec, merged)
        missing = spec.missing_packages()
        if missing:
            raise XrdDependencyError(
                f"{spec.title} needs the Python package {', '.join(missing)}: "
                f"pip install -U {' '.join(missing)}  (radarx needs Python 3.12 or newer)"
            )
        tree = self.tree
        overrides = self._input_overrides(inputs)
        reader = self._reader
        if all_sweeps:
            return self._finish(
                self._run_all(spec, coerced, overrides, progress), out, formats, plot
            )
        sweep = self._check_sweep(self.default_sweep(spec.requires) if sweep is None else sweep)
        if not inputs:
            self._require_fields(spec, sweep)
        payload, fields = plugins.run_module(
            spec.id,
            tree,
            sweep,
            coerced,
            get_sweep_dataset=lambda i: reader.get_sweep_dataset(i),
            progress=progress,
            return_fields=True,
            timeout_s=timeout_s,
            var_overrides=self.mapping,
            overrides=overrides,
        )
        ds = reader.get_sweep_dataset(sweep, mask=False)
        blocks = payload.get("blocks", [])
        by_name = {b.get("name"): b for b in blocks if b.get("type") == "field"}
        arrays: dict[str, xr.DataArray] = {}
        for fname, arr in fields.items():
            block = by_name.get(fname, {})
            attrs: dict[str, Any] = {
                "units": block.get("units") or "",
                "long_name": block.get("title") or fname,
                "sweep": sweep,
            }
            cats = block.get("categories")
            if cats:
                attrs["flag_values"] = np.array([int(c["value"]) for c in cats], dtype=np.int32)
                attrs["flag_meanings"] = " ".join("_".join(str(c["label"]).split()) for c in cats)
            arrays[fname] = xr.DataArray(
                np.asarray(arr, dtype=np.float32),
                dims=("azimuth", "range"),
                coords={"azimuth": ds["azimuth"].values, "range": ds["range"].values},
                name=fname,
                attrs=attrs,
            )
        result = ModuleResult(
            module=spec.id,
            title=spec.title,
            sweep=sweep,
            params=payload.get("params", coerced),
            blocks=blocks,
            fields=arrays,
            elapsed_ms=float(payload.get("elapsed_ms", 0.0)),
            notes=payload.get("notes", []),
            warnings=payload.get("warnings", []),
            volume=self,
        )
        return self._finish(result, out, formats, plot)

    def _finish(self, result: ModuleResult, out: Any, formats: Any, plot: Any) -> ModuleResult:
        """Save the result as ``out`` / ``plot`` asked for."""
        notes: list[str] = []
        if out is not None:
            result.save(out, formats)
            notes += result.save_notes
        if plot is not None:
            if result.fields or result.derived:
                save_figure(result.plot(), plot)
            else:
                notes.append(f"{result.module} produced no gridded field; no figure written")
        result.save_notes = notes
        return result

    def _require_fields(self, spec: plugins.ModuleSpec, sweep: int) -> None:
        """Fail early, with the fix, when the module's required fields are not on *sweep*."""
        paired = set()
        for name in spec.paired:
            role = varmap.role_for_name(name)
            paired.add(role.key if role else name)
        ds = self._reader.get_sweep_dataset(sweep, mask=False)
        absent = []
        for name in spec.requires:
            role = varmap.role_for_name(name)
            if (role.key if role else name) in paired:
                continue  # the module may take it from the paired sweep
            if varmap.resolve(ds, name, overrides=self.mapping) is None:
                absent.append(name)
        if not absent:
            return
        elsewhere = self.sweeps_with(spec.requires)
        have = ", ".join(self.variables(sweep, include_derived=False))
        text = (
            f"{spec.title} needs {', '.join(spec.requires)}, but sweep {sweep} has no "
            f"{', '.join(absent)} (it has: {have})."
        )
        if elsewhere:
            text += (
                f" Sweeps that have all of them: {', '.join(map(str, elsewhere[:8]))}. "
                f"Use sweep={elsewhere[0]} in Python or -s {elsewhere[0]} on the command line "
                "(without a sweep the right one is chosen for you)."
            )
        else:
            text += (
                " No single sweep of this volume has all of them; use --all-sweeps if the module "
                "supports it, or map your variable names with Volume(mapping={...})."
            )
        raise XrdArgumentError(text)

    def _input_overrides(self, inputs: Mapping[str, str] | None) -> Any:
        from engine import pipeline

        if not inputs:
            return None
        try:
            return pipeline.module_overrides(self._store, dict(inputs))
        except plugins.ModuleError as exc:
            raise XrdArgumentError(
                f"{exc}. Derived variables: {[v['name'] for v in self.derived] or 'none yet'}"
            ) from exc

    def _moments(self, index: int) -> set[str]:
        data_vars = self._reader.get_sweep_dataset(index, mask=False).data_vars
        return {str(k) for k, v in data_vars.items() if v.ndim >= 2}

    def _run_all(
        self,
        spec: plugins.ModuleSpec,
        params: dict[str, Any],
        overrides: Any,
        progress: ProgressFn | None,
    ) -> ModuleResult:
        if not spec.per_sweep:
            raise plugins.ModuleError(
                f"{spec.title} computes a whole-volume product and cannot be run on all sweeps; "
                "run it on one sweep instead"
            )
        reader = self._reader

        def grid(i: int) -> tuple[int, int]:
            ds = reader.get_sweep_dataset(i, mask=False)
            return int(ds["azimuth"].size), int(ds["range"].size)

        run = derived.run_all_sweeps(
            spec.id,
            self.tree,
            self.n_sweeps,
            params,
            get_sweep_dataset=lambda i: reader.get_sweep_dataset(i),
            get_sweep_variables=self._moments,
            get_sweep_grid=grid,
            progress=progress,
            var_overrides=self.mapping,
            overrides=overrides,
        )
        built = derived.build_variables(run)
        store = self._store
        try:
            for name, arrays, meta in built:
                store.put_variable(name, arrays, meta)
        except derived.DerivedError as exc:
            raise plugins.ModuleError(str(exc)) from exc
        names = [name for name, _, _ in built if store.has(name)]
        return ModuleResult(
            module=spec.id,
            title=spec.title,
            sweep=None,
            params=run.params,
            blocks=[],
            fields={},
            elapsed_ms=run.elapsed_ms,
            derived=names,
            skipped=run.skipped,
            summaries=run.summaries,
            volume=self,
        )

    # -- pipelines -----------------------------------------------------------------------

    def pipeline(
        self,
        recipe: Any,
        target: str | None = None,
        *,
        sweep: int | None = None,
        all_sweeps: bool | None = None,
        params: Mapping[str, Any] | None = None,
        out: str | os.PathLike[str] | Sequence[str | os.PathLike[str]] | None = None,
        formats: Sequence[str] | None = None,
        plot: str | os.PathLike[str] | None = None,
        plot_sweep: int | None = None,
        progress: ProgressFn | None = None,
    ) -> PipelineResult:
        """Apply a QC recipe (ordered steps such as SNR gate, clutter filter, de-aliasing).

        The result is stored as ONE derived variable (``vol.derived``); modules can then read
        it through ``inputs=`` and exports can include it.

        Parameters
        ----------
        recipe : str, dict, Path or Recipe
            A built-in or saved recipe name (``api.recipes()``), the path of a recipe JSON
            file, or a recipe dict ``{"name", "target", "steps": [{"step_id", "params"}]}``.
        target : str, optional
            Variable to process (overrides the recipe's own target), e.g. ``"VRADH"``.
        sweep : int, optional
            Process only this sweep.
        all_sweeps : bool, optional
            Process every sweep (default when ``sweep`` is not given).
        params : dict, optional
            Change step settings: ``{"snr_gate.min_snr": 5, "gap_fill": True}``
            (``"step.parameter"`` sets a parameter, a bare ``"step"`` enables / disables it;
            CLI: ``--param``).
        out : path or list of paths, optional
            Also save (CLI: ``-o``): ``.nc`` (volume with the result), ``.json`` (statistics),
            or a folder; see :meth:`PipelineResult.save`.
        formats : list of str, optional
            With a folder ``out``: which of ``nc``, ``json``, ``png`` to write.
        plot : path, optional
            Also save a before / after figure (CLI: ``-p``) of ``plot_sweep`` (default: the
            first processed sweep).
        progress : callable, optional
            ``progress(percent, message)``.

        Returns
        -------
        PipelineResult

        Examples
        --------
        >>> res = vol.pipeline("Doppler velocity: dealias + clutter + bio filter",
        ...                    target="VRADH", all_sweeps=True)           # doctest: +SKIP
        >>> res.steps                                                      # doctest: +SKIP
        >>> vol.run("radarx_shear", all_sweeps=True,
        ...         inputs={"velocity": res.name})                # doctest: +SKIP
        """
        from engine import pipeline as pipe

        rec = _resolve_recipe_arg(recipe, target, params)
        if sweep is not None and all_sweeps:
            raise XrdArgumentError("Give either sweep=N or all_sweeps=True, not both")
        sweeps = None
        if sweep is not None:
            sweeps = [self._check_sweep(sweep)]
        reader = self._reader
        try:
            run = pipe.run_recipe_all_sweeps(
                rec,
                self.tree,
                self.n_sweeps,
                get_sweep_dataset=lambda i: reader.get_sweep_dataset(i),
                get_sweep_variables=self._moments,
                progress=progress,
                sweeps=sweeps,
            )
        except plugins.ModuleError as exc:
            raise plugins.ModuleError(
                f"{exc}. Check the target variable (--target) and the sweeps with `rxd info`."
            ) from exc
        built = pipe.build_variables(run)
        store = self._store
        try:
            for name, arrays, meta in built:
                store.put_variable(name, arrays, meta)
        except derived.DerivedError as exc:
            raise plugins.ModuleError(str(exc)) from exc
        primary = built[0] if built else None
        result = PipelineResult(
            recipe=rec.to_dict(),
            name=primary[0] if primary else None,
            derived=[n for n, _, _ in built if store.has(n)],
            sweeps=sorted(primary[1]) if primary else [],
            skipped=run.skipped,
            step_rows=run.steps,
            per_sweep=run.per_sweep,
            elapsed_ms=run.elapsed_ms,
            volume=self,
        )
        if out is not None:
            result.save(out, formats, sweep=plot_sweep)
        if plot is not None:
            save_figure(result.plot(plot_sweep), plot)
        return result

    # -- other analyses ------------------------------------------------------------------

    def cells(
        self,
        sweep: int | None = None,
        variable: str = "DBZH",
        threshold_dbz: float = 35.0,
        min_size_km2: float = 10.0,
        *,
        backend: str = "auto",
        thresholds: Sequence[float] | None = None,
        grid_spacing_km: float | None = None,
        smooth: float | None = None,
        out: str | os.PathLike[str] | Sequence[str | os.PathLike[str]] | None = None,
        plot: str | os.PathLike[str] | None = None,
    ) -> CellsResult:
        """Identify storm cells on one sweep (radarx backend if installed, else numpy).

        Parameters
        ----------
        sweep : int, optional
            Default: the lowest sweep that has *variable*.
        variable : str
            Reflectivity field (any name the variable mapping knows).
        threshold_dbz, min_size_km2 : float
            Gates at or above the threshold form a cell; smaller cells are dropped.
        backend : {"auto", "radarx", "numpy"}
        thresholds : list of float, optional
            radarx only: ascending thresholds that split cells with several cores.
        grid_spacing_km, smooth : float, optional
            radarx only: labelling grid spacing and Gaussian smoothing.
        out : path or list of paths, optional
            Also save: ``.csv`` (cell table), ``.json``, ``.png`` (figure).
        plot : path, optional
            Also save the figure with the cell outlines (CLI: ``-p``).

        Returns
        -------
        CellsResult
            Sequence of cells with ``.to_dataframe()`` and ``.plot()``.

        Examples
        --------
        >>> vol.cells(sweep=0, threshold_dbz=40).to_dataframe()      # doctest: +SKIP
        """
        from engine import cells as cell_engine

        sweep = self._check_sweep(self.default_sweep([variable]) if sweep is None else sweep)
        name = self.resolve(variable, sweep)
        opts: dict[str, Any] = {"backend": backend}
        for key, val in (
            ("thresholds", list(thresholds) if thresholds else None),
            ("grid_spacing_km", grid_spacing_km),
            ("smooth", smooth),
        ):
            if val is not None:
                opts[key] = val
        try:
            opts = cell_engine.options_from_params(opts)
        except ValueError as exc:
            raise XrdArgumentError(str(exc)) from exc
        data = self.field(name, sweep)
        scan = cell_engine.identify_sweep(
            self.tree, data, float(threshold_dbz), float(min_size_km2), **opts
        )
        result = CellsResult(
            scan,
            data,
            sweep=sweep,
            variable=name,
            threshold_dbz=float(threshold_dbz),
            min_size_km2=float(min_size_km2),
        )
        if out is not None:
            result.save(out)
        if plot is not None:
            save_figure(result.plot(), plot)
        return result

    def retrieve(self, name: str, sweep: int = 0, **params: Any) -> xr.DataArray:
        """Run one of the built-in retrievals (``kdp``, ``rain_rate_z`` ... see below).

        Examples
        --------
        >>> from engine.retrievals import RETRIEVAL_REGISTRY
        >>> sorted(RETRIEVAL_REGISTRY)                       # doctest: +SKIP
        >>> vol.retrieve("rain_rate_z", sweep=0)             # doctest: +SKIP
        """
        from engine.retrievals import run_retrieval

        return run_retrieval(
            self.tree, name, self._check_sweep(sweep), params, var_overrides=self.mapping
        )

    def qpe(self, **kwargs: Any) -> Any:
        """Rain rate of this volume on a map grid (``rxd qpe -i FILE``); see :func:`qpe`.

        One volume gives an instantaneous composite (``result.rate_last``, mm/h) with its
        uncertainty and quality fields; for an accumulation use :func:`qpe` with several volumes.

        Examples
        --------
        >>> res = vol.qpe(relation="tropical")            # doctest: +SKIP
        >>> res.save("rate.nc")                            # doctest: +SKIP
        """
        return qpe([self], **kwargs)

    def statistics(
        self, variable: str, sweep: int = 0, stat_type: str = "histogram", **params: Any
    ) -> dict[str, Any]:
        """Histogram / vertical profile / cell stats / QC stats of a variable.

        ``stat_type`` is ``"histogram"``, ``"profile"``, ``"cell_stats"`` or ``"qc"``.
        """
        from engine.statistics import get_statistics

        name = self.resolve(variable, self._check_sweep(sweep))
        return get_statistics(self.tree, name, sweep, stat_type, params)

    # -- figures and files ---------------------------------------------------------------

    def plot(
        self,
        variable: str | None = None,
        sweep: int | None = None,
        *,
        out: str | os.PathLike[str] | None = None,
        **kwargs: Any,
    ) -> plt.Figure:
        """A PPI figure of one variable with the publication renderer.

        File variables use the same renderer as the app's publication export (range rings,
        colour scale per variable, no-echo codes masked). Derived variables use their own
        colour scale / categories.

        Parameters
        ----------
        variable : str, optional
            Default: reflectivity if present, else the first variable of the sweep.
        sweep : int, optional
            Default: the lowest sweep that has *variable* (0 when no variable is named).
        out : str or Path, optional
            Also save the figure (``.png``, ``.svg`` or ``.pdf``; CLI: ``-o``).
        dpi : int
        theme : {"light", "dark"}
        vmin, vmax : float, optional
            Colour range override.
        title : str, optional
        rings, colorbar : bool
        fig_size : float
            Figure edge in inches (publication renderer only).
        font_size : int
        ax : matplotlib Axes, optional
            Draw into this axes instead of creating a figure.

        Returns
        -------
        matplotlib.figure.Figure
            ``fig.savefig("ppi.png", dpi=200)`` writes it; in a notebook it displays.

        Examples
        --------
        >>> fig = vol.plot("DBZH", sweep=0, theme="dark")      # doctest: +SKIP
        >>> vol.plot("velocity", 1, out="vel.png")             # doctest: +SKIP
        """
        if sweep is None:
            sweep = self.default_sweep([variable] if variable else [])
        fig = self._plot(variable, sweep, **kwargs)
        if out is not None:
            from engine.api_results import save_figure

            save_figure(fig, out, dpi=int(kwargs.get("dpi", 100)), keep=True)
        return fig

    def _plot(
        self,
        variable: str | None = None,
        sweep: int = 0,
        *,
        dpi: int = 100,
        theme: str = "light",
        vmin: float | None = None,
        vmax: float | None = None,
        title: str | None = None,
        rings: bool = True,
        colorbar: bool = True,
        fig_size: float = 7.5,
        font_size: int = 11,
        ax: plt.Axes | None = None,
    ) -> plt.Figure:
        """A PPI figure of one variable with the publication renderer.

        File variables use the same renderer as the app's publication export (range rings,
        colour scale per variable, no-echo codes masked). Derived variables use their own
        colour scale / categories.

        Parameters
        ----------
        variable : str, optional
            Default: reflectivity if present, else the first variable of the sweep.
        sweep : int
        dpi : int
        theme : {"light", "dark"}
        vmin, vmax : float, optional
            Colour range override.
        title : str, optional
        rings, colorbar : bool
        fig_size : float
            Figure edge in inches (publication renderer only).
        font_size : int
        ax : matplotlib Axes, optional
            Draw into this axes instead of creating a figure.

        Returns
        -------
        matplotlib.figure.Figure
            ``fig.savefig("ppi.png", dpi=200)`` writes it; in a notebook it displays.

        Examples
        --------
        >>> fig = vol.plot("DBZH", sweep=0, theme="dark")      # doctest: +SKIP
        >>> fig.savefig("ppi.png")                             # doctest: +SKIP
        """
        from engine import exporter

        if theme not in ("light", "dark"):
            raise XrdArgumentError("theme must be 'light' or 'dark'")
        sweep = self._check_sweep(sweep)
        if variable is None:
            variable = self._default_variable(sweep)
        name = self.resolve(variable, sweep)
        if self._store.has(name, sweep):
            meta = self.derived_meta(name)
            da = self.field(name, sweep)
            return plot_ppi(
                np.asarray(da.values),
                da["azimuth"].values,
                da["range"].values,
                ax=ax,
                title=title or f"{meta['label']} | sweep {sweep}",
                units=str(meta.get("units") or ""),
                label=str(meta.get("label") or name),
                vmin=vmin if vmin is not None else meta.get("vmin"),
                vmax=vmax if vmax is not None else meta.get("vmax"),
                cmap=meta.get("cmap"),
                categories=meta.get("categories"),
                rings=rings,
                colorbar=colorbar,
            )
        if ax is not None:
            da = self.field(name, sweep)
            lo, hi = exporter._get_range(name)
            return plot_ppi(
                np.asarray(da.values),
                da["azimuth"].values,
                da["range"].values,
                ax=ax,
                title=title or f"{name} | sweep {sweep}",
                units=exporter._get_units(name, dict(da.attrs)),
                label=name,
                vmin=vmin if vmin is not None else lo,
                vmax=vmax if vmax is not None else hi,
                cmap=exporter._get_cmap(name),
                rings=rings,
                colorbar=colorbar,
            )
        return exporter.render_publication_figure(
            self.tree,
            name,
            sweep,
            dpi=int(dpi),
            fig_size=float(fig_size),
            font_size=int(font_size),
            show_rings=rings,
            show_colorbar=colorbar,
            show_title=True,
            title_override=title,
            dark_background=theme == "dark",
            vmin=vmin,
            vmax=vmax,
        )

    def _default_variable(self, sweep: int) -> str:
        names = self.variables(sweep, include_derived=False)
        ds = self._reader.get_sweep_dataset(sweep, mask=False)
        match = varmap.resolve(ds, "DBZH", overrides=self.mapping)
        if match is not None:
            return match.variable
        if not names:
            raise XrdArgumentError(f"Sweep {sweep} has no radar moments to plot")
        return names[0]

    def to_dataset(
        self,
        sweep: int = 0,
        *,
        include_derived: bool | Sequence[str] = False,
        mask: bool = True,
    ) -> xr.Dataset:
        """One sweep as an ``xarray.Dataset``, optionally with derived variables added.

        Derived variables are named like in exported files (``"qc:cleaned"`` becomes
        ``"qc__cleaned"``) and carry units / long_name; sweeps they were not computed on are
        NaN.

        Examples
        --------
        >>> ds = vol.to_dataset(0, include_derived=True)        # doctest: +SKIP
        """
        from engine.reader import mask_below_threshold, sorted_sweep_names

        sweep = self._check_sweep(sweep)
        names = self._derived_selection(include_derived)
        if not names:
            return self.dataset(sweep, mask=mask)
        tree = derived.tree_with_derived(self.tree, self._store, names=names)
        ds = tree[sorted_sweep_names(tree)[sweep]].to_dataset()
        return mask_below_threshold(ds) if mask else ds

    def _derived_selection(
        self, include_derived: bool | Sequence[str] | None, *, strict: bool = False
    ) -> list[str]:
        if not include_derived:
            return []
        available = [v["name"] for v in self.derived]
        if include_derived is True:
            if strict and not available:
                raise XrdArgumentError(
                    "This volume has no derived variables yet: create them with "
                    "vol.run(..., all_sweeps=True) or vol.pipeline(...) first"
                )
            return available
        wanted = [include_derived] if isinstance(include_derived, str) else list(include_derived)
        unknown = [n for n in wanted if n not in available]
        if unknown:
            raise XrdArgumentError(
                f"No derived variable(s) {unknown}. Derived: {available or 'none yet'}. "
                "Create them with vol.run(..., all_sweeps=True) or vol.pipeline(...)."
            )
        return wanted

    def export(
        self,
        fmt: str | os.PathLike[str] | None = None,
        out: str | os.PathLike[str] | None = None,
        *,
        variable: str | None = None,
        sweep: int = 0,
        include_derived: bool | Sequence[str] = False,
        include_original: bool = True,
        dpi: int = 300,
        progress: ProgressFn | None = None,
        **options: Any,
    ) -> Path:
        """Write the volume (or one sweep / variable) to a file.

        Parameters
        ----------
        fmt : {"netcdf", "cfradial2", "zarr", "csv", "png", "svg", "pdf"}, optional
            ``"nc"`` is accepted for ``"netcdf"`` (CfRadial1). ``csv`` and the image formats
            write the given ``variable`` / ``sweep``; the others write the whole volume.
            Optional when ``out`` has a telling extension (``.nc``, ``.zarr``, ``.csv``,
            ``.png``, ``.svg``, ``.pdf``); ``"cfradial2"`` must be named. ``vol.export("x.nc")``
            works too: a first argument that is a path is taken as ``out``.
        out : str or Path, optional
            Destination (default: the current directory, named after the file).
        variable : str, optional
            For csv / images (any mapped name or a derived variable).
        sweep : int
        include_derived : bool or list of str
            Add derived variables (all, or the named ones) to netcdf / zarr / cfradial2 files.
        include_original : bool
            ``False`` writes only the derived variables (plus the 1-D metadata).
        dpi : int
            Image formats.
        **options
            Image options: ``fig_size``, ``font_size``, ``show_rings``, ``show_colorbar``,
            ``title``, ``dark_background``, ``vmin``, ``vmax``.

        Returns
        -------
        pathlib.Path
            The file that was written.

        Examples
        --------
        >>> vol.export("netcdf", "ktlx_qc.nc", include_derived=True)      # doctest: +SKIP
        >>> vol.export(out="ktlx.zarr")                                    # doctest: +SKIP
        >>> vol.export("png", "ppi.png", variable="DBZH", sweep=0, dpi=200)  # doctest: +SKIP
        """
        from engine.exporter import RadarExporter

        if (
            out is None
            and fmt is not None
            and os.fspath(fmt).lower().strip() not in {*EXPORT_FORMATS, *_FORMAT_ALIASES}
        ):
            out, fmt = fmt, None  # vol.export("ktlx.nc")
        key = _export_format(fmt, out)
        sweep = self._check_sweep(sweep)
        names = self._derived_selection(include_derived, strict=True)
        if variable is not None:
            variable = self.resolve(variable, sweep)
        asked = [variable] if variable and self._store.has(variable) else []
        names = sorted({*names, *asked})
        if not include_original and not names:
            raise XrdArgumentError("Nothing to export: no derived variables selected")
        tree = self.tree
        opts = dict(options)
        if names:
            tree = derived.tree_with_derived(
                tree, self._store, names=names, include_original=include_original
            )
            if asked:
                meta = self.derived_meta(asked[0])
                for k in ("vmin", "vmax"):
                    if opts.get(k) is None and isinstance(meta.get(k), (int, float)):
                        opts[k] = meta[k]
                variable = derived.export_name(asked[0])
        if key in ("csv", "png", "svg", "pdf") and variable is None:
            variable = self._default_variable(sweep)
        target = Path(out).expanduser() if out else self._default_export_path(key, variable, sweep)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            written = RadarExporter().export(
                tree,
                fmt=key,
                dpi=int(dpi),
                output_path=str(target),
                variable=variable,
                sweep=sweep,
                progress=progress or (lambda _p, _m: None),
                options=opts,
            )
        except (AttributeError, KeyError) as exc:
            if key not in ("netcdf", "cfradial2"):
                raise
            raise XrdError(
                f"Could not write {key}: the volume lacks metadata the format needs "
                f"({type(exc).__name__}: {exc}). Export as zarr or csv instead."
            ) from exc
        return Path(written)

    def _default_export_path(self, fmt: str, variable: str | None, sweep: int) -> Path:
        stem = Path(self.path).name.split(".")[0] if self.path else self.name
        parts = [stem]
        if fmt in ("csv", "png", "svg", "pdf"):
            parts += [(variable or "var").replace(":", "_"), f"sweep{sweep}"]
        else:
            parts.append("rxd")
        return Path.cwd() / ("_".join(parts) + _EXPORT_EXT[fmt])


_EXT_FORMATS = {
    ".nc": "netcdf",
    ".zarr": "zarr",
    ".csv": "csv",
    ".png": "png",
    ".svg": "svg",
    ".pdf": "pdf",
}


def _export_format(fmt: Any, out: Any) -> str:
    """Resolve the export format from ``fmt`` and/or the extension of ``out``."""
    if fmt is not None:
        low = os.fspath(fmt).lower().strip()
        key = _FORMAT_ALIASES.get(low, low)
        if key in EXPORT_FORMATS:
            return key
        if out is None and Path(low).suffix in _EXT_FORMATS:  # export("ktlx.nc")
            return _EXT_FORMATS[Path(low).suffix]
        raise XrdArgumentError(
            f"Unknown export format '{fmt}'. Choose one of: {', '.join(EXPORT_FORMATS)}"
        )
    suffix = Path(os.fspath(out)).suffix.lower() if out is not None else ""
    if suffix in _EXT_FORMATS:
        return _EXT_FORMATS[suffix]
    raise XrdArgumentError(
        "Cannot tell the export format: give fmt=... (one of "
        f"{', '.join(EXPORT_FORMATS)}) or an out= path ending in "
        f"{', '.join(sorted(_EXT_FORMATS))}"
    )


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _resolve_recipe_arg(
    spec: Any, target: str | None, params: Mapping[str, Any] | None = None
) -> Recipe:
    """The recipe with a new target and / or changed step settings applied."""
    from engine import pipeline

    rec = recipe(spec)
    if not target and not params:
        return rec
    data = rec.to_dict()
    data["builtin"] = False
    if target:
        data["target"] = target
    for key, value in (params or {}).items():
        step_id, _, pname = str(key).partition(".")
        hits = [st for st in data["steps"] if st["step_id"] == step_id]
        if not hits:
            have = ", ".join(sorted({st["step_id"] for st in data["steps"]}))
            raise XrdArgumentError(
                f"Recipe '{rec.name}' has no step '{step_id}' (param '{key}'). Steps: {have}"
            )
        for st in hits:
            if not pname or pname == "enabled":
                st["enabled"] = _parse_bool(value) if isinstance(value, str) else bool(value)
            else:
                specs = pipeline.get_step(step_id).params
                st["params"][pname] = coerce_user_params(
                    specs, {pname: value}, f"Step '{step_id}'"
                )[pname]
    try:
        return pipeline.parse_recipe(data, strict_name=False)
    except plugins.ModuleError as exc:
        raise XrdArgumentError(f"Invalid recipe settings: {exc}") from exc


# ---------------------------------------------------------------------------
# Opening files
# ---------------------------------------------------------------------------


def open(  # noqa: A001 - the documented entry point is api.open(); the builtin is not used here
    path: str | os.PathLike[str] | xr.DataTree | Volume,
    *,
    mapping: Mapping[str, str] | None = None,
) -> Volume:
    """Open a radar file (any format xradar reads) and return a :class:`Volume`.

    Parameters
    ----------
    path : str, Path, xarray.DataTree or Volume
        A file path (``.gz`` Level-II archives are decompressed transparently), an in-memory
        ``DataTree`` or an existing Volume (returned unchanged).
    mapping : dict, optional
        Variable-mapping overrides ``{role: variable}``, e.g. ``{"velocity": "V"}``.

    Raises
    ------
    XrdArgumentError
        The file does not exist or is not a file.
    XrdError
        The file exists but no reader could open it (the message lists what was tried).

    Examples
    --------
    >>> from engine import api
    >>> vol = api.open("~/data/KTLX20130520_201643_V06.gz")        # doctest: +SKIP
    >>> vol
    <Volume KTLX20130520_201643_V06.gz | nexrad_level2 | 14 sweeps | 0 derived>
    """
    if isinstance(path, Volume):
        if mapping:
            path.mapping = varmap.normalize_overrides(mapping)
        return path
    if isinstance(path, xr.DataTree):
        return Volume.from_tree(path, mapping=mapping)
    from engine.reader import RadarReader

    raw = os.fspath(path)
    resolved = os.path.abspath(os.path.expanduser(raw))
    if not os.path.exists(resolved):
        raise XrdInputError(
            f"File not found: {raw} (looked for {resolved}). Check the path and the spelling."
        )
    if os.path.isdir(resolved):
        raise XrdInputError(
            f"{raw} is a directory. Give a radar file (for example one *_V06 file), not a folder "
            "(on the command line a folder is expanded to the radar files inside it)."
        )
    reader = RadarReader()
    try:
        reader.open_file(resolved)
    except (ValueError, PermissionError, OSError) as exc:
        raise XrdError(_message(exc)) from exc
    return Volume(reader, mapping=mapping)


def _as_volumes(
    items: Any, *, minimum: int = 1, what: str = "volume"
) -> tuple[list[Volume], list[Volume]]:
    """``(volumes, opened_here)``: paths are opened; Volumes are used as they are."""
    if isinstance(items, (str, os.PathLike, Volume, xr.DataTree)):
        items = [items]
    items = list(items)
    if len(items) < minimum:
        raise XrdArgumentError(
            f"Need at least {minimum} {what}(s), got {len(items)}. Pass a list of files or Volumes."
        )
    opened: list[Volume] = []
    out: list[Volume] = []
    try:
        for item in items:
            was_volume = isinstance(item, Volume)
            vol = open(item)
            out.append(vol)
            if not was_volume:
                opened.append(vol)
    except BaseException:
        for v in opened:
            v.close()
        raise
    return out, opened


def _pairs(volumes: Sequence[Volume]) -> list[tuple[str | None, Any]]:
    return [(v.path or f"volume_{i}", v._reader) for i, v in enumerate(volumes)]


def _close_all(volumes: Iterable[Volume]) -> None:
    for v in volumes:
        v.close()


# ---------------------------------------------------------------------------
# Mosaic
# ---------------------------------------------------------------------------


def _split_options(
    options: Mapping[str, Any],
    groups: Mapping[str, set[str]],
    rest_allowed: set[str],
    label: str,
    aliases: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Sort flat keyword options into groups of the engine (grid / merge / options ...)."""
    out: dict[str, dict[str, Any]] = {g: {} for g in groups}
    out["rest"] = {}
    everything = set(rest_allowed).union(*groups.values())
    for key, value in options.items():
        key = (aliases or {}).get(key, key)
        for g, names in groups.items():
            if key in names:
                out[g][key] = value
                break
        else:
            if key not in rest_allowed:
                close = difflib.get_close_matches(key, sorted(everything), n=2, cutoff=0.5)
                hint = f" Did you mean {' or '.join(repr(c) for c in close)}?" if close else ""
                raise XrdArgumentError(
                    f"{label}: unknown option '{key}'.{hint} "
                    f"Options: {', '.join(sorted(everything))}"
                )
            out["rest"][key] = value
    return out


def _fields(cls: type) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)}


def _bbox_extent(extent: Any) -> Any:
    """``(west, south, east, north)`` degrees -> the engine's dict; ``None``/dict pass through."""
    if extent is None or extent == "auto" or isinstance(extent, Mapping):
        return "auto" if extent is None else extent
    try:
        west, south, east, north = (float(v) for v in extent)
    except (TypeError, ValueError) as exc:
        raise XrdArgumentError(
            "extent must be 'auto', (west, south, east, north) in degrees or a dict with those keys"
        ) from exc
    return {"west": west, "south": south, "east": east, "north": north}


def mosaic(
    files: Sequence[Volume | str | os.PathLike[str]] | Volume | str,
    product: str = "composite",
    variable: str = "DBZH",
    resolution_m: float = 1000.0,
    height_m: float = 3000.0,
    *,
    extent: Any = None,
    max_range_km: float | None = None,
    merge: bool = True,
    out: str | os.PathLike[str] | Sequence[str | os.PathLike[str]] | None = None,
    plot: str | os.PathLike[str] | None = None,
    progress: ProgressFn | None = None,
    **options: Any,
) -> Mosaic:
    """Merge several radars (or one) onto one map grid.

    Parameters
    ----------
    files : list of Volume or path
        One or more volumes scanned at about the same time (CLI: ``rxd mosaic -i F1 F2 ...``).
    product : {"composite", "cappi", "echo_top"}
        Column maximum, constant-altitude PPI at ``height_m`` (above sea level), or the
        height of the ``threshold_dbz`` echo.
    variable : str
        Field to mosaic (reflectivity by default; any mapped name works).
    resolution_m : float
        Cell size (250 to 5000 m).
    height_m : float
        CAPPI height above sea level (``product="cappi"`` only).
    extent : "auto" or (west, south, east, north), optional
        Map box in degrees (default: covers all radars).
    max_range_km : float, optional
        Ignore data beyond this range from each radar.
    merge : bool
        ``False`` keeps only the first radar's value where radars overlap instead of the
        distance / time / beam weighted merge.
    out : path or list of paths, optional
        Also save the result: ``.png`` (picture + world file + projection) and / or ``.nc``
        (CLI: ``-o``).
    plot : path, optional
        Also save a map figure with the radar sites (``.png`` / ``.svg`` / ``.pdf``; CLI: ``-p``).
    progress : callable, optional
    **options
        More engine options, passed through: ``threshold_dbz``, ``vmin``, ``vmax``,
        ``min_rhohv``, ``z_max_m``, ``z_step_m``, ``range_scale_km``, ``time_scale_s``,
        ``beamwidth_deg``, ``calibrate``, ``reference_index``, ``advect``, ``motion``,
        ``average`` ... (see :class:`engine.mosaic.GridSpec` / :class:`engine.mosaic.MergeSpec`
        and :func:`engine.mosaic.build_mosaic`).

    Returns
    -------
    Mosaic

    Examples
    --------
    >>> mos = api.mosaic([khpx, knqa, kpah, kvwx], product="composite")   # doctest: +SKIP
    >>> mos.save_png("mosaic.png"); mos.save_netcdf("mosaic.nc")          # doctest: +SKIP
    """
    from engine import mosaic as me

    vols, opened = _as_volumes(files)
    try:
        build_params = {
            n
            for n in inspect.signature(me.build_mosaic).parameters
            if n not in {"readers", "variable", "product", "grid", "merge", "progress", "cache"}
        }
        split = _split_options(
            options,
            {
                "grid": _fields(me.GridSpec) - {"resolution_m", "extent", "max_range_km"},
                "merge": _fields(me.MergeSpec) - {"enabled"},
            },
            build_params - {"height_m"},
            "mosaic",
        )
        grid = {"resolution_m": resolution_m, "extent": _bbox_extent(extent), **split["grid"]}
        if max_range_km is not None:
            grid["max_range_km"] = max_range_km
        merge_opts = {"enabled": bool(merge), **split["merge"]}
        record = me.build_mosaic(
            _pairs(vols),
            variable,
            product,
            grid,
            merge_opts,
            height_m=height_m,
            progress=progress,
            cache=None,
            **split["rest"],
        )
    finally:
        _close_all(opened)
    result = Mosaic(record)
    if out is not None:
        result.save(out)
    if plot is not None:
        save_figure(result.plot(), plot)
    return result


# ---------------------------------------------------------------------------
# Hydrology: rainfall estimation, accumulation, gauges and basins
# ---------------------------------------------------------------------------


def qpe(
    files: Sequence[Volume | str | os.PathLike[str]] | Volume | str,
    *,
    method: str = "auto",
    relation: str | None = None,
    band: str = "S",
    precip: str = "rain",
    start: Any = None,
    end: Any = None,
    resolution_m: float = 1000.0,
    extent: Any = None,
    max_range_km: float = 230.0,
    gauges: Any = None,
    adjust: str | None = None,
    basins: Any = None,
    thresholds: Sequence[float] | None = None,
    frequency: Any = None,
    dem: Any = None,
    gauge_options: Mapping[str, Any] | None = None,
    basin_options: Mapping[str, Any] | None = None,
    adjust_options: Mapping[str, Any] | None = None,
    out: str | os.PathLike[str] | Sequence[str | os.PathLike[str]] | None = None,
    plot: str | os.PathLike[str] | None = None,
    field: str | None = None,
    progress: ProgressFn | None = None,
    **options: Any,
) -> Any:
    """Radar rainfall: rate, accumulation, uncertainty, gauge adjustment and basin averages.

    ``rxd qpe -i FILES -o qpe.nc`` is ``api.qpe(FILES, out="qpe.nc")``. Methods, equations and
    limits are in ``docs/HYDROLOGY.md``; every output is labelled ``RETRIEVED`` (a radar estimate)
    or ``ADJUSTED`` (after gauges), never as an observation.

    Parameters
    ----------
    files : list of Volume or path
        Volumes of one or several radars. Several scans in time give an accumulation over
        ``start`` to ``end`` (default: first to last scan); one volume per radar gives the
        instantaneous rate (``result.rate_last``).
    method : {"auto", "hybrid", "z", "kdp", "z_zdr", "a"}
        ``auto`` = hybrid when ZDR and PHIDP/KDP exist, else R(Z,ZDR), else R(Z).
    relation : str, optional
        Z-R relation of the ``z`` method and of the light-rain regime: ``wsr88d`` (default),
        ``marshall_palmer``, ``tropical``, ``stratiform_east`` ..., snow ``snow_sekhon_srivastava``
        with ``precip="snow"``. Custom: ``zr_a=200, zr_b=1.6`` (``Z = a R^b``).
    gauges : path or GaugeSet, optional
        Rain-gauge records (CSV/JSON/NOAA/METAR); the accumulation is adjusted with ``adjust``
        (``mfb`` mean-field bias, ``brandes`` spatial ratio, ``ked`` kriging with external drift)
        and the skill before/after (also leave-one-out) is in ``result.validation``.
    basins : path or BasinSet, optional
        GeoJSON / shapefile / GeoPackage polygons: area-weighted statistics and rainfall series in
        ``result.basin_series``; ``frequency`` adds the return period of the basin-mean depth.
    out, plot : path(s)
        ``.nc`` / ``.png`` (+ ``.pgw``, ``.prj``) / ``.json`` / ``.csv`` and a figure.
    **options
        Any :class:`engine.hydro.qpe.QpeConfig` field (``freezing_level_m``, ``z_cap``,
        ``attenuation``, ``zdr_offset_db`` ...) or accumulation option (``composite``,
        ``advect``, ``step_s``, ``max_gap_s``, ``hold_edges_s``, ``n_sweeps`` ...).

    Returns
    -------
    engine.hydro.accumulate.QpeResult

    Examples
    --------
    >>> res = api.qpe(sorted(glob("mayfield/KHPX/*")), out="qpe.nc")   # doctest: +SKIP
    >>> res = api.qpe(files, gauges="g.csv", basins="b.geojson", adjust="ked")  # doctest: +SKIP
    >>> res.basin_series.summary()                                      # doctest: +SKIP
    """
    from engine.hydro import run as hydro_run
    from engine.hydro.qpe import QpeConfig, QpeError

    try:
        cfg_opts, acc_opts = hydro_run.split_options(options)
        config = {
            "method": method,
            "relation": relation,
            "band": band,
            "precip": precip,
            **cfg_opts,
        }
        QpeConfig.from_dict(config)  # wrong values are usage errors, not processing failures
    except QpeError as exc:
        raise XrdArgumentError(str(exc)) from exc
    if extent is not None:
        acc_opts["extent"] = _extent4(extent)
    sources = _qpe_sources(files)
    result = hydro_run.run_qpe(
        sources,
        config=config,
        basins=basins,
        basin_options=basin_options,
        thresholds=thresholds,
        frequency=frequency,
        gauges=gauges,
        gauge_options=gauge_options,
        adjust=adjust or ("mfb" if gauges is not None else None),
        adjust_options=adjust_options,
        dem=dem,
        progress=progress,
        resolution_m=resolution_m,
        max_range_km=max_range_km,
        start=start,
        end=end,
        **acc_opts,
    )
    if out is not None:
        result.save(out, field=field)
    if plot is not None:
        save_figure(result.plot(field), plot)
    return result


def _qpe_sources(files: Any) -> list[Any]:
    """Volumes for :func:`qpe`: files stay paths (opened one at a time while accumulating, so a
    long series needs the memory of one volume); an opened Volume or DataTree is used as it is."""
    if isinstance(files, (str, os.PathLike, Volume, xr.DataTree)):
        files = [files]
    items = list(files)
    if not items:
        raise XrdArgumentError("Need at least 1 volume(s), got 0. Pass a list of files or Volumes.")
    out: list[Any] = []
    for item in items:
        if isinstance(item, Volume):
            out.append(item._reader)
        elif isinstance(item, xr.DataTree):
            out.append(item)
        else:
            raw = os.fspath(item)
            resolved = os.path.abspath(os.path.expanduser(raw))
            if not os.path.exists(resolved):
                raise XrdInputError(
                    f"File not found: {raw} (looked for {resolved}). "
                    "Check the path and the spelling."
                )
            if os.path.isdir(resolved):
                raise XrdInputError(f"{raw} is a directory. Give radar files, not a folder.")
            out.append(resolved)
    return out


def _extent4(extent: Any) -> tuple[float, float, float, float]:
    """``(west, south, east, north)`` degrees from a 4-sequence or a mosaic-style dict."""
    try:
        if isinstance(extent, Mapping):
            return tuple(float(extent[k]) for k in ("west", "south", "east", "north"))  # type: ignore[return-value]
        west, south, east, north = (float(v) for v in extent)
    except (TypeError, ValueError, KeyError) as exc:
        raise XrdArgumentError(
            "extent must be (west, south, east, north) in degrees or a dict with those keys"
        ) from exc
    return (west, south, east, north)


# ---------------------------------------------------------------------------
# Multi-Doppler winds
# ---------------------------------------------------------------------------


def _origin_arg(origin: Any) -> Any:
    if origin is None or origin == "auto":
        return "auto"
    if isinstance(origin, Mapping):
        return origin
    try:
        lat, lon = (float(v) for v in origin)
    except (TypeError, ValueError) as exc:
        raise XrdArgumentError(
            "origin must be 'auto', (lat, lon) or {'lat':..., 'lon':...}"
        ) from exc
    return {"lat": lat, "lon": lon}


def _wind_extent(extent: Any) -> Any:
    """km half-width, ``(x_min, x_max, y_min, y_max)`` km, or a dict -> the engine's box."""
    if extent is None or extent == "auto" or isinstance(extent, Mapping):
        return "auto" if extent is None else extent
    if isinstance(extent, (int, float)) and not isinstance(extent, bool):
        half = float(extent)
        return {"x_min_km": -half, "x_max_km": half, "y_min_km": -half, "y_max_km": half}
    try:
        x0, x1, y0, y1 = (float(v) for v in extent)
    except (TypeError, ValueError) as exc:
        raise XrdArgumentError(
            "extent must be 'auto', a half-width in km, or (x_min, x_max, y_min, y_max) in km "
            "east / north of the origin"
        ) from exc
    return {"x_min_km": x0, "x_max_km": x1, "y_min_km": y0, "y_max_km": y1}


def winds(
    files: Sequence[Volume | str | os.PathLike[str]],
    method: str = "analytic",
    dealias: str | None = "auto",
    *,
    extent: Any = None,
    origin: Any = None,
    dx_m: float | None = None,
    dz_m: float | None = None,
    z_min_m: float | None = None,
    z_max_m: float | None = None,
    max_range_km: float | None = None,
    out: str | os.PathLike[str] | Sequence[str | os.PathLike[str]] | None = None,
    plot: str | os.PathLike[str] | None = None,
    level_km: float | None = None,
    plot_field: str = "speed",
    progress: ProgressFn | None = None,
    cancel: Callable[[], bool] | None = None,
    **options: Any,
) -> Winds:
    """Retrieve 3-D winds from two or more radars that scan the same weather.

    Parameters
    ----------
    files : list of Volume or path
        At least two volumes, scanned at about the same time (CLI: ``rxd winds -i F1 F2``).
    method : {"analytic", "variational"}
        Dual-Doppler solution (fast) or variational analysis (smoother, slower).
    dealias : str or None
        Velocity unfolding mode passed to the engine when it supports one (``"auto"`` by
        default; ignored silently if this version of the engine has no such option, an error
        if you ask for a specific mode it cannot take).
    extent : float or (x_min, x_max, y_min, y_max), optional
        Analysis box in km east / north of ``origin``: one number is a half-width, a 4-tuple
        gives explicit bounds. Default: derived from the radar positions.
    origin : (lat, lon), optional
        Grid origin (default: the centroid of the radars).
    dx_m, dz_m : float, optional
        Horizontal and vertical grid spacing in metres.
    z_min_m, z_max_m : float, optional
        Lowest and highest level, metres above sea level.
    max_range_km : float, optional
        Ground range beyond which a radar's data is not used.
    out : path or list of paths, optional
        Also save the result: ``.nc`` (NetCDF), ``.json``, and / or ``.png`` (map of one
        level, see :meth:`Winds.save`).
    plot : path, optional
        Also save a map of one level (CLI: ``-p``), at ``level_km`` km above sea level (default:
        the best-covered level) coloured by ``plot_field`` (default ``"speed"``).
    progress, cancel : callable, optional
        Progress callback ``(percent, message)`` and a ``() -> bool`` cancel check.
    **options
        Retrieval options of :class:`engine.multidoppler.Options`: ``min_crossing_angle``,
        ``fall_speed_correction``, ``freezing_level``, ``melting_depth_m``, ``boundary``,
        ``w_bottom``, ``obs_error``, ``background`` (``"vvp"``, ``"none"`` or a dict),
        ``storm_motion``, ``uncertainty`` ... New options of the engine pass through.

    Returns
    -------
    Winds

    Examples
    --------
    >>> w = api.winds([khpx, knqa], method="analytic", extent=80, dz_m=500)   # doctest: +SKIP
    >>> w.plot(level_km=3.0).savefig("winds.png")                            # doctest: +SKIP
    >>> w.save_netcdf("winds.nc")                                            # doctest: +SKIP
    """
    from engine import multidoppler as md

    vols, opened = _as_volumes(files, minimum=2, what="radar volume")
    try:
        option_fields = _fields(md.Options)
        grid_fields = _fields(md.GridSpec)
        aliases = {"zmin_m": "z_min_m", "zmax_m": "z_max_m", "dz": "dz_m", "dx": "dx_m"}
        split = _split_options(
            options, {"grid": grid_fields, "options": option_fields}, set(), "winds", aliases
        )
        grid: dict[str, Any] = {
            "origin": _origin_arg(origin),
            "extent": _wind_extent(extent),
            **split["grid"],
        }
        for key, val in (
            ("dx_m", dx_m),
            ("dz_m", dz_m),
            ("z_min_m", z_min_m),
            ("z_max_m", z_max_m),
            ("max_range_km", max_range_km),
        ):
            if val is not None:
                grid[key] = val
        opts: dict[str, Any] = {"method": method, **split["options"]}
        if isinstance(opts.get("background"), str):
            opts["background"] = {"mode": opts["background"]}
        if dealias is not None:
            if "dealias" in option_fields:
                opts["dealias"] = dealias
            elif dealias != "auto":
                raise XrdArgumentError(
                    f"This engine version has no dealias option (asked for '{dealias}'). "
                    "Update radarx-desktop or de-alias with vol.pipeline(...) first."
                )
        record = md.run_multidoppler(
            _pairs(vols), grid, opts, progress=progress, cancel=cancel, cache=None
        )
    finally:
        _close_all(opened)
    result = Winds(record)
    if out is not None:
        result.save(out, level_km, plot_field)
    if plot is not None:
        save_figure(result.plot(level_km, plot_field), plot)
    return result


# ---------------------------------------------------------------------------
# Cell tracking
# ---------------------------------------------------------------------------


def track(
    files: Sequence[Volume | str | os.PathLike[str]],
    variable: str = "DBZH",
    sweep: int = 0,
    threshold_dbz: float = 35.0,
    min_size_km2: float = 10.0,
    *,
    dt_seconds: float | None = None,
    max_distance_km: float = 30.0,
    min_overlap: float = 0.3,
    motion: tuple[float, float] | None = None,
    backend: str = "auto",
    thresholds: Sequence[float] | None = None,
    grid_spacing_km: float | None = None,
    smooth: float | None = None,
    sort: bool = True,
    out: str | os.PathLike[str] | Sequence[str | os.PathLike[str]] | None = None,
    plot: str | os.PathLike[str] | None = None,
) -> TrackingResult:
    """Identify storm cells in several consecutive volumes of ONE radar and link them in time.

    Parameters
    ----------
    files : list of Volume or path
        Volumes of the same radar (CLI: ``rxd cells -i F1 F2 F3``). They are put in time
        order when ``sort`` is true and every
        volume carries ray times.
    variable, sweep, threshold_dbz, min_size_km2, backend, thresholds, grid_spacing_km, smooth
        As in :meth:`Volume.cells`.
    dt_seconds : float, optional
        Fixed time step between volumes; default: the times stored in the files (5 minutes if
        a file has none).
    max_distance_km : float
        Largest centroid distance for linking cells that do not overlap.
    min_overlap : float
        radarx: overlap, as a fraction of either cell, needed to link two cells.
    motion : (u, v), optional
        Storm motion in m/s used to advect cells (default: estimated).
    out : path or list of paths, optional
        Also save: ``.csv`` (all cells), ``.json`` (tracks and cells), ``.png`` (track map).
    plot : path, optional
        Also save the track map figure (CLI: ``-p``).

    Returns
    -------
    TrackingResult

    Examples
    --------
    >>> res = api.track(["scan1", "scan2", "scan3"], threshold_dbz=40)    # doctest: +SKIP
    >>> res.to_dataframe()                                                # doctest: +SKIP
    """
    from engine import cells as cell_engine

    vols, opened = _as_volumes(files)
    try:
        scans = [
            v.cells(
                sweep,
                variable,
                threshold_dbz,
                min_size_km2,
                backend=backend,
                thresholds=thresholds,
                grid_spacing_km=grid_spacing_km,
                smooth=smooth,
            )
            for v in vols
        ]
    finally:
        _close_all(opened)
    if sort and len(scans) > 1 and all(s.scan.time_s is not None for s in scans):
        scans.sort(key=lambda s: s.scan.time_s)
    times = [i * float(dt_seconds) for i in range(len(scans))] if dt_seconds else None
    tracks = cell_engine.track(
        [s.scan for s in scans],
        times_s=times,
        max_distance_km=max_distance_km,
        min_overlap=min_overlap,
        motion=motion,
        backend=backend,
        grid_spacing_km=grid_spacing_km,
    )
    result = TrackingResult(tracks, scans)
    if out is not None:
        result.save(out)
    if plot is not None:
        save_figure(result.plot(), plot)
    return result


# ---------------------------------------------------------------------------
# One-liners: open the file, call the Volume method (same names as the CLI)
# ---------------------------------------------------------------------------


def info(file: str | os.PathLike[str] | Volume) -> dict[str, Any]:
    """:meth:`Volume.info` of a file (``rxd info -i FILE``)."""
    return open(file).info()


def sounding(
    file: str | os.PathLike[str],
    *,
    format: str = "auto",  # noqa: A002 - same name as the command-line option
    wind_units: str | None = None,
    virtual_temperature: bool = True,
) -> dict[str, Any]:
    """Read a radiosonde file and return its labelled diagnostics (``rxd sounding -i FILE --json``).

    Formats: University of Wyoming text list and CSV, SPC / SHARPpy text, IEM RAOB JSON, IGRA2 and
    any CSV whose header names the columns. The result is a plain dict: ``levels`` (hPa, m, degC,
    m/s), ``provenance``, ``launch_time``, ``drift_note`` and ``diagnostics`` with the parcels
    (surface-based, mixed-layer, most-unstable: CAPE, CIN, LCL, LFC, EL, lifted index), bulk shear,
    storm-relative helicity with the Bunkers motion, freezing and wet-bulb-zero levels and
    precipitable water, each with its definition. Heights are metres above the launch site.

    Examples
    --------
    >>> d = api.sounding("BMX_20220330_00Z.csv", wind_units="kt")  # doctest: +SKIP
    >>> d["diagnostics"]["parcels"]["most-unstable"]["cape"]  # doctest: +SKIP
    """
    from engine import sounding as sounding_engine

    try:
        snd = sounding_engine.read_sounding_file(file, format, wind_units)
        result = sounding_engine.analyse(snd)
        if not virtual_temperature:
            result["diagnostics"] = sounding_engine.diagnose(snd, virtual=False)
        return result
    except sounding_engine.SoundingError as exc:
        raise XrdInputError(f"{Path(file).name}: {exc}") from exc
    except sounding_engine.RadarxMissing as exc:
        raise XrdDependencyError(str(exc)) from exc


def plot(
    file: str | os.PathLike[str] | Volume,
    variable: str | None = None,
    sweep: int | None = None,
    **kwargs: Any,
) -> plt.Figure:
    """:meth:`Volume.plot` of a file (``rxd plot -i FILE -V VARIABLE -s SWEEP -p OUT``).

    Examples
    --------
    >>> api.plot("KTLX20130520_201643_V06.gz", "velocity", out="vel.png")  # doctest: +SKIP
    """
    return open(file).plot(variable, sweep, **kwargs)


def run(
    module: str,
    file: str | os.PathLike[str] | Volume,
    sweep: int | None = None,
    **kwargs: Any,
) -> ModuleResult:
    """:meth:`Volume.run` on a file (``rxd run MODULE -i FILE``).

    The result keeps its volume alive, so ``all_sweeps=True`` results stay usable.

    Examples
    --------
    >>> f = "KTLX20130520_201643_V06.gz"
    >>> res = api.run("storm_detection", f, threshold_dbz=40)  # doctest: +SKIP
    """
    return open(file).run(module, sweep, **kwargs)


def pipeline(
    recipe: Any,
    file: str | os.PathLike[str] | Volume,
    target: str | None = None,
    **kwargs: Any,
) -> PipelineResult:
    """:meth:`Volume.pipeline` on a file (``rxd pipeline RECIPE -i FILE``)."""
    return open(file).pipeline(recipe, target, **kwargs)


def cells(
    file: str | os.PathLike[str] | Volume,
    sweep: int | None = None,
    variable: str = "DBZH",
    threshold_dbz: float = 35.0,
    min_size_km2: float = 10.0,
    **kwargs: Any,
) -> CellsResult:
    """:meth:`Volume.cells` of a file (``rxd cells -i FILE``); several files: :func:`track`."""
    return open(file).cells(sweep, variable, threshold_dbz, min_size_km2, **kwargs)


def export(
    file: str | os.PathLike[str] | Volume,
    fmt: str | None = None,
    out: str | os.PathLike[str] | None = None,
    **kwargs: Any,
) -> Path:
    """:meth:`Volume.export` of a file (``rxd export -i FILE -o OUT``).

    Examples
    --------
    >>> api.export("KTLX20130520_201643_V06.gz", out="ktlx.zarr")  # doctest: +SKIP
    """
    return open(file).export(fmt, out, **kwargs)
