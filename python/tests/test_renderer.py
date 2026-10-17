"""Tests for engine.renderer — input validation and parameter bounds."""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr

from engine.renderer import (
    RadarRenderer,
    _apply_colormap,
    _build_colorbar,
    _polar_to_cartesian,
    _rasterize_ppi,
    _resolve_cmap,
    _resolve_range,
)

# ---------------------------------------------------------------------------
# Colormap resolution
# ---------------------------------------------------------------------------


class TestResolveCmap:
    @pytest.mark.parametrize(
        "variable,expected_substring",
        [
            ("DBZH", "rainbow"),
            ("VRADH", "coolwarm"),
            ("WRADH", "fire"),
            ("ZDR", "bgy"),
            ("RHOHV", "blues"),
        ],
    )
    def test_known_variables(self, variable: str, expected_substring: str) -> None:
        # _resolve_cmap returns a list of hex colors; just check it's non-empty
        cmap = _resolve_cmap(variable)
        assert isinstance(cmap, list)
        assert len(cmap) > 0

    def test_unknown_variable_fallback(self) -> None:
        cmap = _resolve_cmap("TOTALLY_UNKNOWN")
        assert isinstance(cmap, list)
        assert len(cmap) > 0

    def test_reflectivity_substring_match(self) -> None:
        """Variables containing 'dbz' should get rainbow even if not exact match."""
        import colorcet as cc

        cmap = _resolve_cmap("corrected_dbz_field")
        assert cmap == cc.rainbow

    def test_velocity_substring_match(self) -> None:
        import colorcet as cc

        cmap = _resolve_cmap("mean_velocity")
        assert cmap == cc.coolwarm


# ---------------------------------------------------------------------------
# Value range resolution
# ---------------------------------------------------------------------------


class TestResolveRange:
    def test_known_variable(self) -> None:
        vmin, vmax = _resolve_range("DBZH")
        assert vmin == -20.0
        assert vmax == 75.0

    def test_unknown_variable_returns_none(self) -> None:
        vmin, vmax = _resolve_range("UNKNOWN_VAR")
        assert vmin is None
        assert vmax is None

    @pytest.mark.parametrize("variable", ["VRADH", "ZDR", "RHOHV", "PHIDP", "KDP"])
    def test_all_ranges_have_correct_order(self, variable: str) -> None:
        vmin, vmax = _resolve_range(variable)
        assert vmin is not None
        assert vmax is not None
        assert vmin < vmax


# ---------------------------------------------------------------------------
# Polar to Cartesian conversion
# ---------------------------------------------------------------------------


class TestPolarToCartesian:
    def test_basic_conversion(self) -> None:
        azimuth = np.array([0.0, 90.0, 180.0, 270.0])
        range_m = np.array([1000.0, 2000.0])
        values = np.ones((4, 2), dtype=np.float32)

        df = _polar_to_cartesian(azimuth, range_m, values)
        assert len(df) == 8  # 4 azimuths * 2 ranges, all finite
        assert set(df.columns) == {"x", "y", "val"}

    def test_nan_values_excluded(self) -> None:
        azimuth = np.array([0.0, 90.0])
        range_m = np.array([1000.0])
        values = np.array([[1.0], [np.nan]], dtype=np.float32)

        df = _polar_to_cartesian(azimuth, range_m, values)
        assert len(df) == 1  # only the finite value

    def test_north_azimuth_gives_positive_y(self) -> None:
        """Azimuth 0 (north) should produce x~0, y>0."""
        azimuth = np.array([0.0])
        range_m = np.array([1000.0])
        values = np.array([[5.0]], dtype=np.float32)

        df = _polar_to_cartesian(azimuth, range_m, values)
        assert abs(df["x"].iloc[0]) < 1.0  # essentially zero
        assert df["y"].iloc[0] > 999.0

    def test_east_azimuth_gives_positive_x(self) -> None:
        """Azimuth 90 (east) should produce x>0, y~0."""
        azimuth = np.array([90.0])
        range_m = np.array([1000.0])
        values = np.array([[5.0]], dtype=np.float32)

        df = _polar_to_cartesian(azimuth, range_m, values)
        assert df["x"].iloc[0] > 999.0
        assert abs(df["y"].iloc[0]) < 1.0


# ---------------------------------------------------------------------------
# Renderer — parameter validation
# ---------------------------------------------------------------------------


class TestRendererParameterBounds:
    def test_render_empty_data(self, synthetic_dataarray: xr.DataArray) -> None:
        """DataArray with all NaN should produce an empty-result image."""
        renderer = RadarRenderer()
        nan_data = synthetic_dataarray.copy()
        nan_data.values[:] = np.nan

        result = renderer.render_sweep(nan_data, "DBZH", sweep=0, width=100, height=100)
        assert "image" in result
        assert "metadata" in result
        assert result["metadata"]["variable"] == "DBZH"

    def test_render_returns_base64_png(self, synthetic_dataarray: xr.DataArray) -> None:
        renderer = RadarRenderer()
        result = renderer.render_sweep(synthetic_dataarray, "DBZH", sweep=0, width=200, height=200)
        assert "image" in result
        # Verify it's valid base64 by decoding
        import base64

        raw = base64.b64decode(result["image"])
        # PNG magic bytes
        assert raw[:4] == b"\x89PNG"

    def test_render_respects_dimensions(self, synthetic_dataarray: xr.DataArray) -> None:
        renderer = RadarRenderer()
        result = renderer.render_sweep(synthetic_dataarray, "DBZH", sweep=0, width=64, height=64)
        assert result["metadata"]["width"] == 64
        assert result["metadata"]["height"] == 64

    def test_render_with_bbox(self, synthetic_dataarray: xr.DataArray) -> None:
        renderer = RadarRenderer()
        bbox = [-50000, -50000, 50000, 50000]
        result = renderer.render_sweep(
            synthetic_dataarray, "DBZH", sweep=0, width=100, height=100, bbox=bbox
        )
        assert "image" in result


# ---------------------------------------------------------------------------
# PPI rasterization correctness
# ---------------------------------------------------------------------------


def _sample(azimuth, range_m, values, az_deg: float, r_m: float) -> float:
    """Rasterize a 1x1 image whose single pixel centre is at polar (az_deg, r_m)."""
    x = r_m * np.sin(np.radians(az_deg))
    y = r_m * np.cos(np.radians(az_deg))
    out = _rasterize_ppi(azimuth, range_m, values, 1, 1, (x - 1.0, x + 1.0), (y - 1.0, y + 1.0))
    assert out.shape == (1, 1)
    return float(out[0, 0])


class TestRasterizeAzimuth:
    def test_wraps_across_north(self) -> None:
        """Rays at 0.5, 2.5, ... 358.5: pixel at 359.6 is nearer ray 0.5 (0.9) than 358.5 (1.1)."""
        azimuth = 0.5 + 2.0 * np.arange(180)
        range_m = np.array([1000.0, 2000.0])
        values = np.repeat(np.arange(180, dtype=np.float32)[:, None], 2, axis=1)
        assert _sample(azimuth, range_m, values, 359.6, 1000.0) == 0.0
        assert _sample(azimuth, range_m, values, 359.0, 1000.0) == 179.0
        assert _sample(azimuth, range_m, values, 0.2, 1000.0) == 0.0
        assert _sample(azimuth, range_m, values, 1.7, 1000.0) == 1.0

    def test_unsorted_azimuth_mapping(self) -> None:
        """Scan order starting mid-circle (xradar style) must map to the right rows."""
        az_sorted = np.arange(0.5, 360.0, 1.0)
        shift = 100
        azimuth = np.roll(az_sorted, -shift)
        range_m = np.array([1000.0, 2000.0])
        values = np.repeat(np.roll(np.arange(360, dtype=np.float32), -shift)[:, None], 2, axis=1)
        for a in (0.4, 90.1, 179.6, 359.7):
            assert _sample(azimuth, range_m, values, a, 1500.0) == float(int(a))

    def test_sector_gap_is_blank(self) -> None:
        """A 90-degree sector scan must not smear its edge rays over the whole disc."""
        azimuth = np.arange(0.5, 90.0, 1.0)
        range_m = np.array([1000.0, 2000.0])
        values = np.ones((azimuth.size, 2), dtype=np.float32)
        assert _sample(azimuth, range_m, values, 45.0, 1000.0) == 1.0
        assert np.isnan(_sample(azimuth, range_m, values, 200.0, 1000.0))

    def test_matches_brute_force(self) -> None:
        rng = np.random.default_rng(3)
        # jitter avoids exact ties on the symmetric pixel grid
        azimuth = (np.arange(360) + 0.5 + rng.uniform(-0.2, 0.2, 360)) % 360.0
        range_m = 250.0 + 250.0 * np.arange(120)
        values = rng.uniform(0, 1, (360, 120)).astype(np.float32)
        w = h = 64
        lim = (-35_000.0, 35_000.0)
        out = _rasterize_ppi(azimuth, range_m, values, w, h, lim, lim)
        xs = lim[0] + (np.arange(w) + 0.5) * (lim[1] - lim[0]) / w
        ys = lim[1] - (np.arange(h) + 0.5) * (lim[1] - lim[0]) / h
        mismatches = 0
        for i in range(h):
            for j in range(w):
                x, y = xs[j], ys[i]
                r = np.hypot(x, y)
                a = np.degrees(np.arctan2(x, y)) % 360.0
                if r < range_m[0] - 125 or r > range_m[-1] + 125:
                    assert np.isnan(out[i, j])
                    continue
                d = np.abs(azimuth - a)
                ai = int(np.minimum(d, 360 - d).argmin())
                ri = int(np.abs(range_m - r).argmin())
                mismatches += values[ai, ri] != out[i, j]
        # azimuth uses a fine LUT: only pixels within ~0.01 deg of a ray midpoint may differ
        assert mismatches <= 0.01 * w * h


class TestRasterizeRange:
    def test_nearest_gate_not_next_gate(self) -> None:
        azimuth = np.arange(0.5, 360.0, 1.0)
        range_m = np.array([1000.0, 2000.0, 3000.0, 4000.0])
        values = np.tile(np.array([10.0, 20.0, 30.0, 40.0], dtype=np.float32), (360, 1))
        assert _sample(azimuth, range_m, values, 90.5, 1400.0) == 10.0
        assert _sample(azimuth, range_m, values, 90.5, 1600.0) == 20.0
        assert _sample(azimuth, range_m, values, 90.5, 3900.0) == 40.0

    def test_nonuniform_range_nearest(self) -> None:
        azimuth = np.arange(0.5, 360.0, 1.0)
        range_m = np.array([1000.0, 2000.0, 5000.0])
        values = np.tile(np.array([1.0, 2.0, 3.0], dtype=np.float32), (360, 1))
        assert _sample(azimuth, range_m, values, 10.5, 3400.0) == 2.0
        assert _sample(azimuth, range_m, values, 10.5, 3600.0) == 3.0

    def test_outside_annulus_is_nan(self) -> None:
        azimuth = np.arange(0.5, 360.0, 1.0)
        range_m = np.array([1000.0, 2000.0, 3000.0])
        values = np.ones((360, 3), dtype=np.float32)
        assert np.isnan(_sample(azimuth, range_m, values, 10.5, 200.0))
        assert np.isnan(_sample(azimuth, range_m, values, 10.5, 4000.0))
        assert _sample(azimuth, range_m, values, 10.5, 3400.0) == 1.0  # within the last gate

    def test_descending_range_is_reordered(self) -> None:
        azimuth = np.arange(0.5, 360.0, 1.0)
        range_m = np.array([3000.0, 2000.0, 1000.0])
        values = np.tile(np.array([3.0, 2.0, 1.0], dtype=np.float32), (360, 1))
        assert _sample(azimuth, range_m, values, 10.5, 1100.0) == 1.0

    def test_shape_mismatch_raises(self) -> None:
        with pytest.raises(ValueError):
            _rasterize_ppi(np.arange(4.0), np.arange(3.0), np.ones((3, 3)), 4, 4, (-1, 1), (-1, 1))


# ---------------------------------------------------------------------------
# Colormap / colorbar
# ---------------------------------------------------------------------------


class TestColormap:
    def test_nan_black_and_endpoints(self) -> None:
        import colorcet as cc

        grid = np.array([[np.nan, -20.0, 75.0, 200.0, -500.0]], dtype=np.float32)
        img = np.asarray(_apply_colormap(grid, cc.rainbow, -20.0, 75.0))
        assert img.shape == (1, 5, 4)
        assert tuple(img[0, 0]) == (0, 0, 0, 255)
        first = tuple(int(cc.rainbow[0][i : i + 2], 16) for i in (1, 3, 5))
        last = tuple(int(cc.rainbow[-1][i : i + 2], 16) for i in (1, 3, 5))
        assert tuple(img[0, 1, :3]) == first
        assert tuple(img[0, 2, :3]) == last
        assert tuple(img[0, 3, :3]) == last  # clipped high
        assert tuple(img[0, 4, :3]) == first  # clipped low
        assert (img[..., 3] == 255).all()

    def test_distinct_palettes_same_length_not_confused(self) -> None:
        """The LUT cache must key on palette contents, not object identity."""
        pal_a = ["#ff0000"] * 8
        grid = np.array([[0.5]], dtype=np.float32)
        a = tuple(np.asarray(_apply_colormap(grid, pal_a, 0.0, 1.0))[0, 0, :3])
        pal_b = ["#00ff00"] * 8
        b = tuple(np.asarray(_apply_colormap(grid, pal_b, 0.0, 1.0))[0, 0, :3])
        assert a == (255, 0, 0)
        assert b == (0, 255, 0)

    def test_degenerate_range(self) -> None:
        grid = np.full((2, 2), 5.0, dtype=np.float32)
        img = _apply_colormap(grid, ["#102030", "#405060"], 5.0, 5.0)
        assert img.size == (2, 2)

    def test_colorbar_gradient_direction(self) -> None:
        import colorcet as cc

        bar = np.asarray(_build_colorbar(cc.rainbow, 0.0, 1.0, "X", "", 200))
        x = 5 + 3  # inside the swatch
        top = tuple(bar[5 + 11 + 1, x, :3])
        bottom = tuple(bar[200 - 5 - 11 - 2, x, :3])
        c_hi = tuple(int(cc.rainbow[-1][i : i + 2], 16) for i in (1, 3, 5))
        c_lo = tuple(int(cc.rainbow[0][i : i + 2], 16) for i in (1, 3, 5))
        assert sum(abs(int(a) - int(b)) for a, b in zip(top, c_hi, strict=True)) < 30
        assert sum(abs(int(a) - int(b)) for a, b in zip(bottom, c_lo, strict=True)) < 30


# ---------------------------------------------------------------------------
# Render cache
# ---------------------------------------------------------------------------


def _polar_da(fill: float, n_az: int = 90, n_rng: int = 60) -> xr.DataArray:
    az = np.arange(n_az) * (360.0 / n_az)
    rng = 500.0 + 500.0 * np.arange(n_rng)
    vals = np.full((n_az, n_rng), fill, dtype=np.float32)
    return xr.DataArray(vals, dims=("azimuth", "range"), coords={"azimuth": az, "range": rng})


class TestRenderCache:
    def test_cache_isolated_per_token(self) -> None:
        r = RadarRenderer()
        a = r.render_sweep(_polar_da(10.0), "DBZH", 0, 32, 32, cache_token="file-a")
        b = r.render_sweep(_polar_da(60.0), "DBZH", 0, 32, 32, cache_token="file-b")
        assert a["image"] != b["image"]
        # Same token -> served from cache (identical object)
        assert r.render_sweep(_polar_da(10.0), "DBZH", 0, 32, 32, cache_token="file-a") is a
        assert r.render_sweep(_polar_da(60.0), "DBZH", 0, 32, 32, cache_token="file-b") is b

    def test_cache_isolated_without_token(self) -> None:
        """No token: the cache falls back to a content hash, never another file's frame."""
        r = RadarRenderer()
        a = r.render_sweep(_polar_da(10.0), "DBZH", 0, 32, 32)
        b = r.render_sweep(_polar_da(60.0), "DBZH", 0, 32, 32)
        assert a["image"] != b["image"]
        assert r.render_sweep(_polar_da(10.0), "DBZH", 0, 32, 32) is a

    def test_cache_is_lru(self) -> None:
        r = RadarRenderer()
        r._max_cache = 2
        da = _polar_da(30.0)
        k1 = r.render_sweep(da, "DBZH", 0, 16, 16, cache_token="t")
        r.render_sweep(da, "DBZH", 1, 16, 16, cache_token="t")
        assert r.render_sweep(da, "DBZH", 0, 16, 16, cache_token="t") is k1  # refresh k1
        r.render_sweep(da, "DBZH", 2, 16, 16, cache_token="t")  # evicts sweep=1, not sweep=0
        assert r.render_sweep(da, "DBZH", 0, 16, 16, cache_token="t") is k1
        assert len(r._cache) == 2

    def test_invalidate_clears(self) -> None:
        r = RadarRenderer()
        da = _polar_da(30.0)
        a = r.render_sweep(da, "DBZH", 0, 16, 16, cache_token="t")
        r.invalidate_cache()
        assert r.render_sweep(da, "DBZH", 0, 16, 16, cache_token="t") is not a

    def test_bbox_not_cached_and_validated(self) -> None:
        r = RadarRenderer()
        da = _polar_da(30.0)
        r.render_sweep(da, "DBZH", 0, 16, 16, bbox=[-1000, -1000, 1000, 1000], cache_token="t")
        assert len(r._cache) == 0
        with pytest.raises(ValueError):
            r.render_sweep(da, "DBZH", 0, 16, 16, bbox=[0, -1000, 0, 1000])
        with pytest.raises(ValueError):
            r.render_sweep(da, "DBZH", 0, 16, 16, bbox=[0, 0, float("nan"), 1])

    def test_bbox_with_origin_on_edge_renders(self) -> None:
        """Rings used to divide by zero when the viewport edge touched the radar."""
        r = RadarRenderer()
        res = r.render_sweep(
            _polar_da(30.0), "DBZH", 0, 32, 32, bbox=[0, 0, 20000, 20000], cache_token="t"
        )
        assert res["metadata"]["x_range"] == [0.0, 20.0]

    def test_output_png_decodes_with_colorbar_width(self) -> None:
        import base64
        import io

        from PIL import Image

        r = RadarRenderer()
        res = r.render_sweep(_polar_da(30.0), "DBZH", 0, 40, 30, cache_token="t")
        img = Image.open(io.BytesIO(base64.b64decode(res["image"])))
        assert img.size == (40 + 45, 30)
