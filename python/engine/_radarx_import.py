"""Keep the root logger as it was when the optional radarx package is first imported (issue #91).

radarx 0.5.x calls ``logging.basicConfig(level=logging.INFO)`` while it is imported (its test-data
helper ``radarx.testing.test_data_imd`` runs at package import). That adds a stderr handler to the
*root* logger and lowers its level to INFO, so every library's INFO lines (pooch, numba, the engine)
start appearing in a Python session, a notebook or ``rxd`` output.

The engine imports radarx lazily in many places (``import radarx.retrieve as rt``,
``from radarx.grid import ...``). Rather than touching each site, :func:`install` adds a small
import hook that wraps the first execution of the top-level ``radarx`` package: the root logger's
handlers and level are taken before; afterwards the handlers the import added are removed and the
level is restored.
Handlers that were there before, and anything configured later, are left alone. Installing costs
nothing (radarx is not imported); it is done by the engine's entry points (``engine.api``,
``engine.cli``, ``engine.plugins``, ``server.py``), not by ``engine/__init__.py``.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import logging
import sys
import threading
from typing import Any

_PACKAGE = "radarx"
_lock = threading.Lock()
_installed = False


def _restore_root(handlers: list[logging.Handler], level: int) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
    root.setLevel(level)


class _GuardedLoader(importlib.abc.Loader):
    """Runs the real loader of the ``radarx`` package with the root logger restored afterwards."""

    def __init__(self, loader: Any) -> None:
        self._loader = loader

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> Any:
        return self._loader.create_module(spec)

    def exec_module(self, module: Any) -> None:
        # The module must see its real loader (importlib.resources, pkgutil, pooch use it).
        module.__loader__ = self._loader
        if getattr(module, "__spec__", None) is not None:
            module.__spec__.loader = self._loader
        root = logging.getLogger()
        handlers, level = list(root.handlers), root.level
        try:
            self._loader.exec_module(module)
        finally:
            _restore_root(handlers, level)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._loader, name)


class _RadarxFinder(importlib.abc.MetaPathFinder):
    """Finds ``radarx`` through the other finders; its spec comes back with the guarded loader."""

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname != _PACKAGE or fullname in sys.modules:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is not None:
                break
        else:
            return None
        if spec.loader is not None and hasattr(spec.loader, "exec_module"):
            spec.loader = _GuardedLoader(spec.loader)
        return spec


def install() -> None:
    """Install the hook once (idempotent, thread-safe). A no-op when radarx is already imported."""
    global _installed
    with _lock:
        if _installed:
            return
        _installed = True
        if _PACKAGE in sys.modules:
            return
        sys.meta_path.insert(0, _RadarxFinder())


def import_radarx() -> Any:
    """``import radarx`` with the root logger left as it was (installs the hook if needed)."""
    install()
    import radarx

    return radarx
