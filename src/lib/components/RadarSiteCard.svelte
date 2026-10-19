<script lang="ts">
  /**
   * RadarSiteCard - the side card of a clicked radar site: what the catalogue says about the site,
   * what the engine said about its latest scan (only after the user agreed to contact AWS), and
   * the two actions: open the latest volume, or browse the archive in the Cloud panel.
   *
   * Every time shown here is UTC and says where it comes from. No status is shown that the engine
   * has not reported for this site.
   */
  import { siteChecks, checkLatest, openLatest, browseArchive, siteFetchable, BUILTIN_SOURCE } from '../stores/radarSites';
  import { connectionStatus } from '../stores/radarData';
  import { scanState, liveAge, formatAge, formatBytes, type RadarSite } from '../utils/radarSites';

  let { site, isOpenFile, now, onclose }: { site: RadarSite; isOpenFile: boolean; now: number; onclose: () => void } = $props();

  const id = $derived(site.id.toUpperCase());
  const check = $derived($siteChecks[id]);
  const info = $derived(check?.info);
  const fetchable = $derived(siteFetchable(site));
  const connected = $derived($connectionStatus === 'connected');
  const busy = $derived(check?.state === 'loading');
  const age = $derived(check?.state === 'ok' ? liveAge(info, check.receivedAt, now) : null);
  const state = $derived(scanState(age));

  const fmtDeg = (v: number, pos: string, neg: string) => `${Math.abs(v).toFixed(3)}° ${v >= 0 ? pos : neg}`;
  const utc = (iso?: string) => (iso ? iso.replace('T', ' ').replace(/(\.\d+)?Z$/, ' UTC') : '');
  const clock = (iso?: string) => (iso ? (iso.slice(11, 19) + ' UTC') : '');

  function onKey(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.stopPropagation();
      onclose();
    }
  }
  const stop = (e: Event) => e.stopPropagation();
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<aside
  class="rs-card brackets"
  aria-label="Radar site {site.id}"
  onkeydown={onKey}
  onmousedown={stop}
  onmouseup={stop}
  onclick={stop}
  ondblclick={stop}
  oncontextmenu={stop}
  onwheel={stop}
>
  <header>
    <div class="ttl">
      <span class="sid mono">{site.id}</span>
      <span class="nm">{site.name}{site.state ? `, ${site.state}` : ''}</span>
    </div>
    <button type="button" class="x" aria-label="Close site card" onclick={onclose}>&times;</button>
  </header>

  <div class="chips">
    {#if isOpenFile}<span class="chip">open file</span>{/if}
    <span class="chip" class:derived={site.custom}>{site.network ?? 'site'}</span>
  </div>

  <dl class="facts">
    <dt>Position</dt>
    <dd class="mono">{fmtDeg(site.lat, 'N', 'S')}, {fmtDeg(site.lon, 'E', 'W')}</dd>
    {#if site.elev_m !== null}
      <dt>Ground elevation</dt>
      <dd class="mono">{Math.round(site.elev_m)} m</dd>
    {/if}
    <dt>Source</dt>
    <dd>{site.custom ? 'Your site list' : `${BUILTIN_SOURCE.text}, retrieved ${BUILTIN_SOURCE.retrieved}`}</dd>
  </dl>

  <section class="scan" aria-live="polite">
    <h4 class="hud-label">Latest scan</h4>
    {#if !fetchable}
      <p class="note">The engine can fetch NEXRAD Level II only. Open a file of this site from disk.</p>
    {:else if !check}
      <p class="note">Not checked. Checking asks you first, then lists the site's folder on AWS S3.</p>
    {:else if check.state === 'loading'}
      <p class="note">Listing the site's folder on AWS S3...</p>
    {:else if check.state === 'ok' && info}
      <p class="scan-line">
        <span class="chip" class:nominal={state === 'recent'} class:caution={state === 'stale'}>
          {state === 'recent' ? 'recent' : 'older than 10 min'}
        </span>
        <span class="mono">{age === null ? '' : formatAge(age) + ' old'}</span>
      </p>
      <dl class="facts tight">
        <dt>Volume</dt>
        <dd class="mono small">{info.name}</dd>
        <dt>Scan time</dt>
        <dd class="mono">{utc(info.scan_time)}</dd>
        <dt>Size</dt>
        <dd class="mono">{formatBytes(info.size)}</dd>
      </dl>
      <p class="fine">Listed {clock(info.now)} (engine clock). Scan time is the volume start parsed from the file name; the age is counted from it.</p>
    {:else if check.state === 'not_found'}
      <p class="note warn">No volume found for {site.id} today or yesterday (UTC). The radar may be down or the archive not yet updated.</p>
    {:else if check.state === 'offline'}
      <p class="note warn">Offline: could not reach AWS. Check the connection and try again.</p>
    {:else}
      <p class="note warn">{check.message || 'The check failed.'}</p>
    {/if}
    {#if fetchable && !busy}
      <button type="button" class="link" onclick={() => checkLatest(site, true)} disabled={!connected}>
        {check ? 'Check again' : 'Check latest scan'}
      </button>
    {/if}
  </section>

  <div class="actions">
    <button type="button" class="primary" disabled={!fetchable || !connected || busy} onclick={() => openLatest(site)}>
      Open latest volume{check?.state === 'ok' && info?.size ? ` (${formatBytes(info.size)})` : ''}
    </button>
    <button type="button" disabled={!fetchable || !connected} onclick={() => browseArchive(site)}>Browse archive</button>
  </div>
  {#if !connected}<p class="fine">The engine is not connected.</p>{/if}
</aside>

<style>
  .rs-card {
    position: absolute;
    left: 12px;
    top: 56px;
    width: min(300px, calc(100% - 24px));
    max-height: calc(100% - 140px);
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
  header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: var(--space-2);
  }
  .ttl {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }
  .sid {
    font-size: var(--fs-lg);
    font-weight: 600;
    letter-spacing: 0.04em;
    color: var(--c-accent);
  }
  .nm {
    color: var(--c-text-dim);
    overflow-wrap: anywhere;
  }
  .x {
    min-width: 26px;
    height: 26px;
    padding: 0;
    line-height: 1;
    font-size: var(--fs-lg);
    background: transparent;
    border-color: transparent;
    color: var(--c-text-dim);
  }
  .x:hover { color: var(--c-text); border-color: var(--c-border); }
  .chips { display: flex; gap: var(--space-1); margin: var(--space-2) 0; flex-wrap: wrap; }
  .facts {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 2px var(--space-3);
    margin: 0 0 var(--space-3);
  }
  .facts.tight { margin-bottom: var(--space-1); }
  dt { color: var(--c-muted); }
  dd { margin: 0; overflow-wrap: anywhere; }
  .mono { font-family: var(--font-mono); }
  .small { font-size: var(--fs-2xs); }
  .scan { border-top: 1px solid var(--c-border); padding-top: var(--space-2); }
  h4 { margin: 0 0 var(--space-1); }
  .scan-line { display: flex; align-items: center; gap: var(--space-2); margin: var(--space-1) 0; }
  .note { margin: var(--space-1) 0; color: var(--c-text-dim); }
  .note.warn { color: var(--c-caution); }
  .fine { margin: var(--space-1) 0; color: var(--c-muted); font-size: var(--fs-2xs); }
  .link {
    background: none;
    border: 0;
    padding: 0;
    height: auto;
    color: var(--c-accent);
    text-decoration: underline;
    min-height: 26px;
  }
  .link:disabled { color: var(--c-muted); text-decoration: none; }
  .actions { display: flex; gap: var(--space-2); margin-top: var(--space-3); flex-wrap: wrap; }
  .actions button { flex: 1 1 auto; }
</style>
