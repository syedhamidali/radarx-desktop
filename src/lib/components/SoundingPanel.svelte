<script lang="ts">
  /**
   * Sounding: a radiosonde profile as a Skew-T log-p diagram and a hodograph, with the parcel,
   * shear, helicity, freezing-level and moisture numbers the engine computes. Every number says
   * which parcel it is, its unit and its height reference (m AGL = above the launch site); hover a
   * label or open "Definitions" for the definition and reference. Launch time, station, and a note
   * on balloon drift are always shown. Files are read locally; an archive sounding is downloaded
   * only after the approval dialog.
   */
  import { portal } from '../utils/portal';
  import { focusTrap } from '../utils/focusTrap';
  import { sounding, selectedParcel, openSoundingFile, openExampleSounding, fetchArchiveSounding, clearSounding } from '../stores/sounding';
  import { envStatus, envSettingsError, setEnvSettings, clearEnvCache, loadEnvStatus } from '../stores/environment';
  import { connectionStatus } from '../stores/radarData';
  import { formatUtc } from '../utils/provenance';
  import {
    PARCEL_KEYS,
    PARCEL_SHORT,
    parcelRows,
    shearRows,
    srhRows,
    levelRows,
    launchTimeUtc,
    validStation,
    fmt,
    type ParcelDiag,
    type Row,
  } from '../utils/soundingView';
  import { formatBytes } from '../utils/updateLogic';
  import CollapsiblePanel from './CollapsiblePanel.svelte';
  import ProvenanceChip from './ProvenanceChip.svelte';
  import SkewT from './SkewT.svelte';
  import Hodograph from './Hodograph.svelte';

  const st = $derived($sounding);
  const data = $derived(st.data);
  const diag = $derived(data?.diagnostics);
  const connected = $derived($connectionStatus === 'connected');
  const parcel = $derived((diag?.parcels?.[$selectedParcel] as ParcelDiag | undefined) ?? undefined);
  const defs = $derived((diag?.definitions ?? {}) as Record<string, string>);
  const noUnit = $derived(!!data?.notes.some((n) => n.includes('no unit')));

  let lastPath = $state('');
  let showArchive = $state(false);
  let station = $state('');
  let launch = $state('');
  let archiveError = $state('');
  let enlarged = $state(false);
  let showSettings = $state(false);
  let cacheMb = $state<number | null>(null);
  let cacheDir = $state('');

  $effect(() => {
    const s = $envStatus?.settings;
    if (s) {
      cacheMb = s.cache_max_mb;
      cacheDir = s.cache_dir;
    }
  });

  async function chooseFile() {
    let path: string | null = null;
    try {
      const { open } = await import('@tauri-apps/plugin-dialog');
      const picked = await open({ multiple: false, title: 'Open a sounding file' });
      path = Array.isArray(picked) ? (picked[0] as string) : (picked as string | null);
    } catch {
      path = prompt('Path of a sounding file (Wyoming text/CSV, SHARPpy, IEM JSON, CSV):');
    }
    if (path) {
      lastPath = path.trim();
      openSoundingFile(lastPath);
    }
  }

  function reread(units: string) {
    if (lastPath) openSoundingFile(lastPath, { windUnits: units });
  }

  function askArchive() {
    archiveError = '';
    if (!validStation(station)) {
      archiveError = 'Enter a station id of 3 to 12 letters or digits, for example KJAN or 72235.';
      return;
    }
    const t = launchTimeUtc(launch);
    if (!t) {
      archiveError = 'Enter the launch time in UTC, for example 2022-03-30 00:00 (usually 00 or 12 UTC).';
      return;
    }
    void fetchArchiveSounding(station, t);
  }

  function applyCache() {
    const changes: Record<string, unknown> = {};
    if (cacheMb !== null && cacheMb !== $envStatus?.settings.cache_max_mb) changes.cache_max_mb = cacheMb;
    if (cacheDir.trim() !== ($envStatus?.settings.cache_dir ?? '')) changes.cache_dir = cacheDir.trim();
    if (Object.keys(changes).length) setEnvSettings(changes as any);
  }

  const tables = $derived<{ title: string; rows: Row[] }[]>(
    diag
      ? [
          { title: `${PARCEL_SHORT[$selectedParcel]} parcel`, rows: parcelRows(parcel) },
          { title: 'Wind shear', rows: shearRows(diag.shear) },
          { title: 'Helicity and storm motion', rows: srhRows(diag.srh, diag.storm_motion) },
          { title: 'Levels and moisture', rows: levelRows(diag.levels, diag.moisture) },
        ].filter((t) => t.rows.length)
      : [],
  );

  $effect(() => {
    if (!enlarged) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        enlarged = false;
      }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  });
</script>

{#snippet numbers()}
  {#if data && diag}
    <div class="parcels" role="group" aria-label="Parcel">
      {#each PARCEL_KEYS as k (k)}
        <button aria-pressed={$selectedParcel === k} class:on={$selectedParcel === k} onclick={() => selectedParcel.set(k)} title={diag.parcel_definitions?.[k]}>{PARCEL_SHORT[k]}</button>
      {/each}
    </div>
    {#if parcel}
      <p class="plabel">
        {parcel.label}{#if parcel.start}. Starts at {fmt(parcel.start.pressure_hpa, 'hPa')}, {fmt(parcel.start.temperature_c, '°C', 1)}, dew point {fmt(parcel.start.dewpoint_c, '°C', 1)}.{/if}
      </p>
      {#if parcel.note}<p class="warn">{parcel.note}</p>{/if}
    {/if}
    {#each tables as t (t.title)}
      <table class="nums" data-testid="sounding-table">
        <caption>{t.title}</caption>
        <tbody>
          {#each t.rows as r (r.label)}
            <tr>
              <th scope="row"><abbr title={defs[r.key] ?? ''}>{r.label}</abbr></th>
              <td class="mono">{r.value}{#if r.sub}<span class="sub"> {r.sub}</span>{/if}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/each}
    <p class="unitnote">Heights are metres above the launch site ({fmt(data.surface_height_m, 'm')} above sea level).</p>
  {/if}
{/snippet}

<CollapsiblePanel title="Sounding" badge={data ? data.station || 'file' : null}>
  <div class="snd" data-testid="sounding-panel">
    <div class="actions">
      <button onclick={chooseFile} disabled={!connected || st.loading}>Open file...</button>
      <button onclick={openExampleSounding} disabled={!connected || st.loading} title="A synthetic sounding (Weisman and Klemp 1982 thermodynamics), labelled simulated">Example</button>
      <button class:on={showArchive} aria-pressed={showArchive} onclick={() => (showArchive = !showArchive)} disabled={!connected}>Archive...</button>
      {#if data}<button onclick={() => (enlarged = true)} title="Larger diagram">Enlarge</button><button class="quiet" onclick={clearSounding}>Clear</button>{/if}
    </div>

    {#if showArchive}
      <form class="archive" onsubmit={(e) => { e.preventDefault(); askArchive(); }}>
        <label>Station <input bind:value={station} placeholder="KJAN or 72235" maxlength="12" spellcheck="false" autocomplete="off" /></label>
        <label>Launch (UTC) <input bind:value={launch} placeholder="2022-03-30 00:00" spellcheck="false" autocomplete="off" /></label>
        <button type="submit" class="primary" disabled={st.loading}>Ask</button>
        <p class="hint">Iowa Environmental Mesonet radiosonde archive. You see the source, the size and where it is saved, and approve, before anything is downloaded.</p>
        {#if archiveError}<p class="err" role="alert">{archiveError}</p>{/if}
      </form>
    {/if}

    {#if st.loading}<p class="hint" role="status">Reading the sounding...</p>{/if}
    {#if st.error}<p class="err" role="alert">{st.error}</p>{/if}

    {#if !data && !st.loading && !st.error}
      <p class="hint">Open a radiosonde file (University of Wyoming text or CSV, SPC / SHARPpy, IEM JSON, IGRA2, or a CSV with named columns) to see its Skew-T, hodograph and the parameters. Nothing is downloaded unless you ask for the archive and approve.</p>
    {/if}

    {#if data && diag}
      <div class="head">
        <ProvenanceChip provenance={data.provenance} title={data.provenance?.title} />
        <div class="id">
          <strong>{data.station || 'Sounding'}</strong>{#if data.station_name} <span class="dim">{data.station_name}</span>{/if}
          <div class="mono dim">{data.launch_time ? formatUtc(data.launch_time) : 'launch time not in the file'}</div>
        </div>
      </div>
      <p class="drift">{data.drift_note}</p>
      {#if noUnit && lastPath}
        <p class="warn">The wind speed has no unit in this file; m/s was assumed. Read it as
          <button class="link" onclick={() => reread('kt')}>knots</button> or <button class="link" onclick={() => reread('m/s')}>m/s</button>.</p>
      {/if}

      <SkewT {data} {parcel} />
      <Hodograph {data} />
      {@render numbers()}

      {#if diag.notes?.length}
        <ul class="notes" aria-label="Notes about this sounding">
          {#each diag.notes as n (n)}<li>{n}</li>{/each}
        </ul>
      {/if}
      <details class="defs">
        <summary>Definitions and references</summary>
        <dl>
          {#each Object.entries(defs) as [k, text] (k)}<dt>{k}</dt><dd>{text}</dd>{/each}
          {#each Object.entries(diag.parcel_definitions ?? {}) as [k, text] (k)}<dt>{k}</dt><dd>{text}</dd>{/each}
        </dl>
        <ul>{#each Object.values(diag.references ?? {}) as r (r)}<li>{r}</li>{/each}</ul>
        <p class="dim">Parcel thermodynamics: radarx {diag.library?.radarx}. Source file: {data.file || 'n/a'} ({data.format}, {data.levels.n_levels} levels{data.levels.thinned ? ', thinned for drawing only' : ''}). The parcel path is not drawn.</p>
      </details>
    {/if}

    <details class="settings" bind:open={showSettings} ontoggle={() => showSettings && loadEnvStatus()}>
      <summary>Environment data settings</summary>
      {#if $envStatus}
        {@const s = $envStatus.settings}
        <label class="chk"><input type="checkbox" checked={s.effective_offline} disabled={s.locked.includes('offline')} onchange={(e) => setEnvSettings({ offline: (e.target as HTMLInputElement).checked })} /> Offline mode: never use the network (cached data keeps working){#if s.locked.includes('offline')} <span class="dim">(set by XRADAR_OFFLINE)</span>{/if}</label>
        <label>Cache size limit (MB) <input type="number" min="16" step="64" bind:value={cacheMb} disabled={s.locked.includes('cache_max_mb')} onchange={applyCache} /></label>
        <label>Cache folder <input bind:value={cacheDir} placeholder={s.default_cache_dir} disabled={s.locked.includes('cache_dir')} onchange={applyCache} spellcheck="false" /></label>
        <p class="hint mono">{formatBytes($envStatus.cache.bytes) || '0 B'} used of {formatBytes($envStatus.cache.max_bytes)}, {$envStatus.cache.files} files, in {$envStatus.cache.root}</p>
        <button onclick={clearEnvCache}>Clear the cache</button>
        {#if $envSettingsError}<p class="err" role="alert">{$envSettingsError}</p>{/if}
        {#if $envStatus.providers.length}
          <h5>Sources</h5>
          <ul class="srcs">
            {#each $envStatus.providers as p (p.id)}
              <li><ProvenanceChip provenance={{ kind: p.kind as any, source: p.name }} compact title={p.name} /> <span><strong>{p.name}</strong>. {p.attribution} <span class="dim">{p.licence}</span></span></li>
            {/each}
          </ul>
        {/if}
      {:else}
        <p class="hint">Loading...</p>
      {/if}
    </details>
  </div>
</CollapsiblePanel>

{#if enlarged && data && diag}
  <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
  <div class="big-overlay" use:portal onclick={(e) => { if (e.target === e.currentTarget) enlarged = false; }}>
    <div class="big" role="dialog" aria-modal="true" aria-label="Sounding, enlarged" use:focusTrap={{ initial: '.close' }}>
      <header>
        <ProvenanceChip provenance={data.provenance} title={data.provenance?.title} />
        <strong>{data.station || 'Sounding'}</strong>
        <span class="mono dim">{data.launch_time ? formatUtc(data.launch_time) : 'launch time not in the file'}</span>
        <button class="close" onclick={() => (enlarged = false)}>Close</button>
      </header>
      <div class="cols">
        <div class="col"><SkewT {data} {parcel} /></div>
        <div class="col side"><Hodograph {data} />{@render numbers()}</div>
      </div>
      <p class="drift">{data.drift_note}</p>
    </div>
  </div>
{/if}

<style>
  .snd { display: flex; flex-direction: column; gap: var(--space-3); padding: var(--space-3); }
  .actions { display: flex; flex-wrap: wrap; gap: var(--space-2); }
  .actions .on { border-color: var(--c-accent); }
  .actions .quiet { margin-left: auto; background: transparent; border-color: transparent; color: var(--c-muted); }
  .hint { margin: 0; font-size: var(--fs-xs); color: var(--c-text-dim); line-height: 1.5; }
  .err { margin: 0; font-size: var(--fs-xs); color: var(--c-alarm); }
  .warn { margin: 0; font-size: var(--fs-xs); color: var(--c-caution); line-height: 1.5; }
  .dim { color: var(--c-muted); }
  .mono { font-family: var(--font-mono); font-size: 11px; }
  .archive { display: grid; gap: var(--space-2); padding: var(--space-2); border: 1px solid var(--c-border); border-radius: var(--radius-sm); }
  label { display: flex; flex-direction: column; gap: 2px; font-size: var(--fs-xs); color: var(--c-text-dim); }
  label.chk { flex-direction: row; align-items: flex-start; gap: var(--space-2); }
  .head { display: flex; align-items: flex-start; gap: var(--space-2); }
  .id { font-size: var(--fs-sm); line-height: 1.3; }
  .drift { margin: 0; font-size: var(--fs-xs); color: var(--c-text-dim); line-height: 1.45; }
  .parcels { display: flex; gap: 2px; }
  .parcels button { flex: 1; padding: 0 4px; font-size: var(--fs-xs); }
  .parcels button.on { border-color: var(--c-accent); color: var(--c-accent); background: rgba(var(--c-accent-rgb), 0.08); }
  .plabel { margin: 0; font-size: var(--fs-xs); color: var(--c-text-dim); line-height: 1.45; }
  table.nums { width: 100%; border-collapse: collapse; font-size: var(--fs-xs); }
  caption { text-align: left; color: var(--c-muted); text-transform: uppercase; letter-spacing: var(--ls-label); font-size: 10px; padding: var(--space-2) 0 2px; }
  th { text-align: left; font-weight: 400; color: var(--c-text-dim); padding: 2px 0; }
  th abbr { text-decoration: none; border-bottom: 1px dotted var(--c-muted); cursor: help; }
  td { text-align: right; padding: 2px 0; color: var(--c-text); }
  .sub { color: var(--c-muted); margin-left: 8px; }
  .unitnote { margin: 0; font-size: 10.5px; color: var(--c-muted); }
  .notes { margin: 0; padding-left: 16px; font-size: var(--fs-xs); color: var(--c-caution); line-height: 1.45; }
  .defs, .settings { font-size: var(--fs-xs); color: var(--c-text-dim); }
  summary { cursor: pointer; color: var(--c-text-dim); text-transform: uppercase; letter-spacing: var(--ls-label); font-size: 10.5px; }
  dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px var(--space-2); margin: var(--space-2) 0; }
  dt { font-family: var(--font-mono); color: var(--c-muted); }
  dd { margin: 0; }
  h5 { margin: var(--space-2) 0 2px; font-size: 10.5px; color: var(--c-muted); text-transform: uppercase; letter-spacing: var(--ls-label); font-weight: 500; }
  .srcs { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
  .srcs li { display: flex; gap: var(--space-2); align-items: flex-start; }
  .link { background: none; border: none; padding: 0; height: auto; color: var(--c-accent); text-decoration: underline; cursor: pointer; font-size: inherit; }
  .big-overlay { position: fixed; inset: 0; z-index: 930; background: var(--c-scrim); display: flex; align-items: center; justify-content: center; }
  .big { width: min(1040px, 96vw); max-height: 94vh; overflow: auto; background: var(--c-overlay); border: 1px solid var(--c-border-strong); border-radius: var(--radius-sm); box-shadow: var(--shadow-3); padding: var(--space-4); display: flex; flex-direction: column; gap: var(--space-3); }
  .big header { display: flex; align-items: center; gap: var(--space-3); }
  .big header .close { margin-left: auto; }
  .cols { display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr); gap: var(--space-4); }
  .col.side { display: flex; flex-direction: column; gap: var(--space-3); }
  @media (max-width: 760px) { .cols { grid-template-columns: 1fr; } }
</style>
