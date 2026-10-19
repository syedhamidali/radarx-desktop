"""Hydrometeor class, hail indices and convection diagnostics on the bundle grid (all RETRIEVED).

* ``unified_hid`` runs radarx's fuzzy-logic hydrometeor classification per radar (Park et al. 2009 at
  S band; Dolan et al. 2013 at C band; Dolan and Rutledge 2009 at X band) and maps the band-specific
  class codes to ONE legend (``HID_LEGEND``), because the three methods number their classes differently.
  X band has no hail class: the heaviest ice there is high-density graupel.
* ``hail_fields``: SHI, MESH and POSH of Witt et al. (1998) from radarx (0 and -20 degC heights from
  the environment sounding), VIL of Greene and Clark (1972) (capped at 56 dBZ, radarx's choice).
* ``echo_tops``: Lakshmanan et al. (2013) as in radarx; the grid ends at 10 km, so tops of deep
  storms are truncated (reported).
* ``zdr_column_km``: Snyder et al. (2015) as in radarx (depth above the freezing level where
  ZDR >= 1 dB).
* ``steiner_houze_yuter``: convective / stratiform separation of the reflectivity at 2 km,
  after Steiner, Houze and Yuter (1995).
* ``bwer``: a simple bounded weak echo region (BWER) candidate detector (documented below).
"""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy import ndimage

# one legend for all bands: code -> (abbreviation, name)
HID_LEGEND: dict[int, tuple[str, str]] = {
    0: ("--", "no class"),
    1: ("DZ", "drizzle / light rain"),
    2: ("RN", "rain"),
    3: ("HR", "heavy rain"),
    4: ("BD", "big drops"),
    5: ("CR", "ice crystals"),
    6: ("AG", "aggregates / dry snow"),
    7: ("WS", "wet snow"),
    8: ("GR", "graupel (low density)"),
    9: ("HG", "high-density graupel / small hail"),
    10: ("HA", "hail (C and S band); S band: rain-hail mixture"),
    11: ("VI", "vertically aligned ice"),
}
# band -> {native abbreviation: unified code}
_MAP = {
    "S": {"DS": 6, "WS": 7, "CR": 5, "GR": 8, "BD": 4, "RA": 2, "HR": 3, "RH": 10},
    "C": {
        "DZ": 1,
        "RN": 2,
        "CR": 5,
        "AG": 6,
        "WS": 7,
        "HDG": 9,
        "LDG": 8,
        "HA": 10,
        "BD": 4,
        "VI": 11,
    },
    "X": {"DZ": 1, "RN": 2, "AG": 6, "CR": 5, "LDG": 8, "HDG": 9, "VI": 11},
}
HID_METHOD = {
    "S": "Park et al. (2009) fuzzy logic",
    "C": "Dolan et al. (2013) fuzzy logic",
    "X": "Dolan and Rutledge (2009) fuzzy logic",
}


def temperature_dataset(height_asl_m: np.ndarray, temperature_k: np.ndarray):
    import xarray as xr

    o = np.argsort(height_asl_m)
    return xr.Dataset(
        {"temperature": (("height",), np.asarray(temperature_k, float)[o])},
        coords={"height": np.asarray(height_asl_m, float)[o]},
    )


def unified_hid(
    fields: dict[str, np.ndarray],
    z_asl_m: np.ndarray,
    x_m: np.ndarray,
    y_m: np.ndarray,
    temperature,
    band: str,
) -> np.ndarray | None:
    """uint8 (nz, ny, nx) of ``HID_LEGEND`` codes for one radar's grid, or None if it has no dual-pol."""
    import radarx.retrieve as rxr
    import xarray as xr

    if not all(k in fields for k in ("dbz", "zdr", "rho")):
        return None
    data = {"REF": fields["dbz"], "ZDR": fields["zdr"], "RHO": fields["rho"]}
    kw: dict[str, Any] = {}
    if "kdp" in fields:
        data["KDP"] = fields["kdp"]
        kw["kdp"] = "KDP"
    ds = xr.Dataset(
        {k: (("z", "y", "x"), v) for k, v in data.items()},
        coords={"z": z_asl_m, "y": y_m, "x": x_m},
    )
    out = rxr.hid(
        ds,
        temperature=temperature,
        band=band,
        dbz="REF",
        zdr="ZDR",
        rhohv="RHO",
        quality=False,
        scores=False,
        **kw,
    )
    native = np.asarray(out["HID"].values)
    classes = rxr.hid_classes("auto", band)
    lut = np.zeros(256, np.uint8)
    for code, abbr, _name in classes:
        lut[code] = _MAP[band].get(abbr, 0)
    res = lut[np.clip(native, 0, 255).astype(np.uint8)]
    res[~np.isfinite(fields["dbz"])] = 0
    return res


def isotherm_heights(
    height_asl_m: np.ndarray, temperature_k: np.ndarray
) -> dict[str, float | None]:
    """Heights (m ASL) of the highest 0 and -20 degC crossings of a profile (linear in height)."""
    h = np.asarray(height_asl_m, float)
    t = np.asarray(temperature_k, float) - 273.15
    o = np.argsort(h)
    h, t = h[o], t[o]
    out: dict[str, float | None] = {}
    for name, level in (("freezing_m", 0.0), ("minus20_m", -20.0)):
        res = None
        for i in range(len(h) - 1):
            a, b = t[i] - level, t[i + 1] - level
            if np.isfinite(a) and np.isfinite(b) and a * b <= 0 and a != b:
                res = float(h[i] + (h[i + 1] - h[i]) * a / (a - b))
        out[name] = res
    return out


def hail_fields(
    dbz: np.ndarray,
    z_asl_m: np.ndarray,
    x_m: np.ndarray,
    y_m: np.ndarray,
    freezing_m: float,
    minus20_m: float,
) -> dict[str, np.ndarray]:
    """SHI, MESH (mm), POSH (%) and VIL (kg m-2) maps (ny, nx) from the merged reflectivity."""
    import radarx.retrieve as rxr
    import xarray as xr

    ds = xr.Dataset({"REF": (("z", "y", "x"), dbz)}, coords={"z": z_asl_m, "y": y_m, "x": x_m})
    kw = {"freezing_level": float(freezing_m), "minus20_level": float(minus20_m)}
    out = {
        "shi": np.asarray(rxr.shi(ds, dbz="REF", **kw).values, np.float32),
        "mesh_mm": np.asarray(rxr.mesh(ds, dbz="REF", **kw).values, np.float32),
        "posh_pct": np.asarray(rxr.posh(ds, dbz="REF", **kw).values, np.float32),
        "vil": np.asarray(rxr.vil(ds, dbz="REF")["VIL"].values, np.float32),
    }
    echo = ~np.isfinite(dbz).any(axis=0)
    for k in out:
        out[k][echo] = np.nan  # columns with no echo at all: nothing to say
    return out


def echo_tops(
    dbz: np.ndarray,
    z_asl_m: np.ndarray,
    x_m: np.ndarray,
    y_m: np.ndarray,
    thresholds=(18.0, 30.0, 40.0),
) -> dict[str, np.ndarray]:
    """Echo-top heights (km ASL) for each threshold, NaN where the column never reaches it."""
    import radarx.retrieve as rxr
    import xarray as xr

    ds = xr.Dataset({"REF": (("z", "y", "x"), dbz)}, coords={"z": z_asl_m, "y": y_m, "x": x_m})
    out = {}
    for t in thresholds:
        top = (
            np.asarray(rxr.echo_top(ds, dbz="REF", threshold=float(t)).values, np.float32) / 1000.0
        )
        reach = np.nanmax(np.where(np.isfinite(dbz), dbz, -999.0), axis=0) >= t
        top[~reach] = np.nan
        out[f"echotop{int(t)}_km"] = top
    return out


def zdr_column_km(
    zdr: np.ndarray,
    rho: np.ndarray,
    dbz: np.ndarray,
    z_asl_m: np.ndarray,
    x_m: np.ndarray,
    y_m: np.ndarray,
    freezing_m: float,
    min_dbz: float | None = 30.0,
) -> np.ndarray:
    """ZDR column depth (km) above the freezing level (Snyder et al. 2015, radarx): ZDR >= 1 dB, RHO >= 0.8."""
    import radarx.retrieve as rxr
    import xarray as xr

    ds = xr.Dataset(
        {
            "ZDR": (("z", "y", "x"), zdr),
            "RHO": (("z", "y", "x"), rho),
            "REF": (("z", "y", "x"), dbz),
        },
        coords={"z": z_asl_m, "y": y_m, "x": x_m},
    )
    d = rxr.zdr_column(
        ds, zdr="ZDR", rhohv="RHO", dbz="REF", freezing_level=float(freezing_m), min_dbz=min_dbz
    )
    out = np.asarray(d.values, np.float32) / 1000.0
    out[out <= 0] = np.nan
    return out


def _disk(radius_cells: float) -> np.ndarray:
    r = int(np.ceil(radius_cells))
    yy, xx = np.mgrid[-r : r + 1, -r : r + 1]
    return (xx**2 + yy**2) <= radius_cells**2


def steiner_houze_yuter(
    dbz2d: np.ndarray,
    cell_km: float,
    bg_radius_km: float = 11.0,
    intense_dbz: float = 40.0,
    min_echo_dbz: float = 0.0,
) -> np.ndarray:
    """uint8 map: 0 no echo, 1 stratiform, 2 convective (Steiner, Houze and Yuter 1995 as described below).

    A grid point with echo is convective if (a) its reflectivity is at least ``intense_dbz``, or (b) it
    exceeds the mean reflectivity of the echo within ``bg_radius_km`` (the background, averaged in linear
    Z, echo points only) by more than ``dZ(bg) = 10`` (bg < 0), ``10 - bg^2 / 180`` (0 <= bg < 42.43) or
    ``0`` (bg >= 42.43) dB (the "peakedness" criterion). Convective points then convert the points
    within a radius of 1, 2, 3, 4 or 5 km (background < 25, 25-30, 30-35, 35-40, >= 40 dBZ) to convective.
    The radius table and dZ(bg) are the paper's as remembered by the author of this module, not checked
    against the paper; the parameters can be changed (see ``docs/PERILS_BUNDLE.md``).
    """
    z = np.asarray(dbz2d, np.float64)
    echo = np.isfinite(z) & (z >= min_echo_dbz)
    lin = np.where(echo, 10.0 ** (z / 10.0), 0.0)
    k = _disk(bg_radius_km / cell_km).astype(np.float64)
    num = ndimage.convolve(lin, k, mode="constant")
    den = ndimage.convolve(echo.astype(np.float64), k, mode="constant")
    with np.errstate(invalid="ignore", divide="ignore"):
        bg = np.where(
            den > 0, 10.0 * np.log10(np.maximum(num / np.maximum(den, 1e-9), 1e-9)), np.nan
        )
    dz = np.where(bg < 0, 10.0, np.where(bg < 42.43, 10.0 - bg**2 / 180.0, 0.0))
    center = echo & ((z >= intense_dbz) | (z - bg > dz))
    out = np.zeros(z.shape, np.uint8)
    out[echo] = 1
    # convective radius by background
    conv = center.copy()
    for lo, hi, rad in (
        (-np.inf, 25.0, 1.0),
        (25.0, 30.0, 2.0),
        (30.0, 35.0, 3.0),
        (35.0, 40.0, 4.0),
        (40.0, np.inf, 5.0),
    ):
        sel = center & (bg >= lo) & (bg < hi)
        if sel.any():
            conv |= ndimage.binary_dilation(sel, structure=_disk(rad / cell_km))
    out[conv & echo] = 2
    return out


def bwer(dbz: np.ndarray, z_km: np.ndarray, cell_km: float) -> np.ndarray:
    """uint8 (ny, nx) BWER candidates: 1 where a column has a weak-echo hole under an echo overhang with echo around.

    In a column: ``wer`` = minimum reflectivity between 1 and 4 km (a missing value counts as no
    echo only if the column has echo above), ``top`` = maximum reflectivity between 4.5 and 8 km.
    Candidate if ``top >= 35`` dBZ, ``wer <= top - 20`` dB, and the mean reflectivity of the 3 to 6 km
    ring around the column at the 1 to 4 km level (maximum over the levels) is at least ``wer + 15``
    (the hole is bounded). A candidate list, not a mesocyclone detector: it needs an inflow
    notch (strong updraft) and these thresholds are the author's choice (no published value used).
    """
    lo = (z_km >= 1.0) & (z_km <= 4.0)
    hi = (z_km >= 4.5) & (z_km <= 8.0)
    sub = np.where(np.isfinite(dbz[lo]), dbz[lo], -10.0)
    wer = sub.min(axis=0)
    with np.errstate(all="ignore"):
        top = np.nanmax(np.where(np.isfinite(dbz[hi]), dbz[hi], -np.inf), axis=0)
    top = np.where(np.isfinite(top), top, -np.inf)
    cand = (top >= 35.0) & (wer <= top - 20.0)
    r_in, r_out = 3.0 / cell_km, 6.0 / cell_km
    rr = int(np.ceil(r_out))
    yy, xx = np.mgrid[-rr : rr + 1, -rr : rr + 1]
    d2 = xx**2 + yy**2
    ring = ((d2 <= r_out**2) & (d2 >= r_in**2)).astype(np.float64)
    bounded = np.zeros(wer.shape, bool)
    if cand.any():
        best = np.full(wer.shape, -np.inf)
        for k in np.nonzero(lo)[0]:
            lev = np.where(np.isfinite(dbz[k]), dbz[k], np.nan)
            filled = np.where(np.isfinite(lev), lev, 0.0)
            cnt = ndimage.convolve(np.isfinite(lev).astype(np.float64), ring, mode="constant")
            s = ndimage.convolve(filled, ring, mode="constant")
            with np.errstate(invalid="ignore", divide="ignore"):
                mean = np.where(cnt > 0, s / np.maximum(cnt, 1), -np.inf)
            best = np.maximum(best, mean)
        bounded = best >= wer + 15.0
    return (cand & bounded).astype(np.uint8)


def column_max(dbz: np.ndarray) -> np.ndarray:
    with np.errstate(all="ignore"):
        m = np.nanmax(np.where(np.isfinite(dbz), dbz, -np.inf), axis=0)
    return np.where(np.isfinite(m), m, np.nan).astype(np.float32)
