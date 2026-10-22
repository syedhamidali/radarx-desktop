<script lang="ts">
  /**
   * Layers: every layer the app currently holds (radar moments of each open file, derived
   * variables, mosaic, winds, storm cells, cross-section, 3D volume, sounding, fetched environment
   * data), each with a provenance chip, its source, valid time and the time offset to the active
   * radar scan. Nothing here is a control: it is the answer to "what am I looking at, and where
   * does it come from?".
   */
  import { layerList, activeScanTime } from '../stores/layers';
  import { activeFileId } from '../stores/fileManager';
  import { liveShownFrame } from '../stores/live';
  import { liveLayerLine } from '../utils/liveDisplay';
  import { KINDS, KIND_INFO, formatOffset, formatUtc, type LayerGroup } from '../utils/provenance';
  import CollapsiblePanel from './CollapsiblePanel.svelte';
  import ProvenanceChip from './ProvenanceChip.svelte';

  const rows = $derived($layerList);
  const scan = $derived($activeScanTime);
  // With no radar file open, the live composite is what the panel describes: say when it is valid
  // and which radars it is made of, instead of "unknown".
  const liveFrame = $derived($liveShownFrame);
  const hasLive = $derived(rows.some((r) => r.id === 'live:composite'));

  /** "2022-03-30 00:05 UTC · +12 min" (the active scan itself has no offset); "time unknown" when the engine gave none. */
  function whenText(r: { id: string; provenance: { valid_time?: string } | null; offset: number | null }): string {
    const t = r.provenance?.valid_time;
    if (!t) return 'time unknown';
    // no offset when there is no active radar scan to count from (the live composite alone)
    return r.id === `file:${$activeFileId}` || (r.offset === null && !scan) ? formatUtc(t) : `${formatUtc(t)} \u00b7 ${formatOffset(r.offset)}`;
  }

  const grouped = $derived.by(() => {
    const out: { group: LayerGroup; items: typeof rows }[] = [];
    for (const r of rows) {
      const g = out.find((x) => x.group === r.group);
      if (g) g.items.push(r);
      else out.push({ group: r.group, items: [r] });
    }
    return out;
  });
</script>

<CollapsiblePanel title="Layers" badge={rows.length || null}>
  <div class="layers">
    {#if rows.length === 0}
      <p class="empty">No layers yet. Open a radar file, run a module or fetch data and each layer is listed here with what it is and where it comes from.</p>
    {:else}
      <p class="ref mono" title="Valid time of the radar moments of the active file; every offset below is relative to it.">
        {#if scan}Radar scan: {formatUtc(scan)}{:else if hasLive && liveFrame}{liveLayerLine(liveFrame.time, liveFrame.radars.map((r) => r.station))}{:else}Radar scan: unknown{/if}
      </p>
      {#each grouped as g (g.group)}
        <h4 class="hud-label">{g.group}</h4>
        <ul>
          {#each g.items as r (r.id)}
            <li class:active={r.id === `file:${$activeFileId}`} data-layer={r.id}>
              <ProvenanceChip provenance={r.provenance} offset={r.offset} title={r.title} compact />
              <div class="main">
                <span class="name" title={r.title}>{r.title}</span>
                <span class="src" title={r.provenance?.source ?? ''}>{r.provenance?.source ?? 'origin not known'}</span>
                <span class="when mono">{whenText(r)}</span>
              </div>
            </li>
          {/each}
        </ul>
      {/each}
    {/if}

    <details class="legend">
      <summary class="hud-label">What the chips mean</summary>
      <dl>
        {#each KINDS as k (k)}
          <dt><ProvenanceChip provenance={{ kind: k, source: KIND_INFO[k].label }} compact /></dt>
          <dd><strong>{KIND_INFO[k].label}.</strong> {KIND_INFO[k].definition}</dd>
        {/each}
      </dl>
    </details>
  </div>
</CollapsiblePanel>

<style>
  .layers { display: flex; flex-direction: column; gap: var(--space-2); padding: var(--space-3); }
  .empty { margin: 0; color: var(--c-muted); font-size: var(--fs-xs); line-height: 1.5; }
  .ref { margin: 0; font-size: var(--fs-xs); color: var(--c-text-dim); }
  h4 { margin: var(--space-2) 0 0; }
  ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
  li {
    display: flex;
    align-items: flex-start;
    gap: var(--space-2);
    padding: 5px var(--space-2);
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
  }
  li:hover { background: var(--bg-hover); border-color: var(--c-border); }
  li.active { border-color: rgba(var(--c-accent-rgb), 0.35); background: rgba(var(--c-accent-rgb), 0.05); }
  li :global(.prov-wrap) { margin-top: 1px; }
  .main { display: flex; flex-direction: column; min-width: 0; flex: 1; }
  .name { font-size: var(--fs-sm); color: var(--c-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .src { font-size: var(--fs-xs); color: var(--c-text-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .when { font-size: 10.5px; color: var(--c-muted); }
  .legend { margin-top: var(--space-2); border-top: 1px solid var(--c-border); padding-top: var(--space-2); }
  .legend summary { cursor: pointer; }
  .legend dl { display: grid; grid-template-columns: max-content 1fr; gap: 6px var(--space-2); margin: var(--space-2) 0 0; font-size: var(--fs-xs); color: var(--c-text-dim); line-height: 1.45; }
  .legend dt { margin: 0; }
  .legend dd { margin: 0; }
</style>
