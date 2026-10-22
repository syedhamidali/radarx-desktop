"""Known answers for the units and derived quantities of model and reanalysis fields."""

from __future__ import annotations

import numpy as np
import pytest

from engine.environment import derive


def test_wind_speed_and_direction_of_the_four_cardinal_winds():
    # u east, v north; the direction is where the wind blows FROM
    speed, direction = derive.wind_speed_direction([0, -10, 0, 10], [-10, 0, 10, 0])
    assert speed.tolist() == [10, 10, 10, 10]
    assert direction.tolist() == pytest.approx([0, 90, 180, 270])


def test_wind_speed_direction_345_triangle_and_calm():
    s, d = derive.wind_speed_direction([3.0, 0.0], [4.0, 0.0])
    assert s[0] == pytest.approx(5.0)
    assert d[0] == pytest.approx(180 + np.degrees(np.arctan(3 / 4)), abs=1e-9)  # from the SSW
    assert s[1] == 0 and np.isnan(d[1])  # calm: no direction, never 0


def test_wind_components_round_trip():
    rng = np.random.default_rng(1)
    speed = rng.uniform(1, 40, 200)
    direction = rng.uniform(0, 360, 200)
    u, v = derive.wind_components(speed, direction)
    s2, d2 = derive.wind_speed_direction(u, v)
    assert s2 == pytest.approx(speed)
    assert d2 == pytest.approx(direction % 360, abs=1e-9)


def test_saturation_vapour_pressure_bolton():
    # Bolton (1980) eq. 10 at 20 degC gives 23.3 hPa; the Smithsonian tables give 23.37 hPa
    assert derive.saturation_vapour_pressure(293.15) / 100 == pytest.approx(23.3, abs=0.15)
    assert derive.saturation_vapour_pressure(273.15) / 100 == pytest.approx(6.112, abs=1e-6)


def test_dewpoint_from_relative_humidity():
    # 20 degC and 50 % -> 9.3 degC (the usual table value, 9.26)
    td = derive.dewpoint_from_rh(293.15, 50.0)
    assert td - 273.15 == pytest.approx(9.3, abs=0.1)
    # saturated air: dew point equals the temperature
    assert derive.dewpoint_from_rh(285.0, 100.0) == pytest.approx(285.0, abs=1e-9)
    assert np.isnan(derive.dewpoint_from_rh(285.0, 0.0))


def test_dewpoint_and_relative_humidity_are_inverses():
    t = np.linspace(250, 310, 13)
    rh = np.linspace(5, 100, 13)
    td = derive.dewpoint_from_rh(t, rh)
    assert derive.rh_from_dewpoint(t, td) == pytest.approx(rh, rel=1e-9)
    assert np.all(td <= t + 1e-9)


def test_dewpoint_from_specific_humidity_known_answer():
    # q = 0.01 kg/kg at 1000 hPa: e = q p / (0.622 + 0.378 q) = 15.98 hPa, which is the saturation
    # vapour pressure at 14.0 degC (Bolton): dew point 14.0 degC
    td = derive.dewpoint_from_specific_humidity(0.01, 100000.0)
    assert td - 273.15 == pytest.approx(14.0, abs=0.1)
    e = derive.vapour_pressure_from_specific_humidity(0.01, 100000.0)
    assert e / 100 == pytest.approx(15.98, abs=0.02)


def test_mixing_ratio_and_geopotential_height():
    assert derive.mixing_ratio_from_specific_humidity(0.01) == pytest.approx(0.01 / 0.99)
    # ERA5 geopotential of 5.5e4 m2 s-2 is 5609.4 m
    assert derive.geopotential_height(5.5e4) == pytest.approx(5.5e4 / 9.80665)


def test_temperature_conversions_round_trip():
    assert derive.kelvin_to_celsius(273.15) == 0.0
    assert derive.celsius_to_kelvin(-40.0) == pytest.approx(233.15)


def test_lambert_cone_constant():
    # tangent cone at 38.5 degrees: n = sin(38.5 deg), the value in HRRR's projection
    assert derive.lambert_cone_constant(38.5, 38.5) == pytest.approx(np.sin(np.radians(38.5)))
    # two parallels: Snyder's example (33 and 45 degrees) gives n = 0.6304
    assert derive.lambert_cone_constant(33.0, 45.0) == pytest.approx(0.6304, abs=1e-4)


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
