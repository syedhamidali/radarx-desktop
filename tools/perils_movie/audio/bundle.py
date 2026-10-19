"""Loading the PERiLS data bundle (SPEC v1) for the soundtrack. Parsing is tolerant: the data agent's
final layout is documented in docs/PERILS_BUNDLE.md and the loader accepts the variants below.

All times are converted to "data time": seconds since the window start of the manifest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .dsd import Dsd, load_dsd
from .radar import RadarVolumes
from .timeline import parse_iso


@dataclass
class Flash:
    id: str
    t_start_s: float
    t_end_s: float
    centroid: tuple[float, float, float]  # lat, lon, alt_m
    sources: np.ndarray  # (n, 5): t_s (data time), lat, lon, alt_m, power_dbw
    n_sources: int = 0


@dataclass
class Station:
    id: str
    name: str
    type: str
    lat: float
    lon: float
    alt_m: float
    series: dict[str, tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    dsd: Dsd | None = None
    meta: dict = field(
        default_factory=dict
    )  # every non-series field of stations.json (network, instrument, ...)

    def has(self, name: str) -> bool:
        return name in self.series and np.isfinite(self.series[name][1]).any()

    def at(self, name: str, t_data, hold_s: float = 300.0) -> np.ndarray:
        """Series interpolated at data time(s); NaN outside the record (+- hold_s) or without data."""
        t = np.asarray(t_data, float)
        if name not in self.series:
            return np.full(t.shape, np.nan)
        ts, vs = self.series[name]
        m = np.isfinite(vs) & np.isfinite(ts)
        if not m.any():
            return np.full(t.shape, np.nan)
        ts, vs = ts[m], vs[m]
        out = np.interp(t, ts, vs)
        out = np.where((t < ts[0] - hold_s) | (t > ts[-1] + hold_s), np.nan, out)
        return out

    def at_circular_deg(self, name: str, t_data, hold_s: float = 300.0):
        """(u, v) unit vectors of a direction series (deg), for averaging."""
        d = np.radians(self.at(name, t_data, hold_s))
        return np.sin(d), np.cos(d)


@dataclass
class TornadoTrack:
    id: str
    name: str
    ef: str | None
    t: np.ndarray  # data time (s)
    lat: np.ndarray
    lon: np.ndarray
    width_m: float = 200.0
    meta: dict = field(default_factory=dict)

    def position(self, t_data):
        """(lat, lon, exists) at data time(s); the tornado exists only inside its surveyed track."""
        t = np.asarray(t_data, float)
        exists = (t >= self.t[0]) & (t <= self.t[-1])
        return np.interp(t, self.t, self.lat), np.interp(t, self.t, self.lon), exists


@dataclass
class Event:
    type: str
    t_data_s: float | None
    t_clip_s: float | None
    t_end_data_s: float | None
    t_end_clip_s: float | None
    lat: float | None
    lon: float | None
    alt_m: float
    props: dict


@dataclass
class Bundle:
    window_start: str
    window_end: str | None
    flashes: list[Flash]
    stations: list[Station]
    tornadoes: list[TornadoTrack] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    radar_samples: dict[str, np.ndarray] | None = None
    radar_volumes: object | None = None  # radar.RadarVolumes: the volumes sampled at the listener
    manifest: dict = field(default_factory=dict)
    path: str | None = None


def _read(p: Path):
    return json.loads(p.read_text()) if p.exists() else None


def _num(x, default=np.nan):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _window(manifest: dict, flashes_raw) -> tuple[str, str | None]:
    w = manifest.get("window") if isinstance(manifest.get("window"), dict) else manifest
    for k in ("start", "window_start", "t0", "data_start", "start_time"):
        if w.get(k):
            return w[k], w.get("end") or w.get("window_end") or w.get("data_end")
    if isinstance(flashes_raw, dict) and flashes_raw.get("t0"):
        return flashes_raw["t0"], None
    raise ValueError("cannot find the window start in manifest.json (expected window.start)")


def _time(v, ws_dt, key_data="t_s") -> float | None:
    """A time value that is either seconds since the window start or an ISO string."""
    if v is None:
        return None
    if isinstance(v, str):
        return (parse_iso(v) - ws_dt).total_seconds()
    return float(v)


def parse_flashes(raw, window_start: str) -> list[Flash]:
    if raw is None:
        return []
    ws = parse_iso(window_start)
    t0 = raw.get("t0") if isinstance(raw, dict) else None
    shift = (parse_iso(t0) - ws).total_seconds() if t0 else 0.0
    out = []
    for i, f in enumerate(raw["flashes"] if isinstance(raw, dict) else raw):
        src = np.array(f.get("sources") or [], float).reshape(-1, 5)
        if src.size:
            src[:, 0] += shift
        c = f.get("centroid") or [np.nan, np.nan, 0.0]
        ts, te = float(f["t_start_s"]) + shift, float(f.get("t_end_s", f["t_start_s"])) + shift
        if not src.size and np.isfinite(c[0]):
            # no source list: the centroid, spread over the flash duration, at a nominal power
            tt = np.linspace(ts, te, 8)
            src = np.column_stack(
                [tt, np.full(8, c[0]), np.full(8, c[1]), np.full(8, c[2]), np.full(8, 0.0)]
            )
        out.append(
            Flash(
                str(f.get("id", i)),
                ts,
                te,
                (float(c[0]), float(c[1]), float(c[2])),
                src,
                int(f.get("n_sources", len(src))),
            )
        )
    return out


def parse_stations(raw) -> list[Station]:
    if raw is None:
        return []
    out = []
    for i, s in enumerate(raw["stations"] if isinstance(raw, dict) else raw):
        ser = {}
        sr = s.get("series") or {}
        t = np.array([_num(x) for x in sr.get("t_s", [])], float)
        for k, v in sr.items():
            if k == "t_s" or not isinstance(v, list) or len(v) != len(t):
                continue
            ser[k] = (t, np.array([_num(x) for x in v], float))
        out.append(
            Station(
                str(s.get("id", i)),
                str(s.get("name", s.get("id", i))),
                str(s.get("type", "")),
                float(s["lat"]),
                float(s["lon"]),
                float(s.get("alt_m") or 0.0),
                ser,
                list(s.get("events") or []),
                meta={k: v for k, v in s.items() if k not in ("series", "events")},
            )
        )
    return out


def parse_tornadoes(raw, window_start: str) -> list[TornadoTrack]:
    """tornadoes.json: a list (or {"tornadoes": [...]}) of tracks. Each has a path of points with
    a time ("t_s" seconds since the window start, or "time" ISO) and lat/lon, as a list of dicts or
    of [t, lat, lon]; or "start_time"/"end_time" with "start"/"end" lat/lon (straight track)."""
    if raw is None:
        return []
    ws = parse_iso(window_start)
    items = raw.get("tornadoes", raw.get("features", [])) if isinstance(raw, dict) else raw
    out = []
    for i, tr in enumerate(items):
        tr = tr.get("properties", tr) | ({"path": tr["path"]} if "path" in tr else {})
        pts = tr.get("path") or tr.get("track") or tr.get("points")
        t, la, lo = [], [], []
        if pts:
            for p in pts:
                if isinstance(p, dict):
                    tv = _time(p.get("t_s", p.get("time", p.get("t"))), ws)
                    t.append(tv)
                    la.append(float(p["lat"]))
                    lo.append(float(p["lon"]))
                else:
                    t.append(_time(p[0], ws))
                    la.append(float(p[1]))
                    lo.append(float(p[2]))
        elif tr.get("start") and tr.get("end"):
            ts = _time(tr.get("start_time", tr.get("t_start_s")), ws)
            te = _time(tr.get("end_time", tr.get("t_end_s")), ws)
            s, e = tr["start"], tr["end"]
            s = (s["lat"], s["lon"]) if isinstance(s, dict) else s
            e = (e["lat"], e["lon"]) if isinstance(e, dict) else e
            t, la, lo = [ts, te], [s[0], e[0]], [s[1], e[1]]
        if len(t) < 2 or any(x is None for x in t):
            continue
        o = np.argsort(t)
        out.append(
            TornadoTrack(
                str(tr.get("id", i)),
                str(tr.get("name", tr.get("id", i))),
                tr.get("ef", tr.get("rating")),
                np.array(t, float)[o],
                np.array(la, float)[o],
                np.array(lo, float)[o],
                float(tr.get("width_m") or 200.0),
                {k: v for k, v in tr.items() if k not in ("path", "track", "points")},
            )
        )
    return out


def parse_events(raw, window_start: str) -> list[Event]:
    """events.json of the renderer. Either its dict {"flashes": [...], "tornado": {"t_clip_start_s",
    "t_clip_end_s", "positions": [{t_clip_s, lat, lon}]}, "hail": [{station, t_clip_s, t_data_s, lat,
    lon, max_d_mm, n_large, ...}], ...} or a flat list / {"events": [...]} of {"type": "tornado" |
    "hail" | "flash", "t_clip_s" and/or "t_s", optional "t_end_clip_s" / "t_end_s", "lat", "lon", ...}."""
    if raw is None:
        return []
    ws = parse_iso(window_start)
    items: list[dict] = []
    if (
        isinstance(raw, dict)
        and ("tornado" in raw or "hail" in raw or "flashes" in raw)
        and "events" not in raw
    ):
        tor = raw.get("tornado")
        if tor:
            items.append(
                {
                    "type": "tornado",
                    "t_clip_s": tor.get("t_clip_start_s"),
                    "t_end_clip_s": tor.get("t_clip_end_s"),
                    "id": tor.get("id"),
                    "positions": tor.get("positions"),
                    "rating": tor.get("rating"),
                }
            )
        items += [{"type": "hail", **h} for h in raw.get("hail") or []]
        items += [{"type": "flash", **f} for f in raw.get("flashes") or []]
    else:
        items = raw.get("events", []) if isinstance(raw, dict) else raw
    out = []
    for e in items:
        td = _time(e.get("t_s", e.get("time", e.get("t_data_s"))), ws)
        te = _time(e.get("t_end_s", e.get("end_time")), ws)
        out.append(
            Event(
                str(e.get("type", "")).lower(),
                td,
                None if e.get("t_clip_s") is None else float(e["t_clip_s"]),
                te,
                None if e.get("t_end_clip_s") is None else float(e["t_end_clip_s"]),
                None if e.get("lat") is None else float(e["lat"]),
                None if e.get("lon") is None else float(e["lon"]),
                float(e.get("alt_m", 0.0) or 0.0),
                e,
            )
        )
    return out


def parse_radar_samples(raw, window_start: str):
    """Optional radar-derived samples along the listener's ground track (assumed layout, see
    docs/PERILS_AUDIO.md): {"samples": [{"t_s", "lat", "lon", "convective": 0..1, "hail": 0..1,
    "dbz"}]}. Returns column arrays or None."""
    if raw is None:
        return None
    ws = parse_iso(window_start)
    rows = raw.get("samples", []) if isinstance(raw, dict) else raw
    if not rows:
        return None
    return {
        "t": np.array([_time(r.get("t_s", r.get("time")), ws) for r in rows], float),
        "lat": np.array([_num(r.get("lat")) for r in rows]),
        "lon": np.array([_num(r.get("lon")) for r in rows]),
        "convective": np.array([_num(r.get("convective", 0.0), 0.0) for r in rows]),
        "hail": np.array([_num(r.get("hail", 0.0), 0.0) for r in rows]),
        "dbz": np.array([_num(r.get("dbz")) for r in rows]),
    }


HAIL_SAMPLE_S = 10.0  # the PIPS size-velocity samples are 10 s


def attach_hail_events(stations: list[Station], raw) -> None:
    """stations/hail_events.json: ``[{"station", "t_s", "kind": "hail_candidate", "max_d_mm", "n_large"}]``
    (one per 10 s sample with large particles) -> normalised station events with a time span, the
    maximum diameter and the particle counts of that sample."""
    if not raw:
        return
    by_id = {s.id: s for s in stations}
    for e in raw:
        s = by_id.get(str(e.get("station")))
        if s is None:
            continue
        t0 = float(e["t_s"])
        s.events.append(
            {
                "type": str(e.get("kind", e.get("type", "hail_candidate"))),
                "t_s": t0,
                "t_end_s": float(e.get("t_end_s", t0 + HAIL_SAMPLE_S)),
                "dmax_mm": e.get("max_d_mm", e.get("dmax_mm")),
                "counts": e.get("n_large", e.get("counts")),
                "evidence": e.get("evidence"),
            }
        )


def attach_mrr(stations: list[Station], path: Path, site: str, window_start: str) -> None:
    """profiles/mrr_<site>.npz (MRR: t_s, height_m, fall_speed_ms (t, gate), rain_rate_mmh (t, gate)):
    the lowest usable gates (150 to 600 m) become the station series ``mrr_fall_speed_ms`` (absolute value)
    and ``mrr_rain_rate_mmh`` of the station whose id contains the site name."""
    if not path.exists():
        return
    st = next((s for s in stations if site.lower() in s.id.lower()), None)
    if st is None:
        return
    z = np.load(path, allow_pickle=False)
    h = np.asarray(z["height_m"], float)
    gates = np.where((h >= 150.0) & (h <= 600.0))[0]
    if len(gates) == 0 or "t_s" not in z.files:
        return
    t = np.asarray(z["t_s"], float)
    for key, name, fn in (
        ("fall_speed_ms", "mrr_fall_speed_ms", np.abs),
        ("rain_rate_mmh", "mrr_rain_rate_mmh", None),
    ):
        if key in z.files:
            a = np.asarray(z[key], float)[:, gates]
            with np.errstate(all="ignore"):
                v = np.nanmedian(a if fn is None else fn(a), axis=1)
            st.series[name] = (t, v)


def load_bundle(bundle_dir) -> Bundle:
    d = Path(bundle_dir)
    manifest = _read(d / "manifest.json") or {}
    fl_raw = _read(d / "lma" / "flashes.json")
    ws, we = _window(manifest, fl_raw)
    radar = _read(d / "radar" / "overhead_samples.json")
    stations = parse_stations(_read(d / "stations" / "stations.json"))
    attach_hail_events(stations, _read(d / "stations" / "hail_events.json"))
    for site_file in (
        sorted((d / "profiles").glob("mrr_*.npz")) if (d / "profiles").exists() else []
    ):
        attach_mrr(stations, site_file, site_file.stem[len("mrr_") :], ws)
    for s in stations:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in s.id)
        for cand in (d / "stations" / f"{s.id}_dsd.npz", d / "stations" / f"{safe}_dsd.npz"):
            if cand.exists():
                s.dsd = load_dsd(cand)
                break
    return Bundle(
        window_start=ws,
        window_end=we,
        flashes=parse_flashes(fl_raw, ws),
        stations=stations,
        tornadoes=parse_tornadoes(_read(d / "tornadoes" / "tornadoes.json"), ws),
        events=parse_events(_read(d / "events.json"), ws),
        radar_samples=parse_radar_samples(radar, ws),
        radar_volumes=RadarVolumes.from_manifest(d, manifest, ws),
        manifest=manifest,
        path=str(d),
    )
