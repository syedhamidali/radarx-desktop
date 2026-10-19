"""Quantitative precipitation estimation (QPE) for one polar sweep, with quality and uncertainty.

This is the science core of the hydrology tools. It sits on top of ``radarx.retrieve`` (power-law
rain-rate relations, KDP processing, attenuation correction, hydrometeor classification, beam
blockage) and adds what a hydrologist needs around the rate: *which* relation was used at
every gate,
*how good* the beam was there and *how uncertain* the number is. Every coefficient is printed in a
cited paper; nothing is fitted here. See ``docs/HYDROLOGY.md`` for the equations and the limits.

Rain rate relations (R in mm/h, Z in mm^6/m^3, KDP in deg/km, zdr = 10^(ZDR/10))

* ``z``      R = a Z^b          WSR-88D ``Z = 300 R^1.4`` by default (Fulton et al. 1998);
             other Z-R pairs are selectable (:data:`ZR_RELATIONS`), including snow Z-S.
* ``kdp``    R = a |KDP|^b      S: 44.0 |KDP|^0.822 (Giangrande & Ryzhkov 2008); C, X: radarx.
* ``z_zdr``  R = a Z^b zdr^c    S: 1.42e-2 Z^0.770 zdr^-1.67 (Giangrande & Ryzhkov 2008).
* ``a``      R = a(T) A^b(T)    Ryzhkov et al. (2014), Table 1; A = alpha KDP (Bringi et al. 1990).
* ``hybrid`` per gate: the hydrometeor class (radarx HID, Park et al. 2009) and the rain-rate
             regime choose among the four (see :func:`estimate_sweep`).

Gate states of the returned rain-rate array: a finite value is an estimate (0 = observed
no-echo or below ``min_dbz``), NaN means *no estimate* (no data, hail/melting layer/ice excluded,
a required field missing). The state is never silently turned into zero.

Provenance: every output of this module is a RETRIEVED quantity (an estimate from a model of
the drop size distribution), never an observation.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import xarray as xr

from engine import provenance

EARTH_RADIUS_M = 6_371_000.0
K_E = 4.0 / 3.0

METHODS = ("auto", "hybrid", "z", "kdp", "z_zdr", "a")

#: relation codes of ``SweepQpe.relation`` (uint8)
REL_NONE, REL_Z, REL_ZDR, REL_KDP, REL_A, REL_SNOW = 0, 1, 2, 3, 4, 5
REL_NAMES = {
    REL_NONE: "none",
    REL_Z: "R(Z)",
    REL_ZDR: "R(Z,ZDR)",
    REL_KDP: "R(KDP)",
    REL_A: "R(A)",
    REL_SNOW: "S(Z)",
}

#: bits of ``SweepQpe.flags`` (uint8)
FLAG_ML = 1  # beam inside / above the melting layer: no (or flagged) estimate
FLAG_HAIL = 2  # hail / graupel / rain-hail class, or reflectivity capped
FLAG_ATTEN = 4  # estimated path-integrated attenuation above the threshold
FLAG_BLOCK = 8  # cumulative beam blockage above the threshold (needs a DEM)
FLAG_ZCAP = 16  # reflectivity capped before R(Z)
FLAG_NONMET = 32  # low correlation coefficient: not weather, rate set to 0
FLAG_NOEST = 64  # echo present but no valid relation (class excluded or field missing)
FLAG_MEANINGS = {
    FLAG_ML: "melting_layer_or_ice",
    FLAG_HAIL: "hail_graupel_or_capped",
    FLAG_ATTEN: "attenuation",
    FLAG_BLOCK: "beam_blockage",
    FLAG_ZCAP: "reflectivity_capped",
    FLAG_NONMET: "non_meteorological",
    FLAG_NOEST: "no_valid_relation",
}


class QpeError(Exception):
    """A user-facing QPE problem (missing field, bad option)."""


# ---------------------------------------------------------------------------
# Z-R relations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ZRelation:
    """``Z = A R^beta`` (Z in mm^6/m^3, R or S in mm/h)."""

    key: str
    label: str
    A: float
    beta: float
    kind: str  # "rain" | "snow"
    reference: str
    note: str = ""

    @property
    def a(self) -> float:
        """Prefactor of ``R = a Z^b`` (the form radarx uses)."""
        return self.A ** (-1.0 / self.beta)

    @property
    def b(self) -> float:
        return 1.0 / self.beta

    def text(self) -> str:
        unit = "S" if self.kind == "snow" else "R"
        return f"Z = {self.A:g} {unit}^{self.beta:g}  (R = {self.a:.4g} Z^{self.b:.4g})"


ZR_RELATIONS: dict[str, ZRelation] = {
    r.key: r
    for r in (
        ZRelation(
            "wsr88d",
            "WSR-88D default (convective)",
            300.0,
            1.4,
            "rain",
            "Fulton et al. 1998, Wea. Forecasting 13, 377-395 (operational WSR-88D relation)",
            "Default of the US network; also the 'convective' relation.",
        ),
        ZRelation(
            "convective",
            "Convective (= WSR-88D default)",
            300.0,
            1.4,
            "rain",
            "Fulton et al. 1998, Wea. Forecasting 13, 377-395",
            "Alias of 'wsr88d'.",
        ),
        ZRelation(
            "marshall_palmer",
            "Marshall-Palmer (stratiform)",
            200.0,
            1.6,
            "rain",
            "Marshall & Palmer 1948, J. Meteor. 5, 165-166",
            "Exponential drop-size distribution of widespread (stratiform) rain.",
        ),
        ZRelation(
            "stratiform",
            "Stratiform (= Marshall-Palmer)",
            200.0,
            1.6,
            "rain",
            "Marshall & Palmer 1948, J. Meteor. 5, 165-166",
            "Alias of 'marshall_palmer'.",
        ),
        ZRelation(
            "tropical",
            "Tropical (Rosenfeld)",
            250.0,
            1.2,
            "rain",
            "Rosenfeld et al. 1993, J. Appl. Meteor. 32, 50-72 (WSR-88D 'tropical' option)",
            "Many small drops: tropical / maritime convection, tropical cyclone rain bands.",
        ),
        ZRelation(
            "stratiform_east",
            "East-cool-season stratiform (WSR-88D option)",
            130.0,
            2.0,
            "rain",
            "WSR-88D Z-R option list, Fulton et al. 1998",
            "Cool-season stratiform rain of the eastern US; an operational option, not a fit.",
        ),
        ZRelation(
            "stratiform_west",
            "West-cool-season stratiform (WSR-88D option)",
            75.0,
            2.0,
            "rain",
            "WSR-88D Z-R option list, Fulton et al. 1998",
            "Orographic cool-season rain of the western US; an operational option, not a fit.",
        ),
        ZRelation(
            "snow_sekhon_srivastava",
            "Snow Z-S (Sekhon & Srivastava 1970)",
            1780.0,
            2.21,
            "snow",
            "Sekhon & Srivastava 1970, J. Atmos. Sci. 27, 299-307",
            "Liquid-equivalent snowfall rate S in mm/h; dry snow; uncertain by a factor 2 or more.",
        ),
        ZRelation(
            "snow_cool_stratiform",
            "Snow with the WSR-88D west-cool-stratiform pair",
            75.0,
            2.0,
            "snow",
            "WSR-88D Z-R option list, Fulton et al. 1998",
            "The operational cool-season pair applied to snow: a practical choice, not a snow fit.",
        ),
    )
}
DEFAULT_ZR = "wsr88d"
DEFAULT_SNOW = "snow_sekhon_srivastava"

#: Rain relations are fitted to liquid drops; in the melting layer and above they do not apply.
HYDROMETEOR_RAIN = ("drizzle", "rain", "light_and_moderate_rain", "heavy_rain", "big_drops")
HYDROMETEOR_MIXED = ("rain_hail_mixture", "hail", "big_hail", "large_hail")
HYDROMETEOR_GRAUPEL = ("graupel", "high_density_graupel", "low_density_graupel")
HYDROMETEOR_WET = ("wet_snow", "melting_hail", "melting_graupel")


def zr_relation(key: str | None, snow: bool = False) -> ZRelation:
    """The selected Z-R (or Z-S) relation; ``None`` gives the default of the precipitation type."""
    if key in (None, "", "auto"):
        key = DEFAULT_SNOW if snow else DEFAULT_ZR
    if key not in ZR_RELATIONS:
        raise QpeError(f"unknown Z-R relation {key!r}; choose one of {', '.join(ZR_RELATIONS)}")
    rel = ZR_RELATIONS[key]
    if snow and rel.kind != "snow":
        warnings.warn(f"{key!r} is a rain relation but precipitation is set to snow", stacklevel=2)
    if not snow and rel.kind == "snow":
        raise QpeError(f"{key!r} is a snow relation; set precip='snow' to use it")
    return rel


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class QpeConfig:
    """All options of the sweep estimator (also the dictionary the CLI/UI/API pass through)."""

    method: str = "auto"
    relation: str | None = None  # Z-R key (rain) or Z-S key (snow)
    zr_a: float | None = None  # custom Z = A R^b (overrides ``relation``)
    zr_b: float | None = None
    precip: str = "rain"  # "rain" | "snow"
    band: str = "S"
    z_cap: float | None = 53.0  # dBZ; hail contamination limit (Giangrande & Ryzhkov 2008)
    min_dbz: float = 5.0  # below this an observed echo is "no rain" (rate 0)
    min_rhohv: float = 0.80  # below this a gate is not weather (rate 0)
    zdr_floor: float = 0.0  # ZDR (dB) below this is raised to it before R(Z,ZDR)
    zdr_offset_db: float = 0.0  # calibration offset subtracted from ZDR
    z_offset_db: float = 0.0  # calibration offset subtracted from DBZH
    kdp_min: float = 0.3  # deg/km; R(KDP) is noise below this (radarx kdp docs)
    r_light: float = 6.0  # hybrid: below this R(Z) -> R(Z) only
    r_heavy: float = 50.0  # hybrid: above this R(Z) -> R(KDP) when KDP is usable
    use_hid: bool = True  # hybrid: gate by hydrometeor class
    hail_policy: str = (
        "kdp"  # "kdp": rain-hail/hail -> R(KDP) or capped R(Z); "exclude": no estimate
    )
    freezing_level_m: float | None = None  # m above sea level; None = unknown
    ml_thickness_m: float = 500.0
    ml_policy: str = "exclude"  # "exclude": no estimate in/above the ML; "flag": estimate, flagged
    attenuation: str = "auto"  # "auto": correct at C/X, flag only at S; "on"; "off"
    atten_flag_db: float = 1.0
    rain_temperature_c: float = 20.0
    sigma_z_db: float = 1.0  # 1-sigma calibration uncertainty of Z (dB)
    sigma_zdr_db: float = 0.2  # ... of ZDR (dB)
    sigma_kdp: float = 0.3  # deg/km, used when the KDP processing gives no uncertainty
    snow_sigma_ln: float = 0.7  # assumed 1-sigma of ln S for Z-S (not a fit)
    dem: Any = None  # xarray.DataArray of terrain height (m ASL) or None
    correct_blockage: bool = False
    block_flag: float = 0.1
    block_max: float = 0.5  # beams blocked more than this are not used by the compositor
    beamwidth_deg: float | None = None
    q_height_scale_m: float = 3000.0
    q_range_scale_km: float = 150.0

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> QpeConfig:
        d = dict(d or {})
        valid = set(cls.__dataclass_fields__)
        unknown = sorted(set(d) - valid)
        if unknown:
            raise QpeError(f"unknown QPE option(s): {', '.join(unknown)}")
        # None means "not given" (the default applies) except for z_cap, where None is "no cap"
        cfg = cls(**{k: v for k, v in d.items() if v is not None or k == "z_cap"})
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.method not in METHODS:
            raise QpeError(f"method must be one of {', '.join(METHODS)}, not {self.method!r}")
        if self.precip not in ("rain", "snow"):
            raise QpeError("precip must be 'rain' or 'snow'")
        if str(self.band).upper() not in ("S", "C", "X"):
            raise QpeError("band must be S, C or X")
        self.band = str(self.band).upper()
        if self.hail_policy not in ("kdp", "exclude"):
            raise QpeError("hail_policy must be 'kdp' or 'exclude'")
        if self.ml_policy not in ("exclude", "flag"):
            raise QpeError("ml_policy must be 'exclude' or 'flag'")
        if self.attenuation not in ("auto", "on", "off"):
            raise QpeError("attenuation must be 'auto', 'on' or 'off'")
        if (self.zr_a is None) != (self.zr_b is None):
            raise QpeError("give both zr_a and zr_b (Z = A R^b) or neither")
        if self.zr_a is not None and (self.zr_a <= 0 or self.zr_b <= 0):
            raise QpeError("zr_a and zr_b must be positive")
        if self.r_light >= self.r_heavy:
            raise QpeError("r_light must be smaller than r_heavy")
        self.zr()  # an unknown relation, or a rain relation for snow, is an error here

    def zr(self) -> ZRelation:
        if self.zr_a is not None:
            return ZRelation(
                "custom",
                "Custom Z-R",
                float(self.zr_a),
                float(self.zr_b),
                "snow" if self.precip == "snow" else "rain",
                "user supplied",
            )
        return zr_relation(self.relation, snow=self.precip == "snow")


# ---------------------------------------------------------------------------
# Beam geometry
# ---------------------------------------------------------------------------


def beam_geometry(
    range_m: np.ndarray, elevation_deg: float, radar_alt_m: float = 0.0, k_e: float = K_E
) -> tuple[np.ndarray, np.ndarray]:
    """Ground range ``s`` and beam-centre height ``h`` above the radar for slant range ``r``.

    Effective-Earth-radius model (Doviak & Zrnic 1993, Eq. 2.28): with ``R' = k_e a``,
    ``rho = sqrt(r^2 + R'^2 + 2 r R' sin(theta))``, ``h = rho - R'`` and
    ``s = R' arcsin(r cos(theta) / rho)``. ``radar_alt_m`` is only added to ``h`` to give
    the height above mean sea level (it does not change the path).
    """
    r = np.asarray(range_m, dtype=np.float64)
    th = math.radians(float(elevation_deg))
    rp = k_e * EARTH_RADIUS_M
    rho = np.sqrt(r * r + rp * rp + 2.0 * r * rp * math.sin(th))
    s = rp * np.arcsin(np.clip(r * math.cos(th) / rho, -1.0, 1.0))
    return s, rho - rp + float(radar_alt_m)


def beam_half_width_m(range_m: np.ndarray, beamwidth_deg: float) -> np.ndarray:
    """Half the vertical beam width in metres at slant range ``r`` (half-power width)."""
    return np.asarray(range_m, dtype=np.float64) * math.radians(beamwidth_deg) / 2.0


# ---------------------------------------------------------------------------
# DEM
# ---------------------------------------------------------------------------

_DEM_NAMES = ("elevation", "dem", "z", "height", "topo", "topography", "altitude", "Band1")


def load_dem(path: str, variable: str = "") -> xr.DataArray:
    """A terrain-height DataArray (m above sea level) from a local NetCDF file.

    Never downloads. For GeoTIFF/ASCII use the ``radarx_beam_blockage`` module's loader or convert
    with ``gdal_translate -of NetCDF dem.tif dem.nc``.
    """
    try:
        ds = xr.open_dataset(path)
    except Exception as exc:
        raise QpeError(
            f"cannot read the DEM {path!r}: {exc}. Give a NetCDF file (see docs)."
        ) from exc
    names = [variable] if variable else list(_DEM_NAMES)
    for n in names:
        if n in ds.data_vars:
            return ds[n].load()
    if len(ds.data_vars) == 1:
        return next(iter(ds.data_vars.values())).load()
    raise QpeError(
        f"no terrain variable in {path!r}; name it with dem_variable (variables: "
        f"{', '.join(map(str, ds.data_vars))})"
    )


# ---------------------------------------------------------------------------
# Sweep estimate
# ---------------------------------------------------------------------------


@dataclass
class SweepQpe:
    """Result of :func:`estimate_sweep` on the sweep's ``(azimuth, range)`` grid."""

    rate: np.ndarray  # float32 mm/h; 0 = no rain observed, NaN = no estimate
    sigma: np.ndarray  # float32 fractional 1-sigma (sigma of ln R), NaN without estimate
    flags: np.ndarray  # uint8 bit field (FLAG_*)
    relation: np.ndarray  # uint8 relation code (REL_*)
    quality: np.ndarray  # float32 (n_az, n_range) quality index 0..1
    azimuth: np.ndarray  # float64 degrees, ascending
    range_m: np.ndarray
    elevation_deg: float
    ground_range_m: np.ndarray  # (n_range,)
    beam_height_m: np.ndarray  # (n_range,) beam centre above the radar, m
    beam_height_agl_m: (
        np.ndarray
    )  # (n_az, n_range) above ground if a DEM was given else = above radar
    blockage: np.ndarray | None  # (n_az, n_range) CBB 0..1 or None without a DEM
    ray_time: np.ndarray | None  # datetime64 per ray
    meta: dict[str, Any] = field(default_factory=dict)
    order: np.ndarray | None = None  # ray order of the input: arrays here are sorted by azimuth

    def to_input_order(self, arr: np.ndarray) -> np.ndarray:
        """``arr`` (n_az, n_range) back in the ray order of the input sweep."""
        if self.order is None:
            return arr
        out = np.empty_like(arr)
        out[self.order] = arr
        return out

    @property
    def shape(self) -> tuple[int, int]:
        return self.rate.shape  # type: ignore[return-value]


def _arr(ds: xr.Dataset, name: str) -> np.ndarray | None:
    if name not in ds.data_vars:
        return None
    da = ds[name]
    ray = [d for d in da.dims if d != "range"]
    if len(ray) != 1 or "range" not in da.dims:
        return None
    return np.asarray(da.transpose(ray[0], "range").values, dtype=np.float64)


def _class_table(hid: xr.DataArray) -> dict[int, str]:
    meanings = str(hid.attrs.get("flag_meanings", "")).split()
    values = [int(v) for v in np.asarray(hid.attrs.get("flag_values", [])).reshape(-1)]
    return dict(zip(values, meanings, strict=False))


def _rx():
    try:
        import radarx.retrieve as rt
    except ImportError as exc:
        raise QpeError(
            "QPE needs the Python package radarx for polarimetric relations "
            "(pip install -U radarx, Python >= 3.12)"
        ) from exc
    return rt


def relation_scatter(band: str) -> dict[str, float]:
    """DSD-driven scatter (sigma of ln R) of each relation (``radarx.retrieve.relation_scatter``).

    The values come from the prior DSD ensemble of radarx (a broad generic prior around the
    Bringi et al. 2003 statistics); they describe how far the rain rate of a drop size distribution
    can fall from the power law that is fitted to the whole ensemble.
    """
    cache = _SCATTER.get(band)
    if cache is None:
        rt = _rx()
        try:
            ds = rt.relation_scatter(band=band)
            cache = {
                "z": float(ds["ln_sd"].sel(relation="z")),
                "kdp": float(ds["ln_sd"].sel(relation="kdp")),
                "z_zdr": float(ds["ln_sd"].sel(relation="z_zdr")),
                "a": float(ds["ln_sd"].sel(relation="a")),
                "alpha": float(ds["alpha_ln_sd"]),
            }
        except Exception:
            cache = {"z": 0.45, "kdp": 0.27, "z_zdr": 0.10, "a": 0.13, "alpha": 0.23}
            cache["fallback"] = 1.0
        _SCATTER[band] = cache
    return cache


_SCATTER: dict[str, dict[str, float]] = {}


def estimate_sweep(
    ds: xr.Dataset,
    cfg: QpeConfig | dict | None = None,
    *,
    site: dict[str, float] | None = None,
) -> SweepQpe:
    """Rain rate, uncertainty and quality of one sweep ``Dataset`` (canonical variable names).

    Parameters
    ----------
    ds : xarray.Dataset
        A PPI sweep on ``(azimuth, range)`` with ``DBZH`` and optionally ``ZDR``, ``RHOHV``,
        ``PHIDP``, ``KDP``. No-echo gates must be NaN (the engine's reader does this). The
        radar site (``latitude``, ``longitude``, ``altitude``) is taken from the dataset
        coordinates or from ``site``.
    cfg : QpeConfig or dict
        See :class:`QpeConfig`.

    Notes
    -----
    ``method="hybrid"`` decides gate by gate (Giangrande & Ryzhkov 2008 for the class gating,
    Ryzhkov et al. 2005 for the rate regimes; the thresholds are parameters):

    1. gates with RhoHV < ``min_rhohv`` are not weather (rate 0, flagged);
    2. rain classes: R(Z) if R(Z) < ``r_light``; R(Z,ZDR) between; R(KDP) above ``r_heavy`` when
       KDP >= ``kdp_min`` (else R(Z,ZDR));
    3. rain-hail mixture / hail: R(KDP) if usable, else R(Z) with Z capped at ``z_cap``
       (``hail_policy="kdp"``), or no estimate (``"exclude"``);
    4. wet snow, ice, snow, unclassified echo: no estimate;
    5. with a known freezing level, beams inside the melting layer or above it give no
       estimate (``ml_policy``).
    """
    cfg = cfg if isinstance(cfg, QpeConfig) else QpeConfig.from_dict(cfg)  # type: ignore[arg-type]
    cfg.validate()
    rt = None
    dbz = _arr(ds, "DBZH")
    if dbz is None:
        raise QpeError("no reflectivity (DBZH) on this sweep")
    if "azimuth" not in ds.coords or "range" not in ds.coords:
        raise QpeError("this sweep has no azimuth/range coordinates (not a polar PPI)")
    # rays ascending in azimuth
    ray_dim = next(d for d in ds["DBZH"].dims if d != "range")
    az = np.asarray(ds["azimuth"].values, dtype=np.float64) % 360.0
    order = np.argsort(az, kind="stable")
    ds = ds.isel({ray_dim: order}) if not np.array_equal(order, np.arange(order.size)) else ds
    az = az[order]
    dbz = _arr(ds, "DBZH")
    rng = np.asarray(ds["range"].values, dtype=np.float64)
    zdr = _arr(ds, "ZDR")
    rhohv = _arr(ds, "RHOHV")
    phidp = _arr(ds, "PHIDP")
    kdp = _arr(ds, "KDP")
    snr = _arr(ds, "SNRH")
    naz, nr = dbz.shape

    s_info = site or {}
    lat = float(s_info.get("latitude", _scalar(ds, "latitude", math.nan)))
    lon = float(s_info.get("longitude", _scalar(ds, "longitude", math.nan)))
    alt = float(s_info.get("altitude", _scalar(ds, "altitude", 0.0)))
    elev = _elevation(ds)
    bw = cfg.beamwidth_deg or _scalar(ds, "radar_beam_width_h", 0.0) or 1.0
    if not (0.1 <= bw <= 5.0):
        bw = 1.0

    method = cfg.method
    notes: list[str] = []
    snow = cfg.precip == "snow"
    if method == "auto":
        if snow:
            method = "z"
        elif zdr is not None and (kdp is not None or phidp is not None):
            method = "hybrid"
        elif zdr is not None:
            method = "z_zdr"
        else:
            method = "z"
        notes.append(f"Method chosen automatically: {method}.")
    if snow and method != "z":
        raise QpeError("precip='snow' supports only the Z-S relation (method 'z' or 'auto')")
    need = {"z": [], "z_zdr": ["ZDR"], "kdp": [], "a": [], "hybrid": []}[method]
    if "ZDR" in need and zdr is None:
        raise QpeError(f"method {method!r} needs ZDR, which this sweep does not have")
    if method in ("kdp", "a") and kdp is None and phidp is None:
        raise QpeError(f"method {method!r} needs KDP or PHIDP, which this sweep does not have")

    # ---- calibration offsets ---------------------------------------------------------------
    if cfg.z_offset_db:
        dbz = dbz - cfg.z_offset_db
    if zdr is not None and cfg.zdr_offset_db:
        zdr = zdr - cfg.zdr_offset_db

    # ---- geometry, DEM ---------------------------------------------------------------------
    s_gate, h_asl = beam_geometry(rng, elev, alt)
    h_arl = h_asl - alt
    cbb = None
    h_agl = np.broadcast_to(h_arl[None, :], (naz, nr)).astype(np.float32)
    if cfg.dem is not None:
        rt = rt or _rx()
        ds_geo = ds.assign_coords(latitude=lat, longitude=lon, altitude=alt)
        bb = rt.beam_blockage(ds_geo[["DBZH"]], cfg.dem, beamwidth=bw)
        cbb = np.nan_to_num(_arr(bb, "CBB"), nan=0.0).astype(np.float32)
        terr = _arr(bb, "TERRAIN_HEIGHT")
        if terr is not None:
            h_agl = (h_asl[None, :] - np.where(np.isfinite(terr), terr, alt)).astype(np.float32)
        notes.append("Beam blockage and beam height above ground from the supplied DEM.")
    else:
        notes.append(
            "No DEM: beam height is above the radar (equals height above ground only on flat "
            "terrain) and beam blockage is not evaluated."
        )

    # ---- echo mask ---------------------------------------------------------------------------
    obs = np.isfinite(dbz)
    nonmet = np.zeros_like(obs)
    if rhohv is not None:
        nonmet = obs & np.isfinite(rhohv) & (rhohv < cfg.min_rhohv)
    echo = obs & ~nonmet & (dbz >= cfg.min_dbz)

    flags = np.zeros((naz, nr), dtype=np.uint8)
    flags[nonmet] |= FLAG_NONMET

    # ---- phase, KDP, attenuation -----------------------------------------------------------
    kdp_sigma = None
    a_spec = None
    pia = None
    dbz_c, zdr_c = dbz, zdr
    want_kdp = method in ("kdp", "a", "hybrid") or cfg.attenuation != "off"
    if (
        want_kdp
        and phidp is not None
        and rhohv is not None
        and (kdp is None or cfg.attenuation != "off")
    ):
        rt = rt or _rx()
        try:
            ds_in = ds.assign({"DBZH": ((ray_dim, "range"), dbz)})
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                k = rt.estimate_kdp(ds_in, phidp="PHIDP", rhohv="RHOHV", dbz="DBZH", band=cfg.band)
            if kdp is None:
                kdp = _arr(k, "KDP")
                notes.append("KDP estimated from PHIDP (radarx.retrieve.estimate_kdp).")
            kdp_sigma = _arr(k, "KDP_UNCERTAINTY")
            if cfg.attenuation != "off" or method == "a":
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    att = rt.correct_attenuation(
                        ds_in.assign(
                            PHIDP=k["PHIDP_processed"],
                            **({"ZDR": ds["ZDR"]} if zdr is not None else {}),
                        ),
                        dbz="DBZH",
                        zdr="ZDR" if zdr is not None else None,
                        phidp="PHIDP",
                        rhohv="RHOHV",
                        band=cfg.band,
                    )
                pia = _arr(att, "PIA")
                a_spec = _arr(att, "AH")
                apply_corr = cfg.attenuation == "on" or (
                    cfg.attenuation == "auto" and cfg.band != "S"
                )
                if apply_corr:
                    dbz_c = _arr(att, "DBZH_corrected")
                    zdr_c = _arr(att, "ZDR_corrected") if zdr is not None else None
                    notes.append("Reflectivity and ZDR corrected for attenuation (ZPHI, radarx).")
        except Exception as exc:
            notes.append(f"KDP / attenuation processing skipped: {exc}")
    if pia is not None:
        hi_att = echo & np.isfinite(pia) & (pia >= cfg.atten_flag_db)
        flags[hi_att] |= FLAG_ATTEN
    if kdp is None and method in ("kdp", "a", "hybrid"):
        if method != "hybrid":
            raise QpeError(
                f"method {method!r} needs KDP, and it could not be estimated (need PHIDP and RHOHV)"
            )
        notes.append("No KDP: the hybrid uses R(Z) and R(Z,ZDR) only.")

    # beam blockage correction of Z (Fulton et al. 1998: -10 log10(1 - CBB)), moderate cases only
    if cbb is not None:
        flags[echo & (cbb >= cfg.block_flag)] |= FLAG_BLOCK
        if cfg.correct_blockage:
            frac = np.clip(cbb, 0.0, min(cfg.block_max, 0.5))
            dbz_c = dbz_c - 10.0 * np.log10(1.0 - frac)
            notes.append("Reflectivity corrected for partial beam blockage (CBB <= 0.5).")

    # ---- the relations -----------------------------------------------------------------------
    zr = cfg.zr()
    rt = rt or _rx()
    rd = xr.Dataset({"DBZH": (("a", "r"), np.where(np.isfinite(dbz_c), dbz_c, np.nan))})
    if zdr_c is not None:
        rd["ZDR"] = (("a", "r"), zdr_c)
    if kdp is not None:
        rd["KDP"] = (("a", "r"), kdp)
    z_cap = None if snow else cfg.z_cap
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        r_z = np.asarray(
            rt.rain_rate_z(
                rd, "DBZH", band=cfg.band, coefficients=(zr.a, zr.b), z_cap=z_cap
            ).values,
            dtype=np.float64,
        )
        r_zdr = None
        if zdr_c is not None and not snow:
            r_zdr = np.asarray(
                rt.rain_rate_z_zdr(
                    rd, "DBZH", "ZDR", band=cfg.band, z_cap=cfg.z_cap, min_zdr=cfg.zdr_floor
                ).values,
                dtype=np.float64,
            )
        r_kdp = None
        if kdp is not None and not snow:
            r_kdp = np.asarray(
                rt.rain_rate_kdp(rd, "KDP", band=cfg.band, negative="zero").values, dtype=np.float64
            )
        r_a = None
        if method == "a" and not snow:
            a_field = a_spec
            if a_field is None and kdp is not None:
                alpha = rt.attenuation_coefficients(cfg.band)["alpha"]
                a_field = alpha * np.where(np.isfinite(kdp), np.maximum(kdp, 0.0), np.nan)
                notes.append(f"A = alpha KDP with alpha = {alpha:g} dB/deg (Bringi et al. 1990).")
            if a_field is None:
                raise QpeError("R(A) needs KDP/PHIDP to estimate the specific attenuation")
            rd["AH"] = (("a", "r"), a_field)
            r_a = np.asarray(
                rt.rain_rate_a(
                    rd, "AH", band=cfg.band, temperature=cfg.rain_temperature_c, negative="zero"
                ).values,
                dtype=np.float64,
            )

    # ---- freezing level / melting layer -----------------------------------------------------
    ml_cls = np.zeros((nr,), dtype=np.int8)  # 0 below, 1 in ML, 2 above
    if cfg.freezing_level_m is not None and not snow:
        fl = float(cfg.freezing_level_m)
        half = beam_half_width_m(rng, bw)
        bottom, top = h_asl - half, h_asl + half
        ml_bot, ml_top = fl - cfg.ml_thickness_m, fl
        ml_cls = np.where(bottom >= ml_top, 2, np.where(top > ml_bot, 1, 0)).astype(np.int8)
        notes.append(
            f"Freezing level {fl:g} m ASL; melting layer {ml_bot:g}-{ml_top:g} m "
            f"(beam half-width included)."
        )
    ml2 = np.broadcast_to(ml_cls[None, :], (naz, nr))

    # ---- choose the relation per gate ---------------------------------------------------------
    rate = np.full((naz, nr), np.nan)
    relcode = np.zeros((naz, nr), dtype=np.uint8)
    classes = None
    if snow:
        sel = echo
        rate[sel] = r_z[sel]
        relcode[sel] = REL_SNOW
    elif method == "z":
        sel = echo
        rate[sel] = r_z[sel]
        relcode[sel] = REL_Z
    elif method == "z_zdr":
        sel = echo & np.isfinite(r_zdr)
        rate[sel] = r_zdr[sel]
        relcode[sel] = REL_ZDR
    elif method == "kdp":
        sel = echo & np.isfinite(r_kdp)
        rate[sel] = r_kdp[sel]
        relcode[sel] = REL_KDP
    elif method == "a":
        sel = echo & np.isfinite(r_a)
        rate[sel] = r_a[sel]
        relcode[sel] = REL_A
    else:
        rate, relcode, classes = _hybrid(
            cfg,
            rt,
            ds,
            echo,
            nonmet,
            dbz_c,
            zdr_c,
            kdp,
            rhohv,
            r_z,
            r_zdr,
            r_kdp,
            ml2,
            flags,
            notes,
        )
    # ML gating also for the single relations
    if cfg.freezing_level_m is not None and not snow:
        bad = echo & (ml2 >= 1)
        flags[bad] |= FLAG_ML
        if cfg.ml_policy == "exclude":
            rate[bad] = np.nan
            relcode[bad] = REL_NONE
    # gates that carry an echo but got no estimate
    noest = echo & ~np.isfinite(rate)
    flags[noest & ((flags & FLAG_ML) == 0)] |= FLAG_NOEST
    if z_cap is not None:
        flags[echo & (dbz_c > z_cap) & np.isin(relcode, (REL_Z, REL_ZDR))] |= FLAG_ZCAP
    # no echo (NaN reflectivity is how the reader marks below-threshold gates), echo below
    # ``min_dbz`` and non-weather gates: the rate is 0, not "unknown"
    zero = ~echo
    rate = np.where(zero, 0.0, rate)
    relcode = np.where(zero, REL_NONE, relcode).astype(np.uint8)

    # ---- uncertainty (sigma of ln R) ---------------------------------------------------------
    sc = relation_scatter(cfg.band)
    ln10_10 = math.log(10.0) / 10.0
    sigma = np.full((naz, nr), np.nan)
    b_z = zr.b
    s_z = np.sqrt(sc["z"] ** 2 + (b_z * ln10_10 * cfg.sigma_z_db) ** 2)
    if snow:
        s_z = np.sqrt(cfg.snow_sigma_ln**2 + (b_z * ln10_10 * cfg.sigma_z_db) ** 2)
    sigma = np.where(relcode == REL_Z, s_z, sigma)
    sigma = np.where(relcode == REL_SNOW, s_z, sigma)
    s_zzdr = math.sqrt(
        sc["z_zdr"] ** 2
        + (_coef_z_zdr(rt, cfg.band)[1] * ln10_10 * cfg.sigma_z_db) ** 2
        + (_coef_z_zdr(rt, cfg.band)[2] * ln10_10 * cfg.sigma_zdr_db) ** 2
    )
    sigma = np.where(relcode == REL_ZDR, s_zzdr, sigma)
    if kdp is not None:
        ksig = kdp_sigma if kdp_sigma is not None else np.full_like(kdp, cfg.sigma_kdp)
        with np.errstate(invalid="ignore", divide="ignore"):
            rel_k = _KDP_EXP[cfg.band] * np.clip(np.abs(ksig / np.maximum(kdp, 1e-3)), 0.0, 1.5)
        s_kdp = np.sqrt(sc["kdp"] ** 2 + rel_k**2)
        sigma = np.where(relcode == REL_KDP, s_kdp, sigma)
        s_a = np.sqrt(sc["a"] ** 2 + sc["alpha"] ** 2 + rel_k**2)
        sigma = np.where(relcode == REL_A, s_a, sigma)
    sigma = np.where(np.isfinite(rate) & (rate > 0), sigma, np.nan).astype(np.float32)

    # ---- quality index -----------------------------------------------------------------------
    q_h = np.exp(-np.maximum(h_agl, 0.0) / cfg.q_height_scale_m)
    q_r = np.exp(-(((s_gate / 1000.0) / cfg.q_range_scale_km) ** 2))[None, :]
    quality = q_h * q_r
    if cbb is not None:
        quality = quality * (1.0 - np.clip(cbb, 0.0, 1.0))
    quality = quality.astype(np.float32)

    ray_time = None
    if "time" in ds.coords or "time" in ds.variables:
        t = np.asarray(ds["time"].values)
        if t.ndim == 1 and t.size == naz and np.issubdtype(t.dtype, np.datetime64):
            ray_time = t
    meta = {
        "method": method,
        "relation": _relation_text(method, zr, cfg, snow),
        "band": cfg.band,
        "precip": cfg.precip,
        "elevation_deg": elev,
        "beamwidth_deg": bw,
        "site": {"latitude": lat, "longitude": lon, "altitude_m": alt},
        "notes": notes,
        "gate_counts": {
            REL_NAMES[c]: int((relcode == c).sum())
            for c in (REL_Z, REL_ZDR, REL_KDP, REL_A, REL_SNOW)
        },
        "hid_used": classes is not None,
        "snr_available": snr is not None,
        "radarx": provenance.radarx_version(),
    }
    return SweepQpe(
        rate=rate.astype(np.float32),
        sigma=sigma,
        flags=flags,
        relation=relcode,
        quality=quality,
        azimuth=az,
        range_m=rng,
        elevation_deg=elev,
        ground_range_m=s_gate,
        beam_height_m=h_arl,
        beam_height_agl_m=h_agl,
        blockage=cbb,
        ray_time=ray_time,
        order=order,
        meta=meta,
    )


_KDP_EXP = {"S": 0.822, "C": 0.85, "X": 0.801}


def _coef_z_zdr(rt, band: str) -> tuple[float, float, float]:
    from radarx.retrieve import qpe as q

    return q.COEFFICIENTS[band]["z_zdr"]


def _scalar(ds: xr.Dataset, name: str, default: float) -> float:
    for src in (ds.coords, ds.data_vars, ds.attrs):
        if name in src:
            try:
                v = float(np.asarray(getattr(src[name], "values", src[name])).ravel()[0])
            except (TypeError, ValueError, IndexError):
                continue
            if math.isfinite(v):
                return v
    return default


def _elevation(ds: xr.Dataset) -> float:
    if "elevation" in ds.coords or "elevation" in ds.variables:
        el = np.asarray(ds["elevation"].values, dtype=np.float64)
        if el.size and np.isfinite(el).any():
            return float(np.nanmean(el))
    fa = _scalar(ds, "sweep_fixed_angle", math.nan)
    if math.isfinite(fa):
        return fa
    raise QpeError("this sweep has no elevation angle")


def _relation_text(method: str, zr: ZRelation, cfg: QpeConfig, snow: bool) -> str:
    from radarx.retrieve import qpe as q

    c = q.COEFFICIENTS.get(cfg.band, {})
    parts = {
        "z": f"R(Z): {zr.text()}"
        + ("" if snow or cfg.z_cap is None else f", Z capped at {cfg.z_cap:g} dBZ"),
        "z_zdr": (
            "R(Z,ZDR) = {:g} Z^{:g} zdr^{:g}".format(*c["z_zdr"]) if "z_zdr" in c else "R(Z,ZDR)"
        ),
        "kdp": f"R(KDP) = {c['kdp'][0]:g} |KDP|^{c['kdp'][1]:g}" if "kdp" in c else "R(KDP)",
        "a": "R(A) = a(T) A^b(T), Ryzhkov et al. 2014 Table 1",
    }
    if method == "hybrid":
        return (
            f"hybrid: {zr.text()} if R(Z) < {cfg.r_light:g} mm/h; "
            f"{parts['z_zdr']} to {cfg.r_heavy:g} mm/h; {parts['kdp']} above (KDP >= "
            f"{cfg.kdp_min:g} deg/km); class gated by radarx HID"
        )
    return parts[method]


def _hybrid(cfg, rt, ds, echo, nonmet, dbz, zdr, kdp, rhohv, r_z, r_zdr, r_kdp, ml2, flags, notes):
    """The gate-by-gate hybrid; returns ``(rate, relation_code, class_names_or_None)``."""
    naz, nr = echo.shape
    classes = None
    cls_name = None
    if cfg.use_hid and zdr is not None and rhohv is not None:
        try:
            hid_in = xr.Dataset(
                {
                    "DBZH": (("a", "r"), dbz),
                    "ZDR": (("a", "r"), zdr),
                    "RHOHV": (("a", "r"), rhohv),
                }
            )
            kw: dict[str, Any] = {}
            if kdp is not None:
                hid_in["KDP"] = (("a", "r"), kdp)
                kw["kdp"] = "KDP"
            mask = xr.DataArray(echo, dims=("a", "r"))
            if cfg.freezing_level_m is not None:
                kw["melting_layer"] = (
                    max(float(cfg.freezing_level_m) - cfg.ml_thickness_m, 0.0),
                    float(cfg.freezing_level_m),
                )
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                h = rt.hid(
                    hid_in,
                    band=cfg.band,
                    dbz="DBZH",
                    zdr="ZDR",
                    rhohv="RHOHV",
                    mask=mask,
                    scores=False,
                    quality=False,
                    **kw,
                )["HID"]
            table = _class_table(h)
            cls_name = np.nan_to_num(np.asarray(h.values), nan=0).astype(np.int32)
            classes = table
            notes.append(
                "Hydrometeor classes from radarx.retrieve.hid (Park et al. 2009 at S band)."
            )
        except Exception as exc:
            notes.append(
                f"Hydrometeor classification skipped ({exc}); the hybrid uses the rate regime only."
            )
            cls_name = None
    rate = np.full((naz, nr), np.nan)
    relcode = np.zeros((naz, nr), dtype=np.uint8)

    z_ok = np.isfinite(r_z)
    no = np.zeros_like(echo)
    zdr_ok = echo & np.isfinite(r_zdr) if r_zdr is not None else no
    kdp_ok = (
        echo & (np.nan_to_num(kdp, nan=-1.0) >= cfg.kdp_min)
        if (r_kdp is not None and kdp is not None)
        else no
    )

    # regime choice for rain: based on R(Z)
    light = echo & z_ok & (r_z < cfg.r_light)
    heavy = echo & z_ok & (r_z >= cfg.r_heavy)
    mid = echo & z_ok & ~light & ~heavy
    rain_rate = np.where(light, r_z, np.nan)
    rain_rel = np.where(light, REL_Z, REL_NONE)
    if r_zdr is not None:
        use = mid & zdr_ok
        rain_rate = np.where(use, r_zdr, rain_rate)
        rain_rel = np.where(use, REL_ZDR, rain_rel)
    use = (mid | heavy) & np.isnan(rain_rate)
    rain_rate = np.where(use, r_z, rain_rate)
    rain_rel = np.where(use, REL_Z, rain_rel)
    if r_zdr is not None:
        use = heavy & zdr_ok
        rain_rate = np.where(use, r_zdr, rain_rate)
        rain_rel = np.where(use, REL_ZDR, rain_rel)
    if r_kdp is not None:
        use = heavy & kdp_ok
        rain_rate = np.where(use, r_kdp, rain_rate)
        rain_rel = np.where(use, REL_KDP, rain_rel)

    # mixed-phase / hail: R(KDP) when usable else capped R(Z)
    mixed_rate = np.where(kdp_ok, r_kdp, r_z) if r_kdp is not None else r_z.copy()
    mixed_rel = (
        np.where(kdp_ok, REL_KDP, REL_Z) if r_kdp is not None else np.full(echo.shape, REL_Z)
    )

    if cls_name is None:
        sel = echo
        rate = np.where(sel, rain_rate, np.nan)
        relcode = np.where(sel & np.isfinite(rate), rain_rel, 0).astype(np.uint8)
        return rate, relcode, None

    def in_group(group: tuple[str, ...]) -> np.ndarray:
        lut = np.zeros(max([*classes, 0]) + 2, dtype=bool)
        for code, name in classes.items():
            if name in group and code >= 0:
                lut[code] = True
        return lut[np.clip(cls_name, 0, lut.size - 1)]

    is_rain = in_group(HYDROMETEOR_RAIN)
    is_mixed = in_group(HYDROMETEOR_MIXED)
    is_graupel = in_group(HYDROMETEOR_GRAUPEL)
    is_wet = in_group(HYDROMETEOR_WET)
    below_ml = (ml2 == 0) if cfg.freezing_level_m is not None else np.zeros_like(echo)
    rate = np.where(echo & is_rain, rain_rate, rate)
    relcode = np.where(echo & is_rain & np.isfinite(rain_rate), rain_rel, relcode)
    if cfg.hail_policy == "kdp":
        mix = echo & (is_mixed | (is_graupel & below_ml))
        rate = np.where(mix, mixed_rate, rate)
        relcode = np.where(mix & np.isfinite(mixed_rate), mixed_rel, relcode)
        flags[mix] |= FLAG_HAIL
    else:
        flags[echo & (is_mixed | is_graupel)] |= FLAG_HAIL
    flags[echo & (is_wet | is_graupel & ~below_ml)] |= FLAG_ML
    # ice / dry snow / unclassified: flagged as such
    ice = echo & ~is_rain & ~is_mixed & ~is_graupel & ~is_wet & (cls_name > 0)
    flags[ice] |= FLAG_ML
    return rate, relcode.astype(np.uint8), classes
