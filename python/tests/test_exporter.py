"""Tests for engine.exporter — format selection, path validation, error handling."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from engine.exporter import RadarExporter, _extract_datetime
from tests import radar_data

# ---------------------------------------------------------------------------
# Format dispatch
# ---------------------------------------------------------------------------


class TestFormatSelection:
    def test_unsupported_format_raises(self, synthetic_datatree: xr.DataTree) -> None:
        exporter = RadarExporter()
        with pytest.raises(ValueError, match="Unsupported export format"):
            exporter.export(synthetic_datatree, fmt="bmp")

    @pytest.mark.parametrize("fmt", ["png", "PNG", "  png  ", "Png"])
    def test_format_normalization(
        self, synthetic_datatree: xr.DataTree, tmp_output_file: Path, fmt: str
    ) -> None:
        """Format strings should be lowered and stripped before dispatch."""
        exporter = RadarExporter()
        result = exporter.export(
            synthetic_datatree,
            fmt=fmt,
            output_path=str(tmp_output_file),
            variable="DBZH",
            sweep=0,
        )
        assert Path(result).exists()

    def test_geotiff_not_implemented(self, synthetic_datatree: xr.DataTree) -> None:
        exporter = RadarExporter()
        with pytest.raises(NotImplementedError, match="GeoTIFF"):
            exporter.export(synthetic_datatree, fmt="geotiff")


# ---------------------------------------------------------------------------
# Image export (PNG)
# ---------------------------------------------------------------------------


class TestImageExport:
    def test_png_creates_file(self, synthetic_datatree: xr.DataTree, tmp_output_file: Path) -> None:
        exporter = RadarExporter()
        result = exporter.export(
            synthetic_datatree,
            fmt="png",
            output_path=str(tmp_output_file),
            variable="DBZH",
            sweep=0,
        )
        assert Path(result).exists()
        assert Path(result).stat().st_size > 0

    def test_png_auto_selects_variable(
        self, synthetic_datatree: xr.DataTree, tmp_output_file: Path
    ) -> None:
        """When variable is None, exporter should pick the first data var."""
        exporter = RadarExporter()
        result = exporter.export(
            synthetic_datatree,
            fmt="png",
            output_path=str(tmp_output_file),
            variable=None,
            sweep=0,
        )
        assert Path(result).exists()

    def test_png_invalid_sweep_raises(
        self, synthetic_datatree: xr.DataTree, tmp_output_file: Path
    ) -> None:
        exporter = RadarExporter()
        with pytest.raises(IndexError, match="out of range"):
            exporter.export(
                synthetic_datatree,
                fmt="png",
                output_path=str(tmp_output_file),
                variable="DBZH",
                sweep=999,
            )

    def test_png_invalid_variable_raises(
        self, synthetic_datatree: xr.DataTree, tmp_output_file: Path
    ) -> None:
        exporter = RadarExporter()
        with pytest.raises(KeyError, match="not found"):
            exporter.export(
                synthetic_datatree,
                fmt="png",
                output_path=str(tmp_output_file),
                variable="NONEXISTENT",
                sweep=0,
            )

    def test_png_without_path_goes_to_a_visible_export_folder(
        self,
        synthetic_datatree: xr.DataTree,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No destination goes to $XRADAR_EXPORT_DIR (default ~/Downloads/radarx-exports)."""
        monkeypatch.setenv("XRADAR_EXPORT_DIR", str(tmp_path / "exports"))
        exporter = RadarExporter()
        first = exporter.export(
            synthetic_datatree, fmt="png", output_path=None, variable="DBZH", sweep=0
        )
        second = exporter.export(
            synthetic_datatree, fmt="png", output_path=None, variable="DBZH", sweep=0
        )
        assert Path(first).parent == tmp_path / "exports" and Path(first).exists()
        assert Path(second).exists() and first != second  # same second -> no overwrite

    def test_default_export_dir_is_not_the_system_temp(self, monkeypatch) -> None:
        from engine.exporter import default_export_dir

        monkeypatch.delenv("XRADAR_EXPORT_DIR", raising=False)
        d = default_export_dir()
        assert d.name == "radarx-exports"
        assert str(Path(tempfile.gettempdir()).resolve()) not in str(d.resolve())

    def test_progress_callback_called(
        self, synthetic_datatree: xr.DataTree, tmp_output_file: Path
    ) -> None:
        exporter = RadarExporter()
        calls: list[tuple[int, str]] = []

        def track(percent: int, message: str) -> None:
            calls.append((percent, message))

        exporter.export(
            synthetic_datatree,
            fmt="png",
            output_path=str(tmp_output_file),
            variable="DBZH",
            sweep=0,
            progress=track,
        )
        assert len(calls) >= 2
        # Last call should be 100%
        assert calls[-1][0] == 100


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


class TestPathValidation:
    def test_output_path_is_resolved(self, synthetic_datatree: xr.DataTree, tmp_path: Path) -> None:
        exporter = RadarExporter()
        relative_ish = str(tmp_path / "subdir" / ".." / "out.png")
        # Ensure parent exists
        (tmp_path / "subdir").mkdir(exist_ok=True)
        result = exporter.export(
            synthetic_datatree,
            fmt="png",
            output_path=relative_ish,
            variable="DBZH",
            sweep=0,
        )
        resolved = Path(result)
        assert resolved.is_absolute()
        assert ".." not in str(resolved)


# ---------------------------------------------------------------------------
# Regression tests: paths, batch export, vector output, animation
# ---------------------------------------------------------------------------


class TestExportRobustness:
    def test_user_output_path_parent_is_created(
        self, synthetic_datatree: xr.DataTree, tmp_path: Path
    ) -> None:
        target = tmp_path / "does" / "not" / "exist" / "out.png"
        result = RadarExporter().export(
            synthetic_datatree, fmt="png", output_path=str(target), variable="DBZH", sweep=0
        )
        assert Path(result).exists()

    def test_failed_export_leaves_no_temp_file_or_open_figure(
        self,
        synthetic_datatree: xr.DataTree,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import matplotlib.figure
        import matplotlib.pyplot as plt

        monkeypatch.setenv("XRADAR_EXPORT_DIR", str(tmp_path))

        def boom(self: object, *args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(matplotlib.figure.Figure, "savefig", boom)
        plt.close("all")
        with pytest.raises(OSError, match="disk full"):
            RadarExporter().export(
                synthetic_datatree, fmt="png", output_path=None, variable="DBZH", sweep=0
            )
        assert list(tmp_path.iterdir()) == []
        assert plt.get_fignums() == []

    def test_svg_embeds_raster_mesh(self, synthetic_datatree: xr.DataTree, tmp_path: Path) -> None:
        """The PPI mesh must be rasterised: vector quads made SVG/PDF huge and slow."""
        out = tmp_path / "ppi.svg"
        RadarExporter().export(
            synthetic_datatree, fmt="svg", dpi=100, output_path=str(out), variable="DBZH", sweep=0
        )
        assert out.stat().st_size < 3_000_000
        assert "<image" in out.read_text(errors="ignore")

    def test_csv_rows_match_finite_gates(
        self, synthetic_datatree: xr.DataTree, tmp_path: Path
    ) -> None:
        import numpy as np
        import pandas as pd

        sweep_ds = synthetic_datatree["sweep_0"].to_dataset()
        vals = sweep_ds["DBZH"].values.copy()
        vals[10:20, 5:50] = np.nan
        sweep_ds["DBZH"] = (("azimuth", "range"), vals)
        tree = xr.DataTree.from_dict({"/": xr.Dataset(), "/sweep_0": sweep_ds})

        out = tmp_path / "x.csv"
        RadarExporter().export(
            tree, fmt="csv", dpi=100, output_path=str(out), variable="DBZH", sweep=0
        )
        df = pd.read_csv(out, comment="#")  # the file starts with a "# key: value" metadata block
        assert len(df) == int(np.isfinite(vals).sum())
        row = df.iloc[0]
        i = int(np.argmin(np.abs(sweep_ds.azimuth.values - row["azimuth_deg"])))
        j = int(np.argmin(np.abs(sweep_ds.range.values - row["range_m"])))
        assert vals[i, j] == pytest.approx(row["DBZH"], abs=1e-3)


class TestBatchExport:
    def test_duplicates_are_exported_once(
        self, synthetic_datatree: xr.DataTree, tmp_path: Path
    ) -> None:
        saved = RadarExporter().batch_export(
            synthetic_datatree,
            variables=["DBZH", "DBZH"],
            sweeps=[0, 0],
            fmt="png",
            dpi=72,
            output_dir=str(tmp_path / "out"),
        )
        assert len(saved) == 1
        assert Path(saved[0]).parent == (tmp_path / "out").resolve()

    def test_total_failure_raises_instead_of_empty_success(
        self, synthetic_datatree: xr.DataTree, tmp_path: Path
    ) -> None:
        with pytest.raises(RuntimeError, match="failed for all"):
            RadarExporter().batch_export(
                synthetic_datatree,
                variables=["NOPE"],
                sweeps=[0],
                fmt="png",
                dpi=72,
                output_dir=str(tmp_path),
            )

    @pytest.mark.parametrize("bad_dpi", [10, 1000])
    def test_invalid_dpi_fails_fast(
        self, synthetic_datatree: xr.DataTree, tmp_path: Path, bad_dpi: int
    ) -> None:
        with pytest.raises(ValueError, match="DPI"):
            RadarExporter().batch_export(
                synthetic_datatree,
                variables=["DBZH"],
                sweeps=[0],
                dpi=bad_dpi,
                output_dir=str(tmp_path),
            )

    def test_unsupported_format_fails_fast(
        self, synthetic_datatree: xr.DataTree, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError, match="Unsupported"):
            RadarExporter().batch_export(
                synthetic_datatree,
                variables=["DBZH"],
                sweeps=[0],
                fmt="bmp",
                output_dir=str(tmp_path),
            )

    def test_safe_stem_strips_path_components(self) -> None:
        from engine.exporter import _safe_stem

        for name in ("../../etc/passwd", "a/b\\c", "..", ""):
            stem = _safe_stem(name)
            assert "/" not in stem
            assert "\\" not in stem
            assert stem not in ("", "..")

    def test_data_format_extensions(self) -> None:
        from engine.exporter import _FORMAT_EXT

        assert _FORMAT_EXT["netcdf"] == "nc"
        assert _FORMAT_EXT["zarr"] == "zarr"


class TestAnimation:
    def test_gif_has_all_frames_with_equal_size(
        self, synthetic_datatree: xr.DataTree, tmp_path: Path
    ) -> None:
        from PIL import Image

        out = tmp_path / "a.gif"
        RadarExporter().export_animation(
            synthetic_datatree,
            variable="DBZH",
            sweeps=[0, 1, 2],
            output_path=str(out),
            dpi=60,
            fig_size=4.0,
        )
        with Image.open(out) as gif:
            assert gif.n_frames == 3
            sizes = set()
            for i in range(gif.n_frames):
                gif.seek(i)
                sizes.add(gif.size)
        assert len(sizes) == 1


class TestGeometryHelpers:
    def test_polar_edges_are_centred_on_rays(self) -> None:
        import numpy as np

        from engine.exporter import _polar_edges

        az_edges, r_edges = _polar_edges(np.array([0.0, 1.0, 2.0, 3.0]), np.array([1.0, 2.0, 3.0]))
        np.testing.assert_allclose(np.rad2deg(az_edges), [-0.5, 0.5, 1.5, 2.5, 3.5])
        np.testing.assert_allclose(r_edges, [0.5, 1.5, 2.5, 3.5])

    def test_polar_edges_monotonic_across_north(self) -> None:
        import numpy as np

        from engine.exporter import _polar_edges

        az_edges, _ = _polar_edges(np.array([358.0, 359.0, 0.0, 1.0]), np.array([1.0, 2.0]))
        assert np.all(np.diff(az_edges) > 0)

    def test_datetime_extraction_tolerates_non_datetime_time(self) -> None:
        import numpy as np

        from engine.exporter import _extract_datetime

        ds = xr.Dataset(coords={"time": ("azimuth", np.arange(3.0))})
        assert _extract_datetime(xr.DataTree(), ds) == ""

    def test_datetime_is_the_earliest_ray_not_the_first_in_azimuth_order(self) -> None:
        t = np.datetime64("2024-05-01T12:00:00", "ns") + np.array(
            [30, 5, 0, 12, 40]
        ) * np.timedelta64(1, "s")
        t[0] = np.datetime64("NaT")  # the first ray has no time at all
        ds = xr.Dataset(coords={"time": ("azimuth", t)})
        assert _extract_datetime(xr.DataTree(), ds) == "2024-05-01 12:00:00"
        t2 = np.datetime64("2024-05-01T12:00:30", "ns") + np.array([0, -25, -30]) * np.timedelta64(
            1, "s"
        )
        ds2 = xr.Dataset(coords={"time": ("azimuth", t2)})
        assert _extract_datetime(xr.DataTree(), ds2) == "2024-05-01 12:00:00"  # was ...:30

    def test_extreme_options_are_clamped(self) -> None:
        from engine.exporter import _opt_float

        assert _opt_float({"fig_size": 1e9}, "fig_size", 10.0, 2.0, 30.0) == 30.0
        assert _opt_float({"fig_size": "abc"}, "fig_size", 10.0, 2.0, 30.0) == 10.0
        assert _opt_float({"fig_size": float("nan")}, "fig_size", 10.0, 2.0, 30.0) == 10.0


# ---------------------------------------------------------------------------
# NetCDF-unsafe attributes (NEXRAD volumes carry booleans such as mpda_vcp)
# ---------------------------------------------------------------------------


def _tree_with_unsafe_attrs() -> xr.DataTree:
    import numpy as np

    from tests.conftest import make_synthetic_datatree

    tree = make_synthetic_datatree(n_azimuth=12, n_range=20)
    for node in tree.subtree:
        ds = node.to_dataset()
        ds.attrs = {**ds.attrs, "mpda_vcp": True, "flag_np": np.bool_(False), "nothing": None}
        for var in ds.data_vars.values():
            var.attrs = {**var.attrs, "is_valid": True}
        node.dataset = ds
    return tree


def test_netcdf_safe_tree_coerces_attrs_without_touching_original() -> None:
    from engine.exporter import _netcdf_safe_tree

    tree = _tree_with_unsafe_attrs()
    safe = _netcdf_safe_tree(tree)
    root = safe.to_dataset().attrs
    assert root["mpda_vcp"] == 1 and root["flag_np"] == 0 and root["nothing"] == ""
    assert not isinstance(root["mpda_vcp"], bool)
    # the live tree keeps its original values
    assert tree.to_dataset().attrs["mpda_vcp"] is True


def test_sanitized_attrs_are_accepted_by_netcdf4(tmp_path) -> None:
    from engine.exporter import _netcdf_safe_tree

    tree = _tree_with_unsafe_attrs()
    with pytest.raises(TypeError):  # sanity: the raw attributes really are rejected
        tree.to_dataset().to_netcdf(tmp_path / "raw.nc", engine="netcdf4")
    for i, node in enumerate(_netcdf_safe_tree(tree).subtree):
        node.to_dataset().to_netcdf(tmp_path / f"safe_{i}.nc", engine="netcdf4")


_KATX = radar_data.katx() or Path("/nonexistent/KATX20130717_195021_V06")


@pytest.mark.skipif(
    not _KATX.exists() or not os.environ.get("XRADAR_SLOW_TESTS"),
    reason="slow (~45 s): set XRADAR_SLOW_TESTS=1 and provide the real NEXRAD sample",
)
def test_real_nexrad_netcdf_export_roundtrip(tmp_path) -> None:
    from engine.reader import RadarReader

    reader = RadarReader()
    reader.open_file(str(_KATX))
    out = RadarExporter().export(
        reader.datatree, fmt="netcdf", output_path=str(tmp_path / "katx.nc")
    )
    with xr.open_dataset(out) as ds:
        assert ds.sizes["time"] > 0 and "DBZH" in ds.data_vars


class TestCustomTitle:
    def _title(self, **kw):
        import matplotlib.pyplot as plt

        from engine.exporter import render_publication_figure
        from tests.conftest import make_synthetic_datatree

        fig = render_publication_figure(make_synthetic_datatree(), "DBZH", 0, dpi=50, **kw)
        try:
            return fig.axes[0].get_title()
        finally:
            plt.close(fig)

    def test_own_title_is_used_verbatim(self):
        assert self._title(title_override="My storm") == "My storm"

    def test_placeholders_are_filled_in(self):
        text = self._title(title_override="{variable} sweep {sweep} {elevation}deg {unknown}")
        assert text.startswith("DBZH sweep 0 ") and "{unknown}" in text and "{variable}" not in text

    def test_hidden_title_stays_hidden(self):
        assert self._title(show_title=False, title_override="x") == ""
