"""Choosing time steps and merging several radars' grids into ONE 3-D volume per step.

Method (documented in ``docs/PERILS_BUNDLE.md``)
------------------------------------------------
* A *step* is a time ``t`` (every ``step_s`` seconds over the window). Every radar contributes the
  volume whose mid-time (volume start + half the volume duration) is nearest to ``t`` if it is within
  ``tol_s`` (default 150 s); the signed offset ``volume mid-time - t`` is recorded per radar. A
  radar with no volume inside the tolerance does not contribute to that step.
* **Reflectivity** (``dbz``): a weighted mean **in linear Z** (mm6 m-3) of the radars that have a valid
  value in the voxel, weights ``w = band_weight / ROI^2`` (ROI = Py-ART's radius of influence in km,
  floored at 0.5 km: a smaller radius means a gate close to the voxel and a finer beam), converted
  back to dBZ. ``nradars`` counts them. There is no calibration or attenuation correction between
  radars (X band is down-weighted by 0.5, which is a choice and not a measurement).
* **Velocity** (``vel``), a *radial* velocity, cannot be averaged between radars that look from
  different directions: the voxel takes the value of the radar with the smallest ROI (ties: higher band
  weight) and ``vel_src`` says which (1-based index into the manifest's ``radars``; 0 = none).
* **Polarimetric** fields (``zdr``, ``rho``, ``kdp``, hydrometeor class ``hid``) come together from one
  radar, the best (smallest ROI) of those that have both ZDR and RHO there, recorded in ``pol_src``.
  Never averaged between bands (X, C and S differ in ZDR, RHO and KDP).
* NaN means no data. A voxel is NaN for every radar that did not see it (outside range, above the
  top beam, in the cone of silence, QC-rejected).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pgrid

# fields merged by "best radar" (same radar for all of the set)
POL_FIELDS = ("zdr", "rho", "kdp")


@dataclass
class VolumeRef:
    radar: str
    path: Path
    t_start: datetime
    t_end: datetime

    @property
    def t_mid(self) -> datetime:
        return self.t_start + (self.t_end - self.t_start) / 2


@dataclass
class StepPlan:
    index: int
    time: datetime
    volumes: dict[str, VolumeRef]
    offsets_s: dict[str, float]


def window_times(start: datetime, end: datetime, step_s: float) -> list[datetime]:
    """Times ``start, start + step, ... <= end`` (the end is included when it falls on a step)."""
    n = int(np.floor((end - start).total_seconds() / step_s + 1e-9)) + 1
    return [start + timedelta(seconds=step_s * k) for k in range(n)]


def plan_steps(
    index: dict[str, list[VolumeRef]],
    start: datetime,
    end: datetime,
    step_s: float = 60.0,
    tol_s: float = 150.0,
) -> list[StepPlan]:
    """The volume of every radar nearest in time to each step (within ``tol_s``), with the offsets."""
    plans = []
    for k, t in enumerate(window_times(start, end, step_s)):
        vols: dict[str, VolumeRef] = {}
        offs: dict[str, float] = {}
        for radar, refs in sorted(index.items()):
            best = min(refs, key=lambda v: abs((v.t_mid - t).total_seconds()), default=None)
            if best is None:
                continue
            off = (best.t_mid - t).total_seconds()
            if abs(off) <= tol_s:
                vols[radar] = best
                offs[radar] = round(off, 1)
        plans.append(StepPlan(k, t, vols, offs))
    return plans


def build_index(
    root: str | Path, radars: list[str], start: datetime, end: datetime, margin_s: float = 600.0
) -> dict[str, list[VolumeRef]]:
    """Volume references of each radar whose time lies near the window (attributes only are read)."""
    out: dict[str, list[VolumeRef]] = {}
    lo, hi = start - timedelta(seconds=margin_s), end + timedelta(seconds=margin_s)
    for radar in radars:
        refs = []
        for t_name, f in pgrid.list_grids(root, radar):
            if not (
                lo - timedelta(minutes=10) <= t_name <= hi
            ):  # name time is the start: cheap pre-filter
                continue
            try:
                t0, t1 = pgrid.grid_times(f)
            except Exception:
                continue
            if lo <= t0 + (t1 - t0) / 2 <= hi:
                refs.append(VolumeRef(radar, f, t0, t1))
        if refs:
            out[radar] = refs
    return out


# ---------------------------------------------------------------------------
# merging
# ---------------------------------------------------------------------------


def _best_better(
    roi_new: np.ndarray, w_new: float, roi_old: np.ndarray, w_old: np.ndarray
) -> np.ndarray:
    """True where the new radar is better than the stored one: smaller ROI, ties to the larger band weight."""
    with np.errstate(invalid="ignore"):
        return np.isfinite(roi_new) & (
            ~np.isfinite(roi_old) | (roi_new < roi_old) | ((roi_new == roi_old) & (w_new > w_old))
        )


class Merger:
    """Accumulates radars one at a time (memory: a few volume-sized arrays)."""

    def __init__(self, shape: tuple[int, int, int], radar_names: list[str]):
        self.shape = shape
        self.radars = list(radar_names)  # index + 1 is the *_src code
        self.sum_w = np.zeros(shape, np.float32)
        self.sum_wz = np.zeros(shape, np.float32)
        self.n = np.zeros(shape, np.uint8)
        self.vel = np.full(shape, np.nan, np.float32)
        self.vel_roi = np.full(shape, np.nan, np.float32)
        self.vel_w = np.zeros(shape, np.float32)
        self.vel_src = np.zeros(shape, np.uint8)
        self.pol = {k: np.full(shape, np.nan, np.float32) for k in POL_FIELDS}
        self.pol_roi = np.full(shape, np.nan, np.float32)
        self.pol_w = np.zeros(shape, np.float32)
        self.pol_src = np.zeros(shape, np.uint8)
        self.hid = np.zeros(shape, np.uint8)
        self.as_max = np.full(shape, np.nan, np.float32)
        self.as_src = np.zeros(shape, np.uint8)

    def add(
        self,
        radar: str,
        g: pgrid.RadarGrid,
        hid: np.ndarray | None = None,
        shear: np.ndarray | None = None,
    ) -> dict[str, int]:
        """Add one radar's grid (``g.fields``), its unified hydrometeor class and azimuthal shear."""
        code = self.radars.index(radar) + 1
        band = pgrid.RADAR_INFO.get(radar, {}).get("band", "C")
        bw = float(pgrid.BAND_WEIGHT.get(band, 1.0))
        f = g.fields
        roi = f.get("roi")
        if roi is None:
            roi = np.full(self.shape, 2000.0, np.float32)
        dbz = f["dbz"]
        ok = np.isfinite(dbz)
        roi_km = np.maximum(roi / 1000.0, 0.5)
        w = np.where(ok, bw / (roi_km**2), 0.0).astype(np.float32)
        z = np.where(ok, np.power(10.0, dbz / 10.0), 0.0).astype(np.float32)
        self.sum_w += w
        self.sum_wz += w * z
        self.n += ok.astype(np.uint8)
        used = {"dbz": int(ok.sum())}
        vel = f.get("vel")
        if vel is not None:
            m = np.isfinite(vel) & _best_better(
                np.where(np.isfinite(vel), roi, np.nan), bw, self.vel_roi, self.vel_w
            )
            self.vel[m] = vel[m]
            self.vel_roi[m] = roi[m]
            self.vel_w[m] = bw
            self.vel_src[m] = code
            used["vel"] = int(m.sum())
        zdr, rho = f.get("zdr"), f.get("rho")
        if zdr is not None and rho is not None:
            has = np.isfinite(zdr) & np.isfinite(rho)
            m = has & _best_better(np.where(has, roi, np.nan), bw, self.pol_roi, self.pol_w)
            self.pol["zdr"][m] = zdr[m]
            self.pol["rho"][m] = rho[m]
            kdp = f.get("kdp")
            self.pol["kdp"][m] = kdp[m] if kdp is not None else np.nan
            if hid is not None:
                self.hid[m] = hid[m]
            self.pol_roi[m] = roi[m]
            self.pol_w[m] = bw
            self.pol_src[m] = code
            used["pol"] = int(m.sum())
        if shear is not None:
            roi_ok = roi <= 2500.0
            s = np.where(roi_ok, shear, np.nan)
            better = np.isfinite(s) & (~np.isfinite(self.as_max) | (s > self.as_max))
            self.as_max[better] = s[better]
            self.as_src[better] = code
        return used

    def result(self, min_dbz: float | None = 0.0) -> dict[str, np.ndarray]:
        with np.errstate(invalid="ignore", divide="ignore"):
            z = self.sum_wz / self.sum_w
            dbz = np.where(self.sum_w > 0, 10.0 * np.log10(np.maximum(z, 1e-12)), np.nan).astype(
                np.float32
            )
        n = self.n.copy()
        if min_dbz is not None:
            low = np.isfinite(dbz) & (dbz < min_dbz)
            dbz[low] = np.nan
            n[low] = 0
        gone = ~np.isfinite(dbz)  # a voxel without echo keeps no velocity / dual-pol value either
        vel = self.vel.copy()
        out = {"dbz": dbz, "nradars": n}
        vsrc = self.vel_src.copy()
        vel[gone] = np.nan
        vsrc[gone] = 0
        out["vel"], out["vel_src"] = vel, vsrc
        psrc = self.pol_src.copy()
        psrc[gone] = 0
        for k in POL_FIELDS:
            a = self.pol[k].copy()
            a[gone] = np.nan
            out[k] = a
        out["pol_src"] = psrc
        h = self.hid.copy()
        h[gone | (psrc == 0)] = 0
        out["hid"] = h
        out["as_max"], out["as_src"] = self.as_max, self.as_src
        return out
