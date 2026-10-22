<script lang="ts">
  /**
   * LightningOverlay - observed lightning (GLM groups, LMA sources) over the 2D PPI and map.
   *
   * Points inside the time window (tied to the volume time) are drawn at their measured position;
   * the points of one flash are joined in time order; brightness is the point's strength (energy or
   * power, scaled in the engine) times a fade with age. Nothing is drawn where there is no data: a
   * window outside the files' time shows a notice, never an empty "no lightning". The picture is
   * the data only: a pale dot with a dark outline reads on both the dark and the light map.
   *
   * Same projection as the PPI and the map (latLonToDataSpace + the view transform of StormOverlay).
   */
  import { lightning, lightningSettings, lightningWindow, placedFor, visibleNow } from '../stores/lightning';
  import { channelsOf, flashesText, windowCoverageText } from '../utils/lightningLayer';
  import { formatUtc } from '../utils/provenance';
  import ProvenanceChip from './ProvenanceChip.svelte';

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

  let canvas: HTMLCanvasElement | undefined = $state();
  const data = $derived($lightning.data);
  const win = $derived($lightningWindow);
  const on = $derived($lightningSettings.show2d && !!data);

  const summary = $derived.by(() => {
    if (!data || !win) return null;
    void $lightningSettings;
    const vis = visibleNow();
    if (!vis) return null;
    const flashes = new Set<number>();
    for (const i of vis.idx) flashes.add(data.flash[i]);
    return { flashes: flashes.size, points: vis.idx.length, note: windowCoverageText(data, win.start, win.end) };
  });

  function draw() {
    const cv = canvas;
    if (!cv || !data) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, containerWidth);
    const h = Math.max(1, containerHeight);
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
      cv.width = Math.round(w * dpr);
      cv.height = Math.round(h * dpr);
    }
    const ctx = cv.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    if (!$lightningSettings.show2d) return;
    const vis = visibleNow();
    if (!vis || !vis.idx.length) return;
    const placed = placedFor(data, radarLat, radarLon);
    const aspect = w / h;
    const sx = (aspect >= 1 ? scale / aspect : scale) / maxRange;
    const sy = (aspect >= 1 ? scale : scale * aspect) / maxRange;
    const px = (i: number) => (((placed.x[i] + translateX) * sx + 1) / 2) * w;
    const py = (i: number) => ((1 - (placed.y[i] + translateY) * sy) / 2) * h;

    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    const bOf = new Map<number, number>();
    vis.idx.forEach((i, k) => bOf.set(i, vis.b[k]));
    // channels: the points of a flash in time order
    for (const list of channelsOf(data, vis.idx).values()) {
      if (list.length < 2) continue;
      const bMax = Math.max(...list.map((i) => bOf.get(i) ?? 0));
      ctx.beginPath();
      ctx.moveTo(px(list[0]), py(list[0]));
      for (let k = 1; k < list.length; k++) ctx.lineTo(px(list[k]), py(list[k]));
      ctx.strokeStyle = `rgba(14,18,34,${(0.55 * bMax).toFixed(3)})`;
      ctx.lineWidth = 3;
      ctx.stroke();
      ctx.strokeStyle = `rgba(255,236,150,${(0.85 * bMax).toFixed(3)})`;
      ctx.lineWidth = 1.2;
      ctx.stroke();
    }
    // points: pale dot, dark outline; radius and opacity follow the faded brightness
    for (let k = 0; k < vis.idx.length; k++) {
      const i = vis.idx[k];
      const b = vis.b[k];
      const x = px(i);
      const y = py(i);
      if (x < -10 || y < -10 || x > w + 10 || y > h + 10) continue;
      const r = 1.6 + 2.6 * b;
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(255,244,176,${Math.min(1, 0.25 + 0.75 * b).toFixed(3)})`;
      ctx.fill();
      ctx.lineWidth = 1;
      ctx.strokeStyle = `rgba(14,18,34,${Math.min(0.9, 0.2 + 0.7 * b).toFixed(3)})`;
      ctx.stroke();
    }
  }

  $effect(() => {
    // redraw when the view, the data, the window or the settings change
    void [scale, translateX, translateY, maxRange, containerWidth, containerHeight, radarLat, radarLon, data, win, $lightningSettings, canvas];
    draw();
  });
</script>

{#if data}
  <canvas bind:this={canvas} class="lightning-canvas" style:width="{containerWidth}px" style:height="{containerHeight}px" aria-hidden="true"></canvas>
  {#if on}
    <div class="ltg-badge" data-testid="lightning-badge" role="status">
      <ProvenanceChip provenance={{ kind: 'observed', source: data.instrument }} compact />
      <span class="ltg-title">{data.kind === 'glm' ? 'GLM' : 'LMA'}</span>
      {#if win && summary}
        <span class="mono">{flashesText(summary.flashes)} · {formatUtc(new Date(data.tRefMs + win.start * 1000).toISOString()).replace(' UTC', '')} to {new Date(data.tRefMs + win.end * 1000).toISOString().slice(11, 16)} UTC</span>
      {:else}
        <span class="mono">open a radar volume: the window follows its time</span>
      {/if}
      {#if summary?.note}<span class="ltg-note">{summary.note}</span>{/if}
      {#if data.kind === 'glm'}<span class="ltg-note">GLM has no height; positions as the product gives them (parallax: see the Lightning panel).</span>{/if}
    </div>
  {/if}
{/if}

<style>
  .lightning-canvas { position: absolute; left: 0; top: 0; pointer-events: none; z-index: 4; }
  .ltg-badge {
    position: absolute; left: var(--space-3); top: var(--space-3); z-index: 5; max-width: min(520px, calc(100% - 2 * var(--space-3)));
    display: flex; flex-wrap: wrap; align-items: center; gap: 2px var(--space-2); padding: 4px var(--space-2);
    background: rgba(var(--c-surface-1-rgb), 0.82); border: 1px solid var(--c-border); border-radius: var(--radius-sm);
    font-size: var(--fs-xs); color: var(--c-text); pointer-events: none; backdrop-filter: blur(4px);
  }
  .ltg-title { font-weight: 600; letter-spacing: 0.04em; }
  .ltg-note { flex-basis: 100%; color: var(--c-text-dim); line-height: 1.35; }
</style>
