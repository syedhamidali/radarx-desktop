<script lang="ts">
  import {
    annotationsStore,
    visibleMeasurements,
    type DataPoint,
    type Measurement,
    distanceKm,
    bearingDeg,
  } from '../stores/annotations';

  // Props passed from RadarViewer so we can convert data coords to screen coords
  export let scale: number;
  export let translateX: number;
  export let translateY: number;
  export let maxRange: number;
  export let containerWidth: number;
  export let containerHeight: number;

  $: measurements = $visibleMeasurements;
  $: pendingPoints = $annotationsStore.pendingPoints;
  $: measureMode = $annotationsStore.measureMode;

  /** Convert data coordinates (metres) to SVG pixel coordinates. */
  function dataToScreen(pt: DataPoint): { x: number; y: number } {
    const aspect = containerWidth / containerHeight;
    const base = scale / maxRange;
    const sx = aspect >= 1 ? base / aspect : base;
    const sy = aspect >= 1 ? base : base * aspect;
    const ndcX = pt.x * sx + translateX * sx;
    const ndcY = pt.y * sy + translateY * sy;
    const px = (ndcX + 1) / 2 * containerWidth;
    const py = (1 - ndcY) / 2 * containerHeight;
    return { x: px, y: py };
  }

  function measurementLine(m: { points: [DataPoint, DataPoint] }): string {
    const a = dataToScreen(m.points[0]);
    const b = dataToScreen(m.points[1]);
    return `M${a.x},${a.y} L${b.x},${b.y}`;
  }

  function midpoint(m: { points: [DataPoint, DataPoint] }): { x: number; y: number } {
    const a = dataToScreen(m.points[0]);
    const b = dataToScreen(m.points[1]);
    return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
  }

  function polygonPath(pts: DataPoint[]): string {
    if (pts.length < 2) return '';
    const screen = pts.map(dataToScreen);
    return screen.map((p, i) => (i === 0 ? `M${p.x},${p.y}` : `L${p.x},${p.y}`)).join(' ') + ' Z';
  }

  function polygonCentroid(pts: DataPoint[]): { x: number; y: number } {
    const screen = pts.map(dataToScreen);
    const cx = screen.reduce((s, p) => s + p.x, 0) / screen.length;
    const cy = screen.reduce((s, p) => s + p.y, 0) / screen.length;
    return { x: cx, y: cy };
  }

  function pendingLinePath(): string {
    if (pendingPoints.length < 1) return '';
    const pts = pendingPoints.map(dataToScreen);
    return pts.map((p, i) => (i === 0 ? `M${p.x},${p.y}` : `L${p.x},${p.y}`)).join(' ');
  }
</script>

<svg class="measure-overlay" viewBox="0 0 {containerWidth} {containerHeight}"
     xmlns="http://www.w3.org/2000/svg">

  <!-- Completed measurements -->
  {#each measurements as m (m.id)}
    {#if m.type === 'distance'}
      <path d={measurementLine(m)} class="measure-line" />
      <!-- endpoints -->
      <circle cx={dataToScreen(m.points[0]).x} cy={dataToScreen(m.points[0]).y} r="4" class="measure-dot" />
      <circle cx={dataToScreen(m.points[1]).x} cy={dataToScreen(m.points[1]).y} r="4" class="measure-dot" />
      <!-- label -->
      <text x={midpoint(m).x} y={midpoint(m).y - 8} class="measure-label">
        {m.distanceKm.toFixed(2)} km
      </text>

    {:else if m.type === 'bearing'}
      <path d={measurementLine(m)} class="measure-line bearing" />
      <circle cx={dataToScreen(m.points[0]).x} cy={dataToScreen(m.points[0]).y} r="4" class="measure-dot" />
      <circle cx={dataToScreen(m.points[1]).x} cy={dataToScreen(m.points[1]).y} r="4" class="measure-dot" />
      <text x={midpoint(m).x} y={midpoint(m).y - 8} class="measure-label">
        {m.bearingDeg.toFixed(1)}° / {m.distanceKm.toFixed(2)} km
      </text>

    {:else if m.type === 'area'}
      <path d={polygonPath(m.points)} class="measure-polygon" />
      {#each m.points as pt}
        <circle cx={dataToScreen(pt).x} cy={dataToScreen(pt).y} r="3" class="measure-dot" />
      {/each}
      <text x={polygonCentroid(m.points).x} y={polygonCentroid(m.points).y} class="measure-label">
        {m.areaKm2.toFixed(2)} km²
      </text>
    {/if}
  {/each}

  <!-- Pending / in-progress drawing -->
  {#if pendingPoints.length > 0 && (measureMode === 'distance' || measureMode === 'bearing' || measureMode === 'area')}
    <path d={pendingLinePath()} class="measure-line pending" />
    {#each pendingPoints as pt}
      <circle cx={dataToScreen(pt).x} cy={dataToScreen(pt).y} r="4" class="measure-dot pending" />
    {/each}
  {/if}
</svg>

<style>
  .measure-overlay {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    pointer-events: none;
    z-index: 20;
    overflow: visible;
  }

  .measure-overlay {
    --halo: rgba(var(--c-surface-0-rgb), 0.8);
  }
  :global(.theme-light) .measure-overlay {
    --halo: rgba(255, 255, 255, 0.88);
  }
  @media (prefers-color-scheme: light) {
    :global(.theme-auto) .measure-overlay {
      --halo: rgba(255, 255, 255, 0.88);
    }
  }

  .measure-line {
    fill: none;
    stroke: var(--accent-primary, var(--c-accent));
    stroke-width: 2;
    stroke-dasharray: 6 3;
  }

  .measure-line.bearing {
    stroke: var(--accent-warning, #fbbf24);
  }

  .measure-line.pending {
    stroke-opacity: 0.6;
    stroke-dasharray: 4 4;
  }

  .measure-polygon {
    fill: rgba(var(--c-accent-rgb), 0.12);
    stroke: var(--accent-primary, var(--c-accent));
    stroke-width: 2;
    stroke-dasharray: 6 3;
  }

  .measure-dot {
    fill: var(--accent-primary, var(--c-accent));
    stroke: var(--bg-primary, var(--c-surface-0));
    stroke-width: 2;
  }

  .measure-dot.pending {
    fill-opacity: 0.6;
  }

  .measure-label {
    fill: var(--text-primary, #e4e6f4);
    font-family: var(--font-mono, monospace);
    font-size: 11px;
    text-anchor: middle;
    paint-order: stroke;
    stroke: var(--halo);
    stroke-width: 3px;
    stroke-linejoin: round;
  }
</style>
