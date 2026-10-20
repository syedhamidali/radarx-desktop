<script lang="ts">
  import {
    annotationsStore,
    visibleAnnotations,
    visibleMeasurements,
    updateAnnotationText,
    updateAnnotationPosition,
    deleteAnnotation,
    deleteMeasurement,
    exportAnnotationsJSON,
    clearAll,
    type DataPoint,
    type Annotation,
  } from '../stores/annotations';

  let {
    scale,
    translateX,
    translateY,
    maxRange,
    containerWidth,
    containerHeight,
  } = $props<{
    scale: number;
    translateX: number;
    translateY: number;
    maxRange: number;
    containerWidth: number;
    containerHeight: number;
  }>();

  const annotations = $derived($visibleAnnotations);
  const measurements = $derived($visibleMeasurements);
  const pendingPoints = $derived($annotationsStore.pendingPoints);
  const annotationMode = $derived($annotationsStore.annotationMode);

  let editingId: string | null = $state(null);
  let editText = $state('');
  let draggingId: string | null = $state(null);
  let showListPanel = $state(false);
  let svgEl: SVGSVGElement | undefined = $state();
  let grab = { dx: 0, dy: 0 };
  const toolActive = $derived($annotationsStore.measureMode !== 'none' || annotationMode !== 'none');

  function startDrag(e: PointerEvent, ann: { id: string; position: DataPoint }) {
    if (e.button !== 0 || toolActive || editingId === ann.id) return;
    const p = dataToScreen(ann.position);
    const r = svgEl!.getBoundingClientRect();
    grab = { dx: e.clientX - r.left - p.x, dy: e.clientY - r.top - p.y };
    draggingId = ann.id;
    (e.currentTarget as Element).setPointerCapture(e.pointerId);
  }

  function onDrag(e: PointerEvent) {
    if (!draggingId || !svgEl) return;
    const r = svgEl.getBoundingClientRect();
    updateAnnotationPosition(draggingId, screenToData(e.clientX - r.left - grab.dx, e.clientY - r.top - grab.dy));
  }

  function endDrag() {
    draggingId = null;
  }

  function onTextKey(e: KeyboardEvent, ann: { id: string; text: string }) {
    if (editingId) return;
    if (e.key === 'Enter' || e.key === 'F2') {
      e.preventDefault();
      startEdit(ann);
    } else if (e.key === 'Delete' || e.key === 'Backspace') {
      e.preventDefault();
      deleteAnnotation(ann.id);
    }
  }

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

  function screenToData(px: number, py: number): DataPoint {
    const ndcX = (px / containerWidth) * 2 - 1;
    const ndcY = 1 - (py / containerHeight) * 2;
    const aspect = containerWidth / containerHeight;
    const base = scale / maxRange;
    const sx = aspect >= 1 ? base / aspect : base;
    const sy = aspect >= 1 ? base : base * aspect;
    return {
      x: (ndcX - translateX * sx) / sx,
      y: (ndcY - translateY * sy) / sy,
    };
  }

  function startEdit(ann: { id: string; text: string }) {
    editingId = ann.id;
    editText = ann.text;
  }

  function focusSelect(node: HTMLInputElement) {
    // After the double-click that opened the editor has finished selecting a word.
    setTimeout(() => {
      node.focus();
      node.select();
    }, 0);
  }

  function commitEdit() {
    if (editingId) {
      // An emptied label would be an invisible, unselectable box: keep the old text instead.
      if (editText.trim()) updateAnnotationText(editingId, editText.trim());
      editingId = null;
    }
  }

  function onEditKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter') commitEdit();
    if (e.key === 'Escape') { editingId = null; }
  }

  function handleExport() {
    const json = exportAnnotationsJSON();
    const blob = new Blob([json], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'radar-annotations.json';
    a.click();
    URL.revokeObjectURL(url);
  }

  function labelForAnnotation(a: Annotation): string {
    switch (a.type) {
      case 'text': return `Text: "${a.text}"`;
      case 'arrow': return 'Arrow';
      case 'circle': return `Circle (r=${(a.radiusM / 1000).toFixed(1)} km)`;
      case 'rectangle': return 'Rectangle';
    }
  }

  function circleScreenRadius(center: DataPoint, radiusM: number): number {
    const c = dataToScreen(center);
    const edge = dataToScreen({ x: center.x + radiusM, y: center.y });
    return Math.abs(edge.x - c.x);
  }

  // Pending drawing helpers for annotation modes
  function pendingAnnotationPath(): string {
    if (pendingPoints.length < 1) return '';
    const pts = pendingPoints.map(dataToScreen);
    return pts.map((p, i) => (i === 0 ? `M${p.x},${p.y}` : `L${p.x},${p.y}`)).join(' ');
  }
</script>

<!-- SVG overlay for annotations -->
<svg class="annotation-overlay" bind:this={svgEl} viewBox="0 0 {containerWidth} {containerHeight}"
     xmlns="http://www.w3.org/2000/svg">

  {#each annotations as ann (ann.id)}
    {#if ann.type === 'text'}
      {@const pos = dataToScreen(ann.position)}
      <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
      <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
      <g class="annotation-text-group" role="button" tabindex="0"
         aria-label="Text annotation: {ann.text}. Enter to edit, Delete to remove."
         style="pointer-events: {toolActive ? 'none' : 'auto'}; cursor: {draggingId === ann.id ? 'grabbing' : 'move'};"
         on:pointerdown|stopPropagation={(e) => startDrag(e, ann)}
         on:pointermove={onDrag} on:pointerup={endDrag} on:pointercancel={endDrag}
         on:mousedown|stopPropagation on:click|stopPropagation
         on:keydown={(e) => onTextKey(e, ann)}
         on:dblclick|stopPropagation={() => startEdit(ann)}>
        <rect x={pos.x - 4} y={pos.y - 14} width={Math.max(ann.text.length * 7 + 8, 40)} height="20"
              rx="3" class="annotation-text-bg" />
        {#if editingId === ann.id}
          <foreignObject x={pos.x - 4} y={pos.y - 14} width="200" height="24">
            <input class="annotation-edit-input"
                   use:focusSelect
                   aria-label="Annotation text"
                   bind:value={editText}
                   on:blur={commitEdit}
                   on:keydown|stopPropagation={onEditKeydown}
                   style="width:100%;height:22px;font-size:11px;background:var(--bg-secondary);color:var(--text-primary);border:1px solid var(--accent-primary);border-radius:3px;padding:0 4px;outline:none;font-family:var(--font-mono);"
            />
          </foreignObject>
        {:else}
          <text x={pos.x} y={pos.y} class="annotation-text-label">{ann.text}</text>
        {/if}
      </g>

    {:else if ann.type === 'arrow'}
      {@const s = dataToScreen(ann.start)}
      {@const e = dataToScreen(ann.end)}
      <defs>
        <marker id="arrow-{ann.id}" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
          <path d="M0,0 L8,3 L0,6 Z" fill="var(--accent-success)" />
        </marker>
      </defs>
      <line x1={s.x} y1={s.y} x2={e.x} y2={e.y}
            class="annotation-arrow" marker-end="url(#arrow-{ann.id})" />

    {:else if ann.type === 'circle'}
      {@const c = dataToScreen(ann.center)}
      {@const r = circleScreenRadius(ann.center, ann.radiusM)}
      <circle cx={c.x} cy={c.y} {r} class="annotation-region" />
      <text x={c.x} y={c.y - r - 6} class="annotation-region-label">
        {(ann.radiusM / 1000).toFixed(1)} km
      </text>

    {:else if ann.type === 'rectangle'}
      {@const c1 = dataToScreen(ann.corner1)}
      {@const c2 = dataToScreen(ann.corner2)}
      <rect x={Math.min(c1.x, c2.x)} y={Math.min(c1.y, c2.y)}
            width={Math.abs(c2.x - c1.x)} height={Math.abs(c2.y - c1.y)}
            class="annotation-region" />
    {/if}
  {/each}

  <!-- Pending annotation drawing -->
  {#if pendingPoints.length > 0 && (annotationMode === 'arrow' || annotationMode === 'circle' || annotationMode === 'rectangle')}
    {#each pendingPoints as pt}
      {@const s = dataToScreen(pt)}
      <circle cx={s.x} cy={s.y} r="4" class="annotation-pending-dot" />
    {/each}
    <path d={pendingAnnotationPath()} class="annotation-pending-line" />
  {/if}
</svg>

<!-- Annotations list panel -->
{#if showListPanel}
  <!-- svelte-ignore a11y_no_static_element_interactions, a11y_click_events_have_key_events -->
  <div class="annotations-panel" on:click|stopPropagation on:dblclick|stopPropagation
       on:mousedown|stopPropagation on:contextmenu|stopPropagation>
    <div class="annotations-panel-header">
      <span>Annotations & Measurements</span>
      <div class="annotations-panel-actions">
        <button class="panel-btn" on:click={handleExport} title="Export JSON">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
            <polyline points="7 10 12 15 17 10"/>
            <line x1="12" y1="15" x2="12" y2="3"/>
          </svg>
        </button>
        <button class="panel-btn" on:click={clearAll} title="Clear all">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="3 6 5 6 21 6"/>
            <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
            <path d="M10 11v6"/><path d="M14 11v6"/>
          </svg>
        </button>
        <button class="panel-btn" on:click={() => showListPanel = false} title="Close">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
          </svg>
        </button>
      </div>
    </div>
    <div class="annotations-panel-body">
      {#if measurements.length === 0 && annotations.length === 0}
        <p class="empty-msg">No measurements or annotations yet.</p>
      {/if}
      {#each measurements as m (m.id)}
        <div class="annotation-item">
          <span class="item-type">{m.type}</span>
          <span class="item-detail">
            {#if m.type === 'distance'}{m.distanceKm.toFixed(2)} km
            {:else if m.type === 'bearing'}{m.bearingDeg.toFixed(1)}° / {m.distanceKm.toFixed(2)} km
            {:else if m.type === 'area'}{m.areaKm2.toFixed(2)} km²
            {/if}
          </span>
          <button class="item-delete" on:click={() => deleteMeasurement(m.id)} title="Delete">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>
      {/each}
      {#each annotations as a (a.id)}
        <div class="annotation-item">
          <span class="item-type">{a.type}</span>
          <span class="item-detail">{labelForAnnotation(a)}</span>
          <button class="item-delete" on:click={() => deleteAnnotation(a.id)} title="Delete">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>
      {/each}
    </div>
  </div>
{/if}

<!-- Toggle for annotations panel -->
<button class="annotations-toggle" on:click|stopPropagation={() => showListPanel = !showListPanel}
        on:dblclick|stopPropagation on:mousedown|stopPropagation
        title="Show annotations list"
        class:active={showListPanel}>
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
    <polyline points="14 2 14 8 20 8"/>
    <line x1="16" y1="13" x2="8" y2="13"/>
    <line x1="16" y1="17" x2="8" y2="17"/>
  </svg>
  {#if measurements.length + annotations.length > 0}
    <span class="badge">{measurements.length + annotations.length}</span>
  {/if}
</button>

<style>
  .annotation-overlay {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    pointer-events: none;
    z-index: 21;
    overflow: visible;
  }

  .annotation-overlay {
    --halo: rgba(var(--c-surface-0-rgb), 0.8);
  }

  .annotation-text-bg {
    fill: var(--glass-bg-heavy);
    stroke: var(--accent-primary);
    stroke-width: 1;
  }

  .annotation-text-label {
    fill: var(--text-primary);
    font-family: var(--font-mono, monospace);
    font-size: 11px;
    dominant-baseline: middle;
  }

  .annotation-arrow {
    stroke: var(--accent-success);
    stroke-width: 2;
  }

  .annotation-region {
    fill: rgba(var(--c-accent-rgb), 0.08);
    stroke: var(--accent-warning);
    stroke-width: 1.5;
    stroke-dasharray: 5 3;
  }

  .annotation-region-label {
    fill: var(--accent-warning);
    font-family: var(--font-mono, monospace);
    font-size: 10px;
    text-anchor: middle;
    paint-order: stroke;
    stroke: var(--halo);
    stroke-width: 3px;
    stroke-linejoin: round;
  }

  .annotation-pending-dot {
    fill: var(--accent-success);
    fill-opacity: 0.7;
    stroke: var(--bg-primary);
    stroke-width: 2;
  }

  .annotation-pending-line {
    fill: none;
    stroke: var(--accent-success);
    stroke-width: 1.5;
    stroke-dasharray: 4 3;
    stroke-opacity: 0.6;
  }

  /* Annotations toggle button */
  .annotations-toggle {
    position: absolute;
    top: var(--spacing-md);
    right: var(--spacing-md);
    z-index: 25;
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 6px 8px;
    background: var(--glass-bg-heavy);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
    color: var(--text-secondary);
    cursor: pointer;
    backdrop-filter: blur(8px);
    transition: all 150ms ease;
    height: auto;
  }

  .annotations-toggle:hover,
  .annotations-toggle.active {
    border-color: var(--accent-primary);
    color: var(--text-primary);
  }

  .badge {
    background: var(--accent-primary);
    color: var(--c-on-accent);
    font-size: 9px;
    font-weight: 700;
    padding: 1px 5px;
    border-radius: 8px;
    min-width: 16px;
    text-align: center;
    line-height: 1.4;
  }

  /* Annotations list panel */
  .annotations-panel {
    position: absolute;
    top: 44px;
    right: var(--spacing-md);
    z-index: 25;
    width: 280px;
    max-height: 360px;
    background: var(--glass-bg-heavy);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    backdrop-filter: blur(20px);
    overflow: hidden;
    display: flex;
    flex-direction: column;
    box-shadow: var(--shadow-lg);
    animation: fade-in 150ms ease;
  }

  .annotations-panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 12px;
    border-bottom: 1px solid var(--glass-border);
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-secondary);
  }

  .annotations-panel-actions {
    display: flex;
    gap: 4px;
  }

  .panel-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    padding: 0;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 4px;
    color: var(--text-muted);
    cursor: pointer;
  }

  .panel-btn:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
    border-color: var(--glass-border);
  }

  .annotations-panel-body {
    overflow-y: auto;
    padding: 6px;
    flex: 1;
  }

  .empty-msg {
    padding: 16px 8px;
    text-align: center;
    color: var(--text-muted);
    font-size: 11px;
  }

  .annotation-item {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 5px 8px;
    border-radius: 4px;
    font-size: 11px;
    color: var(--text-secondary);
    transition: background 100ms ease;
  }

  .annotation-item:hover {
    background: var(--bg-hover);
  }

  .item-type {
    font-family: var(--font-mono);
    font-size: 9px;
    font-weight: 600;
    text-transform: uppercase;
    color: var(--accent-primary);
    background: rgba(var(--c-accent-rgb), 0.1);
    padding: 1px 5px;
    border-radius: 3px;
    flex-shrink: 0;
  }

  .item-detail {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-family: var(--font-mono);
    font-size: 11px;
  }

  .item-delete {
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 20px;
    padding: 0;
    background: transparent;
    border: none;
    border-radius: 3px;
    color: var(--text-muted);
    cursor: pointer;
    opacity: 0;
    transition: opacity 100ms;
  }

  .annotation-item:hover .item-delete,
  .item-delete:focus-visible {
    opacity: 1;
  }

  .item-delete:hover {
    color: var(--accent-danger);
    background: rgba(var(--c-alarm-rgb), 0.1);
  }
</style>
