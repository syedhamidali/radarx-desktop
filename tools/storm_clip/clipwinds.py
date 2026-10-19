"""Observed winds for the storm clip: a multi-Doppler retrieval per time step.

The clip carries the radar echo between two scans along the tracked storm motion and, where winds
exist, along the OBSERVED wind anomaly (see src/lib/utils/flowMorph.ts). The winds here come from the
app's own multi-Doppler retrieval (engine/multidoppler.py, radarx): the radial velocities of two or
more radars give the horizontal wind where their beams cross well. Nothing is invented: a cell
without a well-crossed pair has no wind (NaN) and its echo is only translated. A time step with fewer
than two radars, or a retrieval that fails, has no winds and the clip says so.

Bundle layout (per step, next to the mosaic): ``wind_<i>.json`` (grid, origin, radars, valid share)
and ``wind_<i>.bin``: float32 little-endian, the u block then the v block, each (nz, ny, nx), NaN =
no wind; u east and v north in m/s, grid axes in km of the wind's own AEQD origin.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import numpy as np


def pack_wind(
    fields: dict[str, np.ndarray],
    x_m: np.ndarray,
    y_m: np.ndarray,
    z_m: np.ndarray,
    origin: tuple[float, float],
    n_radars: int,
    method: str,
) -> tuple[dict[str, Any], bytes]:
    """The bundle header and bytes of one wind step from u / v ``(nz, ny, nx)`` fields."""
    u = np.ascontiguousarray(fields["u"], dtype="<f4")
    v = np.ascontiguousarray(fields["v"], dtype="<f4")
    nz, ny, nx = u.shape
    if v.shape != u.shape or len(x_m) != nx or len(y_m) != ny or len(z_m) != nz:
        raise ValueError("wind fields and axes disagree")
    ok = np.isfinite(u) & np.isfinite(v)
    # a cell with only one component is no wind at all
    u = np.where(ok, u, np.nan).astype("<f4")
    v = np.where(ok, v, np.nan).astype("<f4")
    header = {
        "nx": int(nx),
        "ny": int(ny),
        "nz": int(nz),
        "x0_km": float(x_m[0]) / 1000.0,
        "y0_km": float(y_m[0]) / 1000.0,
        "z0_km": float(z_m[0]) / 1000.0,
        "dx_km": float(x_m[1] - x_m[0]) / 1000.0 if nx > 1 else 1.0,
        "dy_km": float(y_m[1] - y_m[0]) / 1000.0 if ny > 1 else 1.0,
        "dz_km": float(z_m[1] - z_m[0]) / 1000.0 if nz > 1 else 0.5,
        "origin": {"lat": float(origin[0]), "lon": float(origin[1])},
        "n_radars": int(n_radars),
        "method": method,
        "valid_fraction": float(ok.mean()),
        "units": "m/s",
    }
    return header, u.tobytes() + v.tobytes()


def retrieve_step_winds(
    files: dict[str, Path],
    origin: tuple[float, float],
    extent_km: tuple[float, float, float, float],
    motion_ms: tuple[float, float],
    opener,
    dx_m: float = 2000.0,
    max_range_km: float = 230.0,
    z_max_m: float = 10000.0,
    log=print,
) -> tuple[dict[str, Any], bytes] | None:
    """Multi-Doppler winds of one step on the box ``extent_km`` = (x0, y0, x1, y1) around ``origin``.

    ``files`` maps radar -> volume file; ``opener(path)`` returns an open engine reader. Returns the
    bundle header and bytes, or None when the retrieval is not possible (fewer than two radars, no
    Doppler velocity, no good beam crossing, radarx missing): the caller then has no winds there.
    """
    from engine import multidoppler as md

    if len(files) < 2:
        log("    winds: fewer than two radars, none")
        return None
    readers = []
    try:
        for radar, path in sorted(files.items()):
            try:
                readers.append((radar, opener(path)))
            except ValueError:  # an unreadable file (e.g. TDWR): that radar is left out
                continue
        if len(readers) < 2:
            log("    winds: fewer than two readable radars, none")
            return None
        grid = {
            "origin": {"lat": origin[0], "lon": origin[1]},
            "extent": {
                "x_min_km": extent_km[0],
                "y_min_km": extent_km[1],
                "x_max_km": extent_km[2],
                "y_max_km": extent_km[3],
            },
            "dx_m": dx_m,
            "dz_m": 500.0,
            "z_max_m": z_max_m,
            "max_range_km": max_range_km,
        }
        options = {
            "method": "analytic",
            "min_crossing_angle": 25.0,
            "storm_motion": [motion_ms[0], motion_ms[1]],
        }
        rec = md.run_multidoppler(readers, grid, options, cache=None)
    except (md.MultiDopplerError, ValueError, KeyError, RuntimeError) as exc:
        log(f"    winds: not retrieved ({exc})")
        return None
    finally:
        for _, r in readers:
            with contextlib.suppress(Exception):
                r.close()
    names = list(md.FIELD_ORDER)
    fields = {n: rec.fields[names.index(n)] for n in ("u", "v")}
    header, data = pack_wind(
        fields, rec.x, rec.y, rec.z, rec.origin, len(readers), options["method"]
    )
    log(
        f"    winds: {header['nx']}x{header['ny']}x{header['nz']} cells, "
        f"{100 * header['valid_fraction']:.0f}% with wind, {len(readers)} radars"
    )
    return header, data
