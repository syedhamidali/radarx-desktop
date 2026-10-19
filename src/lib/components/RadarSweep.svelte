<script lang="ts">
  // Decorative radar scope with a rotating sweep. Used on the welcome / boot / loading states
  // (empty areas only, never over data). Under prefers-reduced-motion the sweep is frozen at a
  // fixed bearing and the echoes stay lit.
  export let size = 220;
  /** Seconds per revolution. */
  export let period = 5;
  export let rings = 3;
  /** Show range labels on the rings. */
  export let labels = true;
  export let tone: 'accent' | 'alarm' = 'accent';
  export let hub = true;

  // A few echoes: [bearing deg, range 0..1, size px]
  const blips: [number, number, number][] = [
    [38, 0.58, 5],
    [112, 0.34, 4],
    [205, 0.72, 6],
    [284, 0.46, 4],
    [330, 0.82, 3],
  ];
  const ticks = Array.from({ length: 36 }, (_, i) => i * 10);
  const half = 100;
</script>

<div
  class="scope"
  class:alarm={tone === 'alarm'}
  style="--size: {size}px; --period: {period}s"
  aria-hidden="true"
>
  <svg viewBox="-100 -100 200 200" width={size} height={size}>
    {#each Array.from({ length: rings }, (_, i) => i + 1) as i}
      <circle r={(half - 6) * (i / rings)} class="ring" class:outer={i === rings} />
    {/each}
    <line x1="-94" y1="0" x2="94" y2="0" class="axis" />
    <line x1="0" y1="-94" x2="0" y2="94" class="axis" />
    {#each ticks as t}
      <line
        x1="0"
        y1={-(half - 6)}
        x2="0"
        y2={-(half - (t % 90 === 0 ? 13 : t % 30 === 0 ? 10 : 8.5))}
        class="tick"
        class:major={t % 30 === 0}
        transform="rotate({t})"
      />
    {/each}
    {#if labels}
      <text x="0" y="-100" class="lbl" text-anchor="middle" dy="-1">N</text>
      {#each Array.from({ length: rings - 1 }, (_, i) => i + 1) as i}
        <text x="3" y={-(half - 6) * (i / rings) - 2} class="lbl">{i * 50}</text>
      {/each}
    {/if}
  </svg>
  <div class="wedge"></div>
  {#each blips as [deg, r, s]}
    {@const rad = (deg * Math.PI) / 180}
    <span
      class="blip"
      style="left: {50 + Math.sin(rad) * r * 47}%; top: {50 - Math.cos(rad) * r * 47}%; width: {s}px; height: {s}px; animation-delay: {(deg / 360) * period}s"
    ></span>
  {/each}
  {#if hub}<span class="hub"></span>{/if}
</div>

<style>
  .scope {
    position: relative;
    width: var(--size);
    height: var(--size);
    flex-shrink: 0;
    --tone: var(--c-accent);
    --tone-rgb: var(--c-accent-rgb);
  }
  .scope.alarm { --tone: var(--c-alarm); --tone-rgb: var(--c-alarm-rgb); }
  svg { position: absolute; inset: 0; overflow: visible; }
  .ring { fill: none; stroke: var(--tone); stroke-opacity: 0.2; stroke-width: 0.8; }
  .ring.outer { stroke-opacity: 0.6; stroke-width: 1.4; }
  .axis { stroke: var(--tone); stroke-opacity: 0.18; stroke-width: 0.6; }
  .tick { stroke: var(--tone); stroke-opacity: 0.45; stroke-width: 0.8; }
  .tick.major { stroke-opacity: 0.85; stroke-width: 1.2; }
  .lbl { font-family: var(--font-mono); font-size: 5.5px; fill: var(--c-muted); }
  .wedge {
    position: absolute;
    inset: 3%;
    border-radius: 50%;
    background: conic-gradient(from 0deg, transparent 0deg, transparent 270deg, rgba(var(--tone-rgb), 0.04) 285deg, rgba(var(--tone-rgb), 0.38) 359deg, rgba(var(--tone-rgb), 0.8) 360deg);
    animation: hud-sweep var(--period) linear infinite;
  }
  .blip {
    position: absolute;
    transform: translate(-50%, -50%);
    border-radius: 50%;
    background: var(--c-accent-strong);
    box-shadow: 0 0 8px var(--tone);
    opacity: 0.08;
    animation: blip var(--period) linear infinite;
  }
  .hub {
    position: absolute;
    left: 50%;
    top: 50%;
    width: 5px;
    height: 5px;
    transform: translate(-50%, -50%);
    border-radius: 50%;
    background: var(--tone);
    box-shadow: 0 0 8px var(--tone);
  }
  @keyframes blip {
    0% { opacity: 1; }
    80% { opacity: 0.08; }
    100% { opacity: 0.08; }
  }
  @media (prefers-reduced-motion: reduce) {
    .wedge { animation: none; transform: rotate(48deg); }
    .blip { animation: none; opacity: 0.75; }
  }
</style>
