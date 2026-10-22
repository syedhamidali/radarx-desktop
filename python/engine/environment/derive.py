"""Units and derived quantities for model and reanalysis fields (SI in, SI out).

Every function here has a known answer in ``tests/test_environment_derive.py``. Nothing is
smoothed or filled: a value that cannot be computed is NaN.

References
----------
* Bolton, D., 1980: The computation of equivalent potential temperature. Mon. Wea. Rev., 108,
  1046-1053. Eq. (10): saturation vapour pressure ``es = 6.112 exp(17.67 T / (T + 243.5))`` hPa
  (T in degC), good to 0.1 % between -35 and 35 degC; its inverse gives the dew point.
* WMO, 2018: Guide to Instruments and Methods of Observation (WMO-No. 8): the mixing-ratio and
  vapour-pressure relations, epsilon = Rd / Rv = 0.622.
* Standard acceleration of gravity g0 = 9.80665 m s-2 (ISO 80000-3), which converts ERA5
  geopotential (m2 s-2) to geopotential height (m).
"""

from __future__ import annotations

from typing import Any

import numpy as np

T0 = 273.15  # K at 0 degC
EPSILON = 0.622  # Rd / Rv
G0 = 9.80665  # m s-2
#: Bolton (1980) constants for es in hPa, T in degC.
_ES0_HPA = 6.112
_A = 17.67
_B = 243.5  # degC


def _arr(x: Any) -> np.ndarray:
    return np.asarray(x, dtype=float)


def wind_speed_direction(u: Any, v: Any) -> tuple[np.ndarray, np.ndarray]:
    """Speed (same unit as u, v) and meteorological direction in degrees (0 = from the north,
    90 = from the east: where the wind blows **from**). The direction of calm air is NaN."""
    u, v = _arr(u), _arr(v)
    speed = np.hypot(u, v)
    direction = (np.degrees(np.arctan2(-u, -v)) + 360.0) % 360.0
    return speed, np.where(speed > 0, direction, np.nan)


def wind_components(speed: Any, direction_deg: Any) -> tuple[np.ndarray, np.ndarray]:
    """``(u, v)`` from speed and the direction the wind blows **from**."""
    rad = np.radians(_arr(direction_deg))
    s = _arr(speed)
    return -s * np.sin(rad), -s * np.cos(rad)


def saturation_vapour_pressure(t_k: Any) -> np.ndarray:
    """Saturation vapour pressure over liquid water in Pa (Bolton 1980, eq. 10)."""
    tc = _arr(t_k) - T0
    return _ES0_HPA * np.exp(_A * tc / (tc + _B)) * 100.0


def dewpoint_from_vapour_pressure(e_pa: Any) -> np.ndarray:
    """Dew point in K from the vapour pressure in Pa (inverse of Bolton 1980, eq. 10)."""
    e = _arr(e_pa)
    with np.errstate(invalid="ignore", divide="ignore"):
        x = np.log(e / 100.0 / _ES0_HPA)
        td = _B * x / (_A - x) + T0
    return np.where(e > 0, td, np.nan)


def dewpoint_from_rh(t_k: Any, rh_percent: Any) -> np.ndarray:
    """Dew point (K) from temperature (K) and relative humidity (percent, 0-100)."""
    rh = _arr(rh_percent)
    e = np.where(rh > 0, rh / 100.0 * saturation_vapour_pressure(t_k), np.nan)
    return dewpoint_from_vapour_pressure(e)


def rh_from_dewpoint(t_k: Any, td_k: Any) -> np.ndarray:
    """Relative humidity (percent) from temperature and dew point (K)."""
    return 100.0 * saturation_vapour_pressure(td_k) / saturation_vapour_pressure(t_k)


def vapour_pressure_from_specific_humidity(q: Any, p_pa: Any) -> np.ndarray:
    """Vapour pressure (Pa) from specific humidity (kg/kg) and pressure (Pa):
    ``e = q p / (epsilon + (1 - epsilon) q)``."""
    q = _arr(q)
    return q * _arr(p_pa) / (EPSILON + (1.0 - EPSILON) * q)


def dewpoint_from_specific_humidity(q: Any, p_pa: Any) -> np.ndarray:
    """Dew point (K) from specific humidity (kg/kg) and pressure (Pa)."""
    return dewpoint_from_vapour_pressure(vapour_pressure_from_specific_humidity(q, p_pa))


def mixing_ratio_from_specific_humidity(q: Any) -> np.ndarray:
    """Water-vapour mixing ratio (kg/kg) from specific humidity: ``w = q / (1 - q)``."""
    q = _arr(q)
    return q / (1.0 - q)


def geopotential_height(geopotential: Any) -> np.ndarray:
    """Geopotential height (m) from geopotential (m2 s-2), as ERA5 stores it: ``Phi / g0``."""
    return _arr(geopotential) / G0


def kelvin_to_celsius(t_k: Any) -> np.ndarray:
    return _arr(t_k) - T0


def celsius_to_kelvin(t_c: Any) -> np.ndarray:
    return _arr(t_c) + T0


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
