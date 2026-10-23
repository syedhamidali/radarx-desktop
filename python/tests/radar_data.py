"""Real radar files for the tests.

Files come from `open-radar-data <https://github.com/openradar/open-radar-data>`_ (the same sample
data radarx and xradar use) or from a local folder; tests that need one skip when it is missing.

Lookup order for :func:`get`:

1. the folder in ``$XRADAR_TEST_DATA_DIR``
2. a copy already in the open-radar-data cache (``DATASETS.abspath``; no network)
3. only when ``$XRADAR_TEST_DOWNLOAD=1`` (set on GitHub Actions): download it with
   ``open_radar_data``

So a developer's laptop never downloads anything by surprise, while CI runs the real-data tests.
"""

from __future__ import annotations

import os
from pathlib import Path

# open-radar-data names used by the tests
KATX = "KATX20130717_195021_V06"  # NEXRAD Level II (36 MB)
KILX = "KILX20230629_154426_V06"  # NEXRAD Level II, 12 cuts (10 MB)
DOW8_PPI = "cfrad.20211011_201557.188_to_20211011_201617.720_DOW8_PPI.nc"  # CfRadial1 (18 MB)
DOW8_RHI = "cfrad.20211011_201733.023_to_20211011_201745.299_DOW8_RHI.nc"  # CfRadial1 RHI (3.7 MB)
DOW8_RHI_2 = "cfrad.20211011_223602.712_to_20211011_223612.091_DOW8_RHI.nc"  # RHI (1.7 MB)

#: folders with the author's own example files (optional, local only)
EXAMPLES_DIR = Path(
    os.environ.get("XRADAR_EXAMPLES_DIR", "~/Downloads/Others/XRADAR_EXAMPLES")
).expanduser()


def _dataset_dirs() -> list[Path]:
    dirs: list[Path] = []
    env = os.environ.get("XRADAR_TEST_DATA_DIR")
    if env:
        dirs.append(Path(env).expanduser())
    try:
        import open_radar_data

        dirs.append(Path(str(open_radar_data.DATASETS.abspath)))
    except Exception:  # not installed, or its cache folder is not available
        pass
    dirs.append(Path.home() / "Library" / "Caches" / "open-radar-data")  # macOS pooch default
    dirs.append(Path.home() / ".cache" / "open-radar-data")  # Linux pooch default
    return dirs


def get(name: str) -> Path | None:
    """Path of the open-radar-data file *name*, or ``None`` when it is not available."""
    for folder in _dataset_dirs():
        candidate = folder / name
        if candidate.is_file():
            return candidate
    if os.environ.get("XRADAR_TEST_DOWNLOAD") == "1":
        try:
            import open_radar_data

            return Path(open_radar_data.DATASETS.fetch(name))
        except Exception as exc:  # offline, rate-limited, package missing ...
            print(f"[radar_data] could not fetch {name}: {type(exc).__name__}: {exc}")
    return None


def example(relative: str) -> Path | None:
    """One of the author's own example files (``$XRADAR_EXAMPLES_DIR``), or ``None``."""
    for base in (
        (Path(os.environ["XRADAR_TEST_DATA_DIR"]).expanduser(),)
        if os.environ.get("XRADAR_TEST_DATA_DIR")
        else ()
    ):
        if (base / relative).is_file():
            return base / relative
    path = EXAMPLES_DIR / relative
    return path if path.is_file() else None


def katx() -> Path | None:
    """The KATX NEXRAD volume: ``$XRADAR_TEST_KATX``, else open-radar-data."""
    env = os.environ.get("XRADAR_TEST_KATX")
    if env and Path(env).is_file():
        return Path(env)
    return get(KATX)


#: the 2021-12-11 Kentucky/Tennessee tornado-outbreak volumes of the tutorial (optional, local only)
EVENT_DIR = Path(
    os.environ.get("XRADAR_TEST_EVENT_DIR", "~/Downloads/radarx_movie_tutorial/data/nexrad")
).expanduser()


def event_volume(radar: str, stamp: str) -> Path | None:
    """``<radar>20211211_<stamp>_V06`` from the tutorial folder (``$XRADAR_TEST_EVENT_DIR``)."""
    path = EVENT_DIR / radar / f"{radar}20211211_{stamp}_V06"
    return path if path.is_file() else None
