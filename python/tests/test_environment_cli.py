"""``rxd env``: consent, estimate, fetch and cache from the command line.

The source is the in-memory fake provider of the framework's own test harness; the process-wide
environment is replaced by a throw-away one, so no network and no real cache is touched.
"""

from __future__ import annotations

import json

import pytest

from engine import cli
from engine.environment import Environment, EnvSettings, HttpClient, Registry
from engine.environment.providers.fake import FakeProvider, FakeServer, make_catalogue

ARGS = ("fake", "--time", "2022-03-30T00:00Z", "--time-end", "2022-03-30T01:00Z", "-V", "t2m")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    server = FakeServer(make_catalogue(hours=3, size=4000))
    http = HttpClient(opener=server.opener, min_interval_s=0, backoff_s=0, sleep=lambda s: None)
    e = Environment(
        EnvSettings(cache_dir=str(tmp_path / "cache"), cache_max_mb=64),
        http=http,
        registry=Registry(),
    )
    e.registry.register(FakeProvider(server))
    e.server = server  # type: ignore[attr-defined]
    monkeypatch.setattr(Environment, "_default", e)
    monkeypatch.chdir(tmp_path)
    return e


def run(capsys, *argv):
    code = cli.main(["env", *map(str, argv)])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_providers_and_inventory(env, capsys):
    code, out, _ = run(capsys, "providers")
    assert code == 0 and "fake" in out and "model" in out
    code, out, _ = run(capsys, "inventory", "fake")
    assert code == 0 and "product surface" in out and "t2m" in out
    code, out, _ = run(capsys, "inventory", "fake", "--json")
    assert json.loads(out)["inventory"][0]["product"] == "surface"


def test_estimate_prints_the_consent_text_and_downloads_nothing(env, capsys):
    code, out, _ = run(capsys, "estimate", *ARGS)
    assert code == 0
    for token in ("From      data.fake.invalid", "Download  2 file(s)", "Saved to", "Credit",
                  "Licence", "[MODEL]"):  # fmt: skip
        assert token in out, token
    assert env.server.requests == []  # not a single request
    code, out, _ = run(capsys, "estimate", *ARGS, "--json")
    est = json.loads(out)["estimate"]
    assert est["size_exact"] and est["network_bytes"] > 0 and est["requires_approval"]


def test_fetch_without_yes_downloads_nothing_and_says_how_to_approve(env, capsys):
    code, _out, err = run(capsys, "fetch", *ARGS)
    assert code == 2 and "Nothing was downloaded" in err and "--yes" in err
    assert "data.fake.invalid" in err  # the server is named
    assert env.server.requests == []


def test_fetch_yes_downloads_exactly_the_estimate_and_a_repeat_is_free(env, capsys):
    code, out, _ = run(capsys, "fetch", *ARGS, "--yes")
    assert code == 0 and "2 file(s) ready (2 downloaded" in out
    n = len(env.server.requests)
    assert n == 2
    code, out, _ = run(capsys, "fetch", *ARGS)  # cached: no question, no network
    assert code == 0 and "0 downloaded" in out and len(env.server.requests) == n


def test_a_source_without_gridded_fields_says_so(env, capsys):
    code, _o, err = run(capsys, "show", *ARGS, "--yes")
    assert code == 2 and "does not serve gridded fields" in err
    code, _o, err = run(capsys, "profile", *ARGS, "--yes", "--lat", "1", "--lon", "2")
    assert code == 2 and "does not serve vertical profiles" in err


def test_unknown_source_and_missing_arguments_are_clear(env, capsys):
    code, _o, err = run(capsys, "estimate", "nope")
    assert code == 2 and "No environment-data provider named 'nope'" in err
    code, _o, err = run(capsys, "estimate")
    assert code == 2 and "name the source" in err
    code, _o, err = run(capsys, "estimate", "fake", "--bbox", "1,2,3")
    assert code == 2 and "--bbox must be 4" in err


def test_cache_settings_offline_and_clear(env, capsys):
    run(capsys, "fetch", *ARGS, "--yes")
    code, out, _ = run(capsys, "cache")
    # two files and the remembered listing that lets an estimate work offline
    assert code == 0 and "cache folder" in out and "in 3 file(s)" in out
    code, out, _ = run(capsys, "cache", "--offline")
    assert code == 0 and "offline mode  on" in out
    other = ("fake", "--time", "2022-03-30T02:00Z", "-V", "u10", "--yes")
    code, _o, err = run(capsys, "fetch", *other)
    assert code == 3 and "ffline" in err  # a missing file needs the network
    code, out, _ = run(capsys, "cache", "--online", "--clear")
    assert code == 0 and "removed" in out and "offline mode  off" in out
    code, _o, err = run(capsys, "cache", "--online", "--offline")
    assert code == 2


def test_the_overview_and_examples_list_env():
    assert "env" in cli.overview()
    assert "weather" in cli.TOPICS
