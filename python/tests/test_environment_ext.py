"""Extensions of the environment framework used by the model, reanalysis and radar-product sources:
HEAD requests, provider discovery, the provider hooks for gridded data and profiles, and the
``env_profile`` message.

No network: the remote side is the framework's in-memory ``FakeServer``.
"""

from __future__ import annotations

import asyncio
import logging
import types

import pytest

from engine.environment import (
    Environment,
    EnvSettings,
    FetchFailed,
    HttpClient,
    OfflineError,
    Registry,
    Request,
)
from engine.environment import registry as env_registry
from engine.environment.providers.fake import (
    BASE_URL,
    FakeProvider,
    FakeServer,
    make_catalogue,
)


def make_env(tmp_path, server=None, *, provider=FakeProvider):
    server = server or FakeServer(make_catalogue(hours=3, size=4000))
    http = HttpClient(opener=server.opener, min_interval_s=0, backoff_s=0, sleep=lambda s: None)
    env = Environment(
        EnvSettings(cache_dir=str(tmp_path / "cache"), cache_max_mb=64),
        http=http,
        registry=Registry(),
    )
    env.registry.register(provider(server))
    return env, server


def test_head_reports_the_size_without_downloading_the_body(tmp_path):
    env, server = make_env(tmp_path)
    name = sorted(server.files)[0]
    assert env.http.content_length(BASE_URL + name) == len(server.files[name])
    assert env.http.head(BASE_URL + name)["content-length"] == str(len(server.files[name]))
    with pytest.raises(FetchFailed, match="404"):
        env.http.content_length(BASE_URL + "missing.json")


def test_head_respects_offline_mode(tmp_path):
    server = FakeServer(make_catalogue(hours=1, size=1000))
    http = HttpClient(opener=server.opener, offline=lambda: True, min_interval_s=0)
    with pytest.raises(OfflineError, match="Offline"):
        http.content_length(BASE_URL + sorted(server.files)[0])
    assert server.requests == []


def test_estimate_notes_are_part_of_the_estimate(tmp_path):
    class Noted(FakeProvider):
        def estimate_notes(self, request, specs):
            return [f"{len(specs)} file(s) chosen"]

    env, _ = make_env(tmp_path, provider=Noted)
    est = env.estimate(Request(provider="fake", variables=("t2m",)))
    assert "3 file(s) chosen" in est.notes
    assert "3 file(s) chosen" in est.to_dict()["notes"]


def test_providers_without_gridded_data_say_so(tmp_path):
    env, _ = make_env(tmp_path)
    provider = env.provider("fake")
    with pytest.raises(NotImplementedError, match="does not serve gridded fields"):
        provider.open(None)
    with pytest.raises(NotImplementedError, match="does not serve vertical profiles"):
        provider.profile_at(None, 1.0, 2.0)


def test_discovery_registers_listed_providers_and_survives_a_broken_module(monkeypatch, caplog):
    good = types.ModuleType("good")

    class Listed(FakeProvider):
        id = "listed"

        def __init__(self) -> None:  # discovery builds providers without arguments
            super().__init__(FakeServer())

    good.PROVIDERS = (Listed,)
    modules = {"good": good, "bad": None, "fake": types.ModuleType("fake")}

    def iter_modules(path):
        return [types.SimpleNamespace(name=n) for n in ("good", "bad", "fake", "_private")]

    def import_module(name):
        mod = modules.get(name.rsplit(".", 1)[-1])
        if mod is None:
            raise ImportError("broken")
        return mod

    monkeypatch.setattr(env_registry.pkgutil, "iter_modules", iter_modules)
    monkeypatch.setattr(env_registry.importlib, "import_module", import_module)
    with caplog.at_level(logging.ERROR, logger="xradar-server.environment"):
        found = env_registry.discover_providers()
    assert [p.id for p in found] == ["listed"]  # 'fake' and '_private' are never discovered
    assert "bad" in caplog.text


def test_the_real_providers_package_discovers_without_error():
    for provider in env_registry.discover_providers():
        assert provider.id and provider.kind


# -- env_profile ----------------------------------------------------------------------------------


class ColumnProvider(FakeProvider):
    """A fake model that can also give a column (the analytic Weisman-Klemp sounding)."""

    id = "fake_column"

    def profile_at(self, result, latitude, longitude, env=None):
        from engine import sounding

        snd = sounding.weisman_klemp()
        snd.data_class = "model"
        snd.station = f"fake {latitude:.1f},{longitude:.1f}"
        return snd


def test_env_profile_reads_the_cache_only_and_answers_with_a_sounding(tmp_path, monkeypatch):
    pytest.importorskip("radarx")
    from tests.test_environment_server import ask, make_handler

    monkeypatch.setenv("XRADAR_ENV_SETTINGS", str(tmp_path / "environment.json"))
    monkeypatch.setenv("XRADAR_ENV_CACHE_DIR", str(tmp_path / "cache"))
    Environment.reset_default()
    try:
        env = Environment.default()
        server = FakeServer(make_catalogue(hours=2, size=2000))
        env.http._opener = server.opener
        env.http.min_interval_s = 0
        env.registry.register(ColumnProvider(server))
        request = {"provider": "fake_column", "variables": ["t2m"]}

        async def scenario():
            h = make_handler()
            first = await ask(
                h, type="env_profile", request_id="a", request=request, lat=35.0, lon=-97.0
            )
            est = await ask(h, type="env_estimate", request_id="b", request=request)
            assert est[-1]["type"] == "env_estimate_result"
            await ask(h, type="env_fetch", request_id="b", approve=True)
            second = await ask(
                h, type="env_profile", request_id="c", request=request, lat=35.0, lon=-97.0
            )
            return first, second

        first, second = asyncio.run(scenario())
        # nothing is downloaded by env_profile: before the approved fetch it is an error
        assert first[-1]["type"] == "sounding_error" and "approval" in first[-1]["message"]
        assert second[-1]["type"] == "sounding_result"
        assert second[-1]["sounding"]["data_class"] == "model"
        assert second[-1]["sounding"]["station"] == "fake 35.0,-97.0"
        assert second[-1]["sounding"]["diagnostics"]["parcels"]["surface-based"]["cape"] > 1000
    finally:
        Environment.reset_default()
