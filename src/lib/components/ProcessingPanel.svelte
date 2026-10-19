<script lang="ts">
  import { sweepLabel } from '../utils/rhiGeometry';
  import { onMount, onDestroy, untrack } from 'svelte';
  import { processingProgress, radarData, connectionStatus, selectedVariable, selectedSweep } from '../stores/radarData';
  import { wsManager } from '../utils/websocket';
  import { runFeatureRequest, type FeatureRequestHandle } from '../utils/featureRequest';
  import CollapsiblePanel from './CollapsiblePanel.svelte';

  let despeckle = $state(false);
  let velocityDealiasing = $state(false);

  /** Variables the QC steps can be applied to (velocity-only steps need one of these). */
  const VELOCITY_VARS = ['VRADH', 'VRAD', 'V', 'VEL'];

  // Retrievals state
  interface RetrievalDef {
    name: string;
    description: string;
    required_variables: string[];
    params: Record<string, { type: string; default: number; label: string }>;
  }

  let retrievals = $state<RetrievalDef[]>([]);
  let retrievalsFailed = $state(false);
  let selectedRetrieval = $state<string>('');
  let retrievalParams = $state<Record<string, number>>({});
  let retrievalSweep = $state<number>(0);
  let retrievalRunning = $state(false);
  let retrievalResult = $state<{ name: string; sweep: number; units: string; long_name: string; stats: Record<string, number> } | null>(null);

  // Statistics state
  let statVariable = $state<string>('');
  let statSweep = $state<number>(0);
  let statType = $state<string>('histogram');
  let statResult = $state<Record<string, any> | null>(null);
  let statRunning = $state(false);

  // Despeckle / dealiasing run
  let processingRunning = $state(false);
  let processingDone = $state<string | null>(null);

  const progress = $derived($processingProgress);
  const hasData = $derived($radarData.variables.length > 0);
  const isConnected = $derived($connectionStatus === 'connected');
  const canProcess = $derived(hasData && isConnected);
  const variables = $derived($radarData.variables);
  const sweeps = $derived($radarData.sweeps || []);
  const filePath = $derived($radarData.filePath);
  const viewVariable = $derived($selectedVariable);
  const viewSweep = $derived($selectedSweep);
  const anyRunning = $derived(processingRunning || retrievalRunning || statRunning);

  /** Variables present in a sweep (split-cut volumes carry different ones per sweep). */
  function sweepVariables(i: number): string[] {
    return sweeps[i]?.variables ?? variables;
  }

  // Processing steps apply to the displayed variable.
  const dealiasBlocked = $derived(
    velocityDealiasing && !!viewVariable && !VELOCITY_VARS.includes(viewVariable.toUpperCase()),
  );
  const processingHint = $derived(
    !despeckle && !velocityDealiasing
      ? 'Tick at least one step'
      : dealiasBlocked
        ? `Velocity dealiasing needs a velocity field; ${viewVariable} is displayed`
        : '',
  );

  // Retrieval requirements, checked against the chosen sweep.
  const retrievalDef = $derived(retrievals.find((r) => r.name === selectedRetrieval) ?? null);
  const missingRequired = $derived(
    retrievalDef ? retrievalDef.required_variables.filter((v) => !sweepVariables(retrievalSweep).includes(v)) : [],
  );
  /** Nearest sweep that has everything the retrieval needs (null when the file has none). */
  const suggestedSweep = $derived.by(() => {
    if (!retrievalDef || missingRequired.length === 0) return null;
    let best: number | null = null;
    for (let i = 0; i < sweeps.length; i++) {
      if (!retrievalDef.required_variables.every((v) => sweepVariables(i).includes(v))) continue;
      if (best === null || Math.abs(i - retrievalSweep) < Math.abs(best - retrievalSweep)) best = i;
    }
    return best;
  });
  const retrievalBlocked = $derived(missingRequired.length > 0);

  // Statistics: the variable must exist in the chosen sweep.
  const statVariables = $derived(sweepVariables(statSweep));

  let unsubRadar: (() => void) | null = null;
  let unsubConnection: (() => void) | null = null;
  let retrievalsFetched = false;
  let retrievalsTimer: ReturnType<typeof setTimeout> | null = null;
  let procHandle: FeatureRequestHandle | null = null;

  function updateRetrievalParams(name: string) {
    const def = retrievals.find(r => r.name === name);
    if (def) {
      retrievalParams = {};
      for (const [key, spec] of Object.entries(def.params)) {
        retrievalParams[key] = spec.default;
      }
    }
  }

  // A different file: results, sweep choices and variable choices of the old one no longer apply.
  let lastFile: string | null | undefined = undefined;
  $effect(() => {
    const f = filePath;
    untrack(() => {
      if (lastFile !== undefined && f !== lastFile) {
        retrievalResult = null;
        statResult = null;
        processingDone = null;
        retrievalSweep = 0;
        statSweep = 0;
        statVariable = '';
      }
      lastFile = f;
    });
  });

  // Keep the selections valid (variable in the sweep, sweep in the file).
  $effect(() => {
    const vars = statVariables;
    const nSweeps = sweeps.length;
    untrack(() => {
      if (nSweeps > 0) {
        if (retrievalSweep >= nSweeps) retrievalSweep = 0;
        if (statSweep >= nSweeps) statSweep = 0;
      }
      if (vars.length > 0 && !vars.includes(statVariable)) {
        statVariable = viewVariable && vars.includes(viewVariable) ? viewVariable : vars[0];
      }
    });
  });

  onMount(() => {
    unsubRadar = radarData.subscribe(() => {
      tryFetchRetrievals();
    });
    unsubConnection = connectionStatus.subscribe((st) => {
      if (st !== 'connected' && retrievals.length === 0) retrievalsFetched = false;
      tryFetchRetrievals();
    });
  });

  onDestroy(() => {
    unsubRadar?.();
    unsubConnection?.();
    if (retrievalsTimer) clearTimeout(retrievalsTimer);
  });

  function tryFetchRetrievals() {
    const rd = $radarData;
    const conn = $connectionStatus;
    if (rd.variables.length > 0 && conn === 'connected' && !retrievalsFetched) {
      retrievalsFetched = true;
      fetchRetrievals();
    }
  }

  function fetchRetrievals() {
    retrievalsFailed = false;
    const unsubscribe = wsManager.onMessage('retrievals_list', (msg: any) => {
      retrievals = msg.retrievals;
      retrievalsFailed = false;
      if (retrievals.length > 0 && !selectedRetrieval) {
        selectedRetrieval = retrievals[0].name;
        updateRetrievalParams(selectedRetrieval);
      }
      if (retrievalsTimer) clearTimeout(retrievalsTimer);
      unsubscribe();
    });
    wsManager.send({ type: 'list_retrievals' });
    if (retrievalsTimer) clearTimeout(retrievalsTimer);
    retrievalsTimer = setTimeout(() => {
      if (retrievals.length === 0) {
        unsubscribe();
        retrievalsFailed = true;
        retrievalsFetched = false;
      }
    }, 8000);
  }

  /**
   * Despeckle / dealiasing act on the displayed variable and sweep, through the same quality-control
   * engine as the Quality Control panel (the server's generic `process` pipeline is a stub that
   * leaves the data unchanged).
   */
  function applyProcessing() {
    if (!canProcess || processingRunning || processingHint || !viewVariable) return;
    const variable = viewVariable;
    const sweep = viewSweep;
    const steps: { name: string; params: Record<string, any> }[] = [];
    if (despeckle) steps.push({ name: 'despeckle', params: { window_size: 3, threshold: 0.5 } });
    if (velocityDealiasing) steps.push({ name: 'velocity_dealiasing', params: { nyquist_vel: 25 } });
    const names = steps.map((s) => s.name.replace(/_/g, ' ')).join(' + ');

    processingRunning = true;
    processingDone = null;
    procHandle = runFeatureRequest({
      kind: 'qc',
      label: `Processing ${variable} sweep ${sweep}: ${names}`,
      message: { type: 'run_qc', steps, variable, sweep },
      resultTypes: ['qc_data_ready'],
      accept: (e: any) => e.variable === variable && e.sweep === sweep,
      onResult: () => { processingDone = `Applied ${names} to ${variable}, sweep ${sweep}`; },
      onSettled: () => { processingRunning = false; procHandle = null; },
      retry: applyProcessing,
      timeoutMs: 120000,
    });
  }

  function runRetrieval() {
    if (!canProcess || retrievalRunning || !selectedRetrieval || retrievalBlocked) return;
    retrievalRunning = true;
    retrievalResult = null;

    const currentRetrieval = selectedRetrieval;
    const sweep = retrievalSweep;
    runFeatureRequest({
      kind: 'retrieval',
      label: `Retrieval: ${retrievalDef?.description ?? currentRetrieval}`,
      message: {
        type: 'run_retrieval',
        name: currentRetrieval,
        sweep,
        params: $state.snapshot(retrievalParams),
      },
      resultTypes: ['retrieval_result'],
      accept: (msg: any) => msg.name === currentRetrieval && msg.sweep === sweep,
      onResult: (msg: any) => {
        retrievalResult = {
          name: msg.name,
          sweep: msg.sweep,
          units: msg.units,
          long_name: msg.long_name,
          stats: msg.stats,
        };
      },
      onSettled: () => { retrievalRunning = false; },
      retry: runRetrieval,
    });
  }

  function runStatistics() {
    if (!canProcess || statRunning || !statVariable) return;
    statRunning = true;
    statResult = null;

    const variable = statVariable;
    const sweep = statSweep;
    const type = statType;
    runFeatureRequest({
      kind: 'stats',
      label: `Statistics: ${variable} sweep ${sweep}`,
      message: { type: 'get_statistics', variable, sweep, stat_type: type },
      resultTypes: ['statistics_result'],
      onResult: (msg: any) => { statResult = { ...msg, _type: type, _sweep: sweep }; },
      onSettled: () => { statRunning = false; },
      retry: runStatistics,
    });
  }

  function fmtNum(v: any, digits = 2): string {
    return typeof v === 'number' && Number.isFinite(v) ? v.toFixed(digits) : 'N/A';
  }
</script>

<CollapsiblePanel title="Processing">
  {#if hasData}
    <div class="controls">
      <label class="checkbox-label" title="Remove isolated noisy pixels from the displayed sweep">
        <input type="checkbox" bind:checked={despeckle} disabled={!canProcess || processingRunning} />
        <span>Despeckle</span>
      </label>

      <label class="checkbox-label" title="Correct aliased velocity values of the displayed sweep">
        <input type="checkbox" bind:checked={velocityDealiasing} disabled={!canProcess || processingRunning} />
        <span>Velocity Dealiasing</span>
      </label>

      <div class="field">
        <label for="gridding-method">Gridding Method</label>
        <select id="gridding-method" disabled title="Gridding to a Cartesian grid is not available yet (see the 3D Volume and Mosaic panels)">
          <option value="none">None</option>
        </select>
      </div>

      <button
        class="primary action-btn"
        on:click={applyProcessing}
        disabled={!canProcess || processingRunning || !!processingHint}
        title={!isConnected ? 'Sidecar not connected' : !hasData ? 'No data loaded' : processingHint || `Run on ${viewVariable}, sweep ${viewSweep}`}
      >
        {#if processingRunning}
          <span class="spinner"></span>
          Processing...
        {:else}
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="5 3 19 12 5 21 5 3"/></svg>
          Apply Processing
        {/if}
      </button>
      {#if processingHint && (despeckle || velocityDealiasing)}
        <p class="hint" role="status">{processingHint}</p>
      {/if}
      {#if processingDone && !processingRunning}
        <p class="hint ok" role="status">{processingDone}</p>
      {/if}
    </div>

    {#if processingRunning && progress}
      <div class="progress-section">
        <div class="progress-label">
          <span>{progress.message}</span>
          <span class="progress-pct">{progress.percent}%</span>
        </div>
        <div class="progress-bar">
          <div class="progress-bar-fill" style="width: {progress.percent}%"></div>
        </div>
      </div>
    {/if}
  {:else}
    <p class="no-data-msg">Load a file to enable processing</p>
  {/if}
</CollapsiblePanel>

<CollapsiblePanel title="Retrievals">
  {#if hasData && retrievals.length > 0}
    <div class="controls">
      <div class="field">
        <label for="retrieval-select">Algorithm</label>
        <select id="retrieval-select" bind:value={selectedRetrieval} disabled={!canProcess || retrievalRunning} on:change={() => { updateRetrievalParams(selectedRetrieval); retrievalResult = null; }}>
          {#each retrievals as ret}
            <option value={ret.name}>{ret.description}</option>
          {/each}
        </select>
      </div>

      {#if retrievalDef}
        {#if retrievalDef.required_variables.length > 0}
          <div class="req-vars">
            <span class="req-label">Requires:</span>
            {#each retrievalDef.required_variables as v}
              {@const have = sweepVariables(retrievalSweep).includes(v)}
              <span class="var-tag" class:available={have} class:missing={!have}>{v}</span>
            {/each}
          </div>
        {/if}

        {#each Object.entries(retrievalDef.params) as [key, spec]}
          <div class="field param-field">
            <label for="param-{key}">{spec.label}</label>
            <input
              id="param-{key}"
              type="number"
              step="any"
              bind:value={retrievalParams[key]}
              disabled={!canProcess || retrievalRunning}
            />
          </div>
        {/each}
      {/if}

      <div class="field">
        <label for="retrieval-sweep">Sweep</label>
        <select id="retrieval-sweep" bind:value={retrievalSweep} disabled={!canProcess || retrievalRunning} on:change={() => (retrievalResult = null)}>
          {#each sweeps as s, i}
            <option value={i}>{s.kind === 'rhi' ? sweepLabel(s) : `Sweep ${i}${s.elevation != null ? ` (${s.elevation.toFixed(1)}\u00B0)` : ''}`}</option>
          {/each}
        </select>
      </div>

      {#if retrievalBlocked}
        <p class="hint" role="status">
          {missingRequired.join(', ')} {missingRequired.length > 1 ? 'are' : 'is'} not in sweep {retrievalSweep}{missingRequired.some((v) => !variables.includes(v)) ? ' or this file' : ''}.
          {#if suggestedSweep !== null}
            <button class="link-btn" on:click={() => (retrievalSweep = suggestedSweep ?? 0)}>Use sweep {suggestedSweep}</button>
          {/if}
        </p>
      {/if}

      <button
        class="primary action-btn"
        on:click={runRetrieval}
        disabled={!canProcess || retrievalRunning || retrievalBlocked}
        title={retrievalBlocked ? `Missing ${missingRequired.join(', ')}` : 'Run the retrieval on the chosen sweep'}
      >
        {#if retrievalRunning}
          <span class="spinner"></span>
          Running...
        {:else}
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="5 3 19 12 5 21 5 3"/></svg>
          Run Retrieval
        {/if}
      </button>

      {#if retrievalResult}
        <div class="result-card">
          <div class="result-title">{retrievalResult.long_name} (sweep {retrievalResult.sweep})</div>
          <div class="result-units">{retrievalResult.units}</div>
          {#if retrievalResult.stats && Object.keys(retrievalResult.stats).length > 0}
            <div class="result-stats">
              {#if retrievalResult.stats.min != null}<span>Min: {retrievalResult.stats.min.toFixed(2)}</span>{/if}
              {#if retrievalResult.stats.max != null}<span>Max: {retrievalResult.stats.max.toFixed(2)}</span>{/if}
              {#if retrievalResult.stats.mean != null}<span>Mean: {retrievalResult.stats.mean.toFixed(2)}</span>{/if}
            </div>
          {:else}
            <div class="result-stats"><span>No valid values</span></div>
          {/if}
        </div>
      {/if}
    </div>
  {:else if hasData && retrievalsFailed}
    <p class="no-data-msg">
      Could not load the retrievals.
      <button class="link-btn" on:click={tryFetchRetrievals}>Retry</button>
    </p>
  {:else if hasData}
    <p class="no-data-msg">Loading retrievals...</p>
  {:else}
    <p class="no-data-msg">Load a file to enable retrievals</p>
  {/if}
</CollapsiblePanel>

<CollapsiblePanel title="Statistics">
  {#if hasData}
    <div class="controls">
      <div class="field">
        <label for="stat-sweep">Sweep</label>
        <select id="stat-sweep" bind:value={statSweep} disabled={!canProcess || statRunning} on:change={() => (statResult = null)}>
          {#each sweeps as s, i}
            <option value={i}>{s.kind === 'rhi' ? sweepLabel(s) : `Sweep ${i}${s.elevation != null ? ` (${s.elevation.toFixed(1)}\u00B0)` : ''}`}</option>
          {/each}
        </select>
      </div>

      <div class="field">
        <label for="stat-variable">Variable</label>
        <select id="stat-variable" bind:value={statVariable} disabled={!canProcess || statRunning} on:change={() => (statResult = null)}>
          {#each statVariables as v}
            <option value={v}>{v}</option>
          {/each}
        </select>
      </div>

      <div class="field">
        <label for="stat-type">Statistic</label>
        <select id="stat-type" bind:value={statType} disabled={!canProcess || statRunning} on:change={() => (statResult = null)}>
          <option value="histogram">Histogram</option>
          <option value="profile">Vertical Profile</option>
          <option value="cell_stats">Cell Statistics</option>
          <option value="qc">QC Metrics</option>
        </select>
      </div>

      <button
        class="primary action-btn"
        on:click={runStatistics}
        disabled={!canProcess || statRunning || !statVariable}
      >
        {#if statRunning}
          <span class="spinner"></span>
          Computing...
        {:else}
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
            <rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/>
            <rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/>
          </svg>
          Compute
        {/if}
      </button>

      {#if statResult}
        <div class="result-card">
          {#if statResult._type === 'histogram' && statResult.stats}
            <div class="result-title">Histogram: {statResult.variable} (sweep {statResult._sweep})</div>
            <div class="result-stats">
              <span>Mean: {fmtNum(statResult.stats.mean)}</span>
              <span>Std: {fmtNum(statResult.stats.std)}</span>
              <span>Min: {fmtNum(statResult.stats.min)}</span>
              <span>Max: {fmtNum(statResult.stats.max)}</span>
              <span>Valid: {statResult.stats.count_valid?.toLocaleString() ?? 'N/A'}</span>
            </div>
          {:else if statResult._type === 'cell_stats'}
            <div class="result-title">Cell Statistics</div>
            <div class="result-stats">
              {#if statResult.max_reflectivity != null}<span>Max Z: {statResult.max_reflectivity.toFixed(1)} dBZ</span>{/if}
              {#if statResult.vil_estimate != null}<span>VIL: {statResult.vil_estimate} kg/m2</span>{/if}
              {#if statResult.echo_top_km != null}<span>Echo Top: {statResult.echo_top_km} km</span>{/if}
              <span>Sweeps: {statResult.n_sweeps}</span>
            </div>
          {:else if statResult._type === 'qc'}
            <div class="result-title">QC: {statResult.variable} (sweep {statResult._sweep})</div>
            <div class="result-stats">
              <span>Total gates: {statResult.total_gates?.toLocaleString()}</span>
              <span>Missing: {statResult.pct_missing}%</span>
              {#if statResult.pct_below_noise > 0}<span>Below noise: {statResult.pct_below_noise}%</span>{/if}
            </div>
          {:else if statResult._type === 'profile'}
            <div class="result-title">Vertical Profile: {statResult.variable}</div>
            <div class="result-stats">
              {#each statResult.elevations as elev, i}
                <span>{elev.toFixed(1)}&deg;: {fmtNum(statResult.means[i])}</span>
              {/each}
            </div>
          {:else}
            <div class="result-stats"><span>No data</span></div>
          {/if}
        </div>
      {/if}
    </div>
  {:else}
    <p class="no-data-msg">Load a file to enable statistics</p>
  {/if}
</CollapsiblePanel>

<style>
  .controls {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md);
  }

  .checkbox-label {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    font-size: 12px;
    color: var(--text-primary);
    cursor: pointer;
    transition: opacity var(--transition-fast);
  }

  .checkbox-label:has(input:disabled) {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .checkbox-label input {
    margin: 0;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .field label {
    font-size: 11px;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }

  .field select,
  .field input[type="number"] {
    width: 100%;
  }

  .param-field input {
    font-family: var(--font-mono);
    font-size: 12px;
    padding: 4px 6px;
  }

  .action-btn {
    margin-top: var(--spacing-xs);
    width: 100%;
  }

  .spinner {
    display: inline-block;
    width: 12px;
    height: 12px;
    border: 2px solid rgba(255, 255, 255, 0.3);
    border-top-color: #fff;
    border-radius: 50%;
    animation: spin 600ms linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  .progress-section {
    margin-top: var(--spacing-md);
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .progress-label {
    display: flex;
    justify-content: space-between;
    font-size: 11px;
    color: var(--text-secondary);
  }

  .progress-pct {
    font-family: var(--font-mono);
    color: var(--text-accent);
  }

  .hint {
    margin: 0;
    font-size: 11px;
    line-height: 1.4;
    color: var(--text-secondary);
  }

  .hint.ok {
    color: var(--accent-success, #4caf50);
  }

  .link-btn {
    display: inline;
    padding: 0 4px;
    height: auto;
    font-size: inherit;
    color: var(--accent-primary, #5b6cf7);
    background: transparent;
    border: none;
    text-decoration: underline;
    cursor: pointer;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }

  .no-data-msg {
    font-size: 12px;
    color: var(--text-muted);
    text-align: center;
    padding: var(--spacing-md) 0;
  }

  .req-vars {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    flex-wrap: wrap;
    font-size: 11px;
  }

  .req-label {
    color: var(--text-secondary);
  }

  .var-tag {
    padding: 1px 6px;
    border-radius: 3px;
    font-family: var(--font-mono);
    font-size: 10px;
  }

  .var-tag.available {
    background: rgba(76, 175, 80, 0.2);
    color: var(--accent-success, #4caf50);
  }

  .var-tag.missing {
    background: rgba(244, 67, 54, 0.2);
    color: var(--accent-danger, #f44336);
  }

  .result-card {
    margin-top: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-surface, rgba(255, 255, 255, 0.05));
    border-radius: 6px;
    border: 1px solid var(--border-subtle, rgba(255, 255, 255, 0.08));
  }

  .result-title {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-primary);
    margin-bottom: 4px;
  }

  .result-units {
    font-size: 10px;
    color: var(--text-secondary);
    font-family: var(--font-mono);
    margin-bottom: 4px;
  }

  .result-stats {
    display: flex;
    flex-direction: column;
    gap: 2px;
    font-size: 11px;
    font-family: var(--font-mono);
    color: var(--text-secondary);
  }

  .result-stats span {
    display: flex;
    justify-content: space-between;
  }
</style>
