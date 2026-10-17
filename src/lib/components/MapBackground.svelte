<script lang="ts">
  /**
   * MapBackground — renders slippy-map tiles on a canvas behind the radar PPI.
   *
   * Uses CartoDB basemap tiles (dark/light) with no API key required.
   * Tiles are fetched, cached, and painted in sync with the PPI view transform.
   *
   * It also draws the radar mosaic (a Web-Mercator PNG placed by its four corners) with
   * the radar sites and range rings, between the base tiles and a labels-only tile layer,
   * and shows a hover tooltip (value + per-radar contributions) while the mosaic view is on.
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
  import { mosaicLayer, mosaicProbe, probeMosaic, clearMosaicProbe, type MosaicLayer } from '../stores/mosaic';

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

  let canvasEl: HTMLCanvasElement;
  let ctx: CanvasRenderingContext2D | null = null;
  let themeMode: 'dark' | 'light' | 'auto' = 'dark';
  let rafId: number = 0;
  let rootEl: HTMLDivElement;
  let layer: MosaicLayer | null = null;
  $: layer = $mosaicLayer;
  $: mosaicShown = !!layer && layer.visible;
  $: probing = mosaicShown && !!layer?.view;

  // Tile image cache: "kind/z/x/y" -> HTMLImageElement
  const tileCache = new Map<string, HTMLImageElement>();
  const loadingTiles = new Set<string>();
  const TILE_SIZE = 256;
  const MAX_CACHE = 512;

  $: themeMode = $settings.theme.mode;

  // Determine tile URL template based on theme
  $: tileUrlBase =
    themeMode === 'light'
      ? 'https://a.basemaps.cartocdn.com/light_all'
      : 'https://a.basemaps.cartocdn.com/dark_all';

  // Re-render whenever view params change
  $: if (ctx && radarLat != null && radarLon != null) {
    // Trigger reactive update on all view params
    void (scale, translateX, translateY, containerWidth, containerHeight, maxRange, opacity, tileUrlBase, showTiles, layer);
    scheduleRender();
  }

  onMount(() => {
    ctx = canvasEl.getContext('2d');
    scheduleRender();
  });

  onDestroy(() => {
    if (rafId) cancelAnimationFrame(rafId);
    detachProbe();
    clearMosaicProbe();
  });

  function scheduleRender() {
    if (rafId) cancelAnimationFrame(rafId);
    rafId = requestAnimationFrame(renderTiles);
  }

  type TileKind = 'base' | 'labels';

  function tileUrl(kind: TileKind, z: number, x: number, y: number): string {
    const base = kind === 'base' ? tileUrlBase : tileUrlBase.replace(/(dark|light)_all$/, '$1_only_labels');
    return `${base}/${z}/${x}/${y}.png`;
  }

  function tileKey(kind: TileKind, z: number, x: number, y: number): string {
    return `${kind}/${themeMode === 'light' ? 'l' : 'd'}/${z}/${x}/${y}`;
  }

  function loadTile(kind: TileKind, z: number, x: number, y: number): HTMLImageElement | null {
    const key = tileKey(kind, z, x, y);
    if (tileCache.has(key)) return tileCache.get(key)!;
    if (loadingTiles.has(key)) return null;

    loadingTiles.add(key);
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.onload = () => {
      loadingTiles.delete(key);
      // Evict oldest if cache too large
      if (tileCache.size >= MAX_CACHE) {
        const first = tileCache.keys().next().value;
        if (first !== undefined) tileCache.delete(first);
      }
      tileCache.set(key, img);
      scheduleRender();
    };
    img.onerror = () => {
      loadingTiles.delete(key);
    };
    img.src = tileUrl(kind, z, x, y);
    return null;
  }

  function renderTiles() {
    rafId = 0;
    if (!ctx || !canvasEl || radarLat == null || radarLon == null) return;

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const cw = containerWidth;
    const ch = containerHeight;
    canvasEl.width = Math.round(cw * dpr);
    canvasEl.height = Math.round(ch * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cw, ch);
    ctx.globalAlpha = opacity;

    const zoom = computeTileZoom(maxRange, scale, cw);

    // Find the lat/lon bounds of the visible area by sampling screen corners
    const corners: [number, number][] = [];
    for (const px of [0, cw]) {
      for (const py of [0, ch]) {
        // Reverse-project screen pixel to data-space, then to lat/lon
        const ndcX = (px / cw) * 2 - 1;
        const ndcY = 1 - (py / ch) * 2;
        const aspect = cw / ch;
        const sx = (aspect >= 1 ? scale / aspect : scale) / maxRange;
        const sy = (aspect >= 1 ? scale : scale * aspect) / maxRange;
        const dataX = ndcX / sx - translateX;
        const dataY = ndcY / sy - translateY;
        // data-space to lat/lon (azimuthal equidistant)
        const cosRadarLat = Math.cos(radarLat * (Math.PI / 180));
        const lat = radarLat + (dataY / 6371000) * (180 / Math.PI);
        const lon = radarLon + (dataX / (6371000 * cosRadarLat)) * (180 / Math.PI);
        corners.push([lat, lon]);
      }
    }

    // Get tile bounds
    const lats = corners.map((c) => c[0]);
    const lons = corners.map((c) => c[1]);
    const minLat = Math.min(...lats);
    const maxLat = Math.max(...lats);
    const minLon = Math.min(...lons);
    const maxLon = Math.max(...lons);

    const topLeft = latLonToTile(maxLat, minLon, zoom);
    const bottomRight = latLonToTile(minLat, maxLon, zoom);

    const tileXMin = Math.max(0, Math.floor(topLeft.tx) - 1);
    const tileXMax = Math.min(Math.pow(2, zoom) - 1, Math.ceil(bottomRight.tx) + 1);
    const tileYMin = Math.max(0, Math.floor(topLeft.ty) - 1);
    const tileYMax = Math.min(Math.pow(2, zoom) - 1, Math.ceil(bottomRight.ty) + 1);

    const c = ctx;
    const project = (lat: number, lon: number): [number, number] =>
      latLonToPixel(lat, lon, radarLat, radarLon, maxRange, scale, translateX, translateY, cw, ch);

    const drawTiles = (kind: TileKind) => {
      for (let tx = tileXMin; tx <= tileXMax; tx++) {
        for (let ty = tileYMin; ty <= tileYMax; ty++) {
          const img = loadTile(kind, zoom, tx, ty);
          if (!img) continue;

          // Get the four corners of this tile in lat/lon
          const [tlLat, tlLon] = tileToLatLon(tx, ty, zoom);
          const [brLat, brLon] = tileToLatLon(tx + 1, ty + 1, zoom);

          // Project tile corners to screen pixels
          const [tlPx, tlPy] = project(tlLat, tlLon);
          const [brPx, brPy] = project(brLat, brLon);

          const drawW = brPx - tlPx;
          const drawH = brPy - tlPy;

          if (drawW > 0.5 && drawH > 0.5) {
            c.drawImage(img, tlPx, tlPy, drawW, drawH);
          }
        }
      }
    };

    if (showTiles) drawTiles('base');

    const lyr = layer;
    if (lyr && lyr.visible) {
      c.globalAlpha = 1;
      drawMosaic(c, lyr, dpr, project);
      // Labels on top of the mosaic so place names stay readable
      if (showTiles) {
        c.globalAlpha = opacity;
        drawTiles('labels');
      }
      c.globalAlpha = 1;
      drawSites(c, lyr, project);
    }

    ctx.globalAlpha = 1;
  }

  // ── Mosaic drawing ─────────────────────────────────────────────────────────
  const DEG = Math.PI / 180;
  const mercY = (lat: number) => Math.log(Math.tan(Math.PI / 4 + (lat * DEG) / 2));
  const invMercY = (y: number) => Math.atan(Math.sinh(y)) / DEG;
  const MOSAIC_STRIPS = 64;

  /**
   * The PNG is uniform in Web Mercator (x linear in lon, y linear in mercator-y) and the
   * screen mapping from (lon, lat) is affine and axis-aligned (local azimuthal-equidistant
   * approximation), so every horizontal strip of the image maps to a screen rectangle. Strips
   * share device-pixel-snapped edges: no hairline seams and no double blending of
   * semi-transparent pixels. 64 strips keep the mercator non-linearity far below a pixel.
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
    const snap = (v: number) => Math.round(v * dpr) / dpr;

    const [x0raw] = project(nw[1], west);
    const [x1raw] = project(nw[1], east);
    const dx0 = snap(x0raw);
    const dx1 = snap(x1raw);
    if (!(dx1 - dx0 > 0)) return;

    c.save();
    c.globalAlpha = lyr.opacity;
    c.imageSmoothingEnabled = true;
    let prevY = snap(project(invMercY(yN), west)[1]);
    for (let k = 0; k < MOSAIC_STRIPS; k++) {
      const v1 = (k + 1) / MOSAIC_STRIPS;
      const y1 = snap(project(invMercY(yN + v1 * (yS - yN)), west)[1]);
      const top = Math.min(prevY, y1);
      const h = Math.abs(y1 - prevY);
      const sy0 = (k / MOSAIC_STRIPS) * H;
      const sh = H / MOSAIC_STRIPS;
      prevY = y1;
      if (h <= 0) continue;
      if (top > containerHeight || top + h < 0 || dx1 < 0 || dx0 > containerWidth) continue;
      c.drawImage(img, 0, sy0, W, sh, dx0, top, dx1 - dx0, h);
    }
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
      c.strokeStyle = 'rgba(8, 10, 20, 0.95)';
      c.stroke();
      c.font = '600 11px Inter, system-ui, sans-serif';
      c.textBaseline = 'middle';
      c.lineWidth = 3;
      c.strokeStyle = 'rgba(8, 10, 20, 0.85)';
      c.strokeText(r.name, x + 9, y);
      c.fillStyle = '#f2f4ff';
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
    if (stops.length === 0) return 'linear-gradient(90deg, #444, #ddd)';
    return `linear-gradient(90deg, ${stops
      .map(([v, col]) => `${col} ${Math.max(0, Math.min(100, ((v - l.vmin) / span) * 100)).toFixed(1)}%`)
      .join(', ')})`;
  }
</script>

<canvas
  bind:this={canvasEl}
  class="map-background-canvas"
  aria-hidden="true"
></canvas>

<div class="mosaic-root" bind:this={rootEl}>
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
    color: var(--text-secondary, #9da2c2);
    background: var(--glass-bg-heavy, rgba(8, 10, 20, 0.85));
    border: 1px solid var(--glass-border, rgba(140, 160, 250, 0.1));
    border-radius: var(--radius-sm, 6px);
    white-space: nowrap;
  }

  .legend-title {
    font-weight: 700;
    color: var(--text-primary, #e4e6f4);
  }

  .legend-bar {
    width: 180px;
    height: 10px;
    border-radius: 2px;
    border: 1px solid var(--border-light, rgba(140, 160, 250, 0.14));
  }

  .mosaic-tooltip {
    position: absolute;
    min-width: 150px;
    padding: 5px 8px;
    font-family: var(--font-mono, monospace);
    font-size: 11px;
    color: var(--text-primary, #e4e6f4);
    background: var(--glass-bg-heavy, rgba(8, 10, 20, 0.92));
    border: 1px solid var(--accent-primary, #5b6cf7);
    border-radius: 4px;
    white-space: nowrap;
    backdrop-filter: blur(4px);
  }

  .mt-value {
    font-size: 13px;
    font-weight: 700;
  }

  .mt-value.muted {
    color: var(--text-muted, #5c6088);
    font-weight: 500;
  }

  .mt-pos {
    color: var(--text-muted, #5c6088);
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
    color: var(--text-primary, #e4e6f4);
  }

  .mt-sub {
    color: var(--text-muted, #5c6088);
  }
</style>
