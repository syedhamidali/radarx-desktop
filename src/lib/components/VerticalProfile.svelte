<script lang="ts">
  /**
   * VerticalProfile -- Line chart showing value (X) vs height (Y).
   * Supports overlaying multiple variables.
   */
  import { onMount, onDestroy } from 'svelte';
  import type { VerticalProfileData } from '../stores/crossSection';

  export let profiles: VerticalProfileData[] = [];

  let canvasEl: HTMLCanvasElement;
  let containerEl: HTMLDivElement;
  let resizeObserver: ResizeObserver | null = null;
  let width = 400;
  let height = 400;

  // Hover
  let hoverActive = false;
  let hoverScreenX = 0;
  let hoverScreenY = 0;
  let hoverHeight = 0;
  let hoverValues: { variable: string; value: number; units: string; color: string }[] = [];

  const margin = { top: 20, right: 20, bottom: 45, left: 65 };

  // Profile colors (for multiple overlays)
  const PROFILE_COLORS = [
    '#5b6cf7', '#f7a35c', '#4fd1c5', '#f56565',
    '#9f7aea', '#48bb78', '#ed64a6', '#ecc94b',
  ];

  // Also redraw when the profiles are cleared, so the old plot does not linger under the placeholder.
  $: if (canvasEl && profiles) {
    requestAnimationFrame(() => draw());
  }

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

  /** Profiles of different units share no axis: each is then drawn against its own min-max. */
  function unitsDiffer(list: VerticalProfileData[]): boolean {
    return new Set(list.map((p) => p.units || '')).size > 1;
  }

  function finiteValues(p: VerticalProfileData): number[] {
    return p.values.filter((v) => Number.isFinite(v));
  }

  function draw() {
    if (!canvasEl) return;
    const ctx = canvasEl.getContext('2d');
    if (!ctx) return;

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    // Clear
    ctx.fillStyle = 'rgb(15, 15, 26)';
    ctx.fillRect(0, 0, width, height);
    if (profiles.length === 0) return;

    const plotW = width - margin.left - margin.right;
    const plotH = height - margin.top - margin.bottom;
    if (plotW <= 0 || plotH <= 0) return;

    // Compute value and height ranges across all profiles
    let allVals: number[] = [];
    let allHeights: number[] = [];
    for (const p of profiles) {
      for (const v of p.values) { if (Number.isFinite(v)) allVals.push(v); }
      for (const h of p.heights_km) { if (Number.isFinite(h)) allHeights.push(h); }
    }

    if (allVals.length === 0 || allHeights.length === 0) return;

    const normalized = unitsDiffer(profiles);
    let valMin = Math.min(...allVals);
    let valMax = Math.max(...allVals);
    if (valMin === valMax) { valMin -= 1; valMax += 1; }
    const valPad = (valMax - valMin) * 0.1;
    valMin -= valPad;
    valMax += valPad;
    // Per-profile min-max (only used when the units differ).
    const spans = profiles.map((p) => {
      const vs = finiteValues(p);
      let lo = vs.length ? Math.min(...vs) : 0;
      let hi = vs.length ? Math.max(...vs) : 1;
      if (lo === hi) { lo -= 1; hi += 1; }
      return { lo, hi };
    });
    if (normalized) { valMin = -0.05; valMax = 1.05; }

    let hMin = Math.min(...allHeights);
    let hMax = Math.max(...allHeights);
    if (hMin === hMax) { hMin -= 0.5; hMax += 0.5; }
    const hPad = (hMax - hMin) * 0.1;
    hMin = Math.max(0, hMin - hPad);
    hMax += hPad;

    function valToX(v: number, pi: number): number {
      const u = normalized && pi >= 0 ? (v - spans[pi].lo) / (spans[pi].hi - spans[pi].lo) : v;
      return margin.left + ((u - valMin) / (valMax - valMin)) * plotW;
    }
    function hToY(h: number): number {
      return margin.top + plotH - ((h - hMin) / (hMax - hMin)) * plotH;
    }

    // Draw axes
    ctx.strokeStyle = 'rgba(255,255,255,0.25)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(margin.left, margin.top);
    ctx.lineTo(margin.left, margin.top + plotH);
    ctx.lineTo(margin.left + plotW, margin.top + plotH);
    ctx.stroke();

    // Grid and tick marks - X axis (values)
    ctx.fillStyle = 'rgba(255,255,255,0.6)';
    ctx.font = '11px var(--font-mono, monospace)';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';

    const xRange = valMax - valMin;
    const xTickSpacing = normalized ? 0.5 : niceTickSpacing(xRange, plotW / 60);
    const xStart = Math.ceil(valMin / xTickSpacing) * xTickSpacing;
    for (let v = xStart; v <= valMax; v += xTickSpacing) {
      const x = valToX(v, -1);
      if (x >= margin.left && x <= margin.left + plotW) {
        ctx.strokeStyle = 'rgba(255,255,255,0.05)';
        ctx.beginPath();
        ctx.moveTo(x, margin.top);
        ctx.lineTo(x, margin.top + plotH);
        ctx.stroke();

        ctx.strokeStyle = 'rgba(255,255,255,0.3)';
        ctx.beginPath();
        ctx.moveTo(x, margin.top + plotH);
        ctx.lineTo(x, margin.top + plotH + 5);
        ctx.stroke();

        ctx.fillStyle = 'rgba(255,255,255,0.6)';
        const tickLabel = normalized
          ? (Math.abs(v) < 1e-9 ? 'min' : Math.abs(v - 1) < 1e-9 ? 'max' : '')
          : v.toFixed(xTickSpacing < 1 ? 1 : 0);
        ctx.fillText(tickLabel, x, margin.top + plotH + 8);
      }
    }

    // X-axis label (variable names)
    const varNames = profiles.map((p) => p.variable).join(', ');
    const units = profiles[0]?.units ?? '';
    ctx.fillStyle = 'rgba(255,255,255,0.5)';
    ctx.font = '12px var(--font-sans, sans-serif)';
    ctx.fillText(
      normalized ? 'each variable scaled to its own min-max' : units ? `${varNames} (${units})` : varNames,
      margin.left + plotW / 2,
      height - 8,
    );

    // Y-axis ticks (height)
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    ctx.font = '11px var(--font-mono, monospace)';

    const yRange = hMax - hMin;
    const yTickSpacing = niceTickSpacing(yRange, plotH / 40);
    const yStart = Math.ceil(hMin / yTickSpacing) * yTickSpacing;
    for (let h = yStart; h <= hMax; h += yTickSpacing) {
      const y = hToY(h);
      if (y >= margin.top && y <= margin.top + plotH) {
        ctx.strokeStyle = 'rgba(255,255,255,0.05)';
        ctx.beginPath();
        ctx.moveTo(margin.left, y);
        ctx.lineTo(margin.left + plotW, y);
        ctx.stroke();

        ctx.strokeStyle = 'rgba(255,255,255,0.3)';
        ctx.beginPath();
        ctx.moveTo(margin.left - 5, y);
        ctx.lineTo(margin.left, y);
        ctx.stroke();

        ctx.fillStyle = 'rgba(255,255,255,0.6)';
        ctx.fillText(h.toFixed(yTickSpacing < 1 ? 1 : 0), margin.left - 8, y);
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

    // Draw profile lines
    for (let pi = 0; pi < profiles.length; pi++) {
      const p = profiles[pi];
      const color = PROFILE_COLORS[pi % PROFILE_COLORS.length];

      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.lineJoin = 'round';
      ctx.beginPath();

      let started = false;
      for (let i = 0; i < p.heights_km.length; i++) {
        const h = p.heights_km[i];
        const v = p.values[i];
        if (!Number.isFinite(v) || !Number.isFinite(h)) continue;

        const x = valToX(v, pi);
        const y = hToY(h);
        if (!started) {
          ctx.moveTo(x, y);
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
      }
      ctx.stroke();

      // Draw points
      ctx.fillStyle = color;
      for (let i = 0; i < p.heights_km.length; i++) {
        const h = p.heights_km[i];
        const v = p.values[i];
        if (!Number.isFinite(v) || !Number.isFinite(h)) continue;
        const x = valToX(v, pi);
        const y = hToY(h);
        ctx.beginPath();
        ctx.arc(x, y, 3, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    // Legend
    if (profiles.length > 1) {
      const legendX = margin.left + 8;
      let legendY = margin.top + 8;
      for (let pi = 0; pi < profiles.length; pi++) {
        const color = PROFILE_COLORS[pi % PROFILE_COLORS.length];
        ctx.fillStyle = color;
        ctx.fillRect(legendX, legendY, 12, 3);
        ctx.fillStyle = 'rgba(255,255,255,0.7)';
        ctx.font = '10px var(--font-mono, monospace)';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        const range = normalized
          ? ` ${spans[pi].lo.toFixed(1)}..${spans[pi].hi.toFixed(1)}${profiles[pi].units ? ' ' + profiles[pi].units : ''}`
          : '';
        ctx.fillText(profiles[pi].variable + range, legendX + 16, legendY + 2);
        legendY += 16;
      }
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

  function onMouseMove(e: MouseEvent) {
    if (profiles.length === 0) return;
    const rect = containerEl.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;

    const plotW = width - margin.left - margin.right;
    const plotH = height - margin.top - margin.bottom;

    if (px < margin.left || px > margin.left + plotW ||
        py < margin.top || py > margin.top + plotH) {
      hoverActive = false;
      return;
    }

    // Compute height bounds
    let allHeights: number[] = [];
    for (const p of profiles) {
      for (const h of p.heights_km) { if (Number.isFinite(h)) allHeights.push(h); }
    }
    let hMin = Math.min(...allHeights);
    let hMax = Math.max(...allHeights);
    if (hMin === hMax) { hMin -= 0.5; hMax += 0.5; }
    const hPad = (hMax - hMin) * 0.1;
    hMin = Math.max(0, hMin - hPad);
    hMax += hPad;

    const hFrac = 1 - (py - margin.top) / plotH;
    const h = hMin + hFrac * (hMax - hMin);

    hoverActive = true;
    hoverScreenX = e.clientX;
    hoverScreenY = e.clientY;
    hoverHeight = h;

    // Find closest value at this height for each profile
    hoverValues = profiles.map((p, pi) => {
      let bestIdx = 0;
      let bestDist = Infinity;
      for (let i = 0; i < p.heights_km.length; i++) {
        if (!Number.isFinite(p.heights_km[i])) continue;
        const d = Math.abs(p.heights_km[i] - h);
        if (d < bestDist) { bestDist = d; bestIdx = i; }
      }
      return {
        variable: p.variable,
        value: p.values[bestIdx],
        units: p.units,
        color: PROFILE_COLORS[pi % PROFILE_COLORS.length],
      };
    });
  }

  function onMouseLeave() { hoverActive = false; }
</script>

<div class="vp-viewer" bind:this={containerEl}
     on:mousemove={onMouseMove}
     on:mouseleave={onMouseLeave}
     role="img"
     aria-label="Vertical profile: value vs height">
  <canvas bind:this={canvasEl} class="vp-canvas"></canvas>

  {#if profiles.length === 0}
    <div class="vp-placeholder">
      <p>Click a point on the PPI to get a vertical profile</p>
    </div>
  {/if}

  {#if hoverActive && hoverValues.length > 0}
    {@const relX = hoverScreenX - (containerEl?.getBoundingClientRect().left ?? 0)}
    <div
      class="vp-tooltip"
      style="left: {relX}px; top: {hoverScreenY - (containerEl?.getBoundingClientRect().top ?? 0) - 10}px; transform: translateX({relX > width * 0.5 ? 'calc(-100% - 14px)' : '14px'})"
    >
      <div class="tt-height">{hoverHeight.toFixed(2)} km</div>
      {#each hoverValues as hv}
        <div class="tt-row">
          <span class="tt-dot" style="background: {hv.color}"></span>
          <span class="tt-var">{hv.variable}:</span>
          <span class="tt-val">{Number.isFinite(hv.value) ? hv.value.toFixed(2) : 'N/A'} {hv.units}</span>
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .vp-viewer {
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

  .vp-canvas {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
  }

  .vp-placeholder {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-muted, rgba(255,255,255,0.35));
    font-size: 13px;
    z-index: 2;
  }

  .vp-tooltip {
    position: absolute;
    padding: 5px 10px;
    background: rgba(15, 15, 26, 0.92);
    border: 1px solid var(--accent-primary, #5b6cf7);
    border-radius: 4px;
    font-family: var(--font-mono, monospace);
    font-size: 11px;
    color: var(--text-primary, #fff);
    pointer-events: none;
    z-index: 50;
    white-space: nowrap;
    backdrop-filter: blur(4px);
  }

  .tt-height {
    color: var(--text-muted, rgba(255,255,255,0.5));
    font-size: 10px;
    margin-bottom: 2px;
  }

  .tt-row {
    display: flex;
    align-items: center;
    gap: 4px;
  }

  .tt-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .tt-var {
    color: var(--text-secondary, rgba(255,255,255,0.7));
  }

  .tt-val {
    color: var(--accent-hover, #7b8aff);
  }
</style>
