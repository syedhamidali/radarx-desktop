"""Reliability hardening: exporter masking, module-run safety, memory budget, open errors."""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from tests.conftest import make_synthetic_datatree

KTLX = Path.home() / "Downloads/Others/XRADAR_EXAMPLES/KTLX20130520_201643_V06.gz"


def _coded_tree() -> xr.DataTree:
    """Synthetic tree whose DBZH is uint8-encoded with code 0/1 = no echo / range folded."""
    tree = make_synthetic_datatree(n_azimuth=90, n_range=120, elevations=[0.5])
    scale, offset = 0.5, -33.0
    codes = np.zeros((90, 120), dtype=np.int64)
    codes[:, :20] = 60  # -3 dBZ
    codes[:, 20:40] = 140  # 37 dBZ
    codes[10:20, 60:70] = 1  # range folded
    ds = tree["sweep_0"].to_dataset()
    da = xr.DataArray(
        offset + codes * scale,
        dims=("azimuth", "range"),
        coords={"azimuth": ds["azimuth"], "range": ds["range"]},
        attrs={"units": "dBZ"},
    )
    da.encoding.update({"dtype": np.dtype("uint8"), "scale_factor": scale, "add_offset": offset})
    ds["DBZH"] = da
    return xr.DataTree.from_dict({"/": tree.to_dataset(), "/sweep_0": ds})


class TestExporterMasking:
    def test_figure_no_echo_is_transparent(self) -> None:
        import matplotlib.pyplot as plt

        from engine.exporter import render_publication_figure

        fig = render_publication_figure(
            _coded_tree(),
            "DBZH",
            0,
            dpi=72,
            show_rings=False,
            show_colorbar=False,
            show_title=False,
        )
        try:
            mesh = fig.axes[0].collections[0]
            arr = np.ma.getmaskarray(mesh.get_array())
            assert arr.mean() > 0.5  # most of the sweep is no echo -> masked, not painted
        finally:
            plt.close(fig)

    def test_csv_has_no_decoded_no_echo_rows(self, tmp_path: Path) -> None:
        from engine.exporter import RadarExporter

        out = RadarExporter().export(
            _coded_tree(), "csv", output_path=str(tmp_path / "o.csv"), variable="DBZH", sweep=0
        )
        with open(out) as fh:
            rows = (line for line in fh if not line.startswith("#"))  # metadata block
            vals = [float(r["DBZH"]) for r in csv.DictReader(rows)]
        assert vals
        assert min(vals) > -33.0 + 1e-6
        assert len(vals) == 90 * 40  # only the real-echo gates

    def test_unencoded_data_unchanged(self, tmp_path: Path) -> None:
        from engine.exporter import RadarExporter

        out = RadarExporter().export(
            make_synthetic_datatree(n_azimuth=10, n_range=10, elevations=[0.5]),
            "csv",
            output_path=str(tmp_path / "o.csv"),
            variable="DBZH",
            sweep=0,
        )
        with open(out) as fh:
            rows = (line for line in fh if not line.startswith("#"))
            assert len(list(csv.DictReader(rows))) == 100


@pytest.mark.skipif(not KTLX.exists(), reason="KTLX sample not available")
class TestExporterMaskingRealFile:
    @pytest.fixture(scope="class")
    def reader(self):
        from engine.reader import RadarReader

        r = RadarReader()
        r.open_file(str(KTLX))
        return r

    @pytest.mark.parametrize(("var", "sweep", "low"), [("DBZH", 0, -33.0), ("VRADH", 1, None)])
    def test_png_background_not_colormap_minimum(self, reader, var, sweep, low) -> None:
        import matplotlib.pyplot as plt

        from engine.exporter import render_publication_figure

        fig = render_publication_figure(
            reader.datatree,
            var,
            sweep,
            dpi=72,
            show_rings=False,
            show_colorbar=False,
            show_title=False,
        )
        try:
            mesh = fig.axes[0].collections[0]
            masked = np.ma.getmaskarray(mesh.get_array())
            assert masked.mean() > 0.3  # large no-echo fraction is NaN/transparent
            vals = np.ma.getdata(mesh.get_array())[~masked]
            if low is not None:
                assert vals.min() > low
            else:
                assert vals.min() > -60  # -64.5 m/s "range folded" code is gone
            fig.canvas.draw()
            rgba = np.asarray(fig.canvas.buffer_rgba())
            cmap_min = np.array(mesh.cmap(0.0)) * 255
            near_min = (np.abs(rgba[..., :3].astype(float) - cmap_min[:3]).sum(-1) < 6).mean()
            assert near_min < 0.2  # no solid colormap-minimum background
        finally:
            plt.close(fig)

    def test_csv_has_no_minus_33(self, reader, tmp_path: Path) -> None:
        import pandas as pd

        from engine.exporter import RadarExporter

        out = RadarExporter().export(
            reader.datatree,
            "csv",
            output_path=str(tmp_path / "o.csv"),
            variable="DBZH",
            sweep=0,
        )
        df = pd.read_csv(out, comment="#")
        assert len(df) > 1000
        assert df["DBZH"].min() > -32.9
