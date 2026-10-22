<script lang="ts">
  /**
   * LiveRadarChooser - choose the default radars of the live map: click sites on a map (NEXRAD
   * in the USA, DWD in Germany: the two networks the live map can fetch), search by id, name,
   * state or country (radars of other networks are listed with the reason they are not live), order the list, and, only
   * when you press the button and the browser offers it, use your location to pick the nearest
   * radars. Nothing is fetched here except map tiles; the choice is saved in the settings.
   */
  import { get } from 'svelte/store';
  import { onMount } from 'svelte';
  import { portal } from '../utils/portal';
  import { focusTrap } from '../utils/focusTrap';
  import { settings, setLiveRadars } from '../stores/settings';
  import { liveSites, ensureLiveSites, siteLabel, type LiveSite } from '../stores/liveSites';
  import { normalizeRadarIds, toggleRadarId, moveRadarId, liveIdOf, liveUnavailableReason, MAX_LIVE_RADARS } from '../utils/liveLogic';
  import { loadCatalogue, haversineKm, type CatalogueSite, type RadarCatalogue } from '../utils/radarCatalogue';
  import { latLonToPixel } from '../utils/geoProjection';
  import PanZoomMap from './PanZoomMap.svelte';
  import RadarSitesLayer from './RadarSitesLayer.svelte';
  import type { GeoBounds } from '../stores/mosaic';

  let { onclose, onsaved }: { onclose: () => void; onsaved?: (ids: string[]) => void } = $props();

  let ids = $state<string[]>(normalizeRadarIds(get(settings).live.radars));
  let query = $state('');
  let geoMessage = $state('');
  let geoBusy = $state(false);
  let fit = $state<GeoBounds | null>(null);

  const CONUS: GeoBounds = { west: -125, south: 24, east: -66, north: 50 };
  const GERMANY: GeoBounds = { west: 5.2, south: 47.0, east: 15.6, north: 55.2 };
  const picked = $derived(new Set(ids));
  const byId = $derived($liveSites);
  const hasGeolocation = typeof navigator !== 'undefined' && !!navigator.geolocation;

  // Which network's map is shown: the first chosen radar's, else NEXRAD.
  let network = $state<'nexrad' | 'dwd'>(ids[0]?.startsWith('dwd:') ? 'dwd' : 'nexrad');
  const dwdSites = $derived([...byId.values()].filter((s) => s.network === 'dwd'));

  // The radar catalogue (a local file) is read the first time something is searched, so radars of
  // other networks can be found and the reason they are not live can be shown.
  let catalogue = $state<RadarCatalogue | null>(null);
  onMount(() => { void ensureLiveSites(); });
  $effect(() => { if (query.trim() && !catalogue) void loadCatalogue().then((c) => (catalogue = c)); });

  type Match = { kind: 'live'; site: LiveSite } | { kind: 'other'; site: CatalogueSite; reason: string };
  const matches = $derived.by((): Match[] => {
    const q = query.trim().toLowerCase();
    if (!q) return [];
    const live: Match[] = [...byId.values()]
      .filter((s) => s.id.toLowerCase().includes(q) || s.name.toLowerCase().includes(q) || s.region.toLowerCase() === q || s.region.toLowerCase().startsWith(q))
      .slice(0, 25)
      .map((site) => ({ kind: 'live', site }));
    // radars the live map cannot use: listed with the reason (the catalogue's live ones are above)
    const other: Match[] = (catalogue?.search(query, 40) ?? [])
      .filter((c) => !liveIdOf(c))
      .slice(0, 12)
      .map((site) => ({ kind: 'other', site, reason: liveUnavailableReason(site) }));
    return [...live, ...other];
  });

  function toggle(site: { id: string }) {
    if (!byId.has(site.id)) return; // only radars the live map can fetch
    ids = toggleRadarId(ids, site.id);
  }

  function boundsOf(list: string[]): GeoBounds | null {
    const sites = list.map((id) => byId.get(id)).filter((s): s is LiveSite => !!s);
    if (sites.length === 0) return null;
    const lats = sites.map((s) => s.lat);
    const lons = sites.map((s) => s.lon);
    const pad = 2.5;
    return { west: Math.min(...lons) - pad, south: Math.min(...lats) - pad, east: Math.max(...lons) + pad, north: Math.max(...lats) + pad };
  }

  function useMyLocation() {
    if (!hasGeolocation || geoBusy) return;
    geoBusy = true;
    geoMessage = 'Asking the browser for your position…';
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        geoBusy = false;
        const { latitude, longitude } = pos.coords;
        const near = [...byId.values()]
          .map((s) => ({ s, km: haversineKm(latitude, longitude, s.lat, s.lon) }))
          .filter((x) => x.km <= 300)
          .sort((a, b) => a.km - b.km)
          .slice(0, 3);
        if (near.length === 0) {
          geoMessage = `No NEXRAD or DWD radar within 300 km of ${latitude.toFixed(2)}, ${longitude.toFixed(2)}.`;
          return;
        }
        ids = normalizeRadarIds([...ids, ...near.map((x) => x.s.id)]);
        network = near[0].s.network;
        fit = boundsOf(ids);
        geoMessage = `Nearest: ${near.map((x) => `${x.s.id} (${Math.round(x.km)} km)`).join(', ')}. Your position is not stored.`;
      },
      (err) => {
        geoBusy = false;
        geoMessage = err.code === err.PERMISSION_DENIED ? 'Location access was refused; pick radars on the map instead.' : `Location not available (${err.message}).`;
      },
      { enableHighAccuracy: false, timeout: 15_000, maximumAge: 600_000 },
    );
  }

  function save() {
    setLiveRadars(ids);
    onsaved?.(normalizeRadarIds(ids));
    onclose();
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); onclose(); }
  }

  const nameOf = (id: string) => byId.get(id)?.name ?? '';

  function showNetwork(n: 'nexrad' | 'dwd') {
    network = n;
    fit = n === 'dwd' ? GERMANY : CONUS;
  }

  // The chosen radars start in view; an empty choice shows the whole mainland US.
  $effect(() => { if (fit === null) fit = boundsOf(ids) ?? (network === 'dwd' ? GERMANY : CONUS); });
</script>

<svelte:window onkeydowncapture={onKey} />

<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
<div class="lrc-scrim" use:portal onclick={(e) => { if (e.target === e.currentTarget) onclose(); }}>
  <div class="lrc brackets-4" role="dialog" aria-modal="true" aria-labelledby="lrc-title" use:focusTrap={{ initial: '#lrc-search' }}>
    <header class="lrc-head">
      <h3 id="lrc-title">Default radars for the live map</h3>
      <p class="lrc-sub">Click radars on the map or search below. Up to {MAX_LIVE_RADARS}; the order is the order on the status line. The live map can fetch two networks: NEXRAD (USA) and DWD (Germany). Radars of other networks are on the radar network map but have no open live feed the app can use.</p>
      <div class="lrc-nets" role="group" aria-label="Network of the map">
        <button type="button" class="lrc-net" class:on={network === 'nexrad'} aria-pressed={network === 'nexrad'} onclick={() => showNetwork('nexrad')} data-testid="lrc-net-nexrad">NEXRAD (USA)</button>
        <button type="button" class="lrc-net" class:on={network === 'dwd'} aria-pressed={network === 'dwd'} onclick={() => showNetwork('dwd')} data-testid="lrc-net-dwd">DWD (Germany)</button>
      </div>
    </header>
    <div class="lrc-body">
      <div class="lrc-map">
        {#key network}
          <PanZoomMap centerLat={network === 'dwd' ? 51 : 39} centerLon={network === 'dwd' ? 10 : -97} maxRange={network === 'dwd' ? 700_000 : 1_900_000} {fit} label="Map of radar sites. Scroll to zoom, drag to pan, click a radar to choose it.">
            {#snippet children(v)}
              {#if network === 'nexrad'}
                <RadarSitesLayer {...v} pick={(s) => toggle({ id: s.id.toUpperCase() })} picked={new Set([...picked].map((x) => x.toUpperCase()))} />
              {:else}
                {#each dwdSites as s (s.id)}
                  {@const [x, y] = latLonToPixel(s.lat, s.lon, v.radarLat, v.radarLon, v.maxRange, v.scale, v.translateX, v.translateY, v.containerWidth, v.containerHeight)}
                  <button type="button" class="lrc-dwd" class:on={picked.has(s.id)} style:left="{x}px" style:top="{y}px" aria-pressed={picked.has(s.id)} title="{s.id}: {siteLabel(s, s.id)}" onclick={() => toggle(s)}>
                    <span class="lrc-dwd-dot" aria-hidden="true"></span><span class="mono">{s.id.slice(4).toUpperCase()}</span>
                  </button>
                {/each}
              {/if}
              {#each ids as id, i (id)}
                {@const s = byId.get(id)}
                {#if s && s.network === network}
                  {@const [x, y] = latLonToPixel(s.lat, s.lon, v.radarLat, v.radarLon, v.maxRange, v.scale, v.translateX, v.translateY, v.containerWidth, v.containerHeight)}
                  <span class="lrc-num" style:left="{x}px" style:top="{y}px" aria-hidden="true">{i + 1}</span>
                {/if}
              {/each}
            {/snippet}
          </PanZoomMap>
        {/key}
      </div>
      <div class="lrc-side">
        <label class="lrc-label" for="lrc-search">Search id, name or state</label>
        <input id="lrc-search" type="search" bind:value={query} placeholder="KTLX, Essen, Oklahoma, South Africa" autocomplete="off" />
        {#if matches.length}
          <ul class="lrc-list" aria-label="Matching radars">
            {#each matches as m (m.kind === 'live' ? m.site.id : `x:${m.site.id}`)}
              <li>
                {#if m.kind === 'live'}
                  <button type="button" class="lrc-row" class:on={picked.has(m.site.id)} onclick={() => { toggle(m.site); network = m.site.network; }} aria-pressed={picked.has(m.site.id)}>
                    <b class="mono">{m.site.network === 'dwd' ? m.site.id : m.site.id}</b> <span>{siteLabel(m.site, m.site.id)}</span>
                  </button>
                {:else}
                  <div class="lrc-row lrc-off" title={m.reason}>
                    <b class="mono">{m.site.network.split('/')[0]}</b> <span>{m.site.name}, {m.site.country}</span>
                    <span class="lrc-why">not live: {m.reason}</span>
                  </div>
                {/if}
              </li>
            {/each}
          </ul>
        {:else if query.trim()}
          <p class="lrc-empty">No radar matches.</p>
        {/if}

        <div class="lrc-chosen">
          <span class="lrc-label">Chosen ({ids.length}/{MAX_LIVE_RADARS})</span>
          {#if ids.length === 0}
            <p class="lrc-empty">None yet.</p>
          {:else}
            <ol class="lrc-list" aria-label="Chosen radars in order">
              {#each ids as id, i (id)}
                <li class="lrc-chosen-row">
                  <span class="lrc-idx">{i + 1}</span>
                  <b class="mono">{id}</b>
                  <span class="lrc-name">{nameOf(id)}</span>
                  <button type="button" class="lrc-mini" title="Move up" aria-label="Move {id} up" disabled={i === 0} onclick={() => (ids = moveRadarId(ids, id, -1))}>&uarr;</button>
                  <button type="button" class="lrc-mini" title="Move down" aria-label="Move {id} down" disabled={i === ids.length - 1} onclick={() => (ids = moveRadarId(ids, id, 1))}>&darr;</button>
                  <button type="button" class="lrc-mini" title="Remove" aria-label="Remove {id}" onclick={() => (ids = ids.filter((x) => x !== id))}>&times;</button>
                </li>
              {/each}
            </ol>
          {/if}
        </div>

        {#if hasGeolocation}
          <button type="button" class="lrc-btn" onclick={useMyLocation} disabled={geoBusy}>Use my location (nearest 3 radars)</button>
          <p class="lrc-fine">Only when you press this button; the browser asks for permission and the position is used once, in this window.</p>
        {/if}
        {#if geoMessage}<p class="lrc-fine" role="status">{geoMessage}</p>{/if}
      </div>
    </div>
    <footer class="lrc-foot">
      <button type="button" class="lrc-btn" onclick={() => (fit = boundsOf(ids) ?? (network === 'dwd' ? GERMANY : CONUS))} disabled={ids.length === 0}>Show chosen on map</button>
      <span class="lrc-spacer"></span>
      <button type="button" class="lrc-btn" onclick={onclose}>Cancel</button>
      <button type="button" class="lrc-btn primary" onclick={save}>Save{ids.length ? ' and use' : ''}</button>
    </footer>
  </div>
</div>

<style>
  .lrc-scrim { position: fixed; inset: 0; z-index: 1900; display: grid; place-items: center; background: var(--c-scrim); padding: var(--space-4); }
  .lrc {
    width: min(960px, 100%); height: min(680px, 100%); display: flex; flex-direction: column;
    background: var(--c-overlay); border: 1px solid var(--c-border-strong); border-radius: var(--radius-lg);
    box-shadow: var(--shadow-3); color: var(--c-text); overflow: hidden;
  }
  .lrc-head { padding: var(--space-4) var(--space-5) var(--space-2); }
  h3 { margin: 0; font-size: var(--fs-lg); }
  .lrc-sub { margin: var(--space-1) 0 0; color: var(--c-text-dim); font-size: var(--fs-sm); }
  .lrc-body { flex: 1; min-height: 0; display: grid; grid-template-columns: 1fr 300px; gap: var(--space-3); padding: 0 var(--space-5); }
  .lrc-map { position: relative; min-height: 240px; border: 1px solid var(--c-border); border-radius: var(--radius-md); overflow: hidden; }
  .lrc-nets { display: flex; gap: var(--space-1); margin-top: var(--space-2); }
  .lrc-net { height: 24px; padding: 0 var(--space-3); font-size: var(--fs-xs); }
  .lrc-net.on { background: rgba(var(--c-accent-rgb), 0.18); border-color: rgba(var(--c-accent-rgb), 0.5); color: var(--c-accent); }
  .lrc-dwd { position: absolute; transform: translate(-50%, -50%); display: inline-flex; align-items: center; gap: 4px; height: auto; padding: 0; background: none; border: 0; color: var(--c-text); font-size: var(--fs-2xs); text-shadow: 0 0 3px rgba(var(--c-surface-0-rgb), 0.95); cursor: pointer; z-index: 25; }
  .lrc-dwd-dot { width: 11px; height: 11px; border-radius: 50%; border: 2px solid var(--c-text-dim); background: rgba(var(--c-surface-1-rgb), 0.85); box-sizing: border-box; }
  .lrc-dwd.on .lrc-dwd-dot { background: var(--c-accent); border-color: var(--c-accent); }
  .lrc-off { cursor: default; flex-wrap: wrap; opacity: 0.85; }
  .lrc-why { flex-basis: 100%; font-size: var(--fs-2xs); color: var(--c-muted); line-height: 1.35; }
  .lrc-num {
    position: absolute; transform: translate(6px, -18px); font: 700 10px/1 var(--font-mono);
    color: var(--c-surface-0); background: var(--c-accent); border-radius: 8px; padding: 2px 4px; pointer-events: none;
  }
  .lrc-side { display: flex; flex-direction: column; gap: var(--space-2); min-height: 0; overflow: auto; }
  .lrc-label { font-size: var(--fs-xs); color: var(--c-muted); text-transform: uppercase; letter-spacing: 0.04em; }
  .lrc-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; max-height: 180px; overflow: auto; }
  .lrc-row {
    width: 100%; text-align: left; display: flex; gap: var(--space-2); align-items: baseline; padding: 4px 8px; height: auto;
    background: transparent; border: 1px solid transparent; border-radius: var(--radius-sm); color: var(--c-text-dim); font-size: var(--fs-sm);
  }
  .lrc-row:hover { background: rgba(var(--c-accent-rgb), 0.08); color: var(--c-text); }
  .lrc-row.on { border-color: rgba(var(--c-accent-rgb), 0.5); color: var(--c-accent); }
  .lrc-chosen { display: flex; flex-direction: column; gap: var(--space-1); }
  .lrc-chosen-row { display: flex; align-items: center; gap: 6px; font-size: var(--fs-sm); padding: 2px 4px; }
  .lrc-idx { color: var(--c-muted); font-family: var(--font-mono); font-size: var(--fs-xs); width: 14px; }
  .lrc-name { flex: 1; color: var(--c-text-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .lrc-mini { height: 22px; padding: 0 6px; font-size: var(--fs-xs); }
  .lrc-empty, .lrc-fine { margin: 0; color: var(--c-muted); font-size: var(--fs-xs); }
  .lrc-foot { display: flex; gap: var(--space-2); align-items: center; padding: var(--space-3) var(--space-5) var(--space-4); border-top: 1px solid var(--c-border); }
  .lrc-spacer { flex: 1; }
  .lrc-btn { height: 28px; padding: 0 var(--space-3); font-size: var(--fs-sm); }
  .lrc-btn.primary { background: rgba(var(--c-accent-rgb), 0.18); border-color: rgba(var(--c-accent-rgb), 0.5); color: var(--c-accent); }
  .mono { font-family: var(--font-mono); }
  @media (max-width: 760px) {
    .lrc-body { grid-template-columns: 1fr; grid-template-rows: minmax(200px, 1fr) auto; }
  }
</style>
