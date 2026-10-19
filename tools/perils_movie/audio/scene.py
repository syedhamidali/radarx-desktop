"""The scene at control rate: where the listener is, what the nearest observations say at that
moment, and where the tornado / hail evidence is. Everything here is data; no sound yet.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .bundle import Bundle, Event
from .dsd import SENSOR_AREA_M2
from .geo import enu, slant_distance
from .timeline import CameraPath, Timeline

CTL_RATE = 100  # Hz
# a station further than this from the listener is not heard for that variable (rain is local, wind and
# pressure are broader); stations beyond are simply not data for the listener
STATION_REACH_M = {
    "rain_rate_mmh": 30_000.0,
    "dm_mm": 30_000.0,
    "mrr_dm_mm": 3_000.0,
    "mrr_fall_speed_ms": 3_000.0,
    "mrr_rain_rate_mmh": 3_000.0,
    "dbz": 30_000.0,
}
DEFAULT_REACH_M = 40_000.0
TORNADO_REACH_M = 25_000.0  # the roar is silent beyond this (layers.tornado_gain)
# drop classes of the rain sound (mm): every spectrum bin goes to the nearest class (in log D)
DROP_CLASS_MM = np.array([0.4, 0.8, 1.2, 1.8, 2.5, 3.5, 5.0, 7.0])


def _class_of(d_mm) -> np.ndarray:
    """Class index of each bin diameter; bins above 8 mm (hail-sized) are -1: not rain."""
    d = np.asarray(d_mm, float)
    c = np.abs(np.log(d[:, None]) - np.log(DROP_CLASS_MM)[None, :]).argmin(axis=1)
    return np.where(d > 8.0, -1, c)


def _merge_hail_rows(rows: list[dict], gap_s: float = 3.0) -> list[dict]:
    """Merge evidence items of the same station whose clip spans touch (10 s samples): counts add, the
    maximum diameter is kept, the flux is the largest."""
    out: list[dict] = []
    for r in sorted(rows, key=lambda r: (str(r["station_id"]), r["clip_s"][0])):
        last = out[-1] if out else None
        if (
            last
            and last["station_id"] == r["station_id"]
            and last["evidence"] == r["evidence"]
            and r["clip_s"][0] - last["clip_s"][1] <= gap_s
        ):
            last["clip_s"][1] = max(last["clip_s"][1], r["clip_s"][1])
            if last.get("event_data_s") and r.get("event_data_s"):
                last["event_data_s"] = [
                    min(last["event_data_s"][0], r["event_data_s"][0]),
                    max(last["event_data_s"][1], r["event_data_s"][1]),
                ]
            last["max_diameter_mm"] = max(last["max_diameter_mm"], r["max_diameter_mm"])
            last["large_particle_flux_m2s"] = max(
                last["large_particle_flux_m2s"], r["large_particle_flux_m2s"]
            )
            if r["counts"] is not None:
                last["counts"] = (last["counts"] or 0) + r["counts"]
            last["n_samples"] = last.get("n_samples", 1) + 1
            last["min_listener_distance_m"] = min(
                last["min_listener_distance_m"], r["min_listener_distance_m"]
            )
        else:
            out.append({**r, "n_samples": 1, "clip_s": list(r["clip_s"])})
    return out


def _first(d: dict, keys):
    for k in keys:
        if d.get(k) is not None:
            return float(d[k])
    return None


def _dsd_tail(dsd, t0: float, t1: float, d_min: float = 6.0):
    """(mean flux of particles >= d_min mm per m2 per s, largest occupied bin) over [t0, t1]."""
    m = (dsd.t >= t0 - 30) & (dsd.t <= t1 + 30)
    big = d_min <= dsd.D
    if not m.any() or not big.any():
        return None, None
    f = dsd.flux(dsd.N[m])[:, big].sum(axis=1)
    occ = (dsd.N[m][:, big] > 0).any(axis=0)
    return float(f.mean()), (float(dsd.D[big][occ].max()) if occ.any() else None)


def smooth_edges(mask: np.ndarray, ramp: int) -> np.ndarray:
    """0/1 mask -> 0..1 with raised-cosine edges of ``ramp`` control steps (no clicks)."""
    m = mask.astype(float)
    if ramp < 2:
        return m
    k = np.hanning(2 * ramp + 1)
    k /= k.sum()
    return np.clip(
        np.convolve(m, k, mode="full")[ramp : ramp + len(m)], 0.0, 1.0
    )  # ("same" is longer than m for a short m)


@dataclass
class Scene:
    bundle: Bundle
    timeline: Timeline
    camera: CameraPath
    duration: float
    events: list[Event]
    ground_alt_m: float = 0.0
    station_decay_m: float = 2000.0  # nearest-station dominance: weight exp(-(d - dmin) / this)
    n: int = 0

    def __post_init__(self):
        self.n = int(np.ceil(self.duration * CTL_RATE)) + 1
        self.tc = np.arange(self.n) / CTL_RATE
        self.tw = self.timeline.clip_to_data(self.tc)
        c = self.camera.at(self.tc)
        self.lat, self.lon, self.alt, self.heading = (
            c["lat"],
            c["lon"],
            c["alt_m"],
            c["heading_deg"],
        )
        st = self.bundle.stations
        self.dist = (
            np.array([self._station_distance(s) for s in st]) if st else np.zeros((0, self.n))
        )
        if st and self.ground_alt_m == 0.0:
            self.ground_alt_m = float(np.median([s.alt_m for s in st]))
        self._cache: dict = {}

    def _station_distance(self, s) -> np.ndarray:
        """Slant distance (m) listener -> station per control step; a moving station (a vehicle with a
        lat / lon series) is followed, a fixed one uses its position."""
        la, lo = np.full(self.n, s.lat), np.full(self.n, s.lon)
        if "lat" in s.series and "lon" in s.series:
            a, b = s.at("lat", self.tw), s.at("lon", self.tw)
            la, lo = np.where(np.isfinite(a), a, la), np.where(np.isfinite(b), b, lo)
        return slant_distance(self.lat, self.lon, self.alt, la, lo, s.alt_m)

    def radar_at(self):
        """The radar volumes sampled at the listener (conv, hail, mesh_mm, ok), or None."""
        if "radar" not in self._cache:
            rv = self.bundle.radar_volumes
            self._cache["radar"] = None if rv is None else rv.sample(self.tw, self.lat, self.lon)
        return self._cache["radar"]

    # -- station blending ---------------------------------------------------------------------
    def station_weights(self, var: str):
        """(weights (S, n), values (S, n)) for a variable: nearest valid station dominates,
        near ties crossfade smoothly. Weights are zero where a station has no data."""
        key = ("w", var)
        if key in self._cache:
            return self._cache[key]
        st = self.bundle.stations
        vals = np.array([s.at(var, self.tw) for s in st]) if st else np.zeros((0, self.n))
        valid = np.isfinite(vals) & (self.dist <= STATION_REACH_M.get(var, DEFAULT_REACH_M))
        d = np.where(valid, self.dist, np.inf)
        dmin = d.min(axis=0) if len(st) else np.full(self.n, np.inf)
        with np.errstate(invalid="ignore"):
            w = np.where(valid, np.exp(-(self.dist - dmin) / self.station_decay_m), 0.0)
        w = np.where(np.isfinite(dmin), w, 0.0)
        self._cache[key] = (w, vals)
        return w, vals

    def blend(self, var: str):
        """(value, distance_m, covered) of a variable at the listener: weighted mean of the
        stations (nearest dominates). value is 0 and covered False where no station has data."""
        w, vals = self.station_weights(var)
        sw = w.sum(axis=0)
        ok = sw > 0
        v = np.where(ok, (w * np.nan_to_num(vals)).sum(axis=0) / np.where(ok, sw, 1), 0.0)
        d = (
            np.where(ok, (w * self.dist).sum(axis=0) / np.where(ok, sw, 1), np.inf)
            if len(w)
            else np.full(self.n, np.inf)
        )
        return v, d, ok

    def blend_wind_vector(self):
        """Wind from direction (deg, meteorological) as the weighted mean vector, plus covered."""
        w, _ = self.station_weights("wind_dir_deg")
        st = self.bundle.stations
        uu = (
            np.array([s.at_circular_deg("wind_dir_deg", self.tw)[0] for s in st])
            if st
            else np.zeros((0, self.n))
        )
        vv = (
            np.array([s.at_circular_deg("wind_dir_deg", self.tw)[1] for s in st])
            if st
            else np.zeros((0, self.n))
        )
        sw = np.maximum(w.sum(axis=0), 1e-12)
        u = (w * np.nan_to_num(uu)).sum(axis=0) / sw
        v = (w * np.nan_to_num(vv)).sum(axis=0) / sw
        return np.degrees(np.arctan2(u, v)) % 360.0, w.sum(axis=0) > 0

    def air_state(self, t_clip: float) -> tuple[float, float]:
        """(temperature C, pressure hPa) at the listener at a clip time: nearest station with data,
        else a standard 15 C / 1000 hPa (used only for the sound absorption)."""
        i = min(self.n - 1, max(0, round(t_clip * CTL_RATE)))
        out = []
        for var, default in (("temperature_c", 15.0), ("pressure_hpa", 1000.0)):
            v, _, ok = self.blend(var)
            out.append(float(v[i]) if ok[i] else default)
        return out[0], out[1]

    # -- tornado ------------------------------------------------------------------------------
    def tornado(self):
        """(distance_m, intensity 0..1 existence, sources) of the nearest existing tornado at each
        control step. Existence = inside a surveyed/observed track in time (and inside the
        renderer's tornado event interval when it lists one). No evidence -> exists is all zero."""
        if "tor" in self._cache:
            return self._cache["tor"]
        dist = np.full(self.n, np.inf)
        exist = np.zeros(self.n)
        srcs: list[str] = []
        ivs = [
            (
                self._ev_clip(e, e.t_data_s, e.t_clip_s),
                self._ev_clip(e, e.t_end_data_s, e.t_end_clip_s),
            )
            for e in self.events
            if e.type == "tornado"
        ]
        ivs = [(a, b) for a, b in ivs if a is not None and b is not None and b > a]
        for tr in self.bundle.tornadoes:
            la, lo, ex = tr.position(self.tw)
            if ivs:
                inside = np.zeros(self.n, bool)
                for a, b in ivs:
                    inside |= (self.tc >= a) & (self.tc <= b)
                ex = ex & inside
            if not ex.any():
                continue
            d = slant_distance(self.lat, self.lon, self.alt, la, lo, self.ground_alt_m)
            d = np.where(ex, d, np.inf)
            if not (d < TORNADO_REACH_M).any():
                continue  # a tornado of the day that is never near the listener: no evidence for this clip
            closer = d < dist
            dist = np.where(closer, d, dist)
            exist = np.maximum(exist, ex.astype(float))
            srcs.append(f"track:{tr.id}")
        if not self.bundle.tornadoes:
            # no survey track: tornado events with a position and an interval
            for e in self.events:
                pos = e.props.get("positions") if e.type == "tornado" else None
                if e.type != "tornado" or (e.lat is None and not pos):
                    continue
                a = self._ev_clip(e, e.t_data_s, e.t_clip_s)
                b = self._ev_clip(e, e.t_end_data_s, e.t_end_clip_s)
                if a is None or b is None or b <= a:
                    continue
                ex = (self.tc >= a) & (self.tc <= b)
                if pos:  # the renderer lists the tornado position per clip second
                    pt = np.array([p["t_clip_s"] for p in pos], float)
                    la = np.interp(self.tc, pt, [p["lat"] for p in pos])
                    lo = np.interp(self.tc, pt, [p["lon"] for p in pos])
                else:
                    la, lo = e.lat, e.lon
                d = np.where(
                    ex,
                    slant_distance(self.lat, self.lon, self.alt, la, lo, self.ground_alt_m),
                    np.inf,
                )
                dist = np.minimum(dist, d)
                exist = np.maximum(exist, ex.astype(float))
                srcs.append("event")
        res = (dist, smooth_edges(exist > 0, int(4.0 * CTL_RATE)), sorted(set(srcs)))
        self._cache["tor"] = res
        return res

    def tornado_bearing(self) -> np.ndarray:
        """Compass bearing from the listener to the tornado (deg); 0 where none."""
        out = np.zeros(self.n)
        best = np.full(self.n, np.inf)
        for tr in self.bundle.tornadoes:
            la, lo, ex = tr.position(self.tw)
            e, n, _ = enu(self.lat, self.lon, self.alt, la, lo, self.ground_alt_m)
            d = np.where(ex, np.hypot(e, n), np.inf)
            b = np.degrees(np.arctan2(e, n)) % 360.0
            better = d < best
            out = np.where(better, b, out)
            best = np.where(better, d, best)
        return out

    # -- hail ---------------------------------------------------------------------------------
    def _ev_clip(self, e: Event, td, tclip):
        if tclip is not None:
            return float(tclip)
        if td is not None:
            return float(self.timeline.data_to_clip(td))
        return None

    def hail(self, reach_m: float = 5000.0):
        """Hail evidence at the listener per control step. Evidence = station hail events
        (``hail_candidate`` in stations.json: time span, max diameter, counts), hail events of
        events.json with a position, radar hail samples. Returns a dict with ``w`` (0..1 proximity
        weighted evidence), ``dmax_mm``, ``flux`` (large particles per m2 per s, from the event counts
        or the tail of the station's drop spectrum), ``n_events``, ``sources`` and ``rows`` (one per
        evidence item, for the report). Without any evidence ``w`` is zero everywhere."""
        if "hail" in self._cache:
            return self._cache["hail"]
        n = self.n
        w = np.zeros(n)
        best = np.zeros(n)
        dmax = np.zeros(n)
        flux = np.zeros(n)
        dm_acc = np.zeros(n)
        rr_acc = np.zeros(n)
        wsum = np.zeros(n)
        rows: list[dict] = []
        srcs: set[str] = set()
        n_items = 0

        def add(label, sid, name, mask, dist_m, dmx, flx, dm, rr, ev_span, assumed, counts=None):
            nonlocal w, best, dmax, flux, dm_acc, rr_acc, wsum, n_items
            n_items += 1
            dist_a = np.broadcast_to(np.asarray(dist_m, float), (n,))
            prox = np.clip(1.0 - dist_a / reach_m, 0.0, 1.0) ** 2
            ww = smooth_edges(mask, int(0.5 * CTL_RATE)) * prox
            if not (ww > 0).any():
                return
            w = np.maximum(w, ww)
            wsum += ww
            flux += ww * flx
            dm_acc += ww * dm
            rr_acc += ww * rr
            top = ww > best
            dmax = np.where(top, dmx, dmax)
            best = np.where(top, ww, best)
            act = ww > 0.02
            clip_t = self.tc[act]
            rows.append(
                {
                    "evidence": label,
                    "station_id": sid,
                    "station_name": name,
                    "event_data_s": ev_span,
                    "clip_s": [float(clip_t.min()), float(clip_t.max())],
                    "max_diameter_mm": float(dmx),
                    "counts": counts,
                    "large_particle_flux_m2s": float(np.max(np.asarray(flx)[act]))
                    if np.ndim(flx)
                    else float(flx),
                    "dm_mm": float(dm),
                    "rain_rate_mmh": float(rr),
                    "min_listener_distance_m": float(dist_a[act].min()),
                    "assumed": assumed,
                }
            )
            srcs.add(label)

        for s_i, s in enumerate(self.bundle.stations):
            for ev in s.events:
                if not str(ev.get("type", "")).lower().startswith("hail"):
                    continue
                t0 = float(ev.get("t_s", ev.get("t_start_s", np.nan)))
                t1 = float(ev.get("t_end_s", t0 + 60.0))
                if not np.isfinite(t0):
                    continue
                assumed = []
                mask = (self.tw >= t0) & (self.tw <= t1)
                dmx = _first(ev, ("dmax_mm", "max_diameter_mm", "d_max_mm", "max_d_mm"))
                tail_f = tail_dmax = None
                if s.dsd is not None:
                    tail_f, tail_dmax = _dsd_tail(s.dsd, t0, t1)
                if dmx is None:
                    dmx = tail_dmax
                if dmx is None:
                    dmx = 15.0
                    assumed.append("max_diameter_mm")
                counts = _first(ev, ("counts", "n_large", "n_particles", "n_large_particles"))
                dur = max(t1 - t0, 1.0)
                if counts is not None:
                    flx = float(counts) / (SENSOR_AREA_M2 * dur)
                elif tail_f is not None:
                    flx = tail_f
                else:
                    flx = 2.0
                    assumed.append("large_particle_flux")
                dm = float(ev.get("dm_mm", 8.0))
                rr = float(ev.get("rain_rate_mmh", 50.0))
                if "dm_mm" not in ev:
                    assumed.append("dm_mm")
                add(
                    "station_event",
                    s.id,
                    s.name,
                    mask,
                    self.dist[s_i],
                    dmx,
                    flx,
                    dm,
                    rr,
                    [t0, t1],
                    assumed,
                    counts,
                )
        for e in self.events:
            if e.type != "hail" or e.lat is None:
                continue
            if str(e.props.get("station")) in {s.id for s in self.bundle.stations}:
                continue  # the renderer's list repeats the station events, which are counted from the stations
            a = self._ev_clip(e, e.t_data_s, e.t_clip_s)
            b = self._ev_clip(e, e.t_end_data_s, e.t_end_clip_s)
            if a is None:
                continue
            b = a + 4.0 if b is None else b
            d = slant_distance(self.lat, self.lon, self.alt, e.lat, e.lon, self.ground_alt_m)
            counts = _first(e.props, ("counts", "n_large"))
            dmx = _first(e.props, ("dmax_mm", "max_diameter_mm"))
            assumed = [
                k
                for k, v in (("max_diameter_mm", dmx), ("large_particle_flux", counts))
                if v is None
            ]
            dur_data = max(b - a, 1.0) * float(self.timeline.speedup_at(a))
            flx = float(counts) / (SENSOR_AREA_M2 * dur_data) if counts else 2.0
            add(
                "events.json",
                None,
                None,
                (self.tc >= a) & (self.tc <= b),
                d,
                float(dmx or 15.0),
                flx,
                float(e.props.get("dm_mm", 8.0)),
                float(e.props.get("rain_rate_mmh", 50.0)),
                [float(self.timeline.clip_to_data(a)), float(self.timeline.clip_to_data(b))],
                assumed,
                counts,
            )
        for s_i, s in enumerate(self.bundle.stations):
            hits = s.at("hail_intensity_hits_cm2h", self.tw)  # a piezo hail sensor: hits / cm2 / h
            m = np.isfinite(hits) & (hits > 0.5)
            if m.any():
                add(
                    "station_hail_sensor",
                    s.id,
                    s.name,
                    m,
                    self.dist[s_i],
                    10.0,
                    np.where(m, np.nan_to_num(hits) * 1e4 / 3600.0, 0.0),
                    8.0,
                    50.0,
                    [float(self.tw[m].min()), float(self.tw[m].max())],
                    ["max_diameter_mm (the sensor counts hits, not sizes)"],
                )
        ra = self.radar_at()
        if ra is not None:
            m = ra["ok"] & (ra["hail"] > 0.15)
            if m.any():
                dmx = np.where(ra["mesh_mm"] > 0, ra["mesh_mm"], 20.0)
                add(
                    "radar_hail_class",
                    None,
                    "radar volumes (hydrometeor class)",
                    m,
                    np.zeros(n),
                    float(dmx[m].max()),
                    2.0,
                    8.0,
                    50.0,
                    [float(self.tw[m].min()), float(self.tw[m].max())],
                    ["large_particle_flux", "dm_mm"]
                    + ([] if (ra["mesh_mm"][m] > 0).any() else ["max_diameter_mm"]),
                )
        rs = self.bundle.radar_samples
        if rs is not None:
            h = self._radar_lookup(rs, "hail")
            m = h > 0.5
            if m.any():
                add(
                    "radar_hail_class",
                    None,
                    "radar volume",
                    m,
                    np.zeros(n),
                    20.0,
                    2.0,
                    8.0,
                    50.0,
                    None,
                    ["max_diameter_mm", "large_particle_flux", "dm_mm"],
                )
        rows = _merge_hail_rows(rows)
        res = {
            "w": w,
            "dmax_mm": dmax,
            "flux": flux,
            "dm_mm": np.where(w > 0, dm_acc / np.maximum(wsum, 1e-9), 0.0),
            "rr_mmh": np.where(w > 0, rr_acc / np.maximum(wsum, 1e-9), 0.0),
            "n_events": n_items,
            "sources": sorted(srcs),
            "rows": rows,
        }
        self._cache["hail"] = res
        return res

    # -- drop-size distributions -------------------------------------------------------------
    def dsd_state(self):
        """Rain from the real drop spectra: per control step the number flux per drop class
        (``flux`` (C, n), drops per m2 per s), the total ``F``, the DSD ``rr`` (mm/h) and ``dm``
        (mm), the distance and the weights of the nearest stations that have a spectrum, or None when
        no station has one."""
        if "dsd" in self._cache:
            return self._cache["dsd"]
        st = [(i, s) for i, s in enumerate(self.bundle.stations) if s.dsd is not None]
        if not st:
            self._cache["dsd"] = None
            return None
        nc = len(DROP_CLASS_MM)
        fl = np.zeros((len(st), nc, self.n))
        rr = np.zeros((len(st), self.n))
        dm = np.zeros((len(st), self.n))
        valid = np.zeros((len(st), self.n), bool)
        for j, (si, s) in enumerate(st):
            d = s.dsd
            N = d.at(self.tw)
            valid[j] = np.isfinite(N).all(axis=1) & (
                self.dist[si] <= STATION_REACH_M["rain_rate_mmh"]
            )
            N = np.nan_to_num(N)
            f = d.flux(N)  # (n, nd)
            cls = _class_of(d.D)
            for c in range(nc):
                fl[j, c] = f[:, cls == c].sum(axis=1)
            rr[j] = d.rain_rate_mmh(N)
            dm[j] = d.dm_mm(N)
        dist = np.array([self.dist[i] for i, _ in st])
        w = self._weights(valid, dist)
        sw = np.maximum(w.sum(axis=0), 1e-12)
        res = {
            "ok": w.sum(axis=0) > 0,
            "flux": (w[:, None, :] * fl).sum(axis=0) / sw,
            "rr": (w * rr).sum(axis=0) / sw,
            "dm": (w * dm).sum(axis=0) / sw,
            "dist": (w * dist).sum(axis=0) / sw,
            "weights": w,
            "stations": [s for _, s in st],
        }
        res["F"] = res["flux"].sum(axis=0)
        self._cache["dsd"] = res
        return res

    def _weights(self, valid: np.ndarray, dist: np.ndarray) -> np.ndarray:
        d = np.where(valid, dist, np.inf)
        dmin = d.min(axis=0)
        with np.errstate(invalid="ignore"):
            w = np.where(valid, np.exp(-(dist - dmin) / self.station_decay_m), 0.0)
        return np.where(np.isfinite(dmin), w, 0.0)

    def runs(self, mask: np.ndarray, label: str, min_s: float = 0.25) -> list[dict]:
        """Spans (clip and data seconds) where a mask holds, for the provenance of non-station data."""
        out, i = [], 0
        while i < self.n:
            if not mask[i]:
                i += 1
                continue
            j = i
            while j + 1 < self.n and mask[j + 1]:
                j += 1
            if (j - i + 1) / CTL_RATE >= min_s:
                out.append(
                    {
                        "source": label,
                        "clip_s": [float(self.tc[i]), float(self.tc[j])],
                        "data_s": [float(self.tw[i]), float(self.tw[j])],
                    }
                )
            i = j + 1
        return out

    # -- provenance ---------------------------------------------------------------------------
    def dominant_segments(self, w: np.ndarray, stations, variables, min_s: float = 0.25):
        """Runs of control steps during which one station has the largest weight: what drove a layer
        and when (clip and data time). ``w`` (S, n), ``stations`` the matching Station list."""
        if w.shape[0] == 0:
            return []
        ok = w.sum(axis=0) > 0
        dom = np.where(ok, w.argmax(axis=0), -1)
        out = []
        i = 0
        while i < self.n:
            j = i
            while j + 1 < self.n and dom[j + 1] == dom[i]:
                j += 1
            if dom[i] >= 0 and (j - i + 1) / CTL_RATE >= min_s:
                s = stations[dom[i]]
                k = self.bundle.stations.index(s)
                out.append(
                    {
                        "station_id": s.id,
                        "station_name": s.name,
                        "station_type": s.type,
                        "network": s.meta.get("network")
                        or s.meta.get("source")
                        or s.meta.get("instrument"),
                        "credit": s.meta.get("credit"),
                        "variables": list(variables),
                        "clip_s": [float(self.tc[i]), float(self.tc[j])],
                        "data_s": [float(self.tw[i]), float(self.tw[j])],
                        "mean_listener_distance_m": float(self.dist[k, i : j + 1].mean()),
                    }
                )
            i = j + 1
        return out

    # -- surface wind / pressure events ---------------------------------------------------------
    def pressure_jump(
        self, window_s: float = 120.0, thresh_hpa: float = 0.8, full_hpa: float = 2.3
    ):
        """Gust-front pressure jump at the listener: (index 0..1, events). Per station the rise of
        its OWN pressure series over ``window_s`` of data time (so a calibration offset between
        stations is never a jump); only rises above ``thresh_hpa`` count. Blended like the wind."""
        if "pjump" in self._cache:
            return self._cache["pjump"]
        st = self.bundle.stations
        w, _ = self.station_weights("pressure_hpa")
        rows, events = [], []
        for s in st:
            dp = s.at("pressure_hpa", self.tw + window_s / 2) - s.at(
                "pressure_hpa", self.tw - window_s / 2
            )
            j = np.clip((dp - thresh_hpa) / (full_hpa - thresh_hpa), 0.0, 1.0)
            rows.append(np.nan_to_num(j))
            m = np.where(np.isfinite(dp), dp, -9.0)
            if m.max() >= thresh_hpa:
                k = int(m.argmax())
                events.append(
                    {
                        "station_id": s.id,
                        "data_s": float(self.tw[k]),
                        "clip_s": float(self.tc[k]),
                        "pressure_rise_hpa": float(dp[k]),
                        "window_s": window_s,
                    }
                )
        if not rows:
            res = (np.zeros(self.n), [])
        else:
            sw = np.maximum(w.sum(axis=0), 1e-12)
            res = (np.clip((w * np.array(rows)).sum(axis=0) / sw, 0.0, 1.0), events)
        self._cache["pjump"] = res
        return res

    def gust_fronts(self, window_s: float = 120.0, rise_ms: float = 5.0):
        """Sharp surface wind rises (>= rise_ms within window_s of data time) at any station."""
        out = []
        for s in self.bundle.stations:
            dv = s.at("wind_speed_ms", self.tw + window_s / 2) - s.at(
                "wind_speed_ms", self.tw - window_s / 2
            )
            m = np.where(np.isfinite(dv), dv, -9.0)
            if m.max() >= rise_ms:
                k = int(m.argmax())
                out.append(
                    {
                        "station_id": s.id,
                        "data_s": float(self.tw[k]),
                        "clip_s": float(self.tc[k]),
                        "wind_rise_ms": float(m[k]),
                        "window_s": window_s,
                    }
                )
        return out

    # -- convective core ----------------------------------------------------------------------
    def _radar_lookup(self, rs, key, max_km=8.0, max_dt=300.0):
        out = np.zeros(self.n)
        for i in range(0, self.n, 10):  # 10 Hz is plenty, held in between
            dt = np.abs(rs["t"] - self.tw[i])
            e, n, _ = enu(self.lat[i], self.lon[i], 0.0, rs["lat"], rs["lon"], 0.0)
            d = np.hypot(e, n)
            ok = (dt <= max_dt) & (d <= max_km * 1000)
            if ok.any():
                j = np.where(ok)[0][np.argmin(d[ok] / 1000.0 + dt[ok] / 60.0)]
                out[i : i + 10] = rs[key][j]
        return out

    def convective(self):
        """Convective-core index 0..1 at the listener: from the radar classification samples when the
        bundle has them, otherwise from the nearest station reflectivity (>= 40 dBZ ramps in, 50 full)
        and only while the rain rate is above 5 mm/h. Returns (index, source)."""
        if "conv" in self._cache:
            return self._cache["conv"]
        ra = self.radar_at()
        if ra is not None and ra["ok"].any():
            res = (
                np.where(ra["ok"], np.clip(ra["conv"], 0.0, 1.0), 0.0),
                "radar_volumes (Steiner-Houze-Yuter)",
            )
        elif self.bundle.radar_samples is not None:
            res = (
                np.clip(self._radar_lookup(self.bundle.radar_samples, "convective"), 0, 1),
                "radar_volume",
            )
        else:
            z, d, okz = self.blend("dbz")
            rr, _, okr = self.blend("rain_rate_mmh")
            idx = np.clip((z - 40.0) / 10.0, 0.0, 1.0) * (rr > 5.0) * okz * okr
            res = (
                idx * np.clip(1.0 - d / 8000.0, 0.0, 1.0) if len(self.bundle.stations) else idx,
                "station_dbz",
            )
        self._cache["conv"] = res
        return res
