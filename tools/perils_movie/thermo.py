"""Small thermodynamic helpers (Bolton 1980) for the station series and the Skew-T parcel paths.

All formulas are from Bolton, D., 1980: The computation of equivalent potential temperature, Mon. Wea.
Rev. 108, 1046-1053: saturation vapour pressure (eq. 10), dew point (inverse of eq. 10), LCL temperature
(eq. 15), theta-e (eq. 43). Temperatures in degC unless the name says ``_k``; pressure in hPa.
"""

from __future__ import annotations

import numpy as np

EPS = 0.62197
RD_CP = 0.2854  # Rd / cp, Bolton 1980 (kappa)


def es_hpa(t_c):
    """Saturation vapour pressure over water (hPa), Bolton (1980) eq. 10."""
    t = np.asarray(t_c, float)
    return 6.112 * np.exp(17.67 * t / (t + 243.5))


def dewpoint_c(t_c, rh_pct):
    """Dew point (degC) from temperature and relative humidity (%), inverse of Bolton eq. 10."""
    t = np.asarray(t_c, float)
    rh = np.clip(np.asarray(rh_pct, float), 1.0, 100.0) / 100.0
    with np.errstate(invalid="ignore", divide="ignore"):
        ln = np.log(rh * es_hpa(t) / 6.112)
        return 243.5 * ln / (17.67 - ln)


def mixing_ratio(td_c, p_hpa):
    """Mixing ratio (g/kg) from the dew point and the pressure (hPa)."""
    e = es_hpa(td_c)
    with np.errstate(invalid="ignore", divide="ignore"):
        return 1000.0 * EPS * e / (np.asarray(p_hpa, float) - e)


def theta_e_k(t_c, td_c, p_hpa):
    """Equivalent potential temperature (K), Bolton (1980) eq. 43 (with the LCL temperature of eq. 15)."""
    t = np.asarray(t_c, float) + 273.15
    td = np.asarray(td_c, float) + 273.15
    p = np.asarray(p_hpa, float)
    r = mixing_ratio(td_c, p) / 1000.0
    with np.errstate(invalid="ignore", divide="ignore"):
        tl = 1.0 / (1.0 / (td - 56.0) + np.log(t / td) / 800.0) + 56.0
        th = t * (1000.0 / p) ** (RD_CP * (1.0 - 0.28 * r))
        return th * np.exp((3036.0 / tl - 1.78) * r * (1.0 + 0.448 * r))


def theta_k(t_c, p_hpa):
    return (np.asarray(t_c, float) + 273.15) * (1000.0 / np.asarray(p_hpa, float)) ** RD_CP


def parcel_path(
    p_start_hpa: float,
    t_start_c: float,
    td_start_c: float,
    p_top_hpa: float = 100.0,
    dp: float = 5.0,
):
    """Pseudo-adiabatic parcel path for a Skew-T: ``(p_hpa, t_c)`` arrays from the start upward.

    Dry adiabat (constant theta) to the LCL (Bolton eq. 15 for T, pressure from Poisson), then the
    saturated adiabat by integrating ``dT/dp = (Rd T + Lv rs) / (p (cp + Lv^2 rs eps / (Rd T^2)))`` in
    steps of ``dp`` hPa (4th-order Runge-Kutta). Reversible ice effects are ignored (liquid only), so
    it is a drawing aid and may differ from the CAPE kernel of radarx near and above the freezing level.
    """
    rd, cp, lv = 287.04, 1005.7, 2.501e6
    t0 = t_start_c + 273.15
    td0 = td_start_c + 273.15
    tl = 1.0 / (1.0 / (td0 - 56.0) + np.log(t0 / td0) / 800.0) + 56.0
    p_lcl = p_start_hpa * (tl / t0) ** (1.0 / RD_CP)
    ps, ts = [p_start_hpa], [t_start_c]
    p = p_start_hpa
    while p - dp > p_lcl:
        p -= dp
        ps.append(p)
        ts.append(t0 * (p / p_start_hpa) ** RD_CP - 273.15)
    ps.append(p_lcl)
    ts.append(tl - 273.15)

    def f(pp, tt):
        rs = EPS * es_hpa(tt - 273.15) / (pp - es_hpa(tt - 273.15))
        return (
            (rd * tt + lv * rs) / (pp * 100.0 * (cp + lv * lv * rs * EPS / (rd * tt * tt))) * 100.0
        )

    p, t = p_lcl, tl
    while p - dp >= p_top_hpa:
        h = -dp
        k1 = f(p, t)
        k2 = f(p + h / 2, t + h / 2 * k1)
        k3 = f(p + h / 2, t + h / 2 * k2)
        k4 = f(p + h, t + h * k3)
        t = t + h / 6 * (k1 + 2 * k2 + 2 * k3 + k4)
        p = p + h
        ps.append(p)
        ts.append(t - 273.15)
    return np.array(ps), np.array(ts)
