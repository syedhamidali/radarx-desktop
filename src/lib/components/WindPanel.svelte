<script lang="ts">
  /**
   * WindPanel — multi-Doppler wind retrieval from two or more open radar files.
   * The result is drawn on the 2-D map (speed raster + wind arrows at the chosen height, see
   * MapBackground / RadarViewer) and as a vertical cross-section in this panel.
   */
  import { connectionStatus } from '../stores/radarData';
  import { openFiles } from '../stores/fileManager';
  import { RADAR_COLORS, inTauri } from '../stores/mosaic';
  import {
    windSettings,
    windFileRows,
    windSelectedIds,
    windRun,
    windResult,
    windDisplay,
    windExporting,
    toggleWindFile,
    updateWindSettings,
    resetWindSettings,
    updateWindDisplay,
    runWind,
    cancelWindRun,
    clearWind,
    exportWind,
    fitWindToMap,
    parseProfile,
    type WindSettings,
  } from '../stores/wind';
  import {
    FIELD_STYLES,
    defaultRange,
    drawSection,
    scaleGradient,
    type WindFieldKey,
  } from '../utils/wind';
  import CollapsiblePanel from './CollapsiblePanel.svelte';

  const isConnected = $derived($connectionStatus === 'connected');
  const rows = $derived($windFileRows);
  const selectedCount = $derived($windSelectedIds.length);
  const s = $derived($windSettings);
  const run = $derived($windRun);
  const res = $derived($windResult);
  const disp = $derived($windDisplay);
  const desktop = inTauri();
  let showAdvanced = $state(false);

  const blockedReason = $derived.by((): string | null => {
    if (!isConnected) return 'Sidecar not connected';
    if ($openFiles.length < 2) return 'Open the files of at least two radars first';
    if (selectedCount < 2) return 'Select at least two radars with radial velocity';
    return null;
  });

  const DX = [500, 1000, 2000, 3000, 5000];
  const DZ = [250, 500, 1000];
  const MAP_FIELDS: WindFieldKey[] = ['speed', 'w', 'u', 'v', 'divergence', 'vorticity', 'uncertainty', 'crossing_angle', 'dbz'];
  const SECTION_FIELDS: WindFieldKey[] = ['w', 'speed', 'u', 'v', 'divergence', 'vorticity', 'dbz'];

  function setNum(key: keyof WindSettings, min: number, max: number) {
    return (e: Event) => {
      const el = e.currentTarget as HTMLInputElement;
      const n = Number(el.value);
      if (el.value.trim() === '' || !Number.isFinite(n)) {
        el.value = String($windSettings[key] as number);
        return;
      }
      const v = Math.min(max, Math.max(min, n));
      updateWindSettings({ [key]: v } as Partial<WindSettings>);
      el.value = String(v);
    };
  }

  const profileOk = $derived(s.bgMode !== 'manual' || parseProfile(s.bgProfile) !== null);

  // ── Display ──────────────────────────────────────────────────────────────
  const range = $derived(res ? defaultRange(res, disp.field) : ([0, 1] as [number, number]));
  const vmin = $derived(disp.vmin ?? range[0]);
  const vmax = $derived(disp.vmax !== null && disp.vmax > vmin ? disp.vmax : range[1]);
  const style = $derived(FIELD_STYLES[disp.field]);
  const fieldUnits = $derived(res?.fields.find((f) => f.name === disp.field)?.units ?? '');
  const heightKm = $derived(res ? (res.grid.z[Math.min(disp.level, res.grid.nz - 1)] / 1000).toFixed(2) : '');
  const sectionMax = $derived(res ? (disp.sectionAxis === 'x' ? res.grid.ny : res.grid.nx) - 1 : 0);
  const sectionKm = $derived.by(() => {
    if (!res) return '';
    const g = res.grid;
    const idx = Math.min(disp.sectionIndex, sectionMax);
    const v = disp.sectionAxis === 'x' ? g.y[0] + idx * g.dy : g.x[0] + idx * g.dx;
    return `${(v / 1000).toFixed(0)} km`;
  });

  let sectionCanvas = $state<HTMLCanvasElement | null>(null);
  let sectionWidth = $state(0);
  const sectionRange = $derived(res ? defaultRange(res, disp.sectionField) : ([0, 1] as [number, number]));

  $effect(() => {
    if (!sectionCanvas || !res) return;
    void sectionWidth;
    const idx = Math.min(disp.sectionIndex, sectionMax);
    drawSection(sectionCanvas, res, {
      field: disp.sectionField,
      axis: disp.sectionAxis,
      index: idx,
      vmin: sectionRange[0],
      vmax: sectionRange[1],
      minCrossing: disp.minCrossing,
      maxUncertainty: disp.maxUncertainty,
    });
  });

  function fmt(n: number | null | undefined, d = 1): string {
    return n === null || n === undefined || !Number.isFinite(n) ? '–' : n.toFixed(d);
  }

  function seconds(ms: number): string {
    return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
  }

  function onSectionAxis(axis: 'x' | 'y') {
    if (!res) return updateWindDisplay({ sectionAxis: axis });
    const n = axis === 'x' ? res.grid.ny : res.grid.nx;
    updateWindDisplay({ sectionAxis: axis, sectionIndex: Math.floor(n / 2) });
  }
</script>

<CollapsiblePanel title="Wind (Multi-Doppler)" icon="🌬" badge={selectedCount > 0 ? selectedCount : null}>
  <div class="wind-root">
    <!-- ── Radars ────────────────────────────────────────────────────────── -->
    <section class="block">
      <h4>Radars</h4>
      {#if rows.length === 0}
        <p class="empty-hint">Open the files of two or more radars that scan the same weather at about the same time.</p>
      {:else}
        <ul class="file-list">
          {#each rows as r, i (r.id)}
            <li class="file-row" class:off={!r.selected}>
              <label class="file-main">
                <input type="checkbox" checked={r.selected} disabled={!r.hasVelocity} onchange={() => toggleWindFile(r.id)} />
                <span class="radar-chip" style="background: {RADAR_COLORS[i % RADAR_COLORS.length]}"></span>
                <span class="file-text">
                  <span class="file-name" title={r.filename}>{r.name}</span>
                  <span class="file-meta mono">
                    {#if r.lat !== null && r.lon !== null}{r.lat.toFixed(2)}, {r.lon.toFixed(2)}{:else}no location{/if}
                    · {r.timeLabel}
                  </span>
                </span>
              </label>
              {#if r.warnings.length > 0}
                <div class="chips">
                  {#each r.warnings as w}<span class="chip warn" title={w}>&#9888; {w}</span>{/each}
                </div>
              {/if}
            </li>
          {/each}
        </ul>
        <p class="help">Needs dealiased radial velocity. Run a dealiasing module first if the velocities fold.</p>
      {/if}
    </section>

    <!-- ── Grid ──────────────────────────────────────────────────────────── -->
    <section class="block">
      <h4>Analysis grid</h4>
      <div class="grid2">
        <label class="field">
          <span>Horizontal spacing</span>
          <select value={String(s.dxM)} onchange={(e) => updateWindSettings({ dxM: Number(e.currentTarget.value) })}>
            {#each DX as d}<option value={String(d)}>{d >= 1000 ? d / 1000 + ' km' : d + ' m'}</option>{/each}
          </select>
        </label>
        <label class="field">
          <span>Vertical spacing</span>
          <select value={String(s.dzM)} onchange={(e) => updateWindSettings({ dzM: Number(e.currentTarget.value) })}>
            {#each DZ as d}<option value={String(d)}>{d} m</option>{/each}
          </select>
        </label>
        <label class="field">
          <span>Top height ASL (m)</span>
          <input type="number" min="1000" max="20000" step="500" value={s.zMaxM} onchange={setNum('zMaxM', 1000, 20000)} />
        </label>
        <label class="field">
          <span>Max range (km)</span>
          <input type="number" min="10" max="400" step="10" value={s.maxRangeKm} onchange={setNum('maxRangeKm', 10, 400)} />
        </label>
      </div>
      <p class="help">The area and the lowest level are chosen automatically where two radars see the storm and their beams cross well.</p>
    </section>

    <!-- ── Retrieval ─────────────────────────────────────────────────────── -->
    <section class="block">
      <h4>Retrieval</h4>
      <div class="seg" role="group" aria-label="Method">
        <button type="button" class="seg-btn" class:on={s.method === 'analytic'} onclick={() => updateWindSettings({ method: 'analytic' })}
          title="Classic dual-Doppler solution: fast, uses the data exactly where beams cross well">Analytic</button>
        <button type="button" class="seg-btn" class:on={s.method === 'variational'} onclick={() => updateWindSettings({ method: 'variational' })}
          title="Variational analysis with mass continuity and smoothness; uses a background wind">Variational</button>
      </div>
      <div class="grid2">
        <label class="field">
          <span>Min crossing angle (°)</span>
          <input type="number" min="0" max="90" step="5" value={s.minCrossing} onchange={setNum('minCrossing', 0, 90)} />
        </label>
        <label class="field">
          <span>Vertical boundary</span>
          <select value={s.boundary} onchange={(e) => updateWindSettings({ boundary: e.currentTarget.value as WindSettings['boundary'] })}>
            <option value="upward">Upward (w = 0 at bottom)</option>
            <option value="downward">Downward (w = 0 at top)</option>
            <option value="obrien">O'Brien adjusted</option>
          </select>
        </label>
      </div>
      <label class="check">
        <input type="checkbox" checked={s.fallSpeed} onchange={(e) => updateWindSettings({ fallSpeed: e.currentTarget.checked })} />
        <span>Correct for hydrometeor fall speed <span class="muted">(needs reflectivity)</span></span>
      </label>
      {#if s.fallSpeed}
        <label class="check">
          <input type="checkbox" checked={s.freezingManual} onchange={(e) => updateWindSettings({ freezingManual: e.currentTarget.checked })} />
          <span>Freezing level known <span class="muted">(otherwise the rain law is used at all heights)</span></span>
        </label>
        {#if s.freezingManual}
          <label class="field">
            <span>Freezing level ASL (m)</span>
            <input type="number" min="0" max="8000" step="100" value={s.freezingLevelM} onchange={setNum('freezingLevelM', 0, 8000)} />
          </label>
        {/if}
      {/if}
    </section>

    <!-- ── Background wind ───────────────────────────────────────────────── -->
    <section class="block">
      <h4>Background wind</h4>
      {#if s.method !== 'variational'}
        <p class="help">Only the variational method uses a background wind.</p>
      {:else}
        <label class="field">
          <span>Source</span>
          <select value={s.bgMode} onchange={(e) => updateWindSettings({ bgMode: e.currentTarget.value as WindSettings['bgMode'] })}>
            <option value="none">None</option>
            <option value="vvp">Fitted from the radars (VVP)</option>
            <option value="uniform">Uniform</option>
            <option value="manual">Manual profile</option>
          </select>
        </label>
        {#if s.bgMode === 'uniform'}
          <div class="grid2">
            <label class="field"><span>u east (m/s)</span><input type="number" step="1" value={s.bgU} onchange={setNum('bgU', -100, 100)} /></label>
            <label class="field"><span>v north (m/s)</span><input type="number" step="1" value={s.bgV} onchange={setNum('bgV', -100, 100)} /></label>
          </div>
        {:else if s.bgMode === 'manual'}
          <label class="field">
            <span>Lines of "height m, u, v"</span>
            <textarea rows="4" value={s.bgProfile} oninput={(e) => updateWindSettings({ bgProfile: e.currentTarget.value })}></textarea>
          </label>
          {#if !profileOk}<p class="help warn-text">Each line needs three numbers: height (m), u, v.</p>{/if}
        {/if}
      {/if}
    </section>

    <!-- ── Advanced ──────────────────────────────────────────────────────── -->
    <section class="block">
      <button type="button" class="disclosure" onclick={() => (showAdvanced = !showAdvanced)} aria-expanded={showAdvanced}>
        <span class="caret" class:open={showAdvanced}>&#9656;</span> Advanced
      </button>
      {#if showAdvanced}
        <label class="check">
          <input type="checkbox" checked={s.zMinAuto} onchange={(e) => updateWindSettings({ zMinAuto: e.currentTarget.checked })} />
          <span>Lowest level automatic <span class="muted">(where both radars' lowest beams reach)</span></span>
        </label>
        {#if !s.zMinAuto}
          <label class="field">
            <span>Lowest level ASL (m)</span>
            <input type="number" min="-500" max="10000" step="100" value={s.zMinM} onchange={setNum('zMinM', -500, 10000)} />
          </label>
        {/if}
        <label class="check">
          <input type="checkbox" checked={s.extentAuto} onchange={(e) => updateWindSettings({ extentAuto: e.currentTarget.checked })} />
          <span>Area automatic <span class="muted">(good dual-Doppler region)</span></span>
        </label>
        {#if !s.extentAuto}
          <div class="grid2">
            <label class="field"><span>x min (km)</span><input type="number" step="5" value={s.xMinKm} onchange={setNum('xMinKm', -500, 500)} /></label>
            <label class="field"><span>x max (km)</span><input type="number" step="5" value={s.xMaxKm} onchange={setNum('xMaxKm', -500, 500)} /></label>
            <label class="field"><span>y min (km)</span><input type="number" step="5" value={s.yMinKm} onchange={setNum('yMinKm', -500, 500)} /></label>
            <label class="field"><span>y max (km)</span><input type="number" step="5" value={s.yMaxKm} onchange={setNum('yMaxKm', -500, 500)} /></label>
          </div>
          <p class="help">Relative to the mean position of the selected radars.</p>
        {/if}
        <label class="check">
          <input type="checkbox" checked={s.motionOn} onchange={(e) => updateWindSettings({ motionOn: e.currentTarget.checked })} />
          <span>Advect volumes to a common time <span class="muted">(storm motion)</span></span>
        </label>
        {#if s.motionOn}
          <div class="grid2">
            <label class="field"><span>u (m/s)</span><input type="number" step="1" value={s.motionU} onchange={setNum('motionU', -100, 100)} /></label>
            <label class="field"><span>v (m/s)</span><input type="number" step="1" value={s.motionV} onchange={setNum('motionV', -100, 100)} /></label>
          </div>
        {/if}
        <label class="field">
          <span>Melting depth (m)</span>
          <input type="number" min="0" max="3000" step="100" value={s.meltingDepthM} onchange={setNum('meltingDepthM', 0, 3000)} />
        </label>
        {#if s.method === 'variational'}
          <label class="check">
            <input type="checkbox" checked={s.uncertainty} onchange={(e) => updateWindSettings({ uncertainty: e.currentTarget.checked })} />
            <span>Estimate the error of the wind <span class="muted">(slow)</span></span>
          </label>
        {/if}
        <button type="button" class="mini-btn" onclick={resetWindSettings}>Reset settings</button>
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
          <button type="button" class="mini-btn" onclick={cancelWindRun} title="Stop the retrieval at its next stage">Cancel</button>
        </div>
      {:else}
        <button type="button" class="run-btn" onclick={() => runWind()} disabled={blockedReason !== null} title={blockedReason ?? 'Retrieve the wind'}>
          {res ? 'Re-run retrieval' : 'Retrieve wind'}
          <span class="muted-on-btn">{selectedCount} radar{selectedCount === 1 ? '' : 's'}</span>
        </button>
        {#if blockedReason}<p class="help">{blockedReason}</p>{/if}
      {/if}
      {#if run.error}
        <div class="error-box" role="alert">
          <span class="error-text">{run.error}</span>
          <button type="button" class="mini-btn" onclick={() => windRun.update((r) => ({ ...r, error: null }))}>Dismiss</button>
        </div>
      {/if}
    </section>

    <!-- ── Result ────────────────────────────────────────────────────────── -->
    {#if res}
      <section class="block result" aria-label="Wind result">
        <div class="block-head">
          <h4>{res.method === 'analytic' ? 'Analytic' : 'Variational'} wind · {res.radars.map((r) => r.name).join(' + ')}</h4>
          <span class="spacer"></span>
          <span class="mono muted">{seconds(res.elapsed_ms)}</span>
        </div>

        <dl class="scalars">
          <div class="scalar"><dt>Coverage</dt><dd>{fmt(res.coverage_pct, 0)}%</dd></div>
          <div class="scalar"><dt>w valid</dt><dd>{fmt(res.w_valid_pct, 0)}%</dd></div>
          <div class="scalar"><dt>Grid</dt><dd>{res.grid.nx}×{res.grid.ny}×{res.grid.nz}</dd></div>
          <div class="scalar"><dt>Crossing</dt><dd>{fmt(res.crossing_angle.median, 0)}°<span class="unit">med</span></dd></div>
          <div class="scalar"><dt>Max speed</dt><dd>{fmt(res.field_stats.speed?.max)}<span class="unit">m/s</span></dd></div>
          <div class="scalar"><dt>Max |w|</dt><dd>{fmt(Math.max(Math.abs(res.field_stats.w?.min ?? 0), Math.abs(res.field_stats.w?.max ?? 0)))}<span class="unit">m/s</span></dd></div>
        </dl>
        <p class="help">
          Crossing angle {fmt(res.crossing_angle.min, 0)}–{fmt(res.crossing_angle.max, 0)}° (min required {res.min_crossing_angle}°) ·
          background: {res.background.mode === 'none' ? 'none' : res.background.mode === 'uniform' ? `uniform ${res.background.u}, ${res.background.v} m/s` : res.background.mode} ·
          lowest level {(res.grid.z[0] / 1000).toFixed(2)} km ASL{res.z_min_auto ? ' (auto)' : ''}
        </p>
        {#each res.warnings as w}
          <p class="help warn-text">&#9888; {w}</p>
        {/each}

        <h4>Map layer</h4>
        <div class="grid2">
          <label class="field">
            <span>Field</span>
            <select value={disp.field} onchange={(e) => updateWindDisplay({ field: e.currentTarget.value as WindFieldKey, vmin: null, vmax: null })}>
              {#each MAP_FIELDS as f}<option value={f}>{FIELD_STYLES[f].label}</option>{/each}
            </select>
          </label>
          <label class="field">
            <span>Colour max ({fieldUnits})</span>
            <input type="number" step="1" placeholder="auto" value={disp.vmax ?? ''}
              onchange={(e) => { const n = Number(e.currentTarget.value); updateWindDisplay({ vmax: e.currentTarget.value.trim() === '' || !Number.isFinite(n) ? null : n, vmin: e.currentTarget.value.trim() === '' || !Number.isFinite(n) ? null : style.symmetric ? -n : (style.floor ?? vmin) }); }} />
          </label>
        </div>
        <label class="slider-row">
          <span>Height</span>
          <input type="range" min="0" max={res.grid.nz - 1} step="1" value={disp.level}
            oninput={(e) => updateWindDisplay({ level: Number(e.currentTarget.value) })} />
          <span class="mono">{heightKm} km</span>
        </label>
        <div class="legend">
          <div class="legend-bar" style="background: {scaleGradient(style.kind)}"></div>
          <div class="legend-labels mono"><span>{fmt(vmin, 1)}</span><span>{fieldUnits}</span><span>{fmt(vmax, 1)}</span></div>
        </div>
        <div class="display-row">
          <label class="check inline"><input type="checkbox" checked={disp.visible} onchange={(e) => updateWindDisplay({ visible: e.currentTarget.checked })} /><span>Show on map</span></label>
          <label class="check inline"><input type="checkbox" checked={disp.showRaster} onchange={(e) => updateWindDisplay({ showRaster: e.currentTarget.checked })} /><span>Colours</span></label>
          <label class="check inline"><input type="checkbox" checked={disp.showVectors} onchange={(e) => updateWindDisplay({ showVectors: e.currentTarget.checked })} /><span>Arrows</span></label>
          <label class="check inline"><input type="checkbox" checked={disp.hidePpi} onchange={(e) => updateWindDisplay({ hidePpi: e.currentTarget.checked })} title="Hide the PPI so the map with the wind fills the view" /><span>Hide PPI</span></label>
        </div>
        <label class="slider-row">
          <span>Opacity</span>
          <input type="range" min="0" max="100" step="1" value={Math.round(disp.opacity * 100)}
            oninput={(e) => updateWindDisplay({ opacity: Number(e.currentTarget.value) / 100 })} />
          <span class="mono">{Math.round(disp.opacity * 100)}%</span>
        </label>
        <label class="slider-row" title="Hide cells where the two beams cross at less than this angle">
          <span>Min crossing</span>
          <input type="range" min={Math.floor(res.min_crossing_angle)} max="90" step="5" value={Math.max(disp.minCrossing, Math.floor(res.min_crossing_angle))}
            oninput={(e) => updateWindDisplay({ minCrossing: Number(e.currentTarget.value) })} />
          <span class="mono">{Math.max(disp.minCrossing, Math.floor(res.min_crossing_angle))}°</span>
        </label>
        {#if res.has_uncertainty && res.field_stats.uncertainty?.p98 != null}
          <label class="slider-row" title="Hide cells whose wind error is larger than this">
            <span>Max error</span>
            <input type="range" min="0.5" max={Math.max(1, Math.ceil(res.field_stats.uncertainty.p98 * 2))} step="0.5"
              value={disp.maxUncertainty ?? Math.max(1, Math.ceil(res.field_stats.uncertainty.p98 * 2))}
              oninput={(e) => updateWindDisplay({ maxUncertainty: Number(e.currentTarget.value) })} />
            <span class="mono">{disp.maxUncertainty === null ? 'off' : disp.maxUncertainty.toFixed(1)}</span>
          </label>
        {/if}

        <h4>Vertical cross-section</h4>
        <div class="grid2">
          <label class="field">
            <span>Field</span>
            <select value={disp.sectionField} onchange={(e) => updateWindDisplay({ sectionField: e.currentTarget.value as WindFieldKey })}>
              {#each SECTION_FIELDS as f}<option value={f}>{FIELD_STYLES[f].label}</option>{/each}
            </select>
          </label>
          <div class="field">
            <span>Slice</span>
            <div class="seg" role="group" aria-label="Slice direction">
              <button type="button" class="seg-btn" class:on={disp.sectionAxis === 'x'} onclick={() => onSectionAxis('x')} title="West to east at a fixed y">W–E</button>
              <button type="button" class="seg-btn" class:on={disp.sectionAxis === 'y'} onclick={() => onSectionAxis('y')} title="South to north at a fixed x">S–N</button>
            </div>
          </div>
        </div>
        <label class="slider-row">
          <span>{disp.sectionAxis === 'x' ? 'y' : 'x'}</span>
          <input type="range" min="0" max={sectionMax} step="1" value={Math.min(disp.sectionIndex, sectionMax)}
            oninput={(e) => updateWindDisplay({ sectionIndex: Number(e.currentTarget.value) })} />
          <span class="mono">{sectionKm}</span>
        </label>
        <div class="section-wrap" bind:clientWidth={sectionWidth}>
          <canvas bind:this={sectionCanvas} class="section-canvas" aria-label="Vertical cross-section of the wind analysis"></canvas>
        </div>
        <div class="legend">
          <div class="legend-bar" style="background: {scaleGradient(FIELD_STYLES[disp.sectionField].kind)}"></div>
          <div class="legend-labels mono">
            <span>{fmt(sectionRange[0], 1)}</span>
            <span>{res.fields.find((f) => f.name === disp.sectionField)?.units ?? ''} · height km ASL vs distance km</span>
            <span>{fmt(sectionRange[1], 1)}</span>
          </div>
        </div>

        <div class="btn-row">
          <button type="button" class="mini-btn" onclick={fitWindToMap}>Fit to analysis</button>
          <span class="spacer"></span>
          <button type="button" class="mini-btn" disabled={!desktop || $windExporting} onclick={() => exportWind()}
            title={desktop ? 'Save the gridded fields as NetCDF' : 'Exporting needs the desktop app'}>NetCDF</button>
          <button type="button" class="mini-btn danger" onclick={clearWind}>Clear</button>
        </div>
        {#if !desktop}<p class="help">Export is available in the desktop app.</p>{/if}
      </section>
    {/if}
  </div>
</CollapsiblePanel>

<style>
  .wind-root { display: flex; flex-direction: column; gap: var(--spacing-md, 12px); }
  .block { display: flex; flex-direction: column; gap: var(--spacing-sm, 8px); }
  .block-head { display: flex; align-items: center; gap: var(--spacing-xs, 4px); }
  h4 {
    margin: 0;
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-muted, #5c6088);
  }
  .result h4:first-child { text-transform: none; letter-spacing: 0; font-size: 12px; color: var(--text-primary, #e4e6f4); }
  .spacer { flex: 1; }
  .mono { font-family: var(--font-mono, monospace); font-variant-numeric: tabular-nums; }
  .muted { color: var(--text-muted, #5c6088); }
  .help { margin: 0; font-size: 11px; color: var(--text-muted, #5c6088); line-height: 1.35; }
  .warn-text { color: var(--accent-warning, #fbbf24); }
  .empty-hint { margin: 0; padding: var(--spacing-sm, 8px) 0; font-size: 12px; text-align: center; color: var(--text-muted, #5c6088); }

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
  .mini-btn:hover:not(:disabled) { color: var(--text-primary, #e4e6f4); border-color: var(--accent-primary, #5b6cf7); background: transparent; }
  .mini-btn.danger:hover:not(:disabled) { color: var(--accent-danger, #f87171); border-color: var(--accent-danger, #f87171); }
  .mini-btn:disabled { opacity: 0.45; cursor: not-allowed; }

  .file-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; max-height: 200px; overflow-y: auto; }
  .file-row { padding: 5px 6px; background: var(--bg-primary, #080a14); border: 1px solid var(--border-color, #2a2a3a); border-radius: var(--radius-sm, 4px); }
  .file-row.off { opacity: 0.55; }
  .file-main { display: flex; align-items: flex-start; gap: 8px; cursor: pointer; }
  .file-main input { margin-top: 2px; accent-color: var(--accent-primary, #5b6cf7); }
  .file-text { display: flex; flex-direction: column; min-width: 0; }
  .file-name { font-size: 12px; font-weight: 600; color: var(--text-primary, #e4e6f4); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .file-meta { font-size: 10px; color: var(--text-muted, #5c6088); }
  .radar-chip { display: inline-block; flex: none; width: 9px; height: 9px; margin-top: 4px; border-radius: 50%; }
  .chips { display: flex; flex-wrap: wrap; gap: 4px; margin: 4px 0 0 22px; }
  .chip { display: inline-block; padding: 1px 6px; font-size: 10px; color: var(--text-secondary, #9da2c2); border: 1px solid var(--border-light, #2a2a3a); border-radius: 9px; }
  .chip.warn { color: var(--accent-warning, #fbbf24); border-color: var(--accent-warning, #fbbf24); background: rgba(251, 191, 36, 0.08); }

  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: var(--spacing-sm, 8px); }
  .field { display: flex; flex-direction: column; gap: 3px; min-width: 0; font-size: 10px; color: var(--text-muted, #5c6088); text-transform: uppercase; letter-spacing: 0.04em; }
  .field select, .field input, .field textarea { width: 100%; box-sizing: border-box; font-size: 12px; text-transform: none; letter-spacing: 0; }
  .field select, .field input { height: 26px; }
  .field textarea { font-family: var(--font-mono, monospace); resize: vertical; }
  .check { display: flex; align-items: flex-start; gap: 6px; font-size: 12px; color: var(--text-secondary, #9da2c2); cursor: pointer; }
  .check input { margin-top: 2px; accent-color: var(--accent-primary, #5b6cf7); }
  .check.inline { align-items: center; }

  .seg { display: flex; border: 1px solid var(--border-light, #2a2a3a); border-radius: var(--radius-sm, 4px); overflow: hidden; }
  .seg-btn { flex: 1; height: 26px; padding: 0 6px; font-size: 11px; color: var(--text-secondary, #9da2c2); background: transparent; border: none; border-radius: 0; text-transform: none; letter-spacing: 0; }
  .seg-btn + .seg-btn { border-left: 1px solid var(--border-light, #2a2a3a); }
  .seg-btn.on { color: #fff; background: var(--accent-primary, #5b6cf7); }

  .disclosure { align-self: flex-start; height: auto; padding: 0; font-size: 11px; font-weight: 600; color: var(--text-muted, #5c6088); background: transparent; border: none; box-shadow: none; backdrop-filter: none; }
  .disclosure:hover { color: var(--text-primary, #e4e6f4); background: transparent; box-shadow: none; }
  .caret { display: inline-block; transition: transform 150ms ease; }
  .caret.open { transform: rotate(90deg); }

  .run-btn { height: 32px; gap: 8px; font-weight: 600; color: #fff; background: var(--accent-primary, #5b6cf7); border-color: var(--accent-primary, #5b6cf7); }
  .run-btn:hover:not(:disabled) { background: var(--accent-hover, #7c8cf8); border-color: var(--accent-hover, #7c8cf8); }
  .muted-on-btn { font-size: 10px; font-weight: 500; opacity: 0.8; }
  .progress { height: 6px; overflow: hidden; background: var(--bg-primary, #080a14); border: 1px solid var(--border-color, #2a2a3a); border-radius: 3px; }
  .progress-fill { height: 100%; background: var(--accent-primary, #5b6cf7); transition: width 200ms ease; }
  .progress-row { display: flex; align-items: center; gap: 8px; font-size: 11px; color: var(--text-secondary, #9da2c2); }
  .progress-msg { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .error-box { display: flex; align-items: flex-start; gap: 8px; padding: 6px 8px; font-size: 11px; color: var(--accent-danger, #f87171); background: rgba(248, 113, 113, 0.08); border: 1px solid var(--accent-danger, #f87171); border-radius: var(--radius-sm, 4px); }
  .error-text { flex: 1; min-width: 0; word-break: break-word; user-select: text; }

  .result { padding-top: var(--spacing-sm, 8px); border-top: 1px solid var(--border-color, #2a2a3a); }
  .legend-bar { height: 12px; border: 1px solid var(--border-light, #2a2a3a); border-radius: 2px; }
  .legend-labels { display: flex; justify-content: space-between; gap: 6px; margin-top: 2px; font-size: 10px; color: var(--text-muted, #5c6088); }
  .scalars { display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px; margin: 0; }
  .scalar { padding: 4px 6px; background: var(--bg-primary, #080a14); border: 1px solid var(--border-color, #2a2a3a); border-radius: var(--radius-sm, 4px); min-width: 0; }
  .scalar dt { font-size: 9px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-muted, #5c6088); }
  .scalar dd { margin: 0; font-size: 12px; font-family: var(--font-mono, monospace); font-variant-numeric: tabular-nums; color: var(--text-primary, #e4e6f4); white-space: nowrap; }
  .unit { margin-left: 3px; font-size: 9px; color: var(--text-muted, #5c6088); }
  .display-row { display: flex; flex-wrap: wrap; gap: var(--spacing-sm, 8px) var(--spacing-md, 12px); }
  .slider-row { display: flex; align-items: center; gap: 8px; font-size: 11px; color: var(--text-muted, #5c6088); }
  .slider-row > span:first-child { min-width: 52px; }
  .slider-row input[type='range'] { flex: 1; min-width: 0; accent-color: var(--accent-primary, #5b6cf7); }
  .slider-row .mono { min-width: 44px; text-align: right; }
  .btn-row { display: flex; align-items: center; gap: 6px; }
  .section-wrap { width: 100%; }
  .section-canvas { display: block; width: 100%; height: 150px; background: var(--bg-primary, #080a14); border: 1px solid var(--border-color, #2a2a3a); border-radius: var(--radius-sm, 4px); }
</style>
