# Stability: what was tried to break the app, what broke, what was fixed

This is the record of a fault-injection pass over the engine (`python/engine`, `python/server.py`),
the Rust shell that supervises it (`src-tauri/src/main.rs`) and the WebSocket client of the UI
(`src/lib/utils/websocket.ts`). The goal was to find crashes, hangs, leaks and unhelpful errors by
attacking the app, then fix the *causes*. Related: [RELIABILITY.md](RELIABILITY.md) (earlier
guard rails: module timeout, memory budget, open-file messages), [RESPONSIVENESS.md](RESPONSIVENESS.md)
(client-side queueing), [WEB.md](WEB.md) (public service).

Nothing here needed new data: damaged files were made from a real NEXRAD volume, a CfRadial1 file
and ODIM_H5 volumes that already sit on the author's disk (opened read-only, corrupted copies were
written elsewhere), the rest from the synthetic volumes in `python/tests/conftest.py`.

## Findings

| # | What happened | Severity | Fixed? |
|---|---|---|---|
| 1 | A plug-in module that calls `sys.exit()` (or raises `KeyboardInterrupt`) **killed the engine process**: the exception left the worker thread, asyncio re-raises `SystemExit` out of the event loop. | High | Yes |
| 2 | **After the engine crashed and the shell restarted it, the UI kept showing the old files.** Every request then failed with "Unknown or closed file_id"; nothing said the engine had restarted. | High | Yes |
| 3 | A gzipped NetCDF/HDF5 volume (`.nc.gz`, `.h5.gz`) opened but **every later data read failed with FileNotFound**: the decompressed copy was deleted right after opening while the backend re-opens it by name. | High | Yes |
| 4 | Export to a chosen path wrote **in place**: a full disk, a crash or a cancel left a truncated file there and **destroyed the previous good export** of the same name. | High | Yes |
| 5 | `get_histogram` / `get_statistics` accepted any bin count: `bins = 2e8` ran **513 s and used 3.8 GB** (`n_bins`: 53 s, then an error). | High | Yes |
| 6 | Cancelling an export, a mosaic build or a `process` (client disconnect) **released the lock while the worker thread kept running**; the next request started alongside it. radarx uses every core, so repeated cancel/retry stacks heavy jobs. | Medium | Yes |
| 7 | Web mode: a worker thread that finished after the visitor left **re-created the session folder** (with a partial export in it). Nothing removed it until the next server start plus one hour. A visitor could do this on purpose. | Medium | Yes |
| 8 | Two open files asked for the same variable and sweep, replies out of order: `sweep_data` carried no file id, so the UI could **paint file A's data over file B**. | Medium | Yes |
| 9 | JSON the parser rejects with `ValueError`/`RecursionError` (a 5000-digit integer, 100 000 nested arrays) got **no reply at all**; the request hung until the UI's timeout. | Medium | Yes |
| 10 | Rust shell: a restart that failed was retried **every 5 s forever** with no pause (a broken install burned CPU and spammed the log); if the old port was taken it never moved; after the UI's retries ran out it never reconnected even when the engine came back. | Medium | Yes |
| 11 | `float32` values near the type limit (3e38) overflowed the histogram arithmetic ("index 9223372036854775807 is out of bounds"). | Medium | Yes |
| 12 | NaN/inf ray angles or gate ranges: the PPI render failed with "cannot convert float NaN to integer" and the PNG/PDF export with a matplotlib `pcolormesh` message. The whole sweep was lost for one bad ray. | Medium | Yes |
| 13 | Zero-ray / zero-gate sweeps: "memoryview: cannot cast view with zeros in shape" (data), "index 0 is out of bounds" (figures). | Low | Yes |
| 14 | Messages: open failures showed xradar internals (`unsupported operand type(s) for +: 'NoneType' and 'int'`) and the file's full path again; a folder gave "Path is a directory ... : /full/path"; disk full / read-only / permission gave `I/O error in export: [Errno 28] ...`; reads of a damaged file gave "NetCDF: HDF error"; unexpected errors gave no next step. | Low | Yes |
| 15 | An empty destination (`path: ""`, `output_dir: ""`) wrote into the engine's working directory; an animation with `dpi` below 72 or a frame time over 65 535 ms died with a matplotlib / `struct` message. | Low | Yes |
| 16 | The engine can be killed by a **native crash** (HDF5/netCDF on a corrupt file) or hang with a **blocked event loop**. | Medium | Crash: not seen (see below), recovery is #2/#10. Hang: not detected, see "What remains". |

### How each was fixed

1. `plugins._call_module` turns `SystemExit` / `KeyboardInterrupt` / `GeneratorExit` raised by module
   code into a `ModuleError` ("tried to stop the program"). `ConnectionHandler._handle_message` has
   the same net for every other handler.
2. `pong` carries `engine_id` (random per engine process). The UI asks on every connect
   (`engineIdentity.ts`); a different id than before means the sidecar restarted: open files are
   forgotten (`clearAllFiles`) and a toast says what happened. Files are **not** re-opened
   automatically, because the file that killed the engine would kill it again. Web mode got the same
   clean-up for its "session was reset" case.
3. `RadarReader` keeps the decompressed copy while a NetCDF/HDF5 based tree reads from it
   (`_reads_lazily`) and removes it when the file is closed, replaced, the reader is collected, or at
   exit. NEXRAD volumes keep their own handle (and are the big ones), so those copies are still
   removed at once. In web mode the copy lives in, and goes away with, the session folder.
4. `exporter._output_target` writes `.xradar-<random>-<name>` next to the destination and
   `os.replace`s it when the export finished; any failure (or cancel) removes it and the old file
   stays. Folders (Zarr) are swapped the same way.
5. Bin counts are clamped to 1..10 000 (`statistics._clamp_bins`).
6. `server._run_holding` runs the work in the executor and releases the lock **from the worker
   thread** when it returns, whatever happened to the awaiting task. Used for export, batch export,
   animation, `process`, mosaic and multi-Doppler.
7. `SessionContext.track_job` registers every worker-thread job of a session (through the context
   executor); the folder is removed again when a job ends after the session was closed.
8. The `sweep_data` header names its file (`file_id`). `sweepMatch.findRequestForReply` picks the
   request of that file; a reply for a file that is no longer on screen is neither cached nor shown.
9. Parse errors of any kind get `Invalid JSON: ...`.
10. `monitor_step` (Rust): restart with `0, 5, 10, 20, 40, 60 s` pauses when the engine keeps dying
    soon after it starts or cannot be started, try another free port if the old one is taken, set the
    reason for `get_sidecar_error`, and emit `sidecar-ready` / `sidecar-error` so the UI reconnects
    at once. The UI keeps listening for `sidecar-ready` for the whole session.
11. Histogram data is converted to `float64` first.
12/13. The rasterizer and the figure exporter drop rays/gates without a position; empty sweeps raise a
    one-sentence `ValueError` (`reader.get_sweep_data`, `render_publication_figure`).
14. `server._failure_text` is the one place that turns an exception into the text for the user. It keeps
    the "... in `<message type>`" / "... for `<type>`" wording the UI uses to attribute a failure
    to its request. Reader open errors name the backend's own words for the *most likely* format only.
15. `_blank_to_none` in the export handlers; the animation handler checks `dpi` (72-600) and the frame
    time (50-60 000 ms) before starting.

## What was tried (all of it ran; the automated part is in `python/tests/test_stability_*.py`)

| Area | How | Result |
|---|---|---|
| Damaged files | ~185 variants of a NEXRAD volume (plain and gzipped), a CfRadial1 file and two ODIM_H5 volumes, each in its own process: truncated at 7 offsets, 6 random bit-flip sets, zeroed 4 KB blocks, smashed header; wrong extensions (`.png`, `.txt`, none, `.RAW`, `.uf`, ...); 0/1-byte and 100 KB-of-zeros files; empty / garbage / nested gzip; HTML and JSON saved as data; folder; symlink loop; dangling link; FIFO; no permission; 300-character name; unicode and spaces | No crash, no hang, no segfault, no leftover temp file. Truncated or flipped ODIM/CfRadial files often *open* (metadata is intact) and fail when the data is read: now a clear "damaged file" message (#14). Test equivalents: `test_stability_files.py` (synthetic NetCDF + the real KTLX sample when present). |
| Odd sweeps | 26 synthetic cases (NaN-only, all no-echo with NEXRAD encoding, one ray, two rays, sectors incl. wrap-around, duplicate and unsorted rays, NaN/inf angles, missing azimuth/range/elevation/DBZH, +-3e38 and inf, all-inf, float64, int16, 1 gate, 0 gates, 0 rays, negative and constant range, 0 sweeps, uneven sweeps, 1000x3000 gates) x every analysis handler x PNG/SVG/CSV/NetCDF export x every registered module | #11-#13 found. Everything else answers with a readable message; modules report "needs ZDR", "no valid reflectivity gates", "range must increase" ... instead of tracebacks. |
| Hostile messages | ~3 000 mutations: every key of about 40 valid messages replaced by `None`, `""`, 5 000 characters, -1, 2^63, 1e308, NaN, +-inf, bool, `[]`, `{}`, nested, `../../etc/passwd`, NUL | One reply per request (cancel messages are by design silent). Only `inf` -> int conversions and a few out-of-range animation values said "Unexpected": now "Invalid input" / a range message. |
| Resource bombs | bins 2e8, scatter subsample 1e9, 10000x10000 render, 600 dpi x 30 inch figure, 500^3 volume, 500-frame animation | #5. The rest finishes (6-56 s) or is user-chosen. |
| Request flood | 3 000 `get_sweep_data` + 1 000 `render` in 0.2 s on one socket | All answered in 5 s, memory flat, ping answered. The engine does not drop stale work itself (the client scheduler keeps 3 in flight). |
| Event-loop lag | ping every 50 ms while opening a file, rendering, volume, PNG 300 dpi, NetCDF export, cells, modules | Worst round trip 112 ms, except a 300 dpi PNG (0.45 s) and a NetCDF export (1.9 s; the C libraries hold the GIL). No handler blocks the loop. |
| Connection drop / reconnect | client aborts the socket with 30 sweep requests and an export in flight, 3 times; state after reconnect | Tasks cancelled, nothing leaked, files still open on reconnect (desktop). |
| Oversized messages | 22 MB and 64 MB frames | Connection closed with 1009, engine unaffected. |
| Engine killed | `SIGKILL` the engine 4 times with a file opening; start a new one on the same port at once | Rebinds immediately. New `engine_id` each time. Shell restart logic: cargo tests with a stub engine (restart, crash-loop backoff, failed-then-repaired restart). |
| Modules | raise, `sys.exit`, `KeyboardInterrupt`, `MemoryError`, hang (timeout 0.3 s), cooperative cancel, bad shape, all-inf (earlier tests + #1) | #1 only. |
| Export targets | read-only folder, disk full (simulated `ENOSPC` at the write call), cancel mid-write, same name twice, Zarr folder replace, batch with one bad variable | #4, #14. |
| Leaks | 50 open/render/close cycles per format (NEXRAD, NEXRAD.gz, CfRadial1, ODIM) on a real engine: RSS, descriptors, temp folder; 400 cycles of CfRadial1 | Descriptors flat (272-313 depending on the format), temp folder empty, RSS bounces 160-650 MB with no trend. Test: `test_stability_leaks.py` (50 cycles, 0 descriptors, +0.65 MB of Python memory). |
| Web mode | two sessions at once with the same file name, ids and a foreign `file_id`; 10 open/close sessions; session ends during an export; error texts scanned for server paths | #7. No cross-talk, no server paths in messages. |

## Run the manual parts

```bash
cd python
python tests/manual/ws_stress.py /path/to/volume.nc            # all scenarios against a real engine
python tests/manual/ws_stress.py /path/to/volume.nc kill leaks  # some of them
cd ../src-tauri && cargo test                                    # shell supervision (unix)
cd .. && npm run test:unit                                       # UI logic (engine identity, reply matching)
```

## Native crash in NetCDF files (SIGSEGV / "Bus error", engine gone, no traceback)

**Symptom.** Opening a small synthetic RHI NetCDF in the app: "Couldn't load DBZH sweep 0:
Connection lost", the engine process had vanished, nothing in the log. 4 of 6 runs of the repro
(`open_file` -> the prefetch that follows it -> `get_sweep_data`) died with exit code 139. Once a
"Bus error" on CI Ubuntu in `test_two_files_opening_at_once`.

**What it is not: a thread race.** The first hypothesis was that netCDF-C/HDF5 were entered by two
threads at once (the prefetch task vs a request thread). The `faulthandler` stack of the crash says
otherwise: only one worker thread existed, the event loop was idle in `select`, and the same crash
happens with everything on the main thread (`open_file` + `.values`, 5 of 12). It is a
use-after-free inside the C libraries (`H5T__conv_vlen` -> `H5VL__native_blob_specific`), reached
from `netCDF4.Dataset.__init__` while *re-opening* a file.

**Minimal trigger (pure netCDF4, no engine, no xarray):** the file has a variable-length string
variable (every `str` that xarray writes: `sweep_mode`, ...), and

1. handle A on the file is open,
2. handle B on the same file reads that string variable and is closed,
3. handle C is opened on the file -> crash, 8 times in 20 (netCDF4 1.7.4 / netCDF-C 4.9.3 /
   HDF5 1.14.6, the wheels in `python/.venv` on macOS arm64).

Not needed: threads. Not triggering: handle A on a *different* file, strictly sequential
open/read/close, reading only numeric variables. The netCDF4 1.7.5 wheels (HDF5 2.x) in the
Python 3.12 environment do not show it (100/100 and 30/30 clean even with the guard off), so what
you see depends on which wheels are installed.

**How the engine produced steps 1-3 by itself.** `open_file` tries formats in order. The failed
`cfradial1` attempt keeps its handle (A). `cfradial2` then opens the file (B), xradar builds its
tree and closes the file again (`with open_datatree(...)`), and the first data read re-opens it
(C), because the arrays are lazy. A second reader of the same file, or an export reading a file
that is open, does the same.

**Fix: `python/engine/hdf5_guard.py`** (installed when `engine.reader` is imported; the
installed-in-process effect is on xarray's `CachingFileManager`):

* *One handle per file.* Read-only managers of the same netCDF4 / h5netcdf file get the same cache
  key (xarray gave every manager a random id), so they share the handle. Closing it (explicitly by
  xradar, by garbage collection, or by cache eviction) makes each user re-open later, one after the
  other, never two at once. Writers are not shared.
* *One lock.* A re-entrant `LOCK` is installed as xarray's own netCDF-C and HDF5 locks (so every
  array read takes it) and as the lock of the managers; open, close and eviction take it too.
  Engine code that touches the libraries outside the managers uses `native_section()` or
  `@guarded`: opening a tree in `RadarReader`, the CfRadial1/CfRadial2 writers, the mosaic and
  multi-Doppler NetCDF writers, `Result.save` NetCDF, `plot_wind` and the beam-blockage DEM
  reader. That also covers the thread-safety half of the hypothesis: the wheels' libraries are not
  thread-safe, and reads, opens, closes and exports of NetCDF/HDF5 data now take turns.
* Large volumes stay lazy (no eager load), so memory behaviour is unchanged.
* `XRADAR_HDF5_GUARD=0` switches the patches off (for comparing; not for use).

**Why not the other designs.** (b) alone, one lock for opens and attributes, is in the fix but would
not have stopped the crash: the repro is single-threaded. (c) eager loading of small files would
hide the overlap for small files only, and large CfRadial files (100 MB+) are exactly where the
engine keeps handles for a long time.

**Results** (`python/tests/test_stability_native.py`, children are separate processes so a
segfault is a failed test):

| | before | after |
|---|---|---|
| reported repro, Python 3.14 venv | 21/40 died (other batches: 5/12, 2/8, 8/25) | 160/160 clean |
| same, Python 3.12 venv (guard on) | not reproducible there | 100/100 clean |
| pure netCDF4/xarray overlap scenarios | 8/20 and 3/20 died | 30/30 clean |
| 8 threads on 3-4 files, 3-6 s per run, 8 runs | 8/8 runs died (3 files) | 8/8 clean (4 files) |
| the four new subprocess tests with the guard off | 4/4 fail | 4/4 pass |

**If the engine still dies natively.** The engine now enables `faulthandler`
(`server.enable_crash_log`): the Python stack of every thread goes to
`~/.xradar-desktop/engine-crash.log` (web mode: stderr; the file is cut at 512 KB). The shell
logs *how* the process ended in plain words ("crashed (segmentation fault, signal 11)") and puts
it into the restart message; the UI's "engine restarted" notice (see #2 above) now says the engine
crashed and where the log is. Files are not re-opened automatically.

**Limits.** The guard serialises NetCDF/HDF5 work: a long NetCDF export blocks reads of
NetCDF-backed sweeps until it ends (NEXRAD volumes are not affected, they do not use the lock).
Do not wait for dask workers while holding the lock (the engine does not open files with dask
chunks). The guard patches xarray internals (`CachingFileManager`, `FILE_CACHE`); on an xarray
that changes them it logs a warning and does nothing. It cannot fix a library bug in code the
engine does not control (for example a future h5py/HDF5 crash in another reader); the crash log
is how that would show up. Newer netCDF4 wheels may fix the root bug, but the guard is cheap and
stays.

## What remains (honest list)

* **Cancel stops waiting, not working.** A cancelled open/export/module keeps running in the engine
  until it ends (only multi-Doppler and pipelines have a real cancel message). `_run_holding` now
  stops the pile-up, but a long job still occupies a thread, and there is no `cancel_module`.
* **A hung engine is not detected.** The shell only notices a process that *exited*. A deadlocked or
  GIL-starved engine looks "connected" to the UI. A heartbeat (the UI pings; no pong in N seconds ->
  ask the shell to restart) would close this.
* **No memory guard for a module.** A `MemoryError` is reported nicely, but a module that allocates
  without bound can still exhaust the machine before it fails; a process-wide `RLIMIT_AS` is not safe
  for the shared engine. The timeout (300 s desktop, 120 s web) is the only limit. Moving modules into
  a child process would fix it and is a larger change.
* **A native crash still takes the engine down.** ~230 damaged files did not trigger one, but HDF5,
  netCDF4 and xradar are C/Python code reading untrusted bytes. The recovery path is #2/#10 (restart
  plus a message); the web service should keep running in a container as WEB.md says.
* The desktop engine has no per-connection limit on requests in flight (web mode has 16); stale
  requests are bounded by the UI's scheduler only.
* Cloud opens (`open_nexrad_l2`, realtime) have no overall timeout besides the ARCO one; not testable
  offline here.
* `purge_stale_dirs` deletes folders left by a *killed* web server only when they are over an hour
  old, so that several workers can share one folder.
* NetCDF export of a 12-sweep NEXRAD volume took 28 s and starves the loop (GIL) for up to 2 s. Not a
  stability bug; left to the speed work.
* Windows was not run (Rust supervision tests and the descriptor test are unix-only). The UI changes
  were verified by a production build and unit tests of their pure logic; they were not clicked
  through in the running app.
