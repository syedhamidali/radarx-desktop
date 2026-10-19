<script lang="ts">
  /**
   * RadarSitesLayer - radar-site markers over the 2D map.
   *
   * One button per site in view, positioned with the same projection as the map tiles and the PPI
   * (latLonToPixel), so a marker sits exactly on its tile position at any zoom. Labels are thinned
   * with a greedy collision pass (placeSites); the id and name always show on hover and focus.
   * A marker's state is "unknown" (hollow) until the engine has answered for that very site this
   * session, then "recent" (< 10 min, filled) or "stale" (half-filled). Nothing is inferred.
   *
   * Keyboard: Tab enters the layer (one stop), the arrow keys move between markers, Enter or
   * Space opens the card, Escape closes it.
   */
  import { onMount, onDestroy, tick } from 'svelte';
  import { latLonToPixel } from '../utils/geoProjection';
  import { catalogue, selectedSiteId, siteChecks, consentPrompt } from '../stores/radarSites';
  import {
    placeSites,
    nearestSite,
    scanState,
    liveAge,
    type RadarSite,
    type ScanState,
  } from '../utils/radarSites';
  import RadarSiteCard from './RadarSiteCard.svelte';
  import RadarSiteConsent from './RadarSiteConsent.svelte';

  let {
    radarLat,
    radarLon,
    maxRange,
    scale,
    translateX,
    translateY,
    containerWidth,
    containerHeight,
  }: {
    radarLat: number;
    radarLon: number;
    maxRange: number;
    scale: number;
    translateX: number;
    translateY: number;
    containerWidth: number;
    containerHeight: number;
  } = $props();

  let now = $state(Date.now());
  let timer: ReturnType<typeof setInterval> | null = null;
  onMount(() => {
    // The age of a scan keeps growing: re-evaluate recent/stale every 10 s (cheap: one pass).
    timer = setInterval(() => (now = Date.now()), 10_000);
  });
  onDestroy(() => {
    if (timer) clearInterval(timer);
    $consentPrompt?.answer('cancel');
  });

  const sites = $derived($catalogue);
  const selected = $derived($selectedSiteId);
  /** The site of the file on screen (within 3 km of the map centre), if the catalogue has it. */
  const openId = $derived(nearestSite(sites, radarLat, radarLon, 3)?.site.id ?? null);

  function stateOf(id: string): ScanState {
    const c = $siteChecks[id.toUpperCase()];
    return c?.state === 'ok' ? scanState(liveAge(c.info, c.receivedAt, now)) : 'unknown';
  }

  const placed = $derived.by(() => {
    void $siteChecks;
    return placeSites(
      sites,
      (lat, lon) => latLonToPixel(lat, lon, radarLat, radarLon, maxRange, scale, translateX, translateY, containerWidth, containerHeight),
      {
        width: containerWidth,
        height: containerHeight,
        pinned: new Set([selected, openId].filter((x): x is string => !!x)),
        priority: (s) => (s.id === selected ? 1000 : 0) + (s.id === openId ? 500 : 0) + ($siteChecks[s.id.toUpperCase()] ? 100 : 0) + (s.custom ? 0 : 1),
      },
    );
  });

  const selectedSite = $derived(sites.find((s) => s.id === selected) ?? null);

  // Hover / focus label (id and name), above the label thinning.
  let tip = $state<{ site: RadarSite; x: number; y: number } | null>(null);

  function showTip(p: { site: RadarSite; x: number; y: number }) {
    tip = { site: p.site, x: p.x, y: p.y };
  }
  function hideTip() {
    tip = null;
  }

  function select(id: string) {
    selectedSiteId.set(id === selected ? null : id);
  }

  // ── Keyboard: one tab stop, arrows move to the nearest marker in that direction ─────────────
  let rootEl: HTMLDivElement;
  let rover = $state<string | null>(null);
  const tabbable = $derived(
    placed.some((p) => p.site.id === rover) ? rover : (placed.find((p) => p.site.id === selected)?.site.id ?? placed[0]?.site.id ?? null),
  );

  async function onKey(e: KeyboardEvent, from: { site: RadarSite; x: number; y: number }) {
    const dir: Record<string, [number, number]> = { ArrowRight: [1, 0], ArrowLeft: [-1, 0], ArrowDown: [0, 1], ArrowUp: [0, -1] };
    if (e.key === 'Escape' && selected) {
      e.stopPropagation();
      selectedSiteId.set(null);
      return;
    }
    const d = dir[e.key];
    if (!d) return;
    e.preventDefault();
    let best: (typeof placed)[number] | null = null;
    let bestScore = Infinity;
    for (const q of placed) {
      if (q === from) continue;
      const dx = q.x - from.x;
      const dy = q.y - from.y;
      const along = dx * d[0] + dy * d[1];
      if (along <= 0.5) continue;
      const across = Math.abs(dx * d[1] + dy * d[0]);
      const score = along + 2 * across; // prefer markers that are straight ahead
      if (score < bestScore) { bestScore = score; best = q; }
    }
    if (best) {
      rover = best.site.id;
      await tick();
      rootEl?.querySelector<HTMLButtonElement>(`[data-site="${CSS.escape(best.site.id)}"]`)?.focus();
    }
  }

  function label(p: { site: RadarSite }): string {
    const st = stateOf(p.site.id);
    const word = st === 'recent' ? 'latest scan under 10 minutes old' : st === 'stale' ? 'latest scan older than 10 minutes' : 'scan age not checked';
    return `${p.site.id}, ${p.site.name}${p.site.state ? ', ' + p.site.state : ''}. ${word}.`;
  }

  const stop = (e: Event) => e.stopPropagation();
</script>

<div class="rs-layer" bind:this={rootEl} role="group" aria-label="Radar sites. Arrow keys move between sites, Enter opens the site card.">
  {#each placed as p (p.site.id)}
    {@const st = stateOf(p.site.id)}
    {@const chk = $siteChecks[p.site.id.toUpperCase()]?.state}
    <button
      type="button"
      class="rs-dot"
      class:custom={p.site.custom}
      class:sel={p.site.id === selected}
      class:open={p.site.id === openId}
      class:loading={chk === 'loading'}
      data-state={st}
      data-site={p.site.id}
      tabindex={p.site.id === tabbable ? 0 : -1}
      style:left="{p.x}px"
      style:top="{p.y}px"
      aria-label={label(p)}
      aria-pressed={p.site.id === selected}
      onclick={(e) => { e.stopPropagation(); select(p.site.id); }}
      onmousedown={stop}
      onmouseup={stop}
      ondblclick={stop}
      oncontextmenu={stop}
      onmouseenter={() => showTip(p)}
      onmouseleave={hideTip}
      onfocus={() => { rover = p.site.id; showTip(p); }}
      onblur={hideTip}
      onkeydown={(e) => onKey(e, p)}
    >
      <span class="rs-mark" aria-hidden="true"></span>
    </button>
    {#if p.label}
      <span class="rs-lbl" class:sel={p.site.id === selected} style:left="{p.x + 9}px" style:top="{p.y}px" aria-hidden="true">{p.site.id}</span>
    {/if}
  {/each}

  {#if tip}
    <div class="rs-tip" style:left="{Math.min(Math.max(tip.x, 70), Math.max(70, containerWidth - 70))}px" style:top="{tip.y - 14}px" role="tooltip">
      <b class="mono">{tip.site.id}</b> {tip.site.name}{tip.site.state ? `, ${tip.site.state}` : ''}
    </div>
  {/if}

  {#if selectedSite}
    <RadarSiteCard site={selectedSite} isOpenFile={selectedSite.id === openId} {now} onclose={() => selectedSiteId.set(null)} />
  {/if}
</div>

{#if $consentPrompt}
  <RadarSiteConsent prompt={$consentPrompt} />
{/if}

<style>
  .rs-layer {
    position: absolute;
    inset: 0;
    z-index: 12;
    pointer-events: none;
    overflow: hidden;
  }
  .rs-dot {
    position: absolute;
    width: 22px;
    height: 22px;
    margin: -11px 0 0 -11px;
    padding: 0;
    border: 0;
    background: transparent;
    pointer-events: auto;
    cursor: pointer;
    display: grid;
    place-items: center;
    border-radius: 50%;
  }
  .rs-dot:focus-visible {
    outline: 2px solid var(--c-accent);
    outline-offset: 1px;
  }
  /* The mark: hollow = unknown, solid = recent, half-filled = stale (never colour alone). */
  .rs-mark {
    width: 11px;
    height: 11px;
    border-radius: 50%;
    box-sizing: border-box;
    border: 2px solid var(--c-text-dim);
    background: rgba(var(--c-surface-1-rgb), 0.85);
    box-shadow: 0 0 0 1px rgba(var(--c-surface-0-rgb), 0.7);
    transition: transform var(--dur-fast) var(--ease-out);
  }
  .rs-dot.custom .rs-mark {
    border-radius: 2px;
    transform: rotate(45deg);
  }
  .rs-dot:hover .rs-mark {
    transform: scale(1.25);
  }
  .rs-dot.custom:hover .rs-mark {
    transform: rotate(45deg) scale(1.25);
  }
  .rs-dot[data-state='recent'] .rs-mark {
    border-color: var(--c-nominal);
    background: var(--c-nominal);
  }
  .rs-dot[data-state='stale'] .rs-mark {
    border-color: var(--c-caution);
    background: linear-gradient(90deg, var(--c-caution) 50%, rgba(var(--c-surface-1-rgb), 0.85) 50%);
  }
  .rs-dot.open .rs-mark {
    box-shadow: 0 0 0 1px rgba(var(--c-surface-0-rgb), 0.7), 0 0 0 4px rgba(var(--c-accent-rgb), 0.45);
  }
  .rs-dot.sel .rs-mark {
    border-color: var(--c-accent);
    box-shadow: 0 0 0 1px rgba(var(--c-surface-0-rgb), 0.7), var(--glow-accent);
  }
  .rs-dot.loading .rs-mark {
    animation: rs-pulse 1s ease-in-out infinite;
  }
  @keyframes rs-pulse {
    50% { opacity: 0.35; }
  }
  .rs-lbl {
    position: absolute;
    transform: translateY(-50%);
    font: 500 var(--fs-2xs) / 1 var(--font-mono);
    letter-spacing: 0.02em;
    color: var(--c-text);
    text-shadow:
      0 0 3px rgba(var(--c-surface-0-rgb), 0.95),
      0 0 3px rgba(var(--c-surface-0-rgb), 0.95),
      0 0 1px rgba(var(--c-surface-0-rgb), 1);
    pointer-events: none;
    white-space: nowrap;
  }
  .rs-lbl.sel {
    color: var(--c-accent);
  }
  .rs-tip {
    position: absolute;
    transform: translate(-50%, -100%);
    padding: 3px 7px;
    border-radius: var(--radius-sm);
    background: var(--c-overlay);
    border: 1px solid var(--c-border-strong);
    color: var(--c-text);
    font-size: var(--fs-xs);
    white-space: nowrap;
    pointer-events: none;
    box-shadow: var(--shadow-2);
    z-index: 2;
  }
  .mono {
    font-family: var(--font-mono);
  }
  @media (prefers-reduced-motion: reduce) {
    .rs-dot.loading .rs-mark { animation: none; }
  }
</style>
