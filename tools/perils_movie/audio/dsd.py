"""Drop-size distributions (stations/<id>_dsd.npz) and the drop physics the rain / hail sound needs.

The npz layout of the data agent is documented in docs/PERILS_BUNDLE.md; the loader accepts the
variants below (keys are looked up case-insensitively):

  time        t_s | time_s | t                       data seconds since the window start, (nt,)
  diameter    d_mm | diameter_mm | diam_mm | D       bin centres in mm, (nd,)
  width       dD_mm | bin_width_mm                   optional, else from the centres
  fall speed  vt_ms | fall_speed_ms | v_ms | vt      optional, (nd,) or (nt, nd); else Atlas-Ulbrich
  spectrum    nd | nd_m3mm | n_d | concentration     N(D) in m^-3 mm^-1, (nt, nd)
              counts | raw_counts                    particle counts, (nt, nd) (or (nt, nd, nv)); converted
                                                     with the sensor area 0.0054 m2 (Parsivel) and the
                                                     sampling time: reported as ``from_counts``
"""

# ruff: noqa: N803, N815
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

SENSOR_AREA_M2 = 0.0054  # Parsivel2: 180 mm x 30 mm


def fall_speed_ms(d_mm):
    """Terminal fall speed of a raindrop (Atlas & Ulbrich 1977 fit, m/s); hail-sized particles are
    capped at 20 m/s (Gunn & Kinzer saturates near 9 m/s for rain, hail falls faster)."""
    d = np.asarray(d_mm, float)
    v = 9.65 - 10.3 * np.exp(-0.6 * d)
    return np.where(d > 5.5, np.minimum(9.0 + 1.1 * (d - 5.5), 20.0), np.maximum(v, 0.05))


def dm_from_fall_speed(v_ms):
    """Diameter (mm) of a drop that falls at v (inverse of the Atlas-Ulbrich fit)."""
    v = np.clip(np.asarray(v_ms, float), 0.3, 9.4)
    return -np.log((9.65 - v) / 10.3) / 0.6


def _pick(d: dict, names):
    low = {k.lower(): k for k in d}
    for n in names:
        if n.lower() in low:
            return d[low[n.lower()]]
    return None


@dataclass
class Dsd:
    t: np.ndarray  # (nt,) data seconds
    D: np.ndarray  # (nd,) mm
    dD: np.ndarray  # (nd,) mm
    N: np.ndarray  # (nt, nd) m^-3 mm^-1
    vt: np.ndarray  # (nd,) m/s
    from_counts: bool = False
    source: str = ""

    def at(self, t_data, hold_s: float = 180.0) -> np.ndarray:
        """N(D) at times (n,) -> (n, nd), NaN outside the record (+- hold_s)."""
        t = np.asarray(t_data, float)
        out = np.empty((len(t), len(self.D)))
        for k in range(len(self.D)):
            out[:, k] = np.interp(t, self.t, self.N[:, k])
        bad = (t < self.t[0] - hold_s) | (t > self.t[-1] + hold_s)
        out[bad] = np.nan
        return out

    def flux(self, N: np.ndarray) -> np.ndarray:
        """Drops per m2 per s per bin: N v dD (N: (..., nd))."""
        return N * self.vt * self.dD

    def rain_rate_mmh(self, N: np.ndarray) -> np.ndarray:
        return 6e-4 * np.pi * (N * self.D**3 * self.vt * self.dD).sum(axis=-1)

    def dm_mm(self, N: np.ndarray) -> np.ndarray:
        num = (N * self.D**4 * self.dD).sum(axis=-1)
        den = (N * self.D**3 * self.dD).sum(axis=-1)
        return np.where(den > 0, num / np.maximum(den, 1e-30), 0.0)


def load_dsd(path) -> Dsd | None:
    p = Path(path)
    if not p.exists():
        return None
    z = np.load(p, allow_pickle=False)
    d = {k: z[k] for k in z.files}
    t = _pick(d, ("t_s", "time_s", "t", "time"))
    D = _pick(d, ("d_mm", "diameter_mm", "diam_mm", "D", "diameter", "bin_centers_mm"))
    if t is None or D is None:
        return None
    t = np.asarray(t, float).ravel()
    D = np.asarray(D, float).ravel()
    dD = _pick(d, ("dD_mm", "bin_width_mm", "dD", "width_mm"))
    lo, hi = _pick(d, ("diameter_min_mm",)), _pick(d, ("diameter_max_mm",))
    if dD is None and lo is not None and hi is not None:
        dD = np.asarray(hi, float) - np.asarray(lo, float)
    if dD is None:
        edges = np.concatenate([[D[0] - (D[1] - D[0]) / 2], (D[1:] + D[:-1]) / 2, [D[-1] + (D[-1] - D[-2]) / 2]])
        dD = np.diff(edges)
    dD = np.asarray(dD, float).ravel()
    vt = _pick(d, ("vt_ms", "fall_speed_ms", "v_ms", "vt", "fall_speed", "fallspeed_ms"))
    vt = None if vt is None else np.asarray(vt, float)
    if vt is not None and vt.ndim == 2 and vt.shape[-1] == len(D):
        vt = np.nanmean(vt, axis=0)
    if vt is None or vt.shape != D.shape:  # e.g. the fall-speed AXIS of a size-velocity matrix
        vt = fall_speed_ms(D)
    N = _pick(d, ("nd", "nd_m3mm", "n_d", "concentration", "N_D", "dsd"))
    from_counts = False
    if N is None:
        c = _pick(d, ("counts", "raw_counts", "n_particles"))
        if c is None:
            return None
        c = np.asarray(c, float)
        if c.ndim == 3:
            c = c.sum(axis=2)
        dt = float(np.median(np.diff(t))) if len(t) > 1 else 60.0
        N = c / (SENSOR_AREA_M2 * dt * np.maximum(vt, 0.05) * np.maximum(dD, 1e-6))
        from_counts = True
    N = np.asarray(N, float)
    if N.shape != (len(t), len(D)):
        if N.shape == (len(D), len(t)):
            N = N.T
        else:
            return None
    return Dsd(t, D, dD, N, vt, from_counts, p.name)
