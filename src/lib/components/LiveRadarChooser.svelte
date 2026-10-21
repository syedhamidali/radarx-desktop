<script lang="ts">
  /**
   * LiveRadarChooser - choose the default radars of the live map: click sites on a map (the
   * radar-sites layer in picker mode), search by id, name or state, order the list, and, only
   * when you press the button and the browser offers it, use your location to pick the nearest
   * radars. Nothing is fetched here except map tiles; the choice is saved in the settings.
   */
  import { get } from 'svelte/store';
  import { portal } from '../utils/portal';
  import { focusTrap } from '../utils/focusTrap';
  import { settings, setLiveRadars } from '../stores/settings';
  import { BUILTIN_SITES } from '../stores/radarSites';
  import { normalizeRadarIds, toggleRadarId, moveRadarId, MAX_LIVE_RADARS } from '../utils/liveLogic';
  import { nearestSite, type RadarSite } from '../utils/radarSites';
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
  const picked = $derived(new Set(ids));
  const byId = new Map(BUILTIN_SITES.map((s) => [s.id.toUpperCase(), s]));
  const hasGeolocation = typeof navigator !== 'undefined' && !!navigator.geolocation;

  const matches = $derived.by(() => {
    const q = query.trim().toLowerCase();
    if (!q) return [] as RadarSite[];
    return BUILTIN_SITES.filter((s) => s.id.toLowerCase().includes(q) || s.name.toLowerCase().includes(q) || (s.state ?? '').toLowerCase() === q).slice(0, 25);
  });

  function toggle(site: RadarSite) {
    if (!byId.has(site.id.toUpperCase())) return; // only NEXRAD sites can be fetched
    ids = toggleRadarId(ids, site.id);
  }

  function boundsOf(list: string[]): GeoBounds | null {
    const sites = list.map((id) => byId.get(id)).filter((s): s is RadarSite => !!s);
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
        const near = [...BUILTIN_SITES]
          .map((s) => ({ s, km: nearestSite([s], latitude, longitude, 1e9)?.km ?? Infinity }))
          .filter((x) => x.km <= 300)
          .sort((a, b) => a.km - b.km)
          .slice(0, 3);
        if (near.length === 0) {
          geoMessage = `No NEXRAD radar within 300 km of ${latitude.toFixed(2)}, ${longitude.toFixed(2)}.`;
          return;
        }
        ids = normalizeRadarIds([...ids, ...near.map((x) => x.s.id)]);
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

  // The chosen radars start in view; an empty choice shows the whole mainland US.
  $effect(() => { if (fit === null) fit = boundsOf(ids) ?? CONUS; });
</script>

<svelte:window onkeydowncapture={onKey} />

<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
<div class="lrc-scrim" use:portal onclick={(e) => { if (e.target === e.currentTarget) onclose(); }}>
  <div class="lrc brackets-4" role="dialog" aria-modal="true" aria-labelledby="lrc-title" use:focusTrap={{ initial: '#lrc-search' }}>
    <header class="lrc-head">
      <h3 id="lrc-title">Default radars for the live map</h3>
      <p class="lrc-sub">Click radars on the map or search below. Up to {MAX_LIVE_RADARS}; the order is the order on the status line. Only NEXRAD sites can be fetched.</p>
    </header>
    <div class="lrc-body">
      <div class="lrc-map">
        <PanZoomMap centerLat={39} centerLon={-97} maxRange={1_900_000} {fit} label="Map of radar sites. Scroll to zoom, drag to pan, click a radar to choose it.">
          {#snippet children(v)}
            <RadarSitesLayer {...v} pick={toggle} {picked} />
            {#each ids as id, i (id)}
              {@const s = byId.get(id)}
              {#if s}
                {@const [x, y] = latLonToPixel(s.lat, s.lon, v.radarLat, v.radarLon, v.maxRange, v.scale, v.translateX, v.translateY, v.containerWidth, v.containerHeight)}
                <span class="lrc-num" style:left="{x}px" style:top="{y}px" aria-hidden="true">{i + 1}</span>
              {/if}
            {/each}
          {/snippet}
        </PanZoomMap>
      </div>
      <div class="lrc-side">
        <label class="lrc-label" for="lrc-search">Search id, name or state</label>
        <input id="lrc-search" type="search" bind:value={query} placeholder="KTLX, Oklahoma City, OK" autocomplete="off" />
        {#if matches.length}
          <ul class="lrc-list" aria-label="Matching radars">
            {#each matches as s (s.id)}
              <li>
                <button type="button" class="lrc-row" class:on={picked.has(s.id.toUpperCase())} onclick={() => toggle(s)} aria-pressed={picked.has(s.id.toUpperCase())}>
                  <b class="mono">{s.id}</b> <span>{s.name}{s.state ? `, ${s.state}` : ''}</span>
                </button>
              </li>
            {/each}
          </ul>
        {:else if query.trim()}
          <p class="lrc-empty">No NEXRAD site matches.</p>
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
      <button type="button" class="lrc-btn" onclick={() => (fit = boundsOf(ids) ?? CONUS)} disabled={ids.length === 0}>Show chosen on map</button>
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
