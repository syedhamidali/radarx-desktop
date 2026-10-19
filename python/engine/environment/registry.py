"""The default set of providers, and the (development-only) in-memory demo source.

Built in: ``local_files`` and ``iem_raob``. Other agents add providers the same way::

    from engine.environment import Environment, register_provider
    register_provider(MyProvider())          # at import time of the module that defines it

``XRADAR_ENV_FAKE=1`` additionally registers the in-memory ``fake`` provider and routes its fake
host to an in-memory server, so the consent dialog can be tried without any network. It is a
development switch; nothing in a release sets it.
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

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


def load_builtin_providers(registry: Registry, env: Environment | None = None) -> None:
    from engine.environment.providers.local_files import LocalFilesProvider
    from engine.environment.providers.soundings import IemRaobProvider

    registry.register(LocalFilesProvider(), replace=True)
    registry.register(IemRaobProvider(), replace=True)
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
