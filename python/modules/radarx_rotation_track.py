"""radarx: rotation tracks, the swath of the largest low-level azimuthal shear over volumes."""

from __future__ import annotations

import glob
import importlib.util
import math
import sys
from pathlib import Path

import numpy as np

from engine import rotation as rot
from engine.plugins import ModuleContext, ModuleError, Result, line, module, param, point


def _load_adapter():
    """Import the sibling ``_radarx_adapter.py`` (modules are loaded by path)."""
    name = "xradar_module_radarx_adapter"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).with_name("_radarx_adapter.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


A = _load_adapter()
MAX_TRACK_ROWS = 40

NOTES = """\
Rotation tracks: the maximum, over a sequence of volumes of ONE radar, of the low-level azimuthal \
shear, drawn on one radar-centred grid, and the couplets of the volumes linked into tracks. \
Class RETRIEVED: a quantity inferred from observed Doppler velocity with assumptions. It is not \
a tornado warning, not a tornado detection and not a damage estimate, and its width and length \
are not the width and length of any damage.

Per volume (all by radarx): the lowest sweep with velocity (at most the chosen elevation) is \
dealiased (region method), the azimuthal shear is the local linear least-squares derivative of \
Smith and Elmore (2004) and Miller et al. (2013) in a window of fixed size (default 750 m along \
x 2500 m across the beam; positive = cyclonic in the Northern Hemisphere), and couplets are \
compact maxima of it (rotation_couplets). The swath keeps, in every cell of a 500 m grid, the \
largest shear that any volume had at the nearest gate (the idea of the NSSL MRMS rotation tracks, \
Smith et al. 2016, which use many radars and a layer of tilts; this module uses one radar and one \
tilt). Couplets are linked from volume to volume by nearest neighbour within a speed limit \
(default 40 m/s): a plain greedy link, not the reflectivity cell tracker.

Limits you must read with every result:
- Sampling gaps. A volume takes 4 to 6 minutes; between volumes nothing is observed. The swath is \
a set of snapshots, not a continuous path, and a short-lived vortex can fall between two. The \
gaps between the volumes used are listed.
- Range-dependent resolution. The beam widens with range (about 1 km wide at 57 km for a 1 degree \
beam) and the window holds fewer rays; a circulation narrower than about two beam widths is not \
resolved, and the shear of a small vortex is under-estimated at long range. Near the radar the \
window spans a large angle and the shear is biased high (a Rankine vortex 15 km away reads about \
20 % above its true value in the tests), and a plain uniform wind shows as shear of about wind \
speed divided by range (20 m/s at 5 km: 0.004 1/s); gates closer than the minimum range (5 km) \
are left out. The beam is also higher with range: far from the radar the lowest tilt is not \
near the ground.
- Aliasing. Shear is only valid if the velocity is dealiased; a wrong unfolding makes fold lines \
that look like strong rotation. Check the dealiasing (Velocity Dealiasing module) for the volume.
- Other things that give shear: gust fronts, convergence lines, noise and ground clutter.
- One tilt: rotation that is only above the lowest tilt is not seen, and a volume whose lowest \
velocity tilt is above the chosen elevation is skipped.
Detection skill (hits, misses, false alarms) has not been measured for this product."""


def _expand(spec: str, limit: int) -> list[str]:
    """Files named by a folder, a glob pattern or a comma-separated list."""
    out: list[str] = []
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        path = Path(part).expanduser()
        if path.is_dir():
            out += sorted(
                str(p) for p in path.iterdir() if p.is_file() and not p.name.startswith(".")
            )
        elif any(ch in part for ch in "*?["):
            out += sorted(glob.glob(str(path)))
        elif path.is_file():
            out.append(str(path))
        else:
            raise ModuleError(f"'{part}' is not a file, a folder or a pattern that matches files")
    seen: dict[str, None] = {}
    for p in out:
        seen.setdefault(str(Path(p).resolve()), None)
    return list(seen)[:limit]


def _scan_time_s(ds) -> float | None:
    """Mean ray time of a sweep, seconds since the epoch."""
    try:
        t = np.asarray(ds["time"].values)
        t = t[~np.isnat(t)].astype("datetime64[ns]")
        if t.size == 0:
            return None
        t0 = t.min()
        mean = t0 + (t - t0).astype(np.int64).mean().astype("timedelta64[ns]")
        return float(mean.astype("datetime64[ms]").astype(np.int64)) / 1000.0
    except Exception:
        return None


def _lowest_velocity_sweep(tree, max_elev: float):
    """Index and elevation of the lowest sweep that has velocity, or (None, reason)."""
    from engine import varmap

    best = None
    dss = varmap.sweep_datasets(tree)
    for i, ds in enumerate(dss):
        if varmap.resolve(ds, "VRADH") is None:
            continue
        el = varmap.sweep_fixed_angle(ds)
        if el is None:
            continue
        if best is None or el < best[1]:
            best = (i, el)
    if best is None:
        return None, "no sweep with Doppler velocity"
    if best[1] > max_elev:
        return None, f"its lowest velocity sweep is at {best[1]:.1f} deg, above {max_elev:g}"
    return best, ""


def _one_volume(tree, rt, p: dict, label: str, progress=None):
    """Shear field and couplets of the lowest velocity sweep of one volume (all radarx)."""
    from engine.hydro.accumulate import TreeReader

    picked, why = _lowest_velocity_sweep(tree, p["max_elevation_deg"])
    if picked is None:
        return None, why
    index, elev = picked
    reader = TreeReader(tree)
    ctx = ModuleContext(tree, index, get_sweep_dataset=reader.get_sweep_dataset)
    ds = A.prepare_sweep(ctx, georeference=True)
    t_s = _scan_time_s(ds)
    if t_s is None:
        return None, "no time stamps"
    nyq, source = A.nyquist_velocity(ds, "VRADH", p["nyquist_velocity"])
    if p["dealias_first"]:
        da = A.dealias(rt, ds, "VRADH", nyq)
        ds = ds.assign({"VRADH_dealiased": da})
        field = "VRADH_dealiased"
    else:
        field = "VRADH"
    window = (float(p["window_along_m"]), float(p["window_across_m"]))
    keep = None  # True = keep
    if p["min_dbz"] > -30 and ctx.has("DBZH"):
        keep = ds["DBZH"] >= p["min_dbz"]  # echo-free gates carry no rotation
    if p["min_range_km"] > 0:
        near = ds["range"] >= p["min_range_km"] * 1000.0
        keep = near if keep is None else (keep & near)
    llsd_kwargs = {} if keep is None else {"mask": keep}
    with A.guard("Azimuthal shear"):
        sh = rt.llsd(ds, field, window=window, **llsd_kwargs)
    shear = A.polar_array(sh["azimuthal_shear"], ds, "azimuthal shear")
    kwargs = {
        "threshold": float(p["threshold"]),
        "window": window,
        "min_area": float(p["min_area_km2"]) * 1e6,
        "rotation": p["rotation"],
    }
    if p["min_dbz"] > -30 and ctx.has("DBZH"):
        kwargs["min_dbz"] = float(p["min_dbz"])
    with A.guard("Rotation couplets"):
        found = rt.rotation_couplets(ds, field, **kwargs)
    couplets = []
    for i in range(int(found.sizes["couplet"])):
        if float(found["range"].values[i]) < p["min_range_km"] * 1000.0:
            continue  # inside the minimum range the arc window is too wide for a plane fit
        couplets.append(
            rot.Couplet(
                x_m=float(found["x"].values[i]),
                y_m=float(found["y"].values[i]),
                azimuth_deg=float(found["azimuth"].values[i]),
                range_m=float(found["range"].values[i]),
                shear=float(found["azimuthal_shear"].values[i]),
                delta_v=float(found["delta_v"].values[i]),
                area_km2=float(found["area"].values[i]),
            )
        )
    info = {
        "label": label,
        "time_s": t_s,
        "sweep": index,
        "elevation": float(elev),
        "nyquist": float(nyq),
        "nyquist_source": source,
        "azimuth": np.asarray(ds["azimuth"].values, dtype=np.float64),
        "range": np.asarray(ds["range"].values, dtype=np.float64),
        "site": A.site_coords(tree),
    }
    return (info, shear, couplets), ""


def _utc(t_s: float) -> str:
    return np.datetime_as_string(np.datetime64(round(t_s * 1000), "ms"), unit="s") + "Z"


@module(
    id="radarx_rotation_track",
    title="Rotation Tracks (radarx)",
    description=(
        "Maximum low-level azimuthal shear over a sequence of volumes of one radar, drawn as a "
        "swath, with the rotation couplets linked into tracks. radarx dealiasing, LLSD shear "
        "(Smith & Elmore 2004) and couplets per volume. RETRIEVED; never a tornado warning or "
        "damage estimate; sampling gaps between volumes and range-dependent resolution apply."
    ),
    category="Hazards",
    requires=["VRADH"],
    needs=A.NEEDS,
    scope="volume",
    params=[
        param(
            "files",
            "string",
            "",
            "Other volumes (folder, pattern or list)",
            help="Volumes of the SAME radar, e.g. a folder or /data/KPAH/KPAH20211211_03*; "
            "separate several with commas. The open volume is always included.",
        ),
        param("max_volumes", "int", 20, "Most volumes used", min=2, max=60, step=1),
        param(
            "max_elevation_deg",
            "number",
            1.5,
            "Highest elevation of the lowest tilt (deg)",
            min=0.2,
            max=5,
            step=0.1,
            help="A volume whose lowest velocity tilt is higher is skipped (and said so)",
        ),
        param(
            "window_across_m",
            "number",
            2500.0,
            "Shear window across the beam (m)",
            min=500,
            max=6000,
            step=250,
        ),
        param(
            "window_along_m",
            "number",
            750.0,
            "Shear window along the beam (m)",
            min=250,
            max=3000,
            step=250,
        ),
        param(
            "threshold",
            "number",
            0.012,
            "Couplet threshold (1/s)",
            min=0.002,
            max=0.05,
            step=0.001,
            help="Shear at or above which a region is a couplet (tracks only, not the swath). "
            "radarx uses 0.006; on a 5-volume KPAH sample that gave ~45 couplets per volume, "
            "mostly noise",
        ),
        param("min_area_km2", "number", 0.5, "Min couplet area (km²)", min=0, max=20, step=0.1),
        param(
            "rotation",
            "select",
            "cyclonic",
            "Rotation",
            options=["cyclonic", "anticyclonic", "both"],
            help="Cyclonic = positive shear (Northern Hemisphere mesocyclones)",
        ),
        param(
            "min_dbz",
            "number",
            10.0,
            "Min reflectivity (dBZ, -30 = off)",
            help="Gates below this are left out of the shear fit and the couplets (needs DBZH)",
            min=-30,
            max=50,
            step=1,
        ),
        param(
            "min_range_km",
            "number",
            5.0,
            "Minimum range (km, 0 = off)",
            min=0,
            max=30,
            step=1,
            help="Gates closer than this are left out: the window spans a large angle and a "
            "uniform wind alone shows as shear of about wind speed / range",
        ),
        param(
            "min_track_volumes",
            "int",
            3,
            "Volumes a track must span",
            min=2,
            max=10,
            step=1,
            help="Shorter chains are dropped; a vortex seen in one or two volumes is mostly noise",
        ),
        param(
            "max_speed_ms",
            "number",
            40.0,
            "Fastest plausible couplet motion (m/s)",
            min=5,
            max=100,
            step=5,
            help="A couplet joins a track only within this speed of the track's last position",
        ),
        param(
            "grid_spacing_m",
            "number",
            500.0,
            "Swath grid spacing (m)",
            min=250,
            max=2000,
            step=250,
        ),
        param("dealias_first", "bool", True, "Dealias velocity first"),
        param(
            "nyquist_velocity",
            "number",
            0.0,
            "Nyquist velocity (m/s, 0 = auto)",
            min=0,
            max=100,
            step=0.5,
        ),
    ],
    version="1.0",
)
def run(
    ctx,
    files,
    max_volumes,
    max_elevation_deg,
    window_across_m,
    window_along_m,
    threshold,
    min_area_km2,
    rotation,
    min_dbz,
    min_range_km,
    min_track_volumes,
    max_speed_ms,
    grid_spacing_m,
    dealias_first,
    nyquist_velocity,
):
    from engine import web
    from engine.reader import RadarReader
    from engine.tracking import beam_height_km

    rt = A.rx()
    if not str(files).strip():
        raise ModuleError(
            "Give the other volumes of this radar in 'Other volumes' (a folder, a pattern or a "
            "comma-separated list); a track needs at least two volumes"
        )
    if web.is_web_mode():
        raise ModuleError("This module reads files from the server's disk and is off in web mode")
    paths = _expand(str(files), int(max_volumes))
    p = {
        "max_elevation_deg": float(max_elevation_deg),
        "window_across_m": float(window_across_m),
        "window_along_m": float(window_along_m),
        "threshold": float(threshold),
        "min_area_km2": float(min_area_km2),
        "rotation": rotation,
        "min_dbz": float(min_dbz),
        "min_range_km": float(min_range_km),
        "dealias_first": bool(dealias_first),
        "nyquist_velocity": float(nyquist_velocity),
    }
    notes: list[str] = []
    volumes: list[dict] = []
    frames: list[tuple[float, list[rot.Couplet]]] = []
    swath = None
    axes = None
    site0 = None
    max_range = 0.0

    def take(tree, label: str) -> None:
        nonlocal swath, axes, site0, max_range
        got, why = _one_volume(tree, rt, p, label)
        if got is None:
            notes.append(f"{label} skipped: {why}")
            return
        info, shear, couplets = got
        site = info["site"]
        lat, lon = (
            float(np.ravel(site.get("latitude", [np.nan]))[0]),
            float(np.ravel(site.get("longitude", [np.nan]))[0]),
        )
        if site0 is None:
            site0 = (lat, lon)
        elif np.isfinite(lat) and (abs(lat - site0[0]) > 0.01 or abs(lon - site0[1]) > 0.01):
            notes.append(f"{label} skipped: a different radar position (tracks use one radar)")
            return
        if any(abs(info["time_s"] - v["time_s"]) < 1.0 for v in volumes):
            notes.append(f"{label} skipped: same scan time as a volume already used")
            return
        if swath is None:
            top = float(info["range"][-1]) * math.cos(math.radians(info["elevation"]))
            max_range = max(max_range, top)
            axes = rot.swath_grid(top, float(grid_spacing_m))
            swath = np.full((axes[1].size, axes[0].size), np.nan)
        updated = rot.accumulate_swath(
            swath, shear, info["azimuth"], info["range"], info["elevation"], axes, rotation
        )
        finite = shear[np.isfinite(shear)]
        info["max_shear"] = float(finite.max()) if finite.size else None
        info["couplets"] = len(couplets)
        info["cells"] = updated
        volumes.append(info)
        frames.append((info["time_s"], couplets))

    ctx.progress(5, "Open volume")
    take(ctx.raw_datatree, "open volume")
    for k, path in enumerate(paths):
        ctx.progress(10 + 80 * k // max(len(paths), 1), f"Volume {k + 1} of {len(paths)}")
        reader = RadarReader()
        try:
            reader.open_file(path)
            take(reader.datatree, Path(path).name)
        except ModuleError:
            raise
        except Exception as exc:
            notes.append(f"{Path(path).name} skipped: {exc}")
        finally:
            reader.close()
    if len(volumes) < 2:
        raise ModuleError(
            "Fewer than two usable volumes: " + ("; ".join(notes) if notes else "none found")
        )
    volumes.sort(key=lambda v: v["time_s"])
    gaps = np.diff([v["time_s"] for v in volumes])
    tracks = rot.link_tracks(frames, float(max_speed_ms), min_obs=int(min_track_volumes))

    # swath on the polar grid of the sweep the user looks at, and its grid cells
    ds_sel = ctx.dataset
    az = np.asarray(ds_sel["azimuth"].values, dtype=np.float64)
    rg = np.asarray(ds_sel["range"].values, dtype=np.float64)
    from engine import varmap

    el_sel = varmap.sweep_fixed_angle(ds_sel) or 0.0
    gr = rg * math.cos(math.radians(el_sel))
    x = gr[None, :] * np.sin(np.radians(az))[:, None]
    y = gr[None, :] * np.cos(np.radians(az))[:, None]
    spacing = float(grid_spacing_m)
    ix = np.clip(np.rint((x - axes[0][0]) / spacing).astype(int), 0, axes[0].size - 1)
    iy = np.clip(np.rint((y - axes[1][0]) / spacing).astype(int), 0, axes[1].size - 1)
    on_sel = swath[iy, ix].astype(np.float32)

    peak_shear = float(np.nanmax(swath)) if np.isfinite(swath).any() else None
    res = Result()
    res.field(
        "Rotation track swath (RETRIEVED)",
        on_sel,
        units="1/s",
        vmin=0.0,
        vmax=max(0.012, peak_shear or 0.012),
        title="Max low-level azimuthal shear over the volumes (RETRIEVED)",
    )
    vol_rows = [
        {
            "time": _utc(v["time_s"]),
            "source": v["label"],
            "sweep": v["sweep"],
            "elev": round(v["elevation"], 2),
            "nyq": round(v["nyquist"], 1),
            "max_shear": None if v["max_shear"] is None else round(v["max_shear"], 4),
            "couplets": v["couplets"],
        }
        for v in volumes
    ]
    track_rows, lines, pts = [], [], []
    for i, tr in enumerate(tracks[:MAX_TRACK_ROWS]):
        tid = f"T{i + 1:02d}"
        speed, heading = tr.motion()
        pk = tr.peak
        track_rows.append(
            {
                "id": tid,
                "start": _utc(tr.times_s[0]),
                "end": _utc(tr.times_s[-1]),
                "obs": len(tr.couplets),
                "peak_shear": round(pk.shear, 4),
                "peak_vrot": round(pk.vrot, 1),
                "peak_time": _utc(tr.peak_time_s()),
                "length_km": round(tr.length_km(), 1),
                "speed": None if speed is None else round(speed, 1),
                "heading": None if heading is None else round(heading),
                "max_gap_min": round(max(tr.gaps_s) / 60.0, 1) if tr.gaps_s else None,
            }
        )
        color = "#ef4444" if pk.shear >= 0 else "#3b82f6"
        lines.append(
            line(tid, [[c.x_m, c.y_m] for c in tr.couplets], frame="xy", label=tid, color=color)
        )
        pts.append(
            point(f"{tid}p", [pk.x_m, pk.y_m], frame="xy", label=f"{pk.vrot:.0f} m/s", color=color)
        )
    res.scalars(
        [
            {"label": "Volumes used", "value": len(volumes)},
            {"label": "First volume", "value": _utc(volumes[0]["time_s"])},
            {"label": "Last volume", "value": _utc(volumes[-1]["time_s"])},
            {
                "label": "Mean time between volumes",
                "value": float(gaps.mean() / 60.0),
                "unit": "min",
            },
            {
                "label": "Longest gap between volumes",
                "value": float(gaps.max() / 60.0),
                "unit": "min",
            },
            {"label": "Swath peak shear", "value": peak_shear, "unit": "1/s"},
            {"label": f"Tracks ({int(min_track_volumes)}+ volumes)", "value": len(tracks)},
            {
                "label": "Beam height at 60 km, lowest tilt",
                "value": float(beam_height_km(60000.0, volumes[0]["elevation"])),
                "unit": "km",
            },
            {
                "label": "Provenance",
                "value": "RETRIEVED (estimate from Doppler velocity, not a warning)",
            },
            {"label": "Rotation sense", "value": rotation},
            {"label": "radarx", "value": A.radarx_version()},
        ]
    )
    res.table(
        f"Rotation tracks ({len(tracks)}"
        + (f", strongest {MAX_TRACK_ROWS} shown)" if len(tracks) > MAX_TRACK_ROWS else ")"),
        [
            {"key": "id", "label": "Track"},
            {"key": "start", "label": "First seen (UTC)"},
            {"key": "end", "label": "Last seen (UTC)"},
            {"key": "obs", "label": "Volumes", "digits": 0},
            {"key": "peak_shear", "label": "Peak shear", "unit": "1/s", "digits": 4},
            {"key": "peak_vrot", "label": "Peak Vrot", "unit": "m/s", "digits": 1},
            {"key": "peak_time", "label": "Peak at (UTC)"},
            {"key": "length_km", "label": "Length (between volumes)", "unit": "km", "digits": 1},
            {"key": "speed", "label": "Mean motion", "unit": "m/s", "digits": 1},
            {"key": "heading", "label": "Toward", "unit": "°", "digits": 0},
            {"key": "max_gap_min", "label": "Longest gap", "unit": "min", "digits": 1},
        ],
        track_rows,
        id_key="id",
    )
    res.table(
        "Volumes used",
        [
            {"key": "time", "label": "Time (UTC)"},
            {"key": "source", "label": "Source"},
            {"key": "sweep", "label": "Sweep", "digits": 0},
            {"key": "elev", "label": "Elevation", "unit": "°", "digits": 2},
            {"key": "nyq", "label": "Nyquist", "unit": "m/s", "digits": 1},
            {"key": "max_shear", "label": "Max shear", "unit": "1/s", "digits": 4},
            {"key": "couplets", "label": "Couplets", "digits": 0},
        ],
        vol_rows,
    )
    if lines:
        res.overlay(
            "Rotation tracks (positions at the volume times, straight between them)", [*lines, *pts]
        )
    long_gap = float(gaps.max()) / 60.0
    warn = [
        f"The longest gap between volumes is {long_gap:.1f} min; nothing is observed between "
        "volumes, so the swath is a set of snapshots and a short-lived vortex can be missed."
    ]
    if gaps.max() > 10 * 60:
        warn.append("A gap above 10 min: a track across it may link different circulations.")
    res.text("\n".join([*warn, *notes, "", NOTES]), title="About this product")
    return res
