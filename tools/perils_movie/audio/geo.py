"""Small geometry helpers: local east/north/up from lat/lon/alt, bearings, azimuth relative to a heading."""

from __future__ import annotations

import numpy as np

EARTH_RADIUS_M = 6_371_008.8


def enu(lat0, lon0, alt0, lat, lon, alt):
    """East, north, up (m) of (lat, lon, alt) in the tangent frame at (lat0, lon0, alt0).

    Equirectangular about the listener: the error is negligible below ~100 km (the farthest
    thunder that is still audible). Arguments broadcast.
    """
    lat0 = np.asarray(lat0, float)
    lat = np.asarray(lat, float)
    dlat = np.radians(lat - lat0)
    dlon = np.radians(np.asarray(lon, float) - np.asarray(lon0, float))
    dlon = (dlon + np.pi) % (2 * np.pi) - np.pi
    n = EARTH_RADIUS_M * dlat
    e = EARTH_RADIUS_M * np.cos(np.radians((lat0 + lat) / 2)) * dlon
    u = np.asarray(alt, float) - np.asarray(alt0, float)
    return e, n, u


def slant_distance(lat0, lon0, alt0, lat, lon, alt):
    e, n, u = enu(lat0, lon0, alt0, lat, lon, alt)
    return np.sqrt(e * e + n * n + u * u)


def bearing_deg(e, n):
    """Compass bearing (deg, 0 = north, clockwise) of the vector (east, north)."""
    return np.degrees(np.arctan2(e, n)) % 360.0


def wrap180(a):
    """Wrap angles to (-180, 180]."""
    a = (np.asarray(a, float) + 180.0) % 360.0 - 180.0
    return np.where(a == -180.0, 180.0, a)


def rel_azimuth_deg(bearing, heading):
    """Azimuth of a source relative to the heading: positive = to the right, negative = left."""
    return wrap180(np.asarray(bearing, float) - np.asarray(heading, float))
