<script lang="ts">
  /**
   * LightningHeights - how many LMA sources (bars) and flash initiations (dots) lie in each height bin,
   * km above mean sea level. Counted by radarx in the engine; the numbers under the chart are the
   * median and the peak height of the sources. This is a distribution of located sources: it depends
   * on the range from the network (shallow sources far away are missed first). It does not say where
   * charge is; that would be an interpretation and is not offered.
   */
  import type { LightningStats } from '../utils/lightningLayer';
  import { heightPercentile, modeHeight } from '../utils/lightningLayer';

  let { stats }: { stats: LightningStats } = $props();

  const W = 280;
  const H = 150;
  const PAD = { l: 30, r: 6, t: 6, b: 16 };

  const g = $derived.by(() => {
    const z = stats.height.zKm;
    const step = stats.height.zStepM / 1000;
    const max = Math.max(1, ...stats.height.sources);
    const maxIni = Math.max(1, ...stats.height.initiations);
    const y = (km: number) => PAD.t + (1 - (km - (z[0] - step / 2)) / (z[z.length - 1] - z[0] + step)) * (H - PAD.t - PAD.b);
    const x = (v: number) => PAD.l + (v / max) * (W - PAD.l - PAD.r);
    const xi = (v: number) => PAD.l + (v / maxIni) * (W - PAD.l - PAD.r);
    return { z, step, max, maxIni, y, x, xi };
  });
  const median = $derived(heightPercentile(stats.height.zKm, stats.height.sources, 0.5, stats.height.zStepM / 1000));
  const peak = $derived(modeHeight(stats.height.zKm, stats.height.sources));
  const total = $derived(stats.height.sources.reduce((a, c) => a + c, 0));
  const label = $derived(`Height of ${total} sources, median ${Number.isFinite(median) ? median.toFixed(1) : 'unknown'} km, most in the bin at ${Number.isFinite(peak) ? peak.toFixed(1) : 'unknown'} km above mean sea level`);
  const ticks = $derived.by(() => {
    const lo = Math.ceil(g.z[0] - g.step / 2);
    const hi = Math.floor(g.z[g.z.length - 1] + g.step / 2);
    const out: number[] = [];
    const every = hi - lo > 12 ? 4 : hi - lo > 6 ? 2 : 1;
    for (let k = lo; k <= hi; k += every) out.push(k);
    return out;
  });
</script>

<figure class="hts" data-testid="lightning-heights">
  <figcaption>Source height <span class="unit">(km above mean sea level)</span></figcaption>
  <svg viewBox="0 0 {W} {H}" role="img" aria-label={label}>
    {#each ticks as k (k)}
      <line x1={PAD.l} x2={W - PAD.r} y1={g.y(k)} y2={g.y(k)} stroke="var(--c-border)" stroke-width="0.6" />
      <text x={PAD.l - 4} y={g.y(k) + 3} class="tick" text-anchor="end">{k}</text>
    {/each}
    {#each g.z as zc, i (i)}
      {#if stats.height.sources[i] > 0}
        <rect x={PAD.l} y={g.y(zc + g.step / 2) + 0.4} width={g.x(stats.height.sources[i]) - PAD.l} height={Math.max(1, g.y(zc - g.step / 2) - g.y(zc + g.step / 2) - 0.8)} fill="var(--c-accent)" opacity="0.8"><title>{zc.toFixed(2)} km: {stats.height.sources[i]} sources, {stats.height.initiations[i]} flash initiations</title></rect>
      {/if}
      {#if stats.height.initiations[i] > 0}
        <circle cx={g.xi(stats.height.initiations[i])} cy={g.y(zc)} r="2.6" fill="var(--c-text)" />
      {/if}
    {/each}
    {#if Number.isFinite(median)}
      <line x1={PAD.l} x2={W - PAD.r} y1={g.y(median)} y2={g.y(median)} stroke="var(--c-caution)" stroke-width="1" stroke-dasharray="4 3" />
    {/if}
    <text x={W - PAD.r} y={H - 3} class="tick" text-anchor="end">sources (max {g.max}), dots: initiations (max {g.maxIni})</text>
  </svg>
  <p class="note">Median {Number.isFinite(median) ? median.toFixed(1) : '?'} km (dashed), most sources near {Number.isFinite(peak) ? peak.toFixed(1) : '?'} km. Located sources only: shallow ones far from the array are missed first.</p>
</figure>

<style>
  .hts { margin: 0; }
  figcaption { font-size: 10.5px; text-transform: uppercase; letter-spacing: var(--ls-label); color: var(--c-muted); }
  .unit { text-transform: none; letter-spacing: 0; }
  svg { width: 100%; height: auto; display: block; }
  .tick { font-size: 9px; fill: var(--c-text-dim); font-family: var(--font-mono); }
  .note { margin: 2px 0 0; font-size: 10.5px; color: var(--c-muted); line-height: 1.4; }
</style>
