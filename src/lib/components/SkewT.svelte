<script lang="ts">
  /**
   * Skew-T log-p diagram of one sounding, drawn as SVG with the app's tokens (both themes).
   * Reference curves: isotherms (0 degC emphasised), dry adiabats, saturation mixing-ratio lines and
   * pseudo-adiabats. Data: temperature and dew point (only where the file reported it), wind barbs
   * in knots on the right, height above the launch site on the right axis. The LCL, LFC and EL of
   * the selected parcel are marked at their pressure; the parcel's path is NOT drawn (every number
   * here comes from the engine, and the engine does not return the path).
   */
  import {
    P_BOTTOM,
    P_TOP,
    PRESSURE_TICKS,
    T_MIN,
    T_MAX,
    SKEW,
    makeFrame,
    project,
    pathOf,
    dryAdiabatC,
    mixingRatioLineC,
    pseudoAdiabat,
    heightAtPressure,
    pressureLevels,
  } from '../utils/skewT';
  import { barbPath, type ParcelDiag } from '../utils/soundingView';
  import type { SoundingData } from '../stores/sounding';

  let { data, parcel, markFreezing = true }: { data: SoundingData; parcel?: ParcelDiag; markFreezing?: boolean } = $props();

  const W = 560;
  const H = 600;
  const f = makeFrame(W, H, { left: 40, right: 64, top: 14, bottom: 28 });
  const lv = $derived(data.levels);
  const p = $derived(lv.pressure_hpa);

  const levels = pressureLevels(10);
  const isotherms = Array.from({ length: 16 }, (_, i) => -140 + i * 10); // -140 .. 10: skewed lines reach the diagram everywhere
  const isothermD = (t: number) => pathOf(f, levels.map(() => t), levels);
  const dryD = (theta: number) => pathOf(f, levels.map((q) => dryAdiabatC(theta, q)), levels);
  const mixingD = (w: number) => {
    const ps = pressureLevels(25).filter((q) => q >= 600);
    return pathOf(f, ps.map((q) => mixingRatioLineC(w, q)), ps);
  };
  const moistD = (t0: number) => {
    const pa = pseudoAdiabat(t0, P_TOP, 20);
    return pathOf(f, pa.map(([, t]) => t), pa.map(([q]) => q));
  };
  const thetas = Array.from({ length: 17 }, (_, i) => 250 + i * 10);
  const mixing = [1, 2, 4, 8, 12, 16, 20];
  const moist = [-8, -4, 0, 4, 8, 12, 16, 20, 24, 28, 32];

  const tPath = $derived(pathOf(f, lv.temperature_c, p));
  const tdPath = $derived(pathOf(f, lv.dewpoint_c, p));
  const ground = $derived(data.surface_height_m);
  const heightTicks = $derived(
    PRESSURE_TICKS.map((q) => ({ p: q, z: heightAtPressure(p, lv.height_msl_m, q) }))
      .filter((t) => t.z !== null)
      .map((t) => ({ p: t.p, km: ((t.z as number) - ground) / 1000 })),
  );

  // wind barbs at the levels closest to the standard ones
  const barbs = $derived.by(() => {
    const out: { y: number; d: string }[] = [];
    for (const q of [1000, 925, 850, 800, 700, 600, 500, 400, 300, 250, 200, 150]) {
      let best = -1;
      let dist = Infinity;
      for (let i = 0; i < p.length; i++) {
        const pi = p[i];
        if (pi == null || lv.u_ms[i] == null || lv.v_ms[i] == null) continue;
        const d = Math.abs(Math.log(pi / q));
        if (d < dist) {
          dist = d;
          best = i;
        }
      }
      if (best < 0 || dist > 0.06) continue;
      const [, y] = project(f, 0, p[best] as number);
      out.push({ y, d: barbPath(f.right + 22, y, lv.u_ms[best] as number, lv.v_ms[best] as number, 22) });
    }
    return out;
  });

  const marks = $derived(
    parcel
      ? ([
          ['LCL', parcel.lcl_hpa],
          ['LFC', parcel.lfc_hpa],
          ['EL', parcel.el_hpa],
        ] as [string, number | null | undefined][])
          .filter(([, q]) => q != null && (q as number) <= P_BOTTOM && (q as number) >= P_TOP)
          .map(([name, q]) => ({ name, y: project(f, 0, q as number)[1] }))
      : [],
  );

  const label = $derived(
    `Skew-T log-p diagram of the ${data.station || 'sounding'} ${data.launch_time || ''} profile: ${lv.n_levels} levels` +
      (data.diagnostics?.parcels?.['surface-based']?.cape != null ? `, surface-based CAPE ${data.diagnostics.parcels['surface-based'].cape} J/kg` : ''),
  );
  const clipId = `skew-clip-${Math.random().toString(36).slice(2, 7)}`;
</script>

<svg class="skewt" viewBox="0 0 {W} {H}" role="img" aria-label={label} data-testid="skewt">
  <title>{label}</title>
  <defs>
    <clipPath id={clipId}><rect x={f.left} y={f.top} width={f.right - f.left} height={f.bottom - f.top} /></clipPath>
  </defs>
  <rect class="plot" x={f.left} y={f.top} width={f.right - f.left} height={f.bottom - f.top} />
  <g clip-path="url(#{clipId})">
    {#each moist as t0 (t0)}<path class="moist" d={moistD(t0)} />{/each}
    {#each thetas as th (th)}<path class="dry" d={dryD(th)} />{/each}
    {#each mixing as w (w)}<path class="mix" d={mixingD(w)} />{/each}
    {#each isotherms as t (t)}<path class="iso" class:zero={t === 0} d={isothermD(t)} />{/each}
    {#each PRESSURE_TICKS as q (q)}
      <line class="pline" x1={f.left} x2={f.right} y1={project(f, 0, q)[1]} y2={project(f, 0, q)[1]} />
    {/each}
    <path class="dewpoint" d={tdPath} />
    <path class="temp" d={tPath} />
  </g>

  <!-- pressure axis -->
  {#each PRESSURE_TICKS as q (q)}
    <text class="tick" x={f.left - 5} y={project(f, 0, q)[1] + 3.5} text-anchor="end">{q}</text>
  {/each}
  <text class="axis" x={10} y={f.top + 8} transform="rotate(-90 10 {f.top + 8})" text-anchor="end">hPa</text>
  <!-- temperature axis (at the bottom edge, where the isotherms meet it) -->
  {#each [-30, -20, -10, 0, 10, 20, 30, 40] as t (t)}
    <text class="tick" x={project(f, t, P_BOTTOM)[0]} y={f.bottom + 14} text-anchor="middle">{t}</text>
  {/each}
  <text class="axis" x={f.right} y={H - 3} text-anchor="end">temperature, degC</text>
  <!-- height axis, km above the launch site -->
  {#each heightTicks as t (t.p)}
    <text class="tick km" x={f.right + 4} y={project(f, 0, t.p)[1] - 2}>{t.km.toFixed(t.km < 10 ? 1 : 0)} km</text>
  {/each}
  <text class="axis" x={W - 4} y={f.top + 8} text-anchor="end">AGL</text>

  {#each barbs as b (b.y)}
    <path class="barb" d={b.d} />
  {/each}

  {#each marks as m (m.name)}
    <line class="mark" x1={f.left} x2={f.left + 26} y1={m.y} y2={m.y} />
    <text class="marktext" x={f.left + 30} y={m.y + 3.5}>{m.name}</text>
  {/each}
</svg>

<style>
  .skewt { width: 100%; height: auto; display: block; background: var(--c-surface-0); border: 1px solid var(--c-border); border-radius: var(--radius-sm); }
  .plot { fill: var(--c-surface-1); stroke: var(--c-border-strong); stroke-width: 1; }
  path { fill: none; vector-effect: non-scaling-stroke; }
  .iso { stroke: rgba(var(--c-line-rgb), 0.22); stroke-width: 1; }
  .iso.zero { stroke: rgba(var(--c-accent-rgb), 0.7); stroke-width: 1.4; }
  .dry { stroke: rgba(var(--c-caution-rgb), 0.3); stroke-width: 1; }
  .mix { stroke: rgba(var(--c-nominal-rgb), 0.35); stroke-width: 1; stroke-dasharray: 3 3; }
  .moist { stroke: rgba(var(--c-accent-rgb), 0.25); stroke-width: 1; stroke-dasharray: 1 3; }
  .pline { stroke: rgba(var(--c-line-rgb), 0.3); stroke-width: 1; }
  .temp { stroke: var(--c-alarm); stroke-width: 2.2; }
  .dewpoint { stroke: var(--c-nominal); stroke-width: 2.2; }
  .barb { stroke: var(--c-text-dim); stroke-width: 1.1; }
  .tick { fill: var(--c-text-dim); font: 10px var(--font-mono); }
  .tick.km { fill: var(--c-muted); }
  .axis { fill: var(--c-muted); font: 10px var(--font-sans); letter-spacing: 0.06em; }
  .mark { stroke: var(--c-accent); stroke-width: 1.6; }
  .marktext { fill: var(--c-accent); font: 600 10px var(--font-mono); }
</style>
