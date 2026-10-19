<script lang="ts">
  import { onMount, onDestroy, untrack } from 'svelte';
  import { radarData, connectionStatus, selectedVariable, selectedSweep, processingProgress } from '../stores/radarData';
  import { wsManager } from '../utils/websocket';
  import { runFeatureRequest, type FeatureRequestHandle } from '../utils/featureRequest';
  import CollapsiblePanel from './CollapsiblePanel.svelte';

  // --- Types ---
  interface QCParamSpec {
    type: string;
    default: any;
    label: string;
    min?: number;
    max?: number;
    options?: string[];
  }

  interface QCAlgorithm {
    name: string;
    description: string;
    params: Record<string, QCParamSpec>;
    applicable_variables: string[];
  }

  interface PipelineStep {
    name: string;
    params: Record<string, any>;
    expanded: boolean;
  }

  interface PipelinePreset {
    name: string;
    steps: { name: string; params: Record<string, any> }[];
  }

  // --- State ---
  let algorithms: QCAlgorithm[] = $state([]);
  let pipelineSteps: PipelineStep[] = $state([]);
  let isRunning = $state(false);
  /** Targets (file | variable | sweep) QC has been applied to, with the steps that were applied. */
  let qcApplied: Record<string, string[]> = $state({});
  let presets: PipelinePreset[] = $state([]);
  let presetName = $state('');
  let dragIndex: number | null = $state(null);
  let dropIndex: number | null = $state(null);
  let algorithmsFailed = $state(false);
  let runHandle: FeatureRequestHandle | null = null;

  const progress = $derived($processingProgress);
  const hasData = $derived($radarData.variables.length > 0);
  const isConnected = $derived($connectionStatus === 'connected');
  const variables = $derived($radarData.variables);
  const sweeps = $derived($radarData.sweeps || []);
  const currentVariable = $derived($selectedVariable);
  const currentSweep = $derived($selectedSweep);
  const filePath = $derived($radarData.filePath);

  // QC changes the cached sweep itself, so "applied" belongs to one file/variable/sweep: changing
  // any of them must not keep claiming that the sweep on screen is cleaned.
  const currentKey = $derived(`${filePath ?? ''}|${currentVariable ?? ''}|${currentSweep}`);
  const hasQCData = $derived(currentKey in qcApplied);
  const appliedSteps = $derived(qcApplied[currentKey] ?? []);

  // Problems that make the pipeline unrunnable for the displayed variable.
  const stepIssues = $derived(pipelineSteps.map((s) => applicability(s.name)));
  const paramIssues = $derived(
    pipelineSteps.map((s) => {
      const out: Record<string, string> = {};
      for (const [key, spec] of Object.entries(getAlgoParams(s.name))) {
        const msg = paramProblem(s.params[key], spec);
        if (msg) out[key] = msg;
      }
      return out;
    }),
  );
  const blockedReason = $derived.by(() => {
    const bi = stepIssues.findIndex((x) => x === 'block');
    if (bi >= 0) return `Step ${bi + 1} (${pipelineSteps[bi].name.replace(/_/g, ' ')}) does not apply to ${currentVariable}`;
    const pi = paramIssues.findIndex((x) => Object.keys(x).length > 0);
    if (pi >= 0) return `Step ${pi + 1}: fix the highlighted parameter`;
    return '';
  });
  const canRun = $derived(
    hasData && isConnected && pipelineSteps.length > 0 && !isRunning && !!currentVariable && !blockedReason,
  );

  let unsubRadar: (() => void) | null = null;
  let unsubConnection: (() => void) | null = null;
  let algorithmsFetched = false;
  let algorithmsTimer: ReturnType<typeof setTimeout> | null = null;

  // A different file: nothing that was applied to the old one carries over.
  let lastFile: string | null | undefined = undefined;
  $effect(() => {
    const f = filePath;
    untrack(() => {
      if (lastFile !== undefined && f !== lastFile) qcApplied = {};
      lastFile = f;
    });
  });

  onMount(() => {
    // Load presets from localStorage once
    try {
      const saved = localStorage.getItem('xradar-qc-presets');
      if (saved) presets = JSON.parse(saved);
    } catch { /* ignore */ }

    unsubRadar = radarData.subscribe(() => tryFetchAlgorithms());
    unsubConnection = connectionStatus.subscribe((st) => {
      // After a reconnect the list is fetched again if it never arrived.
      if (st !== 'connected' && algorithms.length === 0) algorithmsFetched = false;
      tryFetchAlgorithms();
    });
  });

  onDestroy(() => {
    unsubRadar?.();
    unsubConnection?.();
    if (algorithmsTimer) clearTimeout(algorithmsTimer);
  });

  function tryFetchAlgorithms() {
    const rd = $radarData;
    const conn = $connectionStatus;
    if (rd.variables.length > 0 && conn === 'connected' && !algorithmsFetched) {
      algorithmsFetched = true;
      fetchAlgorithms();
    }
  }

  function fetchAlgorithms() {
    algorithmsFailed = false;
    const unsub = wsManager.onMessage('qc_algorithms_list', (msg: any) => {
      algorithms = msg.algorithms;
      algorithmsFailed = false;
      if (algorithmsTimer) clearTimeout(algorithmsTimer);
      unsub();
    });
    wsManager.send({ type: 'list_qc_algorithms' });
    if (algorithmsTimer) clearTimeout(algorithmsTimer);
    algorithmsTimer = setTimeout(() => {
      if (algorithms.length === 0) {
        unsub();
        algorithmsFailed = true;
        algorithmsFetched = false;
      }
    }, 8000);
  }

  /** Is the algorithm meant for the displayed variable? 'block' when its list is narrow (a mismatch is nonsense). */
  function applicability(name: string): 'ok' | 'warn' | 'block' {
    const algo = algorithms.find((a) => a.name === name);
    if (!algo || !currentVariable) return 'ok';
    const list = algo.applicable_variables.map((v) => v.toUpperCase());
    if (list.includes(currentVariable.toUpperCase())) return 'ok';
    return list.length <= 5 ? 'block' : 'warn';
  }

  function paramProblem(v: any, spec: QCParamSpec): string | null {
    if (spec.options) return spec.options.includes(v) ? null : 'Pick an option';
    if (spec.type !== 'float' && spec.type !== 'int') return null;
    if (v === null || v === undefined || v === '') return spec.default === null ? null : 'Required';
    if (typeof v !== 'number' || !Number.isFinite(v)) return 'Not a number';
    if (spec.type === 'int' && !Number.isInteger(v)) return 'Whole number';
    if (spec.min != null && v < spec.min) return `Min ${spec.min}`;
    if (spec.max != null && v > spec.max) return `Max ${spec.max}`;
    return null;
  }

  function addStep(algoName: string) {
    const algo = algorithms.find(a => a.name === algoName);
    if (!algo) return;

    const params: Record<string, any> = {};
    for (const [key, spec] of Object.entries(algo.params)) {
      params[key] = spec.default;
    }
    pipelineSteps = [...pipelineSteps, { name: algoName, params, expanded: true }];
  }

  function removeStep(index: number) {
    pipelineSteps = pipelineSteps.filter((_, i) => i !== index);
  }

  function toggleExpand(index: number) {
    pipelineSteps = pipelineSteps.map((s, i) => i === index ? { ...s, expanded: !s.expanded } : s);
  }

  function moveStep(fromIdx: number, toIdx: number) {
    if (fromIdx === toIdx) return;
    const updated = [...pipelineSteps];
    const [item] = updated.splice(fromIdx, 1);
    updated.splice(toIdx, 0, item);
    pipelineSteps = updated;
  }

  // --- Drag and drop ---
  function onDragStart(e: DragEvent, index: number) {
    dragIndex = index;
    if (e.dataTransfer) {
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', String(index));
    }
  }

  function onDragOver(e: DragEvent, index: number) {
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
    dropIndex = index;
  }

  function onDragLeave() {
    dropIndex = null;
  }

  function onDrop(e: DragEvent, index: number) {
    e.preventDefault();
    if (dragIndex !== null && dragIndex !== index) {
      moveStep(dragIndex, index);
    }
    dragIndex = null;
    dropIndex = null;
  }

  function onDragEnd() {
    dragIndex = null;
    dropIndex = null;
  }

  // --- Run pipeline ---
  function runQC() {
    if (!canRun || !currentVariable) return;
    const variable = currentVariable;
    const sweep = currentSweep;
    const key = currentKey;

    const steps = pipelineSteps.map(s => ({
      name: s.name,
      params: { ...s.params },
    }));
    const names = steps.map((s) => s.name);
    const mark = () => { qcApplied = { ...qcApplied, [key]: names }; };

    isRunning = true;
    runHandle = runFeatureRequest({
      kind: 'qc',
      label: `Quality control: ${variable} sweep ${sweep}`,
      message: { type: 'run_qc', steps, variable, sweep },
      resultTypes: ['qc_data_ready'],
      accept: (e: any) => e.variable === variable && e.sweep === sweep,
      onResult: mark,
      // The result replaces the sweep wherever it shows up, even after Cancel / a timeout.
      onLate: mark,
      onSettled: () => { isRunning = false; runHandle = null; },
      retry: runQC,
      timeoutMs: 120000,
    });
  }

  function cancelQC() {
    runHandle?.cancel();
  }

  // --- Undo: reload original data ---
  function undoQC() {
    if (!currentVariable || !hasQCData) return;
    const key = currentKey;
    wsManager.requestSweepData(currentVariable, currentSweep, undefined, {
      onSettled: (result) => {
        if (result !== 'done') return;
        const { [key]: _gone, ...rest } = qcApplied;
        qcApplied = rest;
      },
    });
  }

  // --- Preview toggle ---
  function togglePreview() {
    if (hasQCData) undoQC();
    else runQC();
  }

  // --- Presets ---
  function savePreset() {
    if (!presetName.trim() || pipelineSteps.length === 0) return;
    const preset: PipelinePreset = {
      name: presetName.trim(),
      steps: pipelineSteps.map(s => ({ name: s.name, params: { ...s.params } })),
    };
    presets = [...presets.filter(p => p.name !== preset.name), preset];
    try { localStorage.setItem('xradar-qc-presets', JSON.stringify(presets)); } catch { /* ignore */ }
    presetName = '';
  }

  function loadPreset(preset: PipelinePreset) {
    pipelineSteps = preset.steps.map(s => ({ name: s.name, params: { ...s.params }, expanded: false }));
  }

  function deletePreset(name: string) {
    presets = presets.filter(p => p.name !== name);
    try { localStorage.setItem('xradar-qc-presets', JSON.stringify(presets)); } catch { /* ignore */ }
  }

  function getAlgoDescription(name: string): string {
    return algorithms.find(a => a.name === name)?.description ?? name;
  }

  function getAlgoParams(name: string): Record<string, QCParamSpec> {
    return algorithms.find(a => a.name === name)?.params ?? {};
  }
</script>

<CollapsiblePanel title="Quality Control" badge={pipelineSteps.length > 0 ? pipelineSteps.length : null}>
  {#if hasData}
    <div class="qc-panel">
      <!-- Algorithm picker -->
      <div class="section-label">Add QC Step</div>
      <div class="algo-list">
        {#each algorithms as algo}
          {@const applicable = algo.applicable_variables.some(v => variables.includes(v))}
          <button
            class="algo-btn"
            class:dimmed={!applicable}
            on:click={() => addStep(algo.name)}
            title={algo.description + (applicable ? '' : ' (no applicable variables loaded)')}
            disabled={isRunning}
          >
            <span class="algo-name">{algo.name.replace(/_/g, ' ')}</span>
            <svg class="algo-add-icon" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
              <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
            </svg>
          </button>
        {/each}
        {#if algorithms.length === 0}
          {#if algorithmsFailed}
            <div class="loading-msg">
              Could not load the algorithm list.
              <button class="link-btn" on:click={tryFetchAlgorithms}>Retry</button>
            </div>
          {:else}
            <div class="loading-msg">Loading algorithms...</div>
          {/if}
        {/if}
      </div>

      <!-- Pipeline builder -->
      {#if pipelineSteps.length > 0}
        <div class="section-label">Pipeline ({pipelineSteps.length} steps)</div>
        <div class="pipeline-list">
          {#each pipelineSteps as step, i (i)}
            <div
              class="pipeline-step"
              class:dragging={dragIndex === i}
              class:drop-target={dropIndex === i && dragIndex !== i}
              draggable="true"
              on:dragstart={(e) => onDragStart(e, i)}
              on:dragover={(e) => onDragOver(e, i)}
              on:dragleave={onDragLeave}
              on:drop={(e) => onDrop(e, i)}
              on:dragend={onDragEnd}
              role="listitem"
            >
              <div class="step-header">
                <span class="step-grip" title="Drag to reorder">
                  <svg width="8" height="12" viewBox="0 0 8 12" fill="currentColor">
                    <circle cx="2" cy="2" r="1.2"/><circle cx="6" cy="2" r="1.2"/>
                    <circle cx="2" cy="6" r="1.2"/><circle cx="6" cy="6" r="1.2"/>
                    <circle cx="2" cy="10" r="1.2"/><circle cx="6" cy="10" r="1.2"/>
                  </svg>
                </span>
                <span class="step-index">{i + 1}.</span>
                <button class="step-name-btn" on:click={() => toggleExpand(i)} title={getAlgoDescription(step.name)}>
                  {step.name.replace(/_/g, ' ')}
                </button>
                <button
                  class="step-move-btn"
                  on:click={() => moveStep(i, i - 1)}
                  title="Move up"
                  aria-label="Move step {i + 1} up"
                  disabled={isRunning || i === 0}
                >
                  <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="18 15 12 9 6 15"/></svg>
                </button>
                <button
                  class="step-move-btn"
                  on:click={() => moveStep(i, i + 1)}
                  title="Move down"
                  aria-label="Move step {i + 1} down"
                  disabled={isRunning || i === pipelineSteps.length - 1}
                >
                  <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="6 9 12 15 18 9"/></svg>
                </button>
                <button class="step-remove-btn" on:click={() => removeStep(i)} title="Remove step" aria-label="Remove step {i + 1}" disabled={isRunning}>
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                    <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
                  </svg>
                </button>
              </div>

              {#if stepIssues[i] !== 'ok'}
                <div class="step-note" class:block={stepIssues[i] === 'block'} role="note">
                  {stepIssues[i] === 'block' ? 'Not applicable to' : 'Not meant for'} {currentVariable}
                  (expects {algorithms.find((a) => a.name === step.name)?.applicable_variables.slice(0, 4).join(', ')})
                </div>
              {/if}

              {#if step.expanded}
                <div class="step-params">
                  {#each Object.entries(getAlgoParams(step.name)) as [key, spec]}
                    <div class="param-row">
                      <label class="param-label" for="qc-param-{i}-{key}">{spec.label}</label>
                      {#if spec.options}
                        <select
                          id="qc-param-{i}-{key}"
                          bind:value={step.params[key]}
                          disabled={isRunning}
                          class="param-input"
                        >
                          {#each spec.options as opt}
                            <option value={opt}>{opt}</option>
                          {/each}
                        </select>
                      {:else if spec.type === 'float' || spec.type === 'int'}
                        <input
                          id="qc-param-{i}-{key}"
                          type="number"
                          step={spec.type === 'int' ? '1' : 'any'}
                          min={spec.min}
                          max={spec.max}
                          bind:value={step.params[key]}
                          disabled={isRunning}
                          class="param-input"
                          class:invalid={!!paramIssues[i]?.[key]}
                          aria-invalid={!!paramIssues[i]?.[key]}
                          title={paramIssues[i]?.[key] ?? (spec.min != null && spec.max != null ? `${spec.min} to ${spec.max}` : '')}
                        />
                        {#if paramIssues[i]?.[key]}
                          <span class="param-error">{paramIssues[i][key]}</span>
                        {/if}
                      {:else}
                        <input
                          id="qc-param-{i}-{key}"
                          type="text"
                          bind:value={step.params[key]}
                          disabled={isRunning}
                          class="param-input"
                        />
                      {/if}
                    </div>
                  {/each}
                  {#if Object.keys(getAlgoParams(step.name)).length === 0}
                    <div class="no-params">No parameters</div>
                  {/if}
                </div>
              {/if}
            </div>
          {/each}
        </div>

        <!-- Actions -->
        <div class="actions">
          <button
            class="primary action-btn"
            on:click={runQC}
            disabled={!canRun}
            title={!isConnected ? 'Not connected' : !hasData ? 'No data' : pipelineSteps.length === 0 ? 'Add steps first' : blockedReason || 'Run QC pipeline on the displayed sweep'}
          >
            {#if isRunning}
              <span class="spinner"></span>
              Processing...
            {:else}
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="5 3 19 12 5 21 5 3"/></svg>
              Apply QC
            {/if}
          </button>

          {#if isRunning}
            <button class="secondary action-btn-sm cancel-btn" on:click={cancelQC} title="Stop waiting for the result">
              Cancel
            </button>
          {:else}
            <div class="action-row">
              <button
                class="secondary action-btn-sm"
                on:click={togglePreview}
                disabled={!hasQCData && !canRun}
                class:active={hasQCData}
                title={hasQCData ? 'Show the original data again' : 'Apply the pipeline to preview it on the displayed sweep'}
              >
                {hasQCData ? 'Hide Preview' : 'Preview'}
              </button>

              <button
                class="secondary action-btn-sm"
                on:click={undoQC}
                disabled={!hasQCData}
                title="Revert the displayed sweep to the original data"
              >
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                  <polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/>
                </svg>
                Undo
              </button>
            </div>
          {/if}
          {#if blockedReason && !isRunning}
            <div class="blocked-hint" role="status">{blockedReason}</div>
          {/if}
        </div>

        <!-- Progress -->
        {#if isRunning && progress}
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

        {#if hasQCData && !isRunning}
          <div class="result-card">
            <div class="result-title">QC applied to {currentVariable}, sweep {currentSweep}</div>
            <div class="result-stats">
              {#each appliedSteps as name, i}
                <span>{i + 1}. {name.replace(/_/g, ' ')}</span>
              {/each}
            </div>
          </div>
        {/if}
      {/if}

      <!-- Presets -->
      <div class="section-label preset-header">Presets</div>
      <div class="preset-section">
        {#if pipelineSteps.length > 0}
          <div class="preset-save-row">
            <input
              type="text"
              bind:value={presetName}
              placeholder="Preset name..."
              class="preset-name-input"
              on:keydown={(e) => e.key === 'Enter' && savePreset()}
            />
            <button class="preset-save-btn" on:click={savePreset} disabled={!presetName.trim()}>Save</button>
          </div>
        {/if}

        {#if presets.length > 0}
          <div class="preset-list">
            {#each presets as preset}
              <div class="preset-item">
                <button class="preset-load-btn" on:click={() => loadPreset(preset)} title="Load preset: {preset.name}">
                  <span class="preset-item-name">{preset.name}</span>
                  <span class="preset-item-count">{preset.steps.length} steps</span>
                </button>
                <button class="preset-delete-btn" on:click={() => deletePreset(preset.name)} title="Delete preset">
                  <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                    <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
                  </svg>
                </button>
              </div>
            {/each}
          </div>
        {:else}
          <div class="no-presets">No saved presets</div>
        {/if}
      </div>
    </div>
  {:else}
    <p class="no-data-msg">Load a file to enable quality control</p>
  {/if}
</CollapsiblePanel>

<style>
  .qc-panel {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm);
  }

  .section-label {
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-muted);
    margin-top: var(--spacing-xs);
  }

  /* Algorithm picker */
  .algo-list {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .algo-btn {
    display: flex;
    align-items: center;
    justify-content: space-between;
    width: 100%;
    padding: 5px 8px;
    font-size: 11px;
    color: var(--text-secondary);
    background: transparent;
    border: 1px solid transparent;
    border-radius: var(--radius-sm, 4px);
    cursor: pointer;
    transition: all var(--transition-fast, 150ms);
    text-align: left;
    height: auto;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }

  .algo-btn:hover:not(:disabled) {
    background: rgba(var(--c-accent-rgb), 0.06);
    border-color: var(--border-color);
    color: var(--text-primary);
  }

  .algo-btn.dimmed {
    opacity: 0.45;
  }

  .algo-name {
    text-transform: capitalize;
  }

  .algo-add-icon {
    opacity: 0;
    transition: opacity var(--transition-fast, 150ms);
  }

  .algo-btn:hover .algo-add-icon {
    opacity: 0.6;
  }

  .loading-msg, .no-presets, .no-params {
    font-size: 11px;
    color: var(--text-muted);
    text-align: center;
    padding: var(--spacing-sm) 0;
  }

  /* Pipeline steps */
  .pipeline-list {
    display: flex;
    flex-direction: column;
    gap: 3px;
  }

  .pipeline-step {
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm, 4px);
    transition: all var(--transition-fast, 150ms);
    cursor: grab;
  }

  .pipeline-step.dragging {
    opacity: 0.4;
    border-color: var(--accent-primary);
  }

  .pipeline-step.drop-target {
    border-color: var(--accent-primary);
    box-shadow: 0 0 8px rgba(var(--c-accent-rgb), 0.15);
  }

  .step-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs, 4px);
    padding: 5px 6px;
  }

  .step-grip {
    color: var(--text-muted);
    opacity: 0.4;
    cursor: grab;
    flex-shrink: 0;
    display: flex;
    align-items: center;
  }

  .step-index {
    font-size: 10px;
    color: var(--text-muted);
    font-family: var(--font-mono);
    flex-shrink: 0;
    min-width: 16px;
  }

  .step-name-btn {
    flex: 1;
    justify-content: flex-start;
    font-size: 11px;
    font-weight: 500;
    color: var(--text-primary);
    background: transparent;
    border: none;
    cursor: pointer;
    text-align: left;
    padding: 0;
    text-transform: capitalize;
    height: auto;
    border-radius: 0;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }

  .step-name-btn:hover {
    color: var(--accent-primary);
  }

  .step-remove-btn {
    padding: 2px;
    color: var(--text-muted);
    background: transparent;
    border: none;
    cursor: pointer;
    border-radius: 3px;
    display: flex;
    align-items: center;
    justify-content: center;
    height: auto;
    width: auto;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }

  .step-remove-btn:hover {
    color: var(--accent-danger);
    background: rgba(var(--c-alarm-rgb), 0.1);
  }

  .step-params {
    padding: 4px 8px 8px 28px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    border-top: 1px solid var(--border-color);
  }

  .param-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm, 6px);
  }

  .param-label {
    font-size: 10px;
    color: var(--text-secondary);
    min-width: 80px;
    flex-shrink: 0;
  }

  .param-input {
    flex: 1;
    font-family: var(--font-mono);
    font-size: 11px;
    padding: 3px 5px;
    min-width: 0;
  }

  .param-input.invalid {
    border-color: var(--accent-danger);
    box-shadow: 0 0 0 1px var(--accent-danger);
  }

  .param-error {
    font-size: 9px;
    color: var(--accent-danger);
    white-space: nowrap;
  }

  .step-move-btn {
    padding: 2px;
    color: var(--text-muted);
    background: transparent;
    border: none;
    cursor: pointer;
    border-radius: 3px;
    display: flex;
    align-items: center;
    justify-content: center;
    height: auto;
    width: auto;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }

  .step-move-btn:hover:not(:disabled) {
    color: var(--text-primary);
    background: rgba(var(--c-accent-rgb), 0.12);
  }

  .step-move-btn:disabled {
    opacity: 0.25;
    cursor: default;
  }

  .step-note {
    margin: 0 6px 6px 28px;
    padding: 3px 6px;
    font-size: 10px;
    color: var(--text-secondary);
    background: rgba(var(--c-caution-rgb), 0.1);
    border-left: 2px solid var(--c-caution);
    border-radius: 2px;
  }

  .step-note.block {
    background: rgba(var(--c-alarm-rgb), 0.1);
    border-left-color: var(--accent-danger);
  }

  .blocked-hint {
    font-size: 10px;
    color: var(--text-secondary);
  }

  .cancel-btn {
    width: 100%;
  }

  .link-btn {
    display: inline;
    padding: 0 4px;
    height: auto;
    font-size: inherit;
    color: var(--accent-primary);
    background: transparent;
    border: none;
    text-decoration: underline;
    cursor: pointer;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }

  /* Actions */
  .actions {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs, 4px);
    margin-top: var(--spacing-xs, 4px);
  }

  .action-row {
    display: flex;
    gap: var(--spacing-xs, 4px);
  }

  .action-btn {
    width: 100%;
  }

  .action-btn-sm {
    flex: 1;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 4px;
    font-size: 11px;
    padding: 5px 8px;
  }

  .action-btn-sm.active {
    background: var(--bg-active);
    border-color: rgba(var(--c-accent-rgb), 0.3);
    color: var(--accent-primary);
  }

  .spinner {
    display: inline-block;
    width: 12px;
    height: 12px;
    border: 2px solid rgba(var(--c-ink-rgb), 0.3);
    border-top-color: var(--c-on-accent);
    border-radius: 50%;
    animation: spin 600ms linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  /* Progress */
  .progress-section {
    margin-top: var(--spacing-xs, 4px);
    display: flex;
    flex-direction: column;
    gap: 3px;
  }

  .progress-label {
    display: flex;
    justify-content: space-between;
    font-size: 10px;
    color: var(--text-secondary);
  }

  .progress-pct {
    font-family: var(--font-mono);
    color: var(--text-accent);
  }

  .progress-bar {
    height: 3px;
    background: var(--bg-secondary);
    border-radius: 2px;
    overflow: hidden;
  }

  .progress-bar-fill {
    height: 100%;
    background: linear-gradient(90deg, var(--accent-primary), var(--accent-hover));
    transition: width 300ms ease;
    border-radius: 2px;
  }

  /* Result card */
  .result-card {
    margin-top: var(--spacing-xs, 4px);
    padding: var(--spacing-sm, 6px) var(--spacing-md, 10px);
    background: rgba(var(--c-nominal-rgb), 0.06);
    border: 1px solid rgba(var(--c-nominal-rgb), 0.15);
    border-radius: 6px;
  }

  .result-title {
    font-size: 11px;
    font-weight: 600;
    color: var(--accent-success);
    margin-bottom: 3px;
  }

  .result-stats {
    display: flex;
    flex-direction: column;
    gap: 1px;
    font-size: 10px;
    font-family: var(--font-mono);
    color: var(--text-secondary);
  }

  /* Presets */
  .preset-header {
    margin-top: var(--spacing-md, 12px);
  }

  .preset-section {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs, 4px);
  }

  .preset-save-row {
    display: flex;
    gap: var(--spacing-xs, 4px);
  }

  .preset-name-input {
    flex: 1;
    font-size: 11px;
    padding: 4px 6px;
    min-width: 0;
  }

  .preset-save-btn {
    font-size: 10px;
    padding: 4px 10px;
    white-space: nowrap;
  }

  .preset-list {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .preset-item {
    display: flex;
    align-items: center;
    gap: 2px;
  }

  .preset-load-btn {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 4px 8px;
    font-size: 11px;
    color: var(--text-secondary);
    background: transparent;
    border: 1px solid transparent;
    border-radius: var(--radius-sm, 4px);
    cursor: pointer;
    text-align: left;
    height: auto;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }

  .preset-load-btn:hover {
    background: rgba(var(--c-accent-rgb), 0.06);
    border-color: var(--border-color);
    color: var(--text-primary);
  }

  .preset-item-name {
    font-weight: 500;
  }

  .preset-item-count {
    font-size: 9px;
    color: var(--text-muted);
    font-family: var(--font-mono);
  }

  .preset-delete-btn {
    padding: 3px;
    color: var(--text-muted);
    background: transparent;
    border: none;
    cursor: pointer;
    border-radius: 3px;
    display: flex;
    align-items: center;
    justify-content: center;
    height: auto;
    width: auto;
    opacity: 0.4;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }

  .preset-delete-btn:hover {
    opacity: 1;
    color: var(--accent-danger);
    background: rgba(var(--c-alarm-rgb), 0.1);
  }

  .no-data-msg {
    font-size: 12px;
    color: var(--text-muted);
    text-align: center;
    padding: var(--spacing-md, 12px) 0;
  }
</style>
