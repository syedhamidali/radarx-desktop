"""Radar sweep renderer — fast NumPy-based PPI rasterization."""

from __future__ import annotations

import base64
import functools
import hashlib
import io
import logging
import math
import threading
from collections import OrderedDict
from typing import Any

import colorcet as cc
import numpy as np
import xarray as xr
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# --- Rendering constraints ---
MIN_DIMENSION = 1
MAX_DIMENSION = 10000
DEFAULT_WIDTH = 800
DEFAULT_HEIGHT = 800
DEFAULT_BACKGROUND = "black"
PNG_FORMAT = "PNG"
# zlib level for PNG encoding.  The image goes over a local websocket, so CPU
# time matters far more than size: level 1 is ~4-5x faster than the PIL default
# (6) for a modest size increase.
PNG_COMPRESS_LEVEL = 1

_PALETTES: dict[str, list[str]] = {
    "rainbow": cc.rainbow,
    "coolwarm": cc.coolwarm,
    "fire": cc.fire,
    "blues": cc.blues,
    "bgy": cc.bgy,
    "kr": cc.kr,
}

_VARIABLE_CMAPS: dict[str, str] = {
    "DBZH": "rainbow",
    "DBZ": "rainbow",
    "reflectivity": "rainbow",
    "corrected_reflectivity": "rainbow",
    "total_power": "rainbow",
    "VRADH": "coolwarm",
    "VEL": "coolwarm",
    "velocity": "coolwarm",
    "corrected_velocity": "coolwarm",
    "WRADH": "fire",
    "WIDTH": "fire",
    "spectrum_width": "fire",
    "ZDR": "bgy",
    "differential_reflectivity": "bgy",
    "RHOHV": "blues",
    "cross_correlation_ratio": "blues",
    "PHIDP": "fire",
    "differential_phase": "fire",
    "KDP": "bgy",
    "specific_differential_phase": "bgy",
}

_VARIABLE_RANGES: dict[str, tuple[float, float]] = {
    "DBZH": (-20.0, 75.0),
    "DBZ": (-20.0, 75.0),
    "reflectivity": (-20.0, 75.0),
    "corrected_reflectivity": (-20.0, 75.0),
    "total_power": (-20.0, 75.0),
    "VRADH": (-30.0, 30.0),
    "VEL": (-30.0, 30.0),
    "velocity": (-30.0, 30.0),
    "corrected_velocity": (-30.0, 30.0),
    "WRADH": (0.0, 10.0),
    "WIDTH": (0.0, 10.0),
    "spectrum_width": (0.0, 10.0),
    "ZDR": (-1.0, 5.0),
    "differential_reflectivity": (-1.0, 5.0),
    "RHOHV": (0.0, 1.05),
    "cross_correlation_ratio": (0.0, 1.05),
    "PHIDP": (0.0, 180.0),
    "differential_phase": (0.0, 180.0),
    "KDP": (-2.0, 6.0),
    "specific_differential_phase": (-2.0, 6.0),
}


# ---------------------------------------------------------------------------
# Colorbar helpers
# ---------------------------------------------------------------------------

# Semi-transparent dark background for the colorbar strip (RGBA).
_CB_BG: tuple[int, int, int, int] = (15, 15, 15, 200)
# Width of the color-gradient swatch in pixels.
_CB_SWATCH_W: int = 18
# Padding on all sides (px).
_CB_PAD: int = 5
# Total colorbar strip width: swatch + left-pad + right-pad + ~18 px for labels.
_CB_STRIP_W: int = 45


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Convert a CSS hex color string (e.g. '#ab12ef') to an (R, G, B) tuple."""
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


@functools.lru_cache(maxsize=8)
def _cb_font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    """Return a PIL font at *size* points, falling back to the built-in bitmap.

    Cached: loading a TrueType face from disk on every render is wasteful.
    """
    candidates = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/SFNSText.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "DejaVuSans.ttf",
        "arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _fmt_tick(v: float) -> str:
    """Format a tick value compactly."""
    if v == int(v) and abs(v) < 1e5:
        return str(int(v))
    return f"{v:.1f}"


def _build_colorbar(
    palette: list[str],
    vmin: float,
    vmax: float,
    variable: str,
    units: str,
    height: int,
) -> Image.Image:
    """
    Build a vertical colorbar strip (RGBA) of size (_CB_STRIP_W x height).

    Layout within the strip:
        [_CB_PAD] [title row ~11 px] [gradient swatch _CB_SWATCH_W wide]
        with tick labels to the right of the swatch.

    Gradient direction: top = vmax color, bottom = vmin color.
    """
    strip_w = _CB_STRIP_W
    img = Image.new("RGBA", (strip_w, height), _CB_BG)
    draw = ImageDraw.Draw(img)

    font_sm = _cb_font(9)
    font_title = font_sm

    # Vertical regions
    title_h = 11  # px at top for variable / unit label
    bottom_label_h = 11  # px at bottom so the min tick label isn't clipped
    grad_y0 = _CB_PAD + title_h
    grad_y1 = height - _CB_PAD - bottom_label_h
    grad_h = max(grad_y1 - grad_y0, 1)

    swatch_x0 = _CB_PAD
    swatch_x1 = swatch_x0 + _CB_SWATCH_W

    # --- colour gradient (vectorised) ---
    # top row -> vmax (palette end), bottom row -> vmin (palette start)
    pal_rgb = _palette_lut(tuple(palette))[:-1, :3]
    n = len(pal_rgb)
    frac = 1.0 - np.arange(grad_h) / grad_h
    idx = np.minimum((frac * n).astype(np.intp), n - 1)
    strip = np.empty((grad_h, _CB_SWATCH_W, 4), dtype=np.uint8)
    strip[..., :3] = pal_rgb[idx][:, None, :]
    strip[..., 3] = 255
    img.paste(Image.fromarray(strip), (swatch_x0, grad_y0))

    # --- tick labels ---
    lx = swatch_x1 + 2  # x position for label text

    def _place_label(text: str, row_y: int, color: tuple[int, int, int, int]) -> None:
        draw.text((lx, row_y), text, font=font_sm, fill=color)

    bright = (220, 220, 220, 255)
    mid_col = (180, 180, 180, 255)

    _place_label(_fmt_tick(vmax), grad_y0, bright)
    _place_label(_fmt_tick((vmin + vmax) / 2.0), grad_y0 + grad_h // 2 - 4, mid_col)
    _place_label(_fmt_tick(vmin), grad_y1 - 9, bright)

    # --- title: variable name + units ---
    title = f"{variable}" if not units else f"{variable} ({units})"
    # Truncate to fit the strip width (≤ 7 chars + ellipsis)
    if len(title) > 8:
        title = title[:7] + "…"
    draw.text((_CB_PAD, _CB_PAD), title, font=font_title, fill=(210, 210, 210, 255))

    return img


@functools.lru_cache(maxsize=16)
def _cached_colorbar(
    palette: tuple[str, ...],
    vmin: float,
    vmax: float,
    variable: str,
    units: str,
    height: int,
) -> Image.Image:
    """Memoised :func:`_build_colorbar` (the strip is never mutated by callers)."""
    return _build_colorbar(list(palette), vmin, vmax, variable, units, height)


def _composite_colorbar(
    radar_img: Image.Image,
    palette: list[str],
    vmin: float,
    vmax: float,
    variable: str,
    units: str,
) -> Image.Image:
    """
    Composite a colorbar strip onto the right side of *radar_img*.

    Returns a new RGBA image that is _CB_STRIP_W pixels wider.
    """
    w, h = radar_img.size
    colorbar = _cached_colorbar(tuple(palette), float(vmin), float(vmax), variable, units, h)

    # Create output canvas (radar image + colorbar side-by-side)
    out = Image.new("RGBA", (w + _CB_STRIP_W, h), (0, 0, 0, 255))
    # Paste the radar frame (convert to RGBA so alpha_composite works uniformly)
    out.paste(radar_img.convert("RGBA"), (0, 0))
    # Alpha-composite the colorbar so its semi-transparent background blends
    out.alpha_composite(colorbar, dest=(w, 0))
    return out


def _resolve_cmap(variable: str) -> list[str]:
    name = _VARIABLE_CMAPS.get(variable)
    if name is not None:
        return _PALETTES.get(name, cc.rainbow)
    vlow = variable.lower()
    if any(tok in vlow for tok in ("dbz", "reflectivity", "power")):
        return cc.rainbow
    if any(tok in vlow for tok in ("vel", "vrad")):
        return cc.coolwarm
    return cc.fire


def _resolve_range(variable: str) -> tuple[float | None, float | None]:
    r = _VARIABLE_RANGES.get(variable)
    return r if r is not None else (None, None)


def _polar_to_cartesian(
    azimuth: np.ndarray,
    range_m: np.ndarray,
    values: np.ndarray,
):
    """Convert polar radar gates to finite Cartesian points.

    Coordinates use radar convention: azimuth 0 points north (+y), and
    azimuth 90 points east (+x).
    """
    import pandas as pd

    az = np.asarray(azimuth, dtype=np.float64)
    rng = np.asarray(range_m, dtype=np.float64)
    vals = np.asarray(values)
    if vals.shape != (az.size, rng.size):
        raise ValueError(
            f"values shape {vals.shape} does not match azimuth/range ({az.size}, {rng.size})"
        )

    az_rad = np.deg2rad(az)[:, None]
    x = np.sin(az_rad) * rng[None, :]
    y = np.cos(az_rad) * rng[None, :]
    finite = np.isfinite(vals)

    return pd.DataFrame(
        {
            "x": x[finite],
            "y": y[finite],
            "val": vals[finite],
        }
    )


# ---------------------------------------------------------------------------
# PPI rasterization
# ---------------------------------------------------------------------------

# Minimum / maximum size (power of two) of the azimuth lookup table.
_AZ_LUT_MIN = 4096
_AZ_LUT_MAX = 1 << 18
# A pixel whose nearest ray is farther than this many median ray spacings is
# left blank (sector scans / missing rays) instead of smeared from the edge.
_AZ_GAP_TOLERANCE = 1.5

_INDEX_MAP_CACHE: OrderedDict[tuple, np.ndarray] = OrderedDict()
_INDEX_MAP_CACHE_MAX = 3
_INDEX_MAP_LOCK = threading.Lock()


def _build_az_lut(azimuth: np.ndarray, n_rows_stride: int) -> tuple[np.ndarray, int]:
    """Build a 1-D table mapping a fine azimuth bin to a flat row offset.

    The table has ``M`` (a power of two) entries covering [0, 360).  Entry *k*
    holds ``row * n_rows_stride`` for the ray circularly nearest to the bin
    centre, or ``n * n_rows_stride`` (the padding row) when no ray is within
    ``_AZ_GAP_TOLERANCE`` median ray spacings.
    """
    n = azimuth.size
    az = np.mod(azimuth, 360.0)
    order = np.argsort(az, kind="stable")
    az_sorted = az[order]

    if n >= 2:
        gaps = np.diff(np.concatenate([az_sorted, [az_sorted[0] + 360.0]]))
        med = float(np.median(gaps))
    else:
        med = 1.0
    if med <= 0.0:
        med = 360.0 / max(n, 1)
    m = int(2 ** math.ceil(math.log2(max(64.0 * 360.0 / med, 2.0))))
    m = min(max(m, _AZ_LUT_MIN), _AZ_LUT_MAX)

    centres = (np.arange(m, dtype=np.float64) + 0.5) * (360.0 / m)
    # Extend circularly so that wrap-around neighbours are ordinary neighbours.
    ext = np.concatenate([[az_sorted[-1] - 360.0], az_sorted, [az_sorted[0] + 360.0]])
    hi = np.searchsorted(ext, centres, side="left")  # in [1, n+1]
    lo = hi - 1
    d_hi = ext[hi] - centres
    d_lo = centres - ext[lo]
    use_lo = d_lo <= d_hi
    nearest_ext = np.where(use_lo, lo, hi)
    dist = np.where(use_lo, d_lo, d_hi)
    nearest = (nearest_ext - 1) % n  # index into az_sorted
    rows = order[nearest].astype(np.intp)

    tol = _AZ_GAP_TOLERANCE * med if n >= 2 else 0.5
    lut = rows * n_rows_stride
    lut[dist > tol] = n * n_rows_stride
    return lut, m


def _range_index(r_pix: np.ndarray, range_m: np.ndarray) -> np.ndarray:
    """Nearest-gate index for each pixel range; ``len(range_m)`` marks "no data".

    Gates are treated as intervals centred on their range value, so the
    annulus covers half a gate beyond the first/last centre.
    """
    nr = range_m.size
    if nr == 1:
        dr = 1.0
        ri = np.where(np.abs(r_pix - range_m[0]) <= 0.5 * dr, 0, nr)
        return ri.astype(np.int32)

    d0 = float(range_m[1] - range_m[0])
    expected = range_m[0] + d0 * np.arange(nr)
    if d0 > 0 and float(np.max(np.abs(range_m - expected))) < 1e-3 * d0:
        # Uniform gate spacing: pure arithmetic, no search.
        t = r_pix - np.float32(range_m[0])
        t *= np.float32(1.0 / d0)
        np.rint(t, out=t)
        ri = t.astype(np.int32)
        # negatives wrap to huge unsigned values; one minimum() catches both ends
        np.minimum(ri.view(np.uint32), np.uint32(nr), out=ri.view(np.uint32))
        return ri

    edges = 0.5 * (range_m[1:] + range_m[:-1])
    lo = range_m[0] - 0.5 * (range_m[1] - range_m[0])
    hi = range_m[-1] + 0.5 * (range_m[-1] - range_m[-2])
    ri = np.searchsorted(edges, r_pix).astype(np.int32)
    ri[(r_pix < lo) | (r_pix > hi)] = nr
    return ri


def _build_index_map(
    azimuth: np.ndarray,
    range_m: np.ndarray,
    width: int,
    height: int,
    x_range: tuple[float, float],
    y_range: tuple[float, float],
) -> np.ndarray:
    """Flat indices into the padded ``(n_az + 1, n_rng + 1)`` value array.

    Depends only on geometry, so it is shared by every variable rendered for
    the same sweep/viewport.
    """
    nr = range_m.size
    x_min, x_max = x_range
    y_min, y_max = y_range

    # Pixel *centres*: pixel i spans [x_min + i*dx, x_min + (i+1)*dx].
    col = (x_min + (np.arange(width) + 0.5) * ((x_max - x_min) / width)).astype(np.float32)
    row = (y_max - (np.arange(height) + 0.5) * ((y_max - y_min) / height)).astype(np.float32)

    # Range (float32; the 2-D grid is built by broadcasting, no meshgrid).
    r_pix = np.square(row)[:, None] + np.square(col)[None, :]
    np.sqrt(r_pix, out=r_pix)
    ri = _range_index(r_pix, range_m)
    del r_pix

    # Azimuth bin via a fine LUT; M is a power of two so wrapping is a bitmask.
    lut, m = _build_az_lut(azimuth, nr + 1)
    ang = np.arctan2(col[None, :], row[:, None])  # float32, [-pi, pi]; 0 = north, cw
    ang *= np.float32(m / (2.0 * math.pi))
    ang += np.float32(m)  # make positive; M is a multiple of M
    bins = ang.astype(np.int32)
    del ang
    bins &= m - 1

    flat = lut[bins]  # intp
    flat += ri
    return flat


def _get_index_map(
    azimuth: np.ndarray,
    range_m: np.ndarray,
    width: int,
    height: int,
    x_range: tuple[float, float],
    y_range: tuple[float, float],
) -> np.ndarray:
    h = hashlib.blake2b(digest_size=16)
    h.update(np.ascontiguousarray(azimuth, dtype=np.float64).tobytes())
    h.update(np.ascontiguousarray(range_m, dtype=np.float64).tobytes())
    key = (h.digest(), width, height, tuple(map(float, x_range)), tuple(map(float, y_range)))
    with _INDEX_MAP_LOCK:
        hit = _INDEX_MAP_CACHE.get(key)
        if hit is not None:
            _INDEX_MAP_CACHE.move_to_end(key)
            return hit
    flat = _build_index_map(azimuth, range_m, width, height, x_range, y_range)
    with _INDEX_MAP_LOCK:
        _INDEX_MAP_CACHE[key] = flat
        while len(_INDEX_MAP_CACHE) > _INDEX_MAP_CACHE_MAX:
            _INDEX_MAP_CACHE.popitem(last=False)
    return flat


def _rasterize_ppi(
    azimuth: np.ndarray,
    range_m: np.ndarray,
    values: np.ndarray,
    width: int,
    height: int,
    x_range: tuple[float, float],
    y_range: tuple[float, float],
) -> np.ndarray:
    """Inverse-map rasterization: for each output pixel, find nearest (az, range) gate.

    Produces a gap-free PPI image.  Azimuth lookup is circular (wraps at
    0/360), range lookup picks the *nearest* gate, and pixels outside the data
    annulus (or in an unscanned sector) are NaN.  The pixel->gate index map
    only depends on geometry and is cached, so re-rendering another variable
    of the same sweep is a single gather.

    Returns a (height, width) float32 array with NaN for missing data.
    """
    azimuth = np.asarray(azimuth, dtype=np.float64)
    range_m = np.asarray(range_m, dtype=np.float64)
    values = np.asarray(values)
    if values.shape != (azimuth.size, range_m.size):
        raise ValueError(
            f"values shape {values.shape} does not match azimuth/range "
            f"({azimuth.size}, {range_m.size})"
        )
    n_az, nr = values.shape
    if n_az == 0 or nr == 0:
        return np.full((height, width), np.nan, dtype=np.float32)

    # The range search assumes ascending range; reorder columns otherwise.
    if nr > 1 and not np.all(np.diff(range_m) > 0):
        order = np.argsort(range_m, kind="stable")
        range_m = range_m[order]
        values = values[:, order]

    flat = _get_index_map(azimuth, range_m, width, height, x_range, y_range)

    # Padded copy: extra NaN row/column absorb every "no data" index.
    padded = np.full((n_az + 1, nr + 1), np.nan, dtype=np.float32)
    padded[:n_az, :nr] = values
    out = padded.ravel()[flat]
    return out.reshape(height, width)


@functools.lru_cache(maxsize=32)
def _palette_lut(palette: tuple[str, ...]) -> np.ndarray:
    """(n_colors + 1, 4) uint8 RGBA LUT; the last row is opaque black (NaN)."""
    lut = np.zeros((len(palette) + 1, 4), dtype=np.uint8)
    for i, c in enumerate(palette):
        lut[i, :3] = _hex_to_rgb(c)
    lut[:, 3] = 255
    return lut


def _apply_colormap(
    grid: np.ndarray,
    palette: list[str],
    vmin: float,
    vmax: float,
) -> Image.Image:
    """Apply a colorcet palette to a 2D float grid, returning an RGBA PIL Image.

    NaN values become the background colour (black, fully opaque).
    """
    h, w = grid.shape
    lut = _palette_lut(tuple(palette))
    n_colors = lut.shape[0] - 1
    lut32 = np.ascontiguousarray(lut).view(np.uint32).reshape(-1)

    span = vmax - vmin
    if not span > 0:
        span = 1.0
    scale = np.float32((n_colors - 1) / span)

    g = np.asarray(grid, dtype=np.float32)
    with np.errstate(invalid="ignore"):
        t = g - np.float32(vmin)
        t *= scale
        t += np.float32(0.5)  # round to nearest colour
        np.clip(t, 0.0, n_colors - 1, out=t)
    # NaN -> the opaque-black LUT row (avoids undefined NaN -> int casts too)
    np.nan_to_num(t, copy=False, nan=float(n_colors))
    idx = t.astype(np.intp)

    rgba = lut32[idx].view(np.uint8).reshape(h, w, 4)
    return Image.fromarray(rgba)


def _normalize_cache_token(token: Any) -> Any:
    return token if token is None or isinstance(token, (str, int, float, tuple)) else str(token)


@functools.lru_cache(maxsize=8)
def _overlay_layer(
    w: int,
    h: int,
    x_range: tuple[float, float],
    y_range: tuple[float, float],
) -> Image.Image:
    """Transparent RGBA layer with range rings, labels and crosshair.

    Depends only on the viewport geometry, so it is memoised and composited
    onto every frame (callers must not mutate the returned image).
    """
    x_min, x_max = x_range
    y_min, y_max = y_range
    data_w = x_max - x_min  # metres spanning full image width
    data_h = y_max - y_min  # metres spanning full image height

    def data_to_pixel(xd: float, yd: float) -> tuple[int, int]:
        """Map data-space (metres) to pixel (col, row)."""
        px = int((xd - x_min) / data_w * w)
        py = int((y_max - yd) / data_h * h)
        return px, py

    # x and y scales are equal for a square, symmetric canvas; use x.
    px_per_m = w / data_w

    # Radar is always centred at data (0, 0)
    cx, cy = data_to_pixel(0.0, 0.0)

    lo_x, hi_x = sorted((x_min, x_max))
    lo_y, hi_y = sorted((y_min, y_max))
    origin_visible = lo_x <= 0.0 <= hi_x and lo_y <= 0.0 <= hi_y
    if origin_visible:
        max_range_m = min(abs(x_min), abs(x_max), abs(y_min), abs(y_max))
        min_range_m = 0.0
        if max_range_m <= 0.0:  # origin on the viewport edge
            max_range_m = max(abs(x_min), abs(x_max), abs(y_min), abs(y_max))
    else:
        corners = [(x, y) for x in (lo_x, hi_x) for y in (lo_y, hi_y)]
        max_range_m = max(math.hypot(x, y) for x, y in corners)
        # nearest point of the viewport to the origin
        min_range_m = math.hypot(min(max(0.0, lo_x), hi_x), min(max(0.0, lo_y), hi_y))

    ring_radii_m: list[float] = []
    if max_range_m > 0.0:
        ring_interval = RadarRenderer._pick_ring_interval(max_range_m)
        if ring_interval > 0.0:
            ring_radii_m = [
                ring_interval * i
                for i in range(1, int(max_range_m / ring_interval) + 1)
                if ring_interval * i <= max_range_m * 1.01
                and ring_interval * i >= min_range_m - ring_interval
            ]

    # --- draw on a transparent layer so we can composite with alpha ---
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    ring_color = (220, 220, 220, 80)  # soft white, semi-transparent
    cross_color = (220, 220, 220, 60)  # slightly more transparent
    label_color = (255, 255, 255, 200)  # near-opaque white text
    font = _cb_font(11)

    for r_m in ring_radii_m:
        r_px = r_m * px_per_m
        draw.ellipse([cx - r_px, cy - r_px, cx + r_px, cy + r_px], outline=ring_color, width=1)

        # Label at the top of the ring (12 o'clock position), centred.
        label = f"{round(r_m / 1000)} km"
        left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
        text_w = right - left
        text_h = bottom - top
        draw.text((cx - text_w / 2, cy - r_px - text_h - 2), label, fill=label_color, font=font)

    # --- Crosshair ---
    draw.line([(0, cy), (w - 1, cy)], fill=cross_color, width=1)
    draw.line([(cx, 0), (cx, h - 1)], fill=cross_color, width=1)
    return overlay


class RadarRenderer:
    """Renders radar sweep data into base64-encoded PNG images.

    Uses fast NumPy inverse-mapping rasterization with cached pixel->gate
    index maps, a LUT-based colormap and a low-compression PNG encoder.
    """

    def __init__(self) -> None:
        # Real LRU: (cache_token, variable, sweep, width, height) -> result dict
        self._cache: OrderedDict[tuple, dict[str, Any]] = OrderedDict()
        self._max_cache = 50
        self._lock = threading.Lock()

    def invalidate_cache(self) -> None:
        """Clear the render cache (call when a new file is opened)."""
        with self._lock:
            self._cache.clear()

    def release_memory(self) -> None:
        """Drop every re-computable cache (rendered tiles and pixel->gate index maps)."""
        self.invalidate_cache()
        with _INDEX_MAP_LOCK:
            _INDEX_MAP_CACHE.clear()

    @staticmethod
    def _data_fingerprint(data: xr.DataArray) -> str:
        """Content hash used as the cache identity when no ``cache_token`` is given."""
        h = hashlib.blake2b(digest_size=16)
        vals = np.ascontiguousarray(data.values)
        h.update(str(vals.shape).encode())
        h.update(str(vals.dtype).encode())
        h.update(vals.tobytes())
        for name in ("azimuth", "range"):
            if name in data.coords:
                h.update(np.ascontiguousarray(data.coords[name].values).tobytes())
        return h.hexdigest()

    def render_sweep(
        self,
        data: xr.DataArray,
        variable: str,
        sweep: int,
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
        bbox: list[float] | None = None,
        cache_token: Any = None,
    ) -> dict[str, Any]:
        """Render one sweep to a base64 PNG.

        ``cache_token`` identifies the data source (e.g. the server's file id)
        and is part of the cache key so that several open files never share
        cached frames.  When omitted, a content hash of the data is used
        instead (correct, but costs a pass over the array).
        """
        # Validate numeric parameters
        if not isinstance(width, int) or width < MIN_DIMENSION or width > MAX_DIMENSION:
            raise ValueError(
                f"width must be an integer between {MIN_DIMENSION} and {MAX_DIMENSION}, got {width}"
            )
        if not isinstance(height, int) or height < MIN_DIMENSION or height > MAX_DIMENSION:
            raise ValueError(
                f"height must be an integer between {MIN_DIMENSION}"
                f" and {MAX_DIMENSION}, got {height}"
            )
        if not isinstance(sweep, int) or sweep < 0:
            raise ValueError(f"sweep must be a non-negative integer, got {sweep}")
        if bbox is not None:
            if len(bbox) != 4 or not all(isinstance(v, (int, float)) for v in bbox):
                raise ValueError("bbox must be a list of exactly 4 numeric values")
            if not all(math.isfinite(v) for v in bbox) or bbox[0] == bbox[2] or bbox[1] == bbox[3]:
                raise ValueError("bbox must be finite with non-zero width and height")

        # Check cache (bbox not cached — only default views)
        token = _normalize_cache_token(cache_token)
        cache_key: tuple | None = None
        if bbox is None:
            if token is None:
                token = "content:" + self._data_fingerprint(data)
            cache_key = (token, variable, sweep, width, height)
            with self._lock:
                cached = self._cache.get(cache_key)
                if cached is not None:
                    self._cache.move_to_end(cache_key)
                    logger.info("Cache hit for %s sweep=%d %dx%d", variable, sweep, width, height)
                    return cached

        logger.info("Rendering variable=%s sweep=%d size=%dx%d", variable, sweep, width, height)

        # Extract unit string from xarray attributes (best-effort).
        units: str = str(data.attrs.get("units", data.attrs.get("unit", "")))

        if "azimuth" in data.dims and "range" in data.dims and data.ndim == 2:
            data = data.transpose("azimuth", "range")
        values = data.values  # evaluate (possibly lazy) data exactly once
        azimuth, range_m = self._extract_polar_coords(data)

        if not np.isfinite(values).any():
            return self._empty_result(variable, sweep, width, height)

        vmin, vmax = _resolve_range(variable)

        # Fall back to actual data range when no entry in _VARIABLE_RANGES.
        if vmin is None:
            vmin = float(np.nanmin(values))
        if vmax is None:
            vmax = float(np.nanmax(values))

        if bbox:
            x_range = (float(bbox[0]), float(bbox[2]))
            y_range = (float(bbox[1]), float(bbox[3]))
        else:
            max_r = float(range_m.max())
            if not max_r > 0.0:
                max_r = 1.0
            x_range = (-max_r, max_r)
            y_range = (-max_r, max_r)

        # Fast numpy-based PPI rasterization (inverse mapping)
        grid = _rasterize_ppi(azimuth, range_m, values, width, height, x_range, y_range)

        # Apply colormap
        cmap_palette = _resolve_cmap(variable)
        pil_img = _apply_colormap(grid, cmap_palette, vmin, vmax)

        # Overlays
        pil_img = self._draw_overlays(pil_img, x_range, y_range)
        pil_img = _composite_colorbar(pil_img, cmap_palette, vmin, vmax, variable, units)

        # Encode PNG (low compression: this goes over a local websocket)
        buf = io.BytesIO()
        # The frame is fully opaque, so RGB is enough (smaller, faster to encode).
        pil_img.convert("RGB").save(buf, format=PNG_FORMAT, compress_level=PNG_COMPRESS_LEVEL)
        png = buf.getvalue()
        b64 = base64.b64encode(png).decode("ascii")

        logger.info("Render complete — %d bytes PNG", len(png))

        result = {
            "image": b64,
            "metadata": {
                "variable": variable,
                "sweep": sweep,
                "width": width,
                "height": height,
                "units": units,
                "vmin": vmin,
                "vmax": vmax,
                "x_range": [x_range[0] / 1000.0, x_range[1] / 1000.0],
                "y_range": [y_range[0] / 1000.0, y_range[1] / 1000.0],
            },
        }

        # Store in cache (LRU eviction)
        if cache_key is not None:
            with self._lock:
                self._cache[cache_key] = result
                self._cache.move_to_end(cache_key)
                while len(self._cache) > self._max_cache:
                    self._cache.popitem(last=False)

        return result

    # ------------------------------------------------------------------
    # Overlay helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pick_ring_interval(max_range_m: float) -> float:
        """Return a ring spacing (metres) that yields 4-5 rings for *max_range_m*."""
        if not max_range_m > 0.0:
            return 0.0
        # Candidate spacings in metres (10 km → 500 km)
        candidates = [
            10_000,
            20_000,
            25_000,
            50_000,
            100_000,
            150_000,
            200_000,
            250_000,
            500_000,
        ]
        for spacing in candidates:
            n_rings = int(max_range_m / spacing)
            if 4 <= n_rings <= 5:
                return spacing
        # Fallback: just divide into 5 equal rings
        return max_range_m / 5.0

    @staticmethod
    def _draw_overlays(
        pil_img: Image.Image,
        x_range: tuple[float, float],
        y_range: tuple[float, float],
    ) -> Image.Image:
        """Overlay range rings, crosshair, and range labels onto *pil_img*.

        Coordinate system:
            - Data space: x_range / y_range in metres, centre = (0, 0).
            - Pixel space: (0, 0) = top-left corner of the image; y=y_range[1]
              is at the top edge and y=y_range[0] at the bottom edge.

        The overlay layer is memoised per viewport (see :func:`_overlay_layer`).
        """
        img = pil_img if pil_img.mode == "RGBA" else pil_img.convert("RGBA")
        w, h = img.size
        layer = _overlay_layer(
            w,
            h,
            (float(x_range[0]), float(x_range[1])),
            (float(y_range[0]), float(y_range[1])),
        )
        return Image.alpha_composite(img, layer)

    def _empty_result(self, variable: str, sweep: int, width: int, height: int) -> dict[str, Any]:
        img = Image.new("RGBA", (width, height), (0, 0, 0, 255))
        buf = io.BytesIO()
        img.save(buf, format=PNG_FORMAT, compress_level=PNG_COMPRESS_LEVEL)
        return {
            "image": base64.b64encode(buf.getvalue()).decode("ascii"),
            "metadata": {"variable": variable, "sweep": sweep, "width": width, "height": height},
        }

    @staticmethod
    def _extract_polar_coords(
        data: xr.DataArray,
    ) -> tuple[np.ndarray, np.ndarray]:
        if "azimuth" in data.coords:
            azimuth = data.coords["azimuth"].values.astype(float)
        elif "azimuth" in data.dims:
            azimuth = np.arange(data.sizes["azimuth"], dtype=float)
        else:
            azimuth = np.arange(data.shape[0], dtype=float)

        if "range" in data.coords:
            range_m = data.coords["range"].values.astype(float)
        elif "range" in data.dims:
            range_m = np.arange(data.sizes.get("range", data.shape[-1]), dtype=float)
        else:
            range_m = np.arange(data.shape[-1], dtype=float)

        return azimuth, range_m
