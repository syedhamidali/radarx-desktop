"""Result objects returned by the public Python API (:mod:`engine.api`).

Each class wraps what the engine produced (module blocks, a mosaic record, a wind analysis,
tracked cells) and adds the things a notebook user expects: pandas ``DataFrame`` tables,
``xarray`` objects, ``.plot()`` returning a matplotlib ``Figure`` and ``.save_*()`` helpers.

Nothing here talks to the WebSocket server and nothing keeps global state: a result owns its data.
Plotting uses matplotlib's non-interactive Agg backend (like the publication export), so it works
on a headless machine; in a notebook the returned ``Figure`` is displayed as usual.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import xarray as xr

from engine import hdf5_guard

if TYPE_CHECKING:
    import matplotlib.pyplot as plt
    import pandas as pd

    from engine import cells as cell_engine
    from engine import mosaic as mosaic_engine
    from engine import multidoppler as mdop_engine
    from engine.api import Volume

__all__ = [
    "CellsResult",
    "ModuleResult",
    "Mosaic",
    "PipelineResult",
    "TrackingResult",
    "Winds",
    "XrdArgumentError",
    "XrdDependencyError",
    "XrdError",
    "XrdInputError",
]


class XrdError(Exception):
    """A user-facing failure of an API call (the message says what to do about it)."""


class XrdArgumentError(XrdError, ValueError):
    """The call itself was wrong: unknown module / parameter / variable, bad value."""


class XrdInputError(XrdArgumentError):
    """An input file is missing, is a folder, or cannot be read (the message says which)."""


class XrdDependencyError(XrdError):
    """An optional Python package the call needs is not installed (the message says how to)."""


def jsonable(obj: Any) -> Any:
    """*obj* made strictly JSON-safe: numpy -> python, NaN / Inf -> None, Path -> str."""
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return jsonable(obj.tolist())
    if isinstance(obj, np.generic):
        return jsonable(obj.item())
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "to_dict") and callable(obj.to_dict) and not isinstance(obj, (str, bytes)):
        return jsonable(obj.to_dict(orient="records") if hasattr(obj, "columns") else obj.to_dict())
    return obj


def dumps(obj: Any) -> str:
    """Pretty JSON text of *obj* (see :func:`jsonable`)."""
    import json

    return json.dumps(jsonable(obj), indent=2, ensure_ascii=False, allow_nan=False)


def save_figure(fig: Any, path: str | Path, dpi: int = 150, *, keep: bool = False) -> Path:
    """Write *fig* as ``.png`` / ``.svg`` / ``.pdf`` (parent folders are created); returns the path.

    The figure is closed afterwards unless ``keep=True``.
    """
    target = Path(path).expanduser()
    try:
        if target.suffix.lower() not in (".png", ".svg", ".pdf"):
            raise XrdArgumentError(
                f"Cannot write a figure to '{target}': use a .png, .svg or .pdf file name"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(target, dpi=dpi, bbox_inches="tight", facecolor=fig.get_facecolor())
    finally:
        if not keep:
            _pyplot().close(fig)
    return target


_KIND_BY_SUFFIX = {
    ".json": "json",
    ".csv": "csv",
    ".nc": "nc",
    ".zarr": "zarr",
    ".png": "fig",
    ".svg": "fig",
    ".pdf": "fig",
}


def as_list(out: Any) -> list[Any]:
    """``None`` -> ``[]``, one path -> ``[path]``, a sequence of paths -> a list."""
    if out is None:
        return []
    if isinstance(out, (str, Path)) or hasattr(out, "__fspath__"):
        return [out]
    return list(out)


class Destination:
    """Where a result is written: one exact file (chosen by its extension) or a folder.

    ``out`` with a known extension (``.json .csv .nc .png .svg .pdf``) is a single file and
    the extension selects what is written; any other path is a folder that receives one file
    per format in ``formats`` (default ``default``). Giving exactly one format together with
    an ``out`` that has an extension writes that format to exactly that file name.
    """

    def __init__(
        self,
        out: str | Path,
        base: str,
        formats: Sequence[str] | None = None,
        default: Sequence[str] = (),
    ) -> None:
        self.path = Path(out).expanduser()
        suffix = self.path.suffix.lower()
        known = _KIND_BY_SUFFIX.get(suffix)
        if formats:
            self.formats = list(formats)
            self.single = len(self.formats) == 1 and bool(suffix)
            self.explicit = True
        elif known:
            self.formats, self.single, self.explicit = [known], True, True
        else:
            self.formats, self.single, self.explicit = list(default), False, False
        self.base = base
        self._used: dict[str, int] = {}
        (self.path.parent if self.single else self.path).mkdir(parents=True, exist_ok=True)

    def file(self, kind: str, extra: str = "") -> Path:
        """Path for the next file of *kind* (``json csv nc png zarr``); *extra* names a part."""
        ext = {"fig": "png"}.get(kind, kind)
        if self.single:
            n = self._used.get(kind, 0)
            self._used[kind] = n + 1
            if n == 0:
                return self.path
            return self.path.with_name(f"{self.path.stem}_{extra or n}{self.path.suffix}")
        tail = f"_{extra}" if extra else ""
        return self.path / f"{self.base}{tail}.{ext}"


def volume_stem(volume: Any) -> str:
    """File-name stem for outputs derived from *volume* (``KTLX20130520_201643_V06``)."""
    path = getattr(volume, "path", None)
    return Path(path).name.split(".")[0] if path else str(getattr(volume, "name", "radar"))


def _slug(text: str) -> str:
    import re

    text = re.sub(r"\(\d+\)", "", text)  # "Storm cells (23)" -> "Storm cells"
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower() or "x"


# ---------------------------------------------------------------------------
# matplotlib helpers
# ---------------------------------------------------------------------------


def _pyplot() -> Any:
    from engine.exporter import _pyplot as exporter_pyplot

    return exporter_pyplot()


def resolve_cmap(name: str | None, fallback: str = "viridis") -> Any:
    """A matplotlib colormap from a name used by the engine (cmweather names included)."""
    plt = _pyplot()
    # registers NWSRef, NWSVel, PrecipRate, pyart_* ... as matplotlib colormaps
    with contextlib.suppress(ImportError):
        import cmweather  # noqa: F401
    for candidate in (name, f"pyart_{name}" if name else None, fallback):
        if not candidate:
            continue
        try:
            return plt.get_cmap(candidate)
        except ValueError:
            continue
    return plt.get_cmap("viridis")


def _ring_interval_km(max_range_km: float) -> float:
    from engine.exporter import _pick_ring_interval_km

    return _pick_ring_interval_km(max_range_km)


def plot_ppi(
    values: np.ndarray,
    azimuth: np.ndarray,
    range_m: np.ndarray,
    *,
    title: str | None = None,
    units: str = "",
    label: str = "",
    vmin: float | None = None,
    vmax: float | None = None,
    cmap: str | None = None,
    categories: Sequence[dict[str, Any]] | None = None,
    overlays: Iterable[dict[str, Any]] | None = None,
    max_range_km: float | None = None,
    rings: bool = True,
    colorbar: bool = True,
    ax: plt.Axes | None = None,
    figsize: tuple[float, float] = (6.5, 6.0),
) -> plt.Figure:
    """Draw one polar field as a PPI (km east / north of the radar) and return the Figure.

    ``categories`` (``[{"value", "label", "color"}, ...]``) draws a categorical field with a
    legend instead of a colour bar. ``overlays`` are the module overlay features (polygons,
    points, lines in a polar or xy frame).
    """
    from matplotlib.colors import BoundaryNorm, ListedColormap
    from matplotlib.patches import Patch

    from engine.exporter import _polar_edges

    plt = _pyplot()
    vals = np.asarray(values, dtype=np.float64)
    range_km = np.asarray(range_m, dtype=np.float64) / 1000.0
    az_edges, range_edges = _polar_edges(np.asarray(azimuth, dtype=np.float64), range_km)
    r_grid, az_grid = np.meshgrid(range_edges, az_edges)
    x, y = r_grid * np.sin(az_grid), r_grid * np.cos(az_grid)

    created = ax is None
    if ax is None:
        fig, ax = plt.subplots(figsize=figsize, dpi=110)
    else:
        fig = ax.figure
    legend_handles = None
    norm = None
    if categories:
        cats = sorted(categories, key=lambda c: c["value"])
        colors = [c.get("color") or "#888888" for c in cats]
        cm = ListedColormap(colors)
        bounds = [float(c["value"]) - 0.5 for c in cats] + [float(cats[-1]["value"]) + 0.5]
        norm = BoundaryNorm(bounds, cm.N) if len(bounds) > 1 and bounds == sorted(bounds) else None
        legend_handles = [
            Patch(color=col, label=str(c["label"])) for c, col in zip(cats, colors, strict=True)
        ]
        mesh = ax.pcolormesh(x, y, vals, cmap=cm, norm=norm, shading="flat", rasterized=True)
    else:
        finite = vals[np.isfinite(vals)]
        if vmin is None:
            vmin = float(np.percentile(finite, 2)) if finite.size else 0.0
        if vmax is None:
            vmax = float(np.percentile(finite, 98)) if finite.size else 1.0
        if vmax <= vmin:
            vmax = vmin + 1.0
        mesh = ax.pcolormesh(
            x,
            y,
            vals,
            cmap=resolve_cmap(cmap),
            vmin=vmin,
            vmax=vmax,
            shading="flat",
            rasterized=True,
        )
    lim = float(max_range_km) if max_range_km else float(np.nanmax(range_km) or 1.0)
    if not max_range_km:  # crop to the farthest gate that holds data
        has_data = np.isfinite(vals).any(axis=0)
        if has_data.any():
            lim = min(lim, float(range_km[np.nonzero(has_data)[0][-1]]) * 1.05 + 5.0)
    if not math.isfinite(lim) or lim <= 0:
        lim = 1.0
    ax.set_aspect("equal")
    ax.set_xlim(-lim, lim)
    ax.set_ylim(-lim, lim)
    ax.set_xlabel("East-West distance (km)")
    ax.set_ylabel("North-South distance (km)")
    if rings:
        step = _ring_interval_km(lim)
        theta = np.linspace(0.0, 2.0 * np.pi, 361)
        for r in np.arange(step, lim * 1.001, step):
            ax.plot(r * np.cos(theta), r * np.sin(theta), color="gray", lw=0.5, ls="--", alpha=0.5)
        ax.axhline(0, color="gray", lw=0.3, alpha=0.4)
        ax.axvline(0, color="gray", lw=0.3, alpha=0.4)
    for feature in overlays or []:
        _draw_overlay(ax, feature)
    if legend_handles is not None:
        ax.legend(
            handles=legend_handles,
            loc="center left",
            bbox_to_anchor=(1.01, 0.5),
            fontsize=8,
            frameon=False,
        )
    elif colorbar:
        cbar = fig.colorbar(mesh, ax=ax, pad=0.02, fraction=0.046)
        text = label or ""
        if units:
            from engine.cfwrite import normalize_units

            # the file's free-text spelling ("meters per seconds") in UDUNITS form ("m s-1")
            units = normalize_units(units)
        if units and units != "1":
            text = f"{text} ({units})" if text else units
        if text:
            cbar.set_label(text)
    if title:
        ax.set_title(title, fontsize=10)
    if created:
        fig.tight_layout()
    return fig


def _draw_overlay(ax: plt.Axes, feature: dict[str, Any]) -> None:
    """Draw one module overlay feature (``polygon`` / ``point`` / ``line``) in km."""
    coords = np.asarray(feature.get("coords") or [], dtype=np.float64)
    if coords.size == 0:
        return
    if feature.get("frame", "polar") == "polar":
        az, rng = np.radians(coords[:, 0]), coords[:, 1] / 1000.0
        xs, ys = rng * np.sin(az), rng * np.cos(az)
    else:
        xs, ys = coords[:, 0] / 1000.0, coords[:, 1] / 1000.0
    color = feature.get("color") or "black"
    kind = feature.get("kind")
    if kind == "point":
        ax.plot(xs, ys, marker="o", ms=4, color=color, ls="none")
    elif kind == "polygon":
        ax.fill(xs, ys, facecolor=color, alpha=float(feature.get("fill") or 0.1), lw=0)
        ring_x, ring_y = np.append(xs, xs[0]), np.append(ys, ys[0])
        ax.plot(ring_x, ring_y, color="white", lw=2.4, alpha=0.9)  # halo: readable on any colour
        ax.plot(ring_x, ring_y, color=color, lw=1.0)
    else:
        ax.plot(xs, ys, color=color, lw=1.2)
    label = feature.get("label")
    if label:
        ax.annotate(str(label), (float(xs[0]), float(ys[0])), fontsize=7, color=color)


def _dataframe(rows: list[dict[str, Any]], columns: list[str] | None = None) -> pd.DataFrame:
    """A DataFrame of *rows* with numeric columns converted (None -> NaN)."""
    import pandas as pd

    df = pd.DataFrame(rows)
    if columns:
        ordered = [c for c in columns if c in df.columns]
        df = df[ordered + [c for c in df.columns if c not in ordered]]
    for col in df.columns:
        if len(df) and all(isinstance(v, str) for v in df[col]):
            continue  # text columns stay text (an empty string must not become NaN)
        with contextlib.suppress(ValueError, TypeError):
            df[col] = pd.to_numeric(df[col])
    return df


# ---------------------------------------------------------------------------
# Module result
# ---------------------------------------------------------------------------


class ModuleResult:
    """Output of :meth:`engine.api.Volume.run`.

    Attributes
    ----------
    module : str
        Module id, e.g. ``"radarx_hid"``.
    title : str
        Human-readable module title.
    sweep : int or None
        The sweep the module ran on; ``None`` when it ran on all sweeps (then the fields are
        stored on the volume as derived variables, see :attr:`derived`).
    params : dict
        The parameters actually used (defaults filled in).
    elapsed_ms : float
        Wall-clock run time.
    fields : dict[str, xarray.DataArray]
        Gridded products on the sweep's ``(azimuth, range)`` grid (single-sweep runs).
    derived : list[str]
        Names of the derived variables created by an all-sweeps run (``"radarx_hid:..."``).
    skipped : list[dict]
        Sweeps an all-sweeps run could not process, each with a ``reason``.
    notes, warnings : list[str]
        Data-source notes (e.g. "velocity taken from sweep 1") and field warnings.

    Examples
    --------
    >>> res = vol.run("storm_detection", sweep=0, threshold_dbz=40)   # doctest: +SKIP
    >>> res.scalars["Cells found"]                                    # doctest: +SKIP
    >>> res.table.head()                                              # doctest: +SKIP
    >>> res.plot()                                                    # doctest: +SKIP
    """

    def __init__(
        self,
        *,
        module: str,
        title: str,
        sweep: int | None,
        params: dict[str, Any],
        blocks: list[dict[str, Any]],
        fields: dict[str, xr.DataArray],
        elapsed_ms: float,
        notes: Sequence[str] = (),
        warnings: Sequence[str] = (),
        derived: Sequence[str] = (),
        skipped: Sequence[dict[str, Any]] = (),
        summaries: Sequence[dict[str, Any]] = (),
        volume: Volume | None = None,
    ) -> None:
        self.module = module
        self.title = title
        self.sweep = sweep
        self.params = params
        self.blocks = blocks
        self.fields = fields
        self.elapsed_ms = elapsed_ms
        self.notes = list(notes)
        self.warnings = list(warnings)
        self.derived = list(derived)
        self.skipped = list(skipped)
        self.summaries = list(summaries)
        self.save_notes: list[str] = []
        self._volume = volume

    def __repr__(self) -> str:
        where = "all sweeps" if self.sweep is None else f"sweep {self.sweep}"
        parts = [f"{self.module}", where, f"{self.elapsed_ms:.0f} ms"]
        if self.fields:
            parts.append("fields=" + str(list(self.fields)))
        if self.derived:
            parts.append("derived=" + str(self.derived))
        if self.tables:
            parts.append("tables=" + str(list(self.tables)))
        if self.skipped:
            parts.append(f"{len(self.skipped)} sweep(s) skipped")
        return f"<ModuleResult {' | '.join(parts)}>"

    # -- structured access ------------------------------------------------------------------

    @property
    def scalars(self) -> dict[str, Any]:
        """All key/value summaries of the module as one ``{label: value}`` dict."""
        out: dict[str, Any] = {}
        for block in self.blocks:
            if block.get("type") == "scalars":
                for item in block.get("items") or []:
                    if isinstance(item, dict) and "label" in item:
                        out[str(item["label"])] = item.get("value")
        return out

    @property
    def tables(self) -> dict[str, pd.DataFrame]:
        """Every table block as a DataFrame, keyed by its title."""
        out: dict[str, pd.DataFrame] = {}
        for block in self.blocks:
            if block.get("type") != "table":
                continue
            cols = [c["key"] if isinstance(c, dict) else str(c) for c in block.get("columns", [])]
            df = _dataframe(list(block.get("rows") or []), cols)
            units = {
                c["key"]: c["unit"]
                for c in block.get("columns", [])
                if isinstance(c, dict) and c.get("unit")
            }
            df.attrs["units"] = units
            out[str(block.get("title") or f"table{len(out) + 1}")] = df
        return out

    @property
    def table(self) -> pd.DataFrame:
        """The first table (an empty DataFrame if the module produced none)."""
        import pandas as pd

        tables = self.tables
        return next(iter(tables.values())) if tables else pd.DataFrame()

    @property
    def text(self) -> str:
        """The text blocks (notes, explanations) joined into one string."""
        return "\n\n".join(
            str(b.get("body", "")) for b in self.blocks if b.get("type") == "text" and b.get("body")
        )

    @property
    def overlays(self) -> list[dict[str, Any]]:
        """All overlay features (polygons, points, lines) of the module."""
        return [f for b in self.blocks if b.get("type") == "overlay" for f in b.get("features", [])]

    @property
    def summary(self) -> pd.DataFrame:
        """All-sweeps runs: one row of scalar results per sweep (empty for a single sweep)."""
        rows = [{"sweep": s["sweep"], **(s.get("blocks_scalars") or {})} for s in self.summaries]
        return _dataframe(rows, ["sweep"])

    def field(self, name: str | None = None) -> xr.DataArray:
        """One gridded product by name (default: the first)."""
        if not self.fields:
            if self.derived:
                raise KeyError(
                    "This run covered all sweeps; its fields are derived variables of the "
                    f"volume ({', '.join(self.derived)}). Use vol.field(name, sweep)."
                )
            raise KeyError(f"{self.module} produced no gridded field")
        if name is None:
            return next(iter(self.fields.values()))
        if name in self.fields:
            return self.fields[name]
        lowered = [k for k in self.fields if k.lower().startswith(name.lower())]
        if len(lowered) == 1:
            return self.fields[lowered[0]]
        raise KeyError(f"No field '{name}'. Fields: {list(self.fields)}")

    def to_dataset(self, sweep: int | None = None) -> xr.Dataset:
        """The gridded products as an ``xarray.Dataset`` (variable names are slugs).

        For an all-sweeps run, returns the volume's dataset of *sweep* (default 0) with the
        derived variables added.
        """
        from engine.derived import slug

        if self.sweep is None:
            if self._volume is None:
                raise ValueError("this result is not attached to a volume")
            return self._volume.to_dataset(sweep or 0, include_derived=self.derived or True)
        data = {slug(k): v.rename(slug(k)) for k, v in self.fields.items()}
        ds = xr.Dataset(data)
        ds.attrs.update(module=self.module, sweep=int(self.sweep), params=str(self.params))
        return ds

    # -- plotting ---------------------------------------------------------------------------

    def plot(
        self,
        field: str | None = None,
        *,
        sweep: int | None = None,
        overlays: bool = True,
        ax: plt.Axes | None = None,
        **kwargs: Any,
    ) -> plt.Figure:
        """PPI figure of one gridded product (with the module's overlay outlines).

        Parameters
        ----------
        field : str, optional
            Field name (default: the first field, or the first derived variable).
        sweep : int, optional
            All-sweeps runs only: which sweep to draw (default 0).
        overlays : bool
            Draw the module's polygons / points / lines.
        ax : matplotlib Axes, optional
            Draw into an existing axes.
        **kwargs
            Passed to :func:`plot_ppi` (``vmin``, ``vmax``, ``cmap``, ``max_range_km`` ...).

        Returns
        -------
        matplotlib.figure.Figure
        """
        if self.sweep is None:
            if self._volume is None or not self.derived:
                raise ValueError("nothing to plot: the run produced no derived variable")
            name = field if field in self.derived else self.derived[0]
            if sweep is None:
                sweep = self._volume.derived_meta(name)["sweeps"][0]
            return self._volume.plot(name, sweep=sweep, ax=ax, **kwargs)
        if not self.fields:
            raise ValueError(f"{self.module} produced no gridded field to plot")
        da = self.field(field)
        block = next(
            (b for b in self.blocks if b.get("type") == "field" and b.get("name") == da.name),
            {},
        )
        opts: dict[str, Any] = {
            "units": block.get("units") or da.attrs.get("units", ""),
            "label": block.get("title") or str(da.name),
            "vmin": block.get("vmin"),
            "vmax": block.get("vmax"),
            "cmap": block.get("cmap"),
            "categories": block.get("categories"),
            "overlays": self.overlays if overlays else None,
            "title": f"{self.title} | {da.name} | sweep {self.sweep}",
        }
        opts.update(kwargs)
        return plot_ppi(
            np.asarray(da.values), da["azimuth"].values, da["range"].values, ax=ax, **opts
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe summary: parameters, scalars, tables, field list, derived names, notes."""
        fields = [
            {
                "name": name,
                "units": str(da.attrs.get("units", "")),
                "shape": list(da.shape),
                "valid_fraction": float(da.notnull().mean()) if da.size else 0.0,
            }
            for name, da in self.fields.items()
        ]
        return jsonable(
            {
                "module": self.module,
                "title": self.title,
                "file": getattr(self._volume, "path", None),
                "sweep": self.sweep,
                "all_sweeps": self.sweep is None,
                "params": self.params,
                "elapsed_ms": self.elapsed_ms,
                "scalars": self.scalars,
                "tables": {k: df.to_dict("records") for k, df in self.tables.items()},
                "fields": fields,
                "derived": self.derived,
                "skipped": self.skipped,
                "notes": self.notes,
                "warnings": self.warnings,
                "text": self.text,
            }
        )

    def save(
        self,
        out: Any,
        formats: Sequence[str] | None = None,
        *,
        sweep: int | None = None,
        dpi: int = 150,
    ) -> list[Path]:
        """Write the result: ``json`` (summary), ``csv`` (tables), ``nc``, ``png`` (figures).

        ``out`` is a file (the extension picks the kind; ``.png`` / ``.svg`` / ``.pdf`` draw the
        first field) or a folder (default: json, csv and nc). For an all-sweeps run the NetCDF is
        the whole volume with the derived variable, and *sweep* (default: the first processed
        sweep) is the one drawn.

        Returns the paths written; what could not be written (no table, no field) is listed in
        ``self.save_notes``.
        """
        self.save_notes: list[str] = []
        written: list[Path] = []
        vol = self._volume
        stem = volume_stem(vol) if vol is not None else "radar"
        where = "all" if self.sweep is None else f"sweep{self.sweep}"
        fig_sweep = sweep
        if fig_sweep is None:
            fig_sweep = self.sweep if self.sweep is not None else 0
            if self.sweep is None and self.derived and vol is not None:
                fig_sweep = vol.derived_meta(self.derived[0])["sweeps"][0]
        for target in as_list(out):
            dest = Destination(
                target, f"{stem}_{self.module}_{where}", formats, ("json", "csv", "nc")
            )
            for kind in dest.formats:
                if kind == "json":
                    path = dest.file("json")
                    path.write_text(dumps(self.to_dict()), encoding="utf-8")
                    written.append(path)
                elif kind == "csv":
                    tables = self.tables
                    if not tables and dest.explicit:
                        self.save_notes.append(f"{self.module} produced no table; no CSV written")
                    for title, df in tables.items():
                        path = dest.file("csv", _slug(title))
                        df.to_csv(path, index=False)
                        written.append(path)
                elif kind == "nc":
                    if self.sweep is None and vol is not None:
                        written.append(
                            vol.export("netcdf", dest.file("nc"), include_derived=self.derived)
                        )
                    elif self.fields:
                        path = dest.file("nc")
                        with hdf5_guard.native_section():
                            self.to_dataset().to_netcdf(path)
                        written.append(path)
                    elif dest.explicit:
                        self.save_notes.append(
                            f"{self.module} produced no gridded field; no NetCDF written"
                        )
                elif kind in ("png", "fig"):
                    names = self.derived if self.sweep is None else list(self.fields)
                    if not names and dest.explicit:
                        self.save_notes.append(
                            f"{self.module} produced no gridded field; no figure written"
                        )
                    for name in names:
                        fig = (
                            self.plot(name, sweep=fig_sweep)
                            if self.sweep is None
                            else self.plot(name)
                        )
                        written.append(save_figure(fig, dest.file("fig", _slug(name)), dpi))
                else:
                    raise XrdArgumentError(
                        f"Cannot write '{kind}' for a module result (json, csv, nc, png)"
                    )
        return written


# ---------------------------------------------------------------------------
# Pipeline result
# ---------------------------------------------------------------------------


class PipelineResult:
    """Output of :meth:`engine.api.Volume.pipeline`.

    Attributes
    ----------
    recipe : dict
        The recipe that ran (name, target, steps with parameters).
    name : str
        Name of the derived variable holding the result (``"pipeline:<recipe>_<role>"``).
    derived : list[str]
        All derived variables stored (more than one when the recipe has ``apply_to_all``).
    sweeps : list[int]
        Sweeps the pipeline produced data for.
    skipped : list[dict]
        Sweeps that could not be processed, with the reason.
    steps : pandas.DataFrame
        Per-step statistics summed over the sweeps (gates removed / modified / filled ...).
    elapsed_ms : float

    Examples
    --------
    >>> res = vol.pipeline("Reflectivity QC", target="DBZH", all_sweeps=True)  # doctest: +SKIP
    >>> res.steps[["title", "removed", "valid_pct"]]                           # doctest: +SKIP
    >>> res.plot(sweep=0)                                                      # doctest: +SKIP
    """

    def __init__(
        self,
        *,
        recipe: dict[str, Any],
        name: str | None,
        derived: Sequence[str],
        sweeps: Sequence[int],
        skipped: Sequence[dict[str, Any]],
        step_rows: list[dict[str, Any]],
        per_sweep: Sequence[dict[str, Any]],
        elapsed_ms: float,
        volume: Volume,
    ) -> None:
        self.recipe = recipe
        self.name = name
        self.derived = list(derived)
        self.sweeps = list(sweeps)
        self.skipped = list(skipped)
        self.per_sweep = list(per_sweep)
        self.elapsed_ms = elapsed_ms
        self._step_rows = step_rows
        self._volume = volume

    def __repr__(self) -> str:
        return (
            f"<PipelineResult {self.recipe.get('name')!r} -> {self.name} | "
            f"{len(self.sweeps)} sweep(s), {len(self.skipped)} skipped | "
            f"{self.elapsed_ms:.0f} ms>"
        )

    @property
    def steps(self) -> pd.DataFrame:
        """Per-step statistics as a DataFrame."""
        cols = [
            "index",
            "step_id",
            "title",
            "status",
            "removed",
            "modified",
            "filled",
            "valid",
            "valid_pct",
            "ms",
            "reason",
        ]
        return _dataframe(
            [{k: v for k, v in r.items() if k != "notes"} for r in self._step_rows], cols
        )

    def to_dataset(self, sweep: int | None = None) -> xr.Dataset:
        """Dataset of *sweep* (default: the first processed one) with the result added."""
        return self._volume.to_dataset(
            self.sweeps[0] if sweep is None else sweep, include_derived=self.derived
        )

    def plot(self, sweep: int | None = None, **kwargs: Any) -> plt.Figure:
        """Before / after PPI of one sweep (same colour scale on both panels)."""
        plt = _pyplot()
        sweep = self.sweeps[0] if sweep is None else sweep
        if sweep not in self.sweeps:
            raise ValueError(f"The pipeline has no result on sweep {sweep} (has {self.sweeps})")
        target = str(self.recipe.get("target"))
        fig, axes = plt.subplots(1, 2, figsize=(12.5, 6.0), dpi=100)
        before = self._volume.field(target, sweep)
        after = self._volume.field(str(self.name), sweep)
        meta = self._volume.derived_meta(str(self.name))
        vmin = kwargs.pop("vmin", meta.get("vmin"))
        vmax = kwargs.pop("vmax", meta.get("vmax"))
        cmap = kwargs.pop("cmap", meta.get("cmap"))
        for ax, da, label in ((axes[0], before, "Before"), (axes[1], after, "After")):
            plot_ppi(
                np.asarray(da.values),
                da["azimuth"].values,
                da["range"].values,
                ax=ax,
                title=f"{label}: {target} sweep {sweep}"
                if label == "Before"
                else f"{label}: {self.recipe.get('name')}",
                units=str(meta.get("units") or da.attrs.get("units", "")),
                label=target,
                vmin=vmin,
                vmax=vmax,
                cmap=cmap,
                **kwargs,
            )
        fig.tight_layout()
        return fig

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe summary: recipe, derived names, per-step statistics, skipped sweeps."""
        return jsonable(
            {
                "recipe": self.recipe,
                "file": getattr(self._volume, "path", None),
                "derived": self.derived,
                "name": self.name,
                "sweeps": self.sweeps,
                "skipped": self.skipped,
                "steps": self.steps.to_dict("records"),
                "elapsed_ms": self.elapsed_ms,
            }
        )

    def save(
        self,
        out: Any,
        formats: Sequence[str] | None = None,
        *,
        sweep: int | None = None,
        dpi: int = 150,
    ) -> list[Path]:
        """Write the result: ``nc`` (volume with the result), ``json``, ``png`` (before / after).

        ``out`` is a file (extension picks the kind) or a folder (default: nc and json).
        *sweep* is the sweep drawn in the figure (default: the first processed).
        """
        written: list[Path] = []
        stem = volume_stem(self._volume)
        base = f"{stem}_{_slug(str(self.recipe.get('name')))[:40]}"
        fig_sweep = self.sweeps[0] if sweep is None else sweep
        for target in as_list(out):
            dest = Destination(target, base, formats, ("nc", "json"))
            for kind in dest.formats:
                if kind == "json":
                    path = dest.file("json")
                    path.write_text(dumps(self.to_dict()), encoding="utf-8")
                    written.append(path)
                elif kind == "nc":
                    written.append(
                        self._volume.export("netcdf", dest.file("nc"), include_derived=self.derived)
                    )
                elif kind in ("png", "fig"):
                    written.append(
                        save_figure(
                            self.plot(fig_sweep), dest.file("fig", f"sweep{fig_sweep}"), dpi
                        )
                    )
                else:
                    raise XrdArgumentError(
                        f"Cannot write '{kind}' for a pipeline result (nc, json, png)"
                    )
        return written


# ---------------------------------------------------------------------------
# Mosaic
# ---------------------------------------------------------------------------


class Mosaic:
    """A multi-radar mosaic (:func:`engine.api.mosaic`).

    The product is a regular grid (rows north to south, Web-Mercator spaced) in :attr:`image`;
    NaN marks cells where no radar measured an echo.

    Attributes
    ----------
    image : numpy.ndarray
        2-D float32 product, ``(ny, nx)``.
    bbox : tuple
        ``(west, south, east, north)`` in degrees.
    product, variable, units : str
    height_m : float or None
        CAPPI height above sea level.
    radars : list[dict]
        Per radar: name, position, volume time, coverage fraction, merge share, bias.
    stats : dict
        Min / max / mean of the product.
    warnings : list[str]

    Examples
    --------
    >>> mos = api.mosaic([vol1, vol2], product="composite", variable="DBZH")   # doctest: +SKIP
    >>> mos.save_png("mosaic.png")       # also writes mosaic.pgw and mosaic.prj   # doctest: +SKIP
    >>> mos.save_netcdf("mosaic.nc")                                              # doctest: +SKIP
    """

    def __init__(self, record: mosaic_engine.MosaicRecord) -> None:
        self._rec = record

    @property
    def record(self) -> mosaic_engine.MosaicRecord:
        """The engine record (for advanced use)."""
        return self._rec

    def __repr__(self) -> str:
        r = self._rec
        ny, nx = r.image.shape
        height = f", {r.height_m:g} m" if r.height_m is not None else ""
        return (
            f"<Mosaic {r.product} of {r.variable}{height} | {nx}x{ny} @ {r.resolution_m:g} m | "
            f"{len(r.radars)} radar(s)>"
        )

    image = property(lambda self: self._rec.image)
    bbox = property(lambda self: self._rec.bbox)
    product = property(lambda self: self._rec.product)
    variable = property(lambda self: self._rec.variable)
    units = property(lambda self: self._rec.units)
    height_m = property(lambda self: self._rec.height_m)
    stats = property(lambda self: self._rec.stats)
    warnings = property(lambda self: list(self._rec.warnings))
    png = property(lambda self: self._rec.png)

    @property
    def radars(self) -> list[dict[str, Any]]:
        return [r.as_dict() for r in self._rec.radars]

    @property
    def analysis_time(self) -> str | None:
        return self._rec.analysis_time

    def header(self) -> dict[str, Any]:
        """JSON-able description (grid, legend, radars, merge settings, warnings)."""
        return self._rec.header()

    def to_dataset(self) -> xr.Dataset:
        """CF-style ``(lat, lon)`` dataset of the product plus per-radar metadata."""
        from engine import mosaic as mosaic_engine

        return mosaic_engine.to_dataset(self._rec)

    def save_netcdf(self, path: str | Path) -> Path:
        """Write the CF NetCDF; returns the path."""
        from engine import mosaic as mosaic_engine

        target = _prepare_target(path)
        mosaic_engine.write_netcdf(self._rec, target)
        return target

    def save_png(self, path: str | Path) -> Path:
        """Write the colour-mapped PNG plus ``.pgw`` (world file) and ``.prj`` (EPSG:3857).

        Open the PNG in QGIS / GDAL and it lands in the right place.
        """
        from engine import mosaic as mosaic_engine

        target = _prepare_target(path)
        target.write_bytes(self._rec.png)
        target.with_suffix(".pgw").write_text(mosaic_engine.world_file(self._rec), encoding="ascii")
        target.with_suffix(".prj").write_text(mosaic_engine.projection_wkt(), encoding="ascii")
        return target

    def plot(
        self,
        ax: plt.Axes | None = None,
        *,
        title: str | None = None,
        radars: bool = True,
        **kwargs: Any,
    ) -> plt.Figure:
        """Map of the mosaic (longitude / latitude) with the radar sites marked."""
        plt = _pyplot()
        r = self._rec
        ds = self.to_dataset()
        name = f"{r.variable}_{r.product}"
        if ax is None:
            fig, ax = plt.subplots(figsize=(8.5, 6.5), dpi=110)
        else:
            fig = ax.figure
        opts = {"vmin": r.vmin, "vmax": r.vmax, "cmap": resolve_cmap(r.cmap)}
        opts.update(kwargs)
        mesh = ax.pcolormesh(
            ds["lon"].values, ds["lat"].values, ds[name].values, shading="auto", **opts
        )
        lat0 = 0.5 * (r.bbox[1] + r.bbox[3])
        ax.set_aspect(1.0 / max(math.cos(math.radians(lat0)), 0.05))
        ax.set_xlim(r.bbox[0], r.bbox[2])
        ax.set_ylim(r.bbox[1], r.bbox[3])
        ax.set_xlabel("Longitude (deg)")
        ax.set_ylabel("Latitude (deg)")
        ax.grid(True, lw=0.3, alpha=0.4)
        if radars:
            for rad in r.radars:
                ax.plot(rad.longitude, rad.latitude, marker="^", color="black", ms=6)
                ax.annotate(
                    rad.name,
                    (rad.longitude, rad.latitude),
                    xytext=(4, 4),
                    textcoords="offset points",
                    fontsize=8,
                )
        cbar = fig.colorbar(mesh, ax=ax, pad=0.02, fraction=0.046)
        from engine.cfwrite import normalize_units

        cb_units = normalize_units(r.units)
        cbar.set_label(f"{r.variable} ({cb_units})" if cb_units else r.variable)
        if title is None:
            height = f" at {r.height_m:g} m" if r.product == "cappi" and r.height_m else ""
            when = f" | {r.analysis_time}" if r.analysis_time else ""
            title = f"{r.product} of {r.variable}{height}{when}"
        ax.set_title(title, fontsize=10)
        fig.tight_layout()
        return fig

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe description (grid, radars, statistics, warnings; no colour legend)."""
        header = jsonable(self._rec.header())
        header.pop("legend", None)
        return header

    def save(self, out: Any) -> list[Path]:
        """Write each path in *out* by extension: ``.png`` (+ ``.pgw`` / ``.prj``) or ``.nc``."""
        written: list[Path] = []
        for target in as_list(out):
            suffix = Path(target).suffix.lower()
            if suffix == ".png":
                path = self.save_png(target)
                written += [path, path.with_suffix(".pgw"), path.with_suffix(".prj")]
            elif suffix == ".nc":
                written.append(self.save_netcdf(target))
            else:
                raise XrdArgumentError(
                    f"Cannot write a mosaic to '{target}': use .png (picture + world file) or .nc"
                )
        return written


def _prepare_target(path: str | Path) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


# ---------------------------------------------------------------------------
# Multi-Doppler winds
# ---------------------------------------------------------------------------


class Winds:
    """A multi-Doppler wind analysis (:func:`engine.api.winds`).

    Attributes
    ----------
    x, y : numpy.ndarray
        Metres east / north of :attr:`origin`.
    z : numpy.ndarray
        Heights above sea level (m).
    origin : tuple
        ``(latitude, longitude)`` of the grid origin.
    radars : list[dict]
        The radars that went into the analysis.
    summary : dict
        Method, grid size, statistics and timing reported by the engine.
    warnings : list[str]

    Examples
    --------
    >>> w = api.winds([vol1, vol2], method="analytic", extent=60, dz_m=500)   # doctest: +SKIP
    >>> w.plot(level_km=3.0)                                                  # doctest: +SKIP
    >>> w.save_netcdf("winds.nc")                                             # doctest: +SKIP
    """

    def __init__(self, record: mdop_engine.WindRecord) -> None:
        self._rec = record

    @property
    def record(self) -> mdop_engine.WindRecord:
        """The engine record (for advanced use)."""
        return self._rec

    def __repr__(self) -> str:
        r = self._rec
        nz, ny, nx = r.fields.shape[1:]
        return (
            f"<Winds {r.summary.get('method', '?')} | {nx}x{ny}x{nz} grid | "
            f"{len(r.radars)} radar(s)>"
        )

    x = property(lambda self: self._rec.x)
    y = property(lambda self: self._rec.y)
    z = property(lambda self: self._rec.z)
    origin = property(lambda self: self._rec.origin)
    radars = property(lambda self: self._rec.radars)
    summary = property(lambda self: self._rec.summary)
    warnings = property(lambda self: list(self._rec.warnings))

    @property
    def fields(self) -> dict[str, np.ndarray]:
        """``{name: (z, y, x) float32 array}`` of u, v, w, speed, divergence, ..."""
        from engine.multidoppler import FIELD_ORDER

        return {name: self._rec.fields[i] for i, name in enumerate(FIELD_ORDER)}

    def to_dataset(self) -> xr.Dataset:
        """CF-flavoured Dataset: ``(z, y, x)`` fields with 2-D ``lon`` / ``lat`` coordinates."""
        from engine import multidoppler as mdop_engine

        return mdop_engine.to_dataset(self._rec)

    def save_netcdf(self, path: str | Path) -> Path:
        """Write the Dataset as compressed NetCDF4; returns the path."""
        from engine import multidoppler as mdop_engine

        target = _prepare_target(path)
        mdop_engine.write_netcdf(self._rec, target)
        return target

    def plot(
        self,
        level_km: float | None = None,
        field: str = "speed",
        *,
        stride: int | None = None,
        ax: plt.Axes | None = None,
        **kwargs: Any,
    ) -> plt.Figure:
        """Horizontal map at one level: a colour field with wind vectors.

        Parameters
        ----------
        level_km : float, optional
            Height above sea level (km); the nearest analysis level is drawn. Default: the
            level with the most valid wind vectors.
        field : str
            Field shown in colour (``"speed"``, ``"w"``, ``"vorticity"`` ...).
        stride : int, optional
            Draw every *stride*-th vector (default: about 25 per axis).
        """
        plt = _pyplot()
        r = self._rec
        fields = self.fields
        if field not in fields:
            raise KeyError(f"Unknown field '{field}'. Fields: {list(fields)}")
        u, v = fields["u"], fields["v"]
        if level_km is None:
            counts = np.isfinite(u).sum(axis=(1, 2))
            k = int(np.argmax(counts))
        else:
            k = int(np.argmin(np.abs(r.z - level_km * 1000.0)))
        shown = fields[field][k]
        if ax is None:
            fig, ax = plt.subplots(figsize=(7.5, 6.5), dpi=110)
        else:
            fig = ax.figure
        xk, yk = r.x / 1000.0, r.y / 1000.0
        diverging = field in {"w", "divergence", "vorticity", "u", "v"}
        finite = shown[np.isfinite(shown)]
        lim = float(np.percentile(np.abs(finite), 98)) if finite.size and diverging else None
        opts: dict[str, Any] = {"cmap": "RdBu_r" if diverging else "viridis"}
        if lim:
            opts.update(vmin=-lim, vmax=lim)
        elif finite.size:  # robust upper limit: a few wild gates must not wash out the map
            opts.update(vmin=float(finite.min()), vmax=float(np.percentile(finite, 98)))
        opts.update(kwargs)
        mesh = ax.pcolormesh(xk, yk, shown, shading="nearest", **opts)
        step = stride or max(1, round(max(len(xk), len(yk)) / 80))
        uk, vk = u[k][::step, ::step], v[k][::step, ::step]
        top = float(np.nanmax(np.hypot(uk, vk))) if np.isfinite(uk).any() else 1.0
        ax.quiver(
            xk[::step],
            yk[::step],
            uk,
            vk,
            color="black",
            width=0.0028,
            scale=max(top, 1.0) * 18.0,
            scale_units="width",
        )
        for rad in r.radars:  # sites in the grid's x/y frame, if the engine reports them
            if "x_km" in rad and "y_km" in rad:
                ax.plot(rad["x_km"], rad["y_km"], marker="^", color="red", ms=6)
        ax.set_aspect("equal")
        ax.set_xlabel("East of origin (km)")
        ax.set_ylabel("North of origin (km)")
        from engine.multidoppler import FIELD_INFO

        cbar = fig.colorbar(mesh, ax=ax, pad=0.02, fraction=0.046)
        cbar.set_label(f"{field} ({FIELD_INFO[field][0]})")
        ax.set_title(f"{self.summary.get('method', 'wind')} winds at {r.z[k] / 1000.0:.2f} km ASL")
        fig.tight_layout()
        return fig

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe description: grid, radars, statistics, warnings."""
        info = jsonable(self._rec.header())
        for key in ("fields", "shape"):
            info.pop(key, None)
        return {"method": self.summary.get("method"), **info}

    def save(
        self, out: Any, level_km: float | None = None, field: str = "speed", dpi: int = 150
    ) -> list[Path]:
        """Write each path in *out* by its extension.

        ``.nc`` is the NetCDF analysis, ``.png`` / ``.svg`` / ``.pdf`` a map of one level
        (*level_km* above sea level, default: the best-covered one) coloured by *field*, and
        ``.json`` the description.
        """
        written: list[Path] = []
        for target in as_list(out):
            suffix = Path(target).suffix.lower()
            if suffix == ".nc":
                written.append(self.save_netcdf(target))
            elif suffix in (".png", ".svg", ".pdf"):
                written.append(save_figure(self.plot(level_km, field), target, dpi))
            elif suffix == ".json":
                path = _prepare_target(target)
                path.write_text(dumps(self.to_dict()), encoding="utf-8")
                written.append(path)
            else:
                raise XrdArgumentError(
                    f"Cannot write winds to '{target}': use .nc, .png (map of one level) or .json"
                )
        return written


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------


def _iso_from_epoch(seconds: float | None) -> str | None:
    if seconds is None or not math.isfinite(seconds):
        return None
    stamp = np.datetime64(0, "s") + np.timedelta64(round(seconds), "s")
    return str(stamp) + "Z"


class CellsResult:
    """Storm cells found on one sweep (:meth:`engine.api.Volume.cells`).

    Behaves like a read-only sequence of :class:`engine.tracking.CellInfo`.

    Attributes
    ----------
    scan : engine.cells.CellScan
        The engine object (cells plus label image, geometry and time).
    backend : str
        ``"radarx"`` or ``"numpy"``: what actually ran.
    note : str
        Explanation when the requested backend was not used.
    time : str or None
        Scan time (UTC, ISO 8601) when the file carries ray times.

    Examples
    --------
    >>> cells = vol.cells(sweep=0, threshold_dbz=40, min_size_km2=20)   # doctest: +SKIP
    >>> cells.to_dataframe().sort_values("max_dbz", ascending=False)    # doctest: +SKIP
    >>> cells.plot()                                                    # doctest: +SKIP
    """

    def __init__(
        self,
        scan: cell_engine.CellScan,
        data: xr.DataArray,
        *,
        sweep: int,
        variable: str,
        threshold_dbz: float,
        min_size_km2: float,
    ) -> None:
        self.scan = scan
        self.data = data
        self.sweep = sweep
        self.variable = variable
        self.threshold_dbz = threshold_dbz
        self.min_size_km2 = min_size_km2

    def __len__(self) -> int:
        return len(self.scan)

    def __iter__(self) -> Any:
        return iter(self.scan)

    def __getitem__(self, index: int) -> Any:
        return self.scan[index]

    def __repr__(self) -> str:
        return f"<CellsResult {len(self)} cell(s) | sweep {self.sweep} | backend {self.backend}>"

    backend = property(lambda self: self.scan.backend)
    note = property(lambda self: self.scan.note)

    @property
    def time(self) -> str | None:
        return _iso_from_epoch(self.scan.time_s)

    def to_dataframe(self) -> pd.DataFrame:
        """One row per cell (id, position, area, peak / mean dBZ, motion, extras ...)."""
        from engine import cells as cell_engine

        return _dataframe(cell_engine.cell_rows(self.scan), ["id"])

    def plot(self, ax: plt.Axes | None = None, **kwargs: Any) -> plt.Figure:
        """The sweep with every cell outlined and labelled."""
        opts: dict[str, Any] = {
            "units": str(self.data.attrs.get("units", "")),
            "label": self.variable,
            "title": f"{len(self)} cell(s) | {self.variable} sweep {self.sweep} | "
            f">= {self.threshold_dbz:g} dBZ, >= {self.min_size_km2:g} km2",
            "overlays": cell_overlays(self.scan),
        }
        if self.variable.upper() in {"DBZH", "DBZ"} or str(self.data.attrs.get("units")) == "dBZ":
            opts.update(vmin=-10.0, vmax=70.0, cmap="NWSRef")
        opts.update(kwargs)
        return plot_ppi(
            np.asarray(self.data.values),
            self.data["azimuth"].values,
            self.data["range"].values,
            ax=ax,
            **opts,
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe description: sweep, backend, time and one record per cell."""
        return jsonable(
            {
                "sweep": self.sweep,
                "variable": self.variable,
                "threshold_dbz": self.threshold_dbz,
                "min_size_km2": self.min_size_km2,
                "backend": self.backend,
                "note": self.note,
                "time": self.time,
                "cells": self.to_dataframe().to_dict("records"),
            }
        )

    def save(self, out: Any, dpi: int = 150) -> list[Path]:
        """Write each path in *out* by its extension: ``.csv`` table, ``.json``, ``.png`` figure."""
        written: list[Path] = []
        for target in as_list(out):
            suffix = Path(target).suffix.lower()
            if suffix == ".csv":
                path = _prepare_target(target)
                self.to_dataframe().to_csv(path, index=False)
                written.append(path)
            elif suffix == ".json":
                path = _prepare_target(target)
                path.write_text(dumps(self.to_dict()), encoding="utf-8")
                written.append(path)
            elif suffix in (".png", ".svg", ".pdf"):
                written.append(save_figure(self.plot(), target, dpi))
            else:
                raise XrdArgumentError(f"Cannot write cells to '{target}': use .csv, .json or .png")
        return written


def cell_overlays(
    cells: Iterable[Any], colors: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Overlay polygons (and labels) for ``CellInfo`` objects, for :func:`plot_ppi`."""
    out = []
    for c in cells:
        out.append(
            {
                "id": c.id,
                "kind": "polygon",
                "frame": "polar",
                "coords": c.boundary_points,
                "label": c.id,
                "color": (colors or {}).get(c.id, "black"),
                "fill": 0.0,
            }
        )
    return out


class TrackingResult:
    """Cells tracked over several volumes (:func:`engine.api.track`).

    Attributes
    ----------
    tracks : engine.cells.TrackResult
        One :class:`engine.tracking.CellTrack` per cell lineage.
    scans : list[CellsResult]
        The cells found in each volume, in time order.
    motion : tuple or None
        Mean storm motion ``(u east, v north)`` in m/s used by the radarx backend.
    backend, note : str

    Examples
    --------
    >>> res = api.track([v1, v2, v3], variable="DBZH", threshold_dbz=40)   # doctest: +SKIP
    >>> res.to_dataframe()                                                  # doctest: +SKIP
    >>> res.plot()                                                          # doctest: +SKIP
    """

    def __init__(self, tracks: cell_engine.TrackResult, scans: list[CellsResult]) -> None:
        self.tracks = tracks
        self.scans = scans

    def __len__(self) -> int:
        return len(self.tracks)

    def __iter__(self) -> Any:
        return iter(self.tracks)

    def __repr__(self) -> str:
        return (
            f"<TrackingResult {len(self)} track(s) over {len(self.scans)} scan(s) | "
            f"backend {self.backend}>"
        )

    backend = property(lambda self: self.tracks.backend)
    note = property(
        lambda self: self.tracks.note or "; ".join(s.note for s in self.scans if s.note)
    )
    motion = property(lambda self: self.tracks.motion)
    times_s = property(lambda self: list(self.tracks.times_s))

    def to_dataframe(self) -> pd.DataFrame:
        """One row per track: lifetime, speed, direction, u / v, split / merge, last position."""
        rows = []
        for t in self.tracks:
            last = t.cell_history[-1] if t.cell_history else None
            first = t.cell_history[0] if t.cell_history else None
            rows.append(
                {
                    "track_id": t.track_id,
                    "lifetime": int(t.lifetime or len(t.cell_history)),
                    "speed_mps": round(t.speed_mps, 2),
                    "direction_deg": round(t.direction_deg, 1),
                    "u_mps": round(t.u_mps, 2),
                    "v_mps": round(t.v_mps, 2),
                    "status": t.status,
                    "split_from": t.split_from,
                    "merged_into": t.merged_into,
                    "first_lat": getattr(first, "lat", None),
                    "first_lon": getattr(first, "lon", None),
                    "last_lat": getattr(last, "lat", None),
                    "last_lon": getattr(last, "lon", None),
                    "max_dbz": max((c.max_dbz for c in t.cell_history), default=float("nan")),
                }
            )
        return _dataframe(rows, ["track_id"])

    def cells_dataframe(self) -> pd.DataFrame:
        """All cells of all scans, with the scan index and time."""
        rows = []
        for i, scan in enumerate(self.scans):
            df = scan.to_dataframe()
            df.insert(0, "scan", i)
            df.insert(1, "time", scan.time)
            rows.extend(df.to_dict("records"))
        return _dataframe(rows, ["scan", "time", "id"])

    def plot(self, ax: plt.Axes | None = None) -> plt.Figure:
        """Cell tracks as lines (km east / north of the radar) over the last scan's outlines."""
        plt = _pyplot()
        if ax is None:
            fig, ax = plt.subplots(figsize=(7.0, 6.5), dpi=110)
        else:
            fig = ax.figure
        cmap = plt.get_cmap("tab20")
        last = self.scans[-1] if self.scans else None
        if last is not None:
            for feature in cell_overlays(last.scan):
                feature["color"] = "lightgray"
                feature["label"] = None
                _draw_overlay(ax, feature)
        for i, t in enumerate(self.tracks):
            xs, ys = [], []
            for c in t.cell_history:
                az = math.radians(c.centroid_az)
                xs.append(c.centroid_range / 1000.0 * math.sin(az))
                ys.append(c.centroid_range / 1000.0 * math.cos(az))
            colour = cmap(i % 20)
            ax.plot(xs, ys, "-o", color=colour, ms=3, lw=1.4)
            if xs:
                ax.annotate(
                    t.track_id,
                    (xs[-1], ys[-1]),
                    xytext=(3, 3),
                    textcoords="offset points",
                    fontsize=7,
                )
        ax.plot(0, 0, marker="^", color="black")
        ax.set_aspect("equal")
        ax.set_xlabel("East-West distance (km)")
        ax.set_ylabel("North-South distance (km)")
        ax.grid(True, lw=0.3, alpha=0.4)
        motion = ""
        if self.motion is not None:
            from engine import cells as cell_engine

            speed, from_deg = cell_engine.uv_to_speed_dir(*self.motion)
            motion = f" | motion {speed:.1f} m/s from {from_deg:.0f} deg"
        ax.set_title(f"{len(self)} track(s) over {len(self.scans)} scans{motion}", fontsize=10)
        fig.tight_layout()
        return fig

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe description: backend, motion, scan times, tracks and cells."""
        return jsonable(
            {
                "backend": self.backend,
                "note": self.note,
                "motion": self.motion,
                "times_s": self.times_s,
                "tracks": self.to_dataframe().to_dict("records"),
                "cells": self.cells_dataframe().to_dict("records"),
            }
        )

    def save(self, out: Any, tracks: str | Path | None = None, dpi: int = 150) -> list[Path]:
        """Write by extension: ``.csv`` (all cells), ``.json``, ``.png`` (track map).

        ``tracks`` additionally writes the track table as CSV.
        """
        written: list[Path] = []
        for target in as_list(out):
            suffix = Path(target).suffix.lower()
            path = _prepare_target(target)
            if suffix == ".csv":
                self.cells_dataframe().to_csv(path, index=False)
                written.append(path)
            elif suffix == ".json":
                path.write_text(dumps(self.to_dict()), encoding="utf-8")
                written.append(path)
            elif suffix in (".png", ".svg", ".pdf"):
                written.append(save_figure(self.plot(), target, dpi))
            else:
                raise XrdArgumentError(
                    f"Cannot write tracks to '{target}': use .csv, .json or .png"
                )
        if tracks is not None:
            path = _prepare_target(tracks)
            self.to_dataframe().to_csv(path, index=False)
            written.append(path)
        return written
