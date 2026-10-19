"""Tests for range-height (RHI) support: detection, ray order, wire format, guards and export."""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

import server
from engine import rhi
from engine.exporter import RadarExporter, render_publication_figure
from engine.reader import RadarReader
from engine.volume import _collect_sweeps
from tests.conftest import make_synthetic_datatree
from tests.test_server import _binary, _make_handler

CPOL_RHI = Path(
    os.environ.get(
        "XRADAR_RHI_TEST_FILE",
        "/Users/syed44/Downloads/Others/XRADAR_EXAMPLES/"
        "RHI_20170825_223408_to_20170825_223445_CPOLRVP_MATT_RHI.nc",
    )
)

# ---------------------------------------------------------------------------
# Synthetic data
# ---------------------------------------------------------------------------


def make_rhi_sweep(
    *,
    n_rays: int = 40,
    n_range: int = 120,
    azimuth: float = 135.0,
    el_lo: float = 1.0,
    el_hi: float = 40.0,
    shuffle: bool = True,
    mode: str | None = "rhi",
    fixed_angle: float | None = None,
    echo_top_km: float = 8.0,
) -> xr.Dataset:
    """An RHI sweep: constant azimuth, rays over elevation (stored out of order), with an echo of
    known top height so the geometry can be checked."""
    el = np.linspace(el_lo, el_hi, n_rays)
    if shuffle:
        el = np.concatenate([el[n_rays // 2 :], el[: n_rays // 2]])  # like CPOL's stitched passes
    range_m = np.linspace(500.0, 60_000.0, n_range)
    _, height = rhi.beam_geometry(range_m[None, :], el[:, None])
    values = np.where(height / 1000.0 < echo_top_km, 30.0, np.nan).astype(np.float32)
    ds = xr.Dataset(
        {"DBZH": (["azimuth", "range"], values)},
        coords={
            "azimuth": ("azimuth", np.full(n_rays, azimuth)),
            "elevation": ("azimuth", el.astype(np.float32)),
            "range": range_m,
        },
        attrs={"sweep_fixed_angle": azimuth if fixed_angle is None else fixed_angle},
    )
    if mode is not None:
        ds["sweep_mode"] = xr.DataArray(mode)
    return ds


def rhi_tree(*sweeps: xr.Dataset) -> xr.DataTree:
    nodes = {f"/sweep_{i}": ds for i, ds in enumerate(sweeps)}
    return xr.DataTree.from_dict({"/": xr.Dataset(attrs={"instrument_name": "RHI-TEST"}), **nodes})


def _reader(tree: xr.DataTree) -> RadarReader:
    reader = RadarReader()
    reader._dtree = tree
    reader._format = "synthetic"
    return reader


@pytest.fixture(autouse=True)
def _isolated_state():
    saved = (dict(server._file_readers), server._last_file_id, dict(server._generations))
    server._file_readers.clear()
    server._generations.clear()
    server._last_file_id = None
    yield
    server._file_readers.clear()
    server._file_readers.update(saved[0])
    server._last_file_id = saved[1]
    server._generations.clear()
    server._generations.update(saved[2])


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class TestDetection:
    @pytest.mark.parametrize(
        "mode", ["rhi", "RHI", "manual_rhi", "elevation_surveillance", "Elevation Surveillance"]
    )
    def test_rhi_modes(self, mode: str) -> None:
        assert rhi.is_rhi(make_rhi_sweep(mode=mode))

    @pytest.mark.parametrize("mode", ["azimuth_surveillance", "sector", "ppi", "vertical_pointing"])
    def test_ppi_modes_win_over_the_heuristic(self, mode: str) -> None:
        assert not rhi.is_rhi(make_rhi_sweep(mode=mode))

    def test_heuristic_without_sweep_mode(self) -> None:
        assert rhi.is_rhi(make_rhi_sweep(mode=None))

    def test_ppi_is_not_an_rhi(self) -> None:
        ppi = make_synthetic_datatree(n_azimuth=90, n_range=20)["sweep_0"].to_dataset()
        assert not rhi.is_rhi(ppi)
        sector = ppi.isel(azimuth=slice(0, 40))
        assert not rhi.is_rhi(sector)

    def test_a_few_rays_are_not_enough_for_the_heuristic(self) -> None:
        assert not rhi.is_rhi(make_rhi_sweep(n_rays=3, mode=None))

    def test_no_angles_means_ppi(self) -> None:
        ds = make_rhi_sweep().drop_vars("elevation")
        assert not rhi.is_rhi(ds.drop_vars("sweep_mode"))

    def test_circular_helpers_cross_the_seam(self) -> None:
        az = np.array([359.0, 0.5, 1.0, 358.5])
        assert rhi.circular_span(az) == pytest.approx(2.5)
        assert rhi.circular_median(az) == pytest.approx(0.0, abs=0.8) or rhi.circular_median(
            az
        ) == pytest.approx(360.0, abs=0.8)


class TestRays:
    def test_rays_are_sorted_by_elevation(self) -> None:
        ds = make_rhi_sweep(shuffle=True)
        rays = rhi.rhi_rays(ds)
        assert rays is not None
        assert np.all(np.diff(rays.elevation) > 0)
        assert rays.order.size == 40
        # the permutation really maps the stored rays
        assert np.allclose(ds["elevation"].values[rays.order], rays.elevation)

    def test_none_for_ppi(self) -> None:
        assert (
            rhi.rhi_rays(make_synthetic_datatree(n_azimuth=30, n_range=5)["sweep_0"].to_dataset())
            is None
        )

    def test_plane_azimuth_uses_fixed_angle_when_rays_point_there(self) -> None:
        assert rhi.rhi_rays(make_rhi_sweep(azimuth=135.0)).plane_azimuth == pytest.approx(135.0)

    def test_plane_azimuth_ignores_an_elevation_stored_as_fixed_angle(self) -> None:
        # NPOL writes ~30 (an elevation) there while the rays point at 243
        ds = make_rhi_sweep(azimuth=243.0, fixed_angle=29.9)
        assert rhi.rhi_rays(ds).plane_azimuth == pytest.approx(243.0)

    def test_plane_azimuth_across_north(self) -> None:
        ds = make_rhi_sweep(azimuth=359.9, fixed_angle=12.0)
        assert rhi.rhi_rays(ds).plane_azimuth == pytest.approx(359.9, abs=0.01)

    def test_duplicate_elevations_keep_the_ray_nearest_the_plane(self) -> None:
        # a pseudo-RHI cut from a volume: two rays per tilt, one near 242 and one near 244
        el = np.array([0.5, 0.5, 0.9, 0.9, 1.4, 1.4])
        az = np.array([244.3, 242.2, 244.2, 241.8, 242.3, 244.3])
        ds = xr.Dataset(
            {"DBZH": (["azimuth", "range"], np.zeros((6, 3), dtype=np.float32))},
            coords={
                "azimuth": ("azimuth", az),
                "elevation": ("azimuth", el),
                "range": [1e3, 2e3, 3e3],
            },
            attrs={"sweep_fixed_angle": 242.0},
        )
        ds["sweep_mode"] = xr.DataArray("rhi")
        rays = rhi.rhi_rays(ds)
        assert rays.elevation.tolist() == [0.5, 0.9, 1.4]
        assert rays.azimuth.tolist() == pytest.approx([242.2, 241.8, 242.3])
        assert rays.plane_azimuth == 242.0

    def test_nonfinite_angles_are_dropped(self) -> None:
        ds = make_rhi_sweep(n_rays=20)
        ds["elevation"].values[3] = np.nan
        assert rhi.rhi_rays(ds).order.size == 19

    def test_describe_sweep(self) -> None:
        info = rhi.describe_sweep(make_rhi_sweep(el_lo=1.0, el_hi=40.0))
        assert info == {
            "kind": "rhi",
            "azimuth": 135.0,
            "elevation_min": 1.0,
            "elevation_max": 40.0,
            "n_rays": 40,
        }
        assert rhi.describe_sweep(xr.Dataset()) == {"kind": "ppi"}


class TestGeometry:
    def test_matches_radarx(self) -> None:
        # radarx.fundamentals.geometry: beam_center_height(100000, 0.5), ground_range(100000, 0.5)
        ground, height = rhi.beam_geometry(100_000.0, 0.5)
        assert float(height) == pytest.approx(1461.1325028166175, abs=1e-6)
        assert float(ground) == pytest.approx(99981.30372405128, abs=1e-6)

    def test_radar_height_offsets_the_result(self) -> None:
        _, h0 = rhi.beam_geometry(50_000.0, 3.0)
        _, h1 = rhi.beam_geometry(50_000.0, 3.0, radar_height_m=320.0)
        assert float(h1 - h0) == pytest.approx(320.0)

    def test_vertical_beam(self) -> None:
        ground, height = rhi.beam_geometry(10_000.0, 90.0)
        assert float(ground) == pytest.approx(0.0, abs=1e-6)
        assert float(height) == pytest.approx(10_000.0, abs=1e-6)

    def test_ray_bounds_bridge_small_steps_and_split_holes(self) -> None:
        lo, hi = rhi.ray_bounds(np.array([1.0, 2.0, 3.0, 4.0]))
        assert lo.tolist() == [0.5, 1.5, 2.5, 3.5]
        assert hi.tolist() == [1.5, 2.5, 3.5, 4.5]
        lo, hi = rhi.ray_bounds(np.array([1.0, 2.0, 3.0, 4.0, 30.0, 31.0]))
        assert hi[3] == pytest.approx(4.5) and lo[4] == pytest.approx(29.5)  # the hole stays open

    def test_contiguous_mesh_blanks_a_hole(self) -> None:
        angles = np.array([1.0, 2.0, 3.0, 20.0, 21.0])
        values = np.arange(10, dtype=float).reshape(5, 2)
        edges, vals = rhi.contiguous_mesh(angles, values)
        assert edges.size == vals.shape[0] + 1
        assert np.all(np.diff(edges) > 0)
        assert vals.shape[0] == 6  # one blank row inserted
        assert np.isnan(vals[3]).all()
        assert not np.isnan(vals[[0, 1, 2, 4, 5]]).any()
        # without a hole the rows pass through untouched
        edges2, vals2 = rhi.contiguous_mesh(np.array([1.0, 2.0, 3.0]), values[:3])
        assert vals2.shape == (3, 2) and edges2.tolist() == [0.5, 1.5, 2.5, 3.5]


# ---------------------------------------------------------------------------
# Reader schema
# ---------------------------------------------------------------------------


class TestSchema:
    def test_sweeps_are_labelled_rhi_or_ppi(self) -> None:
        ppi = make_synthetic_datatree(n_azimuth=30, n_range=20, elevations=[0.5])[
            "sweep_0"
        ].to_dataset()
        reader = _reader(
            rhi_tree(make_rhi_sweep(azimuth=135.0), make_rhi_sweep(azimuth=200.0), ppi)
        )
        sweeps = reader.get_schema()["sweeps"]
        assert [s["kind"] for s in sweeps] == ["rhi", "rhi", "ppi"]
        assert sweeps[0]["azimuth"] == 135.0 and sweeps[1]["azimuth"] == 200.0
        assert sweeps[0]["elevation_min"] == pytest.approx(1.0)
        assert "azimuth" not in sweeps[2]
        assert reader.sweep_kinds() == ["rhi", "rhi", "ppi"]

    def test_ppi_files_gain_only_a_kind(self) -> None:
        sweeps = _reader(make_synthetic_datatree(n_azimuth=30, n_range=20)).get_schema()["sweeps"]
        assert all(
            set(s) == {"index", "elevation", "variables", "kind"} and s["kind"] == "ppi"
            for s in sweeps
        )

    def test_site_altitude_is_reported_for_rhi(self) -> None:
        tree = rhi_tree(make_rhi_sweep())
        tree["/"].dataset = xr.Dataset(
            {"altitude": xr.DataArray(320.0)}, attrs={"instrument_name": "X"}
        )
        sweeps = _reader(tree).get_schema()["sweeps"]
        assert sweeps[0]["altitude"] == 320.0

    def test_kinds_cache_follows_the_tree(self) -> None:
        reader = _reader(rhi_tree(make_rhi_sweep()))
        assert reader.sweep_kinds() == ["rhi"]
        reader._dtree = make_synthetic_datatree(n_azimuth=30, n_range=20, elevations=[0.5])
        assert reader.sweep_kinds() == ["ppi"]


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------


class TestWireFormat:
    def test_pack_field_without_rays_is_the_ppi_payload(self) -> None:
        da = make_synthetic_datatree(n_azimuth=10, n_range=8)["sweep_0"]["DBZH"]
        fields, arrays = server._pack_field(da, "DBZH")
        assert len(arrays) == 3
        assert "sweep_kind" not in fields and "elevation_bytes" not in fields

    def test_pack_field_with_rays(self) -> None:
        ds = make_rhi_sweep(n_rays=20, n_range=30)
        rays = rhi.rhi_rays(ds)
        fields, arrays = server._pack_field(ds["DBZH"], "DBZH", rays=rays)
        assert fields["sweep_kind"] == "rhi" and fields["rhi_azimuth"] == 135.0
        assert fields["n_az"] == 20 and fields["elevation_bytes"] == 20 * 4
        az, _rng, values, el = arrays
        assert el.dtype == np.float32 and np.all(np.diff(el) > 0)
        assert np.allclose(az, 135.0)
        # the rows follow the elevation order, not the stored order
        expected = np.nan_to_num(ds["DBZH"].values[rays.order], nan=server.NODATA_SENTINEL)
        assert np.array_equal(values, expected.astype(np.float32))
        assert fields["values_bytes"] == values.nbytes

    @pytest.mark.asyncio
    async def test_get_sweep_data_delivers_the_elevation_array(self) -> None:
        ds = make_rhi_sweep(n_rays=24, n_range=50)
        file_id = server._register_reader(_reader(rhi_tree(ds)))
        handler, _ = _make_handler()
        await handler.handle_message(
            json.dumps(
                {"type": "get_sweep_data", "variable": "DBZH", "sweep": 0, "file_id": file_id}
            )
        )
        header = handler._sent[0]
        assert header["type"] == "sweep_data" and header["sweep_kind"] == "rhi"
        buf = _binary(handler)
        assert len(buf) == (
            header["azimuth_bytes"]
            + header["range_bytes"]
            + header["values_bytes"]
            + header["elevation_bytes"]
        )
        el = np.frombuffer(buf, dtype=np.float32, offset=len(buf) - header["elevation_bytes"])
        assert el.size == 24 and np.all(np.diff(el) > 0)
        assert el[0] == pytest.approx(1.0) and el[-1] == pytest.approx(40.0)

    @pytest.mark.asyncio
    async def test_ppi_sweep_data_is_unchanged(self) -> None:
        file_id = server._register_reader(
            _reader(make_synthetic_datatree(n_azimuth=20, n_range=30))
        )
        handler, _ = _make_handler()
        await handler.handle_message(
            json.dumps(
                {"type": "get_sweep_data", "variable": "DBZH", "sweep": 0, "file_id": file_id}
            )
        )
        header = handler._sent[0]
        assert "sweep_kind" not in header and "elevation_bytes" not in header
        assert (
            len(_binary(handler))
            == header["azimuth_bytes"] + header["range_bytes"] + header["values_bytes"]
        )

    @pytest.mark.asyncio
    async def test_data_table_shows_elevation_for_an_rhi(self) -> None:
        ds = make_rhi_sweep(n_rays=12, n_range=10, shuffle=False)
        file_id = server._register_reader(_reader(rhi_tree(ds)))
        handler, _ = _make_handler()
        await handler.handle_message(
            json.dumps(
                {"type": "get_data_table", "variable": "DBZH", "sweep": 0, "file_id": file_id}
            )
        )
        reply = handler._sent[0]
        assert reply["type"] == "data_table_result" and reply["ray_axis"] == "elevation"
        assert reply["azimuths"][0] == pytest.approx(1.0) and reply["azimuths"][
            -1
        ] == pytest.approx(40.0)


# ---------------------------------------------------------------------------
# PPI-only features refuse an RHI with a clear message
# ---------------------------------------------------------------------------


class TestPpiOnlyGuards:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("msg", "error_type"),
        [
            ({"type": "get_volume", "variable": "DBZH", "box": {}}, "error"),
            ({"type": "get_cross_section_3d", "variable": "DBZH"}, "error"),
            ({"type": "run_module", "module": "hot_gates", "sweep": 0}, "module_error"),
            ({"type": "identify_cells", "variable": "DBZH", "sweep": 0}, "error"),
        ],
    )
    async def test_rhi_requests_get_a_clear_error(self, msg: dict, error_type: str) -> None:
        file_id = server._register_reader(_reader(rhi_tree(make_rhi_sweep())))
        handler, _ = _make_handler()
        await handler.handle_message(json.dumps({**msg, "file_id": file_id}))
        assert len(handler._sent) == 1
        reply = handler._sent[0]
        assert reply["type"] == error_type
        assert "PPI sweeps" in reply["message"] and "RHI" in reply["message"]

    @pytest.mark.asyncio
    async def test_mosaic_and_winds_use_their_own_error_types(self) -> None:
        file_id = server._register_reader(_reader(rhi_tree(make_rhi_sweep())))
        handler, _ = _make_handler()
        await handler.handle_message(
            json.dumps(
                {
                    "type": "create_mosaic",
                    "request_id": "r1",
                    "file_ids": [file_id],
                    "variable": "DBZH",
                }
            )
        )
        await handler.handle_message(
            json.dumps({"type": "run_multidoppler", "request_id": "r2", "file_ids": [file_id]})
        )
        assert [(m["type"], m["request_id"]) for m in handler._sent] == [
            ("mosaic_error", "r1"),
            ("multidoppler_error", "r2"),
        ]
        assert all("PPI sweeps" in m["message"] for m in handler._sent)

    def test_ppi_files_are_never_blocked(self) -> None:
        file_id = server._register_reader(
            _reader(make_synthetic_datatree(n_azimuth=20, n_range=10))
        )
        for t in ("get_volume", "run_module", "create_mosaic", "identify_cells"):
            assert (
                server._ppi_only_block(t, {"file_id": file_id, "file_ids": [file_id], "sweep": 0})
                is None
            )

    def test_a_mixed_file_only_blocks_the_rhi_sweep(self) -> None:
        ppi = make_synthetic_datatree(n_azimuth=30, n_range=20, elevations=[0.5])[
            "sweep_0"
        ].to_dataset()
        file_id = server._register_reader(_reader(rhi_tree(make_rhi_sweep(), ppi)))
        assert server._ppi_only_block("run_module", {"file_id": file_id, "sweep": 0}) is not None
        assert server._ppi_only_block("run_module", {"file_id": file_id, "sweep": 1}) is None
        assert server._ppi_only_block("get_volume", {"file_id": file_id}) is None  # PPIs remain

    def test_volume_skips_rhi_sweeps(self) -> None:
        ppi = make_synthetic_datatree(n_azimuth=60, n_range=20, elevations=[0.5, 1.5])
        tree = rhi_tree(make_rhi_sweep(), ppi["sweep_0"].to_dataset(), ppi["sweep_1"].to_dataset())
        sweeps = _collect_sweeps(tree, "DBZH", None)
        assert len(sweeps) == 2  # the RHI is not stacked as a PPI


# ---------------------------------------------------------------------------
# Figure export
# ---------------------------------------------------------------------------


def _mesh_top_km(fig) -> float:
    """Highest cell corner (km) of the drawn data (non-blank cells)."""
    ax = fig.axes[0]
    mesh = ax.collections[0]
    coords = mesh.get_coordinates()  # (n_y, n_x, 2)
    data = np.asarray(mesh.get_array())
    filled = ~np.ma.getmaskarray(data) if np.ma.isMaskedArray(data) else np.isfinite(data)
    filled = filled.reshape(coords.shape[0] - 1, coords.shape[1] - 1)
    ys = np.maximum(coords[:-1, :-1, 1], coords[1:, 1:, 1])
    return float(ys[filled].max())


class TestFigureExport:
    def test_rhi_is_a_range_height_plot(self) -> None:
        import matplotlib.pyplot as plt

        tree = rhi_tree(make_rhi_sweep(echo_top_km=8.0))
        fig = render_publication_figure(tree, "DBZH", 0, dpi=60)
        try:
            ax = fig.axes[0]
            assert ax.get_xlabel() == "Ground range (km)"
            assert ax.get_ylabel() == "Height above radar (km)"
            assert "RHI az 135.0" in ax.get_title()
            # equal aspect: one km looks the same in x and y
            assert ax.get_aspect() in (1.0, "equal")
            # the synthetic echo is 8 km tall; the cells reach just above it (one cell of slack)
            top = _mesh_top_km(fig)
            assert 7.5 < top < 9.5
            ymax = ax.get_ylim()[1]
            assert 8.0 < ymax < 12.0
            assert ax.get_xlim()[0] <= 0 <= ax.get_xlim()[1]
            assert len(fig.axes) == 2  # plot + colorbar
        finally:
            plt.close(fig)

    def test_title_and_options_are_honoured(self) -> None:
        import matplotlib.pyplot as plt

        tree = rhi_tree(make_rhi_sweep())
        fig = render_publication_figure(
            tree,
            "DBZH",
            0,
            dpi=50,
            show_colorbar=False,
            show_rings=False,
            title_override="My RHI {variable} {radar}",
            vmin=0,
            vmax=50,
        )
        try:
            assert len(fig.axes) == 1
            assert fig.axes[0].get_title() == "My RHI DBZH RHI-TEST"
            assert fig.axes[0].collections[0].get_clim() == (0, 50)
            assert not fig.axes[0].lines[1:]  # no grid lines, only the ground line
        finally:
            plt.close(fig)

    def test_png_export_goes_through_the_rhi_path(self, tmp_path: Path) -> None:
        out = tmp_path / "rhi.png"
        path = RadarExporter().export(
            rhi_tree(make_rhi_sweep()),
            fmt="png",
            dpi=72,
            output_path=str(out),
            variable="DBZH",
            sweep=0,
        )
        assert Path(path).stat().st_size > 5_000
        assert Path(path).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    def test_ppi_figures_are_still_polar_maps(self) -> None:
        import matplotlib.pyplot as plt

        fig = render_publication_figure(
            make_synthetic_datatree(n_azimuth=60, n_range=40), "DBZH", 0, dpi=50
        )
        try:
            assert fig.axes[0].get_xlabel() == "East-West Distance (km)"
        finally:
            plt.close(fig)

    def test_blank_scan_still_draws(self) -> None:
        import matplotlib.pyplot as plt

        ds = make_rhi_sweep()
        ds["DBZH"].values[:] = np.nan
        fig = render_publication_figure(rhi_tree(ds), "DBZH", 0, dpi=50)
        plt.close(fig)

    @pytest.mark.skipif(not CPOL_RHI.exists(), reason="CPOL RHI example file not available")
    def test_real_cpol_rhi(self, tmp_path: Path) -> None:
        import matplotlib.pyplot as plt

        reader = RadarReader()
        reader.open_file(str(CPOL_RHI))
        schema = reader.get_schema()
        assert [s["kind"] for s in schema["sweeps"]] == ["rhi"] * 4
        assert schema["sweeps"][0]["azimuth"] == pytest.approx(135.0)
        fig = render_publication_figure(reader.datatree, "DBZ", 0, dpi=60)
        try:
            ax = fig.axes[0]
            assert ax.get_xlabel() == "Ground range (km)"
            # the data reach ~124 km of range and ~41 km of height (the file's own height limit)
            assert 110 < ax.get_xlim()[1] < 135
            assert 38 < _mesh_top_km(fig) < 47
        finally:
            plt.close(fig)
        # and through the wire format: rays are in ascending elevation although the file is not
        rays = rhi.rhi_rays(reader.get_sweep_dataset(0, mask=False))
        assert rays is not None and np.all(np.diff(rays.elevation) > 0)
        assert rays.order.size == 152
