"""Build the seed data of the tornado simulation from the PERiLS IOP2 bundle.

Reads the bundle written by tools/perils_movie (tornadoes, rotation/vortex kinematics, stations, lightning
events, merged radar volume) and writes ``tornado_seed.json``, the input of ``TornadoScene.setSeedData``
(see docs/TORNADO_SIM.md). Nothing is downloaded; the bundle is only read.

    python tools/tornado_sim/make_seed.py --movie ~/Downloads/xradar-exports/perils_movie \
        --out ~/Downloads/xradar-exports/perils_movie/tornado_sim/tornado_seed.json

OBSERVED in the seed: survey rating / width / begin and end points and times; the parent-circulation Vrot and
couplet size from the radar (median of the steps); near-surface T, Td and p of the nearest station; the
lightning flash positions and times; the lowest merged radar reflectivity level around the tornado.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

TORNADO_ID = "NSED_1035605"
M_PER_DEG_LAT = 110574.0
M_PER_DEG_LON_EQ = 111320.0


def km_between(lat1, lon1, lat2, lon2):
    return math.hypot((lat1 - lat2) * 111.2, (lon1 - lon2) * 111.2 * math.cos(math.radians(lat1)))


def vol_path(man: dict, k: int) -> str:
    """The volume file of step ``k`` as the manifest names it (volumes/ or volumes_v2/)."""
    st = man["steps"][k]
    return st["volume"] if isinstance(st, dict) and "volume" in st else f"volumes/step_{k:04d}.npz"


def station_series(st, key, t0, t1):
    se = st["series"]
    t = np.array(se["t_s"], dtype=float)
    v = np.array([np.nan if x is None else x for x in se[key]], dtype=float)
    m = (t >= t0) & (t <= t1) & np.isfinite(v)
    return float(np.mean(v[m])) if m.any() else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--movie", default=str(Path.home() / "Downloads/xradar-exports/perils_movie"))
    ap.add_argument("--out", default=None)
    ap.add_argument(
        "--shot",
        default="415,505",
        help="data-clock seconds of the shot (stations, echo and flashes are taken here)",
    )
    ap.add_argument("--echo-half-km", type=float, default=8.0)
    ap.add_argument(
        "--manifest",
        default="manifest.json",
        help="manifest.json or manifest_v2.json of the bundle",
    )
    ap.add_argument(
        "--ramp-form",
        type=float,
        default=0.0,
        help="seconds the simulated tornado takes to reach full intensity after the surveyed begin time (0: the simulation's default of 25 s)",
    )
    ap.add_argument(
        "--ramp-decay",
        type=float,
        default=40.0,
        help="seconds it takes to die away before the surveyed end time",
    )
    ap.add_argument(
        "--echo-series",
        action="store_true",
        help="also write the echo of every step of the survey's life (echoSeries) and the pattern motion",
    )
    args = ap.parse_args()
    movie = Path(args.movie).expanduser()
    bundle = movie / "bundle"
    t0, t1 = (float(x) for x in args.shot.split(","))
    tm = 0.5 * (t0 + t1)

    tors = json.loads((bundle / "tornadoes" / "tornadoes.json").read_text())
    tor = next(t for t in tors if t["id"] == TORNADO_ID)
    track = [{"t": p[0], "lat": p[1], "lon": p[2]} for p in tor["path"]]
    begin = tor["begin"]
    lat_t = float(np.interp(tm, [p["t"] for p in track], [p["lat"] for p in track]))
    lon_t = float(np.interp(tm, [p["t"] for p in track], [p["lon"] for p in track]))

    # parent circulation from the radar (context/vortex_kinematics.json): median over the steps
    vk = json.loads((bundle / "context" / "vortex_kinematics.json").read_text())
    summ = vk["summary"]
    vrot = float(summ["vrot_ms_min_median_max"][1])
    diam_km = float(summ["couplet_diameter_km_min_median_max"][1])
    meso = {
        "vrot": round(vrot, 2),
        "radiusM": round(diam_km * 1000 / 2, 1),
        "radar": ",".join(summ.get("best_radars", [])) or None,
    }
    sense = (
        "cyclonic"
        if summ["sense_counts"].get("cyclonic", 0) >= summ["sense_counts"].get("anticyclonic", 0)
        else "anticyclonic"
    )

    # stations: nearest to the tornado at the shot time with T and Td; and the nearest warm (T - Td >= 1.5 K) station
    stations = json.loads((bundle / "stations" / "stations.json").read_text())
    cands = []
    for s in stations:
        se = s.get("series", {})
        if "temperature_c" not in se or "dewpoint_c" not in se or "pressure_hpa" not in se:
            continue
        T = station_series(s, "temperature_c", t0, t1)
        Td = station_series(s, "dewpoint_c", t0, t1)
        P = station_series(s, "pressure_hpa", t0, t1)
        if T is None or Td is None or P is None:
            continue
        lat = s.get("lat")
        lon = s.get("lon")
        if lat is None:  # mobile mesonet: lat/lon are series
            la = station_series(s, "lat", t0, t1)
            lo = station_series(s, "lon", t0, t1)
            lat, lon = la, lo
        if lat is None:
            continue
        cands.append(
            {"id": s["id"], "T": T, "Td": Td, "P": P, "km": km_between(lat_t, lon_t, lat, lon)}
        )
    cands.sort(key=lambda c: c["km"])
    near = cands[0]
    warm = next((c for c in cands if c["T"] - c["Td"] >= 1.5), near)

    def thermo(c, why):
        return {
            "t0": round(c["T"], 2),
            "td0": round(c["Td"], 2),
            "p0": round(c["P"], 1),
            "source": f"{c['id']} {c['km']:.0f} km from the tornado, mean over data t = {t0:.0f}-{t1:.0f} s, T {c['T']:.1f} Td {c['Td']:.1f} C ({why})",
        }

    # lightning flashes (events.json of the movie: position and time of every LMA flash)
    ev = json.loads((movie / "events.json").read_text())
    flashes = []
    for f in ev["flashes"]:
        td = f["t_data_s"]
        if td < t0 - 60 or td > t1 + 60:
            continue
        if km_between(lat_t, lon_t, f["lat"], f["lon"]) > 45:
            continue
        flashes.append(
            {
                "t": round(td, 3),
                "lat": f["lat"],
                "lon": f["lon"],
                "altM": f["alt_m"],
                "nSources": f["n_sources"],
            }
        )

    # radar echo: lowest reliable level of the merged volume nearest to the shot time, a box around the tornado
    man = json.loads((bundle / args.manifest).read_text())
    steps = man["steps"]
    k = (
        min(range(len(steps)), key=lambda i: abs(steps[i]["t_s"] - tm))
        if isinstance(steps[0], dict) and "t_s" in steps[0]
        else round(tm / 60.0)
    )
    vol = np.load(bundle / vol_path(man, k))
    g = man["grid"]
    xs = np.array(g["x_km"])
    ys = np.array(g["y_km"])
    zs = np.array(g["z_km"])
    zi = int(np.argmin(abs(zs - 0.5)))
    dbz = vol["dbz"][zi]
    # tornado position in the grid frame at tm
    bx = begin["x_km"]
    by = begin["y_km"]
    ex = tor["end_point"]["x_km"]
    ey = tor["end_point"]["y_km"]
    f = (tm - track[0]["t"]) / (track[-1]["t"] - track[0]["t"])
    tx, ty = bx + (ex - bx) * f, by + (ey - by) * f
    h = args.echo_half_km
    ix = np.where((xs >= tx - h) & (xs <= tx + h))[0]
    iy = np.where((ys >= ty - h) & (ys <= ty + h))[0]
    sub = dbz[np.ix_(iy, ix)]
    echo = {
        "x0": float((xs[ix[0]] - bx) * 1000.0),
        "y0": float((ys[iy[0]] - by) * 1000.0),
        "dx": 500.0,
        "nx": len(ix),
        "ny": len(iy),
        "dbz": [None if not np.isfinite(v) else round(float(v), 1) for v in sub.ravel()],
        "t": float(k * 60.0),
    }

    # the echo through the survey's life: every observed step, on one grid that covers the whole track, so that the simulation's rain
    # follows the radar echo as it moves and changes (motion-compensated between the steps, see src/lib/utils/tornado/echoField.ts)
    series = []
    if args.echo_series:
        mid_x, mid_y = 0.5 * (bx + ex), 0.5 * (by + ey)
        hh = 0.5 * math.hypot(ex - bx, ey - by) + args.echo_half_km
        jx = np.where((xs >= mid_x - hh) & (xs <= mid_x + hh))[0]
        jy = np.where((ys >= mid_y - hh) & (ys <= mid_y + hh))[0]
        for kk, st in enumerate(steps):
            ts = st["t_s"] if isinstance(st, dict) and "t_s" in st else kk * 60.0
            if ts < track[0]["t"] - 150.0 or ts > track[-1]["t"] + 150.0:
                continue
            v2 = np.load(bundle / vol_path(man, kk))
            sub2 = v2["dbz"][zi][np.ix_(jy, jx)]
            series.append(
                {
                    "x0": float((xs[jx[0]] - bx) * 1000.0),
                    "y0": float((ys[jy[0]] - by) * 1000.0),
                    "dx": 500.0,
                    "nx": len(jx),
                    "ny": len(jy),
                    "dbz": [
                        None if not np.isfinite(q) else round(float(q), 1) for q in sub2.ravel()
                    ],
                    "t": float(ts),
                }
            )
    pm = man.get("pattern_motion") or {}

    seed = {
        "ef": tor["rating"],
        "width": tor["width_m"],
        "track": track,
        "span": [track[0]["t"], track[-1]["t"]],
        "sense": sense,
        "meso": meso,
        "thermo": thermo(
            near, "nearest station: the near-surface air that the funnel's pressure drop condenses"
        ),
        "inflowThermo": thermo(
            warm,
            "nearest station with T - Td >= 1.5 K: the warm inflow that sets the ambient cloud base",
        ),
        "lightningFlashes": flashes,
        "echo": echo,
        **(
            {
                "echoSeries": series,
                "echoMotion": [float(pm.get("u_ms", 0.0)), float(pm.get("v_ms", 0.0))],
            }
            if series
            else {}
        ),
        "survey": {
            "id": tor["id"],
            "source": "NWS damage survey (NCEI Storm Events, SPC) EF2 event 1035605: rating, width, begin/end and times",
            "narrative": tor.get("survey_narrative_start"),
        },
        "epochUtc": man["window"]["start"],
        **({"ramp": {"form": args.ramp_form, "decay": args.ramp_decay}} if args.ramp_form else {}),
    }
    notes = {
        "origin": "first track point (survey begin) unless the seed names one",
        "observed": [
            "survey rating, width, path (straight line between begin and end), times",
            f"parent circulation Vrot {meso['vrot']} m/s (median of the steps), couplet diameter {diam_km} km; the tornado vortex itself is unresolved on a 500 m grid",
            "station T, Td, p",
            f"{len(flashes)} lightning flashes (LMA position and time)",
            "merged radar reflectivity at 0.5 km",
        ],
        "modelled": ["near-core wind profile", "funnel shape", "debris", "cloud and rain texture"],
        "survey_scale_estimate": vk.get("survey_scale_estimate"),
        "shot": [t0, t1],
        "stations_by_distance": [
            {k2: (round(v, 2) if isinstance(v, float) else v) for k2, v in c.items()}
            for c in cands[:6]
        ],
    }
    out = Path(args.out).expanduser() if args.out else movie / "tornado_sim" / "tornado_seed.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"seed": seed, "notes": notes}, indent=1))
    print(
        f"wrote {out}: {len(flashes)} flashes, echo {echo['nx']}x{echo['ny']}"
        + (
            f" and a series of {len(series)} steps {series[0]['nx']}x{series[0]['ny']}"
            if series
            else ""
        )
        + f", nearest station {near['id']} ({near['km']:.1f} km, T {near['T']:.1f} Td {near['Td']:.1f}), warm {warm['id']} ({warm['km']:.1f} km)"
    )


if __name__ == "__main__":
    main()
