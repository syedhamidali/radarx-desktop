"""Convert a PERiLS bundle (SPEC v1, docs/PERILS_BUNDLE.md) into what the clip page reads.

The clip page (``?clip=1``, src/lib/components/StormClipView.svelte) cannot read ``.npz``, so this
step turns every merged volume into the 8-bit mosaic files of the existing storm-clip bundle
(``mosaic_K.json`` + ``mosaic_K.bin``, the format of tools/storm_clip/clipdata.py) and writes the
rest of the bundle as one compact ``perils.json`` in the scene frame (km east / north of the
volume origin, heights in km), plus small binary layers (MRMS, convective / stratiform, echo top).

Nothing here changes a measured value except the quantisation of the volume to 8 bits (0.31 dB for
the -10..70 dBZ range). The line motion used to interpolate between steps is *estimated from the
data* here, by phase correlation of the column-maximum reflectivity of consecutive steps (the
frames between observed steps are labelled INTERPOLATED on screen).

    python convert_bundle.py --bundle <bundle dir> --out <clip bundle dir>
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

VMIN, VMAX = -10.0, 70.0
# hydrometeor class ids of the data bundle (docs/PERILS_BUNDLE.md); hail = the "hail" class of the
# CSU HID scheme.  Changed here, in one place, if the bundle's table differs.
HAIL_IDS = (9,)
GRAUPEL_IDS = (7, 8)


# ── time and geometry ───────────────────────────────────────────────────────


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


class Geo:
    """lat / lon <-> km east / north of the volume origin (azimuthal equidistant, as the engine's mosaics)."""

    def __init__(self, lat0: float, lon0: float):
        self.lat0, self.lon0 = lat0, lon0
        try:
            import pyproj

            self._p = pyproj.Proj(proj="aeqd", lat_0=lat0, lon_0=lon0, datum="WGS84")
        except Exception:  # pragma: no cover - pyproj is a dependency of the engine
            self._p = None

    def xy(self, lat, lon):
        lat = np.asarray(lat, float)
        lon = np.asarray(lon, float)
        if self._p is not None:
            x, y = self._p(lon, lat)
            return np.asarray(x) / 1000.0, np.asarray(y) / 1000.0
        k = 111.19
        return (lon - self.lon0) * k * math.cos(math.radians(self.lat0)), (lat - self.lat0) * k

    def latlon(self, x, y):
        x = np.asarray(x, float)
        y = np.asarray(y, float)
        if self._p is not None:
            lon, lat = self._p(x * 1000.0, y * 1000.0, inverse=True)
            return np.asarray(lat), np.asarray(lon)
        k = 111.19
        return self.lat0 + y / k, self.lon0 + x / (k * math.cos(math.radians(self.lat0)))


# ── line motion by phase correlation ────────────────────────────────────────


def _prep(a: np.ndarray, lo: float = 10.0, hi: float = 55.0) -> np.ndarray:
    f = np.clip(np.nan_to_num(a, nan=lo), lo, hi) - lo
    f = f - f.mean()
    wy = np.hanning(f.shape[0])[:, None]
    wx = np.hanning(f.shape[1])[None, :]
    return f * wy * wx


def phase_correlate(a: np.ndarray, b: np.ndarray, max_shift_px: float | None = None) -> tuple[float, float, float]:
    """Shift (dx, dy) in pixels that carries ``a`` onto ``b`` (x = columns, y = rows) and the peak sharpness.

    Cross-power spectrum normalised to unit magnitude, sub-pixel peak by a parabola fit. A shift
    larger than ``max_shift_px`` is not searched for (the peak is looked for inside that radius).
    """
    fa, fb = np.fft.rfft2(_prep(a)), np.fft.rfft2(_prep(b))
    cross = np.conj(fa) * fb
    cross /= np.abs(cross) + 1e-9
    r = np.fft.irfft2(cross, s=a.shape)
    ny, nx = r.shape
    yy = np.fft.fftfreq(ny, 1.0 / ny)[:, None]
    xx = np.fft.fftfreq(nx, 1.0 / nx)[None, :]
    if max_shift_px is not None:
        r = np.where(np.hypot(yy, xx) <= max_shift_px, r, -np.inf)
    iy, ix = np.unravel_index(int(np.argmax(r)), r.shape)

    def sub(vm, v0, vp):
        d = vm - 2 * v0 + vp
        return 0.0 if abs(d) < 1e-12 else 0.5 * (vm - vp) / d

    ry = sub(r[(iy - 1) % ny, ix], r[iy, ix], r[(iy + 1) % ny, ix])
    rx = sub(r[iy, (ix - 1) % nx], r[iy, ix], r[iy, (ix + 1) % nx])
    dy = float(yy[iy, 0] + ry)
    dx = float(xx[0, ix] + rx)
    finite = r[np.isfinite(r)]
    sharp = float((r[iy, ix] - np.median(finite)) / (np.std(finite) + 1e-9))
    return dx, dy, sharp


def estimate_motion(colmax: list[np.ndarray], cell_km: float, times_s: list[float], max_speed_ms: float = 45.0) -> list[dict]:
    """Per interval: the displacement (km east, north) of the echo pattern, the velocity (m/s) and the peak sharpness.

    Sub-pixel phase correlation of the column-maximum reflectivity. A weak or implausible peak
    (sharpness < 4, or speed above ``max_speed_ms``) is replaced by the median of the other intervals
    and flagged, so a bad pair never throws an interval away from its neighbours' motion.
    """
    out: list[dict] = []
    for k in range(len(colmax) - 1):
        dt = times_s[k + 1] - times_s[k]
        mx = max_speed_ms * dt / 1000.0 / cell_km
        dx, dy, sh = phase_correlate(colmax[k], colmax[k + 1], max_shift_px=mx)
        out.append({"dx_km": dx * cell_km, "dy_km": dy * cell_km, "dt_s": dt, "sharpness": sh, "fallback": False})
    good = [o for o in out if o["sharpness"] >= 4.0 and math.hypot(o["dx_km"], o["dy_km"]) * 1000.0 / o["dt_s"] <= max_speed_ms]
    if good:
        mu = float(np.median([o["dx_km"] / o["dt_s"] for o in good]))
        mv = float(np.median([o["dy_km"] / o["dt_s"] for o in good]))
    else:
        mu = mv = 0.0
    for o in out:
        ok = o in good
        if not ok:
            o["dx_km"], o["dy_km"], o["fallback"] = mu * o["dt_s"], mv * o["dt_s"], True
        o["u_ms"] = o["dx_km"] * 1000.0 / o["dt_s"]
        o["v_ms"] = o["dy_km"] * 1000.0 / o["dt_s"]
    return out


# ── reading the bundle ──────────────────────────────────────────────────────


def _load_json(p: Path):
    if not p.exists():
        return None
    return json.loads(p.read_text())


def first_key(npz, *names):
    for n in names:
        if n in npz.files:
            return npz[n]
    return None


def encode_dbz(dbz: np.ndarray) -> np.ndarray:
    """dBZ (NaN = nothing) -> uint8 codes 0 = none, 1..255 over VMIN..VMAX."""
    q = 1.0 + (np.clip(dbz, VMIN, VMAX) - VMIN) / (VMAX - VMIN) * 254.0
    return np.where(np.isfinite(dbz), np.rint(q), 0).astype(np.uint8)


def _round(a, n=3):
    return np.round(np.asarray(a, float), n).tolist()


def read_series(s: dict) -> dict:
    """One station series with NaN / None kept as None."""
    out = {}
    for k, v in (s or {}).items():
        out[k] = [None if (x is None or (isinstance(x, float) and not math.isfinite(x))) else float(x) for x in v]
    return out


def sounding_levels(csv_text: str) -> dict:
    """Levels of a sounding CSV: pressure, height, temperature, dew point, u, v (tolerant to column names)."""
    lines = [ln for ln in csv_text.strip().splitlines() if ln.strip() and not ln.startswith("#")]
    head = [h.strip().lower() for h in lines[0].split(",")]

    def col(*names):
        for n in names:
            for i, h in enumerate(head):
                if h == n or h.startswith(n):
                    return i
        return None

    ip = col("pressure", "pres", "p_")
    iz = col("height_msl", "height", "alt", "gpheight", "z_")
    it = col("temperature", "temp", "tdry", "t_c", "t_")
    itd = col("dewpoint", "dewp", "dpt", "td")
    iu, iv = col("u_ms", "u_wind", "u"), col("v_ms", "v_wind", "v")
    isp, idr = col("wind_speed", "wspd", "speed"), col("wind_dir", "wdir", "direction")
    cols = {k: [] for k in ("pressure_hpa", "height_msl_m", "temperature_c", "dewpoint_c", "u_ms", "v_ms")}

    def val(parts, i):
        if i is None or i >= len(parts):
            return None
        try:
            v = float(parts[i])
        except ValueError:
            return None
        return v if math.isfinite(v) and v > -900 else None

    for ln in lines[1:]:
        parts = [p.strip() for p in ln.split(",")]
        cols["pressure_hpa"].append(val(parts, ip))
        cols["height_msl_m"].append(val(parts, iz))
        cols["temperature_c"].append(val(parts, it))
        cols["dewpoint_c"].append(val(parts, itd))
        u, v = val(parts, iu), val(parts, iv)
        if (u is None or v is None) and isp is not None and idr is not None:
            sp, dr = val(parts, isp), val(parts, idr)
            if sp is not None and dr is not None:
                u, v = -sp * math.sin(math.radians(dr)), -sp * math.cos(math.radians(dr))
        cols["u_ms"].append(u)
        cols["v_ms"].append(v)
    return cols


# ── the conversion ──────────────────────────────────────────────────────────


def convert(bundle: Path, out: Path, *, hail_cap: int = 5000, log=print) -> dict:
    bundle, out = Path(bundle), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    man = json.loads((bundle / "manifest.json").read_text())
    g = man["grid"]
    xs, ys, zs = (np.asarray(g[k], float) for k in ("x_km", "y_km", "z_km"))
    lat0, lon0 = float(g["origin_lat"]), float(g["origin_lon"])
    geo = Geo(lat0, lon0)
    cell = float(xs[1] - xs[0])
    t0 = parse_iso(man["steps"][0]["time"])
    steps = man["steps"]
    times_s = [(parse_iso(s["time"]) - t0).total_seconds() for s in steps]

    def rel(iso) -> float:
        return (parse_iso(iso) - t0).total_seconds()

    radars = man.get("radars", [])
    rsites = []
    for r in radars:
        x, y = geo.xy(r["lat"], r["lon"])
        rsites.append({"name": r["name"], "latitude": r["lat"], "longitude": r["lon"], "altitude_m": r.get("alt_m", 0.0), "x_m": float(x) * 1000.0, "y_m": float(y) * 1000.0, "type": r.get("type", "")})

    colmax: list[np.ndarray] = []
    msteps = []
    hail_pts, tds_pts, layers_meta, zdrcol = [], [], [], []
    rng = np.random.default_rng(11)
    for k, st in enumerate(steps):
        z = np.load(bundle / st["volume"])
        dbz = z["dbz"]
        nz, ny, nx = dbz.shape
        if (nz, ny, nx) != (len(zs), len(ys), len(xs)):
            raise ValueError(f"step {k}: volume {dbz.shape} does not match the manifest grid {(len(zs), len(ys), len(xs))}")
        codes = encode_dbz(dbz)
        header = {
            "mosaic_id": f"perils_{k:04d}", "variable": "DBZH", "units": "dBZ", "product": "mosaic",
            "grid": {
                "x0": float(xs[0]) * 1000.0, "y0": float(ys[0]) * 1000.0, "cell_m": cell * 1000.0, "nx": nx, "ny": ny, "nz": nz,
                "z": [float(v) * 1000.0 for v in zs], "origin_lat": lat0, "origin_lon": lon0,
            },
            "vmin": VMIN, "vmax": VMAX, "dtype": "uint8", "scale": {"offset": VMIN, "step": (VMAX - VMIN) / 254.0, "nodata": 0},
            "cmap": "NWSRef", "radars": rsites, "analysis_time": st["time"],
        }
        (out / f"mosaic_{k}.json").write_text(json.dumps(header))
        (out / f"mosaic_{k}.bin").write_bytes(np.ascontiguousarray(codes).tobytes())
        cm = np.nanmax(np.where(np.isfinite(dbz), dbz, -99.0), axis=0)
        colmax.append(np.where(cm > -90, cm, np.nan))
        # extras of this step (all optional)
        hid = z["hid"] if "hid" in z.files else None
        if hid is not None:
            sel = np.isin(hid, HAIL_IDS)
            kk, jj, ii = np.nonzero(sel)
            if len(kk) > hail_cap:
                pick = rng.choice(len(kk), hail_cap, replace=False)
                kk, jj, ii = kk[pick], jj[pick], ii[pick]
            hail_pts.append({"n": int(sel.sum()), "p": _round(np.stack([xs[ii], ys[jj], zs[kk]], 1), 2)})
        else:
            hail_pts.append(None)
        tds = first_key(z, "tds", "tds_flag")
        if tds is not None:
            kk, jj, ii = np.nonzero(tds > 0) if tds.ndim == 3 else (np.zeros(0, int),) * 3
            if tds.ndim == 2:
                jj, ii = np.nonzero(tds > 0)
                kk = np.zeros_like(jj)
            tds_pts.append(_round(np.stack([xs[ii], ys[jj], zs[kk]], 1), 2)[:3000])
        else:
            tds_pts.append(None)
        lay = {}
        cs = first_key(z, "conv_strat", "convstrat")
        if cs is not None:
            lay["conv_strat"] = np.asarray(cs, np.uint8)
        et = first_key(z, "echotop40_km", "echotop40")
        if et is not None:
            lay["echotop40"] = np.clip(np.rint(np.nan_to_num(et, nan=0) * 10), 0, 255).astype(np.uint8)
        vil = first_key(z, "vil")
        if vil is not None:
            lay["vil"] = np.clip(np.rint(np.nan_to_num(vil, nan=0)), 0, 255).astype(np.uint8)
        if lay:
            blob = b"".join(np.ascontiguousarray(v).tobytes() for v in lay.values())
            (out / f"layers_{k}.bin").write_bytes(blob)
            layers_meta.append({"bin": f"layers_{k}.bin", "names": list(lay.keys()), "ny": ny, "nx": nx})
        else:
            layers_meta.append(None)
        zc = first_key(z, "zdr_column_km", "zdr_col_km")
        if zc is not None:
            zdrcol.append(_zdr_markers(zc, xs, ys))
        else:
            zdrcol.append(None)
        msteps.append(
            {
                "index": k, "time_utc": st["time"], "t_s": times_s[k],
                "files": {r: {"name": r, "offset_s": float(st.get("offsets_s", {}).get(r, 0.0))} for r in st.get("radars", [])},
                "mosaic": {"header": f"mosaic_{k}.json", "bin": f"mosaic_{k}.bin"}, "wind": None, "eye": None,
            }
        )
        log(f"  step {k + 1}/{len(steps)}: {st['time']}  {int(np.isfinite(dbz).sum())} echo cells, radars {st.get('radars')}")

    mot = estimate_motion(colmax, cell, times_s)
    for k, o in enumerate(mot):
        log(f"  interval {k}: line motion {o['u_ms']:.1f}, {o['v_ms']:.1f} m/s (sharpness {o['sharpness']:.1f}{', FALLBACK' if o['fallback'] else ''})")
    # pseudo-track: the centre of the strong echo of step 0, then the estimated displacement per interval
    c0 = colmax[0]
    jj, ii = np.nonzero(np.nan_to_num(c0, nan=-9) >= 40)
    cx0 = float(xs[ii].mean()) if len(ii) else 0.0
    cy0 = float(ys[jj].mean()) if len(jj) else 0.0
    track = [(cx0, cy0)]
    for o in mot:
        track.append((track[-1][0] + o["dx_km"], track[-1][1] + o["dy_km"]))
    span = times_s[-1] - times_s[0]
    u = (track[-1][0] - track[0][0]) * 1000.0 / span if span else 0.0
    v = (track[-1][1] - track[0][1]) * 1000.0 / span if span else 0.0
    manifest = {
        "version": 1, "title": man.get("title", "PERiLS IOP2"), "case": man.get("case", ""), "variable": "DBZH", "units": "dBZ",
        "origin": {"lat": lat0, "lon": lon0}, "crop_half_km": float(min(xs.max(), ys.max())), "detail_m": cell * 1000.0,
        "steps": msteps,
        "storm": {
            "track_km": [list(p) for p in track], "track_lonlat": [[float(geo.latlon(p[0], p[1])[1]), float(geo.latlon(p[0], p[1])[0])] for p in track],
            "motion_ms": [u, v], "speed_ms": math.hypot(u, v), "heading_deg": (math.degrees(math.atan2(u, v)) + 360.0) % 360.0,
        },
        "sites": rsites, "perils": "perils.json",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))

    # ── everything else, in the scene frame ──
    p: dict = {
        "version": 1, "time0": steps[0]["time"], "origin": {"lat": lat0, "lon": lon0},
        "window": man.get("window", {}), "title": man.get("title", ""), "case": man.get("case", ""),
        "grid": {"x0": float(xs[0]), "y0": float(ys[0]), "dx": cell, "nx": len(xs), "ny": len(ys), "zTop": float(zs[-1])},
        "steps": [{"t_s": times_s[k], "time": s["time"], "radars": s.get("radars", []), "offsets_s": s.get("offsets_s", {})} for k, s in enumerate(steps)],
        "motion": mot, "radars": [], "sources": man.get("sources", []), "hail": hail_pts, "tds": tds_pts, "layers": layers_meta, "zdrColumns": zdrcol,
    }
    for r in rsites:
        p["radars"].append({"name": r["name"], "x": r["x_m"] / 1000.0, "y": r["y_m"] / 1000.0, "altKm": r["altitude_m"] / 1000.0, "type": r["type"], "lat": r["latitude"], "lon": r["longitude"]})
    # lightning mapping array
    lma = _load_json(bundle / man["lma"]) if man.get("lma") else None
    p["lma"] = None
    if lma:
        lt0 = rel(lma.get("t0", steps[0]["time"]))
        fl = []
        for f in lma["flashes"]:
            src = np.asarray(f["sources"], float)
            if not len(src):
                continue
            x, y = geo.xy(src[:, 1], src[:, 2])
            fl.append({"id": f["id"], "t0": lt0 + float(f["t_start_s"]), "t1": lt0 + float(f["t_end_s"]), "n": int(f.get("n_sources", len(src))),
                       "s": _round(np.stack([src[:, 0] + lt0, x, y, src[:, 3] / 1000.0, src[:, 4]], 1), 4)})
        p["lma"] = {"flashes": fl, "nSources": int(sum(len(f["s"]) for f in fl))}
        log(f"  LMA: {len(fl)} flashes, {p['lma']['nSources']} sources")
    # stations
    p["stations"] = []
    sts = _load_json(bundle / man["stations"]) if man.get("stations") else None
    for s in sts or []:
        x, y = geo.xy(s["lat"], s["lon"])
        p["stations"].append({"id": s["id"], "name": s.get("name", s["id"]), "type": s.get("type", ""), "x": float(x), "y": float(y), "lat": s["lat"], "lon": s["lon"], "altKm": s.get("alt_m", 0.0) / 1000.0,
                              "series": read_series(s.get("series")), "events": s.get("events") or s.get("hail_candidates") or []})
    # soundings
    p["soundings"] = []
    snd = _load_json(bundle / man["soundings"]) if man.get("soundings") else None
    for s in snd or []:
        x, y = geo.xy(s["lat"], s["lon"])
        csvp = bundle / s["profile_csv"] if s.get("profile_csv") else None
        levels = sounding_levels(csvp.read_text()) if csvp and csvp.exists() else None
        drift = []
        for d in s.get("drift", []):
            dx, dy = geo.xy(d[1], d[2])
            drift.append([float(d[0]) + rel(s["launch"]), float(dx), float(dy), float(d[3]) / 1000.0])
        p["soundings"].append({"id": s["id"], "site": s.get("site", s["id"]), "launch": rel(s["launch"]), "launchIso": s["launch"], "x": float(x), "y": float(y), "drift": drift, "levels": levels, "diagnostics": s.get("diagnostics", {})})
    p["environment"] = _load_json(bundle / man["environment"]) if man.get("environment") else None
    # tornadoes and rotation tracks
    p["tornadoes"] = []
    for tor in (_load_json(bundle / man["tornadoes"]) if man.get("tornadoes") else None) or []:
        path = []
        for q in tor.get("path", []):
            x, y = geo.xy(q["lat"], q["lon"])
            path.append({"t": float(q["t_s"]) if "t_s" in q else rel(q["time"]), "x": float(x), "y": float(y)})
        p["tornadoes"].append({**{k: v for k, v in tor.items() if k not in ("path", "start", "end")}, "path": path})
    p["rotation"] = []
    for tr in (_load_json(bundle / man["rotation_tracks"]) if man.get("rotation_tracks") else None) or []:
        pts = []
        for q in tr.get("points", []):
            x, y = geo.xy(q["lat"], q["lon"])
            pts.append({**{k: v for k, v in q.items() if k not in ("lat", "lon")}, "t": float(q["t_s"]) if "t_s" in q else rel(q["time"]), "x": float(x), "y": float(y)})
        p["rotation"].append({**{k: v for k, v in tr.items() if k != "points"}, "points": pts})
    # MRMS composites
    p["mrms"] = []
    for k, st in enumerate(steps):
        p["mrms"].append(_convert_mrms(bundle, st, k, geo, out) if st.get("mrms") else None)
    (out / "perils.json").write_text(json.dumps(p))
    log(f"wrote {out} ({len(steps)} steps, {sum(1 for m in p['mrms'] if m)} MRMS frames)")
    return p


def _zdr_markers(zc: np.ndarray, xs, ys, thr_km: float = 1.0, cap: int = 60) -> list:
    """Local maxima of the ZDR-column height above ``thr_km`` (x, y, height) as markers."""
    from scipy.ndimage import maximum_filter

    a = np.nan_to_num(zc.astype(float), nan=0.0)
    mx = (a == maximum_filter(a, size=9)) & (a >= thr_km)
    jj, ii = np.nonzero(mx)
    order = np.argsort(-a[jj, ii])[:cap]
    return [[round(float(xs[ii[o]]), 2), round(float(ys[jj[o]]), 2), round(float(a[jj[o], ii[o]]), 2)] for o in order]


def _convert_mrms(bundle: Path, st: dict, k: int, geo: Geo, out: Path) -> dict | None:
    f = bundle / st["mrms"]
    if not f.exists():
        return None
    z = np.load(f)
    refl = first_key(z, "refl", "dbz", "reflectivity", "composite", "MergedReflectivityQCComposite")
    if refl is None:
        return None
    refl = np.asarray(refl, float)
    if "x_km" in z.files and "y_km" in z.files:
        mx, my = np.asarray(z["x_km"], float), np.asarray(z["y_km"], float)
        grid = refl
    else:  # lat / lon regular grid: resample onto a 1 km local grid of the same extent
        from scipy.ndimage import map_coordinates

        lat, lon = np.asarray(z["lat"], float), np.asarray(z["lon"], float)
        lat_a = lat if lat.ndim == 1 else lat[:, 0]
        lon_a = lon if lon.ndim == 1 else lon[0, :]
        half = 250.0
        mx = my = np.arange(-half, half + 0.01, 1.0)
        X, Y = np.meshgrid(mx, my)
        la, lo = geo.latlon(X, Y)
        flip = lat_a[0] > lat_a[-1]
        ri = np.interp(la, lat_a[::-1], np.arange(len(lat_a))[::-1]) if flip else np.interp(la, lat_a, np.arange(len(lat_a)))
        ci = np.interp(lo, lon_a, np.arange(len(lon_a)))
        grid = map_coordinates(np.nan_to_num(refl, nan=-30.0), [ri, ci], order=1, mode="nearest")
        grid = np.where(grid < 0, np.nan, grid)
    codes = encode_dbz(grid)
    (out / f"mrms_{k}.bin").write_bytes(np.ascontiguousarray(codes).tobytes())
    return {"bin": f"mrms_{k}.bin", "x0": float(mx[0]), "y0": float(my[0]), "dx": float(mx[1] - mx[0]), "nx": int(len(mx)), "ny": int(len(my))}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if not (Path(a.bundle) / "manifest.json").exists():
        sys.exit(f"{a.bundle}/manifest.json not found")
    convert(Path(a.bundle), Path(a.out))


if __name__ == "__main__":
    main()
