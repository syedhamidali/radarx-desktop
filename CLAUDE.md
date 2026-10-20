# radarx-desktop

A cross-platform desktop (and web) tool for weather radar data: open a file, see it in 2D and 3D, clean it, run
algorithms, merge radars, export. Built with **Tauri 2** (Rust shell) + **Svelte 5** (frontend, its own WebGL2
renderers) + a **Python engine** (xradar / xarray / radarx) that the shell starts and talks to over a local
WebSocket. Everything the app does also runs without the GUI (`rxd` command line, `engine.api` in Python).

## Rules that apply to every change

- **Follow the science**: read `docs/SCIENCE.md` (provenance classes OBSERVED / MODEL-ANALYSIS / RETRIEVED /
  SIMULATED-STYLISED, cite methods, show limits, invent nothing, test against known answers, reproducible run
  records, downloads only with consent, stylised visuals labelled). The checklist is in the PR template.
- **Commits**: small and focused, **no Claude/Anthropic or other AI co-author/attribution trailers**; sign-off
  (`git commit -s`) is recommended for contributors (see `CONTRIBUTING.md`, `DCO`).
- **Licences**: project licence is PolyForm Noncommercial 1.0.0 (do not change it). New dependencies, fonts,
  colour maps, data and imagery go through `python scripts/licenses/generate_notices.py` (`--check` in CI) and
  `THIRD_PARTY_NOTICES.md`; see `docs/LICENSES.md`.
- **Look at it**: run the app and look at screenshots in dark and light before calling a visual change done.

## Architecture

See `ARCHITECTURE.md` for the system diagram, data flow and design decisions.

### Stack summary
- **Tauri 2** (`src-tauri/`): native window, starts and supervises the Python engine, file associations,
  signed auto-update (`updates.rs`); a few MB of Rust instead of a bundled browser
- **Svelte 5 + TypeScript** (`src/`): UI. **Own WebGL2 renderers** (`ppiRenderer.ts` for the 2D PPI,
  `cartesianRenderer.ts` / `Viewer3D` for the ray-marched 3D volume, `flowRenderer.ts` for wind flow), a
  tile-based map background (OpenStreetMap / OpenTopoMap, or none). No deck.gl, no Mapbox
- **Python engine** (`python/`): subprocess, WebSocket server `server.py`
  - **xradar** reads NEXRAD, ODIM, CfRadial, IRIS, Rainbow, Furuno, IMD, UF; **xarray** (+ dask) data model
  - **radarx** for algorithms (QC, hydrometeor classification, KDP, QPE, multi-Doppler, mosaics); optional
  - **matplotlib / Pillow** for figures and PNG/GIF export (`engine/exporter.py`, `engine/renderer.py`)
  - **Apache Arrow** is optional (`engine/arrow_bridge.py`, extra `arrow`): the app itself sends raw binary
    chunks over the WebSocket, so pyarrow is neither installed by default nor bundled
- **CLI and API**: `rxd` (`engine/cli.py`), `engine.api` / `import rxd`; docs `docs/CLI.md`, `docs/API.md`
- **Web mode**: the same engine as an anonymous web service (`docs/WEB.md`)

### Project structure
```
radarx-desktop/
├── src-tauri/          # Rust shell (main.rs: engine supervision; updates.rs: updater), icons, tauri*.conf.json
├── src/                # Svelte frontend
│   ├── App.svelte
│   └── lib/
│       ├── components/ # viewers (RadarViewer, Viewer3D, RhiViewer, ...), panels (Modules, Mosaic, Wind, ...)
│       ├── stores/     # reactive state (radarData, settings, volume3d, wind, updater, ...)
│       ├── utils/      # WebSocket client, renderers, colormaps, geometry (+ *.test.mjs unit tests)
│       ├── workers/    # binary decoding and mesh building off the main thread
│       └── styles/     # tokens, theme, bundled IBM Plex fonts
├── python/
│   ├── server.py       # WebSocket server entry
│   ├── engine/         # reader, volume, renderer, exporter, pipeline, mosaic, multidoppler, cells, rhi,
│   │                   # provenance, cfwrite, cli, api, web ...
│   ├── modules/        # plug-in algorithms (one file each; drop a file to add one)
│   ├── packaging/      # PyInstaller spec: the self-contained engine inside the installers
│   └── tests/
├── docs/               # Sphinx sources (Read the Docs); docs/SCIENCE.md is the science policy
├── scripts/            # perf measurements, licence notices (scripts/licenses), link checker
├── tools/storm_clip/   # offline storm-centred 3D clip renderer
├── THIRD_PARTY_NOTICES.md  # generated; CONTRIBUTING.md; DCO; NOTICE; LICENSE
└── package.json, vite.config.ts, svelte.config.js, ruff.toml
```

### Communication
Frontend and engine talk over one local WebSocket: JSON requests/answers with a type (`open_file`,
`render`, `run_module`, `create_mosaic`, `run_multidoppler`, `export`, ... see `python/server.py`)
and binary frames for arrays. Requests are scheduled and cancellable (`docs/RESPONSIVENESS.md`).

### Data flow
1. User opens a file: `xradar.open_*()` gives a lazy `xr.Dataset` volume; metadata is sent at once
2. Sweeps are sent as binary arrays; the GPU draws them (WebGL2) with the colour scale applied on the GPU
3. Modules, pipelines and mosaics run in the engine and return fields/tables/overlays or gridded volumes
4. Export writes standards-clean files with provenance (CfRadial1/2, Zarr, CSV, PNG/SVG/PDF; `docs/OUTPUT_STANDARDS.md`)

## Current phase

Released as 0.2.x. Shipped: 2D/3D viewers, cross-sections and RHI, modules and pipelines, mosaics and
multi-Doppler winds with 3D flow, realistic ground and stylised looks (labelled), storm clips, CLI and Python API,
cf-clean outputs, smaller installers, signed auto-update. In progress towards 0.3.0: UI consistency, radar-site
layer, smart open, animation, environment-data framework with provenance labels, hydrology/QPE. The master list
of planned work is the GitHub issue "Roadmap: the futuristic radar workbench (master list)" (#120).

## Commands

- `npm ci` (or `npm install`): frontend dependencies
- `npm run tauri dev`: run the desktop app (uses `python/.venv` automatically; `XRADAR_PYTHON` overrides)
- `npm run tauri build`: installers (the engine is bundled first, see `docs/BUNDLING.md`)
- `npm run dev:web` / `npm run build:web`: the browser build against a remote engine
- Checks: `ruff check/format --config ruff.toml python/`, `(cd python && python -m pytest tests/)`,
  `npx svelte-check --tsconfig ./tsconfig.json`, `npx tsc --noEmit`, `npm run test:unit`, `npm run build`,
  `python scripts/check_links.py`, docs: `cd docs && python -m sphinx -b html . _build/html`

## Prerequisites

- Node.js 20+ (CI uses 24 for the unit tests)
- Rust (stable, via rustup); on Linux the WebKit/GTK development packages
- Python 3.10+ (3.12+ for radarx) with `pip install -e "python[dev,radarx]"`
- Tauri CLI comes with `npm ci` (`@tauri-apps/cli`)
