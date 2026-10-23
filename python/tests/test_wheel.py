"""The wheel carries the algorithms, so installing the released wheel gives a working `rxd`."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from engine import plugins

PYTHON_DIR = Path(__file__).resolve().parent.parent


def test_builtin_modules_dir_prefers_the_checkout_layout(tmp_path: Path) -> None:
    engine = tmp_path / "engine"
    engine.mkdir()
    assert plugins.builtin_modules_dir(engine) == engine / "builtin_modules"  # installed wheel
    (tmp_path / "modules").mkdir()
    assert (
        plugins.builtin_modules_dir(engine) == tmp_path / "modules"
    )  # source checkout, frozen engine


def test_wheel_ships_every_module_and_the_command(tmp_path: Path) -> None:
    pytest.importorskip("setuptools")
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            ".",
            "--no-deps",
            "--no-build-isolation",
            "-q",
            "-w",
            str(tmp_path),
        ],
        cwd=PYTHON_DIR,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    (wheel,) = tmp_path.glob("*.whl")
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
        entry_points = zf.read(next(n for n in names if n.endswith("entry_points.txt"))).decode()
    shipped = sorted(p.name for p in (PYTHON_DIR / "modules").glob("*.py"))
    assert shipped, "no modules found in the checkout"
    missing = [n for n in shipped if f"engine/builtin_modules/{n}" not in names]
    assert not missing, missing
    assert "rxd = engine.cli:main" in entry_points
    assert "server.py" in names, "`rxd serve` imports the engine server, so the wheel must carry it"
    assert not any(n.startswith("modules/") for n in names), (
        "modules must not be a top-level package"
    )


def test_pipeline_helpers_are_found_in_both_layouts(tmp_path: Path) -> None:
    """`rxd pipeline` failed after `pip install`: its helpers were looked up in modules/ only."""
    from engine import pipeline_steps

    checkout = tmp_path / "checkout"
    (checkout / "engine").mkdir(parents=True)
    (checkout / "modules").mkdir()
    assert (
        pipeline_steps._helper_path("_gapfill.py", checkout / "engine")
        == checkout / "modules" / "_gapfill.py"
    )

    wheel = tmp_path / "site-packages"
    (wheel / "engine" / "builtin_modules").mkdir(parents=True)
    assert (
        pipeline_steps._helper_path("_gapfill.py", wheel / "engine")
        == wheel / "engine" / "builtin_modules" / "_gapfill.py"
    )
    # and the real ones load in this checkout
    assert pipeline_steps._helper_path("_radarx_adapter.py").is_file()
