"""Known answers for the units and derived quantities of model and reanalysis fields."""

from __future__ import annotations

import numpy as np
import pytest

from engine.environment import derive

needs_radarx = pytest.mark.usefixtures("radarx_installed")


@pytest.fixture()
def radarx_installed():
    pytest.importorskip("radarx")


@needs_radarx
def test_dewpoint_from_specific_humidity_known_answer():
    # q = 0.01 kg/kg at 1000 hPa: e = q p / (0.622 + 0.378 q) = 15.98 hPa, which is the saturation
    # vapour pressure at 14.0 degC (Bolton): dew point 14.0 degC
    td = derive.dewpoint_from_specific_humidity(0.01, 100000.0)
    assert td - 273.15 == pytest.approx(14.0, abs=0.1)


PLACES = [(36.75, -88.64), (45.0, -110.0), (30.0, -75.0), (38.5, -97.5)]


@pytest.mark.parametrize("lat, lon", PLACES)
def test_grid_winds_to_earth_agrees_with_an_independent_projection(lat, lon):
    """The rotation against the local axes of the projection computed by PROJ (pyproj)."""
    pyproj = pytest.importorskip("pyproj")
    crs = "+proj=lcc +lat_1=38.5 +lat_2=38.5 +lat_0=38.5 +lon_0=-97.5 +R=6371229 +units=m"
    t = pyproj.Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    x0, y0 = t.transform(lon, lat)
    x1, y1 = t.transform(lon, lat + 0.001)  # a step to true north
    north = np.array([x1 - x0, y1 - y0])
    north /= np.hypot(*north)
    east = np.array([north[1], -north[0]])
    ug, vg = -0.852, 5.490  # a wind read from the file along the grid axes
    ue, ve = derive.grid_winds_to_earth(ug, vg, lon, -97.5, 38.5, 38.5)
    assert ue == pytest.approx(float(np.dot([ug, vg], east)), abs=1e-6)
    assert ve == pytest.approx(float(np.dot([ug, vg], north)), abs=1e-6)


def test_grid_winds_to_earth_known_angle_and_invariants():
    n = np.sin(np.radians(38.5))
    lon = np.array([-97.5, -97.5 + 10.0, -97.5 - 10.0])  # central meridian, 10 deg east, 10 west
    ue, ve = derive.grid_winds_to_earth([10.0, 10.0, 10.0], [0.0, 0.0, 0.0], lon, -97.5, 38.5, 38.5)
    g = np.radians(n * np.array([0.0, 10.0, -10.0]))
    assert ue == pytest.approx(10 * np.cos(g))
    assert ve == pytest.approx(-10 * np.sin(g))
    assert np.hypot(ue, ve) == pytest.approx(10.0)  # a rotation keeps the speed
    # on the central meridian nothing changes
    assert (ue[0], ve[0]) == pytest.approx((10.0, 0.0))
    # a grid-north wind east of the central meridian blows from slightly east of north... the
    # earth-relative eastward part is positive there
    ue2, ve2 = derive.grid_winds_to_earth(0.0, 10.0, -87.5, -97.5, 38.5, 38.5)
    assert ue2 > 0 and ve2 < 10
