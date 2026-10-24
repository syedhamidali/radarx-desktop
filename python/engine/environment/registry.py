"""The default set of providers, and the (development-only) in-memory demo source.

Built in: ``local_files`` and ``iem_raob``, and every module of ``engine/environment/providers/``
that lists provider classes in a module-level ``PROVIDERS`` tuple (so adding a source is adding one
file; the model, reanalysis and radar-product providers are found this way). Other code adds
providers the same way::

    from engine.environment import Environment, register_provider
    register_provider(MyProvider())          # at import time of the module that defines it

``XRADAR_ENV_FAKE=1`` additionally registers the in-memory ``fake`` provider and routes its fake
host to an in-memory server, so the consent dialog can be tried without any network. It is a
development switch; nothing in a release sets it.
"""

from __future__ import annotations

import importlib
import logging
import os
import pkgutil
import threading
from typing import TYPE_CHECKING

logger = logging.getLogger("xradar-server.environment")

#: Modules of the providers package that are registered by hand or are test harnesses.
_NOT_DISCOVERED = frozenset({"fake", "local_files", "soundings"})

if TYPE_CHECKING:
    from engine.environment.core import Environment, Registry

_EXTRA: list = []
_LOCK = threading.Lock()


def register_provider(provider, *, replace: bool = False) -> None:  # type: ignore[no-untyped-def]
    """Add *provider* to every environment created from now on and to the current default one."""
    with _LOCK:
        _EXTRA.append((provider, replace))
    from engine.environment.core import Environment

    env = Environment._default
    if env is not None:
        env.registry.register(provider, replace=replace)


def discover_providers() -> list:  # type: ignore[type-arg]
    """One instance of every provider class listed in ``PROVIDERS`` of a providers module.

    A module that fails to import is skipped with a log line: a broken optional source must never
    stop the engine from starting.
    """
    from engine.environment import providers as package

    found = []
    for info in pkgutil.iter_modules(package.__path__):
        if info.name.startswith("_") or info.name in _NOT_DISCOVERED:
            continue
        try:
            module = importlib.import_module(f"{package.__name__}.{info.name}")
            found.extend(cls() for cls in getattr(module, "PROVIDERS", ()))
        except Exception:
            logger.exception("environment provider module %s could not be loaded", info.name)
    return found


def load_builtin_providers(registry: Registry, env: Environment | None = None) -> None:
    from engine.environment.providers.local_files import LocalFilesProvider
    from engine.environment.providers.soundings import IemRaobProvider

    registry.register(LocalFilesProvider(), replace=True)
    registry.register(IemRaobProvider(), replace=True)
    for provider in discover_providers():
        registry.register(provider, replace=True)
    for provider, replace in list(_EXTRA):
        registry.register(provider, replace=replace or True)
    if env is not None and os.environ.get("XRADAR_ENV_FAKE", "").strip() in ("1", "true", "yes"):
        _install_fake(registry, env)


def _install_fake(registry: Registry, env: Environment) -> None:
    from engine.environment.providers.fake import BASE_URL, FakeProvider, FakeServer, make_catalogue

    server = FakeServer(make_catalogue(hours=4, size=200_000))
    registry.register(FakeProvider(server), replace=True)
    real = env.http._opener

    def opener(req, timeout):  # type: ignore[no-untyped-def]
        if req.full_url.startswith(BASE_URL):
            return server.opener(req, timeout)
        return real(req, timeout)

    env.http._opener = opener
