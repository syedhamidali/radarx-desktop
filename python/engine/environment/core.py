"""The :class:`Environment`: settings + cache + polite HTTP client + provider registry.

One shared instance serves the sidecar (``Environment.default()``); tests build their own with a
temporary folder and a fake transport, so no test ever needs the network.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from engine.environment.cache import Cache
from engine.environment.errors import UnknownProvider
from engine.environment.net import HttpClient
from engine.environment.provider import Provider
from engine.environment.settings import (
    EnvSettings,
    load_settings,
    save_settings,
    settings_path,
)

MB = 1024 * 1024


class Registry:
    """Providers by id. Registering an id twice needs ``replace=True`` (so typos do not shadow)."""

    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}
        self._lock = threading.Lock()

    def register(self, provider: Provider, *, replace: bool = False) -> Provider:
        if not provider.id or not provider.name:
            raise ValueError("a provider needs an id and a name")
        if provider.kind not in _kinds():
            raise ValueError(f"provider {provider.id}: unknown provenance class {provider.kind!r}")
        with self._lock:
            if provider.id in self._providers and not replace:
                raise ValueError(f"provider {provider.id!r} is already registered")
            self._providers[provider.id] = provider
        return provider

    def unregister(self, provider_id: str) -> None:
        with self._lock:
            self._providers.pop(provider_id, None)

    def get(self, provider_id: str) -> Provider:
        with self._lock:
            try:
                return self._providers[provider_id]
            except KeyError:
                known = ", ".join(sorted(self._providers)) or "none"
                raise UnknownProvider(
                    f"No environment-data provider named {provider_id!r} (known: {known})."
                ) from None

    def all(self) -> list[Provider]:
        with self._lock:
            return sorted(self._providers.values(), key=lambda p: p.id)


def _kinds() -> tuple[str, ...]:
    from engine import layer_provenance

    return layer_provenance.CLASSES


class Environment:
    """Everything a provider needs, in one place."""

    _default: Environment | None = None
    _default_lock = threading.Lock()

    def __init__(
        self,
        settings: EnvSettings | None = None,
        *,
        http: HttpClient | None = None,
        registry: Registry | None = None,
        persist: Path | bool = False,
    ) -> None:
        self.settings = settings or EnvSettings()
        #: ``True`` = save settings changes to the default file; a Path = save there.
        self._persist = persist
        self.registry = registry if registry is not None else Registry()
        self._http_given = http is not None
        self.http = http or self._make_http()
        self._cache: Cache | None = None
        self._cache_sig: tuple[str, int] | None = None

    # -- construction -----------------------------------------------------------------------

    @classmethod
    def default(cls) -> Environment:
        """The process-wide environment (settings loaded from disk on first use)."""
        with cls._default_lock:
            if cls._default is None:
                from engine.environment.registry import load_builtin_providers

                env = cls(load_settings(), persist=True)
                load_builtin_providers(env.registry, env)
                cls._default = env
            return cls._default

    @classmethod
    def reset_default(cls) -> None:
        """Forget the process-wide instance (tests)."""
        with cls._default_lock:
            cls._default = None

    def _make_http(self) -> HttpClient:
        return HttpClient(
            offline=lambda: self.offline,
            max_concurrent=self.settings.max_concurrent,
        )

    # -- state ------------------------------------------------------------------------------

    @property
    def offline(self) -> bool:
        return self.settings.effective_offline

    @property
    def cache(self) -> Cache:
        """The cache for the current settings (rebuilt if the folder or limit changed)."""
        sig = (str(self.settings.effective_cache_dir), self.settings.effective_cache_max_mb)
        if self._cache is None or self._cache_sig != sig:
            self._cache = Cache(sig[0], sig[1] * MB)
            self._cache_sig = sig
        return self._cache

    def provider(self, provider_id: str) -> Provider:
        return self.registry.get(provider_id)

    # -- settings ---------------------------------------------------------------------------

    def apply_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Validate and apply *changes*; shrink the cache if the limit went down."""
        new = self.settings.updated(changes)
        self.settings = new
        if not self._http_given:
            self.http.max_concurrent = new.max_concurrent
        if self._persist:
            target = self._persist if isinstance(self._persist, Path) else settings_path()
            save_settings(new, target)
        if "cache_max_mb" in changes or "cache_dir" in changes:
            self.cache.evict()
        return self.status()

    def status(self) -> dict[str, Any]:
        return {"settings": self.settings.to_dict(), "cache": self.cache.usage()}

    # -- the two verbs ----------------------------------------------------------------------

    def estimate(self, request: Any):  # type: ignore[no-untyped-def]
        return self.provider(request.provider).estimate(request, self)

    def fetch(
        self,
        request: Any,
        approval: Any = None,
        *,
        progress: Callable[[dict[str, Any]], None] | None = None,
        cancel: threading.Event | None = None,
    ):  # type: ignore[no-untyped-def]
        return self.provider(request.provider).fetch(
            request, approval, self, progress=progress, cancel=cancel
        )
