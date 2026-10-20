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
  `xattr -dr com.apple.quarantine /Applications/radarx-desktop.app`.
- **Windows:** SmartScreen shows "Windows protected your PC": click More info -> Run anyway.
- **Linux:** `sudo apt install ./radarx-desktop_*.deb` (or `sudo dnf install ./*.rpm`); no warning.

## Known limits

- **Not signed** (see above). Signing would need an Apple Developer ID + notarization and a Windows code-signing certificate (Tauri reads them from
  the usual `APPLE_*` / `TAURI_SIGNING_*` secrets).
- **First launch is slow** (measured 20-70 s on macOS, see [PACKAGING_AUDIT.md](PACKAGING_AUDIT.md)): the OS checks the newly installed libraries.
  Later launches take about a second. The app waits up to 3 minutes for the engine.
- Linux ships `.deb` and `.rpm`. There is no AppImage: linuxdeploy fails on the bundled Python libraries.
- Linux installers are built on Ubuntu 22.04 and need glibc >= 2.35 (Ubuntu 22.04+, Debian 12+, Fedora 36+).
