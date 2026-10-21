<script lang="ts">
  /**
   * LiveMapView - the real-time map: the latest composite of the default radars on a tile map,
   * a LIVE status line (what each radar answered, when, how old), the update timer with pause,
   * and the recent loop (the last frames in memory). Provenance: the composite is RETRIEVED
   * (merged, gridded reflectivity); the volumes behind it are OBSERVED Level II data.
   */
  import { onMount, onDestroy } from 'svelte';
  import {
    liveState, liveFrames, liveShownIndex, liveShownFrame, liveLoopPlaying, liveConsentPrompt,
    startLive, stopLive, pauseLive, resumeLive, refreshLive, rescheduleLive, clearLiveFrames,
    LIVE_RING_KM, MAX_FRAMES, type LiveFrame,
  } from '../stores/live';
  import { liveRadars, liveIntervalMin, setLiveIntervalMin, mapHidden } from '../stores/settings';
  import { BUILTIN_SITES } from '../stores/radarSites';
  import { RADAR_COLORS, type MosaicLayer, type GeoBounds } from '../stores/mosaic';
  import { countdownText, hhmmss, radarStatusText, stepFrame, MIN_INTERVAL_MIN, MAX_INTERVAL_MIN } from '../utils/liveLogic';
  import { latLonToPixel } from '../utils/geoProjection';
  import PanZoomMap from './PanZoomMap.svelte';
  import RadarSiteConsent from './RadarSiteConsent.svelte';
  import ProvenanceChip from './ProvenanceChip.svelte';

  let { onchoose }: { onchoose: () => void } = $props();

  let now = $state(Date.now());
  let tick: ReturnType<typeof setInterval> | null = null;
  let loopTimer: ReturnType<typeof setInterval> | null = null;
  const LOOP_STEP_MS = 700;

  onMount(() => {
    tick = setInterval(() => (now = Date.now()), 1000);
    // The view opened with radars set but nothing running: start (the consent dialog follows).
    if (!$liveState.running && $liveRadars.length > 0) startLive();
  });
  onDestroy(() => {
    if (tick) clearInterval(tick);
    if (loopTimer) clearInterval(loopTimer);
  });

  const s = $derived($liveState);
  const frames = $derived($liveFrames);
  const frame = $derived($liveShownFrame);
  const shownIndex = $derived($liveShownIndex);
  const isLatest = $derived(shownIndex === null || shownIndex === frames.length - 1);
  const radarIds = $derived(s.running ? s.stations : $liveRadars);
  const byId = new Map(BUILTIN_SITES.map((st) => [st.id.toUpperCase(), st]));

  // Map centre: the frame's grid centre, else the centroid of the default radars, else the US.
  const centre = $derived.by(() => {
    if (frame) return { lat: (frame.grid.north + frame.grid.south) / 2, lon: (frame.grid.east + frame.grid.west) / 2 };
    const sites = radarIds.map((id) => byId.get(id)).filter((x): x is NonNullable<typeof x> => !!x);
    if (sites.length) return { lat: sites.reduce((a, x) => a + x.lat, 0) / sites.length, lon: sites.reduce((a, x) => a + x.lon, 0) / sites.length };
    return { lat: 39, lon: -97 };
  });
  // The projection is anchored once per session on the first centre so panning stays consistent.
  let anchor = $state<{ lat: number; lon: number } | null>(null);
  $effect(() => { if (!anchor && (frame || radarIds.length)) anchor = centre; });

  const overlay = $derived.by((): MosaicLayer | null => {
    if (!frame) return null;
    return {
      image: frame.image,
      corners: frame.corners,
      radars: frame.radarInfo.map((r, i) => ({ ...r, color: RADAR_COLORS[i % RADAR_COLORS.length] })),
      ringKm: LIVE_RING_KM,
      opacity: 0.85,
      visible: true,
      view: true,
      units: frame.units,
      vmin: frame.vmin,
      vmax: frame.vmax,
      legend: frame.legend,
      mosaicId: frame.mosaicId,
      variable: frame.variable,
    };
  });

  let fit = $state<GeoBounds | null>(null);
  let fittedOnce = false;
  $effect(() => {
    // Fit the map to the first frame of the session (not on every update: the user may have panned).
    if (frame && !fittedOnce) { fittedOnce = true; fit = frameBounds(frame); }
  });
  function frameBounds(f: LiveFrame): GeoBounds {
    return { west: f.grid.west, south: f.grid.south, east: f.grid.east, north: f.grid.north };
  }
  function fitNow() { if (frame) fit = { ...frameBounds(frame) }; }

  // ── Status line ─────────────────────────────────────────────────────────────────────────────
  const busy = $derived(s.phase === 'listing' || s.phase === 'downloading' || s.phase === 'building' || s.phase === 'waiting');
  const headline = $derived.by(() => {
    if (!s.running) return frames.length ? 'Stopped' : 'Not running';
    if (s.phase === 'waiting') return 'Waiting for your answer';
    if (s.phase === 'listing') return 'Listing the newest volumes';
    if (s.phase === 'downloading') return 'Downloading';
    if (s.phase === 'building') return 'Building the composite';
    if (s.paused) return 'Paused';
    if (s.phase === 'error') return `Update failed${s.failures > 1 ? ` (${s.failures} times)` : ''}`;
    return 'LIVE';
  });
  const nextText = $derived.by(() => {
    if (!s.running || s.paused || busy) return '';
    if (s.nextDueAt === null) return s.onceOnly ? 'allowed once: no further update' : '';
    const c = countdownText(s.nextDueAt, now);
    return c ? `next in ${c}` : '';
  });
  const radarRows = $derived(radarIds.map((id) => ({ id, entry: s.radars[id] ?? { station: id, status: 'pending' as const }, inFrame: !!frame?.radars.some((r) => r.station === id) })));

  function onInterval(e: Event) {
    const v = Number((e.currentTarget as HTMLSelectElement).value);
    setLiveIntervalMin(v);
    rescheduleLive();
  }
  const intervals = Array.from({ length: MAX_INTERVAL_MIN - MIN_INTERVAL_MIN + 1 }, (_, i) => MIN_INTERVAL_MIN + i);

  // ── Loop ────────────────────────────────────────────────────────────────────────────────────
  function step(dir: 1 | -1) { liveShownIndex.set(stepFrame(shownIndex, frames.length, dir)); }
  function togglePlay() {
    liveLoopPlaying.update((p) => !p);
  }
  $effect(() => {
    const playing = $liveLoopPlaying;
    if (loopTimer) { clearInterval(loopTimer); loopTimer = null; }
    if (playing && frames.length > 1) {
      loopTimer = setInterval(() => step(1), LOOP_STEP_MS);
    } else if (playing) {
      liveLoopPlaying.set(false);
    }
  });
  function onSlider(e: Event) {
    const i = Number((e.currentTarget as HTMLInputElement).value);
    liveShownIndex.set(i >= frames.length - 1 ? null : i);
  }
  const frameTime = (f: LiveFrame | null) => (f?.time ? f.time.replace('T', ' ').slice(0, 19) + ' UTC' : 'time unknown');
  const frameNo = $derived(frames.length ? (shownIndex === null ? frames.length : shownIndex + 1) : 0);
  const shownAgeS = $derived(frame?.time ? Math.max(0, (now - Date.parse(frame.time)) / 1000) : null);
  const stationLabel = (f: LiveFrame) => f.radars.map((r) => r.station).join(', ');
  function onKey(e: KeyboardEvent) {
    if (e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement) return;
    if (e.key === ',') { step(-1); e.preventDefault(); }
    else if (e.key === '.') { step(1); e.preventDefault(); }
  }
</script>

<svelte:window onkeydown={onKey} />

<div class="live-root" data-testid="live-view">
  <!-- LIVE status line -->
  <div class="live-status" role="status" aria-live="polite">
    <span class="live-chip" class:on={s.running && !s.paused && s.phase === 'live'} class:busy class:bad={s.phase === 'error'}>
      <span class="live-dot" aria-hidden="true"></span>{headline}
    </span>
    {#if s.lastUpdateAt}<span class="live-item">updated {hhmmss(s.lastUpdateAt)}</span>{/if}
    {#if nextText}<span class="live-item">{nextText}</span>{/if}
    {#if s.message}<span class="live-item msg" title={s.message}>{s.message}</span>{/if}
    <span class="live-spacer"></span>
    <label class="live-item">every
      <select value={$liveIntervalMin} onchange={onInterval} aria-label="Minutes between updates">
        {#each intervals as m}<option value={m}>{m} min</option>{/each}
      </select>
    </label>
    {#if s.running}
      {#if s.paused || s.onceOnly}
        <button type="button" class="live-btn" onclick={resumeLive} title="Keep updating on the timer">Resume</button>
      {:else}
        <button type="button" class="live-btn" onclick={pauseLive} title="Stop the timer, keep the map">Pause</button>
      {/if}
      <button type="button" class="live-btn" onclick={refreshLive} disabled={busy} title="List and fetch the newest volumes now">Update now</button>
      <button type="button" class="live-btn" onclick={stopLive} title="Stop the live map and release the volumes in the engine">Stop</button>
    {:else}
      <button type="button" class="live-btn primary" onclick={() => startLive()} disabled={radarIds.length === 0}>Start</button>
    {/if}
    <button type="button" class="live-btn" onclick={onchoose} title="Choose the default radars">Radars…</button>
  </div>

  <!-- Per-radar facts -->
  <div class="live-radars" aria-label="Radars of the live map">
    {#if radarIds.length === 0}
      <span class="live-item">No default radars yet: press <b>Radars…</b> to choose them on the map.</span>
    {/if}
    {#each radarRows as r (r.id)}
      <span class="live-radar" data-status={r.entry.status} class:out={s.running && !r.inFrame && r.entry.status === 'ok'}>
        <b class="mono">{r.id}</b> <span class="live-fact">{s.running || r.entry.status !== 'pending' ? radarStatusText(r.entry, now) : 'not checked'}</span>
      </span>
    {/each}
  </div>

  <!-- Map -->
  <div class="live-map">
    {#if anchor}
      <PanZoomMap centerLat={anchor.lat} centerLon={anchor.lon} maxRange={400_000} {overlay} showTiles={!$mapHidden} {fit} label="Live map. Scroll to zoom, drag to pan, double-click to fit the composite.">
        {#snippet children(v)}
          {#each radarRows as r (r.id)}
            {@const site = byId.get(r.id)}
            {#if site && !r.inFrame}
              {@const [x, y] = latLonToPixel(site.lat, site.lon, v.radarLat, v.radarLon, v.maxRange, v.scale, v.translateX, v.translateY, v.containerWidth, v.containerHeight)}
              <span class="live-marker" data-status={r.entry.status} style:left="{x}px" style:top="{y}px" title="{r.id}: {radarStatusText(r.entry, now)}">
                <span class="live-marker-dot" aria-hidden="true"></span><span class="mono">{r.id}</span>
              </span>
            {/if}
          {/each}
        {/snippet}
      </PanZoomMap>
    {/if}
    {#if !frame}
      <div class="live-empty">
        {#if radarIds.length === 0}
          <p>Choose one or several radars; their newest scans are then merged into one picture here.</p>
        {:else if !s.running}
          <p>Press <b>Start</b>. The app asks before it lists or downloads anything.</p>
        {:else}
          <p>{headline}{s.message ? `: ${s.message}` : '…'}</p>
        {/if}
      </div>
    {/if}
    {#if frame}
      <div class="live-prov">
        <ProvenanceChip provenance={frame.provenance} />
        <span>Composite (column maximum of {frame.variable}) merged from {frame.radars.length} radar volume{frame.radars.length === 1 ? '' : 's'}, {Math.round(frame.grid.resolution_m / 1000)} km grid. {!isLatest ? 'Recent loop: not the newest frame.' : ''} Not an official product.</span>
        <button type="button" class="live-btn small" onclick={fitNow}>Fit</button>
      </div>
    {/if}
  </div>

  <!-- Recent loop -->
  <div class="live-loop" aria-label="Recent loop">
    <button type="button" class="live-btn small" onclick={() => step(-1)} disabled={frames.length < 2} title="Previous frame (,)" aria-label="Previous frame">&lsaquo;</button>
    <button type="button" class="live-btn small" onclick={togglePlay} disabled={frames.length < 2} aria-label={$liveLoopPlaying ? 'Pause the loop' : 'Play the loop'}>{$liveLoopPlaying ? '❚❚' : '▶'}</button>
    <button type="button" class="live-btn small" onclick={() => step(1)} disabled={frames.length < 2} title="Next frame (.)" aria-label="Next frame">&rsaquo;</button>
    <input type="range" min="0" max={Math.max(0, frames.length - 1)} value={frames.length ? frameNo - 1 : 0} oninput={onSlider} disabled={frames.length < 2} aria-label="Frame of the recent loop" />
    <span class="live-item mono">
      {#if frames.length}
        {isLatest ? 'newest' : `frame ${frameNo}/${frames.length}`} · {frameTime(frame)}{shownAgeS !== null ? ` (${Math.round(shownAgeS / 60)} min ago)` : ''} · {frame ? stationLabel(frame) : ''}
      {:else}
        no frames yet (the last {MAX_FRAMES} composites are kept in memory)
      {/if}
    </span>
    <span class="live-spacer"></span>
    {#if !isLatest}<button type="button" class="live-btn small" onclick={() => liveShownIndex.set(null)}>Newest</button>{/if}
    {#if frames.length}<button type="button" class="live-btn small" onclick={clearLiveFrames} title="Forget the kept frames">Clear</button>{/if}
  </div>
</div>

{#if $liveConsentPrompt}
  <RadarSiteConsent prompt={$liveConsentPrompt} />
{/if}

<style>
  .live-root { position: absolute; inset: 0; display: flex; flex-direction: column; background: var(--c-surface-0); color: var(--c-text); }
  .live-status, .live-radars, .live-loop {
    display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap;
    padding: var(--space-1) var(--space-3); font-size: var(--fs-xs); border-bottom: 1px solid var(--c-border); background: var(--c-surface-1);
  }
  .live-loop { border-bottom: 0; border-top: 1px solid var(--c-border); }
  .live-chip {
    display: inline-flex; align-items: center; gap: 6px; padding: 2px 8px; border-radius: 999px;
    font-weight: 700; letter-spacing: 0.06em; font-size: var(--fs-2xs); text-transform: uppercase;
    border: 1px solid var(--c-border-strong); color: var(--c-text-dim); background: var(--c-surface-2);
  }
  .live-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--c-muted); }
  .live-chip.on { color: var(--c-nominal); border-color: rgba(var(--c-nominal-rgb), 0.5); }
  .live-chip.on .live-dot { background: var(--c-nominal); animation: live-pulse 1.6s ease-in-out infinite; }
  .live-chip.busy .live-dot { background: var(--c-accent); animation: live-pulse 0.9s ease-in-out infinite; }
  .live-chip.bad { color: var(--c-alarm); border-color: rgba(var(--c-alarm-rgb), 0.5); }
  .live-chip.bad .live-dot { background: var(--c-alarm); }
  @keyframes live-pulse { 50% { opacity: 0.25; } }
  .live-item { color: var(--c-text-dim); white-space: nowrap; }
  .live-item.msg { color: var(--c-caution); white-space: normal; overflow: hidden; text-overflow: ellipsis; max-width: 48ch; }
  .live-item select { height: 22px; font-size: var(--fs-xs); margin-left: 4px; }
  .live-spacer { flex: 1; }
  .live-btn { height: 24px; padding: 0 var(--space-2); font-size: var(--fs-xs); }
  .live-btn.small { height: 22px; }
  .live-btn.primary { background: rgba(var(--c-accent-rgb), 0.18); border-color: rgba(var(--c-accent-rgb), 0.5); color: var(--c-accent); }
  .live-radar { display: inline-flex; gap: 5px; align-items: baseline; padding: 1px 6px; border-radius: var(--radius-sm); border: 1px solid var(--c-border); }
  .live-radar[data-status='ok'] { border-color: rgba(var(--c-nominal-rgb), 0.45); }
  .live-radar[data-status='ok'].out { border-style: dashed; }
  .live-radar[data-status='not_found'], .live-radar[data-status='offline'] { border-color: rgba(var(--c-caution-rgb), 0.6); }
  .live-radar[data-status='error'] { border-color: rgba(var(--c-alarm-rgb), 0.6); }
  .live-fact { color: var(--c-text-dim); }
  .live-map { position: relative; flex: 1; min-height: 0; }
  .live-empty {
    position: absolute; inset: 0; display: grid; place-items: center; pointer-events: none; padding: var(--space-5);
  }
  .live-empty p { max-width: 46ch; text-align: center; color: var(--c-text-dim); background: rgba(var(--c-surface-1-rgb), 0.85); padding: var(--space-3) var(--space-4); border-radius: var(--radius-md); border: 1px solid var(--c-border); }
  .live-prov {
    position: absolute; left: var(--space-2); bottom: var(--space-2); display: flex; gap: var(--space-2); align-items: center;
    max-width: calc(100% - 2 * var(--space-2)); padding: 3px 8px; font-size: var(--fs-2xs); color: var(--c-text-dim);
    background: rgba(var(--c-surface-1-rgb), 0.9); border: 1px solid var(--c-border); border-radius: var(--radius-sm);
  }
  .live-marker { position: absolute; transform: translate(-50%, -50%); display: inline-flex; align-items: center; gap: 4px; font-size: var(--fs-2xs); color: var(--c-text); pointer-events: none; text-shadow: 0 0 3px rgba(var(--c-surface-0-rgb), 0.95); }
  .live-marker-dot { width: 9px; height: 9px; border-radius: 50%; border: 2px solid var(--c-text-dim); background: rgba(var(--c-surface-1-rgb), 0.85); box-sizing: border-box; }
  .live-marker[data-status='error'] .live-marker-dot, .live-marker[data-status='offline'] .live-marker-dot { border-color: var(--c-alarm); }
  .live-marker[data-status='not_found'] .live-marker-dot { border-color: var(--c-caution); border-style: dashed; }
  .live-loop input[type='range'] { flex: 0 1 260px; min-width: 80px; }
  .mono { font-family: var(--font-mono); }
  @media (prefers-reduced-motion: reduce) { .live-dot { animation: none !important; } }
</style>
