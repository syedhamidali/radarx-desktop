<script lang="ts">
  /**
   * LightningSeries - a small bar chart of flashes (or points) per minute over the loaded data.
   * Bins the files do not cover completely are drawn as hatched gaps ("no data"), never as zero.
   * The shaded band is the layer's time window, the dashed line the volume time.
   */
  import type { Series } from '../utils/lightningLayer';

  let {
    series,
    title,
    unit,
    windowS = null,
    volumeS = null,
    tRefMs,
    color = 'var(--c-accent)',
    marks = [],
  }: {
    series: Series | null;
    title: string;
    unit: string;
    windowS?: { start: number; end: number } | null;
    volumeS?: number | null;
    tRefMs: number;
    color?: string;
    /** Times (seconds after tRef) of lightning jumps to mark. */
    marks?: number[];
  } = $props();

  const W = 280;
  const H = 74;
  const PAD = { l: 4, r: 4, t: 6, b: 16 };
  const uid = Math.random().toString(36).slice(2, 8);

  const geom = $derived.by(() => {
    if (!series || series.edges.length < 2) return null;
    const t0 = series.edges[0];
    const t1 = series.edges[series.edges.length - 1];
    const finite = series.rate.filter((v) => Number.isFinite(v));
    const max = Math.max(1, ...finite);
    const x = (t: number) => PAD.l + ((t - t0) / (t1 - t0)) * (W - PAD.l - PAD.r);
    const y = (v: number) => H - PAD.b - (v / max) * (H - PAD.b - PAD.t);
    const bars = series.rate.map((v, k) => ({
      x: x(series.edges[k]),
      w: Math.max(1, x(series.edges[k + 1]) - x(series.edges[k]) - 0.6),
      y: Number.isFinite(v) ? y(v) : PAD.t,
      h: Number.isFinite(v) ? Math.max(0, H - PAD.b - y(v)) : H - PAD.b - PAD.t,
      gap: !Number.isFinite(v),
      v,
      c: series.count[k],
    }));
    return { t0, t1, max, bars, x };
  });

  const hhmm = (s: number) => new Date(tRefMs + s * 1000).toISOString().slice(11, 16);
  const total = $derived(series ? series.count.reduce((a, c) => a + (Number.isFinite(c) ? c : 0), 0) : 0);
  const gaps = $derived(series ? series.count.filter((c) => !Number.isFinite(c)).length : 0);
  const label = $derived(
    geom
      ? `${title}: ${total} in the covered bins, peak ${Number(geom.max.toPrecision(3))} per minute, from ${hhmm(geom.t0)} to ${hhmm(geom.t1)} UTC${gaps ? `, ${gaps} bins without data` : ''}`
      : `${title}: no data`,
  );
</script>

<figure class="ser">
  <figcaption>{title} <span class="unit">({unit})</span></figcaption>
  {#if geom}
    <svg viewBox="0 0 {W} {H}" role="img" aria-label={label} data-testid="lightning-series">
      <defs>
        <pattern id="gap-{uid}" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <line x1="0" y1="0" x2="0" y2="5" stroke="var(--c-muted)" stroke-width="1" opacity="0.55" />
        </pattern>
      </defs>
      {#if windowS}
        {@const a = Math.max(geom.t0, windowS.start)}
        {@const b = Math.min(geom.t1, windowS.end)}
        {#if b > a}<rect x={geom.x(a)} y={PAD.t - 2} width={Math.max(1.5, geom.x(b) - geom.x(a))} height={H - PAD.b - PAD.t + 2} fill="var(--c-accent)" opacity="0.16" />{/if}
      {/if}
      <line x1={PAD.l} x2={W - PAD.r} y1={H - PAD.b} y2={H - PAD.b} stroke="var(--c-border-strong)" stroke-width="1" />
      {#each geom.bars as bar, k (k)}
        {#if bar.gap}
          <rect x={bar.x} y={PAD.t} width={bar.w} height={H - PAD.b - PAD.t} fill="url(#gap-{uid})"><title>no data in this bin</title></rect>
        {:else if bar.h > 0}
          <rect x={bar.x} y={bar.y} width={bar.w} height={bar.h} fill={color} opacity="0.85"><title>{hhmm(series!.edges[k])} UTC: {bar.c} ({Number(bar.v.toPrecision(3))} per minute)</title></rect>
        {/if}
      {/each}
      {#if volumeS !== null && volumeS >= geom.t0 && volumeS <= geom.t1}
        <line x1={geom.x(volumeS)} x2={geom.x(volumeS)} y1={PAD.t - 2} y2={H - PAD.b} stroke="var(--c-text)" stroke-width="1" stroke-dasharray="3 2" />
      {/if}
      {#each marks as m (m)}
        {#if m >= geom.t0 && m <= geom.t1}
          <line x1={geom.x(m)} x2={geom.x(m)} y1={PAD.t - 2} y2={H - PAD.b} stroke="var(--c-alarm)" stroke-width="1.6"><title>lightning jump starts {hhmm(m)} UTC</title></line>
        {/if}
      {/each}
      <text x={PAD.l} y={H - 3} class="tick">{hhmm(geom.t0)}</text>
      <text x={W - PAD.r} y={H - 3} class="tick" text-anchor="end">{hhmm(geom.t1)} UTC</text>
      <text x={W - PAD.r} y={PAD.t + 4} class="tick" text-anchor="end">max {Number(geom.max.toPrecision(3))}</text>
    </svg>
    {#if gaps}<p class="gapnote">Hatched: no data in the loaded files (not "no lightning").</p>{/if}
  {:else}
    <p class="gapnote">No data to plot.</p>
  {/if}
</figure>

<style>
  .ser { margin: 0; }
  figcaption { font-size: 10.5px; text-transform: uppercase; letter-spacing: var(--ls-label); color: var(--c-muted); }
  .unit { text-transform: none; letter-spacing: 0; }
  svg { width: 100%; height: auto; display: block; }
  .tick { font-size: 9px; fill: var(--c-text-dim); font-family: var(--font-mono); }
  .gapnote { margin: 2px 0 0; font-size: 10.5px; color: var(--c-muted); }
</style>
