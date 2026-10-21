<script lang="ts">
  /**
   * RadarNetworkCard - the card of a site clicked on the radar network map: what the catalogue
   * says (with the source, its licence and retrieval date), what the engine said about the site's
   * latest volume (only after consent), and the actions that exist for this site.
   *
   * Every time shown is UTC and says where it comes from; no state is shown that the engine has
   * not reported for this very site in this session.
   */
  import { onMount } from 'svelte';
  import { ACCESS_INFO, requestDefaultRadar, type CatalogueSite, type RadarCatalogue } from '../utils/radarCatalogue';
  import { scanState, liveAge, formatAge, formatBytes } from '../utils/radarSites';
  import { siteChecks } from '../stores/radarSites';
  import { connectionStatus } from '../stores/radarData';
  import {
    providers,
    loadProviders,
    networkChecks,
    nexradSiteOf,
    checkLatestFor,
    openLatestFor,
    browseArchiveFor,
    listNetworkScans,
    openNetworkScan,
    type NetworkScan,
  } from '../stores/radarNetworks';
  import { addToast } from '../stores/toasts';

  let { site, catalogue, now, onclose }: { site: CatalogueSite; catalogue: RadarCatalogue; now: number; onclose: () => void } = $props();

  const connected = $derived($connectionStatus === 'connected');
  const access = $derived(ACCESS_INFO[site.data_access]);
  const source = $derived(catalogue.sourceOf(site));
  const loadable = $derived(site.data_access === 'open-loadable' && !!site.provider && !!site.provider_site);
  const isNexrad = $derived(loadable && site.provider === 'nexrad');
  const provider = $derived(site.provider && site.provider !== 'nexrad' ? ($providers?.[site.provider] ?? null) : null);

  // The engine's answer for this site (NEXRAD: the Radar sites store; others: the network store).
  const check = $derived.by(() => {
    if (!loadable) return null;
    if (isNexrad) return $siteChecks[site.provider_site!.toUpperCase()] ?? null;
    return $networkChecks[`${site.provider}:${site.provider_site}`] ?? null;
  });
  const info = $derived(check?.info as any);
  const age = $derived(check?.state === 'ok' ? liveAge(info, check.receivedAt, now) : null);
  const scanSt = $derived(scanState(age));
  const busy = $derived(check?.state === 'loading');

  // Archive picker of the network providers (NEXRAD goes to the Cloud panel's Archive tab).
  let archiveOpen = $state(false);
  let archiveDate = $state('');
  let scans = $state<NetworkScan[]>([]);
  let listing = $state(false);
  let listError = $state('');

  onMount(() => {
    if (loadable && !isNexrad && connected) void loadProviders();
  });
  $effect(() => {
    if (!archiveDate) archiveDate = provider?.default_date || new Date().toISOString().slice(0, 10);
  });

  const fmtDeg = (v: number, pos: string, neg: string) => `${Math.abs(v).toFixed(4)}° ${v >= 0 ? pos : neg}`;
  const utc = (iso?: string) => (iso ? iso.replace('T', ' ').replace(/(\.\d+)?Z$/, ' UTC') : '');
  const clock = (iso?: string) => (iso ? iso.slice(11, 19) + ' UTC' : '');

  async function listDay() {
    if (!site.provider || !site.provider_site || !archiveDate) return;
    listing = true;
    listError = '';
    scans = [];
    try {
      scans = await listNetworkScans(site.provider, site.provider_site, archiveDate);
      if (scans.length === 0) listError = `No volume of ${site.provider_site} listed for ${archiveDate} (UTC).`;
    } catch (e) {
      listError = (e as Error).message;
    } finally {
      listing = false;
    }
  }

  function openLink(url: string) {
    if ((window as any).__TAURI_INTERNALS__) {
      void import('../utils/updater').then((m) => m.openExternal(url));
    } else {
      window.open(url, '_blank', 'noopener');
    }
  }

  function setDefault() {
    requestDefaultRadar(site);
    addToast('info', `Default radar request sent for ${site.id} (the default-radar setting is being built; nothing is saved yet).`);
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.stopPropagation();
      onclose();
    }
  }
  const stop = (e: Event) => e.stopPropagation();
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<!-- Pointer events are stopped too: the map underneath pans and selects on pointerdown/up. -->
<aside class="rn-card brackets" aria-label="Radar site {site.id}" onkeydown={onKey} onpointerdown={stop} onpointerup={stop} onmousedown={stop} onmouseup={stop} onclick={stop} ondblclick={stop} oncontextmenu={stop} onwheel={stop} data-testid="rn-card">
  <header>
    <div class="ttl">
      <span class="sid mono">{site.id}</span>
      <span class="nm">{site.name}{site.region ? `, ${site.region}` : ''}, {site.country}</span>
    </div>
    <button type="button" class="x" aria-label="Close site card" onclick={onclose}>&times;</button>
  </header>

  <div class="chips">
    <span class="chip access" data-access={site.data_access}><span aria-hidden="true">{access.glyph}</span> {access.label}</span>
    <span class="chip">{site.network}</span>
    {#if site.band}<span class="chip">{site.band}-band{site.polarisation ? `, ${site.polarisation}-pol` : ''}</span>{/if}
    <span class="chip" class:caution={!site.status.startsWith('operational')}>{site.status}</span>
  </div>
  <p class="meaning">{access.meaning}</p>

  <dl class="facts">
    <dt>Position</dt>
    <dd class="mono">{fmtDeg(site.lat, 'N', 'S')}, {fmtDeg(site.lon, 'E', 'W')}</dd>
    {#if site.elev_m !== null}
      <dt>Elevation</dt>
      <dd class="mono">{Math.round(site.elev_m)} m (as given by the source)</dd>
    {/if}
    {#if site.wigos || site.wmo}
      <dt>Ids</dt>
      <dd class="mono small">{[site.wigos ? `WIGOS ${site.wigos}` : '', site.wmo ? `WMO ${site.wmo}` : ''].filter(Boolean).join(', ')}</dd>
    {/if}
    <dt>Source</dt>
    <dd>{source.name}, retrieved {site.retrieved}</dd>
    <dt>Licence</dt>
    <dd>{source.licence}</dd>
    {#if source.attribution}
      <dt>Credit</dt>
      <dd>{source.attribution}</dd>
    {/if}
  </dl>
  {#if site.note}<p class="note">{site.note}</p>{/if}

  {#if loadable}
    <section class="scan" aria-live="polite">
      <h4 class="hud-label">Latest volume</h4>
      {#if provider}
        <p class="fine">{provider.name}. {provider.attribution}. Licence: {provider.licence}.</p>
        {#if provider.archive_only}<p class="note warn">Archive sample only: "latest" is the newest file of a fixed sample, not a live feed.</p>{/if}
      {/if}
      {#if !check}
        <p class="note">Not checked. Checking asks you first, then lists the site's folder{isNexrad ? ' on AWS S3' : ` on ${provider?.host.split(' ')[0] ?? 'the server'}`}.</p>
      {:else if check.state === 'loading'}
        <p class="note">Listing...</p>
      {:else if check.state === 'ok' && info}
        <p class="scan-line">
          <span class="chip" class:nominal={scanSt === 'recent'} class:caution={scanSt === 'stale'}>{scanSt === 'recent' ? 'recent' : 'older than 10 min'}</span>
          <span class="mono">{age === null ? '' : formatAge(age) + ' old'}</span>
        </p>
        <dl class="facts tight">
          <dt>Volume</dt>
          <dd class="mono small">{info.name}</dd>
          <dt>Scan time</dt>
          <dd class="mono">{utc(info.scan_time)}</dd>
          <dt>Size</dt>
          <dd class="mono">{formatBytes(info.size)}{info.n_files > 1 ? ` in ${info.n_files} files` : ''}</dd>
        </dl>
        <p class="fine">Listed {clock(info.now)} (engine clock). Scan time is the volume start from the file name; the age is counted from it.</p>
      {:else if check.state === 'not_found'}
        <p class="note warn">No volume found for {site.provider_site} in the listing.</p>
      {:else if check.state === 'offline'}
        <p class="note warn">Offline: {check.message || 'the server could not be reached.'}</p>
      {:else}
        <p class="note warn">{check.message || 'The check failed.'}</p>
      {/if}
      {#if !busy}
        <button type="button" class="link" onclick={() => checkLatestFor(site, true)} disabled={!connected}>{check ? 'Check again' : 'Check latest volume'}</button>
      {/if}
    </section>
  {/if}

  <div class="actions">
    {#if loadable}
      <button type="button" class="primary" disabled={!connected || busy} onclick={() => openLatestFor(site)} data-testid="rn-open-latest">
        Open latest scan{check?.state === 'ok' && info?.size ? ` (${formatBytes(info.size)})` : ''}
      </button>
      {#if isNexrad}
        <button type="button" disabled={!connected} onclick={() => browseArchiveFor(site)}>Choose a time (archive)</button>
      {:else}
        <button type="button" disabled={!connected} onclick={() => { archiveOpen = !archiveOpen; }} aria-expanded={archiveOpen}>Choose a time (archive)</button>
      {/if}
    {/if}
    {#if site.url}
      <button type="button" onclick={() => openLink(site.url!)}>Open the provider's page</button>
    {:else if provider?.homepage}
      <button type="button" onclick={() => openLink(provider!.homepage)}>Open the provider's page</button>
    {/if}
    <button type="button" onclick={setDefault} title="Sends a request to the default-radar setting (being built on another branch)">Set as default radar</button>
  </div>

  {#if archiveOpen && loadable && !isNexrad}
    <section class="archive">
      <div class="row">
        <label class="hud-label" for="rn-date">UTC day</label>
        <input id="rn-date" type="date" bind:value={archiveDate} />
        <button type="button" onclick={listDay} disabled={!connected || listing || !archiveDate}>{listing ? 'Listing...' : 'List volumes'}</button>
      </div>
      {#if provider?.archive_only}<p class="fine">Sample days: see the provider description ({provider.description}).</p>{/if}
      {#if listError}<p class="note warn">{listError}</p>{/if}
      {#if scans.length > 0}
        <div class="list" aria-label="Volumes of the day">
          {#each scans as s (s.key)}
            <button type="button" class="row-btn" disabled={s.complete === false} onclick={() => openNetworkScan(site.provider!, site.provider_site!, s)} title={s.complete === false ? 'incomplete volume' : 'Open this volume'}>
              <span class="mono">{utc(s.scan_time)}</span>
              <span class="dim">{formatBytes(s.size)}{s.sweeps ? `, ${s.sweeps} sweeps` : ''}{s.complete === false ? ' (incomplete)' : ''}</span>
            </button>
          {/each}
        </div>
        <p class="fine">Opening a volume asks you first and shows its exact size.</p>
      {/if}
    </section>
  {/if}
  {#if !connected}<p class="fine">The engine is not connected.</p>{/if}
</aside>

<style>
  .rn-card {
    position: absolute;
    left: 12px;
    bottom: 12px;
    width: min(340px, calc(100% - 24px));
    max-height: calc(100% - 24px);
    overflow: auto;
    padding: var(--space-3);
    background: var(--c-overlay);
    border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-md);
    box-shadow: var(--shadow-2);
    color: var(--c-text);
    font-size: var(--fs-xs);
    pointer-events: auto;
    z-index: 3;
  }
  header { display: flex; justify-content: space-between; align-items: flex-start; gap: var(--space-2); }
  .ttl { display: flex; flex-direction: column; min-width: 0; }
  .sid { font-size: var(--fs-lg); font-weight: 600; letter-spacing: 0.04em; color: var(--c-accent); overflow-wrap: anywhere; }
  .nm { color: var(--c-text-dim); overflow-wrap: anywhere; }
  .x { min-width: 26px; height: 26px; padding: 0; line-height: 1; font-size: var(--fs-lg); background: transparent; border-color: transparent; color: var(--c-text-dim); }
  .x:hover { color: var(--c-text); border-color: var(--c-border); }
  .chips { display: flex; gap: var(--space-1); margin: var(--space-2) 0 var(--space-1); flex-wrap: wrap; }
  .chip.access[data-access='open-loadable'] { border-color: var(--c-accent); color: var(--c-accent); }
  .meaning { margin: 0 0 var(--space-2); color: var(--c-text-dim); }
  .facts { display: grid; grid-template-columns: auto 1fr; gap: 2px var(--space-3); margin: 0 0 var(--space-2); }
  .facts.tight { margin-bottom: var(--space-1); }
  dt { color: var(--c-muted); }
  dd { margin: 0; overflow-wrap: anywhere; }
  .mono { font-family: var(--font-mono); }
  .small { font-size: var(--fs-2xs); }
  .dim { color: var(--c-muted); }
  .scan, .archive { border-top: 1px solid var(--c-border); padding-top: var(--space-2); margin-top: var(--space-2); }
  h4 { margin: 0 0 var(--space-1); }
  .scan-line { display: flex; align-items: center; gap: var(--space-2); margin: var(--space-1) 0; }
  .note { margin: var(--space-1) 0; color: var(--c-text-dim); }
  .note.warn { color: var(--c-caution); }
  .fine { margin: var(--space-1) 0; color: var(--c-muted); font-size: var(--fs-2xs); }
  .link { background: none; border: 0; padding: 0; height: auto; color: var(--c-accent); text-decoration: underline; min-height: 26px; }
  .link:disabled { color: var(--c-muted); text-decoration: none; }
  .actions { display: flex; gap: var(--space-2); margin-top: var(--space-2); flex-wrap: wrap; }
  .actions button { flex: 1 1 45%; }
  .row { display: flex; gap: var(--space-2); align-items: center; }
  .row input { flex: 1; min-width: 0; }
  .list { display: flex; flex-direction: column; gap: 2px; max-height: 160px; overflow: auto; margin-top: var(--space-2); border: 1px solid var(--c-border); border-radius: var(--radius-sm); padding: 2px; }
  .row-btn { display: flex; justify-content: space-between; gap: var(--space-2); height: auto; padding: 3px 8px; font-size: var(--fs-2xs); background: transparent; border: 0; text-align: left; }
  .row-btn:hover:not(:disabled) { background: rgba(var(--c-accent-rgb), 0.1); }
</style>
