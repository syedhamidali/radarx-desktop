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
function only uses numpy: the pipeline copies it verbatim into exported scripts.
"""

from __future__ import annotations

import numpy as np

__all__ = ["REFERENCES", "fold_reference_shift"]

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
