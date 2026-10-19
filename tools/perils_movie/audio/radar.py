"""The radar volumes of the bundle (volumes/step_NNNN.npz) as a sampler at the listener's position:
the convective / stratiform class of the Steiner-Houze-Yuter separation and the hail class of the
hydrometeor classification overhead. Only 2-D maps and the lowest layers of the class volume are read.

Grid (manifest): azimuthal equidistant on a sphere of radius 6370997 m about ``grid.origin_lat/lon``,
500 m cells (x east, y north, from -90 to 90 km); z above mean sea level every 250 m. Hail class code
10 ("HA": hail; at S band a rain-hail mixture); X band has no hail class.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy import ndimage

from .timeline import parse_iso

R_AEQD_M = 6_370_997.0
HAIL_CODE = 10
LOW_LEVELS = 7  # z index 0..6: the lowest 1.5 km above sea level (ground is ~100 m here)


def aeqd_km(lat0, lon0, lat, lon):
    """(x east, y north) in km of lat/lon on the azimuthal equidistant plane about (lat0, lon0)."""
    p1, l1 = np.radians(lat0), np.radians(lon0)
    p, lam = np.radians(np.asarray(lat, float)), np.radians(np.asarray(lon, float))
    cosc = np.clip(np.sin(p1) * np.sin(p) + np.cos(p1) * np.cos(p) * np.cos(lam - l1), -1.0, 1.0)
    c = np.arccos(cosc)
    k = np.where(c < 1e-9, 1.0, c / np.maximum(np.sin(c), 1e-12))
    x = R_AEQD_M * k * np.cos(p) * np.sin(lam - l1)
    y = R_AEQD_M * k * (np.cos(p1) * np.sin(p) - np.sin(p1) * np.cos(p) * np.cos(lam - l1))
    return x / 1000.0, y / 1000.0


@dataclass
class RadarVolumes:
    root: Path
    origin: tuple[float, float]
    t: np.ndarray  # step times, data seconds since the window start
    files: list[str]
    x_km: np.ndarray
    y_km: np.ndarray
    _maps: dict = field(default_factory=dict)

    @classmethod
    def from_manifest(cls, root, manifest: dict, window_start: str) -> RadarVolumes | None:
        g, steps = manifest.get("grid"), manifest.get("steps")
        if not g or not steps:
            return None
        ws = parse_iso(window_start)
        t, files = [], []
        for s in steps:
            if s.get("volume") and s.get("time"):
                f = Path(root) / s["volume"]
                if f.exists():
                    t.append((parse_iso(s["time"]) - ws).total_seconds())
                    files.append(str(f))
        if not files:
            return None
        return cls(
            Path(root),
            (float(g["origin_lat"]), float(g["origin_lon"])),
            np.array(t),
            files,
            np.array(g["x_km"], float),
            np.array(g["y_km"], float),
        )

    def _step_maps(self, k: int):
        if k not in self._maps:
            with np.load(self.files[k]) as z:
                conv = (
                    (z["conv_strat"] == 2).astype(np.float32) if "conv_strat" in z.files else None
                )
                hid = z["hid"][:LOW_LEVELS] if "hid" in z.files else None
                mesh = (
                    np.nan_to_num(z["mesh_mm"].astype(np.float32)) if "mesh_mm" in z.files else None
                )
                dbz = z["dbz"][:3].astype(np.float32) if "dbz" in z.files else None
            hail = None
            if hid is not None:
                hail = ndimage.uniform_filter((hid == HAIL_CODE).any(axis=0).astype(np.float32), 5)
            rain = dbz_low = None
            if dbz is not None:
                # the lowest valid level of the lowest 750 m above sea level, mean of Z over +-1 km
                low = np.full(dbz.shape[1:], np.nan, np.float32)
                for lev in dbz[::-1]:
                    low = np.where(np.isfinite(lev), lev, low)
                zlin = np.where(np.isfinite(low), 10.0 ** (low / 10.0), 0.0)
                zm = ndimage.uniform_filter(zlin, 5)
                dbz_low = 10.0 * np.log10(np.maximum(zm, 1e-3))
                rain = zr_rain_rate(zm)
            self._maps[k] = {
                "conv": None if conv is None else ndimage.uniform_filter(conv, 5),  # +-1 km
                "hail": hail,
                "mesh": None if mesh is None else ndimage.maximum_filter(mesh, 5),
                "rain": rain,
                "dbz_low": dbz_low,
            }
        return self._maps[k]

    def sample(self, t_data, lat, lon, max_dt_s: float = 90.0) -> dict[str, np.ndarray]:
        """At the listener: ``conv`` (0..1), ``hail`` (0..1 fraction of the +-1 km box with the hail
        class), ``mesh_mm``, ``dbz_low`` and ``rain_mmh`` (Z-R estimate from the lowest echo) and ``ok`` (a
        volume within ``max_dt_s`` and the point inside the grid). conv, dbz and rain are interpolated
        linearly in time between the two volumes around the time; hail and mesh take the nearest."""
        t_data = np.asarray(t_data, float)
        n = len(t_data)
        out = {
            "conv": np.zeros(n),
            "hail": np.zeros(n),
            "mesh_mm": np.zeros(n),
            "dbz_low": np.full(n, -30.0),
            "rain_mmh": np.zeros(n),
            "ok": np.zeros(n, bool),
            "step": np.full(n, -1),
        }
        x, y = aeqd_km(*self.origin, lat, lon)
        ix = np.round((x - self.x_km[0]) / (self.x_km[1] - self.x_km[0])).astype(int)
        iy = np.round((y - self.y_km[0]) / (self.y_km[1] - self.y_km[0])).astype(int)
        inside = (ix >= 0) & (ix < len(self.x_km)) & (iy >= 0) & (iy < len(self.y_km))
        nk = len(self.t)
        k1 = np.clip(np.searchsorted(self.t, t_data), 0, nk - 1)
        k0 = np.clip(k1 - 1, 0, nk - 1)
        span = np.where(k1 > k0, self.t[k1] - self.t[k0], 1.0)
        w1 = np.where(k1 > k0, np.clip((t_data - self.t[k0]) / span, 0.0, 1.0), 0.0)
        near = (t_data >= self.t[0] - max_dt_s) & (t_data <= self.t[-1] + max_dt_s)
        ok = inside & near
        nearest = np.where(w1 >= 0.5, k1, k0)
        acc: dict[str, np.ndarray] = {}
        for k in np.unique(np.concatenate([k0[ok], k1[ok]])) if ok.any() else []:
            maps = self._step_maps(int(k))
            w = np.where(k0 == k, 1.0 - w1, 0.0) + np.where(k1 == k, w1, 0.0)
            w = np.where(k0 == k1, 1.0, w)  # a single bracketing step
            m = ok & (w > 0)
            for key, name in (("conv", "conv"), ("dbz_low", "dbz_low"), ("rain", "rain_mmh")):
                if maps[key] is not None:
                    v = np.zeros(n)
                    v[m] = maps[key][iy[m], ix[m]]
                    acc.setdefault(name, np.zeros(n))
                    acc[name] += w * v * m
            sel = ok & (nearest == k)
            for key, name in (("hail", "hail"), ("mesh", "mesh_mm")):
                if maps[key] is not None:
                    out[name][sel] = maps[key][iy[sel], ix[sel]]
            out["step"][sel] = k
        for name, v in acc.items():
            out[name] = np.where(ok, v, out[name])
        out["ok"] = ok
        return out


def zr_rain_rate(z_lin) -> np.ndarray:
    """Rain rate (mm/h) from linear reflectivity (mm6/m3) with the convective WSR-88D relation
    Z = 300 R^1.4, nothing below 12 dBZ, capped at 150 mm/h. A rough estimate: no attenuation, no
    hail contamination, no drop-size information."""
    z = np.asarray(z_lin, float)
    r = (np.maximum(z, 0.0) / 300.0) ** (1.0 / 1.4)
    return np.where(z >= 10.0**1.2, np.minimum(r, 150.0), 0.0).astype(np.float32)
