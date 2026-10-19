"""The rotation of the tornado, from the data: what is observed, what is assumed, and the numbers the page animates.

The merged radar grid is 500 m and the MRMS rotation tracks are 1 km products: a tornado of a few hundred metres is
NOT resolved by them, only the mesocyclone-scale couplet around it is. So:

* OBSERVED / RETRIEVED: the rotation tracks near the tornado give the radar rotational velocity ``Vrot`` and the
  azimuthal shear ``AS`` at the 1 km scale: for a solid-body mesocyclone the angular rate is ``AS`` and its radius
  ``Vrot / AS`` (cyclonic when ``AS > 0``).
* OBSERVED (survey): the path width (radius of the funnel and the debris ring = half the width) and the rating, whose
  EF range of damage-based wind estimates (mph) is the only measure of the tornado-scale wind speed the data have.
* ASSUMED, STYLISED: the tangential speed of the funnel at the radius is the middle of the rating's wind range (a
  damage-based estimate, not a measurement); the angular rate is ``omega = Vt / r`` and the period ``2 pi r / Vt``; the sense is
  cyclonic (counter-clockwise seen from above), the sign of the observed azimuthal shear.

The same formulas live in ``src/lib/perils/perilsVortex.ts`` (the page animates with them) and are tested to agree.
"""

from __future__ import annotations

import math

MPH = 0.44704  # m/s per mph

# damage-based wind estimate ranges of the EF scale (mph, 3-s gust)
EF_RANGE_MPH = {
    "EF0": (65.0, 85.0),
    "EF1": (86.0, 110.0),
    "EF2": (111.0, 135.0),
    "EF3": (136.0, 165.0),
    "EF4": (166.0, 200.0),
    "EF5": (201.0, 250.0),
}


def ef_range_ms(rating: str | None) -> tuple[float, float] | None:
    r = EF_RANGE_MPH.get((rating or "").upper().replace("-", ""))
    return None if r is None else (r[0] * MPH, r[1] * MPH)


def rotation_near(
    tracks: list[dict], td: float, x: float, y: float, near_km: float = 6.0, window_s: float = 90.0
) -> dict | None:
    """The strongest rotation track point within ``near_km`` of (x, y) and ``window_s`` of ``td`` (or None)."""
    best = None
    for tr in tracks or []:
        pts = tr.get("points") or []
        cand = [q for q in pts if abs(q["t"] - td) <= window_s]
        if not cand:
            continue
        q = min(cand, key=lambda q: abs(q["t"] - td))
        if math.hypot(q["x"] - x, q["y"] - y) > near_km:
            continue
        v = q.get("vrot_ms")
        if v is None:
            continue
        if best is None or v > best["vrot_ms"]:
            best = {
                "vrot_ms": float(v),
                "as": q.get("as"),
                "x": q["x"],
                "y": q["y"],
                "radar": tr.get("radar"),
                "resolution_m": tr.get("resolution_m"),
            }
    return best


def vortex_params(tor: dict, tracks: list[dict], td: float, x: float, y: float) -> dict | None:
    """Rotation numbers of the tornado ``tor`` (a bundle tornado record) at data time ``td`` and position (x, y) km."""
    width = tor.get("width_m")
    radius = min(800.0, max(30.0, 0.5 * float(width))) if width else 75.0
    obs = rotation_near(tracks, td, x, y)
    rng = ef_range_ms(tor.get("rating"))
    if rng is not None:
        vt = 0.5 * (rng[0] + rng[1])
        basis = (
            f"middle of the {tor.get('rating')} damage-based wind range "
            f"({rng[0] / MPH:.0f}-{rng[1] / MPH:.0f} mph): an assumption, not a measurement"
        )
    elif obs is not None:
        vt = obs["vrot_ms"]
        basis = "radar Vrot of the nearest rotation track (1 km scale, a lower bound)"
    else:
        return None
    omega = vt / (radius)
    out = {
        "radius_m": radius,
        "vt_ms": vt,
        "omega_rad_s": omega,
        "period_s": 2.0 * math.pi * radius / vt,
        "sense": "cyclonic (counter-clockwise from above)",
        "vt_basis": basis,
        "radius_basis": "half the surveyed path width (an upper bound of the core radius)"
        if width
        else "assumed 75 m (the survey gives no width)",
        "vrot_radar_ms": None,
        "azshear_s1": None,
        "meso_radius_m": None,
        "meso_omega_rad_s": None,
        "meso_period_s": None,
    }
    if obs is not None:
        out["vrot_radar_ms"] = obs["vrot_ms"]
        a = obs.get("as")
        if a and a > 0:
            out["azshear_s1"] = float(a)
            out["meso_radius_m"] = obs["vrot_ms"] / float(a)
            out["meso_omega_rad_s"] = float(a)
            out["meso_period_s"] = 2.0 * math.pi / float(a)
    return out
