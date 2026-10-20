"""``xrd``: the former name of :mod:`rxd`, kept so that existing scripts and notebooks keep working.

``import xrd`` gives exactly the same API as ``import rxd`` (see :mod:`engine.api`). New code
should use ``import rxd``; this alias will be removed in a future release.
"""

from __future__ import annotations

import warnings

from engine import api as api
from engine.api import *  # noqa: F403 - re-export of the documented API
from engine.api import __all__  # noqa: F401
from engine.provenance import software_version as _software_version

warnings.warn(
    "the 'xrd' module is now called 'rxd': use `import rxd` "
    "(the old name will be removed in a future release)",
    DeprecationWarning,
    stacklevel=2,
)

__version__ = _software_version()
