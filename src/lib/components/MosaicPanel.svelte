<script lang="ts">
  /**
   * MosaicPanel — merge several open radar files (or just one) into a georeferenced
   * mosaic that is drawn on the map (see MapBackground / RadarViewer "Mosaic view").
   */
  import { connectionStatus } from '../stores/radarData';
  import { openFiles } from '../stores/fileManager';
  import {
    mosaicSettings,
    mosaicFileRows,
    mosaicSelectedIds,
    mosaicVariableChoices,
    mosaicRun,
    mosaicResult,
    mosaicDisplay,
    mosaicExporting,
    RADAR_COLORS,
    toggleMosaicFile,
    selectAllMosaicFiles,
    updateMosaicSettings,
    resetMosaicSettings,
    runMosaic,
    cancelMosaicRun,
    clearMosaic,
    exportMosaic,
    fitMapToMosaic,
    formatPercent,
    formatTimeMs,
    parseTimeMs,
    inTauri,
    type MosaicProduct,
    type MosaicSettings,
    type MosaicResult,
  } from '../stores/mosaic';
  import CollapsiblePanel from './CollapsiblePanel.svelte';

  // ── Derived state ────────────────────────────────────────────────────────
  const isConnected = $derived($connectionStatus === 'connected');
  const rows = $derived($mosaicFileRows);
  const selectedCount = $derived($mosaicSelectedIds.length);
  const single = $derived(selectedCount <= 1);
  const s = $derived($mosaicSettings);
  const run = $derived($mosaicRun);
  const res = $derived($mosaicResult);
  const anyWarnings = $derived(rows.some((r) => r.selected && r.warnings.length > 0));
  const desktop = inTauri();

  const blockedReason = $derived.by((): string | null => {
    if (!isConnected) return 'Sidecar not connected';
    if ($openFiles.length === 0) return 'Open at least one radar file first';
    if (selectedCount === 0) return 'Select at least one file';
    if ($mosaicVariableChoices.length === 0) return 'The selected files share no variable';
    return null;
  });

  let showAdvanced = $state(false);

  const PRODUCTS: { value: MosaicProduct; label: string; hint: string }[] = [
    { value: 'composite', label: 'Composite (column max)', hint: 'Maximum over all heights' },
    { value: 'cappi', label: 'CAPPI (constant altitude)', hint: 'Value at a fixed height above sea level' },
    { value: 'echo_top', label: 'Echo top', hint: 'Highest height with the value above the threshold' },
  ];
  const RESOLUTIONS = [250, 500, 1000, 2000, 3000, 5000];

  // ── Input helpers ────────────────────────────────────────────────────────
  function setNum(
    key: keyof MosaicSettings,
    raw: string,
    min: number,
    max: number,
    el: HTMLInputElement | null = null,
  ) {
    const n = Number(raw);
    if (raw.trim() === '' || !Number.isFinite(n)) {
      if (el) el.value = String($mosaicSettings[key] as number);
      return;
    }
    const v = Math.min(max, Math.max(min, n));
    updateMosaicSettings({ [key]: v } as Partial<MosaicSettings>);
    if (el) el.value = String(v);
  }

  function setOptionalNum(key: 'vmin' | 'vmax', raw: string) {
    const n = Number(raw);
    updateMosaicSettings({ [key]: raw.trim() === '' || !Number.isFinite(n) ? null : n } as Partial<MosaicSettings>);
  }

  function onNum(key: keyof MosaicSettings, min: number, max: number) {
    return (e: Event) => setNum(key, (e.currentTarget as HTMLInputElement).value, min, max, e.currentTarget as HTMLInputElement);
  }

  // ── Result formatting ────────────────────────────────────────────────────
  function legendGradient(r: MosaicResult): string {
    const span = r.vmax - r.vmin || 1;
    const stops = [...(r.legend ?? [])].sort((a, b) => a[0] - b[0]);
    if (stops.length === 0) return 'linear-gradient(90deg, #333, #ddd)';
    return `linear-gradient(90deg, ${stops
      .map(([v, c]) => `${c} ${Math.max(0, Math.min(100, ((v - r.vmin) / span) * 100)).toFixed(1)}%`)
      .join(', ')})`;
  }

  function fmt(n: number | null | undefined, d = 1): string {
    return n === null || n === undefined || !Number.isFinite(n) ? '–' : n.toFixed(d);
  }

  function fmtBias(b: number | null): string {
    if (b === null || b === undefined || !Number.isFinite(b)) return '–';
    return `${b > 0 ? '+' : ''}${b.toFixed(1)}`;
  }

  function fmtVolume(t: string | null): string {
    if (!t) return '–';
    const ms = parseTimeMs(t);
    return ms === null ? t : formatTimeMs(ms).replace(' UTC', 'Z').slice(5);
  }

  function productLabel(r: MosaicResult): string {
    const p = r.product === 'cappi' ? `CAPPI ${r.height_m != null ? (r.height_m / 1000).toFixed(1) + ' km' : ''}`
      : r.product === 'echo_top' ? 'Echo top' : 'Composite';
    return `${p} · ${r.variable}`;
  }

  function onRun() {
    runMosaic();
  }
</script>

<CollapsiblePanel title="Mosaic" icon="🗺" badge={selectedCount > 0 ? selectedCount : null}>
  <div class="mosaic-root">
    <!-- ── Files ─────────────────────────────────────────────────────────── -->
    <section class="block">
      <div class="block-head">
        <h4>Radars</h4>
        {#if rows.length > 1}
          <span class="spacer"></span>
          <button type="button" class="mini-btn" onclick={() => selectAllMosaicFiles(true)}>All</button>
          <button type="button" class="mini-btn" onclick={() => selectAllMosaicFiles(false)}>None</button>
        {/if}
      </div>
      {#if rows.length === 0}
        <p class="empty-hint">Open one or more radar files (or fetch them from Cloud Data) to build a mosaic.</p>
      {:else}
        <ul class="file-list">
          {#each rows as r (r.id)}
            <li class="file-row" class:off={!r.selected}>
              <label class="file-main">
                <input type="checkbox" checked={r.selected} onchange={() => toggleMosaicFile(r.id)} />
                <span class="file-text">
                  <span class="file-name" title={r.filename}>{r.name}</span>
                  <span class="file-meta mono">
                    {#if r.lat !== null && r.lon !== null}{r.lat.toFixed(2)}, {r.lon.toFixed(2)}{:else}no location{/if}
                    · {r.timeLabel}
                  </span>
                </span>
              </label>
              {#if r.selected && r.warnings.length > 0}
                <div class="chips">
                  {#each r.warnings as w}
                    <span class="chip warn" title={w}>&#9888; {w}</span>
                  {/each}
                </div>
              {/if}
            </li>
          {/each}
        </ul>
        {#if anyWarnings}
          <p class="help warn-text">
            Radars scanned at different times or listed twice blend poorly. Consider deselecting the odd file out.
          </p>
        {/if}
      {/if}
    </section>

    <!-- ── Product / grid ────────────────────────────────────────────────── -->
    <section class="block">
      <h4>Product</h4>
      <div class="grid2">
        <label class="field">
          <span>Variable</span>
          <select
            value={s.variable}
            disabled={$mosaicVariableChoices.length === 0}
            onchange={(e) => updateMosaicSettings({ variable: e.currentTarget.value })}
          >
            {#each $mosaicVariableChoices as v}
              <option value={v}>{v}</option>
            {/each}
            {#if $mosaicVariableChoices.length === 0}<option value="">–</option>{/if}
          </select>
        </label>
        <label class="field">
          <span>Product</span>
          <select
            value={s.product}
            onchange={(e) => updateMosaicSettings({ product: e.currentTarget.value as MosaicProduct })}
            title={PRODUCTS.find((p) => p.value === s.product)?.hint}
          >
            {#each PRODUCTS as p}
              <option value={p.value}>{p.label}</option>
            {/each}
          </select>
        </label>
        {#if s.product === 'cappi'}
          <label class="field">
            <span>Height (m)</span>
            <input type="number" min="0" max="20000" step="500" value={s.heightM} onchange={onNum('heightM', 0, 20000)} />
          </label>
        {:else if s.product === 'echo_top'}
          <label class="field">
            <span>Threshold (dBZ)</span>
            <input type="number" min="-30" max="70" step="1" value={s.thresholdDbz} onchange={onNum('thresholdDbz', -30, 70)} />
          </label>
        {/if}
        <label class="field">
          <span>Resolution</span>
          <select
            value={String(s.resolutionM)}
            onchange={(e) => updateMosaicSettings({ resolutionM: Number(e.currentTarget.value) })}
          >
            {#each RESOLUTIONS as r}
              <option value={String(r)}>{r >= 1000 ? r / 1000 + ' km' : r + ' m'}</option>
            {/each}
            {#if !RESOLUTIONS.includes(s.resolutionM)}
              <option value={String(s.resolutionM)}>{s.resolutionM} m</option>
            {/if}
          </select>
        </label>
      </div>
      <div class="field">
        <span>Extent</span>
        <div class="seg" role="group" aria-label="Mosaic extent">
          <button
            type="button"
            class="seg-btn"
            class:on={s.extentMode === 'auto'}
            onclick={() => updateMosaicSettings({ extentMode: 'auto' })}
            title="Bounding box of all radar coverage circles"
          >Auto (all radars)</button>
          <button
            type="button"
            class="seg-btn"
            class:on={s.extentMode === 'view'}
            onclick={() => updateMosaicSettings({ extentMode: 'view' })}
            title="Use the part of the map that is visible now"
          >Current map view</button>
        </div>
      </div>
    </section>

    <!-- ── Merge ─────────────────────────────────────────────────────────── -->
    <section class="block">
      <div class="block-head">
        <h4>Merge</h4>
        <span class="spacer"></span>
        {#if single}<span class="chip">single radar</span>{/if}
      </div>
      {#if single}
        <p class="help">One radar selected: the mosaic is a re-gridded map of that radar, nothing to blend.</p>
      {:else}
        <label class="check">
          <input
            type="checkbox"
            checked={s.mergeEnabled}
            onchange={(e) => updateMosaicSettings({ mergeEnabled: e.currentTarget.checked })}
          />
          <span>Weighted blend in overlaps <span class="muted">(off = nearest radar wins)</span></span>
        </label>
        {#if s.mergeEnabled}
          <div class="grid2">
            <label class="field">
              <span>Range scale (km)</span>
              <input type="number" min="1" max="500" step="5" value={s.rangeScaleKm} onchange={onNum('rangeScaleKm', 1, 500)} />
            </label>
            <label class="field">
              <span>Time scale (s)</span>
              <input type="number" min="1" max="7200" step="30" value={s.timeScaleS} onchange={onNum('timeScaleS', 1, 7200)} />
            </label>
            <label class="field">
              <span>Beamwidth (°)</span>
              <input type="number" min="0.1" max="5" step="0.1" value={s.beamwidthDeg} onchange={onNum('beamwidthDeg', 0.1, 5)} />
            </label>
          </div>
        {/if}
        <label class="check">
          <input
            type="checkbox"
            checked={s.calibrate}
            onchange={(e) => updateMosaicSettings({ calibrate: e.currentTarget.checked })}
          />
          <span>Calibrate radar bias in overlaps</span>
        </label>
        {#if s.calibrate}
          <label class="field">
            <span>Reference radar</span>
            <select
              value={s.referenceFileId || $mosaicSelectedIds[0] || ''}
              onchange={(e) => updateMosaicSettings({ referenceFileId: e.currentTarget.value })}
            >
              {#each rows.filter((r) => r.selected) as r (r.id)}
                <option value={r.id}>{r.name}</option>
              {/each}
            </select>
          </label>
        {/if}
        <label class="check">
          <input
            type="checkbox"
            checked={s.advect}
            onchange={(e) => updateMosaicSettings({ advect: e.currentTarget.checked })}
          />
          <span>Advect to a common time <span class="muted">(shift echoes by the volume time offset)</span></span>
        </label>
      {/if}
    </section>

    <!-- ── Advanced ──────────────────────────────────────────────────────── -->
    <section class="block">
      <button type="button" class="disclosure" onclick={() => (showAdvanced = !showAdvanced)} aria-expanded={showAdvanced}>
        <span class="caret" class:open={showAdvanced}>&#9656;</span> Advanced
      </button>
      {#if showAdvanced}
        <div class="grid2">
          <label class="field">
            <span>Max range (km)</span>
            <input type="number" min="10" max="500" step="10" value={s.maxRangeKm} onchange={onNum('maxRangeKm', 10, 500)} />
          </label>
          <label class="field">
            <span>Top height (m)</span>
            <input type="number" min="1000" max="30000" step="500" value={s.zMaxM} onchange={onNum('zMaxM', 1000, 30000)} />
          </label>
          <label class="field">
            <span>Level step (m)</span>
            <input type="number" min="100" max="5000" step="100" value={s.zStepM} onchange={onNum('zStepM', 100, 5000)} />
          </label>
          <span></span>
          <label class="field">
            <span>Colour min</span>
            <input type="number" placeholder="auto" value={s.vmin ?? ''} onchange={(e) => setOptionalNum('vmin', e.currentTarget.value)} />
          </label>
          <label class="field">
            <span>Colour max</span>
            <input type="number" placeholder="auto" value={s.vmax ?? ''} onchange={(e) => setOptionalNum('vmax', e.currentTarget.value)} />
          </label>
        </div>
        <button type="button" class="mini-btn" onclick={resetMosaicSettings}>Reset settings</button>
      {/if}
    </section>

    <!-- ── Run ───────────────────────────────────────────────────────────── -->
    <section class="block run-block">
      {#if run.running}
        <div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow={Math.round(run.percent)}>
          <div class="progress-fill" style="width: {Math.max(4, run.percent)}%"></div>
        </div>
        <div class="progress-row">
          <span class="progress-msg" title={run.message}>{run.message || 'Working…'}</span>
          <span class="mono">{Math.round(run.percent)}%</span>
          <button type="button" class="mini-btn" onclick={cancelMosaicRun} title="Stop waiting; a late result is ignored">
            Cancel
          </button>
        </div>
      {:else}
        <button type="button" class="run-btn" onclick={onRun} disabled={blockedReason !== null} title={blockedReason ?? 'Create the mosaic'}>
          {res ? 'Re-run mosaic' : 'Create mosaic'}
          <span class="muted-on-btn">{selectedCount} radar{selectedCount === 1 ? '' : 's'}</span>
        </button>
        {#if blockedReason}<p class="help">{blockedReason}</p>{/if}
      {/if}
      {#if run.error}
        <div class="error-box" role="alert">
          <span class="error-text">{run.error}</span>
          <button type="button" class="mini-btn" onclick={() => mosaicRun.update((r) => ({ ...r, error: null }))}>Dismiss</button>
        </div>
      {/if}
    </section>

    <!-- ── Result ────────────────────────────────────────────────────────── -->
    {#if res}
      <section class="block result" aria-label="Mosaic result">
        <div class="block-head">
          <h4>{productLabel(res)}</h4>
          <span class="spacer"></span>
          <span class="mono muted">{res.elapsed_ms >= 1000 ? (res.elapsed_ms / 1000).toFixed(1) + ' s' : Math.round(res.elapsed_ms) + ' ms'}</span>
        </div>

        <div class="legend">
          <div class="legend-bar" style="background: {legendGradient(res)}"></div>
          <div class="legend-labels mono">
            <span>{res.vmin}</span>
            <span>{res.units}</span>
            <span>{res.vmax}</span>
          </div>
        </div>

        <dl class="scalars">
          <div class="scalar"><dt>Valid</dt><dd>{formatPercent(res.stats.valid_fraction)}</dd></div>
          <div class="scalar"><dt>Max</dt><dd>{fmt(res.stats.max)}<span class="unit">{res.units}</span></dd></div>
          <div class="scalar"><dt>Mean</dt><dd>{fmt(res.stats.mean)}<span class="unit">{res.units}</span></dd></div>
          <div class="scalar"><dt>Min</dt><dd>{fmt(res.stats.min)}<span class="unit">{res.units}</span></dd></div>
          <div class="scalar"><dt>Overlap</dt><dd>{formatPercent(res.overlap_fraction)}</dd></div>
          <div class="scalar"><dt>Grid</dt><dd>{res.grid.nx}×{res.grid.ny}</dd></div>
        </dl>

        <div class="table-wrap">
          <table class="data-table">
            <thead>
              <tr>
                <th>Radar</th>
                <th class="num">Cover</th>
                <th class="num">Share</th>
                <th class="num">Bias dB</th>
              </tr>
            </thead>
            <tbody>
              {#each res.radars as r, i (r.file_id)}
                <tr title="{r.name} · {r.latitude.toFixed(3)}, {r.longitude.toFixed(3)} · {fmtVolume(r.volume_time)}">
                  <td>
                    <span class="radar-chip" style="background: {RADAR_COLORS[i % RADAR_COLORS.length]}"></span>
                    <span class="radar-name">{r.name}</span>
                    <span class="radar-time mono">{fmtVolume(r.volume_time)}</span>
                  </td>
                  <td class="num mono">{formatPercent(r.coverage_fraction)}</td>
                  <td class="num mono">{formatPercent(r.share)}</td>
                  <td class="num mono">{fmtBias(r.bias_db)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>

        <div class="display-row">
          <label class="check inline">
            <input
              type="checkbox"
              checked={$mosaicDisplay.visible}
              onchange={(e) => mosaicDisplay.update((d) => ({ ...d, visible: e.currentTarget.checked }))}
            />
            <span>Show on map</span>
          </label>
          <label class="check inline">
            <input
              type="checkbox"
              checked={$mosaicDisplay.mosaicView}
              onchange={(e) => mosaicDisplay.update((d) => ({ ...d, mosaicView: e.currentTarget.checked }))}
              title="Hide the PPI so the map with the mosaic fills the view (hover for values)"
            />
            <span>Mosaic view</span>
          </label>
        </div>
        <label class="slider-row">
          <span>Opacity</span>
          <input
            type="range"
            min="0"
            max="100"
            step="1"
            value={Math.round($mosaicDisplay.opacity * 100)}
            oninput={(e) => mosaicDisplay.update((d) => ({ ...d, opacity: Number(e.currentTarget.value) / 100 }))}
          />
          <span class="mono">{Math.round($mosaicDisplay.opacity * 100)}%</span>
        </label>

        <div class="btn-row">
          <button type="button" class="mini-btn" onclick={fitMapToMosaic}>Fit to mosaic</button>
          <span class="spacer"></span>
          <button
            type="button"
            class="mini-btn"
            disabled={!desktop || $mosaicExporting}
            onclick={() => exportMosaic('netcdf')}
            title={desktop ? 'Save the gridded mosaic as NetCDF' : 'Exporting needs the desktop app'}
          >NetCDF</button>
          <button
            type="button"
            class="mini-btn"
            disabled={!desktop || $mosaicExporting}
            onclick={() => exportMosaic('png')}
            title={desktop ? 'Save the mosaic image (PNG)' : 'Exporting needs the desktop app'}
          >PNG</button>
          <button type="button" class="mini-btn danger" onclick={clearMosaic}>Clear</button>
        </div>
        {#if !desktop}<p class="help">Export buttons are available in the desktop app.</p>{/if}
      </section>
    {/if}
  </div>
</CollapsiblePanel>

<style>
  .mosaic-root {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-md, 12px);
  }

  .block {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm, 8px);
  }

  .block-head {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs, 4px);
  }

  h4 {
    margin: 0;
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-muted, #5c6088);
  }

  .result h4 {
    text-transform: none;
    letter-spacing: 0;
    font-size: 12px;
    color: var(--text-primary, #e4e6f4);
  }

  .spacer {
    flex: 1;
  }

  .mono {
    font-family: var(--font-mono, monospace);
    font-variant-numeric: tabular-nums;
  }

  .muted {
    color: var(--text-muted, #5c6088);
  }

  .help {
    margin: 0;
    font-size: 11px;
    color: var(--text-muted, #5c6088);
    line-height: 1.35;
  }

  .warn-text {
    color: var(--accent-warning, #fbbf24);
  }

  .empty-hint {
    margin: 0;
    padding: var(--spacing-sm, 8px) 0;
    font-size: 12px;
    text-align: center;
    color: var(--text-muted, #5c6088);
  }

  .mini-btn {
    height: auto;
    padding: 2px 8px;
    font-size: 11px;
    color: var(--text-secondary, #9da2c2);
    background: transparent;
    border: 1px solid var(--border-light, #2a2a3a);
    border-radius: 3px;
    cursor: pointer;
    white-space: nowrap;
  }

  .mini-btn:hover:not(:disabled) {
    color: var(--text-primary, #e4e6f4);
    border-color: var(--accent-primary, #5b6cf7);
    background: transparent;
  }

  .mini-btn.danger:hover:not(:disabled) {
    color: var(--accent-danger, #f87171);
    border-color: var(--accent-danger, #f87171);
  }

  .mini-btn:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }

  /* Files */
  .file-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
    max-height: 220px;
    overflow-y: auto;
  }

  .file-row {
    padding: 5px 6px;
    background: var(--bg-primary, #080a14);
    border: 1px solid var(--border-color, #2a2a3a);
    border-radius: var(--radius-sm, 4px);
  }

  .file-row.off {
    opacity: 0.55;
  }

  .file-main {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    cursor: pointer;
  }

  .file-main input {
    margin-top: 2px;
    accent-color: var(--accent-primary, #5b6cf7);
  }

  .file-text {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }

  .file-name {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-primary, #e4e6f4);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .file-meta {
    font-size: 10px;
    color: var(--text-muted, #5c6088);
  }

  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin: 4px 0 0 22px;
  }

  .chip {
    display: inline-block;
    padding: 1px 6px;
    font-size: 10px;
    color: var(--text-secondary, #9da2c2);
    border: 1px solid var(--border-light, #2a2a3a);
    border-radius: 9px;
  }

  .chip.warn {
    color: var(--accent-warning, #fbbf24);
    border-color: var(--accent-warning, #fbbf24);
    background: rgba(251, 191, 36, 0.08);
  }

  /* Form */
  .grid2 {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--spacing-sm, 8px);
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 3px;
    min-width: 0;
    font-size: 10px;
    color: var(--text-muted, #5c6088);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .field select,
  .field input {
    width: 100%;
    box-sizing: border-box;
    height: 26px;
    font-size: 12px;
    text-transform: none;
    letter-spacing: 0;
  }

  .check {
    display: flex;
    align-items: flex-start;
    gap: 6px;
    font-size: 12px;
    color: var(--text-secondary, #9da2c2);
    cursor: pointer;
  }

  .check input {
    margin-top: 2px;
    accent-color: var(--accent-primary, #5b6cf7);
  }

  .check.inline {
    align-items: center;
  }

  .seg {
    display: flex;
    border: 1px solid var(--border-light, #2a2a3a);
    border-radius: var(--radius-sm, 4px);
    overflow: hidden;
  }

  .seg-btn {
    flex: 1;
    height: 26px;
    padding: 0 6px;
    font-size: 11px;
    color: var(--text-secondary, #9da2c2);
    background: transparent;
    border: none;
    border-radius: 0;
    text-transform: none;
    letter-spacing: 0;
  }

  .seg-btn + .seg-btn {
    border-left: 1px solid var(--border-light, #2a2a3a);
  }

  .seg-btn.on {
    color: #fff;
    background: var(--accent-primary, #5b6cf7);
  }

  .disclosure {
    align-self: flex-start;
    height: auto;
    padding: 0;
    font-size: 11px;
    font-weight: 600;
    color: var(--text-muted, #5c6088);
    background: transparent;
    border: none;
    box-shadow: none;
    backdrop-filter: none;
  }

  .disclosure:hover {
    color: var(--text-primary, #e4e6f4);
    background: transparent;
    box-shadow: none;
  }

  .caret {
    display: inline-block;
    transition: transform 150ms ease;
  }

  .caret.open {
    transform: rotate(90deg);
  }

  /* Run */
  .run-btn {
    height: 32px;
    gap: 8px;
    font-weight: 600;
    color: #fff;
    background: var(--accent-primary, #5b6cf7);
    border-color: var(--accent-primary, #5b6cf7);
  }

  .run-btn:hover:not(:disabled) {
    background: var(--accent-hover, #7c8cf8);
    border-color: var(--accent-hover, #7c8cf8);
  }

  .muted-on-btn {
    font-size: 10px;
    font-weight: 500;
    opacity: 0.8;
  }

  .progress {
    height: 6px;
    overflow: hidden;
    background: var(--bg-primary, #080a14);
    border: 1px solid var(--border-color, #2a2a3a);
    border-radius: 3px;
  }

  .progress-fill {
    height: 100%;
    background: var(--accent-primary, #5b6cf7);
    transition: width 200ms ease;
  }

  .progress-row {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 11px;
    color: var(--text-secondary, #9da2c2);
  }

  .progress-msg {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .error-box {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 6px 8px;
    font-size: 11px;
    color: var(--accent-danger, #f87171);
    background: rgba(248, 113, 113, 0.08);
    border: 1px solid var(--accent-danger, #f87171);
    border-radius: var(--radius-sm, 4px);
  }

  .error-text {
    flex: 1;
    min-width: 0;
    word-break: break-word;
    user-select: text;
  }

  /* Result */
  .result {
    padding-top: var(--spacing-sm, 8px);
    border-top: 1px solid var(--border-color, #2a2a3a);
  }

  .legend-bar {
    height: 12px;
    border: 1px solid var(--border-light, #2a2a3a);
    border-radius: 2px;
  }

  .legend-labels {
    display: flex;
    justify-content: space-between;
    margin-top: 2px;
    font-size: 10px;
    color: var(--text-muted, #5c6088);
  }

  .scalars {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 4px;
    margin: 0;
  }

  .scalar {
    padding: 4px 6px;
    background: var(--bg-primary, #080a14);
    border: 1px solid var(--border-color, #2a2a3a);
    border-radius: var(--radius-sm, 4px);
    min-width: 0;
  }

  .scalar dt {
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted, #5c6088);
  }

  .scalar dd {
    margin: 0;
    font-size: 12px;
    font-family: var(--font-mono, monospace);
    font-variant-numeric: tabular-nums;
    color: var(--text-primary, #e4e6f4);
    white-space: nowrap;
  }

  .unit {
    margin-left: 3px;
    font-size: 9px;
    color: var(--text-muted, #5c6088);
  }

  .table-wrap {
    max-height: 200px;
    overflow: auto;
    border: 1px solid var(--border-color, #2a2a3a);
    border-radius: var(--radius-sm, 4px);
  }

  .data-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 11px;
  }

  .data-table th {
    position: sticky;
    top: 0;
    padding: 4px 6px;
    font-size: 10px;
    font-weight: 600;
    text-align: left;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted, #5c6088);
    background: var(--bg-tertiary, #111628);
    border-bottom: 1px solid var(--border-light, #2a2a3a);
  }

  .data-table td {
    padding: 3px 6px;
    color: var(--text-secondary, #9da2c2);
    border-bottom: 1px solid var(--border-color, #2a2a3a);
    white-space: nowrap;
  }

  .data-table .num {
    text-align: right;
  }

  .radar-chip {
    display: inline-block;
    width: 9px;
    height: 9px;
    margin-right: 6px;
    border-radius: 50%;
    vertical-align: middle;
  }

  .radar-name {
    color: var(--text-primary, #e4e6f4);
    font-weight: 600;
  }

  .radar-time {
    display: block;
    margin-left: 15px;
    font-size: 9px;
    color: var(--text-muted, #5c6088);
  }

  .display-row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--spacing-md, 12px);
  }

  .slider-row {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 11px;
    color: var(--text-muted, #5c6088);
  }

  .slider-row input[type='range'] {
    flex: 1;
    min-width: 0;
    accent-color: var(--accent-primary, #5b6cf7);
  }

  .btn-row {
    display: flex;
    align-items: center;
    gap: 6px;
  }
</style>
