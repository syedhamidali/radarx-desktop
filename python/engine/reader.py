"""Radar file reader using xradar with auto-format detection."""

from __future__ import annotations

import atexit
import logging
import math
import os
import shutil
import tempfile
import threading
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

from engine import rhi

logger = logging.getLogger(__name__)

_SWEEP_PREFIX = "sweep_"

# Map of xradar opener functions keyed by format name.
# Each entry: (format_label, opener_function_name, supported_extensions)
_FORMAT_TABLE: list[tuple[str, str, set[str]]] = [
    ("nexrad_level2", "open_nexradlevel2_datatree", {".gz", ".bz2", ""}),
    ("cfradial1", "open_cfradial1_datatree", {".nc", ".ncf", ".cdf"}),
    ("odim", "open_odim_datatree", {".h5", ".hdf5", ".hdf"}),
    ("gamic", "open_gamic_datatree", {".h5", ".hdf5", ".hdf"}),
    ("iris", "open_iris_datatree", {".RAW", ".raw"}),
    ("furuno", "open_furuno_datatree", {".scn", ".scnx"}),
    ("rainbow", "open_rainbow_datatree", {".vol", ".azi"}),
    ("cfradial2", "open_cfradial2_datatree", set()),
    ("uf", "open_uf_datatree", {".uf"}),
    ("imd", "open_imd_datatree", set()),
    ("datamet", "open_datamet_datatree", set()),
    ("hpl", "open_hpl_datatree", set()),
    ("metek", "open_metek_datatree", set()),
]
_OPENER_NAMES: dict[str, str] = {label: fname for label, fname, _ in _FORMAT_TABLE}
# Format labels whose xradar *engine* name differs from the label.  Everything else
# uses the label itself as the engine name (cfradial1, odim, gamic, iris, ...).
_ENGINE_BY_FORMAT: dict[str, str] = {"nexrad_level2": "nexradlevel2"}
_FORMAT_BY_ENGINE: dict[str, str] = {v: k for k, v in _ENGINE_BY_FORMAT.items()}

# Temp files that could not be unlinked right away (Windows keeps open files locked).
_LEFTOVER_TMP_FILES: set[str] = set()


@atexit.register
def _cleanup_leftover_tmp_files() -> None:
    for tmp in list(_LEFTOVER_TMP_FILES):
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        except OSError:
            continue
        _LEFTOVER_TMP_FILES.discard(tmp)


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        _LEFTOVER_TMP_FILES.add(path)


# ----------------------------------------------------------------------
# Sweep ordering helpers (public: other engine modules should use these)
# ----------------------------------------------------------------------


def sweep_index(name: str) -> int:
    """Return the numeric index of a ``sweep_N`` node name (huge if unparsable)."""
    try:
        return int(name[len(_SWEEP_PREFIX) :])
    except ValueError:
        return 1 << 30


def mask_variable(v: xr.DataArray) -> xr.DataArray:
    """NaN-out the reserved "no measurement" codes of an integer-encoded radar moment.

    Level-II style files store moments as unsigned integers (``value = offset + code * scale``).
    Code 0 means *below threshold / no echo* and code 1 *range folded*. Decoders turn them into
    real numbers (-33 dBZ for reflectivity, -8 dB for ZDR, 0.2 for RhoHV, -64.5 m/s for velocity)
    that then get averaged, histogrammed and interpolated as if they were measurements, and they
    make up ~90 % of the gates of a sweep. The encoding on the variable says exactly which values
    those are; arrays without such an encoding (synthetic data, already-masked data) are returned
    unchanged.
    """
    enc = v.encoding
    scale, offset, dtype = enc.get("scale_factor"), enc.get("add_offset"), enc.get("dtype")
    if scale is None or offset is None or dtype is None or v.dtype.kind != "f":
        return v
    try:
        if np.dtype(dtype).kind != "u":
            return v
    except TypeError:
        return v
    scale, offset = float(scale), float(offset)
    if scale == 0.0 or not np.isfinite(scale) or not np.isfinite(offset):
        return v
    tol = 0.25 * abs(scale)
    vals = v.variable
    bad = (abs(vals - offset) <= tol) | (abs(vals - (offset + scale)) <= tol)
    return v.where(~bad, other=np.nan).assign_attrs(v.attrs)


def mask_below_threshold(ds: xr.Dataset) -> xr.Dataset:
    """:func:`mask_variable` applied to every 2-D data variable of *ds*."""
    masked = {name: mask_variable(v) for name, v in ds.data_vars.items() if v.ndim >= 2}
    changed = {k: m for k, m in masked.items() if m is not ds[k]}
    return ds.assign(changed) if changed else ds


DEFAULT_OPEN_FRACTION = 0.6


def system_memory_bytes() -> int | None:
    """Physical RAM in bytes (``os.sysconf`` on POSIX, ``GlobalMemoryStatusEx`` on Windows)."""
    try:
        return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, ValueError, OSError):
        pass
    try:  # Windows has no sysconf
        import ctypes

        class _MemStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemStatus()
        status.dwLength = ctypes.sizeof(_MemStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return int(status.ullTotalPhys)
    except Exception:  # pragma: no cover - platform specific
        pass
    return None


def max_open_bytes() -> int | None:
    """Budget for the decoded size of all open volumes, or ``None`` for "no limit".

    ``XRADAR_MAX_OPEN_MB`` (a value <= 0 disables the budget); otherwise 60 % of physical RAM.
    """
    raw = os.environ.get("XRADAR_MAX_OPEN_MB", "").strip()
    if raw:
        try:
            mb = float(raw)
        except ValueError:
            logger.warning("Ignoring invalid XRADAR_MAX_OPEN_MB=%r", raw)
        else:
            if math.isfinite(mb):
                return int(mb * 1024 * 1024) if mb > 0 else None
    total = system_memory_bytes()
    return int(total * DEFAULT_OPEN_FRACTION) if total else None


def tree_memory_bytes(dtree: xr.DataTree) -> int:
    """Decoded size of every array of *dtree* (what a full read would hold in RAM).

    Backend-lazy arrays count at their decoded size (an upper bound for netCDF/HDF5 files that
    stay on disk until read; NEXRAD Level II volumes really are ~this big once touched).
    Dask-backed arrays are chunked/out-of-core and count as zero. Reads metadata only.
    """
    total = 0
    for node in dtree.subtree:
        for var in node.to_dataset().variables.values():
            if var.chunks is None:
                total += int(var.nbytes)
    return total


_SITE_POSITION_VARS = ("latitude", "longitude", "altitude", "altitude_agl")


def collapse_site_position(ds: xr.Dataset) -> xr.Dataset:
    """Turn a per-ray site position into the single value a radar site really has.

    Some files store ``latitude``/``longitude``/``altitude`` along ``time`` with fill values (NaN,
    ``-9999``) on some rays. Everything downstream (maps, 3D, modules, NetCDF export) wants one
    scalar, so fill values are ignored and the most common valid value is kept. A variable with no
    valid value at all is dropped.
    """
    for name in _SITE_POSITION_VARS:
        var = ds.variables.get(name)
        if var is None or "time" not in var.dims:
            continue
        values = np.asarray(var.values, dtype="float64")
        valid = values[np.isfinite(values) & (values > -9990.0)]
        ds = ds.drop_vars(name)
        if valid.size:
            uniq, counts = np.unique(valid, return_counts=True)
            ds = ds.assign({name: ((), float(uniq[np.argmax(counts)]), var.attrs)})
    return ds


def _with_scalar_site(dtree: xr.DataTree) -> xr.DataTree:
    """*dtree* with the root's per-ray site position collapsed to scalars (no-op if scalar)."""
    try:
        root = dtree.to_dataset()
        if not any(n in root.variables and "time" in root[n].dims for n in _SITE_POSITION_VARS):
            return dtree
        dtree.dataset = collapse_site_position(root)
    except Exception as exc:  # never fail an open over cosmetics
        logger.debug("could not collapse per-ray site position: %s", exc)
    return dtree


def sorted_sweep_names(dtree: xr.DataTree) -> list[str]:
    """Names of ``sweep_N`` children in numeric order (``sweep_2`` before ``sweep_10``).

    A plain ``sorted()`` is lexicographic and scrambles volumes with 11+ sweeps
    (e.g. NEXRAD VCPs), which makes sweep index N map to the wrong elevation.
    """
    names = [n for n in dtree.children if n.startswith(_SWEEP_PREFIX)]
    names.sort(key=lambda n: (sweep_index(n), n))
    return names


# ----------------------------------------------------------------------
# Format detection
# ----------------------------------------------------------------------


def _xradar_engines() -> set[str] | None:
    """Engine names ``xd.open_datatree`` accepts (empty if it cannot enumerate them),
    or ``None`` if the API is absent.

    xradar >= 0.13 provides ``xd.open_datatree(path, engine=...)``; older releases
    only have the per-format ``xd.io.open_*_datatree`` functions.
    """
    import xradar as xd

    if not hasattr(xd, "open_datatree"):
        return None
    lister = getattr(xd.io, "list_xradar_engines", None)
    if lister is None:
        return set()
    try:
        return {str(e) for e in lister()}
    except Exception as exc:
        logger.debug("list_xradar_engines failed: %s", exc)
        return set()


def preload_backends() -> None:
    """Import xradar and its readers ahead of the first ``open_file`` (call from a thread).

    The first open otherwise pays ~0.6-0.9 s of imports on top of reading the file.
    """
    try:
        import xradar  # noqa: F401

        _xradar_engines()
    except Exception as exc:  # opening a file reports the real problem later
        logger.debug("xradar preload failed: %s", exc)


@cache
def _get_opener(format_name: str):
    """Return ``opener(path) -> DataTree`` for *format_name* (raises if unavailable).

    Prefers ``xd.open_datatree(path, engine=...)`` when the installed xradar has it
    (the per-format ``open_*_datatree`` functions are deprecated there); otherwise
    falls back to the dedicated ``xd.io.open_*_datatree`` function.  Both routes go
    straight to the one backend, so the fast path costs the same.
    """
    import xradar as xd

    func_name = _OPENER_NAMES.get(format_name)
    if func_name is None:
        # An engine xradar knows about that is not in our table (future formats).
        engines = _xradar_engines()
        if engines and format_name in engines:
            return _engine_opener(xd, format_name)
        raise ValueError(f"Unknown format: {format_name}")

    engine = _ENGINE_BY_FORMAT.get(format_name, format_name)
    engines = _xradar_engines()
    if engines and engine in engines:
        return _engine_opener(xd, engine)
    # xradar.io exposes the dedicated openers as xradar.io.<name>
    opener = getattr(xd.io, func_name, None)
    if opener is None:
        raise ImportError(f"xradar.io.{func_name} not available in installed xradar version")
    return opener


def _engine_opener(xd: Any, engine: str):
    def opener(path: str) -> xr.DataTree:
        return xd.open_datatree(path, engine=engine)

    opener.__name__ = f"open_{engine}"
    return opener


def _discover_format(path: str) -> str | None:
    """Ask xradar (>= 0.13: ``discover_engine`` via every backend's ``guess_can_open``)
    which format *path* is.  Returns a format label, or ``None`` if unsupported/unknown."""
    try:
        import xradar as xd

        discover = getattr(xd.io, "discover_engine", None)
        if discover is None:
            return None
        engine = str(discover(path))
    except Exception as exc:  # no backend claims the file, or discovery itself failed
        logger.debug("Engine discovery failed for %s: %s", path, exc)
        return None
    return _FORMAT_BY_ENGINE.get(engine, engine)


class _TooLargeError(Exception):
    """A gzip archive inflated past the configured limit."""


def _decompress_if_needed(
    path: str, tmp_dir: str | None = None, max_bytes: int | None = None
) -> tuple[str, bool]:
    """If *path* is gzip-compressed, decompress to a temp file and return (tmp_path, True).
    Otherwise return (path, False). Caller is responsible for unlinking the temp file.

    *tmp_dir* places the temp file in a given folder (web mode: the session folder) and
    *max_bytes* refuses archives that inflate beyond it (a decompression bomb)."""
    import gzip
    import zlib

    try:
        with open(path, "rb") as f:
            magic = f.read(2)
    except OSError:
        return path, False
    if magic != b"\x1f\x8b":
        return path, False
    tmp_path: str | None = None
    try:
        suffix = Path(path).stem  # strip .gz → use inner name as suffix hint
        fd, tmp_path = tempfile.mkstemp(suffix=f"_{suffix}", dir=tmp_dir)
        os.close(fd)
        # Stream: never hold a whole decompressed volume in memory.
        with gzip.open(path, "rb") as gz, open(tmp_path, "wb") as out:
            if max_bytes is None:
                shutil.copyfileobj(gz, out, length=1 << 20)
            else:
                written = 0
                while block := gz.read(1 << 20):
                    written += len(block)
                    if written > max_bytes:
                        raise _TooLargeError(
                            f"Could not open {Path(path).name}: it expands to more than "
                            f"{max_bytes // (1024 * 1024)} MB when decompressed"
                        )
                    out.write(block)
        logger.info("Decompressed %s → %s", path, tmp_path)
        return tmp_path, True
    except _TooLargeError as exc:
        if tmp_path is not None:
            _unlink_quietly(tmp_path)
        raise ValueError(str(exc)) from exc
    except (EOFError, gzip.BadGzipFile, zlib.error) as exc:  # BadGzipFile is an OSError
        if tmp_path is not None:
            _unlink_quietly(tmp_path)
        raise ValueError(
            f"Could not open {Path(path).name}: the gzip archive is truncated or corrupt "
            f"({exc}). Re-download or re-copy the file (if it was still downloading, wait "
            "for it to finish)."
        ) from exc
    except OSError as exc:
        if tmp_path is not None:
            _unlink_quietly(tmp_path)
        raise OSError(
            f"Could not decompress {Path(path).name} into {tempfile.gettempdir()}: {exc}. "
            "Check the free disk space of that folder."
        ) from exc
    except Exception as exc:
        logger.warning("Failed to decompress %s: %s", path, exc)
        if tmp_path is not None:
            _unlink_quietly(tmp_path)
        return path, False


def _sniff_magic(path: str) -> str | None:
    """Read the first few bytes of a file to identify its format via magic bytes.

    Returns a format name from _FORMAT_TABLE or None if unrecognised.
    """
    try:
        with open(path, "rb") as f:
            header = f.read(16)
    except OSError:
        return None

    if not header:
        return None

    # HDF5: starts with \x89HDF\r\n\x1a\n
    # NetCDF4 also uses HDF5 container, so check extension to disambiguate.
    if header[:8] == b"\x89HDF\r\n\x1a\n":
        ext = Path(path).suffix.lower()
        if ext in (".nc", ".ncf", ".cdf"):
            return "cfradial1"
        # Default to ODIM for .h5/.hdf5; fallback handles GAMIC
        return "odim"

    # NetCDF classic: starts with 'CDF'
    if header[:3] == b"CDF":
        return "cfradial1"

    # NEXRAD Level 2: starts with 'AR2V' or 'ARCH'
    if header[:4] in (b"AR2V", b"ARCH"):
        return "nexrad_level2"

    # IRIS/Sigmet: struct header with product_hdr magic (27 as int16 LE at byte 0)
    if len(header) >= 4 and header[0:2] == b"\x1b\x00":
        return "iris"

    # Gzip-compressed: could be NEXRAD
    if header[:2] == b"\x1f\x8b":
        return "nexrad_level2"

    # Bzip2-compressed: could be NEXRAD
    if header[:3] == b"BZh":
        return "nexrad_level2"

    return None


_FORMAT_LABELS = {
    "nexrad_level2": "NEXRAD Level II",
    "cfradial1": "CfRadial / NetCDF",
    "odim": "ODIM_H5 / HDF5",
}
SUPPORTED_FORMATS_TEXT = (
    "NEXRAD Level II, CfRadial1/2 (.nc), ODIM_H5 (.h5), GAMIC, IRIS/Sigmet, Furuno, "
    "Rainbow, UF, IMD"
)


def _diagnose_unreadable(open_path: str, original_path: str) -> str:
    """One actionable sentence on why *open_path* is not a readable radar file."""
    try:
        with open(open_path, "rb") as f:
            head = f.read(512)
    except OSError:
        head = b""
    stripped = head.lstrip().lower()
    if stripped.startswith((b"<!doctype", b"<html", b"<?xml", b"<head", b"<body")):
        return (
            "it is a web page / XML document, not radar data. The download probably failed "
            "(login or error page saved instead of the file); download it again."
        )
    if head and all(b in b"\t\r\n" or 32 <= b < 127 for b in head):
        return (
            "it is a plain-text file, not a radar volume. Open the radar data file itself "
            f"({SUPPORTED_FORMATS_TEXT})."
        )
    sniffed = _sniff_magic(open_path)
    if sniffed:
        label = _FORMAT_LABELS.get(sniffed, sniffed)
        return (
            f"it looks like a {label} file but could not be read: it is probably truncated or "
            "corrupt (re-download or re-copy it), or a variant xradar does not support."
        )
    ext = Path(original_path).suffix.lower()
    ext_note = f" ('{ext}')" if ext else ""
    return (
        f"the file type{ext_note} is not recognised as radar data. Supported: "
        f"{SUPPORTED_FORMATS_TEXT}. If it should work, it may be damaged."
    )


def _guess_formats_by_extension(path: str) -> list[str]:
    """Return a priority-ordered list of format names to try (each at most once)."""
    return _guess_formats(path)[0]


def _guess_formats(path: str) -> tuple[list[str], bool]:
    """Like :func:`_guess_formats_by_extension`, plus whether magic bytes or the
    extension actually pointed at a format (False: the order is just table order)."""
    p = Path(path)
    suffixes = {s.lower() for s in p.suffixes}
    name_upper = p.name.upper()

    ordered: list[str] = []
    seen: set[str] = set()

    def add(fmt: str) -> None:
        if fmt not in seen:
            seen.add(fmt)
            ordered.append(fmt)

    # 1. Magic byte sniffing — fastest, most reliable
    magic_fmt = _sniff_magic(path)
    if magic_fmt:
        add(magic_fmt)

    # 2. Extension-based hints
    if not p.suffix or suffixes & {".gz", ".bz2"}:
        add("nexrad_level2")
    if suffixes & {".nc", ".ncf", ".cdf"}:
        add("cfradial1")
    if suffixes & {".h5", ".hdf5", ".hdf"}:
        add("odim")
        add("gamic")
    if suffixes & {".raw"} or "RAW" in name_upper:
        add("iris")
    if suffixes & {".scn", ".scnx"}:
        add("furuno")
    if suffixes & {".vol", ".azi"}:
        add("rainbow")

    if suffixes & {".uf"}:
        add("uf")

    hinted = bool(ordered)

    # 3. Remaining formats as fallbacks (deduped)
    for fmt, _, _ in _FORMAT_TABLE:
        add(fmt)

    return ordered, hinted


def _open_generic_netcdf(path: str) -> xr.DataTree:
    """Fallback opener for non-standard NetCDF radar files (e.g. IMD/IRIS-origin).

    These files are single-sweep NetCDFs with variables like Z, V, W, T
    and coordinate arrays like radialAzim, radialElev, gateSize.
    We wrap them into a DataTree that looks like xradar output.
    """
    with xr.open_dataset(path) as ds:
        # Identify 2D float moment variables
        moment_vars = [v for v in ds.data_vars if ds[v].ndim == 2 and ds[v].dtype.kind == "f"]
        if not moment_vars:
            raise ValueError("No 2D float variables found — not a radar file")

        # Find azimuth and range coordinates
        azimuth = None
        for candidate in ("radialAzim", "azimuth", "Azimuth", "azi"):
            if candidate in ds:
                azimuth = ds[candidate].values.astype(np.float64)
                break

        # Build range array from gate metadata or existing coord
        range_arr = None
        for candidate in ("range", "Range", "distance"):
            if candidate in ds and ds[candidate].ndim == 1:
                range_arr = ds[candidate].values.astype(np.float64)
                break
        if range_arr is None and "gateSize" in ds and "firstGateRange" in ds:
            gate_size = float(ds["gateSize"].values)
            first_range = float(ds["firstGateRange"].values)
            n_gates = ds[moment_vars[0]].shape[-1]
            range_arr = np.arange(n_gates, dtype=np.float64) * gate_size + first_range

        # Get elevation
        elevation = None
        for candidate in ("elevationAngle", "elevation", "Elevation", "elev"):
            if candidate in ds:
                val = ds[candidate].values
                elevation = float(val.flat[0]) if val.ndim > 0 else float(val)
                break

        # Build a single-sweep dataset with xradar-compatible coords
        sweep_ds_vars = {
            v: xr.DataArray(ds[v].values, dims=("azimuth", "range")) for v in moment_vars
        }

        coords: dict[str, Any] = {}
        if azimuth is not None:
            coords["azimuth"] = ("azimuth", azimuth)
        if range_arr is not None:
            coords["range"] = ("range", range_arr)

        sweep_ds = xr.Dataset(sweep_ds_vars, coords=coords)
        if elevation is not None:
            sweep_ds.attrs["sweep_fixed_angle"] = elevation

        # Preserve useful global attrs
        for attr_key in ("siteLat", "siteLon", "siteAlt", "title", "Conventions", "history"):
            if attr_key in ds.attrs:
                sweep_ds.attrs[attr_key] = ds.attrs[attr_key]

    # Wrap in DataTree (xarray >= 2024 API: children dict)
    sweep_node = xr.DataTree(dataset=sweep_ds, name="sweep_0")
    return xr.DataTree(children={"sweep_0": sweep_node}, name="root")


def _is_placeholder(ds: xr.Dataset) -> bool:
    """True for the attrs-only stand-ins used for not-yet-loaded ARCO sweeps."""
    return not ds.data_vars and "_sweep_variables" in ds.attrs


class RadarReader:
    """Opens radar files via xradar and provides access to sweep data and metadata.

    Thread-safety: ``get_sweep_data`` / ``get_sweep_dataset`` may be called from
    several executor threads at once.  Lazily-loaded ARCO sweeps are guarded by
    per-sweep locks so concurrent requests for the same sweep download it once.

    ``_dtree`` / ``_path`` / ``_format`` may be assigned from outside (the server
    shares trees between readers); all caches are therefore keyed on the identity
    of the tree object and rebuilt automatically when it changes.
    """

    def __init__(self) -> None:
        self._dtree: xr.DataTree | None = None
        self._path: str | None = None
        self._format: str | None = None
        # (tree, n_children, [sweep nodes]) — rebuilt when the tree object changes
        self._nodes_cache: tuple[xr.DataTree, int, list[xr.DataTree]] | None = None
        # (tree, ["ppi" | "rhi" per sweep]) -- see sweep_kinds()
        self._kinds_cache: tuple[xr.DataTree, list[str]] | None = None
        # ARCO lazy-loading state (reset by ``_commit`` whenever a new file is opened)
        self._arco_session: Any = None
        self._arco_scan_key: str | None = None
        self._arco_sweep_names: list[str] = []
        self._arco_loaded_sweeps: dict[int, xr.Dataset] = {}
        self._lock = threading.RLock()
        self._sweep_locks: dict[int, threading.Lock] = {}
        # (tree, decoded bytes) -- the tree walk is cheap but this is polled by health checks
        self._mem_cache: tuple[xr.DataTree, int] | None = None
        # Web mode: scratch files (decompression, S3 downloads) go here, not the system temp dir,
        # and a gzip file may not inflate beyond ``max_decompressed_bytes``.
        self.tmp_dir: str | None = None
        self.max_decompressed_bytes: int | None = None

    # ------------------------------------------------------------------
    # Memory accounting
    # ------------------------------------------------------------------

    def memory_bytes(self) -> int:
        """Decoded size of the open volume plus loaded ARCO sweeps (:func:`tree_memory_bytes`)."""
        tree = self._dtree
        if tree is None:
            return 0
        cached = self._mem_cache
        if cached is None or cached[0] is not tree:
            cached = self._mem_cache = (tree, tree_memory_bytes(tree))
        loaded = sum(
            int(v.nbytes)
            for ds in list(self._arco_loaded_sweeps.values())
            for v in ds.variables.values()
            if v.chunks is None
        )
        return cached[1] + loaded

    def release_caches(self) -> int:
        """Drop re-loadable per-sweep arrays (ARCO sweeps are re-fetched on demand).

        Returns the approximate number of bytes released. The open tree itself is untouched.
        """
        with self._lock:
            loaded, self._arco_loaded_sweeps = self._arco_loaded_sweeps, {}
        return sum(
            int(v.nbytes)
            for ds in loaded.values()
            for v in ds.variables.values()
            if v.chunks is None
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def open_file(self, path: str) -> dict[str, Any]:
        """Open a radar file, auto-detecting the format.

        Returns the schema dict (same shape as ``get_schema()``).  The reader's
        state is only replaced once a format has opened *and* its schema was
        built successfully, so a failed open leaves the previous file intact.

        Raises
        ------
        FileNotFoundError
            If *path* does not exist on the local filesystem.
        ValueError
            If no xradar backend can open the file or the path is invalid.
        """
        if not path or not isinstance(path, str):
            raise ValueError("Path must be a non-empty string")

        p = Path(path)

        # Directory traversal protection: require absolute paths and ensure
        # the resolved path doesn't escape via '..' tricks.
        if not p.is_absolute():
            raise ValueError(f"Only absolute file paths are accepted, got: {path}")
        if ".." in p.resolve().parts:
            raise ValueError(f"Path contains disallowed '..' components: {path}")

        if not p.exists():
            raise FileNotFoundError(f"File not found: {path}")
        if p.is_dir():
            raise ValueError(f"Path is a directory, not a radar file: {path}")
        if not os.access(path, os.R_OK):
            raise PermissionError(
                f"Permission denied reading {p.name}. Check the file's permissions (on macOS, "
                "allow xradar-desktop to access this folder under System Settings > Privacy & "
                "Security), or copy the file to a folder you own."
            )
        try:
            if p.stat().st_size == 0:
                raise ValueError(
                    f"Could not open {p.name}: the file is empty (0 bytes). A download or copy "
                    "probably failed; fetch it again."
                )
        except OSError:
            pass  # unreadable stat: the openers below report the real problem

        # Transparently decompress gzip files — xradar can't handle the gzip wrapper directly.
        open_path, is_tmp = _decompress_if_needed(path, self.tmp_dir, self.max_decompressed_bytes)

        errors: list[tuple[str, Exception]] = []
        tried: set[str] = set()

        def attempt(fmt: str) -> dict[str, Any] | None:
            """Open *path* as *fmt*; commit and return the schema, or None on failure."""
            tried.add(fmt)
            try:
                opener = _get_opener(fmt)
                logger.info("Trying format '%s' for %s", fmt, open_path)
                dtree = opener(open_path)
                schema = self._build_schema(dtree, fmt)
                if not schema["sweeps"]:
                    # Lenient backends (e.g. cfradial2) "open" any netCDF as an empty tree.
                    raise ValueError(f"no radar sweeps found (as {fmt})")
            except ImportError as exc:  # xradar too old for this format: not worth reporting
                logger.debug("Format '%s' unavailable: %s", fmt, exc)
                return None
            except Exception as exc:  # any backend may raise anything on a wrong format
                logger.debug("Format '%s' failed for %s: %s", fmt, open_path, exc)
                errors.append((fmt, exc))
                return None
            self._commit(dtree, path, fmt)
            logger.info("Opened %s as %s", path, fmt)
            return schema

        try:
            # Fast path: magic bytes / extension pick the dedicated backend directly.
            candidates, hinted = _guess_formats(open_path)
            discovered: str | None = None
            if not hinted:
                # Nothing recognisable: let xradar (>= 0.13) tell us which backend claims
                # the file instead of trying every format and eating each failure.
                discovered = _discover_format(open_path)
                if discovered:
                    candidates = [discovered] + [f for f in candidates if f != discovered]
            for fmt in candidates:
                if fmt in tried:
                    continue
                schema = attempt(fmt)
                if schema is not None:
                    return schema

            # A backend xradar knows about that this table does not (newer xradar).
            if hinted:
                discovered = _discover_format(open_path)
            if discovered and discovered not in tried:
                schema = attempt(discovered)
                if schema is not None:
                    return schema

            # Last resort: try generic NetCDF (IMD/IRIS-origin single-sweep files, etc.)
            try:
                logger.info("Trying generic NetCDF fallback for %s", open_path)
                dtree = _open_generic_netcdf(open_path)
                schema = self._build_schema(dtree, "generic_netcdf")
            except Exception as exc:
                logger.debug("Generic NetCDF fallback failed: %s", exc)
                errors.append(("generic_netcdf", exc))
            else:
                self._commit(dtree, path, "generic_netcdf")
                logger.info("Opened %s as generic NetCDF", path)
                return schema

            # Report the most informative failures (the sniffed/preferred formats come first).
            detail = "; ".join(f"{fmt}: {exc}" for fmt, exc in errors[:3])
            raise ValueError(
                f"Could not open {p.name}: {_diagnose_unreadable(open_path, path)} "
                f"({len(errors)} formats tried; first errors: {detail})"
            ) from (errors[0][1] if errors else None)
        finally:
            if is_tmp:
                _unlink_quietly(open_path)

    def open_s3_file(self, s3_path: str) -> dict[str, Any]:
        """Open a NEXRAD Level II file directly from S3 (anonymous access).

        Parameters
        ----------
        s3_path:
            Full S3 URL, e.g. ``s3://noaa-nexrad-level2/2024/01/01/KLOT/KLOT20240101_000000_V06``
        """
        from engine.cloud import _s3

        key = s3_path.removeprefix("s3://")
        bucket, _, obj_key = key.partition("/")
        if not bucket or not obj_key:
            raise ValueError(f"Invalid S3 path (expected s3://bucket/key): {s3_path}")
        filename = obj_key.split("/")[-1]

        fd, tmp_path = tempfile.mkstemp(suffix=f"_{filename}", dir=self.tmp_dir)
        os.close(fd)
        try:
            logger.info("Downloading s3://%s/%s -> %s", bucket, obj_key, tmp_path)
            try:
                _s3().download_file(bucket, obj_key, tmp_path)
            except Exception as exc:
                raise RuntimeError(f"Could not download {s3_path}: {exc}") from exc
            result = self.open_file(tmp_path)
            # Report the S3 URL, not the (deleted) temp file, as the file's origin.
            self._path = s3_path
            result["source"] = s3_path
            result["source_type"] = "nexrad_l2_s3"
            return result
        finally:
            _unlink_quietly(tmp_path)

    def open_arco_scan(self, station: str, scan_key: str) -> dict[str, Any]:
        """Open a VCP type from the NEXRAD ARCO icechunk store.

        Only sweep_0 is opened eagerly; all other sweeps are loaded on demand
        when ``get_sweep_data`` is called for them. This reduces open latency
        from 30-170 s (17 sequential S3 requests) to ~5-10 s (1 request).
        """
        try:
            import zarr
        except ImportError as exc:
            raise RuntimeError("icechunk and zarr required: pip install icechunk zarr") from exc

        from engine.cloud import _open_icechunk_session

        station_upper = station.upper()
        arco_bucket = "nexrad-arco"
        store_url = f"icechunk://{arco_bucket}/{station_upper}/{scan_key}"
        logger.info("Opening ARCO %s %s (lazy mode)", station_upper, scan_key)

        session = _open_icechunk_session(station_upper)

        root_zarr = zarr.open_group(session.store, mode="r")
        vcp_zarr = root_zarr[scan_key]
        sweep_groups = {
            name: grp for name, grp in vcp_zarr.groups() if name.startswith(_SWEEP_PREFIX)
        }
        sweep_names = sorted(sweep_groups, key=lambda n: (sweep_index(n), n))
        if not sweep_names:
            raise RuntimeError(f"No sweep groups found in {scan_key}")

        # Extract radar site coordinates from VCP-level arrays
        site: dict[str, float] = {}
        for coord_key in ("latitude", "longitude", "altitude"):
            if coord_key in vcp_zarr:
                try:
                    val = float(np.asarray(vcp_zarr[coord_key]).flat[0])
                except Exception as exc:
                    logger.debug("ARCO %s unreadable: %s", coord_key, exc)
                    continue
                if math.isfinite(val):
                    site[coord_key] = val

        # Read sweep metadata from zarr in-memory tree — instant, no extra S3 calls.
        sweep_elevations: dict[str, float | None] = {}
        sweep_variables: dict[str, list[str]] = {}
        for sweep_name in sweep_names:
            sweep_zarr = sweep_groups[sweep_name]
            elev: float | None = None
            for elev_key in ("sweep_fixed_angle", "fixed_angle"):
                if elev_key in sweep_zarr:
                    try:
                        elev = float(np.asarray(sweep_zarr[elev_key]).flat[0])
                        break
                    except Exception as exc:
                        logger.debug("ARCO %s/%s unreadable: %s", sweep_name, elev_key, exc)
            if elev is None and "elevation" in sweep_zarr:
                try:
                    elev = float(sweep_zarr["elevation"][0, 0])
                except Exception as exc:
                    logger.debug("ARCO %s/elevation unreadable: %s", sweep_name, exc)
            sweep_elevations[sweep_name] = elev
            # Variable names come from zarr array metadata (in-memory, no S3 round-trip)
            vars_in: list[str] = []
            try:
                # Use .arrays() (zarr v3 / icechunk) if available; fall back to
                # duck-typed key iteration so isinstance(zarr.Array) version mismatch
                # doesn't silently produce an empty list.
                arr_iter = (
                    sweep_zarr.arrays()
                    if hasattr(sweep_zarr, "arrays")
                    else ((k, sweep_zarr[k]) for k in sweep_zarr.keys())  # noqa: SIM118
                )
                for k, m in arr_iter:
                    if (
                        hasattr(m, "ndim")
                        and hasattr(m, "dtype")
                        and m.ndim >= 2
                        and np.dtype(m.dtype).kind == "f"
                    ):
                        vars_in.append(k)
            except Exception as exc:
                logger.debug("ARCO %s variable listing failed: %s", sweep_name, exc)
            sweep_variables[sweep_name] = sorted(
                v for v in vars_in if not v.startswith("ray_") and v != "sweep_fixed_angle"
            )

        # Open ONLY sweep_0 fully — all other sweeps get placeholder Datasets
        sweep0_group = f"{scan_key}/{sweep_names[0]}"
        ds0 = xr.open_dataset(session.store, engine="zarr", group=sweep0_group, consolidated=False)
        scan_time_str: str | None = None
        if "vcp_time" in ds0.dims:
            if "time" in ds0.coords:
                try:
                    t_scalar = ds0["time"].isel(vcp_time=-1).isel(azimuth=0).values
                    scan_time_str = str(np.datetime64(t_scalar, "s"))
                except Exception as exc:
                    logger.debug("ARCO scan time unreadable: %s", exc)
            ds0 = _squeeze_vcp_time(ds0)

        # Build DataTree: real Dataset for sweep_0, attrs-only placeholders for the rest
        # Site coordinates live on the root node so engine helpers that look for the
        # radar position in the tree (cross-sections, profiles, plugins) can find them.
        sweep_dict: dict[str, xr.Dataset] = {
            "/": xr.Dataset(attrs=dict(site)),
            sweep_names[0]: ds0,
        }
        for sweep_name in sweep_names[1:]:
            attrs: dict[str, Any] = {"_sweep_variables": sweep_variables.get(sweep_name, [])}
            elev = sweep_elevations.get(sweep_name)
            if elev is not None:
                attrs["sweep_fixed_angle"] = elev
            sweep_dict[sweep_name] = xr.Dataset(attrs=attrs)

        dtree = xr.DataTree.from_dict(sweep_dict)
        result = self._build_schema(dtree, "nexrad_arco")
        attributes = result["attributes"]
        for key in ("latitude", "longitude", "altitude"):
            if key in site:
                attributes[key] = site[key]
        if scan_time_str:
            attributes["scan_time"] = scan_time_str
        result["source"] = store_url
        result["source_type"] = "nexrad_arco"
        result["vcp"] = scan_key
        result["station"] = station_upper

        with self._lock:
            self._commit(dtree, store_url, "nexrad_arco")
            self._arco_session = session
            self._arco_scan_key = scan_key
            self._arco_sweep_names = sweep_names
            self._arco_loaded_sweeps = {}

        logger.info(
            "ARCO %s %s opened (%d sweeps, lazy)", station_upper, scan_key, len(sweep_names)
        )
        return result

    def close(self) -> None:
        """Release the open file: drop the tree, caches and ARCO session.

        The DataTree is deliberately not ``.close()``d: the server shares one tree
        between several readers (``_shared_reader._dtree = reader._dtree``), and
        closing it here would break the others.  Dropping our references lets
        xarray's file manager close the handles once the last owner lets go.
        """
        with self._lock:
            self._dtree = None
            self._path = None
            self._format = None
            self._reset_caches()

    def get_schema(self) -> dict[str, Any]:
        """Extract metadata from the currently-open datatree.

        Returns a dict with keys:
            variables  - list of variable name strings
            dimensions - dict mapping dim name to size
            attributes - dict of global attributes
            sweeps     - list of dicts with sweep index and elevation
            format     - format label the file was opened with (e.g. ``"cfradial1"``)
        """
        self._ensure_open()
        assert self._dtree is not None
        return self._build_schema(self._dtree, self._format)

    def get_sweep_dataset(self, sweep: int, *, mask: bool = True) -> xr.Dataset:
        """Return the full ``xr.Dataset`` of one sweep (all variables and coords).

        With ``mask=True`` (default) the reserved "no echo" / "range folded" codes of
        integer-encoded moments are NaN (see :func:`mask_variable`). This builds new arrays, so
        ask for ``mask=False`` when only metadata or one variable is needed.

        Placeholder sweeps of an ARCO scan are loaded on demand (once, thread-safe)
        and the real dataset is returned.  Non-ARCO sweeps return the node's dataset
        (a cheap view sharing the underlying arrays; no data is copied).

        Raises
        ------
        RuntimeError
            If no file is open.
        IndexError
            If *sweep* is out of range.
        """
        self._ensure_open()
        nodes = self._sweep_nodes()
        if not isinstance(sweep, (int, np.integer)) or sweep < 0 or sweep >= len(nodes):
            raise IndexError(f"Sweep index {sweep} out of range (0..{len(nodes) - 1})")
        ds = nodes[sweep].to_dataset()
        if _is_placeholder(ds) and self._format == "nexrad_arco":
            ds = self._load_arco_sweep(int(sweep))
        return mask_below_threshold(ds) if mask else ds

    def get_sweep_data(self, sweep: int, variable: str) -> xr.DataArray:
        """Return the DataArray for a specific sweep index and variable.

        Parameters
        ----------
        sweep : int
            Zero-based sweep index.
        variable : str
            Variable name (e.g. ``"DBZH"``, ``"reflectivity"``).

        Returns
        -------
        xr.DataArray
        """
        ds = self.get_sweep_dataset(sweep, mask=False)  # mask just the one variable below
        if variable not in ds.data_vars:
            available = sorted(str(v) for v in ds.data_vars)
            raise KeyError(f"Variable '{variable}' not in sweep {sweep}. Available: {available}")
        da = ds[variable]
        if self._arco_active(sweep):
            # Remote (zarr/S3) arrays: read once under a per-sweep lock so concurrent
            # renders of the same sweep don't download it several times.
            with self._sweep_lock(int(sweep)):
                da.load()
        return mask_variable(da)

    def sweep_kinds(self) -> list[str]:
        """Scan type of every sweep, ``"ppi"`` or ``"rhi"`` (cached per tree; ``[]`` if no file)."""
        tree = self._dtree
        if tree is None:
            return []
        cached = self._kinds_cache
        if cached is not None and cached[0] is tree:
            return cached[1]
        kinds: list[str] = []
        for node in self._sweep_nodes():
            try:
                kinds.append("rhi" if rhi.is_rhi(node.to_dataset()) else "ppi")
            except Exception:  # never let scan-type detection break a request
                kinds.append("ppi")
        self._kinds_cache = (tree, kinds)
        return kinds

    @property
    def datatree(self) -> xr.DataTree | None:
        """Access the underlying DataTree (may be None if no file is open)."""
        return self._dtree

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_open(self) -> None:
        if self._dtree is None:
            raise RuntimeError("No file is currently open. Call open_file() first.")

    def _reset_caches(self) -> None:
        self._nodes_cache = None
        self._kinds_cache = None
        self._arco_session = None
        self._arco_scan_key = None
        self._arco_sweep_names = []
        self._arco_loaded_sweeps = {}
        self._sweep_locks = {}

    def _commit(self, dtree: xr.DataTree, path: str, fmt: str) -> None:
        """Atomically make *dtree* the open file and drop everything derived from the old one."""
        dtree = _with_scalar_site(dtree)
        with self._lock:
            self._reset_caches()
            self._dtree = dtree
            self._path = path
            self._format = fmt

    def _sweep_lock(self, sweep: int) -> threading.Lock:
        with self._lock:
            lock = self._sweep_locks.get(sweep)
            if lock is None:
                lock = self._sweep_locks[sweep] = threading.Lock()
            return lock

    def _arco_active(self, sweep: int) -> bool:
        """True if an ARCO scan is open and *sweep* is one of its (remote, lazy) sweeps."""
        return (
            self._format == "nexrad_arco"
            and self._arco_session is not None
            and 0 <= sweep < len(self._arco_sweep_names)
        )

    def _load_arco_sweep(self, sweep_idx: int) -> xr.Dataset:
        """Load a single ARCO sweep on demand and cache it (thread-safe)."""
        with self._sweep_lock(sweep_idx):
            cached = self._arco_loaded_sweeps.get(sweep_idx)
            if cached is not None:
                return cached

            session = self._arco_session
            if session is None or not 0 <= sweep_idx < len(self._arco_sweep_names):
                raise RuntimeError(f"Sweep {sweep_idx} is not an ARCO sweep of the open file")
            sweep_name = self._arco_sweep_names[sweep_idx]
            group = f"{self._arco_scan_key}/{sweep_name}"
            logger.info("Lazy-loading ARCO sweep %d (%s)", sweep_idx, group)

            ds = xr.open_dataset(session.store, engine="zarr", group=group, consolidated=False)
            ds = _squeeze_vcp_time(ds)
            self._arco_loaded_sweeps[sweep_idx] = ds
            return ds

    def _sweep_nodes(self) -> list[xr.DataTree]:
        """Return child nodes that represent sweeps, in numeric order (cached per tree)."""
        tree = self._dtree
        assert tree is not None
        cached = self._nodes_cache
        n_children = len(tree.children)
        if cached is not None and cached[0] is tree and cached[1] == n_children:
            return cached[2]
        children = tree.children
        nodes = [children[name] for name in sorted_sweep_names(tree)]
        self._nodes_cache = (tree, n_children, nodes)
        return nodes

    def _build_schema(self, dtree: xr.DataTree, fmt: str | None = None) -> dict[str, Any]:
        """Build the schema dict for *dtree* without touching reader state.

        *fmt* is the format label the file was opened with (``nexrad_level2``,
        ``cfradial1``, ``odim``, ...), reported as ``format``.
        """
        children = dtree.children
        sweep_nodes = [children[name] for name in sorted_sweep_names(dtree)]

        # Extract variables and dimensions from first sweep.
        # Only include actual radar moment variables (2D float: azimuth x range).
        # Uses Dataset.variables (metadata only) — never constructs DataArrays or loads data.
        variables: set[str] = set()
        dimensions: dict[str, int] = {}
        ds0: xr.Dataset | None = None
        if sweep_nodes:
            ds0 = sweep_nodes[0].to_dataset()
            variables.update(_moment_names(ds0))
            for dim, size in ds0.sizes.items():
                dimensions[str(dim)] = int(size)

        # Extract per-sweep elevation and variable lists.
        # Placeholder ARCO sweeps store available variables in attrs["_sweep_variables"].
        sweeps: list[dict[str, Any]] = []
        for idx, node in enumerate(sweep_nodes):
            elevation: float | None = None
            sweep_vars: list[str] | None = None
            scan_fields: dict[str, Any] = {"kind": "ppi"}
            try:
                ds = ds0 if idx == 0 and ds0 is not None else node.to_dataset()
                scan_fields = rhi.describe_sweep(ds)  # kind 'ppi' | 'rhi' (+ plane azimuth, span)
                elevation = _finite_or_none(_sweep_elevation(ds))
                # Per-sweep variable list: from real data_vars or stored attrs
                if ds.data_vars:
                    sweep_vars = sorted(_moment_names(ds))
                elif "_sweep_variables" in ds.attrs:
                    sweep_vars = list(ds.attrs["_sweep_variables"])
            except Exception as exc:
                logger.debug("Could not read metadata of sweep %d: %s", idx, exc)
            entry: dict[str, Any] = {"index": idx, "elevation": elevation}
            entry.update(scan_fields)
            if sweep_vars:
                entry["variables"] = sweep_vars
                variables.update(sweep_vars)
            sweeps.append(entry)

        # Global attributes — from root node
        root_attrs: dict[str, Any] = {}
        root_ds: xr.Dataset | None = None
        try:
            root_ds = dtree.to_dataset()
            for k, v in root_ds.attrs.items():
                root_attrs[str(k)] = _serializable(v)
        except (ValueError, KeyError, AttributeError) as exc:
            logger.debug("Could not extract root attributes: %s", exc)

        # Extract radar site coordinates from coords/data_vars/attrs
        # xradar stores these as coordinate variables, not always as attrs
        if "latitude" not in root_attrs or "longitude" not in root_attrs:
            lat, lon = self._extract_site_coords(dtree, sweep_nodes, root_ds, ds0)
            if lat is not None:
                root_attrs["latitude"] = lat
            if lon is not None:
                root_attrs["longitude"] = lon

        if any(sw.get("kind") == "rhi" for sw in sweeps):
            # Range-height views can show height above sea level when the site altitude is known.
            altitude = _site_altitude(root_ds, ds0)
            if altitude is not None:
                for sw in sweeps:
                    if sw.get("kind") == "rhi":
                        sw["altitude"] = altitude

        return {
            "variables": sorted(variables),
            "dimensions": dimensions,
            "attributes": root_attrs,
            "sweeps": sweeps,
            "format": fmt,
        }

    @staticmethod
    def _extract_site_coords(
        dtree: Any,
        sweep_nodes: list[Any],
        root_ds: xr.Dataset | None = None,
        ds0: xr.Dataset | None = None,
    ) -> tuple[float | None, float | None]:
        """Try to find radar site lat/lon from the DataTree."""
        lat: float | None = None
        lon: float | None = None

        lat_keys = ("latitude", "origin_latitude", "siteLat")
        lon_keys = ("longitude", "origin_longitude", "siteLon")

        def find(ds: xr.Dataset, keys: tuple[str, ...]) -> float | None:
            for k in keys:
                if k in ds.variables:  # coords and data_vars alike
                    try:
                        return _finite_or_none(float(ds.variables[k].values.flat[0]))
                    except Exception as exc:
                        logger.debug("Site coordinate %s unreadable: %s", k, exc)
            return None

        # 1. Check root dataset coords and data_vars
        try:
            if root_ds is None:
                root_ds = dtree.to_dataset()
            lat = find(root_ds, lat_keys)
            lon = find(root_ds, lon_keys)
        except Exception as exc:
            logger.debug("Root site coordinates unavailable: %s", exc)

        # 2. Fallback: check first sweep's coords
        if (lat is None or lon is None) and sweep_nodes:
            try:
                if ds0 is None:
                    ds0 = sweep_nodes[0].to_dataset()
                if lat is None:
                    lat = find(ds0, lat_keys)
                if lon is None:
                    lon = find(ds0, lon_keys)
            except Exception as exc:
                logger.debug("Sweep site coordinates unavailable: %s", exc)

        return lat, lon


def _site_altitude(*datasets: xr.Dataset | None) -> float | None:
    """Radar site altitude above sea level (metres) from the first dataset that stores it."""
    for ds in datasets:
        if ds is None or "altitude" not in ds.variables:
            continue
        try:
            value = _finite_or_none(float(np.asarray(ds.variables["altitude"].values).flat[0]))
        except (TypeError, ValueError, IndexError):
            continue
        if value is not None:
            return value
    return None


def _moment_names(ds: xr.Dataset) -> list[str]:
    """Names of 2-D float data variables (radar moments); metadata-only, no data access."""
    variables = ds.variables
    return [
        str(name)
        for name in ds.data_vars
        if variables[name].ndim == 2 and variables[name].dtype.kind == "f"
    ]


def _sweep_elevation(ds: xr.Dataset) -> float | None:
    if "sweep_fixed_angle" in ds.attrs:
        return float(ds.attrs["sweep_fixed_angle"])
    if "sweep_fixed_angle" in ds.variables:
        return float(ds.variables["sweep_fixed_angle"].values.flat[0])
    if "elevation" in ds.variables:
        return float(ds.variables["elevation"].values.flat[0])
    return None


def _finite_or_none(value: float | None) -> float | None:
    """NaN/Inf become None so JSON.parse() doesn't reject the message."""
    if value is None or math.isnan(value) or math.isinf(value):
        return None
    return value


def _squeeze_vcp_time(ds: xr.Dataset) -> xr.Dataset:
    """Select the last vcp_time index and drop the dimension if present."""
    if "vcp_time" in ds.dims:
        return ds.isel(vcp_time=-1, drop=True)
    return ds


def _serializable(value: Any) -> Any:
    """Convert numpy types to plain Python for JSON serialization.

    NaN and Inf are replaced with None because JSON.parse() rejects them.
    """
    if isinstance(value, np.ndarray):
        return _serializable(value.tolist())
    if isinstance(value, np.generic):
        return _serializable(value.item())
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (list, tuple)):
        return [_serializable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _serializable(v) for k, v in value.items()}
    return value
