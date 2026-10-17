# xradar-desktop

A lightweight, cross-platform desktop research tool for radar data, built with **Tauri 2.0** (Rust shell) + **Svelte** (frontend) + **Python sidecar** (xradar/xarray/Dask engine).

## Architecture

See `ARCHITECTURE.md` for the full system diagram, data flow, and design decisions.

### Stack summary
- **Tauri 2.0** — Rust-based desktop shell (~15MB vs Electron's 150MB)
- **Svelte** — frontend UI framework
- **deck.gl / Mapbox GL** — GPU-accelerated radar visualization
- **Python sidecar** — runs as a subprocess, communicates via WebSocket
  - **xradar** — reads all major radar formats (NEXRAD, ODIM, CfRadial, IRIS, Rainbow, Furuno, IMD)
  - **xarray + Dask** — lazy/chunked out-of-core data loading
  - **Datashader** — fast server-side rasterization for both interactive views AND high-DPI export
  - **Cairo (pycairo)** — vector overlays (map borders, gridlines, colorbars, labels) for publication export
  - **Apache Arrow IPC** — zero-copy data transfer to frontend

### Project structure
```
xradar-desktop/
├── src-tauri/              # Rust / Tauri shell
│   ├── Cargo.toml
│   └── src/main.rs
├── src/                    # Svelte frontend
│   ├── App.svelte
│   └── lib/
│       ├── components/     # RadarViewer, DataInspector, ProcessingPanel, ExportPanel
│       ├── stores/         # Reactive state (radarData, settings)
│       └── utils/          # WebSocket client, Arrow deserialization
├── python/                 # Python sidecar
│   ├── pyproject.toml
│   ├── server.py           # WebSocket server entry
│   └── engine/             # reader, processor, renderer, exporter, arrow_bridge
├── package.json
├── svelte.config.js
└── vite.config.ts
```

### Communication
Frontend ↔ Python sidecar communicate via WebSocket:
- `open_file(path)` → returns metadata (vars, dims, attrs)
- `render(var, sweep, bbox)` → returns Arrow IPC buffer (raster tile)
- `process(pipeline_config)` → streams progress, returns updated schema
- `export(format, dpi, extent)` → saves file, returns path

### Data flow
1. User opens file → `xradar.open_*()` returns lazy Dask-backed `xr.Dataset`
2. Interactive view → Datashader rasterizes → PNG tile → WebSocket → deck.gl BitmapLayer
3. Publication export → Datashader @ high DPI + Cairo vector overlays → SVG/PDF/PNG

## Current phase

**Phase 1 — Skeleton**: Set up the Tauri + Svelte scaffold, Python sidecar with WebSocket server, and basic file-open → metadata display flow.

## Commands

- `npm install` — install frontend dependencies
- `npm run tauri dev` — run the app in development mode
- `npm run tauri build` — build distributable installers

## Prerequisites

- Node.js 18+
- Rust (via rustup)
- Python 3.10+ with xradar, xarray, dask, pycairo, pillow
- Tauri CLI: `cargo install tauri-cli@^2`
