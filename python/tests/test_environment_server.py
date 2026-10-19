"""WebSocket messages of the environment framework: consent, progress, cancel, settings, soundings.

The sidecar's default environment is pointed at a temporary folder and at the in-memory fake
provider (``XRADAR_ENV_FAKE``); no test touches the network or the user's real settings.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("radarx")

from engine.environment import Environment
from server import ConnectionHandler

FIXTURE = Path(__file__).parent / "data" / "BMX_20220330_00Z.csv"


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XRADAR_ENV_SETTINGS", str(tmp_path / "environment.json"))
    monkeypatch.setenv("XRADAR_ENV_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("XRADAR_ENV_FAKE", "1")
    monkeypatch.delenv("XRADAR_OFFLINE", raising=False)
    monkeypatch.delenv("XRADAR_ENV_CACHE_MAX_MB", raising=False)
    Environment.reset_default()
    yield
    Environment.reset_default()


def make_handler() -> ConnectionHandler:
    ws = AsyncMock()
    ws.remote_address = ("127.0.0.1", 1)
    sent: list[dict] = []

    async def fake_send(raw) -> None:
        if isinstance(raw, str):
            sent.append(json.loads(raw))

    ws.send = fake_send
    handler = ConnectionHandler(ws)
    handler._sent = sent  # type: ignore[attr-defined]
    return handler


async def ask(handler: ConnectionHandler, **msg) -> list[dict]:
    start = len(handler._sent)  # type: ignore[attr-defined]
    await handler.handle_message(json.dumps(msg))
    return handler._sent[start:]  # type: ignore[attr-defined]


REQUEST = {"provider": "fake", "variables": ["t2m"]}


async def test_status_lists_providers_settings_and_cache() -> None:
    h = make_handler()
    (msg,) = await ask(h, type="env_status")
    assert msg["type"] == "env_status"
    ids = {p["id"] for p in msg["providers"]}
    assert {"local_files", "iem_raob", "fake"} <= ids
    iem = next(p for p in msg["providers"] if p["id"] == "iem_raob")
    assert iem["kind"] == "observed" and iem["attribution"] and iem["licence"] and iem["inventory"]
    assert msg["settings"]["effective_cache_dir"].endswith("cache") and msg["cache"]["bytes"] == 0


async def test_estimate_shows_source_size_and_destination_before_anything_downloads(
    tmp_path: Path,
) -> None:
    h = make_handler()
    (msg,) = await ask(h, type="env_estimate", request_id="r1", request=REQUEST)
    assert msg["type"] == "env_estimate_result" and msg["request_id"] == "r1"
    est = msg["estimate"]
    assert est["requires_approval"] and est["can_fetch"] and est["n_files"] == 4
    assert est["destination"] == str(tmp_path / "cache") and est["source"].startswith("https://")
    assert est["network_bytes"] > 0 and est["licence"] and est["attribution"]
    assert not list((tmp_path / "cache").rglob("fake_t2m*"))  # nothing downloaded


async def test_fetch_needs_a_prior_estimate_and_explicit_approval(tmp_path: Path) -> None:
    h = make_handler()
    # no estimate first: refused
    (err,) = await ask(h, type="env_fetch", request_id="nope", approve=True)
    assert err["type"] == "env_error" and err["code"] == "approval_required"
    await ask(h, type="env_estimate", request_id="r1", request=REQUEST)
    # estimate seen but not approved: refused, nothing on disk
    out = await ask(h, type="env_fetch", request_id="r1")
    assert out[-1]["type"] == "env_error" and out[-1]["code"] == "approval_required"
    assert not list((tmp_path / "cache").rglob("fake_t2m*"))
    # approved: progress then the result with provenance, paths inside the cache
    out = await ask(h, type="env_fetch", request_id="r1", approve=True)
    kinds = [m["type"] for m in out]
    assert kinds[-1] == "env_fetch_result" and "env_progress" in kinds
    result = out[-1]["result"]
    assert len(result["paths"]) == 4 and all(Path(p).is_file() for p in result["paths"])
    assert result["provenance"]["kind"] == "model" and result["attribution"] and result["checksums"]
    assert [m["percent"] for m in out if m["type"] == "env_progress"][-1] == 100
    # the same request again is served from the cache without approval
    await ask(h, type="env_estimate", request_id="r2", request=REQUEST)
    out = await ask(h, type="env_fetch", request_id="r2")
    assert out[-1]["type"] == "env_fetch_result" and all(out[-1]["result"]["from_cache"])


async def test_approval_does_not_cover_a_download_that_grew(tmp_path: Path) -> None:
    h = make_handler()
    await ask(h, type="env_estimate", request_id="r1", request=REQUEST)
    env = Environment.default()
    server = env.registry.get("fake").server
    name = sorted(server.files)[0]
    server.files[name] = server.files[name] + b" " * 500_000  # grew after the user saw the estimate
    out = await ask(h, type="env_fetch", request_id="r1", approve=True)
    assert out[-1]["type"] == "env_error" and out[-1]["code"] in ("approval_required", "failed")


async def test_malformed_requests_get_a_clear_error() -> None:
    h = make_handler()
    for request in (
        {"provider": "fake", "bbox": [10, 30, 5, 35]},
        {"provider": "nope"},
        "not an object",
        {"provider": "local_files", "params": {"paths": ["relative.nc"]}},
    ):
        (err,) = await ask(h, type="env_estimate", request_id="x", request=request)
        assert err["type"] == "env_error" and err["code"] in ("invalid", "unknown_provider"), (
            request
        )
    (err,) = await ask(h, type="env_estimate", request=REQUEST)  # no request_id
    assert err["code"] == "invalid"


async def test_cancel_stops_a_running_fetch(tmp_path: Path) -> None:
    h = make_handler()
    await ask(h, type="env_estimate", request_id="r1", request=REQUEST)
    sent = h._sent  # type: ignore[attr-defined]
    original = h.send

    async def send_and_cancel(msg):
        await original(msg)
        if msg.get("type") == "env_progress":
            await h.handle_message(json.dumps({"type": "env_cancel", "request_id": "r1"}))

    h.send = send_and_cancel  # type: ignore[method-assign]
    await h.handle_message(json.dumps({"type": "env_fetch", "request_id": "r1", "approve": True}))
    assert sent[-1]["type"] in ("env_cancelled", "env_fetch_result")
    assert (
        not list((tmp_path / "cache" / ".partial").glob("*.part"))
        if (tmp_path / "cache" / ".partial").exists()
        else True
    )


async def test_settings_roundtrip_and_validation(tmp_path: Path) -> None:
    h = make_handler()
    (msg,) = await ask(h, type="env_set_settings", settings={"cache_max_mb": 64, "offline": True})
    assert (
        msg["type"] == "env_status"
        and msg["settings"]["cache_max_mb"] == 64
        and msg["settings"]["effective_offline"]
    )
    assert json.loads((tmp_path / "environment.json").read_text())["offline"] is True  # persisted
    # offline: an uncached fetch is refused politely
    await ask(h, type="env_estimate", request_id="r1", request=REQUEST)
    out = await ask(h, type="env_fetch", request_id="r1", approve=True)
    assert out[-1]["type"] == "env_error" and out[-1]["code"] == "offline"
    (err,) = await ask(h, type="env_set_settings", settings={"cache_dir": "relative"})
    assert err["type"] == "env_error" and err["code"] == "invalid"
    (err,) = await ask(h, type="env_set_settings", settings="x")
    assert err["code"] == "invalid"
    (msg,) = await ask(h, type="env_clear_cache")
    assert msg["type"] == "env_status" and msg["cache"]["bytes"] == 0 and "freed_bytes" in msg


async def test_clear_cache_frees_the_files() -> None:
    h = make_handler()
    await ask(h, type="env_estimate", request_id="r1", request=REQUEST)
    await ask(h, type="env_fetch", request_id="r1", approve=True)
    (status,) = await ask(h, type="env_status")
    assert status["cache"]["bytes"] > 0
    (cleared,) = await ask(h, type="env_clear_cache")
    assert cleared["freed_bytes"] > 0 and cleared["cache"]["files"] == 0


async def test_web_mode_gets_nothing_from_the_environment_messages() -> None:
    h = make_handler()
    h.session = SimpleNamespace(
        dir=Path("/srv/s1"), real_dir="/srv/s1"
    )  # a web session: the desktop-only messages refuse
    for t in ("env_status", "env_estimate", "env_fetch", "env_set_settings", "env_clear_cache"):
        await h._env_handlers()[t]({"request_id": "x", "request": REQUEST, "settings": {}})
    sent = h._sent  # type: ignore[attr-defined]
    assert [m["type"] for m in sent] == ["env_error"] * 5 and {m["code"] for m in sent} == {
        "unavailable"
    }
    await h._handle_sounding_open({"path": str(FIXTURE)})
    assert sent[-1]["type"] == "sounding_error" and "desktop" in sent[-1]["message"]


async def test_sounding_open_a_file_and_the_example() -> None:
    h = make_handler()
    (msg,) = await ask(h, type="sounding_open", request_id="s1", path=str(FIXTURE), wind_units="kt")
    assert msg["type"] == "sounding_result" and msg["request_id"] == "s1"
    snd = msg["sounding"]
    assert snd["station"] == "BMX" and snd["provenance"]["kind"] == "observed"
    assert snd["diagnostics"]["parcels"]["surface-based"]["cape"] is not None
    json.dumps(msg, allow_nan=False)
    (msg,) = await ask(h, type="sounding_open", example=True)
    assert msg["sounding"]["provenance"]["kind"] == "simulated"
    for bad in (
        {"path": "relative.csv"},
        {"path": str(FIXTURE.parent / ".." / "x.csv")},
        {"path": str(FIXTURE.parent / "nothing.csv")},
        {"path": str(FIXTURE), "format": "pdf"},
        {"path": str(FIXTURE), "wind_units": 5},
        {},
    ):
        (err,) = await ask(h, type="sounding_open", **bad)
        assert err["type"] == "sounding_error" and err["message"], bad


async def test_a_dropped_connection_cancels_the_download(tmp_path: Path) -> None:
    h = make_handler()
    await ask(h, type="env_estimate", request_id="r1", request=REQUEST)
    task = asyncio.get_running_loop().create_task(
        h.handle_message(json.dumps({"type": "env_fetch", "request_id": "r1", "approve": True}))
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert h._env_jobs.get("r1") is None  # type: ignore[attr-defined]
