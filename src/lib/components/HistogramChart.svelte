<script lang="ts">
  import { onDestroy, createEventDispatcher } from 'svelte';
  import { runFeatureRequest, type FeatureRequestHandle } from '../utils/featureRequest';
  import { selectedSweep, radarData } from '../stores/radarData';

  const dispatch = createEventDispatcher();

  // ── Props ──────────────────────────────────────────────────────────────
  export let variable: string = 'DBZH';
  export let bins: number = 50;
  export let showGaussian: boolean = true;
  export let width: number = 400;
  export let height: number = 340;
  /** Sweep to plot; null follows the sweep shown in the viewer. */
  export let sweepOverride: number | null = null;

  // ── Internal state ─────────────────────────────────────────────────────
  let binEdges: number[] = [];
  let counts: number[] = [];
  let gaussianFit: number[] = [];
  let stats: Record<string, number> = {};
  let units = '';
  let nValid = 0;
  let isLoading = false;
  let errorText = '';

  // Range selection (drag to select value range)
  let rangeSelecting = false;
  let rangeStart: number | null = null;
  let rangeEnd: number | null = null;
  let rangeStartPx = 0;

  // Hover
  let hoveredBin: number | null = null;
  let hoverX = 0;
  let hoverY = 0;

  // Stats rows + info bar under the plot; a narrow chart wraps the stats onto a third row.
  $: statsH = width < 340 ? 72 : 56;
  $: svgH = Math.max(60, height - statsH);
  const MARGIN = { top: 10, right: 12, bottom: 38, left: 52 };
  const plotW = () => width - MARGIN.left - MARGIN.right;
  // Template expressions must read these directly (or get them as arguments): a call to a script
  // function that reads state internally is not re-evaluated when only that state changes.
  $: pw = width - MARGIN.left - MARGIN.right;
  $: ph = svgH - MARGIN.top - MARGIN.bottom;

  const uid = Math.random().toString(36).slice(2, 8);
  let svgEl: SVGSVGElement;
  let handle: FeatureRequestHandle | null = null;

  $: sweep = sweepOverride ?? $selectedSweep;
  $: filePath = $radarData.filePath;

  // Reactive request (a different file re-requests too: the same variable and sweep are another scan)
  let prevKey = '';
  $: {
    const key = `${filePath}:${variable}:${sweep}:${bins}`;
    if (variable && key !== prevKey) {
      prevKey = key;
      requestData();
    }
  }

  function requestData() {
    handle?.cancel();
    isLoading = true;
    errorText = '';
    const v = variable;
    const sw = sweep;
    const h = runFeatureRequest({
      kind: 'dualpol',
      label: `Histogram ${v}`,
      message: { type: 'get_histogram', variable: v, sweep: sw, bins },
      resultTypes: ['histogram_result'],
      accept: (m: any) => m.variable === v && (m.sweep === undefined || m.sweep === sw),
      acceptError: (text) => !text.includes("'") || text.includes(`'${v}'`),
      onResult: (msg: any) => {
        binEdges = msg.bin_edges || [];
        counts = msg.counts || [];
        gaussianFit = msg.gaussian_fit || [];
        stats = msg.stats || {};
        units = msg.units || '';
        nValid = msg.n_valid || 0;
        rangeStart = null;
        rangeEnd = null;
      },
      onSettled: (outcome, err) => {
        if (handle !== h) return; // superseded by a newer request
        isLoading = false;
        handle = null;
        if (outcome === 'error' || outcome === 'timeout') {
          binEdges = [];
          counts = [];
          gaussianFit = [];
          stats = {};
          errorText = outcome === 'timeout' ? 'The request timed out' : (err ?? 'Could not load the data');
        }
      },
      retry: requestData,
    });
    handle = h;
  }

  onDestroy(() => { handle?.cancel(); });

  // ── Derived values ─────────────────────────────────────────────────────
  $: maxCount = counts.length > 0 ? Math.max(...counts, 1) : 1;
  $: binCenters = binEdges.length > 1
    ? binEdges.slice(0, -1).map((e, i) => (e + binEdges[i + 1]) / 2)
    : [];
  $: xMin = binEdges.length > 0 ? binEdges[0] : 0;
  $: xMax = binEdges.length > 0 ? binEdges[binEdges.length - 1] : 1;
  $: barWidth = binEdges.length > 1
    ? (pw / (binEdges.length - 1))
    : pw;

  // ── Coordinate transforms ──────────────────────────────────────────────
  function xToSvg(v: number, lo: number, hi: number, w: number): number {
    return MARGIN.left + ((v - lo) / (hi - lo)) * w;
  }
  function svgToDataX(px: number): number {
    return xMin + ((px - MARGIN.left) / plotW()) * (xMax - xMin);
  }
  function cToSvg(c: number, max: number, h: number): number {
    return MARGIN.top + h - (c / max) * h;
  }

  // ── Ticks ──────────────────────────────────────────────────────────────
  function niceStep(range: number, maxTicks: number): number {
    const rough = range / maxTicks;
    const mag = Math.pow(10, Math.floor(Math.log10(rough)));
    const norm = rough / mag;
    let nice: number;
    if (norm <= 1.5) nice = 1;
    else if (norm <= 3) nice = 2;
    else if (norm <= 7) nice = 5;
    else nice = 10;
    return nice * mag;
  }

  function makeTicks(lo: number, hi: number, maxTicks: number = 6): number[] {
    if (hi <= lo) return [];
    const step = niceStep(hi - lo, maxTicks);
    const start = Math.ceil(lo / step) * step;
    const ticks: number[] = [];
    for (let t = start; t <= hi; t += step) {
      ticks.push(parseFloat(t.toPrecision(10)));
    }
    return ticks;
  }

  $: xTicks = makeTicks(xMin, xMax);
  $: yTicks = makeTicks(0, maxCount, 5);

  function fmtTick(v: number): string {
    if (Math.abs(v) >= 10000) return v.toExponential(1);
    if (Math.abs(v) < 0.01 && v !== 0) return v.toExponential(1);
    return parseFloat(v.toPrecision(4)).toString();
  }

  // ── Gaussian line path ─────────────────────────────────────────────────
  $: gaussianPath = showGaussian && gaussianFit.length > 0 && binCenters.length > 0
    ? 'M' + binCenters.map((cx, i) =>
        `${xToSvg(cx, xMin, xMax, pw)},${cToSvg(gaussianFit[i], maxCount, ph)}`
      ).join('L')
    : '';

  // ── Mouse handlers ─────────────────────────────────────────────────────
  function getSvgCoords(e: MouseEvent): { sx: number; sy: number } {
    const rect = svgEl.getBoundingClientRect();
    return { sx: e.clientX - rect.left, sy: e.clientY - rect.top };
  }

  function onMouseDown(e: MouseEvent) {
    if (e.button !== 0) return;
    const { sx } = getSvgCoords(e);
    rangeSelecting = true;
    rangeStartPx = sx;
    rangeStart = svgToDataX(sx);
    rangeEnd = rangeStart;
  }

  function onMouseMove(e: MouseEvent) {
    const { sx, sy } = getSvgCoords(e);

    if (rangeSelecting) {
      rangeEnd = svgToDataX(sx);
      return;
    }

    // Hover detection
    if (binEdges.length < 2) return;
    const dv = svgToDataX(sx);
    const binIdx = Math.floor(((dv - xMin) / (xMax - xMin)) * counts.length);
    if (binIdx >= 0 && binIdx < counts.length) {
      hoveredBin = binIdx;
      hoverX = sx;
      hoverY = sy;
    } else {
      hoveredBin = null;
    }
  }

  function onMouseUp() {
    if (rangeSelecting && rangeStart !== null && rangeEnd !== null) {
      rangeSelecting = false;
      const lo = Math.min(rangeStart, rangeEnd);
      const hi = Math.max(rangeStart, rangeEnd);
      if (hi - lo > (xMax - xMin) * 0.01) {
        dispatch('rangeSelect', { min: lo, max: hi, variable });
      } else {
        rangeStart = null;
        rangeEnd = null;
      }
    }
  }

  function onDblClick() {
    rangeStart = null;
    rangeEnd = null;
    dispatch('rangeSelect', { min: null, max: null, variable });
  }

  // ── Selection highlight rect ───────────────────────────────────────────
  $: selRectX = rangeStart !== null && rangeEnd !== null
    ? xToSvg(Math.min(rangeStart, rangeEnd), xMin, xMax, pw)
    : 0;
  $: selRectW = rangeStart !== null && rangeEnd !== null
    ? Math.abs(xToSvg(rangeEnd, xMin, xMax, pw) - xToSvg(rangeStart, xMin, xMax, pw))
    : 0;

  // ── Stats format ───────────────────────────────────────────────────────
  function fmtStat(v: number | undefined): string {
    if (v === undefined) return '---';
    return v.toFixed(2);
  }
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<div class="histogram-container" style="width:{width}px;height:{height}px">
  {#if isLoading}
    <div class="hist-loading">Loading...</div>
  {:else if errorText}
    <div class="hist-loading hist-error" role="alert">{errorText}</div>
  {/if}

  <!-- Pointer-driven chart (range selection); the same counts are in the statistics beside it. -->
  <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
  <svg
    bind:this={svgEl}
    role="img"
    aria-label="Histogram of {variable}. Drag to select a range, double-click to reset."
    {width}
    height={svgH}
    on:mousedown={onMouseDown}
    on:mousemove={onMouseMove}
    on:mouseup={onMouseUp}
    on:mouseleave={() => { rangeSelecting = false; hoveredBin = null; }}
    on:dblclick={onDblClick}
    class="hist-svg"
  >
    <defs>
      <clipPath id="hist-clip-{uid}">
        <rect x={MARGIN.left} y={MARGIN.top} width={pw} height={ph} />
      </clipPath>
    </defs>

    <!-- Background -->
    <rect x={MARGIN.left} y={MARGIN.top} width={pw} height={ph} fill="var(--bg-primary)" />

    <!-- Grid lines -->
    {#each xTicks as t}
      <line x1={xToSvg(t, xMin, xMax, pw)} y1={MARGIN.top} x2={xToSvg(t, xMin, xMax, pw)} y2={MARGIN.top + ph}
            stroke="var(--border-color)" stroke-width="0.5" />
      <text x={xToSvg(t, xMin, xMax, pw)} y={svgH - MARGIN.bottom + 16}
            fill="var(--text-muted)" font-size="9" text-anchor="middle"
            font-family="var(--font-mono, monospace)">{fmtTick(t)}</text>
    {/each}
    {#each yTicks as t}
      <line x1={MARGIN.left} y1={cToSvg(t, maxCount, ph)} x2={MARGIN.left + pw} y2={cToSvg(t, maxCount, ph)}
            stroke="var(--border-color)" stroke-width="0.5" />
      <text x={MARGIN.left - 6} y={cToSvg(t, maxCount, ph) + 3}
            fill="var(--text-muted)" font-size="9" text-anchor="end"
            font-family="var(--font-mono, monospace)">{fmtTick(t)}</text>
    {/each}

    <!-- Axis labels -->
    <text x={MARGIN.left + pw / 2} y={svgH - 3}
          fill="var(--text-secondary)" font-size="10" text-anchor="middle"
          font-family="var(--font-mono, monospace)">{variable} {units ? `(${units})` : ''}</text>
    <text x={12} y={MARGIN.top + ph / 2}
          fill="var(--text-secondary)" font-size="10" text-anchor="middle"
          font-family="var(--font-mono, monospace)"
          transform="rotate(-90, 12, {MARGIN.top + ph / 2})">Count</text>

    <!-- Bars -->
    <g clip-path="url(#hist-clip-{uid})">
      {#each counts as c, i}
        <rect
          x={MARGIN.left + i * barWidth}
          y={cToSvg(c, maxCount, ph)}
          width={Math.max(barWidth - 1, 1)}
          height={MARGIN.top + ph - cToSvg(c, maxCount, ph)}
          fill={hoveredBin === i ? 'var(--accent-primary)' : 'rgba(var(--c-accent-rgb), 0.6)'}
        />
      {/each}

      <!-- Gaussian overlay -->
      {#if gaussianPath}
        <path d={gaussianPath} fill="none" stroke="var(--c-caution)" stroke-width="1.5" opacity="0.8" />
      {/if}

      <!-- Mean line -->
      {#if stats.mean !== undefined}
        <line x1={xToSvg(stats.mean, xMin, xMax, pw)} y1={MARGIN.top} x2={xToSvg(stats.mean, xMin, xMax, pw)} y2={MARGIN.top + ph}
              stroke="var(--c-accent)" stroke-width="1" stroke-dasharray="4,3" />
      {/if}
      <!-- Median line -->
      {#if stats.median !== undefined}
        <line x1={xToSvg(stats.median, xMin, xMax, pw)} y1={MARGIN.top} x2={xToSvg(stats.median, xMin, xMax, pw)} y2={MARGIN.top + ph}
              stroke="var(--c-derived)" stroke-width="1" stroke-dasharray="4,3" />
      {/if}

      <!-- Selection highlight -->
      {#if rangeStart !== null && rangeEnd !== null}
        <rect x={selRectX} y={MARGIN.top} width={selRectW} height={ph}
              fill="rgba(var(--c-caution-rgb), 0.15)" stroke="var(--c-caution)" stroke-width="1" />
      {/if}
    </g>

    <!-- Border -->
    <rect x={MARGIN.left} y={MARGIN.top} width={pw} height={ph} fill="none" stroke="var(--border-color)" />

    <!-- Hover tooltip -->
    {#if hoveredBin !== null && binEdges.length > hoveredBin + 1}
      <g pointer-events="none">
        <rect x={hoverX + 8} y={hoverY - 34} width="160" height="28" rx="3"
              fill="rgba(var(--c-surface-1-rgb), 0.92)" stroke="var(--accent-primary)" />
        <text x={hoverX + 14} y={hoverY - 16}
              fill="var(--text-primary)" font-size="10"
              font-family="var(--font-mono, monospace)">
          [{binEdges[hoveredBin].toFixed(1)}, {binEdges[hoveredBin + 1].toFixed(1)}) = {counts[hoveredBin]}
        </text>
      </g>
    {/if}
  </svg>

  <!-- Stats summary -->
  <div class="hist-stats">
    <div class="stat-row">
      <span class="stat-label">Mean</span>
      <span class="stat-value mean-color">{fmtStat(stats.mean)}</span>
      <span class="stat-label">Median</span>
      <span class="stat-value median-color">{fmtStat(stats.median)}</span>
      <span class="stat-label">Std</span>
      <span class="stat-value">{fmtStat(stats.std)}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Min</span>
      <span class="stat-value">{fmtStat(stats.min)}</span>
      <span class="stat-label">Max</span>
      <span class="stat-value">{fmtStat(stats.max)}</span>
      <span class="stat-label">Skew</span>
      <span class="stat-value">{fmtStat(stats.skewness)}</span>
    </div>
  </div>

  <div class="hist-info">
    <span>{nValid} valid pts | {bins} bins</span>
    <span class="hist-hint">Drag: select range | Dbl-click: clear</span>
  </div>
</div>

<style>
  .histogram-container {
    position: relative;
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md, 6px);
    overflow: hidden;
  }
  .hist-svg {
    display: block;
    cursor: col-resize;
    user-select: none;
  }
  .hist-loading {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(var(--c-surface-1-rgb), 0.7);
    color: var(--text-muted);
    font-size: 12px;
    z-index: 5;
  }
  .hist-error {
    background: var(--bg-secondary);
    color: var(--accent-danger);
    padding: 0 16px;
    text-align: center;
  }
  .hist-stats {
    padding: 4px 8px;
    display: flex;
    flex-direction: column;
    gap: 2px;
    background: var(--bg-primary);
    border-top: 1px solid var(--border-color);
  }
  .stat-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0 6px;
    font-size: 9px;
    font-family: var(--font-mono, monospace);
  }
  .stat-label {
    color: var(--text-muted);
    min-width: 32px;
  }
  .stat-value {
    color: var(--text-primary);
    min-width: 48px;
  }
  .mean-color { color: var(--c-accent); }
  .median-color { color: var(--c-derived); }
  .hist-info {
    display: flex;
    justify-content: space-between;
    padding: 2px 8px;
    font-size: 9px;
    font-family: var(--font-mono, monospace);
    color: var(--text-muted);
    background: var(--bg-primary);
    border-top: 1px solid var(--border-color);
  }
  .hist-info span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
  .hist-info span:first-child { flex-shrink: 0; }
  .hist-hint { opacity: 0.6; }
</style>
