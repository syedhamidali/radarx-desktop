"""Radar meteorological retrievals and derived products.

Pure-numpy implementations of common radar retrievals. Each function accepts
an xarray DataArray and returns an xarray DataArray with proper metadata.
"""

from __future__ import annotations

import logging
import warnings
from typing import Any

import numpy as np
import xarray as xr

from engine import varmap
from engine._lazy import LazyModule
from engine.reader import mask_below_threshold, sorted_sweep_names

ndimage = LazyModule("scipy.ndimage")  # imported on first use (~150 ms)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# KDP estimation
# ---------------------------------------------------------------------------


def estimate_kdp(
    phidp: xr.DataArray,
    *,
    gate_spacing_m: float | None = None,
    window_len: int = 21,
) -> xr.DataArray:
    """Estimate specific differential phase (KDP) from PhiDP.

    Uses the NaN-aware least-squares slope of PhiDP over a sliding window of
    range gates (equivalent to a centred finite difference of a moving-average
    smoothed PhiDP for linear data, but robust at ray ends and around gaps).

    Parameters
    ----------
    phidp : xr.DataArray
        Differential phase (degrees), shape ``(azimuth, range)``.
    gate_spacing_m : float, optional
        Gate spacing in metres. If *None*, inferred from the ``range``
        coordinate.
    window_len : int
        Number of range gates in the smoothing window (must be odd).

    Returns
    -------
    xr.DataArray
        KDP in degrees per kilometre, same shape as *phidp*.
    """
    phi = phidp.values.astype(np.float64)  # astype always copies
    mask = np.isnan(phi)
    range_axis = phidp.get_axis_num("range") if "range" in phidp.dims else phidp.ndim - 1

    # Infer gate spacing
    if gate_spacing_m is None:
        spacing = None
        if "range" in phidp.coords and phidp.coords["range"].size > 1:
            spacing = abs(float(np.median(np.diff(phidp.coords["range"].values))))
        if spacing is not None and np.isfinite(spacing) and spacing > 0:
            gate_spacing_m = spacing
        else:
            gate_spacing_m = 250.0
            logger.warning("No usable range coord; assuming gate_spacing=%.0f m", gate_spacing_m)

    window_len = int(window_len)
    if window_len < 3:
        window_len = 3
    if window_len % 2 == 0:
        window_len += 1

    # NaN-aware local least-squares slope of PhiDP along range over a window of
    # `window_len` gates.  Only valid gates enter the sums, so ray ends and gaps
    # do not bias the result (zero-filling NaNs / zero-padding the ends did), and
    # for a linear PhiDP it equals the centred finite difference of a moving
    # average while using every sample in the window (less noisy).
    n_gates = phi.shape[range_axis]
    shape = [1] * phi.ndim
    shape[range_axis] = n_gates
    x = np.arange(n_gates, dtype=np.float64).reshape(shape)
    valid = (~mask).astype(np.float64)
    phi[mask] = 0.0
    kw = {"size": window_len, "axis": range_axis, "mode": "constant", "cval": 0.0}
    n = ndimage.uniform_filter1d(valid, **kw)
    sx = ndimage.uniform_filter1d(valid * x, **kw)
    sy = ndimage.uniform_filter1d(phi, **kw)
    sxy = ndimage.uniform_filter1d(phi * x, **kw)
    sxx = ndimage.uniform_filter1d(valid * x * x, **kw)
    with np.errstate(invalid="ignore", divide="ignore"):
        cov = sxy - sx * sy / n
        var = sxx - sx * sx / n
        ok = (n * window_len >= 2) & (var > 1e-4)  # >= 2 distinct valid gates
        slope = np.where(ok, cov / np.where(ok, var, 1.0), np.nan)  # deg per gate

    # KDP = 0.5 * dPhiDP/dr: factor 0.5 because PhiDP is the two-way differential
    # phase; deg/m -> deg/km via 1000.
    kdp = slope / gate_spacing_m * 0.5 * 1000.0

    # Restore NaN mask
    kdp[mask] = np.nan

    result = xr.DataArray(
        kdp.astype(np.float32),
        dims=phidp.dims,
        coords=phidp.coords,
        attrs={
            "units": "degrees/km",
            "long_name": "Specific Differential Phase",
            "standard_name": "specific_differential_phase_hv",
        },
    )
    return result


# ---------------------------------------------------------------------------
# Rainfall rate estimators
# ---------------------------------------------------------------------------


def rain_rate_z(
    dbz: xr.DataArray,
    *,
    a: float = 200.0,
    b: float = 1.6,
) -> xr.DataArray:
    """Estimate rainfall rate from reflectivity using Z = a * R^b.

    Default coefficients are Marshall-Palmer (a=200, b=1.6).

    Parameters
    ----------
    dbz : xr.DataArray
        Reflectivity in dBZ.
    a, b : float
        Z-R relationship coefficients.

    Returns
    -------
    xr.DataArray
        Rainfall rate in mm/h.
    """
    if not (a > 0 and b > 0):
        raise ValueError(f"Z-R coefficients must be positive, got a={a}, b={b}")
    dbz_vals = dbz.values.astype(np.float64)
    z_linear = 10.0 ** (dbz_vals / 10.0)
    rain = (z_linear / a) ** (1.0 / b)
    rain = np.where(np.isnan(dbz_vals), np.nan, rain)
    rain = np.clip(rain, 0.0, 500.0)

    return xr.DataArray(
        rain.astype(np.float32),
        dims=dbz.dims,
        coords=dbz.coords,
        attrs={
            "units": "mm/h",
            "long_name": "Rainfall Rate (Z-R)",
            "method": f"Z = {a} * R^{b}",
        },
    )


def rain_rate_kdp(
    kdp: xr.DataArray,
    *,
    a: float = 44.0,
    b: float = 0.822,
) -> xr.DataArray:
    """Estimate rainfall rate from KDP using R = a * KDP^b.

    Default coefficients follow Bringi and Chandrasekar (2001).

    Parameters
    ----------
    kdp : xr.DataArray
        Specific differential phase in deg/km.
    a, b : float
        R-KDP relationship coefficients.

    Returns
    -------
    xr.DataArray
        Rainfall rate in mm/h.
    """
    k = kdp.values.astype(np.float64)
    positive = k > 0  # False for NaN and negative KDP
    rain = np.zeros_like(k)
    rain[positive] = a * np.power(k[positive], b)
    rain = np.where(np.isnan(k), np.nan, rain)
    rain = np.clip(rain, 0.0, 500.0)

    return xr.DataArray(
        rain.astype(np.float32),
        dims=kdp.dims,
        coords=kdp.coords,
        attrs={
            "units": "mm/h",
            "long_name": "Rainfall Rate (R-KDP)",
            "method": f"R = {a} * KDP^{b}",
        },
    )


def rain_rate_z_zdr(
    dbz: xr.DataArray,
    zdr: xr.DataArray,
    *,
    a: float = 6.7e-3,
    b: float = 0.927,
    c: float = -3.43,
) -> xr.DataArray:
    """Estimate rainfall rate from Z and ZDR.

    R = a * Z_linear^b * ZDR_linear^c, with ZDR_linear = 10^(ZDR_dB / 10),
    i.e. R = a * Z_linear^b * 10^(0.1 * c * ZDR_dB).

    Parameters
    ----------
    dbz : xr.DataArray
        Reflectivity in dBZ.
    zdr : xr.DataArray
        Differential reflectivity in dB.
    a, b, c : float
        Empirical coefficients (Bringi & Chandrasekar: ZDR exponent applies
        to the *linear* ratio).

    Returns
    -------
    xr.DataArray
        Rainfall rate in mm/h.
    """
    z_lin = 10.0 ** (dbz.values.astype(np.float64) / 10.0)
    zdr_val = zdr.values.astype(np.float64)
    rain = a * np.power(z_lin, b) * np.power(10.0, 0.1 * c * zdr_val)
    combined_mask = np.isnan(dbz.values) | np.isnan(zdr.values)
    rain = np.where(combined_mask, np.nan, rain)
    rain = np.clip(rain, 0.0, 500.0)

    return xr.DataArray(
        rain.astype(np.float32),
        dims=dbz.dims,
        coords=dbz.coords,
        attrs={
            "units": "mm/h",
            "long_name": "Rainfall Rate (Z-ZDR)",
            "method": f"R = {a} * Z^{b} * ZDR^{c} (Z, ZDR linear)",
        },
    )


# ---------------------------------------------------------------------------
# Attenuation correction
# ---------------------------------------------------------------------------


def correct_attenuation_phidp(
    dbz: xr.DataArray,
    phidp: xr.DataArray,
    *,
    alpha: float = 0.08,
) -> xr.DataArray:
    """Apply simple path-integrated attenuation (PIA) correction using PhiDP.

    PIA(r) = alpha * (PhiDP(r) - PhiDP_offset), made non-decreasing with
    range; gates with missing PhiDP inherit the PIA accumulated so far. The
    corrected reflectivity is Z_corr = Z + PIA.

    Parameters
    ----------
    dbz : xr.DataArray
        Reflectivity in dBZ.
    phidp : xr.DataArray
        Differential phase in degrees.
    alpha : float
        Specific attenuation coefficient (dB per degree of PhiDP).

    Returns
    -------
    xr.DataArray
        Attenuation-corrected reflectivity in dBZ.
    """
    phi_vals = phidp.values.astype(np.float64)
    z_vals = dbz.values.astype(np.float64)

    # PIA is driven by the *propagation* phase accumulated since the radar, so
    # remove the per-ray system offset (median of the first valid gates).
    valid = np.isfinite(phi_vals)
    first = valid & (np.cumsum(valid, axis=-1) <= 5)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN rays
        offset = np.nanmedian(np.where(first, phi_vals, np.nan), axis=-1, keepdims=True)
    offset = np.where(np.isfinite(offset), offset, 0.0)

    pia = alpha * (phi_vals - offset)
    pia = np.where(valid, np.clip(pia, 0.0, None), -np.inf)  # PIA is never negative
    # PIA is cumulative: it cannot decrease with range, and gates with missing
    # PhiDP inherit the attenuation accumulated so far instead of losing Z.
    pia = np.maximum.accumulate(pia, axis=-1)
    pia = np.where(np.isfinite(pia), pia, 0.0)

    z_corrected = np.where(np.isnan(z_vals), np.nan, z_vals + pia)

    return xr.DataArray(
        z_corrected.astype(np.float32),
        dims=dbz.dims,
        coords=dbz.coords,
        attrs={
            "units": "dBZ",
            "long_name": "Attenuation-Corrected Reflectivity",
            "correction_method": f"PIA = {alpha} * (PhiDP - offset)",
        },
    )


# ---------------------------------------------------------------------------
# Hydrometeor classification (fuzzy logic)
# ---------------------------------------------------------------------------

# Membership-function centres and widths for each class / variable pair.
# Format: {class_name: {variable: (centre, width)}}
# Simplified trapezoidal membership functions approximated as Gaussians.
_HID_PARAMS: dict[str, dict[str, tuple[float, float]]] = {
    "drizzle": {"DBZH": (15.0, 10.0), "ZDR": (0.3, 0.4), "KDP": (0.0, 0.1), "RHOHV": (0.99, 0.02)},
    "rain": {"DBZH": (35.0, 12.0), "ZDR": (1.5, 1.0), "KDP": (0.5, 0.5), "RHOHV": (0.98, 0.02)},
    "wet_snow": {"DBZH": (30.0, 8.0), "ZDR": (1.0, 0.8), "KDP": (0.1, 0.2), "RHOHV": (0.90, 0.05)},
    "dry_snow": {"DBZH": (20.0, 10.0), "ZDR": (0.3, 0.5), "KDP": (0.0, 0.1), "RHOHV": (0.98, 0.03)},
    "graupel": {"DBZH": (42.0, 8.0), "ZDR": (0.2, 0.5), "KDP": (0.2, 0.3), "RHOHV": (0.95, 0.04)},
    "hail": {"DBZH": (55.0, 8.0), "ZDR": (-0.5, 1.0), "KDP": (0.0, 0.5), "RHOHV": (0.90, 0.06)},
}

_HID_CLASS_NAMES = list(_HID_PARAMS.keys())
_HID_CLASS_IDS = {name: idx for idx, name in enumerate(_HID_CLASS_NAMES)}


def classify_hydrometeors(
    dbz: xr.DataArray,
    zdr: xr.DataArray,
    kdp: xr.DataArray,
    rhohv: xr.DataArray,
    *,
    weights: dict[str, float] | None = None,
) -> xr.DataArray:
    """Classify hydrometeors using fuzzy-logic membership functions.

    Categories: drizzle (0), rain (1), wet_snow (2), dry_snow (3),
    graupel (4), hail (5). Returns -1 where any input is NaN.

    Parameters
    ----------
    dbz, zdr, kdp, rhohv : xr.DataArray
        Polarimetric variables, all same shape.
    weights : dict, optional
        Per-variable weights (keys: DBZH, ZDR, KDP, RHOHV). Default: equal.

    Returns
    -------
    xr.DataArray
        Integer class index, shape matching inputs.
    """
    if weights is None:
        weights = {"DBZH": 1.0, "ZDR": 1.0, "KDP": 1.0, "RHOHV": 1.0}

    shape = dbz.shape
    n_classes = len(_HID_CLASS_NAMES)

    # Stack inputs
    inputs = {
        "DBZH": dbz.values.astype(np.float64),
        "ZDR": zdr.values.astype(np.float64),
        "KDP": kdp.values.astype(np.float64),
        "RHOHV": rhohv.values.astype(np.float64),
    }

    # NaN mask: any input NaN -> classification invalid
    nan_mask = np.zeros(shape, dtype=bool)
    for v in inputs.values():
        nan_mask |= np.isnan(v)

    # Compute aggregated membership for each class
    scores = np.zeros((*shape, n_classes), dtype=np.float64)
    for cls_idx, cls_name in enumerate(_HID_CLASS_NAMES):
        for var_name, (centre, width) in _HID_PARAMS[cls_name].items():
            w = weights.get(var_name, 1.0)
            val = inputs[var_name]
            # Gaussian membership
            membership = np.exp(-0.5 * ((val - centre) / max(width, 1e-9)) ** 2)
            scores[..., cls_idx] += w * membership

    # Classify as the argmax class
    classification = np.argmax(scores, axis=-1).astype(np.int8)
    classification[nan_mask] = -1

    return xr.DataArray(
        classification,
        dims=dbz.dims,
        coords=dbz.coords,
        attrs={
            "units": "1",
            "long_name": "Hydrometeor Classification",
            "class_names": _HID_CLASS_NAMES,
            "flag_values": list(range(n_classes)),
            "flag_meanings": " ".join(_HID_CLASS_NAMES),
        },
    )


# ---------------------------------------------------------------------------
# Echo classification (clutter vs meteorological)
# ---------------------------------------------------------------------------


def classify_echo(
    dbz: xr.DataArray,
    zdr: xr.DataArray | None = None,
    phidp: xr.DataArray | None = None,
    rhohv: xr.DataArray | None = None,
    *,
    texture_window: int = 5,
    zdr_texture_thresh: float = 2.0,
    phidp_texture_thresh: float = 25.0,
    rhohv_thresh: float = 0.7,
) -> xr.DataArray:
    """Classify radar echoes as meteorological or clutter.

    Uses texture (local standard deviation) of ZDR and PhiDP, plus
    a threshold on RhoHV, to flag clutter. Points with high texture or
    low RhoHV are classified as clutter.

    Result values: 0 = no echo, 1 = meteorological, 2 = clutter.

    Parameters
    ----------
    dbz : xr.DataArray
        Reflectivity in dBZ.
    zdr, phidp, rhohv : xr.DataArray, optional
        Polarimetric variables. If absent, that test is skipped.
    texture_window : int
        Window size for texture (local std dev) computation.
    zdr_texture_thresh : float
        ZDR texture threshold above which echo is clutter.
    phidp_texture_thresh : float
        PhiDP texture threshold above which echo is clutter.
    rhohv_thresh : float
        RhoHV threshold below which echo is clutter.

    Returns
    -------
    xr.DataArray
        Echo classification (0=no echo, 1=meteorological, 2=clutter).
    """
    z = dbz.values.astype(np.float64)
    has_echo = ~np.isnan(z)
    is_clutter = np.zeros(z.shape, dtype=bool)

    # Helper: texture = local standard deviation along range (NaN-aware box filter)
    w = max(int(texture_window), 3)

    def _texture(arr: np.ndarray) -> np.ndarray:
        """Rolling std-dev along the last (range) axis, ignoring NaNs."""
        valid = np.isfinite(arr)
        shift = float(arr[valid].mean()) if valid.any() else 0.0
        x0 = np.where(valid, arr - shift, 0.0)
        kw = {"size": w, "axis": -1, "mode": "constant", "cval": 0.0}
        cnt = ndimage.uniform_filter1d(valid.astype(np.float64), **kw)
        s1 = ndimage.uniform_filter1d(x0, **kw)
        s2 = ndimage.uniform_filter1d(x0 * x0, **kw)
        ok = cnt > 0.5 / w
        safe = np.where(ok, cnt, 1.0)
        mean = s1 / safe
        return np.where(ok, np.sqrt(np.maximum(s2 / safe - mean * mean, 0.0)), np.nan)

    if zdr is not None:
        tex_zdr = _texture(zdr.values.astype(np.float64))
        is_clutter |= (tex_zdr > zdr_texture_thresh) & has_echo

    if phidp is not None:
        tex_phidp = _texture(phidp.values.astype(np.float64))
        is_clutter |= (tex_phidp > phidp_texture_thresh) & has_echo

    if rhohv is not None:
        rho = rhohv.values.astype(np.float64)
        is_clutter |= (rho < rhohv_thresh) & has_echo

    # Build classification array
    result = np.zeros(z.shape, dtype=np.int8)
    result[has_echo & ~is_clutter] = 1  # meteorological
    result[has_echo & is_clutter] = 2  # clutter

    return xr.DataArray(
        result,
        dims=dbz.dims,
        coords=dbz.coords,
        attrs={
            "units": "1",
            "long_name": "Echo Classification",
            "flag_values": [0, 1, 2],
            "flag_meanings": "no_echo meteorological clutter",
        },
    )


# ---------------------------------------------------------------------------
# Registry: name -> (function, required_variables, parameter_spec)
# ---------------------------------------------------------------------------

RETRIEVAL_REGISTRY: dict[str, dict[str, Any]] = {
    "kdp_estimation": {
        "function": estimate_kdp,
        "required_variables": ["PHIDP"],
        "params": {
            "window_len": {"type": "int", "default": 21, "label": "Smoothing Window (gates)"},
        },
        "description": "Estimate KDP from PhiDP using finite-difference method",
    },
    "rain_rate_z": {
        "function": rain_rate_z,
        "required_variables": ["DBZH"],
        "params": {
            "a": {"type": "float", "default": 200.0, "label": "Z-R coefficient a"},
            "b": {"type": "float", "default": 1.6, "label": "Z-R exponent b"},
        },
        "description": "Rainfall rate from Z using Marshall-Palmer Z=aR^b",
    },
    "rain_rate_kdp": {
        "function": rain_rate_kdp,
        "required_variables": ["KDP"],
        "params": {
            "a": {"type": "float", "default": 44.0, "label": "R-KDP coefficient a"},
            "b": {"type": "float", "default": 0.822, "label": "R-KDP exponent b"},
        },
        "description": "Rainfall rate from KDP",
    },
    "rain_rate_z_zdr": {
        "function": rain_rate_z_zdr,
        "required_variables": ["DBZH", "ZDR"],
        "params": {
            "a": {"type": "float", "default": 6.7e-3, "label": "Coefficient a"},
            "b": {"type": "float", "default": 0.927, "label": "Exponent b"},
            "c": {"type": "float", "default": -3.43, "label": "ZDR coefficient c"},
        },
        "description": "Rainfall rate from Z and ZDR",
    },
    "attenuation_correction": {
        "function": correct_attenuation_phidp,
        "required_variables": ["DBZH", "PHIDP"],
        "params": {
            "alpha": {"type": "float", "default": 0.08, "label": "Specific attenuation (dB/deg)"},
        },
        "description": "Attenuation correction using PhiDP (PIA method)",
    },
    "hydrometeor_classification": {
        "function": classify_hydrometeors,
        "required_variables": ["DBZH", "ZDR", "KDP", "RHOHV"],
        "params": {},
        "description": "Fuzzy-logic hydrometeor classification (6 classes)",
    },
    "echo_classification": {
        "function": classify_echo,
        "required_variables": ["DBZH"],
        "params": {
            "texture_window": {"type": "int", "default": 5, "label": "Texture window (gates)"},
            "zdr_texture_thresh": {
                "type": "float",
                "default": 2.0,
                "label": "ZDR texture threshold",
            },
            "phidp_texture_thresh": {
                "type": "float",
                "default": 25.0,
                "label": "PhiDP texture threshold",
            },
            "rhohv_thresh": {"type": "float", "default": 0.7, "label": "RhoHV threshold"},
        },
        "description": "Echo classification (clutter vs meteorological)",
    },
}


def run_retrieval(
    datatree: xr.DataTree,
    name: str,
    sweep: int = 0,
    params: dict[str, Any] | None = None,
    var_overrides: dict[str, str] | None = None,
) -> xr.DataArray:
    """Run a named retrieval on a specific sweep of a DataTree.

    Parameters
    ----------
    datatree : xr.DataTree
        Radar DataTree with sweep_N children.
    name : str
        Retrieval name from RETRIEVAL_REGISTRY.
    sweep : int
        Sweep index.
    params : dict, optional
        Override default parameters.
    var_overrides : dict, optional
        User variable mapping ``{role: variable}`` (see :mod:`engine.varmap`).

    Returns
    -------
    xr.DataArray
        The retrieval result.

    Raises
    ------
    KeyError
        If the retrieval name is not found.
    ValueError
        If required variables are missing.
    """
    if name not in RETRIEVAL_REGISTRY:
        raise KeyError(f"Unknown retrieval: {name}. Available: {list(RETRIEVAL_REGISTRY.keys())}")

    entry = RETRIEVAL_REGISTRY[name]
    func = entry["function"]
    required_vars = entry["required_variables"]

    # Get sweep dataset
    sweep_nodes = sorted_sweep_names(datatree)
    if sweep >= len(sweep_nodes):
        raise IndexError(f"Sweep {sweep} out of range (0..{len(sweep_nodes) - 1})")

    ds = mask_below_threshold(datatree[sweep_nodes[sweep]].to_dataset())

    # Flexible variable lookup (roles, CF standard names, aliases, patterns, user overrides)
    def _find_var(canonical: str) -> xr.DataArray | None:
        return varmap.find_in(ds, canonical, overrides=var_overrides)

    # Collect required data arrays
    data_args: list[xr.DataArray] = []
    for var_name in required_vars:
        da = _find_var(var_name)
        if da is None:
            raise ValueError(
                f"Retrieval '{name}' cannot run. "
                + varmap.explain_missing(
                    var_name,
                    ds,
                    sweep=sweep,
                    tree=datatree,
                    overrides=var_overrides,
                )
            )
        data_args.append(da)

    # Build keyword arguments from params
    merged_params: dict[str, Any] = {}
    for pname, pspec in entry["params"].items():
        merged_params[pname] = pspec["default"]
    if params:
        for k, v in params.items():
            if k in merged_params:
                merged_params[k] = v

    # Special handling for echo_classification: pass optional polvar arrays
    if name == "echo_classification":
        zdr_da = _find_var("ZDR")
        phidp_da = _find_var("PHIDP")
        rhohv_da = _find_var("RHOHV")
        return func(data_args[0], zdr=zdr_da, phidp=phidp_da, rhohv=rhohv_da, **merged_params)

    return func(*data_args, **merged_params)
