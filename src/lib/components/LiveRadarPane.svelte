<script lang="ts">
  /**
   * LiveRadarPane - the left pane while the live map is shown: one row per default radar (its
   * name, the volume it answered with and how old it is, what the engine holds of it), what is
   * open as files and the disk those files use. Replaces the empty "No file loaded" inspector.
   * Facts only: what the listing and the engine reported, nothing guessed.
   */
  import { onMount, onDestroy } from 'svelte';
  import {
    liveState, liveFrames, liveHeld, liveOpenedNow, liveAction,
    openLiveVolumes, heldSummary,
  } from '../stores/live';
  import { liveRadars } from '../stores/settings';
  import { BUILTIN_SITES } from '../stores/radarSites';
  import { radarStatusText } from '../utils/liveLogic';
  import { formatBytes } from '../utils/radarSites';

  let { onchoose }: { onchoose: () => void } = $props();

  let now = $state(Date.now());
  let tick: ReturnType<typeof setInterval> | null = null;
  onMount(() => { tick = setInterval(() => (now = Date.now()), 5000); });
  onDestroy(() => { if (tick) clearInterval(tick); });

  const s = $derived($liveState);
  const frames = $derived($liveFrames);
  const held = $derived($liveHeld);
  const opened = $derived($liveOpenedNow);
  const busy = $derived($liveAction !== '');
  const ids = $derived(s.running ? s.stations : $liveRadars);
  const byId = new Map(BUILTIN_SITES.map((st) => [st.id.toUpperCase(), st]));
  const newest = $derived(frames[frames.length - 1] ?? null);

  const rows = $derived(
    ids.map((id) => {
      const entry = s.radars[id] ?? { station: id, status: 'pending' as const };
      const h = held.find((x) => x.station === id && x.on_disk);
      const open = opened.find((o) => o.station === id);
      return {
        id,
        site: byId.get(id),
        entry,
        held: h ?? null,
        open: open ?? null,
        inFrame: !!newest?.radars.some((r) => r.station === id),
      };
    }),
  );
  const heldTotal = $derived(held.filter((h) => h.on_disk).reduce((n, h) => n + h.size, 0));
  const openedTotal = $derived(opened.reduce((n, o) => n + o.size, 0));
</script>

<div class="lrp" data-testid="live-pane">
  <div class="lrp-head">
    <span class="hud-label">Live radars</span>
    <button type="button" class="lrp-link" onclick={onchoose} title="Choose the default radars">Choose…</button>
  </div>

  {#if rows.length === 0}
    <p class="lrp-empty">No default radars yet. Press <b>Choose…</b> to pick them on the map; their newest scans are merged into one picture.</p>
  {:else}
    <ul class="lrp-list">
      {#each rows as r (r.id)}
        <li class="lrp-row" data-status={r.entry.status}>
          <div class="lrp-line">
            <b class="mono lrp-id">{r.id}</b>
            <span class="lrp-name">{r.site?.name ?? ''}{r.site?.state ? `, ${r.site.state}` : ''}</span>
          </div>
          <div class="lrp-fact">
            {s.running || r.entry.status !== 'pending' ? radarStatusText(r.entry, now) : 'not checked'}
            {#if s.running && !r.inFrame && r.entry.status === 'ok'}<span class="lrp-out"> · not in the picture</span>{/if}
          </div>
          {#if r.held}
            <div class="lrp-fact mono" title={r.held.name}>{r.held.name} · {formatBytes(r.held.size)}</div>
          {/if}
          <div class="lrp-actions">
            {#if r.open}
              <span class="lrp-open" title="This volume is open as a file: it stays when the live map updates">open as a file</span>
            {:else}
              <button
                type="button" class="lrp-btn"
                disabled={!r.held || busy}
                title={r.held ? `Open this volume as a normal file. ${heldSummary(held, [r.id])}.` : 'No volume is held for this radar yet.'}
                onclick={() => openLiveVolumes([r.id], 'open')}
              >Open this volume</button>
            {/if}
            <button
              type="button" class="lrp-btn"
              disabled={!r.held || busy}
              title={r.held ? 'Open this volume and show it in 3D.' : 'No volume is held for this radar yet.'}
              onclick={() => openLiveVolumes([r.id], '3d')}
            >3D</button>
          </div>
        </li>
      {/each}
    </ul>
  {/if}

  <div class="lrp-foot">
    <div><span class="hud-label">Held by the live map</span></div>
    <div class="lrp-fact">{held.filter((h) => h.on_disk).length} volume{held.filter((h) => h.on_disk).length === 1 ? '' : 's'}, {formatBytes(heldTotal)}. The next update replaces them.</div>
    <div class="lrp-gap"><span class="hud-label">Open as files</span></div>
    <div class="lrp-fact">
      {#if opened.length === 0}none. <i>Open these volumes</i> keeps them, with no new download.
      {:else}{opened.length} volume{opened.length === 1 ? '' : 's'} ({opened.map((o) => o.station).join(', ')}), {formatBytes(openedTotal)} on disk until you close them.{/if}
    </div>
  </div>
</div>

<style>
  .lrp { display: flex; flex-direction: column; gap: var(--space-2); padding: var(--space-3); height: 100%; overflow-y: auto; font-size: var(--fs-sm); }
  .lrp-head { display: flex; align-items: center; justify-content: space-between; }
  .lrp-link { background: none; border: 0; color: var(--c-accent); cursor: pointer; font-size: var(--fs-xs); padding: 0; }
  .lrp-empty { margin: 0; color: var(--c-text-dim); line-height: 1.5; }
  .lrp-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-2); }
  .lrp-row { border: 1px solid var(--c-border); border-left-width: 3px; border-radius: var(--radius-sm); padding: 6px var(--space-2); background: rgba(var(--c-surface-2-rgb), 0.35); }
  .lrp-row[data-status='ok'] { border-left-color: rgba(var(--c-nominal-rgb), 0.7); }
  .lrp-row[data-status='not_found'], .lrp-row[data-status='offline'] { border-left-color: rgba(var(--c-caution-rgb), 0.8); }
  .lrp-row[data-status='error'] { border-left-color: rgba(var(--c-alarm-rgb), 0.8); }
  .lrp-line { display: flex; gap: 6px; align-items: baseline; }
  .lrp-id { font-size: var(--fs-sm); }
  .lrp-name { color: var(--c-text-dim); font-size: var(--fs-xs); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .lrp-fact { color: var(--c-text-dim); font-size: var(--fs-xs); line-height: 1.45; overflow: hidden; text-overflow: ellipsis; }
  .lrp-out { color: var(--c-caution); }
  .lrp-actions { display: flex; gap: 6px; margin-top: 5px; align-items: center; }
  .lrp-btn { height: 22px; padding: 0 var(--space-2); font-size: var(--fs-xs); }
  .lrp-open { font-size: var(--fs-xs); color: var(--c-nominal); }
  .lrp-foot { margin-top: auto; border-top: 1px solid var(--c-border); padding-top: var(--space-2); display: flex; flex-direction: column; gap: 2px; }
  .lrp-gap { margin-top: var(--space-2); }
  .mono { font-family: var(--font-mono); }
</style>
