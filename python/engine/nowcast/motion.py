"""Motion of the echo pattern from a short sequence of gridded frames.

Two estimators, both on regular grids of square cells (NaN = not observed):

``lucas_kanade``
    Dense, pyramidal Lucas-Kanade optical flow (Lucas and Kanade 1981; coarse-to-fine pyramid
    and iterative warping after Bouguet 2001). For every cell the local translation that best
    explains the change between two frames is the least-squares solution of
    ``I_x u + I_y v = -I_t`` over a window around the cell. Cells whose window has too little
    texture (smallest eigenvalue of the structure tensor below ``min_eigen``) or no echo give no
    solution; the field is then completed by normalised convolution (Knutsson and Westin 1993):
    a Gaussian-weighted mean of the reliable vectors, falling back to the domain-wide vector where
    no reliable vector is near. This is the dense counterpart of the sparse Lucas-Kanade tracking
    that pysteps uses (Pulkkinen et al. 2019).

``global_translation``
    One vector for the whole domain from the peak of the FFT cross-correlation of the two frames
    (Rinehart and Garvey 1978, the TREC idea applied to the whole field), refined to sub-cell
    precision with a parabolic fit of the peak. The fallback when the dense estimate is not
    reliable, and the baseline "one storm motion" the verification compares against.

Several frame pairs (the last ``n_frames`` frames, default 3) are estimated one by one and
averaged in m/s, so an irregular scan cadence (4.5 then 6 minutes) is handled by each pair's
own time step.

The estimate is a **retrieval**: a motion of the pattern as seen by the radar, which is neither
the wind nor the motion of individual cells (Germann and Zawadzki 2002 discuss the difference).

References
----------
* Bouguet, J.-Y., 2001: Pyramidal implementation of the affine Lucas Kanade feature tracker.
  Intel Corporation, Microprocessor Research Labs.
* Germann, U., and I. Zawadzki, 2002: Scale-dependence of the predictability of precipitation
  from continental radar images. Part I: Description of the methodology. *Mon. Wea. Rev.*, **130**,
  2859-2873.
* Knutsson, H., and C.-F. Westin, 1993: Normalized and differential convolution. *Proc. IEEE
  CVPR*, 515-523.
* Lucas, B. D., and T. Kanade, 1981: An iterative image registration technique with an
  application to stereo vision. *Proc. 7th Int. Joint Conf. on Artificial Intelligence*, 674-679.
* Pulkkinen, S., et al., 2019: Pysteps: an open-source Python library for probabilistic
  precipitation nowcasting (v1.0). *Geosci. Model Dev.*, **12**, 4185-4219.
* Rinehart, R. E., and E. T. Garvey, 1978: Three-dimensional storm motion detection by
  conventional weather radar. *Nature*, **273**, 287-289.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from engine._lazy import LazyModule

ndimage = LazyModule("scipy.ndimage")

#: dBZ below which a cell counts as "no echo" for the motion estimate (and the value the
#: tracking image takes there, so that gradients at the echo edge are well defined)
DEFAULT_FLOOR_DBZ = 10.0
#: below this peak correlation of the two frames (at the best shift) there is no pattern to
#: track: noise or a field that changed completely; the motion is then zero and says so
MIN_PATTERN_CORRELATION = 0.2


class MotionError(Exception):
    """The motion could not be estimated from the given frames."""


@dataclass
class MotionField:
    """Motion on the grid: ``u`` east and ``v`` north in m/s (shape ``(ny, nx)``).

    ``reliable`` marks the cells with a direct local solution (the rest is interpolated),
    ``global_uv`` is the one-vector estimate of the whole domain, ``method`` says which estimator
    produced the field and ``notes`` what happened on the way (a fallback, a sparse echo).
    """

    u: np.ndarray
    v: np.ndarray
    cell_m: float
    method: str
    reliable: np.ndarray
    global_uv: tuple[float, float]
    n_pairs: int = 1
    parameters: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def shape(self) -> tuple[int, int]:
        return self.u.shape

    @property
    def reliable_fraction(self) -> float:
        return float(self.reliable.mean()) if self.reliable.size else 0.0

    def mean_uv(self, weights: np.ndarray | None = None) -> tuple[float, float]:
        """Domain mean (optionally weighted, e.g. by echo) of the field, m/s."""
        if weights is None:
            return float(np.mean(self.u)), float(np.mean(self.v))
        w = np.asarray(weights, dtype=np.float64)
        s = w.sum()
        if s <= 0:
            return float(np.mean(self.u)), float(np.mean(self.v))
        return float((w * self.u).sum() / s), float((w * self.v).sum() / s)

    def speed_direction(self) -> tuple[float, float]:
        """Mean speed (m/s) and the direction the pattern moves **towards** (degrees from north)."""
        u, v = self.global_uv
        return math.hypot(u, v), (math.degrees(math.atan2(u, v)) % 360.0)

    def to_dict(self) -> dict[str, Any]:
        spd, direction = self.speed_direction()
        return {
            "method": self.method,
            "u_mean_ms": round(float(np.mean(self.u)), 3),
            "v_mean_ms": round(float(np.mean(self.v)), 3),
            "global_u_ms": round(self.global_uv[0], 3),
            "global_v_ms": round(self.global_uv[1], 3),
            "speed_ms": round(spd, 3),
            "direction_towards_deg": round(direction, 1),
            "reliable_fraction": round(self.reliable_fraction, 4),
            "n_pairs": self.n_pairs,
            "parameters": dict(self.parameters),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Tracking image
# ---------------------------------------------------------------------------


def tracking_image(
    frame: np.ndarray, *, floor: float = DEFAULT_FLOOR_DBZ, sigma: float = 1.0
) -> tuple[np.ndarray, np.ndarray]:
    """``(image, echo)``: the frame with no-data and values below ``floor`` set to ``floor``,
    then Gaussian-smoothed (``sigma`` cells); ``echo`` is where the frame had a value above
    the floor. The smoothing makes the gradients well posed; it changes nothing in the data
    that is extrapolated (only the motion estimate looks at this image)."""
    a = np.asarray(frame, dtype=np.float64)
    echo = np.isfinite(a) & (a > floor)
    img = np.where(echo, a, floor)
    if sigma > 0:
        img = ndimage.gaussian_filter(img, sigma, mode="nearest")
    return img, echo


# ---------------------------------------------------------------------------
# Global translation (FFT cross-correlation)
# ---------------------------------------------------------------------------


def _parabolic_peak(c: np.ndarray, i: int, j: int) -> tuple[float, float]:
    """Sub-cell position of the maximum of ``c`` around ``(i, j)`` (1-D parabolas, each axis)."""

    def refine(m1: float, m0: float, p1: float) -> float:
        den = m1 - 2.0 * m0 + p1
        if den >= 0 or not math.isfinite(den):
            return 0.0
        return float(np.clip(0.5 * (m1 - p1) / den, -0.5, 0.5))

    ny, nx = c.shape
    di = refine(c[(i - 1) % ny, j], c[i, j], c[(i + 1) % ny, j])
    dj = refine(c[i, (j - 1) % nx], c[i, j], c[i, (j + 1) % nx])
    return di, dj


def global_translation(
    a: np.ndarray, b: np.ndarray, *, max_shift: int | None = None
) -> tuple[float, float, float]:
    """Displacement ``(dx, dy)`` in cells (x east, y north) from ``a`` to ``b`` and the peak
    correlation coefficient, from the FFT cross-correlation of the mean-removed images.

    ``max_shift`` limits the search (cells); the default is a quarter of the smaller dimension.
    """
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.shape != b.shape or a.ndim != 2:
        raise MotionError("global_translation needs two 2-D frames of the same shape")
    ny, nx = a.shape
    fa = a - a.mean()
    fb = b - b.mean()
    sa, sb = float(np.sqrt((fa**2).sum())), float(np.sqrt((fb**2).sum()))
    if sa <= 0 or sb <= 0:
        return 0.0, 0.0, 0.0
    cc = np.real(np.fft.ifft2(np.fft.fft2(fb) * np.conj(np.fft.fft2(fa)))) / (sa * sb)
    lim = max_shift if max_shift is not None else max(1, min(ny, nx) // 4)
    ky = np.minimum(np.arange(ny), ny - np.arange(ny))
    kx = np.minimum(np.arange(nx), nx - np.arange(nx))
    allowed = (ky[:, None] <= lim) & (kx[None, :] <= lim)
    cc_m = np.where(allowed, cc, -np.inf)
    i, j = np.unravel_index(int(np.argmax(cc_m)), cc.shape)
    di, dj = _parabolic_peak(cc, int(i), int(j))
    dy = (i + ny // 2) % ny - ny // 2 + di
    dx = (j + nx // 2) % nx - nx // 2 + dj
    return float(dx), float(dy), float(cc[i, j])


# ---------------------------------------------------------------------------
# Dense Lucas-Kanade
# ---------------------------------------------------------------------------


def _downsample(img: np.ndarray) -> np.ndarray:
    sm = ndimage.gaussian_filter(img, 1.0, mode="nearest")
    return sm[::2, ::2]


def _warp(img: np.ndarray, u: np.ndarray, v: np.ndarray, fill: float) -> np.ndarray:
    """``img`` sampled at ``(y + v, x + u)`` (bilinear; ``fill`` outside the domain)."""
    ny, nx = img.shape
    yy, xx = np.mgrid[0:ny, 0:nx].astype(np.float64)
    coords = np.array([yy + v, xx + u])
    return ndimage.map_coordinates(img, coords, order=1, mode="constant", cval=fill)


def _lk_level(
    a: np.ndarray,
    b: np.ndarray,
    u: np.ndarray,
    v: np.ndarray,
    *,
    window: int,
    iterations: int,
    min_eigen: float,
    fill: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Refine ``(u, v)`` (cells per frame) on one pyramid level; returns the reliable mask too."""
    nd = ndimage
    reliable = np.zeros(a.shape, dtype=bool)
    for _ in range(max(1, iterations)):
        bw = _warp(b, u, v, fill)
        mid = 0.5 * (a + bw)
        iy, ix = np.gradient(mid)
        it = bw - a
        sxx = nd.uniform_filter(ix * ix, window, mode="nearest")
        syy = nd.uniform_filter(iy * iy, window, mode="nearest")
        sxy = nd.uniform_filter(ix * iy, window, mode="nearest")
        sxt = nd.uniform_filter(ix * it, window, mode="nearest")
        syt = nd.uniform_filter(iy * it, window, mode="nearest")
        tr = sxx + syy
        det = sxx * syy - sxy * sxy
        disc = np.sqrt(np.maximum(0.25 * tr * tr - det, 0.0))
        lam_min = 0.5 * tr - disc
        ok = lam_min > min_eigen
        with np.errstate(divide="ignore", invalid="ignore"):
            du = np.where(ok, -(syy * sxt - sxy * syt) / det, 0.0)
            dv = np.where(ok, -(sxx * syt - sxy * sxt) / det, 0.0)
        # a step larger than the window is not a local solution: it is noise
        du = np.clip(np.nan_to_num(du), -window, window)
        dv = np.clip(np.nan_to_num(dv), -window, window)
        u = u + du
        v = v + dv
        reliable = ok
    return u, v, reliable


def _normalised_convolution(
    u: np.ndarray, v: np.ndarray, weight: np.ndarray, sigma: float, fallback: tuple[float, float]
) -> tuple[np.ndarray, np.ndarray]:
    """Gaussian-weighted mean of the vectors where ``weight`` > 0, ``fallback`` elsewhere."""
    nd = ndimage
    w = np.asarray(weight, dtype=np.float64)
    wsum = nd.gaussian_filter(w, sigma, mode="constant", cval=0.0)
    us = nd.gaussian_filter(w * u, sigma, mode="constant", cval=0.0)
    vs = nd.gaussian_filter(w * v, sigma, mode="constant", cval=0.0)
    # Below this the smoothed weight is noise (a few far-away vectors): use the global vector
    near = wsum > 1e-3
    with np.errstate(divide="ignore", invalid="ignore"):
        uo = np.where(near, us / wsum, fallback[0])
        vo = np.where(near, vs / wsum, fallback[1])
    return uo, vo


def lucas_kanade(
    a: np.ndarray,
    b: np.ndarray,
    *,
    levels: int = 3,
    window: int = 9,
    iterations: int = 3,
    min_eigen: float = 0.05,
    floor: float = DEFAULT_FLOOR_DBZ,
    smooth_sigma: float = 1.0,
    fill_sigma: float = 8.0,
    min_echo_cells: int = 50,
    min_corr: float = MIN_PATTERN_CORRELATION,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, tuple[float, float], list[str]]:
    """Dense displacement ``(u, v)`` in cells from frame ``a`` to frame ``b`` (x east, y north).

    Returns ``(u, v, reliable, global_uv, notes)``: the completed field, where it rests on a local
    solution, the global translation used for the completion, and notes on fallbacks. Two
    frames whose patterns do not correlate at any shift (peak correlation below ``min_corr``:
    noise, or a field that changed completely) carry no motion information: zero motion.
    """
    img_a, echo_a = tracking_image(a, floor=floor, sigma=smooth_sigma)
    img_b, echo_b = tracking_image(b, floor=floor, sigma=smooth_sigma)
    if img_a.shape != img_b.shape or img_a.ndim != 2:
        raise MotionError("the two frames must be 2-D and of the same shape")
    notes: list[str] = []
    n_echo = int(min(echo_a.sum(), echo_b.sum()))
    gx, gy, cc = global_translation(img_a, img_b)
    z = np.zeros(img_a.shape)
    if n_echo < min_echo_cells:
        notes.append(
            f"too little echo for a motion estimate ({n_echo} cells above {floor:g}): "
            "zero motion assumed"
        )
        return z, z.copy(), np.zeros(img_a.shape, bool), (0.0, 0.0), notes
    if cc < min_corr:
        notes.append(
            f"no coherent pattern between the frames (peak correlation {cc:.2f} below "
            f"{min_corr:g}): zero motion assumed"
        )
        return z, z.copy(), np.zeros(img_a.shape, bool), (0.0, 0.0), notes

    # pyramid (coarsest last)
    pyr_a, pyr_b = [img_a], [img_b]
    for _ in range(max(0, levels - 1)):
        if min(pyr_a[-1].shape) < 2 * window:
            break
        pyr_a.append(_downsample(pyr_a[-1]))
        pyr_b.append(_downsample(pyr_b[-1]))
    n_levels = len(pyr_a)
    scale = 2.0 ** (n_levels - 1)
    u = np.full(pyr_a[-1].shape, gx / scale)
    v = np.full(pyr_a[-1].shape, gy / scale)
    reliable = np.zeros(pyr_a[-1].shape, bool)
    for lvl in range(n_levels - 1, -1, -1):
        if lvl != n_levels - 1:
            zoom = (pyr_a[lvl].shape[0] / u.shape[0], pyr_a[lvl].shape[1] / u.shape[1])
            u = ndimage.zoom(u, zoom, order=1, mode="nearest") * 2.0
            v = ndimage.zoom(v, zoom, order=1, mode="nearest") * 2.0
        u, v, reliable = _lk_level(
            pyr_a[lvl],
            pyr_b[lvl],
            u,
            v,
            window=window,
            iterations=iterations,
            min_eigen=min_eigen,
            fill=floor,
        )
    # Only cells with echo in the earlier frame and texture around them carry information
    reliable &= echo_a | echo_b
    if reliable.sum() < min_echo_cells:
        notes.append(
            f"the dense estimate was reliable in only {int(reliable.sum())} cells: the global "
            "translation (cross-correlation) is used everywhere"
        )
        return (
            np.full(img_a.shape, gx),
            np.full(img_a.shape, gy),
            reliable & False,
            (gx, gy),
            notes,
        )
    # global vector of the dense estimate: the median of the reliable cells (robust)
    med = (float(np.median(u[reliable])), float(np.median(v[reliable])))
    # remove the gross outliers of the local solutions before completing the field
    dev = np.hypot(u - med[0], v - med[1])
    spread = float(np.median(dev[reliable])) * 3.0 + 0.5
    keep = reliable & (dev <= max(spread, 1.0))
    u_c, v_c = _normalised_convolution(u, v, keep, fill_sigma, med)
    return u_c, v_c, keep, med, notes


# ---------------------------------------------------------------------------
# Public entry: a sequence of frames -> MotionField in m/s
# ---------------------------------------------------------------------------


def estimate_motion(
    frames: Sequence[np.ndarray] | np.ndarray,
    times: Sequence[Any] | np.ndarray,
    cell_m: float,
    *,
    method: str = "lucas_kanade",
    n_frames: int = 3,
    floor: float = DEFAULT_FLOOR_DBZ,
    max_speed_ms: float = 60.0,
    **options: Any,
) -> MotionField:
    """Motion field (m/s, ``u`` east, ``v`` north) from the last ``n_frames`` frames.

    ``frames`` is ``(n, ny, nx)`` (or a list of 2-D arrays) in time order, NaN = not observed;
    ``times`` the matching UTC times (``datetime64`` or ISO strings). ``method`` is
    ``"lucas_kanade"`` (dense, default), ``"global"`` (one vector) or ``"none"`` (zero motion:
    Eulerian persistence). Vectors faster than ``max_speed_ms`` are clipped and noted.
    """
    arr = [np.asarray(f, dtype=np.float32) for f in frames]
    if len(arr) < 1:
        raise MotionError("no frames")
    t = np.asarray([np.datetime64(x, "ns") for x in times])
    if t.size != len(arr):
        raise MotionError("frames and times differ in length")
    if method == "none" or len(arr) < 2:
        z = np.zeros(arr[-1].shape, dtype=np.float32)
        return MotionField(
            u=z,
            v=z.copy(),
            cell_m=float(cell_m),
            method="none",
            reliable=np.zeros(arr[-1].shape, bool),
            global_uv=(0.0, 0.0),
            n_pairs=0,
            parameters={"n_frames": 1},
            notes=["zero motion (Eulerian persistence)"]
            if method == "none"
            else ["only one frame: zero motion assumed"],
        )
    if method not in ("lucas_kanade", "global"):
        raise MotionError(f"unknown motion method {method!r}")
    use = arr[-max(2, n_frames) :]
    tt = t[-len(use) :]
    us, vs, rel, gl, notes, wts = [], [], [], [], [], []
    for k in range(len(use) - 1):
        dt = float((tt[k + 1] - tt[k]) / np.timedelta64(1, "s"))
        if not (dt > 0):
            notes.append("a frame pair has no positive time step and was skipped")
            continue
        if method == "global":
            ia, _ = tracking_image(use[k], floor=floor)
            ib, _ = tracking_image(use[k + 1], floor=floor)
            gx, gy, cc = global_translation(ia, ib)
            if cc < MIN_PATTERN_CORRELATION:
                notes.append(
                    f"no coherent pattern between the frames (peak correlation {cc:.2f}): "
                    "zero motion assumed"
                )
                gx = gy = 0.0
            u = np.full(ia.shape, gx)
            v = np.full(ia.shape, gy)
            r = np.zeros(ia.shape, bool)
            g = (gx, gy)
        else:
            u, v, r, g, n = lucas_kanade(use[k], use[k + 1], floor=floor, **options)
            notes += n
        f = float(cell_m) / dt  # cells per frame -> m/s
        us.append(u * f)
        vs.append(v * f)
        rel.append(r)
        gl.append((g[0] * f, g[1] * f))
        wts.append(1.0)
    if not us:
        raise MotionError("no usable frame pair (check the frame times)")
    w = np.asarray(wts) / sum(wts)
    u_m = sum(wi * ui for wi, ui in zip(w, us, strict=True))
    v_m = sum(wi * vi for wi, vi in zip(w, vs, strict=True))
    reliable = np.logical_or.reduce(rel)
    g_u = float(sum(wi * g[0] for wi, g in zip(w, gl, strict=True)))
    g_v = float(sum(wi * g[1] for wi, g in zip(w, gl, strict=True)))
    speed = np.hypot(u_m, v_m)
    if np.any(speed > max_speed_ms):
        fac = np.where(speed > max_speed_ms, max_speed_ms / np.maximum(speed, 1e-9), 1.0)
        u_m, v_m = u_m * fac, v_m * fac
        n_fast = int((speed > max_speed_ms).sum())
        notes.append(f"{n_fast} vectors faster than {max_speed_ms:g} m/s were clipped")
    params = {"n_frames": len(use), "floor_dbz": floor, "max_speed_ms": max_speed_ms}
    params.update({k: v for k, v in options.items() if isinstance(v, (int, float, str))})
    return MotionField(
        u=u_m.astype(np.float32),
        v=v_m.astype(np.float32),
        cell_m=float(cell_m),
        method=method,
        reliable=reliable,
        global_uv=(g_u, g_v),
        n_pairs=len(us),
        parameters=params,
        notes=sorted(set(notes)),
    )
