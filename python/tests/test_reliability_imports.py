"""Heavy imports stay out of sidecar start-up (they load on first use / in a background thread)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PYTHON_DIR = Path(__file__).resolve().parent.parent


def _run(code: str) -> str:
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PYTHON_DIR,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout.strip()


def test_server_import_does_not_load_matplotlib_or_xradar() -> None:
    loaded = _run(
        "import sys, server;"
        "print(','.join(m for m in ('matplotlib', 'colorcet', 'xradar') if m in sys.modules))"
    )
    assert loaded == ""


def test_export_and_render_still_work_after_lazy_imports() -> None:
    out = _run(
        "import sys, numpy as np, xarray as xr;"
        "from engine.exporter import render_publication_figure, _get_cmap;"
        "from engine.renderer import RadarRenderer, _resolve_cmap;"
        "from tests.conftest import make_synthetic_datatree;"
        "t = make_synthetic_datatree(n_azimuth=30, n_range=40);"
        "fig = render_publication_figure(t, 'DBZH', 0, dpi=72);"
        "print(_get_cmap('DBZH'), len(_resolve_cmap('DBZH')) > 10, 'matplotlib' in sys.modules)"
    )
    assert out.endswith("True True")
