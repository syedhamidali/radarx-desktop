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
pip install -e ".[radarx,bundle,dev]"          # radarx needs Python >= 3.12
python packaging/build_engine.py           # -> python/dist/xradar-engine/, then smoke-tests it

cd ..
npm ci
# macOS
CI=true npm run tauri build -- --config src-tauri/tauri.bundle.macos.conf.json --bundles app,dmg
# Linux / Windows
npm run tauri build -- --config src-tauri/tauri.bundle.conf.json --bundles deb,rpm   # Linux
npm run tauri build -- --config src-tauri/tauri.bundle.conf.json --bundles nsis,msi       # Windows
```

PyInstaller cannot cross-compile, so each OS is built on its own runner; the **Release** workflow
(`.github/workflows/release.yml`) does all of them: macOS (Apple silicon and Intel), Linux and Windows.
Push a tag such as `v0.2.0` to get a draft GitHub release with the installers attached, or run the
workflow by hand to download them as artifacts.

`CI=true` on macOS makes the dmg step skip the Finder-based window styling (which needs a GUI session).
The bundle configs are separate files because the engine only exists after `build_engine.py`; putting it in
`tauri.conf.json` would break `cargo check` and `tauri dev`.

## What `build_engine.py` checks

It starts the frozen engine, waits for `READY:<port>`, then drives it like the app does: lists the plugin
modules, opens a volume, fetches a sweep, exports PNG and CSV, and runs a module. A build that cannot do
that fails.

## Size

| | unpacked | download (macOS dmg) |
|---|---|---|
| first working build | 771 MB | 299 MB |
| now | ~395 MB | ~156 MB |

How: the renderer rasterises with numpy, so datashader (and with it numba/llvmlite, ~130 MB) is no longer a dependency; unused
pyarrow components, other-service AWS models, test suites and headers are dropped (see the spec); and on
macOS the engine is added through `bundle.macOS.files`, which keeps symlinks (Tauri's `resources` copies
each symlink as a full file, ~140 MB of duplicates).

Where the rest is: pyarrow 83, NetCDF/HDF5 libraries 55, icechunk 38, scipy 36, rustytree 20, pyproj 17,
pandas 17 MB. Smaller still would mean dropping a feature (icechunk cloud archive) or a dependency.

## Installing the unsigned builds

The installers are not code-signed (no certificates), so the OS warns on first launch:

- **macOS:** open the dmg, drag the app to Applications, then right-click it -> Open -> Open. If macOS says the app
  is "damaged", run `xattr -dr com.apple.quarantine "/Applications/xradar desktop.app"` once.
- **Windows:** SmartScreen shows "Windows protected your PC": click More info -> Run anyway.
- **Linux:** `sudo apt install ./xradar-desktop_*.deb` (or `sudo dnf install ./*.rpm`); no warning.

## Known limits

- **Not signed** (see above). Signing would need an Apple Developer ID + notarization and a Windows code-signing certificate (Tauri reads them from
  the usual `APPLE_*` / `TAURI_SIGNING_*` secrets).
- **First launch is slow** (seconds to a minute on macOS): the OS scans the newly installed libraries.
  Later launches take about a second. The app waits up to 3 minutes for the engine.
- Linux ships `.deb` and `.rpm`. There is no AppImage: linuxdeploy fails on the bundled Python libraries.
- Linux installers are built on Ubuntu 22.04 and need glibc >= 2.35 (Ubuntu 22.04+, Debian 12+, Fedora 36+).
