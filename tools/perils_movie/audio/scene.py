"""The scene at control rate: where the listener is, what the nearest observations say at that
moment, and where the tornado / hail evidence is. Everything here is data; no sound yet.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .bundle import Bundle, Event
from .geo import enu, slant_distance
from .timeline import CameraPath, Timeline

CTL_RATE = 100  # Hz


def smooth_edges(mask: np.ndarray, ramp: int) -> np.ndarray:
    """0/1 mask -> 0..1 with raised-cosine edges of ``ramp`` control steps (no clicks)."""
    m = mask.astype(float)
    if ramp < 2:
        return m
    k = np.hanning(2 * ramp + 1)
    k /= k.sum()
    return np.clip(np.convolve(m, k, mode="same"), 0.0, 1.0)


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
        self.lat, self.lon, self.alt, self.heading = c["lat"], c["lon"], c["alt_m"], c["heading_deg"]
        st = self.bundle.stations
        self.dist = (
            np.array([slant_distance(self.lat, self.lon, self.alt, s.lat, s.lon, s.alt_m) for s in st])
            if st
            else np.zeros((0, self.n))
        )
        if st and self.ground_alt_m == 0.0:
            self.ground_alt_m = float(np.median([s.alt_m for s in st]))
        self._cache: dict = {}

    # -- station blending ---------------------------------------------------------------------
    def station_weights(self, var: str):
        """(weights (S, n), values (S, n)) for a variable: nearest valid station dominates,
        near ties crossfade smoothly. Weights are zero where a station has no data."""
        key = ("w", var)
        if key in self._cache:
            return self._cache[key]
        st = self.bundle.stations
        vals = np.array([s.at(var, self.tw) for s in st]) if st else np.zeros((0, self.n))
        valid = np.isfinite(vals)
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
        d = np.where(ok, (w * self.dist).sum(axis=0) / np.where(ok, sw, 1), np.inf) if len(w) else np.full(self.n, np.inf)
        return v, d, ok

    def blend_wind_vector(self):
        """Wind from direction (deg, meteorological) as the weighted mean vector, plus covered."""
        w, _ = self.station_weights("wind_dir_deg")
        st = self.bundle.stations
        uu = np.array([s.at_circular_deg("wind_dir_deg", self.tw)[0] for s in st]) if st else np.zeros((0, self.n))
        vv = np.array([s.at_circular_deg("wind_dir_deg", self.tw)[1] for s in st]) if st else np.zeros((0, self.n))
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
            (self._ev_clip(e, e.t_data_s, e.t_clip_s), self._ev_clip(e, e.t_end_data_s, e.t_end_clip_s))
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
            closer = d < dist
            dist = np.where(closer, d, dist)
            exist = np.maximum(exist, ex.astype(float))
            srcs.append(f"track:{tr.id}")
        if not self.bundle.tornadoes:
            # no survey track: tornado events with a position and an interval
            for e in self.events:
                if e.type != "tornado" or e.lat is None:
                    continue
                a = self._ev_clip(e, e.t_data_s, e.t_clip_s)
                b = self._ev_clip(e, e.t_end_data_s, e.t_end_clip_s)
                if a is None or b is None or b <= a:
                    continue
                ex = (self.tc >= a) & (self.tc <= b)
                d = np.where(ex, slant_distance(self.lat, self.lon, self.alt, e.lat, e.lon, self.ground_alt_m), np.inf)
                dist = np.minimum(dist, d)
                exist = np.maximum(exist, ex.astype(float))
                srcs.append("event")
        res = (dist, smooth_edges(exist > 0, int(1.5 * CTL_RATE)), sorted(set(srcs)))
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
        """Hail evidence at the listener per control step: dict with ``w`` (0..1 proximity-weighted
        evidence), ``dm_mm`` and ``rr_mmh`` (weighted), ``n_events`` and ``sources``. Evidence =
        station hail events (hail_candidate in stations.json), hail events of events.json with a
        position, radar hail samples. Without any of them, ``w`` is zero everywhere."""
        if "hail" in self._cache:
            return self._cache["hail"]
        w = np.zeros(self.n)
        dm_acc = np.zeros(self.n)
        rr_acc = np.zeros(self.n)
        wsum = np.zeros(self.n)
        n_ev = 0
        srcs: set[str] = set()

        def add(mask, dist_m, dm, rr, label):
            nonlocal w, dm_acc, rr_acc, wsum, n_ev
            prox = np.clip(1.0 - dist_m / reach_m, 0.0, 1.0) ** 2
            ww = smooth_edges(mask, int(0.5 * CTL_RATE)) * prox
            if not (ww > 0).any():
                return
            w = np.maximum(w, ww)
            wsum += ww
            dm_acc += ww * dm
            rr_acc += ww * rr
            n_ev += 1
            srcs.add(label)

        for s_i, s in enumerate(self.bundle.stations):
            for ev in s.events:
                if not str(ev.get("type", "")).lower().startswith("hail"):
                    continue
                t0 = float(ev.get("t_s", ev.get("t_start_s", np.nan)))
                t1 = float(ev.get("t_end_s", t0 + 60.0))
                if not np.isfinite(t0):
                    continue
                mask = (self.tw >= t0) & (self.tw <= t1)
                add(mask, self.dist[s_i], float(ev.get("dm_mm", 8.0)), float(ev.get("rain_rate_mmh", 50.0)),
                    "station_event")
        for e in self.events:
            if e.type != "hail" or e.lat is None:
                continue
            a = self._ev_clip(e, e.t_data_s, e.t_clip_s)
            b = self._ev_clip(e, e.t_end_data_s, e.t_end_clip_s)
            if a is None:
                continue
            b = a + 4.0 if b is None else b
            d = slant_distance(self.lat, self.lon, self.alt, e.lat, e.lon, self.ground_alt_m)
            add((self.tc >= a) & (self.tc <= b), d, float(e.props.get("dm_mm", 8.0)),
                float(e.props.get("rain_rate_mmh", 50.0)), "events.json")
        rs = self.bundle.radar_samples
        if rs is not None:
            h = self._radar_lookup(rs, "hail")
            m = h > 0.5
            if m.any():
                add(m, np.zeros(self.n), 8.0, 50.0, "radar_hail_class")
        res = {
            "w": w,
            "dm_mm": np.where(w > 0, dm_acc / np.maximum(wsum, 1e-9), 0.0) if n_ev else np.zeros(self.n),
            "rr_mmh": np.where(w > 0, rr_acc / np.maximum(wsum, 1e-9), 0.0) if n_ev else np.zeros(self.n),
            "n_events": n_ev,
            "sources": sorted(srcs),
        }
        self._cache["hail"] = res
        return res

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
        if self.bundle.radar_samples is not None:
            res = (np.clip(self._radar_lookup(self.bundle.radar_samples, "convective"), 0, 1), "radar_volume")
        else:
            z, d, okz = self.blend("dbz")
            rr, _, okr = self.blend("rain_rate_mmh")
            idx = np.clip((z - 40.0) / 10.0, 0.0, 1.0) * (rr > 5.0) * okz * okr
            res = (idx * np.clip(1.0 - d / 8000.0, 0.0, 1.0) if len(self.bundle.stations) else idx, "station_dbz")
        self._cache["conv"] = res
        return res
