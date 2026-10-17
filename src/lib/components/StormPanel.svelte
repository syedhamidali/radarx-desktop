<script lang="ts">
  /**
   * StormPanel — control panel for storm cell identification and tracking.
   *
   * Provides threshold controls, an "Identify Cells" button, a results table,
   * and cell selection for highlighting on the PPI.
   */
  import { connectionStatus, radarData, selectedVariable, selectedSweep } from '../stores/radarData';
  import { stormCells, clearStormCells, type CellData } from '../stores/stormCells';
  import { runFeatureRequest, type FeatureRequestHandle } from '../utils/featureRequest';
  import { addToast } from '../stores/toasts';
  import { onDestroy, untrack } from 'svelte';
  import CollapsiblePanel from './CollapsiblePanel.svelte';

  // Threshold controls
  let thresholdDbz: number | null = 35;
  let minAreaKm2: number | null = 10;
  // Tracking: the time between the two scans being compared sets the speed
  let dtSeconds: number | null = 300;
  let maxMoveKm: number | null = 30;

  // State
  let cells: CellData[] = [];
  let tracks: import('../stores/stormCells').TrackData[] = [];
  let selectedId: string | null = null;
  let overlayVisible: boolean = true;
  let isIdentifying: boolean = false;
  let hasData: boolean = false;
  let isConnected: boolean = false;
  let variable: string | null = null;
  let sweep: number = 0;
  let filePath: string | null = null;
  let source: import('../stores/stormCells').CellSource | null = null;

  $: cells = $stormCells.cells;
  $: tracks = $stormCells.tracks;
  $: selectedId = $stormCells.selectedCellId;
  $: overlayVisible = $stormCells.overlayVisible;
  $: isIdentifying = $stormCells.isIdentifying;
  $: source = $stormCells.source;
  $: hasData = $radarData.variables.length > 0;
  $: filePath = $radarData.filePath;
  $: isConnected = $connectionStatus === 'connected';
  $: sweep = $selectedSweep;

  const isReflectivity = (v: string) => /^(dbz|zh$|zv$|refl)/i.test(v);

  /** Cells are found on reflectivity (the thresholds are dBZ): the viewed field when it is one, else the file's. */
  // (spelled out in the statement: `$:` only re-runs for what the statement itself names)
  $: sweepVariables = $radarData.sweeps?.[sweep]?.variables ?? $radarData.variables;
  $: variable =
    $selectedVariable && isReflectivity($selectedVariable) && sweepVariables.includes($selectedVariable)
      ? $selectedVariable
      : (sweepVariables.find(isReflectivity) ?? null);

  // Threshold sanity (the number boxes read as null when emptied).
  $: thresholdProblem =
    thresholdDbz === null || !Number.isFinite(thresholdDbz) || thresholdDbz < 0 || thresholdDbz > 100
      ? 'Min dBZ must be between 0 and 100'
      : minAreaKm2 === null || !Number.isFinite(minAreaKm2) || minAreaKm2 <= 0
        ? 'Min area must be above 0'
        : '';
  $: trackProblem =
    dtSeconds === null || !Number.isFinite(dtSeconds) || dtSeconds <= 0
      ? 'The time between scans must be above 0'
      : maxMoveKm === null || !Number.isFinite(maxMoveKm) || maxMoveKm <= 0
        ? 'Max movement must be above 0'
        : '';

  $: blockedReason = !variable
    ? `No reflectivity field in sweep ${sweep}`
    : thresholdProblem;
  $: canIdentify = hasData && isConnected && !isIdentifying && !blockedReason;
  $: canTrack = canIdentify && cells.length > 0 && !trackProblem;

  // Cells belong to one file and sweep: a different file drops them, a different sweep hides them.
  let lastFile: string | null | undefined = undefined;
  $: {
    const f = filePath;
    untrack(() => {
      if (lastFile !== undefined && f !== lastFile) clearStormCells();
      lastFile = f;
    });
  }
  $: onOtherSweep = !!source && cells.length > 0 && (source.file !== filePath || source.sweep !== sweep);

  let handle: FeatureRequestHandle | null = null;

  onDestroy(() => handle?.cancel());

  function setBusy(v: boolean) {
    stormCells.update((s) => ({ ...s, isIdentifying: v }));
  }

  function identifyCells() {
    if (!canIdentify || !variable) return;
    const v = variable;
    const sw = sweep;
    const file = filePath;
    setBusy(true);
    const h = runFeatureRequest({
      kind: 'storm',
      label: `Identifying storm cells (${v}, sweep ${sw})`,
      message: {
        type: 'identify_cells',
        variable: v,
        sweep: sw,
        params: {
          threshold_dbz: thresholdDbz,
          min_size_km2: minAreaKm2,
        },
      },
      resultTypes: ['cells_identified'],
      accept: (msg: any) => msg.variable === v && msg.sweep === sw,
      onResult: (msg: any) => {
        const cellList: CellData[] = msg.cells ?? [];
        stormCells.update((s) => ({
          ...s,
          cells: cellList,
          tracks: [],
          selectedCellId: null,
          source: { file, variable: v, sweep: sw },
        }));
        if (cellList.length > 0) {
          addToast('success', `Found ${cellList.length} storm cells`);
        } else {
          addToast('info', `No storm cells found at ${thresholdDbz} dBZ / ${minAreaKm2} km\u00B2 in sweep ${sw}; try a lower threshold`);
        }
      },
      onSettled: () => {
        setBusy(false);
        if (handle === h) handle = null;
      },
      retry: identifyCells,
    });
    handle = h;
  }

  function trackCells() {
    if (!canTrack || !variable) return;
    const v = variable;
    const sw = sweep;
    const file = filePath;
    setBusy(true);
    const h = runFeatureRequest({
      kind: 'storm',
      label: `Tracking storm cells (${v}, sweep ${sw})`,
      message: {
        type: 'track_cells',
        variable: v,
        sweep: sw,
        params: {
          threshold_dbz: thresholdDbz,
          min_size_km2: minAreaKm2,
          max_distance_km: maxMoveKm,
          dt_seconds: dtSeconds,
        },
      },
      resultTypes: ['cells_tracked'],
      accept: (msg: any) => msg.variable === v && msg.sweep === sw,
      onResult: (msg: any) => {
        const newTracks = msg.tracks ?? [];
        stormCells.update((s) => ({
          ...s,
          cells: msg.cells ?? [],
          tracks: newTracks,
          selectedCellId: null,
          source: { file, variable: v, sweep: sw },
        }));
        const matched = newTracks.filter((t: any) => (t.cell_history?.length ?? 0) > 1).length;
        addToast(matched > 0 ? 'success' : 'info', `Tracked ${matched} of ${newTracks.length} cells (matched between the two scans)`);
      },
      onSettled: () => {
        setBusy(false);
        if (handle === h) handle = null;
      },
      retry: trackCells,
    });
    handle = h;
  }

  function toggleOverlay() {
    stormCells.update((s) => ({ ...s, overlayVisible: !s.overlayVisible }));
  }

  function selectCell(id: string) {
    stormCells.update((s) => ({
      ...s,
      selectedCellId: s.selectedCellId === id ? null : id,
    }));
  }

  function onRowKey(e: KeyboardEvent, id: string) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      selectCell(id);
    }
  }

  function clearCells() {
    handle?.cancel();
    clearStormCells();
  }

  $: matchedTracks = tracks.filter((t) => (t.cell_history?.length ?? 0) > 1);
</script>

<CollapsiblePanel title="Storm Cells" badge={cells.length > 0 ? cells.length : null}>
  {#if hasData}
    <div class="storm-controls">
      <!-- Threshold inputs -->
      <div class="field">
        <label for="threshold-dbz">Min dBZ</label>
        <input
          id="threshold-dbz"
          type="number"
          bind:value={thresholdDbz}
          min="0"
          max="100"
          step="5"
          disabled={isIdentifying}
        />
      </div>

      <div class="field">
        <label for="min-area">Min Area (km&sup2;)</label>
        <input
          id="min-area"
          type="number"
          bind:value={minAreaKm2}
          min="1"
          max="1000"
          step="5"
          disabled={isIdentifying}
        />
      </div>

      <!-- Action buttons -->
      <div class="button-row">
        <button
          class="primary action-btn"
          on:click={identifyCells}
          disabled={!canIdentify}
          title={!isConnected ? 'Sidecar not connected' : !hasData ? 'No data loaded' : blockedReason || `Find storm cells in ${variable}, sweep ${sweep}`}
        >
          {#if isIdentifying}
            <span class="spinner"></span>
            Identifying...
          {:else}
            Identify Cells
          {/if}
        </button>

        {#if cells.length > 0}
          <button
            class="action-btn secondary"
            on:click={trackCells}
            disabled={!canTrack}
            title={trackProblem || 'Match the cells found earlier with the cells of the displayed sweep'}
          >
            Track
          </button>
        {/if}
        {#if isIdentifying}
          <button class="action-btn secondary" on:click={() => handle?.cancel()} title="Stop waiting for the result">Cancel</button>
        {/if}
      </div>

      {#if blockedReason && hasData}
        <p class="note" role="status">{blockedReason}</p>
      {/if}

      {#if cells.length > 0}
        <div class="track-fields">
          <div class="field">
            <label for="track-dt" title="Time between the two scans being compared">Scan gap (s)</label>
            <input id="track-dt" type="number" bind:value={dtSeconds} min="1" step="30" disabled={isIdentifying} />
          </div>
          <div class="field">
            <label for="track-dist" title="A cell is matched only if it moved less than this">Max move (km)</label>
            <input id="track-dist" type="number" bind:value={maxMoveKm} min="1" step="5" disabled={isIdentifying} />
          </div>
        </div>
        <p class="note">
          Track matches the cells found earlier with those of the displayed sweep; the speeds follow the time you give.
        </p>
      {/if}

      <!-- Overlay toggle + clear -->
      {#if onOtherSweep}
        <p class="note" role="status">
          These cells are from {source?.file !== filePath ? 'another file' : `sweep ${source?.sweep}`}; they are drawn on the PPI only there.
        </p>
      {/if}

      {#if cells.length > 0}
        <div class="toggle-row">
          <label class="checkbox-label">
            <input type="checkbox" checked={overlayVisible} on:change={toggleOverlay} />
            <span>Show overlay</span>
          </label>
          <button class="clear-btn" on:click={clearCells} title="Clear all cells">
            Clear
          </button>
        </div>
      {/if}
    </div>

    <!-- Results table -->
    {#if cells.length > 0}
      <div class="cells-table-wrapper">
        <table class="cells-table">
          <thead>
            <tr>
              <th>ID</th>
              <th title="Maximum reflectivity (dBZ)">Max</th>
              <th title="Mean reflectivity (dBZ)">Mean</th>
              <th title="Area (km&sup2;)">km&sup2;</th>
              <th title="Range of the centroid from the radar (km)">Rng km</th>
            </tr>
          </thead>
          <tbody>
            {#each cells as cell (cell.id)}
              <!-- svelte-ignore a11y_click_events_have_key_events -->
              <tr
                class="cell-row"
                class:selected={cell.id === selectedId}
                on:click={() => selectCell(cell.id)}
                on:keydown={(e) => onRowKey(e, cell.id)}
                role="button"
                tabindex="0"
                aria-pressed={cell.id === selectedId}
              >
                <td class="cell-id-col">{cell.id}</td>
                <td class="mono">{cell.max_dbz.toFixed(1)}</td>
                <td class="mono">{cell.mean_dbz.toFixed(1)}</td>
                <td class="mono">{cell.area_km2.toFixed(1)}</td>
                <td class="mono">{(cell.centroid_range / 1000).toFixed(1)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}

    <!-- Tracks -->
    {#if tracks.length > 0}
      <div class="cells-table-wrapper">
        <table class="cells-table">
          <thead>
            <tr><th>Track</th><th>Speed</th><th>From</th><th>Scans</th></tr>
          </thead>
          <tbody>
            {#each tracks as t (t.track_id)}
              {@const matched = (t.cell_history?.length ?? 0) > 1}
              <tr>
                <td class="cell-id-col">{t.track_id}</td>
                <td class="mono">{matched ? `${t.speed_mps.toFixed(1)} m/s (${(t.speed_mps * 3.6).toFixed(0)} km/h)` : '--'}</td>
                <td class="mono">{matched && t.speed_mps > 0 ? `${t.direction_deg.toFixed(0)}\u00B0` : '--'}</td>
                <td class="mono">{matched ? 'matched' : t.track_id.startsWith('TN') ? 'new' : 'ended'}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      {#if matchedTracks.length === 0}
        <p class="note">No cell was found again within the maximum movement.</p>
      {/if}
    {/if}

    <!-- Selected cell detail -->
    {#if selectedId}
      {@const selected = cells.find((c) => c.id === selectedId)}
      {#if selected}
        <div class="cell-detail">
          <div class="detail-header">{selected.id} Detail</div>
          <div class="detail-grid">
            <span class="detail-label">Max dBZ</span>
            <span class="detail-value">{selected.max_dbz.toFixed(1)}</span>
            <span class="detail-label">Mean dBZ</span>
            <span class="detail-value">{selected.mean_dbz.toFixed(1)}</span>
            <span class="detail-label">Area</span>
            <span class="detail-value">{selected.area_km2.toFixed(1)} km&sup2;</span>
            <span class="detail-label">Centroid Az</span>
            <span class="detail-value">{selected.centroid_az.toFixed(1)}&deg;</span>
            <span class="detail-label">Centroid Range</span>
            <span class="detail-value">{(selected.centroid_range / 1000).toFixed(1)} km</span>
            <span class="detail-label">Boundary pts</span>
            <span class="detail-value">{selected.boundary_points.length}</span>
          </div>
        </div>
      {/if}
    {/if}
  {:else}
    <p class="empty-hint">Open a radar file to identify storm cells</p>
  {/if}
</CollapsiblePanel>

<style>
  .storm-controls {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm, 6px);
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .field label {
    font-size: 11px;
    color: var(--text-muted, #6b6b80);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }

  .field input[type='number'] {
    width: 100%;
    padding: 4px 8px;
    font-family: var(--font-mono, monospace);
    font-size: 12px;
    background: var(--bg-primary, #0f0f1a);
    border: 1px solid var(--border-color, #2a2a3a);
    border-radius: var(--radius-sm, 4px);
    color: var(--text-primary, #e8e8ee);
  }

  .field input:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .button-row {
    display: flex;
    gap: var(--spacing-sm, 6px);
    margin-top: var(--spacing-xs, 4px);
  }

  .action-btn {
    flex: 1;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    padding: 6px 12px;
    font-size: 12px;
    font-weight: 600;
    border-radius: var(--radius-sm, 4px);
    cursor: pointer;
    transition: background 150ms ease, opacity 150ms ease;
    border: 1px solid var(--border-color, #2a2a3a);
    color: var(--text-primary, #e8e8ee);
    background: var(--bg-surface, #1a1a2e);
  }

  .action-btn.primary {
    background: var(--accent-primary, #5b6cf7);
    border-color: var(--accent-primary, #5b6cf7);
    color: #fff;
  }

  .action-btn.primary:hover:not(:disabled) {
    background: var(--accent-hover, #7080ff);
  }

  .action-btn.secondary:hover:not(:disabled) {
    background: var(--bg-hover, #24243a);
  }

  .action-btn:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }

  .spinner {
    width: 12px;
    height: 12px;
    border: 2px solid rgba(255, 255, 255, 0.3);
    border-top-color: #fff;
    border-radius: 50%;
    animation: spin 600ms linear infinite;
    display: inline-block;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  .toggle-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-top: var(--spacing-xs, 4px);
  }

  .checkbox-label {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: var(--text-secondary, #a0a0b8);
    cursor: pointer;
  }

  .clear-btn {
    padding: 2px 8px;
    font-size: 11px;
    color: var(--text-muted, #6b6b80);
    background: transparent;
    border: 1px solid var(--border-color, #2a2a3a);
    border-radius: 3px;
    cursor: pointer;
    transition: color 150ms ease, border-color 150ms ease;
  }

  .clear-btn:hover {
    color: var(--accent-danger, #ff4060);
    border-color: var(--accent-danger, #ff4060);
  }

  /* Table */
  .cells-table-wrapper {
    margin-top: var(--spacing-sm, 6px);
    max-height: 200px;
    overflow: auto;
    border: 1px solid var(--border-color, #2a2a3a);
    border-radius: var(--radius-sm, 4px);
  }

  .cells-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 11px;
  }

  .cells-table th {
    position: sticky;
    top: 0;
    padding: 4px 6px;
    text-align: left;
    font-weight: 600;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted, #6b6b80);
    background: var(--bg-tertiary, #161625);
    border-bottom: 1px solid var(--border-color, #2a2a3a);
    white-space: nowrap;
  }

  .cells-table td {
    padding: 3px 6px;
    border-bottom: 1px solid var(--border-color, #2a2a3a);
    color: var(--text-secondary, #a0a0b8);
  }

  .cell-row {
    cursor: pointer;
    transition: background 100ms ease;
  }

  .cell-row:hover {
    background: var(--bg-hover, #24243a);
  }

  .cell-row.selected {
    background: var(--bg-active, #1e1e40);
  }

  .cell-row.selected td {
    color: var(--text-primary, #e8e8ee);
  }

  .cell-id-col {
    font-weight: 700;
    color: var(--accent-primary, #5b6cf7) !important;
  }

  .mono {
    font-family: var(--font-mono, monospace);
    font-variant-numeric: tabular-nums;
  }

  /* Detail card */
  .cell-detail {
    margin-top: var(--spacing-sm, 6px);
    padding: var(--spacing-sm, 6px);
    border: 1px solid var(--border-color, #2a2a3a);
    border-radius: var(--radius-sm, 4px);
    background: var(--bg-primary, #0f0f1a);
  }

  .detail-header {
    font-size: 11px;
    font-weight: 700;
    color: var(--accent-primary, #5b6cf7);
    margin-bottom: var(--spacing-xs, 4px);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .detail-grid {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 2px 8px;
    font-size: 11px;
  }

  .detail-label {
    color: var(--text-muted, #6b6b80);
    white-space: nowrap;
  }

  .detail-value {
    font-family: var(--font-mono, monospace);
    color: var(--text-primary, #e8e8ee);
    text-align: right;
  }

  .note {
    margin: 0;
    font-size: 11px;
    line-height: 1.4;
    color: var(--text-secondary, #a0a0b8);
  }

  .track-fields {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--spacing-sm, 6px);
  }

  .empty-hint {
    font-size: 12px;
    color: var(--text-muted, #6b6b80);
    text-align: center;
    padding: var(--spacing-md, 12px) 0;
  }
</style>
