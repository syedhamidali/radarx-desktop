# Web mode

The engine can run as a public web service (a browser is the client, nothing is
installed; a free radarx account is required, see *Accounts* below). The desktop engine trusts its caller; web mode assumes **every visitor is hostile** and
closes every door to the server: no server paths, no persistent state, no plug-in loading, nothing
that outlives the connection.

```
python server.py --web --host 0.0.0.0          # port from $PORT (Render) or --port N
XRADAR_WEB=1 python server.py --port 8765      # same thing via the environment
```

Desktop behaviour is unchanged when web mode is off (the default). Code: `python/engine/web.py`
(settings, sessions, path guard, uploads, downloads, HTTP routes) plus small hook points in
`python/server.py`. Tests: `python/tests/test_web_mode.py`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `XRADAR_WEB` | off | `1`/`true` enables web mode (CLI: `--web`). |
| `RADARX_WEB_REQUIRE_ACCOUNT` | **on** | A visitor must present a signed-in radarx account before the engine does any work (see *Accounts* below). `0` / `false` turns the gate off, for a private deployment. Ignored by a frozen (installer) build. |
| `PORT` | | Port when `--port` is not given (web mode only). |
| `--host` | `127.0.0.1` (web), `localhost` (desktop) | Interface to bind; Docker uses `0.0.0.0`. |
| `XRADAR_ALLOWED_ORIGINS` | *(empty)* | Comma-separated allowed `Origin`s (`https://app.example.com`). Empty = same-origin only. `*` allows every origin and is honoured **only if set explicitly**. |
| `XRADAR_MAX_UPLOAD_MB` | 200 | Largest single upload. |
| `XRADAR_MAX_SESSION_MB` | 600 | Disk quota per session (uploads + exports + scratch). |
| `XRADAR_MAX_SESSIONS` | 8 | Concurrent WebSocket connections; extra ones are refused. |
| `XRADAR_MAX_OPEN_FILES` | 3 | Open radar files per session (uploads and cloud opens). |
| `XRADAR_IDLE_TIMEOUT_S` | 1800 | No message other than `ping` for this long closes the session. |
| `XRADAR_DOWNLOAD_TTL_S` | 600 | Lifetime of a download link. |
| `XRADAR_WEB_TMP` | system temp | Parent of the per-session folders (`xradar-sess-*`). |
| `XRADAR_RATE_PER_S` / `XRADAR_RATE_BURST` | 100 / 200 | Per-connection message rate limit (token bucket). |
| `XRADAR_MODULE_TIMEOUT_S` | 120 in web mode | Wall-clock limit of one module run (existing setting). |
| `XRADAR_MAX_OPEN_MB` | derived | Decoded-data budget **per session**; a file that does not fit is refused (desktop only warns). Set it to roughly `RAM x 0.6 / concurrent sessions`. |

## Protocol

All messages are JSON text frames on the existing WebSocket. Raster/array payloads keep using the
existing binary frames. The web mode adds the messages below; every existing message still works
except that anything touching a file path follows the *path policy*.

### On connect

The server sends immediately (web mode only; desktop sends nothing new):

```json
{"type": "web_info", "web_mode": true,
 "limits": {"max_upload_mb": 200, "max_sessions": 8, "idle_timeout_s": 1800, "max_open_files": 3},
 "features": {"uploads": true, "cloud": true, "local_paths": false, "downloads": true}}
```

`{type: "get_web_info"}` returns the same message in web mode and `{type:"web_info", web_mode:false}`
on the desktop. If the server is full it sends
`{type:"error", code:"server_busy", message}` and closes with code 1013. A rejected `Origin` closes
with 1008 (and the HTTP upgrade is answered 403). No session token is used in web mode.

### Accounts (`auth_session`)

Web mode has the same account gate as the desktop engine (`docs/ACCOUNTS.md`), **on by default**. On every new
connection the browser signs in with Supabase (the web build shows the sign-in screen first) and presents the
access token:

```
client -> {type: "auth_session", access_token, reason: "start" | "sign_in" | "refresh" | "reconnect"}
server -> {type: "auth_ok", email, stale, grace_days_left}     | {type: "auth_required", reason, message}
client -> {type: "auth_signout"}                               # server -> {type: "auth_signed_out"}
client -> {type: "auth_status"}                                # server -> auth_ok | auth_required
```

Until the connection has an accepted account, every message except `ping`, `get_web_info` and `auth_*` is answered
`{type: "auth_required", reason, message, request_type, request_id}` and **no work happens** (an upload does not
start, no file is opened). The token is verified offline against the project's public key; the refresh token never
reaches the server, and the web engine never writes a session or an activity queue to disk. `/health` stays open.

**Behaviour change for a public demo:** before accounts existed, anyone who could reach the port could open a session;
now they need a radarx account. Set `RADARX_WEB_REQUIRE_ACCOUNT=0` to keep a deployment anonymous.

### Uploads

```
client -> {type: "upload_start", upload_id, filename, size}
server -> {type: "upload_ready", upload_id}            | {type: "upload_error", upload_id, message}
client -> {type: "upload_chunk", upload_id, seq, data} # seq starts at 0 and increases by 1
server -> {type: "upload_progress", upload_id, received}  | {type: "upload_error", ...}
client -> {type: "upload_end", upload_id}
server -> {type: "upload_complete", upload_id, path, size} | {type: "upload_error", ...}
client -> {type: "upload_abort", upload_id}            # optional, no reply
```

* `upload_id`: 1-64 characters of `A-Za-z0-9_-`, unique among *active* uploads (a second
  `upload_start` with the same id is refused and does not disturb the first; the id may be reused
  after the upload completed or failed).
* `size`: exact byte count (positive integer, at most `max_upload_mb`). `data` is base64 of at most
  1 MiB of raw bytes. Chunks must arrive in order; the server accepts pipelined chunks.
* Any protocol violation (bad base64, out-of-order or duplicate `seq`, more bytes than declared,
  quota exceeded) aborts that upload with `upload_error` and deletes the partial file. `upload_end`
  fails if received != declared size.
* At most 2 uploads in parallel, 200 per session, and `XRADAR_MAX_SESSION_MB` in total.
* `filename` is sanitised to `[A-Za-z0-9._-]` (everything else becomes `_`, leading dots dropped,
  directory parts removed, the extension kept for format sniffing).
* `path` in `upload_complete` is **opaque**, inside the connection's private session folder, and the
  only kind of path `open_file` accepts. The client then sends the normal `{type:"open_file", path}`.
* HDF5/NetCDF-4 uploads that reference data outside themselves (external links, external storage,
  virtual datasets) are rejected at `upload_end`.

### Downloads

Exports write inside the session and the success reply gains `download_url`; `path` is only the
**basename** in web mode:

| Request | Success reply (additions in web mode) |
|---|---|
| `export` | `export_complete {path, download_url, derived_exported}` |
| `batch_export` | `batch_export_complete {paths: [basename], download_urls: [url], count, output_dir: basename}` |
| `export_animation` | `animation_export_complete {path, download_url}` |
| `save_text_file` | `file_saved {path, download_url}` |
| `export_mosaic`, `export_multidoppler` | `file_saved {path, download_url}` |

Output names (`path`, `output_dir`) must be **bare file names** (no `/`, `\`, `:`, `..`, leading `.`
or `~`); anything else is refused with "Choose a file name only ...". Omit `path` for a generated
name. Existing names are never overwritten (`-2`, `-3`, ... is appended). Zarr (a folder) is
refused, `dpi` is limited to 300, batches to 60 files, animations to 60 frames, `render` to
4096 px, `save_text_file` to 5 MB.

`download_url` is relative, e.g. `/download/<token>/<filename>`; the client prefixes the engine's
HTTP origin (same host and port as the WebSocket, `http(s)` instead of `ws(s)`).

### HTTP routes (same port)

* `GET /health` -> `200 ok`.
* `GET /download/<token>/<filename>` -> the file with `Content-Disposition: attachment`,
  `Content-Length`, `X-Content-Type-Options: nosniff`, `Cache-Control: no-store` and
  `Access-Control-Allow-Origin` echoing the allowed origin. The token is 192 random bits, bound to
  the session that created it, expires after `XRADAR_DOWNLOAD_TTL_S` and disappears when the
  session ends. A wrong token, wrong filename or expired link is `404`.
* A request with a disallowed `Origin` is `403` (a WebSocket upgrade without any `Origin` too,
  unless `*` was configured). Any other path is `404`. The `websockets` parser only accepts `GET`;
  other methods (e.g. `HEAD`) never reach the router and get no response.

### Errors and limits

Errors use the usual `{type: "error", message}` (or the feature's own error type). Refused server
paths always say: *"Opening files from the server is disabled; upload a file or use the cloud tab"*
(the message never reveals whether a path exists). Over the rate limit: `{type:"error",
code:"rate_limited"}` (the message is dropped; a client that keeps hammering is disconnected);
more than 16 requests in flight: `code:"busy"`. Server paths inside messages are replaced by
`<session>`.

## Path policy

| Surface | Web mode |
|---|---|
| `open_file` | Only paths **inside the session folder** (real path compared, symlinks resolved, `..`, relative paths, `~`, URL schemes such as `file://`, `s3://`, `C:` and NUL rejected). |
| `export`, `batch_export`, `export_animation`, `save_text_file`, `export_mosaic`, `export_multidoppler` | Bare file/folder names inside `<session>/exports`; reply has `download_url`. |
| `open_nexrad_l2` | Only `s3://unidata-nexrad-level2/<key>` (or `noaa-nexrad-level2`); no other bucket, no credentials. |
| `open_nexrad_realtime`, `list_*`, `open_arco_scan` | Enabled (public NOAA data, anonymous). The ARCO scan key is validated. |
| Cloud downloads, gzip decompression | Scratch files live in the session folder; gzip inflation is capped at the session quota. |
| Module parameters named `*_path`, `*_file`, `*_dir` | Must be empty or inside the session (so a DEM can be uploaded); `model_path` is refused. |
| Plug-in modules | Only `python/modules`; `~/.xradar-desktop/modules` and `XRADAR_MODULES_DIR` are never read, `list_modules {reload}` is ignored, server paths are hidden. |
| Recipes | Kept in `<session>/recipes` (max 50), never in `~`; `XRADAR_RECIPES_DIR` is ignored. |
| Variable mappings | In memory per session; `variable_maps.json` is neither read nor written. |
| Exporter default folder (`~/Downloads/xradar-exports`) | Disabled. |

## Isolation

Each connection owns a private `0700` temp folder (`xradar-sess-*` under `XRADAR_WEB_TMP`) and a
private copy of all engine state: open files, derived variables, generations and cache keys,
renderer/processor/exporter, variable mappings, pipeline and multi-Doppler cancel flags, locks.
Mosaics and wind analyses live in shared LRU caches but are only reachable by the session that
created them and are discarded when it ends. On disconnect, idle timeout or server shutdown the
folder, readers, derived data and download tokens are removed; leftover folders from a crashed
process are purged at startup. Worker threads see the connection's state through a context-copying
executor; a request that somehow has no session raises instead of falling back to shared state.

## What is *not* covered (read before deploying)

* **Untrusted file parsers.** Uploads are parsed by xradar, netCDF4/h5netcdf/HDF5, zlib and friends,
  which are C libraries. A memory-corruption bug in one of them is exploitable by an upload. The
  Python layer cannot fix that: run the container as a non-root user, with a read-only root file
  system, no secrets and no outbound credentials, a small memory/CPU/pids limit and an ephemeral
  disk.
* **CPU and memory are shared.** Rate limits, per-session quotas, request caps and the per-session
  memory budget bound the damage, but one visitor can still use all CPU for the duration of a
  module run (`XRADAR_MODULE_TIMEOUT_S`; a timed-out module's thread cannot be killed). Put a
  reverse proxy / platform limit in front, and size `XRADAR_MAX_SESSIONS` for your RAM.
* **Downloads are read into memory** (up to the session quota) while being served.
* Authentication is the radarx account (`RADARX_WEB_REQUIRE_ACCOUNT`, default on; any valid free account passes
  today, not a per-user quota). With the gate switched off anyone who can reach the port can open a session: add
  auth at the proxy then. Origin checks protect against other *websites* using your service from a visitor's
  browser, not against scripted clients, which can send any `Origin`.
* The cloud tab makes outbound requests to NOAA/AWS from the server (anonymous, public buckets).
* TLS is the platform's job (`wss://` / `https://`).
