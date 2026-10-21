# Building the installers

The installers are self-contained: the Python engine (xradar, xarray, radarx, matplotlib, ...) is frozen
with PyInstaller and shipped inside the app, so nobody has to install Python or anything else.

```
Tauri app (Rust + Svelte)  ──starts──▶  xradar-engine  (frozen Python + libraries, in the app's resources)
```

An installed app starts `resources/engine/xradar-engine`; in development (no packaged engine present) it
starts `python/.venv` + `server.py` as before. `XRADAR_PYTHON=/path/to/python` forces the development path.

## Build locally (on the OS you are targeting)

```bash
cd python
pip install -e ".[radarx,bundle,dev]"          # radarx needs Python >= 3.12; no system libraries needed
python packaging/build_engine.py           # -> python/dist/xradar-engine/, then smoke-tests it

# The packaged engine requires a signed-in account and ignores the development bypass, so the smoke test
# checks the closed gate and the packaged sign-in check offline. To also run the full workflow (open, render,
# export, module), give it a test account's access token: RADARX_SMOKE_ACCESS_TOKEN=... python packaging/build_engine.py

cd ..
npm ci
# macOS
CI=true npm run tauri build -- --config src-tauri/tauri.bundle.macos.conf.json --bundles dmg
# Linux / Windows
npm run tauri build -- --config src-tauri/tauri.bundle.conf.json --bundles deb,rpm   # Linux
npm run tauri build -- --config src-tauri/tauri.bundle.conf.json --bundles nsis       # Windows
```

PyInstaller cannot cross-compile, so each OS is built on its own runner; the **Release** workflow
(`.github/workflows/release.yml`) does all of them: macOS (Apple silicon and Intel), Linux and Windows.
Push a tag such as `v0.2.0` to get a draft GitHub release with the installers attached, or run the
workflow by hand to download them as artifacts. When the signing key secret exists, the macOS and Windows jobs also
produce the signed update files and `latest.json` (see [UPDATES.md](UPDATES.md)); the macOS job builds `app,dmg` for that.
A last job copies the draft release to the download host as a candidate; publishing means running the **Promote release**
workflow ([RELEASING.md](RELEASING.md)).

`CI=true` on macOS makes the dmg step skip the Finder-based window styling (which needs a GUI session).
The bundle configs are separate files because the engine only exists after `build_engine.py`; putting it in
`tauri.conf.json` would break `cargo check` and `tauri dev`.

## The renderer files (`frontend`)

Movies (`rxd animate`, the Movie Studio) are drawn by the app's own 3D renderer in headless Chrome, which needs the
built frontend as real files (`dist/` with `index.html` and the storm-clip renderer). The webview loads the copy
embedded in the executable, so the bundle configs also ship `dist/` as a resource next to the engine:

| | engine | frontend |
|---|---|---|
| macOS (`bundle.macOS.files`) | `Radarx.app/Contents/Resources/engine` | `Radarx.app/Contents/Resources/frontend` |
| Windows (`bundle.resources`) | `<install dir>\engine` | `<install dir>\frontend` |
| Linux deb/rpm (`bundle.resources`) | `/usr/lib/Radarx/engine` | `/usr/lib/Radarx/frontend` |

`npm run tauri build` runs `npm run build` first, so `dist/` exists before the bundler copies it. The shell sets
`RADARX_APP_DIST=<resources>/frontend` when it starts the packaged engine (unless the variable is already set), and
`engine/animate_render.py::frontend_candidates` also looks next to a frozen engine (`<resources>/engine/..` ->
`<resources>/frontend`) and in the usual install locations, for an `rxd` installed with pip. Order: `--app-dist`,
`RADARX_APP_DIST`, `XRADAR_APP_DIST`, next to the frozen engine, the source checkout's `dist/`, `./dist`, the installed
apps. Cost: `dist/` is 2.5 MB (146 files), about 0.9 MB compressed in an installer.

The optional `video` extra is **not** frozen into the engine: Playwright is ~133 MB unpacked (its Node driver 117 MB)
on top of the 220 MB engine, and the spec excludes the packages even when the building environment has them. Instead
it comes as the **movie components pack** below. Google Chrome is always the system one.

**No video library ships, anywhere.** The film is encoded inside the headless Chrome that draws it: the clip page
(`src/lib/utils/clipEncoder.ts`) encodes every frame with the browser's WebCodecs `VideoEncoder` (H.264) and
`AudioEncoder` (AAC) and muxes them with the vendored `mp4-muxer` (MIT, `src/lib/vendor/`); where a Chromium has
no H.264 encoder it writes VP9 + Opus in WebM with the vendored `webm-muxer`. PyAV (whose PyPI wheels bundle an
FFmpeg built `--enable-gpl` with x264 / x265, incompatible with the PolyForm-Noncommercial licence) is never in an
installer, never in the pack and never a hard dependency: `build_movie_pack.py` refuses a pack that contains `av`
or an FFmpeg library, and the engine uses PyAV only when the user installed it themselves (`--encoder pyav`, or
the automatic fallback of a browser that cannot encode). The H.264 and AAC codecs are the browser vendor's.

## The movie components pack

A zip per OS / architecture, built by `scripts/release/build_movie_pack.py` and attached to every release
(`radarx-movie-pack-<version>-<os>-<arch>.zip`, about 42 MB compressed; see [RELEASING.md](RELEASING.md) for the
index and the download host). The installed app offers it in the Movie Studio with a consent card (what, from where,
how big, licences), downloads it with `engine/movie_pack.py` (https from `downloads.labshope.com` only, size and
SHA-256 checked against the release index, zip-slip and symlinks refused, every file verified against the pack's
`manifest.json` before the folder is renamed into place, the previous pack kept until then), keeps it under
`<user config dir>/radarx-desktop/movie-pack/<version>/` and puts that folder on `sys.path` before `playwright`
is imported (`animate_render.have_module`). Nothing from the pack is executed except by importing that
library. Settings › Data lists it with Remove.

```bash
python scripts/release/build_movie_pack.py --out dist/movie-pack          # with the engine's Python!
```

What the script does: `pip install --target` of the exact pins in `PINS` (playwright, greenlet, pyee,
typing_extensions; `--no-deps --only-binary=:all:`, `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1`: Playwright's own browsers
are never downloaded, the pack relies on the system Chrome), strips what is never loaded (tests, `.pyi`/`.pxd`
stubs, bytecode caches, Playwright's trace viewer and recorder UIs, `RECORD`), refuses `av` / FFmpeg libraries,
copies every component's licence texts into `licenses/`, writes `manifest.json` (schema, version, `os`, `arch`,
`python` = the interpreter's `cache_tag` such as `cpython-312`, components with versions and licences, every file
with size and SHA-256), zips deterministically (executable bits kept for the Node driver) and writes the sidecar
`radarx-movie-pack-<version>-<os>-<arch>.manifest.json` (the manifest without the file list plus the zip's size and
SHA-256) that `build_index.py` reads.

**The Python tag must match the frozen engine's** (greenlet is a compiled extension built for one CPython minor
version): the Release workflow builds the pack on the same four runners with the same `python-version` as the
engine jobs, and the engine refuses a pack whose `python` differs from its own. Measured on macOS arm64 (Python
3.14): 41.6 MB compressed, 133.0 MB unpacked, 179 files; playwright 132.5 MB (its Node driver 117 MB), greenlet
0.3 MB, the rest under 0.3 MB.

Environment overrides for tests and dry runs: `RADARX_MOVIE_PACK_DIR` (the folder), `RADARX_MOVIE_PACK_BASE_URL`
(another host, e.g. a local `http://127.0.0.1:port` standing in for the download host), `RADARX_MOVIE_PACK_ENABLED=1`
(treat a non-frozen engine like the installed app).

## What `build_engine.py` checks

It starts the frozen engine, waits for `READY:<port>`, then drives it like the app does: lists the plugin
modules, opens a volume, fetches a sweep, exports PNG and CSV, and runs a module. A build that cannot do
that fails.

## Size

| | unpacked engine | macOS app | download (macOS dmg) |
|---|---|---|---|
| first working build | 771 MB | | 299 MB |
| before the packaging audit | 346 MB | 361 MB | ~156 MB |
| now | 220 MB | 229 MB | ~101 MB |

How: the renderer rasterises with numpy, so datashader (and with it numba/llvmlite, ~130 MB) is not a dependency; pyarrow
(55 MB, only the unused `engine/arrow_bridge.py` needs it), pycairo, s3fs/aiohttp and `dask[complete]` are no longer
installed; test suites, bytecode caches, unused Pillow plugins, matplotlib GUI backends and fonts, other-service AWS models and
headers are dropped (see the spec); local symbols are stripped from the libraries; and on macOS the engine is added through
`bundle.macOS.files`, which keeps symlinks (Tauri's `resources` copies each symlink as a full file, ~140 MB of duplicates).
The audit, with the numbers and the reasons each item is safe, is in [PACKAGING_AUDIT.md](PACKAGING_AUDIT.md).

Where the rest is: icechunk 32, scipy 31, NetCDF/HDF5 libraries 34 (netCDF4 + h5py), pyproj 18, pandas 16, radarx 14,
matplotlib 12 MB, and the 28 MB executable (the compressed Python modules). Smaller still would mean dropping a feature
(icechunk cloud archive) or a dependency.

## Installing the unsigned builds

The installers are not code-signed by a registered developer (no certificates). The macOS app is ad-hoc signed
(`signingIdentity: "-"`), which makes the bundle valid but not trusted, so the OS warns on first launch:

- **macOS:** open the dmg and drag the app to Applications. On first launch macOS says it "cannot be opened" or
  "could not verify" it. On macOS 15 and later, right-click -> Open no longer works: go to **System Settings ->
  Privacy & Security**, scroll down to the message about radarx-desktop and click **Open Anyway**. Or, in Terminal:
  `xattr -dr com.apple.quarantine /Applications/Radarx.app`.
- **Windows:** SmartScreen shows "Windows protected your PC": click More info -> Run anyway.
- **Linux:** `sudo apt install ./Radarx_*.deb` (or `sudo dnf install ./*.rpm`); no warning.

## Known limits

- **Not signed** (see above). Signing would need an Apple Developer ID + notarization and a Windows code-signing certificate (Tauri reads them from
  the usual `APPLE_*` / `TAURI_SIGNING_*` secrets).
- **First launch is slow** (measured 20-70 s on macOS, see [PACKAGING_AUDIT.md](PACKAGING_AUDIT.md)): the OS checks the newly installed libraries.
  Later launches take about a second. The app waits up to 3 minutes for the engine.
- Linux ships `.deb` and `.rpm`. There is no AppImage: linuxdeploy fails on the bundled Python libraries.
- Linux installers are built on Ubuntu 22.04 and need glibc >= 2.35 (Ubuntu 22.04+, Debian 12+, Fedora 36+).
