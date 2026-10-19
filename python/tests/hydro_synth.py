"""Synthetic radar volumes with a known rain field, for the hydrology tests.

``make_reader(lat, lon, rate_fn, t0)`` builds a PPI volume whose reflectivity is the exact
``Z = 300 R^1.4`` of a rain rate field ``rate_fn(x_m, y_m)`` given in metres east / north of the
radar (the rain column is taken as uniform in height, so the beam height does not matter for the
truth). The tests know the answer: the rain rate of every gate is ``rate_fn`` at its position.
"""

from __future__ import annotations

import numpy as np
import xarray as xr
from engine.reader import RadarReader

R_EFF = 4.0 / 3.0 * 6371e3


def z_of_r(rate: np.ndarray) -> np.ndarray:
    """Reflectivity (dBZ) of the WSR-88D relation ``Z = 300 R^1.4``."""
    with np.errstate(divide="ignore"):
        return 10.0 * np.log10(300.0 * np.power(rate, 1.4))


def make_tree(
    lat: float,
    lon: float,
    rate_fn,
    t0: str,
    *,
    name: str = "SYN",
    elevations=(0.5, 1.5),
    n_range: int = 400,
    gate_m: float = 500.0,
    alt: float = 100.0,
    n_az: int = 360,
    extra: dict | None = None,
) -> xr.DataTree:
    azimuth = (np.arange(n_az) + 0.5) * 360.0 / n_az
    range_m = gate_m / 2 + gate_m * np.arange(n_range)
    sweeps = {}
    for i, el in enumerate(elevations):
        az = np.radians(azimuth)[:, None]
        r = range_m[None, :]
        th = np.radians(el)
        rho = np.sqrt(r**2 + R_EFF**2 + 2 * r * R_EFF * np.sin(th))
        s = R_EFF * np.arcsin(r * np.cos(th) / rho)  # ground range of the beam (flat-Earth free)
        x, y = s * np.sin(az), s * np.cos(az)
        rate = np.asarray(rate_fn(x, y), dtype=np.float64)
        dbz = np.where(rate > 0, z_of_r(np.maximum(rate, 1e-12)), np.nan).astype(np.float32)
        data = {
            "DBZH": (("azimuth", "range"), dbz, {"units": "dBZ"}),
            "sweep_mode": ((), "azimuth_surveillance"),
            "sweep_fixed_angle": ((), float(el)),
        }
        for k, v in (extra or {}).items():
            data[k] = (("azimuth", "range"), np.broadcast_to(v, dbz.shape).astype(np.float32))
        sweeps[f"/sweep_{i}"] = xr.Dataset(
            data,
            coords={
                "azimuth": ("azimuth", azimuth),
                "range": ("range", range_m.astype(np.float32)),
                "elevation": ("azimuth", np.full(n_az, el)),
                "time": (
                    "azimuth",
                    np.datetime64(t0) + (np.arange(n_az) * 40).astype("timedelta64[ms]"),
                ),
            },
        )
    root = xr.Dataset(
        coords={"latitude": np.float64(lat), "longitude": np.float64(lon), "altitude": alt},
        attrs={"instrument_name": name},
    )
    return xr.DataTree.from_dict({"/": root, **sweeps})


def make_reader(*args, **kwargs) -> RadarReader:
    reader = RadarReader()
    reader._dtree = make_tree(*args, **kwargs)
    reader._format = "synthetic"
    return reader
