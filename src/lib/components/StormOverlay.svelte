<script lang="ts">
  /**
   * StormOverlay — renders storm cell boundaries, IDs, and max dBZ labels
   * as HTML overlays positioned over the PPI canvas.
   *
   * Uses absolute positioning relative to the viewer-canvas container.
   * Cells are projected from polar (azimuth, range) to screen coordinates
   * using the same transform the WebGL PPI uses.
   */
  import { stormCells, type CellData, type TrackData } from '../stores/stormCells';
  import { radarData, selectedSweep } from '../stores/radarData';

  // View parameters — must match RadarViewer's current state
  export let scale: number = 1;
  export let translateX: number = 0;
  export let translateY: number = 0;
  export let maxRange: number = 1;
  export let containerWidth: number = 800;
  export let containerHeight: number = 600;

  let cells: CellData[] = [];
  let tracks: TrackData[] = [];
  let visible: boolean = true;
  let selectedId: string | null = null;

  // Cells are polar positions of one sweep of one file; on any other sweep they would sit in the
  // wrong place (beam geometry differs per elevation), so they are drawn only where they were found.
  $: source = $stormCells.source;
  $: onSourceSweep = !source || (source.file === $radarData.filePath && source.sweep === $selectedSweep);
  $: cells = onSourceSweep ? $stormCells.cells : [];
  $: tracks = onSourceSweep ? $stormCells.tracks.filter((t) => (t.cell_history?.length ?? 0) > 1) : [];
  $: visible = $stormCells.overlayVisible;
  $: selectedId = $stormCells.selectedCellId;

  /**
   * Convert polar (azimuth_deg, range_m) to screen pixel coordinates.
   * Mirrors the WebGL vertex transform in ppiRenderer. The view is passed in rather than read from
   * the props inside: a template call that only reads state internally is not re-run when the
   * view changes, so the cells would not follow a pan or zoom.
   */
  function polarToScreen(
    azDeg: number,
    rangeM: number,
    sc: number, tx: number, ty: number, mr: number, w: number, h: number,
  ): { x: number; y: number } {
    const azRad = (azDeg * Math.PI) / 180;
    // Data-space: x = range * sin(az), y = range * cos(az)
    const dataX = rangeM * Math.sin(azRad);
    const dataY = rangeM * Math.cos(azRad);

    const aspect = w / h;
    const sx = (aspect >= 1 ? sc / aspect : sc) / mr;
    const sy = (aspect >= 1 ? sc : sc * aspect) / mr;

    const ndcX = (dataX + tx) * sx;
    const ndcY = (dataY + ty) * sy;

    // NDC [-1,1] to pixel
    const px = ((ndcX + 1) / 2) * w;
    const py = ((1 - ndcY) / 2) * h;
    return { x: px, y: py };
  }

  function selectCell(id: string) {
    stormCells.update((s) => ({
      ...s,
      selectedCellId: s.selectedCellId === id ? null : id,
    }));
  }

  /** Where a matched cell moved: from its earlier centroid to its latest one. */
  function trackLine(
    t: TrackData,
    sc: number, tx: number, ty: number, mr: number, w: number, h: number,
  ): { x1: number; y1: number; x2: number; y2: number } | null {
    const first = t.cell_history[0];
    const last = t.cell_history[t.cell_history.length - 1];
    if (!first || !last) return null;
    const a = polarToScreen(first.centroid_az, first.centroid_range, sc, tx, ty, mr, w, h);
    const b = polarToScreen(last.centroid_az, last.centroid_range, sc, tx, ty, mr, w, h);
    return { x1: a.x, y1: a.y, x2: b.x, y2: b.y };
  }

  function dbzColor(dbz: number): string {
    if (dbz >= 60) return '#ff0040';
    if (dbz >= 50) return '#ff4000';
    if (dbz >= 45) return '#ff8000';
    if (dbz >= 40) return '#ffcc00';
    return '#00ccff';
  }

  /**
   * Build an SVG polygon path from boundary points for a cell.
   */
  function boundaryPath(
    cell: CellData,
    sc: number, tx: number, ty: number, mr: number, w: number, h: number,
  ): string {
    if (cell.boundary_points.length < 3) return '';
    const pts = cell.boundary_points.map(([az, rng]) => polarToScreen(az, rng, sc, tx, ty, mr, w, h));
    return pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x},${p.y}`).join(' ') + ' Z';
  }
</script>

{#if visible && cells.length > 0}
  <div class="storm-overlay" aria-hidden="true">
    <!-- SVG layer for boundaries -->
    <svg class="boundary-svg" viewBox="0 0 {containerWidth} {containerHeight}">
      {#each cells as cell (cell.id)}
        {@const path = boundaryPath(cell, scale, translateX, translateY, maxRange, containerWidth, containerHeight)}
        {#if path}
          <path
            d={path}
            class="cell-boundary"
            class:selected={cell.id === selectedId}
            style="stroke: {dbzColor(cell.max_dbz)}"
          />
        {/if}
      {/each}

      <!-- Tracks: earlier centroid -> latest centroid -->
      <defs>
        <marker id="storm-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" fill="var(--c-text)" />
        </marker>
      </defs>
      {#each tracks as t (t.track_id)}
        {@const ln = trackLine(t, scale, translateX, translateY, maxRange, containerWidth, containerHeight)}
        {#if ln}
          <line class="track-line" x1={ln.x1} y1={ln.y1} x2={ln.x2} y2={ln.y2} marker-end="url(#storm-arrow)" />
        {/if}
      {/each}
    </svg>

    <!-- HTML labels for each cell -->
    {#each cells as cell (cell.id)}
      {@const pos = polarToScreen(cell.centroid_az, cell.centroid_range, scale, translateX, translateY, maxRange, containerWidth, containerHeight)}
      <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
      <div
        class="cell-label"
        class:selected={cell.id === selectedId}
        style="left: {pos.x}px; top: {pos.y}px; --cell-color: {dbzColor(cell.max_dbz)}"
        on:click|stopPropagation={() => selectCell(cell.id)}
        title="{cell.id}: {cell.max_dbz.toFixed(1)} dBZ, {cell.area_km2.toFixed(1)} km2"
      >
        <span class="cell-id">{cell.id}</span>
        <span class="cell-dbz">{cell.max_dbz.toFixed(0)}</span>
      </div>
    {/each}
  </div>
{/if}

<style>
  .storm-overlay {
    position: absolute;
    inset: 0;
    pointer-events: none;
    z-index: 20;
    overflow: hidden;
  }

  .boundary-svg {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
  }

  .track-line {
    stroke: var(--c-text);
    stroke-width: 2;
    stroke-opacity: 0.9;
    filter: drop-shadow(0 0 2px rgba(var(--c-shadow-rgb), 0.8));
  }

  .cell-boundary {
    fill: none;
    stroke-width: 1.5;
    stroke-opacity: 0.7;
    stroke-dasharray: 4 2;
    transition: stroke-opacity 150ms ease, stroke-width 150ms ease;
  }

  .cell-boundary.selected {
    stroke-width: 2.5;
    stroke-opacity: 1;
    stroke-dasharray: none;
  }

  .cell-label {
    position: absolute;
    transform: translate(-50%, -50%);
    display: flex;
    align-items: center;
    gap: 3px;
    padding: 2px 6px;
    border-radius: 3px;
    background: rgba(var(--c-surface-1-rgb), 0.82);
    border: 1px solid var(--cell-color, var(--c-accent));
    font-family: var(--font-mono);
    font-size: 10px;
    /* a fixed light-on-dark pill: it floats over the data whatever the app theme */
    color: var(--c-text);
    white-space: nowrap;
    pointer-events: auto;
    cursor: pointer;
    backdrop-filter: blur(3px);
    transition: transform 150ms ease, border-color 150ms ease;
    user-select: none;
  }

  .cell-label:hover {
    transform: translate(-50%, -50%) scale(1.1);
    z-index: 5;
  }

  .cell-label.selected {
    border-width: 2px;
    background: rgba(var(--c-surface-1-rgb), 0.92);
    z-index: 10;
  }

  .cell-id {
    font-weight: 700;
    color: var(--cell-color, var(--c-accent));
  }

  .cell-dbz {
    color: var(--c-text-dim);
    font-size: 9px;
  }
</style>
