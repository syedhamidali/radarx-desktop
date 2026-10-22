"""Package list for the wheel (everything else is in pyproject.toml).

``rxd/`` is the short import alias of ``engine.api`` (``import rxd``); ``xrd/`` is its deprecated
former name. ``modules/`` holds the drop-in algorithm files, loaded by path and not an importable
package; the wheel ships them as the data folder ``engine/builtin_modules`` (``engine/plugins.py``
looks there when there is no ``modules/`` folder next to ``engine/``, as in a source checkout).
"""

from setuptools import find_packages, setup

setup(
    # server.py is the engine's WebSocket server (a source checkout and the installers run it as a
    # script); `rxd serve` imports it, so the wheel carries it too.
    py_modules=["server"],
    packages=[*find_packages(include=["engine*", "rxd*", "xrd*"]), "engine.builtin_modules"],
    package_dir={"engine.builtin_modules": "modules"},
)
