<script lang="ts">
  import { onDestroy, createEventDispatcher } from 'svelte';
  import { runFeatureRequest, type FeatureRequestHandle } from '../utils/featureRequest';
  import { selectedSweep, radarData } from '../stores/radarData';

  const dispatch = createEventDispatcher();

  // ── Props ──────────────────────────────────────────────────────────────
  export let var1: string = 'DBZH';
  export let var2: string = 'ZDR';
  export let colorVar: string | null = null;
  export let subsample: number = 5000;
  export let width: number = 400;
  export let height: number = 340;
  /** Sweep to plot; null follows the sweep shown in the viewer. */
  export let sweepOverride: number | null = null;

  // ── Internal state ─────────────────────────────────────────────────────
  let data: { x: number[]; y: number[]; color?: number[] } | null = null;
  let units1 = '';
  let units2 = '';
  let nReturned = 0;
  let nValid = 0;
  let isLoading = false;
  let errorText = '';

  // View transform (zoom/pan)
  let viewXMin = 0;
  let viewXMax = 1;
  let viewYMin = 0;
  let viewYMax = 1;

  // Interaction state
  let hoveredIdx: number | null = null;
  let hoverX = 0;
  let hoverY = 0;
  let isDragging = false;
  let dragStartSvgX = 0;
  let dragStartSvgY = 0;
  let dragStartVXMin = 0;
  let dragStartVXMax = 0;
  let dragStartVYMin = 0;
  let dragStartVYMax = 0;

  // Lasso selection
  let lassoActive = false;
  let lassoPoints: { x: number; y: number }[] = [];
  let selectedIndices: Set<number> = new Set();

  // Density mode
  export let densityMode: boolean = false;
  let densityGrid: number[][] = [];
  const DENSITY_BINS = 50;

  // Layout constants
  const INFO_H = 18; // the point-count bar under the plot
  $: svgH = Math.max(60, height - INFO_H);
  const MARGIN = { top: 10, right: 15, bottom: 38, left: 52 };
  const plotW = () => width - MARGIN.left - MARGIN.right;
  const plotH = () => svgH - MARGIN.top - MARGIN.bottom;
  // Template expressions must read these directly (or get them as arguments): a call to a script
  // function that reads state internally is not re-evaluated when only that state changes.
  $: pw = width - MARGIN.left - MARGIN.right;
  $: ph = svgH - MARGIN.top - MARGIN.bottom;

  const uid = Math.random().toString(36).slice(2, 8);
  let svgEl: SVGSVGElement;
  let handle: FeatureRequestHandle | null = null;

  $: sweep = sweepOverride ?? $selectedSweep;
  $: filePath = $radarData.filePath;

  // Reactive request (a different file re-requests too: the same variables and sweep are another scan)
  let prevKey = '';
  $: {
    const key = `${filePath}:${var1}:${var2}:${sweep}:${colorVar}:${subsample}`;
    if (var1 && var2 && key !== prevKey) {
      prevKey = key;
      requestData();
    }
  }

  function requestData() {
    handle?.cancel();
    isLoading = true;
    errorText = '';
    const v1 = var1;
    const v2 = var2;
    const sw = sweep;
    const msg: Record<string, any> = {
      type: 'get_scatter_data',
      var1: v1,
      var2: v2,
      sweep: sw,
      subsample,
    };
    if (colorVar) msg.color_var = colorVar;
    const mine = (text: string) => {
      const quoted = [...text.matchAll(/'([^']+)'/g)].map((m) => m[1]);
      return quoted.length === 0 || quoted.includes(v1) || quoted.includes(v2) || (!!colorVar && quoted.includes(colorVar));
    };
    const h = runFeatureRequest({
      kind: 'dualpol',
      label: `Scatter ${v1} vs ${v2}`,
      message: msg,
      resultTypes: ['scatter_data_result'],
      accept: (m: any) => m.var1 === v1 && m.var2 === v2 && (m.sweep === undefined || m.sweep === sw),
      acceptError: mine,
      onResult: (m: any) => {
        data = { x: m.x, y: m.y, color: m.color };
        units1 = m.units1 || '';
        units2 = m.units2 || '';
        nReturned = m.n_returned || 0;
        nValid = m.n_valid || 0;
        selectedIndices = new Set();
        fitView();
        if (densityMode) computeDensity();
      },
      onSettled: (outcome, err) => {
        if (handle !== h) return; // superseded by a newer request
        isLoading = false;
        handle = null;
        if (outcome === 'error' || outcome === 'timeout') {
          data = null;
          errorText = outcome === 'timeout' ? 'The request timed out' : (err ?? 'Could not load the data');
        }
      },
      retry: requestData,
    });
    handle = h;
  }

  onDestroy(() => { handle?.cancel(); });

  /** Single-pass min/max (no argument spreading: spread throws on very large arrays). */
  function minMax(arr: ArrayLike<number>): [number, number] {
    let lo = Infinity;
    let hi = -Infinity;
    for (let i = 0; i < arr.length; i++) {
      const v = arr[i];
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    return [lo, hi];
  }

  function fitView() {
    if (!data || data.x.length === 0) return;
    const [xLo, xHi] = minMax(data.x);
    const [yLo, yHi] = minMax(data.y);
    const xPad = (xHi - xLo) * 0.05 || 1;
    const yPad = (yHi - yLo) * 0.05 || 1;
    viewXMin = xLo - xPad;
    viewXMax = xHi + xPad;
    viewYMin = yLo - yPad;
    viewYMax = yHi + yPad;
  }

  // ── Coordinate transforms ──────────────────────────────────────────────
  function xToSvg(v: number, lo: number, hi: number, w: number): number {
    return MARGIN.left + ((v - lo) / (hi - lo)) * w;
  }
  function yToSvg(v: number, lo: number, hi: number, h: number): number {
    return MARGIN.top + h - ((v - lo) / (hi - lo)) * h;
  }
  function svgToDataX(px: number): number {
    return viewXMin + ((px - MARGIN.left) / plotW()) * (viewXMax - viewXMin);
  }
  function svgToDataY(px: number): number {
    return viewYMin + ((plotH() - (px - MARGIN.top)) / plotH()) * (viewYMax - viewYMin);
  }

  // ── Grid lines ─────────────────────────────────────────────────────────
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
    const step = niceStep(hi - lo, maxTicks);
    const start = Math.ceil(lo / step) * step;
    const ticks: number[] = [];
    for (let t = start; t <= hi; t += step) {
      ticks.push(parseFloat(t.toPrecision(10)));
    }
    return ticks;
  }

  $: xTicks = makeTicks(viewXMin, viewXMax);
  $: yTicks = makeTicks(viewYMin, viewYMax);

  // ── Point color ────────────────────────────────────────────────────────
  // Colour range computed once per data set (pointColor runs once per plotted point,
  // so scanning the whole colour array inside it was O(n^2) per render).
  $: [colorMin, colorMax] = data?.color && data.color.length > 0 ? minMax(data.color) : [0, 1];

  function pointColor(i: number): string {
    if (selectedIndices.size > 0) {
      return selectedIndices.has(i) ? 'var(--c-caution)' : 'rgba(var(--c-ink-rgb), 0.22)';
    }
    if (data?.color && data.color.length > i) {
      const c = data.color;
      const t = colorMax > colorMin ? (c[i] - colorMin) / (colorMax - colorMin) : 0.5;
      return hslColor(t);
    }
    return 'rgba(var(--c-accent-rgb), 0.5)';
  }

  function hslColor(t: number): string {
    // Viridis-like: purple -> teal -> yellow
    const h = 260 - t * 200;
    const s = 70 + t * 20;
    const l = 30 + t * 40;
    return `hsl(${h}, ${s}%, ${l}%)`;
  }

  // ── Density heatmap ────────────────────────────────────────────────────
  // The bins cover the whole data extent (not the current zoom), so the heat map follows pan and zoom.
  let densityExtent = { xLo: 0, xHi: 1, yLo: 0, yHi: 1 };

  function computeDensity() {
    if (!data || data.x.length === 0) return;
    const [xLo, xHi] = minMax(data.x);
    const [yLo, yHi] = minMax(data.y);
    const xRange = xHi - xLo || 1;
    const yRange = yHi - yLo || 1;
    densityExtent = { xLo, xHi: xLo + xRange, yLo, yHi: yLo + yRange };
    const grid: number[][] = Array.from({ length: DENSITY_BINS }, () =>
      new Array(DENSITY_BINS).fill(0)
    );
    for (let i = 0; i < data.x.length; i++) {
      const bx = Math.min(DENSITY_BINS - 1, Math.floor(((data.x[i] - xLo) / xRange) * DENSITY_BINS));
      const by = Math.min(DENSITY_BINS - 1, Math.floor(((data.y[i] - yLo) / yRange) * DENSITY_BINS));
      grid[by][bx]++;
    }
    densityGrid = grid;
  }

  $: if (densityMode && data) computeDensity();

  function densityColor(count: number, maxCount: number): string {
    if (count === 0) return 'transparent';
    const t = Math.log1p(count) / Math.log1p(maxCount);
    const h = 260 - t * 200;
    const s = 70 + t * 20;
    const l = 20 + t * 50;
    return `hsl(${h}, ${s}%, ${l}%)`;
  }

  // ── Mouse handlers ─────────────────────────────────────────────────────
  function getSvgCoords(e: MouseEvent): { sx: number; sy: number } {
    const rect = svgEl.getBoundingClientRect();
    return { sx: e.clientX - rect.left, sy: e.clientY - rect.top };
  }

  function onWheel(e: WheelEvent) {
    e.preventDefault();
    const { sx, sy } = getSvgCoords(e);
    const dataX = svgToDataX(sx);
    const dataY = svgToDataY(sy);
    const factor = e.deltaY < 0 ? 0.85 : 1.18;

    viewXMin = dataX - (dataX - viewXMin) * factor;
    viewXMax = dataX + (viewXMax - dataX) * factor;
    viewYMin = dataY - (dataY - viewYMin) * factor;
    viewYMax = dataY + (viewYMax - dataY) * factor;
  }

  function onMouseDown(e: MouseEvent) {
    if (e.shiftKey) {
      // Start lasso
      lassoActive = true;
      lassoPoints = [];
      const { sx, sy } = getSvgCoords(e);
      lassoPoints.push({ x: svgToDataX(sx), y: svgToDataY(sy) });
      return;
    }
    if (e.button === 0) {
      isDragging = true;
      const { sx, sy } = getSvgCoords(e);
      dragStartSvgX = sx;
      dragStartSvgY = sy;
      dragStartVXMin = viewXMin;
      dragStartVXMax = viewXMax;
      dragStartVYMin = viewYMin;
      dragStartVYMax = viewYMax;
    }
  }

  function onMouseMove(e: MouseEvent) {
    const { sx, sy } = getSvgCoords(e);

    if (lassoActive) {
      lassoPoints = [...lassoPoints, { x: svgToDataX(sx), y: svgToDataY(sy) }];
      return;
    }

    if (isDragging) {
      const dxPx = sx - dragStartSvgX;
      const dyPx = sy - dragStartSvgY;
      const dxData = -(dxPx / plotW()) * (dragStartVXMax - dragStartVXMin);
      const dyData = (dyPx / plotH()) * (dragStartVYMax - dragStartVYMin);
      viewXMin = dragStartVXMin + dxData;
      viewXMax = dragStartVXMax + dxData;
      viewYMin = dragStartVYMin + dyData;
      viewYMax = dragStartVYMax + dyData;
      return;
    }

    // Hover detection
    if (!data) return;
    const dx = svgToDataX(sx);
    const dy = svgToDataY(sy);
    let bestDist = Infinity;
    let bestIdx: number | null = null;
    const xRange = viewXMax - viewXMin;
    const yRange = viewYMax - viewYMin;
    for (let i = 0; i < data.x.length; i++) {
      const ndx = (data.x[i] - dx) / xRange;
      const ndy = (data.y[i] - dy) / yRange;
      const d = ndx * ndx + ndy * ndy;
      if (d < bestDist && d < 0.001) {
        bestDist = d;
        bestIdx = i;
      }
    }
    hoveredIdx = bestIdx;
    hoverX = sx;
    hoverY = sy;
  }

  function onMouseUp() {
    isDragging = false;
    if (lassoActive) {
      lassoActive = false;
      computeLassoSelection();
    }
  }

  function onDblClick() {
    selectedIndices = new Set();
    dispatch('selection', { indices: [] });
    fitView();
  }

  // ── Lasso selection ────────────────────────────────────────────────────
  function computeLassoSelection() {
    if (!data || lassoPoints.length < 3) return;
    const sel = new Set<number>();
    for (let i = 0; i < data.x.length; i++) {
      if (pointInPolygon(data.x[i], data.y[i], lassoPoints)) {
        sel.add(i);
      }
    }
    selectedIndices = sel;
    dispatch('selection', { indices: Array.from(sel) });
  }

  function pointInPolygon(px: number, py: number, poly: { x: number; y: number }[]): boolean {
    let inside = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      const xi = poly[i].x, yi = poly[i].y;
      const xj = poly[j].x, yj = poly[j].y;
      if ((yi > py) !== (yj > py) && px < ((xj - xi) * (py - yi)) / (yj - yi) + xi) {
        inside = !inside;
      }
    }
    return inside;
  }

  // ── Lasso SVG path ─────────────────────────────────────────────────────
  $: lassoPath = lassoPoints.length > 1
    ? 'M' + lassoPoints.map(p => `${xToSvg(p.x, viewXMin, viewXMax, pw)},${yToSvg(p.y, viewYMin, viewYMax, ph)}`).join('L') + 'Z'
    : '';

  // ── Density rendering helpers ──────────────────────────────────────────
  $: maxDensity = densityGrid.length > 0
    ? Math.max(1, ...densityGrid.flat())
    : 1;

  function fmtTick(v: number): string {
    if (Math.abs(v) >= 1000) return v.toExponential(1);
    if (Math.abs(v) < 0.01 && v !== 0) return v.toExponential(1);
    return parseFloat(v.toPrecision(4)).toString();
  }
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<div class="scatter-container" style="width:{width}px;height:{height}px">
  {#if isLoading}
    <div class="scatter-loading">Loading...</div>
  {:else if errorText}
    <div class="scatter-loading scatter-error" role="alert">{errorText}</div>
  {/if}

  <svg
    bind:this={svgEl}
    {width}
    height={svgH}
    on:wheel={onWheel}
    on:mousedown={onMouseDown}
    on:mousemove={onMouseMove}
    on:mouseup={onMouseUp}
    on:mouseleave={() => { isDragging = false; hoveredIdx = null; }}
    on:dblclick={onDblClick}
    class="scatter-svg"
  >
    <!-- Clip path -->
    <defs>
      <clipPath id="plot-clip-{uid}">
        <rect x={MARGIN.left} y={MARGIN.top} width={pw} height={ph} />
      </clipPath>
    </defs>

    <!-- Background -->
    <rect x={MARGIN.left} y={MARGIN.top} width={pw} height={ph} fill="var(--bg-primary)" />

    <!-- Grid lines -->
    {#each xTicks as t}
      <line
        x1={xToSvg(t, viewXMin, viewXMax, pw)} y1={MARGIN.top}
        x2={xToSvg(t, viewXMin, viewXMax, pw)} y2={MARGIN.top + ph}
        stroke="var(--border-color)" stroke-width="0.5"
      />
      <text x={xToSvg(t, viewXMin, viewXMax, pw)} y={svgH - MARGIN.bottom + 16}
            fill="var(--text-muted)" font-size="9" text-anchor="middle"
            font-family="var(--font-mono, monospace)">{fmtTick(t)}</text>
    {/each}
    {#each yTicks as t}
      <line
        x1={MARGIN.left} y1={yToSvg(t, viewYMin, viewYMax, ph)}
        x2={MARGIN.left + pw} y2={yToSvg(t, viewYMin, viewYMax, ph)}
        stroke="var(--border-color)" stroke-width="0.5"
      />
      <text x={MARGIN.left - 6} y={yToSvg(t, viewYMin, viewYMax, ph) + 3}
            fill="var(--text-muted)" font-size="9" text-anchor="end"
            font-family="var(--font-mono, monospace)">{fmtTick(t)}</text>
    {/each}

    <!-- Axis labels -->
    <text x={MARGIN.left + pw / 2} y={svgH - 3}
          fill="var(--text-secondary)" font-size="10" text-anchor="middle"
          font-family="var(--font-mono, monospace)">{var1} {units1 ? `(${units1})` : ''}</text>
    <text x={12} y={MARGIN.top + ph / 2}
          fill="var(--text-secondary)" font-size="10" text-anchor="middle"
          font-family="var(--font-mono, monospace)"
          transform="rotate(-90, 12, {MARGIN.top + ph / 2})">{var2} {units2 ? `(${units2})` : ''}</text>

    <!-- Data points or density -->
    <g clip-path="url(#plot-clip-{uid})">
      {#if densityMode && densityGrid.length > 0}
        {#each densityGrid as row, by}
          {#each row as count, bx}
            {#if count > 0}
              {@const x0 = xToSvg(densityExtent.xLo + (bx / DENSITY_BINS) * (densityExtent.xHi - densityExtent.xLo), viewXMin, viewXMax, pw)}
              {@const x1 = xToSvg(densityExtent.xLo + ((bx + 1) / DENSITY_BINS) * (densityExtent.xHi - densityExtent.xLo), viewXMin, viewXMax, pw)}
              {@const y0 = yToSvg(densityExtent.yLo + ((by + 1) / DENSITY_BINS) * (densityExtent.yHi - densityExtent.yLo), viewYMin, viewYMax, ph)}
              {@const y1 = yToSvg(densityExtent.yLo + (by / DENSITY_BINS) * (densityExtent.yHi - densityExtent.yLo), viewYMin, viewYMax, ph)}
              <rect x={x0} y={y0} width={Math.max(1, x1 - x0 + 1)} height={Math.max(1, y1 - y0 + 1)} fill={densityColor(count, maxDensity)} />
            {/if}
          {/each}
        {/each}
      {:else if data}
        {#each data.x as xv, i}
          <circle
            cx={xToSvg(xv, viewXMin, viewXMax, pw)}
            cy={yToSvg(data.y[i], viewYMin, viewYMax, ph)}
            r={hoveredIdx === i ? 4 : 2}
            fill={pointColor(i)}
            opacity={hoveredIdx === i ? 1 : 0.6}
          />
        {/each}
      {/if}

      <!-- Lasso path -->
      {#if lassoPath}
        <path d={lassoPath} fill="rgba(var(--c-caution-rgb), 0.1)" stroke="var(--c-caution)" stroke-width="1.5" stroke-dasharray="4,3" />
      {/if}
    </g>

    <!-- Border -->
    <rect x={MARGIN.left} y={MARGIN.top} width={pw} height={ph} fill="none" stroke="var(--border-color)" />

    <!-- Hover tooltip -->
    {#if hoveredIdx !== null && data}
      <g pointer-events="none">
        <rect x={hoverX + 8} y={hoverY - 30} width="140" height="24" rx="3"
              fill="rgba(var(--c-surface-1-rgb), 0.92)" stroke="var(--accent-primary)" />
        <text x={hoverX + 14} y={hoverY - 14}
              fill="var(--text-primary)" font-size="10"
              font-family="var(--font-mono, monospace)">
          {data.x[hoveredIdx].toFixed(2)}, {data.y[hoveredIdx].toFixed(2)}
        </text>
      </g>
    {/if}
  </svg>

  <div class="scatter-info">
    <span>{nReturned}/{nValid} pts</span>
    <span class="scatter-hint">Shift+drag: lasso | Scroll: zoom | Dbl-click: reset</span>
  </div>
</div>

<style>
  .scatter-container {
    position: relative;
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md, 6px);
    overflow: hidden;
  }
  .scatter-svg {
    display: block;
    cursor: crosshair;
    user-select: none;
  }
  .scatter-loading {
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
  .scatter-error {
    background: var(--bg-secondary);
    color: var(--accent-danger);
    padding: 0 16px;
    text-align: center;
  }
  .scatter-info {
    display: flex;
    justify-content: space-between;
    padding: 2px 8px;
    font-size: 9px;
    font-family: var(--font-mono, monospace);
    color: var(--text-muted);
    background: var(--bg-primary);
    border-top: 1px solid var(--border-color);
  }
  .scatter-info span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
  .scatter-info span:first-child { flex-shrink: 0; }
  .scatter-hint { opacity: 0.6; }
</style>
