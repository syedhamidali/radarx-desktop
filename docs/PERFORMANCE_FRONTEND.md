# Frontend performance

What was measured, what was changed, and how to measure it again. Everything here is about the
Svelte frontend (`src/`); the engine has its own notes. The measuring scripts live in
`scripts/perf/` and are not part of the app build.

Test machine: Apple Silicon Mac, headless Google Chrome (real GPU through ANGLE/Metal), a local engine,
a NEXRAD volume (`KTLX20130520_201643_V06.gz`) and a CfRadial file for the open/close cycles.
"CPU x4" means Chrome's CPU throttling at 4x slower, a rough stand-in for a modest laptop.
All numbers are medians of 11 cold loads (startup) or of one 8 s run (frames).

## Results

### Bundle (`npm run build`)

| | before | after |
|---|---|---|
| Entry chunk, what the first paint must download and run | 968.7 KB raw / 319.5 KB gzip | **229.0 KB raw / 91.0 KB gzip** |
| Entry CSS | 244.5 KB raw / 36.5 KB gzip | **73.1 KB raw / 13.2 KB gzip** |
| All JS in `dist/` | 992.8 KB raw / 327.2 KB gzip | 927.6 KB raw / 340.3 KB gzip |
| JS chunks | 6 | 36 (the heavy ones load on demand) |
| npm packages in the lockfile | 190 | 112 |

The sum of all JS barely changes (gzip is slightly worse because the chunks no longer share a
compression window); what changed is how much of it sits in front of the first paint.

Largest chunks now, in the order they are needed: entry 229 KB (boot screen, sidebars, engine
link, Svelte runtime), `RadarViewer` 103 KB (fetched when idle, used when a file opens),
`RadarViewer3D` 58 KB (first visit to 3D), `DualPolDashboard` 32 KB, the panels 10 to 30 KB each.

### Startup (cold load, engine connected, `scripts/perf/measure_startup.py`)

| | CPU x1 before | CPU x1 after | CPU x4 before | CPU x4 after |
|---|---|---|---|---|
| first contentful paint | 192 ms | **112 ms** | 640 ms | **272 ms** |
| usable UI painted (status bar + main area, two frames) | 176 ms | **84 ms** | 635 ms | **291 ms** |
| JS to fetch before that (gzip) | 320 KB | **91 KB** | 320 KB | **91 KB** |
| main-thread blocking time before the UI is there | 51 ms | **0 ms** | 453 ms | **95 ms** |
| blocking time over the whole load | 51 ms | **3 ms** | 453 ms | **226 ms** |
| everything fetched and mounted ("settled") | 181 ms | 208 ms | 666 ms | 850 ms |

The last row is the price of lazy loading and is deliberate: the panels and the 2D viewer are
fetched and mounted one by one while the browser is idle, after the UI is already usable. Total work
is similar; it just no longer sits in front of the first paint.

### Interaction (`scripts/perf/measure_runtime.py`, NEXRAD volume, headless Chrome with a real GPU)

| | before | after |
|---|---|---|
| file registered -> 2D canvas on screen | 17.3 ms | 17.5 ms (CPU x4: 89.7 / 85.5) |
| 2D pan + wheel zoom, frame time p50 / p95 / max | 16.7 / 16.7 / 16.8 ms | 16.7 / 16.8 / 16.8 ms |
| 3D orbit, frame time p50 / p95 / max | 16.7 / 16.7 / 16.8 ms | 16.7 / 16.8 / 33.3 ms (one frame) |
| long tasks (> 50 ms) while panning or orbiting | 0 | 0 |
| main thread busy while orbiting (CPU x1 / x4) | 7.6 % / 36.8 % | 8.8 % / 31.2 % |

The view was already running at the display rate (60 Hz, so p50 = 16.7 ms) with the main thread
mostly idle; a CPU profile of 6 s of panning and of orbiting shows 88 to 89 % idle and no function
above 0.5 % (`scripts/perf/profile.py`). So there was no runtime hot spot to fix, and nothing was
changed there beyond the cleanup below. Frame times are limited by v-sync in headless Chrome; the
busy percentage and the profile are the more sensitive numbers.

### Memory after 20 open / switch sweeps / 3D / close cycles

(`measure_runtime.py --only leak`, alternating a 9.5 MB NEXRAD file and a 14 MB CfRadial file,
JS heap measured after a forced GC.)

| | before | after |
|---|---|---|
| JS heap: baseline / after cycle 2 / after cycle 20 | 6.5 / 7.4 / 8.8 MB | 6.3 / 7.3 / 8.7 MB |
| live DOM nodes (flat after warm-up) | 6969 -> 7387 | 7035 -> 7233 |
| live WebGL objects (texture / buffer / program / framebuffer) | 2 / 5 / 2 / 1 | 2 / 4 / 2 / 1 |
| WebGL contexts still alive at the end (created: 42) | 2 | 1 |
| renderer process RSS (noisy, GC timing) | 188 -> 223 -> 207 MB | 240 -> 277 -> 170 MB |

No leak was found: the heap rises about 0.1 MB per cycle for the first few cycles and then stays
flat, DOM nodes and listeners are flat after warm-up, and every texture, buffer and program is
deleted when a viewer goes away. (Two early "leaks" in this investigation turned out to be the
measuring code itself keeping WebGL objects alive; the script now uses weak references.)

## What changed

1. **Code splitting** (`src/lib/utils/lazy.svelte.ts`, `App.svelte`, `PanelManager.svelte`,
   `DataInspector.svelte`). The first paint only needs the boot / welcome screen, the sidebars and
   the engine link. Loaded on demand: the 3D viewer (and its renderer, quality governor, transfer
   function editor), the dual-pol dashboard, the diff viewer, cross-section dock and dialog,
   command palette, settings, shortcuts, about, onboarding tour, data table and metadata viewer.
   The 12 right-hand panels (mosaic, wind, modules, ... each its own chunk) are fetched right
   after the first paint, expanded ones at once, the rest one per idle slot; they stay mounted
   afterwards exactly as before (their stores listen for engine and window events). The 2D viewer
   and the timeline are prefetched while idle so that opening a file does not wait for them; the 3D
   and dual-pol views also start loading when the pointer reaches their buttons.
2. **Entry chunk kept lean**: the sweep cache moved out of `ppiRenderer.ts` into `sweepCache.ts`,
   and `canonicalVariable` into `variableAliases.ts`, because the always-loaded websocket layer,
   file manager and variable-mapping store imported them and thereby pulled in the 45 KB WebGL
   renderer and the 30 KB Modules store. `ppiRenderer.ts` re-exports the old names.
3. **Colormaps as base64** (`colormaps.ts`): 29 tables of 256 RGB triplets were written as number
   lists (81 KB of source, 27 KB gzip), now base64 (33 KB, 22 KB gzip) and cheaper to parse. A unit
   test (`colormaps.test.mjs`) pins a SHA-256 of all bytes taken from the old form, so they are
   provably identical.
4. **Svelte 4 component API shim removed** from `svelte.config.js` (`compatibility.componentApi: 4`):
   nothing creates components with `new Component({target})`, the app uses `mount()`. About 9 KB
   less in the entry chunk and a little in every other chunk.
5. **Removed as unused** (each confirmed by grep, knip, a build, svelte-check, tsc, the unit tests
   and the browser smoke test):
   - `SplitView.svelte` and `Tooltip.svelte` (no importer anywhere), `utils/arrow.ts`;
   - the npm dependencies `@deck.gl/core`, `@deck.gl/layers`, `@deck.gl/mapbox`, `apache-arrow` and
     `mapbox-gl` (no import in the code; the map is drawn by the app's own tile renderer). They were
     already tree-shaken out of the bundle, so this is 78 packages less to install and audit, not
     bytes shipped;
   - 5 unused component imports in `App.svelte` (the right-hand panels are only used by `PanelManager`).
6. **WebGL hygiene** (`RadarViewer3D.svelte`, `ppiRenderer.ts`): the full-screen-quad buffer is now
   deleted, both viewers call `WEBGL_lose_context` when they are destroyed (GPU memory is returned
   at once instead of when the garbage collector reaches the canvas; the 2D view is rebuilt on every
   2D/3D switch), and the `window.__xr3d` diagnostics hook no longer keeps a destroyed 3D viewer and
   its DOM alive.

## Left alone, on purpose

- **Fonts**: already latin-subset woff2, and the three Sans weights (400/500/600) and two Mono
  weights (400/500) are all used by the design; dropping one would change the look.
- **Icon** (`src/lib/assets/icon.png`, `public/favicon.png`, 52 KB): shown unchanged by design; a
  lossless re-compression would save only 3 KB.
- **Runtime rendering code**: no hot spot (see above).
- **About 15 exported-but-unused helper functions and ~50 unused exported types** (knip). They are
  tree-shaken out of the bundle (zero bytes shipped) and some form small public store APIs.
- **`ARCHITECTURE.md` / `CLAUDE.md` still describe deck.gl and Mapbox GL** as the planned map stack;
  that text is aspirational and outside this change except for the removed `arrow.ts`.
- Real-GPU behaviour on Windows/Linux and software-GL machines was not measured here.

## Measuring again

```sh
# bundle: per-chunk sizes, and the biggest modules in each chunk
npm i --no-save rollup-plugin-visualizer          # one-off, not a project dependency
npx vite build -c scripts/perf/vite.analyze.config.ts
node scripts/perf/bundle_report.mjs dist-analyze --modules
node scripts/perf/bundle_report.mjs dist           # after a normal `npm run build`

# dead code
npx knip -c scripts/perf/knip.json --include files,dependencies,exports

# startup + frames + memory cycles for one build (needs an engine and a Python with playwright)
npm run build && npx vite preview --host 127.0.0.1 --port 4175 &
npx vite build -c scripts/perf/vite.perf.config.ts && npx vite preview --outDir dist-perf --host 127.0.0.1 --port 4174 &
PY=python ENGINE_PORT=... FILE=volume.gz FILE2=other.nc PLAIN_URL=http://127.0.0.1:4175/ \
  PERF_URL=http://127.0.0.1:4174/ OUT=/tmp/perf sh scripts/perf/run_all.sh after
python scripts/perf/summarize.py /tmp/perf base after

# UI smoke test of a build (every view, every panel, the dialogs; non-zero exit on failure)
python scripts/perf/smoke.py --url http://127.0.0.1:4174/ --engine-port ... --file volume.gz

# diagnosing: CPU profile, retained DOM, element growth
PERF_NOMINIFY=1 PERF_OUT=dist-prof npx vite build -c scripts/perf/vite.perf.config.ts
python scripts/perf/profile.py ...   python scripts/perf/detached.py ...   python scripts/perf/domdiff.py ...
```

Notes for anyone repeating this: start Chrome with `--no-proxy-server` (without it, headless Chrome
spends 2.5 s on proxy auto-detection before every navigation, which swamps every other number),
serve with `--host 127.0.0.1` (vite preview binds IPv6 only by default), and never share a Vite
`cacheDir` between worktrees.
