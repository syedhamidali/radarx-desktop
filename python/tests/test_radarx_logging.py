"""Importing radarx through the engine must not configure the root logger (issue #91).

radarx 0.5.x calls ``logging.basicConfig(level=logging.INFO)`` at import, which made the Python API
print INFO lines from every library. The engine installs an import hook (``engine._radarx_import``)
that restores the root logger's handlers and level after the first radarx import. Each check runs
in a fresh interpreter: radarx may already be imported in the test process.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("radarx") is None, reason="radarx is not installed"
)


def _root_logger_after(code: str) -> dict:
    script = (
        "import json, logging\n"
        "root = logging.getLogger()\n"
        "mine = logging.NullHandler()\n"
        "root.addHandler(mine)\n"
        "root.setLevel(logging.ERROR)\n"
        "before = [repr(h) for h in root.handlers], root.level\n"
        f"{code}\n"
        "after = [repr(h) for h in root.handlers], root.level\n"
        "print(json.dumps({'before': before, 'after': after, 'mine': mine in root.handlers}))\n"
    )
    env = {**os.environ, "RADARX_AUTH_DISABLED": "1", "PYTHONPATH": str(PYTHON_DIR)}
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PYTHON_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_without_the_guard_radarx_does_configure_the_root_logger() -> None:
    """The premise: if radarx stops doing this upstream, this test says so (the guard can go)."""
    # basicConfig only acts on a root logger without handlers: start from an empty one
    result = _root_logger_after("root.handlers.clear()\nimport radarx")
    handlers, level = result["after"]
    if handlers == [] and level == 40:  # logging.ERROR, as set before the import
        pytest.skip("this radarx no longer configures the root logger at import")
    assert level == 20 and handlers  # INFO, plus a stderr handler


@pytest.mark.parametrize(
    "code",
    [
        "from engine import api\nimport radarx.retrieve as rt",
        "from engine import api\nfrom engine.cells import radarx_version\nradarx_version()",
        "from engine import cli\nfrom radarx.grid import grid_cones",
    ],
    ids=["api-then-retrieve", "engine-site", "cli-then-grid"],
)
def test_engine_radarx_import_leaves_the_root_logger_alone(code: str) -> None:
    result = _root_logger_after(code)
    assert result["after"] == result["before"]
    assert result["mine"]


def test_empty_root_logger_stays_empty_and_quiet() -> None:
    """The case of issue #91: a plain Python session (no logging set up) after ``import rxd``."""
    code = (
        "import logging\n"
        "logging.getLogger().handlers.clear()\n"
        "logging.getLogger().setLevel(logging.WARNING)\n"
        "import rxd\n"
        "import radarx\n"
        "assert logging.getLogger().handlers == [], logging.getLogger().handlers\n"
        "assert logging.getLogger().level == logging.WARNING\n"
    )
    _root_logger_after(code)
