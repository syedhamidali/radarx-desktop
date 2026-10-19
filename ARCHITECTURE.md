# xradar-desktop — Architecture

A lightweight, cross-platform desktop application for radar data research, built on top of **xradar**, **xarray**, and **Dask**.

---

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        xradar-desktop                              │
│                   (Tauri 2.0 — Rust Shell)                         │
│         Native menus · File dialogs · OS notifications             │
│              Auto-update · ~15MB installer                         │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌─────────────────────────────────┐   ┌──────────────────────────┐ │
│  │     Frontend (Svelte)           │   │    Python Sidecar        │ │
│  │                                 │   │    (Bundled via PyO3     │ │
│  │  ┌───────────────────────────┐  │   │     or subprocess)      │ │
│  │  │   Radar Viewer            │  │   │                          │ │
│  │  │   ┌───────────────────┐   │  │   │  ┌──────────────────┐   │ │
│  │  │   │  deck.gl / Mapbox │   │  │   │  │    xradar        │   │ │
│  │  │   │  GPU-rendered map │   │◄─┼───┼──│  ┌────────────┐  │   │ │
│  │  │   │  + radar overlay  │   │  │   │  │  │ NEXRAD L2  │  │   │ │
│  │  │   └───────────────────┘   │  │   │  │  │ ODIM H5    │  │   │ │
│  │  └───────────────────────────┘  │   │  │  │ CfRadial   │  │   │ │
│  │                                 │   │  │  │ IRIS/Sigmet│  │   │ │
│  │  ┌───────────────────────────┐  │   │  │  │ Rainbow    │  │   │ │
│  │  │   Data Inspector          │  │   │  │  │ Furuno     │  │   │ │
│  │  │   • Variable browser      │  │   │  │  │ IMD        │  │   │ │
│  │  │   • Metadata viewer       │  │   │  │  └────────────┘  │   │ │
│  │  │   • Coordinate explorer   │  │   │  └──────────────────┘   │ │
│  │  └───────────────────────────┘  │   │           │              │ │
│  │                                 │   │           ▼              │ │
│  │  ┌───────────────────────────┐  │   │  ┌──────────────────┐   │ │
│  │  │   Processing Panel        │  │   │  │  xarray + Dask   │   │ │
│  │  │   • QC / filtering        │  │   │  │                  │   │ │
│  │  │   • Gridding              │  │   │  │  • Lazy loading  │   │ │
│  │  │   • Compositing           │  │   │  │  • Chunked I/O   │   │ │
│  │  │   • Dual-pol products     │  │   │  │  • Parallel ops  │   │ │
│  │  └───────────────────────────┘  │   │  │  • Out-of-core   │   │ │
│  │                                 │   │  └──────────────────┘   │ │
│  │  ┌───────────────────────────┐  │   │           │              │ │
│  │  │   Export Panel             │  │   │           ▼              │ │
│  │  │   • PNG/SVG/PDF (pub)     │  │   │  ┌──────────────────┐   │ │
│  │  │   • NetCDF / CfRadial2   │  │   │  │  Rendering       │   │ │
│  │  │   • GeoTIFF / Zarr       │  │   │  │                  │   │ │
│  │  │   • Animation (GIF/MP4)  │  │   │  │  Datashader      │   │ │
│  │  └───────────────────────────┘  │   │  │  (fast raster)   │   │ │
│  │                                 │   │  │       +          │   │ │
│  └─────────────────────────────────┘   │  │  Cairo/Pillow    │   │ │
│              ▲                         │  │  (pub-quality)    │   │ │
│              │                         │  └──────────────────┘   │ │
│              │    WebSocket / IPC      │           │              │ │
│              │  (Arrow IPC for data)   │           │              │ │
│              └─────────────────────────┘           │              │ │
│                                                     │              │ │
└─────────────────────────────────────────────────────────────────────┘
                                                      │
                    ┌─────────────────────────────────┘
                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         File System / Data                          │
│                                                                     │
│   ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│   │ Local    │  │  S3 /    │  │  THREDDS │  │  Real-time feed  │  │
│   │ files    │  │  Cloud   │  │  OPeNDAP │  │  (LDM / polling) │  │
│   └──────────┘  └──────────┘  └──────────┘  └──────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Data Flow

```
User opens file(s)
        │
        ▼
┌──────────────────┐     Lazy — no full read
│  xradar.open_*   │────────────────────────┐
└──────────────────┘                        │
        │                                   ▼
        ▼                          ┌──────────────────┐
  xr.Dataset (Dask-backed)        │  Dask Scheduler   │
        │                          │  (threaded or     │
        │                          │   distributed)    │
        ▼                          └──────────────────┘
┌──────────────────┐                        │
│  User applies    │                        │
│  operations:     │  ← only triggers ──────┘
│  • slice sweep   │    compute on the
│  • threshold     │    chunks needed
│  • grid          │
└──────────────────┘
        │
        ├──── Interactive view ──► Datashader rasterize()
        │                          → PNG tile → WebSocket
        │                          → deck.gl BitmapLayer
        │                          (60fps pan/zoom)
        │
        └──── Publication export ─► Datashader @ high DPI (300-600)
                                    + Cairo vector overlays (map borders,
                                      gridlines, colorbars, labels)
                                    → SVG/PDF/PNG
                                    (journal-quality, no matplotlib)
```

---

## Communication Protocol

```
Frontend (JS)                         Python Sidecar
     │                                      │
     │──── open_file(path) ────────────────►│
     │◄─── schema: {vars, dims, attrs} ────│  (metadata only, instant)
     │                                      │
     │──── render(var, sweep, bbox) ───────►│
     │◄─── Arrow IPC buffer ───────────────│  (zero-copy raster tile)
     │                                      │
     │──── process(pipeline_config) ───────►│
     │◄─── progress stream ────────────────│  (via WebSocket)
     │◄─── result: updated schema ─────────│
     │                                      │
     │──── export(format, dpi, extent) ────►│
     │◄─── file_saved(path) ───────────────│
```

---

## Technology Stack

| Layer | Technology | Purpose |
|---|---|---|
| Desktop shell | **Tauri 2.0** (Rust) | Cross-platform window, native OS integration, ~15MB installer |
| Frontend | **Svelte** | Smallest bundle size, fastest DOM updates |
| Map / Radar view | **deck.gl** + Mapbox GL | GPU-accelerated radar overlays, 60fps pan/zoom |
| IPC | **WebSocket** + **Apache Arrow IPC** | Zero-copy data transfer between Python and JS |
| Data engine | **xradar** | Read/write all major radar formats |
| Lazy compute | **xarray + Dask** | Out-of-core chunked loading, parallel processing |
| Fast rendering | **Datashader** | Server-side rasterization, handles 10M+ points in ms |
| Pub figures | **Datashader + Cairo** | Datashader @ high DPI + Cairo vector overlays for SVG/PDF/PNG |
| Cloud access | **fsspec / s3fs** | Open S3, GCS, or HTTP URLs natively via xarray |

---

## Key Design Decisions

| Concern | Decision | Rationale |
|---|---|---|
| Large data | Dask-backed xarray | Out-of-core: only loads the chunks that are needed |
| Interactive speed | Datashader rasterization | Server-side rendering avoids sending millions of points to the browser |
| Publication output | Datashader + Cairo | Datashader rasterizes at high DPI; Cairo adds vector overlays (map, labels). Single rendering engine for both interactive and export — no matplotlib overhead |
| Data transfer | Arrow IPC | Zero-copy serialization, ~0ms overhead for large arrays |
| App size | Tauri over Electron | 15MB vs 150MB+, uses native webview instead of bundling Chromium |
| Frontend framework | Svelte over React | Smaller bundle, no virtual DOM overhead, simpler reactivity |
| No Rust rewrite | Keep xradar as-is | Dask + Datashader already solve the performance bottleneck |

---

## Project Structure

```
xradar-desktop/
├── ARCHITECTURE.md
├── src-tauri/              # Rust / Tauri shell
│   ├── Cargo.toml
│   ├── src/
│   │   └── main.rs         # Tauri entry, IPC commands, sidecar management
│   ├── tauri.conf.json
│   └── icons/
├── src/                    # Svelte frontend
│   ├── App.svelte
│   ├── lib/
│   │   ├── components/
│   │   │   ├── RadarViewer.svelte
│   │   │   ├── DataInspector.svelte
│   │   │   ├── ProcessingPanel.svelte
│   │   │   └── ExportPanel.svelte
│   │   ├── stores/
│   │   │   ├── radarData.ts     # Reactive state for loaded datasets
│   │   │   └── settings.ts
│   │   └── utils/
│   │       └── websocket.ts     # WebSocket client to Python sidecar
│   ├── assets/
│   └── main.ts
├── python/                 # Python sidecar
│   ├── pyproject.toml
│   ├── server.py           # WebSocket server entry point
│   ├── engine/
│   │   ├── __init__.py
│   │   ├── reader.py       # xradar file opening + lazy loading
│   │   ├── processor.py    # QC, gridding, dual-pol pipelines
│   │   ├── renderer.py     # Datashader rasterization
│   │   ├── exporter.py     # Datashader + Cairo high-DPI figure export
│   │   └── arrow_bridge.py # xarray → Arrow IPC serialization
│   └── tests/
├── package.json
├── svelte.config.js
├── vite.config.ts
└── README.md
```

---

## Build & Distribution

| Platform | Packaging | Notes |
|---|---|---|
| macOS | `.dmg` via Tauri bundler | Universal binary (Intel + Apple Silicon) |
| Windows | `.msi` / `.exe` via Tauri bundler | Includes WebView2 bootstrapper |
| Linux | `.AppImage` / `.deb` via Tauri bundler | Uses system WebKitGTK |
| Python sidecar | Bundled via **PyInstaller** or **Nuitka** | Frozen Python env with xradar + deps, ~50-80MB |

---

## Milestones

### Phase 1 — Skeleton
- Tauri + Svelte scaffold
- Python sidecar with WebSocket server
- Open a single radar file, display metadata in Data Inspector

### Phase 2 — Visualization
- deck.gl radar viewer with Datashader-rendered tiles
- Sweep selector, variable selector
- Pan/zoom at 60fps

### Phase 3 — Processing
- QC pipeline UI (despeckle, velocity dealiasing)
- Gridding via xradar/wradlib
- Compositing multiple files

### Phase 4 — Export
- Datashader + Cairo publication figures (high-DPI raster + vector overlays)
- CfRadial2 / Zarr / GeoTIFF export
- GIF/MP4 animation from time series

### Phase 5 — Polish & Distribution
- Auto-update via Tauri updater
- Installers for macOS, Windows, Linux
- Cloud file access (S3, THREDDS, OPeNDAP)
