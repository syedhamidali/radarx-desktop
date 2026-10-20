"""`import rxd` / `engine.api` and the account gate: importing is free, using needs an account."""

from __future__ import annotations

import inspect

import pytest

import rxd
from engine import api
from engine.account import core
from engine.account.errors import AccountRequired
from engine.account.store import SessionStore
from tests.conftest import make_synthetic_datatree


def _volume() -> api.Volume:
    return api.Volume.from_tree(make_synthetic_datatree(n_azimuth=30, n_range=40))


def test_rxd_and_api_export_the_same_exception():
    assert rxd.AccountRequired is api.AccountRequired is AccountRequired
    assert "AccountRequired" in api.__all__


def test_every_public_function_is_gated_or_deliberately_open(gate_on):
    """If a function is added to the API without the gate, this fails."""
    for name in api.__all__:
        member = getattr(api, name)
        if name in api.API_UNGATED:
            continue
        assert inspect.isfunction(member), name
        assert getattr(member, "__radarx_gated__", False), f"api.{name} is not gated"
        with pytest.raises(AccountRequired):
            member()  # the gate answers before any argument is looked at


def test_the_open_list_is_only_pure_helpers_and_classes():
    for name in api.API_UNGATED:
        member = getattr(api, name)
        assert inspect.isclass(member) or name in {
            "check_params",
            "coerce_user_params",
            "format_module",
            "user_errors",
            "EXPORT_FORMATS",
        }


def test_every_volume_method_is_gated(gate_on, monkeypatch):
    monkeypatch.setenv("RADARX_AUTH_DISABLED", "1")  # to be allowed to build one
    volume = _volume()
    monkeypatch.delenv("RADARX_AUTH_DISABLED")
    public = [
        n
        for n, m in vars(api.Volume).items()
        if inspect.isfunction(m) and (not n.startswith("_") or n == "__init__") and n != "close"
    ]
    assert {"run", "export", "pipeline", "plot", "field", "info", "cells", "__init__"} <= set(
        public
    )
    for name in public:
        method = getattr(api.Volume, name)
        assert getattr(method, "__radarx_gated__", False), f"Volume.{name} is not gated"
    for call in (
        lambda: volume.info(),
        lambda: volume.field("DBZH"),
        lambda: volume.run("radarx_hid"),
        lambda: volume.export("zarr", "x.zarr"),
        lambda: volume.tree,
        lambda: api.Volume.from_tree(make_synthetic_datatree()),
        lambda: api.Volume(object()),
    ):
        with pytest.raises(AccountRequired):
            call()


def test_open_and_friends_raise_a_clear_error(gate_on, tmp_path):
    for call in (
        lambda: rxd.open(tmp_path / "anything.nc"),
        lambda: rxd.info("x"),
        lambda: rxd.modules(),
        lambda: rxd.mosaic(["a", "b"]),
        lambda: rxd.winds(["a", "b"]),
        lambda: rxd.qpe(["a"]),
        lambda: rxd.track(["a", "b"]),
        lambda: rxd.sounding("x.csv"),
    ):
        with pytest.raises(AccountRequired) as err:
            call()
        assert err.value.reason == "no_session"
        assert "rxd login" in str(err.value) or "not signed in" in str(err.value)


def test_a_missing_file_is_still_an_input_error_once_signed_in(gate_on):
    core.login_with_password("ada@example.test", "correct horse 1")
    with pytest.raises(rxd.XrdInputError):
        rxd.open("/does/not/exist.nc")
    assert [m["id"] for m in rxd.modules()]


def test_signing_out_in_another_program_is_noticed_within_seconds(gate_on):
    core.login_with_password("ada@example.test", "correct horse 1")
    assert rxd.modules()
    SessionStore().clear()  # `rxd logout` in a terminal
    core.reset_runtime_state()  # (the check is reused for up to 30 s inside one process)
    with pytest.raises(AccountRequired):
        rxd.modules()


def test_a_working_session_works_through_the_whole_api(gate_on, tmp_path, monkeypatch):
    core.login_with_password("ada@example.test", "correct horse 1")
    volume = _volume()
    assert volume.n_sweeps == 3
    assert volume.info()["n_sweeps"] == 3
    out = volume.export("csv", tmp_path / "v.csv", variable="DBZH")
    assert out.exists()


def test_the_check_is_cheap_when_repeated(gate_on):
    core.login_with_password("ada@example.test", "correct horse 1")
    gate_on.calls.clear()
    for _ in range(200):
        core.require_account()
    assert gate_on.calls == []  # no network; the verified result is reused for a short time


def test_animate_needs_an_account_too(gate_on):
    from engine import animate

    with pytest.raises(AccountRequired):
        animate.run(object())  # the gate answers before the options are looked at
