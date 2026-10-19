<script lang="ts">
  /**
   * Hodograph of the sounding's winds (u east, v north, m/s), the trace coloured by height above the
   * launch site, with the 0-6 km mean wind and the Bunkers storm motions marked. Rings are every
   * 10 m/s. The numbers (mean wind, storm motion) come from the engine.
   */
  import { HODO_LAYERS, aglOf, hodographFrame, hodographPaths, hodoPoint } from '../utils/skewT';
  import type { SoundingData } from '../stores/sounding';

  let { data }: { data: SoundingData } = $props();

  const SIZE = 260;
  const lv = $derived(data.levels);
  const zAgl = $derived(aglOf(lv.height_msl_m, data.surface_height_m));
  const storm = $derived(data.diagnostics?.storm_motion as any);
  const extra = $derived(
    (['right_mover', 'left_mover', 'mean_wind_0_6km'] as const)
      .map((k) => storm?.[k])
      .filter(Boolean)
      .map((m: any) => [m.u, m.v] as [number, number]),
  );
  const frame = $derived(hodographFrame(SIZE, lv.u_ms, lv.v_ms, extra));
  const paths = $derived(hodographPaths(frame, lv.u_ms, lv.v_ms, zAgl));
  const rings = $derived(Array.from({ length: frame.max / 10 }, (_, i) => (i + 1) * 10));
  const has = $derived(paths.length > 0);
  const marks = $derived(
    (
      [
        ['RM', storm?.right_mover],
        ['LM', storm?.left_mover],
        ['MW', storm?.mean_wind_0_6km],
      ] as [string, { u: number; v: number } | undefined][]
    )
      .filter(([, m]) => m)
      .map(([name, m]) => ({ name, xy: hodoPoint(frame, (m as any).u, (m as any).v) })),
  );
  const label = $derived(has ? 'Hodograph: winds with height, coloured by layer above the launch site' : 'Hodograph: this sounding has no winds');
</script>

<figure class="hodo">
  <svg viewBox="0 0 {SIZE} {SIZE}" role="img" aria-label={label} data-testid="hodograph">
    <title>{label}</title>
    <rect class="bg" x="0" y="0" width={SIZE} height={SIZE} />
    {#each rings as r (r)}
      <circle class="ring" cx={frame.cx} cy={frame.cy} r={r * frame.scale} />
      <text class="rtext" x={frame.cx + r * frame.scale * 0.707 + 2} y={frame.cy - r * frame.scale * 0.707 - 2}>{r}</text>
    {/each}
    <line class="axis" x1="0" x2={SIZE} y1={frame.cy} y2={frame.cy} />
    <line class="axis" x1={frame.cx} x2={frame.cx} y1="0" y2={SIZE} />
    {#each paths as pth, i (i)}
      <path class="trace" d={pth.d} style="stroke:{HODO_LAYERS[pth.layer].token}" />
    {/each}
    {#each marks as m (m.name)}
      <circle class="mark {m.name}" cx={m.xy[0]} cy={m.xy[1]} r="4" />
      <text class="mtext" x={m.xy[0] + 6} y={m.xy[1] + 3}>{m.name}</text>
    {/each}
    {#if !has}
      <text class="none" x={SIZE / 2} y={SIZE / 2} text-anchor="middle">no winds in this file</text>
    {/if}
  </svg>
  <figcaption>
    <span class="legend">
      {#each HODO_LAYERS.slice(0, 4) as l (l.label)}<span class="sw"><i style="background:{l.token}"></i>{l.label}</span>{/each}
    </span>
    <span class="cap">m/s, rings every 10; RM / LM Bunkers right / left mover, MW 0-6 km mean wind</span>
  </figcaption>
</figure>

<style>
  .hodo { margin: 0; }
  svg { width: 100%; max-width: 320px; height: auto; display: block; margin: 0 auto; }
  .bg { fill: var(--c-surface-1); stroke: var(--c-border); }
  .ring { fill: none; stroke: rgba(var(--c-line-rgb), 0.28); stroke-width: 1; }
  .axis { stroke: rgba(var(--c-line-rgb), 0.4); stroke-width: 1; }
  .rtext { fill: var(--c-muted); font: 9px var(--font-mono); }
  .trace { fill: none; stroke-width: 2.4; stroke-linecap: round; stroke-linejoin: round; }
  .mark { fill: var(--c-surface-1); stroke: var(--c-text); stroke-width: 1.5; }
  .mark.RM { stroke: var(--c-alarm); }
  .mark.LM { stroke: var(--c-accent); }
  .mark.MW { stroke: var(--c-text-dim); stroke-dasharray: 2 2; }
  .mtext { fill: var(--c-text-dim); font: 600 10px var(--font-mono); }
  .none { fill: var(--c-muted); font: 11px var(--font-sans); }
  figcaption { display: flex; flex-direction: column; gap: 2px; margin-top: 4px; font-size: var(--fs-xs); color: var(--c-muted); }
  .legend { display: flex; flex-wrap: wrap; gap: 4px 10px; color: var(--c-text-dim); }
  .sw { display: inline-flex; align-items: center; gap: 4px; }
  .sw i { width: 14px; height: 3px; border-radius: 1px; display: inline-block; }
</style>
