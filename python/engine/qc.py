"""Data quality control (QC) pipeline for radar data.

Provides individual QC algorithms and a composable pipeline builder
that chains multiple steps in user-defined order.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import xarray as xr

from engine._lazy import LazyModule

ndimage = LazyModule("scipy.ndimage")  # imported on first use (~150 ms)

logger = logging.getLogger(__name__)


# ======================================================================
# Helpers
# ======================================================================


def _azimuth_axis(data: xr.DataArray) -> int:
    """Index of the azimuth axis (the first axis unless a dim is named ``azimuth``)."""
    if "azimuth" in data.dims:
        return int(data.get_axis_num("azimuth"))
    return 0


def _is_full_circle(data: xr.DataArray) -> bool:
    """True if the azimuth coordinate covers 360 degrees without a large hole.

    Only then is it legitimate to wrap 2-D filters around the azimuth axis.
    """
    if "azimuth" not in data.coords:
        return False
    az = np.asarray(data.coords["azimuth"].values, dtype=np.float64).ravel()
    az = az[np.isfinite(az)]
    if az.size < 3:
        return False
    az = np.sort(np.mod(az, 360.0))
    gaps = np.diff(np.concatenate([az, [az[0] + 360.0]]))
    typical = float(np.median(gaps[gaps > 0])) if np.any(gaps > 0) else 0.0
    if typical <= 0:
        return False
    return bool(gaps.max() <= 3.0 * typical)


def _filter_modes(data: xr.DataArray, *, edge: str = "reflect") -> tuple[str, ...]:
    """Per-axis boundary modes: ``wrap`` on azimuth (full sweeps), *edge* elsewhere."""
    modes = [edge] * data.ndim
    if data.ndim >= 1 and _is_full_circle(data):
        modes[_azimuth_axis(data)] = "wrap"
    return tuple(modes)


def _nearest_index(mask: np.ndarray) -> tuple[np.ndarray, ...] | None:
    """Index arrays mapping every gate to its nearest valid gate (None if no gaps)."""
    if not mask.any() or mask.all():
        return None
    idx = ndimage.distance_transform_edt(mask, return_distances=False, return_indices=True)
    return tuple(idx)


def _fill_nearest(
    values: np.ndarray,
    mask: np.ndarray,
    index: tuple[np.ndarray, ...] | None = None,
) -> np.ndarray:
    """Replace masked gates by the value of the nearest valid gate.

    Unlike zero-filling this does not bias rank / moving-window filters near
    gaps in the data.  *mask* is True where ``values`` is missing.  Pass a
    precomputed *index* (:func:`_nearest_index`) when filling repeatedly.
    """
    if index is None:
        index = _nearest_index(mask)
    if index is None:
        return np.where(mask, 0.0, values)
    return values[index]


def _local_nan_stats(
    values: np.ndarray,
    valid: np.ndarray,
    size: int,
    modes: tuple[str, ...],
) -> tuple[np.ndarray, np.ndarray]:
    """NaN-aware local mean and (population) standard deviation in a box window.

    Computed with separable box filters, so the cost is independent of *size*.
    Gates with no valid neighbour get NaN mean and zero std.
    """
    # Out-of-grid cells must not count as valid data; only azimuth wrap brings
    # real data in, so map "reflect"/"nearest" to "constant".
    box_modes = tuple("wrap" if m == "wrap" else "constant" for m in modes)
    n_win = float(size**values.ndim)
    shift = float(values[valid].mean()) if valid.any() else 0.0
    x0 = np.where(valid, values - shift, 0.0)
    cnt = ndimage.uniform_filter(valid.astype(np.float64), size=size, mode=box_modes)
    s1 = ndimage.uniform_filter(x0, size=size, mode=box_modes)
    s2 = ndimage.uniform_filter(x0 * x0, size=size, mode=box_modes)
    ok = cnt > 0.5 / n_win
    safe = np.where(ok, cnt, 1.0)
    mean0 = s1 / safe
    var = np.maximum(s2 / safe - mean0 * mean0, 0.0)
    mean = np.where(ok, mean0 + shift, np.nan)
    std = np.where(ok, np.sqrt(var), 0.0)
    return mean, std


def _median_filter(values: np.ndarray, size: int, modes: tuple[str, ...]) -> np.ndarray:
    """Median filter honouring per-axis ``wrap`` modes.

    ``scipy.ndimage`` rank filters accept only a single boundary mode, so
    wrapped axes are padded explicitly and the padding cropped afterwards.
    """
    half = size // 2
    pad = [(half, half) if m == "wrap" else (0, 0) for m in modes]
    if not any(p != (0, 0) for p in pad):
        return ndimage.median_filter(values, size=size, mode="reflect")
    out = ndimage.median_filter(np.pad(values, pad, mode="wrap"), size=size, mode="reflect")
    crop = tuple(slice(p[0], out.shape[i] - p[1]) for i, p in enumerate(pad))
    return out[crop]


def _odd(n: int, minimum: int = 3) -> int:
    n = max(int(n), minimum)
    return n if n % 2 == 1 else n + 1


# ======================================================================
# Individual QC algorithms
# ======================================================================


def despeckle(
    data: xr.DataArray,
    window_size: int = 3,
    threshold: float = 0.5,
) -> xr.DataArray:
    """Remove isolated pixels using a median filter.

    Pixels whose value deviates from the local median by more than
    *threshold* times the local standard deviation are replaced by NaN.
    Missing gates are ignored (not treated as zeros) and the filters wrap
    around the azimuth axis for full 360-degree sweeps.

    Parameters
    ----------
    data : xr.DataArray
        2-D radar field (azimuth x range).
    window_size : int
        Side length of the square median-filter kernel (forced odd).
    threshold : float
        Number of local standard deviations to tolerate before masking.
    """
    values = data.values.astype(np.float64)
    mask = np.isnan(values)
    size = _odd(window_size)
    modes = _filter_modes(data)

    filled = _fill_nearest(values, mask)
    median = _median_filter(filled, size, modes)
    _, local_std = _local_nan_stats(values, ~mask, size, modes)
    local_std = np.where(local_std < 1e-9, 1.0, local_std)

    with np.errstate(invalid="ignore"):
        speckle = np.abs(filled - median) / local_std > threshold
    result = np.where(speckle & ~mask, np.nan, values)

    out = data.copy(data=result)
    out.attrs = {**data.attrs, "qc_despeckle": f"window={size}, threshold={threshold}"}
    return out


def remove_clutter(
    data: xr.DataArray,
    texture_threshold: float = 0.3,
    rhohv_threshold: float = 0.7,
    *,
    rhohv: xr.DataArray | None = None,
) -> xr.DataArray:
    """Mask ground clutter using texture and optional RhoHV thresholding.

    Clutter is identified where the local texture (standard deviation in a
    3x3 window, ignoring missing gates) exceeds *texture_threshold* **or**
    where RhoHV is below *rhohv_threshold* (if RhoHV data is available).

    Parameters
    ----------
    data : xr.DataArray
        Primary radar field.
    texture_threshold : float
        Maximum tolerated local-texture value.
    rhohv_threshold : float
        Minimum RhoHV for meteorological echoes.
    rhohv : xr.DataArray, optional
        Co-polar correlation coefficient field (same grid as *data*).
    """
    values = data.values.astype(np.float64)
    mask = np.isnan(values)

    _, texture = _local_nan_stats(values, ~mask, 3, _filter_modes(data))
    # Normalise texture to [0, 1] for thresholding
    tex_max = float(texture.max()) if texture.size else 0.0
    if tex_max > 0:
        texture = texture / tex_max

    clutter = texture > texture_threshold

    if rhohv is not None:
        rhv = rhohv.values.astype(np.float64)
        with np.errstate(invalid="ignore"):
            clutter = clutter | (rhv < rhohv_threshold)

    result = np.where(clutter & ~mask, np.nan, values)
    out = data.copy(data=result)
    out.attrs = {
        **data.attrs,
        "qc_clutter_removal": f"texture={texture_threshold}, rhohv={rhohv_threshold}",
    }
    return out


_DEALIAS_MAX_ITER = 2
_DEALIAS_MAX_SHIFT = 3  # Nyquist intervals
_DEALIAS_MIN_REGION = 8  # gates; smaller regions are left to the local-median pass


def _region_unfold(values: np.ndarray, nyq: float, wrap_az: bool) -> np.ndarray:
    """Region-based unfolding of a 2-D (azimuth x range) velocity field.

    1. Gates are grouped into regions connected through neighbours that differ
       by no more than the Nyquist velocity (no fold between them).
    2. Where two regions touch across a jump larger than the Nyquist velocity,
       the jump implies an integer number of Nyquist intervals between them.
    3. The most strongly connected region graph (maximum spanning tree) is walked
       from the largest region, accumulating the interval shift of each region.

    Returns the field with every region shifted by a multiple of ``2 * nyq``.
    """
    from collections import deque

    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components, minimum_spanning_tree

    ny, nx = values.shape
    valid = np.isfinite(values)
    if valid.sum() < 2:
        return values
    interval = 2.0 * nyq
    idx = np.arange(ny * nx).reshape(ny, nx)
    flat = values.ravel()

    ea: list[np.ndarray] = []
    eb: list[np.ndarray] = []
    slabs = [
        ((slice(None), slice(0, -1)), (slice(None), slice(1, None))),  # along range
        ((slice(0, -1), slice(None)), (slice(1, None), slice(None))),  # along azimuth
    ]
    if wrap_az and ny > 2:
        slabs.append(((slice(-1, None), slice(None)), (slice(0, 1), slice(None))))
    for sa, sb in slabs:
        ok = valid[sa] & valid[sb]
        ea.append(idx[sa][ok])
        eb.append(idx[sb][ok])
    a = np.concatenate(ea)
    b = np.concatenate(eb)
    if a.size == 0:
        return values
    d = flat[b] - flat[a]
    smooth = np.abs(d) <= nyq

    graph = coo_matrix((np.ones(int(smooth.sum())), (a[smooth], b[smooth])), shape=(ny * nx,) * 2)
    _, label = connected_components(graph, directed=False)
    sizes = np.bincount(label[valid.ravel()], minlength=label.max() + 1)
    big = sizes >= _DEALIAS_MIN_REGION
    if not big.any():
        return values

    la, lb = label[a], label[b]
    cross = (~smooth) & big[la] & big[lb] & (la != lb)
    if not cross.any():
        return values
    la, lb = la[cross], lb[cross]
    # Shift (in intervals) region lb needs relative to region la so that they agree.
    rel = -np.round(d[cross] / interval).astype(np.int64)
    swap = la > lb
    la, lb = np.where(swap, lb, la), np.where(swap, la, lb)
    rel = np.where(swap, -rel, rel)

    triples, counts = np.unique(np.stack([la, lb, rel], axis=1), axis=0, return_counts=True)
    order = np.lexsort((-counts, triples[:, 1], triples[:, 0]))  # per pair: best vote first
    triples, counts = triples[order], counts[order]
    first = np.ones(len(triples), dtype=bool)
    first[1:] = (triples[1:, 0] != triples[:-1, 0]) | (triples[1:, 1] != triples[:-1, 1])
    pair_start = np.flatnonzero(first)
    pair_weight = np.add.reduceat(counts, pair_start)
    pairs = triples[pair_start]  # columns: region a, region b, best rel

    regions, inv = np.unique(pairs[:, :2], return_inverse=True)
    inv = inv.reshape(-1, 2)
    n_reg = len(regions)
    tree = minimum_spanning_tree(
        coo_matrix((1.0 / pair_weight, (inv[:, 0], inv[:, 1])), shape=(n_reg, n_reg)).tocsr()
    ).tocoo()

    adj: list[list[tuple[int, int]]] = [[] for _ in range(n_reg)]
    rel_of = {(int(i), int(j)): int(r) for (i, j), r in zip(inv, pairs[:, 2], strict=True)}
    for i, j in zip(tree.row.tolist(), tree.col.tolist(), strict=True):
        r = rel_of[(i, j)] if (i, j) in rel_of else -rel_of[(j, i)]
        adj[i].append((j, r))
        adj[j].append((i, -r))

    shift = np.zeros(n_reg, dtype=np.int64)
    seen = np.zeros(n_reg, dtype=bool)
    for root in np.argsort(-sizes[regions], kind="stable"):  # largest region first
        if seen[root]:
            continue
        seen[root] = True
        queue = deque([int(root)])
        while queue:
            i = queue.popleft()
            for j, r in adj[i]:
                if not seen[j]:
                    seen[j] = True
                    shift[j] = shift[i] + r
                    queue.append(j)

    label_shift = np.zeros(label.max() + 1, dtype=np.int64)
    # Real winds rarely exceed a few Nyquist intervals; bound the damage when
    # noisy data lets region votes accumulate nonsense along a long tree path.
    label_shift[regions] = np.clip(shift, -_DEALIAS_MAX_SHIFT, _DEALIAS_MAX_SHIFT)
    return values + label_shift[label].reshape(ny, nx) * interval


def velocity_dealiasing(
    data: xr.DataArray,
    nyquist_vel: float,
    reference: str = "none",
) -> xr.DataArray:
    """Region-based velocity dealiasing.

    Contiguous regions of gates (no jump larger than the Nyquist velocity
    between neighbours) are identified, and the integer number of Nyquist
    intervals (``2 * nyquist_vel``) separating adjacent regions is
    propagated outwards from the largest region.  A short local-median pass
    then fixes isolated gates.  Missing gates are ignored and the azimuth
    axis wraps for full sweeps.  Unfolded velocities may legitimately exceed
    +-Nyquist.

    Parameters
    ----------
    data : xr.DataArray
        Radial velocity field (2-D, azimuth x range).
    nyquist_vel : float
        Nyquist velocity (m/s) — half the unambiguous velocity interval.
    reference : {"none", "zero", "vad"}
        How the absolute fold of the whole sweep is chosen. ``"none"`` (default) keeps the
        largest region's raw values, so the whole sweep can be off by a multiple of
        ``2 * nyquist_vel`` (issue #152). ``"zero"`` shifts it so the along-beam wind averages
        nearest zero around the circle (refused unless the echo covers the full circle);
        ``"vad"`` uses the constant term of a uniform-wind VAD fit (refused when the echo leaves
        an azimuth gap wider than 180 degrees). See :mod:`engine.dealias_reference`.
    """
    from engine.dealias_reference import REFERENCES, fold_reference_shift

    if not np.isfinite(nyquist_vel) or nyquist_vel <= 0:
        raise ValueError(f"nyquist_vel must be a positive number, got {nyquist_vel}")
    if reference not in REFERENCES:
        raise ValueError(f"reference must be one of {', '.join(REFERENCES)}, got {reference!r}")
    if reference != "none" and (data.ndim != 2 or "azimuth" not in data.coords):
        raise ValueError(
            f"reference {reference!r} needs a 2-D sweep with the azimuth coordinate of the rays"
        )

    values = data.values.astype(np.float64)
    mask = np.isnan(values)
    interval = 2.0 * nyquist_vel
    modes = _filter_modes(data)

    if not mask.all():
        if values.ndim == 2:
            az_axis = _azimuth_axis(data)
            work = np.moveaxis(values, az_axis, 0)
            work = _region_unfold(work, nyquist_vel, modes[az_axis] == "wrap")
            values = np.moveaxis(work, 0, az_axis)

        nearest = _nearest_index(mask)  # gaps are fixed, so compute once
        for _ in range(_DEALIAS_MAX_ITER):
            filled = _fill_nearest(values, mask, nearest)
            ref = _median_filter(filled, 5, modes)
            n_folds = np.where(mask, 0.0, np.round((filled - ref) / interval))
            if not n_folds.any():
                break
            values = values - n_folds * interval

    note = f"nyquist_vel={nyquist_vel}"
    if reference != "none":
        az_axis = _azimuth_axis(data)
        k, how = fold_reference_shift(
            np.moveaxis(values, az_axis, 0), data.coords["azimuth"].values, nyquist_vel, reference
        )
        values = values + k * interval
        note += f", {how}"

    out = data.copy(data=values)
    out.attrs = {**data.attrs, "qc_dealiased": note}
    return out


def noise_removal(
    data: xr.DataArray,
    noise_floor_dbz: float = -10.0,
) -> xr.DataArray:
    """Mask values below the noise floor.

    Parameters
    ----------
    data : xr.DataArray
        Radar field (typically reflectivity in dBZ).
    noise_floor_dbz : float
        Threshold below which values are considered noise.
    """
    values = data.values.astype(np.float64)
    result = np.where(values < noise_floor_dbz, np.nan, values)
    out = data.copy(data=result)
    out.attrs = {**data.attrs, "qc_noise_removal": f"floor={noise_floor_dbz}"}
    return out


def range_correction(
    data: xr.DataArray,
    range_m: np.ndarray | None = None,
) -> xr.DataArray:
    """Apply 1/r^2 range correction.

    Compensates for the decrease in returned power with range by adding
    ``20 * log10(r / r_ref)`` dB, where *r_ref* is the first range gate.

    Parameters
    ----------
    data : xr.DataArray
        Radar reflectivity field.
    range_m : ndarray, optional
        Range values in metres.  If *None*, extracted from ``data.coords["range"]``.
    """
    if range_m is None:
        if "range" in data.coords:
            range_m = data.coords["range"].values.astype(np.float64)
        else:
            logger.warning("range_correction: no range coordinate found; returning data unchanged")
            return data

    r = np.array(range_m, dtype=np.float64)  # copy; accepts lists / int arrays
    if r.size == 0:
        return data
    r[r <= 0] = r[r > 0].min() if np.any(r > 0) else 1.0
    r_ref = r[0]

    correction = 20.0 * np.log10(r / r_ref)
    # Broadcast along the range dimension (the last axis unless named otherwise)
    range_axis = data.get_axis_num("range") if "range" in data.dims else data.ndim - 1
    shape = [1] * data.ndim
    shape[range_axis] = -1
    values = data.values.astype(np.float64) + correction.reshape(shape)

    out = data.copy(data=values)
    out.attrs = {**data.attrs, "qc_range_correction": "1/r^2"}
    return out


def smooth_data(
    data: xr.DataArray,
    method: str = "gaussian",
    sigma: float = 1.0,
) -> xr.DataArray:
    """Apply Gaussian or median smoothing.

    Missing gates do not contribute to (or bias) the smoothed values, and
    full 360-degree sweeps wrap around the azimuth axis.

    Parameters
    ----------
    data : xr.DataArray
        2-D radar field.
    method : str
        ``"gaussian"`` or ``"median"``.
    sigma : float
        For Gaussian: standard deviation.  For median: kernel size
        (rounded up to an odd integer, minimum 3).
    """
    values = data.values.astype(np.float64)
    mask = np.isnan(values)
    modes = _filter_modes(data)

    if method == "gaussian":
        # Normalised convolution: smooth(value * valid) / smooth(valid)
        weight = ndimage.gaussian_filter((~mask).astype(np.float64), sigma=sigma, mode=modes)
        num = ndimage.gaussian_filter(np.where(mask, 0.0, values), sigma=sigma, mode=modes)
        with np.errstate(invalid="ignore", divide="ignore"):
            smoothed = np.where(weight > 1e-6, num / weight, values)
    elif method == "median":
        filled = _fill_nearest(values, mask)
        smoothed = _median_filter(filled, _odd(sigma), modes)
    else:
        logger.warning("smooth_data: unknown method '%s'; returning data unchanged", method)
        return data

    result = np.where(mask, np.nan, smoothed)
    out = data.copy(data=result)
    out.attrs = {**data.attrs, "qc_smoothed": f"{method}, sigma={sigma}"}
    return out


def threshold_filter(
    data: xr.DataArray,
    vmin: float | None = None,
    vmax: float | None = None,
) -> xr.DataArray:
    """Mask values outside [vmin, vmax].

    Parameters
    ----------
    data : xr.DataArray
        Radar field.
    vmin, vmax : float, optional
        Lower / upper threshold.  ``None`` means no bound on that side.
    """
    values = data.values.astype(np.float64)
    mask = np.zeros_like(values, dtype=bool)
    if vmin is not None:
        mask |= values < vmin
    if vmax is not None:
        mask |= values > vmax

    result = np.where(mask, np.nan, values)
    out = data.copy(data=result)
    out.attrs = {**data.attrs, "qc_threshold": f"vmin={vmin}, vmax={vmax}"}
    return out


# ======================================================================
# QC Algorithm Registry
# ======================================================================

QC_REGISTRY: dict[str, dict[str, Any]] = {
    "despeckle": {
        "function": despeckle,
        "description": "Remove isolated noisy pixels using median filter comparison",
        "params": {
            "window_size": {
                "type": "int",
                "default": 3,
                "label": "Window size",
                "min": 3,
                "max": 11,
            },
            "threshold": {
                "type": "float",
                "default": 0.5,
                "label": "Threshold (std devs)",
                "min": 0.1,
                "max": 5.0,
            },
        },
        "applicable_variables": [
            "DBZH",
            "DBZ",
            "ZH",
            "DBZV",
            "ZDR",
            "KDP",
            "PHIDP",
            "RHOHV",
            "VRADH",
            "VRAD",
        ],
    },
    "remove_clutter": {
        "function": remove_clutter,
        "description": "Mask ground clutter using texture analysis and RhoHV",
        "params": {
            "texture_threshold": {
                "type": "float",
                "default": 0.3,
                "label": "Texture threshold",
                "min": 0.05,
                "max": 1.0,
            },
            "rhohv_threshold": {
                "type": "float",
                "default": 0.7,
                "label": "RhoHV threshold",
                "min": 0.3,
                "max": 1.0,
            },
        },
        "applicable_variables": ["DBZH", "DBZ", "ZH", "DBZV", "ZDR"],
    },
    "velocity_dealiasing": {
        "function": velocity_dealiasing,
        "description": "Region-based velocity dealiasing using Nyquist interval",
        "params": {
            "nyquist_vel": {
                "type": "float",
                "default": 25.0,
                "label": "Nyquist velocity (m/s)",
                "min": 1.0,
                "max": 100.0,
            },
            "reference": {
                "type": "str",
                "default": "none",
                "label": "Fold reference",
                "options": ["none", "zero", "vad"],
            },
        },
        "applicable_variables": ["VRADH", "VRAD", "V", "VEL"],
    },
    "noise_removal": {
        "function": noise_removal,
        "description": "Mask values below the noise floor threshold",
        "params": {
            "noise_floor_dbz": {
                "type": "float",
                "default": -10.0,
                "label": "Noise floor (dBZ)",
                "min": -40.0,
                "max": 10.0,
            },
        },
        "applicable_variables": ["DBZH", "DBZ", "ZH", "DBZV"],
    },
    "range_correction": {
        "function": range_correction,
        "description": "Apply 1/r² range correction to compensate power loss with distance",
        "params": {},
        "applicable_variables": ["DBZH", "DBZ", "ZH", "DBZV"],
    },
    "smooth_data": {
        "function": smooth_data,
        "description": "Apply Gaussian or median smoothing to reduce noise",
        "params": {
            "method": {
                "type": "str",
                "default": "gaussian",
                "label": "Method",
                "options": ["gaussian", "median"],
            },
            "sigma": {
                "type": "float",
                "default": 1.0,
                "label": "Sigma / kernel size",
                "min": 0.5,
                "max": 5.0,
            },
        },
        "applicable_variables": [
            "DBZH",
            "DBZ",
            "ZH",
            "DBZV",
            "ZDR",
            "KDP",
            "PHIDP",
            "RHOHV",
            "VRADH",
            "VRAD",
        ],
    },
    "threshold_filter": {
        "function": threshold_filter,
        "description": "Mask values outside a specified min/max range",
        "params": {
            "vmin": {
                "type": "float",
                "default": None,
                "label": "Minimum value",
                "min": -100.0,
                "max": 200.0,
            },
            "vmax": {
                "type": "float",
                "default": None,
                "label": "Maximum value",
                "min": -100.0,
                "max": 200.0,
            },
        },
        "applicable_variables": [
            "DBZH",
            "DBZ",
            "ZH",
            "DBZV",
            "ZDR",
            "KDP",
            "PHIDP",
            "RHOHV",
            "VRADH",
            "VRAD",
        ],
    },
}


# ======================================================================
# QC Pipeline Builder
# ======================================================================


class QCPipeline:
    """Composable QC pipeline that chains multiple steps in order.

    Usage::

        pipeline = QCPipeline()
        pipeline.add_step("despeckle", {"window_size": 5})
        pipeline.add_step("noise_removal", {"noise_floor_dbz": -5})
        cleaned = pipeline.run(data)
    """

    def __init__(self) -> None:
        self._steps: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Step management
    # ------------------------------------------------------------------

    def add_step(self, name: str, params: dict[str, Any] | None = None) -> None:
        """Append a QC step to the pipeline.

        Parameters
        ----------
        name : str
            Algorithm name (must be a key in :data:`QC_REGISTRY`).
        params : dict, optional
            Override default parameters for this step.

        Raises
        ------
        ValueError
            If *name* is not in the registry.
        """
        if name not in QC_REGISTRY:
            raise ValueError(f"Unknown QC algorithm: '{name}'. Available: {list(QC_REGISTRY)}")
        self._steps.append({"name": name, "params": params or {}})

    def remove_step(self, index: int) -> None:
        """Remove the step at *index*."""
        if 0 <= index < len(self._steps):
            self._steps.pop(index)
        else:
            raise IndexError(f"Step index {index} out of range (0..{len(self._steps) - 1})")

    def reorder(self, from_idx: int, to_idx: int) -> None:
        """Move a step from *from_idx* to *to_idx*."""
        if not (0 <= from_idx < len(self._steps)):
            raise IndexError(f"from_idx {from_idx} out of range")
        if not (0 <= to_idx < len(self._steps)):
            raise IndexError(f"to_idx {to_idx} out of range")
        step = self._steps.pop(from_idx)
        self._steps.insert(to_idx, step)

    @property
    def steps(self) -> list[dict[str, Any]]:
        return list(self._steps)

    def __len__(self) -> int:
        return len(self._steps)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def run(self, data: xr.DataArray, *, rhohv: xr.DataArray | None = None) -> xr.DataArray:
        """Apply all pipeline steps in order and return the cleaned DataArray.

        *rhohv* (the co-polar correlation coefficient of the same sweep, when it has one) is
        handed to ``remove_clutter`` so its RhoHV threshold takes effect.
        """
        result = data
        for i, step in enumerate(self._steps):
            name = step["name"]
            params = step["params"]
            entry = QC_REGISTRY.get(name)
            if entry is None:
                logger.warning("Skipping unknown QC step '%s'", name)
                continue

            fn = entry["function"]
            logger.info("QC step %d/%d: %s %s", i + 1, len(self._steps), name, params)
            kwargs = dict(params)
            if name == "remove_clutter" and rhohv is not None and "rhohv" not in kwargs:
                kwargs["rhohv"] = rhohv
            try:
                result = fn(result, **kwargs)
            except Exception:
                logger.error("QC step '%s' failed", name, exc_info=True)
                raise
        return result

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> list[dict[str, Any]]:
        """Serialise pipeline to a JSON-friendly list."""
        return [{"name": s["name"], "params": s["params"]} for s in self._steps]

    @classmethod
    def from_dict(cls, steps: list[dict[str, Any]]) -> QCPipeline:
        """Deserialise a pipeline from a list of step dicts."""
        pipeline = cls()
        for step in steps:
            pipeline.add_step(step["name"], step.get("params", {}))
        return pipeline
