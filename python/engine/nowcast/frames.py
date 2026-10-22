"""Frame sequences: ``(time, y, x)`` grids the nowcast reads, and where they come from.

A :class:`FrameSequence` is a stack of 2-D grids on one regular grid of square cells in metres
(the azimuthal-equidistant grid of the mosaic engine when the frames are made from radar
volumes), with the time of every frame, the quantity (reflectivity in dBZ or rain rate in
mm/h), and a coverage mask that tells a dry cell (observed, no echo: NaN in the data) from one
no radar observed (``covered`` False).

Loaders:

* :func:`from_radar_files`: radar volumes (any format the engine reads). Files are grouped by
  scan time (several radars scanned within ``group_s`` of each other form one frame) and each
  group is gridded by the mosaic engine (composite reflectivity by default) on a grid fixed from
  the first group, so every frame has the same cells.
* :func:`from_netcdf`: NetCDF files with a ``(time, y, x)`` or ``(y, x)`` variable and 1-D
  ``x``/``y`` coordinates in metres (the engine's own QPE and nowcast files, or any CF grid of
  that shape); several files are stacked in time.
* :func:`from_arrays`: arrays from Python.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from engine import memtrim
from engine import provenance as prov

logger = logging.getLogger(__name__)

ProgressFn = Callable[[int, str], None]

FIELD_REFLECTIVITY = "reflectivity"
FIELD_RAIN_RATE = "rain_rate"
UNITS = {FIELD_REFLECTIVITY: "dBZ", FIELD_RAIN_RATE: "mm h-1"}

#: names (lower case) that mean reflectivity / rain rate in a NetCDF variable
_REFL_HINTS = ("dbzh", "dbz", "reflectivity", "refl", "composite", "corrected_reflectivity")
_RATE_HINTS = ("rain_rate", "rate", "lwe_precipitation_rate", "precipitation_rate", "rr")


class FrameError(Exception):
    """The frames could not be read or do not form a usable sequence."""


@dataclass
class FrameSequence:
    """Frames in time order on one grid (see the module docstring)."""

    times: np.ndarray  # (n,) datetime64[ns], UTC
    data: np.ndarray  # (n, ny, nx) float32; NaN = no echo (if covered) or not observed
    field: str  # FIELD_REFLECTIVITY | FIELD_RAIN_RATE
    cell_m: float
    x: np.ndarray  # (nx,) metres east of the grid origin, increasing
    y: np.ndarray  # (ny,) metres north of the grid origin, increasing
    origin: tuple[float, float] | None = None  # (lat, lon) of the projection origin
    proj4: str = ""
    covered: np.ndarray | None = None  # (ny, nx) bool; None = everything finite counts as observed
    sources: list[str] = field(default_factory=list)  # input file names (never paths)
    radars: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    gridding: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.times = np.asarray(self.times).astype("datetime64[ns]")
        self.data = np.asarray(self.data, dtype=np.float32)
        if self.data.ndim != 3:
            raise FrameError("frames must be a (time, y, x) array")
        if self.times.shape[0] != self.data.shape[0]:
            raise FrameError("the number of frame times does not match the number of frames")
        if self.data.shape[0] and np.any(np.diff(self.times) <= np.timedelta64(0, "s")):
            raise FrameError("frame times must be strictly increasing (duplicate scans?)")
        if self.field not in UNITS:
            raise FrameError(f"field must be one of {', '.join(UNITS)}")
        self.x = np.asarray(self.x, dtype=np.float64)
        self.y = np.asarray(self.y, dtype=np.float64)
        if self.x.size != self.data.shape[2] or self.y.size != self.data.shape[1]:
            raise FrameError("x / y coordinates do not match the frame shape")
        if self.covered is not None:
            self.covered = np.asarray(self.covered, dtype=bool)
            if self.covered.shape != self.data.shape[1:]:
                raise FrameError("the coverage mask does not match the frame shape")

    # -- basic facts ----------------------------------------------------------------------
    @property
    def n(self) -> int:
        return int(self.data.shape[0])

    @property
    def shape(self) -> tuple[int, int]:
        return (int(self.data.shape[1]), int(self.data.shape[2]))

    @property
    def units(self) -> str:
        return UNITS[self.field]

    @property
    def coverage(self) -> np.ndarray:
        """Where the grid was observed (all cells when no mask is known)."""
        if self.covered is not None:
            return self.covered
        return np.ones(self.shape, dtype=bool)

    def step_s(self) -> float:
        """Median time between frames (s); 0 with fewer than two frames."""
        if self.n < 2:
            return 0.0
        d = np.diff(self.times).astype("timedelta64[s]").astype(np.float64)
        return float(np.median(d))

    def echo_fraction(self, threshold: float | None = None) -> np.ndarray:
        """Per frame: share of the covered cells with an echo (``>= threshold`` if given)."""
        cov = self.coverage
        n_cov = max(int(cov.sum()), 1)
        out = []
        for k in range(self.n):
            f = self.data[k]
            hit = np.isfinite(f) & cov if threshold is None else (f >= threshold) & cov
            out.append(float(hit.sum()) / n_cov)
        return np.asarray(out)

    def index_at(self, when: Any) -> int:
        """Index of the frame nearest to ``when`` (``datetime64`` or ISO text)."""
        t = np.datetime64(when, "ns")
        return int(np.argmin(np.abs(self.times - t)))

    def subset(self, indices: Sequence[int]) -> FrameSequence:
        idx = list(indices)
        return FrameSequence(
            times=self.times[idx],
            data=self.data[idx],
            field=self.field,
            cell_m=self.cell_m,
            x=self.x,
            y=self.y,
            origin=self.origin,
            proj4=self.proj4,
            covered=self.covered,
            sources=[self.sources[i] for i in idx]
            if len(self.sources) == self.n
            else list(self.sources),
            radars=list(self.radars),
            notes=list(self.notes),
            gridding=dict(self.gridding),
        )

    def to_netcdf(self, path: str | os.PathLike[str]) -> Path:
        """Write the sequence as a CF ``(time, y, x)`` grid that :func:`from_netcdf` reads back
        (the frame cache of the verification harness; the data are the mosaic engine's grids
        and carry its provenance class, RETRIEVED)."""
        import xarray as xr

        name = "reflectivity" if self.field == FIELD_REFLECTIVITY else "rain_rate"
        ds = xr.Dataset(coords={"time": self.times, "y": self.y, "x": self.x})
        ds["y"].attrs = {"standard_name": "projection_y_coordinate", "units": "m", "axis": "Y"}
        ds["x"].attrs = {"standard_name": "projection_x_coordinate", "units": "m", "axis": "X"}
        ds[name] = (
            ("time", "y", "x"),
            self.data,
            {
                "long_name": f"gridded {self.field} frames",
                "units": self.units,
                "provenance_type": "RETRIEVED",
                "comment": "NaN = no echo (inside coverage) or not observed (see covered)",
            },
        )
        ds["covered"] = (("y", "x"), self.coverage.astype(np.int8), {"units": "1"})
        if self.origin is not None:
            ds["crs"] = xr.DataArray(
                np.int32(0),
                attrs={
                    "grid_mapping_name": "azimuthal_equidistant",
                    "latitude_of_projection_origin": float(self.origin[0]),
                    "longitude_of_projection_origin": float(self.origin[1]),
                    "proj4": self.proj4,
                },
            )
        ds.attrs = prov.standard_global_attrs(
            title="Gridded radar frames (nowcast input)",
            source_files=self.sources,
            action="nowcast frames",
            params=self.gridding,
            conventions="CF-1.10",
            extra={
                "radars": ", ".join(r["name"] for r in self.radars),
                "sources_per_frame": "; ".join(self.sources),
                "bbox": ", ".join(f"{v:.4f}" for v in self.gridding.get("bbox", [])),
                "notes": " | ".join(self.notes),
            },
        )
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        enc: dict[str, Any] = {"zlib": True, "complevel": 4}
        if self.field == FIELD_REFLECTIVITY:
            # packed to 0.1 dBZ (the Level II data are quantised at 0.5 dBZ): half the size of
            # float32 before compression, and the frames read back as float32 (NaN kept)
            enc.update({"dtype": "int16", "scale_factor": 0.1, "_FillValue": -32768})
        ds.to_netcdf(p, encoding={name: enc})
        return p

    def describe(self) -> dict[str, Any]:
        return {
            "n_frames": self.n,
            "field": self.field,
            "units": self.units,
            "shape": list(self.shape),
            "cell_m": self.cell_m,
            "step_s": self.step_s(),
            "first": prov.iso_utc(self.times[0]) if self.n else None,
            "last": prov.iso_utc(self.times[-1]) if self.n else None,
            "origin": list(self.origin) if self.origin else None,
            "covered_fraction": float(self.coverage.mean()),
            "sources": list(self.sources),
            "radars": list(self.radars),
            "notes": list(self.notes),
            "gridding": dict(self.gridding),
        }


# ---------------------------------------------------------------------------
# From arrays
# ---------------------------------------------------------------------------


def from_arrays(
    data: Any,
    times: Sequence[Any],
    cell_m: float,
    *,
    field: str = FIELD_REFLECTIVITY,
    x: np.ndarray | None = None,
    y: np.ndarray | None = None,
    covered: np.ndarray | None = None,
    origin: tuple[float, float] | None = None,
    sources: Sequence[str] = (),
) -> FrameSequence:
    """A sequence from a ``(time, y, x)`` array (or a list of 2-D arrays) and its times."""
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim == 2:
        arr = arr[None]
    t = np.asarray([np.datetime64(v, "ns") for v in times])
    ny, nx = arr.shape[1:]
    if x is None:
        x = (np.arange(nx) - 0.5 * (nx - 1)) * float(cell_m)
    if y is None:
        y = (np.arange(ny) - 0.5 * (ny - 1)) * float(cell_m)
    return FrameSequence(
        times=t,
        data=arr,
        field=field,
        cell_m=float(cell_m),
        x=np.asarray(x),
        y=np.asarray(y),
        origin=origin,
        covered=covered,
        sources=[prov.clean_text(s) for s in sources],
    )


# ---------------------------------------------------------------------------
# From the engine's gridded NetCDF (or any CF grid of that shape)
# ---------------------------------------------------------------------------


def _pick_variable(ds: Any, variable: str | None) -> tuple[str, str]:
    """``(name, field)`` of the frame variable in a dataset."""
    if variable:
        if variable not in ds.data_vars:
            raise FrameError(f"variable {variable!r} is not in the file")
        name = variable
    else:
        cands = [
            v
            for v in ds.data_vars
            if ds[v].ndim in (2, 3) and {"x", "y"} <= set(ds[v].dims) and v not in ("lat", "lon")
        ]
        if not cands:
            raise FrameError("no (time, y, x) or (y, x) variable with x/y dimensions in the file")

        def rank(v: str) -> int:
            low = v.lower()
            if any(h in low for h in _REFL_HINTS):
                return 0
            if any(h in low for h in _RATE_HINTS) or low in ("accumulation", "rain_rate_last"):
                return 1
            return 2

        name = sorted(cands, key=rank)[0]
    units = str(ds[name].attrs.get("units", "")).lower()
    low = name.lower()
    if units.startswith("dbz") or any(h in low for h in _REFL_HINTS):
        fld = FIELD_REFLECTIVITY
    elif "mm" in units or any(h in low for h in _RATE_HINTS):
        fld = FIELD_RAIN_RATE
    else:
        raise FrameError(
            f"cannot tell whether {name!r} is reflectivity (dBZ) or a rain rate (mm/h): "
            "give --field"
        )
    return name, fld


def from_netcdf(
    paths: Sequence[str | os.PathLike[str]],
    *,
    variable: str | None = None,
    field: str | None = None,
) -> FrameSequence:
    """Stack ``(time, y, x)`` / ``(y, x)`` grids of NetCDF files into one sequence."""
    import xarray as xr

    if not paths:
        raise FrameError("no NetCDF file given")
    frames: list[np.ndarray] = []
    times: list[np.datetime64] = []
    ref: dict[str, Any] = {}
    names: list[str] = []
    for p in paths:
        path = Path(p)
        with xr.open_dataset(path) as ds:
            name, fld = _pick_variable(ds, variable)
            fld = field or fld
            da = ds[name]
            if "x" not in ds.coords or "y" not in ds.coords:
                raise FrameError(f"{path.name}: needs 1-D x and y coordinates in metres")
            x = np.asarray(ds["x"].values, dtype=np.float64)
            y = np.asarray(ds["y"].values, dtype=np.float64)
            if da.ndim == 2:
                stack = np.asarray(da.transpose("y", "x").values, dtype=np.float32)[None]
                t = ds["time"].values if "time" in ds.coords else None
                tt = [np.datetime64(t, "ns")] if t is not None and np.ndim(t) == 0 else None
                if tt is None:
                    raise FrameError(f"{path.name}: the frame has no time coordinate")
            else:
                tdim = next(d for d in da.dims if d not in ("y", "x"))
                stack = np.asarray(da.transpose(tdim, "y", "x").values, dtype=np.float32)
                tt = [np.datetime64(v, "ns") for v in ds[tdim].values]
            if not ref:
                origin = None
                if "crs" in ds.variables:
                    a = ds["crs"].attrs
                    if "latitude_of_projection_origin" in a:
                        origin = (
                            float(a["latitude_of_projection_origin"]),
                            float(a["longitude_of_projection_origin"]),
                        )
                crs_attrs = ds["crs"].attrs if "crs" in ds.variables else {}
                bbox_text = str(ds.attrs.get("bbox", ""))
                bbox = [float(v) for v in bbox_text.split(",")] if bbox_text else []
                radars = [
                    r.strip() for r in str(ds.attrs.get("radars", "")).split(",") if r.strip()
                ]
                ref = {
                    "x": x,
                    "y": y,
                    "field": fld,
                    "origin": origin,
                    "proj4": str(crs_attrs.get("proj4", "")),
                    "covered": (
                        np.asarray(ds["covered"].values) > 0 if "covered" in ds.variables else None
                    ),
                    "gridding": {"bbox": bbox} if len(bbox) == 4 else {},
                    "radars": [{"name": r} for r in radars],
                    "frame_sources": [
                        s.strip()
                        for s in str(ds.attrs.get("sources_per_frame", "")).split(";")
                        if s.strip()
                    ],
                    "notes": [
                        s.strip() for s in str(ds.attrs.get("notes", "")).split(" | ") if s.strip()
                    ],
                }
            elif x.size != ref["x"].size or y.size != ref["y"].size:
                raise FrameError(f"{path.name}: its grid differs from the first file's")
            frames.append(stack)
            times.extend(tt)
            names.append(path.name)
    data = np.concatenate(frames, axis=0)
    t = np.asarray(times)
    order = np.argsort(t)
    cell = float(np.median(np.diff(ref["x"]))) if ref["x"].size > 1 else 1.0
    sources = ref["frame_sources"] if len(ref["frame_sources"]) == t.size else names
    return FrameSequence(
        times=t[order],
        data=data[order],
        field=ref["field"],
        cell_m=cell,
        x=ref["x"],
        y=ref["y"],
        origin=ref["origin"],
        proj4=ref["proj4"],
        covered=ref["covered"],
        sources=[sources[i] for i in order] if len(sources) == t.size else sources,
        radars=ref["radars"],
        notes=ref.get("notes", []),
        gridding=ref["gridding"],
    )


# ---------------------------------------------------------------------------
# From radar volumes (gridded by the mosaic engine)
# ---------------------------------------------------------------------------


def _scan_time(reader: Any) -> np.datetime64:
    from engine.hydro.accumulate import scan_meta

    return scan_meta(reader).time


def _grid_group(
    files: list[str], variable: str, product: str, grid: dict[str, Any]
) -> dict[str, Any]:
    """Open the volumes of one frame, grid them, close them; plain arrays and numbers come back
    (this runs in a worker process when ``workers`` > 1)."""
    from engine import mosaic as me
    from engine.reader import RadarReader

    readers = []
    try:
        for f in files:
            rd = RadarReader()
            try:
                rd.open_file(f)
            except Exception as exc:  # the reader's own message says what is wrong with it
                raise FrameError(f"{Path(f).name}: {exc}") from exc
            readers.append((Path(f).name, rd))
        rec = me.build_mosaic(
            readers, variable, product, grid, {"enabled": True}, cache=None, keep_volume=False
        )
    except me.MosaicError as exc:
        raise FrameError(str(exc)) from exc
    finally:
        for _, rd in readers:
            with _suppress():
                rd.close()
        readers.clear()
    if rec.merged_aeqd is None:
        raise FrameError("the mosaic engine did not return the working grid")
    out = {
        "merged": np.asarray(rec.merged_aeqd, dtype=np.float32),
        "covered": rec.covered_aeqd,
        "bbox": tuple(rec.bbox),
        "origin": tuple(rec.origin),
        "x0": float(rec.x0),
        "y0": float(rec.y0),
        "cell_m": float(rec.cell_m),
        "radars": [
            {"name": r.name, "latitude": r.latitude, "longitude": r.longitude} for r in rec.radars
        ],
        "time": me._iso_to_dt64(rec.analysis_time) if rec.analysis_time else None,
        "warnings": list(rec.warnings),
    }
    del rec
    with _suppress():  # a worker that grids many frames hands its freed pages back each time
        memtrim.trim()
    return out


#: Resident memory one gridding worker needs for a single volume, measured on NEXRAD Level II
#: (about 1.2 GB peak), and for each further volume of the same frame (about 0.35 GB); the
#: product is doubled as a margin because the volumes of one site differ in size.
_WORKER_BASE_BYTES = int(1.2 * 1024**3)
_WORKER_PER_VOLUME_BYTES = int(0.35 * 1024**3)
_WORKER_MARGIN = 2.0
#: A gridding worker is replaced after this many frames, so that whatever a dependency keeps
#: between volumes can never add up over a long run (Python 3.11 and later).
_FRAMES_PER_WORKER = 8


def workers_for_memory(requested: int, volumes_per_frame: int, budget_bytes: int | None) -> int:
    """How many gridding workers fit the memory budget (at least 1, at most ``requested``)."""
    if budget_bytes is None or requested <= 1:
        return max(1, int(requested))
    per_worker = (
        _WORKER_BASE_BYTES + max(0, volumes_per_frame - 1) * _WORKER_PER_VOLUME_BYTES
    ) * _WORKER_MARGIN
    return max(1, min(int(requested), int(budget_bytes // per_worker)))


def _memory_budget_bytes() -> int | None:
    try:
        from engine import governor

        return int(governor.machine_budget().budget_bytes)
    except Exception:  # an unknown budget does not stop the run
        return None


_NEXRAD_NAME = re.compile(r"^[A-Z]{4}(\d{8})_(\d{6})(?:_|\.|$)")


def time_from_name(name: str) -> np.datetime64 | None:
    """The volume start time in a NEXRAD Level II file name (``KTLX20130520_201643_V06``),
    else ``None`` (the file is then opened to read its time). Used only to group files by
    time: the frame time itself comes from the gridded volume."""
    m = _NEXRAD_NAME.match(name)
    if not m:
        return None
    d, t = m.group(1), m.group(2)
    try:
        return np.datetime64(f"{d[:4]}-{d[4:6]}-{d[6:]}T{t[:2]}:{t[2:4]}:{t[4:]}", "ns")
    except ValueError:
        return None


def group_by_time(
    items: Sequence[tuple[np.datetime64, Any]], group_s: float
) -> list[list[tuple[np.datetime64, Any]]]:
    """Groups of items whose times lie within ``group_s`` of the group's first item."""
    ordered = sorted(items, key=lambda it: it[0])
    groups: list[list[tuple[np.datetime64, Any]]] = []
    for t, it in ordered:
        if groups and (t - groups[-1][0][0]) / np.timedelta64(1, "s") <= group_s:
            groups[-1].append((t, it))
        else:
            groups.append([(t, it)])
    return groups


def from_radar_files(
    paths: Sequence[str | os.PathLike[str]],
    *,
    variable: str = "DBZH",
    product: str = "composite",
    resolution_m: float = 1000.0,
    max_range_km: float = 230.0,
    extent: Any = None,
    group_s: float = 150.0,
    workers: int = 1,
    on_error: str = "raise",
    progress: ProgressFn | None = None,
    cancel: Callable[[], bool] | None = None,
) -> FrameSequence:
    """Grid radar volumes into frames with the mosaic engine (needs radarx).

    ``workers`` > 1 grids the frames after the first in that many processes.
    Volumes of several radars taken within ``group_s`` seconds form one frame (their mosaic);
    the grid is fixed from the first frame (or ``extent`` = ``(west, south, east, north)``).
    Composite reflectivity (``product="composite"``) is the default field; ``"cappi"`` uses the
    engine's default CAPPI height. A volume that cannot be read or gridded stops the run
    (``on_error="raise"``) or is left out with a note in ``notes`` (``on_error="skip"``: the
    sequence then has a gap at that time, which the nowcast's time handling allows for).
    """
    from engine.reader import RadarReader

    if on_error not in ("raise", "skip"):
        raise FrameError("on_error must be 'raise' or 'skip'")
    skip = on_error == "skip"
    report = progress or (lambda _p, _m: None)
    files = [os.fspath(p) for p in paths]
    if not files:
        raise FrameError("no radar file given")
    report(2, f"Reading the scan times of {len(files)} volumes")
    notes: list[str] = []
    stamped: list[tuple[np.datetime64, str]] = []
    for i, f in enumerate(files):
        if cancel and cancel():
            raise FrameError("cancelled")
        t = time_from_name(Path(f).name)  # NEXRAD names carry the time: no decode needed
        if t is None:
            rd = RadarReader()
            try:
                rd.open_file(f)
                t = _scan_time(rd)
            except Exception as exc:
                if skip:
                    notes.append(f"{Path(f).name}: not gridded: {exc}")
                    continue
                raise FrameError(f"{Path(f).name}: {exc}") from exc
            finally:
                with _suppress():
                    rd.close()
        if np.isnat(t):
            raise FrameError(f"{Path(f).name}: the volume has no scan time")
        stamped.append((t, f))
        if i % 5 == 0:
            report(2 + int(8 * i / len(files)), f"Read {i + 1}/{len(files)} scan times")
    if not stamped:
        raise FrameError("no volume could be read: " + "; ".join(notes))
    groups = group_by_time(stamped, group_s)
    frames: list[np.ndarray] = []
    times: list[np.datetime64] = []
    names: list[str] = []
    layout: dict[str, Any] = {}
    covered: np.ndarray | None = None
    radars: list[dict[str, Any]] = []

    def grid_spec() -> dict[str, Any]:
        spec: dict[str, Any] = {
            "resolution_m": float(resolution_m),
            "max_range_km": float(max_range_km),
            "extent": layout.get("extent") or (extent if extent is not None else "auto"),
        }
        if isinstance(spec["extent"], (list, tuple)):
            w, s, e, n = spec["extent"]
            spec["extent"] = {"west": w, "south": s, "east": e, "north": n}
        return spec

    # The first frame fixes the grid; the others can be gridded in parallel processes (opening
    # and decoding a volume is single-threaded Python work).
    results: list[dict[str, Any] | None] = [None] * len(groups)
    first_gi = 0
    while True:
        report(10, f"Gridding frame {first_gi + 1}/{len(groups)}")
        try:
            results[first_gi] = _grid_group(
                [f for _, f in groups[first_gi]], variable, product, grid_spec()
            )
            break
        except FrameError as exc:
            if not skip or first_gi + 1 >= len(groups):
                raise
            notes.append(f"{_group_name(groups[first_gi])}: not gridded: {exc}")
            first_gi += 1
    first = results[first_gi]
    assert first is not None
    w, s, e, n = first["bbox"]
    layout = {
        "extent": {"west": w, "south": s, "east": e, "north": n},
        "origin": first["origin"],
        "x0": first["x0"],
        "y0": first["y0"],
        "cell_m": first["cell_m"],
        "shape": first["merged"].shape,
    }
    rest = list(range(first_gi + 1, len(groups)))
    if rest and workers > 1:
        from concurrent.futures import ProcessPoolExecutor

        spec = grid_spec()
        biggest = max(len(groups[gi]) for gi in rest)
        n_workers = workers_for_memory(int(workers), biggest, _memory_budget_bytes())
        n_workers = min(n_workers, len(rest))
        if n_workers < int(workers):
            notes.append(
                f"gridding used {n_workers} worker process(es) instead of {int(workers)}: "
                "that is what the memory budget allows"
            )
        pool_kw: dict[str, Any] = {}
        if sys.version_info >= (3, 11):
            pool_kw["max_tasks_per_child"] = _FRAMES_PER_WORKER
        with ProcessPoolExecutor(max_workers=n_workers, **pool_kw) as pool:
            futures = {
                pool.submit(_grid_group, [f for _, f in groups[gi]], variable, product, spec): gi
                for gi in rest
            }
            for done, fut in enumerate(futures, start=1):
                gi = futures[fut]
                try:
                    results[gi] = fut.result()
                except Exception as exc:
                    if not skip:
                        pool.shutdown(cancel_futures=True)
                        raise FrameError(str(exc)) from exc
                    notes.append(f"{_group_name(groups[gi])}: not gridded: {exc}")
                report(10 + int(85 * done / len(groups)), f"Gridded frame {done + 1}/{len(groups)}")
                if cancel and cancel():
                    pool.shutdown(cancel_futures=True)
                    raise FrameError("cancelled")
    else:
        for gi in rest:
            if cancel and cancel():
                raise FrameError("cancelled")
            report(10 + int(85 * gi / len(groups)), f"Gridding frame {gi + 1}/{len(groups)}")
            try:
                results[gi] = _grid_group(
                    [f for _, f in groups[gi]], variable, product, grid_spec()
                )
            except FrameError as exc:
                if not skip:
                    raise
                notes.append(f"{_group_name(groups[gi])}: not gridded: {exc}")
    for gi, grp in enumerate(groups):
        rec = results[gi]
        if rec is None:
            continue  # not gridded (noted above)
        merged = rec["merged"]
        if gi == first_gi:
            covered = rec["covered"]
            radars = list(rec["radars"])
        elif merged.shape != layout["shape"]:
            raise FrameError("the grid changed between frames (different extent?)")
        else:
            seen = {r["name"] for r in radars}
            for r in rec["radars"]:
                if r["name"] not in seen:
                    radars.append(dict(r))
                    seen.add(r["name"])
            if rec["covered"] is not None and covered is not None:
                covered = covered | rec["covered"]
        # the frame time is the mosaic's analysis time (mean ray time of the volumes); the file
        # names only grouped the volumes
        tm = rec["time"]
        if tm is None or np.isnat(tm):
            times_g = [t for t, _ in grp]
            t0 = min(times_g)
            tm = t0 + np.mean(
                [(t - t0) / np.timedelta64(1, "s") for t in times_g]
            ) * np.timedelta64(1, "s")
        frames.append(np.asarray(merged, dtype=np.float32))
        times.append(np.datetime64(tm, "ns"))
        names.append(_group_name(grp))
        for wmsg in rec["warnings"]:
            if wmsg not in notes:
                notes.append(wmsg)
    if len(frames) < 1:
        raise FrameError("no volume could be gridded")
    ny, nx = layout["shape"]
    x = layout["x0"] + np.arange(nx) * layout["cell_m"]
    y = layout["y0"] + np.arange(ny) * layout["cell_m"]
    lat0, lon0 = layout["origin"]
    report(97, "Frames ready")
    return FrameSequence(
        times=np.asarray(times),
        data=np.stack(frames),
        field=FIELD_REFLECTIVITY if variable.upper().startswith("DBZ") else FIELD_RAIN_RATE,
        cell_m=float(layout["cell_m"]),
        x=x,
        y=y,
        origin=(float(lat0), float(lon0)),
        proj4=aeqd_proj4(lat0, lon0),
        covered=covered,
        sources=names,
        radars=radars,
        notes=notes,
        gridding={
            "engine": "mosaic (radarx)",
            "product": product,
            "variable": variable,
            "resolution_m": float(resolution_m),
            "max_range_km": float(max_range_km),
            "group_s": float(group_s),
            "bbox": list(layout["extent"].values()),
        },
    )


def _group_name(grp: Sequence[tuple[np.datetime64, str]]) -> str:
    return "+".join(Path(f).name for _, f in grp)


def aeqd_proj4(lat0: float, lon0: float) -> str:
    """The PROJ string of the azimuthal-equidistant grid centred on ``(lat0, lon0)``."""
    return (
        f"+proj=aeqd +lat_0={lat0:.8f} +lon_0={lon0:.8f} +x_0=0 +y_0=0 +datum=WGS84 "
        "+units=m +no_defs"
    )


def _suppress() -> Any:
    return contextlib.suppress(Exception)


def load(
    inputs: Sequence[str | os.PathLike[str]],
    *,
    field: str | None = None,
    variable: str | None = None,
    progress: ProgressFn | None = None,
    cancel: Callable[[], bool] | None = None,
    **gridding: Any,
) -> FrameSequence:
    """NetCDF grids (``.nc``) or radar volumes (anything else), by the files' extensions."""
    paths = [os.fspath(p) for p in inputs]
    if not paths:
        raise FrameError("no input")
    if all(p.lower().endswith((".nc", ".nc4", ".cdf")) for p in paths):
        return from_netcdf(paths, variable=variable, field=field)
    kw = dict(gridding)
    if variable:
        kw["variable"] = variable
    return from_radar_files(paths, progress=progress, cancel=cancel, **kw)
