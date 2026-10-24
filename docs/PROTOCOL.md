# Engine protocol (for maintainers)

> A maintainer page: it is not published (it is in `INTERNAL_PAGES` in `docs/conf.py`). It describes the
> WebSocket conversation between the app (or any client) and the Python engine (`python/server.py`) as it was
> **observed on a running engine**; anything not observed says so. The source is the reference: the handler
> table is in `ConnectionHandler.handle_message`.

## Transport

* One WebSocket per client, `ws://localhost:PORT`. The engine is started with `--port N` (the app picks the
  port) and prints `READY:N` on stdout when it listens. A busy port prints one `error:` line on stderr and exits
  with code 75. `--version` prints the version and exits.
* Text frames carry one JSON object each, `{"type": "...", ...}`; replies have a `type` too. Binary frames carry
  arrays (below). Incoming messages up to about 21 MiB are accepted; larger frames close the connection (1009).
* Anything that is not a JSON object, has no `type`, or has a `type` that is not a string gets
  `{"type": "error", "message": ...}`; an unknown type gets `Unknown message type: NAME`
  (while nobody is signed in the account gate answers first, see below).

## Who may connect

1. **Handshake** (`engine/local_access.py`): the `Host` header must name this computer and an `Origin`, when
   present, must be the app's own webview or its dev server; everything else gets HTTP 403 before a WebSocket opens.
2. **Session token**: an engine started by the app has `XRADAR_SESSION_TOKEN` set and every message must carry
   `"token": "<it>"`; otherwise `{"type": "error", "message": "Unauthorized WebSocket message"}`.
3. **Account gate** (`docs/ACCOUNTS.md`): without a signed-in account on the connection only `ping`,
   `get_web_info` and `auth_*` are answered. Any other known request gets
   `{"type": "auth_required", "reason": "no_session", "message": "You are not signed in.", "request_type": T,
   "request_id": ID-or-null}`. The client signs the connection in with
   `{"type": "auth_session", "access_token": "...", "reason": "start", "device_id": "..."}` and gets `auth_ok`
   (it carries `email` and `stale`).

## The core conversation (observed on a synthetic two-sweep file)

| Client sends | Engine replies |
|---|---|
| `{"type": "ping"}` | `{"type": "pong", "status": "ok", "file_open": false, "memory": {"open_mb": 0.0, "max_mb": N, "files": 0}, "engine_id": "..."}` |
| `{"type": "open_file", "path": "/abs/file.nc"}` | several `{"type": "progress", "percent": 5..100, "message": ...}`, then `{"type": "file_opened", "file_id": "<uuid>", "data": {...metadata...}, "provenance": {...}}` |
| `{"type": "get_sweep_data", "variable": "DBZH", "sweep": 0, "file_id": ID}` | `{"type": "sweep_data", "variable", "sweep", "file_id", "n_az", "n_range", "azimuth_bytes", "range_bytes", "values_bytes", "vmin", "vmax", "units", "max_range", "provenance", "chunks": K}` followed by K binary frames |
| `{"type": "close_file", "file_id": ID}` | `{"type": "file_closed", "file_id": ID}`; `ping` then reports `files: 0` and `file_open: false` |

* `file_id` may be left out of `get_sweep_data` while one file is open; `close_file` needs it
  (`close_file: missing or invalid 'file_id'` otherwise).
* **Binary layout** of a sweep: the binary frames, joined in order, hold the azimuth array (`azimuth_bytes`), the
  range array (`range_bytes`) and the values (`values_bytes`), all little-endian `float32`, in that order; the values
  are `n_az * n_range` long. Missing data are the sentinel `-9999.0` (from the code, not seen in the synthetic file). In the observed case (24 x 40) it was one 4096
  byte frame (96 + 160 + 3840).
* Errors are `{"type": "error", "message": ...}`.

Not documented here, because it was not observed for this page: the other request types (modules, mosaics,
exports, winds, environment, animation, uploads in web mode ...). Read the table in `handle_message`.

## Running the engine without the app, from a source checkout

```
cd python
RADARX_AUTH_DISABLED=1 python server.py --port 8765        # PowerShell: $env:RADARX_AUTH_DISABLED = "1"
```

`RADARX_AUTH_DISABLED=1` is the one development switch for the account gate. It is honoured **only when the
program is not frozen** (an installer's engine ignores it) and is meant for a local engine on `localhost`. Do not
use it anywhere reachable from other computers. There is no second switch (no `--dev` flag); a test that needs the
gate uses the `gate_on` fixture (a local test key and a mocked account server, no network). The same variable is
set for the whole Python test run in `tests/conftest.py`.

## The smoke test

`python/tests/test_engine_smoke.py` starts a real engine in the test process on a free port and talks to it over a
real WebSocket: ping, open a small synthetic file, request a sweep and check its size and values, close the file
and check the engine's memory report returns to its starting value, plus the clear errors for bad input and the
signed-out refusal. It needs no
download and no account server and takes a couple of seconds. It is part of the normal `pytest` run.
