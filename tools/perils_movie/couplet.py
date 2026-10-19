"""Velocity couplet kinematics at the surveyed tornado: Vrot, couplet diameter, angular velocity, sense, and the verdict
whether the tornado-scale vortex is resolved by the data (RETRIEVED from radar velocity; the survey is OBSERVED).

Two kinds of evidence:

* **Gridded mobile and fixed radars** (``grid_couplet``): the radial velocity of one radar's 500 m grid is sampled along the
  azimuthal line through a vortex centre (bilinear, every 100 m, +-3 km); the largest outbound (``Vmax``) and
  inbound (``Vmin``) values and their separation ``d`` give ``dV = Vmax - Vmin``, the rotational velocity
  ``Vrot = dV / 2``, and for a solid-body core ``omega = dV / d`` (rad/s) and the period ``2 pi / omega``. The sense is
  *cyclonic* when the outbound extreme is on the clockwise side of the inbound one (as seen from the radar).
* **Polar data at native resolution** (``polar_local``): the KGWX CfRadial volumes of 01:05, 01:11 and 01:18 UTC (the only polar files
  of the window: the COW1 CfRadial files end at 23:20 UTC, the other mobile radars exist only as grids) give the native gates around
  the surveyed position, with the azimuthal sample spacing ``range x ray spacing``, the beam width and the beam height.

Resolution verdict: the vortex is *resolved* only if the surveyed tornado width is at least twice the effective
resolution, ``max(grid spacing, radius of influence, range x beam width)``. The beam width of the mobile radars is not in the grids:
a nominal 1.0 degree is assumed (stated in the output). A couplet that is wider than the tornado describes the parent
circulation (the low-level mesocyclone or shear zone), not the tornado.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
from scipy import ndimage

UTC = timezone.utc
NOMINAL_BEAM_WIDTH_DEG = 1.0


def _sample(
    field: np.ndarray, xs: np.ndarray, ys: np.ndarray, x_km: np.ndarray, y_km: np.ndarray
) -> np.ndarray:
    """Bilinear samples of ``field`` (ny, nx) at (xs, ys) in km; NaN where a neighbour is missing."""
    dx = float(x_km[1] - x_km[0])
    ci = (xs - x_km[0]) / dx
    cj = (ys - y_km[0]) / dx
    ok = np.isfinite(field)
    fz = np.where(ok, field, 0.0)
    v = ndimage.map_coordinates(fz, [cj, ci], order=1, mode="nearest")
    w = ndimage.map_coordinates(ok.astype(np.float64), [cj, ci], order=1, mode="nearest")
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(w > 0.99, v / np.maximum(w, 1e-9), np.nan)


def couplet_on_line(
    vel2d: np.ndarray,
    x_km: np.ndarray,
    y_km: np.ndarray,
    radar_xy_km: tuple[float, float],
    center_km: tuple[float, float],
    half_length_km: float = 3.0,
    step_km: float = 0.1,
    min_sep_km: float = 0.3,
) -> dict[str, Any] | None:
    """Couplet across the azimuthal line through ``center_km`` for one radar's level, or None."""
    X = center_km[0] - radar_xy_km[0]
    Y = center_km[1] - radar_xy_km[1]
    R = math.hypot(X, Y)
    if R < 1.0:
        return None
    ex, ey = Y / R, -X / R  # azimuthal (clockwise) unit vector
    s = np.arange(-half_length_km, half_length_km + 1e-9, step_km)
    v = _sample(vel2d, center_km[0] + s * ex, center_km[1] + s * ey, x_km, y_km)
    ok = np.isfinite(v)
    if ok.sum() < 20:
        return None
    # largest pair (min, max) separated by at least min_sep, over all pairs on the line
    idx = np.nonzero(ok)[0]
    best = None
    vv, ss = v[idx], s[idx]
    for a in range(len(idx)):
        diff = vv - vv[a]  # outbound minus inbound if a is the inbound (minimum) candidate
        sep = np.abs(ss - ss[a])
        m = sep >= min_sep_km
        if not m.any():
            continue
        j = int(np.argmax(np.where(m, diff, -np.inf)))
        if m[j] and (best is None or diff[j] > best[0]):
            best = (float(diff[j]), a, j)
    if best is None:
        return None
    dv, a, j = best
    d = float(abs(ss[j] - ss[a]))
    cyc = ss[j] > ss[a]  # outbound maximum on the clockwise side: cyclonic
    omega = dv / (d * 1000.0) * (1.0 if cyc else -1.0)
    return {
        "dV_ms": round(dv, 2),
        "vrot_ms": round(dv / 2.0, 2),
        "v_inbound_ms": round(float(vv[a]), 2),
        "v_outbound_ms": round(float(vv[j]), 2),
        "couplet_diameter_km": round(d, 2),
        "couplet_radius_km": round(d / 2.0, 2),
        "omega_s-1": round(omega, 5),
        "period_s": round(2.0 * math.pi / abs(omega), 1) if omega else None,
        "sense": "cyclonic" if cyc else "anticyclonic",
        "range_km": round(R, 2),
        "azimuth_deg": round((math.degrees(math.atan2(X, Y)) + 360.0) % 360.0, 1),
    }


def grid_couplet(
    vel: np.ndarray,
    roi: np.ndarray | None,
    x_km: np.ndarray,
    y_km: np.ndarray,
    z_km: np.ndarray,
    radar_xy_km: tuple[float, float],
    survey_xy_km: tuple[float, float],
    search_km: float = 3.0,
    z_max_km: float = 1.0,
    cell_km: float = 0.5,
    beam_width_deg: float = NOMINAL_BEAM_WIDTH_DEG,
) -> dict[str, Any] | None:
    """The strongest couplet near the surveyed position in the levels below ``z_max_km`` of one radar's grid.

    The centre is searched on the azimuthal-shear field within ``search_km`` of the survey (largest absolute shear),
    then the couplet is measured across it; also across the surveyed position itself (``at_survey``).
    """
    import rotation

    lev = np.nonzero(z_km <= z_max_km + 1e-9)[0]
    best = None
    for k in lev:
        v2 = vel[k]
        if not np.isfinite(v2).any():
            continue
        shear = rotation.azimuthal_shear(v2[None], x_km, y_km, radar_xy_km)[0]
        jj, ii = np.mgrid[0 : y_km.size, 0 : x_km.size]
        near = (x_km[ii] - survey_xy_km[0]) ** 2 + (y_km[jj] - survey_xy_km[1]) ** 2 <= search_km**2
        sh = np.where(near & np.isfinite(shear), shear, np.nan)
        if not np.isfinite(sh).any():
            continue
        p = int(np.nanargmax(np.abs(sh)))
        cj, ci = np.unravel_index(p, sh.shape)
        centre = (float(x_km[ci]), float(y_km[cj]))
        c1 = couplet_on_line(v2, x_km, y_km, radar_xy_km, centre)
        c0 = couplet_on_line(v2, x_km, y_km, radar_xy_km, survey_xy_km)
        if c1 is None:
            continue
        rec = {
            "level_km": float(z_km[k]),
            "centre_km": [round(centre[0], 2), round(centre[1], 2)],
            "centre_offset_from_survey_km": round(
                math.hypot(centre[0] - survey_xy_km[0], centre[1] - survey_xy_km[1]), 2
            ),
            "azimuthal_shear_s-1": round(float(sh[cj, ci]), 4),
            "couplet": c1,
            "couplet_across_surveyed_position": c0,
        }
        if best is None or c1["dV_ms"] > best["couplet"]["dV_ms"]:
            best = rec
    if best is None:
        return None
    rng_km = best["couplet"]["range_km"]
    k = int(np.argmin(np.abs(z_km - best["level_km"])))
    ci = round((best["centre_km"][0] - x_km[0]) / (x_km[1] - x_km[0]))
    cj = round((best["centre_km"][1] - y_km[0]) / (x_km[1] - x_km[0]))
    roi_m = float(roi[k, cj, ci]) if roi is not None and np.isfinite(roi[k, cj, ci]) else None
    beam_m = rng_km * 1000.0 * math.radians(beam_width_deg)
    eff = max(cell_km * 1000.0, roi_m or 0.0, beam_m)
    best["effective_resolution_m"] = round(eff, 0)
    best["resolution_terms_m"] = {
        "grid_spacing": cell_km * 1000.0,
        "radius_of_influence": roi_m,
        "beam_width_at_range": round(beam_m, 0),
    }
    return best


def resolved_verdict(
    width_m: float | None, effective_resolution_m: float, couplet_diameter_km: float | None
) -> dict[str, Any]:
    """Resolved / unresolved for the tornado width and what the measured couplet stands for."""
    out: dict[str, Any] = {"effective_resolution_m": effective_resolution_m}
    if width_m:
        out["tornado_width_m"] = width_m
        out["tornado_resolved"] = bool(width_m >= 2.0 * effective_resolution_m)
        out["ratio_width_to_resolution"] = round(width_m / effective_resolution_m, 2)
    else:
        out["tornado_resolved"] = False
    if couplet_diameter_km is not None:
        out["couplet_diameter_km"] = couplet_diameter_km
        out["couplet_resolved"] = bool(couplet_diameter_km * 1000.0 >= 2.0 * effective_resolution_m)
        out["couplet_is"] = (
            "the tornado-scale vortex"
            if width_m and couplet_diameter_km * 1000.0 <= 2.0 * width_m and out["tornado_resolved"]
            else "the parent circulation (low-level shear zone / mesocyclone), wider than the tornado"
        )
    out["verdict"] = (
        "tornado vortex RESOLVED"
        if out.get("tornado_resolved")
        else "tornado vortex UNRESOLVED: narrower than twice the effective resolution"
    )
    return out


# ---------------------------------------------------------------------------
# polar data at native resolution
# ---------------------------------------------------------------------------


def polar_local(
    path: str | Path,
    radar_latlon_alt: tuple[float, float, float],
    target_fn,
    proj,
    box_km: float = 4.0,
    max_elev_deg: float = 1.6,
    max_sweeps: int = 3,
    survey_width_m: float | None = None,
) -> dict[str, Any] | None:
    """Native gates of the lowest sweeps in a ``box_km`` box around the surveyed position, and the couplet evidence.

    ``target_fn(t_abs_s)`` returns the surveyed (lat, lon) at an absolute time (or None); ``proj`` is the grid projection
    (pyproj) used to give the gate positions in the grid frame (x, y in km). Gate heights use the 4/3 earth model.
    """
    import netCDF4
    import pyproj

    lat0, lon0, alt0 = radar_latlon_alt
    rp = pyproj.Proj(proj="aeqd", lat_0=lat0, lon_0=lon0, R=6370997.0)
    with netCDF4.Dataset(path) as ds:
        units = ds["time"].units
        base = datetime.fromisoformat(units.split("since")[1].strip().replace("Z", "+00:00"))
        base = base if base.tzinfo else base.replace(tzinfo=UTC)
        t = np.asarray(ds["time"][:], float)
        az = np.asarray(ds["azimuth"][:], float)
        el = np.asarray(ds["elevation"][:], float)
        rng = np.asarray(ds["range"][:], float)
        fixed = np.asarray(ds["fixed_angle"][:], float)
        s0 = np.asarray(ds["sweep_start_ray_index"][:], int)
        s1 = np.asarray(ds["sweep_end_ray_index"][:], int)
        bw = (
            float(np.ravel(np.asarray(ds["radar_beam_width_h"][:]))[0])
            if "radar_beam_width_h" in ds.variables
            else 1.0
        )
        nyq = (
            np.asarray(ds["nyquist_velocity"][:], float)
            if "nyquist_velocity" in ds.variables
            else None
        )
        sweeps = [i for i in np.argsort(fixed) if fixed[i] <= max_elev_deg][:max_sweeps]
        gate_m = float(np.median(np.diff(rng)))
        out_sweeps = []
        arrays: dict[str, list] = {
            k: []
            for k in (
                "x_km",
                "y_km",
                "z_m",
                "sweep",
                "azimuth_deg",
                "range_m",
                "vel",
                "ref",
                "rho",
                "zdr",
            )
        }
        for si in sweeps:
            r = slice(int(s0[si]), int(s1[si]) + 1)
            t_mid = base + timedelta(seconds=float(np.mean(t[r])))
            tgt = target_fn(t_mid.timestamp())
            if tgt is None:
                continue
            tx, ty = rp(tgt[1], tgt[0])  # metres east / north of the radar
            tx, ty = tx / 1000.0, ty / 1000.0
            R0 = math.hypot(tx, ty)
            az0 = (math.degrees(math.atan2(tx, ty)) + 360.0) % 360.0
            dazs = ((az[r] - az0 + 540.0) % 360.0) - 180.0
            half_az = math.degrees(math.atan2(box_km, R0)) * 1.1
            rays = np.nonzero(np.abs(dazs) <= half_az)[0] + r.start
            gates = np.nonzero(np.abs(rng - R0 * 1000.0) <= box_km * 1000.0)[0]
            if rays.size < 3 or gates.size < 3:
                continue

            def g(name, rays=rays, gates=gates):
                return (
                    np.ma.filled(np.ma.asarray(ds[name][rays][:, gates]).astype(float), np.nan)
                    if name in ds.variables
                    else np.full((rays.size, gates.size), np.nan)
                )

            vel, ref, rho, zdr = g("VEL"), g("REF"), g("RHO"), g("ZDR")
            A, G = np.meshgrid(np.radians(az[rays]), rng[gates], indexing="ij")
            e = np.radians(el[rays])[:, None]
            gx = G * np.cos(e) * np.sin(A) / 1000.0
            gy = G * np.cos(e) * np.cos(A) / 1000.0
            gz = G * np.sin(e) + G**2 / (2.0 * (4.0 / 3.0) * 6371000.0) + alt0
            inbox = (np.abs(gx - tx) <= box_km) & (np.abs(gy - ty) <= box_km)
            # position in the grid frame
            lon_g, lat_g = rp(gx * 1000.0, gy * 1000.0, inverse=True)
            xg, yg = proj(lon_g, lat_g)
            sel = inbox & np.isfinite(vel)
            ray_spacing_deg = float(np.median(np.abs(np.diff(np.sort(az[rays])))))
            az_spacing_m = R0 * 1000.0 * math.radians(ray_spacing_deg)
            rec: dict[str, Any] = {
                "sweep": int(si),
                "fixed_angle_deg": float(fixed[si]),
                "time": t_mid.isoformat().replace("+00:00", "Z"),
                "target_range_km": round(R0, 2),
                "target_azimuth_deg": round(az0, 2),
                "gate_spacing_m": round(gate_m, 1),
                "ray_spacing_deg": round(ray_spacing_deg, 3),
                "azimuthal_sample_spacing_m": round(az_spacing_m, 0),
                "beam_width_deg": bw,
                "beam_width_at_target_m": round(R0 * 1000.0 * math.radians(bw), 0),
                "beam_height_m_asl": round(float(np.nanmean(gz[inbox])), 0)
                if inbox.any()
                else None,
                "n_gates_in_box": int(inbox.sum()),
                "n_valid_velocity_gates": int(sel.sum()),
                "nyquist_ms": None if nyq is None else round(float(np.nanmedian(nyq[r])), 1),
            }
            if sel.sum() >= 6:
                vmin, vmax = float(np.nanmin(vel[sel])), float(np.nanmax(vel[sel]))
                rec["v_inbound_min_ms"], rec["v_outbound_max_ms"] = round(vmin, 1), round(vmax, 1)
                # largest velocity difference between neighbouring rays at the same gate (the gate-to-gate couplet)
                order = np.argsort(az[rays])
                vs = vel[order]
                dvv = np.abs(np.diff(vs, axis=0))
                ok2 = np.isfinite(dvv) & inbox[order][1:] & inbox[order][:-1]
                rec["max_dV_adjacent_rays_ms"] = (
                    round(float(np.nanmax(np.where(ok2, dvv, np.nan))), 1) if ok2.any() else None
                )
                if rec["max_dV_adjacent_rays_ms"] is not None:
                    rec["max_azimuthal_shear_s-1"] = round(
                        rec["max_dV_adjacent_rays_ms"] / az_spacing_m, 4
                    )
                iu = np.unravel_index(int(np.nanargmin(np.where(sel, vel, np.nan))), vel.shape)
                io = np.unravel_index(int(np.nanargmax(np.where(sel, vel, np.nan))), vel.shape)
                rec["separation_inbound_outbound_km"] = round(
                    float(math.hypot(xg[iu] - xg[io], yg[iu] - yg[io])) / 1000.0, 2
                )
                nq = rec["nyquist_ms"]
                if nq and rec.get("max_dV_adjacent_rays_ms") is not None:
                    rec["max_dV_near_nyquist"] = bool(rec["max_dV_adjacent_rays_ms"] >= 0.85 * nq)
                    if rec["max_dV_near_nyquist"]:
                        rec["max_dV_note"] = (
                            "the largest ray-to-ray difference is close to the Nyquist velocity: it may be a residual "
                            "dealiasing fold, not rotation"
                        )
            eff = max(az_spacing_m, gate_m)
            rec["effective_resolution_m"] = round(eff, 0)
            rec["resolution"] = resolved_verdict(survey_width_m, eff, None)
            out_sweeps.append(rec)
            m = inbox
            arrays["x_km"].append((xg[m] / 1000.0).astype(np.float32))
            arrays["y_km"].append((yg[m] / 1000.0).astype(np.float32))
            arrays["z_m"].append(gz[m].astype(np.float32))
            arrays["sweep"].append(np.full(int(m.sum()), si, np.int16))
            arrays["azimuth_deg"].append(np.degrees(A)[m].astype(np.float32))
            arrays["range_m"].append(G[m].astype(np.float32))
            for nm, val in (("vel", vel), ("ref", ref), ("rho", rho), ("zdr", zdr)):
                arrays[nm].append(val[m].astype(np.float32))
    if not out_sweeps:
        return None
    return {"sweeps": out_sweeps, "arrays": {k: np.concatenate(v) for k, v in arrays.items() if v}}
