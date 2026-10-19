"""Cross-section engine behaviour: sector scans, RHI-style lines, section metadata, image saving."""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock

import numpy as np
import pytest
import xarray as xr

import server
from engine.processor import (
    MAX_SECTION_POINTS,
    _geod,
    _ground_range_azimuth,
    _sample_sweep,
    extract_cross_section,
    rhi_line,
)
from server import ConnectionHandler

RADAR_LAT, RADAR_LON = 40.0, -100.0
RANGE = 250.0 * (1 + np.arange(600))  # to 150 km


def _point_at(ground_m: float, azimuth_deg: float) -> tuple[float, float]:
    lon, lat, _ = _geod().fwd(RADAR_LON, RADAR_LAT, azimuth_deg, ground_m, return_back_azimuth=True)
    return float(lat), float(lon)


def _tree(az: np.ndarray, elevs=(0.5,), root_attrs=None, times=None) -> xr.DataTree:
    parts = {
        "/": xr.Dataset(attrs={"latitude": RADAR_LAT, "longitude": RADAR_LON, **(root_attrs or {})})
    }
    for i, e in enumerate(elevs):
        vals = np.full((az.size, RANGE.size), 30.0, dtype=np.float32)
        ds = xr.Dataset(
            {"DBZH": (("azimuth", "range"), vals, {"units": "dBZ"})},
            coords={"azimuth": az, "range": RANGE},
            attrs={"sweep_fixed_angle": e},
        )
        if times is not None:
            ds = ds.assign_coords(time=("azimuth", times))
        parts[f"/sweep_{i}"] = ds
    return xr.DataTree.from_dict(parts)


class TestSectorScans:
    """A section must not be stretched across the part of the circle a sector scan skipped."""

    def test_bearings_outside_the_sector_are_empty(self) -> None:
        az = np.arange(50.0, 180.5, 1.0)
        vals = np.full((az.size, RANGE.size), 30.0, dtype=np.float32)
        probe = np.array([10.0, 49.0, 49.8, 50.0, 100.0, 180.0, 180.4, 181.0, 200.0, 300.0, 359.5])
        got = _sample_sweep(vals, az, RANGE, probe, np.full(probe.shape, 50_000.0))
        inside = (probe >= 49.5) & (probe <= 180.5)
        assert np.all(np.isfinite(got[inside]))
        assert np.all(np.isnan(got[~inside]))

    def test_full_circle_has_no_gaps_including_the_seam(self) -> None:
        az = np.arange(0.0, 360.0, 0.5)
        vals = np.full((az.size, RANGE.size), 7.0, dtype=np.float32)
        probe = np.linspace(0.0, 359.99, 200)
        got = _sample_sweep(vals, az, RANGE, probe, np.full(probe.shape, 20_000.0))
        assert np.all(got == 7.0)

    def test_small_hole_of_lost_rays_is_filled_but_a_wide_one_is_not(self) -> None:
        az = np.arange(0.0, 360.0, 1.0)
        vals = np.full((az.size, RANGE.size), 1.0, dtype=np.float32)
        small = (az < 100) | (az > 103)  # 3 rays lost
        big = (az < 200) | (az > 230)  # 30 deg lost
        p = np.array([101.5, 215.0])
        r = np.full(2, 30_000.0)
        got_small = _sample_sweep(vals[small], az[small], RANGE, p, r)
        got_big = _sample_sweep(vals[big], az[big], RANGE, p, r)
        assert np.isfinite(got_small[0])
        assert np.isnan(got_big[1])

    def test_cross_section_through_a_sector_leaves_the_rest_empty(self) -> None:
        tree = _tree(np.arange(50.0, 180.5, 1.0))
        # west -> east across the radar: the east half (az 90) is scanned, the west (270) is not
        out = extract_cross_section(
            tree, _point_at(80_000.0, 270.0), _point_at(80_000.0, 90.0), "DBZH", n_points=81
        )
        row = np.array([np.nan if v is None else v for v in out["values"][0]])
        d = np.array(out["distance_km"])
        assert np.all(np.isnan(row[d < 78]))  # the unscanned west side
        assert np.all(np.isfinite(row[d > 84]))  # the scanned east side

    def test_single_ray_sweep_covers_only_its_own_bearing(self) -> None:
        vals = np.full((1, RANGE.size), 5.0, dtype=np.float32)
        got = _sample_sweep(
            vals, np.array([90.0]), RANGE, np.array([90.0, 91.0, 180.0]), np.full(3, 30_000.0)
        )
        assert np.isfinite(got[0]) and np.isnan(got[1]) and np.isnan(got[2])


class TestSectionMetadata:
    def test_rhi_line_runs_out_along_the_azimuth(self) -> None:
        tree = _tree(np.arange(0.0, 360.0, 10.0))
        a, b = rhi_line(tree, 135.0, 100.0)
        assert a == (RADAR_LAT, RADAR_LON)
        ground, az = _ground_range_azimuth(np.array([b[0]]), np.array([b[1]]), RADAR_LAT, RADAR_LON)
        assert ground[0] == pytest.approx(100_000.0, rel=1e-6)
        assert az[0] == pytest.approx(135.0, abs=1e-6)

    def test_rhi_line_can_start_away_from_the_radar(self) -> None:
        tree = _tree(np.arange(0.0, 360.0, 10.0))
        a, b = rhi_line(tree, 0.0, 120.0, min_range_km=20.0)
        g0, _ = _ground_range_azimuth(np.array([a[0]]), np.array([a[1]]), RADAR_LAT, RADAR_LON)
        g1, _ = _ground_range_azimuth(np.array([b[0]]), np.array([b[1]]), RADAR_LAT, RADAR_LON)
        assert g0[0] == pytest.approx(20_000.0, rel=1e-6)
        assert g1[0] == pytest.approx(120_000.0, rel=1e-6)

    def test_result_describes_the_line_and_the_radar(self) -> None:
        tree = _tree(np.arange(0.0, 360.0, 1.0), elevs=(0.5, 1.5, 2.5))
        a, b = rhi_line(tree, 90.0, 90.0)
        out = extract_cross_section(tree, a, b, "DBZH", n_points=10)
        assert out["elevations_deg"] == pytest.approx([0.5, 1.5, 2.5])
        assert out["start"] == pytest.approx(list(a))
        assert out["end"] == pytest.approx(list(b))
        assert (out["radar_lat"], out["radar_lon"]) == (RADAR_LAT, RADAR_LON)
        assert out["radar_altitude_m"] is None  # this file has none
        assert len(out["ground_range_km"]) == len(out["azimuth_deg"]) == 10
        assert out["ground_range_km"][0] == pytest.approx(0.0, abs=1e-6)
        assert out["ground_range_km"][-1] == pytest.approx(90.0, rel=1e-6)
        assert out["azimuth_deg"][-1] == pytest.approx(90.0, abs=1e-6)
        # a full RHI-style line out from the radar is filled all the way along
        assert all(v is not None for v in out["values"][0][1:])

    def test_altitude_and_time_are_reported_when_the_file_has_them(self) -> None:
        az = np.arange(0.0, 360.0, 10.0)
        times = np.datetime64("2013-05-20T20:16:43") + np.arange(az.size).astype("timedelta64[s]")
        tree = _tree(az, root_attrs={"altitude": 370.0}, times=times)
        out = extract_cross_section(tree, *rhi_line(tree, 0.0, 50.0), "DBZH", n_points=5)
        assert out["radar_altitude_m"] == pytest.approx(370.0)
        assert out["time"] == "2013-05-20 20:16:43"

    def test_point_count_is_capped(self) -> None:
        tree = _tree(np.arange(0.0, 360.0, 10.0))
        out = extract_cross_section(tree, *rhi_line(tree, 0.0, 50.0), "DBZH", n_points=10**6)
        assert len(out["distance_km"]) == MAX_SECTION_POINTS


# ---------------------------------------------------------------------------
# WebSocket handlers
# ---------------------------------------------------------------------------


def _handler() -> tuple[ConnectionHandler, list[dict]]:
    ws = AsyncMock()
    ws.remote_address = ("127.0.0.1", 9999)
    sent: list[dict] = []

    async def fake_send(raw) -> None:
        if isinstance(raw, str):
            sent.append(json.loads(raw))

    ws.send = fake_send
    return ConnectionHandler(ws), sent


@pytest.fixture()
def open_tree():
    from engine.reader import RadarReader

    saved = (dict(server._file_readers), server._last_file_id)
    server._file_readers.clear()
    reader = RadarReader()
    reader._dtree = _tree(np.arange(0.0, 360.0, 1.0), elevs=(0.5, 1.5))
    reader._format = "synthetic"
    file_id = server._register_reader(reader)
    yield file_id
    server._file_readers.clear()
    server._file_readers.update(saved[0])
    server._last_file_id = saved[1]


class TestCrossSectionHandler:
    @pytest.mark.asyncio
    async def test_rhi_request_echoes_the_azimuth_and_line(self, open_tree) -> None:
        h, sent = _handler()
        await h.handle_message(
            json.dumps(
                {
                    "type": "get_cross_section",
                    "variable": "DBZH",
                    "azimuth": 45,
                    "max_range_km": 80,
                    "n_points": 120,
                    "file_id": open_tree,
                }
            )
        )
        out = next(m for m in sent if m["type"] == "cross_section_result")
        assert out["rhi_azimuth"] == 45.0
        assert len(out["distance_km"]) == 120
        assert out["distance_km"][-1] == pytest.approx(80.0, rel=1e-3)
        assert out["start"] == [RADAR_LAT, RADAR_LON]

    @pytest.mark.asyncio
    async def test_line_request_still_works_without_n_points(self, open_tree) -> None:
        h, sent = _handler()
        await h.handle_message(
            json.dumps(
                {
                    "type": "get_cross_section",
                    "variable": "DBZH",
                    "start": list(_point_at(10_000.0, 0.0)),
                    "end": list(_point_at(60_000.0, 0.0)),
                    "file_id": open_tree,
                }
            )
        )
        out = next(m for m in sent if m["type"] == "cross_section_result")
        assert len(out["distance_km"]) == 200
        assert "rhi_azimuth" not in out

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload",
        [
            {"azimuth": "north", "max_range_km": 50},
            {"azimuth": 10, "max_range_km": 0},
            {"azimuth": 10, "max_range_km": float("nan")},
            {"start": [1, 2], "end": [3, "x"]},
            {"start": [1, 2], "end": [3, 4], "n_points": 1},
            {"start": [1, 2], "end": [3, 4], "n_points": True},
        ],
    )
    async def test_bad_requests_are_errors(self, payload, open_tree) -> None:
        h, sent = _handler()
        await h.handle_message(
            json.dumps(
                {"type": "get_cross_section", "variable": "DBZH", "file_id": open_tree, **payload}
            )
        )
        assert any(m["type"] == "error" for m in sent), sent
        assert not any(m["type"] == "cross_section_result" for m in sent)


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class TestSaveImageFile:
    @pytest.mark.asyncio
    async def test_writes_the_png(self, tmp_path) -> None:
        h, sent = _handler()
        target = tmp_path / "section.png"
        await h.handle_message(
            json.dumps(
                {
                    "type": "save_image_file",
                    "path": str(target),
                    "data_base64": base64.b64encode(PNG).decode(),
                }
            )
        )
        assert sent[-1] == {"type": "file_saved", "path": str(target)}
        assert target.read_bytes() == PNG

    @pytest.mark.asyncio
    async def test_refuses_to_replace_unless_told_to(self, tmp_path) -> None:
        target = tmp_path / "section.png"
        target.write_bytes(b"old")
        payload = {
            "type": "save_image_file",
            "path": str(target),
            "data_base64": base64.b64encode(PNG).decode(),
        }
        h, sent = _handler()
        await h.handle_message(json.dumps(payload))
        assert sent[-1]["type"] == "error"
        assert target.read_bytes() == b"old"
        await h.handle_message(json.dumps({**payload, "overwrite": True}))
        assert sent[-1]["type"] == "file_saved"
        assert target.read_bytes() == PNG

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "data",
        [base64.b64encode(b"not a png").decode(), "***not base64***", 42],
    )
    async def test_rejects_non_png_payloads(self, data, tmp_path) -> None:
        h, sent = _handler()
        target = tmp_path / "x.png"
        await h.handle_message(
            json.dumps({"type": "save_image_file", "path": str(target), "data_base64": data})
        )
        assert sent[-1]["type"] == "error"
        assert not target.exists()

    @pytest.mark.asyncio
    async def test_missing_folder_is_an_error(self, tmp_path) -> None:
        h, sent = _handler()
        await h.handle_message(
            json.dumps(
                {
                    "type": "save_image_file",
                    "path": str(tmp_path / "nope" / "x.png"),
                    "data_base64": base64.b64encode(PNG).decode(),
                }
            )
        )
        assert sent[-1]["type"] == "error"
