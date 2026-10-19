"""A long series of files: opened one at a time, grids spilled to disk, same numbers as in memory."""

from __future__ import annotations

import glob
import tempfile
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("radarx")

from engine import api
from engine.hydro import accumulate as acc
from engine.reader import RadarReader
from tests.hydro_synth import make_tree

LAT, LON = 35.0, -97.0


@pytest.fixture(autouse=True)
def hyd_files(monkeypatch):
    """``*.hyd``: '<ISO time> <rate>'; counts how often each file is opened."""
    real_open = RadarReader.open_file
    opened: dict[str, int] = {}

    def fake_open(self, path: str):
        if not path.endswith(".hyd"):
            return real_open(self, path)
        opened[path] = opened.get(path, 0) + 1
        when, rate = Path(path).read_text().split()
        r = float(rate)
        tree = make_tree(
            LAT, LON, lambda x, y: np.full(np.broadcast(x, y).shape, r), when, n_range=200, n_az=180
        )
        self._commit(tree, path, "synthetic")
        return self.get_schema()

    monkeypatch.setattr(RadarReader, "open_file", fake_open)
    return opened


def _write_series(tmp_path: Path, n: int, step_min: int = 5, rate: float = 6.0) -> list[Path]:
    t0 = np.datetime64("2024-05-01T12:00:00")
    out = []
    for k in range(n):
        p = tmp_path / f"v{k:03d}.hyd"
        p.write_text(f"{t0 + np.timedelta64(k * step_min * 60, 's')} {rate}")
        out.append(p)
    return out


def test_a_long_series_spills_to_disk_and_gives_the_same_numbers(tmp_path, hyd_files, monkeypatch):
    files = _write_series(tmp_path, 30)  # more than FieldStore.SPILL_AFTER (24)
    before = set(glob.glob(str(Path(tempfile.gettempdir()) / "xrd_qpe_*")))
    kw = {"min_dbz": -30.0, "resolution_m": 3000.0, "max_range_km": 80.0}
    spilled = api.qpe(files, extent=(-97.9, 34.3, -96.1, 35.7), **kw)
    # memory mode for the same inputs
    monkeypatch.setattr(acc.FieldStore, "SPILL_AFTER", 1000)
    inmem = api.qpe(files, extent=(-97.9, 34.3, -96.1, 35.7), **kw)
    hours = 29 * 5 / 60
    wet = np.isfinite(spilled.accumulation) & (spilled.range_km > 8)
    assert wet.sum() > 100
    assert spilled.accumulation[wet] == pytest.approx(6.0 * hours, rel=3e-3)
    both = np.isfinite(spilled.accumulation) & np.isfinite(inmem.accumulation)
    np.testing.assert_allclose(
        spilled.accumulation[both], inmem.accumulation[both], rtol=1e-4, atol=1e-5
    )
    # float16 storage of the derived fields: quality/height/range agree to their precision
    q = np.isfinite(spilled.quality) & np.isfinite(inmem.quality)
    np.testing.assert_allclose(spilled.quality[q], inmem.quality[q], atol=2e-3)
    # the temporary folder is gone
    assert set(glob.glob(str(Path(tempfile.gettempdir()) / "xrd_qpe_*"))) == before


def test_each_file_is_decoded_once_when_the_extent_is_given_twice_otherwise(tmp_path, hyd_files):
    files = _write_series(tmp_path, 4)
    kw = {"min_dbz": -30.0, "resolution_m": 3000.0, "max_range_km": 80.0}
    api.qpe(files, extent=(-97.9, 34.3, -96.1, 35.7), **kw)
    assert all(v == 1 for v in hyd_files.values()) and len(hyd_files) == 4
    hyd_files.clear()
    api.qpe(files, **kw)  # no extent: one pass to read the radar site, one to compute
    assert all(v == 2 for v in hyd_files.values())


def test_paths_are_opened_lazily_and_errors_name_the_file(tmp_path, hyd_files):
    files = _write_series(tmp_path, 3)
    bad = tmp_path / "broken.hyd"
    bad.write_text("not-a-time")
    with pytest.raises(Exception, match=r"broken\.hyd|cannot open|not enough"):
        api.qpe([*files, bad], min_dbz=-30.0, resolution_m=3000.0, max_range_km=80.0)
    with pytest.raises(api.XrdInputError, match="File not found"):
        api.qpe([tmp_path / "missing.hyd"])
    with pytest.raises(api.XrdInputError, match="directory"):
        api.qpe([tmp_path])
