"""Stage 1: inspect the inputs cheaply (metadata only) and build the inventory.

What is read and what is not, so that the cost stays bounded for hundreds of volumes:

* every file: size on disk; radar and time from the NAME when it says (NEXRAD style), nothing
  else is touched;
* a SAMPLE of files per radar (all when few, otherwise first / middle / last: ``SAMPLE_PER_RADAR``)
  is opened lazily (``engine.api.open``, no sweep data loaded) for format, site position, scan
  kinds (PPI / RHI), fields, band, Nyquist, range, memory ratio;
* ONE representative file per radar gets the engine's data check (``engine.quality``) with its
  normal time budget;
* nothing is downloaded, nothing is written.

Every sampling decision is recorded in ``inventory["reductions"]`` so the plan and the run record
can repeat it (docs/SCIENCE.md: every automatic decision is explained).
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections.abc import Callable
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SAMPLE_PER_RADAR = 3  # files opened per radar when the series is long (first, middle, last)
OPEN_ALL_BELOW = 6  # open every file of a radar when it has at most this many
DUAL_POL_ROLES = ("zdr", "rhohv", "phidp", "kdp")
ProgressFn = Callable[[int, str], None]


class AutopilotError(ValueError):
    """A problem with the inputs or the request that the user can fix."""


def _progress(fn: ProgressFn | None, pct: int, text: str) -> None:
    if fn is not None:
        fn(int(max(0, min(100, pct))), text)


def expand_inputs(items: Any) -> list[Path]:
    """Files, folders (recursive) and globs -> unique radar-file candidates (sorted)."""
    import glob

    skip_suffix = {
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf", ".txt", ".md", ".csv", ".xlsx",
        ".ipynb", ".json", ".py", ".html", ".mp4", ".zip", ".log", ".ds_store",
    }  # fmt: skip
    out: list[Path] = []
    seen: set[Path] = set()

    def add(p: Path) -> None:
        if p.name.startswith(".") or p.suffix.lower() in skip_suffix:
            return
        if p.name.endswith(".zarr") or p.is_file():
            r = p.resolve()
            if r not in seen:
                seen.add(r)
                out.append(p)

    for raw in [items] if isinstance(items, (str, os.PathLike)) else list(items or []):
        text = os.path.expanduser(os.fspath(raw))
        matches = glob.glob(text) if any(c in text for c in "*?[") else [text]
        for m in matches:
            p = Path(m)
            if p.is_dir() and not p.name.endswith(".zarr"):
                for child in sorted(p.rglob("*")):
                    if child.is_dir() and not child.name.endswith(".zarr"):
                        continue
                    if any(part.endswith(".zarr") for part in child.parent.parts):
                        continue  # inside a zarr store
                    add(child)
            elif p.exists():
                add(p)
            else:
                raise AutopilotError(f"Input not found: {raw}")
    return sorted(out, key=lambda p: (str(p.parent), p.name))


def _size(p: Path) -> int:
    if p.is_dir():
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _roles_present(variables: list[str]) -> dict[str, str]:
    """``{role: variable}`` for the known radar moments among ``variables`` (names only)."""
    from engine import varmap

    found: dict[str, str] = {}
    keys = {id(r): k for k, r in varmap.ROLES.items()}
    for name in variables:
        role = varmap.role_for_name(name)
        key = keys.get(id(role)) if role is not None else None
        if key is not None and key not in found:
            found[key] = name
    return found


def _sample_indices(n: int) -> list[int]:
    if n <= OPEN_ALL_BELOW:
        return list(range(n))
    return sorted({0, n // 2, n - 1})[:SAMPLE_PER_RADAR]


def _open_meta(path: Path) -> dict[str, Any]:
    """Format, site, kinds, fields, band and memory ratio of one file (opened lazily)."""
    from engine import api, quality

    with api.open(str(path)) as vol:
        info = vol.info()
        tree = vol.tree
        kinds = sorted({str(s.get("kind", "ppi")) for s in vol.sweeps})
        attrs: dict[str, Any] = {}
        try:
            attrs = {**dict(tree.attrs), **dict(tree.root.to_dataset().attrs)}
        except Exception:
            attrs = {}
        band = None
        try:
            ds0 = vol.dataset(0, mask=False) if vol.n_sweeps else None
            band, band_how = quality.radar_band(tree, ds0, vol.format)
        except Exception as exc:  # the band is a nicety
            band, band_how = None, f"not determined ({type(exc).__name__})"
        nyq = [s.get("nyquist_ms") or s.get("nyquist_estimated_ms") for s in info["sweeps"]]
        nyq = [float(v) for v in nyq if v]
        rng = [s.get("max_range_km") for s in info["sweeps"] if s.get("max_range_km")]
        elevs = [s.get("elevation") for s in info["sweeps"] if s.get("elevation") is not None]
        return {
            "format": info.get("format"),
            "time": info.get("time"),
            "latitude": info.get("latitude"),
            "longitude": info.get("longitude"),
            "altitude_m": info.get("altitude_m"),
            "n_sweeps": info.get("n_sweeps"),
            "elevations": [round(float(e), 2) for e in elevs],
            "kinds": kinds,
            "variables": list(info.get("variables") or []),
            "roles": _roles_present(list(info.get("variables") or [])),
            "band": band,
            "band_how": band_how,
            "nyquist_ms": round(max(nyq), 1) if nyq else None,
            "max_range_km": round(max(rng), 1) if rng else None,
            "memory_bytes": int(info.get("memory_bytes") or 0),
            "size_bytes": int(info.get("size_bytes") or 0),
            "attrs": {
                k: attrs[k] for k in ("instrument_name", "station", "site_name") if k in attrs
            },
        }


def _data_check(path: Path) -> dict[str, Any]:
    from engine import api, quality

    with api.open(str(path)) as vol:
        rep = quality.check_volume(vol)
    # keep the report small: the panel shows titles and the recipe; the full report is rxd check
    return {
        "counts": rep.get("counts"),
        "band": rep.get("band"),
        "findings": [
            {
                "id": f["id"],
                "title": f["title"],
                "severity": f["severity"],
                "summary": f["summary"],
                "steps": f.get("steps", []),
            }
            for f in rep.get("findings", [])
        ],
        "recipes": rep.get("recommendation", {}).get("recipes", []),
        "first_view": rep.get("recommendation", {}).get("first_view"),
        "not_checked": rep.get("not_checked", []),
        "elapsed_ms": rep.get("elapsed_ms"),
    }


def _gaps(times: list[datetime]) -> tuple[float | None, list[dict[str, Any]]]:
    """Median cadence (s) and the gaps longer than 2.5 x cadence."""
    if len(times) < 2:
        return None, []
    deltas = [(b - a).total_seconds() for a, b in pairwise(times)]
    srt = sorted(deltas)
    cadence = srt[len(srt) // 2]
    gaps = [
        {"after": times[i].isoformat(), "seconds": round(d)}
        for i, d in enumerate(deltas)
        if cadence > 0 and d > 2.5 * cadence
    ]
    return round(cadence), gaps


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def dual_doppler_geometry(
    d_km: float, r1_km: float, r2_km: float, min_crossing: float = 30.0
) -> dict[str, Any]:
    """Share of the dual-coverage area with beam crossing >= ``min_crossing`` deg.

    Same geometry as :func:`engine.multidoppler._dual_mask` on a coarse 2 km grid: the two radars
    sit on the x axis at +-d/2. ESTIMATE (geometry only; no data is read).
    """
    import numpy as np

    from engine.multidoppler import _crossing_deg

    reach = max(r1_km, r2_km)
    x = np.arange(-(d_km / 2 + reach), d_km / 2 + reach, 2.0)
    y = np.arange(-reach, reach, 2.0)
    xx, yy = np.meshgrid(x, y)
    sites = [(-d_km / 2, 0.0, r1_km), (d_km / 2, 0.0, r2_km)]
    inside, bearing = [], []
    for sx, sy, rr in sites:
        dx, dy = xx - sx, yy - sy
        dist = np.hypot(dx, dy)
        inside.append((dist <= rr) & (dist > 1.0))
        bearing.append(np.arctan2(dx, dy))
    both = inside[0] & inside[1]
    cross = np.where(both, _crossing_deg(bearing[0], bearing[1]), 0.0)
    n_both = int(both.sum())
    good = int((cross >= min_crossing).sum())
    return {
        "dual_area_km2": n_both * 4,
        "good_crossing_km2": good * 4,
        "good_fraction": round(good / n_both, 3) if n_both else 0.0,
        "min_crossing_deg": min_crossing,
        "kind": "ESTIMATE",
        "method": "beam-crossing angle on a 2 km grid (engine.multidoppler geometry)",
    }


def inspect_inputs(
    inputs: Any,
    *,
    progress: ProgressFn | None = None,
    cancel: Callable[[], bool] | None = None,
    sample_per_radar: int = SAMPLE_PER_RADAR,
    data_check: bool = True,
) -> dict[str, Any]:
    """The inventory of ``inputs`` (files / folders / globs). Metadata only; see the module doc."""
    from engine.animate_plan import parse_volume_name, radar_label

    t0 = time.perf_counter()
    paths = expand_inputs(inputs)
    if not paths:
        raise AutopilotError("No input files: give radar files, folders or globs")
    reductions: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    unreadable: list[dict[str, str]] = []
    total_bytes = 0

    # 1. names and sizes (free)
    _progress(progress, 2, f"Listing {len(paths)} files...")
    for p in paths:
        size = _size(p)
        total_bytes += size
        parsed = parse_volume_name(p)
        files.append(
            {
                "path": str(p),
                "name": p.name,
                "size_bytes": size,
                "radar": parsed[0] if parsed else None,
                "time": parsed[1].isoformat() if parsed else None,
                "source": "name" if parsed else None,
                "opened": False,
            }
        )

    # 2. open a sample per radar (lazily) for the metadata the name cannot give
    unnamed = [f for f in files if f["radar"] is None]
    if len(unnamed) > 40:
        # names say nothing: opening every file would be the slow part; open a sample and say so
        keep = sorted(
            {0, len(unnamed) // 2, len(unnamed) - 1}
            | set(range(0, len(unnamed), max(1, len(unnamed) // 12)))
        )
        reductions.append(
            {
                "what": "metadata sampling",
                "from": len(unnamed),
                "to": len(keep),
                "reason": "the file names carry no radar/time; only a sample of files is "
                "opened for the inventory (the plan says which files it used)",
            }
        )
        to_open = [unnamed[i] for i in keep]
    else:
        to_open = unnamed
    groups: dict[str, list[dict[str, Any]]] = {}
    for f in files:
        if f["radar"]:
            groups.setdefault(f["radar"], []).append(f)
    for radar, fl in groups.items():
        fl.sort(key=lambda f: f["time"] or "")
        idx = (
            list(range(len(fl)))
            if len(fl) <= OPEN_ALL_BELOW
            else sorted({0, len(fl) // 2, len(fl) - 1})[:sample_per_radar]
        )
        if len(idx) < len(fl):
            reductions.append(
                {
                    "what": f"metadata sampling for {radar}",
                    "from": len(fl),
                    "to": len(idx),
                    "reason": "a long series: first, middle and last volume are opened for fields, "
                    "scan kinds and size; the others are assumed alike (checked at run time)",
                }
            )
        to_open.extend(fl[i] for i in idx)

    for n, f in enumerate(to_open):
        if cancel is not None and cancel():
            raise AutopilotError("cancelled")
        _progress(progress, 5 + int(60 * n / max(1, len(to_open))), f"Reading metadata {f['name']}")
        try:
            meta = _open_meta(Path(f["path"]))
        except Exception as exc:
            unreadable.append({"name": f["name"], "reason": str(exc)[:160]})
            f["error"] = str(exc)[:160]
            continue
        f.update(meta)
        f["opened"] = True
        if f["radar"] is None:
            f["radar"] = radar_label(
                meta["attrs"], meta["latitude"], meta["longitude"], Path(f["path"]).stem
            )
            f["source"] = "file"
        if f["time"] is None:
            f["time"] = meta["time"]
    # files that were neither named nor opened inherit nothing: they stay "unknown"
    for f in files:
        if f["radar"] is None and "error" not in f:
            f["radar"] = "UNKNOWN"
            f["source"] = "not opened"

    # 3. per-radar summary
    radars: dict[str, dict[str, Any]] = {}
    for f in files:
        if "error" in f and f["source"] != "name":
            continue  # unreadable and nameless: nothing to group it by
        r = radars.setdefault(
            f["radar"],
            {
                "name": f["radar"],
                "n_files": 0,
                "bytes": 0,
                "times": [],
                "formats": set(),
                "kinds": set(),
                "variables": set(),
                "roles": {},
                "band": None,
                "band_how": None,
                "latitude": None,
                "longitude": None,
                "altitude_m": None,
                "nyquist_ms": None,
                "max_range_km": None,
                "n_sweeps": None,
                "elevations": None,
                "memory_ratio": None,
                "representative": None,
            },
        )
        r["n_files"] += 1
        r["bytes"] += f["size_bytes"]
        if f["time"]:
            r["times"].append(f["time"])
        if f.get("opened"):
            r["formats"].add(f.get("format") or "?")
            r["kinds"].update(f.get("kinds", []))
            r["variables"].update(f.get("variables", []))
            for k, v in f.get("roles", {}).items():
                r["roles"].setdefault(k, v)
            for key in (
                "band",
                "band_how",
                "latitude",
                "longitude",
                "altitude_m",
                "nyquist_ms",
                "max_range_km",
                "n_sweeps",
                "elevations",
            ):
                if r[key] is None and f.get(key) is not None:
                    r[key] = f[key]
            if r["representative"] is None:
                r["representative"] = f["path"]
            if f.get("size_bytes") and f.get("memory_bytes"):
                r["memory_ratio"] = round(f["memory_bytes"] / f["size_bytes"], 2)
    for r in radars.values():
        times = sorted(datetime.fromisoformat(t) for t in r["times"])
        cadence, gaps = _gaps(times)
        r["t0"] = times[0].isoformat() if times else None
        r["t1"] = times[-1].isoformat() if times else None
        r["span_s"] = (times[-1] - times[0]).total_seconds() if len(times) > 1 else 0
        r["cadence_s"] = cadence
        r["gaps"] = gaps
        r["dual_pol"] = all(k in r["roles"] for k in ("zdr", "rhohv")) and "phidp" in r["roles"]
        r["has_velocity"] = "velocity" in r["roles"]
        r["has_reflectivity"] = "reflectivity" in r["roles"]
        r["rhi"] = "rhi" in r["kinds"]
        r["formats"] = sorted(r["formats"])
        r["kinds"] = sorted(r["kinds"])
        r["variables"] = sorted(r["variables"])
        del r["times"]

    # 4. the data check on one representative file per radar
    checks: dict[str, Any] = {}
    if data_check:
        named = [r for r in radars.values() if r["representative"]]
        for n, r in enumerate(named):
            if cancel is not None and cancel():
                raise AutopilotError("cancelled")
            _progress(progress, 66 + int(28 * n / max(1, len(named))), f"Data check {r['name']}")
            try:
                checks[r["name"]] = _data_check(Path(r["representative"]))
            except Exception as exc:
                checks[r["name"]] = {"error": str(exc)[:200]}
        if len(named) and any(r["n_files"] > 1 for r in named):
            reductions.append(
                {
                    "what": "data check",
                    "from": sum(r["n_files"] for r in named),
                    "to": len(named),
                    "reason": "one representative volume per radar is checked (about 1 s each); "
                    "'rxd check' runs it on any file",
                }
            )

    # 5. pairs: distance, coverage overlap, time overlap, dual-Doppler geometry
    pairs: list[dict[str, Any]] = []
    names = [
        n
        for n in radars
        if radars[n]["latitude"] is not None and radars[n]["longitude"] is not None
    ]
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            ra, rb = radars[a], radars[b]
            d = _km(ra["latitude"], ra["longitude"], rb["latitude"], rb["longitude"])
            r1 = ra["max_range_km"] or 230.0
            r2 = rb["max_range_km"] or 230.0
            overlap_space = d < (r1 + r2)
            t_overlap = None
            if ra["t0"] and rb["t0"]:
                lo = max(ra["t0"], rb["t0"])
                hi = min(ra["t1"] or ra["t0"], rb["t1"] or rb["t0"])
                t_overlap = (
                    datetime.fromisoformat(hi) - datetime.fromisoformat(lo)
                ).total_seconds()
                t_overlap = (
                    max(t_overlap, 0.0)
                    if hi >= lo
                    else -(datetime.fromisoformat(lo) - datetime.fromisoformat(hi)).total_seconds()
                )
            geom = (
                dual_doppler_geometry(d, min(r1, 150.0), min(r2, 150.0))
                if overlap_space and d > 10
                else None
            )
            pairs.append(
                {
                    "a": a,
                    "b": b,
                    "distance_km": round(d, 1),
                    "overlap_space": overlap_space,
                    "time_overlap_s": t_overlap,
                    "both_velocity": ra["has_velocity"] and rb["has_velocity"],
                    "dual_doppler": geom,
                }
            )

    _progress(progress, 96, "Inventory ready")
    return {
        "version": 1,
        "n_files": len(files),
        "n_opened": sum(1 for f in files if f.get("opened")),
        "total_bytes": total_bytes,
        "files": files,
        "radars": radars,
        "pairs": pairs,
        "checks": checks,
        "unreadable": unreadable,
        "reductions": reductions,
        "elapsed_ms": round(1000 * (time.perf_counter() - t0)),
        "note": "Metadata only: no sweep data was loaded beyond the data check, nothing was "
        "downloaded or written.",
    }
