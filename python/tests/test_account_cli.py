"""`rxd` and the account gate: refuses without a session (exit code 5), passes with one, and the
login / logout / whoami commands. Mocked account server, no real network (tests/conftest.py)."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

import pytest

from engine import cli
from engine.account import core
from engine.account.store import SessionStore
from tests.account_helpers import run_python

PYTHON_DIR = Path(__file__).resolve().parents[1]


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture(autouse=True)
def at_a_terminal(monkeypatch):
    """These tests are a person at a terminal; the no-terminal tests take it away again."""
    monkeypatch.setattr("engine.account.commands.stdin_is_a_terminal", lambda: True)


@pytest.fixture()
def typed_password(monkeypatch):
    """What the user types at the hidden password prompt; records that getpass was used."""
    calls: list[str] = []

    def fake_getpass(prompt="Password: ", stream=None):
        calls.append(prompt)
        return "correct horse 1"

    monkeypatch.setattr(getpass, "getpass", fake_getpass)
    return calls


def test_exit_code_is_5_and_documented():
    assert cli.EXIT_ACCOUNT == 5
    assert "5 not signed in" in cli.build_parser().epilog
    assert "5 no signed-in account" in cli.__doc__


GATED = [
    ["info", "-i", "x.nc"],
    ["modules"],
    ["modules", "list"],
    ["pipeline", "list"],
    ["run", "radarx_hid", "-i", "x.nc"],
    ["plot", "-i", "x.nc"],
    ["check", "-i", "x.nc"],
    ["cells", "-i", "x.nc"],
    ["export", "-i", "x.nc", "-o", "y.nc"],
    ["mosaic", "-i", "a.nc", "b.nc"],
    ["winds", "-i", "a.nc", "b.nc"],
    ["qpe", "-i", "a.nc"],
    ["basin", "-i", "a.nc", "-b", "b.geojson"],
    ["indicators", "-i", "a.nc", "--guidance", "g.nc"],
    ["sounding", "-i", "s.csv"],
    ["animate", "-i", "a.nc"],
]


@pytest.mark.parametrize("argv", GATED, ids=lambda a: " ".join(a))
def test_every_working_command_refuses_without_an_account(gate_on, capsys, argv):
    code, out, err = run(capsys, *argv)
    assert code == 5, (out, err)
    assert out == ""
    assert "not signed in" in err
    assert "rxd login" in err  # the way out, in plain words
    assert "Traceback" not in err


def test_every_registered_command_is_either_gated_or_exempt(gate_on, capsys):
    from engine.account import commands

    for name in cli.COMMANDS:
        if name in commands.EXEMPT:
            continue
        assert cli._account_gate(argparse.Namespace(command=name)) == 5, (
            name
        )  # nothing slips through
    capsys.readouterr()


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["--version"],
        ["completion", "bash"],
        ["examples"],
        ["examples", "mosaic"],
        ["logout"],
        ["whoami"],
        [],
    ],
    ids=lambda a: " ".join(a) or "(no arguments)",
)
def test_exempt_commands_need_no_account(gate_on, capsys, argv):
    code, out, err = run(capsys, *argv)
    expected = 5 if argv == ["whoami"] else 0  # whoami reports "not signed in" with 5
    assert code == expected, (out, err)
    assert out or err


def test_with_a_session_commands_run(gate_on, capsys, typed_password):
    code, out, err = run(capsys, "login", "--email", "ada@example.test")
    assert code == 0, err
    assert "Signed in as ada@example.test" in out
    assert typed_password  # the password came from getpass
    code, out, err = run(capsys, "modules", "--json")
    assert code == 0, err
    assert json.loads(out)  # the algorithm list
    code, out, err = run(capsys, "info", "-i", "/does/not/exist.nc")
    assert code == 3  # past the gate: now an ordinary input error, not an account error


def test_login_never_takes_the_password_from_the_command_line(gate_on, capsys, typed_password):
    parser = cli.build_parser()
    login = cli._subparsers(parser)["login"]
    options = {o for a in login._actions for o in a.option_strings}
    assert not any("pass" in o for o in options)
    code, out, err = run(capsys, "login", "--password", "hunter2")
    assert code == 2 and "hunter2" not in err + out  # unknown option, and it is not echoed back


def test_password_is_not_echoed_stored_or_printed(gate_on, capsys, typed_password):
    code, out, err = run(capsys, "login", "--email", "ada@example.test")
    assert code == 0
    assert "correct horse" not in out + err
    text = "".join(
        p.read_text(errors="ignore") for p in SessionStore().directory.glob("*") if p.is_file()
    )
    assert "correct horse" not in text
    assert typed_password == ["Password (not shown): "]


def test_login_asks_for_the_email_when_left_out(gate_on, capsys, typed_password, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "ada@example.test")
    code, out, _err = run(capsys, "login")
    assert code == 0 and "ada@example.test" in out


def test_login_failure_is_friendly(gate_on, capsys, monkeypatch):
    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: "wrong password 9")
    code, _out, err = run(capsys, "login", "--email", "ada@example.test")
    assert code == 1
    assert "do not match" in err and "Traceback" not in err
    assert not SessionStore().path.exists()
    gate_on.mode = "unreachable"
    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: "correct horse 1")
    code, _out, err = run(capsys, "login", "--email", "ada@example.test")
    assert code == 1 and "Cannot reach the account server" in err


def test_whoami_and_logout(gate_on, capsys, typed_password):
    run(capsys, "login", "--email", "ada@example.test")
    code, out, _err = run(capsys, "whoami")
    assert code == 0 and "ada@example.test" in out and "valid until" in out
    code, out, _err = run(capsys, "whoami", "--json")
    info = json.loads(out)
    assert code == 0 and info["signed_in"] is True and info["email"] == "ada@example.test"
    code, out, _err = run(capsys, "logout")
    assert code == 0 and "Signed out" in out
    code, out, _err = run(capsys, "logout")
    assert code == 0 and "not signed in" in out
    code, out, _err = run(capsys, "whoami", "--json")
    assert code == 5 and json.loads(out) == {"signed_in": False, "reason": "no_session"}
    code, out, _err = run(capsys, "modules")
    assert code == 5


def test_whoami_says_when_it_works_offline(gate_on, capsys, typed_password):
    run(capsys, "login", "--email", "ada@example.test")
    gate_on.mode = "unreachable"
    gate_on.now += 5 * 86400
    core.reset_runtime_state()
    code, out, _err = run(capsys, "whoami")
    assert code == 0 and "offline" in out and "25 more day" in out


def test_after_30_days_offline_the_cli_asks_to_reconnect(gate_on, capsys, typed_password):
    run(capsys, "login", "--email", "ada@example.test")
    gate_on.mode = "unreachable"
    gate_on.now += 31 * 86400
    core.reset_runtime_state()
    code, _out, err = run(capsys, "modules")
    assert code == 5
    assert "30 days" in err and "rxd login" in err


def test_deprecated_xrd_alias_behaves_identically(gate_on, capsys, typed_password, monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
    assert cli.main_xrd(["modules"]) == 5
    err = capsys.readouterr().err
    assert "not signed in" in err and "rxd login" in err
    assert cli.main_xrd(["login", "--email", "ada@example.test"]) == 0
    assert cli.main_xrd(["modules", "--json"]) == 0
    assert cli.main_xrd(["completion", "bash"]) == 0


def test_the_command_name_in_messages_comes_from_prog(gate_on, capsys, monkeypatch):
    monkeypatch.setattr(cli, "PROG", "mytool")
    code, _out, err = run(capsys, "modules")
    assert code == 5 and "mytool login" in err and "rxd login" not in err


def test_real_process_exit_code_without_session(tmp_path):
    env = {
        "PYTHONPATH": str(PYTHON_DIR),
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "RADARX_CONFIG_DIR": str(tmp_path / "cfg"),
    }
    for module in ("engine", "rxd"):
        proc = run_python(["-W", "ignore", "-m", module, "modules"], env=env)
        assert proc.returncode == 5, proc.stderr
        assert "rxd login" in proc.stderr
    ok = run_python(["-W", "ignore", "-m", "engine", "--help"], env=env)
    assert ok.returncode == 0 and "exit codes" in ok.stdout


def test_bypass_variable_lets_the_cli_run_for_developers(tmp_path):
    env = {
        "PYTHONPATH": str(PYTHON_DIR),
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "RADARX_CONFIG_DIR": str(tmp_path / "cfg"),
        "RADARX_AUTH_DISABLED": "1",
    }
    proc = run_python(["-W", "ignore", "-m", "engine", "modules", "--json"], env=env)
    assert proc.returncode == 0, proc.stderr


# -- no terminal: a plain message and exit code 5, never "this looks like a bug" -----------------


def test_login_without_a_terminal_says_so_and_exits_5(gate_on, capsys, monkeypatch):
    asked: list[str] = []
    monkeypatch.setattr("engine.account.commands.stdin_is_a_terminal", lambda: False)
    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: asked.append("password") or "x")
    monkeypatch.setattr("builtins.input", lambda *a: asked.append("email") or "x")
    code, _out, err = run(capsys, "login", "--email", "ada@example.test")
    assert code == 5
    assert "no terminal" in err and "ssh -t" in err and "radarx-desktop app" in err
    assert "bug" not in err and "Traceback" not in err
    assert asked == []  # nothing was asked, nothing was read
    assert SessionStore().load() is None


def test_login_without_a_terminal_in_json_mode(gate_on, capsys, monkeypatch):
    monkeypatch.setattr("engine.account.commands.stdin_is_a_terminal", lambda: False)
    code, out, err = run(capsys, "login", "--json")
    assert code == 5
    assert json.loads(out) == {"signed_in": False, "error": "no_terminal"}
    assert "no terminal" in err


@pytest.mark.parametrize("where", ["email", "password"])
def test_end_of_input_at_a_prompt_is_a_cancel_not_a_bug(gate_on, capsys, monkeypatch, where):
    def end_of_input(*_a, **_k):
        raise EOFError

    if where == "email":
        monkeypatch.setattr("builtins.input", end_of_input)
        argv = ("login",)
    else:
        monkeypatch.setattr(getpass, "getpass", end_of_input)
        argv = ("login", "--email", "ada@example.test")
    code, _out, err = run(capsys, *argv)
    assert code == 5 and "cancelled" in err and "bug" not in err
    assert SessionStore().load() is None


def test_the_real_program_without_a_terminal_gives_the_same_answer(tmp_path):
    import subprocess

    env = {**__import__("os").environ, "RADARX_CONFIG_DIR": str(tmp_path), "PYTHONUTF8": "1"}
    env.pop("RADARX_AUTH_DISABLED", None)
    done = subprocess.run(
        [sys.executable, "-m", "engine", "login", "--email", "ada@example.test"],
        cwd=PYTHON_DIR,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 5, done.stderr
    assert "no terminal" in done.stderr and "bug" not in done.stderr
