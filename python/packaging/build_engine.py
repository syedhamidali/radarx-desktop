"""Build the self-contained engine with PyInstaller and smoke-test it.

    python packaging/build_engine.py            # build + smoke test
    python packaging/build_engine.py --no-smoke

Output: python/dist/xradar-engine/ (run ``xradar-engine --port N``). Needs ``pip install
pyinstaller`` and the engine's own dependencies in the same environment. PyInstaller does not
cross-compile: build on the OS you are targeting.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PYTHON_DIR = HERE.parent
DIST = PYTHON_DIR / "dist"
BUILD = PYTHON_DIR / "build"
EXE_NAME = "xradar-engine.exe" if os.name == "nt" else "xradar-engine"


def build() -> Path:
    for path in (DIST / "xradar-engine", BUILD):
        shutil.rmtree(path, ignore_errors=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--distpath",
            str(DIST),
            "--workpath",
            str(BUILD),
            str(HERE / "xradar_engine.spec"),
        ],
        cwd=PYTHON_DIR,
        check=True,
    )
    _remove_dangling_symlinks(DIST / "xradar-engine")
    _strip_macos(DIST / "xradar-engine")
    exe = DIST / "xradar-engine" / EXE_NAME
    if not exe.exists():
        raise SystemExit(f"build finished but {exe} is missing")
    return exe


def _remove_dangling_symlinks(root: Path) -> None:
    """Links to files the spec left out; Tauri refuses a resource folder that contains any."""
    for path in root.rglob("*"):
        if path.is_symlink() and not path.exists():
            print("removing dangling symlink", path.relative_to(root))
            path.unlink()


def _strip_macos(root: Path) -> None:
    """Drop local symbols from the bundled Mach-O libraries and sign them again (ad hoc).

    PyInstaller's own ``strip`` only removes debug symbols on macOS (-S) because the default
    breaks some libraries; ``-x`` (local symbols only, the exports stay) saves about 20 % on the
    big ones (icechunk, libarrow, libarrow_compute) and is checked by the smoke test below.
    """
    if sys.platform != "darwin":
        return
    before = sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink())
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        if not (path.suffix == ".so" or ".dylib" in path.name):
            continue
        subprocess.run(["strip", "-x", str(path)], check=False, capture_output=True)
        subprocess.run(
            ["codesign", "--force", "--sign", "-", str(path)], check=True, capture_output=True
        )
    after = sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink())
    print(f"stripped local symbols: {before / 1e6:.0f} -> {after / 1e6:.0f} MB")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def smoke_test(exe: Path, timeout: float = 120.0) -> None:
    """Start the frozen engine, wait for READY, then drive it through a real workflow."""
    port = _free_port()
    token = "smoke-test-token"
    env = dict(os.environ, XRADAR_SESSION_TOKEN=token)
    start = time.monotonic()
    proc = subprocess.Popen(
        [str(exe), "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    try:
        ready = False
        deadline = start + timeout
        assert proc.stdout is not None
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            print("  [engine]", line.rstrip())
            if line.startswith("READY:"):
                ready = True
                break
        if not ready:
            raise SystemExit(f"engine did not report READY (exit code {proc.poll()})")
        print(f"engine ready after {time.monotonic() - start:.1f}s")
        _check_engine(port, token)
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _synthetic_volume(path: Path) -> None:
    """Write a small group-structured NetCDF volume (uses the *build* environment's NetCDF libs)."""
    sys.path.insert(0, str(PYTHON_DIR))
    from tests.conftest import make_synthetic_datatree

    make_synthetic_datatree(n_azimuth=90, n_range=120).to_netcdf(str(path), engine="netcdf4")


def _check_engine(port: int, token: str) -> None:
    """Drive the frozen engine like the app: open a file, fetch a sweep, export, run a module."""
    import asyncio
    import json
    import tempfile

    import websockets

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        volume = Path(tmp) / "synthetic_cfrad.nc"
        _synthetic_volume(volume)

        async def request(ws, msg, want, timeout=120.0):
            """Send *msg*; return the first JSON reply of type *want* (raise on an error reply)."""
            await ws.send(json.dumps({**msg, "token": token}))
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                raw = await asyncio.wait_for(ws.recv(), timeout)
                if isinstance(raw, bytes):
                    continue
                reply = json.loads(raw)
                if reply.get("type") == want:
                    return reply
                if reply.get("type") in ("error", "module_error"):
                    raise SystemExit(f"{msg['type']} failed: {reply}")
            raise SystemExit(f"{msg['type']}: no {want!r} reply")

        async def run() -> None:
            async with websockets.connect(f"ws://127.0.0.1:{port}", max_size=None) as ws:
                modules = await request(ws, {"type": "list_modules"}, "modules_list")
                if len(modules["modules"]) < 5 or modules["errors"]:
                    raise SystemExit(f"plugin modules not bundled correctly: {modules['errors']}")
                print(f"  modules: {len(modules['modules'])}")

                opened = await request(
                    ws, {"type": "open_file", "path": str(volume)}, "file_opened"
                )
                file_id = opened["file_id"]
                print("  open_file ok")

                await request(
                    ws,
                    {"type": "get_sweep_data", "variable": "DBZH", "sweep": 0, "file_id": file_id},
                    "sweep_data",
                )
                print("  get_sweep_data ok")

                for fmt, ext in (("png", "png"), ("csv", "csv")):
                    out = Path(tmp) / f"out.{ext}"
                    done = await request(
                        ws,
                        {
                            "type": "export",
                            "format": fmt,
                            "path": str(out),
                            "variable": "DBZH",
                            "sweep": 0,
                            "dpi": 100,
                            "file_id": file_id,
                        },
                        "export_complete",
                    )
                    if not Path(done["path"]).exists():
                        raise SystemExit(f"{fmt} export reported {done['path']} but it is missing")
                    print(f"  export {fmt} ok")

                await request(
                    ws,
                    {
                        "type": "run_module",
                        "module": "storm_detection",
                        "sweep": 0,
                        "params": {},
                        "file_id": file_id,
                    },
                    "module_result",
                )
                print("  run_module ok")

                await ws.send(
                    json.dumps({"type": "close_file", "file_id": file_id, "token": token})
                )

        asyncio.run(run())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-smoke", action="store_true")
    ap.add_argument("--smoke-only", action="store_true", help="test an existing build")
    args = ap.parse_args()
    exe_path = DIST / "xradar-engine" / EXE_NAME if args.smoke_only else build()
    if not args.no_smoke:
        smoke_test(exe_path)
    print("OK:", exe_path)
