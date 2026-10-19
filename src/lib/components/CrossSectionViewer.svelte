<script lang="ts">
  /**
   * CrossSectionViewer -- Canvas-based 2D viewer showing height (Y) vs distance (X)
   * for an RHI-like cross-section through radar data.
   */
  import { onMount, onDestroy } from 'svelte';
  import { COLORMAP_DATA } from '../utils/colormaps';
  import { getDefaultCmap, getDefaultRange, getColormapRGB } from '../utils/ppiRenderer';
  import type { CrossSectionData } from '../stores/crossSection';
  import { smoothingStore } from '../stores/crossSectionSmoothing';
  import { buildCells } from '../utils/crossSectionGeometry';
  import { buildSectionGrid, isPlain, type Grid } from '../utils/crossSectionRaster';
  import { maskOf, maskPredicate, valueMasks } from '../stores/valueMask';
  import { drawRaster } from '../utils/crossSectionDraw';

  let { data = null, cmapName = 'turbo', vmin = 0, vmax = 60 }: {
    data?: CrossSectionData | null;
    cmapName?: string;
    vmin?: number;
    vmax?: number;
  } = $props();

  let canvasEl = $state<HTMLCanvasElement>(undefined as any);
  let containerEl = $state<HTMLDivElement>(undefined as any);
  let resizeObserver: ResizeObserver | null = null;
  let width = $state(600);
  let height = $state(400);

  // Zoom / pan
  let scale = $state(1);
  let panX = $state(0);
  let panY = $state(0);
  let isDragging = $state(false);
  let dragStartX = 0;
  let dragStartY = 0;
  let dragStartPanX = 0;
  let dragStartPanY = 0;

  // Hover tooltip
  let hoverActive = $state(false);
  let hoverScreenX = $state(0);
  let hoverScreenY = $state(0);
  let hoverDist = $state(0);
  let hoverHeight = $state(0);
  let hoverValue = $state<number | null>(null);

  // Margins for axis labels
  const margin = { top: 20, right: 52, bottom: 45, left: 60 };

  // Derived data bounds
  let distMin = $state(0);
  let distMax = $state(1);
  let hMin = $state(0);
  let hMax = $state(1);

  $effect(() => {
    if (data) {
      // Compute into locals: reading the bounds back out of the state this effect writes would make
      // it depend on itself (and grow them without end).
      const lastDist = data.distance_km[data.distance_km.length - 1];
      let lo = Infinity;
      let hi = -Infinity;
      for (const row of data.height_km) {
        for (const h of row) {
          if (typeof h === 'number' && Number.isFinite(h)) {
            if (h < lo) lo = h;
            if (h > hi) hi = h;
          }
        }
      }
      if (!Number.isFinite(lo) || !Number.isFinite(hi)) { lo = 0; hi = 1; }
      if (lo === hi) hi = lo + 1;
      // Add 10% padding to height
      const hPad = (hi - lo) * 0.1;
      distMin = 0;
      distMax = lastDist || 1;
      hMin = Math.max(0, lo - hPad);
      // Cap at 20 km: beyond the horizon the upper sweeps' heights run to 100 km and would squash
      // the troposphere, where the data are, into a sliver.
      hMax = lo < 20 ? Math.min(hi + hPad, 20) : hi + hPad;
      // Reset view when new data arrives
      scale = 1;
      panX = 0;
      panY = 0;
    }
  });

  $effect(() => {
    if (data && canvasEl) {
      requestAnimationFrame(() => draw());
    }
  });

  // Redraw when any display setting changes. Each one is read explicitly: a short-circuiting
  // `a || b || c` only subscribes to the first truthy operand, so editing the range did nothing.
  $effect(() => {
    const deps = [cmapName, vmin, vmax, scale, panX, panY, width, height, $smoothingStore, $valueMasks];
    if (canvasEl && deps.length) {
      requestAnimationFrame(() => draw());
    }
  });

  onMount(() => {
    resizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width: w, height: h } = entry.contentRect;
        if (w > 0 && h > 0) {
          width = Math.round(w);
          height = Math.round(h);
          if (canvasEl) {
            const dpr = Math.min(window.devicePixelRatio || 1, 2);
            canvasEl.width = Math.round(width * dpr);
            canvasEl.height = Math.round(height * dpr);
            draw();
          }
        }
      }
    });
    if (containerEl) resizeObserver.observe(containerEl);
  });

  onDestroy(() => {
    resizeObserver?.disconnect();
  });

  function draw() {
    if (!canvasEl || !data) return;
    const ctx = canvasEl.getContext('2d');
    if (!ctx) return;

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    // Clear
    ctx.fillStyle = 'rgb(15, 15, 26)';
    ctx.fillRect(0, 0, width, height);

    const plotW = width - margin.left - margin.right;
    const plotH = height - margin.top - margin.bottom;
    if (plotW <= 0 || plotH <= 0) return;

    // Transform: data coords -> pixel coords (with zoom/pan)
    const viewDistMin = distMin - panX / scale;
    const viewDistMax = distMin + (distMax - distMin) / scale - panX / scale;
    const viewHMin = hMin - panY / scale;
    const viewHMax = hMin + (hMax - hMin) / scale - panY / scale;

    function distToX(d: number): number {
      return margin.left + ((d - viewDistMin) / (viewDistMax - viewDistMin)) * plotW;
    }
    function hToY(h: number): number {
      return margin.top + plotH - ((h - viewHMin) / (viewHMax - viewHMin)) * plotH;
    }

    // Build the colormap lookup
    const cmapRgb = COLORMAP_DATA[cmapName] ?? COLORMAP_DATA['turbo'];

    // Render each sweep's data as colored rectangles
    const nPoints = data.distance_km.length;
    const nSweeps = data.n_sweeps;

    const masked = maskPredicate(maskOf($valueMasks, data.variable));

    // Smoothing on: the shared smoothed picture instead of the blocky cells.
    const smooth = $smoothingStore;
    const smoothGrid = isPlain(smooth) ? null : gridFor(smooth);
    if (smoothGrid) {
      ctx.save();
      ctx.beginPath();
      ctx.rect(margin.left, margin.top, plotW, plotH);
      ctx.clip();
      drawRaster(
        ctx,
        { x: margin.left, y: margin.top, w: plotW, h: plotH },
        {
          grid: smoothGrid,
          view: { d0: viewDistMin, d1: viewDistMax, h0: viewHMin, h1: viewHMax },
          cmapName,
          vmin,
          vmax,
          interp: smooth.interp,
        },
        dpr,
      );
      ctx.restore();
    }

    for (let si = 0; si < (smoothGrid ? 0 : nSweeps); si++) {
      const sweepH = data.height_km[si];
      const sweepV = data.values[si];
      if (!sweepH || !sweepV) continue;

      for (let pi = 0; pi < nPoints - 1; pi++) {
        const val = sweepV[pi];
        if (val === null || val === undefined || !Number.isFinite(val)) continue;
        if (val < vmin) continue; // like the PPI: gates below the colour range are not drawn
        if (masked && masked(val)) continue; // hidden by the value mask

        const d0 = data.distance_km[pi];
        const d1 = data.distance_km[pi + 1];
        const h0 = sweepH[pi];
        if (h0 == null) continue; // beyond the radar horizon

        // Determine cell height: use neighboring sweep or a default
        let cellHalfH: number;
        if (si > 0 && data.height_km[si - 1]) {
          cellHalfH = Math.abs(h0 - data.height_km[si - 1][pi]) * 0.5;
        } else if (si < nSweeps - 1 && data.height_km[si + 1]) {
          cellHalfH = Math.abs(data.height_km[si + 1][pi] - h0) * 0.5;
        } else {
          cellHalfH = (hMax - hMin) / (nSweeps * 2);
        }

        const x0 = distToX(d0);
        const x1 = distToX(d1);
        const y0 = hToY(h0 + cellHalfH);
        const y1 = hToY(h0 - cellHalfH);

        // Skip if outside viewport
        if (x1 < margin.left || x0 > margin.left + plotW) continue;
        if (y1 < margin.top || y0 > margin.top + plotH) continue;

        // Colormap lookup
        const t = Math.max(0, Math.min(1, (val - vmin) / (vmax - vmin || 1)));
        const ci = Math.round(t * 255);
        const r = cmapRgb[ci * 3];
        const g = cmapRgb[ci * 3 + 1];
        const b = cmapRgb[ci * 3 + 2];

        ctx.fillStyle = `rgb(${r},${g},${b})`;
        ctx.fillRect(
          Math.max(x0, margin.left),
          Math.max(y0, margin.top),
          Math.min(x1, margin.left + plotW) - Math.max(x0, margin.left),
          Math.min(y1, margin.top + plotH) - Math.max(y0, margin.top)
        );
      }
    }

    // Draw axes
    ctx.strokeStyle = 'rgba(255,255,255,0.25)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(margin.left, margin.top);
    ctx.lineTo(margin.left, margin.top + plotH);
    ctx.lineTo(margin.left + plotW, margin.top + plotH);
    ctx.stroke();

    // Tick marks and labels
    ctx.fillStyle = 'rgba(255,255,255,0.6)';
    ctx.font = '11px var(--font-mono, monospace)';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';

    // X-axis ticks (distance)
    const xRange = viewDistMax - viewDistMin;
    const xTickSpacing = niceTickSpacing(xRange, plotW / 60);
    const xStart = Math.ceil(viewDistMin / xTickSpacing) * xTickSpacing;
    for (let d = xStart; d <= viewDistMax; d += xTickSpacing) {
      const x = distToX(d);
      if (x >= margin.left && x <= margin.left + plotW) {
        ctx.beginPath();
        ctx.moveTo(x, margin.top + plotH);
        ctx.lineTo(x, margin.top + plotH + 5);
        ctx.strokeStyle = 'rgba(255,255,255,0.3)';
        ctx.stroke();
        ctx.fillText(d.toFixed(xTickSpacing < 1 ? 1 : 0), x, margin.top + plotH + 8);

        // Grid line
        ctx.strokeStyle = 'rgba(255,255,255,0.05)';
        ctx.beginPath();
        ctx.moveTo(x, margin.top);
        ctx.lineTo(x, margin.top + plotH);
        ctx.stroke();
      }
    }

    // X-axis label
    ctx.fillStyle = 'rgba(255,255,255,0.5)';
    ctx.font = '12px var(--font-sans, sans-serif)';
    ctx.fillText('Distance (km)', margin.left + plotW / 2, height - 8);

    // Y-axis ticks (height)
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    ctx.font = '11px var(--font-mono, monospace)';
    ctx.fillStyle = 'rgba(255,255,255,0.6)';

    const yRange = viewHMax - viewHMin;
    const yTickSpacing = niceTickSpacing(yRange, plotH / 40);
    const yStart = Math.ceil(viewHMin / yTickSpacing) * yTickSpacing;
    for (let h = yStart; h <= viewHMax; h += yTickSpacing) {
      const y = hToY(h);
      if (y >= margin.top && y <= margin.top + plotH) {
        ctx.beginPath();
        ctx.moveTo(margin.left - 5, y);
        ctx.lineTo(margin.left, y);
        ctx.strokeStyle = 'rgba(255,255,255,0.3)';
        ctx.stroke();
        ctx.fillText(h.toFixed(yTickSpacing < 1 ? 1 : 0), margin.left - 8, y);

        // Grid line
        ctx.strokeStyle = 'rgba(255,255,255,0.05)';
        ctx.beginPath();
        ctx.moveTo(margin.left, y);
        ctx.lineTo(margin.left + plotW, y);
        ctx.stroke();
      }
    }

    // Y-axis label
    ctx.save();
    ctx.translate(14, margin.top + plotH / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = 'rgba(255,255,255,0.5)';
    ctx.font = '12px var(--font-sans, sans-serif)';
    ctx.fillText('Height (km)', 0, 0);
    ctx.restore();

    // Draw colorbar
    drawColorbar(ctx, plotW, plotH);
  }

  let gridCache: { data: CrossSectionData; key: string; grid: Grid } | null = null;
  /** The smoothed grid of this section (rebuilt only when the data or a setting changes). */
  function gridFor(smooth: { interp: string; sigma: number; upsample: number }): Grid | null {
    if (!data || data.distance_km.length < 2) return null;
    const last = data.distance_km[data.distance_km.length - 1];
    const maskNow = maskOf($valueMasks, data.variable);
    const key = `${smooth.interp}|${smooth.sigma}|${smooth.upsample}|${last}|${hMax}|${JSON.stringify(maskNow)}`;
    if (gridCache && gridCache.data === data && gridCache.key === key) return gridCache.grid;
    const cells = buildCells(data, { fill: true, masked: maskPredicate(maskNow) });
    const grid = buildSectionGrid(
      cells.columns,
      data.distance_km,
      { d0: data.distance_km[0], d1: last, h0: 0, h1: hMax },
      smooth as any,
    );
    gridCache = { data, key, grid };
    return grid;
  }

  function drawColorbar(ctx: CanvasRenderingContext2D, plotW: number, plotH: number) {
    const barW = 14;
    const barH = Math.min(plotH - 20, 200);
    const barX = margin.left + plotW + 4;
    const barY = margin.top + (plotH - barH) / 2;

    const cmapRgb = COLORMAP_DATA[cmapName] ?? COLORMAP_DATA['turbo'];
    for (let i = 0; i < barH; i++) {
      const t = 1 - i / barH;
      const ci = Math.round(t * 255);
      const r = cmapRgb[ci * 3];
      const g = cmapRgb[ci * 3 + 1];
      const b = cmapRgb[ci * 3 + 2];
      ctx.fillStyle = `rgb(${r},${g},${b})`;
      ctx.fillRect(barX, barY + i, barW, 1);
    }

    ctx.strokeStyle = 'rgba(255,255,255,0.2)';
    ctx.strokeRect(barX, barY, barW, barH);

    ctx.fillStyle = 'rgba(255,255,255,0.6)';
    ctx.font = '10px var(--font-mono, monospace)';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.fillText(vmax.toFixed(1), barX + barW + 4, barY);
    ctx.fillText(vmin.toFixed(1), barX + barW + 4, barY + barH);

    if (data?.units) {
      ctx.fillStyle = 'rgba(255,255,255,0.4)';
      ctx.font = '9px var(--font-sans, sans-serif)';
      ctx.fillText(data.units, barX + barW + 4, barY + barH + 12);
    }
  }

  function niceTickSpacing(range: number, maxTicks: number): number {
    const rough = range / Math.max(maxTicks, 1);
    const mag = Math.pow(10, Math.floor(Math.log10(rough)));
    const norm = rough / mag;
    let nice: number;
    if (norm <= 1.5) nice = 1;
    else if (norm <= 3.5) nice = 2;
    else if (norm <= 7.5) nice = 5;
    else nice = 10;
    return nice * mag;
  }

  // Screen pixel -> data coords
  function screenToData(clientX: number, clientY: number): [number, number] {
    const rect = containerEl.getBoundingClientRect();
    const px = clientX - rect.left;
    const py = clientY - rect.top;

    const plotW = width - margin.left - margin.right;
    const plotH = height - margin.top - margin.bottom;

    const viewDistMin = distMin - panX / scale;
    const viewDistMax = distMin + (distMax - distMin) / scale - panX / scale;
    const viewHMin = hMin - panY / scale;
    const viewHMax = hMin + (hMax - hMin) / scale - panY / scale;

    const d = viewDistMin + ((px - margin.left) / plotW) * (viewDistMax - viewDistMin);
    const h = viewHMax - ((py - margin.top) / plotH) * (viewHMax - viewHMin);
    return [d, h];
  }

  function findNearestValue(dist: number, h: number): number | null {
    if (!data) return null;
    // Find nearest distance index
    const dists = data.distance_km;
    let bestDi = 0;
    let bestDDist = Infinity;
    for (let i = 0; i < dists.length; i++) {
      const dd = Math.abs(dists[i] - dist);
      if (dd < bestDDist) { bestDDist = dd; bestDi = i; }
    }
    // Find nearest sweep by height
    let bestVal: number | null = null;
    let bestHDist = Infinity;
    for (let si = 0; si < data.n_sweeps; si++) {
      const sh = data.height_km[si]?.[bestDi];
      const sv = data.values[si]?.[bestDi];
      if (sh == null || sv == null || !Number.isFinite(sv)) continue;
      const hd = Math.abs(sh - h);
      if (hd < bestHDist) { bestHDist = hd; bestVal = sv; }
    }
    return bestVal;
  }

  function onWheel(e: WheelEvent) {
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
    const newScale = Math.min(Math.max(scale * factor, 0.5), 20);
    if (newScale === scale) return;

    // Zoom toward the cursor: the data point under it stays under it.
    const rect = containerEl.getBoundingClientRect();
    const plotW = width - margin.left - margin.right;
    const plotH = height - margin.top - margin.bottom;
    const fx = Math.min(1, Math.max(0, (e.clientX - rect.left - margin.left) / plotW));
    const fy = Math.min(1, Math.max(0, 1 - (e.clientY - rect.top - margin.top) / plotH));
    const [d, h] = screenToData(e.clientX, e.clientY);
    const newViewDistMin = d - fx * ((distMax - distMin) / newScale);
    const newViewHMin = h - fy * ((hMax - hMin) / newScale);
    panX = (distMin - newViewDistMin) * newScale;
    panY = (hMin - newViewHMin) * newScale;
    scale = newScale;
  }

  function onMouseDown(e: MouseEvent) {
    if (e.button !== 0) return;
    isDragging = true;
    dragStartX = e.clientX;
    dragStartY = e.clientY;
    dragStartPanX = panX;
    dragStartPanY = panY;
  }

  function onMouseMove(e: MouseEvent) {
    if (!data) return;

    if (isDragging) {
      const dx = e.clientX - dragStartX;
      const dy = e.clientY - dragStartY;
      const plotW = width - margin.left - margin.right;
      const plotH = height - margin.top - margin.bottom;
      panX = dragStartPanX + (dx / plotW) * (distMax - distMin);
      panY = dragStartPanY - (dy / plotH) * (hMax - hMin);
      draw();
      return;
    }

    // Hover info
    const [d, h] = screenToData(e.clientX, e.clientY);
    const plotW = width - margin.left - margin.right;
    const plotH = height - margin.top - margin.bottom;
    const rect = containerEl.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;

    if (px >= margin.left && px <= margin.left + plotW &&
        py >= margin.top && py <= margin.top + plotH) {
      hoverActive = true;
      hoverScreenX = e.clientX;
      hoverScreenY = e.clientY;
      hoverDist = d;
      hoverHeight = h;
      hoverValue = findNearestValue(d, h);
    } else {
      hoverActive = false;
    }
  }

  function onMouseUp() { isDragging = false; }
  function onMouseLeave() { isDragging = false; hoverActive = false; }

  function onDblClick(e: MouseEvent) {
    e.preventDefault();
    scale = 1;
    panX = 0;
    panY = 0;
    draw();
  }
</script>

<div class="cs-viewer" bind:this={containerEl}
     on:wheel={onWheel}
     on:mousedown={onMouseDown}
     on:mousemove={onMouseMove}
     on:mouseup={onMouseUp}
     on:mouseleave={onMouseLeave}
     on:dblclick={onDblClick}
     role="img"
     aria-label="Cross-section viewer: height vs distance">
  <canvas bind:this={canvasEl} class="cs-canvas"></canvas>

  {#if !data}
    <div class="cs-placeholder">
      <p>Draw a line on the PPI to request a cross-section</p>
    </div>
  {/if}

  {#if hoverActive}
    {@const relX = hoverScreenX - (containerEl?.getBoundingClientRect().left ?? 0)}
    <div
      class="cs-tooltip"
      style="left: {relX}px; top: {hoverScreenY - (containerEl?.getBoundingClientRect().top ?? 0) - 30}px; transform: translateX({relX > width * 0.5 ? 'calc(-100% - 14px)' : '14px'})"
    >
      <span class="tt-label">D:</span> {hoverDist.toFixed(1)} km
      <span class="tt-sep">|</span>
      <span class="tt-label">H:</span> {hoverHeight.toFixed(2)} km
      {#if hoverValue !== null}
        <span class="tt-sep">|</span>
        <span class="tt-val">{hoverValue.toFixed(2)} {data?.units ?? ''}</span>
      {/if}
    </div>
  {/if}
</div>

<style>
  .cs-viewer {
    position: relative;
    width: 100%;
    height: 100%;
    min-height: 200px;
    cursor: crosshair;
    user-select: none;
    overflow: hidden;
    background: rgb(15, 15, 26);
    border-radius: var(--radius-sm, 4px);
  }

  .cs-canvas {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
  }

  .cs-placeholder {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-muted, rgba(255,255,255,0.35));
    font-size: 13px;
    z-index: 2;
  }

  .cs-tooltip {
    position: absolute;
    padding: 3px 8px;
    background: rgba(15, 15, 26, 0.92);
    border: 1px solid var(--accent-primary, #5b6cf7);
    border-radius: 3px;
    font-family: var(--font-mono, monospace);
    font-size: 11px;
    color: var(--text-primary, #fff);
    pointer-events: none;
    z-index: 50;
    white-space: nowrap;
    backdrop-filter: blur(4px);
  }

  .tt-label { color: var(--text-muted, rgba(255,255,255,0.5)); font-weight: 600; }
  .tt-sep { color: var(--border-color, rgba(255,255,255,0.15)); margin: 0 3px; }
  .tt-val { color: var(--accent-hover, #7b8aff); }
</style>
