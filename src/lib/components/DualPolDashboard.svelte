<script lang="ts">
  import { onMount, onDestroy, untrack } from 'svelte';
  import ScatterPlot from './ScatterPlot.svelte';
  import HistogramChart from './HistogramChart.svelte';
  import { runFeatureRequest, type FeatureRequestHandle } from '../utils/featureRequest';
  import { radarData, selectedSweep, connectionStatus } from '../stores/radarData';

  // ── State ──────────────────────────────────────────────────────────────

  // Variable pair selection
  const COMMON_PAIRS: [string, string][] = [
    ['DBZH', 'ZDR'],
    ['DBZH', 'KDP'],
    ['ZDR', 'KDP'],
    ['DBZH', 'RHOHV'],
  ];
  let selectedPairIdx = $state(0);
  let histVar = $state('DBZH');
  let colorVar: string | null = $state(null);

  // HID classes and colors
  const HID_CLASSES = [
    { name: 'Drizzle', color: '#22d3ee', id: 0 },
    { name: 'Rain', color: '#3b82f6', id: 1 },
    { name: 'Wet Snow', color: '#a78bfa', id: 2 },
    { name: 'Dry Snow', color: '#e2e8f0', id: 3 },
    { name: 'Graupel', color: '#f59e0b', id: 4 },
    { name: 'Hail', color: '#ef4444', id: 5 },
  ];

  // Region stats
  let regionStats: Record<string, any> | null = $state(null);
  let regionLabel = $state('');
  let regionLoading = $state(false);
  let regionHandle: FeatureRequestHandle | null = null;

  // Histogram controls
  let binCount = $state(50);
  let showGaussian = $state(true);
  let densityMode = $state(false);

  // Layout: the plots fill one grid cell each (all four cells are the same size)
  let containerEl: HTMLDivElement | undefined = $state(undefined);
  let bodyW = $state(400);
  let bodyH = $state(300);

  // ── Derived ────────────────────────────────────────────────────────────
  const variables = $derived($radarData.variables);
  const sweeps = $derived($radarData.sweeps ?? []);
  const filePath = $derived($radarData.filePath);
  const viewSweep = $derived($selectedSweep);
  const isConnected = $derived($connectionStatus === 'connected');
  const plotW = $derived(Math.max(120, bodyW - 2));
  const plotH = $derived(Math.max(120, bodyH - 2));

  /** Pairs whose both variables are in the file (anything else could only fail). */
  const availablePairs = $derived.by((): [string, string][] => {
    const ok = COMMON_PAIRS.filter(([a, b]) => variables.includes(a) && variables.includes(b));
    if (ok.length > 0) return ok;
    return variables.length >= 2 ? [[variables[0], variables[1]]] : [];
  });
  const pair = $derived(availablePairs[Math.min(selectedPairIdx, Math.max(availablePairs.length - 1, 0))] ?? null);
  const scatterVar1 = $derived(pair?.[0] ?? '');
  const scatterVar2 = $derived(pair?.[1] ?? '');

  function sweepVars(i: number): string[] {
    return sweeps[i]?.variables ?? variables;
  }

  /**
   * The sweep to plot for these variables: the one in the viewer when it has them, otherwise the
   * nearest that does (split-cut volumes measure dual-pol fields only in the surveillance cuts).
   * null when no sweep has them all.
   */
  function sweepWith(vars: (string | null)[]): number | null {
    const need = vars.filter((v): v is string => !!v);
    if (need.length === 0 || sweeps.length === 0) return viewSweep;
    let best: number | null = null;
    for (let i = 0; i < sweeps.length; i++) {
      if (!need.every((v) => sweepVars(i).includes(v))) continue;
      if (best === null || Math.abs(i - viewSweep) < Math.abs(best - viewSweep)) best = i;
    }
    return best;
  }

  // The colour variable only counts where it exists together with the pair.
  const effColorVar = $derived(colorVar && sweepWith([scatterVar1, scatterVar2, colorVar]) !== null ? colorVar : null);
  const scatterSweep = $derived(sweepWith([scatterVar1, scatterVar2, effColorVar]));
  const histSweep = $derived(sweepWith([histVar]));

  function sweepNote(effective: number | null): string {
    if (effective === null || effective === viewSweep) return '';
    const el = sweeps[effective]?.elevation;
    return `sweep ${effective}${el != null ? ` (${el.toFixed(1)}\u00B0)` : ''}`;
  }

  // ── Effects ────────────────────────────────────────────────────────────
  // Keep the selections valid for the open file.
  $effect(() => {
    const vars = variables;
    untrack(() => {
      if (vars.length === 0) return;
      if (!vars.includes(histVar)) histVar = vars.includes('DBZH') ? 'DBZH' : vars[0];
      if (colorVar && !vars.includes(colorVar)) colorVar = null;
      if (selectedPairIdx >= availablePairs.length) selectedPairIdx = 0;
    });
  });

  // Region stats describe one file / sweep / variable; show nothing stale.
  $effect(() => {
    void [filePath, histVar, histSweep];
    untrack(() => {
      regionHandle?.cancel();
      regionStats = null;
      regionLoading = false;
    });
  });

  // ── Lifecycle ──────────────────────────────────────────────────────────
  onDestroy(() => {
    regionHandle?.cancel();
  });

  // ── Handlers ───────────────────────────────────────────────────────────
  function onPairChange(e: Event) {
    selectedPairIdx = parseInt((e.target as HTMLSelectElement).value, 10);
  }

  function onHistVarChange(e: Event) {
    histVar = (e.target as HTMLSelectElement).value;
  }

  function onColorVarChange(e: Event) {
    const v = (e.target as HTMLSelectElement).value;
    colorVar = v === '' ? null : v;
  }

  function onBinChange(e: Event) {
    binCount = parseInt((e.target as HTMLInputElement).value, 10);
  }

  function requestRegionStats(label: string, azMin: number, azMax: number, rangeMin: number, rangeMax: number) {
    if (histSweep === null) return;
    regionHandle?.cancel();
    regionLoading = true;
    regionStats = null;
    const variable = histVar;
    const sweep = histSweep;
    const h = runFeatureRequest({
      kind: 'dualpol',
      label: `Region stats: ${variable} ${label}`,
      message: {
        type: 'get_region_stats',
        variable,
        sweep,
        az_min: azMin,
        az_max: azMax,
        range_min: rangeMin,
        range_max: rangeMax,
      },
      resultTypes: ['region_stats_result'],
      accept: (m: any) => m.variable === variable && (m.sweep === undefined || m.sweep === sweep),
      onResult: (m: any) => {
        regionStats = m;
        regionLabel = label;
      },
      onSettled: () => {
        if (regionHandle !== h) return;
        regionLoading = false;
        regionHandle = null;
      },
      retry: () => requestRegionStats(label, azMin, azMax, rangeMin, rangeMax),
    });
    regionHandle = h;
  }

  function onScatterSelection(e: CustomEvent) {
    // Lasso selection from scatter plot - could be used to highlight on PPI
    const indices = e.detail.indices;
    if (indices && indices.length > 0) {
      // Could dispatch to PPI in a future integration
      console.log(`[DualPol] Lasso selected ${indices.length} points`);
    }
  }

  function onHistRangeSelect(e: CustomEvent) {
    const { min, max, variable: v } = e.detail;
    if (min !== null && max !== null) {
      console.log(`[DualPol] Histogram range: ${v} [${min.toFixed(2)}, ${max.toFixed(2)}]`);
    }
  }

  function fmtStat(v: number | undefined): string {
    if (v === undefined || v === null) return '---';
    return typeof v === 'number' ? v.toFixed(2) : String(v);
  }

  const REGIONS: { label: string; az: [number, number]; rng: [number, number] }[] = [
    { label: 'NE Quad', az: [0, 90], rng: [0, 150000] },
    { label: 'SE Quad', az: [90, 180], rng: [0, 150000] },
    { label: 'SW Quad', az: [180, 270], rng: [0, 150000] },
    { label: 'NW Quad', az: [270, 360], rng: [0, 150000] },
    { label: 'Near (0-50km)', az: [0, 360], rng: [0, 50000] },
    { label: 'Far (50-150km)', az: [0, 360], rng: [50000, 150000] },
  ];
</script>

<div class="dualpol-dashboard" bind:this={containerEl}>
  <!-- Controls bar -->
  <div class="dp-controls">
    <div class="dp-ctrl-group">
      <label for="dp-pair">Pair</label>
      <select id="dp-pair" value={selectedPairIdx} on:change={onPairChange} disabled={!isConnected || availablePairs.length === 0}>
        {#each availablePairs as pr, i}
          <option value={i}>{pr[0]} / {pr[1]}</option>
        {/each}
      </select>
    </div>

    <div class="dp-ctrl-group">
      <label for="dp-histvar">Histogram</label>
      <select id="dp-histvar" value={histVar} on:change={onHistVarChange} disabled={!isConnected}>
        {#each variables as v}
          <option value={v}>{v}</option>
        {/each}
      </select>
    </div>

    <div class="dp-ctrl-group">
      <label for="dp-colorvar">Color by</label>
      <select id="dp-colorvar" value={colorVar ?? ''} on:change={onColorVarChange} disabled={!isConnected}>
        <option value="">None</option>
        {#each variables as v}
          <option value={v}>{v}</option>
        {/each}
      </select>
    </div>

    <div class="dp-ctrl-group">
      <label for="dp-bins">Bins ({binCount})</label>
      <input id="dp-bins" type="range" min="10" max="200" step="5" value={binCount} on:input={onBinChange} />
    </div>

    <div class="dp-ctrl-group">
      <label class="dp-toggle">
        <input type="checkbox" bind:checked={showGaussian} />
        Gaussian
      </label>
      <label class="dp-toggle">
        <input type="checkbox" bind:checked={densityMode} />
        Density
      </label>
    </div>
  </div>

  <!-- 2x2 grid -->
  <div class="dp-grid">
    <!-- Top left: HID Legend -->
    <div class="dp-cell">
      <div class="dp-cell-header">Hydrometeor ID Legend</div>
      <div class="hid-legend">
        {#each HID_CLASSES as cls}
          <div class="hid-item">
            <span class="hid-swatch" style="background:{cls.color}"></span>
            <span class="hid-name">{cls.name}</span>
          </div>
        {/each}
      </div>

      <!-- Quick region selector -->
      <div class="region-selector">
        <div class="dp-cell-header">Quick Region Query: {histVar}{histSweep !== null ? `, sweep ${histSweep}` : ''}</div>
        <div class="region-form">
          {#each REGIONS as r}
            <button
              class="dp-btn"
              class:active={regionLabel === r.label && !!regionStats}
              on:click={() => requestRegionStats(r.label, r.az[0], r.az[1], r.rng[0], r.rng[1])}
              disabled={!isConnected || regionLoading || histSweep === null}
            >{r.label}</button>
          {/each}
        </div>
      </div>

      <!-- Region stats (if available) -->
      <div class="region-stats-box" aria-live="polite">
        {#if regionLoading}
          <div class="rs-empty">Computing...</div>
        {:else if regionStats}
          <div class="dp-cell-header">Region Stats: {regionLabel} ({regionStats.variable}{regionStats.units ? `, ${regionStats.units}` : ''})</div>
          {#if regionStats.stats && Object.keys(regionStats.stats).length > 0}
            <div class="rs-grid">
              <span class="rs-label">Mean</span><span class="rs-value">{fmtStat(regionStats.stats.mean)}</span>
              <span class="rs-label">Std</span><span class="rs-value">{fmtStat(regionStats.stats.std)}</span>
              <span class="rs-label">Min</span><span class="rs-value">{fmtStat(regionStats.stats.min)}</span>
              <span class="rs-label">Max</span><span class="rs-value">{fmtStat(regionStats.stats.max)}</span>
              <span class="rs-label">Count</span><span class="rs-value">{regionStats.stats.count ?? regionStats.n_valid}</span>
            </div>
          {:else}
            <div class="rs-empty">No data in selected region</div>
          {/if}
        {:else if histSweep === null}
          <div class="rs-empty">{histVar} is not measured in any sweep</div>
        {/if}
      </div>
    </div>

    <!-- Top right: Scatter plot -->
    <div class="dp-cell">
      <div class="dp-cell-header">
        Scatter: {scatterVar1} vs {scatterVar2}
        {#if sweepNote(scatterSweep)}<span class="dp-sweep-note" title="Not measured in the displayed sweep">{sweepNote(scatterSweep)}</span>{/if}
      </div>
      <div class="dp-cell-body" bind:clientWidth={bodyW} bind:clientHeight={bodyH}>
        {#if pair && scatterSweep !== null}
          <ScatterPlot
            var1={scatterVar1}
            var2={scatterVar2}
            colorVar={effColorVar}
            {densityMode}
            sweepOverride={scatterSweep}
            width={plotW}
            height={plotH}
            on:selection={onScatterSelection}
          />
        {:else}
          <div class="dp-empty">{pair ? `${scatterVar1} and ${scatterVar2} are not measured together in any sweep` : 'This file has fewer than two variables'}</div>
        {/if}
      </div>
    </div>

    <!-- Bottom left: Histogram -->
    <div class="dp-cell">
      <div class="dp-cell-header">
        Distribution: {histVar}
        {#if sweepNote(histSweep)}<span class="dp-sweep-note" title="Not measured in the displayed sweep">{sweepNote(histSweep)}</span>{/if}
      </div>
      <div class="dp-cell-body">
        {#if histSweep !== null}
          <HistogramChart
            variable={histVar}
            bins={binCount}
            {showGaussian}
            sweepOverride={histSweep}
            width={plotW}
            height={plotH}
            on:rangeSelect={onHistRangeSelect}
          />
        {:else}
          <div class="dp-empty">{histVar} is not measured in any sweep</div>
        {/if}
      </div>
    </div>

    <!-- Bottom right: Scatter (second pair or rotated view) -->
    <div class="dp-cell">
      <div class="dp-cell-header">
        Scatter: {scatterVar2} vs {scatterVar1}
        {#if sweepNote(scatterSweep)}<span class="dp-sweep-note" title="Not measured in the displayed sweep">{sweepNote(scatterSweep)}</span>{/if}
      </div>
      <div class="dp-cell-body">
        {#if pair && scatterSweep !== null}
          <ScatterPlot
            var1={scatterVar2}
            var2={scatterVar1}
            colorVar={effColorVar}
            {densityMode}
            sweepOverride={scatterSweep}
            width={plotW}
            height={plotH}
            on:selection={onScatterSelection}
          />
        {:else}
          <div class="dp-empty">{pair ? `${scatterVar1} and ${scatterVar2} are not measured together in any sweep` : 'This file has fewer than two variables'}</div>
        {/if}
      </div>
    </div>
  </div>
</div>

<style>
  .dualpol-dashboard {
    display: flex;
    flex-direction: column;
    height: 100%;
    background: var(--bg-primary, #0f0f1a);
    overflow: hidden;
  }

  .dp-controls {
    display: flex;
    align-items: center;
    gap: var(--spacing-lg, 16px);
    padding: var(--spacing-sm, 6px) var(--spacing-md, 12px);
    background: var(--bg-secondary, #1a1a2e);
    border-bottom: 1px solid var(--border-color, #2a2a3e);
    flex-shrink: 0;
    flex-wrap: wrap;
  }

  .dp-ctrl-group {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm, 6px);
  }

  .dp-ctrl-group label {
    font-size: 10px;
    color: var(--text-muted, #6a6a80);
    text-transform: uppercase;
    letter-spacing: 0.03em;
    font-weight: 600;
    white-space: nowrap;
  }

  .dp-ctrl-group select {
    min-width: 90px;
    font-size: 11px;
  }

  .dp-ctrl-group input[type="range"] {
    width: 80px;
    accent-color: var(--accent-primary, var(--c-accent));
  }

  .dp-toggle {
    display: flex;
    align-items: center;
    gap: 4px;
    cursor: pointer;
    font-size: 10px !important;
  }

  .dp-toggle input[type="checkbox"] {
    accent-color: var(--accent-primary, var(--c-accent));
  }

  .dp-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    grid-template-rows: 1fr 1fr;
    gap: 8px;
    padding: 8px;
    flex: 1;
    overflow: hidden;
  }

  .dp-cell {
    display: flex;
    flex-direction: column;
    background: var(--bg-secondary, #1a1a2e);
    border: 1px solid var(--border-color, #2a2a3e);
    border-radius: var(--radius-md, 6px);
    overflow: hidden;
  }

  .dp-cell-body {
    flex: 1;
    min-height: 0;
    min-width: 0;
    overflow: hidden;
  }

  .dp-empty {
    height: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0 16px;
    text-align: center;
    font-size: 11px;
    color: var(--text-muted, #6a6a80);
  }

  .dp-sweep-note {
    margin-left: 6px;
    padding: 0 5px;
    border-radius: 3px;
    font-weight: 500;
    text-transform: none;
    letter-spacing: 0;
    color: var(--accent-hover, #7b8aff);
    background: rgba(var(--c-accent-rgb), 0.15);
  }

  .dp-cell-header {
    padding: 4px 8px;
    font-size: 10px;
    font-weight: 600;
    color: var(--text-secondary, #a0a0b0);
    background: var(--bg-primary, #0f0f1a);
    border-bottom: 1px solid var(--border-color, #2a2a3e);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    flex-shrink: 0;
  }

  /* HID Legend */
  .hid-legend {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    padding: 8px;
  }

  .hid-item {
    display: flex;
    align-items: center;
    gap: 4px;
  }

  .hid-swatch {
    width: 12px;
    height: 12px;
    border-radius: 2px;
    border: 1px solid rgba(var(--c-line-rgb), 0.1);
  }

  .hid-name {
    font-size: 10px;
    color: var(--text-primary, #e0e0f0);
    font-family: var(--font-mono, monospace);
  }

  /* Region stats */
  .region-stats-box {
    border-top: 1px solid var(--border-color, #2a2a3e);
  }

  .rs-grid {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 2px 8px;
    padding: 4px 8px;
    font-size: 10px;
    font-family: var(--font-mono, monospace);
  }

  .rs-label {
    color: var(--text-muted, #6a6a80);
  }

  .rs-value {
    color: var(--text-primary, #e0e0f0);
  }

  .rs-empty {
    padding: 4px 8px;
    font-size: 10px;
    color: var(--text-muted, #6a6a80);
    font-style: italic;
  }

  /* Region selector */
  .region-selector {
    border-top: 1px solid var(--border-color, #2a2a3e);
    flex: 1;
  }

  .region-form {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    padding: 6px 8px;
  }

  .dp-btn {
    padding: 3px 8px;
    font-size: 9px;
    font-family: var(--font-mono, monospace);
    background: var(--bg-primary, #0f0f1a);
    color: var(--text-secondary, #a0a0b0);
    border: 1px solid var(--border-color, #2a2a3e);
    border-radius: var(--radius-sm, 3px);
    cursor: pointer;
    transition: background 0.15s, color 0.15s;
  }

  .dp-btn:hover:not(:disabled) {
    background: var(--accent-primary, var(--c-accent));
    color: var(--text-primary, #e0e0f0);
    border-color: var(--accent-primary, var(--c-accent));
  }

  .dp-btn.active {
    border-color: var(--accent-primary, var(--c-accent));
    color: var(--accent-hover, #7b8aff);
  }

  .dp-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
</style>
