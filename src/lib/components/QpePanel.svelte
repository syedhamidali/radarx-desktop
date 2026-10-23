<script lang="ts">
  /**
   * QpePanel - rainfall from radar: rate / accumulation with uncertainty and quality, adjustment to
   * rain gauges with a validation view, and basin rainfall tables and series. Everything shown is
   * labelled RETRIEVED or ADJUSTED (an estimate, never an observation). The map layer is the
   * mosaic layer, so opacity, legend and hover values work as for a mosaic.
   */
  import { onMount } from 'svelte';
  import { connectionStatus } from '../stores/radarData';
  import { openFiles } from '../stores/fileManager';
  import {
    mosaicFileRows,
    mosaicSelectedIds,
    mosaicDisplay,
    toggleMosaicFile,
    selectAllMosaicFiles,
    clearMosaic,
    fitMapToMosaic,
    inTauri,
  } from '../stores/mosaic';
  import {
    qpeSettings,
    qpeInfo,
    qpeRun,
    qpeState,
    qpeSeries,
    qpeBusy,
    basinOverlayOn,
    updateQpeSettings,
    resetQpeSettings,
    runQpe,
    cancelQpe,
    setQpeField,
    adjustQpe,
    loadSeries,
    pickFile,
    exportQpe,
    clearQpe,
    initQpe,
  } from '../stores/qpe';
  import {
    formatNumber,
    formatPercent,
    signedPercent,
    scatterGeometry,
    seriesGeometry,
    provenanceLabel,
    uncertaintyText,
    binMinutesFor,
    type GaugePoint,
  } from '../utils/qpeView';
  import CollapsiblePanel from './CollapsiblePanel.svelte';

  onMount(() => initQpe());

  const s = $derived($qpeSettings);
  const info = $derived($qpeInfo);
  const run = $derived($qpeRun);
  const st = $derived($qpeState);
  const rows = $derived($mosaicFileRows);
  const selectedCount = $derived($mosaicSelectedIds.length);
  const desktop = inTauri();
  const isConnected = $derived($connectionStatus === 'connected');

  const blocked = $derived.by((): string | null => {
    if (!isConnected) return 'Sidecar not connected';
    if ($openFiles.length === 0) return 'Open radar volumes first (several in time give an accumulation)';
    if (selectedCount === 0) return 'Select at least one file';
    return null;
  });

  let showAdvanced = $state(false);
  let showNotes = $state(false);
  let valTab = $state<'scatter' | 'range' | 'amount' | 'map'>('scatter');

  const METHOD_LABELS: Record<string, string> = {
    auto: 'Auto',
    hybrid: 'Hybrid',
    z: 'R(Z)',
    kdp: 'R(KDP)',
    z_zdr: 'R(Z,ZDR)',
    a: 'R(A)',
  };
  const METHOD_HINT: Record<string, string> = {
    auto: 'Hybrid if ZDR and phase exist, else R(Z,ZDR), else R(Z)',
    hybrid: 'Per gate: hydrometeor class and rain-rate regime choose R(Z), R(Z,ZDR) or R(KDP)',
    z: 'Reflectivity only (the chosen Z-R relation)',
    kdp: 'Specific differential phase: immune to calibration, attenuation and hail; noisy below 0.3 deg/km',
    z_zdr: 'Reflectivity and differential reflectivity',
    a: 'Specific attenuation (Ryzhkov et al. 2014)',
  };
  const FIELD_HINT: Record<string, string> = {
    accumulation: 'Rainfall depth over the period',
    accumulation_adjusted: 'Depth after the gauge adjustment',
    accumulation_sigma: '1 sigma of the depth (mm)',
    rate_max: 'Largest composite rate in the period',
    rate_last: 'Rate at the end of the period',
    valid_fraction: 'Share of the period with a radar estimate',
    quality: 'Beam quality index 0-1 (low, unblocked beams score high)',
    beam_height_agl_m: 'Beam height above ground / above the radar',
    range_km: 'Distance to the serving radar',
    adjustment_factor: 'Adjusted / radar',
  };

  const relations = $derived((info?.relations ?? []).filter((r) => r.kind === s.precip));
  const relInfo = $derived(info?.relations.find((r) => r.key === s.relation));

  function setNum(key: 'resolutionM' | 'maxRangeKm' | 'holdEdgesS' | 'zCap' | 'freezingLevelM' | 'gaugeIntervalS', raw: string, min: number, max: number, nullable = false) {
    const n = Number(raw);
    if (raw.trim() === '' || !Number.isFinite(n)) {
      updateQpeSettings({ [key]: nullable ? null : (s as any)[key] } as any);
      return;
    }
    updateQpeSettings({ [key]: Math.min(max, Math.max(min, n)) } as any);
  }

  function setPrecip(p: 'rain' | 'snow') {
    const rel = info?.default_relation?.[p];
    updateQpeSettings({ precip: p, ...(rel ? { relation: rel } : {}) });
  }

  async function browse(key: 'gaugePath' | 'basinPath' | 'frequencyPath' | 'gaugeStations', title: string, ext: string[]) {
    const p = await pickFile(title, ext);
    if (p) updateQpeSettings({ [key]: p } as any);
  }

  function legendGradient(l: { vmin: number; vmax: number; legend: [number, string][] }): string {
    const span = l.vmax - l.vmin || 1;
    const stops = [...(l.legend ?? [])].sort((a, b) => a[0] - b[0]);
    if (stops.length === 0) return 'linear-gradient(90deg, var(--c-surface-3), var(--c-text-dim))';
    return `linear-gradient(90deg, ${stops.map(([v, c]) => `${c} ${Math.max(0, Math.min(100, ((v - l.vmin) / span) * 100)).toFixed(1)}%`).join(', ')})`;
  }

  function fmtTime(iso: string | null | undefined): string {
    return iso ? iso.replace('T', ' ').replace('Z', '').slice(0, 16) + 'Z' : '–';
  }

  const summary = $derived(st?.summary ?? null);
  const instant = $derived(!!summary && summary.duration_s === 0);
  const adjusted = $derived(summary?.provenance.type === 'ADJUSTED');
  const fieldChoices = $derived((summary?.fields ?? []).filter((f) => !(instant && ['accumulation', 'accumulation_adjusted', 'rate_max', 'valid_fraction'].includes(f.name))));

  // uncertainty summary: from the basins table when present, else not shown
  const validation = $derived((summary?.gauge_validation ?? null) as Record<string, any> | null);
  const points = $derived((validation?.validation_before?.points ?? []) as GaugePoint[]);
  const loo = $derived((validation?.cross_validation?.predictions ?? []) as (number | null)[]);
  const scatter = $derived(
    scatterGeometry([...points.map((p) => p.gauge_mm), ...points.map((p) => p.radar_mm), ...loo.filter((v): v is number => v !== null)]),
  );
  const series = $derived($qpeSeries);
  const seriesGeo = $derived(series ? seriesGeometry(series.rows ?? [], series.hyetograph ?? { start: [], end: [], intensity_mm_h: [] }) : null);

  function metricRow(label: string, m: Record<string, any> | undefined | null) {
    return {
      label,
      n: m?.n ?? 0,
      bias: signedPercent(m?.relative_bias),
      rmse: formatNumber(m?.rmse_mm),
      corr: formatNumber(m?.correlation),
      kge: formatNumber(m?.kge),
    };
  }
  const metricRows = $derived(
    validation
      ? [
          metricRow('Radar', validation.validation_before?.overall),
          metricRow('Adj. in-sample', validation.validation_after_in_sample?.overall),
          ...(validation.cross_validation ? [metricRow('Adj. leave-1-out', validation.cross_validation.adjusted)] : []),
        ]
      : [],
  );

  // residual map geometry (equirectangular, aspect corrected)
  const resMap = $derived.by(() => {
    if (!points.length) return null;
    const lons = points.map((p: any) => p.lon as number);
    const lats = points.map((p: any) => p.lat as number);
    const lat0 = (Math.min(...lats) + Math.max(...lats)) / 2;
    const k = Math.cos((lat0 * Math.PI) / 180);
    const w = Math.max((Math.max(...lons) - Math.min(...lons)) * k, 1e-6);
    const h = Math.max(Math.max(...lats) - Math.min(...lats), 1e-6);
    const size = 200;
    const scale = (size - 24) / Math.max(w, h);
    const maxRes = Math.max(...points.map((p) => Math.abs(p.residual_mm)), 1e-6);
    return {
      size,
      maxRes,
      items: points.map((p: any) => ({
        x: 12 + (p.lon - Math.min(...lons)) * k * scale,
        y: size - 12 - (p.lat - Math.min(...lats)) * scale,
        r: 3 + 7 * (Math.abs(p.residual_mm) / maxRes),
        high: p.residual_mm >= 0,
        label: `${p.station}: radar ${formatNumber(p.radar_mm)} mm, gauge ${formatNumber(p.gauge_mm)} mm`,
      })),
    };
  });

  function onField(e: Event) {
    setQpeField((e.currentTarget as HTMLSelectElement).value);
  }
</script>

<CollapsiblePanel title="Rainfall" icon="☔" badge={st ? (adjusted ? 'ADJ' : 'RET') : selectedCount > 0 ? selectedCount : null}>
  <div class="root">
    <!-- Data -->
    <section class="block">
      <div class="block-head">
        <h3>Radar volumes</h3>
        <span class="spacer"></span>
        {#if rows.length > 1}
          <button type="button" class="mini" onclick={() => selectAllMosaicFiles(true)}>All</button>
          <button type="button" class="mini" onclick={() => selectAllMosaicFiles(false)}>None</button>
        {/if}
      </div>
      {#if rows.length === 0}
        <p class="help">Open radar volumes (several in time, one or more radars). Their times give the accumulation period.</p>
      {:else}
        <ul class="files">
          {#each rows as r (r.id)}
            <li class:off={!r.selected}>
              <label>
                <input type="checkbox" checked={r.selected} onchange={() => toggleMosaicFile(r.id)} />
                <span class="fname" title={r.filename}>{r.name}</span>
                <span class="mono dim">{r.timeLabel}</span>
              </label>
            </li>
          {/each}
        </ul>
        <p class="help">
          {selectedCount === 1
            ? 'One volume gives the instantaneous rain rate. Select several scans for an accumulation.'
            : `${selectedCount} volumes: the period runs from the first to the last scan.`}
        </p>
      {/if}
    </section>

    <!-- Estimate -->
    <section class="block">
      <h3>Estimate</h3>
      <div class="grid2">
        <label class="field">
          <span>Method</span>
          <select value={s.method} title={METHOD_HINT[s.method]} onchange={(e) => updateQpeSettings({ method: e.currentTarget.value })}>
            {#each info?.methods ?? ['auto', 'hybrid', 'z', 'kdp', 'z_zdr', 'a'] as m}
              <option value={m}>{METHOD_LABELS[m] ?? m}</option>
            {/each}
          </select>
        </label>
        <label class="field">
          <span>Precipitation</span>
          <div class="seg" role="group" aria-label="Precipitation type">
            <button type="button" class="seg-btn" class:on={s.precip === 'rain'} onclick={() => setPrecip('rain')}>Rain</button>
            <button type="button" class="seg-btn" class:on={s.precip === 'snow'} onclick={() => setPrecip('snow')} title="Z-S relation: very uncertain">Snow</button>
          </div>
        </label>
        <label class="field wide">
          <span>{s.precip === 'snow' ? 'Z-S relation' : 'Z-R relation'}</span>
          <select value={s.relation} onchange={(e) => updateQpeSettings({ relation: e.currentTarget.value })}>
            {#each relations as r}
              <option value={r.key} title={r.label}>{r.equation.split('  ')[0]} · {r.label}</option>
            {/each}
            {#if relations.length === 0}<option value={s.relation}>{s.relation}</option>{/if}
          </select>
        </label>
        <label class="field">
          <span>Band</span>
          <select value={s.band} onchange={(e) => updateQpeSettings({ band: e.currentTarget.value as 'S' | 'C' | 'X' })}>
            <option value="S">S (WSR-88D)</option>
            <option value="C">C</option>
            <option value="X">X</option>
          </select>
        </label>
        <label class="field">
          <span>Freezing level (m)</span>
          <input type="number" min="0" max="8000" step="100" placeholder="unknown" value={s.freezingLevelM ?? ''} onchange={(e) => setNum('freezingLevelM', e.currentTarget.value, 0, 8000, true)} />
        </label>
      </div>
      {#if relInfo}
        <p class="help" title={relInfo.reference}>{relInfo.note} <span class="dim">{relInfo.reference.split(',')[0]}</span></p>
      {/if}
      {#if s.freezingLevelM === null && s.precip === 'rain'}
        <p class="help caution">Without a freezing level, beams in or above the melting layer are not excluded; the class gating of the hybrid is the only protection.</p>
      {/if}
      <button type="button" class="disclosure" onclick={() => (showAdvanced = !showAdvanced)} aria-expanded={showAdvanced}>
        <span class="caret" class:open={showAdvanced}>&#9656;</span> Grid, period and compositing
      </button>
      {#if showAdvanced}
        <div class="grid2">
          <label class="field">
            <span>Cell (m)</span>
            <select value={String(s.resolutionM)} onchange={(e) => updateQpeSettings({ resolutionM: Number(e.currentTarget.value) })}>
              {#each [250, 500, 1000, 2000, 3000, 5000] as r}<option value={String(r)}>{r >= 1000 ? r / 1000 + ' km' : r + ' m'}</option>{/each}
            </select>
          </label>
          <label class="field">
            <span>Max range (km)</span>
            <input type="number" min="10" max="600" step="10" value={s.maxRangeKm} onchange={(e) => setNum('maxRangeKm', e.currentTarget.value, 10, 600)} />
          </label>
          <label class="field">
            <span>Start (UTC)</span>
            <input type="text" placeholder="first scan" value={s.start} onchange={(e) => updateQpeSettings({ start: e.currentTarget.value })} />
          </label>
          <label class="field">
            <span>End (UTC)</span>
            <input type="text" placeholder="last scan" value={s.end} onchange={(e) => updateQpeSettings({ end: e.currentTarget.value })} />
          </label>
          <label class="field">
            <span>Radars combined by</span>
            <select value={s.composite} onchange={(e) => updateQpeSettings({ composite: e.currentTarget.value as 'weighted' | 'best' })}>
              <option value="weighted">Weighted mean</option>
              <option value="best">Best beam only</option>
            </select>
          </label>
          <label class="field">
            <span>Attenuation</span>
            <select value={s.attenuation} onchange={(e) => updateQpeSettings({ attenuation: e.currentTarget.value as 'auto' | 'on' | 'off' })}>
              <option value="auto">Auto (C/X band)</option>
              <option value="on">Always (ZPHI)</option>
              <option value="off">Off</option>
            </select>
          </label>
          <label class="field">
            <span>Z cap (dBZ)</span>
            <input type="number" min="40" max="70" step="1" value={s.zCap ?? ''} placeholder="none" disabled={s.precip === 'snow'} onchange={(e) => setNum('zCap', e.currentTarget.value, 40, 70, true)} />
          </label>
          <label class="field">
            <span>Hold edges (s)</span>
            <input type="number" min="0" max="900" step="30" value={s.holdEdgesS} onchange={(e) => setNum('holdEdgesS', e.currentTarget.value, 0, 900)} title="Hold the first/last scan this long at the period edges" />
          </label>
          <label class="check wide">
            <input type="checkbox" checked={s.advect} onchange={(e) => updateQpeSettings({ advect: e.currentTarget.checked })} />
            <span>Advect between scans <span class="dim">(follow the storm; off = linear in time)</span></span>
          </label>
        </div>
        <button type="button" class="mini" onclick={resetQpeSettings}>Reset settings</button>
      {/if}
    </section>

    <!-- Gauges -->
    <section class="block">
      <h3>Rain gauges <span class="dim">(optional, local file)</span></h3>
      <div class="pathrow">
        <input type="text" placeholder="gauges.csv / .json / NOAA / METAR" value={s.gaugePath} onchange={(e) => updateQpeSettings({ gaugePath: e.currentTarget.value })} aria-label="Gauge file" />
        <button type="button" class="mini" disabled={!desktop} onclick={() => browse('gaugePath', 'Gauge records', ['csv', 'json', 'geojson', 'txt'])} title={desktop ? 'Choose the gauge file' : 'File dialogs need the desktop app: type the path'}>Browse</button>
      </div>
      {#if s.gaugePath.trim()}
        <div class="grid2">
          <label class="field">
            <span>Adjustment</span>
            <select value={s.adjust} onchange={(e) => updateQpeSettings({ adjust: e.currentTarget.value as 'mfb' | 'brandes' | 'ked' })}>
              <option value="mfb">Mean-field bias</option>
              <option value="brandes">Spatial ratio (Brandes-type)</option>
              <option value="ked">Kriging, radar as drift</option>
            </select>
          </label>
          <label class="field">
            <span>Units</span>
            <select value={s.gaugeUnits} onchange={(e) => updateQpeSettings({ gaugeUnits: e.currentTarget.value })}>
              <option value="mm">mm</option>
              <option value="in">inches</option>
              <option value="0.01in">0.01 in</option>
              <option value="0.1mm">0.1 mm</option>
            </select>
          </label>
          <label class="field">
            <span>Time zone</span>
            <input type="text" value={s.gaugeTz} onchange={(e) => updateQpeSettings({ gaugeTz: e.currentTarget.value || 'UTC' })} title="UTC or an IANA name such as America/Chicago" />
          </label>
          <label class="field">
            <span>Record interval (s)</span>
            <input type="number" min="60" step="60" placeholder="from data" value={s.gaugeIntervalS ?? ''} onchange={(e) => setNum('gaugeIntervalS', e.currentTarget.value, 60, 604800, true)} />
          </label>
        </div>
        <p class="help">A record is the depth over the interval <em>ending</em> at its time (UTC). The period of the radar accumulation should match the gauge hours (use Start/End and Hold edges).</p>
        {#if st && !instant}
          <button type="button" class="mini" disabled={$qpeBusy === 'adjust'} onclick={adjustQpe}>
            {$qpeBusy === 'adjust' ? 'Adjusting…' : 'Adjust current result'}
          </button>
        {/if}
      {/if}
    </section>

    <!-- Basins -->
    <section class="block">
      <h3>Basins <span class="dim">(optional, local file)</span></h3>
      <div class="pathrow">
        <input type="text" placeholder="basins.geojson / .shp / .gpkg" value={s.basinPath} onchange={(e) => updateQpeSettings({ basinPath: e.currentTarget.value })} aria-label="Basin file" />
        <button type="button" class="mini" disabled={!desktop} onclick={() => browse('basinPath', 'Basin polygons', ['geojson', 'json', 'shp', 'gpkg'])} title={desktop ? 'Choose the basin file' : 'File dialogs need the desktop app: type the path'}>Browse</button>
      </div>
      {#if s.basinPath.trim()}
        <div class="grid2">
          <label class="field">
            <span>Id attribute</span>
            <input type="text" placeholder="auto" value={s.basinIdField} onchange={(e) => updateQpeSettings({ basinIdField: e.currentTarget.value })} />
          </label>
          <label class="field">
            <span>Thresholds (mm)</span>
            <input type="text" value={s.thresholds} onchange={(e) => updateQpeSettings({ thresholds: e.currentTarget.value })} />
          </label>
        </div>
        <div class="pathrow">
          <input type="text" placeholder="precipitation-frequency table (JSON), optional" value={s.frequencyPath} onchange={(e) => updateQpeSettings({ frequencyPath: e.currentTarget.value })} aria-label="Frequency table" />
          <button type="button" class="mini" disabled={!desktop} onclick={() => browse('frequencyPath', 'Frequency table', ['json'])}>Browse</button>
        </div>
      {/if}
    </section>

    <!-- Probabilistic rainfall -->
    <section class="block">
      <h4>Probabilistic rainfall <span class="dim">(optional)</span></h4>
      <div class="grid2">
        <label class="field">
          <span>Members (0 = off)</span>
          <input type="number" min="0" max="500" step="1" value={s.ensembleMembers} onchange={(e) => updateQpeSettings({ ensembleMembers: Math.max(0, Math.min(500, Math.round(Number(e.currentTarget.value) || 0))) })} />
        </label>
        {#if s.ensembleMembers >= 2}
          <label class="field">
            <span>Seed</span>
            <input type="number" min="0" step="1" value={s.ensembleSeed} onchange={(e) => updateQpeSettings({ ensembleSeed: Math.max(0, Math.round(Number(e.currentTarget.value) || 0)) })} />
          </label>
          <label class="field wide">
            <span>Probability of at least (mm)</span>
            <input type="text" value={s.ensembleThresholds} onchange={(e) => updateQpeSettings({ ensembleThresholds: e.currentTarget.value })} />
          </label>
        {/if}
      </div>
      {#if s.ensembleMembers >= 2}
        <p class="help">An ensemble of possible truths under an <strong>assumed</strong> error model (multiplicative, spatially correlated). With gauges the model comes from them, without gauges from the engine's uncertainty and marked defaults. Not a forecast; the probabilities are not calibrated unless the result says they were checked.</p>
      {/if}
    </section>

    <!-- Run -->
    <section class="block">
      {#if run.running}
        <div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow={Math.round(run.percent)}>
          <div class="fill" style="width: {Math.max(4, run.percent)}%"></div>
        </div>
        <div class="progress-row">
          <span class="msg" title={run.message}>{run.message || 'Working…'}</span>
          <span class="mono">{Math.round(run.percent)}%</span>
          <button type="button" class="mini" onclick={cancelQpe}>Cancel</button>
        </div>
      {:else}
        <button type="button" class="run" onclick={() => runQpe()} disabled={blocked !== null} title={blocked ?? 'Estimate the rainfall'}>
          {st ? 'Re-run' : 'Estimate rainfall'}
          <span class="onbtn">{selectedCount} volume{selectedCount === 1 ? '' : 's'}</span>
        </button>
        {#if blocked}<p class="help">{blocked}</p>{/if}
      {/if}
      {#if run.error}
        <div class="error" role="alert">
          <span class="etext">{run.error}</span>
          <button type="button" class="mini" onclick={() => qpeRun.update((r) => ({ ...r, error: null }))}>Dismiss</button>
        </div>
      {/if}
    </section>

    <!-- Result -->
    {#if st && summary}
      <section class="block result" aria-label="Rainfall result">
        <div class="block-head">
          <h3>{instant ? 'Rain rate' : 'Accumulation'}</h3>
          <span class="spacer"></span>
          <span class="chip" class:adj={adjusted} title="An estimate from radar{adjusted ? ' merged with rain gauges' : ''}; not an observation">{provenanceLabel(summary.provenance.type)}</span>
        </div>
        <p class="help mono">{instant ? fmtTime(summary.start) : `${fmtTime(summary.start)} to ${fmtTime(summary.end)} (${(summary.duration_s / 60).toFixed(1)} min)`} · {summary.radars.length} radar{summary.radars.length === 1 ? '' : 's'} · {summary.grid.nx}×{summary.grid.ny} @ {summary.grid.resolution_m} m · {(summary.timing.total_s ?? 0).toFixed(1)} s</p>

        <label class="field">
          <span>Layer on the map</span>
          <select value={st.layer.field} disabled={$qpeBusy === 'layer'} onchange={onField} title={FIELD_HINT[st.layer.field]}>
            {#each fieldChoices as f}<option value={f.name}>{f.label} [{f.units}]</option>{/each}
          </select>
        </label>
        <div class="legend">
          <div class="legend-bar" style="background: {legendGradient(st.layer)}"></div>
          <div class="legend-labels mono"><span>{st.layer.vmin}</span><span>{st.layer.units}</span><span>{st.layer.vmax}</span></div>
        </div>

        <dl class="scalars">
          {#if !instant}
            <div class="scalar"><dt>Max depth</dt><dd>{formatNumber(summary.stats.max_mm)}<span class="unit">mm</span></dd></div>
            <div class="scalar"><dt>Mean depth</dt><dd>{formatNumber(summary.stats.mean_mm)}<span class="unit">mm</span></dd></div>
          {/if}
          <div class="scalar"><dt>Max rate</dt><dd>{formatNumber(summary.stats.max_rate_mm_h)}<span class="unit">mm/h</span></dd></div>
          <div class="scalar"><dt>Rain area</dt><dd>{formatNumber(summary.stats.wet_area_km2)}<span class="unit">km²</span></dd></div>
          {#if !instant}
            <div class="scalar"><dt>Observed</dt><dd>{formatPercent(summary.stats.mean_valid_fraction)}</dd></div>
          {/if}
          <div class="scalar"><dt>Valid cells</dt><dd>{formatPercent(st.layer.stats.valid_fraction)}</dd></div>
        </dl>
        <p class="help" title={summary.provenance.uncertainty.model}>
          Uncertainty {uncertaintyText(summary.stats.uncertainty_median_rel)}: relation scatter and calibration only. It excludes vertical profile, bright band, overshoot and advection (see the quality layers).
        </p>
        <p class="method" title={summary.provenance.method}>{summary.provenance.method}</p>

        <div class="table-wrap">
          <table class="data">
            <thead><tr><th>Radar</th><th class="num">Scans</th><th>Span UTC</th><th class="num">u, v m/s</th></tr></thead>
            <tbody>
              {#each summary.radars as r (r.name)}
                <tr>
                  <td class="strong">{r.name}</td>
                  <td class="num mono">{r.scans}</td>
                  <td class="mono">{(r.first ?? '–').slice(11, 16)}–{(r.last ?? '–').slice(11, 16)}</td>
                  <td class="num mono">{r.motion_mean_ms ? `${r.motion_mean_ms[0].toFixed(1)}, ${r.motion_mean_ms[1].toFixed(1)}` : '–'}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>

        <div class="display-row">
          <label class="check inline"><input type="checkbox" checked={$mosaicDisplay.visible} onchange={(e) => mosaicDisplay.update((d) => ({ ...d, visible: e.currentTarget.checked }))} /><span>Show on map</span></label>
          <label class="check inline"><input type="checkbox" checked={$mosaicDisplay.mosaicView} onchange={(e) => mosaicDisplay.update((d) => ({ ...d, mosaicView: e.currentTarget.checked }))} /><span>Map view</span></label>
        </div>
        <label class="slider-row">
          <span>Opacity</span>
          <input type="range" min="0" max="100" step="1" value={Math.round($mosaicDisplay.opacity * 100)} oninput={(e) => mosaicDisplay.update((d) => ({ ...d, opacity: Number(e.currentTarget.value) / 100 }))} />
          <span class="mono">{Math.round($mosaicDisplay.opacity * 100)}%</span>
        </label>

        {#if summary.notes.length}
          <button type="button" class="disclosure" onclick={() => (showNotes = !showNotes)} aria-expanded={showNotes}>
            <span class="caret" class:open={showNotes}>&#9656;</span> Notes and assumptions ({summary.notes.length})
          </button>
          {#if showNotes}
            <ul class="notes">{#each summary.notes as n}<li>{n}</li>{/each}</ul>
          {/if}
        {/if}

        <div class="btn-row">
          <button type="button" class="mini" onclick={fitMapToMosaic}>Fit to map</button>
          <span class="spacer"></span>
          {#each ['nc', 'png', 'json'] as f}
            <button type="button" class="mini" disabled={!desktop || $qpeBusy === 'export'} onclick={() => exportQpe(f as 'nc' | 'png' | 'json')} title={desktop ? `Save as ${f.toUpperCase()}` : 'Exporting needs the desktop app'}>{f === 'nc' ? 'NetCDF' : f.toUpperCase()}</button>
          {/each}
          {#if summary.basins}
            <button type="button" class="mini" disabled={!desktop || $qpeBusy === 'export'} onclick={() => exportQpe('csv')} title="Basin table and the series of every basin, with units and provenance in the header">CSV</button>
            <button type="button" class="mini" disabled={!desktop || $qpeBusy === 'export'} onclick={() => exportQpe('geojson')} title="Basin outlines with their statistics, for a GIS">GeoJSON</button>
          {/if}
          <button type="button" class="mini danger" onclick={() => { clearQpe(); clearMosaic(); }}>Clear</button>
        </div>
        {#if !desktop}<p class="help">Export buttons are available in the desktop app.</p>{/if}
      </section>

      <!-- Gauge validation -->
      {#if validation}
        <section class="block result" aria-label="Gauge validation">
          <div class="block-head">
            <h3>Gauge comparison</h3>
            <span class="spacer"></span>
            <span class="chip adj">{summary.adjustment?.method}</span>
          </div>
          <p class="help">{validation.pairs} gauges used{validation.qc?.records ? ` of ${validation.qc.records} records` : ''}. Leave-one-out is the honest skill: each gauge predicted from the others. Gauges are point measurements with errors of their own.</p>
          <div class="table-wrap">
            <table class="data">
              <thead><tr><th></th><th class="num">n</th><th class="num">Bias</th><th class="num" title="mm">RMSE</th><th class="num" title="Pearson correlation">r</th><th class="num" title="Kling-Gupta efficiency">KGE</th></tr></thead>
              <tbody>
                {#each metricRows as m}
                  <tr><td class="strong">{m.label}</td><td class="num mono">{m.n}</td><td class="num mono">{m.bias}</td><td class="num mono">{m.rmse}</td><td class="num mono">{m.corr}</td><td class="num mono">{m.kge}</td></tr>
                {/each}
              </tbody>
            </table>
          </div>
          <div class="seg" role="tablist" aria-label="Validation view">
            {#each [['scatter', 'Scatter'], ['range', 'By range'], ['amount', 'By amount'], ['map', 'Residual map']] as [k, l]}
              <button type="button" role="tab" aria-selected={valTab === k} class="seg-btn" class:on={valTab === k} onclick={() => (valTab = k as typeof valTab)}>{l}</button>
            {/each}
          </div>
          {#if valTab === 'scatter'}
            <svg class="plot" viewBox="0 0 220 220" role="img" aria-label="Radar accumulation against gauge accumulation, with the 1:1 line">
              {#each scatter.ticks as t}
                <line x1={scatter.left} x2={scatter.left + scatter.size} y1={scatter.y(t.value)} y2={scatter.y(t.value)} class="grid" />
                <text x={scatter.left - 4} y={scatter.y(t.value) + 3} text-anchor="end" class="tick">{t.label}</text>
                <text x={scatter.x(t.value)} y={scatter.top + scatter.size + 11} text-anchor="middle" class="tick">{t.label}</text>
              {/each}
              <line x1={scatter.x(0)} y1={scatter.y(0)} x2={scatter.x(scatter.max)} y2={scatter.y(scatter.max)} class="one" />
              {#each points as p, i}
                <circle cx={scatter.x(p.gauge_mm)} cy={scatter.y(p.radar_mm)} r="3.2" class="pt before"><title>{p.station}: gauge {formatNumber(p.gauge_mm)} mm, radar {formatNumber(p.radar_mm)} mm</title></circle>
                {#if loo[i] !== null && loo[i] !== undefined}
                  <rect x={scatter.x(p.gauge_mm) - 3} y={scatter.y(loo[i] as number) - 3} width="6" height="6" class="pt after" transform="rotate(45 {scatter.x(p.gauge_mm)} {scatter.y(loo[i] as number)})"><title>{p.station}: adjusted (leave-one-out) {formatNumber(loo[i])} mm</title></rect>
                {/if}
              {/each}
              <text x={scatter.left + scatter.size / 2} y="218" text-anchor="middle" class="axis">gauge accumulation (mm)</text>
              <text x="9" y={scatter.top + scatter.size / 2} text-anchor="middle" class="axis" transform="rotate(-90 9 {scatter.top + scatter.size / 2})">radar (mm)</text>
            </svg>
            <p class="legend-line"><span class="swatch before"></span> radar before <span class="swatch after"></span> adjusted, leave-one-out <span class="dim">· dashed: 1:1</span></p>
          {:else if valTab === 'map'}
            {#if resMap}
              <svg class="plot" viewBox="0 0 {resMap.size} {resMap.size}" role="img" aria-label="Residual map: radar minus gauge at each gauge">
                <rect x="0" y="0" width={resMap.size} height={resMap.size} class="mapbg" />
                {#each resMap.items as it}
                  <circle cx={it.x} cy={it.y} r={it.r} class="res" class:high={it.high} class:low={!it.high}><title>{it.label}</title></circle>
                {/each}
              </svg>
              <p class="legend-line"><span class="swatch high"></span> radar higher than gauge <span class="swatch low"></span> radar lower <span class="dim">· size ∝ |difference|, max {formatNumber(resMap.maxRes)} mm</span></p>
            {/if}
          {:else}
            {@const klass = valTab === 'range' ? validation.validation_before?.by_range_km : validation.validation_before?.by_amount_mm}
            <div class="table-wrap">
              <table class="data">
                <thead><tr><th>{valTab === 'range' ? 'Range km' : 'Gauge mm'}</th><th class="num">n</th><th class="num">Bias</th><th class="num">RMSE</th><th class="num">r</th></tr></thead>
                <tbody>
                  {#each klass ?? [] as k}
                    <tr><td class="mono">{k.range_class_km ?? k.gauge_class_mm}</td><td class="num mono">{k.n}</td><td class="num mono">{signedPercent(k.relative_bias)}</td><td class="num mono">{formatNumber(k.rmse_mm)}</td><td class="num mono">{formatNumber(k.correlation)}</td></tr>
                  {/each}
                </tbody>
              </table>
            </div>
            <p class="help">Radar before adjustment, stratified by {valTab === 'range' ? 'distance from the serving radar (beam height grows with range)' : 'the gauge amount'}.</p>
          {/if}
        </section>
      {/if}

      <!-- Ensemble -->
      {#if summary.ensemble}
        {@const ens = summary.ensemble}
        <section class="block result" aria-label="Ensemble under an assumed error model">
          <div class="block-head">
            <h4>Ensemble</h4>
            <span class="spacer"></span>
            <span class="chip">RETRIEVED · ASSUMED MODEL</span>
          </div>
          <p class="help">{ens.disclaimer}</p>
          <p class="help mono">
            {ens.n_members} members, seed {ens.seed} · σ(ln) {formatNumber(ens.error_model.sigma_ln)} · correlation {formatNumber(ens.error_model.corr_length_km)} km ·
            shares {formatNumber(ens.error_model.shares.common)} / {formatNumber(ens.error_model.shares.spatial)} / {formatNumber(ens.error_model.shares.cell_to_cell)} (common / spatial / cell)
          </p>
          <ul class="notes">
            {#each Object.entries(ens.error_model.sources) as [k, v]}<li><strong>{k}</strong>: {v}</li>{/each}
            {#each ens.error_model.notes as n}<li>{n}</li>{/each}
          </ul>
          <p class="help">
            {#if !ens.reliability}
              No gauges: the probabilities are <strong>not validated</strong> against observations.
            {:else if !ens.reliability.validated}
              {ens.reliability.note}
            {:else}
              Checked at {ens.reliability.n_gauges} gauges: Brier skill {formatNumber(ens.reliability.pooled.brier_skill_score)}, reliability slope {formatNumber(ens.reliability.pooled.reliability_slope)} (1 is perfect). {ens.reliability.note}
            {/if}
            Switch the map layer above to <span class="mono">ens_std</span> (spread) or <span class="mono">prob_ge_…mm</span> (probability) to see them.
          </p>
          {#if ens.basins?.length}
            <div class="table-wrap">
              <table class="data">
                <thead><tr><th>Basin</th><th class="num">Mean mm</th><th class="num">5 %</th><th class="num">95 %</th>{#each ens.thresholds_mm as t}<th class="num">P ≥{t}</th>{/each}</tr></thead>
                <tbody>
                  {#each ens.basins as b (b.id)}
                    {#if b.basin_mean_mm}
                      <tr>
                        <td class="strong">{b.id}</td>
                        <td class="num mono">{formatNumber(b.basin_mean_mm.mean)}</td>
                        <td class="num mono">{formatNumber(b.basin_mean_mm.p05)}</td>
                        <td class="num mono">{formatNumber(b.basin_mean_mm.p95)}</td>
                        {#each ens.thresholds_mm as t}<td class="num mono">{formatPercent(b.prob_basin_mean_ge[String(t)])}</td>{/each}
                      </tr>
                    {/if}
                  {/each}
                </tbody>
              </table>
            </div>
          {/if}
        </section>
      {/if}

      <!-- Basins -->
      {#if summary.basins?.length}
        <section class="block result" aria-label="Basin rainfall">
          <div class="block-head">
            <h3>Basins</h3>
            <span class="spacer"></span>
            <label class="check inline" title="Draw the basin outlines, filled by the basin-mean accumulation, on the map"><input type="checkbox" bind:checked={$basinOverlayOn} /><span>On map</span></label>
            <span class="chip" class:adj={adjusted}>{adjusted ? 'ADJUSTED' : 'RETRIEVED'}</span>
          </div>
          <div class="table-wrap">
            <table class="data">
              <thead><tr><th>Basin</th><th class="num">km²</th><th class="num">Mean mm</th><th class="num">Max mm</th><th class="num">Peak mm/h</th><th class="num">Cover</th></tr></thead>
              <tbody>
                {#each summary.basins as b (b.id)}
                  <tr class="click" class:sel={series?.basin === b.id} onclick={() => loadSeries(b.id, binMinutesFor(summary.duration_s))}>
                    <td class="strong" title={b.name}>{b.id}</td>
                    <td class="num mono">{formatNumber(b.area_km2)}</td>
                    <td class="num mono">{formatNumber(adjusted ? (b.accumulation_adjusted_mean_mm ?? b.accumulation_mean_mm) : b.accumulation_mean_mm)}</td>
                    <td class="num mono">{formatNumber(adjusted ? (b.accumulation_adjusted_max_mm ?? b.accumulation_max_mm) : b.accumulation_max_mm)}</td>
                    <td class="num mono">{formatNumber(b.peak_mean_rate_mm_h)}</td>
                    <td class="num mono">{formatPercent(b.coverage)}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
          <p class="help">Area-weighted: the polygon is intersected with the grid cells exactly and weighted with the true cell area. Click a row for its series.</p>
          {#if series && seriesGeo}
            <svg class="plot wide" viewBox="0 0 260 120" role="img" aria-label="Basin mean rain rate, hyetograph and cumulative depth of {series.basin}">
              {#each seriesGeo.bars as b}<rect x={b.x} y={b.y} width={b.w} height={b.h} class="bar" />{/each}
              <path d={seriesGeo.ratePath} class="rate" />
              <path d={seriesGeo.cumPath} class="cum" />
              {#each seriesGeo.xTicks as t}<text x={t.x} y="116" text-anchor="middle" class="tick">{t.label}</text>{/each}
              <text x="28" y="9" text-anchor="end" class="tick">{formatNumber(seriesGeo.rateMax)}</text>
              <text x="232" y="9" class="tick">{formatNumber(seriesGeo.cumMax)}</text>
              <text x="2" y="62" class="axis" transform="rotate(-90 8 62)">mm/h</text>
              <text x="256" y="62" class="axis" transform="rotate(90 256 62)">mm</text>
            </svg>
            <p class="legend-line"><span class="swatch bar"></span> hyetograph <span class="swatch rate"></span> mean rate <span class="swatch cum"></span> cumulative depth <span class="dim">· {series.basin}, {provenanceLabel(series.provenance_type)}{series.exact_adjustment === false ? ', hyetograph shape approximate' : ''}</span></p>
            {#if series.stats?.accumulation_sigma_mm != null}
              <p class="help">
                Basin-mean accumulation {formatNumber(series.stats.accumulation_mean_mm)} mm, 1σ ±{formatNumber(series.stats.accumulation_sigma_mm)} mm
                (relation scatter and calibration, errors fully correlated in time; vertical profile, bright band and advection are not included).
                {formatPercent(series.stats.coverage)} of the basin area has an estimate; the mean is over that part.
                <button type="button" class="mini" disabled={!desktop || $qpeBusy === 'export'} onclick={() => exportQpe('series')} title="Save the series of the basins as a PNG figure">Series PNG</button>
              </p>
            {/if}
            {#if series.stats}
              <p class="help mono">
                {#each Object.keys(series.stats).filter((k) => k.startsWith('area_acc_ge_')) as k}
                  {k.replace('area_acc_ge_', '≥').replace('mm_km2', ' mm')}: {formatNumber(series.stats[k])} km² ·
                {/each}
              </p>
            {/if}
            {#if series.exceedance}
              <p class="help">
                Return period of the largest {series.exceedance.duration_h} h basin-mean depth ({formatNumber(series.exceedance.max_depth_mm)} mm):
                <strong>{series.exceedance.return_period_years === null ? '–' : `${series.exceedance.flag.startsWith('below') ? '<' : series.exceedance.flag.startsWith('above') ? '>' : ''}${formatNumber(series.exceedance.return_period_years)} yr`}</strong>.
                {#if series.exceedance.areal_reduction_factor}
                  The table holds point depths: the area mean was divided by your areal reduction factor {formatNumber(series.exceedance.areal_reduction_factor)} (point equivalent {formatNumber(series.exceedance.point_equivalent_depth_mm)} mm).
                {:else}
                  The table holds point depths and no areal reduction is applied, so for a large basin the return period is understated (a lower bound).
                {/if}
              </p>
            {/if}
          {/if}
        </section>
      {/if}
    {/if}
  </div>
</CollapsiblePanel>

<style>
  .root { display: flex; flex-direction: column; gap: var(--space-3); }
  .block { display: flex; flex-direction: column; gap: var(--space-2); }
  .block-head { display: flex; align-items: center; gap: var(--space-1); }
  h3 { margin: 0; font-size: var(--fs-2xs); font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; color: var(--c-muted); }
  .result { padding-top: var(--space-2); border-top: 1px solid var(--c-border); }
  .result h3 { text-transform: none; letter-spacing: 0; font-size: var(--fs-sm); color: var(--c-text); }
  .spacer { flex: 1; }
  .mono { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
  .dim { color: var(--c-muted); font-weight: 400; }
  .strong { color: var(--c-text); font-weight: 600; }
  .help { margin: 0; font-size: var(--fs-xs); color: var(--c-muted); line-height: 1.4; }
  .help.caution { color: var(--c-caution); }
  .method { margin: 0; font-size: var(--fs-2xs); color: var(--c-muted); line-height: 1.35; word-break: break-word; font-family: var(--font-mono); max-height: 3.9em; overflow: hidden; }

  .mini { height: auto; padding: 2px var(--space-2); font-size: var(--fs-xs); color: var(--c-text-dim); background: transparent; border: 1px solid var(--c-border-strong); border-radius: var(--radius-sm); cursor: pointer; white-space: nowrap; }
  .mini:hover:not(:disabled) { color: var(--c-text); border-color: var(--c-accent); background: transparent; }
  .mini.danger:hover:not(:disabled) { color: var(--c-alarm); border-color: var(--c-alarm); }
  .mini:disabled { opacity: 0.45; cursor: not-allowed; }

  .files { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-1); max-height: 170px; overflow-y: auto; }
  .files li { padding: 3px 6px; background: var(--c-surface-0); border: 1px solid var(--c-border); border-radius: var(--radius-md); }
  .files li.off { opacity: 0.55; }
  .files label { display: flex; align-items: center; gap: var(--space-2); cursor: pointer; font-size: var(--fs-xs); }
  .files input { accent-color: var(--c-accent); }
  .fname { font-weight: 600; color: var(--c-text); min-width: 3.2em; }

  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-2); }
  .field { display: flex; flex-direction: column; gap: 3px; min-width: 0; font-size: var(--fs-2xs); color: var(--c-muted); text-transform: uppercase; letter-spacing: 0.04em; }
  .field.wide, .check.wide { grid-column: 1 / -1; }
  .field select, .field input { width: 100%; box-sizing: border-box; height: 26px; font-size: var(--fs-sm); text-transform: none; letter-spacing: 0; }
  .check { display: flex; align-items: flex-start; gap: 6px; font-size: var(--fs-sm); color: var(--c-text-dim); cursor: pointer; }
  .check input { margin-top: 2px; accent-color: var(--c-accent); }
  .check.inline { align-items: center; }
  .pathrow { display: flex; gap: var(--space-1); }
  .pathrow input { flex: 1; min-width: 0; height: 26px; font-size: var(--fs-xs); }

  .seg { display: flex; border: 1px solid var(--c-border-strong); border-radius: var(--radius-md); overflow: hidden; }
  .seg-btn { flex: 1; height: 26px; padding: 0 6px; font-size: var(--fs-xs); color: var(--c-text-dim); background: transparent; border: none; border-radius: 0; text-transform: none; letter-spacing: 0; }
  .seg-btn + .seg-btn { border-left: 1px solid var(--c-border-strong); }
  .seg-btn.on { color: var(--c-on-accent); background: var(--c-accent); }
  .disclosure { align-self: flex-start; height: auto; padding: 0; font-size: var(--fs-xs); font-weight: 600; color: var(--c-muted); background: transparent; border: none; box-shadow: none; }
  .disclosure:hover { color: var(--c-text); background: transparent; box-shadow: none; }
  .caret { display: inline-block; transition: transform var(--dur-fast) var(--ease-out); }
  .caret.open { transform: rotate(90deg); }

  .run { height: 32px; gap: var(--space-2); font-weight: 600; color: var(--c-on-accent); background: var(--c-accent); border-color: var(--c-accent); }
  .run:hover:not(:disabled) { background: var(--c-accent-strong); border-color: var(--c-accent-strong); }
  .onbtn { font-size: var(--fs-2xs); font-weight: 500; opacity: 0.8; }
  .progress { height: 6px; overflow: hidden; background: var(--c-surface-0); border: 1px solid var(--c-border); border-radius: var(--radius-sm); }
  .fill { height: 100%; background: var(--c-accent); transition: width var(--dur-base) var(--ease-out); }
  .progress-row { display: flex; align-items: center; gap: var(--space-2); font-size: var(--fs-xs); color: var(--c-text-dim); }
  .msg { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .error { display: flex; align-items: flex-start; gap: var(--space-2); padding: 6px var(--space-2); font-size: var(--fs-xs); color: var(--c-alarm); background: rgba(var(--c-alarm-rgb), 0.08); border: 1px solid var(--c-alarm); border-radius: var(--radius-md); }
  .etext { flex: 1; min-width: 0; word-break: break-word; user-select: text; }

  .chip { display: inline-block; padding: 1px 7px; font-size: var(--fs-2xs); font-weight: 700; letter-spacing: 0.05em; color: var(--c-accent); border: 1px solid var(--c-accent); border-radius: 9px; background: rgba(var(--c-accent-rgb), 0.08); }
  .chip.adj { color: var(--c-derived); border-color: var(--c-derived); background: rgba(var(--c-derived-rgb), 0.1); }

  .legend-bar { height: 12px; border: 1px solid var(--c-border-strong); border-radius: var(--radius-xs); }
  .legend-labels { display: flex; justify-content: space-between; margin-top: 2px; font-size: var(--fs-2xs); color: var(--c-muted); }
  .scalars { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--space-1); margin: 0; }
  .scalar { padding: 4px 6px; background: var(--c-surface-0); border: 1px solid var(--c-border); border-radius: var(--radius-md); min-width: 0; }
  .scalar dt { font-size: 9px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--c-muted); }
  .scalar dd { margin: 0; font-size: var(--fs-sm); font-family: var(--font-mono); font-variant-numeric: tabular-nums; color: var(--c-text); white-space: nowrap; }
  .unit { margin-left: 3px; font-size: 9px; color: var(--c-muted); }

  .table-wrap { max-height: 190px; overflow: auto; border: 1px solid var(--c-border); border-radius: var(--radius-md); }
  .data { width: 100%; border-collapse: collapse; font-size: var(--fs-xs); }
  .data th { position: sticky; top: 0; padding: 4px 4px; font-size: var(--fs-2xs); font-weight: 600; text-align: left; text-transform: uppercase; letter-spacing: 0.04em; color: var(--c-muted); background: var(--c-surface-2); border-bottom: 1px solid var(--c-border-strong); }
  .data td { padding: 3px 4px; color: var(--c-text-dim); border-bottom: 1px solid var(--c-border); white-space: nowrap; }
  .data .num { text-align: right; }
  .data tr.click { cursor: pointer; }
  .data tr.click:hover td { background: var(--bg-hover); }
  .data tr.sel td { background: var(--bg-active); }

  .display-row { display: flex; flex-wrap: wrap; gap: var(--space-3); }
  .slider-row { display: flex; align-items: center; gap: var(--space-2); font-size: var(--fs-xs); color: var(--c-muted); }
  .slider-row input[type='range'] { flex: 1; min-width: 0; accent-color: var(--c-accent); }
  .btn-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .notes { margin: 0; padding-left: 16px; font-size: var(--fs-xs); color: var(--c-muted); line-height: 1.4; }

  .plot { width: 100%; max-width: 260px; align-self: center; height: auto; }
  .plot.wide { max-width: 100%; }
  .plot .grid { stroke: var(--c-border); stroke-width: 0.6; }
  .plot .tick { font-size: 8px; fill: var(--c-muted); font-family: var(--font-mono); }
  .plot .axis { font-size: 8.5px; fill: var(--c-text-dim); }
  .plot .one { stroke: var(--c-muted); stroke-width: 1; stroke-dasharray: 4 3; }
  .plot .pt.before { fill: var(--c-accent); fill-opacity: 0.85; stroke: var(--c-surface-0); stroke-width: 0.6; }
  .plot .pt.after { fill: none; stroke: var(--c-derived); stroke-width: 1.4; }
  .plot .mapbg { fill: var(--c-surface-0); stroke: var(--c-border); }
  .plot .res.high { fill: var(--c-caution); fill-opacity: 0.7; stroke: var(--c-caution); }
  .plot .res.low { fill: var(--c-accent); fill-opacity: 0.55; stroke: var(--c-accent); }
  .plot .bar { fill: var(--c-accent); fill-opacity: 0.28; }
  .plot .rate { fill: none; stroke: var(--c-accent); stroke-width: 1.5; }
  .plot .cum { fill: none; stroke: var(--c-derived); stroke-width: 1.5; }
  .legend-line { margin: 0; font-size: var(--fs-xs); color: var(--c-text-dim); display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; }
  .swatch { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 2px; vertical-align: middle; }
  .swatch.before, .swatch.rate { background: var(--c-accent); }
  .swatch.after, .swatch.cum { background: transparent; border: 2px solid var(--c-derived); }
  .swatch.high { background: var(--c-caution); }
  .swatch.low { background: var(--c-accent); opacity: 0.6; }
  .swatch.bar { background: var(--c-accent); opacity: 0.3; border-radius: 2px; }
</style>
