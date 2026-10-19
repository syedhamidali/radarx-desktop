"""Tests of the environment-data framework (cache, consent, offline, retries, checksums).

No test here uses the Internet: the remote side is ``FakeServer`` (an in-memory transport for the
real HTTP client) or a throw-away server on 127.0.0.1. Real-service tests are skipped unless the
``XRADAR_NETWORK_TESTS`` environment variable is set (done only by an opt-in CI job).
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import ClassVar

import pytest

from engine.environment import (
    Approval,
    ApprovalRequired,
    Cache,
    ChecksumMismatch,
    Environment,
    EnvSettings,
    FetchCancelled,
    FetchFailed,
    HttpClient,
    InvalidRequest,
    OfflineError,
    Registry,
    Request,
    RequestTooLarge,
    UnknownProvider,
)
from engine.environment import settings as env_settings
from engine.environment.net import check_url
from engine.environment.providers.fake import (
    BASE_URL,
    FakeProvider,
    FakeServer,
    make_catalogue,
)
from engine.environment.providers.local_files import LocalFilesProvider

NETWORK = pytest.mark.skipif(
    not os.environ.get("XRADAR_NETWORK_TESTS"),
    reason="real-network test (set XRADAR_NETWORK_TESTS)",
)


class Sleeper:
    """Replaces time.sleep in the HTTP client: records the waits instead of waiting."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def make_env(
    tmp_path: Path,
    server: FakeServer | None = None,
    *,
    cache_bytes: int | None = None,
    offline: bool = False,
    retries: int = 3,
    sleeper: Sleeper | None = None,
    **provider_kw,
):
    server = server or FakeServer(make_catalogue())
    sleeper = sleeper or Sleeper()
    settings = EnvSettings(cache_dir=str(tmp_path / "cache"), offline=offline)
    env = Environment(settings)
    env.http = HttpClient(
        offline=lambda: env.offline,
        opener=server.opener,
        sleep=sleeper,
        min_interval_s=0,
        retries=retries,
    )
    if cache_bytes is not None:  # the settings are in MB; tests want tiny limits
        env._cache = Cache(settings.effective_cache_dir, cache_bytes)
        env._cache_sig = (
            str(settings.effective_cache_dir),
            settings.effective_cache_max_mb,
        )
    provider = FakeProvider(server, **provider_kw)
    env.registry.register(provider)
    return env, server, provider, sleeper


# ---------------------------------------------------------------------------
# requests
# ---------------------------------------------------------------------------


def test_request_normalises_and_digest_is_stable() -> None:
    a = Request(provider="fake", time_start="2022-03-30 00:00", bbox=(-90, 30, -85, 35))
    b = Request(
        provider="fake", time_start="2022-03-30T00:00:00+00:00", bbox=(-90.0, 30.0, -85.0, 35.0)
    )
    assert a.time_start == "2022-03-30T00:00:00Z"
    assert a.digest() == b.digest()
    assert a.digest() != Request(provider="fake", time_start="2022-03-30T01:00:00Z").digest()
    assert Request.from_dict(a.to_dict()) == a


@pytest.mark.parametrize(
    "kw",
    [
        {"provider": "bad id!"},
        {"provider": "x", "bbox": (10, 30, 5, 35)},  # west > east
        {"provider": "x", "bbox": (-200, 0, 0, 10)},
        {"provider": "x", "bbox": (0, 40, 10, 30)},  # south > north
        {"provider": "x", "time_start": "yesterday"},
        {"provider": "x", "time_start": "2022-03-31", "time_end": "2022-03-30"},
        {"provider": "x", "variables": tuple(f"v{i}" for i in range(100))},
        {"provider": "x", "params": {"a": "b" * 20000}},
    ],
)
def test_request_rejects_malformed(kw) -> None:
    with pytest.raises(InvalidRequest):
        Request(**kw)


def test_request_from_dict_rejects_wrong_types() -> None:
    for bad in (
        "text",
        {"provider": "x", "variables": "t2m"},
        {"provider": "x", "bbox": "1,2,3,4"},
    ):
        with pytest.raises(InvalidRequest):
            Request.from_dict(bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------


def _put(cache: Cache, key: str, size: int, provider: str = "p") -> Path:
    return cache.put_bytes(provider, key, os.urandom(size), name=f"{key}.bin")


def test_cache_lru_eviction_order(tmp_path: Path) -> None:
    clock = iter(range(1000))
    cache = Cache(tmp_path, 3000, clock=lambda: next(clock))
    _put(cache, "a", 1000)
    _put(cache, "b", 1000)
    _put(cache, "c", 1000)
    assert cache.usage()["bytes"] == 3000
    assert cache.lookup("p", "a") is not None  # a is now the most recently used
    _put(cache, "d", 1000)  # over the limit: b (least recently used) must go
    assert not cache.contains("p", "b")
    assert cache.contains("p", "a") and cache.contains("p", "c") and cache.contains("p", "d")
    assert cache.usage()["bytes"] <= 3000


def test_cache_protects_files_of_the_current_request(tmp_path: Path) -> None:
    cache = Cache(tmp_path, 2000)
    _put(cache, "old", 1500)
    tmp = cache.tmp_path("new")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(b"x" * 1500)
    cache.commit("p", "new", tmp, name="new.bin", protect=["new"])
    assert cache.contains("p", "new") and not cache.contains("p", "old")


def test_cache_survives_reload_and_drops_damaged_entries(tmp_path: Path) -> None:
    cache = Cache(tmp_path, 10_000)
    path = _put(cache, "a", 100)
    again = Cache(tmp_path, 10_000)
    assert again.lookup("p", "a") == path
    path.write_bytes(b"truncated")  # changed behind the cache's back
    assert Cache(tmp_path, 10_000).lookup("p", "a") is None


def test_cache_verify_detects_silent_corruption(tmp_path: Path) -> None:
    cache = Cache(tmp_path, 10_000)
    path = _put(cache, "a", 100)
    path.write_bytes(b"\x00" * 100)  # same size, different bytes
    assert cache.lookup("p", "a") is not None  # cheap check passes
    assert cache.lookup("p", "a", verify=True) is None  # content check catches it
    assert not cache.contains("p", "a")


def test_cache_checksum_mismatch_discards_the_file(tmp_path: Path) -> None:
    cache = Cache(tmp_path, 10_000)
    tmp = cache.tmp_path("x")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(b"hello")
    with pytest.raises(ChecksumMismatch):
        cache.commit("p", "x", tmp, name="x.bin", expected_sha256="0" * 64)
    assert not tmp.exists() and not cache.contains("p", "x")
    tmp.write_bytes(b"hello")
    _, digest = cache.commit(
        "p", "x", tmp, name="x.bin", expected_sha256=hashlib.sha256(b"hello").hexdigest()
    )
    assert digest == hashlib.sha256(b"hello").hexdigest()


def test_clearing_a_shared_folder_removes_only_what_the_cache_made(tmp_path: Path) -> None:
    """The user may point the cache at a folder with other things in it (even one named tmp)."""
    root = tmp_path / "work"
    (root / "tmp").mkdir(parents=True)
    (root / "tmp" / "keep.txt").write_text("mine")
    (root / "notes.md").write_text("mine")
    cache = Cache(root, 10_000)
    _put(cache, "a", 100)
    stale = cache.tmp_path("half")
    stale.write_bytes(b"partial")
    assert cache.clear() == 100
    assert (root / "tmp" / "keep.txt").read_text() == "mine" and (root / "notes.md").exists()
    assert not stale.exists() and cache.usage()["files"] == 0


def test_cache_names_cannot_escape_the_folder(tmp_path: Path) -> None:
    cache = Cache(tmp_path / "c", 10_000)
    p = cache.put_bytes("p/../..", "k", b"data", name="../../evil.sh")
    assert (tmp_path / "c") in p.parents and p.is_file()
    assert not (tmp_path / "evil.sh").exists()


def test_cache_clear_and_shrinking_the_limit(tmp_path: Path) -> None:
    cache = Cache(tmp_path, 10_000)
    for k in "abc":
        _put(cache, k, 1000, provider="p" if k != "c" else "q")
    cache.max_bytes = 1500
    cache.evict()
    assert cache.usage()["bytes"] <= 1500
    cache.max_bytes = 10_000
    _put(cache, "z", 100, provider="q")
    assert cache.clear("q") > 0
    assert not cache.contains("q", "z")
    cache.clear()
    assert cache.usage()["files"] == 0


# ---------------------------------------------------------------------------
# HTTP client (fake transport: the real retry / timeout / cap code runs)
# ---------------------------------------------------------------------------


def _client(server: FakeServer, **kw) -> tuple[HttpClient, Sleeper]:
    sl = Sleeper()
    kw.setdefault("min_interval_s", 0)
    return HttpClient(opener=server.opener, sleep=sl, **kw), sl


def test_client_sends_an_honest_user_agent() -> None:
    server = FakeServer({"a.bin": b"abc"})
    client, _ = _client(server)
    assert client.get_bytes(BASE_URL + "a.bin") == b"abc"
    ua = server.requests[0][1]
    assert ua.startswith("radarx-desktop/") and "github.com/syedhamidali/radarx-desktop" in ua


def test_client_retries_with_backoff_then_succeeds() -> None:
    server = FakeServer({"a.bin": b"abc"}, fail_first={"a.bin": 2})
    client, sl = _client(server, backoff_s=1.0)
    assert client.get_bytes(BASE_URL + "a.bin") == b"abc"
    assert server.count("a.bin") == 3 and client.retries_made == 2
    assert len(sl.waits) == 2 and sl.waits[1] > sl.waits[0] >= 1.0  # exponential


def test_client_honours_retry_after() -> None:
    server = FakeServer(
        {"a.bin": b"abc"}, fail_first={"a.bin": 1}, fail_status=429, retry_after="7"
    )
    client, sl = _client(server, backoff_s=0.1)
    client.get_bytes(BASE_URL + "a.bin")
    assert sl.waits and sl.waits[0] >= 7.0


def test_client_gives_up_after_the_retries() -> None:
    server = FakeServer({"a.bin": b"abc"}, fail_first={"a.bin": 99})
    client, _ = _client(server, retries=2)
    with pytest.raises(FetchFailed, match="after 3 tries"):
        client.get_bytes(BASE_URL + "a.bin")
    assert server.count("a.bin") == 3


def test_client_does_not_retry_a_404() -> None:
    server = FakeServer({})
    client, sl = _client(server)
    with pytest.raises(FetchFailed, match="404"):
        client.get_bytes(BASE_URL + "missing.bin")
    assert server.count("missing.bin") == 1 and not sl.waits


def test_client_retries_a_truncated_transfer() -> None:
    body = os.urandom(5000)
    server = FakeServer({"a.bin": body}, truncate_first={"a.bin": 1})
    client, _ = _client(server)
    assert client.get_bytes(BASE_URL + "a.bin") == body
    assert server.count("a.bin") == 2


def test_client_offline_opens_no_connection() -> None:
    server = FakeServer({"a.bin": b"abc"})
    client = HttpClient(opener=server.opener, offline=lambda: True, min_interval_s=0)
    with pytest.raises(OfflineError):
        client.get_bytes(BASE_URL + "a.bin")
    assert server.requests == []


def test_client_enforces_the_byte_cap_both_ways(tmp_path: Path) -> None:
    server = FakeServer({"a.bin": b"x" * 5000})
    client, _ = _client(server)
    with pytest.raises(FetchFailed, match="approved"):
        client.download(BASE_URL + "a.bin", tmp_path / "a.bin", max_bytes=1000)
    assert not (tmp_path / "a.bin").exists()  # nothing left behind
    quiet = FakeServer({"a.bin": b"x" * 5000}, announce_length=False)  # server hides the size
    client2, _ = _client(quiet)
    with pytest.raises(FetchFailed, match="approved"):
        client2.download(BASE_URL + "a.bin", tmp_path / "b.bin", max_bytes=1000)
    assert not (tmp_path / "b.bin").exists()


def test_client_refuses_other_schemes() -> None:
    for url in ("file:///etc/passwd", "ftp://x/y", "javascript:alert(1)", "http://"):
        with pytest.raises(FetchFailed, match="only http"):
            check_url(url)


def test_client_limits_simultaneous_connections() -> None:
    state = {"now": 0, "max": 0}
    lock = threading.Lock()
    inner = FakeServer({"a.bin": b"abc"})

    def opener(req, timeout):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.03)
        try:
            return inner.opener(req, timeout)
        finally:
            with lock:
                state["now"] -= 1

    client = HttpClient(opener=opener, max_concurrent=2, min_interval_s=0)
    threads = [
        threading.Thread(target=client.get_bytes, args=(BASE_URL + "a.bin",)) for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["max"] == 2


def test_client_cancel_stops_the_transfer(tmp_path: Path) -> None:
    server = FakeServer({"a.bin": b"x" * 10000})
    client, _ = _client(server)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(FetchCancelled):
        client.download(BASE_URL + "a.bin", tmp_path / "a.bin", cancel=cancel)


class _Flaky(http.server.BaseHTTPRequestHandler):
    failures = 0
    seen_agents: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        type(self).seen_agents.append(self.headers.get("User-Agent", ""))
        if type(self).failures > 0:
            type(self).failures -= 1
            self.send_response(503)
            self.send_header("Retry-After", "0")
            self.end_headers()
            return
        body = b"real-socket-body" * 100
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # silence
        pass


def test_real_http_stack_against_a_flaky_localhost_server(tmp_path: Path) -> None:
    """The default (socket) transport: a server on 127.0.0.1 that fails twice, then serves."""
    _Flaky.failures = 2
    _Flaky.seen_agents = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Flaky)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/f.bin"
        client = HttpClient(backoff_s=0.01, min_interval_s=0, connect_timeout_s=2, read_timeout_s=2)
        n = client.download(url, tmp_path / "f.bin")
        assert n == 1600 and (tmp_path / "f.bin").read_bytes().startswith(b"real-socket-body")
        assert client.retries_made == 2
        assert all(a.startswith("radarx-desktop/") for a in _Flaky.seen_agents)
        # offline: the very same client refuses before opening a socket
        client._offline = lambda: True
        with pytest.raises(OfflineError):
            client.get_bytes(url)
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------
# provider: estimate / approve / fetch
# ---------------------------------------------------------------------------

REQ = Request(provider="fake", variables=("t2m",))


def test_estimate_downloads_nothing_and_tells_the_user_everything(tmp_path: Path) -> None:
    env, server, _provider, _ = make_env(tmp_path)
    est = env.estimate(REQ)
    assert server.requests == []  # a plan is a listing, not a download
    d = est.to_dict()
    assert d["n_files"] == 3 and d["total_bytes"] == d["network_bytes"] > 0
    assert d["size_exact"] and d["requires_approval"] and d["can_fetch"]
    assert d["destination"] == str(tmp_path / "cache")
    assert "Synthetic" in d["attribution"] and d["licence"] and d["source"].startswith("https://")
    assert all(not f["cached"] for f in d["files"])


def test_fetch_without_approval_downloads_nothing(tmp_path: Path) -> None:
    env, server, *_ = make_env(tmp_path)
    with pytest.raises(ApprovalRequired, match="needs your approval"):
        env.fetch(REQ)
    assert server.requests == []
    assert not list((tmp_path / "cache").rglob("*fake_t2m*"))  # only the listing may be remembered


def test_approval_is_bound_to_the_request_and_destination(tmp_path: Path) -> None:
    env, server, *_ = make_env(tmp_path)
    other = Request(provider="fake", variables=("u10",))
    approval_for_other = env.estimate(other).approve()
    with pytest.raises(ApprovalRequired):
        env.fetch(REQ, approval_for_other)
    mine = env.estimate(REQ).approve()
    moved = Approval(mine.request_digest, mine.max_bytes, "/somewhere/else")
    with pytest.raises(ApprovalRequired):
        env.fetch(REQ, moved)
    too_small = Approval(mine.request_digest, 10, mine.destination)
    with pytest.raises(ApprovalRequired):
        env.fetch(REQ, too_small)
    assert server.requests == []


def test_fetch_with_approval_then_cache_then_offline(tmp_path: Path) -> None:
    env, server, _provider, _ = make_env(tmp_path)
    est = env.estimate(REQ)
    seen: list[dict] = []
    res = env.fetch(REQ, est.approve(), progress=seen.append)
    assert len(res.paths) == 3 and all(p.is_file() for p in res.paths)
    assert res.bytes_downloaded == est.total_bytes and not any(res.from_cache)
    assert res.provenance["kind"] == "model" and "Fake model" in res.provenance["source"]
    assert res.attribution and res.licence and len(res.checksums) == 3
    assert json.loads(res.paths[0].read_text())["var"] == "t2m"
    pct = [p["percent"] for p in seen]
    assert pct == sorted(pct) and pct[-1] == 100 and seen[-1]["message"] == "Done"
    n = len(server.requests)
    # second time: all cached, no approval needed, no request made
    again = env.fetch(REQ)
    assert all(again.from_cache) and again.bytes_downloaded == 0 and len(server.requests) == n
    # offline: still works from the cache, even for a fresh process (new Environment, same folder)
    env.settings.offline = True
    assert env.fetch(REQ).paths == res.paths
    env2, _, _, _ = make_env(tmp_path, FakeServer({}), offline=True)
    est2 = env2.estimate(REQ)  # the listing was remembered with the files
    assert est2.can_fetch and est2.cached == [True] * 3 and not est2.requires_approval
    assert env2.fetch(REQ).paths == res.paths


def test_offline_with_nothing_cached_is_refused_politely(tmp_path: Path) -> None:
    env, server, *_ = make_env(tmp_path, offline=True)
    est = env.estimate(REQ)
    assert not est.can_fetch and "Offline" in est.reason
    with pytest.raises(OfflineError):
        env.fetch(REQ, est.approve())
    assert server.requests == []


def test_offline_partly_cached(tmp_path: Path) -> None:
    env, _server, *_ = make_env(tmp_path)
    one = Request(
        provider="fake",
        variables=("t2m",),
        time_start="2022-03-30T00:00:00Z",
        time_end="2022-03-30T00:59:00Z",
    )
    env.fetch(one, env.estimate(one).approve())
    env.settings.offline = True
    assert env.estimate(one).can_fetch
    assert not env.estimate(REQ).can_fetch  # the other hours are not cached


def test_checksum_mismatch_is_detected_and_nothing_is_kept(tmp_path: Path) -> None:
    server = FakeServer(make_catalogue(), corrupt={"fake_t2m_202203300001.json"})
    env, _, _, _ = make_env(tmp_path, server)
    with pytest.raises(ChecksumMismatch, match="does not match"):
        env.fetch(REQ, env.estimate(REQ).approve())
    assert server.count("fake_t2m_202203300001.json") == 2  # one retry, then it stops
    assert not env.cache.contains("fake", "t2m/202203300001")
    assert not list((tmp_path / "cache" / ".partial").glob("*.part"))  # no partial files left


def test_transient_corruption_is_fixed_by_the_single_retry(tmp_path: Path) -> None:
    server = FakeServer(make_catalogue())
    env, _, _, _ = make_env(tmp_path, server)
    original = server.opener
    state = {"n": 0}

    def opener(req, timeout):
        resp = original(req, timeout)
        if req.full_url.endswith("0001.json") and state["n"] == 0:
            state["n"] += 1
            data = bytearray(resp.read())
            data[0] ^= 0xFF
            resp.close()
            from engine.environment.providers.fake import _Headers, _Response

            return _Response(bytes(data), _Headers({"Content-Length": str(len(data))}))
        return resp

    env.http._opener = opener
    res = env.fetch(REQ, env.estimate(REQ).approve())
    assert len(res.paths) == 3 and state["n"] == 1


def test_flaky_server_is_handled_by_the_retries(tmp_path: Path) -> None:
    server = FakeServer(make_catalogue(), fail_first={"fake_t2m_202203300000.json": 2})
    env, _, _, sleeper = make_env(tmp_path, server)
    res = env.fetch(REQ, env.estimate(REQ).approve())
    assert len(res.paths) == 3 and len(sleeper.waits) == 2


def test_permanent_failure_reports_clearly_and_keeps_finished_files(tmp_path: Path) -> None:
    server = FakeServer(make_catalogue(), fail_first={"fake_t2m_202203300002.json": 99})
    env, _, _, _ = make_env(tmp_path, server, retries=1)
    with pytest.raises(FetchFailed, match="Could not download"):
        env.fetch(REQ, env.estimate(REQ).approve())
    assert not list((tmp_path / "cache" / ".partial").glob("*.part"))


def test_server_sending_more_than_approved_is_stopped(tmp_path: Path) -> None:
    server = FakeServer(make_catalogue())
    env, _, _, _ = make_env(tmp_path, server)
    est = env.estimate(REQ)
    name = "fake_t2m_202203300000.json"
    server.files[name] = server.files[name] + b" " * 100_000  # the file grew after the listing
    with pytest.raises(FetchFailed, match="approved"):
        env.fetch(REQ, est.approve())


def test_request_larger_than_the_cache_is_refused_up_front(tmp_path: Path) -> None:
    env, server, *_ = make_env(tmp_path, cache_bytes=1000)
    est = env.estimate(REQ)
    assert not est.can_fetch and "cache limit" in est.reason
    with pytest.raises(RequestTooLarge):
        env.fetch(REQ, est.approve())
    assert server.requests == []


def test_fetch_evicts_least_recently_used_files_of_other_requests(tmp_path: Path) -> None:
    files = make_catalogue(hours=2, size=2000)
    size = len(next(iter(files.values())))
    env, _server, *_ = make_env(tmp_path, FakeServer(files), cache_bytes=int(size * 2.5))
    t = Request(provider="fake", variables=("t2m",))
    u = Request(provider="fake", variables=("u10",))
    env.fetch(t, env.estimate(t).approve())
    env.fetch(u, env.estimate(u).approve())
    assert env.cache.usage()["bytes"] <= env.cache.max_bytes
    assert env.estimate(u).cached == [True, True]  # the newest request is complete
    assert env.estimate(t).cached != [True, True]  # the oldest paid for it


def test_cancel_stops_everything_and_leaves_no_partial_files(tmp_path: Path) -> None:
    env, _server, *_ = make_env(tmp_path)
    cancel = threading.Event()
    est = env.estimate(REQ)
    approval = est.approve()

    def on_progress(p: dict) -> None:
        cancel.set()  # the user pressed Cancel after the first bytes

    with pytest.raises(FetchCancelled):
        env.fetch(REQ, approval, progress=on_progress, cancel=cancel)
    assert not list((tmp_path / "cache" / ".partial").glob("*.part"))


def test_unknown_sizes_are_flagged_and_capped(tmp_path: Path) -> None:
    server = FakeServer(make_catalogue())
    env, _, provider, _ = make_env(tmp_path, server, with_checksums=False)
    orig = provider.plan

    def plan(request, env_):
        return [type(s)(key=s.key, name=s.name, url=s.url, size=None) for s in orig(request, env_)]

    provider.plan = plan  # type: ignore[method-assign]
    est = env.estimate(REQ)
    assert not est.size_exact and any("not known in advance" in n for n in est.notes)
    assert est.max_network_bytes == 3 * est.unknown_size_cap
    assert len(env.fetch(REQ, est.approve()).paths) == 3


def test_subsetting_by_variable_and_time(tmp_path: Path) -> None:
    env, *_ = make_env(tmp_path)
    all_vars = env.estimate(Request(provider="fake"))
    assert len(all_vars.files) == 6
    t_only = env.estimate(
        Request(provider="fake", variables=("u10",), time_start="2022-03-30T01:00:00Z")
    )
    assert [f.meta["hour"] for f in t_only.files] == [1, 2]
    assert t_only.total_bytes < all_vars.total_bytes


def test_subset_dataset_area_time_variables() -> None:
    import numpy as np
    import xarray as xr

    from engine.environment import subset_dataset

    ds = xr.Dataset(
        {
            "t": (
                ("time", "latitude", "longitude"),
                np.arange(2 * 5 * 8, dtype=float).reshape(2, 5, 8),
            ),
            "q": (("time", "latitude", "longitude"), np.ones((2, 5, 8))),
        },
        coords={
            "time": np.array(["2022-03-30T00", "2022-03-30T06"], dtype="datetime64[h]"),
            "latitude": [30.0, 31, 32, 33, 34],
            "longitude": [270.0, 271, 272, 273, 274, 275, 276, 277],  # 0..360 storage
        },
    )
    req = Request(
        provider="x",
        variables=("t",),
        bbox=(-88.0, 31.0, -85.0, 33.0),
        time_start="2022-03-30T03:00:00Z",
    )
    out = subset_dataset(ds, req)
    assert list(out.data_vars) == ["t"] and out.sizes["time"] == 1
    assert out.latitude.values.tolist() == [31, 32, 33] and out.longitude.values.tolist() == [
        272,
        273,
        274,
        275,
    ]
    with pytest.raises(InvalidRequest, match="Available"):
        subset_dataset(ds, Request(provider="x", variables=("nope",)))


def test_plan_is_remembered_so_an_estimate_works_offline(tmp_path: Path) -> None:
    env, _server, provider, _ = make_env(tmp_path)
    env.estimate(REQ)
    env.estimate(REQ)
    assert provider.plan_calls == 1  # the second estimate used the remembered listing


# ---------------------------------------------------------------------------
# local files, registry, settings
# ---------------------------------------------------------------------------


def test_local_files_need_no_approval_and_declare_their_class(tmp_path: Path) -> None:
    f = tmp_path / "model.nc"
    f.write_bytes(b"data")
    env = Environment(EnvSettings(cache_dir=str(tmp_path / "c")))
    env.registry.register(LocalFilesProvider())
    req = Request(
        provider="local_files", params={"paths": [str(f)], "kind": "model", "title": "My HRRR"}
    )
    est = env.estimate(req)
    assert not est.requires_approval and est.can_fetch and est.cached == [True]
    res = env.fetch(req)
    assert res.paths == [f]
    assert res.provenance["kind"] == "model" and "declared by the user" in res.provenance["method"]
    assert res.provenance["title"] == "My HRRR"


def test_local_files_validation(tmp_path: Path) -> None:
    env = Environment(EnvSettings(cache_dir=str(tmp_path / "c")))
    env.registry.register(LocalFilesProvider())
    for params in (
        {},
        {"paths": []},
        {"paths": ["relative/file.nc"]},
        {"paths": [str(tmp_path / "missing.nc")]},
        {"paths": [str(tmp_path / ".." / "x")]},
        {"paths": [str(tmp_path)]},  # a folder
        {"paths": [str(tmp_path / "a")], "kind": "magic"},
    ):
        with pytest.raises(InvalidRequest):
            env.estimate(Request(provider="local_files", params=params))


def test_registry_rejects_duplicates_and_unknown_ids() -> None:
    reg = Registry()
    p = FakeProvider(FakeServer({}))
    reg.register(p)
    with pytest.raises(ValueError, match="already registered"):
        reg.register(FakeProvider(FakeServer({})))
    reg.register(FakeProvider(FakeServer({})), replace=True)
    with pytest.raises(UnknownProvider, match="known: fake"):
        reg.get("nope")
    bad = FakeProvider(FakeServer({}))
    bad.kind = "wishful"  # type: ignore[misc]
    with pytest.raises(ValueError, match="provenance class"):
        Registry().register(bad)


def test_settings_validation_persistence_and_env_override(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "environment.json"
    monkeypatch.setenv("XRADAR_ENV_SETTINGS", str(path))
    for var in ("XRADAR_OFFLINE", "XRADAR_ENV_CACHE_DIR", "XRADAR_ENV_CACHE_MAX_MB"):
        monkeypatch.delenv(var, raising=False)
    env = Environment(env_settings.load_settings(), persist=True)
    st = env.apply_settings(
        {"cache_dir": str(tmp_path / "mycache"), "cache_max_mb": 64, "offline": True}
    )
    assert st["settings"]["effective_cache_max_mb"] == 64 and env.offline
    assert env_settings.load_settings().cache_dir == str(tmp_path / "mycache")  # persisted
    for bad in (
        {"cache_dir": "relative/dir"},
        {"cache_dir": "/a/../b"},
        {"cache_max_mb": "lots"},
        {"nonsense": 1},
    ):
        with pytest.raises(InvalidRequest):
            env.apply_settings(bad)
    assert (
        env.apply_settings({"cache_max_mb": 1})["settings"]["cache_max_mb"]
        == env_settings.MIN_CACHE_MB
    )  # clamped
    monkeypatch.setenv("XRADAR_OFFLINE", "1")
    monkeypatch.setenv("XRADAR_ENV_CACHE_MAX_MB", "100")
    assert env_settings.load_settings().effective_offline
    assert env_settings.EnvSettings().effective_cache_max_mb == 100
    assert "offline" in env_settings.EnvSettings().locked()


def test_damaged_settings_file_falls_back_to_defaults(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "environment.json"
    path.write_text("{ not json")
    assert env_settings.load_settings(path) == EnvSettings()
    path.write_text(json.dumps({"cache_max_mb": "x", "offline": True}))
    assert env_settings.load_settings(path) == EnvSettings()


def test_the_connection_limit_follows_the_settings(tmp_path: Path) -> None:
    env = Environment(EnvSettings(cache_dir=str(tmp_path / "c")))
    assert env.http.max_concurrent == 2
    env.apply_settings({"max_concurrent": 4})
    assert env.http.max_concurrent == 4 and env.http._sem._value == 4
    env.apply_settings({"max_concurrent": 99})  # clamped to something polite
    assert env.http.max_concurrent == 4


def test_apply_settings_shrinks_the_cache(tmp_path: Path) -> None:
    env, *_ = make_env(tmp_path)
    env.cache.put_bytes("p", "a", b"x" * 1000, name="a")
    env.apply_settings({"cache_max_mb": 16})
    assert env.cache.max_bytes == 16 * 1024 * 1024


# ---------------------------------------------------------------------------
# real services (opt-in)
# ---------------------------------------------------------------------------


@NETWORK
def test_iem_raob_real_sounding(tmp_path: Path) -> None:  # pragma: no cover - network
    env = Environment(EnvSettings(cache_dir=str(tmp_path / "c")))
    from engine.environment.providers.soundings import IemRaobProvider

    env.registry.register(IemRaobProvider())
    req = Request(
        provider="iem_raob", time_start="2022-03-30T00:00:00Z", params={"station": "KJAN"}
    )
    est = env.estimate(req)
    res = env.fetch(req, est.approve())
    from engine import sounding

    snd = sounding.read_sounding_file(res.paths[0], "iem_json")
    assert snd.station and snd.profile.sizes["height"] > 20
    assert urllib.request.getproxies() is not None
