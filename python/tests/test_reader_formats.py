"""Format selection in RadarReader.open_file: fast path, xradar 0.13 API, 0.12 fallback."""

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
from tests.conftest import make_synthetic_datatree

HAS_OPEN_DATATREE = hasattr(xd, "open_datatree")

_SCRATCH = (
    "/private/tmp/claude-502/-Users-syed44-Git-Stuff-Git-Projects-xradar-desktop/"
    "4919c2aa-1324-499f-9e5a-79edf19be890/scratchpad"
)
NEXRAD_FILE = os.environ.get("XRADAR_TEST_NEXRAD", f"{_SCRATCH}/data/KATX20130717_195021_V06")
CFRAD_FILE = os.environ.get(
    "XRADAR_TEST_CFRADIAL1",
    "/Users/syed44/Downloads/untitled_folder/"
    "cfrad.20220330_212540.952_to_20220330_212711.375_COW1high_SUR.nc",
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


class TestFormatTable:
    def test_new_formats_present(self) -> None:
        labels = {label for label, _, _ in _FORMAT_TABLE}
        assert {"uf", "cfradial2", "imd"} <= labels

    def test_labels_unique(self) -> None:
        labels = [label for label, _, _ in _FORMAT_TABLE]
        assert len(labels) == len(set(labels))

    def test_hint_flag(self, tmp_path: Path) -> None:
        assert _guess_formats(_cdf_file(tmp_path))[1] is True
        unknown = tmp_path / "mystery.xyz"
        unknown.write_bytes(b"\x00" * 32)
        order, hinted = _guess_formats(str(unknown))
        assert hinted is False
        assert len(order) == len(_FORMAT_TABLE)
        assert _guess_formats(str(tmp_path / "scan.uf"))[0][0] == "uf"

    def test_installed_xradar_openers_resolve(self) -> None:
        # Every table entry that this xradar provides must resolve to a callable.
        resolved = []
        for label, func, _ in _FORMAT_TABLE:
            try:
                opener = _get_opener(label)
            except ImportError:
                assert not hasattr(xd.io, func)
                continue
            assert callable(opener)
            resolved.append(label)
        assert {"nexrad_level2", "cfradial1", "odim"} <= set(resolved)


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

    def test_falls_back_to_dedicated_opener_without_open_datatree(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulates xradar 0.12: no xd.open_datatree, no discover_engine."""
        monkeypatch.delattr(xd, "open_datatree", raising=False)
        monkeypatch.delattr(xd.io, "discover_engine", raising=False)
        monkeypatch.delattr(xd.io, "list_xradar_engines", raising=False)
        monkeypatch.setattr(xd.io, "open_cfradial1_datatree", _synthetic)
        assert _get_opener("cfradial1") is _synthetic
        assert _discover_format("/nonexistent") is None

    def test_open_file_works_end_to_end_without_open_datatree(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delattr(xd, "open_datatree", raising=False)
        monkeypatch.delattr(xd.io, "discover_engine", raising=False)
        monkeypatch.setattr(xd.io, "open_cfradial1_datatree", _synthetic)
        reader = RadarReader()
        schema = reader.open_file(_cdf_file(tmp_path))
        assert schema["format"] == "cfradial1"
        assert reader._format == "cfradial1"
        assert reader.get_schema()["format"] == "cfradial1"
        assert len(schema["sweeps"]) == 3

    def test_unavailable_format_is_skipped_not_reported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delattr(xd, "open_datatree", raising=False)
        monkeypatch.delattr(xd.io, "open_cfradial1_datatree", raising=False)
        monkeypatch.delattr(xd.io, "open_imd_datatree", raising=False)
        with pytest.raises(ValueError, match="Could not open") as exc:
            RadarReader().open_file(_cdf_file(tmp_path))
        assert "cfradial1" not in str(exc.value)  # ImportError entries are not failures
        assert "imd" not in str(exc.value)


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

real_nexrad = pytest.mark.skipif(not Path(NEXRAD_FILE).exists(), reason="NEXRAD sample missing")
real_cfrad = pytest.mark.skipif(not Path(CFRAD_FILE).exists(), reason="CfRadial1 sample missing")


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
@pytest.mark.skipif(not HAS_OPEN_DATATREE, reason="xradar without open_datatree")
def test_discovery_agrees_with_fast_path_on_real_files() -> None:
    assert _discover_format(CFRAD_FILE) == "cfradial1"
    if Path(NEXRAD_FILE).exists():
        assert _discover_format(NEXRAD_FILE) == "nexrad_level2"
