<script lang="ts">
  /**
   * The thermodynamics inset of the PERiLS movie: the app's own Skew-T and hodograph of the sounding
   * nearest in time, with the surface-based parcel path (computed from the same profile), the engine's
   * numbers (CAPE, CIN, LCL, LFC, EL, shear, helicity: only those the sounding diagnostics contain) and
   * the launch time with its offset from the clip time. Mounted by perils/perilsExtras.ts inside the
   * clip page, so it is part of the frame.
   */
  import SkewT from './SkewT.svelte';
  import Hodograph from './Hodograph.svelte';
  import { makeFrame, pathOf } from '../utils/skewT';
  import type { SoundingData } from '../stores/sounding';
  import type { ParcelPath } from '../perils/perilsThermo';
  import '../styles/provenance.css';

  import type { SoundingLevels } from '../perils/perilsData';
  let { data, parcel, title, when, rows, post = null, postLabel = '' }: { data: SoundingData; parcel: ParcelPath | null; title: string; when: string; rows: [string, string][]; post?: SoundingLevels | null; postLabel?: string } = $props();

  const f = makeFrame(560, 600, { left: 40, right: 64, top: 14, bottom: 28 });
  const pathD = $derived(parcel ? pathOf(f, parcel.path.map((q) => q[1]), parcel.path.map((q) => q[0])) : '');
  // the sounding of the same site after the storm (the cold pool), dashed over the pre-storm one
  const postT = $derived(post ? pathOf(f, post.temperature_c, post.pressure_hpa) : '');
  const postTd = $derived(post ? pathOf(f, post.dewpoint_c, post.pressure_hpa) : '');
</script>

<div class="inset">
  <header>
    <span class="chip"><i></i>OBSERVED</span>
    <strong>{title}</strong>
  </header>
  <p class="when">{when}</p>
  <div class="skew">
    <SkewT {data} />
    {#if pathD || postT}
      <svg class="over" viewBox="0 0 560 600" aria-hidden="true">
        {#if pathD}<path d={pathD} />{/if}
        {#if postT}<path class="post t" d={postT} />{/if}
        {#if postTd}<path class="post td" d={postTd} />{/if}
      </svg>
    {/if}
  </div>
  {#if postLabel}<p class="when">{postLabel}</p>{/if}
  <div class="lower">
    <div class="hodo"><Hodograph {data} /></div>
    <dl>
      {#each rows as [k, v] (k)}
        <dt>{k}</dt>
        <dd>{v}</dd>
      {/each}
    </dl>
  </div>
</div>

<style>
  .inset { width: 388px; background: rgba(var(--c-surface-0-rgb), 0.66); border: 1px solid rgba(var(--c-line-rgb), 0.32); border-radius: var(--radius-sm); padding: 8px 10px 10px; color: var(--c-text); font-family: var(--font-sans); }
  header { display: flex; align-items: center; gap: 8px; font-size: 15px; }
  .chip { display: inline-flex; align-items: center; gap: 6px; font: 700 12px var(--font-mono); padding: 2px 8px; border-radius: 5px; background: rgba(var(--c-prov-observed-rgb), 0.2); border: 1px solid rgba(var(--c-prov-observed-rgb), 0.85); }
  .chip i { width: 6px; height: 6px; border-radius: 50%; background: var(--c-prov-observed); }
  .when { margin: 3px 0 6px; font-size: 13px; color: var(--c-text-dim); }
  .skew { position: relative; }
  .over { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }
  .over path { fill: none; stroke: var(--c-caution); stroke-width: 2; stroke-dasharray: 5 3; vector-effect: non-scaling-stroke; }
  .over path.post { stroke-width: 1.6; stroke-dasharray: 2 3; opacity: 0.9; }
  .over path.post.t { stroke: var(--c-alarm); }
  .over path.post.td { stroke: var(--c-nominal); }
  .lower { display: flex; gap: 8px; margin-top: 8px; align-items: flex-start; }
  .hodo { width: 176px; flex: none; }
  .hodo :global(figcaption) { display: none; }
  dl { margin: 0; display: grid; grid-template-columns: max-content 1fr; gap: 2px 8px; font-size: 13px; align-content: start; }
  dt { color: var(--c-muted); }
  dd { margin: 0; font: 600 13px var(--font-mono); text-align: right; }
  /* the diagram's labels are drawn for a wide panel: make them readable at this size */
  .inset :global(.tick) { font-size: 14px; }
  .inset :global(.axis) { font-size: 13px; }
  .inset :global(.marktext) { font-size: 14px; }
  .inset :global(.rtext), .inset :global(.mtext) { font-size: 11px; }
</style>
