# Engine reliability and speed

What the Python sidecar does so a bad file, a runaway module or a full memory
does not take the app down. Tests: `python/tests/test_reliability*.py`.

## 1. No-echo gates in exports

NEXRAD (and other integer-encoded) moments store "below threshold" (code 0) and
"range folded" (code 1) as ordinary numbers after decoding (-33 dBZ, -64.5 m/s).
`reader.mask_variable()` turns those into NaN. The viewer already used it; the
exporter now does too:

| Export path | Behaviour |
|---|---|
| PNG / SVG / PDF (`render_publication_figure`) | no-echo gates are NaN, so transparent; colour range comes from the real gates |
| CSV | no row for a no-echo gate |
| GIF animation | the shared colour range ignores no-echo gates |
| NetCDF / CfRadial2 / Zarr | unchanged on purpose: they write the raw tree so the file round-trips |
| GeoTIFF | still not implemented |

## 2. Running modules safely

`plugins.run_module` runs the module function in its own daemon thread and waits
for it.

* **Timeout**: `XRADAR_MODULE_TIMEOUT_S` (default `300`; `0` disables). On expiry the UI gets
  `module_error` with `"<Module> timed out after N s. ..."` and the server stays responsive.
  With "run on all sweeps" the limit applies to each sweep and the first timeout aborts the run.
* **Cancellation**: a Python thread cannot be killed. On timeout (or when the connection goes
  away) `ctx.cancelled` turns true and `ctx.progress()` / `ctx.check_cancelled()` raise
  `ModuleCancelled`, so a module that reports progress or polls the flag stops by itself. Whatever
  a late-finishing module returns is discarded. A module that never checks keeps its thread until
  it returns; after 3 such stuck runs new runs are refused with "Restart radarx-desktop".
  The thread is a daemon (it never blocks exit) and is not part of the shared executor pool.
* **Output checks**: every field must be numeric and have the sweep's `(n_azimuth, n_range)` shape;
  +/-inf become NaN (with a `warnings` entry in the result); a field that is only infinities, or
  a non-`Result` return value, is an error that says what to check.
* **Errors**: any exception becomes `module_error` of the form
  `"<Module> failed (ValueError: ...). Check the parameter values ..."` with a hint chosen from the
  exception type. The traceback goes to the sidecar log only.

## 3. Memory budget

`XRADAR_MAX_OPEN_MB` caps the decoded size of all open volumes (default 60 % of physical RAM
via `os.sysconf`, `GlobalMemoryStatusEx` on Windows; `0` disables). The size of a reader is
the sum of its array sizes (`RadarReader.memory_bytes()`; dask-backed arrays count as zero)
plus its derived variables. A NEXRAD volume is about 330 MB.

When a file is opened and the total would exceed the budget the engine

1. releases re-computable caches (rendered tiles, pixel-to-gate index maps, lazily fetched
   ARCO sweeps; derived variables have their own `XRADAR_DERIVED_MAX_MB` LRU and are kept),
2. opens the file anyway,
3. if the total is still over the budget, sends
   `{"type": "warning", "code": "memory_budget", "message": "Opening <file> brings ...", "file", "open_mb", "max_mb"}`.
   The frontend shows it as a toast.

`ping` -> `pong` now also carries `"memory": {"open_mb", "max_mb", "files"}` (additive; the Rust
`sidecar_health_check` only checks the process and is unchanged).

## 4. Speed

Measured on `KTLX20130520_201643_V06.gz` (9.5 MB, 331 MB decoded), Apple silicon, Python 3.14:

| | before | after |
|---|---|---|
| `import server` (sidecar start until the socket listens) | 0.79 s (median of 3) | 0.54 s |
| first `open_file` in a fresh process | 1.05-1.20 s | 0.64 s |
| first render | 0.14-0.17 s | 0.045 s |
| warm `open_file` | 0.52 s | 0.52 s |
| `get_sweep_data` DBZH | 17 ms decode + 10 ms mask | unchanged |

What changed:

* `matplotlib` is imported when the first export runs (`exporter._pyplot()`), and `colorcet`
  (which pulls in matplotlib) when the first tile is rendered (`renderer._palettes()`).
* After `READY` a background thread imports `xradar` and the palettes, so the first open and
  first render do not pay for them while the user is still choosing a file.

What was profiled and left alone: a warm open is 95 % inside xradar's NEXRAD backend
(`get_data_header` runs twice per open); `mask_variable` is 10 ms against a 17 ms decode, not
worth rewriting. `scipy` is imported by xradar itself, so making it lazy in the engine gains nothing.
The PyInstaller spec is unaffected: it already collects `colorcet`, and function-level imports are
found by its analysis.

## 5. Open-file errors

All open failures reach the UI as `open_file failed: <specific text>` (the prefix is what
`App.svelte` uses to attribute the error to the open). Cases:

| Situation | Message says |
|---|---|
| missing | file not found, may be moved/renamed or drive not connected |
| no read permission | permission denied, check permissions / macOS Files and Folders, or copy it |
| 0 bytes | file is empty, download or copy probably failed |
| truncated or corrupt `.gz` | gzip archive is truncated or corrupt, re-download (or wait for the download) |
| temp folder full | could not decompress into `<tmp>`, check free disk space |
| HTML/XML saved as data | it is a web page, the download probably failed |
| plain text | it is a text file, open the radar data file itself |
| known format, unreadable | looks like a `<format>` file but is probably truncated or corrupt |
| unknown binary | file type not recognised; lists supported formats |
| out of memory | close other files / applications |

The backend's own wording for the most likely format follows in parentheses (internal errors of
xradar such as `unsupported operand ... NoneType` are left out; every format's failure is in the
engine log). See [STABILITY.md](STABILITY.md) for what happens after a file opens but turns out to
be damaged, and for the other failure modes that were tried.
