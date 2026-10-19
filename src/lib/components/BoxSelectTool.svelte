<script lang="ts">
  let {
    active = false,
    radarRange = 150000,   // meters
    canvasWidth = 800,
    canvasHeight = 800,
    scale = 1,
    translateX = 0,        // meters (same units as ppiRenderer)
    translateY = 0,        // meters
    onboxselected,
  }: {
    active?: boolean;
    radarRange?: number;
    canvasWidth?: number;
    canvasHeight?: number;
    scale?: number;
    translateX?: number;
    translateY?: number;
    onboxselected?: (box: { xMin: number; xMax: number; yMin: number; yMax: number }) => void;
  } = $props();

  let dragging = $state(false);
  let startX   = $state(0);
  let startY   = $state(0);
  let currX    = $state(0);
  let currY    = $state(0);

  const rectX = $derived(Math.min(startX, currX));
  const rectY = $derived(Math.min(startY, currY));
  const rectW = $derived(Math.abs(currX - startX));
  const rectH = $derived(Math.abs(currY - startY));

  // Inverse of ppiRenderer's NDC→world transform; matches screenToData() in RadarViewer.
  function pixToMeters(px: number, py: number): [number, number] {
    const ndcX   = (px / canvasWidth)  * 2 - 1;
    const ndcY   = 1 - (py / canvasHeight) * 2;
    const aspect = canvasWidth / canvasHeight;
    const sx = (aspect >= 1 ? scale / aspect : scale) / radarRange;
    const sy = (aspect >= 1 ? scale : scale * aspect)  / radarRange;
    return [
      (ndcX - translateX * sx) / sx,
      (ndcY - translateY * sy) / sy,
    ];
  }

  function onmousedown(e: MouseEvent) {
    if (!active) return;
    dragging = true;
    const svg = e.currentTarget as SVGSVGElement;
    const rect = svg.getBoundingClientRect();
    startX = e.clientX - rect.left;
    startY = e.clientY - rect.top;
    currX = startX;
    currY = startY;
  }

  function onmousemove(e: MouseEvent) {
    if (!dragging) return;
    const svg = (e.currentTarget ?? e.target) as SVGSVGElement;
    const rect = svg.getBoundingClientRect();
    currX = Math.max(0, Math.min(canvasWidth,  e.clientX - rect.left));
    currY = Math.max(0, Math.min(canvasHeight, e.clientY - rect.top));
  }

  function onkeydown(e: KeyboardEvent) {
    if (dragging && e.key === 'Escape') {
      e.stopPropagation();
      dragging = false; // cancel without emitting a region
    }
  }

  function onmouseup() {
    if (!dragging) return;
    dragging = false;
    if (rectW < 5 || rectH < 5) return;

    // top pixel Y = northernmost (largest ym), bottom pixel Y = southernmost
    const [xMin, yMax] = pixToMeters(Math.min(startX, currX), Math.min(startY, currY));
    const [xMax, yMin] = pixToMeters(Math.max(startX, currX), Math.max(startY, currY));
    onboxselected?.({ xMin, xMax, yMin, yMax });
  }
</script>

<svelte:window onkeydown={onkeydown} />

{#if active}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <svg
    class="box-select-overlay"
    width={canvasWidth}
    height={canvasHeight}
    {onmousedown}
    {onmousemove}
    {onmouseup}
    onmouseleave={onmouseup}
  >
    {#if dragging && rectW > 0 && rectH > 0}
      <rect x={rectX} y={rectY} width={rectW} height={rectH} class="selection-rect" />
      <circle cx={rectX}        cy={rectY}        r="3" class="corner-handle" />
      <circle cx={rectX + rectW} cy={rectY}        r="3" class="corner-handle" />
      <circle cx={rectX}        cy={rectY + rectH} r="3" class="corner-handle" />
      <circle cx={rectX + rectW} cy={rectY + rectH} r="3" class="corner-handle" />
    {/if}
  </svg>
{/if}

<style>
  .box-select-overlay {
    position: absolute;
    top: 0;
    left: 0;
    cursor: crosshair;
    z-index: 20;
    pointer-events: all;
  }

  .selection-rect {
    fill: rgba(var(--c-accent-rgb), 0.1);
    stroke: rgba(var(--c-accent-rgb), 0.8);
    stroke-width: 1.5;
    stroke-dasharray: 6 3;
  }

  .corner-handle {
    fill: rgba(var(--c-accent-rgb), 0.9);
    stroke: none;
  }
</style>
