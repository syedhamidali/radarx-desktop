"""Resource governor: decide what to do with however much data is given.

Every heavy operation (reading, QC pipelines, mosaics, multi-Doppler winds, cell tracking,
animation, export) asks the governor three things before it starts:

1. **What does this machine allow?** :func:`machine_budget` - free RAM, cores, free disk in the
   output / cache folder, with safe fractions; ``--budget-gb`` / ``RADARX_BUDGET_GB`` override.
2. **What will this cost?** The ``estimate_*`` functions turn cheap metadata (rays x gates x
   fields x sweeps, decoded size, number of files and time steps, grid size and levels) into a
   peak-memory and a run-time estimate. The constants are calibrated on measured runs (see
   ``CAL`` below and the engine performance page).
3. **If it does not fit, what is the least damaging way to make it fit?** :func:`plan` tries the
   strategies in a fixed order (streaming, lazy reading, coarser grid, fewer levels, temporal
   thinning, spatial crop) and returns a :class:`Plan`. Every reduction is a :class:`Reduction`
   record (what, why, how much data was dropped, what is lost scientifically); it goes into the
   result's warnings, its provenance attributes and the plan text. Nothing is dropped silently.
   When nothing fits, the plan is a refusal that says what the user can change
   (:class:`BudgetRefusalError`), never an unexplained out-of-memory crash.

Guards while running: :func:`worker_cap` (parallel workers by memory, not only by cores),
:class:`Watchdog` (cooperative memory and cancel checks in loops; stops a step *before* memory
runs out) and :class:`EtaProgress` (progress messages with an ETA from the estimate).

Default behaviour is unchanged whenever the estimate fits the budget. ``RADARX_GOVERNOR=off``
disables the automatic reductions (the estimate is still logged), ``RADARX_GOVERNOR=strict``
refuses instead of reducing.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MB = 1024 * 1024
GB = 1024 * MB

ENV_BUDGET = "RADARX_BUDGET_GB"
ENV_MODE = "RADARX_GOVERNOR"  # "auto" (default) | "off" | "strict"

# Safe fractions: never plan to use more than this share of the RAM that is free now, nor more
# than the share of physical RAM the open-file budget already uses (reader.DEFAULT_OPEN_FRACTION).
FREE_FRACTION = 0.8
TOTAL_FRACTION = 0.6
DISK_FRACTION = 0.9  # leave 10 % of the free disk untouched

# ---------------------------------------------------------------------------------------------
# Calibration. Measured on an Apple M1 Pro (10 cores, 16 GB), Python 3.14, xradar 0.12, radarx
# 0.5, NEXRAD Level II KHPX20211211_015357_V06 (6.7 MB on disk, 19 sweeps, 720 x 1832 gates,
# decoded size D = 461 MB as counted by reader.tree_memory_bytes), plus KNQA/KPAH/KVWX of the
# same event, in fresh processes (peak RSS, wall time incl. opening the files):
#   open + full read           3.4 s   +660 MB      -> read_peak 1.45 D
#   "Reflectivity QC" all sweeps 6.6 s  +680 MB      -> qc time 0.009 s per decoded MB
#   mosaic composite, 2 radars 7.2 s   +760 MB  (723 x 628 cells, 31 levels)
#   mosaic composite, 4 radars 11.6 s  +1390 MB (723 x 796 cells)  -> ~0.68 D per radar
#   winds analytic, 2 radars   6.0 s   +760 MB  (default 1 km / 500 m grid)
# Measured on the same runs: a 2 km instead of a 1 km grid did NOT lower the peak (mosaic 1116
# vs 1054 MB, winds 882 vs 874 MB): at default grids the read of the volumes dominates, and
# the grid terms below only matter for large boxes or fine grids. The planner therefore only
# coarsens when its estimate says that saves memory, and refuses otherwise.
# and from the published engine performance numbers (same file, warm):
#   export CfRadial1 2.6 s / 3.8 GB, CfRadial2 0.5 s / 32 MB, Zarr 2.9 s / 31 MB, PNG 0.55 s /
#   144 MB, CSV 0.4 s / 28 MB; storm_detection 0.7 s / 287 MB per sweep.
# Estimates are deliberately on the high side (about +10 to +20 % against these runs).
# ---------------------------------------------------------------------------------------------
CAL: dict[str, float] = {
    "base_rss": 130 * MB,  # engine + numpy/xarray/xradar/radarx loaded
    "ref_decoded": 461 * MB,  # D of the calibration volume
    "open_s_per_file_mb_l2": 0.30,  # bz2 Level II: seconds per MB on disk (open + first read)
    "open_s_per_decoded_mb": 0.004,  # NetCDF/HDF5 and others: seconds per decoded MB
    "read_peak": 1.45,  # x D, a fully read volume
    "lazy_peak": 0.25,  # x D, a volume read lazily, one sweep / one moment at a time
    "qc_s_per_mb": 0.009,
    "qc_peak": 0.55,  # x D on top of the read
    "cells_s_per_volume": 0.7,
    "cells_peak": 0.62,  # x D (storm_detection 287 MB of 461)
    "mosaic_peak_per_radar": 0.68,  # x D of each radar (one moment, coordinates, gridding)
    "mosaic_band": 450 * MB,  # mosaic.BAND_BUDGET_BYTES
    "mosaic_s_per_voxel": 25e-9,  # per radar and voxel (radarx gridding)
    "mosaic_prepare_s": 0.65,  # per radar, masking + geometry of one moment
    "winds_peak_per_radar": 0.62,
    "winds_bytes_per_voxel_radar": 16.0,
    "winds_bytes_per_voxel": 48.0,  # output stack + solver arrays
    "winds_s_per_voxel": 0.4e-6,  # analytic
    "winds_variational_factor": 10.0,  # NOT calibrated: an assumption, said so in the estimate
    "animate_render_s_per_frame": 0.5,  # NOT calibrated
    "export_cfradial1_peak": 8.3,  # x D (3.8 GB for D = 461 MB)
    "export_cfradial1_s": 2.6,  # per reference volume
    "export_cfradial1_size": 2.2,  # x D on disk (997 MB file)
    "export_cfradial2_peak": 0.07,
    "export_cfradial2_s": 0.53,
    "export_zarr_peak": 0.07,
    "export_zarr_s": 2.9,
    "export_compact_size": 0.5,  # CfRadial2 / Zarr on disk, x D (compressed)
    "export_png_peak": 0.31,
    "export_png_s": 0.55,
    "export_csv_peak": 0.06,
    "export_csv_s": 0.4,
}

MAX_COARSEN = 2  # at most two doublings of a grid spacing (4 x coarser) before refusing
MAX_THIN = 6  # keep at least every 6th volume
OPERATIONS = ("read", "qc", "mosaic", "winds", "cells", "animate", "export")


# =============================================================================================
# Records
# =============================================================================================


class BudgetRefusalError(ValueError):
    """An operation would not fit this machine even after every allowed reduction."""

    def __init__(self, message: str, plan: Plan | None = None) -> None:
        super().__init__(message)
        self.plan = plan


class WatchdogStopError(ValueError):
    """A running step was stopped before it exhausted memory (or because it was cancelled)."""


@dataclass
class MachineBudget:
    total_ram: int | None
    available_ram: int | None
    budget_bytes: int
    cpu_count: int
    disk_path: str
    disk_free: int | None
    disk_budget: int | None
    source: str  # "auto" | "cli" | "env"
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def text(self) -> str:
        lines = [
            f"memory  budget {_fmt_bytes(self.budget_bytes)} ({self.source})"
            + f"  |  free now {_fmt_bytes(self.available_ram)} of {_fmt_bytes(self.total_ram)}",
            f"cores   {self.cpu_count}",
            f"disk    {_fmt_bytes(self.disk_free)} free in {self.disk_path}",
        ]
        return "\n".join(lines + [f"note    {n}" for n in self.notes])


@dataclass
class InputVolume:
    """Cheap metadata of one input volume."""

    path: str
    radar: str
    time: str | None
    size_bytes: int
    decoded_bytes: int
    fmt: str | None = None
    n_sweeps: int | None = None
    n_rays: int | None = None
    n_gates: int | None = None
    n_fields: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    probed: bool = False  # True: opened and measured; False: scaled from a probed file

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Reduction:
    """One stated change to the data or the processing, made to fit the budget."""

    kind: str  # streaming | lazy | float32 | coarser_grid | fewer_levels | thinning | crop | resume
    what: str
    why: str
    dropped_fraction: float  # share of the data (cells, volumes, gates) not used; 0 = lossless
    scientific_loss: str
    lossless: bool = False
    params: dict[str, Any] = field(default_factory=dict)
    dropped_unit: str = "of the data not used"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def sentence(self) -> str:
        drop = (
            "no data dropped"
            if self.lossless or self.dropped_fraction <= 0
            else f"{100 * self.dropped_fraction:.0f}% {self.dropped_unit}"
        )
        return (
            f"Resource limit: {self.what} ({drop}). Why: {self.why}. Effect: {self.scientific_loss}"
        )


@dataclass
class Estimate:
    operation: str
    peak_bytes: int
    seconds: float
    detail: dict[str, Any] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["peak_mb"] = round(self.peak_bytes / MB)
        return d


@dataclass
class Plan:
    operation: str
    estimate: Estimate  # as asked
    final: Estimate  # after the reductions
    budget: MachineBudget
    reductions: list[Reduction] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)  # the parameters to run with
    refusal: str | None = None
    options: list[str] = field(default_factory=list)  # what the user can change

    @property
    def fits(self) -> bool:
        return self.refusal is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "fits_as_asked": self.estimate.peak_bytes <= self.budget.budget_bytes,
            "estimate": self.estimate.to_dict(),
            "final": self.final.to_dict(),
            "reductions": [r.to_dict() for r in self.reductions],
            "params": self.params,
            "refusal": self.refusal,
            "options": self.options,
        }

    def text(self) -> str:
        e, f = self.estimate, self.final
        head = (
            f"{self.operation:<8} peak {_fmt_bytes(e.peak_bytes):>9}  time {_fmt_s(e.seconds):>8}"
        )
        if self.refusal:
            out = [head + "  -> WILL NOT RUN", f"         {self.refusal}"]
            out += [f"         - {o}" for o in self.options]
            return "\n".join(out)
        if not self.reductions:
            return head + "  -> fits as asked"
        out = [
            head
            + f"  -> fits with {len(self.reductions)} change(s): peak {_fmt_bytes(f.peak_bytes)}, time {_fmt_s(f.seconds)}"  # noqa: E501
        ]
        out += [
            f"         - {r.what} ({'lossless' if r.lossless else 'stated reduction'}): {r.scientific_loss}"  # noqa: E501
            for r in self.reductions
        ]
        return "\n".join(out)

    def warnings(self) -> list[str]:
        return [r.sentence() for r in self.reductions]


# =============================================================================================
# 1. Machine budget
# =============================================================================================


def _available_ram() -> int | None:
    try:
        import psutil  # type: ignore[import-not-found]

        return int(psutil.virtual_memory().available)
    except Exception:
        pass
    if sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
        except OSError:
            return None
    if sys.platform == "darwin":
        try:
            txt = subprocess.run(
                ["vm_stat"], capture_output=True, text=True, timeout=2, check=False
            ).stdout
            page = int(re.search(r"page size of (\d+)", txt).group(1))  # type: ignore[union-attr]
            pages = 0
            for key in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"):
                m = re.search(rf"{key}:\s+(\d+)", txt)
                if m:
                    pages += int(m.group(1))
            return pages * page
        except Exception:
            return None
    if sys.platform == "win32":
        try:
            import ctypes

            class _M(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                    (n, ctypes.c_ulonglong) for n in ("tp", "ap", "tf", "af", "tv", "av", "ax")
                ]

            s = _M()
            s.l = ctypes.sizeof(_M)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s)):  # type: ignore[attr-defined]
                return int(s.ap)
        except Exception:
            return None
    return None


def current_rss() -> int | None:
    """Resident memory of this process now (or its high-water mark when only that is known)."""
    try:
        import psutil  # type: ignore[import-not-found]

        return int(psutil.Process().memory_info().rss)
    except Exception:
        pass
    try:
        with open("/proc/self/statm") as fh:
            return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, AttributeError):
        pass
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)
    except Exception:
        return None


def _env_budget_gb() -> float | None:
    raw = os.environ.get(ENV_BUDGET, "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", ENV_BUDGET, raw)
        return None
    return v if math.isfinite(v) and v > 0 else None


def machine_budget(
    path: str | os.PathLike[str] | None = None, budget_gb: float | None = None
) -> MachineBudget:
    """What this machine allows now: memory budget, cores and free disk in *path*.

    The memory budget is ``min(80 % of the free RAM, 60 % of the physical RAM)`` unless
    *budget_gb* (CLI ``--budget-gb``) or ``RADARX_BUDGET_GB`` gives it explicitly.
    """
    from engine.reader import system_memory_bytes

    total = system_memory_bytes()
    avail = _available_ram()
    notes: list[str] = []
    env = _env_budget_gb()
    if budget_gb is not None and budget_gb > 0:
        budget, source = int(budget_gb * GB), "cli"
    elif env is not None:
        budget, source = int(env * GB), "env"
    else:
        source = "auto"
        candidates = []
        if avail:
            candidates.append(int(avail * FREE_FRACTION))
        if total:
            candidates.append(int(total * TOTAL_FRACTION))
        budget = min(candidates) if candidates else 2 * GB
        if not candidates:
            notes.append("free memory could not be measured: assuming a 2 GB budget")
        # Never plan with less than what the engine itself needs to start.
        budget = max(budget, int(CAL["base_rss"] * 2))
    if source != "auto" and avail and budget > avail:
        notes.append(
            f"the budget ({_fmt_bytes(budget)}) is larger than the free memory now "
            f"({_fmt_bytes(avail)}): the system may swap"
        )
    disk_path = Path(path or os.getcwd()).expanduser()
    probe = disk_path if disk_path.is_dir() else disk_path.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        free = None
    return MachineBudget(
        total_ram=total,
        available_ram=avail,
        budget_bytes=int(budget),
        cpu_count=os.cpu_count() or 1,
        disk_path=str(probe),
        disk_free=free,
        disk_budget=int(free * DISK_FRACTION) if free is not None else None,
        source=source,
        notes=notes,
    )


def mode() -> str:
    m = os.environ.get(ENV_MODE, "auto").strip().lower()
    return m if m in ("auto", "off", "strict") else "auto"


# =============================================================================================
# Inputs: cheap metadata
# =============================================================================================


def _is_level2(path: str) -> bool:
    name = Path(path).name.upper()
    return bool(re.search(r"_V0\d", name)) or name.endswith(".AR2V") or "LEVEL2" in name


def volume_from_reader(reader: Any, name: str | None = None) -> InputVolume:
    """Metadata of an already open :class:`engine.reader.RadarReader` (no data is read)."""
    schema: Mapping[str, Any] = {}
    with contextlib.suppress(Exception):
        schema = reader.get_schema()
    attrs = schema.get("attributes") or {}
    dims = schema.get("dimensions") or {}
    path = str(getattr(reader, "path", None) or getattr(reader, "_path", None) or name or "")
    try:
        size = os.path.getsize(path) if path and os.path.isfile(path) else 0
    except OSError:
        size = 0
    try:
        decoded = int(reader.memory_bytes())
    except Exception:
        decoded = int(CAL["ref_decoded"])
    meta = _name_meta(path or name or "")
    return InputVolume(
        path=path,
        radar=str(attrs.get("instrument_name") or (meta[0] if meta else name or "radar")),
        time=meta[1] if meta else None,
        size_bytes=size,
        decoded_bytes=decoded,
        fmt=schema.get("format"),
        n_sweeps=len(schema.get("sweeps") or []) or None,
        n_rays=dims.get("azimuth"),
        n_gates=dims.get("range"),
        n_fields=len(schema.get("variables") or []) or None,
        latitude=_float(attrs.get("latitude")),
        longitude=_float(attrs.get("longitude")),
        probed=True,
    )


def _name_meta(path: str) -> tuple[str, str] | None:
    try:
        from engine.animate_plan import parse_volume_name

        got = parse_volume_name(path)
    except Exception:
        got = None
    return (got[0], got[1].isoformat()) if got else None


def describe_inputs(paths: Sequence[str], probe: int = 1) -> list[InputVolume]:
    """Metadata of *paths*, opening at most *probe* files per radar (the rest scale by size).

    Opening a NEXRAD volume costs 1.5 to 2.5 s, so a folder of hundreds of files is described
    from one opened file per radar plus the file sizes (marked ``probed=False``).
    """
    from engine.reader import RadarReader

    vols: list[InputVolume] = []
    probed: dict[str, list[InputVolume]] = {}
    for p in paths:
        meta = _name_meta(p)
        radar = meta[0] if meta else Path(p).stem
        try:
            size = os.path.getsize(p)
        except OSError:
            size = 0
        if len(probed.get(radar, [])) < probe:
            rd = RadarReader()
            try:
                rd.open_file(p)
                v = volume_from_reader(rd, p)
                v.path, v.size_bytes = p, size
                if meta:
                    v.time = meta[1]
                probed.setdefault(radar, []).append(v)
                vols.append(v)
                continue
            except Exception as exc:
                logger.info("budget: could not open %s: %s", p, exc)
            finally:
                with contextlib.suppress(Exception):
                    rd.close()
        ref = (probed.get(radar) or [v for vs in probed.values() for v in vs] or [None])[0]
        if ref is not None and ref.size_bytes:
            ratio = ref.decoded_bytes / ref.size_bytes
            v = replace(ref, path=p, size_bytes=size, decoded_bytes=int(size * ratio), probed=False)
            v.radar, v.time = radar, (meta[1] if meta else None)
        else:
            # Nothing measured: Level II expands ~70x (bz2), other formats ~3x (compressed HDF5).
            ratio = 70.0 if _is_level2(p) else 3.0
            v = InputVolume(p, radar, meta[1] if meta else None, size, int(size * ratio))
        vols.append(v)
    return vols


# =============================================================================================
# 2. Cost model
# =============================================================================================


def _open_seconds(v: InputVolume) -> float:
    if v.fmt == "nexrad_level2" or _is_level2(v.path):
        return max(0.3, CAL["open_s_per_file_mb_l2"] * v.size_bytes / MB)
    return max(0.1, CAL["open_s_per_decoded_mb"] * v.decoded_bytes / MB)


def _site_box_km(vols: Sequence[InputVolume], radius_km: float) -> tuple[float, float]:
    """East-west and north-south size (km) of the box covering every radar out to *radius_km*."""
    lats = [v.latitude for v in vols if v.latitude is not None]
    lons = [v.longitude for v in vols if v.longitude is not None]
    if not lats or not lons:
        return 2 * radius_km, 2 * radius_km
    lat0 = sum(lats) / len(lats)
    ew = (max(lons) - min(lons)) * 111.32 * math.cos(math.radians(lat0)) + 2 * radius_km
    ns = (max(lats) - min(lats)) * 110.57 + 2 * radius_km
    return ew, ns


def _unique_radars(vols: Sequence[InputVolume]) -> list[InputVolume]:
    seen: dict[str, InputVolume] = {}
    for v in vols:
        seen.setdefault(v.radar, v)
    return list(seen.values())


def estimate_read(
    vols: Sequence[InputVolume], *, all_at_once: bool = True, lazy: bool = False
) -> Estimate:
    per = CAL["lazy_peak"] if lazy else CAL["read_peak"]
    sizes = [v.decoded_bytes for v in vols] or [0]
    held = sum(sizes) if all_at_once else max(sizes)
    return Estimate(
        "read",
        int(CAL["base_rss"] + per * held),
        sum(_open_seconds(v) for v in vols),
        {
            "files": len(vols),
            "decoded_mb": round(sum(sizes) / MB),
            "all_at_once": all_at_once,
            "lazy": lazy,
        },
    )


def estimate_qc(vols: Sequence[InputVolume], *, all_at_once: bool = False) -> Estimate:
    sizes = [v.decoded_bytes for v in vols] or [0]
    held = sum(sizes) if all_at_once else max(sizes)
    return Estimate(
        "qc",
        int(CAL["base_rss"] + (CAL["read_peak"] + CAL["qc_peak"]) * held),
        sum(_open_seconds(v) + CAL["qc_s_per_mb"] * v.decoded_bytes / MB for v in vols),
        {"files": len(vols), "all_at_once": all_at_once, "recipe": "Reflectivity QC (all sweeps)"},
    )


def mosaic_grid(
    vols: Sequence[InputVolume], grid: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    g = {"resolution_m": 1000.0, "z_max_m": 15000.0, "z_step_m": 500.0, "max_range_km": 230.0}
    g.update({k: v for k, v in dict(grid or {}).items() if v is not None and k in g})
    extent = (grid or {}).get("extent")
    if isinstance(extent, Mapping) and extent:
        try:
            lat0 = (extent["south"] + extent["north"]) / 2
            ew = (extent["east"] - extent["west"]) * 111.32 * math.cos(math.radians(lat0))
            ns = (extent["north"] - extent["south"]) * 110.57
        except (KeyError, TypeError):
            ew, ns = _site_box_km(vols, g["max_range_km"])
    else:
        ew, ns = _site_box_km(vols, g["max_range_km"])
    res_km = g["resolution_m"] / 1000.0
    nx, ny = max(1, int(ew / res_km)), max(1, int(ns / res_km))
    nz = int(g["z_max_m"] // g["z_step_m"]) + 1
    return {**g, "nx": nx, "ny": ny, "nz": nz, "voxels": nx * ny * nz}


def estimate_mosaic(vols: Sequence[InputVolume], grid: Mapping[str, Any] | None = None) -> Estimate:
    radars = _unique_radars(vols)
    g = mosaic_grid(radars, grid)
    nr = len(radars)
    # The moment is read whole whatever the range limit: a crop shrinks the grid, not the read.
    per_radar = sum(CAL["mosaic_peak_per_radar"] * v.decoded_bytes for v in radars)
    band = min(CAL["mosaic_band"], g["voxels"] * nr * 4)
    planes = nr * g["nx"] * g["ny"] * 4 * 3
    volume = g["voxels"] if g["voxels"] <= 64_000_000 else 0
    peak = CAL["base_rss"] + per_radar + band + planes + volume
    secs = (
        sum(_open_seconds(v) + CAL["mosaic_prepare_s"] for v in radars)
        + CAL["mosaic_s_per_voxel"] * g["voxels"] * nr
        + 0.3
    )
    return Estimate("mosaic", int(peak), secs, {"radars": nr, "grid": g})


def winds_grid(
    vols: Sequence[InputVolume], grid: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    g = {"dx_m": 1000.0, "dz_m": 500.0, "z_min_m": 0.0, "z_max_m": 10000.0, "max_range_km": 100.0}
    g.update({k: v for k, v in dict(grid or {}).items() if v is not None and k in g})
    extent = (grid or {}).get("extent")
    if isinstance(extent, Mapping) and extent:
        try:
            ew = extent["x_max_km"] - extent["x_min_km"]
            ns = extent["y_max_km"] - extent["y_min_km"]
        except (KeyError, TypeError):
            ew, ns = _site_box_km(vols, g["max_range_km"])
    else:
        ew, ns = _site_box_km(vols, g["max_range_km"])
    nx, ny = max(1, int(ew * 1000 / g["dx_m"])), max(1, int(ns * 1000 / g["dx_m"]))
    nz = int(max(g["z_max_m"] - (g["z_min_m"] or 0.0), g["dz_m"]) // g["dz_m"]) + 1
    return {**g, "nx": nx, "ny": ny, "nz": nz, "voxels": nx * ny * nz}


def estimate_winds(
    vols: Sequence[InputVolume], grid: Mapping[str, Any] | None = None, method: str = "analytic"
) -> Estimate:
    radars = _unique_radars(vols)
    g = winds_grid(radars, grid)
    nr = len(radars)
    peak = (
        CAL["base_rss"]
        + sum(CAL["winds_peak_per_radar"] * v.decoded_bytes for v in radars)
        + g["voxels"] * (nr * CAL["winds_bytes_per_voxel_radar"] + CAL["winds_bytes_per_voxel"])
    )
    solve = CAL["winds_s_per_voxel"] * g["voxels"]
    assumptions = []
    if method == "variational":
        solve *= CAL["winds_variational_factor"]
        assumptions.append("variational time is an uncalibrated 10x the analytic solve")
    secs = sum(_open_seconds(v) + CAL["mosaic_prepare_s"] for v in radars) + solve
    return Estimate(
        "winds", int(peak), secs, {"radars": nr, "grid": g, "method": method}, assumptions
    )


def estimate_cells(vols: Sequence[InputVolume], *, every: int = 1) -> Estimate:
    used = list(vols)[:: max(1, every)]
    d = max((v.decoded_bytes for v in used), default=0)
    peak = CAL["base_rss"] + (CAL["read_peak"] + CAL["cells_peak"]) * d
    secs = sum(_open_seconds(v) + CAL["cells_s_per_volume"] for v in used)
    return Estimate(
        "cells", int(peak), secs, {"volumes": len(used), "every": every, "streaming": True}
    )


def estimate_animate(
    vols: Sequence[InputVolume],
    *,
    frames: int | None = None,
    grid: Mapping[str, Any] | None = None,
    every: int = 1,
) -> Estimate:
    radars = _unique_radars(vols)
    by_radar: dict[str, list[InputVolume]] = {}
    for v in vols:
        by_radar.setdefault(v.radar, []).append(v)
    steps = max((len(x) for x in by_radar.values()), default=0)
    steps_used = max(1, math.ceil(steps / max(1, every)))
    used = [v for x in by_radar.values() for v in x[:: max(1, every)]]
    g = mosaic_grid(radars, grid)
    step = estimate_mosaic(radars, grid)
    # two time steps are held for the interpolation between them
    peak = step.peak_bytes + g["voxels"] * 4 * 2
    frames = frames or 300
    secs = (
        sum(_open_seconds(v) + CAL["mosaic_prepare_s"] for v in used)
        + steps_used * CAL["mosaic_s_per_voxel"] * g["voxels"] * len(radars)
        + frames * CAL["animate_render_s_per_frame"]
    )
    return Estimate(
        "animate",
        int(peak),
        secs,
        {"radars": len(radars), "steps": steps_used, "every": every, "frames": frames, "grid": g},
        ["frame rendering time (0.5 s per frame) is not calibrated"],
    )


_EXPORT = {
    "nc": ("export_cfradial1_peak", "export_cfradial1_s", "export_cfradial1_size"),
    "netcdf": ("export_cfradial1_peak", "export_cfradial1_s", "export_cfradial1_size"),
    "cfradial1": ("export_cfradial1_peak", "export_cfradial1_s", "export_cfradial1_size"),
    "cfradial2": ("export_cfradial2_peak", "export_cfradial2_s", "export_compact_size"),
    "zarr": ("export_zarr_peak", "export_zarr_s", "export_compact_size"),
    "png": ("export_png_peak", "export_png_s", None),
    "csv": ("export_csv_peak", "export_csv_s", None),
}


def estimate_export(vols: Sequence[InputVolume], fmt: str = "nc") -> Estimate:
    key = _EXPORT.get(fmt.lower().lstrip("."), _EXPORT["nc"])
    d = max((v.decoded_bytes for v in vols), default=0)
    scale = [v.decoded_bytes / CAL["ref_decoded"] for v in vols]
    # CfRadial1's measured peak (8.3 D) already includes reading the volume
    read = 0.0 if key[0] == "export_cfradial1_peak" else CAL["read_peak"]
    peak = CAL["base_rss"] + (read + CAL[key[0]]) * d
    secs = sum(_open_seconds(v) + CAL[key[1]] * s for v, s in zip(vols, scale, strict=False))
    size = sum(CAL[key[2]] * v.decoded_bytes for v in vols) if key[2] else 2 * MB * len(vols)
    return Estimate(
        "export",
        int(peak),
        secs,
        {"format": fmt, "files": len(vols), "output_bytes": int(size), "streaming": True},
    )


def estimate(op: str, vols: Sequence[InputVolume], **params: Any) -> Estimate:
    if op == "read":
        return estimate_read(
            vols, all_at_once=params.get("all_at_once", True), lazy=params.get("lazy", False)
        )
    if op == "qc":
        return estimate_qc(vols, all_at_once=params.get("all_at_once", False))
    if op == "mosaic":
        return estimate_mosaic(vols, params.get("grid"))
    if op == "winds":
        return estimate_winds(vols, params.get("grid"), params.get("method", "analytic"))
    if op == "cells":
        return estimate_cells(vols, every=params.get("every", 1))
    if op == "animate":
        return estimate_animate(
            vols, frames=params.get("frames"), grid=params.get("grid"), every=params.get("every", 1)
        )
    if op == "export":
        return estimate_export(vols, params.get("fmt", "nc"))
    raise ValueError(f"unknown operation {op!r}; one of {', '.join(OPERATIONS)}")


# =============================================================================================
# 3. Strategies
# =============================================================================================


def _fits(e: Estimate, b: MachineBudget) -> bool:
    return e.peak_bytes <= b.budget_bytes


def plan(
    op: str, vols: Sequence[InputVolume], budget: MachineBudget | None = None, **params: Any
) -> Plan:
    """Estimate *op* on *vols* and, when it does not fit, choose the least damaging reductions.

    Order (least damage first): streaming (one volume at a time, lossless), lazy reading
    (lossless), coarser grid, fewer levels, temporal thinning, spatial crop. Float32 is never
    applied automatically (the engine keeps the decoded precision; see the performance page);
    on-disk resume is advised for long runs. When nothing fits the plan is a refusal.
    """
    budget = budget or machine_budget()
    params = dict(params)
    max_s = params.pop("max_seconds", None)
    first = estimate(op, vols, **params)
    cur = first
    reductions: list[Reduction] = []
    p = Plan(op, first, first, budget, reductions, params)

    def slow(e: Estimate) -> bool:
        return bool(max_s) and e.seconds > float(max_s)

    if (_fits(cur, budget) and not slow(cur)) or mode() == "off":
        _disk_and_resume(p, vols)
        return p
    strict = mode() == "strict"

    def stated(r: Reduction) -> bool:
        return r.lossless or not strict

    # (a) streaming: one volume at a time instead of all of them -------------------------
    if op in ("read", "qc") and params.get("all_at_once", op == "read") and len(vols) > 1:
        params["all_at_once"] = False
        cur = estimate(op, vols, **params)
        reductions.append(
            Reduction(
                "streaming",
                "volumes processed one at a time, memory freed after each",
                f"all {len(vols)} volumes together need about {_fmt_bytes(first.peak_bytes)}, "
                f"the budget is {_fmt_bytes(budget.budget_bytes)}",
                0.0,
                "none: every volume is processed in full",
                lossless=True,
            )
        )
    # (b) lazy reading: only the sweep / moment in use is decoded ------------------------
    if not _fits(cur, budget) and op == "read" and not params.get("lazy"):
        params["lazy"] = True
        cur = estimate(op, vols, **params)
        reductions.append(
            Reduction(
                "lazy",
                "data read lazily, one sweep and one moment at a time",
                "a fully decoded volume does not fit",
                0.0,
                "none: the same values are read when they are needed (slower when every sweep is used)",  # noqa: E501
                lossless=True,
            )
        )
    # (c) coarser grid, (d) fewer levels ---------------------------------------------------
    if not _fits(cur, budget) and op in ("mosaic", "winds", "animate") and not strict:
        key = "dx_m" if op == "winds" else "resolution_m"
        grid = dict(params.get("grid") or {})
        g0 = (winds_grid if op == "winds" else mosaic_grid)(_unique_radars(vols), grid)
        start = g0[key]
        limit = 5000.0
        for _ in range(MAX_COARSEN):
            if _fits(cur, budget) or grid.get(key, start) * 2 > limit:
                break
            trial = {**grid, key: grid.get(key, start) * 2}
            est = estimate(op, vols, **{**params, "grid": trial})
            if est.peak_bytes >= cur.peak_bytes:
                break  # coarser cells would not save memory here: do not degrade for nothing
            grid, cur = trial, est
            params["grid"] = grid
        if grid.get(key, start) != start:
            new = grid[key]
            reductions.append(
                Reduction(
                    "coarser_grid",
                    f"horizontal grid spacing {start:g} m -> {new:g} m",
                    f"the {start:g} m grid needs about {_fmt_bytes(first.peak_bytes)}",
                    1.0 - (start / new) ** 2,
                    f"features smaller than about {2 * new / 1000:g} km are smoothed out; "
                    "maxima are lower and gradients weaker than on the finer grid",
                    params={key: new},
                    dropped_unit="fewer grid cells",
                )
            )
        zkey = "dz_m" if op == "winds" else "z_step_m"
        z0 = g0[zkey]
        trial = {**grid, zkey: z0 * 2}
        est = estimate(op, vols, **{**params, "grid": trial})
        if (
            not _fits(cur, budget)
            and z0 * 2 <= (2000.0 if op == "winds" else 5000.0)
            and est.peak_bytes < cur.peak_bytes
        ):
            grid, cur = trial, est
            params["grid"] = grid
            reductions.append(
                Reduction(
                    "fewer_levels",
                    f"vertical spacing {z0:g} m -> {z0 * 2:g} m (half the levels)",
                    "the finer vertical grid does not fit",
                    0.5,
                    "column maxima, echo tops and vertical velocity are resolved only every "
                    f"{z0 * 2:g} m (echo-top heights uncertain by up to {z0 * 2:g} m)",
                    params={zkey: z0 * 2},
                    dropped_unit="fewer levels",
                )
            )
    # (e) temporal thinning ---------------------------------------------------------------
    if (not _fits(cur, budget) or slow(cur)) and op in ("cells", "animate") and not strict:
        every = 1
        while (not _fits(cur, budget) or slow(cur)) and every < MAX_THIN:
            est = estimate(op, vols, **{**params, "every": every + 1})
            if not _fits(cur, budget) and est.peak_bytes >= cur.peak_bytes:
                break  # thinning saves time, not memory: it cannot make this fit
            if est.seconds >= cur.seconds:
                break
            every += 1
            cur = est
            params["every"] = every
        if every > 1:
            reductions.append(
                Reduction(
                    "thinning",
                    f"every {every}th volume used",
                    "the full time series does not fit"
                    + (f" the time limit of {_fmt_s(float(max_s))}" if max_s else ""),
                    1.0 - 1.0 / every,
                    f"time resolution {every} x coarser: short-lived cells and fast changes between "  # noqa: E501
                    "the kept volumes are missed; tracks are matched over longer gaps",
                    params={"every": every},
                    dropped_unit="of the volumes not used",
                )
            )
    # (f) spatial crop around the area of interest -----------------------------------------
    if not _fits(cur, budget) and op in ("mosaic", "winds", "animate") and not strict:
        grid = dict(params.get("grid") or {})
        g = (winds_grid if op == "winds" else mosaic_grid)(_unique_radars(vols), grid)
        r0 = g["max_range_km"]
        r = r0
        while not _fits(cur, budget) and r * 0.75 >= 50.0:
            trial = {**grid, "max_range_km": round(r * 0.75)}
            est = estimate(op, vols, **{**params, "grid": trial})
            if est.peak_bytes >= cur.peak_bytes:
                break
            grid, cur, r = trial, est, trial["max_range_km"]
            params["grid"] = grid
        if r != r0:
            reductions.append(
                Reduction(
                    "crop",
                    f"data used only within {r:g} km of each radar (was {r0:g} km)",
                    "the full coverage does not fit",
                    1.0 - (r / r0) ** 2,
                    f"nothing is analysed beyond {r:g} km from a radar; storms there are absent "
                    "from the result (not zero: missing)",
                    params={"max_range_km": r},
                    dropped_unit="of the area not analysed",
                )
            )
    p.final = cur
    p.params = params
    reductions[:] = [r for r in reductions if stated(r)]
    if not _fits(cur, budget):
        tried = f" even after {len(reductions)} allowed reduction(s)" if reductions else ""
        p.refusal = (
            f"{op} needs about {_fmt_bytes(cur.peak_bytes)}{tried}; this machine allows "
            f"{_fmt_bytes(budget.budget_bytes)}"
            + (" (strict mode: only lossless changes allowed)" if strict else "")
        )
        p.options = _options(op, vols, budget, cur.peak_bytes)
    _disk_and_resume(p, vols)
    return p


def _disk_and_resume(p: Plan, vols: Sequence[InputVolume]) -> None:
    out = int(p.final.detail.get("output_bytes") or 0)
    b = p.budget
    if out and b.disk_budget is not None and out > b.disk_budget and p.refusal is None:
        p.refusal = f"the output needs about {_fmt_bytes(out)}, only {_fmt_bytes(b.disk_free)} is free in {b.disk_path}"  # noqa: E501
        p.options = [
            "write to a disk with more space (-o elsewhere)",
            "use Zarr or CfRadial2 (compressed, about 4 x smaller than NetCDF CfRadial1)",
        ]
    if p.final.seconds > 600 and len(vols) > 1 and p.refusal is None:
        p.reductions.append(
            Reduction(
                "resume",
                "results written per volume as they finish, so an interrupted run can resume",
                f"the run takes about {_fmt_s(p.final.seconds)}",
                0.0,
                "none",
                lossless=True,
            )
        )


def _options(op: str, vols: Sequence[InputVolume], b: MachineBudget, need: int) -> list[str]:
    opts = []
    want = math.ceil(need * 1.1 / GB * 10) / 10
    if b.available_ram and b.available_ram * 0.95 > need:
        opts.append(f"allow more memory: --budget-gb {want:g} (that much is free now)")
    elif b.total_ram and b.total_ram * 0.9 > need:
        opts.append(
            f"free memory (close other programs): about {_fmt_bytes(need)} is needed, "
            f"{_fmt_bytes(b.available_ram)} is free now; then --budget-gb {want:g}"
        )
    else:
        opts.append(f"a machine with more than {_fmt_bytes(need * 1.2)} of RAM")
    if op in ("mosaic", "winds", "animate"):
        opts += [
            "fewer radars (-i)",
            "a smaller box (--extent) or --max-range-km",
            "a coarser grid (--resolution-m / --dx-m)",
        ]
    if op in ("cells", "animate", "read", "qc"):
        opts.append("fewer files or a shorter time window")
    if op == "export":
        opts.append("export CfRadial2 or Zarr (written sweep by sweep) instead of NetCDF CfRadial1")
    return opts


def apply_grid(spec: Any, p: Plan) -> Any:
    """Copy the grid changes of *p* onto a ``GridSpec`` dataclass instance (in place)."""
    for r in p.reductions:
        for k, v in r.params.items():
            if hasattr(spec, k):
                setattr(spec, k, v)
    return spec


def stamp(
    attrs: dict[str, Any], reductions: Iterable[Reduction | Mapping[str, Any]]
) -> dict[str, Any]:
    """Record *reductions* in a dataset's attributes (``processing_reductions`` + ``comment``)."""
    items = [r if isinstance(r, Reduction) else Reduction(**dict(r)) for r in reductions]
    if not items:
        return attrs
    text = " | ".join(r.sentence() for r in items)
    attrs["processing_reductions"] = text
    old = str(attrs.get("comment") or "").strip()
    attrs["comment"] = (
        old + " " if old else ""
    ) + "Reduced to fit the machine's memory: see processing_reductions."
    return attrs


# =============================================================================================
# Hooks used by the engine (no behaviour change when the estimate fits)
# =============================================================================================


def govern_grid(
    op: str, readers: Sequence[tuple[Any, Any]], spec: Any, **params: Any
) -> list[Reduction]:
    """Fit the mosaic / wind grid *spec* (a GridSpec, changed in place) to the machine.

    Returns the reductions (empty when it fits as asked); raises :class:`BudgetRefusalError` when it
    cannot fit. Any failure of the estimate itself is logged and ignored (never blocks a run).
    """
    try:
        vols = [volume_from_reader(rd, str(fid)) for fid, rd in readers]
        grid = {k: getattr(spec, k) for k in vars(spec) if not k.startswith("_")}
        p = plan(op, vols, **params, grid=grid)
    except Exception as exc:
        logger.info("governor: no estimate for %s: %s", op, exc)
        return []
    logger.info(
        "governor %s: estimate %s / %s, budget %s",
        op,
        _fmt_bytes(p.estimate.peak_bytes),
        _fmt_s(p.estimate.seconds),
        _fmt_bytes(p.budget.budget_bytes),
    )
    if not p.fits:
        raise BudgetRefusalError(p.refusal + ". You can: " + "; ".join(p.options), p)
    apply_grid(spec, p)
    return [r for r in p.reductions if r.kind != "resume"]


def check_export(reader: Any, fmt: str, path: str | os.PathLike[str] | None = None) -> Plan | None:
    """Refuse an export that would not fit memory or disk (``None`` when the estimate failed)."""
    try:
        v = volume_from_reader(reader)
        p = plan("export", [v], machine_budget(path), fmt=fmt)
    except Exception as exc:
        logger.info("governor: no export estimate: %s", exc)
        return None
    if not p.fits:
        raise BudgetRefusalError(p.refusal + ". You can: " + "; ".join(p.options), p)
    return p


# =============================================================================================
# 4. Guards
# =============================================================================================


def worker_cap(
    per_task_bytes: int, budget: MachineBudget | None = None, wanted: int | None = None
) -> int:
    """Parallel workers that fit: by memory (``budget / per task``) and by cores, at least 1."""
    b = budget or machine_budget()
    by_mem = max(1, int((b.budget_bytes - CAL["base_rss"]) // max(1, per_task_bytes)))
    by_cpu = max(1, b.cpu_count - 1)
    return max(1, min(by_mem, by_cpu, wanted or by_cpu))


class Watchdog:
    """Cooperative guard for long loops: call :meth:`check` once per volume / band / frame.

    Stops the step with :class:`WatchdogStopError` when the process grows beyond *limit_bytes*
    (default: the machine budget) or when free memory falls under *reserve_bytes*, and when
    *cancel* returns True. The check is cheap (one RSS read, at most every *interval_s*).
    """

    def __init__(
        self,
        limit_bytes: int | None = None,
        *,
        cancel: Callable[[], bool] | None = None,
        reserve_bytes: int = 256 * MB,
        interval_s: float = 0.25,
        what: str = "this step",
    ) -> None:
        self.limit = limit_bytes if limit_bytes is not None else machine_budget().budget_bytes
        self.cancel = cancel
        self.reserve = reserve_bytes
        self.interval = interval_s
        self.what = what
        self._last = 0.0
        self.peak = 0

    def check(self, stage: str = "") -> None:
        if self.cancel is not None and self.cancel():
            raise WatchdogStopError(
                f"{self.what} was cancelled{f' during {stage}' if stage else ''}"
            )
        now = time.monotonic()
        if now - self._last < self.interval:
            return
        self._last = now
        rss = current_rss() or 0
        self.peak = max(self.peak, rss)
        if rss > self.limit:
            raise WatchdogStopError(
                f"{self.what} was stopped{f' during {stage}' if stage else ''} before the computer ran out "  # noqa: E501
                f"of memory: it used {_fmt_bytes(rss)}, the limit is {_fmt_bytes(self.limit)}. "
                "Use fewer files, a coarser grid or a smaller area, or raise the limit with --budget-gb."  # noqa: E501
            )
        avail = _available_ram()
        if avail is not None and avail < self.reserve:
            raise WatchdogStopError(
                f"{self.what} was stopped{f' during {stage}' if stage else ''}: only {_fmt_bytes(avail)} "  # noqa: E501
                "of memory is left on this computer. Close other programs or use less data."
            )

    def __enter__(self) -> Watchdog:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class EtaProgress:
    """Wrap a ``(percent, message)`` progress callback; adds "about N s left" from the estimate."""

    def __init__(self, progress: Callable[[float, str], Any] | None, est_seconds: float) -> None:
        self.progress = progress
        self.est = max(0.1, est_seconds)
        self.t0 = time.monotonic()

    def __call__(self, pct: float, msg: str = "") -> None:
        if self.progress is None:
            return
        el = time.monotonic() - self.t0
        # blend the estimate with the observed rate once there is progress to go on
        left = self.est - el
        if pct > 5:
            left = 0.5 * left + 0.5 * el * (100 - pct) / pct
        suffix = f" (about {_fmt_s(max(0.0, left))} left)" if pct < 100 else ""
        self.progress(pct, f"{msg}{suffix}")


# =============================================================================================
# Report for `rxd budget`
# =============================================================================================


def report(
    paths: Sequence[str],
    *,
    budget_gb: float | None = None,
    out_dir: str | None = None,
    operations: Sequence[str] = OPERATIONS,
    probe: int = 1,
    max_seconds: float | None = None,
) -> dict[str, Any]:
    b = machine_budget(out_dir, budget_gb)
    vols = describe_inputs(paths, probe=probe)
    radars = _unique_radars(vols)
    plans = []
    for op in operations:
        if op in ("mosaic", "winds") and len(radars) < (2 if op == "winds" else 1):
            continue
        if op == "winds" and len(radars) > 8:
            continue
        variants: list[dict[str, Any]] = [{}]
        if op == "read":
            variants = [{"all_at_once": True}]
        if op == "export":
            variants = [{"fmt": "nc"}, {"fmt": "zarr"}]
        for params in variants:
            if max_seconds:
                params["max_seconds"] = max_seconds
            try:
                pl = plan(op, vols, b, **params)
            except Exception as exc:
                logger.info("budget: %s: %s", op, exc)
                continue
            if op == "export":
                pl.operation = "export " + ("NetCDF" if params["fmt"] == "nc" else "Zarr")
            plans.append(pl)
    return {"budget": b, "inputs": vols, "plans": plans}


def report_dict(rep: Mapping[str, Any]) -> dict[str, Any]:
    vols: list[InputVolume] = rep["inputs"]
    return {
        "budget": rep["budget"].to_dict(),
        "inputs": {
            "files": len(vols),
            "radars": sorted({v.radar for v in vols}),
            "decoded_bytes": sum(v.decoded_bytes for v in vols),
            "on_disk_bytes": sum(v.size_bytes for v in vols),
            "probed": sum(1 for v in vols if v.probed),
            "volumes": [v.to_dict() for v in vols],
        },
        "plans": [p.to_dict() for p in rep["plans"]],
        "calibration": "Apple M1 Pro, NEXRAD Level II; estimates are approximate (about +-25 %)",
    }


def report_text(rep: Mapping[str, Any]) -> str:
    vols: list[InputVolume] = rep["inputs"]
    radars = sorted({v.radar for v in vols})
    lines = [rep["budget"].text(), ""]
    lines.append(
        f"inputs  {len(vols)} file(s), {len(radars)} radar(s) {', '.join(radars[:8])}{' ...' if len(radars) > 8 else ''}"  # noqa: E501
        f"  |  {_fmt_bytes(sum(v.size_bytes for v in vols))} on disk, about "
        f"{_fmt_bytes(sum(v.decoded_bytes for v in vols))} decoded"
        f"  ({sum(1 for v in vols if v.probed)} opened, the rest scaled by file size)"
    )
    lines.append("")
    for p in rep["plans"]:
        lines.append(p.text())
        for a in p.final.assumptions:
            lines.append(f"         note: {a}")
    return "\n".join(lines)


# =============================================================================================
# helpers
# =============================================================================================


def _float(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _fmt_bytes(n: float | None) -> str:
    if n is None:
        return "?"
    if n >= GB:
        return f"{n / GB:.1f} GB"
    return f"{n / MB:.0f} MB"


def _fmt_s(s: float) -> str:
    if s < 60:
        return f"{s:.0f} s" if s >= 10 else f"{s:.1f} s"
    if s < 3600:
        return f"{s / 60:.0f} min"
    return f"{s / 3600:.1f} h"
