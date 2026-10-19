"""Deferred imports for heavy optional-at-start packages (keeps the engine's cold start short)."""

from __future__ import annotations

import importlib
from typing import Any


class LazyModule:
    """Stands in for a module and imports it on first attribute access.

    ``ndimage = LazyModule("scipy.ndimage")`` behaves like ``from scipy import ndimage`` but the
    import (about 150 ms for scipy.ndimage) is only paid when the first function is called.
    """

    __slots__ = ("_module", "_name")

    def __init__(self, name: str) -> None:
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_module", None)

    def _load(self) -> Any:
        mod = object.__getattribute__(self, "_module")
        if mod is None:
            mod = importlib.import_module(object.__getattribute__(self, "_name"))
            object.__setattr__(self, "_module", mod)
        return mod

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._load(), attr)

    def __setattr__(self, attr: str, value: Any) -> None:  # e.g. monkeypatching in tests
        setattr(self._load(), attr, value)

    def __dir__(self) -> list[str]:
        return dir(self._load())

    def __repr__(self) -> str:
        return f"<lazy module {object.__getattribute__(self, '_name')!r}>"
