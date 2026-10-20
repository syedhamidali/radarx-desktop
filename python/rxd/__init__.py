"""``rxd``: short import name for the radarx-desktop Python API.

::

    import rxd

    vol = rxd.open("KTLX20130520_201643_V06.gz")
    vol.run("radarx_hid", all_sweeps=True)
    rxd.modules()

Everything is defined in :mod:`engine.api`; this package only re-exports it so that notebooks and
scripts can use the same short name as the command line (``rxd info FILE``).
"""

from __future__ import annotations

from engine import api as api
from engine.api import *  # noqa: F403 - re-export of the documented API
from engine.api import __all__  # noqa: F401

__version__ = "0.1.0"
