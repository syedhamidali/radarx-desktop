# radarx-desktop - Architecture

A desktop (and web) application for weather radar data, built on **xradar**, **xarray** and **radarx**. This page is
the map: what the parts are, how data moves, and why it is built this way. Details live in `docs/`
(each topic has its own page; links below). The rules every feature follows are in
[docs/SCIENCE.md](docs/SCIENCE.md).

---

## High-level picture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Tauri 2 shell (Rust, src-tauri/)                                             │
│ native window · file associations · starts and supervises the engine         │
│ signed auto-update (updates.rs, docs/UPDATES.md)                             │
│                                                                              │
│  ┌────────────────────────────────────┐        ┌───────────────────────────┐ │
│  │ Frontend (Svelte 5, src/)          │        │ Python engine (python/)   │ │
│  │                                    │ WebSock│                           │ │
│  │  2D PPI      WebGL2 ppiRenderer    │  JSON  │ server.py  (WebSocket)    │ │
│  │  3D volume   WebGL2 ray-marcher    │ + raw  │ engine/                   │ │
│  │  3D mosaic + wind flow (WebGL2)    │ binary │   reader   (xradar)       │ │
│  │  RHI, cross-sections, charts       │◀──────▶│   volume · derived        │ │
│  │  panels: Modules, Pipeline, Mosaic,│ chunks │   pipeline · processor    │ │
│  │   Wind, Cells, Export, Settings    │        │   mosaic · multidoppler   │ │
│  │  map tiles (OSM / OpenTopoMap)     │        │   cells · tracking · rhi  │ │
│  │  workers: binary decode, meshes    │        │   exporter · cfwrite      │ │
│  └────────────────────────────────────┘        │   provenance · cli · api  │ │
│                                                 │ modules/  (plug-ins)      │ │
│                                                 └─────────────┬─────────────┘ │
└───────────────────────────────────────────────────────────────┼───────────────┘
                                                                │
          ┌──────────────────┬──────────────────────┬───────────┴────────┐
          ▼                  ▼                      ▼                    ▼
   local files         NOAA NEXRAD on AWS     ARCO / icechunk      exports (CfRadial1/2,
   (any xradar         (Cloud data panel,     (S3, on request)     Zarr, CSV, PNG/SVG/PDF)
    format)             on request)

The same engine runs without the window:  rxd ...  (engine/cli.py)   from engine import api
and as a web service:                     python server.py --web     (docs/WEB.md)
```

## Accounts

An account is required on every surface (app, engine, `rxd`, `import rxd`; [docs/ACCOUNTS.md](docs/ACCOUNTS.md)). The
app signs in with Supabase and hands the short-lived access token to the engine (`auth_session`); the engine refuses
all work on a connection until it has verified the token offline against the project's public key
(`python/engine/account/`), and writes the session to one per-user file that `rxd` and `import rxd` share. One function
(`entitlement_allows`) is where a subscription check would go; there is no billing yet. A feature-level activity log
(never files, places or data) is queued locally and sent with the user's own token. The gate is client-side: it stops
honest use without an account, and docs/ACCOUNTS.md says plainly what it cannot stop.

## Data flow

```
User opens a file
        │
        ▼
 xradar.open_*  ──────────────►  lazy xr.Dataset volume (metadata sent at once)
        │
        ├── View ──► sweep arrays (Float32, raw binary frames) ──► GPU texture
        │            the colour scale, mask and smoothing are applied on the GPU (WebGL2),
        │            so changing them costs no round trip
        │
        ├── Module / pipeline ──► engine computes a Result (scalars, tables, overlay, field)
        │                         or a derived variable stored with the volume (with provenance)
        │
        ├── Mosaic / multi-Doppler ──► radarx grids the radars onto one Cartesian grid;
        │                              the volume is sent to the 3D viewer
        │
        └── Export ──► CfRadial1/2, Zarr, CSV, PNG/SVG/PDF with self-describing metadata
                       (versions, parameters, history; no absolute paths)
```

Requests are scheduled and cancellable on the client (`src/lib/utils/requestScheduler.ts`,
[docs/RESPONSIVENESS.md](docs/RESPONSIVENESS.md)); the engine guards memory, runtime and corrupt input
([docs/RELIABILITY.md](docs/RELIABILITY.md), [docs/STABILITY.md](docs/STABILITY.md)).

## Communication

One WebSocket on localhost (a free port chosen by the shell, a random token on the connection). Messages are JSON
with a `type`; arrays travel as raw binary frames that follow a JSON header:

```
Frontend                                        Engine
  │── open_file {path} ───────────────────────────▶│
  │◀── file_opened {metadata: sweeps, vars, site} ─│   instant, no data read
  │── get_sweep_data / render {var, sweep} ───────▶│
  │◀── JSON header + binary Float32 chunks ────────│
  │── run_module {id, params} ────────────────────▶│
  │◀── progress ... module_result / module_field ──│
  │── create_mosaic / run_multidoppler ───────────▶│
  │◀── progress ... mosaic_volume / wind_volume ───│
  │── export {format, ...} ───────────────────────▶│
  │◀── file_saved {path} ──────────────────────────│
```

The list of message types is in `python/server.py`. **Apache Arrow** is optional: `engine/arrow_bridge.py` and the
`arrow` extra exist for scripts, but the app sends raw binary chunks, so pyarrow (about 55 MB) is not installed by
default and is not bundled.

## Technology

| Layer | Technology | Why |
|---|---|---|
| Desktop shell | **Tauri 2** (Rust) | native webview instead of a bundled browser; small shell binary (about 8 MB) |
| Frontend | **Svelte 5** + TypeScript, Vite | small bundle, fine-grained reactivity |
| Rendering | **own WebGL2 renderers** (PPI, ray-marched 3D volume, wind flow); canvas for charts | exact control of radar geometry (polar sweeps, sector scans, gaps), GPU colour scales, no generic map engine in the way |
| Map background | raster tiles (OpenStreetMap, OpenTopoMap, or none); terrain + imagery for the realistic 3D ground | on request, cached, attributed; works offline without them |
| Engine | **xradar** (readers), **xarray** (+ dask), **radarx** (QC, HID, KDP, QPE, multi-Doppler, gridding) | one data model for all formats; radarx is the algorithm library |
| Figures | **matplotlib**, Pillow | PNG/SVG/PDF figures with colour bar, rings and title; GIF animations |
| Colour | matplotlib / colorcet / cmweather maps, NWS-style tables | see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) |
| Packaging | **PyInstaller** (engine) inside Tauri bundles | installers need nothing else; [docs/BUNDLING.md](docs/BUNDLING.md), [docs/PACKAGING_AUDIT.md](docs/PACKAGING_AUDIT.md) |
| Updates | Tauri updater, signature-checked, user-initiated | [docs/UPDATES.md](docs/UPDATES.md) |

## Key design decisions

| Concern | Decision | Rationale |
|---|---|---|
| Science first | Every layer has a provenance class; nothing invented; stylised looks opt-in and labelled | [docs/SCIENCE.md](docs/SCIENCE.md) |
| Speed | Send data once, do colour/mask/smoothing on the GPU; cache decoded sweeps; lower 3D quality on its own when slow | interaction never waits for the engine; [docs/PERFORMANCE.md](docs/PERFORMANCE.md), [docs/PERFORMANCE_FRONTEND.md](docs/PERFORMANCE_FRONTEND.md) |
| Stability | Engine in a separate process, supervised; modules time-limited; memory budget; cancellable requests | a bad file or module never takes the window down |
| Same engine everywhere | GUI, `rxd` CLI, Python API and the web service share `engine/` | results are identical and scriptable ([docs/CLI.md](docs/CLI.md), [docs/API.md](docs/API.md)) |
| Outputs | CF-1.8 + CfRadial 1.4 / FM-301 layout, provenance attributes, no local paths | files open elsewhere and can be reproduced ([docs/OUTPUT_STANDARDS.md](docs/OUTPUT_STANDARDS.md)) |
| Algorithms | Plug-in modules: one Python file, generated UI | easy to extend ([docs/MODULES.md](docs/MODULES.md)) |
| Size | Tauri over a bundled browser; slimmed engine bundle; optional heavy dependencies out | [docs/PACKAGING_AUDIT.md](docs/PACKAGING_AUDIT.md) |
| Licences | Notices generated from the real dependency trees, allow-list in CI | [docs/LICENSES.md](docs/LICENSES.md) |

## Project structure

```
radarx-desktop/
├── ARCHITECTURE.md  CLAUDE.md  CONTRIBUTING.md  DCO  NOTICE  LICENSE  THIRD_PARTY_NOTICES.md  README.md
├── src-tauri/               Rust shell: main.rs (engine supervision), updates.rs, tauri*.conf.json, icons/
├── src/
│   ├── App.svelte, main.ts
│   └── lib/
│       ├── components/      viewers (RadarViewer, Viewer3D, RhiViewer, CartesianViewer3D ...), panels, dialogs
│       ├── stores/          reactive state (radarData, settings, volume3d, wind, updater ...)
│       ├── utils/           WebSocket client, renderers, colormaps, geometry, caches (+ *.test.mjs)
│       ├── workers/         binary decoding, mesh building
│       └── styles/          tokens, theme, bundled IBM Plex fonts
├── python/
│   ├── server.py            WebSocket server (also --web)
│   ├── engine/              reader, volume, derived, pipeline, mosaic, multidoppler, cells, tracking, rhi,
│   │                        exporter, cfwrite, provenance, cloud, web, api, cli, account/ (sign-in gate, activity log) ...
│   ├── modules/             plug-in algorithms (radarx_*.py, storm_detection.py ...)
│   ├── packaging/           PyInstaller spec and build script
│   ├── tests/  benchmarks/
│   └── rxd/                 `import rxd` alias of engine.api
├── docs/                    Sphinx sources (Read the Docs) and images
├── scripts/                 perf/ (measurements), licenses/ (notices + policy), check_links.py
├── tools/storm_clip/        offline storm-centred 3D clip renderer (docs/STORM_CLIP.md)
└── .github/                 workflows (ci, docs, licenses, release), issue forms, PR template
```

## Build and distribution

| Platform | Package | Notes |
|---|---|---|
| macOS | `.dmg` (Apple silicon and Intel builds) | not code-signed yet; first launch needs "Open Anyway" |
| Windows | `.exe` (NSIS) | WebView2 is part of Windows 10/11 |
| Linux | `.deb`, `.rpm` | uses the system WebKitGTK |
| Engine | PyInstaller one-folder bundle inside the app resources | built on each OS by the Release workflow |

Updates: signed Tauri updater artifacts on GitHub Releases for macOS and Windows; Linux packages are told that a
new version exists. Details: [docs/UPDATES.md](docs/UPDATES.md).

## Where the work is going

Shipped: viewers (2D, 3D, cross-section, RHI), modules and pipelines, mosaics and multi-Doppler winds with 3D
flow, realistic ground and stylised looks (labelled), storm clips, CLI and Python API, standards-clean outputs,
smaller installers, auto-update, licence notices and the science policy. Planned (provenance labels on every
layer, environment data from HRRR/ERA5/soundings/MRMS, hydrology and flash-flood ML, winter weather, lightning,
microphysics, domain packs, animation): the master list is the GitHub issue
"Roadmap: the futuristic radar workbench (master list)" (#120), with one roadmap issue per area.
