"""The activity log: feature-level only, allow-listed, minimal, queued locally, never blocks.

The central guarantee (docs/PRIVACY.md): no recorded event carries a file name or path, coordinates
or place names, radar site identifiers, file contents or free text, and no key outside the
allow-list. `assert_clean_rows` is that check; every flow below runs through it.
"""

from __future__ import annotations

import json
import random
import re
import string
import threading
import time
from pathlib import Path

import pytest

from engine import api, cli
from engine.account import activity, core
from engine.account.store import SessionStore
from tests.conftest import make_synthetic_datatree

ROW_KEYS = {"event", "surface", "occurred_at", "device_id", "app_version", "os", "detail"}
PATHLIKE = re.compile(
    r"[/\\]|:\\|\.(gz|nc|zarr|h5|hdf5|raw|vol|csv|json|png)\b|\.\.|~|^[A-Za-z]:|@|://"
)


def assert_clean_rows(rows, *forbidden: str) -> None:
    """Fail on a key outside the allow-list, on a path-like string, or on any forbidden fragment."""
    assert rows, "no events were recorded"
    for row in rows:
        assert set(row) <= ROW_KEYS | {"uid"}, f"unexpected column(s): {set(row) - ROW_KEYS}"
        assert row["event"] in activity.EVENTS, row["event"]
        assert row["surface"] in activity.SURFACES
        detail = row["detail"]
        assert set(detail) <= set(activity.EVENTS[row["event"]]), (
            f"key outside the allow-list: {detail}"
        )
        for value in detail.values():
            assert isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_ +\-]{1,64}", value), value
        for text in [*map(str, row.values()), json.dumps(detail)]:
            if text == row["occurred_at"]:
                continue
            assert not PATHLIKE.search(text), f"path-like text in {row['event']}: {text!r}"
            for fragment in forbidden:
                assert fragment.lower() not in text.lower(), f"{fragment!r} leaked into {row}"


def queued():
    path = SessionStore().directory / "activity-queue.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture()
def signed_in(gate_on):
    core.login_with_password("ada@example.test", "correct horse 1")
    return gate_on


# -- the vocabulary -----------------------------------------------------------------------------


def test_the_event_list_is_exactly_the_documented_one():
    assert set(activity.EVENTS) == {
        "app_start", "sign_in", "sign_out", "command", "module_run", "file_opened",
        "export", "mosaic_run", "winds_run", "animate_run", "qpe_run", "error",
    }  # fmt: skip
    keys = {k for ks in activity.EVENTS.values() for k in ks}
    assert keys == {"command", "module", "format", "type", "class"}
    for banned in (
        "path",
        "file",
        "filename",
        "name",
        "lat",
        "lon",
        "site",
        "station",
        "text",
        "message",
    ):
        assert banned not in keys


def test_the_client_and_the_database_agree_on_the_allow_list():
    sql = (
        Path(__file__).resolve().parents[2] / "supabase/migrations/20261021000000_activity.sql"
    ).read_text()
    pairs = re.findall(r"when '(\w+)'\s+then array\[([^\]]*)\]", sql)
    in_db = {event: tuple(re.findall(r"'(\w+)'", keys)) for event, keys in pairs}
    assert in_db == {e: tuple(k) for e, k in activity.EVENTS.items()}
    assert "'^[A-Za-z0-9_ +-]{1,64}$'" in sql  # the same value rule as activity._VALUE
    assert activity._VALUE.pattern == r"^[A-Za-z0-9_ +\-]{1,64}$"
    assert set(
        re.findall(r"surface in \(([^)]*)\)", sql)[0].replace("'", "").replace(" ", "").split(",")
    ) == set(activity.SURFACES)


@pytest.mark.parametrize(
    "value",
    [
        "/Users/ada/KTLX20130520_201643_V06.gz",
        "C:\\data\\scan.nc",
        "KTLX20130520_201643_V06.gz",
        "scan.nc",
        "35.47,-97.52",
        "35.47",
        "ada@example.org",
        "https://example.org/x",
        "../etc/passwd",
        "~/x",
        "a" * 65,
        "",
        "   ",
        None,
        5,
        1.5,
        ["NEXRAD"],
        {"a": "b"},
        "line\nbreak",
        "tab\tx",
        "semi;colon",
        "quote'",
        "pct%",
        "ünï",
    ],
)
def test_path_like_or_odd_values_never_pass(value):
    assert activity.clean_value(value) is None
    assert activity.clean_detail("file_opened", {"format": value}) == {}


@pytest.mark.parametrize(
    "value",
    ["NEXRAD", "CfRadial", "radarx_hid", "rain rate", "netcdf", "XrdInputError", "a-b+c_d 1"],
)
def test_plain_words_pass(value):
    assert activity.clean_value(value) == value


def test_keys_outside_the_allow_list_are_dropped():
    assert activity.clean_detail("command", {"command": "info", "path": "x", "file": "y"}) == {
        "command": "info"
    }
    assert activity.clean_detail("app_start", {"command": "info"}) == {}
    assert activity.clean_detail("module_run", {"format": "NEXRAD"}) == {}
    assert activity.build_event("opened_secret_feature") is None


def test_fuzz_nothing_path_like_survives():
    rng = random.Random(7)
    alphabet = string.printable + "éß/\\:.~@%"
    for _ in range(3000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 80)))
        for event, keys in activity.EVENTS.items():
            out = activity.clean_detail(event, dict.fromkeys((*keys, "path", "file"), text))
            for value in out.values():
                assert re.fullmatch(r"[A-Za-z0-9_ +\-]{1,64}", value)
                assert (
                    "/" not in value and "\\" not in value and "." not in value and ":" not in value
                )


def test_format_family_never_uses_a_file_name():
    assert activity.format_family("nexrad_level2") == "NEXRAD"
    assert activity.format_family("cfradial1") == activity.format_family("cfradial2") == "CfRadial"
    assert activity.format_family("odim") == "ODIM"
    for odd in ("KTLX20130520_201643_V06.gz", "/a/b.nc", None, "", "synthetic"):
        assert activity.format_family(odd) == "Other"


def test_only_built_in_module_names_are_reported():
    from engine import plugins

    plugins.discover_modules()
    spec = (
        plugins.MODULES["radarx_hid"]
        if "radarx_hid" in plugins.MODULES
        else next(iter(plugins.MODULES.values()))
    )
    assert activity.module_label(spec.id, spec.source) == spec.id
    assert (
        activity.module_label("acme_client_project_x", "/home/ada/my_modules/acme.py") == "custom"
    )


# -- recording and the local queue --------------------------------------------------------------


def test_nothing_is_recorded_without_an_account(gate_on):
    activity.record("command", surface="cli", command="info")
    assert queued() == []


def test_nothing_is_recorded_with_the_development_bypass(gate_on, monkeypatch):
    core.login_with_password("ada@example.test", "correct horse 1")
    monkeypatch.setenv("RADARX_AUTH_DISABLED", "1")
    activity.record("command", surface="cli", command="info")
    assert queued() == []
    assert activity.flush(1.0) == 0


def test_a_row_has_only_the_documented_columns(signed_in):
    activity.record("file_opened", surface="api", format="NEXRAD", path="/secret/KTLX.gz")
    [row] = queued()
    assert_clean_rows([row], "secret", "KTLX")
    assert row["detail"] == {"format": "NEXRAD"}
    assert row["os"] in {"macOS", "Windows", "Linux", "Other"}
    assert re.fullmatch(r"[A-Za-z0-9]{8,80}", row["device_id"])


def test_record_never_raises_and_never_blocks_a_command(signed_in, monkeypatch):
    monkeypatch.setattr(
        activity, "_append_to_queue", lambda line: (_ for _ in ()).throw(OSError("disk full"))
    )
    activity.record("command", command="info")  # silent
    monkeypatch.setattr(
        activity, "_signed_in_user", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    activity.record("command", command="info")
    assert activity.flush(0.5) >= 0


def test_the_queue_is_capped_and_drops_the_oldest(signed_in, monkeypatch):
    monkeypatch.setattr(activity, "MAX_EVENTS_PER_HOUR", 10_000)
    words = sorted(activity.KNOWN_COMMANDS)
    for i in range(activity.QUEUE_MAX_EVENTS + 120):
        activity.record("command", command=words[i % len(words)])
    rows = queued()
    assert len(rows) == activity.QUEUE_MAX_EVENTS
    assert rows[-1]["detail"]["command"] == words[(activity.QUEUE_MAX_EVENTS + 119) % len(words)]
    assert rows[0]["detail"]["command"] == words[120 % len(words)]  # the oldest 120 are gone
    path = SessionStore().directory / "activity-queue.jsonl"
    assert path.stat().st_size <= activity.QUEUE_MAX_BYTES


def test_the_byte_cap_holds_too(signed_in, monkeypatch):
    monkeypatch.setattr(activity, "QUEUE_MAX_BYTES", 3000)
    monkeypatch.setattr(activity, "MAX_EVENTS_PER_HOUR", 10_000)
    for _ in range(100):
        activity.record("module_run", module="m" * 60)
    assert (SessionStore().directory / "activity-queue.jsonl").stat().st_size <= 3000


def test_a_runaway_script_cannot_flood(signed_in):
    for _ in range(activity.MAX_EVENTS_PER_HOUR + 200):
        activity.record("command", command="info")
    assert len(queued()) == activity.MAX_EVENTS_PER_HOUR


# -- sending ------------------------------------------------------------------------------------


def test_flush_sends_with_the_users_own_token_and_empties_the_queue(signed_in):
    for name in ("info", "run", "export"):
        activity.record("command", surface="cli", command=name)
    assert activity.flush(5.0) == 3
    assert queued() == []
    assert [r["detail"]["command"] for r in signed_in.activity_rows] == ["info", "run", "export"]
    assert signed_in.activity_tokens[0] == f"Bearer {SessionStore().load().access_token}"
    assert_clean_rows(signed_in.activity_rows)
    assert all("uid" not in r for r in signed_in.activity_rows)  # the database sets user_id itself


def test_network_errors_are_silent_and_the_events_are_kept_for_later(signed_in):
    signed_in.activity_mode = "unreachable"
    activity.record("command", surface="cli", command="info")
    assert activity.flush(2.0) == 0  # no exception
    assert len(queued()) == 1
    signed_in.activity_mode = "ok"
    assert activity.flush(2.0) == 1
    assert queued() == []


def test_an_expired_token_waits_instead_of_losing_events(signed_in):
    signed_in.activity_mode = "unauthorized"
    activity.record("command", surface="cli", command="info")
    assert activity.flush(2.0) == 0
    assert len(queued()) == 1


def test_a_batch_the_database_refuses_is_dropped_not_retried_forever(signed_in):
    signed_in.activity_mode = "bad_request"
    activity.record("command", surface="cli", command="info")
    assert activity.flush(2.0) == 0
    assert queued() == []


def test_events_of_another_account_are_never_sent_under_this_one(signed_in):
    path = SessionStore().directory / "activity-queue.jsonl"
    foreign = {**activity.build_event("command", "cli", command="info"), "uid": "someone-else"}
    path.write_text(json.dumps(foreign) + "\n")
    activity.record("command", surface="cli", command="export")
    assert activity.flush(2.0) == 1
    assert [r["detail"]["command"] for r in signed_in.activity_rows] == ["export"]
    assert queued() == []


def test_batches_are_at_most_100(signed_in, monkeypatch):
    monkeypatch.setattr(activity, "MAX_EVENTS_PER_HOUR", 10_000)
    sizes = []
    original = signed_in.request

    def watch(method, url, **kw):
        if url.endswith("activity_events"):
            sizes.append(len(kw["body"]))
        return original(method, url, **kw)

    monkeypatch.setattr("engine.account.http.request_json", watch)
    for _ in range(250):
        activity.record("command", command="info")
    assert activity.flush(5.0) == 250
    assert sizes == [100, 100, 50]


def test_flush_is_bounded_in_time(signed_in, monkeypatch):
    for _ in range(5):
        activity.record("command", command="info")

    def slow(method, url, **kw):
        time.sleep(0.3)
        raise_unreachable()

    def raise_unreachable():
        from engine.account.errors import AccountServerError

        raise AccountServerError(0, "unreachable")

    monkeypatch.setattr("engine.account.http.request_json", slow)
    started = time.monotonic()
    activity.flush(0.5)
    assert time.monotonic() - started < 2.0
    assert len(queued()) == 5


def test_the_background_thread_sends_without_being_asked(signed_in, monkeypatch):
    monkeypatch.setattr(activity, "FLUSH_INTERVAL_S", 0.05)
    activity.set_background(True)
    monkeypatch.setattr(activity, "_thread", None)
    activity.record("command", surface="cli", command="info")
    deadline = time.monotonic() + 5
    while not signed_in.activity_rows and time.monotonic() < deadline:
        time.sleep(0.02)
    assert [r["detail"] for r in signed_in.activity_rows] == [{"command": "info"}]
    assert any(t.name == "radarx-activity" and t.daemon for t in threading.enumerate())


def test_the_recorder_does_not_block_the_caller(signed_in):
    started = time.monotonic()
    for _ in range(100):
        activity.record("command", command="info")
    assert (
        time.monotonic() - started < 2.0
    )  # local file appends only; no network on the caller's thread


# -- the three surfaces, one code path ----------------------------------------------------------


def test_cli_records_the_command_name_and_flushes_before_exit(signed_in, capsys):
    assert cli.main(["modules", "--json"]) == 0
    capsys.readouterr()
    rows = signed_in.activity_rows
    assert_clean_rows(rows)
    assert ("command", "cli", {"command": "modules"}) in [
        (r["event"], r["surface"], r["detail"]) for r in rows
    ]


def test_cli_login_logout_whoami_events(gate_on, capsys, monkeypatch):
    import getpass

    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: "correct horse 1")
    assert cli.main(["login", "--email", "ada@example.test"]) == 0
    assert cli.main(["whoami"]) == 0
    assert cli.main(["logout"]) == 0
    capsys.readouterr()
    events = [(r["event"], r["detail"].get("command")) for r in gate_on.activity_rows]
    assert ("sign_in", None) in events and ("sign_out", None) in events
    # (nothing is recorded for the login command itself: nobody is signed in yet when it starts)
    assert ("command", "whoami") in events and ("command", "logout") in events
    assert_clean_rows(gate_on.activity_rows)


def test_cli_error_records_only_the_class(signed_in, capsys, tmp_path):
    secret = tmp_path / "ada_private_KTLX_storm.nc"
    assert cli.main(["info", "-i", str(secret)]) == 3
    capsys.readouterr()
    rows = signed_in.activity_rows
    assert any(r["event"] == "error" for r in rows)
    assert_clean_rows(rows, "ada_private", "KTLX", "storm", str(tmp_path))
    err = next(r for r in rows if r["event"] == "error")
    assert err["detail"] == {"class": "XrdInputError"}


def test_no_command_line_argument_value_is_recorded(signed_in, capsys, tmp_path):
    cli.main(["info", "-i", str(tmp_path / "KTLX_secret.gz"), "--json"])
    cli.main(["run", "radarx_hid", "-i", "KTLX_secret.gz", "-P", "x=35.4,-97.5"])
    capsys.readouterr()
    assert_clean_rows(signed_in.activity_rows, "KTLX", "secret", "35.4", "97.5")


def test_api_records_open_run_export_with_family_algorithm_and_format_only(
    signed_in, tmp_path, monkeypatch
):
    from engine.reader import RadarReader

    real_open = RadarReader.open_file

    def fake_open(self, path):
        if path.endswith("KTLX_secret_site.syn"):
            self._dtree = make_synthetic_datatree(n_azimuth=30, n_range=40)
            self._format = "nexrad_level2"
            self._path = path
            return self._build_schema(self._dtree, "nexrad_level2")
        return real_open(self, path)

    monkeypatch.setattr(RadarReader, "open_file", fake_open)
    secret = tmp_path / "KTLX_secret_site.syn"
    secret.write_text("x")
    vol = api.open(secret)
    vol.export("csv", tmp_path / "out_for_ada.csv", variable="DBZH")
    activity.flush(5.0)
    rows = signed_in.activity_rows
    kinds = [(r["event"], r["surface"], r["detail"]) for r in rows]
    assert ("file_opened", "api", {"format": "NEXRAD"}) in kinds
    assert ("export", "api", {"format": "csv"}) in kinds
    assert_clean_rows(rows, "KTLX", "secret", "out_for_ada", str(tmp_path))


def test_api_module_run_reports_the_built_in_name(signed_in):
    vol = api.Volume.from_tree(make_synthetic_datatree(n_azimuth=30, n_range=40))
    vol.run("storm_detection", 0, threshold_dbz=40)
    with pytest.raises(api.XrdArgumentError):
        vol.run("no_such_module_for_ada")  # a failed call is not logged as a run
    activity.flush(5.0)
    names = [
        r["detail"].get("module") for r in signed_in.activity_rows if r["event"] == "module_run"
    ]
    assert names == ["storm_detection"]
    assert_clean_rows(signed_in.activity_rows, "ada")


async def test_websocket_flows_record_features_only(signed_in, tmp_path):
    import server
    from engine import plugins
    from tests.test_account_server import Client, session_msg

    plugins.discover_modules()  # the server does this at start-up
    client = Client()
    path = make_netcdf_for(tmp_path / "ada_secret_hurricane.nc")
    await client.call(session_msg(signed_in, device_id="abcdef1234567890", reason="sign_in"))
    await client.call({"type": "open_file", "path": str(path)})
    out = tmp_path / "exported_for_ada"
    await client.call(
        {
            "type": "export",
            "format": "csv",
            "path": str(out) + ".csv",
            "variable": "DBZH",
            "sweep": 0,
        }
    )
    await client.call({"type": "run_module", "module": "storm_detection", "sweep": 0, "params": {}})
    await client.call(
        {"type": "run_module", "module": "no_such_module_for_ada"}
    )  # an error reply, not an event
    await client.call({"type": "auth_signout"})
    server._file_readers.clear()
    await _drain()
    activity.flush(5.0)
    rows = signed_in.activity_rows
    events = [r["event"] for r in rows]
    assert {"app_start", "sign_in"} <= set(events[:3]), (
        events
    )  # (recorded on worker threads: order may differ)
    assert "file_opened" in events and "sign_out" in events and "export" in events
    assert [r["detail"] for r in rows if r["event"] == "module_run"] == [
        {"module": "storm_detection"}
    ]
    assert [r["detail"] for r in rows if r["event"] == "file_opened"] == [{"format": "CfRadial"}]
    assert [r["detail"] for r in rows if r["event"] == "export"] == [{"format": "csv"}]
    assert all(r["surface"] in ("app", "engine") for r in rows)
    assert_clean_rows(rows, "ada_secret", "hurricane", "exported_for_ada", str(tmp_path))


async def test_websocket_errors_record_the_class_only(signed_in, monkeypatch):
    import server
    from tests.test_account_server import Client, session_msg

    client = Client()
    await client.call(session_msg(signed_in))

    async def boom(self, msg):
        raise ValueError("could not read /Users/ada/secret/KTLX20130520.gz")

    monkeypatch.setattr(server.ConnectionHandler, "_handle_list_modules", boom)
    await client.call({"type": "list_modules"})
    await _drain()
    activity.flush(5.0)
    errors = [r for r in signed_in.activity_rows if r["event"] == "error"]
    assert (
        errors
        and errors[0]["detail"] == {"class": "ValueError"}
        and errors[0]["surface"] == "engine"
    )
    assert_clean_rows(signed_in.activity_rows, "secret", "KTLX", "/Users")


async def test_web_service_events_stay_in_memory_and_use_the_visitors_token(signed_in):
    from tests.test_account_server import session_msg
    from tests.web_helpers import WebClient, close_session

    signed_in_dir = SessionStore().directory
    SessionStore().clear()
    web_client = WebClient()
    try:
        message = session_msg(signed_in, reason="sign_in")
        await web_client.call(message)
        await web_client.call({"type": "list_modules"})
        await _drain()
        assert not (signed_in_dir / "activity-queue.jsonl").exists()  # never written on the server
        assert not (signed_in_dir / "device_id").exists()
        activity.flush(5.0)
    finally:
        close_session(web_client.ctx)
    rows = signed_in.activity_rows
    assert {"app_start", "sign_in"} <= {r["event"] for r in rows}
    assert all(r["os"] == "Web" for r in rows)
    assert signed_in.activity_tokens and all(
        t == f"Bearer {message['access_token']}" for t in signed_in.activity_tokens
    )
    assert_clean_rows(rows)


# -- helpers ------------------------------------------------------------------------------------


def make_netcdf_for(path):
    from tests.web_helpers import make_netcdf

    return make_netcdf(path)


async def _drain():
    """Wait for the engine's recorder thread to finish what it was given."""
    import asyncio

    import server

    await asyncio.get_running_loop().run_in_executor(server._activity_pool(), lambda: None)
