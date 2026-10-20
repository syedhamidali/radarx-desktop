<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import {
    radarData,
    exportNotification,
    connectionStatus,
    selectedVariable,
    selectedSweep,
    processingProgress,
  } from '../stores/radarData';
  import { wsManager } from '../utils/websocket';
  import { beginOp, cancelOp, type OpHandle } from '../utils/requestScheduler';
  import { addToast } from '../stores/toasts';
  import { getActiveFile } from '../stores/fileManager';
  import { activeDerived } from '../stores/derived';
  import { get } from 'svelte/store';
  import { rhiNoise } from '../stores/rhiNoise';
  import { isMaskActive, maskDescription, maskOf, valueMasks } from '../stores/valueMask';
  import { engineCleanOptions } from '../utils/rhiClean';
  import CollapsiblePanel from './CollapsiblePanel.svelte';
  import { webUi } from '../stores/webInfo';
  import { inTauri } from '../utils/env';

  // --- Where the file goes -------------------------------------------------
  // The desktop app asks (native Save / Choose-folder dialog, starting in the folder used last).
  // Outside Tauri there is no dialog, so the sidecar writes to ~/Downloads/xradar-exports — a normal
  // folder, never a hidden temp directory.
  const LAST_DIR_KEY = 'xradar.export.lastDir.v1';
  const DEFAULT_FOLDER_HINT = '~/Downloads/xradar-exports';

  function lastDir(): string | undefined {
    try {
      return localStorage.getItem(LAST_DIR_KEY) || undefined;
    } catch {
      return undefined;
    }
  }
  function rememberDir(dir: string | undefined): void {
    if (!dir) return;
    try {
      localStorage.setItem(LAST_DIR_KEY, dir);
    } catch {
      /* storage unavailable — ignore */
    }
  }
  function parentDir(path: string): string | undefined {
    const i = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'));
    return i > 0 ? path.slice(0, i) : undefined;
  }
  /** "<file>_<variable>_sweep<N>" with filesystem-safe characters. */
  function suggestedStem(): string {
    const file = getActiveFile();
    const base = (file?.filename ?? 'radar').replace(/\.[^./\\]+$/, '');
    const variable = $selectedVariable ?? 'data';
    return `${base}_${variable}_sweep${$selectedSweep}`.replace(/[^A-Za-z0-9._-]+/g, '_');
  }

  /** Ask where to save. Resolves a path, `null` if the user cancelled, `undefined` if there is no dialog. */
  async function chooseSavePath(
    ext: string,
    label: string,
  ): Promise<string | null | undefined> {
    if (!inTauri()) return undefined;
    const { save } = await import('@tauri-apps/plugin-dialog');
    const dir = lastDir();
    const name = `${suggestedStem()}.${ext}`;
    const path = await save({
      title: `Export ${label}`,
      defaultPath: dir ? `${dir}/${name}` : name,
      filters: [{ name: label, extensions: [ext] }],
    });
    if (!path) return null;
    rememberDir(parentDir(path));
    return path;
  }

  async function chooseFolder(): Promise<string | null | undefined> {
    if (!inTauri()) return undefined;
    const { open } = await import('@tauri-apps/plugin-dialog');
    const picked = await open({
      directory: true,
      multiple: false,
      title: 'Choose a folder for the exported images',
      defaultPath: batchOutputDir || lastDir(),
    });
    const dir = Array.isArray(picked) ? picked[0] : picked;
    if (!dir) return null;
    rememberDir(dir);
    return dir;
  }

  async function browseBatchFolder() {
    const dir = await chooseFolder();
    if (dir) batchOutputDir = dir;
  }

  // --- State ---
  let hasData = false;
  let isConnected = false;

  // Quick export
  let quickDpi = 300;
  let showRings = true;
  let showTitle = true;
  /** Own title for the figure (empty = the automatic one). Placeholders: {variable} {sweep} {elevation} {time} {radar}. */
  const TITLE_KEY = 'xradar.export.title.v1';
  let exportTitle = (() => {
    try { return localStorage.getItem(TITLE_KEY) ?? ''; } catch { return ''; }
  })();
  $: try { localStorage.setItem(TITLE_KEY, exportTitle); } catch { /* storage unavailable */ }
  let darkBackground = false;

  // Data export: write module results (cleaned fields, …) next to the measured moments.
  let includeDerived = true;
  /** Data files: apply the value mask(s) too (recorded in the file); figures always show the mask. */
  let maskData = true;
  /** Active value masks of the radar variables (wind / module-field masks belong to their own views). */
  $: activeMasks = Object.fromEntries(
    Object.entries($valueMasks).filter(([name, m]) => !name.includes(':') && isMaskActive(m)),
  );
  $: selectedMask = $selectedVariable ? maskOf($valueMasks, $selectedVariable) : null;
  $: selectedMaskNote = selectedMask ? maskDescription(selectedMask) : null;
  let includeOriginal = true;
  $: derivedCount = $activeDerived.length;

  // Batch export
  let batchFormat = 'png';
  let batchDpi = 300;
  let batchOutputDir = '';
  let selectedBatchVars: Set<string> = new Set();
  let selectedBatchSweeps: Set<number> = new Set();
  let batchSelectAllVars = false;
  let batchSelectAllSweeps = false;

  // Data export
  // (no extra state needed)

  // Animation export
  let animFrameRate = 500;
  let animSweepStart = 0;
  let animSweepEnd = 0;

  // Progress
  let progressPercent = 0;
  let progressMessage = '';
  let isExporting = false;

  $: hasData = $radarData.variables.length > 0;
  $: isConnected = $connectionStatus === 'connected';
  $: canExport = hasData && isConnected && !isExporting;

  let unsubRadar: (() => void) | null = null;
  let unsubProgress: (() => void) | null = null;
  let unsubNotification: (() => void) | null = null;
  let unsubError: (() => void) | null = null;
  let unsubConn: (() => void) | null = null;

  // Operations-tray entry for the export in flight. Big batch / animation exports can legitimately
  // take minutes, so the safety timeout is generous; the panel's own state is released with it.
  let exportOp: OpHandle | null = null;
  const EXPORT_TIMEOUT_MS = 5 * 60 * 1000;

  function beginExport(label: string, retry: () => void) {
    if (exportOp) { const prev = exportOp; exportOp = null; cancelOp(prev.id); }
    isExporting = true;
    progressPercent = 0;
    progressMessage = '';
    const op: OpHandle = beginOp({
      kind: 'export',
      label,
      slowMs: 20_000,
      timeoutMs: EXPORT_TIMEOUT_MS,
      onCancel: () => {
        // The server cannot abort an export; stop waiting and let the panel be used again.
        if (exportOp === op) exportOp = null;
        isExporting = false;
      },
      onRetry: retry,
    });
    exportOp = op;
    // Timeout (state 'failed' set by the scheduler) aborts the signal: release the panel too.
    op.signal.addEventListener('abort', () => {
      if (exportOp === op) { exportOp = null; isExporting = false; }
    });
  }

  function endExport(error?: string) {
    const op = exportOp;
    exportOp = null;
    isExporting = false;
    if (!op) return;
    if (error) op.fail(error);
    else op.done();
  }

  onMount(() => {
    unsubRadar = radarData.subscribe((rd) => {
      if (rd.sweeps.length > 0) {
        animSweepEnd = rd.sweeps.length - 1;
      }
    });

    unsubProgress = processingProgress.subscribe((p) => {
      if (p) {
        progressPercent = p.percent;
        progressMessage = p.message;
        if (exportOp) exportOp.update(`${p.message || 'Exporting...'} (${Math.round(p.percent)}%)`);
        if (p.percent >= 100) {
          const op = exportOp;
          setTimeout(() => { if (exportOp === op) endExport(); }, 1000);
        }
      }
    });

    unsubNotification = exportNotification.subscribe((n) => {
      if (n) {
        addToast('success', `${n}`);
        exportNotification.set(null);
        endExport();
      }
    });

    // A server error or a lost connection would otherwise leave the panel stuck on "Exporting...".
    unsubError = wsManager.onMessage('error', (m) => {
      if (exportOp) endExport(String(m?.message ?? 'Export failed'));
    });
    unsubConn = connectionStatus.subscribe((st) => {
      if (st !== 'connected' && exportOp) endExport('Connection to the sidecar was lost');
    });
  });

  onDestroy(() => {
    unsubRadar?.();
    unsubProgress?.();
    unsubNotification?.();
    unsubError?.();
    unsubConn?.();
    if (exportOp) { const op = exportOp; exportOp = null; cancelOp(op.id); }
  });

  // Batch variable/sweep selection helpers
  function toggleBatchVar(v: string) {
    if (selectedBatchVars.has(v)) {
      selectedBatchVars.delete(v);
    } else {
      selectedBatchVars.add(v);
    }
    selectedBatchVars = new Set(selectedBatchVars);
    batchSelectAllVars = selectedBatchVars.size === $radarData.variables.length;
  }

  function toggleAllVars() {
    if (batchSelectAllVars) {
      selectedBatchVars = new Set();
      batchSelectAllVars = false;
    } else {
      selectedBatchVars = new Set($radarData.variables);
      batchSelectAllVars = true;
    }
  }

  function toggleBatchSweep(s: number) {
    if (selectedBatchSweeps.has(s)) {
      selectedBatchSweeps.delete(s);
    } else {
      selectedBatchSweeps.add(s);
    }
    selectedBatchSweeps = new Set(selectedBatchSweeps);
    batchSelectAllSweeps = selectedBatchSweeps.size === $radarData.sweeps.length;
  }

  function toggleAllSweeps() {
    if (batchSelectAllSweeps) {
      selectedBatchSweeps = new Set();
      batchSelectAllSweeps = false;
    } else {
      selectedBatchSweeps = new Set($radarData.sweeps.map(s => s.index));
      batchSelectAllSweeps = true;
    }
  }

  // --- Export actions ---

  function exportOptions(): Record<string, any> {
    return {
      show_rings: showRings,
      show_title: showTitle,
      ...(showTitle && exportTitle.trim() ? { title: exportTitle.trim() } : {}),
      show_colorbar: true,
      dark_background: darkBackground,
      // an RHI figure hides noise the way the viewer does (ignored for PPI sweeps)
      rhi_clean: engineCleanOptions(get(rhiNoise), maskOf(get(valueMasks), $selectedVariable)),
      // the value mask of each variable, the same rules as on screen (the figure says so in a footnote)
      ...(Object.keys(activeMasks).length ? { value_masks: activeMasks } : {}),
    };
  }

  async function doQuickExport(fmt: string) {
    if (!canExport) return;
    const path = await chooseSavePath(fmt, fmt.toUpperCase());
    if (path === null) return; // cancelled
    beginExport(`Exporting ${fmt.toUpperCase()}`, () => doQuickExport(fmt));
    wsManager.send({
      type: 'export',
      format: fmt,
      path: path ?? undefined,
      dpi: quickDpi,
      variable: $selectedVariable,
      sweep: $selectedSweep,
      options: exportOptions(),
    });
  }

  async function doBatchExport() {
    if (!canExport) return;
    const vars = Array.from(selectedBatchVars);
    const sweeps = Array.from(selectedBatchSweeps).sort((a, b) => a - b);
    if (vars.length === 0) {
      addToast('error', 'Select at least one variable for batch export');
      return;
    }
    if (sweeps.length === 0) {
      addToast('error', 'Select at least one sweep for batch export');
      return;
    }
    if (!batchOutputDir && !$webUi) {
      // No folder chosen yet: ask (desktop) instead of silently writing somewhere the user cannot find.
      const dir = await chooseFolder();
      if (dir === null) return; // cancelled
      if (dir) batchOutputDir = dir;
    }
    beginExport(`Batch export (${vars.length * sweeps.length} images)`, () => doBatchExport());
    wsManager.send({
      type: 'batch_export',
      variables: vars,
      sweeps: sweeps,
      format: batchFormat,
      dpi: batchDpi,
      output_dir: batchOutputDir || undefined,
      options: exportOptions(),
    });
  }

  const DATA_EXT: Record<string, string> = { netcdf: 'nc', cfradial2: 'nc', csv: 'csv', zarr: 'zarr' };

  async function doDataExport(fmt: string) {
    if (!canExport) return;
    const path = await chooseSavePath(DATA_EXT[fmt] ?? fmt, fmt.toUpperCase());
    if (path === null) return; // cancelled
    beginExport(`Exporting ${fmt.toUpperCase()} data`, () => doDataExport(fmt));
    wsManager.send({
      type: 'export',
      format: fmt,
      path: path ?? undefined,
      variable: $selectedVariable,
      sweep: $selectedSweep,
      dpi: 300,
      options: maskData && Object.keys(activeMasks).length ? { value_masks: activeMasks } : {},
      // CSV is the single selected variable; the volume formats take every derived field too.
      ...(fmt !== 'csv' && derivedCount > 0
        ? { include_derived: includeDerived, include_original: includeOriginal }
        : {}),
    });
  }

  async function doAnimationExport() {
    if (!canExport) return;
    const sweeps: number[] = [];
    for (let i = animSweepStart; i <= animSweepEnd; i++) {
      sweeps.push(i);
    }
    if (sweeps.length < 2) {
      addToast('error', 'Animation requires at least 2 sweeps');
      return;
    }
    const path = await chooseSavePath('gif', 'Animated GIF');
    if (path === null) return; // cancelled
    beginExport(`Exporting animation (${sweeps.length} frames)`, () => doAnimationExport());
    wsManager.send({
      type: 'export_animation',
      path: path ?? undefined,
      variable: $selectedVariable,
      sweeps: sweeps,
      frame_duration_ms: animFrameRate,
      dpi: 150,
      options: exportOptions(),
    });
  }
</script>

<CollapsiblePanel title="Export" icon="">
  {#if hasData}
    <!-- Progress bar -->
    {#if isExporting}
      <div class="progress-section">
        <div class="progress-bar-track">
          <div class="progress-bar-fill" style="width: {progressPercent}%"></div>
        </div>
        <span class="progress-label">{progressMessage || 'Exporting...'}</span>
      </div>
    {/if}

    <!-- QUICK EXPORT -->
    <div class="export-section">
      <div class="section-header">Quick Export</div>
      <p class="section-desc">Export current view ({$selectedVariable ?? 'none'}, sweep {$selectedSweep})</p>

      <div class="format-buttons">
        <button class="fmt-btn" on:click={() => doQuickExport('png')} disabled={!canExport} title="Export as PNG">
          PNG
        </button>
        <button class="fmt-btn" on:click={() => doQuickExport('svg')} disabled={!canExport} title="Export as SVG">
          SVG
        </button>
        <button class="fmt-btn" on:click={() => doQuickExport('pdf')} disabled={!canExport} title="Export as PDF">
          PDF
        </button>
      </div>

      <div class="quick-options">
        <div class="field inline">
          <label for="quick-dpi">DPI</label>
          <input id="quick-dpi" type="range" min="72" max="600" step="1" bind:value={quickDpi} disabled={!canExport} />
          <span class="range-value">{quickDpi}</span>
        </div>
        <div class="checkbox-row">
          <label class="checkbox-label">
            <input type="checkbox" bind:checked={showRings} disabled={!canExport} />
            Range rings
          </label>
          <label class="checkbox-label">
            <input type="checkbox" bind:checked={showTitle} disabled={!canExport} />
            Title
          </label>
          <label class="checkbox-label">
            <input type="checkbox" bind:checked={darkBackground} disabled={!canExport} />
            Dark BG
          </label>
        </div>
        <div class="field">
          <label for="export-title">Title</label>
          <input
            id="export-title"
            type="text"
            bind:value={exportTitle}
            disabled={!canExport || !showTitle}
            placeholder="Automatic - or write your own"
            title="Placeholders: {'{variable}'} {'{sweep}'} {'{elevation}'} {'{time}'} {'{radar}'} (filled in for every figure of a batch)"
          />
        </div>
      </div>
    </div>

    <!-- BATCH EXPORT -->
    <div class="export-section">
      <div class="section-header">Batch Export</div>

      <div class="field">
        <span class="field-label" id="exp-batch-vars">Variables</span>
        <div class="checkbox-grid" role="group" aria-labelledby="exp-batch-vars">
          <label class="checkbox-label select-all">
            <input type="checkbox" checked={batchSelectAllVars} on:change={toggleAllVars} disabled={!canExport} />
            All
          </label>
          {#each $radarData.variables as v}
            <label class="checkbox-label">
              <input type="checkbox" checked={selectedBatchVars.has(v)} on:change={() => toggleBatchVar(v)} disabled={!canExport} />
              {v}
            </label>
          {/each}
        </div>
      </div>

      <div class="field">
        <span class="field-label" id="exp-batch-sweeps">Sweeps</span>
        <div class="checkbox-grid" role="group" aria-labelledby="exp-batch-sweeps">
          <label class="checkbox-label select-all">
            <input type="checkbox" checked={batchSelectAllSweeps} on:change={toggleAllSweeps} disabled={!canExport} />
            All
          </label>
          {#each $radarData.sweeps as s}
            <label class="checkbox-label">
              <input type="checkbox" checked={selectedBatchSweeps.has(s.index)} on:change={() => toggleBatchSweep(s.index)} disabled={!canExport} />
              {s.kind === 'rhi' ? `${s.index} (RHI ${s.azimuth?.toFixed(1) ?? '?'}\u00B0)` : `${s.index}${s.elevation != null ? ` (${s.elevation.toFixed(1)})` : ''}`}
            </label>
          {/each}
        </div>
      </div>

      <div class="batch-controls">
        <div class="field inline">
          <label for="batch-format">Format</label>
          <select id="batch-format" bind:value={batchFormat} disabled={!canExport}>
            <option value="png">PNG</option>
            <option value="svg">SVG</option>
            <option value="pdf">PDF</option>
          </select>
        </div>
        <div class="field inline">
          <label for="batch-dpi">DPI</label>
          <input id="batch-dpi" type="range" min="72" max="600" step="1" bind:value={batchDpi} disabled={!canExport} />
          <span class="range-value">{batchDpi}</span>
        </div>
        {#if $webUi}
          <p class="section-desc">Each image is downloaded by your browser when it is ready.</p>
        {:else}
        <div class="field inline">
          <label for="batch-dir">Output Dir</label>
          <input id="batch-dir" type="text" bind:value={batchOutputDir} placeholder={DEFAULT_FOLDER_HINT} disabled={!canExport} />
          <button type="button" class="browse-btn" on:click={browseBatchFolder} disabled={!canExport}>Choose…</button>
        </div>
        {/if}
      </div>

      <button class="primary action-btn" on:click={doBatchExport} disabled={!canExport}>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
          <polyline points="7 10 12 15 17 10"/>
          <line x1="12" y1="15" x2="12" y2="3"/>
        </svg>
        Batch Export ({selectedBatchVars.size} vars, {selectedBatchSweeps.size} sweeps)
      </button>
    </div>

    <!-- DATA EXPORT -->
    <div class="export-section">
      <div class="section-header">Data Export</div>
      <p class="section-desc">Export raw data for analysis</p>
      {#if Object.keys(activeMasks).length}
        <div class="checkbox-row">
          <label class="checkbox-label" title="Values hidden by the value mask are written as missing (NaN) and the file records the mask. Untick to write every value.">
            <input type="checkbox" bind:checked={maskData} disabled={!canExport} data-testid="export-mask-data" />
            Apply the value mask{selectedMaskNote ? ` (${$selectedVariable}: ${selectedMaskNote})` : ''}
          </label>
        </div>
      {/if}

      {#if derivedCount > 0}
        <div class="checkbox-row">
          <label class="checkbox-label" title="Add the {derivedCount} module result field(s) to NetCDF, CfRadial2 and Zarr files">
            <input type="checkbox" bind:checked={includeDerived} disabled={!canExport} />
            Include {derivedCount} derived field{derivedCount === 1 ? '' : 's'} (cleaned, …)
          </label>
          <label class="checkbox-label" title="Untick to write only the derived fields">
            <input type="checkbox" bind:checked={includeOriginal} disabled={!canExport || !includeDerived} />
            Include original moments
          </label>
        </div>
      {/if}

      <div class="format-buttons">
        <button class="fmt-btn data-btn" on:click={() => doDataExport('netcdf')} disabled={!canExport} title="Export as CfRadial1 NetCDF">
          NetCDF
        </button>
        <button class="fmt-btn data-btn" on:click={() => doDataExport('cfradial2')} disabled={!canExport} title="Export as CfRadial2">
          CfRadial2
        </button>
        <button class="fmt-btn data-btn" on:click={() => doDataExport('csv')} disabled={!canExport} title="Export sweep as CSV table">
          CSV
        </button>
        <button class="fmt-btn data-btn" on:click={() => doDataExport('zarr')} disabled={!canExport} title="Export as Zarr">
          Zarr
        </button>
      </div>
    </div>

    <!-- ANIMATION EXPORT -->
    <div class="export-section">
      <div class="section-header">Animation</div>
      <p class="section-desc">Animated GIF of sweep sequence for {$selectedVariable ?? 'none'}</p>

      <div class="anim-controls">
        <div class="field inline">
          <label for="anim-start">Start Sweep</label>
          <input id="anim-start" type="number" min="0" max={$radarData.sweeps.length - 1} bind:value={animSweepStart} disabled={!canExport} />
        </div>
        <div class="field inline">
          <label for="anim-end">End Sweep</label>
          <input id="anim-end" type="number" min="0" max={$radarData.sweeps.length - 1} bind:value={animSweepEnd} disabled={!canExport} />
        </div>
        <div class="field inline">
          <label for="anim-rate">Frame (ms)</label>
          <input id="anim-rate" type="range" min="100" max="2000" step="50" bind:value={animFrameRate} disabled={!canExport} />
          <span class="range-value">{animFrameRate}</span>
        </div>
      </div>

      <button class="primary action-btn anim-btn" on:click={doAnimationExport} disabled={!canExport}>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
          <polygon points="5 3 19 12 5 21 5 3"/>
        </svg>
        Export GIF ({animSweepEnd - animSweepStart + 1} frames)
      </button>
    </div>
  {:else}
    <p class="no-data-msg">Load a file to enable export</p>
  {/if}
</CollapsiblePanel>

<style>
  .export-section {
    padding: var(--spacing-sm) 0;
    border-bottom: 1px solid var(--border-color);
  }

  .export-section:last-child {
    border-bottom: none;
  }

  .section-header {
    font-size: 11px;
    font-weight: 600;
    color: var(--text-accent);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    margin-bottom: var(--spacing-xs);
  }

  .section-desc {
    font-size: 11px;
    color: var(--text-muted);
    margin-bottom: var(--spacing-sm);
    line-height: 1.4;
  }

  /* Format buttons row */
  .format-buttons {
    display: flex;
    gap: var(--spacing-xs);
    margin-bottom: var(--spacing-sm);
  }

  .fmt-btn {
    flex: 1;
    padding: var(--spacing-xs) var(--spacing-sm);
    font-size: 11px;
    font-weight: 600;
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    background: var(--bg-secondary);
    color: var(--text-primary);
    cursor: pointer;
    transition: all var(--transition-fast);
    text-align: center;
  }

  .fmt-btn:hover:not(:disabled) {
    background: var(--accent-primary);
    border-color: var(--accent-primary);
    color: var(--c-on-accent);
  }

  .fmt-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .fmt-btn.data-btn {
    font-size: 10px;
  }

  /* Quick export options */
  .quick-options {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  /* Checkbox grid */
  .checkbox-grid {
    display: flex;
    flex-wrap: wrap;
    gap: var(--spacing-xs) var(--spacing-sm);
    max-height: 100px;
    overflow-y: auto;
    padding: var(--spacing-xs);
    background: var(--bg-primary);
    border-radius: var(--radius-sm);
    border: 1px solid var(--border-color);
  }

  .checkbox-label {
    display: flex;
    align-items: center;
    gap: 3px;
    font-size: 11px;
    color: var(--text-secondary);
    cursor: pointer;
    white-space: nowrap;
  }

  .checkbox-label.select-all {
    color: var(--text-accent);
    font-weight: 600;
  }

  .checkbox-label input[type="checkbox"] {
    margin: 0;
    width: 12px;
    height: 12px;
    accent-color: var(--accent-primary);
  }

  .checkbox-row {
    display: flex;
    gap: var(--spacing-md);
    flex-wrap: wrap;
  }

  /* Fields */
  .field {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
    margin-bottom: var(--spacing-xs);
  }

  .field.inline {
    flex-direction: row;
    align-items: center;
    gap: var(--spacing-sm);
  }

  .field label,
  .field .field-label {
    font-size: 10px;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.03em;
    min-width: 60px;
    flex-shrink: 0;
  }

  .field select,
  .field input[type="text"],
  .field input[type="number"] {
    flex: 1;
    min-width: 0;
  }

  .field input[type="range"] {
    flex: 1;
    min-width: 60px;
  }

  .range-value {
    font-size: 10px;
    color: var(--text-muted);
    font-family: var(--font-mono);
    min-width: 28px;
    text-align: right;
  }

  /* Batch & animation controls */
  .batch-controls,
  .anim-controls {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
    margin-bottom: var(--spacing-sm);
  }

  /* Action buttons */
  .action-btn {
    width: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--spacing-xs);
  }

  .anim-btn {
    margin-top: var(--spacing-xs);
  }

  /* Progress bar */
  .progress-section {
    margin-bottom: var(--spacing-md);
  }

  .progress-bar-track {
    width: 100%;
    height: 4px;
    background: var(--bg-primary);
    border-radius: 2px;
    overflow: hidden;
    margin-bottom: var(--spacing-xs);
  }

  .progress-bar-fill {
    height: 100%;
    background: var(--accent-primary);
    border-radius: 2px;
    transition: width 300ms ease;
  }

  .progress-label {
    font-size: 10px;
    color: var(--text-muted);
    font-style: italic;
  }

  /* No data */
  .no-data-msg {
    font-size: 12px;
    color: var(--text-muted);
    text-align: center;
    padding: var(--spacing-md) 0;
  }

  .browse-btn {
    flex-shrink: 0;
    padding: 2px 8px;
    font-size: 11px;
  }
</style>
