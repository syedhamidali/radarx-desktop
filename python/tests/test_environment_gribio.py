"""The GRIB2 reader (ecCodes, optional): values, grids, times, cropping, errors.

The messages are synthetic (``tests.env_synth``); a few hundred bytes each.
"""

from __future__ import annotations

import gzip

import numpy as np
import pytest

from engine.environment import gribio
from engine.environment.errors import InvalidRequest
from tests import env_synth as es

pytestmark = pytest.mark.skipif(not es.have_eccodes(), reason="ecCodes (extra 'grib') missing")
RUN = es.run_time(3)


def field(n_rows=16, n_cols=24):
    return np.arange(n_rows * n_cols, dtype=float).reshape(n_rows, n_cols)


def test_values_times_and_levels_survive_a_round_trip():
    v = field()
    f = gribio.read_first(es.message("CAPE", v, run=RUN, fxx=2))
    assert f.values.dtype == np.float32 and f.values.shape == v.shape
    assert np.abs(f.values - v).max() < 1e-3
    assert f.reference_time == RUN and f.step_hours == 2.0
    assert f.valid_time.strftime("%Y-%m-%dT%H:%M") == "2021-12-11T05:00"
    g = gribio.read_first(es.message("TMP", v, kind="isobaric", level=850, run=RUN))
    assert (g.level_type, g.level) == ("isobaricInhPa", 850.0)
    h = gribio.read_first(es.message("UGRD", v, kind="height", level=10, run=RUN))
    assert (h.level_type, h.level) == ("heightAboveGround", 10.0)


def test_lambert_grid_positions_agree_with_an_independent_projection():
    pyproj = pytest.importorskip("pyproj")
    f = gribio.read_first(es.message("CAPE", field(), run=RUN))
    assert f.grid.kind == "lambert" and f.grid.shape == (16, 24)
    crs = "+proj=lcc +lat_1=38.5 +lat_2=38.5 +lat_0=38.5 +lon_0=-97.5 +R=6371229 +units=m"
    t = pyproj.Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    x0, y0 = pyproj.Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(
        es.LAMBERT["lon0"] - 360.0, es.LAMBERT["lat0"]
    )
    jj, ii = np.mgrid[0:16, 0:24]
    lon, lat = t.transform(x0 + ii * es.LAMBERT["dx"], y0 + jj * es.LAMBERT["dx"])
    assert f.grid.lat == pytest.approx(lat, abs=2e-3)  # within about 200 m
    assert f.grid.lon == pytest.approx(lon, abs=2e-3)
    assert float(f.grid.lon.max()) < 180 and float(f.grid.lon.min()) > -180


def test_regular_grids_are_read_south_to_north_with_longitudes_in_180():
    v = field(8, 10)
    f = gribio.read_first(es.message("TMP", v, run=RUN, grid="regular_ll", lat_first=40.0))
    assert f.grid.lat.ndim == 1 and f.grid.lat[0] < f.grid.lat[-1]
    assert f.grid.lat[0] == pytest.approx(40.0 - 0.07) and f.grid.lat[-1] == pytest.approx(40.0)
    assert f.grid.lon[0] == pytest.approx(-90.0)  # written as 270 east
    # the file runs north to south; the rows are flipped together with the latitudes
    assert np.allclose(f.values[0], v[-1]) and np.allclose(f.values[-1], v[0])


def test_gz_files_and_several_messages_in_one_file(tmp_path):
    m1 = es.message("CAPE", field(), run=RUN)
    m2 = es.message("CIN", field() * 2, run=RUN)
    path = tmp_path / "two.grib2.gz"
    path.write_bytes(gzip.compress(m1 + m2))
    fields = list(gribio.iter_fields(path))
    assert len(fields) == 2 and fields[1].values[1, 1] == pytest.approx(2 * field()[1, 1])


def test_window_is_whole_cells_and_complains_when_nothing_is_inside():
    f = gribio.read_first(es.message("CAPE", field(), run=RUN))
    lat, lon = f.grid.lat, f.grid.lon
    box = (float(lon[4, 6]), float(lat[4, 6]), float(lon[9, 14]), float(lat[9, 14]))
    rows, cols = gribio.window(f.grid, box)
    assert rows.start <= 4 and rows.stop >= 10 and cols.start <= 6 and cols.stop >= 15
    cropped = gribio.crop_grid(f.grid, rows, cols)
    assert cropped.shape == (rows.stop - rows.start, cols.stop - cols.start)
    assert gribio.window(f.grid, None) == (slice(0, 16), slice(0, 24))
    with pytest.raises(InvalidRequest, match="no grid cell centre"):
        gribio.window(f.grid, (10.0, 10.0, 11.0, 11.0))


def test_nearest_cell_on_both_kinds_of_grid():
    f = gribio.read_first(es.message("CAPE", field(), run=RUN))
    j, i, d = gribio.nearest_cell(f.grid, float(f.grid.lat[5, 7]), float(f.grid.lon[5, 7]))
    assert (j, i) == (5, 7) and d < 0.01
    r = gribio.read_first(es.message("TMP", field(8, 10), run=RUN, grid="regular_ll"))
    j, i, d = gribio.nearest_cell(r.grid, float(r.grid.lat[3]), 270.0 + 0.04 - 360.0)
    assert (j, i) == (3, 4) and d < 0.01


def test_a_damaged_file_is_an_error_not_garbage():
    m = es.message("CAPE", field(), run=RUN)
    with pytest.raises(InvalidRequest, match="truncated"):
        list(gribio.iter_fields(m[: len(m) // 2]))
    with pytest.raises(InvalidRequest, match="No GRIB2 message"):
        gribio.read_first(b"nothing here")


def test_units_are_written_the_cf_way_and_reflectivity_is_dbz():
    assert gribio.clean_units("m s**-1") == "m s-1"
    assert gribio.clean_units("m**2 s**-2") == "m2 s-2"
    assert gribio.clean_units("dB", "REFC") == "dBZ"
    assert gribio.clean_units("dB", "SOMETHING") == "dB"
    assert gribio.clean_units("") == ""
