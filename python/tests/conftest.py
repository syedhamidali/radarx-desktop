"""Shared pytest fixtures for radarx-desktop engine tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import xarray as xr

# The account gate (engine/account, docs/ACCOUNTS.md) is OFF for the test suite: this is the
# documented development bypass, honoured only when the code is not frozen. Tests of the gate itself
# (tests/test_account_*.py) switch it on with the ``gate_on`` fixture and use a locally made test
# key, a mocked key document and a mocked account server, never the real network or a real account.
os.environ["RADARX_AUTH_DISABLED"] = "1"
os.environ.setdefault("XRADAR_WATCHDOG_S", "0")  # a test must never end the test process

# ---------------------------------------------------------------------------
# Synthetic radar data
# ---------------------------------------------------------------------------

N_AZIMUTH = 360
N_RANGE = 500
ELEVATIONS = [0.5, 1.5, 2.4]


@pytest.fixture()
def synthetic_datatree() -> xr.DataTree:
    """Build a minimal synthetic radar DataTree that mimics xradar output."""
    return make_synthetic_datatree()


def make_synthetic_datatree(
    n_azimuth: int = N_AZIMUTH,
    n_range: int = N_RANGE,
    elevations: list[float] | None = None,
) -> xr.DataTree:
    """Reusable helper — not a fixture so tests can call it directly."""
    if elevations is None:
        elevations = list(ELEVATIONS)

    azimuth = np.linspace(0, 359, n_azimuth)
    range_m = np.linspace(0, 150_000, n_range)

    sweeps: dict[str, xr.Dataset] = {}
    for i, elev in enumerate(elevations):
        rng = np.random.default_rng(i)
        ds = xr.Dataset(
            {
                "DBZH": (
                    ["azimuth", "range"],
                    rng.uniform(-20, 75, (n_azimuth, n_range)).astype(np.float32),
                ),
                "VRADH": (
                    ["azimuth", "range"],
                    rng.uniform(-30, 30, (n_azimuth, n_range)).astype(np.float32),
                ),
            },
            coords={
                "azimuth": azimuth,
                "range": range_m,
                "elevation": ("azimuth", np.full(n_azimuth, elev, dtype=np.float32)),
            },
            attrs={"sweep_fixed_angle": elev},
        )
        sweeps[f"sweep_{i}"] = ds

    root_ds = xr.Dataset(attrs={"instrument_name": "SYNTHETIC"})
    return xr.DataTree.from_dict({"/": root_ds, **{f"/{k}": v for k, v in sweeps.items()}})


@pytest.fixture()
def synthetic_dataarray() -> xr.DataArray:
    """A single-sweep DataArray suitable for renderer / arrow-bridge tests."""
    rng = np.random.default_rng(42)
    n_az, n_rng = 360, 500
    values = rng.uniform(-20, 75, (n_az, n_rng)).astype(np.float32)
    return xr.DataArray(
        values,
        dims=["azimuth", "range"],
        coords={
            "azimuth": np.linspace(0, 359, n_az),
            "range": np.linspace(0, 150_000, n_rng),
        },
        name="DBZH",
    )


# ---------------------------------------------------------------------------
# Temporary directories
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_dir(tmp_path: Path) -> Path:
    """Provide a clean temporary directory (pytest built-in, re-exported for clarity)."""
    return tmp_path


@pytest.fixture()
def tmp_output_file(tmp_path: Path) -> Path:
    """Provide a temporary file path for export tests (file does not exist yet)."""
    return tmp_path / "output.png"


# ---------------------------------------------------------------------------
# Fake ARCO (icechunk) store
# ---------------------------------------------------------------------------

ARCO_SCAN_KEY = "VCP-12"
ARCO_LAT, ARCO_LON, ARCO_ALT = 41.604, -88.085, 202.0


def make_arco_session(n_sweeps: int = 12, n_az: int = 36, n_rng: int = 20, n_times: int = 2) -> Any:
    """Build an in-memory icechunk repo shaped like the NEXRAD ARCO store.

    Returns a read-only session.

    Sweep ``i`` has fixed angle ``0.5 + i`` and ``DBZH == 10 * i + t`` for vcp_time ``t``.
    """
    icechunk = pytest.importorskip("icechunk")
    repo = icechunk.Repository.create(icechunk.in_memory_storage())
    ws = repo.writable_session("main")
    xr.Dataset(
        {"latitude": ((), ARCO_LAT), "longitude": ((), ARCO_LON), "altitude": ((), ARCO_ALT)}
    ).to_zarr(ws.store, group=ARCO_SCAN_KEY, mode="w", consolidated=False)
    for i in range(n_sweeps):
        times = (
            np.datetime64("2024-01-01T00:00:00")
            + np.arange(n_times)[:, None] * np.timedelta64(300, "s")
            + np.arange(n_az)[None, :] * np.timedelta64(1, "s")
        )
        data = np.full((n_times, n_az, n_rng), 10.0 * i, dtype=np.float32)
        data += np.arange(n_times, dtype=np.float32)[:, None, None]
        ds = xr.Dataset(
            {
                "DBZH": (("vcp_time", "azimuth", "range"), data),
                "sweep_fixed_angle": (("vcp_time",), np.full(n_times, 0.5 + i, np.float32)),
            },
            coords={
                "azimuth": np.arange(n_az) * (360.0 / n_az),
                "range": 250.0 + np.arange(n_rng) * 250.0,
                "time": (("vcp_time", "azimuth"), times),
            },
        )
        ds.to_zarr(ws.store, group=f"{ARCO_SCAN_KEY}/sweep_{i}", mode="w", consolidated=False)
    ws.commit("init")
    return repo.readonly_session("main")


@pytest.fixture()
def fake_arco(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Route ``engine.cloud._open_icechunk_session`` to an in-memory ARCO-like store."""
    session = make_arco_session()
    import engine.cloud as cloud

    monkeypatch.setattr(cloud, "_open_icechunk_session", lambda station: session)
    return session


@pytest.fixture(autouse=True)
def _isolated_export_dir(tmp_path_factory, monkeypatch):
    """Exports without an explicit path must never write into the developer's real ~/Downloads."""
    monkeypatch.setenv("XRADAR_EXPORT_DIR", str(tmp_path_factory.mktemp("radarx-exports")))


@pytest.fixture(autouse=True)
def _isolated_account_dir(tmp_path_factory, monkeypatch):
    """No test may read or write the developer's real session file, key cache or activity queue."""
    monkeypatch.setenv("RADARX_CONFIG_DIR", str(tmp_path_factory.mktemp("radarx-config")))


# ---------------------------------------------------------------------------
# The account gate switched ON, against a mocked account server (tests/test_account_*.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
def gate_on(monkeypatch, tmp_path):
    """The gate ON with a local test key and a mocked account server; the real network is blocked.

    Yields a ``FakeServer`` (``.keys.token(...)`` makes tokens, ``.now`` is the clock, ``.calls``,
    ``.activity_rows`` show what the code sent). The sender thread is off; tests flush explicitly.
    """
    import socket

    from engine.account import activity, core, http, tokens
    from tests.account_helpers import TEST_URL, FakeServer, KeyPair

    monkeypatch.delenv("RADARX_AUTH_DISABLED", raising=False)
    monkeypatch.delenv("RADARX_WEB_REQUIRE_ACCOUNT", raising=False)
    monkeypatch.setenv("RADARX_SUPABASE_URL", TEST_URL)
    monkeypatch.setenv("RADARX_SUPABASE_KEY", "sb_publishable_test_only")
    monkeypatch.setenv("RADARX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delattr("sys.frozen", raising=False)

    real_connect = socket.socket.connect

    def no_network(self, address, *args, **kwargs):  # only this machine may be talked to
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost") and not str(host).startswith("/"):
            raise AssertionError(f"a test tried to open a real network connection to {host}")
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", no_network)

    fake = FakeServer(keys=KeyPair())
    monkeypatch.setattr(http, "request_json", fake.request)
    monkeypatch.setattr(core, "now", fake.clock)
    tokens.reset_cache()
    core.reset_runtime_state()
    activity.set_background(False)
    activity._recent.clear()
    activity._memory_only.clear()
    yield fake
    activity.set_background(True)
    tokens.reset_cache()
    core.reset_runtime_state()


@pytest.fixture(autouse=True)
def _no_leaked_account_threads():
    """The activity sender thread and the server's recorder pool must not outlive a test: a process
    that
    later forks (subprocess.Popen) while such threads run can crash on macOS (segfault in fork)."""
    yield
    from engine.account import activity

    activity.stop_sender()
    server = sys.modules.get("server")
    if server is not None:
        server.shutdown_activity_pool()
