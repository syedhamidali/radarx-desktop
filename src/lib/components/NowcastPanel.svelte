<script lang="ts">
  /**
   * NowcastPanel - the next 5 to 120 minutes of the radar echo, extrapolated from the last frames
   * (open volumes, the live map's recent loop, or a gridded frames file), with an optional ensemble
   * that turns the uncertainty of the motion into probabilities. The result is a FORECAST: it is
   * drawn on the map with a lead-time slider, and every frame says FCST, the origin and valid
   * time, the lead, the guidance sentence and the engine's skill statement (also in every saved
   * file). The verified limits are stated here (docs: Nowcast); it is never an observation.
   */
  import { onMount } from 'svelte';
  import { connectionStatus } from '../stores/radarData';
  import { mosaicFileRows, mosaicDisplay, toggleMosaicFile, selectAllMosaicFiles, inTauri } from '../stores/mosaic';
  import {
    nowcastSettings,
    nowcastRun,
    nowcastResult,
    nowcastView,
    nowcastBusy,
    nowcastLoaded,
    nowcastInput,
    nowcastOnMap,
    updateNowcastSettings,
    setNowcastStep,
    setNowcastSource,
    runNowcast,
    cancelNowcast,
    setLeadIndex,
    stepLead,
    setProduct,
    showNowcastOnMap,
    hideNowcastFromMap,
    togglePlaying,
    saveFramePicture,
    exportNowcast,
    pickGridFile,
    clearNowcast,
    initNowcast,
  } from '../stores/nowcast';
  import {
    NOWCAST_STEPS,
    NOWCAST_MAX_MEMBERS,
    NOWCAST_MAX_LEAD_MIN,
    VERIFIED_LIMITS,
    NOWCAST_NOT_OBSERVATION,
    leadChoices,
    leadCount,
    productChoices,
    frameLabel,
    formatLead,
    formatSigma,
    sigmaChart,
    type NowcastSourceKind,
  } from '../utils/nowcastLogic';
  import { formatUtc } from '../utils/provenance';
  import CollapsiblePanel from './CollapsiblePanel.svelte';
  import ProvenanceChip from './ProvenanceChip.svelte';

  onMount(() => initNowcast());

  const s = $derived($nowcastSettings);
  const run = $derived($nowcastRun);
  const res = $derived($nowcastResult);
  const view = $derived($nowcastView);
  const input = $derived($nowcastInput);
  const rows = $derived($mosaicFileRows);
  const desktop = inTauri();
  const isConnected = $derived($connectionStatus === 'connected');
  const onMap = $derived($nowcastOnMap);
  const busy = $derived($nowcastBusy);

  const blocked = $derived.by((): string | null => {
    if (!isConnected) return 'The engine is not connected';
    return input.problem;
  });

  const label = $derived(res ? frameLabel(res, view.leadIndex, view.product) : null);
  const choices = $derived(res ? productChoices(res) : []);
  const chart = $derived(res ? sigmaChart(res.leads, res.sigmaKm, view.leadIndex) : null);
  const forecastable = $derived(res?.forecastFraction[view.leadIndex] ?? null);
  const nLeads = $derived(leadCount(s));
  let showLimits = $state(true);
  let showNotes = $state(false);

  const SOURCES: { key: NowcastSourceKind; label: string; hint: string }[] = [
    { key: 'files', label: 'Open files', hint: 'The radar volumes that are open and ticked, in time order' },
    { key: 'live', label: 'Live map', hint: 'The newest frames of the live map’s recent loop (the engine keeps the last four)' },
    { key: 'grid', label: 'Gridded file', hint: 'A NetCDF of gridded frames the engine wrote earlier' },
  ];

  function onSource(k: NowcastSourceKind) {
    setNowcastSource(k);
  }

  async function browse() {
    const p = await pickGridFile();
    if (p) updateNowcastSettings({ gridPath: p });
  }

  function onSlider(e: Event) {
    setLeadIndex(Number((e.currentTarget as HTMLInputElement).value));
  }
  function onSliderKey(e: KeyboardEvent) {
    if (e.key === 'Home') { stepLead('first'); e.preventDefault(); }
    else if (e.key === 'End') { stepLead('last'); e.preventDefault(); }
  }
  function onProduct(e: Event) {
    setProduct((e.currentTarget as HTMLSelectElement).value);
  }
  function onMembers(e: Event) {
    updateNowcastSettings({ members: Number((e.currentTarget as HTMLInputElement).value) });
  }
  function pct(v: number | null): string {
    return v === null ? 'not given' : `${Math.round(v * 100)} %`;
  }
</script>

<CollapsiblePanel title="Nowcast" icon="⏱" badge={res ? 'FCST' : null}>
  <div class="root">
    <p class="notice" role="note">
      <span class="chip">FCST</span>
      <span>A nowcast is a <b>forecast</b>: the last radar frames moved ahead. {NOWCAST_NOT_OBSERVATION} Experimental guidance, not an official forecast.</span>
    </p>

    <!-- Input -->
    <section class="block">
      <h3>Start from</h3>
      <div class="seg" role="radiogroup" aria-label="Frames to start from">
        {#each SOURCES as src}
          <button type="button" role="radio" aria-checked={s.source === src.key} class="seg-btn" class:on={s.source === src.key} title={src.hint} onclick={() => onSource(src.key)}>{src.label}</button>
        {/each}
      </div>
      {#if s.source === 'files'}
        {#if rows.length === 0}
          <p class="help">Open radar volumes from one or several radars (several scan times: the motion comes from the last three). The same ticks as the Mosaic panel.</p>
        {:else}
          <div class="block-head">
            <span class="help">Tick the volumes to use</span>
            <span class="spacer"></span>
            {#if rows.length > 1}
              <button type="button" class="mini" onclick={() => selectAllMosaicFiles(true)}>All</button>
              <button type="button" class="mini" onclick={() => selectAllMosaicFiles(false)}>None</button>
            {/if}
          </div>
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
        {/if}
      {:else if s.source === 'live'}
        <p class="help">Uses the live map&rsquo;s recent loop, so nothing new is downloaded. Open the Live map first and let it update a few times.</p>
      {:else}
        <div class="pathrow">
          <input type="text" placeholder="frames.nc (x, y in metres and a time axis)" value={s.gridPath} onchange={(e) => updateNowcastSettings({ gridPath: e.currentTarget.value })} aria-label="Gridded frames file" />
          <button type="button" class="mini" disabled={!desktop} onclick={browse} title={desktop ? 'Choose the file' : 'File dialogs need the desktop app: type the path'}>Browse</button>
        </div>
      {/if}
      {#if input.problem}
        <p class="help">{input.problem}</p>
      {:else}
        <p class="help mono">{input.summary}</p>
        {#if input.caution}<p class="help caution">{input.caution}</p>{/if}
      {/if}
    </section>

    <!-- Settings -->
    <section class="block">
      <h3>Forecast</h3>
      <div class="grid2">
        <label class="field">
          <span>Lead time (max {NOWCAST_MAX_LEAD_MIN} min)</span>
          <select value={String(s.leadMin)} onchange={(e) => updateNowcastSettings({ leadMin: Number(e.currentTarget.value) })}>
            {#each leadChoices(s.stepMin) as l}<option value={String(l)}>{l} min</option>{/each}
          </select>
        </label>
        <label class="field">
          <span>Step</span>
          <select value={String(s.stepMin)} onchange={(e) => setNowcastStep(Number(e.currentTarget.value))}>
            {#each NOWCAST_STEPS as st}<option value={String(st)}>{st} min</option>{/each}
          </select>
        </label>
        <label class="field wide">
          <span>Ensemble members (0 to {NOWCAST_MAX_MEMBERS})</span>
          <div class="memrow">
            <input type="range" min="0" max={NOWCAST_MAX_MEMBERS} step="1" value={s.members} oninput={onMembers} aria-label="Ensemble members" />
            <span class="mono memval">{s.members}</span>
          </div>
        </label>
        {#if s.source === 'files'}
          <label class="field wide">
            <span>Grid cell</span>
            <select value={String(s.resolutionM)} onchange={(e) => updateNowcastSettings({ resolutionM: Number(e.currentTarget.value) as 1000 | 2000 })}>
              <option value="1000">1 km (the grid the skill was measured on)</option>
              <option value="2000">2 km (faster, coarser)</option>
            </select>
          </label>
        {/if}
      </div>
      <p class="help">
        {nLeads} lead{nLeads === 1 ? '' : 's'}, every {s.stepMin} min.
        {#if s.members === 0}No members: the probabilities are a neighbourhood of the single run (position uncertainty only).
        {:else}{s.members} members with the motion perturbed by its verified error give the probabilities.{/if}
      </p>
    </section>

    <!-- Run -->
    <section class="block">
      {#if run.running}
        <div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow={Math.round(run.percent)} aria-label="Nowcast progress">
          <div class="fill" style="width: {Math.max(4, run.percent)}%"></div>
        </div>
        <div class="progress-row">
          <span class="msg" title={run.message} aria-live="polite">{run.message || 'Working…'}</span>
          <span class="mono">{Math.round(run.percent)}%</span>
          <button type="button" class="mini" onclick={cancelNowcast}>Cancel</button>
        </div>
      {:else}
        <button type="button" class="run" onclick={() => runNowcast()} disabled={blocked !== null} title={blocked ?? 'Make the nowcast'}>
          {res ? 'Run again' : 'Make the nowcast'}
          <span class="onbtn">{s.leadMin} min{s.members ? `, ${s.members} members` : ''}</span>
        </button>
        {#if blocked}<p class="help">{blocked}</p>{/if}
      {/if}
      {#if run.error}
        <div class="error" role="alert">
          <span class="etext">{run.error}</span>
          <button type="button" class="mini" onclick={() => nowcastRun.update((r) => ({ ...r, error: null }))}>Dismiss</button>
        </div>
      {/if}
    </section>

    <!-- Result -->
    {#if res && label}
      <section class="block result" aria-label="Nowcast result">
        <div class="block-head">
          <h3>Forecast frame</h3>
          <span class="spacer"></span>
          <ProvenanceChip provenance={res.provenance ?? { kind: 'forecast', source: 'nowcast' }} title="Nowcast" />
        </div>
        <p class="stamp mono" aria-live="polite">
          <b class="lead">{label.headline}</b><br />
          {label.origin}<br />
          {label.valid}
        </p>

        <div class="sliderbox">
          <button type="button" class="mini" onclick={() => stepLead(-1)} disabled={view.leadIndex <= 0} aria-label="Earlier lead">&lsaquo;</button>
          <button type="button" class="mini" onclick={togglePlaying} disabled={res.leads.length < 2 || !onMap} aria-label={view.playing ? 'Pause the lead loop' : 'Play through the leads'}>{view.playing ? '❚❚' : '▶'}</button>
          <button type="button" class="mini" onclick={() => stepLead(1)} disabled={view.leadIndex >= res.leads.length - 1} aria-label="Later lead">&rsaquo;</button>
          <input
            type="range"
            min="0"
            max={res.leads.length - 1}
            step="1"
            value={view.leadIndex}
            oninput={onSlider}
            onkeydown={onSliderKey}
            disabled={res.leads.length < 2}
            aria-label="Lead time of the forecast"
            aria-valuetext="{label.lead}, valid {formatUtc(res.validTimes[view.leadIndex])}"
          />
        </div>
        <div class="ticks mono"><span>{formatLead(res.leads[0])}</span><span>{formatLead(res.leads[res.leads.length - 1])}</span></div>

        <label class="field">
          <span>Show</span>
          <select value={view.product} onchange={onProduct} disabled={busy === 'layers'}>
            {#each choices as c}<option value={c.key}>{c.label}{$nowcastLoaded.includes(c.key) ? '' : ' (loads)'}</option>{/each}
          </select>
        </label>
        {#if busy === 'layers'}<p class="help" aria-live="polite">Drawing the probabilities for every lead…</p>{/if}

        {#if !onMap}
          <div class="offmap">
            <span>The forecast is not on the map now.</span>
            <button type="button" class="mini" onclick={showNowcastOnMap}>Show on the map</button>
          </div>
        {:else}
          <label class="slider-row">
            <span>Opacity</span>
            <input type="range" min="0" max="100" step="1" value={Math.round($mosaicDisplay.opacity * 100)} oninput={(e) => mosaicDisplay.update((d) => ({ ...d, opacity: Number(e.currentTarget.value) / 100 }))} aria-label="Forecast opacity" />
            <span class="mono">{Math.round($mosaicDisplay.opacity * 100)}%</span>
          </label>
        {/if}

        <dl class="scalars">
          <div class="scalar"><dt>Lead</dt><dd>{label.lead}</dd></div>
          <div class="scalar"><dt>Position (1σ)</dt><dd>{formatSigma(res.sigmaKm[view.leadIndex])}</dd></div>
          <div class="scalar"><dt>Forecastable</dt><dd>{pct(forecastable)}</dd></div>
          <div class="scalar"><dt>Motion u, v</dt><dd>{res.motion.u === null || res.motion.v === null ? 'n/a' : `${res.motion.u.toFixed(1)}, ${res.motion.v.toFixed(1)}`}<span class="unit">m/s</span></dd></div>
          <div class="scalar"><dt>Members</dt><dd>{res.members || 'none'}</dd></div>
          <div class="scalar"><dt>Frames</dt><dd>{res.nInputFrames || 'n/a'}</dd></div>
        </dl>

        {#if chart}
          <svg class="plot" viewBox="0 0 {chart.width} {chart.height}" role="img" aria-label="Position uncertainty, one sigma in kilometres, against lead time. At the shown lead it is {formatSigma(res.sigmaKm[view.leadIndex])}.">
            {#each chart.ticksY as t}
              <line x1={chart.left} x2={chart.width - chart.right} y1={t.y} y2={t.y} class="grid" />
              <text x={chart.left - 4} y={t.y + 3} text-anchor="end" class="tick">{t.label}</text>
            {/each}
            {#if chart.points.length > 1}
              <polyline class="sig" points={chart.points.map((p) => `${p.x},${p.y}`).join(' ')} />
            {/if}
            {#each chart.points as p}<circle cx={p.x} cy={p.y} r="2" class="sigpt"><title>{formatLead(p.lead)}: {formatSigma(p.km)}</title></circle>{/each}
            {#if chart.markX !== null}<line x1={chart.markX} x2={chart.markX} y1={chart.top} y2={chart.height - chart.bottom} class="mark" />{/if}
            <text x={chart.left + (chart.width - chart.left - chart.right) / 2} y={chart.height - 3} text-anchor="middle" class="axis">lead time (min), position uncertainty 1σ (km)</text>
          </svg>
        {/if}

        <div class="statements">
          <p class="guide">{res.guidance}</p>
          <p class="skill">{res.skill}</p>
        </div>

        {#if res.notes.length}
          <button type="button" class="disclosure" onclick={() => (showNotes = !showNotes)} aria-expanded={showNotes}>
            <span class="caret" class:open={showNotes}>&#9656;</span> Notes from the run ({res.notes.length})
          </button>
          {#if showNotes}<ul class="notes">{#each res.notes as n}<li>{n}</li>{/each}</ul>{/if}
        {/if}

        <div class="btn-row">
          <button type="button" class="mini" onclick={showNowcastOnMap} title="Draw this frame and fit the map to it">Fit to map</button>
          {#if onMap}<button type="button" class="mini" onclick={hideNowcastFromMap}>Observations</button>{/if}
          <span class="spacer"></span>
          <button type="button" class="mini" disabled={busy === 'picture'} onclick={saveFramePicture} title="Save this frame as a PNG; the whole FORECAST label is part of the picture">Picture</button>
          {#each ['nc', 'json', 'png'] as f}
            <button type="button" class="mini" disabled={!desktop || busy === 'export'} onclick={() => exportNowcast(f as 'nc' | 'json' | 'png')} title={desktop ? (f === 'nc' ? 'The whole nowcast as CF NetCDF (forecast attributes, run record inside)' : f === 'json' ? 'The reproducible run record' : 'The overview figure (every panel says FORECAST)') : 'Saving files needs the desktop app'}>{f === 'nc' ? 'NetCDF' : f === 'json' ? 'Record' : 'Figure'}</button>
          {/each}
          <button type="button" class="mini danger" onclick={clearNowcast}>Clear</button>
        </div>
        {#if !desktop}<p class="help">NetCDF, record and figure are saved in the desktop app. The picture of the frame also works in the browser.</p>{/if}
      </section>
    {/if}

    <!-- Limits -->
    <section class="block limits">
      <button type="button" class="disclosure strong" onclick={() => (showLimits = !showLimits)} aria-expanded={showLimits}>
        <span class="caret" class:open={showLimits}>&#9656;</span> What it can and cannot do (measured)
      </button>
      {#if showLimits}
        <ul class="notes">{#each VERIFIED_LIMITS as l}<li>{l}</li>{/each}</ul>
        <p class="help">Scored on eight public NEXRAD cases that were never used to tune it, next to holding the last frame still. The numbers, cases and method are in the Nowcast page of the manual.</p>
      {/if}
    </section>
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
  .help { margin: 0; font-size: var(--fs-xs); color: var(--c-muted); line-height: 1.4; }
  .help.caution { color: var(--c-caution); }

  .notice {
    display: flex; gap: var(--space-2); align-items: flex-start; margin: 0; padding: 6px var(--space-2);
    font-size: var(--fs-xs); line-height: 1.4; color: var(--c-text-dim);
    background: rgba(var(--c-alarm-rgb), 0.07); border: 1px solid rgba(var(--c-alarm-rgb), 0.45); border-left: 3px solid var(--c-alarm); border-radius: var(--radius-md);
  }
  .notice b { color: var(--c-text); }
  .chip { flex: none; display: inline-block; padding: 0 7px; font-size: var(--fs-2xs); font-weight: 700; letter-spacing: 0.06em; color: var(--c-alarm); border: 1px double var(--c-alarm); border-radius: 9px; }

  .mini { height: auto; padding: 2px var(--space-2); font-size: var(--fs-xs); color: var(--c-text-dim); background: transparent; border: 1px solid var(--c-border-strong); border-radius: var(--radius-sm); cursor: pointer; white-space: nowrap; }
  .mini:hover:not(:disabled) { color: var(--c-text); border-color: var(--c-accent); background: transparent; }
  .mini.danger:hover:not(:disabled) { color: var(--c-alarm); border-color: var(--c-alarm); }
  .mini:disabled { opacity: 0.45; cursor: not-allowed; }
  button:focus-visible, input:focus-visible, select:focus-visible { outline: 2px solid var(--c-accent); outline-offset: 1px; }

  .files { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-1); max-height: 150px; overflow-y: auto; }
  .files li { padding: 3px 6px; background: var(--c-surface-0); border: 1px solid var(--c-border); border-radius: var(--radius-md); }
  .files li.off { opacity: 0.55; }
  .files label { display: flex; align-items: center; gap: var(--space-2); cursor: pointer; font-size: var(--fs-xs); }
  .files input { accent-color: var(--c-accent); }
  .fname { font-weight: 600; color: var(--c-text); min-width: 3.2em; }

  .seg { display: flex; border: 1px solid var(--c-border-strong); border-radius: var(--radius-md); overflow: hidden; }
  .seg-btn { flex: 1; height: 26px; padding: 0 6px; font-size: var(--fs-xs); color: var(--c-text-dim); background: transparent; border: none; border-radius: 0; text-transform: none; letter-spacing: 0; }
  .seg-btn + .seg-btn { border-left: 1px solid var(--c-border-strong); }
  .seg-btn.on { color: var(--c-on-accent); background: var(--c-accent); }

  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-2); }
  .field { display: flex; flex-direction: column; gap: 3px; min-width: 0; font-size: var(--fs-2xs); color: var(--c-muted); text-transform: uppercase; letter-spacing: 0.04em; }
  .field.wide { grid-column: 1 / -1; }
  .field select { width: 100%; box-sizing: border-box; height: 26px; font-size: var(--fs-sm); text-transform: none; letter-spacing: 0; }
  .memrow { display: flex; align-items: center; gap: var(--space-2); }
  .memrow input { flex: 1; min-width: 0; accent-color: var(--c-accent); }
  .memval { min-width: 1.4em; text-align: right; color: var(--c-text); font-size: var(--fs-sm); text-transform: none; letter-spacing: 0; }
  .pathrow { display: flex; gap: var(--space-1); }
  .pathrow input { flex: 1; min-width: 0; height: 26px; font-size: var(--fs-xs); }

  .run { height: 32px; gap: var(--space-2); font-weight: 600; color: var(--c-on-accent); background: var(--c-accent); border-color: var(--c-accent); }
  .run:hover:not(:disabled) { background: var(--c-accent-strong); border-color: var(--c-accent-strong); }
  .onbtn { font-size: var(--fs-2xs); font-weight: 500; opacity: 0.8; }
  .progress { height: 6px; overflow: hidden; background: var(--c-surface-0); border: 1px solid var(--c-border); border-radius: var(--radius-sm); }
  .fill { height: 100%; background: var(--c-accent); transition: width var(--dur-base) var(--ease-out); }
  .progress-row { display: flex; align-items: center; gap: var(--space-2); font-size: var(--fs-xs); color: var(--c-text-dim); }
  .msg { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .error { display: flex; align-items: flex-start; gap: var(--space-2); padding: 6px var(--space-2); font-size: var(--fs-xs); color: var(--c-alarm); background: rgba(var(--c-alarm-rgb), 0.08); border: 1px solid var(--c-alarm); border-radius: var(--radius-md); }
  .etext { flex: 1; min-width: 0; word-break: break-word; user-select: text; }

  .stamp { margin: 0; padding: 4px 8px; font-size: var(--fs-xs); line-height: 1.5; color: var(--c-text-dim); background: var(--c-surface-0); border: 1px solid var(--c-border); border-left: 3px solid var(--c-alarm); border-radius: var(--radius-md); }
  .stamp .lead { color: var(--c-alarm); font-size: var(--fs-sm); letter-spacing: 0.04em; }
  .sliderbox { display: flex; align-items: center; gap: var(--space-1); }
  .sliderbox input[type='range'] { flex: 1; min-width: 0; accent-color: var(--c-alarm); }
  .ticks { display: flex; justify-content: space-between; margin: -4px 0 0 76px; font-size: var(--fs-2xs); color: var(--c-muted); }
  .offmap { display: flex; align-items: center; gap: var(--space-2); font-size: var(--fs-xs); color: var(--c-caution); }
  .slider-row { display: flex; align-items: center; gap: var(--space-2); font-size: var(--fs-xs); color: var(--c-muted); }
  .slider-row input[type='range'] { flex: 1; min-width: 0; accent-color: var(--c-accent); }

  .scalars { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--space-1); margin: 0; }
  .scalar { padding: 4px 6px; background: var(--c-surface-0); border: 1px solid var(--c-border); border-radius: var(--radius-md); min-width: 0; }
  .scalar dt { font-size: 9px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--c-muted); }
  .scalar dd { margin: 0; font-size: var(--fs-sm); font-family: var(--font-mono); font-variant-numeric: tabular-nums; color: var(--c-text); white-space: nowrap; }
  .unit { margin-left: 3px; font-size: 9px; color: var(--c-muted); }

  .plot { width: 100%; height: auto; }
  .plot .grid { stroke: var(--c-border); stroke-width: 0.6; }
  .plot .tick { font-size: 8px; fill: var(--c-muted); font-family: var(--font-mono); }
  .plot .axis { font-size: 8px; fill: var(--c-text-dim); }
  .plot .sig { fill: none; stroke: var(--c-alarm); stroke-width: 1.5; }
  .plot .sigpt { fill: var(--c-alarm); }
  .plot .mark { stroke: var(--c-text); stroke-width: 1; stroke-dasharray: 3 2; }

  .statements { display: flex; flex-direction: column; gap: 3px; padding: 5px 8px; background: rgba(var(--c-alarm-rgb), 0.05); border: 1px solid var(--c-border); border-radius: var(--radius-md); }
  .statements p { margin: 0; font-size: var(--fs-xs); line-height: 1.4; }
  .guide { font-weight: 700; color: var(--c-text); }
  .skill { color: var(--c-text-dim); }

  .disclosure { align-self: flex-start; height: auto; padding: 0; font-size: var(--fs-xs); font-weight: 600; color: var(--c-muted); background: transparent; border: none; box-shadow: none; }
  .disclosure.strong { color: var(--c-text-dim); }
  .disclosure:hover { color: var(--c-text); background: transparent; box-shadow: none; }
  .caret { display: inline-block; transition: transform var(--dur-fast) var(--ease-out); }
  .caret.open { transform: rotate(90deg); }
  .notes { margin: 0; padding-left: 16px; font-size: var(--fs-xs); color: var(--c-text-dim); line-height: 1.45; display: flex; flex-direction: column; gap: 3px; }
  .limits { padding-top: var(--space-2); border-top: 1px solid var(--c-border); }
  .btn-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
</style>
