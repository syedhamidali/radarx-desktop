<script lang="ts">
  import { surface, text } from '../utils/themeColors';
  /**
   * MapBackground — renders slippy-map tiles on a canvas behind the radar PPI.
   *
   * Uses a keyless basemap provider (OpenStreetMap by default, see utils/basemaps.ts). Tiles are
   * fetched through a small bounded loader (concurrency cap, LRU cache, exponential backoff and an
   * offline circuit breaker) and painted in sync with the PPI view transform. The dark theme is a
   * CSS filter on the tile canvas only.
   *
   * The radar mosaic (a Web-Mercator PNG placed by its four corners) with the radar sites and
   * range rings is drawn on a second, unfiltered canvas above the tiles, and a hover tooltip
   * (value + per-radar contributions) is shown while the mosaic view is on.
   */
  import { onMount, onDestroy } from 'svelte';
  import { settings } from '../stores/settings';
  import {
    latLonToTile,
    tileToLatLon,
    computeTileZoom,
    latLonToPixel,
    pixelToLatLon,
    radarToLatLon,
  } from '../utils/geoProjection';
  import { resolveBasemap, basemapTileUrl } from '../utils/basemaps';
  import { mosaicLayer, mosaicProbe, probeMosaic, clearMosaicProbe, type MosaicLayer } from '../stores/mosaic';
  import { windLayer } from '../stores/wind';
  import { drawWindLayer, type WindLayer } from '../utils/wind';

  export let radarLat: number;
  export let radarLon: number;
  export let maxRange: number;
  export let scale: number;
  export let translateX: number;
  export let translateY: number;
  export let containerWidth: number;
  export let containerHeight: number;
  export let opacity: number = 0.6;
  /** Draw the basemap tiles (false when only the mosaic is wanted). */
  export let showTiles: boolean = true;

  let canvasEl: HTMLCanvasElement; // basemap tiles (CSS-filtered for the dark theme)
  let mosaicCanvasEl: HTMLCanvasElement; // mosaic, sites, range rings: never filtered
  let ctx: CanvasRenderingContext2D | null = null;
  let mctx: CanvasRenderingContext2D | null = null;
  let rafId: number = 0;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let rootEl: HTMLDivElement;
  let layer: MosaicLayer | null = null;
  $: layer = $mosaicLayer;
  let wlayer: WindLayer | null = null;
  $: wlayer = $windLayer;
  $: mosaicShown = !!layer && layer.visible;
  $: probing = mosaicShown && !!layer?.view;

  $: basemap = resolveBasemap($settings.mapStyle, $settings.theme.mode);
  $: provider = basemap.provider;
  $: hasTiles = showTiles && !!provider.url;
  const BASE_FILL = '#e9e8ee'; // shown under/instead of tiles; goes through the theme filter

  // ── Tile cache, loader and failure handling ────────────────────────────────
  // LRU cache of decoded tiles ("provider/z/x/y" -> image); a decoded 256x256 tile is ~256 KB.
  const tileCache = new Map<string, HTMLImageElement>();
  const inflight = new Map<string, HTMLImageElement>();
  const failures = new Map<string, { n: number; until: number }>();
  const TILE_SIZE = 256;
  const MAX_CACHE = 192;
  const MAX_INFLIGHT = 6;
  const MAX_ATTEMPTS = 4;
  const BACKOFF_MS = 2000; // doubles per attempt: 2 s, 4 s, 8 s
  const OFFLINE_PAUSE_MS = 30000;
  let wanted: { key: string; z: number; x: number; y: number }[] = [];
  let consecutiveFails = 0;
  let pausedUntil = 0;
  let gen = 0; // bumped when the provider changes so stale loads are ignored

  $: if (provider) resetTiles(provider.id);
  let lastProviderId = '';
  function resetTiles(id: string) {
    if (id === lastProviderId) return;
    lastProviderId = id;
    gen++;
    for (const img of inflight.values()) img.src = '';
    inflight.clear();
    tileCache.clear();
    failures.clear();
    consecutiveFails = 0;
    pausedUntil = 0;
    scheduleRender();
  }

  // Re-render whenever view params change
  $: if (ctx && mctx && radarLat != null && radarLon != null) {
    void (scale, translateX, translateY, containerWidth, containerHeight, maxRange, opacity, provider, hasTiles, layer, wlayer, basemap.theme);
    scheduleRender();
  }

  onMount(() => {
    ctx = canvasEl.getContext('2d');
    mctx = mosaicCanvasEl.getContext('2d');
    scheduleRender();
  });

  onDestroy(() => {
    if (rafId) cancelAnimationFrame(rafId);
    if (retryTimer) clearTimeout(retryTimer);
    mosaicBuf = null;
    gen++;
    for (const img of inflight.values()) img.src = '';
    inflight.clear();
    tileCache.clear();
    detachProbe();
    clearMosaicProbe();
  });

  function scheduleRender() {
    if (rafId) return;
    rafId = requestAnimationFrame(renderTiles);
  }

  function tileKey(z: number, x: number, y: number): string {
    return `${provider.id}/${z}/${x}/${y}`;
  }

  function cached(key: string): HTMLImageElement | null {
    const img = tileCache.get(key);
    if (!img) return null;
    tileCache.delete(key); // LRU touch
    tileCache.set(key, img);
    return img;
  }

  /** Start queued loads for the tiles the last render wanted, nearest-to-centre first. */
  function pump() {
    const now = Date.now();
    if (now < pausedUntil) {
      armRetry(pausedUntil - now);
      return;
    }
    let nextRetry = Infinity;
    for (const t of wanted) {
      if (inflight.size >= MAX_INFLIGHT) break;
      if (tileCache.has(t.key) || inflight.has(t.key)) continue;
      const f = failures.get(t.key);
      if (f) {
        if (f.n >= MAX_ATTEMPTS) continue;
        if (f.until > now) {
          nextRetry = Math.min(nextRetry, f.until - now);
          continue;
        }
      }
      startLoad(t.key, t.z, t.x, t.y);
    }
    if (nextRetry < Infinity) armRetry(nextRetry);
  }

  function armRetry(ms: number) {
    if (retryTimer) clearTimeout(retryTimer);
    retryTimer = setTimeout(() => {
      retryTimer = null;
      scheduleRender();
    }, Math.max(50, ms));
  }

  function startLoad(key: string, z: number, x: number, y: number) {
    const myGen = gen;
    const img = new Image();
    img.decoding = 'async';
    inflight.set(key, img);
    img.onload = () => {
      if (myGen !== gen) return;
      inflight.delete(key);
      failures.delete(key);
      consecutiveFails = 0;
      while (tileCache.size >= MAX_CACHE) {
        const first = tileCache.keys().next().value;
        if (first === undefined) break;
        tileCache.delete(first);
      }
      tileCache.set(key, img);
      scheduleRender();
    };
    img.onerror = () => {
      if (myGen !== gen) return;
      inflight.delete(key);
      const n = (failures.get(key)?.n ?? 0) + 1;
      failures.set(key, { n, until: Date.now() + BACKOFF_MS * 2 ** (n - 1) });
      if (++consecutiveFails >= MAX_INFLIGHT) {
        // Looks like we are offline or blocked: stop hitting the server for a while.
        pausedUntil = Date.now() + OFFLINE_PAUSE_MS;
        consecutiveFails = 0;
        for (const f of failures.values()) f.n = Math.min(f.n, MAX_ATTEMPTS - 1);
      }
      scheduleRender();
    };
    img.src = basemapTileUrl(provider, z, x, y);
  }

  function renderTiles() {
    rafId = 0;
    if (!ctx || !mctx || !canvasEl || radarLat == null || radarLon == null) return;

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const cw = containerWidth;
    const ch = containerHeight;
    const pw = Math.round(cw * dpr);
    const phh = Math.round(ch * dpr);
    // Only resize when needed: assigning width/height reallocates and clears the canvas.
    if (canvasEl.width !== pw || canvasEl.height !== phh) {
      canvasEl.width = pw;
      canvasEl.height = phh;
    }
    if (mosaicCanvasEl.width !== pw || mosaicCanvasEl.height !== phh) {
      mosaicCanvasEl.width = pw;
      mosaicCanvasEl.height = phh;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cw, ch);
    mctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    mctx.clearRect(0, 0, cw, ch);

    const project = (lat: number, lon: number): [number, number] =>
      latLonToPixel(lat, lon, radarLat, radarLon, maxRange, scale, translateX, translateY, cw, ch);

    if (hasTiles) drawBasemap(ctx, cw, ch, dpr, project);

    const lyr = layer;
    if (lyr && lyr.visible) {
      drawMosaic(mctx, lyr, dpr, project);
      drawSites(mctx, lyr, project);
    }
    // Multi-Doppler wind analysis (speed raster, arrows, radar sites)
    if (wlayer && wlayer.visible) drawWindLayer(mctx, wlayer, project, dpr, cw, ch, basemap.theme);
  }

  function drawBasemap(
    c: CanvasRenderingContext2D,
    cw: number,
    ch: number,
    dpr: number,
    project: (lat: number, lon: number) => [number, number],
  ) {
    const zoom = Math.max(provider.minZoom, Math.min(provider.maxZoom, computeTileZoom(maxRange, scale, cw)));

    // Lat/lon bounds of the visible area. Parallels curve and meridians converge on the screen, so
    // sample a grid (not just the corners).
    const lats: number[] = [];
    const lons: number[] = [];
    for (let i = 0; i <= 6; i++) {
      for (let j = 0; j <= 6; j++) {
        const [lat, lon] = pixelToLatLon(
          (cw * i) / 6, (ch * j) / 6, radarLat, radarLon, maxRange, scale, translateX, translateY, cw, ch,
        );
        lats.push(lat);
        lons.push(lon);
      }
    }
    const maxLat = Math.min(85.05, Math.max(...lats));
    const minLat = Math.max(-85.05, Math.min(...lats));
    const minLon = Math.max(-180, Math.min(...lons));
    const maxLon = Math.min(180, Math.max(...lons));
    if (!(maxLat > minLat) || !(maxLon > minLon)) return;

    const topLeft = latLonToTile(maxLat, minLon, zoom);
    const bottomRight = latLonToTile(minLat, maxLon, zoom);
    const n = Math.pow(2, zoom);
    const tileXMin = Math.max(0, Math.floor(topLeft.tx));
    const tileXMax = Math.min(n - 1, Math.floor(bottomRight.tx));
    const tileYMin = Math.max(0, Math.floor(topLeft.ty));
    const tileYMax = Math.min(n - 1, Math.floor(bottomRight.ty));
    if (tileXMax - tileXMin > 24 || tileYMax - tileYMin > 24) return; // absurd extent: do not request

    c.fillStyle = BASE_FILL;
    c.fillRect(0, 0, cw, ch);

    // Draw nearest-to-centre first so they also load first
    const cx = (tileXMin + tileXMax) / 2;
    const cy = (tileYMin + tileYMax) / 2;
    const list: { key: string; z: number; x: number; y: number; d: number }[] = [];
    for (let tx = tileXMin; tx <= tileXMax; tx++) {
      for (let ty = tileYMin; ty <= tileYMax; ty++) {
        list.push({ key: tileKey(zoom, tx, ty), z: zoom, x: tx, y: ty, d: (tx - cx) ** 2 + (ty - cy) ** 2 });
      }
    }
    list.sort((a, b) => a.d - b.d);
    wanted = list;

    for (const t of list) {
      // Prefer the exact tile; while it loads (or has failed), stretch a cached ancestor.
      let img = cached(t.key);
      let sx = 0;
      let sy = 0;
      let sw = TILE_SIZE;
      if (!img) {
        for (let k = 1; k <= 3 && t.z - k >= provider.minZoom; k++) {
          const p = cached(tileKey(t.z - k, t.x >> k, t.y >> k));
          if (p) {
            img = p;
            sw = TILE_SIZE / (1 << k);
            sx = (t.x & ((1 << k) - 1)) * sw;
            sy = (t.y & ((1 << k) - 1)) * sw;
            break;
          }
        }
      }
      if (!img) continue;

      // Tiles are Web Mercator while the screen is azimuthal equidistant about the radar, so a
      // tile is drawn as a small mesh of affine cells (meridians converge, parallels curve).
      const [, tlLon] = tileToLatLon(t.x, t.y, zoom);
      const [, brLon] = tileToLatLon(t.x + 1, t.y + 1, zoom);
      const [tlLat] = tileToLatLon(t.x, t.y, zoom);
      const [brLat] = tileToLatLon(t.x, t.y + 1, zoom);
      const [ax, ay] = project(tlLat, tlLon);
      const [bx, by] = project(brLat, brLon);
      if (Math.max(ax, bx) < -TILE_SIZE * 2 || Math.min(ax, bx) > cw + TILE_SIZE * 2) continue;
      if (Math.max(ay, by) < -TILE_SIZE * 2 || Math.min(ay, by) > ch + TILE_SIZE * 2) continue;
      const cells = Math.max(1, Math.min(8, Math.ceil(Math.max(Math.abs(bx - ax), Math.abs(by - ay)) / 64)));
      drawMesh(
        c, img, sx, sy, sw, sw, cells, cells,
        (u) => tlLon + u * (brLon - tlLon),
        (v) => tileToLatLon(t.x, t.y + v, zoom)[0],
        project, dpr, cw, ch,
      );
    }
    pump();
  }

  // ── Mosaic drawing ─────────────────────────────────────────────────────────
  const DEG = Math.PI / 180;
  const mercY = (lat: number) => Math.log(Math.tan(Math.PI / 4 + (lat * DEG) / 2));
  const invMercY = (y: number) => Math.atan(Math.sinh(y)) / DEG;
  const MOSAIC_CELLS = 40;

  /**
   * Draw `img` (source rect sx,sy,sw,sh) warped onto the screen as an nx*ny mesh of affine cells.
   * u,v in [0,1] across the source rect map to (lon, lat) through lonAt/latAt and then to the
   * screen through `project`; each cell is the parallelogram through its top-left, top-right and
   * bottom-left corners, which is exact to well under a pixel at ~64 px cells. Cells overlap by
   * ~0.6 px on their right/bottom edges so no hairline seams show.
   */
  function drawMesh(
    c: CanvasRenderingContext2D,
    img: CanvasImageSource,
    sx: number,
    sy: number,
    sw: number,
    sh: number,
    nx: number,
    ny: number,
    lonAt: (u: number) => number,
    latAt: (v: number) => number,
    project: (lat: number, lon: number) => [number, number],
    dpr: number,
    cw: number,
    ch: number,
  ) {
    const lons: number[] = [];
    const lats: number[] = [];
    for (let i = 0; i <= nx; i++) lons.push(lonAt(i / nx));
    for (let j = 0; j <= ny; j++) lats.push(latAt(j / ny));
    const pts: [number, number][][] = [];
    for (let i = 0; i <= nx; i++) {
      const col: [number, number][] = [];
      for (let j = 0; j <= ny; j++) col.push(project(lats[j], lons[i]));
      pts.push(col);
    }
    const cellW = sw / nx;
    const cellH = sh / ny;
    for (let i = 0; i < nx; i++) {
      for (let j = 0; j < ny; j++) {
        const p00 = pts[i][j];
        const p10 = pts[i + 1][j];
        const p01 = pts[i][j + 1];
        const p11 = pts[i + 1][j + 1];
        const minX = Math.min(p00[0], p10[0], p01[0], p11[0]);
        const maxX = Math.max(p00[0], p10[0], p01[0], p11[0]);
        const minY = Math.min(p00[1], p10[1], p01[1], p11[1]);
        const maxY = Math.max(p00[1], p10[1], p01[1], p11[1]);
        if (maxX < 0 || minX > cw || maxY < 0 || minY > ch) continue;
        const ex = [p10[0] - p00[0], p10[1] - p00[1]];
        const ey = [p01[0] - p00[0], p01[1] - p00[1]];
        const pxPerSrcX = Math.hypot(ex[0], ex[1]) / cellW;
        const pxPerSrcY = Math.hypot(ey[0], ey[1]) / cellH;
        if (!(pxPerSrcX > 0) || !(pxPerSrcY > 0)) continue;
        const padX = i < nx - 1 ? Math.min(0.6 / pxPerSrcX, cellW * 0.5) : 0;
        const padY = j < ny - 1 ? Math.min(0.6 / pxPerSrcY, cellH * 0.5) : 0;
        c.setTransform(
          (dpr * ex[0]) / cellW, (dpr * ex[1]) / cellW,
          (dpr * ey[0]) / cellH, (dpr * ey[1]) / cellH,
          dpr * p00[0], dpr * p00[1],
        );
        c.drawImage(img, sx + i * cellW, sy + j * cellH, cellW + padX, cellH + padY, 0, 0, cellW + padX, cellH + padY);
      }
    }
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  let mosaicBuf: HTMLCanvasElement | null = null;

  /**
   * The PNG is uniform in Web Mercator (x linear in lon, y linear in mercator-y); it is drawn
   * as a warped mesh so it lands on the same azimuthal-equidistant screen mapping as the tiles,
   * sites and PPI. The mesh is composited at full alpha into a scratch canvas first so that
   * the layer opacity is applied once (no double blending where cells overlap).
   */
  function drawMosaic(
    c: CanvasRenderingContext2D,
    lyr: MosaicLayer,
    dpr: number,
    project: (lat: number, lon: number) => [number, number],
  ) {
    const img = lyr.image;
    const W = img.naturalWidth;
    const H = img.naturalHeight;
    if (!W || !H || lyr.corners.length < 4) return;
    const [nw, , se] = lyr.corners;
    const west = nw[0];
    const east = se[0];
    const yN = mercY(nw[1]);
    const yS = mercY(se[1]);
    const [x0, y0] = project(nw[1], west);
    const [x1, y1] = project(se[1], east);
    if (Math.max(x0, x1) < 0 || Math.min(x0, x1) > containerWidth) return;
    if (Math.max(y0, y1) < 0 || Math.min(y0, y1) > containerHeight) return;
    const ext = Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0));
    const n = Math.max(4, Math.min(MOSAIC_CELLS, Math.ceil(ext / 48)));

    if (!mosaicBuf) mosaicBuf = document.createElement('canvas');
    if (mosaicBuf.width !== c.canvas.width || mosaicBuf.height !== c.canvas.height) {
      mosaicBuf.width = c.canvas.width;
      mosaicBuf.height = c.canvas.height;
    }
    const b = mosaicBuf.getContext('2d');
    if (!b) return;
    b.setTransform(1, 0, 0, 1, 0, 0);
    b.clearRect(0, 0, mosaicBuf.width, mosaicBuf.height);
    b.setTransform(dpr, 0, 0, dpr, 0, 0);
    b.imageSmoothingEnabled = true;
    drawMesh(
      b, img, 0, 0, W, H, n, n,
      (u) => west + u * (east - west),
      (v) => invMercY(yN + v * (yS - yN)),
      project, dpr, containerWidth, containerHeight,
    );
    c.save();
    c.setTransform(1, 0, 0, 1, 0, 0);
    c.globalAlpha = lyr.opacity;
    c.drawImage(mosaicBuf, 0, 0);
    c.restore();
  }

  /** Radar markers + range rings for each radar in the mosaic. */
  function drawSites(
    c: CanvasRenderingContext2D,
    lyr: MosaicLayer,
    project: (lat: number, lon: number) => [number, number],
  ) {
    c.save();
    c.lineJoin = 'round';
    for (const r of lyr.radars) {
      // Range ring (great-circle distance from the site)
      if (lyr.ringKm > 0) {
        c.beginPath();
        for (let i = 0; i <= 120; i++) {
          const [la, lo] = radarToLatLon(lyr.ringKm * 1000, (i * 360) / 120, r.latitude, r.longitude);
          const [x, y] = project(la, lo);
          if (i === 0) c.moveTo(x, y);
          else c.lineTo(x, y);
        }
        c.setLineDash([6, 5]);
        c.lineWidth = 1.25;
        c.strokeStyle = r.color;
        c.globalAlpha = 0.75;
        c.stroke();
        c.setLineDash([]);
      }
      // Site marker + name
      const [x, y] = project(r.latitude, r.longitude);
      c.globalAlpha = 1;
      c.beginPath();
      c.arc(x, y, 5, 0, Math.PI * 2);
      c.fillStyle = r.color;
      c.fill();
      c.lineWidth = 1.5;
      c.strokeStyle = surface(0, 0.95);
      c.stroke();
      c.font = '600 11px Inter, system-ui, sans-serif';
      c.textBaseline = 'middle';
      c.lineWidth = 3;
      c.strokeStyle = surface(0, 0.85);
      c.strokeText(r.name, x + 9, y);
      c.fillStyle = text();
      c.fillText(r.name, x + 9, y);
    }
    c.restore();
  }

  // ── Hover probe ────────────────────────────────────────────────────────────
  let attachedTo: HTMLElement | null = null;
  let mouseDown = false;

  $: if (rootEl) {
    if (probing) attachProbe();
    else {
      detachProbe();
      clearMosaicProbe();
    }
  }

  function attachProbe() {
    const el = rootEl?.parentElement;
    if (!el || attachedTo === el) return;
    detachProbe();
    attachedTo = el;
    el.addEventListener('mousemove', onProbeMove);
    el.addEventListener('mouseleave', onProbeLeave);
    el.addEventListener('mousedown', onProbeDown);
    window.addEventListener('mouseup', onProbeUp);
  }

  function detachProbe() {
    if (attachedTo) {
      attachedTo.removeEventListener('mousemove', onProbeMove);
      attachedTo.removeEventListener('mouseleave', onProbeLeave);
      attachedTo.removeEventListener('mousedown', onProbeDown);
    }
    window.removeEventListener('mouseup', onProbeUp);
    attachedTo = null;
    mouseDown = false;
  }

  function onProbeDown() {
    mouseDown = true;
    clearMosaicProbe();
  }
  function onProbeUp() {
    mouseDown = false;
  }
  function onProbeLeave() {
    clearMosaicProbe();
  }

  function onProbeMove(e: MouseEvent) {
    const lyr = layer;
    if (!lyr || !lyr.visible || mouseDown || !attachedTo) return;
    const rect = attachedTo.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;
    const [lat, lon] = pixelToLatLon(
      px, py, radarLat, radarLon, maxRange, scale, translateX, translateY, containerWidth, containerHeight,
    );
    const [nw, , se] = lyr.corners;
    const west = nw[0];
    const east = se[0];
    const north = nw[1];
    const south = se[1];
    if (lon < west || lon > east || lat < south || lat > north) {
      clearMosaicProbe();
      return;
    }
    probeMosaic(lon, lat, px, py);
  }

  function fmtValue(v: number | null, units: string): string {
    if (v === null || !Number.isFinite(v)) return 'no data';
    return `${Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(1)} ${units}`.trim();
  }

  function radarColor(name: string): string {
    return layer?.radars.find((r) => r.name === name)?.color ?? '#8b9cf8';
  }

  function legendGradient(l: MosaicLayer): string {
    const span = l.vmax - l.vmin || 1;
    const stops = [...l.legend].sort((a, b) => a[0] - b[0]);
    if (stops.length === 0) return 'linear-gradient(90deg, var(--c-surface-3), var(--c-text-dim))';
    return `linear-gradient(90deg, ${stops
      .map(([v, col]) => `${col} ${Math.max(0, Math.min(100, ((v - l.vmin) / span) * 100)).toFixed(1)}%`)
      .join(', ')})`;
  }
</script>

<canvas
  bind:this={canvasEl}
  class="map-background-canvas"
  style="filter: {basemap.filter}; opacity: {opacity}"
  aria-hidden="true"
></canvas>
<canvas bind:this={mosaicCanvasEl} class="map-background-canvas mosaic-canvas" aria-hidden="true"></canvas>

<div class="mosaic-root" bind:this={rootEl}>
  {#if hasTiles && provider.attribution}
    <div class="map-attribution" title={provider.attributionUrl ?? ''}>{provider.attribution}</div>
  {/if}
  {#if probing && layer}
    <div class="mosaic-legend" aria-label="Mosaic legend">
      <span class="legend-title">{layer.variable}</span>
      <span class="legend-end">{layer.vmin}</span>
      <span class="legend-bar" style="background: {legendGradient(layer)}"></span>
      <span class="legend-end">{layer.vmax} {layer.units}</span>
    </div>
    {#if $mosaicProbe?.result}
      {@const pr = $mosaicProbe.result}
      <div
        class="mosaic-tooltip"
        style="left: {Math.min($mosaicProbe.x + 14, Math.max(0, containerWidth - 190))}px; top: {Math.max(4, $mosaicProbe.y - 8)}px"
      >
        <div class="mt-value" class:muted={pr.value === null}>{fmtValue(pr.value, pr.units)}</div>
        <div class="mt-pos">{pr.lat.toFixed(3)}, {pr.lon.toFixed(3)}</div>
        {#each pr.radars as r (r.name)}
          <div class="mt-radar">
            <span class="mt-chip" style="background: {radarColor(r.name)}"></span>
            <span class="mt-name">{r.name}</span>
            <span class="mt-num">{fmtValue(r.value, pr.units)}</span>
            <span class="mt-sub">{r.range_km.toFixed(0)} km, {r.elevation_deg.toFixed(1)}&deg;</span>
          </div>
        {/each}
      </div>
    {/if}
  {/if}
</div>

<style>
  .map-background-canvas {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    z-index: 0;
    pointer-events: none;
  }

  .map-attribution {
    position: absolute;
    right: 0;
    bottom: 0;
    padding: 1px 6px;
    font-size: 10px;
    line-height: 1.4;
    color: var(--c-text-dim);
    background: rgba(var(--c-surface-1-rgb), 0.82);
    border-top-left-radius: 3px;
    pointer-events: none;
    user-select: none;
  }

  .mosaic-root {
    position: absolute;
    inset: 0;
    pointer-events: none;
    z-index: 20;
    overflow: hidden;
  }

  .mosaic-legend {
    position: absolute;
    bottom: var(--spacing-md, 12px);
    left: 50%;
    transform: translateX(-50%);
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    font-family: var(--font-mono, monospace);
    font-size: 10px;
    color: var(--text-secondary);
    background: var(--glass-bg-heavy);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm, 6px);
    white-space: nowrap;
  }

  .legend-title {
    font-weight: 700;
    color: var(--text-primary);
  }

  .legend-bar {
    width: 180px;
    height: 10px;
    border-radius: 2px;
    border: 1px solid var(--border-light);
  }

  .mosaic-tooltip {
    position: absolute;
    min-width: 150px;
    padding: 5px 8px;
    font-family: var(--font-mono, monospace);
    font-size: 11px;
    color: var(--text-primary);
    background: var(--glass-bg-heavy);
    border: 1px solid var(--accent-primary);
    border-radius: 4px;
    white-space: nowrap;
    backdrop-filter: blur(4px);
  }

  .mt-value {
    font-size: 13px;
    font-weight: 700;
  }

  .mt-value.muted {
    color: var(--text-muted);
    font-weight: 500;
  }

  .mt-pos {
    color: var(--text-muted);
    font-size: 10px;
    margin-bottom: 3px;
  }

  .mt-radar {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 10px;
  }

  .mt-chip {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .mt-name {
    min-width: 36px;
  }

  .mt-num {
    margin-left: auto;
    color: var(--text-primary);
  }

  .mt-sub {
    color: var(--text-muted);
  }
</style>
