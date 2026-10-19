<script lang="ts">
  /**
   * CrossSectionLine -- SVG overlay on the PPI viewer.
   * Click two points to define a cross-section line.
   * Draggable endpoints with A/B labels.
   *
   * The line is anchored to the ground (lat/lon), so it stays on the same place of the data while
   * the PPI is panned or zoomed; pixel positions are derived from the current view.
   */
  import { onDestroy } from 'svelte';
  import {
    crossSectionStore,
    dockPreferred,
    requestRhi,
    requestSection,
    setCrossSectionLayout,
    type CrossSectionLinePoints,
  } from '../stores/crossSection';
  import { azRangeToXY, xyToAzRange } from '../utils/crossSectionGeometry';
  import { dataSpaceToLatLon, latLonToDataSpace } from '../utils/geoProjection';
  import { createEventDispatcher } from 'svelte';

  export let active: boolean = false;
  export let scale: number = 1;
  export let translateX: number = 0;
  export let translateY: number = 0;
  export let maxRange: number = 1;
  export let containerWidth: number = 800;
  export let containerHeight: number = 600;

  /** Radar location for coordinate conversion */
  export let radarLat: number = 0;
  export let radarLon: number = 0;

  const dispatch = createEventDispatcher<{
    lineDrawn: CrossSectionLinePoints;
  }>();

  interface GeoPoint { lat: number; lon: number }
  interface Px { x: number; y: number }

  // Drawing state
  let phase: 'idle' | 'first-placed' | 'complete' = 'idle';
  let geoA: GeoPoint | null = null;
  let geoB: GeoPoint | null = null;
  let draggingEndpoint: 'start' | 'end' | null = null;
  let suppressClick = false;
  let mouseX = 0;
  let mouseY = 0;
  let svgEl: SVGSVGElement | undefined;

  // The store is the source of truth once a line is complete (the panel can clear it).
  function syncFromStore(line: CrossSectionLinePoints | null) {
    if (line) {
      if (draggingEndpoint) return;
      geoA = { lat: line.startLat, lon: line.startLon };
      geoB = { lat: line.endLat, lon: line.endLon };
      phase = 'complete';
    } else if (phase === 'complete') {
      phase = 'idle';
      geoA = geoB = null;
    }
  }
  $: syncFromStore($crossSectionStore.line);

  // Data coordinate conversion helpers
  function viewFactors(w: number, h: number, sc: number, mr: number) {
    const aspect = w / h;
    return {
      sx: (aspect >= 1 ? sc / aspect : sc) / mr,
      sy: (aspect >= 1 ? sc : sc * aspect) / mr,
    };
  }

  function pxToData(px: number, py: number): [number, number] {
    // Convert pixel -> NDC -> data (metres from radar)
    const ndcX = (px / containerWidth) * 2 - 1;
    const ndcY = 1 - (py / containerHeight) * 2;
    const { sx, sy } = viewFactors(containerWidth, containerHeight, scale, maxRange);
    const xm = (ndcX - translateX * sx) / sx;
    const ym = (ndcY - translateY * sy) / sy;
    return [xm, ym];
  }

  // The exact projection the PPI and the map are drawn in (and the sidecar's geodesic extraction
  // agrees with), not a flat dLat/dLon approximation.
  function dataToLatLon(xm: number, ym: number): GeoPoint {
    const [lat, lon] = dataSpaceToLatLon(xm, ym, radarLat, radarLon);
    return { lat, lon };
  }

  function pxToGeo(px: number, py: number): GeoPoint {
    const [xm, ym] = pxToData(px, py);
    return dataToLatLon(xm, ym);
  }

  /** Ground point -> screen pixels for the given view (explicit arguments keep `$:` reactive). */
  function geoToPx(
    p: GeoPoint | null,
    sc: number, tx: number, ty: number, mr: number, w: number, h: number, rLat: number, rLon: number,
  ): Px | null {
    if (!p) return null;
    const [xm, ym] = latLonToDataSpace(p.lat, p.lon, rLat, rLon);
    const { sx, sy } = viewFactors(w, h, sc, mr);
    const ndcX = (xm + tx) * sx;
    const ndcY = (ym + ty) * sy;
    return { x: ((ndcX + 1) / 2) * w, y: ((1 - ndcY) / 2) * h };
  }

  $: startPx = geoToPx(geoA, scale, translateX, translateY, maxRange, containerWidth, containerHeight, radarLat, radarLon);
  $: endPx = geoToPx(geoB, scale, translateX, translateY, maxRange, containerWidth, containerHeight, radarLat, radarLon);

  function localXY(e: MouseEvent): Px {
    const rect = (svgEl ?? (e.currentTarget as SVGElement)).getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }

  function handleClick(e: MouseEvent) {
    if (suppressClick) {
      suppressClick = false;
      return;
    }
    if (!active || draggingEndpoint) return;
    const { x, y } = localXY(e);

    if ($crossSectionStore.rhiToolActive) {
      pickRhi(x, y);
      return;
    }

    if (phase === 'idle' || phase === 'complete') {
      // Start a (new) line; a previous one is dropped together with its result.
      geoA = pxToGeo(x, y);
      geoB = null;
      mouseX = x;
      mouseY = y;
      phase = 'first-placed';
      crossSectionStore.update((s) => ({ ...s, line: null }));
    } else if (phase === 'first-placed') {
      geoB = pxToGeo(x, y);
      phase = 'complete';
      emitLine();
    }
  }

  /** One click on the PPI: an RHI-style cut out of the radar along that azimuth. */
  function pickRhi(px: number, py: number) {
    if (radarLat === 0 && radarLon === 0) return;
    const [xm, ym] = pxToData(px, py);
    const { azimuth } = xyToAzRange(xm, ym);
    const st = $crossSectionStore;
    const rangeKm = Math.max(1, Math.round(st.rhiRangeKm ?? Math.min(st.ppiMaxRangeKm ?? maxRange / 1000, 150)));
    const [ex, ey] = azRangeToXY(azimuth, rangeKm);
    const end = dataToLatLon(ex, ey);
    const a = geoToPx({ lat: radarLat, lon: radarLon }, scale, translateX, translateY, maxRange, containerWidth, containerHeight, radarLat, radarLon);
    const b = geoToPx(end, scale, translateX, translateY, maxRange, containerWidth, containerHeight, radarLat, radarLon);
    const line: CrossSectionLinePoints = {
      startX: a?.x ?? 0,
      startY: a?.y ?? 0,
      endX: b?.x ?? 0,
      endY: b?.y ?? 0,
      startLat: radarLat,
      startLon: radarLon,
      endLat: end.lat,
      endLon: end.lon,
    };
    crossSectionStore.update((s) => ({ ...s, rhiToolActive: false, lineToolActive: false }));
    requestRhi(azimuth, rangeKm, line);
    if ($crossSectionStore.layout === 'panel' && dockPreferred()) setCrossSectionLayout('dock');
  }

  function handleMouseMove(e: MouseEvent) {
    const { x, y } = localXY(e);
    mouseX = x;
    mouseY = y;
  }

  function startDragEndpoint(which: 'start' | 'end', e: MouseEvent) {
    if (e.button !== 0) return;
    e.stopPropagation();
    e.preventDefault();
    draggingEndpoint = which;
    window.addEventListener('mousemove', onWindowMove);
    window.addEventListener('mouseup', onWindowUp);
  }

  function onWindowMove(e: MouseEvent) {
    if (!draggingEndpoint || !svgEl) return;
    const rect = svgEl.getBoundingClientRect();
    const g = pxToGeo(e.clientX - rect.left, e.clientY - rect.top);
    if (draggingEndpoint === 'start') geoA = g;
    else geoB = g;
  }

  function onWindowUp() {
    window.removeEventListener('mousemove', onWindowMove);
    window.removeEventListener('mouseup', onWindowUp);
    if (draggingEndpoint) {
      draggingEndpoint = null;
      suppressClick = true; // the click that follows the drag must not start a new line
      setTimeout(() => (suppressClick = false), 0);
      emitLine();
    }
  }

  function onKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape' && active && phase === 'first-placed') {
      phase = 'idle';
      geoA = null;
      e.stopPropagation();
    }
  }

  onDestroy(() => {
    window.removeEventListener('mousemove', onWindowMove);
    window.removeEventListener('mouseup', onWindowUp);
  });

  // Leaving the tool half-way (e.g. pressing Draw Line again) forgets the first point.
  $: if (!active && phase === 'first-placed') {
    phase = 'idle';
    geoA = null;
  }

  function emitLine() {
    if (!geoA || !geoB) return;
    // Derived pixels update on the next flush, so project here.
    const a = geoToPx(geoA, scale, translateX, translateY, maxRange, containerWidth, containerHeight, radarLat, radarLon);
    const b = geoToPx(geoB, scale, translateX, translateY, maxRange, containerWidth, containerHeight, radarLat, radarLon);
    if (!a || !b) return;
    const line: CrossSectionLinePoints = {
      startX: a.x,
      startY: a.y,
      endX: b.x,
      endY: b.y,
      startLat: geoA.lat,
      startLon: geoA.lon,
      endLat: geoB.lat,
      endLon: geoB.lon,
    };

    // Drawing a line ends the tool so the PPI can be panned again; the endpoints stay draggable.
    // A free line (not an RHI any more); it is extracted right away, and shown large when that is
    // the user's preference.
    crossSectionStore.update((s) => ({ ...s, line, lineToolActive: false, rhi: null }));
    dispatch('lineDrawn', line);
    requestSection();
    if ($crossSectionStore.layout === 'panel' && dockPreferred()) setCrossSectionLayout('dock');
  }

  function requestCrossSection() {
    if ($crossSectionStore.line) crossSectionStore.update((s) => ({ ...s, requestSeq: s.requestSeq + 1 }));
  }

  function clearLine() {
    phase = 'idle';
    geoA = geoB = null;
    crossSectionStore.update((s) => ({ ...s, line: null }));
  }
</script>

<svelte:window on:keydown={onKeydown} />

<!-- svelte-ignore a11y_no_static_element_interactions, a11y_click_events_have_key_events -->
<svg
  bind:this={svgEl}
  class="cs-line-overlay"
  class:active
  on:click={handleClick}
  on:mousemove={handleMouseMove}
>
  {#if phase === 'first-placed' && startPx}
    <!-- Rubber-band line from start to cursor -->
    <line
      x1={startPx.x} y1={startPx.y}
      x2={mouseX} y2={mouseY}
      class="cs-line rubber"
    />
    <circle cx={startPx.x} cy={startPx.y} r="6" class="cs-endpoint" />
    <text x={startPx.x - 12} y={startPx.y - 10} class="cs-label">A</text>
  {/if}

  {#if phase === 'complete' && startPx && endPx}
    <!-- Drawn line -->
    <line
      x1={startPx.x} y1={startPx.y}
      x2={endPx.x} y2={endPx.y}
      class="cs-line"
    />

    <!-- Start endpoint (draggable) -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <circle
      cx={startPx.x} cy={startPx.y} r="7"
      class="cs-endpoint draggable"
      on:mousedown={(e) => startDragEndpoint('start', e)}
    />
    <text x={startPx.x - 14} y={startPx.y - 12} class="cs-label">A</text>

    <!-- End endpoint (draggable) -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <circle
      cx={endPx.x} cy={endPx.y} r="7"
      class="cs-endpoint draggable"
      on:mousedown={(e) => startDragEndpoint('end', e)}
    />
    <text x={endPx.x + 10} y={endPx.y - 12} class="cs-label">B</text>
  {/if}
</svg>

{#if phase === 'complete'}
  <div class="cs-line-actions">
    <button class="cs-action-btn primary" on:click={requestCrossSection} title="Request cross-section along this line">
      Request Cross-Section
    </button>
    <button class="cs-action-btn" on:click={() => setCrossSectionLayout('modal')} title="Open the cross-section in a large window">
      Expand
    </button>
    <button class="cs-action-btn" on:click={clearLine} title="Remove the line">
      Clear
    </button>
  </div>
{/if}

<style>
  .cs-line-overlay {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    pointer-events: none;
    z-index: 20;
  }

  .cs-line-overlay.active {
    pointer-events: all;
    cursor: crosshair;
  }

  .cs-line {
    stroke: var(--accent-primary, #5b6cf7);
    stroke-width: 2.5;
    stroke-dasharray: none;
    filter: drop-shadow(0 0 4px rgba(91, 108, 247, 0.5));
  }

  .cs-line.rubber {
    stroke-dasharray: 6 4;
    opacity: 0.7;
  }

  .cs-endpoint {
    fill: var(--accent-primary, #5b6cf7);
    stroke: white;
    stroke-width: 2;
    filter: drop-shadow(0 0 3px rgba(91, 108, 247, 0.6));
  }

  .cs-endpoint.draggable {
    cursor: grab;
    pointer-events: all;
  }

  .cs-endpoint.draggable:active {
    cursor: grabbing;
  }

  .cs-label {
    fill: white;
    font-size: 12px;
    font-weight: 700;
    font-family: var(--font-sans, sans-serif);
    text-shadow: 0 0 6px rgba(0, 0, 0, 0.8);
    pointer-events: none;
  }

  .cs-line-actions {
    position: absolute;
    bottom: 60px;
    left: 50%;
    transform: translateX(-50%);
    display: flex;
    gap: 8px;
    z-index: 25;
    pointer-events: auto;
  }

  .cs-action-btn {
    padding: 6px 14px;
    font-size: 12px;
    font-weight: 600;
    border-radius: var(--radius-sm, 4px);
    border: 1px solid var(--glass-border, rgba(255,255,255,0.1));
    background: rgba(15, 15, 26, 0.85);
    /* a fixed dark pill over the map: its text must not follow the app theme */
    color: rgba(255, 255, 255, 0.78);
    cursor: pointer;
    backdrop-filter: blur(8px);
    transition: all 150ms ease;
  }

  .cs-action-btn:hover {
    background: rgba(91, 108, 247, 0.15);
    border-color: rgba(91, 108, 247, 0.3);
    color: #fff;
  }

  .cs-action-btn.primary {
    background: rgba(91, 108, 247, 0.2);
    border-color: rgba(91, 108, 247, 0.4);
    color: #aab4ff;
  }

  .cs-action-btn.primary:hover {
    background: rgba(91, 108, 247, 0.35);
    color: white;
  }
</style>
