"""``rxd login --password-stdin`` and ``rxd login --browser``, and the messages about which program
renews a sign-in. Mocked account server, no real provider, no real account, no real network."""

from __future__ import annotations

import contextlib
import getpass
import io
import json
import logging
import socket
import threading
import time
import urllib.request
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest

from engine import cli
from engine.account import commands, core, oauth_login
from engine.account.store import SessionStore, StoredSession
from tests.account_helpers import fake_browser
from tests.gotrue_http import RotatingGoTrue

PASSWORD = "correct horse 1"


def run(capsys, *argv):
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture()
def no_prompt(monkeypatch):
    """Any attempt to prompt for the password fails the test (stdin mode must never prompt)."""

    def boom(*a, **k):
        raise AssertionError("getpass was called")

    monkeypatch.setattr(getpass, "getpass", boom)


def stdin(monkeypatch, text: str):
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def everything_on_disk() -> str:
    store = SessionStore()
    return "".join(p.read_text(errors="ignore") for p in store.directory.rglob("*") if p.is_file())


# -- --password-stdin ----------------------------------------------------------------------------


def test_password_stdin_signs_in_without_prompting(gate_on, capsys, monkeypatch, no_prompt):
    stdin(monkeypatch, PASSWORD + "\n")
    code, out, err = run(capsys, "login", "--email", "ada@example.test", "--password-stdin")
    assert code == 0, err
    assert "Signed in as ada@example.test" in out
    assert SessionStore().load() is not None
    assert gate_on.token_bodies[-1] == {"email": "ada@example.test", "password": PASSWORD}


@pytest.mark.parametrize("text", [PASSWORD, PASSWORD + "\n", PASSWORD + "\r\n"])
def test_password_stdin_with_and_without_trailing_newline(gate_on, capsys, monkeypatch, text):
    stdin(monkeypatch, text)
    code, _out, err = run(capsys, "login", "--email", "ada@example.test", "--password-stdin")
    assert code == 0, err
    assert gate_on.token_bodies[-1]["password"] == PASSWORD  # exactly the line, nothing trimmed


def test_password_stdin_keeps_inner_and_trailing_spaces(gate_on, capsys, monkeypatch):
    gate_on.users["ada@example.test"] = " spaced  pass "
    stdin(monkeypatch, " spaced  pass \n")
    code, _o, err = run(capsys, "login", "--email", "ada@example.test", "--password-stdin")
    assert code == 0, err


def test_password_stdin_reads_one_line_only(gate_on, capsys, monkeypatch):
    buf = io.StringIO(PASSWORD + "\nsecond line\nthird\n")
    monkeypatch.setattr("sys.stdin", buf)
    code, _o, err = run(capsys, "login", "--email", "ada@example.test", "--password-stdin")
    assert code == 0, err
    assert buf.read() == "second line\nthird\n"  # the rest of the input was left alone


def test_password_stdin_empty_input_is_a_usage_error(gate_on, capsys, monkeypatch):
    for text in ("", "\n", "\r\n"):
        stdin(monkeypatch, text)
        code, out, err = run(capsys, "login", "--email", "ada@example.test", "--password-stdin")
        assert code == 2 and "no password" in err and out == "", (text, err)
    assert not SessionStore().path.exists()
    assert gate_on.token_bodies == []  # the server was never contacted


def test_password_stdin_very_long_line_is_refused_and_not_echoed(gate_on, capsys, monkeypatch):
    secret = "S3cr3t-" + "x" * 5000
    stdin(monkeypatch, secret + "\n")
    code, out, err = run(capsys, "login", "--email", "ada@example.test", "--password-stdin")
    assert code == 2 and "longer than" in err
    assert "S3cr3t" not in out + err
    assert gate_on.token_bodies == []
    # exactly at the limit is passed on (the server then decides)
    stdin(monkeypatch, "p" * commands.MAX_PASSWORD_CHARS + "\n")
    code, _o, err = run(capsys, "login", "--email", "ada@example.test", "--password-stdin")
    assert code == 1 and "do not match" in err


def test_password_stdin_without_a_newline_but_too_long_is_refused(gate_on, capsys, monkeypatch):
    stdin(monkeypatch, "q" * 3000)
    code, _o, err = run(capsys, "login", "--email", "ada@example.test", "--password-stdin")
    assert code == 2 and "longer than" in err


def test_password_stdin_needs_an_email(gate_on, capsys, monkeypatch):
    stdin(monkeypatch, PASSWORD + "\n")
    code, _o, err = run(capsys, "login", "--password-stdin")
    assert code == 2 and "--email" in err and PASSWORD not in err


def test_password_stdin_refuses_a_keyboard(gate_on, capsys, monkeypatch):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", Tty(PASSWORD + "\n"))
    code, _o, err = run(capsys, "login", "--email", "ada@example.test", "--password-stdin")
    assert code == 2 and "without it" in err  # points to the hidden prompt


def test_password_stdin_bytes_that_are_not_text(gate_on, capsys, monkeypatch):
    class Bad(io.StringIO):
        def readline(self, *a):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr("sys.stdin", Bad())
    code, _o, err = run(capsys, "login", "--email", "ada@example.test", "--password-stdin")
    assert code == 2 and "readable" in err


def test_a_wrong_password_on_stdin_is_a_friendly_failure(gate_on, capsys, monkeypatch):
    stdin(monkeypatch, "wrong password 9\n")
    code, out, err = run(capsys, "login", "--email", "ada@example.test", "--password-stdin")
    assert code == 1 and "do not match" in err and "Traceback" not in err
    assert "wrong password 9" not in out + err
    assert not SessionStore().path.exists()


def test_the_password_appears_nowhere_a_user_or_log_could_see_it(
    gate_on, capsys, monkeypatch, caplog
):
    """Output, errors, log records, the session folder (file, lock, device id, activity queue)
    and the --print-command line never contain the password."""
    caplog.set_level(logging.DEBUG)
    seen = []
    for text, extra in ((PASSWORD, ["--print-command"]), ("not the password 77", [])):
        stdin(monkeypatch, text + "\n")
        code, out, err = run(
            capsys, "login", "--email", "ada@example.test", "--password-stdin", *extra
        )
        seen += [out, err]
        assert code in (0, 1)
    seen.append(caplog.text)
    seen.append(everything_on_disk())
    seen.append(json.dumps(gate_on.activity_rows))
    joined = "\n".join(seen)
    assert PASSWORD not in joined and "not the password 77" not in joined
    # --print-command shows the equivalent command: with the flag, never with the password
    stdin(monkeypatch, PASSWORD + "\n")
    code, out, _e = run(
        capsys, "login", "--email", "ada@example.test", "--password-stdin", "--print-command"
    )
    assert code == 0 and "--password-stdin" in out and PASSWORD not in out


def test_the_options_never_carry_a_password_value():
    login = cli._subparsers(cli.build_parser())["login"]
    for action in login._actions:
        if "--password-stdin" in action.option_strings:
            assert action.nargs == 0  # a flag: it takes no value
    options = {o for a in login._actions for o in a.option_strings}
    assert "--password" not in options


def test_password_stdin_real_process_against_a_local_server(tmp_path):
    from tests.account_helpers import run_python

    with RotatingGoTrue() as server:
        env = {
            "PYTHONPATH": str(__import__("pathlib").Path(__file__).resolve().parents[1]),
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "RADARX_CONFIG_DIR": str(tmp_path / "cfg"),
            "RADARX_SUPABASE_URL": server.url,
            "RADARX_SUPABASE_KEY": "sb_publishable_test_only",
        }
        base = ["-W", "ignore", "-m", "engine"]
        who = run_python(
            [*base, "login", "--email", "ada@example.test", "--password-stdin"],
            env=env,
            input=PASSWORD + "\n",
        )
        assert who.returncode == 0, who.stderr
        assert PASSWORD not in who.stdout + who.stderr
        stored = (tmp_path / "cfg" / "session.json").read_text()
        assert (
            PASSWORD not in stored
            and oct((tmp_path / "cfg" / "session.json").stat().st_mode & 0o777) == "0o600"
        )
        empty = run_python(
            [*base, "login", "--email", "ada@example.test", "--password-stdin"], env=env, input=""
        )
        assert empty.returncode == 2 and "no password" in empty.stderr


# -- --browser -----------------------------------------------------------------------------------


def test_browser_login_signs_in_with_a_provider(gate_on, capsys, monkeypatch):
    hit = {}
    monkeypatch.setattr(oauth_login, "open_system_browser", fake_browser(gate_on, hit))
    monkeypatch.setattr(oauth_login, "has_display", lambda: True)
    code, out, err = run(capsys, "login", "--browser", "--provider", "google")
    assert code == 0, err
    assert "Signed in as ada@example.test" in out
    assert SessionStore().load().refresh_token  # a full session, renewable like a password login
    assert gate_on.pkce_codes == {}  # the code was spent
    url = urlparse(hit["url"])
    query = parse_qs(url.query)
    assert url.path.endswith("/auth/v1/authorize")
    assert query["provider"] == ["google"] and query["code_challenge_method"] == ["s256"]
    assert query["redirect_to"][0].startswith("http://127.0.0.1:")
    assert query["redirect_to"][0].endswith("/auth/callback")
    assert "code_verifier" not in hit["url"]  # the secret half never appears in the address
    assert "Waiting" in err and "same machine" in err and "rxd login --token" in err
    assert hit["url"] in err  # printed too, for a terminal where no browser opens


def test_the_verifier_is_secret_and_matches_the_challenge(gate_on, capsys, monkeypatch):
    hit = {}
    monkeypatch.setattr(oauth_login, "open_system_browser", fake_browser(gate_on, hit))
    monkeypatch.setattr(oauth_login, "has_display", lambda: True)
    run(capsys, "login", "--browser", "--provider", "google")
    verifier = gate_on.token_bodies[-1]["code_verifier"]
    assert 43 <= len(verifier) <= 128 and verifier not in hit["url"]
    verifier2, challenge2 = oauth_login.new_pkce()
    assert verifier2 != verifier and len(challenge2) == 43


def test_a_code_made_for_another_verifier_is_refused_and_stores_nothing(
    gate_on, capsys, monkeypatch
):
    """Login CSRF: somebody makes the browser deliver THEIR code to our listener. PKCE refuses it."""
    attacker_code = "attacker-code-1"
    gate_on.pkce_codes[attacker_code] = "challenge-of-someone-else"

    def opener(url):
        redirect = parse_qs(urlparse(url).query)["redirect_to"][0]
        threading.Thread(
            target=lambda: urllib.request.urlopen(f"{redirect}?code={attacker_code}").read(),
            daemon=True,
        ).start()
        return True

    monkeypatch.setattr(oauth_login, "open_system_browser", opener)
    monkeypatch.setattr(oauth_login, "has_display", lambda: True)
    code, _out, err = run(capsys, "login", "--browser", "--provider", "google")
    assert code == 1 and "did not accept" in err
    assert not SessionStore().path.exists()


def test_provider_text_is_never_shown(gate_on, capsys, monkeypatch):
    monkeypatch.setattr(
        oauth_login, "open_system_browser", fake_browser(gate_on, error="server_error")
    )
    monkeypatch.setattr(oauth_login, "has_display", lambda: True)
    code, out, err = run(capsys, "login", "--browser", "--provider", "google")
    assert code == 1 and "not completed" in err
    assert "PROVIDER" not in out + err and "TEXT" not in out + err
    monkeypatch.setattr(
        oauth_login, "open_system_browser", fake_browser(gate_on, error="access_denied")
    )
    code, _o, err = run(capsys, "login", "--browser", "--provider", "google")
    assert code == 1 and "cancelled" in err
    assert not SessionStore().path.exists()


def test_a_provider_that_is_not_switched_on_gets_a_friendly_message(gate_on, capsys, monkeypatch):
    opened = []
    monkeypatch.setattr(oauth_login, "open_system_browser", lambda url: opened.append(url) or True)
    code, _o, err = run(capsys, "login", "--browser", "--provider", "apple")
    assert code == 1 and "Apple is not switched on yet" in err
    assert "Google" in err and "Facebook" in err  # what is on
    assert "rxd login" in err and "Traceback" not in err
    assert opened == []  # no browser, no listener for a provider that cannot work


def test_browser_login_when_the_server_is_unreachable(gate_on, capsys):
    gate_on.mode = "unreachable"
    code, _o, err = run(capsys, "login", "--browser", "--provider", "google")
    assert code == 1 and "Cannot reach the account server" in err


def test_browser_login_option_checks(gate_on, capsys, monkeypatch):
    code, _o, err = run(capsys, "login", "--browser")  # not a terminal: needs --provider
    assert code == 2 and "--provider" in err
    code, _o, err = run(capsys, "login", "--browser", "--email", "a@b.c", "--provider", "google")
    assert code == 2 and "does not take" in err
    code, _o, err = run(capsys, "login", "--provider", "google")
    assert code == 2 and "go with --browser" in err
    code, _o, err = run(capsys, "login", "--browser", "--provider", "twitter")
    assert code == 2
    # a terminal: the provider is asked for
    hit = {}
    monkeypatch.setattr(oauth_login, "open_system_browser", fake_browser(gate_on, hit))
    monkeypatch.setattr(oauth_login, "has_display", lambda: True)
    monkeypatch.setattr("sys.stdin", type("T", (io.StringIO,), {"isatty": lambda s: True})())
    monkeypatch.setattr("builtins.input", lambda prompt="": "1")
    code, _out, err = run(capsys, "login", "--browser")
    assert code == 0 and "provider=google" in hit["url"], err


def test_nothing_opens_without_a_display_but_the_address_is_printed(gate_on, capsys, monkeypatch):
    opened = []
    monkeypatch.setattr(oauth_login, "open_system_browser", lambda url: opened.append(url) or True)
    monkeypatch.setattr(oauth_login, "has_display", lambda: False)
    monkeypatch.setattr(oauth_login, "WAIT_S", 0.4)
    code, _o, err = run(capsys, "login", "--browser", "--provider", "google")
    assert code == 1 and "Timed out" in err and opened == []
    assert "Open this address in a browser on THIS computer" in err
    assert "/auth/v1/authorize?" in err


def test_ssh_and_headless_linux_do_not_open_a_browser(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    for var in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
        monkeypatch.delenv(var, raising=False)
    assert oauth_login.has_display() is True
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
    assert oauth_login.has_display() is False
    monkeypatch.delenv("SSH_CONNECTION")
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert oauth_login.has_display() is False
    monkeypatch.setenv("DISPLAY", ":0")
    assert oauth_login.has_display() is True


def test_no_open_flag_prints_only(gate_on, capsys, monkeypatch):
    opened = []
    monkeypatch.setattr(oauth_login, "open_system_browser", lambda url: opened.append(url) or True)
    monkeypatch.setattr(oauth_login, "has_display", lambda: True)
    monkeypatch.setattr(oauth_login, "WAIT_S", 0.3)
    code, _o, err = run(capsys, "login", "--browser", "--provider", "google", "--no-open")
    assert code == 1 and opened == [] and "authorize?" in err


# -- the listener itself -------------------------------------------------------------------------


def listener_request(port, raw: bytes, *, read=True, timeout=5):
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(raw)
        if not read:
            return b""
        chunks = b""
        try:
            while True:
                part = s.recv(4096)
                if not part:
                    break
                chunks += part
        except OSError:
            pass
        return chunks


def get(port, target, host=None):
    host = host or f"127.0.0.1:{port}"
    return listener_request(port, f"GET {target} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())


def in_thread(listener):
    box = {}

    def work():
        try:
            box["result"] = listener.wait_for_redirect()
        except oauth_login.OAuthFailed as exc:
            box["error"] = str(exc)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    return t, box


def test_only_a_request_with_a_code_or_error_ends_the_wait():
    with oauth_login.LoopbackListener(wait=20) as listener:
        t, box = in_thread(listener)
        port = listener.port
        for target in (
            "/auth/callback",
            "/auth/callback?state=only",
            "/auth/callback?code=",
            "/auth/callback?xcode=1",
            "/favicon.ico",
            "/auth/callback/extra?code=abc",
            "/other?code=abc",
            "/auth/callback?code=has%20space",
            "/auth/callback?code=<script>",
        ):
            assert get(port, target).startswith(b"HTTP/1.1 404"), target
        assert listener_request(
            port, b"POST /auth/callback?code=abc HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % port
        ).startswith(b"HTTP/1.1 404")
        assert get(port, "/auth/callback?code=abc", host="evil.example").startswith(b"HTTP/1.1 404")
        assert get(port, "/auth/callback?code=abc", host=f"localhost:{port}").startswith(
            b"HTTP/1.1 404"
        )
        time.sleep(0.2)
        assert t.is_alive() and not box
        answer = get(port, "/auth/callback?code=REAL-1&state=whatever")
        assert answer.startswith(b"HTTP/1.1 200") and b"close this tab" in answer
        t.join(5)
        assert box["result"] == {"code": "REAL-1"}


def test_the_first_real_redirect_wins():
    with oauth_login.LoopbackListener(wait=20) as listener:
        t, box = in_thread(listener)
        get(listener.port, "/auth/callback?code=FIRST")
        t.join(5)
        with contextlib.suppress(OSError):
            get(listener.port, "/auth/callback?code=SECOND")
        assert box["result"] == {"code": "FIRST"}


def test_an_error_redirect_carries_only_the_code_not_the_text():
    with oauth_login.LoopbackListener(wait=20) as listener:
        t, box = in_thread(listener)
        reply = get(
            listener.port,
            "/auth/callback?error=access_denied&error_description=Please+visit+evil.example",
        )
        t.join(5)
        assert box["result"] == {"error": "access_denied"}
        assert b"evil" not in reply  # the page is fixed text


def test_idle_connections_do_not_delay_the_real_redirect():
    with oauth_login.LoopbackListener(wait=20) as listener:
        t, box = in_thread(listener)
        idle = [socket.create_connection(("127.0.0.1", listener.port)) for _ in range(5)]
        time.sleep(0.1)
        started = time.monotonic()
        get(listener.port, "/auth/callback?code=QUICK")
        t.join(5)
        assert box["result"] == {"code": "QUICK"}
        assert time.monotonic() - started < oauth_login.READ_TIMEOUT_S, "an idle socket delayed it"
        for s in idle:
            s.close()


def test_an_oversized_or_garbage_request_is_dropped():
    with oauth_login.LoopbackListener(wait=20) as listener:
        t, box = in_thread(listener)
        junk = b"GET /auth/callback?code=" + b"A" * 20000 + b" HTTP/1.1\r\nHost: x\r\n\r\n"
        with contextlib.suppress(OSError):
            listener_request(listener.port, junk)
        listener_request(listener.port, b"\x16\x03\x01garbage\x00\x00")
        time.sleep(0.2)
        assert t.is_alive() and not box


def test_the_wait_has_a_time_limit_and_closes_the_port():
    listener = oauth_login.LoopbackListener(wait=0.3)
    port = listener.port
    with pytest.raises(oauth_login.OAuthFailed, match="Timed out"):
        listener.wait_for_redirect()
    listener.close()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1)


def test_the_listener_binds_loopback_only():
    with oauth_login.LoopbackListener(wait=1) as listener:
        host, port = listener._sock.getsockname()
        assert host == "127.0.0.1" and port > 1023


def test_pkce_pair_follows_rfc7636():
    import base64
    import hashlib

    for _ in range(20):
        verifier, challenge = oauth_login.new_pkce()
        assert 43 <= len(verifier) <= 128 and set(verifier) <= set(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
        )
        digest = hashlib.sha256(verifier.encode()).digest()
        assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert len({oauth_login.new_pkce()[0] for _ in range(50)}) == 50


def test_parse_callback_rules():
    p = oauth_login.parse_callback
    ok = "GET /auth/callback?code=abc-123 HTTP/1.1\r\nHost: 127.0.0.1:4000\r\n\r\n"
    assert p(ok, 4000) == {"code": "abc-123"}
    assert p(ok, 4001) is None  # addressed to another port
    assert p(ok.replace("GET", "HEAD"), 4000) is None
    assert p(ok.replace("HTTP/1.1", "SPDY/3"), 4000) is None
    assert p("", 4000) is None
    both = "GET /auth/callback?code=a&error=access_denied HTTP/1.1\r\nHost: 127.0.0.1:4000\r\n\r\n"
    assert p(both, 4000) == {"code": "a"}


# -- who renews the sign-in (message flow) ----------------------------------------------------


def app_session(gate_on, *, hours_old):
    token = gate_on.keys.token(iat=gate_on.now - hours_old * 3600)
    store = SessionStore()
    store.save(
        StoredSession(
            access_token=token,
            refresh_token=None,
            user_id="11111111-2222-3333-4444-555555555555",
            email="ada@example.test",
            source="app",
            issued_at=gate_on.now - hours_old * 3600,
            high_water=gate_on.now,
        )
    )


def test_an_expired_app_session_says_exactly_what_to_do(gate_on, capsys):
    app_session(gate_on, hours_old=3)
    code, _o, err = run(capsys, "modules")
    assert code == 5
    assert "made by the radarx-desktop app" in err and "has expired" in err
    assert "open the radarx-desktop app" in err
    assert "rxd login" in err and "rxd whoami" in err
    core_message = None
    with pytest.raises(core.AccountRequired) as caught:
        core.check_stored_session()
    core_message = str(caught.value)
    assert caught.value.reason == "expired" and caught.value.app_owned
    assert "rxd login" not in core_message  # the program name is added by the caller (PROG)


def test_whoami_says_which_program_renews_the_sign_in(gate_on, capsys, monkeypatch):
    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: PASSWORD)
    run(capsys, "login", "--email", "ada@example.test")
    code, out, _e = run(capsys, "whoami")
    assert code == 0 and "made by 'rxd login'" in out and "renew it" in out
    info = json.loads(run(capsys, "whoami", "--json")[1])
    assert info["renewed_by"] == "rxd" and info["session"] == "cli"
    app_session(gate_on, hours_old=0)
    core.reset_runtime_state()
    code, out, _e = run(capsys, "whoami")
    assert (
        code == 0
        and "made by the radarx-desktop app" in out
        and "renews it while it is open" in out
    )
    info = json.loads(run(capsys, "whoami", "--json")[1])
    assert info["renewed_by"] == "app"


def test_whoami_offline_with_an_app_session_says_what_to_do_online(gate_on, capsys):
    app_session(gate_on, hours_old=3)
    gate_on.mode = "unreachable"
    core.reset_runtime_state()
    code, out, _e = run(capsys, "whoami")
    assert code == 0 and "offline" in out and "open the app" in out and "rxd login" in out


def test_http_errors_from_the_listener_use_a_plain_status_line():
    with oauth_login.LoopbackListener(wait=5) as listener:
        t, _box = in_thread(listener)
        with pytest.raises(HTTPError) as caught:
            urllib.request.urlopen(f"http://127.0.0.1:{listener.port}/nothing")
        assert caught.value.code == 404
        get(listener.port, "/auth/callback?code=done")
        t.join(5)
