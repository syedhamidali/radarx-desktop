<script lang="ts">
  /**
   * PanZoomMap - a tile map that needs no open file: the same azimuthal-equidistant projection
   * and tile background as the 2D viewer (MapBackground), centred on any point, with wheel zoom
   * about the cursor, drag to pan and double-click to fit. Overlays (markers, the radar-sites
   * layer) are rendered through the `children` snippet, which receives the projection parameters
   * they need for `latLonToPixel`.
   */
  import { onMount, onDestroy, type Snippet } from 'svelte';
  import MapBackground from './MapBackground.svelte';
  import { latLonToDataSpace } from '../utils/geoProjection';
  import type { MosaicLayer, GeoBounds } from '../stores/mosaic';

  export interface MapView {
    radarLat: number;
    radarLon: number;
    maxRange: number;
    scale: number;
    translateX: number;
    translateY: number;
    containerWidth: number;
    containerHeight: number;
  }

  let {
    centerLat,
    centerLon,
    maxRange = 300_000,
    overlay = null,
    showTiles = true,
    opacity = 0.9,
    fit = null,
    label = 'Map. Scroll to zoom, drag to pan, double-click to fit.',
    children,
  }: {
    centerLat: number;
    centerLon: number;
    /** Metres from the centre to the edge of the view at scale 1 (the "range" of the projection). */
    maxRange?: number;
    overlay?: MosaicLayer | null;
    showTiles?: boolean;
    opacity?: number;
    /** When set (and whenever it changes), the view pans and zooms to this lon/lat box. */
    fit?: GeoBounds | null;
    label?: string;
    children?: Snippet<[MapView]>;
  } = $props();

  let el = $state<HTMLDivElement | null>(null);
  let width = $state(0);
  let height = $state(0);
  let scale = $state(1);
  let translateX = $state(0);
  let translateY = $state(0);
  let dragging = $state(false);
  let dragStart = { x: 0, y: 0, tx: 0, ty: 0 };
  let ro: ResizeObserver | null = null;

  const MIN_SCALE = 0.05;
  const MAX_SCALE = 60;

  onMount(() => {
    ro = new ResizeObserver((entries) => {
      for (const e of entries) {
        const w = Math.round(e.contentRect.width);
        const h = Math.round(e.contentRect.height);
        if (w > 0 && h > 0) { width = w; height = h; }
      }
    });
    if (el) ro.observe(el);
  });
  onDestroy(() => ro?.disconnect());

  const view = $derived<MapView>({ radarLat: centerLat, radarLon: centerLon, maxRange, scale, translateX, translateY, containerWidth: width, containerHeight: height });

  function factors(): { sx: number; sy: number } {
    const aspect = width / height;
    return { sx: (aspect >= 1 ? scale / aspect : scale) / maxRange, sy: (aspect >= 1 ? scale : scale * aspect) / maxRange };
  }

  function onWheel(e: WheelEvent) {
    if (!el || width === 0) return;
    e.preventDefault();
    const rect = el.getBoundingClientRect();
    const ndcX = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    const ndcY = 1 - ((e.clientY - rect.top) / rect.height) * 2;
    const { sx, sy } = factors();
    // data-space point under the cursor stays put while the scale changes
    const xm = ndcX / sx - translateX;
    const ym = ndcY / sy - translateY;
    scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale * Math.exp(-e.deltaY * 0.0012)));
    const f = factors();
    translateX = ndcX / f.sx - xm;
    translateY = ndcY / f.sy - ym;
  }

  function onMouseDown(e: MouseEvent) {
    if (e.button !== 0) return;
    dragging = true;
    dragStart = { x: e.clientX, y: e.clientY, tx: translateX, ty: translateY };
  }
  function onMouseMove(e: MouseEvent) {
    if (!dragging || !el) return;
    const rect = el.getBoundingClientRect();
    const { sx, sy } = factors();
    translateX = dragStart.tx + (((e.clientX - dragStart.x) / rect.width) * 2) / sx;
    translateY = dragStart.ty + ((-(e.clientY - dragStart.y) / rect.height) * 2) / sy;
  }
  function endDrag() { dragging = false; }

  /** Pan/zoom so the box fills the view (10 % margin). */
  export function fitBounds(b: GeoBounds) {
    if (width === 0 || height === 0) return;
    const [x1, y1] = latLonToDataSpace(b.south, b.west, centerLat, centerLon);
    const [x2, y2] = latLonToDataSpace(b.north, b.east, centerLat, centerLon);
    const cx = (x1 + x2) / 2;
    const cy = (y1 + y2) / 2;
    const hx = Math.max(Math.abs(x2 - x1) / 2, 1) * 1.1;
    const hy = Math.max(Math.abs(y2 - y1) / 2, 1) * 1.1;
    const aspect = width / height;
    const f = aspect >= 1 ? Math.min((aspect * maxRange) / hx, maxRange / hy) : Math.min(maxRange / hx, maxRange / (aspect * hy));
    scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, f));
    translateX = -cx;
    translateY = -cy;
  }

  function reset() {
    if (fit) fitBounds(fit);
    else { scale = 1; translateX = 0; translateY = 0; }
  }

  // Fit when asked (and once the container has a size).
  let lastFit: GeoBounds | null = null;
  $effect(() => {
    const b = fit;
    const ready = width > 0 && height > 0;
    if (b && ready && b !== lastFit) { lastFit = b; fitBounds(b); }
  });

  function onKey(e: KeyboardEvent) {
    const step = 40;
    const { sx, sy } = factors();
    if (e.key === '+' || e.key === '=') { scale = Math.min(MAX_SCALE, scale * 1.25); e.preventDefault(); }
    else if (e.key === '-') { scale = Math.max(MIN_SCALE, scale / 1.25); e.preventDefault(); }
    else if (e.key === 'ArrowLeft') { translateX += ((step / width) * 2) / sx; e.preventDefault(); }
    else if (e.key === 'ArrowRight') { translateX -= ((step / width) * 2) / sx; e.preventDefault(); }
    else if (e.key === 'ArrowUp') { translateY -= ((step / height) * 2) / sy; e.preventDefault(); }
    else if (e.key === 'ArrowDown') { translateY += ((step / height) * 2) / sy; e.preventDefault(); }
  }
</script>

<!-- A pannable map is a widget (role application): svelte only knows the static role. -->
<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
<div
  class="pz-map"
  class:dragging
  bind:this={el}
  role="application"
  aria-label={label}
  tabindex="0"
  onwheel={onWheel}
  onmousedown={onMouseDown}
  onmousemove={onMouseMove}
  onmouseup={endDrag}
  onmouseleave={endDrag}
  ondblclick={reset}
  onkeydown={onKey}
>
  {#if width > 0 && height > 0}
    <MapBackground
      radarLat={centerLat}
      radarLon={centerLon}
      {maxRange}
      {scale}
      {translateX}
      {translateY}
      containerWidth={width}
      containerHeight={height}
      {opacity}
      {showTiles}
      {overlay}
    />
    {@render children?.(view)}
  {/if}
</div>

<style>
  .pz-map {
    position: relative;
    width: 100%;
    height: 100%;
    overflow: hidden;
    background: var(--c-surface-0);
    cursor: grab;
    outline: none;
  }
  .pz-map.dragging { cursor: grabbing; }
  .pz-map:focus-visible { box-shadow: inset 0 0 0 2px var(--c-accent); }
</style>
