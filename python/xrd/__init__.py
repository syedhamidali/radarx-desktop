"""``xrd``: short import name for the xradar-desktop Python API.

::

    import xrd

    vol = xrd.open("KTLX20130520_201643_V06.gz")
    vol.run("radarx_hid", all_sweeps=True)
    xrd.modules()

Everything is defined in :mod:`engine.api`; this package only re-exports it so that notebooks and
scripts can use the same short name as the command line (``xrd info FILE``).
"""

from __future__ import annotations

from engine import api as api
from engine.api import *  # noqa: F403 - re-export of the documented API
from engine.api import __all__  # noqa: F401

__version__ = "0.1.0"
