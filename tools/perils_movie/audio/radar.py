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
        return cls(Path(root), (float(g["origin_lat"]), float(g["origin_lon"])), np.array(t), files,
                   np.array(g["x_km"], float), np.array(g["y_km"], float))

    def _step_maps(self, k: int):
        if k not in self._maps:
            with np.load(self.files[k]) as z:
                conv = (z["conv_strat"] == 2).astype(np.float32) if "conv_strat" in z.files else None
                hid = z["hid"][:LOW_LEVELS] if "hid" in z.files else None
                mesh = np.nan_to_num(z["mesh_mm"].astype(np.float32)) if "mesh_mm" in z.files else None
            hail = None
            if hid is not None:
                hail = ndimage.uniform_filter((hid == HAIL_CODE).any(axis=0).astype(np.float32), 5)
            self._maps[k] = {
                "conv": None if conv is None else ndimage.uniform_filter(conv, 5),  # +-1 km
                "hail": hail,
                "mesh": None if mesh is None else ndimage.maximum_filter(mesh, 5),
            }
        return self._maps[k]

    def sample(self, t_data, lat, lon, max_dt_s: float = 90.0) -> dict[str, np.ndarray]:
        """conv (0..1), hail (0..1 fraction of the +-1 km box with the hail class), mesh_mm and
        ``ok`` (a volume within ``max_dt_s`` and the point inside the grid) at the listener."""
        t_data = np.asarray(t_data, float)
        n = len(t_data)
        out = {"conv": np.zeros(n), "hail": np.zeros(n), "mesh_mm": np.zeros(n), "ok": np.zeros(n, bool),
               "step": np.full(n, -1)}
        x, y = aeqd_km(*self.origin, lat, lon)
        ix = np.round((x - self.x_km[0]) / (self.x_km[1] - self.x_km[0])).astype(int)
        iy = np.round((y - self.y_km[0]) / (self.y_km[1] - self.y_km[0])).astype(int)
        inside = (ix >= 0) & (ix < len(self.x_km)) & (iy >= 0) & (iy < len(self.y_km))
        kk = np.abs(t_data[:, None] - self.t[None, :]).argmin(axis=1)
        near = np.abs(t_data - self.t[kk]) <= max_dt_s
        ok = inside & near
        for k in np.unique(kk[ok]):
            m = ok & (kk == k)
            maps = self._step_maps(int(k))
            for key, name in (("conv", "conv"), ("hail", "hail"), ("mesh", "mesh_mm")):
                if maps[key] is not None:
                    out[name][m] = maps[key][iy[m], ix[m]]
            out["step"][m] = k
        out["ok"] = ok
        return out
