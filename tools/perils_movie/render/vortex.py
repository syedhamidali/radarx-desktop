"""The rotation of the tornado, from the data: what is measured, what is estimated, and the numbers the page animates.

The merged radar grid is 500 m and the tornado is 366 m wide: NO radar resolves the tornado vortex (the data bundle
says so for every step: ``tornado_resolved = false``). So:

* RETRIEVED (radar, ``context/vortex_kinematics.json``): the couplet of the PARENT circulation around the surveyed
  position: Vrot 8-15 m/s (median 11), about 1.8 km across, cyclonic in most steps, one turn per about 8 minutes.
* OBSERVED (survey): the damage width (the funnel and the debris ring have half of it as radius) and the rating, whose
  damage-based wind range (EF2: 111-135 mph = 50-60 m/s) is the only measure of the tornado-scale wind the data have.
* ESTIMATE, STYLISED: the tangential speed of the funnel at its radius is the middle of that range; the angular rate
  is ``omega = Vt / r`` and the period ``2 pi r / Vt`` (19-23 s over the range); counter-clockwise seen from above
  (cyclonic, as the parent circulation is).

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


def parent_circulation(vortex: dict | None, td: float) -> dict | None:
    """The couplet of the best radar at the step nearest to ``td`` and the medians over the steps (or None)."""
    if not vortex or not vortex.get("steps"):
        return None
    st = min(vortex["steps"], key=lambda s: abs(s["t"] - td))
    sm = vortex.get("summary") or {}
    vr = sm.get("vrot_ms_min_median_max")
    dm = sm.get("couplet_diameter_km_min_median_max")
    return {
        "vrot_ms": st.get("vrot_ms"),
        "diameter_km": st.get("diameter_km"),
        "sense": st.get("sense"),
        "radar": st.get("radar"),
        "resolution_m": st.get("effective_resolution_m"),
        "median_vrot_ms": vr[1] if vr else None,
        "vrot_range_ms": [vr[0], vr[2]] if vr else None,
        "median_diameter_km": dm[1] if dm else None,
        "median_omega_s1": sm.get("omega_s1_median_cyclonic"),
        "cyclonic_steps": sm.get("cyclonic_steps"),
        "n_steps": sm.get("n_steps"),
    }


def vortex_params(tor: dict, vortex: dict | None, td: float) -> dict | None:
    """Rotation numbers of the tornado ``tor`` (a bundle tornado record) at data time ``td``."""
    est = (vortex or {}).get("survey_scale_estimate") or None
    width = (est or {}).get("damage_width_m") or tor.get("width_m")
    radius = min(800.0, max(30.0, 0.5 * float(width))) if width else 75.0
    rng = (
        tuple(est["wind_range_ms"])
        if est and est.get("wind_range_ms")
        else ef_range_ms(tor.get("rating"))
    )
    if rng is None:
        return None
    vt = 0.5 * (rng[0] + rng[1])
    return {
        "radius_m": radius,
        "vt_ms": vt,
        "omega_rad_s": vt / radius,
        "period_s": 2.0 * math.pi * radius / vt,
        "sense": "cyclonic (counter-clockwise from above)",
        "vt_basis": f"middle of the {tor.get('rating') or 'survey'} damage-based wind range "
        f"({rng[0]:.0f}-{rng[1]:.0f} m/s): an estimate, not a measurement",
        "radius_basis": "half the surveyed damage width"
        if width
        else "assumed 75 m (no survey width)",
        "wind_range_ms": list(rng),
        "period_range_s": list(est["rotation_period_s_range"])
        if est and est.get("rotation_period_s_range")
        else None,
        "tornado_resolved": False,
        "parent_circulation": parent_circulation(vortex, td),
    }
