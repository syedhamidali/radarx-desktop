<script lang="ts">
  /**
   * Settings > Data > Radar sites: the map switch, the built-in NEXRAD list, and a CSV/JSON file
   * of your own sites (other networks). The file is read in this window only; nothing is uploaded.
   * Its code (and the catalogue) loads when this section is first shown.
   */
  import { showRadarSites } from '../stores/radarSitesPref';
  import {
    customCatalogue,
    adoptSiteFile,
    clearCustomSites,
    setIncludeBuiltin,
    BUILTIN_SOURCE,
  } from '../stores/radarSites';
  import { parseSiteList, MAX_CUSTOM_SITES, type SiteRejection } from '../utils/radarSites';

  let message = $state('');
  let problems = $state<SiteRejection[]>([]);
  let failed = $state(false);

  const c = $derived($customCatalogue);
  const MAX_BYTES = 5 * 1024 * 1024;

  async function onFile(e: Event) {
    const input = e.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    input.value = '';
    if (!file) return;
    problems = [];
    failed = false;
    if (file.size > MAX_BYTES) {
      failed = true;
      message = `${file.name} is larger than 5 MB.`;
      return;
    }
    let text = '';
    try {
      text = await file.text();
    } catch {
      failed = true;
      message = `Could not read ${file.name}.`;
      return;
    }
    const parsed = parseSiteList(text, file.name);
    problems = parsed.rejected;
    if (parsed.error) {
      failed = true;
      message = `${file.name}: ${parsed.error}`;
      return;
    }
    adoptSiteFile(parsed, file.name);
    message = `${parsed.sites.length} sites loaded from ${file.name}` + (parsed.rejected.length ? `, ${parsed.rejected.length} rows skipped.` : '.');
  }

  function clear() {
    clearCustomSites();
    message = '';
    problems = [];
  }
</script>

<div class="rs-set">
  <h3>Radar sites</h3>
  <p class="hint">
    Markers of radar sites on the 2D map (and the 3D ground). A marker is neutral until you check that site's latest
    scan; nothing is fetched without asking you first.
  </p>

  <label class="row">
    <input type="checkbox" bind:checked={$showRadarSites} />
    <span>Show radar sites on the map</span>
  </label>

  <label class="row">
    <input type="checkbox" checked={c.includeBuiltin} onchange={(e) => setIncludeBuiltin(e.currentTarget.checked)} />
    <span>Include the built-in NEXRAD list ({BUILTIN_SOURCE.count} WSR-88D sites)</span>
  </label>
  <p class="hint small">
    Source: {BUILTIN_SOURCE.text}, retrieved {BUILTIN_SOURCE.retrieved}. NOAA/NWS information is in the public domain.
  </p>

  <h4 class="hud-label">Your own sites (other networks)</h4>
  <p class="hint small">
    CSV with a header row (<code>id, name, lat, lon</code>, optional <code>elev_m, network, state</code>; comma, semicolon or tab)
    or JSON (<code>[{'{'}"id":"ESS","name":"Essen","lat":51.4,"lon":6.97{'}'}]</code> or <code>{'{'}"sites":[...]{'}'}</code>).
    Up to {MAX_CUSTOM_SITES} sites. A site with the id of a built-in one replaces it. The engine can fetch NEXRAD Level II only, so
    other networks show their position and details but no download.
  </p>
  <div class="row">
    <input type="file" accept=".csv,.json,.txt,text/csv,application/json" onchange={onFile} aria-label="Choose a CSV or JSON file of radar sites" />
    {#if c.sites.length}<button type="button" onclick={clear}>Remove my {c.sites.length} sites</button>{/if}
  </div>
  {#if c.sites.length}
    <p class="hint small">Loaded: {c.sites.length} sites from {c.fileName || 'a file'}.</p>
  {/if}
  {#if message}<p class="hint" class:fail={failed} role="status">{message}</p>{/if}
  {#if problems.length}
    <ul class="hint small">
      {#each problems.slice(0, 5) as p}<li>row {p.row}: {p.reason}</li>{/each}
      {#if problems.length > 5}<li>... and {problems.length - 5} more</li>{/if}
    </ul>
  {/if}
</div>

<style>
  .rs-set h3 {
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: var(--ls-label);
    color: var(--c-accent);
    margin: 0 0 var(--space-3);
  }
  h4 { margin: var(--space-4) 0 var(--space-2); }
  .row { display: flex; align-items: center; gap: var(--space-2); min-height: 28px; font-size: 12px; color: var(--c-text); flex-wrap: wrap; }
  .hint { font-size: 12px; color: var(--c-muted); margin: 0 0 var(--space-2); line-height: 1.5; }
  .hint.small { font-size: 11px; }
  .fail { color: var(--c-caution); }
  code { font-family: var(--font-mono); font-size: 11px; color: var(--c-text-dim); }
  ul { padding-left: var(--space-5); margin: 0 0 var(--space-2); }
</style>
