# Packaging audit: what the installed app contains and why

Goal: a smaller installed app and a lighter build, with nothing in the bundle or the repository that the app does not use.
Everything below was measured on macOS (Apple silicon, Python 3.12.3, PyInstaller 6.22) before and after; the Linux and Windows
numbers will differ in absolute terms but the same files are removed.

## Result

| | before | after |
|---|---|---|
| Engine bundle (`python/dist/xradar-engine`, unpacked) | 346 MB, 888 files | 220 MB, 745 files |
| macOS app (`.app`, engine + binary + frontend) | 361 MB | 229 MB |
| macOS dmg, same compression for both (`hdiutil UDZO`) | 159 MB | 115 MB |
| macOS dmg as Tauri builds it | ~156 MB (documented) | 101 MB |
| Tauri binary (`target/release/radarx-desktop`) | 13.8 MB | 7.7 MB |
| Installed Python packages in the build environment | 97 | 70 |
| Warm start, process start to `READY:<port>` | 0.78-0.90 s | 0.70-0.85 s (noise level; the machine was loaded) |
| Cold start (first launch of a fresh copy) | 21-71 s | 22-71 s (no reliable change) |
| Test suite | 1425 passed, 6 skipped | 1408 passed, 7 skipped (the 17 Arrow-bridge tests skip without pyarrow) |

Cold start on macOS is dominated by the operating system checking each library of a newly written app the first time it is
loaded; it varied between 21 s and 71 s for the same bundle. Removing files that are never loaded cannot shorten it. What would
is loading fewer libraries at startup: at `READY` the engine has 154 libraries mapped, 43 of them pandas and 22 scipy (pulled in
by the xarray/xradar imports), which belongs to the engine start-up work, not to packaging.

Not measured: Linux and Windows installer sizes (needs those OSes; the release workflow builds them).

## What was removed, and why that is safe

### Python dependencies (`python/pyproject.toml`)

Method: grep of every import in `engine/`, `modules/`, `server.py`, `xrd/`; the dependency tree of a clean environment
(`uv pip tree`); the full test suite in an environment without the packages; the frozen engine driven against a real NEXRAD
volume.

| Package | Verdict | Evidence |
|---|---|---|
| `pycairo` | removed | No `cairo` import anywhere. SVG/PDF/PNG export is matplotlib. Also drops `libcairo` and its X11/xcb/pixman libraries from the bundle and the cairo system packages from CI, the release workflow, the Dockerfile and the README. |
| `s3fs` (and `aiobotocore`, `aiohttp`, `aioitertools`, `yarl`, `multidict`, ...) | removed | No import. S3 access is `boto3` (NEXRAD archive listing and download) and `icechunk` (ARCO store). |
| `fsspec` | no longer a direct dependency | No direct import; `dask` (required by xradar) still installs it. |
| `dask[complete]` | `dask` | `[complete]` adds `distributed`, `bokeh`, `jinja2`, `tornado`, `msgpack`, ... The engine never imports them (the spec already excluded them from the bundle). xradar requires plain `dask`. |
| `pyarrow` | optional extra `arrow` | Only `engine/arrow_bridge.py` imports it, and nothing imports that module: the WebSocket sends raw binary chunks, and `src/lib/utils/arrow.ts` is not imported by the frontend either. pandas treats pyarrow as optional. 55 MB unpacked. The suite passes without it. `pip install ".[arrow]"` brings it back. |
| kept: `xradar`, `xarray`, `numpy`, `scipy`, `pyproj`, `boto3`, `icechunk`, `zarr`, `pillow`, `matplotlib`, `colorcet`, `cmweather`, `websockets`, `radarx` | in use | Imported by the engine or the plugin modules; `radarx` is the algorithm library behind the `radarx_*` modules. |

Left alone on purpose:

- `netCDF4` + `h5py` (34 MB, two copies of HDF5): xradar, radarx and the NetCDF exporter need them.
- `cartopy` + `shapely` (about 5 MB): pulled in by radarx, only used by `radarx.vis.maxcappi`, which no module calls. Excluding
  them would save 5 MB but remove that radarx function from the app, so this is a decision for the owner, not an audit change.
- `icechunk` (32 MB): the NEXRAD cloud archive feature.

### PyInstaller bundle (`python/packaging/xradar_engine.spec`, `build_engine.py`)

| Change | Saves | Why safe |
|---|---|---|
| `pyarrow` excluded | 55 MB | See above. |
| test packages removed from the bytecode archive (`xarray.tests` etc.) | about 1.5 MB (compressed, estimate) | PyInstaller's package hooks collect whole packages; nothing imports `*.tests` at run time. `*.testing` modules (needed by libraries) are kept. |
| `setuptools`, `pkg_resources`, `pydoc_data` excluded | about 1 MB (estimate) | Dragged in by hooks; the engine does not use them. |
| matplotlib Qt/Tk/GTK/wx/WebAgg/nbAgg backends excluded | under 1 MB (estimate) | `exporter.py` forces `Agg`; they are GUI toolkits that are not installed anyway. |
| Pillow plugins `_avif`, `_webp`, `_imagingcms`, `_imagingtk` and `libavif`, `libwebp*`, `liblcms2` | about 5 MB (estimate) | The engine makes PNG/GIF; matplotlib writes PNG/SVG/PDF. Pillow imports its plugins in `try/except ImportError`. Nothing else links these libraries (checked with `otool -L`). |
| Computer Modern and DejaVu Serif fonts | about 1.5 MB (estimate) | Used only for `mathtext.fontset = 'cm'` and `font.family = serif`; the code sets neither. DejaVu Sans, Mono and STIX stay. |
| `__pycache__` / `*.pyc` data files | 0.4 MB | The developer's `python/modules/__pycache__` was being copied into the bundle. |
| identical copies of a library turned into symlinks | 3.8 MB at the time (pyarrow's three `libarrow_python` copies) | Done on the PyInstaller TOC, same folder only; with pyarrow gone it finds nothing today and guards against the next wheel that does this. |
| `strip` (Linux) and `strip -x` + ad-hoc re-sign (macOS, in `build_engine.py`) | 28 MB on macOS | Removes local symbols only; the exported symbols stay. PyInstaller's own macOS strip only removes debug symbols (and `-x` is its documented "breaks some libraries" case), so this is verified by the smoke test and a real-file run, not assumed. PyInstaller's own strip is switched on for macOS only: on Linux it corrupted numpy's bundled OpenBLAS and on Windows `python312.dll` (found by the first CI build of this change), so Linux and Windows are not stripped. |

A bug found on the way: SVG and PDF export **did not work in the packaged engine before this audit** (`No module named
'matplotlib.backends.backend_svg'`; matplotlib picks the backend by name at `savefig()` time). The four output backends are now
hidden imports, and the build's smoke test exports SVG and PDF as well.

Checked after the changes, with the packaged engine and a real file (`SEVERE_CASES_TEST/mayfield/KVWX/KVWX20211211_020418_V06`,
read only, outputs in a temporary folder): open, sweep data, export PNG, SVG, PDF, CSV, NetCDF and Zarr, and the modules
`storm_detection`, `radarx_clean`, `radarx_echo_mask`. The build's own smoke test (synthetic volume) also passes.

Also not done, deliberately: `upx` (breaks code signing, slows start), `optimize=2` (strips docstrings, which plugin modules and
some libraries read), `noarchive=True` (more files to scan on first launch).

### Rust / Tauri (`src-tauri/Cargo.toml`)

- `[profile.release]`: `opt-level = "s"`, `lto = true`, `codegen-units = 1`, `strip = true`. 13.8 MB to 7.7 MB. The shell only
  starts and watches a process, so speed is irrelevant.
- `panic = "abort"` is **not** set: the panic hook in `main.rs` stops the sidecar only for a main-thread panic; aborting on a
  panic in another thread would end the app and leave the sidecar running.
- `tauri` feature `tray-icon` removed (no tray code); `tokio` `full` reduced to `signal` + `macros` (only `tokio::signal` and
  `select!` are used); `rand 0.7` replaced by `getrandom` for the session token (one OS call, one dependency less).
- `tauri-plugin-shell` stays: the About dialog uses it to open links.

### Repository

- `src-tauri/gen/schemas/` (generated by `tauri-build` on every build) is no longer tracked and is ignored.
- `.gitignore` gains coverage output, `python/dist` / `python/build` and editor folders.
- Checked and kept: every image under `docs/images` is referenced by a document; `src-tauri/icons` is untouched (original icons).
  `public/favicon.png`, `src/lib/assets/icon.png` and `src-tauri/icons/icon-256.png` are byte-identical (54 KB each); the first two
  belong to the frontend and are left to that work.
- `@tauri-apps/plugin-shell` is used; `apache-arrow` (JS) is only imported by the unused `src/lib/utils/arrow.ts`, which can go
  together with the module (frontend change, not made here).

## How to reproduce the measurements

```bash
cd python
pip install -e ".[radarx,bundle,dev]"
python packaging/build_engine.py        # builds, strips, smoke-tests; prints the stripped size
du -sm dist/xradar-engine
# warm start: time from launching dist/xradar-engine/xradar-engine --port N to the READY:N line
```
