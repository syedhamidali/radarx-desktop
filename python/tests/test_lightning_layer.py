"""The observed lightning layer: flash clustering, GLM parallax, readers, flash-rate series, the message.

Known answers, not "looks right": the clustering is tested on synthetic flashes whose grouping is known
by construction (and, when radarx is installed, against radarx's own implementation of the same metric);
the parallax against a spherical-Earth formula; the series on hand-counted events. The GLM and LMA files
are tiny synthetic files written here (no network, no real-data fixtures in the repository).
"""

# ruff: noqa: E501
from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest
import xarray as xr

from engine import lightning_layer as ll
from engine.lightning_obs import offsets_to_seconds

# ── clustering (the algorithm is radarx's; these are the desktop's known answers through the wrapper) ──

LAT0, LON0 = 36.5, -89.0
M_PER_DEG_LON = 111_320.0 * np.cos(np.radians(LAT0))  # metres per degree of longitude at LAT0


def lma_dataset(t, x, alt=8000.0):
    """Sources along a line east of the network centre: times in s, positions in metres east, height in m."""
    t = np.asarray(t, dtype=float)
    x = np.asarray(x, dtype=float)
    base = np.datetime64("2021-12-11T02:00:00", "ns")
    return xr.Dataset(
        {
            "event_latitude": ("number_of_events", np.full(t.size, LAT0)),
            "event_longitude": ("number_of_events", LON0 + x / M_PER_DEG_LON),
            "event_altitude": ("number_of_events", np.full(t.size, alt)),
            "event_power": ("number_of_events", np.full(t.size, 10.0)),
            "event_stations": ("number_of_events", np.full(t.size, 7)),
        },
        coords={"event_time": ("number_of_events", base + (t * 1e9).astype("timedelta64[ns]"))},
    )


def flashes_of(t, x, **link):
    pytest.importorskip("radarx")
    s = ll.lma_set_from_dataset(lma_dataset(t, x), ["x.dat"], link=link or None)
    order = np.argsort(
        np.asarray(t, dtype=float), kind="stable"
    )  # the set holds the sources in time order
    groups: dict[int, set[int]] = {}
    for i, f in enumerate(s.p_flash):
        groups.setdefault(int(f), set()).add(int(order[i]))
    return s, {frozenset(v) for v in groups.values()}


def test_three_known_flashes():
    # A: 10 sources within 1 km in 0.09 s; B: the same place 1 s later; C: 50 km away at the time of A
    ta = np.linspace(0.0, 0.09, 10)
    xa = np.arange(10) * 100.0
    t = np.concatenate([ta, ta + 1.0, ta + 0.05])
    x = np.concatenate([xa, xa, xa + 50_000.0])
    s, groups = flashes_of(t, x)
    assert groups == {frozenset(range(0, 10)), frozenset(range(10, 20)), frozenset(range(20, 30))}
    assert s.n_flashes == 3 and s.f_n_points.tolist() == [10, 10, 10]


def test_the_linking_ellipsoid_around_its_boundary():
    # (dx / 3 km)^2 + (dt / 0.15 s)^2 <= 1: 2.9 km at the same time links, 3.1 km does not;
    # 0.14 s at the same place links, 0.16 s does not (a few metres of the longitude conversion are no issue)
    assert len(flashes_of([0, 0], [0, 2900.0])[1]) == 1
    assert len(flashes_of([0, 0], [0, 3100.0])[1]) == 2
    assert len(flashes_of([0, 0.14], [0, 0])[1]) == 1
    assert len(flashes_of([0, 0.16], [0, 0])[1]) == 2
    # a chain with 0.1 s and a step of 2 km: 0.44 + 0.44 <= 1 links; 2.5 km: 0.69 + 0.44 > 1 does not
    t = np.arange(6) * 0.1
    assert len(flashes_of(t, np.arange(6) * 2000.0)[1]) == 1
    assert len(flashes_of(t, np.arange(6) * 2500.0)[1]) == 6


def test_the_thresholds_are_parameters_and_are_reported():
    t = np.arange(6) * 0.1
    x = np.arange(6) * 2500.0
    s, groups = flashes_of(
        t, x, distance_m=6000.0
    )  # the Alabama / D.C. value of Fuchs et al.: now it links
    assert len(groups) == 1 and s.meta["link"]["distance_m"] == 6000.0
    assert s.meta["link"]["time_s"] == 0.15 and s.meta["link"]["max_duration_s"] == 3.0


def test_max_duration_cuts_a_long_flash_in_time_order():
    t = np.arange(57) * 0.125  # 0 to 7.0 s, exact in binary floating point
    x = np.zeros_like(t)
    assert flashes_of(t, x, max_duration_s=None)[0].n_flashes == 1
    s, _ = flashes_of(t, x, max_duration_s=3.0)
    # a piece lasts until the first source later than 3 s after its start: 0..3.0 (25), 3.125..6.125 (25), 6.25..7.0 (7)
    assert s.f_n_points.tolist() == [25, 25, 7]


# ── geodesy and parallax ────────────────────────────────────────────────────


def test_ecef_round_trip():
    lat = np.array([-60.0, 0.0, 36.5, 89.0])
    lon = np.array([-170.0, 12.0, -89.0, 5.0])
    h = np.array([0.0, 100.0, 12_000.0, -50.0])
    la, lo, hh = ll.ecef_to_geodetic(ll.geodetic_to_ecef(lat, lon, h))
    assert (
        np.allclose(la, lat, atol=1e-9)
        and np.allclose(lo, lon, atol=1e-9)
        and np.allclose(hh, h, atol=1e-4)
    )


SAT_LON, SAT_H = -75.2, 35786.023


def test_parallax_identity_and_subsatellite_point():
    la, lo = ll.glm_parallax_shift(40.0, -89.0, SAT_LON, SAT_H, 12.0, 12.0)
    assert (float(la), float(lo)) == (40.0, -89.0)
    la, lo = ll.glm_parallax_shift(0.0, SAT_LON, SAT_LON, SAT_H, 12.0, 4.0)
    assert abs(float(la)) < 1e-9 and abs(float(lo) - SAT_LON) < 1e-9


def spherical_shift_km(lat, lon, dh):
    """Ground shift of a point seen from the satellite when its height changes by ``dh`` (km), spherical Earth.

    The satellite sees the point at central angle gamma from the sub-satellite point; the line of sight
    leaves the ground at zenith angle zeta with tan(zeta) = r sin(gamma) / (r cos(gamma) - rr). To first
    order the vertical projection of the point at height dh above the sight-line crossing is displaced by
    dh * tan(zeta).
    """
    rr, r = 6371.0, 6371.0 + SAT_H
    gamma = np.arccos(np.cos(np.radians(lat)) * np.cos(np.radians(lon - SAT_LON)))
    return dh * r * np.sin(gamma) / (r * np.cos(gamma) - rr)


@pytest.mark.parametrize("lat,lon", [(36.5, -89.0), (45.0, -100.0), (30.0, -85.0)])
def test_parallax_size_matches_the_spherical_formula(lat, lon):
    la, lo = ll.glm_parallax_shift(lat, lon, SAT_LON, SAT_H, 12.0, 17.0)
    shift = float(ll.distance_km(lat, lon, la, lo))
    assert shift == pytest.approx(float(spherical_shift_km(lat, lon, 5.0)), rel=0.03)
    # 0.7 to 1.6 km per km of height over the United States
    assert 3.5 < shift < 9.0


def test_parallax_direction_and_reversal():
    lat, lon = 36.5, -89.0
    up_la, up_lo = ll.glm_parallax_shift(lat, lon, SAT_LON, SAT_H, 12.0, 16.0)
    down_la, down_lo = ll.glm_parallax_shift(lat, lon, SAT_LON, SAT_H, 12.0, 8.0)
    # a higher flash is displaced towards the sub-satellite point (south and east of this storm), a lower one away from it
    assert float(up_la) < lat and float(up_lo) > lon
    assert float(down_la) > lat and float(down_lo) < lon
    # and moving there and back returns the start
    back_la, back_lo = ll.glm_parallax_shift(up_la, up_lo, SAT_LON, SAT_H, 16.0, 12.0)
    assert abs(float(back_la) - lat) < 1e-6 and abs(float(back_lo) - lon) < 1e-6


# ── series ──────────────────────────────────────────────────────────────────


def test_rate_series_counts_and_gaps():
    # events at 5, 10, 70, 71, 72, 200 s; bins of 60 s from 0 to 240 s; the data cover 0..180 s and 200..240 s
    t = [5, 10, 70, 71, 72, 200]
    s = ll.rate_series(t, 0, 240, 60, coverage=[(0, 180), (200, 240)])
    assert s["edges"].tolist() == [0, 60, 120, 180, 240]
    assert s["count"][:3].tolist() == [2, 3, 0]
    assert s["rate_per_min"][:3].tolist() == [2, 3, 0]
    # the last bin (180..240) is not covered all the way (180..200 is missing): a gap, not a zero, even with an event in it
    assert np.isnan(s["count"][3])
    # a short last bin uses its own length for the rate: 0..90 s, two events in the last 30 s = 4 per minute
    s2 = ll.rate_series([65, 80], 0, 90, 60)
    assert s2["count"].tolist() == [0, 2] and s2["rate_per_min"][1] == pytest.approx(4.0)
    with pytest.raises(ValueError):
        ll.rate_series([], 10, 5)


def test_thunder_delay_is_distance_over_343():
    assert ll.thunder_delay_s(3.43) == pytest.approx(10.0)
    assert ll.thunder_delay_s(3.43, compress=10.0) == pytest.approx(1.0)


# ── GLM files ───────────────────────────────────────────────────────────────

BASE = datetime(2021, 12, 11, 2, 48, 20, tzinfo=UTC)


def write_glm(path: Path, base: datetime = BASE, shift_lat: float = 0.0) -> None:
    """A tiny GLM L2 LCFA file: 3 flashes (one far from the box, one flagged), 6 groups."""
    b64 = np.datetime64(base.replace(tzinfo=None), "ns")

    def at(*secs):
        return b64 + (np.array(secs) * 1e9).astype("timedelta64[ns]")

    # flash 100: 3 groups near (37, -90); flash 101: 2 groups near (37.3, -89.5), quality 1; flash 102: far away (10, -50)
    ds = xr.Dataset(
        {
            "flash_lat": (
                "number_of_flashes",
                np.array([37.0, 37.3, 10.0], dtype="f4") + shift_lat,
            ),
            "flash_lon": ("number_of_flashes", np.array([-90.0, -89.5, -50.0], dtype="f4")),
            "flash_id": ("number_of_flashes", np.array([100, 101, 102], dtype="u2")),
            "flash_energy": ("number_of_flashes", np.array([3e-13, 1e-13, 2e-13], dtype="f4")),
            "flash_area": ("number_of_flashes", np.array([2e9, 1e9, 5e8], dtype="f4")),
            "flash_quality_flag": ("number_of_flashes", np.array([0, 1, 0], dtype="f4")),
            "flash_time_offset_of_first_event": ("number_of_flashes", at(1.0, 2.0, 3.0)),
            "flash_time_offset_of_last_event": ("number_of_flashes", at(1.4, 2.2, 3.1)),
            "group_lat": (
                "number_of_groups",
                np.array([37.0, 37.05, 37.1, 37.3, 37.31, 10.0], dtype="f4") + shift_lat,
            ),
            "group_lon": (
                "number_of_groups",
                np.array([-90.0, -89.95, -89.9, -89.5, -89.49, -50.0], dtype="f4"),
            ),
            "group_energy": (
                "number_of_groups",
                np.array([1e-13, 1e-14, 2e-13, 5e-14, 5e-14, 2e-13], dtype="f4"),
            ),
            "group_parent_flash_id": (
                "number_of_groups",
                np.array([100, 100, 100, 101, 101, 102], dtype="u2"),
            ),
            "group_time_offset": ("number_of_groups", at(1.0, 1.2, 1.4, 2.0, 2.2, 3.0)),
            "nominal_satellite_subpoint_lon": ((), np.float32(SAT_LON)),
            "nominal_satellite_subpoint_lat": ((), np.float32(0.0)),
            "nominal_satellite_height": ((), np.float32(SAT_H)),
            "group_time_threshold": ((), np.float32(0.0)),
            "flash_time_threshold": ((), np.float32(3.33)),
            "lat_field_of_view_bounds": ("n2", np.array([66.56, -66.56], dtype="f4")),
            "lon_field_of_view_bounds": ("n2", np.array([-141.56, -8.44], dtype="f4")),
            "product_time": ((), b64),
        },
        attrs={
            "time_coverage_start": base.strftime("%Y-%m-%dT%H:%M:%S.0Z"),
            "time_coverage_end": (base + timedelta(seconds=20)).strftime("%Y-%m-%dT%H:%M:%S.0Z"),
            "platform_ID": "G16",
            "instrument_ID": "FM1",
            "orbital_slot": "GOES-East",
        },
    )
    ds.to_netcdf(path)


def glm_name(base: datetime) -> str:
    doy = base.timetuple().tm_yday
    end = base + timedelta(seconds=20)
    return f"OR_GLM-L2-LCFA_G16_s{base:%Y}{doy:03d}{base:%H%M%S}0_e{end:%Y}{doy:03d}{end:%H%M%S}0_c20213450249000.nc"


@pytest.fixture
def glm_file(tmp_path: Path) -> Path:
    p = tmp_path / glm_name(BASE)
    write_glm(p)
    return p


def test_offsets_to_seconds_handles_decoded_and_plain_offsets():
    b = BASE
    b64 = np.datetime64(b.replace(tzinfo=None), "ns")
    dec = b64 + (np.array([1.5, -0.25]) * 1e9).astype("timedelta64[ns]")
    assert offsets_to_seconds(dec, b).tolist() == [1.5, -0.25]
    assert offsets_to_seconds(np.array([1.5, -0.25]), b).tolist() == [1.5, -0.25]
    assert offsets_to_seconds((np.array([1.5]) * 1e9).astype("timedelta64[ns]"), b).tolist() == [
        1.5
    ]


def test_read_glm_flashes_groups_and_times(glm_file: Path):
    box = (30.0, -95.0, 40.0, -85.0)
    ls = ll.read_glm([glm_file], box=box)
    # flash 102 is outside the box, flash 101 has a quality flag of 1 and is left out by default
    assert ls.n_flashes == 1 and ls.n_points == 3
    assert ls.kind == "glm" and ls.instrument == "GOES-16 GLM (GOES-East)"
    assert any("quality flag" in n for n in ls.notes)
    # the first flash starts 1.0 s after the product time; times are seconds after t_ref
    t_first = ls.t_ref + timedelta(seconds=float(ls.f_t0[0]))
    assert t_first == BASE + timedelta(seconds=1.0)
    assert (ls.p_t - ls.f_t0[0]).tolist() == pytest.approx(
        [0.0, 0.2, 0.4], abs=1e-5
    )  # groups in time order, 0.2 s apart
    assert (ls.p_flash == 0).all() and ls.f_n_points.tolist() == [3]
    assert np.isnan(ls.p_alt_km).all(), "GLM has no height"
    assert ls.f_strength[0] == pytest.approx(3e-13, rel=1e-5)
    assert ls.f_area_km2[0] == pytest.approx(2000.0, rel=1e-5)
    assert ls.meta["flash_time_threshold"] == pytest.approx(3.33)
    assert ls.coverage == [
        (
            pytest.approx(BASE.timestamp() - ls.t_ref.timestamp()),
            pytest.approx(BASE.timestamp() + 20 - ls.t_ref.timestamp()),
        )
    ]
    # everything kept, with the flagged flash
    all_ = ll.read_glm([glm_file], good_only=False)
    assert all_.n_flashes == 3 and all_.n_points == 6


def test_read_glm_time_window_and_files_outside_it(tmp_path: Path):
    a = tmp_path / glm_name(BASE)
    b = tmp_path / glm_name(BASE + timedelta(seconds=20))
    write_glm(a)
    write_glm(b, BASE + timedelta(seconds=20))
    both = ll.read_glm([a, b], good_only=False)
    assert both.n_flashes == 6
    # a window that ends before the second granule: that file is not even needed
    first_only = ll.read_glm(
        [a, b], t0=BASE.isoformat(), t1=(BASE + timedelta(seconds=5)).isoformat(), good_only=False
    )
    assert first_only.n_flashes == 3 and first_only.files == [a.name]
    # a window with no file raises a clear error
    with pytest.raises(ValueError, match="overlaps"):
        ll.read_glm([a], t0=(BASE + timedelta(minutes=5)).isoformat())
    # two granules are one continuous coverage, a gap between granules stays a gap
    c = tmp_path / glm_name(BASE + timedelta(seconds=60))
    write_glm(c, BASE + timedelta(seconds=60))
    gap = ll.read_glm([a, c], good_only=False)
    assert len(gap.coverage) == 2


def test_read_glm_parallax_is_reported_and_only_applied_on_request(glm_file: Path):
    plain = ll.read_glm([glm_file], good_only=False)
    par = plain.meta["parallax"]
    assert (
        par["applied_to_km"] is None
        and par["assumed_product_height_km"] == ll.GLM_PRODUCT_HEIGHT_KM
    )
    assert "assumption" in par["assumption"].lower() or "assumption" in par["assumption"]
    assert par["uncertainty_shift_km"] == pytest.approx(
        5.0, rel=0.4
    )  # about 1 km per km over the central US
    moved = ll.read_glm([glm_file], good_only=False, parallax_to_km=8.0)
    assert moved.meta["parallax"]["applied_to_km"] == 8.0
    assert (
        moved.p_lat > plain.p_lat
    ).all()  # a lower cloud top than assumed: away from the satellite (north)
    shift = ll.distance_km(plain.p_lat, plain.p_lon, moved.p_lat, moved.p_lon)
    assert (shift > 0.5).all() and (shift < 8).all()


def test_read_lightning_errors(tmp_path: Path, glm_file: Path):
    with pytest.raises(FileNotFoundError, match="nothing is downloaded"):
        ll.read_lightning([tmp_path / "missing.nc"])
    dat = tmp_path / "x.dat"
    dat.write_text("")
    with pytest.raises(ValueError, match="not both"):
        ll.read_lightning([glm_file, dat])
    with pytest.raises(ValueError, match="no lightning files"):
        ll.read_lightning([])
    notglm = tmp_path / "other.nc"
    xr.Dataset({"a": ("x", [1.0])}).to_netcdf(notglm)
    with pytest.raises(ValueError, match="not a GLM"):
        ll.read_glm([notglm])


def test_wire_form_round_trips(glm_file: Path):
    ls = ll.read_glm([glm_file], good_only=False)
    w = ll.to_wire(ls)
    assert w["kind"] == "glm" and w["points"]["n"] == 6 and w["flashes"]["n"] == 3
    t = np.frombuffer(base64.b64decode(w["points"]["t"]), dtype="<f4")
    assert t.tolist() == pytest.approx(ls.p_t.tolist(), abs=1e-5)
    flash = np.frombuffer(base64.b64decode(w["points"]["flash"]), dtype="<i4")
    assert flash.tolist() == ls.p_flash.tolist()
    br = np.frombuffer(base64.b64decode(w["points"]["brightness"]), dtype="<f4")
    assert br.min() >= 0 and br.max() <= 1
    assert w["provenance"]["kind"] == "observed" and "GOES-16" in w["provenance"]["source"]
    assert w["strength"]["unit"] == "J" and "log10" in w["strength"]["brightness_scale"]
    json.dumps(w)  # JSON-safe


# ── LMA files ───────────────────────────────────────────────────────────────

LMA_HEADER = """Lightning Mapping Array synthetic test file (not real data)
Analysis program: synthetic
Analysis program version: 0
Data start time: 12/11/21 02:00:00
Number of seconds analyzed: 600
Location: Synthetic Test Array
Coordinate center (lat,lon,alt):  36.5000 -89.0000 100.0
Number of active stations: 6
Station information: ID, Name, Lat, Long, Altitude, delay
Sta_info: A Alpha 36.40 -89.10 100.0 0.0 0 0
Sta_info: B Bravo 36.60 -89.10 100.0 0.0 0 0
Sta_info: C Charlie 36.40 -88.90 100.0 0.0 0 0
Sta_info: D Delta 36.60 -88.90 100.0 0.0 0 0
Sta_info: E Echo 36.50 -89.20 100.0 0.0 0 0
Sta_info: F Foxtrot 36.50 -88.80 100.0 0.0 0 0
Data: time (UT sec of day), lat, lon, alt(m), reduced chi^2, P(dBW), mask
Data format: 15.9f 10.6f 11.6f 8.1f 6.2f 5.1f 4x
*** data ***
"""


def lma_rows():
    """Two flashes (A: 6 sources over 0.15 s rising 7 to 9 km; B: 4 sources 40 km away) and two sources the filter drops."""
    rows = []
    for k in range(6):  # flash A starts at 7230.0 s of the day = 02:00:30
        rows.append(
            (
                7230.0 + 0.03 * k,
                36.50 + 0.002 * k,
                -89.00 + 0.001 * k,
                7000.0 + 400.0 * k,
                0.4,
                12.0 - k,
                "3f",
            )
        )
    for k in range(4):  # flash B at 7231.0 s, 40 km to the east
        rows.append((7231.0 + 0.02 * k, 36.50, -88.55 + 0.001 * k, 9000.0, 0.6, 8.0, "7f"))
    rows.append((7230.05, 36.5, -89.0, 8000.0, 3.0, 20.0, "3f"))  # chi-square above 1: dropped
    rows.append((7230.06, 36.5, -89.0, 8000.0, 0.5, 20.0, "7"))  # 3 stations: dropped
    rows.sort()
    return rows


@pytest.fixture
def lma_file(tmp_path: Path) -> Path:
    p = tmp_path / "LYLOUT_211211_020000_0600.dat"
    body = "".join(
        f"{t:15.9f} {la:10.6f} {lo:11.6f} {al:8.1f} {c:6.2f} {pw:5.1f} {m}\n"
        for t, la, lo, al, c, pw, m in lma_rows()
    )
    p.write_text(LMA_HEADER + body)
    return p


def test_read_lma_filters_and_clusters_with_known_answer(lma_file: Path):
    pytest.importorskip("radarx")
    ls = ll.read_lma([lma_file])
    # the two bad sources are filtered (chi-square above 1, fewer than 6 stations); the rest are two flashes
    assert ls.n_points == 10 and ls.n_flashes == 2
    assert ls.f_n_points.tolist() == [6, 4]
    # initiation point and height of flash A: its first source at 7000 m = 7 km
    assert ls.f_alt_km[0] == pytest.approx(7.0) and ls.f_lat[0] == pytest.approx(36.5)
    assert ls.p_alt_km[:6].tolist() == pytest.approx([7.0, 7.4, 7.8, 8.2, 8.6, 9.0])
    # times: flash A starts 02:00:30 UTC
    assert ls.t_ref + timedelta(seconds=float(ls.f_t0[0])) == datetime(
        2021, 12, 11, 2, 0, 30, tzinfo=UTC
    )
    assert ls.f_t1[0] - ls.f_t0[0] == pytest.approx(0.15, abs=1e-5)
    assert ls.p_stations.tolist() == [6] * 6 + [7] * 4
    assert ls.meta["link"]["distance_m"] == 3000.0 and ls.meta["qc"] == {
        "max_chi2": 1.0,
        "min_stations": 6,
    }
    assert len(ls.meta["stations"]) == 6 and ls.meta["network_center"]["lat"] == pytest.approx(36.5)
    # the parameters are exposed: a 100 m linking distance splits flash A
    tight = ll.read_lma([lma_file], link={"distance_m": 100.0})
    assert tight.n_flashes > 2
    # without the quality filter the two extra sources are there (and join flash A)
    raw = ll.read_lma([lma_file], max_chi2=None, min_stations=None)
    assert raw.n_points == 12
    # a box that holds only flash B
    only_b = ll.read_lma([lma_file], box=(36.4, -88.7, 36.6, -88.4))
    assert only_b.n_flashes == 1 and only_b.n_points == 4
    assert ll.to_wire(ls)["strength"]["unit"] == "dBW"


def test_empty_window_is_an_empty_set_not_an_invention(lma_file: Path):
    pytest.importorskip("radarx")
    ls = ll.read_lma([lma_file], t0="2021-12-11T05:00:00Z", t1="2021-12-11T05:10:00Z")
    assert ls.n_points == 0 and ls.n_flashes == 0
    assert ls.notes and "No LMA sources" in ls.notes[0]
    assert ls.coverage, "the time the file covers is still reported"
    assert ll.to_wire(ls)["points"]["n"] == 0


# ── LMA statistics (radarx: vertical_source_distribution, lightning_jump) ──


def stats_set(t, x, alt):
    pytest.importorskip("radarx")
    base = np.datetime64("2021-12-11T02:00:00", "ns")
    t = np.asarray(t, dtype=float)
    ds = xr.Dataset(
        {
            "event_latitude": ("number_of_events", np.full(t.size, LAT0)),
            "event_longitude": (
                "number_of_events",
                LON0 + np.asarray(x, dtype=float) / M_PER_DEG_LON,
            ),
            "event_altitude": ("number_of_events", np.asarray(alt, dtype=float)),
            "event_power": ("number_of_events", np.full(t.size, 10.0)),
            "event_stations": ("number_of_events", np.full(t.size, 7)),
        },
        coords={"event_time": ("number_of_events", base + (t * 1e9).astype("timedelta64[ns]"))},
    )
    return ll.lma_set_from_dataset(ds, ["x.dat"])


def test_height_distribution_known_counts():
    from engine import lightning_stats

    # flash A: 12 sources rising from 7.11 to 8.98 km over 0.33 s; flash B: 10 sources at 9.2 km, 40 km away, 1 s later
    # (flash initiations are counted for flashes of at least 10 sources, the radarx default)
    alt_a = 7110.0 + 170.0 * np.arange(12)
    t = np.concatenate([np.arange(12) * 0.03, 1.0 + np.arange(10) * 0.02])
    x = np.concatenate([np.arange(12) * 100.0, 40_000.0 + np.arange(10) * 100.0])
    alt = np.concatenate([alt_a, np.full(10, 9200.0)])
    st = lightning_stats.lma_stats(stats_set(t, x, alt), z_step_m=500.0)
    h = st["height"]
    # 500 m bins starting at 7 km: three of A's sources in each of the first four bins, B's ten in the fifth
    assert h["sources"][:5] == [3, 3, 3, 3, 10] and sum(h["sources"][5:]) == 0
    # flash initiations: A starts at 7.11 km (first bin), B at 9.2 km (fifth bin)
    assert h["initiations"][:5] == [1, 0, 0, 0, 1] and sum(h["initiations"]) == 2
    assert h["z_km"][0] == pytest.approx(7.25) and h["reference"].endswith("mean sea level")
    assert any("Charge layers are not inferred" in n for n in st["notes"])
    assert st["jump"]["available"] is False and "14 minutes" in st["jump"]["reason"]


def flash_times(per_minute):
    """One-source flashes: ``per_minute[m]`` of them, evenly spread in minute m."""
    out = []
    for m, n in enumerate(per_minute):
        out += list(m * 60.0 + np.arange(n) * (60.0 / n))
    return np.asarray(out)


def test_lightning_jump_found_where_the_flash_rate_jumps():
    from engine import lightning_stats

    # 12 and 14 flashes per minute in alternating 2-minute periods for 16 minutes (changes of the 2-minute
    # rate of +-1 per minute squared, so a standard deviation of about 1.1), then 45 per minute: a change of
    # (45 - 14) / 2 = 15.5, about 14 sigma, at a rate above 10 per minute
    jumping = [12 if (m // 2) % 2 == 0 else 14 for m in range(16)] + [45] * 6
    t = flash_times(jumping)
    j = lightning_stats.lma_stats(stats_set(t, np.zeros_like(t), np.full(t.size, 8000.0)))["jump"]
    assert j["available"] is True and j["sigma"] == 2.0 and j["min_rate_per_min"] == 10.0
    assert j["start_s"], "a jump must be found"
    assert 16 <= j["start_s"][0] / 60.0 <= 18.5, (
        f"the jump starts at minute {j['start_s'][0] / 60.0}"
    )
    assert j["level"][0] > 2
    # the same alternation without the jump has none
    steady = flash_times([12 if (m // 2) % 2 == 0 else 14 for m in range(22)])
    j2 = lightning_stats.lma_stats(
        stats_set(steady, np.zeros_like(steady), np.full(steady.size, 8000.0))
    )["jump"]
    assert j2["available"] is True and j2["start_s"] == []


def test_statistics_need_lma_sources(glm_file: Path):
    from engine import lightning_stats

    with pytest.raises(ValueError, match="LMA"):
        lightning_stats.lma_stats(ll.read_glm([glm_file], good_only=False))


def test_lightning_stats_message(lma_file: Path):
    pytest.importorskip("radarx")

    async def run():
        h = make_handler()
        await h._handle_lightning_stats({"request_id": "s0"})  # nothing open yet
        assert h._sent[-1]["type"] == "lightning_stats_error"
        (ok,) = await ask(h, type="lightning_open", request_id="o1", paths=[str(lma_file)])
        assert ok["type"] == "lightning_result" and ok["kind"] == "lma"
        (st,) = await ask(h, type="lightning_stats", request_id="s1", z_step_m=1000)
        assert st["type"] == "lightning_stats_result" and st["request_id"] == "s1"
        assert sum(st["height"]["sources"]) == 10 and st["height"]["z_step_m"] == 1000
        (bad,) = await ask(h, type="lightning_stats", request_id="s2", z_step_m=5)
        assert bad["type"] == "lightning_stats_error"

    asyncio.run(run())


# ── the message ─────────────────────────────────────────────────────────────


def make_handler():
    from server import ConnectionHandler

    ws = AsyncMock()
    ws.remote_address = ("127.0.0.1", 1)
    sent: list[dict] = []

    async def fake_send(raw) -> None:
        if isinstance(raw, str):
            sent.append(json.loads(raw))

    ws.send = fake_send
    h = ConnectionHandler(ws)
    h._sent = sent  # type: ignore[attr-defined]
    return h


async def ask(h, **msg) -> list[dict]:
    start = len(h._sent)
    await h.handle_message(json.dumps(msg))
    return h._sent[start:]


def test_lightning_open_message(glm_file: Path):
    async def run():
        h = make_handler()
        (ok,) = await ask(
            h,
            type="lightning_open",
            request_id="r1",
            paths=[str(glm_file)],
            box=[30, -95, 40, -85],
            parallax_to_km=10,
        )
        assert ok["type"] == "lightning_result" and ok["request_id"] == "r1"
        assert ok["points"]["n"] == 3 and ok["flashes"]["n"] == 1
        assert ok["meta"]["parallax"]["applied_to_km"] == 10.0
        for bad in (
            {"paths": []},
            {"paths": ["relative.nc"]},
            {"paths": [str(glm_file)], "box": [1, 2, 3]},
            {"paths": [str(glm_file)], "link": {"bogus": 1}},
            {"paths": [str(glm_file)], "parallax_to_km": 99},
            {"paths": [str(glm_file.parent / "nope.nc")]},
        ):
            (err,) = await ask(h, type="lightning_open", request_id="r2", **bad)
            assert err["type"] == "lightning_error" and err["request_id"] == "r2" and err["message"]

    asyncio.run(run())


def test_lightning_open_is_refused_in_web_mode(glm_file: Path):
    async def run():
        h = make_handler()
        h.session = SimpleNamespace(
            dir=Path("/nonexistent/session"), real_dir="/nonexistent/session"
        )  # a web session
        await h._handle_lightning_open({"request_id": "w", "paths": [str(glm_file)]})
        err = h._sent[-1]
        assert err["type"] == "lightning_error" and "desktop" in err["message"]

    asyncio.run(run())


def test_rxd_lightning_text_csv_and_json(glm_file: Path, tmp_path: Path, capsys):
    from engine import cli

    box = ["--box", "30", "-95", "40", "-85"]
    code = cli.main(
        [
            "lightning",
            "-i",
            str(glm_file),
            *box,
            "-o",
            str(tmp_path / "flashes.csv"),
            "-o",
            str(tmp_path / "rates.csv"),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0 and "1 flashes, 3 groups" in out and "OBSERVED" in out and "parallax:" in out
    lines = [
        ln for ln in (tmp_path / "flashes.csv").read_text().splitlines() if not ln.startswith("#")
    ]
    assert lines[0].startswith("start_utc,latitude") and len(lines) == 2
    assert lines[1].startswith("2021-12-11T02:48:21")
    rates = [
        ln for ln in (tmp_path / "rates.csv").read_text().splitlines() if not ln.startswith("#")
    ]
    assert rates[0] == "bin_start_utc,flashes,flashes_per_minute"
    # --json carries the provenance and no column dump
    code = cli.main(["lightning", "-i", str(glm_file), *box, "--json", "--parallax-km", "9"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0 and doc["n_flashes"] == 1 and doc["meta"]["parallax"]["applied_to_km"] == 9.0
    assert "points" not in doc and doc["provenance"]["kind"] == "observed"
    # a file that does not exist is an input error with the "nothing is downloaded" words
    code = cli.main(["lightning", "-i", str(tmp_path / "missing.nc")])
    assert code != 0


def test_a_flood_of_groups_is_cut_by_whole_flashes_and_said(glm_file: Path, monkeypatch):
    monkeypatch.setattr(ll, "MAX_POINTS", 4)
    ls = ll.read_glm([glm_file], good_only=False)
    # the three flashes have 3, 2 and 1 groups: only the first fits under a cap of 4, whole
    assert ls.n_flashes == 1 and ls.n_points == 3 and ls.meta["truncated"] is True
    assert any("first 1 flashes" in n for n in ls.notes)
