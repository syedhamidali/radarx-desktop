"""Format selection in RadarReader.open_file: fast path, xradar 0.13 engines, discovery."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import xarray as xr
import xradar as xd

from engine import reader as reader_mod
from engine.reader import (
    _FORMAT_TABLE,
    RadarReader,
    _discover_format,
    _get_opener,
    _guess_formats,
)
from tests import radar_data
from tests.conftest import make_synthetic_datatree

NEXRAD_FILE = os.environ.get("XRADAR_TEST_NEXRAD") or str(radar_data.katx() or "")
CFRAD_FILE = os.environ.get("XRADAR_TEST_CFRADIAL1") or str(
    radar_data.get(radar_data.DOW8_PPI) or ""
)


@pytest.fixture(autouse=True)
def _fresh_opener_cache() -> Iterator[None]:
    _get_opener.cache_clear()
    yield
    _get_opener.cache_clear()


def _cdf_file(tmp_path: Path, name: str = "volume.nc") -> str:
    """A file that sniffs as NetCDF classic (CDF magic) but is not parsable."""
    p = tmp_path / name
    p.write_bytes(b"CDF\x01" + b"\x00" * 64)
    return str(p)


def _synthetic(*_a: Any, **_k: Any) -> xr.DataTree:
    return make_synthetic_datatree(n_azimuth=8, n_range=6)


def _time_first_cfradial2(path: Path) -> Path:
    """A CfRadial2 file whose sweeps run along ``time`` (what xradar's ``to_cfradial2`` writes)."""
    import numpy as np

    tree = make_synthetic_datatree(n_azimuth=36, n_range=30, elevations=[0.5, 1.5])
    t0 = np.datetime64("2024-01-01T00:00:00", "ns")
    for name in list(tree.children):
        ds = tree[name].to_dataset()
        ds = ds.assign_coords(time=("azimuth", t0 + np.arange(ds.sizes["azimuth"]) * 50_000_000))
        tree[name] = xr.DataTree(ds.swap_dims({"azimuth": "time"}))
    tree.to_netcdf(path)
    return path


class TestCfRadial2FirstDimension:
    """A time-first CfRadial2 volume gets the xradar model (``azimuth``) like every format.

    xradar's CfRadial2 reader keeps ``time`` as first dimension unless asked for
    ``first_dim="auto"``: the sweeps then had no ``azimuth`` dimension and every algorithm
    failed with ``KeyError: 'azimuth'``.
    """

    def test_sweeps_run_along_azimuth(self, tmp_path: Path) -> None:
        reader = RadarReader()
        assert reader.open_file(str(_time_first_cfradial2(tmp_path / "cf2.nc")))["format"] == (
            "cfradial2"
        )
        ds = reader.get_sweep_dataset(0)
        assert "azimuth" in ds["DBZH"].dims and "time" not in ds["DBZH"].dims
        assert ds.sizes["azimuth"] == 36

    def test_a_module_runs_on_it(self, tmp_path: Path) -> None:
        from engine import api

        vol = api.open(str(_time_first_cfradial2(tmp_path / "cf2.nc")))
        try:
            res = vol.run("qc_clean", sweep=0)
        finally:
            vol.close()
        assert res is not None

    def test_only_the_cfradial2_opener_asks_for_first_dim_auto(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, dict[str, Any]]] = []

        def fake_open_datatree(path: str, *, engine: str | None = None, **kw: Any) -> str:
            calls.append((str(engine), kw))
            return "tree"

        monkeypatch.setattr(xd, "open_datatree", fake_open_datatree)
        assert _get_opener("cfradial2")("/x") == "tree"
        assert _get_opener("cfradial1")("/y") == "tree"
        assert calls == [("cfradial2", {"first_dim": "auto"}), ("cfradial1", {})]


class TestNetcdfFamilyOrder:
    """A NetCDF file that is not CfRadial1 tries CfRadial2 next, not after every binary format."""

    def test_guess_puts_cfradial2_right_after_cfradial1(self, tmp_path: Path) -> None:
        for path in (_cdf_file(tmp_path), str(tmp_path / "volume.nc")):
            order = _guess_formats(path)[0]
            assert order[:2] == ["cfradial1", "cfradial2"]

    def test_cfradial2_file_opens_without_scanning_it_as_other_formats(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        tried: list[str] = []
        real = reader_mod._get_opener

        def spy(fmt: str) -> Callable[[str], xr.DataTree]:
            tried.append(fmt)
            return real(fmt)

        monkeypatch.setattr(reader_mod, "_get_opener", spy)
        schema = RadarReader().open_file(str(_time_first_cfradial2(tmp_path / "cf2.nc")))
        assert schema["format"] == "cfradial2"
        assert tried == ["cfradial1", "cfradial2"]


class TestFormatTable:
    def test_new_formats_present(self) -> None:
        labels = {label for label, _ in _FORMAT_TABLE}
        assert {"uf", "cfradial2", "imd"} <= labels

    def test_labels_unique(self) -> None:
        labels = [label for label, _ in _FORMAT_TABLE]
        assert len(labels) == len(set(labels))

    def test_hint_flag(self, tmp_path: Path) -> None:
        assert _guess_formats(_cdf_file(tmp_path))[1] is True
        unknown = tmp_path / "mystery.xyz"
        unknown.write_bytes(b"\x00" * 32)
        order, hinted = _guess_formats(str(unknown))
        assert hinted is False
        assert len(order) == len(_FORMAT_TABLE)
        assert _guess_formats(str(tmp_path / "scan.uf"))[0][0] == "uf"

    def test_every_table_entry_resolves_to_an_xradar_engine(self) -> None:
        # xradar >= 0.13 has a reader for each format of the table.
        for label, _ in _FORMAT_TABLE:
            assert callable(_get_opener(label)), label

    def test_an_unknown_format_is_refused(self) -> None:
        with pytest.raises(ValueError, match="Unknown format"):
            _get_opener("no_such_format")


class TestOpenerSelection:
    def test_uses_open_datatree_with_engine_when_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, str | None]] = []

        def fake_open_datatree(path: str, *, engine: str | None = None, **kw: Any) -> str:
            calls.append((path, engine))
            return "tree"

        monkeypatch.setattr(xd, "open_datatree", fake_open_datatree, raising=False)
        monkeypatch.setattr(
            xd.io, "list_xradar_engines", lambda: ["cfradial1", "nexradlevel2"], raising=False
        )
        assert _get_opener("nexrad_level2")("/x") == "tree"
        assert _get_opener("cfradial1")("/y") == "tree"
        assert calls == [("/x", "nexradlevel2"), ("/y", "cfradial1")]

    def test_a_format_xradar_does_not_list_is_skipped_not_reported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(xd.io, "list_xradar_engines", lambda: ["cfradial2"])
        with pytest.raises(ValueError, match="Could not open") as exc:
            RadarReader().open_file(_cdf_file(tmp_path))
        assert "cfradial1" not in str(exc.value)  # ImportError entries are not failures
        assert "imd" not in str(exc.value)

    def test_imd_reader_must_be_claimed_by_its_signature(self, tmp_path: Path) -> None:
        # xradar's IMD reader opens any NetCDF as a one-sweep volume: a file without the IMD
        # variables (here a map grid) must not be taken for one.
        import numpy as np

        grid = tmp_path / "grid.nc"
        xr.Dataset(
            {"DBZH_composite": (("y", "x"), np.zeros((4, 5)))},
            coords={"y": np.arange(4.0), "x": np.arange(5.0)},
        ).to_netcdf(grid)
        with pytest.raises(ValueError, match="Could not open"):
            RadarReader().open_file(str(grid))


class TestSmartFallback:
    def _record_openers(self, monkeypatch: pytest.MonkeyPatch, working: set[str]) -> list[str]:
        order: list[str] = []

        def fake_get_opener(fmt: str) -> Callable[[str], xr.DataTree]:
            def opener(path: str) -> xr.DataTree:
                order.append(fmt)
                if fmt in working:
                    return _synthetic()
                raise ValueError(f"not {fmt}")

            return opener

        monkeypatch.setattr(reader_mod, "_get_opener", fake_get_opener)
        return order

    def test_discovery_goes_first_for_unrecognised_files(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        order = self._record_openers(monkeypatch, {"rainbow"})
        monkeypatch.setattr(reader_mod, "_discover_format", lambda p: "rainbow")
        p = tmp_path / "mystery.xyz"
        p.write_bytes(b"\x00" * 64)
        schema = RadarReader().open_file(str(p))
        assert order == ["rainbow"]
        assert schema["format"] == "rainbow"

    def test_discovery_not_consulted_when_extension_hints(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        order = self._record_openers(monkeypatch, {"cfradial1"})

        def boom(path: str) -> str:
            raise AssertionError("fast path must not run discovery")

        monkeypatch.setattr(reader_mod, "_discover_format", boom)
        RadarReader().open_file(_cdf_file(tmp_path))
        assert order == ["cfradial1"]

    def test_engine_unknown_to_table_is_tried_last(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        order = self._record_openers(monkeypatch, {"newfmt"})
        monkeypatch.setattr(reader_mod, "_discover_format", lambda p: "newfmt")
        schema = RadarReader().open_file(_cdf_file(tmp_path))
        assert schema["format"] == "newfmt"
        assert order[-1] == "newfmt"
        assert order[0] == "cfradial1"  # hinted format still first

    def test_empty_tree_is_not_a_successful_open(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Lenient backends return an empty tree for any netCDF; the reader moves on.
        def fake_get_opener(fmt: str) -> Callable[[str], xr.DataTree]:
            if fmt == "cfradial2":
                return lambda p: xr.DataTree()
            if fmt == "cfradial1":
                return lambda p: (_ for _ in ()).throw(ValueError("nope"))
            raise ImportError(fmt)

        monkeypatch.setattr(reader_mod, "_get_opener", fake_get_opener)
        with pytest.raises(ValueError, match="Could not open") as exc:
            RadarReader().open_file(_cdf_file(tmp_path))
        assert "no radar sweeps" in str(exc.value) or "cfradial1" in str(exc.value)

    def test_failed_open_leaves_previous_format(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._record_openers(monkeypatch, {"cfradial1"})
        reader = RadarReader()
        reader.open_file(_cdf_file(tmp_path))
        self._record_openers(monkeypatch, set())
        with pytest.raises(ValueError):
            reader.open_file(_cdf_file(tmp_path, "other.nc"))
        assert reader._format == "cfradial1"


# ---------------------------------------------------------------------------
# Real files + timing (skipped when the sample data is not on this machine)
# ---------------------------------------------------------------------------

real_nexrad = pytest.mark.skipif(
    not (NEXRAD_FILE and Path(NEXRAD_FILE).is_file()), reason="NEXRAD sample missing"
)
real_cfrad = pytest.mark.skipif(
    not (CFRAD_FILE and Path(CFRAD_FILE).is_file()), reason="CfRadial1 sample missing"
)


def _timed_open(path: str) -> tuple[dict[str, Any], float]:
    t0 = time.perf_counter()
    schema = RadarReader().open_file(path)
    return schema, time.perf_counter() - t0


@real_nexrad
def test_open_real_nexrad() -> None:
    schema, dt = _timed_open(NEXRAD_FILE)
    print(f"\n[bench] xradar {xd.__version__} open KATX: {dt:.2f}s")
    assert schema["format"] == "nexrad_level2"
    assert len(schema["sweeps"]) >= 10
    assert dt < 15


@real_cfrad
def test_open_real_cfradial1_is_fast() -> None:
    schema, dt = _timed_open(CFRAD_FILE)
    print(f"\n[bench] xradar {xd.__version__} open COW1 cfradial1: {dt:.2f}s")
    assert schema["format"] == "cfradial1"
    assert len(schema["sweeps"]) >= 1
    # Regression guard for the stacking slowdown seen with a naive smart opener.
    assert dt < 10


@real_cfrad
def test_discovery_agrees_with_fast_path_on_real_files() -> None:
    assert _discover_format(CFRAD_FILE) == "cfradial1"
    if Path(NEXRAD_FILE).exists():
        assert _discover_format(NEXRAD_FILE) == "nexrad_level2"
