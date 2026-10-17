<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { wsManager } from '../utils/websocket';
  import { openFiles } from '../stores/fileManager';
  import { selectedVariable, selectedSweep, radarData } from '../stores/radarData';
  import {
    probePoint,
    timeSeriesData,
    timeSeriesVariables,
    updateTimeSeries,
    clearTimeSeries,
    type TimeSeriesData,
    type TimeSeriesPoint,
    type ProbePoint,
  } from '../stores/temporal';
  import CollapsiblePanel from './CollapsiblePanel.svelte';

  // ── Chart dimensions ───────────────────────────────────────────────────
  let containerEl: HTMLDivElement;
  let width = 400;
  let height = 200;
  const margin = { top: 16, right: 16, bottom: 32, left: 48 };

  $: plotW = Math.max(width - margin.left - margin.right, 1);
  $: plotH = Math.max(height - margin.top - margin.bottom, 1);

  // ── State ──────────────────────────────────────────────────────────────
  let collapsed = false;
  let variables: string[] = [];
  let selectedVars: string[] = [];
  let probe: ProbePoint | null = null;
  let series: Map<string, TimeSeriesData> = new Map();
  let hoverIdx: number | null = null;
  let hoverX = 0;
  let hoverY = 0;

  // Zoom / pan
  let zoomLevel = 1;
  let panOffset = 0;
  let isPanning = false;
  let panStartX = 0;
  let panStartOffset = 0;

  $: variables = $radarData.variables;
  $: probe = $probePoint;
  $: series = $timeSeriesData;
  $: selectedVars = $timeSeriesVariables.length > 0 ? $timeSeriesVariables : ($selectedVariable ? [$selectedVariable] : []);

  // Colors for multi-line
  const LINE_COLORS = [
    'var(--accent-primary, #5B6CF7)',
    'var(--accent-success, #22C55E)',
    'var(--accent-warning, #F59E0B)',
    'var(--accent-danger, #EF4444)',
    '#06B6D4',
    '#A855F7',
    '#EC4899',
    '#14B8A6',
  ];

  // ── Request time series when probe changes ─────────────────────────────
  let unsubTimeSeries: (() => void) | null = null;

  onMount(() => {
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        width = entry.contentRect.width;
        height = Math.max(entry.contentRect.height, 160);
      }
    });
    if (containerEl) ro.observe(containerEl);

    unsubTimeSeries = wsManager.onMessage('time_series_result', (msg: any) => {
      const points: TimeSeriesPoint[] = [];
      const fileIds: string[] = msg.file_ids ?? [];
      const values: (number | null)[] = msg.values ?? [];
      const timestamps: (string | null)[] = msg.timestamps ?? [];
      const files = $openFiles;

      for (let i = 0; i < fileIds.length; i++) {
        const file = files.find((f) => f.id === fileIds[i]);
        points.push({
          fileId: fileIds[i],
          value: values[i],
          timestamp: timestamps[i] ?? null,
          label: file?.filename ?? fileIds[i].slice(0, 8),
        });
      }

      updateTimeSeries(msg.variable, {
        variable: msg.variable,
        sweep: msg.sweep,
        azimuth: msg.azimuth,
        rangeM: msg.range_m,
        points,
      });
    });

    return () => {
      ro.disconnect();
      if (unsubTimeSeries) unsubTimeSeries();
    };
  });

  // Watch for probe point changes and request data
  $: if (probe && $openFiles.length > 1) {
    requestTimeSeries(probe);
  }

  function requestTimeSeries(pt: ProbePoint) {
    const fileIds = $openFiles.map((f) => f.id);
    if (fileIds.length < 2) return;

    for (const v of selectedVars) {
      wsManager.send({
        type: 'get_time_series',
        variable: v,
        sweep: $selectedSweep,
        azimuth: pt.azimuth,
        range_m: pt.rangeM,
        file_ids: fileIds,
      });
    }
  }

  function toggleVariable(v: string) {
    timeSeriesVariables.update((arr) => {
      if (arr.includes(v)) return arr.filter((x) => x !== v);
      return [...arr, v];
    });
  }

  // ── Chart data computation ─────────────────────────────────────────────
  interface LineSeries {
    variable: string;
    color: string;
    points: { x: number; y: number; value: number | null; label: string }[];
  }

  // zoomLevel/panOffset are passed explicitly: Svelte only tracks variables named in the
  // `$:` statement itself, so reads inside the function would not re-run it on zoom/pan.
  $: lines = computeLines(series, selectedVars, plotW, plotH, zoomLevel, panOffset);

  function computeLines(
    seriesMap: Map<string, TimeSeriesData>,
    vars: string[],
    pw: number,
    ph: number,
    zoom: number,
    pan: number,
  ): LineSeries[] {
    const result: LineSeries[] = [];

    for (let vi = 0; vi < vars.length; vi++) {
      const v = vars[vi];
      const sd = seriesMap.get(v);
      if (!sd || sd.points.length === 0) continue;

      const validValues = sd.points.filter((p) => p.value !== null).map((p) => p.value as number);
      if (validValues.length === 0) continue;

      const yMin = Math.min(...validValues);
      const yMax = Math.max(...validValues);
      const yRange = yMax - yMin || 1;

      const n = sd.points.length;
      const totalW = pw * zoom;
      const points = sd.points.map((p, i) => {
        const rawX = n > 1 ? (i / (n - 1)) * totalW : totalW / 2;
        const x = rawX + pan;
        const y = p.value !== null ? ph - ((p.value - yMin) / yRange) * ph : ph;
        return { x, y, value: p.value, label: p.label };
      });

      result.push({
        variable: v,
        color: LINE_COLORS[vi % LINE_COLORS.length],
        points,
      });
    }

    return result;
  }

  // ── Y-axis ticks ───────────────────────────────────────────────────────
  $: yTicks = computeYTicks(series, selectedVars, plotH);

  function computeYTicks(
    seriesMap: Map<string, TimeSeriesData>,
    vars: string[],
    ph: number,
  ): { y: number; label: string }[] {
    // Use first variable's range for y-axis
    for (const v of vars) {
      const sd = seriesMap.get(v);
      if (!sd) continue;
      const vals = sd.points.filter((p) => p.value !== null).map((p) => p.value as number);
      if (vals.length === 0) continue;

      const yMin = Math.min(...vals);
      const yMax = Math.max(...vals);
      const yRange = yMax - yMin || 1;
      const nTicks = 5;
      const ticks: { y: number; label: string }[] = [];
      for (let i = 0; i <= nTicks; i++) {
        const val = yMin + (yRange * i) / nTicks;
        const y = ph - (i / nTicks) * ph;
        ticks.push({ y, label: val.toFixed(1) });
      }
      return ticks;
    }
    return [];
  }

  // ── X-axis ticks ───────────────────────────────────────────────────────
  $: xTicks = computeXTicks(series, selectedVars, plotW, zoomLevel, panOffset);

  function computeXTicks(
    seriesMap: Map<string, TimeSeriesData>,
    vars: string[],
    pw: number,
    zoom: number,
    pan: number,
  ): { x: number; label: string }[] {
    for (const v of vars) {
      const sd = seriesMap.get(v);
      if (!sd) continue;
      const n = sd.points.length;
      if (n === 0) continue;

      const totalW = pw * zoom;
      const step = Math.max(1, Math.floor(n / 6));
      const ticks: { x: number; label: string }[] = [];
      for (let i = 0; i < n; i += step) {
        const x = (n > 1 ? (i / (n - 1)) * totalW : totalW / 2) + pan;
        if (x >= 0 && x <= pw) {
          const pt = sd.points[i];
          const label = pt.timestamp ? pt.timestamp.slice(11, 16) : `#${i + 1}`;
          ticks.push({ x, label });
        }
      }
      return ticks;
    }
    return [];
  }

  // ── Polyline path ──────────────────────────────────────────────────────
  function polylinePath(pts: { x: number; y: number; value: number | null }[]): string {
    let d = '';
    let started = false;
    for (const p of pts) {
      if (p.value === null) {
        started = false;
        continue;
      }
      if (!started) {
        d += `M${p.x},${p.y}`;
        started = true;
      } else {
        d += `L${p.x},${p.y}`;
      }
    }
    return d;
  }

  // ── Interaction ────────────────────────────────────────────────────────
  function handleChartMouseMove(e: MouseEvent) {
    const rect = (e.currentTarget as SVGElement).getBoundingClientRect();
    const mx = e.clientX - rect.left - margin.left;
    const my = e.clientY - rect.top - margin.top;

    if (isPanning) {
      panOffset = panStartOffset + (mx - panStartX);
      return;
    }

    // Find nearest point across all lines
    let bestDist = Infinity;
    let bestIdx: number | null = null;

    for (const line of lines) {
      for (let i = 0; i < line.points.length; i++) {
        const p = line.points[i];
        if (p.value === null) continue;
        const dist = Math.abs(p.x - mx);
        if (dist < bestDist) {
          bestDist = dist;
          bestIdx = i;
        }
      }
    }

    if (bestDist < 30) {
      hoverIdx = bestIdx;
      hoverX = mx;
      hoverY = my;
    } else {
      hoverIdx = null;
    }
  }

  function handleChartMouseDown(e: MouseEvent) {
    if (e.button === 0) {
      const rect = (e.currentTarget as SVGElement).getBoundingClientRect();
      isPanning = true;
      panStartX = e.clientX - rect.left - margin.left;
      panStartOffset = panOffset;
    }
  }

  function handleChartMouseUp() {
    isPanning = false;
  }

  function handleChartWheel(e: WheelEvent) {
    e.preventDefault();
    const delta = e.deltaY > 0 ? -0.1 : 0.1;
    zoomLevel = Math.max(0.5, Math.min(10, zoomLevel + delta));
  }

  function handleChartMouseLeave() {
    hoverIdx = null;
    isPanning = false;
  }

  // ── Hover tooltip data ─────────────────────────────────────────────────
  $: hoverData = computeHoverData(lines, hoverIdx);

  function computeHoverData(
    allLines: LineSeries[],
    idx: number | null,
  ): { variable: string; color: string; value: string; label: string }[] | null {
    if (idx === null) return null;
    const items: { variable: string; color: string; value: string; label: string }[] = [];
    for (const line of allLines) {
      if (idx < line.points.length) {
        const p = line.points[idx];
        items.push({
          variable: line.variable,
          color: line.color,
          value: p.value !== null ? p.value.toFixed(2) : 'N/A',
          label: p.label,
        });
      }
    }
    return items.length > 0 ? items : null;
  }

  $: hasData = lines.length > 0 && lines.some((l) => l.points.length > 0);
  $: fileCount = $openFiles.length;
</script>

<CollapsiblePanel title="Time Series" icon="" bind:collapsed badge={hasData ? selectedVars.length : null}>
  <div class="ts-chart" bind:this={containerEl}>
    {#if fileCount < 2}
      <div class="ts-empty">
        <span class="ts-empty-icon">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
            <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
          </svg>
        </span>
        <span>Open 2+ files to view time series</span>
      </div>
    {:else if !probe}
      <div class="ts-empty">
        <span class="ts-empty-icon">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
            <circle cx="12" cy="12" r="3" />
            <path d="M12 2v4m0 12v4m-10-10h4m12 0h4" />
          </svg>
        </span>
        <span>Click on the PPI to set a probe point</span>
      </div>
    {:else if !hasData}
      <div class="ts-empty">
        <span class="ts-empty-icon">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
            <circle cx="12" cy="12" r="10" /><path d="M12 6v6l4 2" />
          </svg>
        </span>
        <span>Loading time series...</span>
      </div>
    {:else}
      <!-- Variable selector pills -->
      <div class="ts-var-selector">
        {#each variables.slice(0, 8) as v, i}
          <button
            class="ts-var-pill"
            class:active={selectedVars.includes(v)}
            style="--pill-color: {LINE_COLORS[i % LINE_COLORS.length]}"
            on:click={() => toggleVariable(v)}
          >
            {v}
          </button>
        {/each}
      </div>

      <!-- SVG Chart -->
      <!-- svelte-ignore a11y-no-static-element-interactions -->
      <svg
        class="ts-svg"
        viewBox="0 0 {width} {height}"
        on:mousemove={handleChartMouseMove}
        on:mousedown={handleChartMouseDown}
        on:mouseup={handleChartMouseUp}
        on:mouseleave={handleChartMouseLeave}
        on:wheel={handleChartWheel}
      >
        <g transform="translate({margin.left},{margin.top})">
          <!-- Grid lines -->
          {#each yTicks as tick}
            <line class="ts-grid" x1="0" y1={tick.y} x2={plotW} y2={tick.y} />
            <text class="ts-axis-label ts-y-label" x="-8" y={tick.y + 3} text-anchor="end">{tick.label}</text>
          {/each}

          {#each xTicks as tick}
            <text class="ts-axis-label ts-x-label" x={tick.x} y={plotH + 16} text-anchor="middle">{tick.label}</text>
          {/each}

          <!-- Clip path for plot area -->
          <clipPath id="ts-clip">
            <rect x="0" y="0" width={plotW} height={plotH} />
          </clipPath>

          <g clip-path="url(#ts-clip)">
            <!-- Data lines -->
            {#each lines as line}
              <path
                class="ts-line"
                d={polylinePath(line.points)}
                stroke={line.color}
                fill="none"
                stroke-width="2"
              />
              <!-- Data points -->
              {#each line.points as pt, i}
                {#if pt.value !== null}
                  <circle
                    class="ts-dot"
                    cx={pt.x}
                    cy={pt.y}
                    r={hoverIdx === i ? 5 : 3}
                    fill={line.color}
                    opacity={hoverIdx === i ? 1 : 0.7}
                  />
                {/if}
              {/each}
            {/each}

            <!-- Hover crosshair -->
            {#if hoverIdx !== null}
              <line class="ts-crosshair" x1={hoverX} y1="0" x2={hoverX} y2={plotH} />
            {/if}
          </g>

          <!-- Axes -->
          <line class="ts-axis" x1="0" y1="0" x2="0" y2={plotH} />
          <line class="ts-axis" x1="0" y1={plotH} x2={plotW} y2={plotH} />
        </g>

        <!-- Tooltip -->
        {#if hoverData}
          <foreignObject
            x={Math.min(hoverX + margin.left + 12, width - 140)}
            y={Math.max(hoverY + margin.top - 40, 4)}
            width="130"
            height={28 + hoverData.length * 18}
          >
            <div class="ts-tooltip">
              <div class="ts-tooltip-title">{hoverData[0]?.label ?? ''}</div>
              {#each hoverData as item}
                <div class="ts-tooltip-row">
                  <span class="ts-tooltip-dot" style="background: {item.color}"></span>
                  <span class="ts-tooltip-var">{item.variable}</span>
                  <span class="ts-tooltip-val">{item.value}</span>
                </div>
              {/each}
            </div>
          </foreignObject>
        {/if}
      </svg>

      <!-- Probe info -->
      <div class="ts-probe-info">
        Az: {probe.azimuth.toFixed(1)} | Rng: {(probe.rangeM / 1000).toFixed(1)} km
      </div>
    {/if}
  </div>
</CollapsiblePanel>

<style>
  .ts-chart {
    width: 100%;
    min-height: 180px;
    max-height: 300px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .ts-empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 8px;
    height: 120px;
    color: var(--text-muted);
    font-size: 11px;
    text-align: center;
  }

  .ts-empty-icon {
    opacity: 0.4;
  }

  .ts-var-selector {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    padding: 0 0 4px 0;
  }

  .ts-var-pill {
    font-size: 10px;
    padding: 2px 8px;
    border-radius: 10px;
    border: 1px solid var(--glass-border);
    background: transparent;
    color: var(--text-secondary);
    cursor: pointer;
    transition: all var(--transition-fast);
    height: auto;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }

  .ts-var-pill:hover {
    border-color: var(--pill-color);
    color: var(--pill-color);
  }

  .ts-var-pill.active {
    border-color: var(--pill-color);
    background: color-mix(in srgb, var(--pill-color) 15%, transparent);
    color: var(--pill-color);
    font-weight: 600;
  }

  .ts-svg {
    width: 100%;
    flex: 1;
    cursor: crosshair;
    user-select: none;
  }

  .ts-svg :global(.ts-grid) {
    stroke: var(--glass-border);
    stroke-dasharray: 3 3;
    stroke-width: 0.5;
  }

  .ts-svg :global(.ts-axis) {
    stroke: var(--border-light, var(--glass-border));
    stroke-width: 1;
  }

  .ts-svg :global(.ts-axis-label) {
    fill: var(--text-muted);
    font-size: 9px;
    font-family: var(--font-mono, monospace);
  }

  .ts-svg :global(.ts-line) {
    transition: opacity var(--transition-fast);
  }

  .ts-svg :global(.ts-dot) {
    transition: r 100ms ease, opacity 100ms ease;
  }

  .ts-svg :global(.ts-crosshair) {
    stroke: var(--text-muted);
    stroke-width: 1;
    stroke-dasharray: 4 4;
    opacity: 0.5;
  }

  .ts-tooltip {
    background: var(--glass-bg-heavy, rgba(8, 10, 20, 0.9));
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
    padding: 6px 8px;
    font-size: 10px;
    color: var(--text-primary);
    pointer-events: none;
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
  }

  .ts-tooltip-title {
    font-weight: 600;
    margin-bottom: 4px;
    color: var(--text-secondary);
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .ts-tooltip-row {
    display: flex;
    align-items: center;
    gap: 4px;
  }

  .ts-tooltip-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .ts-tooltip-var {
    color: var(--text-secondary);
    flex: 1;
  }

  .ts-tooltip-val {
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }

  .ts-probe-info {
    font-size: 10px;
    color: var(--text-muted);
    text-align: center;
    font-variant-numeric: tabular-nums;
    font-family: var(--font-mono, monospace);
  }
</style>
