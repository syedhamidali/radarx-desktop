"""Value masks: hide the values of a variable below / above / between / outside some limits.

This is the engine side of the viewer's ``stores/valueMask.ts``; both implement the same rules
so a figure, a data file and the screen agree.

A mask is a dict ``{"mode": "off"|"below"|"above"|"between"|"outside", "min": float|None,
"max": float|None, "inclusive": bool}``. It says which values are HIDDEN::

    below    hides  v < min          (inclusive: v <= min)
    above    hides  v > max          (inclusive: v >= max)
    between  hides  min < v < max    (inclusive: min <= v <= max)
    outside  hides  v < min or v > max (inclusive: v <= min or v >= max)

``between`` / ``outside`` take the smaller of the two limits as the lower one. Missing values (NaN)
are never "masked": they are missing already. A mask whose mode needs a limit that is not set is
inactive (it hides nothing). Hidden values are replaced by NaN, never by a number.

Comparisons are done in the dtype of the data (float32 for radar moments), exactly like the
viewer's shaders, so a value that is the float32 form of a limit behaves the same everywhere.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

MODES = ("off", "below", "above", "between", "outside")


def normalize(mask: Any) -> dict[str, Any] | None:
    """A clean mask dict, or ``None`` when *mask* is missing, off, malformed or incomplete."""
    if not isinstance(mask, dict):
        return None
    mode = mask.get("mode")
    if mode not in MODES or mode == "off":
        return None

    def num(v: Any) -> float | None:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        return float(v) if math.isfinite(v) else None

    lo, hi = num(mask.get("min")), num(mask.get("max"))
    needs_lo = mode in ("below", "between", "outside")
    needs_hi = mode in ("above", "between", "outside")
    if (needs_lo and lo is None) or (needs_hi and hi is None):
        return None
    return {"mode": mode, "min": lo, "max": hi, "inclusive": mask.get("inclusive") is True}


def pick(options: dict[str, Any] | None, variable: str) -> dict[str, Any] | None:
    """The active mask for *variable* from export options.

    ``value_masks`` maps variable names to masks (batch exports carry several variables); a single
    ``value_mask`` belongs to the variable named in ``value_mask_variable`` (default: any).
    """
    if not isinstance(options, dict):
        return None
    masks = options.get("value_masks")
    if isinstance(masks, dict) and variable in masks:
        return normalize(masks[variable])
    single = options.get("value_mask")
    if single is not None and options.get("value_mask_variable", variable) == variable:
        return normalize(single)
    return None


def hidden(values: np.ndarray, mask: Any) -> np.ndarray | None:
    """Boolean array, True where *mask* hides the value; ``None`` when the mask hides nothing."""
    m = normalize(mask)
    if m is None:
        return None
    arr = np.asarray(values)
    if arr.dtype.kind not in "fiu":
        return None
    inc = m["inclusive"]
    cast = arr.dtype.type if arr.dtype.kind == "f" else np.float64
    lo = None if m["min"] is None else cast(m["min"])
    hi = None if m["max"] is None else cast(m["max"])
    mode = m["mode"]
    with np.errstate(invalid="ignore"):
        if mode == "below":
            return arr <= lo if inc else arr < lo
        if mode == "above":
            return arr >= hi if inc else arr > hi
        a, b = (min(lo, hi), max(lo, hi))
        if mode == "between":
            return (arr >= a) & (arr <= b) if inc else (arr > a) & (arr < b)
        return (arr <= a) | (arr >= b) if inc else (arr < a) | (arr > b)


def apply(values: np.ndarray, mask: Any) -> tuple[np.ndarray, int]:
    """``(values with NaN where hidden, number of values hidden)``.

    A copy; *values* is untouched. Integer input becomes float64 so NaN can be stored.
    """
    h = hidden(values, mask)
    arr = np.asarray(values)
    if h is None:
        return arr, 0
    out = arr.astype(np.float64) if arr.dtype.kind != "f" else arr.copy()
    n = int(np.count_nonzero(h))
    out[h] = np.nan
    return out, n


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(round(v, 3))


def describe(mask: Any, units: str = "") -> str | None:
    """ "hide below 0 dBZ" (the viewer's wording), or ``None`` when the mask is inactive."""
    m = normalize(mask)
    if m is None:
        return None
    u = f" {units}" if units else ""
    inc, mode = m["inclusive"], m["mode"]
    lo, hi = m["min"], m["max"]
    if mode == "below":
        return f"hide {_fmt(lo)}{u} and below" if inc else f"hide below {_fmt(lo)}{u}"
    if mode == "above":
        return f"hide {_fmt(hi)}{u} and above" if inc else f"hide above {_fmt(hi)}{u}"
    a, b = min(lo, hi), max(lo, hi)
    if mode == "between":
        return (
            f"hide {_fmt(a)} to {_fmt(b)}{u}" if inc else f"hide between {_fmt(a)} and {_fmt(b)}{u}"
        )
    return (
        f"hide outside {_fmt(a)} to {_fmt(b)}{u}, and those limits"
        if inc
        else f"hide outside {_fmt(a)} to {_fmt(b)}{u}"
    )
