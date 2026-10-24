"""Engine start-up problems that must be one clear line, and a crash log that stays bounded."""

from __future__ import annotations

import faulthandler
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import server

PYTHON_DIR = Path(__file__).resolve().parents[1]


def test_a_port_that_is_taken_gives_one_clear_line_and_its_own_exit_code():
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        done = subprocess.run(
            [sys.executable, str(PYTHON_DIR / "server.py"), "--port", str(port)],
            cwd=PYTHON_DIR,
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
    assert done.returncode == server.EXIT_PORT_IN_USE, done.stderr[-400:]
    errors = [ln for ln in done.stderr.splitlines() if ln.startswith("error:")]
    assert len(errors) == 1 and str(port) in errors[0] and "--port" in errors[0]
    assert "Traceback" not in done.stderr and "READY" not in done.stdout


def test_another_os_error_is_not_hidden(monkeypatch):
    def boom(*_a, **_k):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(server, "_run_server", boom)
    monkeypatch.setattr(sys, "argv", ["server.py", "--port", "50000"])
    monkeypatch.setattr(server, "enable_crash_log", lambda: None)
    with pytest.raises(PermissionError):
        server.main()


def test_an_oversize_crash_log_is_rotated_not_deleted(tmp_path):
    log = tmp_path / "engine-crash.log"
    log.write_text("OLD CRASH REPORT\n" + "x" * 2000, encoding="utf-8")
    server.rotate_crash_log(log, max_bytes=1000)
    assert not log.exists()
    assert "OLD CRASH REPORT" in (tmp_path / "engine-crash.log.1").read_text(encoding="utf-8")


def test_the_log_stays_bounded_over_many_starts(tmp_path):
    log = tmp_path / "engine-crash.log"
    for _ in range(30):
        server.rotate_crash_log(log, max_bytes=1000)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("y" * 400 + "\n")
    sizes = [p.stat().st_size for p in tmp_path.iterdir()]
    assert len(sizes) <= 2 and sum(sizes) <= 2 * (1000 + 401)


def test_a_small_log_is_left_alone(tmp_path):
    log = tmp_path / "engine-crash.log"
    log.write_text("short\n", encoding="utf-8")
    server.rotate_crash_log(log, max_bytes=1000)
    assert (
        log.read_text(encoding="utf-8") == "short\n"
        and not (tmp_path / "engine-crash.log.1").exists()
    )


def test_enable_crash_log_rotates_in_the_users_folder(tmp_path, monkeypatch):
    folder = tmp_path / ".xradar-desktop"
    folder.mkdir()
    (folder / "engine-crash.log").write_text(
        "z" * (server._CRASH_LOG_MAX_BYTES + 10), encoding="utf-8"
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    path = None
    try:
        path = server.enable_crash_log()
        assert path == folder / "engine-crash.log"
        assert (folder / "engine-crash.log.1").stat().st_size > server._CRASH_LOG_MAX_BYTES
        assert "started" in path.read_text(encoding="utf-8")
    finally:
        faulthandler.disable()
        if server._crash_log_file is not None:
            server._crash_log_file.close()
            server._crash_log_file = None
        faulthandler.enable()
