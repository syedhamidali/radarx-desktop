"""Derived quantities for model and reanalysis fields.

The dew point from specific humidity is **radarx's** (``radarx.io.sounding``, Bolton 1980): this
module only calls it with plain arrays and returns a plain array, so there is one implementation.
It needs the optional radarx package; the known-answer test checks radarx's result against a
value worked out by hand.

What is *not* in radarx and is glue to the GRIB2 file format lives here: the rotation of winds
that NCEP stores along the axes of a Lambert conformal grid (the ``uvRelativeToGrid`` flag of the
file) to east and north. It is checked against the local axes computed by PROJ.

References
----------
* Bolton, D., 1980: The computation of equivalent potential temperature. Mon. Wea. Rev., 108,
  1046-1053 (dew point, via radarx).
* Snyder, J. P., 1987: Map Projections: A Working Manual. USGS Professional Paper 1395, section
  15 (Lambert conformal conic: cone constant, convergence of the meridians).
"""

from __future__ import annotations

from typing import Any

import numpy as np


def _arr(x: Any) -> np.ndarray:
    return np.asarray(x, dtype=float)


def _sounding_module() -> Any:
    from engine import sounding

    return sounding._need_radarx()[1]


def _values(x: Any) -> np.ndarray:
    return np.asarray(getattr(x, "values", x), dtype=float)


def dewpoint_from_specific_humidity(q: Any, p_pa: Any) -> np.ndarray:
    """Dew point (K) from specific humidity (kg/kg) and pressure (Pa) (radarx)."""
    return _values(_sounding_module().dewpoint_from_specific_humidity(_arr(q), _arr(p_pa)))


def lambert_cone_constant(latin1_deg: float, latin2_deg: float) -> float:
    """Cone constant ``n`` of a Lambert conformal conic projection with standard parallels
    *latin1_deg* and *latin2_deg* (Snyder 1987, eq. 15-8; ``sin(latin)`` when they are equal)."""
    p1, p2 = np.radians(float(latin1_deg)), np.radians(float(latin2_deg))
    if abs(p1 - p2) < 1e-9:
        return float(np.sin(p1))
    num = np.log(np.cos(p1) / np.cos(p2))
    den = np.log(np.tan(np.pi / 4 + p2 / 2) / np.tan(np.pi / 4 + p1 / 2))
    return float(num / den)


def grid_winds_to_earth(
    u_grid: Any, v_grid: Any, lon_deg: Any, lov_deg: float, latin1_deg: float, latin2_deg: float
) -> tuple[np.ndarray, np.ndarray]:
    """Rotate wind components given along the axes of a Lambert conformal grid (what NCEP's GRIB2
    files hold when ``uvRelativeToGrid`` is set) to east and north components.

    The grid's "up" differs from true north by ``gamma = n (lon - LoV)`` (Snyder 1987, section 15:
    the convergence of the meridians); ``u_e = u cos(gamma) + v sin(gamma)`` and
    ``v_e = -u sin(gamma) + v cos(gamma)``. The speed is unchanged.
    """
    n = lambert_cone_constant(latin1_deg, latin2_deg)
    dl = ((_arr(lon_deg) - float(lov_deg) + 180.0) % 360.0) - 180.0
    g = n * np.radians(dl)
    u, v = _arr(u_grid), _arr(v_grid)
    return u * np.cos(g) + v * np.sin(g), -u * np.sin(g) + v * np.cos(g)
