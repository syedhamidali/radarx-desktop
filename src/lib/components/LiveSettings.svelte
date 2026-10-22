<script lang="ts">
  /**
   * Settings > Data > Live map: the default radars (ordered) and the update interval. The map
   * picker itself is the LiveRadarChooser dialog, opened from here through the app shell.
   */
  import { liveRadars, liveIntervalMin, setLiveRadars, setLiveIntervalMin, liveFloorDbz, setLiveFloorDbz, liveRings, setLiveRings } from '../stores/settings';
  import { BUILTIN_SITES } from '../stores/radarSites';
  import { moveRadarId, MAX_LIVE_RADARS, MIN_INTERVAL_MIN, MAX_INTERVAL_MIN } from '../utils/liveLogic';
  import { FLOOR_CHOICES, DEFAULT_FLOOR_DBZ } from '../utils/liveDisplay';

  const byId = new Map(BUILTIN_SITES.map((s) => [s.id.toUpperCase(), s]));
  const ids = $derived($liveRadars);

  function choose() {
    window.dispatchEvent(new CustomEvent('xradar:live-chooser'));
  }
  function onInterval(e: Event) {
    setLiveIntervalMin(Number((e.currentTarget as HTMLInputElement).value));
  }
</script>

<h3>Live map</h3>
<p class="hint">
  The radars whose newest scans the live map merges, in this order (up to {MAX_LIVE_RADARS}, NEXRAD only). Nothing is
  downloaded until you start the live map and allow it.
</p>
{#if ids.length === 0}
  <p class="hint" data-testid="live-settings-none">No default radars chosen.</p>
{:else}
  <ol class="ls-list" aria-label="Default radars in order">
    {#each ids as id, i (id)}
      <li class="ls-row">
        <span class="ls-idx">{i + 1}</span>
        <b class="mono">{id}</b>
        <span class="ls-name">{byId.get(id)?.name ?? ''}{byId.get(id)?.state ? `, ${byId.get(id)?.state}` : ''}</span>
        <button type="button" class="icon-btn-sm" aria-label="Move {id} up" title="Move up" disabled={i === 0} onclick={() => setLiveRadars(moveRadarId(ids, id, -1))}>&uarr;</button>
        <button type="button" class="icon-btn-sm" aria-label="Move {id} down" title="Move down" disabled={i === ids.length - 1} onclick={() => setLiveRadars(moveRadarId(ids, id, 1))}>&darr;</button>
        <button type="button" class="icon-btn-sm" aria-label="Remove {id}" title="Remove" onclick={() => setLiveRadars(ids.filter((x) => x !== id))}>&times;</button>
      </li>
    {/each}
  </ol>
{/if}
<div class="form-row">
  <button type="button" class="reset-btn small" onclick={choose} data-testid="live-settings-choose">Choose on the map…</button>
</div>
<div class="form-row">
  <label class="form-label" for="st-live-interval">Update every</label>
  <input id="st-live-interval" type="number" min={MIN_INTERVAL_MIN} max={MAX_INTERVAL_MIN} step="1" value={$liveIntervalMin} onchange={onInterval} style="width: 70px" />
  <span class="text-muted">minutes ({MIN_INTERVAL_MIN}–{MAX_INTERVAL_MIN}; a NEXRAD volume takes 4–10 min)</span>
</div>

<div class="form-row">
  <label class="form-label" for="st-live-floor">Hide echo below</label>
  <select id="st-live-floor" value={$liveFloorDbz} onchange={(e) => setLiveFloorDbz(Number(e.currentTarget.value))} data-testid="live-settings-floor">
    {#each FLOOR_CHOICES as c}<option value={c}>{c <= -20 ? 'nothing (all echo)' : `${c} dBZ`}</option>{/each}
  </select>
  <span class="text-muted">dBZ; the NWS convention is {DEFAULT_FLOOR_DBZ}. Display only: the data and the exports are not changed.</span>
</div>
<div class="form-row">
  <label class="form-label" for="st-live-rings">Range rings</label>
  <input id="st-live-rings" type="checkbox" checked={$liveRings} onchange={(e) => setLiveRings(e.currentTarget.checked)} />
  <span class="text-muted">draw each radar's 230 km range, subtly</span>
</div>

<style>
  .hint { margin: 0 0 var(--space-2); color: var(--c-text-dim); font-size: var(--fs-sm); }
  .ls-list { list-style: none; margin: 0 0 var(--space-2); padding: 0; display: flex; flex-direction: column; gap: 2px; }
  .ls-row { display: flex; align-items: center; gap: 6px; font-size: var(--fs-sm); }
  .ls-idx { color: var(--c-muted); font-family: var(--font-mono); font-size: var(--fs-xs); width: 14px; }
  .ls-name { flex: 1; color: var(--c-text-dim); }
  .mono { font-family: var(--font-mono); }
</style>
