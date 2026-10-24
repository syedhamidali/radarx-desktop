"""Opt-in reference for the absolute fold of a dealiased velocity sweep (issue #152).

Region-based dealiasing makes the regions of a sweep agree with each other, but the sweep as a
whole can still be off by a whole number of unambiguous intervals (``2 * Vn``): nothing in the
folded data says which interval the anchor region belongs to. :func:`fold_reference_shift`
chooses that whole-sweep multiple from a stated assumption:

``"none"``
    No reference (the default; the sweep is returned as dealiased).
``"zero"``
    The along-beam wind averages to about zero around a full circle. For a wind that is uniform
    at each height (or varies linearly), the radial component is a first harmonic of azimuth whose
    mean over 360 degrees is zero (Browning and Wexler, 1968, J. Appl. Meteor. 7, 105-113); a
    strong divergence or vertical motion adds a mean of a few m/s at most, well below ``Vn``. This
    is the same idea as Py-ART's ``centered`` option. It needs echo all around the radar; for a
    sector scan or echo on one side it is refused.
``"vad"``
    A uniform-wind VAD (Browning and Wexler, 1968) ``v = a + b cos(az) + c sin(az)`` is fitted to
    the 10-degree sector means of the dealiased sweep, and the multiple is the one that brings the
    constant term ``a`` nearest zero. Unlike ``"zero"`` it allows gaps in azimuth, but the echo
    must leave no gap wider than 180 degrees (otherwise ``a`` cannot be separated from the wind).

The shift is the same for every gate, so the region-to-region structure is untouched. The
functions only use numpy: the pipeline copies them verbatim into exported scripts.

:func:`fold_check` is the check that is always on, whatever the reference. It does not need a
reference: it looks at every 10 km range ring on its own, using the median of each 10-degree
sector (so that a minority of wrongly unfolded gates cannot move it, while a whole sweep in a wrong
interval moves every gate). For a wind that is uniform around a ring, the ring's constant VAD term
is the divergence-like mean of the along-beam wind, a few m/s at most (Browning and Wexler, 1968),
so a ring whose constant is more than one Nyquist velocity from zero has been unfolded to the
wrong interval. If the rings that show this all say the same
multiple, the whole sweep is off by that multiple of ``2 * Vn`` (a correctable offset); if they
disagree, parts of the sweep sit in different intervals, which no single shift can repair, and
the sweep is reported as inconsistent. The check reports, it never changes the data.
"""

from __future__ import annotations

import numpy as np

__all__ = ["REFERENCES", "fold_check", "fold_reference_shift", "implausible_velocity"]

#: accepted values of the ``reference`` option, the first is the default
REFERENCES = ("none", "zero", "vad")


def fold_reference_shift(vel, azimuth, nyquist, reference="none"):
    """Whole number ``k`` of ``2 * nyquist`` intervals to add to a dealiased sweep, and a note.

    ``vel`` is the dealiased (azimuth, range) sweep (NaN = no data), ``azimuth`` the ray azimuths
    in degrees. ``reference`` is ``"none"`` (k = 0), ``"zero"`` or ``"vad"`` (see the module
    docstring). Raises ``ValueError`` when the echo does not cover enough of the circle for the
    chosen reference to be valid.
    """
    if reference == "none":
        return 0, ""
    if reference not in ("zero", "vad"):
        raise ValueError(f"reference must be 'none', 'zero' or 'vad', got {reference!r}")
    if not (np.isfinite(nyquist) and nyquist > 0):
        raise ValueError(f"the Nyquist velocity must be a positive number, got {nyquist}")
    v = np.asarray(vel, dtype=np.float64)
    az = np.mod(np.asarray(azimuth, dtype=np.float64).ravel(), 360.0)
    if v.ndim != 2 or az.size != v.shape[0] or not np.isfinite(az).all():
        raise ValueError("the reference needs one finite azimuth per ray")
    n_sec = 36  # 10-degree sectors: every direction counts the same, however many gates it has
    sector = np.floor(az / (360.0 / n_sec)).astype(np.int64) % n_sec
    ok = np.isfinite(v)
    sums = np.zeros(n_sec)
    counts = np.zeros(n_sec)
    np.add.at(sums, sector, np.where(ok, v, 0.0).sum(axis=1))
    np.add.at(counts, sector, ok.sum(axis=1))
    have = counts > 0
    n_have = int(have.sum())
    if n_have == 0:
        raise ValueError("the sweep has no velocity data")
    means = sums[have] / counts[have]
    centres = (np.flatnonzero(have) + 0.5) * (360.0 / n_sec)
    if reference == "zero":
        if n_have < 33:
            raise ValueError(
                f"reference 'zero' needs echo all around the radar, but only {n_have} of 36 "
                "10-degree sectors have velocity data (a sector scan or one-sided echo); "
                "use 'vad' (allows gaps up to 180 degrees) or 'none'"
            )
        offset = float(np.mean(means))
        how = f"mean of {n_have} sectors {offset:+.1f} m/s"
    else:
        gaps = np.diff(np.concatenate([centres, [centres[0] + 360.0]]))
        gap = float(gaps.max()) - 360.0 / n_sec
        if n_have < 6 or gap > 180.0:
            raise ValueError(
                f"reference 'vad' needs echo around most of the circle, but the largest "
                f"azimuth gap is {gap:.0f} degrees ({n_have} of 36 sectors have data); "
                "a VAD fit is not defined for this sweep, use 'none'"
            )
        th = np.radians(centres)
        design = np.column_stack([np.ones_like(th), np.cos(th), np.sin(th)])
        coef = np.linalg.lstsq(design, means, rcond=None)[0]
        offset = float(coef[0])
        how = f"VAD constant term {offset:+.1f} m/s"
    k = int(np.rint(-offset / (2.0 * nyquist)))
    return k, f"reference {reference}: {how}, shift {k * 2.0 * nyquist:+g} m/s"


def fold_check(vel, azimuth, nyquist, range_m=None):
    """Always-on sanity check of a dealiased sweep: is it (or part of it) off by whole intervals?

    ``vel`` is the dealiased (azimuth, range) sweep (NaN = no data), ``azimuth`` the ray azimuths
    in degrees, ``range_m`` the gate ranges in metres (without it, groups of 40 gates are used).
    Each 10 km range ring with echo in at least 24 of 36 10-degree sectors (no gap wider than
    90 degrees; a sector needs 3 gates) gets a uniform-wind VAD fit (Browning and Wexler, 1968)
    to the sector medians; a ring constant more than
    one Nyquist velocity from zero means that ring is unfolded to a wrong interval. Returns a dict:

    ``status``
        ``"consistent"`` (under 10 % of the gates of the usable rings are affected),
        ``"whole_field_offset"`` (at least 90 % of the usable gates sit in rings with one and the
        same multiple ``k`` of ``2 * Vn``, and the usable rings hold at least half of all gates: the
        sweep as a whole is off by ``-k`` intervals, adding
        ``k * 2 * Vn`` fixes it), ``"inconsistent"`` (10 % or more of the gates are affected but
        not as one whole-sweep multiple: parts of the sweep sit in different intervals, a single
        shift cannot fix it) or ``"undetermined"`` (too little echo around the radar to say).
    ``k``, ``shift_ms``
        the whole-sweep multiple and the shift in m/s to add (0 unless ``whole_field_offset``).
    ``off_share``, ``usable_share``
        share of the usable gates in affected rings, share of all gates in usable rings.
    ``message``
        a sentence for the user.
    """
    v = np.asarray(vel, dtype=np.float64)
    az = np.mod(np.asarray(azimuth, dtype=np.float64).ravel(), 360.0)
    out = {
        "status": "undetermined",
        "k": 0,
        "shift_ms": 0.0,
        "off_share": None,
        "usable_share": None,
        "rings": 0,
        "nyquist_ms": float(nyquist) if np.isfinite(nyquist) else None,
        "message": "",
    }
    if not (np.isfinite(nyquist) and nyquist > 0) or v.ndim != 2 or az.size != v.shape[0]:
        out["message"] = (
            "fold check not possible: needs a 2-D sweep, its azimuths and a Nyquist velocity"
        )
        return out
    ng = v.shape[1]
    if range_m is not None and np.asarray(range_m).size == ng:
        ring = np.floor(np.asarray(range_m, dtype=np.float64) / 10000.0).astype(np.int64)
    else:
        ring = np.arange(ng) // 40
    ring -= ring.min()
    n_ring = int(ring.max()) + 1
    sector = np.floor(az / 10.0).astype(np.int64) % 36
    rows = [np.flatnonzero(sector == q) for q in range(36)]
    centres = np.radians((np.arange(36) + 0.5) * 10.0)
    ks = np.zeros(n_ring, dtype=np.int64)
    usable = np.zeros(n_ring, dtype=bool)
    cnts = np.zeros((36, n_ring))
    for j in range(n_ring):
        block = v[:, ring == j]
        meds = np.full(36, np.nan)
        for q in range(36):
            vals = block[rows[q]].ravel() if rows[q].size else np.empty(0)
            vals = vals[np.isfinite(vals)]
            cnts[q, j] = vals.size
            if vals.size >= 3:  # a median needs a few gates
                # the median, not the mean: gates of a few wrongly unfolded patches must not
                # move the ring, a whole sweep in a wrong interval moves every gate
                meds[q] = np.median(vals)
        have = np.isfinite(meds)
        if int(have.sum()) < 24:
            continue
        idx = np.flatnonzero(have)
        gaps = np.diff(np.concatenate([idx, [idx[0] + 36]])) * 10.0 - 10.0
        if gaps.max() > 90.0:
            continue
        th = centres[have]
        design = np.column_stack([np.ones_like(th), np.cos(th), np.sin(th)])
        coef = np.linalg.lstsq(design, meds[have], rcond=None)[0]
        ks[j] = int(np.rint(-coef[0] / (2.0 * nyquist)))
        usable[j] = True
    gates = cnts.sum(axis=0)
    total = float(gates.sum())
    use = float(gates[usable].sum())
    out["rings"] = int(usable.sum())
    if total <= 0 or use < 0.1 * total or usable.sum() < 1:
        out["message"] = (
            "fold check not possible: too little echo around the radar to test the rings "
            "(sector scan or one-sided echo)"
        )
        return out
    out["usable_share"] = round(use / total, 3)
    bad = usable & (ks != 0)
    off = float(gates[bad].sum()) / use
    out["off_share"] = round(off, 3)
    if off < 0.10:
        out["status"] = "consistent"
        out["message"] = (
            "fold check passed: the typical (median) along-beam wind is within one Nyquist "
            "velocity of zero "
            f"in {100.0 * (1.0 - off):.0f}% of the gates of {int(usable.sum())} range rings"
        )
        return out
    vals = np.unique(ks[bad])
    share = {int(k): float(gates[bad & (ks == k)].sum()) / use for k in vals}
    k_dom = max(share, key=share.get)
    if share[k_dom] >= 0.9 and use >= 0.5 * total:
        shift = k_dom * 2.0 * float(nyquist)
        out.update(status="whole_field_offset", k=k_dom, shift_ms=shift)
        out["message"] = (
            f"fold check FAILED: the whole sweep looks off by {-k_dom:+d} x 2 x Nyquist "
            f"({-shift:+g} m/s at every gate): {100.0 * off:.0f}% of the gates sit in range rings "
            "whose typical (median) along-beam wind is more than one Nyquist velocity from "
            "zero, all in the "
            f"same direction. Set the fold reference to vad or zero to add {shift:+g} m/s, or "
            "check it against a sounding or a model wind"
        )
    else:
        out["status"] = "inconsistent"
        out["message"] = (
            f"fold check FAILED: {100.0 * off:.0f}% of the gates sit in range rings whose "
            "typical (median) along-beam wind is more than one Nyquist velocity from zero "
            "(the other rings are "
            "fine, off by a different multiple, or too incomplete to test; the rings that could "
            f"be tested hold {100.0 * use / total:.0f}% of the gates): parts of the sweep are "
            "unfolded to different intervals and a single shift cannot repair that. Do not use "
            "this sweep for winds, shear or rotation without checking it; try the UNRAVEL "
            "method or the other dealiasing method"
        )
    return out


def implausible_velocity(vel, nyquist):
    """Gates whose unfolded radial velocity is beyond any physical value (True = implausible).

    The bound is the larger of 150 m/s and 5 Nyquist velocities. No radial wind of that size has
    been measured (the highest Doppler radar wind speeds reported are about 135 m/s), and the
    region unfolding of radarx 0.5.1 can give a few gates a fold of thousands of intervals
    (velocities of 5e5 m/s, radarx issue 350). A fold error of one to three intervals stays below
    the bound and is left to :func:`fold_check`. NaN is never flagged.
    """
    v = np.asarray(vel, dtype=np.float64)
    limit = max(150.0, 5.0 * float(nyquist))
    with np.errstate(invalid="ignore"):
        return np.abs(v) > limit
