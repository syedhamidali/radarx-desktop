"""Subsetting helpers for providers: cut a dataset to the requested area, time window and variables.

Providers should subset *at the source* whenever the source allows it (smaller download).
These helpers do the same on an opened dataset, so a provider that has to fetch a whole file can
still keep only what was asked for, and so the semantics are identical everywhere:

* area: inclusive of the box edges; longitudes may be stored 0..360 or -180..180;
* time: inclusive window; ``time_start``/``time_end`` are UTC;
* variables: exact names, unknown names are an error that lists what is available.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from engine.environment.errors import InvalidRequest
from engine.environment.request import Request


def _find(ds: Any, names: tuple[str, ...]) -> str | None:
    for n in names:
        if n in ds.coords or n in ds.dims or n in ds.variables:
            return n
    return None


def subset_dataset(
    ds: Any,
    request: Request,
    *,
    lat: tuple[str, ...] = ("latitude", "lat", "y"),
    lon: tuple[str, ...] = ("longitude", "lon", "x"),
    time: tuple[str, ...] = ("time", "valid_time"),
) -> Any:
    """``ds`` (an ``xarray.Dataset``) restricted to *request*'s variables, time window and box."""
    out = ds
    if request.variables:
        missing = [v for v in request.variables if v not in out.data_vars]
        if missing:
            raise InvalidRequest(
                f"Variable(s) {', '.join(missing)} not in the data. "
                f"Available: {', '.join(sorted(map(str, out.data_vars)))}."
            )
        out = out[list(request.variables)]
    tname = _find(out, time)
    if tname and (request.time_start or request.time_end):
        lo = np.datetime64(request.time_start[:-1]) if request.time_start else None
        hi = np.datetime64(request.time_end[:-1]) if request.time_end else None
        out = out.sel({tname: slice(lo, hi)})
    if request.bbox is not None:
        west, south, east, north = request.bbox
        latn, lonn = _find(out, lat), _find(out, lon)
        if latn and lonn:
            lons = np.asarray(out[lonn].values)
            if lons.size and float(np.nanmax(lons)) > 180.0:  # 0..360 storage
                west, east = west % 360.0, east % 360.0
            lats = np.asarray(out[latn].values)
            if lats.ndim == 1 and lons.ndim == 1:
                latsel = (lats >= south) & (lats <= north)
                if west <= east:
                    lonsel = (lons >= west) & (lons <= east)
                else:  # a box over the 0/360 seam
                    lonsel = (lons >= west) | (lons <= east)
                out = out.isel({latn: np.nonzero(latsel)[0], lonn: np.nonzero(lonsel)[0]})
    return out
