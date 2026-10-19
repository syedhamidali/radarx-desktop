"""Publication-quality radar data exporter — matplotlib figures + data formats."""

from __future__ import annotations

import contextlib
import io
import logging
import os
import re
import shutil
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import xarray as xr

from engine import cfwrite, provenance, rhi, valuemask
from engine.reader import (
    collapse_site_position,
    mask_below_threshold,
    mask_variable,
    sorted_sweep_names,
)

if TYPE_CHECKING:
    import matplotlib.pyplot as plt

logger = logging.getLogger(__name__)


def _pyplot() -> Any:
    """Import matplotlib.pyplot on first use (~0.2 s: keeps it out of sidecar start-up)."""
    import matplotlib

    matplotlib.use("Agg")  # Non-interactive backend
    import matplotlib.pyplot as _plt

    return _plt


# --- Export constraints ---
MIN_DPI = 72
MAX_DPI = 600
DEFAULT_DPI = 300
DEFAULT_FIG_SIZE = 10.0  # inches
DEFAULT_FONT_SIZE = 12
DEFAULT_IMAGE_FORMAT = "png"

ProgressCallback = Callable[[int, str], None]


def _noop_progress(percent: int, message: str) -> None:
    pass


# ---------------------------------------------------------------------------
# Variable metadata: colormaps, ranges, units
# ---------------------------------------------------------------------------

_VARIABLE_CMAPS: dict[str, str] = {
    "DBZH": "pyart_NWSRef",
    "DBZ": "pyart_NWSRef",
    "reflectivity": "pyart_NWSRef",
    "corrected_reflectivity": "pyart_NWSRef",
    "total_power": "pyart_NWSRef",
    "VRADH": "pyart_NWSVel",
    "VEL": "pyart_NWSVel",
    "velocity": "pyart_NWSVel",
    "corrected_velocity": "pyart_NWSVel",
    "WRADH": "pyart_NWS_SPW",
    "WIDTH": "pyart_NWS_SPW",
    "spectrum_width": "pyart_NWS_SPW",
    "ZDR": "pyart_RefDiff",
    "differential_reflectivity": "pyart_RefDiff",
    "RHOHV": "pyart_RefDiff",
    "cross_correlation_ratio": "pyart_RefDiff",
    "PHIDP": "pyart_PhaseField",
    "differential_phase": "pyart_PhaseField",
    "KDP": "pyart_RefDiff",
    "specific_differential_phase": "pyart_RefDiff",
}

_VARIABLE_RANGES: dict[str, tuple[float, float]] = {
    "DBZH": (-20.0, 75.0),
    "DBZ": (-20.0, 75.0),
    "reflectivity": (-20.0, 75.0),
    "corrected_reflectivity": (-20.0, 75.0),
    "total_power": (-20.0, 75.0),
    "VRADH": (-30.0, 30.0),
    "VEL": (-30.0, 30.0),
    "velocity": (-30.0, 30.0),
    "corrected_velocity": (-30.0, 30.0),
    "WRADH": (0.0, 10.0),
    "WIDTH": (0.0, 10.0),
    "spectrum_width": (0.0, 10.0),
    "ZDR": (-1.0, 5.0),
    "differential_reflectivity": (-1.0, 5.0),
    "RHOHV": (0.0, 1.05),
    "cross_correlation_ratio": (0.0, 1.05),
    "PHIDP": (0.0, 180.0),
    "differential_phase": (0.0, 180.0),
    "KDP": (-2.0, 6.0),
    "specific_differential_phase": (-2.0, 6.0),
}

_VARIABLE_UNITS: dict[str, str] = {
    "DBZH": "dBZ",
    "DBZ": "dBZ",
    "reflectivity": "dBZ",
    "corrected_reflectivity": "dBZ",
    "total_power": "dBZ",
    "VRADH": "m/s",
    "VEL": "m/s",
    "velocity": "m/s",
    "corrected_velocity": "m/s",
    "WRADH": "m/s",
    "WIDTH": "m/s",
    "spectrum_width": "m/s",
    "ZDR": "dB",
    "differential_reflectivity": "dB",
    "RHOHV": "",
    "cross_correlation_ratio": "",
    "PHIDP": "deg",
    "differential_phase": "deg",
    "KDP": "deg/km",
    "specific_differential_phase": "deg/km",
}


def _get_cmap(variable: str) -> str:
    """Return a matplotlib colormap name for the variable."""
    cmap_name = _VARIABLE_CMAPS.get(variable)
    if cmap_name and cmap_name in _pyplot().colormaps():
        return cmap_name
    # Fallback colormaps
    vlow = variable.lower()
    if any(tok in vlow for tok in ("dbz", "reflectivity", "power")):
        return "jet"
    if any(tok in vlow for tok in ("vel", "vrad")):
        return "RdBu_r"
    if any(tok in vlow for tok in ("width", "wrad", "spw")):
        return "hot"
    return "viridis"


def _get_range(variable: str) -> tuple[float | None, float | None]:
    r = _VARIABLE_RANGES.get(variable)
    return r if r is not None else (None, None)


def _get_units(variable: str, data_attrs: dict[str, Any] | None = None) -> str:
    """Resolve units from data attributes or fallback table."""
    if data_attrs:
        u = data_attrs.get("units", data_attrs.get("unit", ""))
        if u:
            return str(u)
    return _VARIABLE_UNITS.get(variable, "")


def _extract_sweep_dataset(
    datatree: xr.DataTree,
    sweep: int,
) -> xr.Dataset:
    """Extract the dataset for a given sweep index."""
    sweep_nodes = sorted_sweep_names(datatree)
    if sweep < 0 or sweep >= len(sweep_nodes):
        raise IndexError(f"Sweep {sweep} out of range (0..{len(sweep_nodes) - 1})")
    return datatree[sweep_nodes[sweep]].to_dataset()


def _extract_datetime(datatree: xr.DataTree, sweep_ds: xr.Dataset) -> str:
    """Best-effort datetime extraction for titles."""
    # Try time coordinate in sweep
    try:
        if "time" in sweep_ds.coords:
            t = np.atleast_1d(sweep_ds.coords["time"].values)
            if t.size > 0 and np.issubdtype(t.dtype, np.datetime64) and not np.isnat(t[0]):
                t0 = np.datetime_as_string(t[0], unit="s")
                return str(t0).replace("T", " ")
    except (TypeError, ValueError):
        pass
    # Try root attributes
    try:
        root_ds = datatree.to_dataset()
        for key in ("time_coverage_start", "start_datetime", "time"):
            if key in root_ds.attrs:
                return str(root_ds.attrs[key])
    except Exception:
        pass
    return ""


def _csv_header(
    datatree: xr.DataTree,
    sweep_ds: xr.Dataset,
    da: xr.DataArray,
    variable: str,
    sweep: int,
    options: dict[str, Any],
    n_rows: int,
) -> str:
    """``# key: value`` metadata block placed above the column row of a CSV export.

    Read it with ``pandas.read_csv(path, comment="#")`` / R ``read.csv(comment.char = "#")``.
    """
    from engine.cfwrite import normalize_units

    units = normalize_units(_get_units(variable, dict(da.attrs))) or "1"
    root = datatree.to_dataset()
    site = {
        k: float(np.asarray(root[k].values).ravel()[0])
        for k in ("latitude", "longitude", "altitude")
        if k in root.variables and np.asarray(root[k].values).size
    }
    sources = options.get("source_files") or (getattr(datatree, "encoding", {}) or {}).get(
        "source_path"
    )
    t = _extract_datetime(datatree, sweep_ds)
    vmask_text = valuemask.describe(valuemask.pick(options, variable), units)
    extra = {
        "radar": root.attrs.get("instrument_name") or root.attrs.get("site_name"),
        "radar_position": ", ".join(
            f"{k}={site[k]:.5f}" for k in ("latitude", "longitude", "altitude") if k in site
        )
        + " (degrees_north, degrees_east, m)"
        if site
        else None,
        "data_time": provenance.iso_utc(t) if t else None,
        "sweep": sweep,
        "fixed_angle_deg": None
        if _extract_elevation(sweep_ds) == "?"
        else _extract_elevation(sweep_ds),
        "rows": n_rows,
    }
    return provenance.csv_comment_block(
        title=f"Radar sweep {sweep}: {variable} (one row per gate with an echo)",
        columns={"azimuth_deg": "degrees clockwise from north", "range_m": "m", variable: units},
        source_files=sources,
        fill="gates without an echo (below threshold / range folded / no data) are omitted"
        + ("; gates hidden by the value mask are omitted too" if vmask_text else ""),
        params={
            "variable": variable,
            "sweep": sweep,
            "mask_no_echo": True,
            **({"value_mask": vmask_text} if vmask_text else {}),
        },
        extra=extra,
    )


def _with_utc(text: str) -> str:
    """*text* (a radar time) labelled as UTC unless it already says so."""
    return text if text.rstrip().endswith(("Z", "UTC")) else f"{text} UTC"


def _extract_elevation(sweep_ds: xr.Dataset) -> str:
    """Best-effort elevation extraction."""
    if "sweep_fixed_angle" in sweep_ds.attrs:
        return f"{float(sweep_ds.attrs['sweep_fixed_angle']):.1f}"
    if "elevation" in sweep_ds.coords:
        elev = sweep_ds.coords["elevation"].values
        if hasattr(elev, "__len__") and len(elev) > 0:
            return f"{float(np.nanmean(elev)):.1f}"
    return "?"


def _count_sweep_nodes(datatree: xr.DataTree) -> int:
    return len([n for n in datatree.children if n.startswith("sweep_")])


def _opt_range(options: dict[str, Any]) -> tuple[float | None, float | None]:
    """Optional colour-scale override ``(vmin, vmax)`` from client options (finite numbers only)."""
    out = []
    for key in ("vmin", "vmax"):
        v = options.get(key)
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) and np.isfinite(v)
        out.append(float(v) if ok else None)
    return out[0], out[1]


def _opt_float(options: dict[str, Any], key: str, default: float, lo: float, hi: float) -> float:
    """Read a numeric option, falling back to *default* and clamping to [lo, hi].

    Options come straight from the client; an absurd ``fig_size`` would
    otherwise try to allocate a gigantic canvas.
    """
    try:
        value = float(options.get(key, default))
    except (TypeError, ValueError):
        return default
    if not np.isfinite(value):
        return default
    return min(max(value, lo), hi)


# ---------------------------------------------------------------------------
# Output-path helpers
# ---------------------------------------------------------------------------

_FORMAT_EXT: dict[str, str] = {
    "png": "png",
    "svg": "svg",
    "pdf": "pdf",
    "netcdf": "nc",
    "cfradial2": "nc",
    "csv": "csv",
    "zarr": "zarr",
    "geotiff": "tif",
}


def _netcdf_attr(value: Any) -> Any:
    """Coerce one attribute value to something NetCDF4 can store.

    NEXRAD volumes carry booleans (e.g. ``mpda_vcp``), which netCDF4 rejects with
    ``illegal data type for attribute``; ``None`` and dicts fail likewise.
    """
    if isinstance(value, (bool, np.bool_)):
        return int(value)
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value)
    if isinstance(value, np.ndarray) and value.dtype == np.bool_:
        return value.astype(np.int8)
    if (
        isinstance(value, (list, tuple))
        and value
        and all(isinstance(v, (bool, np.bool_)) for v in value)
    ):
        return [int(v) for v in value]
    return value


_collapse_site_position = collapse_site_position  # the reader does this on open; kept for safety


def _apply_value_mask_to_dataset(
    ds: xr.Dataset, variable: str, vmask: dict[str, Any], text: str
) -> xr.Dataset:
    """*ds* with the values of *variable* that the value mask hides set to NaN (a new variable;
    the original is not modified). The variable's ``value_mask`` attribute says what was hidden."""
    if variable not in ds.data_vars:
        return ds
    da = ds[variable]
    if da.dtype.kind not in "fiu":
        return ds
    values, _ = valuemask.apply(da.values, vmask)
    new = da.copy(data=values)
    new.attrs = {**da.attrs, "value_mask": text}
    return ds.assign({variable: new})


def _netcdf_safe_tree(
    datatree: xr.DataTree,
    *,
    mask: bool = False,
    value_masks: dict[str, tuple[dict[str, Any], str]] | None = None,
) -> xr.DataTree:
    """A shallow copy of *datatree* that NetCDF can store.

    Attributes are coerced to serialisable values and per-ray site position is collapsed to a
    scalar. With ``mask=True`` the "no echo" / "range folded" codes of integer-encoded moments are
    set to NaN (they would otherwise be written as real measurements such as -33 dBZ). The
    original (the reader's live tree) is never modified.

    *value_masks* maps a variable name to ``(mask, description)``: in every sweep the values of that
    variable that its value mask hides are set to NaN (see :mod:`engine.valuemask`).
    """
    safe = datatree.copy(deep=False)
    for node in safe.subtree:
        ds = node.to_dataset().copy(deep=False)
        if node is safe:
            ds = _collapse_site_position(ds)
        if mask and node is not safe:
            ds = mask_below_threshold(ds)
        if value_masks and node is not safe:
            for name, (vmask, text) in value_masks.items():
                ds = _apply_value_mask_to_dataset(ds, name, vmask, text)
        ds.attrs = {k: _netcdf_attr(v) for k, v in ds.attrs.items()}
        for var in ds.variables.values():
            var.attrs = {k: _netcdf_attr(v) for k, v in var.attrs.items()}
        node.dataset = ds
    return safe


# xradar's CfRadial1 writer merges the sweeps with ``compat="no_conflicts"``. The per-sweep scalars
# below (strings among them) are broadcast along ``time`` first and then thrown away, and carrying
# them through that merge is what makes a NEXRAD volume take a minute and 12 GB. Dropping them
# before the merge gives the identical result (tests/test_perf_equivalence.py checks it).
_CF1_SWEEP_SCALARS = ["sweep_fixed_angle", "sweep_number", "sweep_mode", "prt_mode", "follow_mode"]
_CF1_LOCK = threading.Lock()


def _cfradial1_variables(cf1: Any, dtree: xr.DataTree, dim0: str | None = None) -> xr.Dataset:
    """``xradar.io.export.cfradial1._variable_mapper`` minus the wasted merge work."""
    sweep_info = cf1._sweep_info_mapper(dtree)
    vol_info = cf1._main_info_mapper(dtree)
    if "fixed_angle" in vol_info:
        vol_info = vol_info.drop_vars("fixed_angle")
    sweep_datasets = []
    for grp in dtree.groups:
        if "sweep" not in grp:
            continue
        data = dtree[grp].to_dataset(inherit="all_coords")
        if dim0 is None:
            dim0 = "elevation" if data.sweep_mode.load().astype(str) == "rhi" else "azimuth"
            if dim0 not in data.dims:
                dim0 = "time"
                assert dim0 in data.dims
        if dim0 != "time" and dim0 in data.dims:
            data = data.swap_dims({dim0: "time"})
        data = data.sortby("time").drop_vars(["x", "y", "z", *_CF1_SWEEP_SCALARS], errors="ignore")
        data.attrs = {}
        sweep_datasets.append(data)
    result = xr.combine_by_coords(
        sweep_datasets,
        data_vars="all",
        compat="no_conflicts",
        join="outer",
        coords="minimal",
        combine_attrs="no_conflicts",
    )
    result = result.drop_vars(
        ["latitude", "longitude", "altitude", "spatial_ref", "crs_wkt"], errors="ignore"
    )
    result.update(sweep_info)
    result.update(cf1.calculate_sweep_indices(dtree, result))
    result = result.reset_coords(["elevation", "azimuth"])
    result.update(vol_info)
    return result


@contextlib.contextmanager
def _fast_cfradial1_mapper() -> Iterator[None]:
    """Swap xradar's CfRadial1 variable merge for :func:`_cfradial1_variables` while the block runs.

    Falls back to xradar's own mapper when its private helpers are missing or anything in the
    faster path fails, so the file written is always the one xradar would write.
    """
    from xradar.io.export import cfradial1 as cf1

    stock = getattr(cf1, "_variable_mapper", None)
    helpers = ("_sweep_info_mapper", "_main_info_mapper", "calculate_sweep_indices")
    if stock is None or not all(hasattr(cf1, h) for h in helpers):
        yield
        return

    def mapper(tree: xr.DataTree, dim0: str | None = None) -> xr.Dataset:
        try:
            return _cfradial1_variables(cf1, tree, dim0)
        except Exception as exc:
            logger.debug("fast CfRadial1 merge failed (%s); using xradar's", exc)
            return stock(tree, dim0)

    with _CF1_LOCK:  # the swap is process-wide
        cf1._variable_mapper = mapper
        try:
            yield
        finally:
            cf1._variable_mapper = stock


def _to_cfradial1(dtree: xr.DataTree, filename: str) -> None:
    """``xradar.io.export.to_cfradial1`` using the faster variable merge."""
    from xradar.io.export import cfradial1 as cf1

    with _fast_cfradial1_mapper():
        cf1.to_cfradial1(dtree=dtree, filename=filename)


def _safe_stem(name: str) -> str:
    """Make *name* safe to use as (part of) a file name: no separators or '..'."""
    stem = re.sub(r"[^\w.\-]+", "_", str(name)).strip("._")
    return stem or "var"


def default_export_dir() -> Path:
    """Where exports go when the caller did not choose a destination.

    A normal, visible folder (``~/Downloads/xradar-exports``, or ``$XRADAR_EXPORT_DIR``), never a
    hidden system temp directory that the user cannot find.
    """
    from engine import web

    if web.is_web_mode():  # the server always passes a session path; never fall back to ~
        raise ValueError("Exports must be written to the session folder in web mode")
    env = os.environ.get("XRADAR_EXPORT_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    downloads = Path.home() / "Downloads"
    return (downloads if downloads.is_dir() else Path.home()) / "xradar-exports"


def _unique_in(directory: Path, prefix: str, suffix: str) -> Path:
    """``directory/prefix<timestamp>[-n]suffix`` that does not exist yet (directory is created)."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = directory / f"{prefix}{stamp}{suffix}"
    n = 1
    while candidate.exists():
        n += 1
        candidate = directory / f"{prefix}{stamp}-{n}{suffix}"
    return candidate


class _Target(str):
    """The path an exporter should write to; ``final`` is where the result ends up on success."""

    final: str

    def __new__(cls, write_to: str, final: str) -> _Target:
        obj = super().__new__(cls, write_to)
        obj.final = final
        return obj


def _discard(path: str) -> None:
    """Remove a (partial) file or folder; never raises."""
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        with contextlib.suppress(OSError):
            os.remove(path)


@contextlib.contextmanager
def _output_target(
    output_path: str | None,
    *,
    suffix: str,
    prefix: str,
    directory: bool = False,
) -> Iterator[_Target]:
    """Yield the path to write to; a failed export leaves nothing behind.

    With a user-supplied *output_path* the data is written to a hidden sibling file and moved
    over the destination only when the export finished, so a full disk, a crash or a cancelled
    export never leaves a truncated file there (nor destroys an earlier good one). The parent
    folders are created. Without *output_path* a uniquely named file (or folder) is created in
    :func:`default_export_dir` and removed again if the export raises. The yielded path is a
    ``str`` whose ``.final`` attribute is the finished file's location.
    """
    if output_path is None:
        target = _unique_in(default_export_dir(), prefix, suffix)
        if directory:
            target.mkdir(parents=True)
        else:
            target.touch()
        path = str(target)
        try:
            yield _Target(path, path)
        except BaseException:
            _discard(path)
            raise
        return

    final = str(Path(output_path).expanduser())
    parent = Path(final).parent
    parent.mkdir(parents=True, exist_ok=True)
    # Same folder (so the final move is atomic) and same file name tail (so a name-based format
    # or compression guess, e.g. ``.csv.gz``, still works).
    tmp = str(parent / f".xradar-{uuid.uuid4().hex[:8]}-{Path(final).name}")
    try:
        yield _Target(tmp, final)
        if directory and os.path.isdir(final):
            backup = f"{tmp}.old"
            os.replace(final, backup)
            try:
                os.replace(tmp, final)
            except BaseException:
                os.replace(backup, final)
                raise
            _discard(backup)
        else:
            os.replace(tmp, final)
    except BaseException:
        _discard(tmp)
        raise


# ---------------------------------------------------------------------------
# Publication-quality figure rendering
# ---------------------------------------------------------------------------


def _polar_edges(azimuth_deg: np.ndarray, range_km: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Cell edges (azimuth in radians, range in km) for gate/ray *centres*.

    Edges sit half-way between neighbouring centres (extrapolated at the ends)
    so each ray/gate is drawn centred on its coordinate instead of shifted by
    half a beamwidth / gate.
    """
    az_u = np.unwrap(np.deg2rad(azimuth_deg))
    if len(az_u) > 1:
        d = np.diff(az_u)
        az_edges = np.concatenate([[az_u[0] - d[0] / 2], az_u[:-1] + d / 2, [az_u[-1] + d[-1] / 2]])
    else:
        half = np.deg2rad(0.5)
        az_edges = np.array([az_u[0] - half, az_u[0] + half])

    if len(range_km) > 1:
        dr = np.diff(range_km)
        range_edges = np.concatenate(
            [[range_km[0] - dr[0] / 2], range_km[:-1] + dr / 2, [range_km[-1] + dr[-1] / 2]]
        )
        range_edges = np.maximum(range_edges, 0.0)
    else:
        range_edges = np.array([0.0, max(range_km[0] * 2, 1e-3)])
    return az_edges, range_edges


def _expand_title(
    template: str, datatree: xr.DataTree, sweep_ds: xr.Dataset, variable: str, sweep: int
) -> str:
    """A user-written title with ``{variable}``, ``{sweep}``, ``{elevation}``, ``{time}`` and
    ``{radar}`` filled in (plain replacement, so stray braces in a title are left alone)."""
    radar = ""
    for attrs in (getattr(datatree, "attrs", {}), getattr(datatree.to_dataset(), "attrs", {})):
        radar = str(attrs.get("instrument_name") or attrs.get("site_name") or "")
        if radar:
            break
    elevation = _extract_elevation(sweep_ds)
    values = {
        "{variable}": variable,
        "{sweep}": str(sweep),
        "{elevation}": "" if elevation == "?" else f"{elevation}",
        "{time}": _extract_datetime(datatree, sweep_ds) or "",
        "{radar}": radar,
    }
    for key, value in values.items():
        template = template.replace(key, value)
    return template.strip()


def figure_file_metadata(
    fmt: str,
    datatree: xr.DataTree,
    variable: str,
    sweep: int,
    *,
    title: str = "",
    source_files: Any = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``metadata=`` dict for ``savefig`` of a PNG / PDF / SVG figure.

    PNG gets tEXt chunks (Title, Software, Source, Description, Creation Time, Comment), PDF the
    document-info entries, SVG a Dublin-Core ``<metadata>`` block. Only file *names* are recorded
    (never folders), so a shared figure does not reveal the author's directory layout.
    """
    try:
        data_time = _extract_datetime(datatree, _extract_sweep_dataset(datatree, sweep))
    except (IndexError, KeyError, ValueError):
        data_time = ""
    sources = source_files
    if not sources:
        enc = getattr(datatree, "encoding", {}) or {}
        sources = enc.get("source_path") or enc.get("source")
    meta = provenance.figure_metadata(
        title=title,
        source_files=sources,
        variable=variable,
        sweep=sweep,
        data_time=_with_utc(data_time) if data_time else "",
        params=params,
    )
    if fmt == "pdf":
        from datetime import datetime, timezone

        out = {
            "Creator": meta["Software"],
            "Subject": meta.get("Description", ""),
            "Keywords": ", ".join(
                x for x in ("radar", variable, f"sweep {sweep}", meta.get("Source", "")) if x
            ),
            "CreationDate": datetime.now(timezone.utc),
        }
        if meta.get("Title"):
            out["Title"] = meta["Title"]
        return out
    if fmt == "svg":
        out = {
            "Creator": meta["Software"],
            "Date": meta["Creation Time"],
            "Description": " ".join(x for x in (meta.get("Description"), meta.get("Comment")) if x),
        }
        if meta.get("Title"):
            out["Title"] = meta["Title"]
        if meta.get("Source"):
            out["Source"] = meta["Source"]
        return out
    return meta  # png


def _gif_comment(
    datatree: xr.DataTree, variable: str, sweeps: list[int], options: dict[str, Any]
) -> bytes:
    """GIF comment extension: software, source file names, variable, sweeps, creation time."""
    sources = options.get("source_files") or (getattr(datatree, "encoding", {}) or {}).get(
        "source_path"
    )
    names = provenance.source_names(sources)
    parts = [
        provenance.software_string(),
        provenance.utc_stamp(),
        f"{variable} sweeps {','.join(str(s) for s in sweeps)}",
    ]
    if names:
        parts.append("source " + ", ".join(names))
    # a GIF comment block holds at most 255 bytes (longer ones are silently dropped by Pillow)
    return "; ".join(parts).encode("utf-8", "replace")[:255]


def render_publication_figure(
    datatree: xr.DataTree,
    variable: str,
    sweep: int,
    *,
    dpi: int = DEFAULT_DPI,
    fig_size: float = DEFAULT_FIG_SIZE,
    font_size: int = DEFAULT_FONT_SIZE,
    show_rings: bool = True,
    show_colorbar: bool = True,
    show_title: bool = True,
    title_override: str | None = None,
    dark_background: bool = False,
    vmin: float | None = None,
    vmax: float | None = None,
    rhi_clean: Any = None,
    rhi_view: dict[str, Any] | None = None,
    value_mask: dict[str, Any] | None = None,
) -> plt.Figure:
    """Create a publication-quality matplotlib figure of a PPI sweep (or an RHI sweep as a
    range-height plot).

    *vmin*/*vmax* override the colour range (otherwise the per-variable default
    or the data range is used).  *rhi_clean* only matters for RHI sweeps: how noise is hidden
    (``None`` = SQI / SNR / RHOHV masks and despeckle with the defaults, ``False`` = raw data, or a
    dict, see :func:`engine.rhi.parse_clean`); the figure's caption says what was applied.
    *value_mask* hides the values of *variable* the way the viewer does (see
    :mod:`engine.valuemask`); the figure says so in a footnote. For an RHI the mask travels in
    *rhi_clean* instead. *rhi_view* sets the RHI axes: ``{"height_max": 25, "full": False}``.
    By default the axes cover
    the ground range and height that hold valid gates (rounded up), with the height cut at
    ``height_max`` km (25): the beams reach ~95 km but there is no weather up there.
    Returns the Figure object (caller is responsible for saving/closing).
    """
    plt = _pyplot()
    sweep_ds = _extract_sweep_dataset(datatree, sweep)

    if variable not in sweep_ds.data_vars:
        available = list(sweep_ds.data_vars)
        if not available:
            raise ValueError(f"No data variables in sweep {sweep}")
        raise KeyError(f"Variable '{variable}' not found in sweep {sweep}. Available: {available}")

    # NaN-out reserved "no echo" / "range folded" codes (see reader.mask_variable) so the
    # no-echo area is transparent instead of painting the colormap minimum (-33 dBZ etc.).
    da = mask_variable(sweep_ds[variable])
    values = da.values
    if values.size == 0:
        raise ValueError(
            f"Sweep {sweep} has no {variable} data (0 rays or 0 gates). The file may be "
            "truncated or incomplete; try another sweep."
        )

    # A range-height scan has one azimuth: draw it as height over ground range, not as a PPI.
    rays = rhi.rhi_rays(sweep_ds)
    if rays is not None:
        return _render_rhi_figure(
            datatree,
            sweep_ds,
            da,
            rays,
            variable,
            sweep,
            dpi=dpi,
            fig_size=fig_size,
            font_size=font_size,
            show_rings=show_rings,
            show_colorbar=show_colorbar,
            show_title=show_title,
            title_override=title_override,
            dark_background=dark_background,
            vmin=vmin,
            vmax=vmax,
            clean=rhi.parse_clean(rhi_clean),
            view=rhi_view or {},
        )

    # Polar coordinates
    if "azimuth" in da.coords:
        azimuth = da.coords["azimuth"].values.astype(np.float64)
    else:
        azimuth = np.arange(values.shape[0], dtype=np.float64)

    if "range" in da.coords:
        range_m = da.coords["range"].values.astype(np.float64)
    else:
        range_m = np.arange(values.shape[-1], dtype=np.float64)

    # Rays/gates without a position (NaN/inf angle or range) cannot be drawn; drawing the rest
    # beats failing the whole figure with a pcolormesh error.
    ray_ok, gate_ok = np.isfinite(azimuth), np.isfinite(range_m)
    if not (ray_ok.all() and gate_ok.all()):
        if not (ray_ok.any() and gate_ok.any()):
            raise ValueError(
                f"Sweep {sweep} has no ray angles or gate ranges (all NaN); nothing to draw. "
                "The file may be damaged."
            )
        azimuth, range_m = azimuth[ray_ok], range_m[gate_ok]
        values = values[ray_ok][:, gate_ok]

    range_km = range_m / 1000.0

    # The value mask (the same rules as on screen); hidden gates become NaN = not drawn.
    mask_note = None
    units_for_mask = _get_units(variable, dict(da.attrs))
    vmask = valuemask.normalize(value_mask)
    if vmask is not None:
        values, n_hidden = valuemask.apply(values, vmask)
        mask_note = "Value mask: " + (valuemask.describe(vmask, units_for_mask) or "")
        mask_note += f" ({n_hidden} gates hidden)" if n_hidden else " (no gate affected)"

    # Edges for pcolormesh (flat shading needs n+1 edges per axis)
    az_edges, range_edges = _polar_edges(azimuth, range_km)

    # Cartesian transform
    r_grid, az_grid = np.meshgrid(range_edges, az_edges)
    x = r_grid * np.sin(az_grid)
    y = r_grid * np.cos(az_grid)

    # Color range
    default_lo, default_hi = _get_range(variable)
    if vmin is None:
        vmin = default_lo
    if vmax is None:
        vmax = default_hi
    if vmin is None or vmax is None:
        finite = values[np.isfinite(values)]
        if vmin is None:
            vmin = float(finite.min()) if finite.size else 0.0
        if vmax is None:
            vmax = float(finite.max()) if finite.size else 1.0
    if vmax <= vmin:
        vmax = vmin + 1.0

    units = _get_units(variable, dict(da.attrs))
    cmap_name = _get_cmap(variable)

    # Style
    if dark_background:
        plt.style.use("dark_background")
    else:
        plt.rcdefaults()

    plt.rcParams.update(
        {
            "font.size": font_size,
            "axes.labelsize": font_size,
            "axes.titlesize": font_size + 2,
            "xtick.labelsize": font_size - 2,
            "ytick.labelsize": font_size - 2,
            "figure.titlesize": font_size + 4,
        }
    )

    fig, ax = plt.subplots(
        figsize=(fig_size, fig_size),
        dpi=dpi,
    )
    try:
        # Plot PPI.  The mesh is rasterised so SVG/PDF output embeds one image
        # (instead of ~n_az*n_range vector quads: minutes and hundreds of MB).
        mesh = ax.pcolormesh(
            x,
            y,
            values,
            cmap=cmap_name,
            vmin=vmin,
            vmax=vmax,
            shading="flat",
            rasterized=True,
        )

        # Equal aspect ratio
        ax.set_aspect("equal")
        max_r = float(range_km.max())
        if not np.isfinite(max_r) or max_r <= 0:
            max_r = 1.0
        ax.set_xlim(-max_r, max_r)
        ax.set_ylim(-max_r, max_r)

        # Axis labels
        ax.set_xlabel("East-West Distance (km)")
        ax.set_ylabel("North-South Distance (km)")

        # Range rings
        if show_rings:
            ring_interval = _pick_ring_interval_km(max_r)
            theta = np.linspace(0, 2 * np.pi, 360)
            for r in np.arange(ring_interval, max_r + ring_interval, ring_interval):
                if r > max_r * 1.01:
                    break
                ax.plot(
                    r * np.cos(theta),
                    r * np.sin(theta),
                    color="gray",
                    linewidth=0.5,
                    linestyle="--",
                    alpha=0.5,
                )
                # Label ring at 45-degree position
                ax.text(
                    r * np.cos(np.pi / 4) + max_r * 0.01,
                    r * np.sin(np.pi / 4) + max_r * 0.01,
                    f"{r:.0f} km",
                    fontsize=font_size - 3,
                    color="gray",
                    alpha=0.7,
                )

            # Crosshair lines
            ax.axhline(0, color="gray", linewidth=0.3, alpha=0.4)
            ax.axvline(0, color="gray", linewidth=0.3, alpha=0.4)

            # Cardinal direction labels
            offset = max_r * 1.05
            for label, lx, ly, ha, va in (
                ("N", 0, offset, "center", "bottom"),
                ("S", 0, -offset, "center", "top"),
                ("E", offset, 0, "left", "center"),
                ("W", -offset, 0, "right", "center"),
            ):
                ax.text(
                    lx,
                    ly,
                    label,
                    ha=ha,
                    va=va,
                    fontsize=font_size - 1,
                    fontweight="bold",
                    color="gray",
                )

        # Colorbar
        if show_colorbar:
            cbar = fig.colorbar(mesh, ax=ax, pad=0.02, fraction=0.046)
            label = f"{variable}"
            if units:
                label += f" ({units})"
            cbar.set_label(label)

        # Title
        if show_title:
            if title_override:
                title = _expand_title(str(title_override), datatree, sweep_ds, variable, sweep)
            else:
                elev = _extract_elevation(sweep_ds)
                dt_str = _extract_datetime(datatree, sweep_ds)
                parts = [variable]
                if elev != "?":
                    parts.append(f"Elev: {elev} deg")
                parts.append(f"Sweep {sweep}")
                if dt_str:
                    parts.append(_with_utc(dt_str))
                title = " | ".join(parts)
            ax.set_title(title, pad=12)

        if mask_note:
            fig.text(
                0.5, 0.004, mask_note, ha="center", va="bottom", color="gray", fontsize=font_size - 3
            )
            fig.tight_layout(rect=(0, 0.035, 1, 1))
        else:
            fig.tight_layout()
    except BaseException:
        plt.close(fig)
        raise
    return fig


def _nice_step(span: float, target: int = 6) -> float:
    """A 1/2/5 x 10^k step that gives about *target* ticks over *span*."""
    raw = max(span, 1e-9) / max(target, 1)
    pow10 = 10.0 ** np.floor(np.log10(raw))
    f = raw / pow10
    return float((1.0 if f < 1.5 else 2.0 if f < 3.5 else 5.0 if f < 7.5 else 10.0) * pow10)


#: Default cap of an RHI figure's height axis (km): no weather above it.
DEFAULT_RHI_HEIGHT_CAP_KM = 25.0
#: How beam height is computed, stated on the figure.
RHI_BEAM_MODEL = "beam path: standard refraction, 4/3 effective Earth radius"


def _nice_ceil(v: float) -> float:
    """Round *v* up to a readable number (13.3 -> 14, 77.5 -> 80)."""
    if not v > 0:
        return 1.0
    step = _nice_step(v, 4)
    return float(np.ceil(v / step - 1e-9) * step)


def _rhi_mesh(
    rays: rhi.RhiRays, values: np.ndarray, range_m: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Cell corners of an RHI sweep: ``(ground_km, height_km, values)`` for ``pcolormesh``.

    *values* is ``(n_rays, n_gates)`` in elevation order. Corner arrays are ``(n+1, m+1)``; the
    cells are curved quadrilaterals (constant range x constant elevation) on the 4/3 Earth. Holes
    between rays that are too wide to bridge get a blank row (see ``rhi.contiguous_mesh``).
    """
    el_edges, vals = rhi.contiguous_mesh(rays.elevation, values)
    if range_m.size > 1:
        mid = (range_m[:-1] + range_m[1:]) / 2.0
        edges = np.concatenate(
            [[range_m[0] - (mid[0] - range_m[0])], mid, [range_m[-1] + (range_m[-1] - mid[-1])]]
        )
        edges = np.maximum(edges, 0.0)
    else:
        edges = np.array([0.0, max(float(range_m[0]) * 2.0, 1.0)])
    ground, height = rhi.beam_geometry(edges[None, :], el_edges[:, None])
    return ground / 1000.0, height / 1000.0, vals


def _render_rhi_figure(
    datatree: xr.DataTree,
    sweep_ds: xr.Dataset,
    da: xr.DataArray,
    rays: rhi.RhiRays,
    variable: str,
    sweep: int,
    *,
    dpi: int,
    fig_size: float,
    font_size: int,
    show_rings: bool,
    show_colorbar: bool,
    show_title: bool,
    title_override: str | None,
    dark_background: bool,
    vmin: float | None,
    vmax: float | None,
    clean: rhi.RhiClean,
    view: dict[str, Any],
) -> plt.Figure:
    """Range-height figure of an RHI sweep: x ground range (km), y height above the radar (km).

    Equal aspect, with lines of constant range and constant elevation instead of a PPI's range
    rings (*show_rings* switches them). Beam heights use the 4/3 effective Earth radius.
    """
    plt = _pyplot()
    raw = np.asarray(da.values, dtype=np.float64)
    values = raw[rays.order]
    units = _get_units(variable, dict(da.attrs))
    quality: dict[str, tuple[str, np.ndarray]] = {}
    for kind, name in rhi.find_quality_fields(
        [str(v) for v in sweep_ds.data_vars], exclude=variable
    ).items():
        quality[kind] = (
            name,
            np.asarray(mask_variable(sweep_ds[name]).values, dtype=np.float64)[rays.order],
        )
    values, caption = rhi.clean_values(values, quality, clean, units=units)
    if "range" in da.coords:
        range_m = da.coords["range"].values.astype(np.float64)
    else:
        range_m = np.arange(values.shape[-1], dtype=np.float64)

    gx, gy, vals = _rhi_mesh(rays, values, range_m)

    default_lo, default_hi = _get_range(variable)
    if vmin is None:
        vmin = default_lo
    if vmax is None:
        vmax = default_hi
    if vmin is None or vmax is None:
        finite = values[np.isfinite(values)]
        if vmin is None:
            vmin = float(finite.min()) if finite.size else 0.0
        if vmax is None:
            vmax = float(finite.max()) if finite.size else 1.0
    if vmax <= vmin:
        vmax = vmin + 1.0

    cmap_name = _get_cmap(variable)

    # Extent: the cells that hold data and are not below the colour range (the whole scan when none
    # does), always including the radar.
    with np.errstate(invalid="ignore"):
        valid = np.isfinite(vals) & (vals >= vmin)
    if not valid.any():
        valid = np.ones_like(valid)
    corner_x = np.stack(
        [gx[:-1, :-1][valid], gx[1:, 1:][valid], gx[:-1, 1:][valid], gx[1:, :-1][valid]]
    )
    corner_y = np.stack(
        [gy[:-1, :-1][valid], gy[1:, 1:][valid], gy[:-1, 1:][valid], gy[1:, :-1][valid]]
    )
    x_lo, y_lo = min(0.0, float(corner_x.min())), min(0.0, float(corner_y.min()))
    if view.get("full"):
        x_hi, y_hi = float(gx.max()), float(gy.max())  # the whole geometric extent of the scan
    else:
        # where 99.9% of the valid cells end (a few stray gates do not count), rounded up and cut
        # at the height cap: noise and the unreachable heights above the weather stay out
        cap = max(1.0, float(view.get("height_max") or DEFAULT_RHI_HEIGHT_CAP_KM))
        x_hi = min(
            _nice_ceil(float(np.quantile(corner_x.max(axis=0), 0.999)) * 1.03 + 0.5),
            float(gx.max()),
        )
        y_hi = min(_nice_ceil(float(np.quantile(corner_y.max(axis=0), 0.999)) * 1.06 + 0.3), cap)
    x_span = max(x_hi - x_lo, 1.0)
    y_span = max(y_hi - y_lo, 1.0)
    pad_x, pad_y = 0.03 * x_span, 0.05 * y_span

    if dark_background:
        plt.style.use("dark_background")
    else:
        plt.rcdefaults()
    plt.rcParams.update(
        {
            "font.size": font_size,
            "axes.labelsize": font_size,
            "axes.titlesize": font_size + 2,
            "xtick.labelsize": font_size - 2,
            "ytick.labelsize": font_size - 2,
            "figure.titlesize": font_size + 4,
        }
    )

    # Wide, short canvas that follows the data's aspect (axes are equal-aspect).
    fig_w = fig_size * 1.3
    plot_h = fig_w * 0.8 * (y_span + 2 * pad_y) / (x_span + 2 * pad_x)
    fig_h = float(np.clip(plot_h + 2.4, 3.4, fig_size * 1.1))
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=dpi)
    try:
        mesh = ax.pcolormesh(
            gx,
            gy,
            np.ma.masked_invalid(vals),
            cmap=cmap_name,
            vmin=vmin,
            vmax=vmax,
            shading="flat",
            rasterized=True,
        )
        ax.set_aspect("equal", adjustable="box")
        ax.set_xlim(x_lo - pad_x, x_hi + pad_x)
        ax.set_ylim(y_lo - pad_y, y_hi + pad_y)
        ax.set_xlabel(f"Ground range (km)\n{RHI_BEAM_MODEL}")
        ax.set_ylabel("Height above radar (km)")
        ax.axhline(0, color="gray", linewidth=0.5, alpha=0.6)

        if show_rings:
            _draw_rhi_grid(ax, rays, float(range_m.max()), font_size)

        if show_colorbar:
            cbar = fig.colorbar(
                mesh, ax=ax, orientation="horizontal", pad=0.32, shrink=0.6, aspect=40
            )
            label = f"{variable}"
            if units:
                label += f" ({units})"
            cbar.set_label(label)

        if show_title:
            if title_override:
                title = _expand_title(str(title_override), datatree, sweep_ds, variable, sweep)
            else:
                dt_str = _extract_datetime(datatree, sweep_ds)
                parts = [variable, f"RHI az {rays.plane_azimuth:.1f} deg", f"Sweep {sweep}"]
                if dt_str:
                    parts.append(dt_str)
                title = " | ".join(parts)
            ax.set_title(title, pad=12)

        fig.tight_layout()
        # what was applied, under everything (kept in the figure so SVG / PDF / PNG all carry it)
        fig.text(
            0.5,
            0.005,
            caption,
            ha="center",
            va="bottom",
            fontsize=max(font_size - 4, 6),
            color="gray",
            wrap=True,
        )
        fig.subplots_adjust(bottom=fig.subplotpars.bottom + 0.04)
    except BaseException:
        plt.close(fig)
        raise
    return fig


def _draw_rhi_grid(ax: Any, rays: rhi.RhiRays, max_range_m: float, font_size: int) -> None:
    """Dashed lines of constant slant range and constant elevation, labelled inside the frame."""
    el_lo = min(0.0, float(rays.elevation[0]))
    el_hi = float(rays.elevation[-1]) + 1.0
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    style = {"color": "gray", "linewidth": 0.6, "linestyle": "--", "alpha": 0.55}
    halo = {"boxstyle": "round,pad=0.12", "fc": ax.get_facecolor(), "ec": "none", "alpha": 0.7}

    def inside(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        return (
            (x > x0 + 0.02 * (x1 - x0))
            & (x < x1 - 0.02 * (x1 - x0))
            & (y > y0 + 0.04 * (y1 - y0))
            & (y < y1 - 0.04 * (y1 - y0))
        )

    step_km = _nice_step(max_range_m / 1000.0, 6)
    els = np.linspace(el_lo, el_hi, 200)
    for r_km in np.arange(step_km, max_range_m / 1000.0 + 1e-9, step_km):
        g, h = rhi.beam_geometry(r_km * 1000.0, els)
        g, h = g / 1000.0, h / 1000.0
        ax.plot(g, h, **style)
        ok = np.flatnonzero(inside(g, h))
        if ok.size:
            i = ok[-1]
            ax.text(
                g[i],
                h[i],
                f"{r_km:.0f} km",
                fontsize=font_size - 4,
                color="gray",
                ha="center",
                va="top",
                bbox=halo,
            )

    span = el_hi - el_lo
    el_step = 10.0 if span > 40 else 5.0 if span > 18 else 2.0 if span > 8 else 1.0
    ranges = np.linspace(0.0, max_range_m, 200)
    for el in np.arange(np.ceil(max(el_lo, 0.0) / el_step) * el_step, el_hi, el_step):
        if el <= 0:
            continue
        g, h = rhi.beam_geometry(ranges, el)
        g, h = g / 1000.0, h / 1000.0
        ax.plot(g, h, **style)
        ok = np.flatnonzero(inside(g, h))
        if ok.size:
            i = ok[
                int(0.7 * (ok.size - 1))
            ]  # part-way along the visible line, clear of the range labels
            ax.text(
                g[i],
                h[i],
                f"{el:.0f} deg",
                fontsize=font_size - 4,
                color="gray",
                ha="right",
                va="bottom",
                bbox=halo,
            )


def _pick_ring_interval_km(max_range_km: float) -> float:
    """Pick ring interval that gives 4-6 rings."""
    candidates = [5, 10, 20, 25, 50, 100, 150, 200, 250, 500]
    for spacing in candidates:
        n = int(max_range_km / spacing)
        if 3 <= n <= 6:
            return float(spacing)
    return max(max_range_km / 5.0, 1e-3)


# ---------------------------------------------------------------------------
# Main exporter class
# ---------------------------------------------------------------------------


def _volume_value_masks(
    options: dict[str, Any], datatree: xr.DataTree
) -> tuple[dict[str, tuple[dict[str, Any], str]], dict[str, Any]]:
    """``(value_masks for _netcdf_safe_tree, extra provenance params)`` for a volume export.

    Every variable that has an active value mask is masked in every sweep, with its own mask, and
    the file says so (variable attribute ``value_mask`` and the history line).
    """
    masks: dict[str, tuple[dict[str, Any], str]] = {}
    candidates = list(options.get("value_masks") or {}) if isinstance(options, dict) else []
    if isinstance(options, dict) and options.get("value_mask") is not None:
        candidates.append(options.get("value_mask_variable"))
    for name in dict.fromkeys(c for c in candidates if isinstance(c, str)):
        vmask = valuemask.pick(options, name)
        if vmask is None:
            continue
        try:
            units = _get_units(name, dict(_extract_sweep_dataset(datatree, 0)[name].attrs))
        except Exception:  # not in sweep 0: the mask text just has no unit
            units = _get_units(name)
        masks[name] = (vmask, valuemask.describe(vmask, units) or "")
    params = {"value_mask": "; ".join(f"{k}: {t}" for k, (_, t) in masks.items())} if masks else {}
    return masks, params


class RadarExporter:
    """Export radar data to image, data, and animation formats.

    Image formats: PNG, SVG, PDF via matplotlib savefig.
    Data formats: NetCDF (CfRadial1), CfRadial2, CSV, Zarr.
    Animation: GIF via Pillow.
    """

    def export(
        self,
        datatree: xr.DataTree,
        fmt: str,
        dpi: int = DEFAULT_DPI,
        output_path: str | None = None,
        variable: str | None = None,
        sweep: int = 0,
        progress: ProgressCallback = _noop_progress,
        options: dict[str, Any] | None = None,
    ) -> str:
        """Export a single sweep/variable to the specified format."""
        if not isinstance(fmt, str) or not fmt.strip():
            raise ValueError("Export format must be a non-empty string")
        if not isinstance(dpi, int) or dpi < MIN_DPI or dpi > MAX_DPI:
            raise ValueError(f"DPI must be an integer between {MIN_DPI} and {MAX_DPI}, got {dpi}")
        if not isinstance(sweep, int) or sweep < 0:
            raise ValueError(f"Sweep must be a non-negative integer, got {sweep}")

        fmt = fmt.lower().strip()
        if options is None:
            options = {}

        # Resolve variable if not specified
        if variable is None:
            sweep_ds = _extract_sweep_dataset(datatree, sweep)
            data_vars = list(sweep_ds.data_vars)
            if not data_vars:
                raise ValueError(f"No data variables in sweep {sweep}")
            variable = str(data_vars[0])

        dispatch: dict[str, Callable[..., str]] = {
            "png": self._export_figure,
            "svg": self._export_figure,
            "pdf": self._export_figure,
            "netcdf": self._export_netcdf,
            "cfradial2": self._export_cfradial2,
            "csv": self._export_csv,
            "zarr": self._export_zarr,
            "geotiff": self._export_geotiff,
        }
        handler = dispatch.get(fmt)
        if handler is None:
            raise ValueError(
                f"Unsupported export format '{fmt}'. Supported: {sorted(dispatch.keys())}"
            )
        return handler(
            datatree,
            fmt=fmt,
            dpi=dpi,
            output_path=output_path,
            variable=variable,
            sweep=sweep,
            progress=progress,
            options=options,
        )

    # ------------------------------------------------------------------
    # Image export via matplotlib
    # ------------------------------------------------------------------

    def _export_figure(
        self,
        datatree: xr.DataTree,
        *,
        fmt: str,
        dpi: int,
        output_path: str | None,
        variable: str,
        sweep: int,
        progress: ProgressCallback,
        options: dict[str, Any],
    ) -> str:
        progress(10, f"Preparing publication-quality {fmt.upper()} export")

        fig_size = _opt_float(options, "fig_size", DEFAULT_FIG_SIZE, 2.0, 30.0)
        font_size = int(_opt_float(options, "font_size", DEFAULT_FONT_SIZE, 6, 48))
        show_rings = options.get("show_rings", True)
        show_colorbar = options.get("show_colorbar", True)
        show_title = options.get("show_title", True)
        title_override = options.get("title")
        dark_bg = options.get("dark_background", False)
        vmin, vmax = _opt_range(options)
        rhi_clean = options.get("rhi_clean")
        vmask = valuemask.pick(options, variable)
        if isinstance(rhi_clean, dict) and ("value_masks" in options or "value_mask" in options):
            # an RHI uses the mask of ITS variable (batch exports carry several variables)
            rhi_clean = {**rhi_clean, "mask": vmask}

        progress(30, "Rendering figure")

        fig = render_publication_figure(
            datatree,
            variable,
            sweep,
            dpi=dpi,
            fig_size=fig_size,
            font_size=font_size,
            show_rings=show_rings,
            show_colorbar=show_colorbar,
            show_title=show_title,
            title_override=title_override,
            dark_background=dark_bg,
            vmin=vmin,
            vmax=vmax,
            rhi_clean=rhi_clean,
            rhi_view=options.get("rhi_view"),
            value_mask=vmask,
        )

        try:
            progress(70, "Saving file")
            meta = figure_file_metadata(
                fmt,
                datatree,
                variable,
                sweep,
                title=fig.axes[0].get_title() if fig.axes else "",
                source_files=options.get("source_files"),
                params={
                    "dpi": dpi,
                    "colormap": _get_cmap(variable),
                    "vmin": vmin,
                    "vmax": vmax,
                    "mask_no_echo": True,
                    **(
                        {"value_mask": valuemask.describe(vmask, _get_units(variable))}
                        if vmask
                        else {}
                    ),
                },
            )
            with _output_target(output_path, suffix=f".{fmt}", prefix="xradar_export_") as target:
                fig.savefig(
                    target,
                    format=fmt,
                    dpi=dpi,
                    bbox_inches="tight",
                    facecolor=fig.get_facecolor(),
                    edgecolor="none",
                    metadata=meta,
                )
        finally:
            _pyplot().close(fig)

        output_path = str(Path(target.final).resolve())
        progress(100, f"Export complete: {output_path}")
        logger.info("Exported %s to %s", fmt, output_path)
        return output_path

    # ------------------------------------------------------------------
    # Batch export
    # ------------------------------------------------------------------

    def batch_export(
        self,
        datatree: xr.DataTree,
        *,
        variables: list[str],
        sweeps: list[int],
        fmt: str = "png",
        dpi: int = DEFAULT_DPI,
        output_dir: str | None = None,
        progress: ProgressCallback = _noop_progress,
        options: dict[str, Any] | None = None,
    ) -> list[str]:
        """Batch export multiple variables/sweeps.

        Returns list of saved file paths.  Individual failures are logged and
        skipped; if *nothing* could be exported the last error is raised.
        """
        if options is None:
            options = {}

        # Fail fast on arguments that would make every single export fail.
        if not isinstance(fmt, str) or fmt.lower().strip() not in _FORMAT_EXT:
            raise ValueError(f"Unsupported export format '{fmt}'. Supported: {sorted(_FORMAT_EXT)}")
        fmt = fmt.lower().strip()
        if not isinstance(dpi, int) or dpi < MIN_DPI or dpi > MAX_DPI:
            raise ValueError(f"DPI must be an integer between {MIN_DPI} and {MAX_DPI}, got {dpi}")

        # De-duplicate while keeping order so duplicates cannot overwrite each other.
        variables = list(dict.fromkeys(variables))
        sweeps = list(dict.fromkeys(sweeps))
        total = len(variables) * len(sweeps)
        if total == 0:
            raise ValueError("No variables or sweeps selected for batch export")

        if output_dir is None:
            output_dir = str(_unique_in(default_export_dir(), "xradar_batch_", ""))
            os.makedirs(output_dir, exist_ok=True)
        else:
            output_dir = str(Path(output_dir).expanduser())
            os.makedirs(output_dir, exist_ok=True)

        ext = _FORMAT_EXT[fmt]
        saved: list[str] = []
        last_error: Exception | None = None
        used_names: set[str] = set()
        count = 0

        for var in variables:
            for sw in sweeps:
                count += 1
                pct = int(count / total * 90) + 5  # 5-95%
                progress(pct, f"Exporting {var} sweep {sw} ({count}/{total})")

                # Variable names come from the client: never let them escape output_dir.
                base = f"{_safe_stem(var)}_sweep{sw:02d}"
                filename = f"{base}.{ext}"
                n = 2
                while filename.lower() in used_names:  # distinct names that sanitise equally
                    filename = f"{base}_{n}.{ext}"
                    n += 1
                used_names.add(filename.lower())
                filepath = str(Path(output_dir) / filename)

                try:
                    path = self.export(
                        datatree,
                        fmt=fmt,
                        dpi=dpi,
                        output_path=filepath,
                        variable=var,
                        sweep=sw,
                        progress=_noop_progress,
                        options=options,
                    )
                    saved.append(path)
                except Exception as exc:
                    logger.warning("Batch export failed for %s sweep %d: %s", var, sw, exc)
                    last_error = exc
                    # Continue with remaining exports

        if not saved and last_error is not None:
            raise RuntimeError(f"Batch export failed for all {total} item(s): {last_error}") from (
                last_error
            )

        progress(100, f"Batch export complete: {len(saved)}/{total} files")
        return saved

    # ------------------------------------------------------------------
    # Animation export (GIF)
    # ------------------------------------------------------------------

    def export_animation(
        self,
        datatree: xr.DataTree,
        *,
        variable: str,
        sweeps: list[int],
        output_path: str | None = None,
        frame_duration_ms: int = 500,
        dpi: int = 150,
        fig_size: float = 8.0,
        progress: ProgressCallback = _noop_progress,
        options: dict[str, Any] | None = None,
    ) -> str:
        """Export an animated GIF cycling through sweeps for a given variable."""
        from PIL import Image as PILImage

        if options is None:
            options = {}
        if not sweeps:
            raise ValueError("No sweeps specified for animation")
        fig_size = _opt_float({"fig_size": fig_size}, "fig_size", 8.0, 2.0, 30.0)

        # One colour scale for the whole animation, otherwise variables without a
        # fixed range (e.g. unknown names) would rescale every frame and flicker.
        vmin, vmax = _get_range(variable)
        if vmin is None or vmax is None:
            lo, hi = np.inf, -np.inf
            for sw in sweeps:
                sweep_ds = _extract_sweep_dataset(datatree, sw)
                if variable not in sweep_ds.data_vars:
                    continue
                vals = mask_variable(sweep_ds[variable]).values
                finite = vals[np.isfinite(vals)]
                if finite.size:
                    lo, hi = min(lo, float(finite.min())), max(hi, float(finite.max()))
            vmin, vmax = (lo, hi) if np.isfinite(lo) else (None, None)

        frames: list[PILImage.Image] = []
        total = len(sweeps)

        for i, sw in enumerate(sweeps):
            pct = int((i + 1) / total * 85) + 5
            progress(pct, f"Rendering frame {i + 1}/{total} (sweep {sw})")

            fig = render_publication_figure(
                datatree,
                variable,
                sw,
                dpi=dpi,
                fig_size=fig_size,
                font_size=int(_opt_float(options, "font_size", DEFAULT_FONT_SIZE, 6, 48)),
                show_rings=options.get("show_rings", True),
                show_colorbar=True,
                show_title=True,
                dark_background=options.get("dark_background", False),
                vmin=vmin,
                vmax=vmax,
                rhi_clean=options.get("rhi_clean"),
                rhi_view=options.get("rhi_view"),
                value_mask=valuemask.pick(options, variable),
            )

            # Render figure to an opaque RGB PIL image (alpha composited on white)
            try:
                with io.BytesIO() as buf:
                    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
                    buf.seek(0)
                    with PILImage.open(buf) as png:
                        rgba = png.convert("RGBA")
            finally:
                _pyplot().close(fig)
            rgb = PILImage.new("RGB", rgba.size, (255, 255, 255))
            rgb.paste(rgba, mask=rgba.getchannel("A"))
            rgba.close()
            frames.append(rgb)

        progress(90, "Assembling GIF")

        # bbox_inches="tight" can yield slightly different frame sizes (title or
        # tick-label widths).  GIF frames are drawn on top of each other
        # (disposal "none"), so a smaller frame would leave remnants of the
        # previous one visible: pad every frame to a common canvas.
        width = max(f.width for f in frames)
        height = max(f.height for f in frames)
        for k, frame in enumerate(frames):
            if frame.size != (width, height):
                canvas = PILImage.new("RGB", (width, height), frame.getpixel((0, 0)))
                canvas.paste(frame, ((width - frame.width) // 2, (height - frame.height) // 2))
                frame.close()
                frames[k] = canvas

        try:
            with _output_target(output_path, suffix=".gif", prefix="xradar_anim_") as target:
                frames[0].save(
                    target,
                    format="GIF",
                    save_all=True,
                    append_images=frames[1:],
                    duration=frame_duration_ms,
                    loop=0,
                    optimize=True,
                    disposal=1,
                    comment=_gif_comment(datatree, variable, sweeps, options),
                )
        finally:
            n_frames = len(frames)
            for frame in frames:
                frame.close()

        output_path = str(Path(target.final).resolve())
        progress(100, f"Animation export complete: {output_path}")
        logger.info("Exported GIF animation to %s (%d frames)", output_path, n_frames)
        return output_path

    # ------------------------------------------------------------------
    # Data format exports
    # ------------------------------------------------------------------

    def _export_netcdf(
        self,
        datatree: xr.DataTree,
        *,
        fmt: str,
        dpi: int,
        output_path: str | None,
        variable: str,
        sweep: int,
        progress: ProgressCallback,
        options: dict[str, Any],
    ) -> str:
        progress(10, "Preparing CfRadial1 NetCDF export")
        mask = bool(options.get("mask_no_echo", True))
        vm, vm_params = _volume_value_masks(options, datatree)
        with (
            _output_target(output_path, suffix=".nc", prefix="xradar_export_") as target,
            _fast_cfradial1_mapper(),
        ):
            cfwrite.write_cfradial1(
                _netcdf_safe_tree(datatree, mask=mask, value_masks=vm),
                target,
                source_files=options.get("source_files"),
                params={"format": "cfradial1", "mask_no_echo": mask, **vm_params},
                masked=mask,
            )

        progress(100, f"CfRadial1 export complete: {target.final}")
        return str(Path(target.final).resolve())

    def _export_cfradial2(
        self,
        datatree: xr.DataTree,
        *,
        fmt: str,
        dpi: int,
        output_path: str | None,
        variable: str,
        sweep: int,
        progress: ProgressCallback,
        options: dict[str, Any],
    ) -> str:
        progress(10, "Preparing CfRadial2 export")
        mask = bool(options.get("mask_no_echo", True))
        vm, vm_params = _volume_value_masks(options, datatree)
        with _output_target(output_path, suffix=".nc", prefix="xradar_cfradial2_") as target:
            cfwrite.write_cfradial2(
                _netcdf_safe_tree(datatree, mask=mask, value_masks=vm),
                target,
                source_files=options.get("source_files"),
                params={"format": "cfradial2", "mask_no_echo": mask, **vm_params},
                masked=mask,
            )

        progress(100, f"CfRadial2 export complete: {target.final}")
        return str(Path(target.final).resolve())

    def _export_csv(
        self,
        datatree: xr.DataTree,
        *,
        fmt: str,
        dpi: int,
        output_path: str | None,
        variable: str,
        sweep: int,
        progress: ProgressCallback,
        options: dict[str, Any],
    ) -> str:
        """Export sweep data as a CSV table: azimuth, range, value."""
        import pandas as pd

        progress(10, "Preparing CSV export")

        sweep_ds = _extract_sweep_dataset(datatree, sweep)
        if variable not in sweep_ds.data_vars:
            raise KeyError(f"Variable '{variable}' not found in sweep {sweep}")

        da = mask_variable(sweep_ds[variable])  # no-echo / range-folded codes -> NaN -> no CSV row
        values = da.values
        # the value mask of this variable: hidden gates become NaN and get no row
        values, _n_hidden = valuemask.apply(values, valuemask.pick(options, variable))

        azimuth = (
            da.coords["azimuth"].values.astype(float)
            if "azimuth" in da.coords
            else np.arange(values.shape[0], dtype=float)
        )
        range_m = (
            da.coords["range"].values.astype(float)
            if "range" in da.coords
            else np.arange(values.shape[-1], dtype=float)
        )

        progress(40, "Building data table")

        # Index the 1-D coordinates by the finite gates instead of materialising
        # two full-size meshgrids and boolean-masking each of them.
        ray_idx, gate_idx = np.nonzero(np.isfinite(values))

        df = pd.DataFrame(
            {
                "azimuth_deg": azimuth[ray_idx],
                "range_m": range_m[gate_idx],
                variable: values[ray_idx, gate_idx],
            }
        )

        progress(70, "Writing CSV")

        header = _csv_header(datatree, sweep_ds, da, variable, sweep, options, len(df))
        with (
            _output_target(output_path, suffix=".csv", prefix="xradar_export_") as target,
            open(target, "w", encoding="utf-8", newline="") as fh,
        ):
            fh.write(header)
            df.to_csv(fh, index=False, float_format="%.4f", lineterminator="\n")

        output_path = str(Path(target.final).resolve())
        progress(100, f"CSV export complete: {output_path}")
        logger.info("Exported CSV to %s (%d rows)", output_path, len(df))
        return output_path

    def _export_geotiff(
        self,
        datatree: xr.DataTree,
        **kwargs: Any,
    ) -> str:
        raise NotImplementedError("GeoTIFF export is planned for a future release")

    def _export_zarr(
        self,
        datatree: xr.DataTree,
        *,
        fmt: str,
        dpi: int,
        output_path: str | None,
        variable: str,
        sweep: int,
        progress: ProgressCallback,
        options: dict[str, Any],
    ) -> str:
        progress(10, "Preparing Zarr export")
        vm, vm_params = _volume_value_masks(options, datatree)
        with _output_target(
            output_path, suffix=".zarr", prefix="xradar_export_", directory=True
        ) as target:
            mask = bool(options.get("mask_no_echo", True))
            cfwrite.write_zarr(
                _netcdf_safe_tree(datatree, mask=mask, value_masks=vm),
                target,
                source_files=options.get("source_files"),
                params={"format": "zarr", "mask_no_echo": mask, **vm_params},
                masked=mask,
                zarr_format=3 if options.get("zarr_format") == 3 else 2,
            )

        progress(100, f"Zarr export complete: {target.final}")
        return str(Path(target.final).resolve())
