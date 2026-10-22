<script lang="ts">
  /**
   * NowcastOverlay - what the map prints on every nowcast frame: the FORECAST label (class chip FCST,
   * lead, origin and valid time, position uncertainty, the guidance sentence and the engine's skill
   * statement) at the top left, and the lead-time slider at the bottom. It is part of the map, so
   * the label cannot be missed or hidden while a forecast frame is drawn. "Back to observations"
   * takes the forecast off the map.
   */
  import { mosaicResult, mosaicLayer } from '../stores/mosaic';
  import { nowcastResult, nowcastView, setLeadIndex, stepLead, togglePlaying, hideNowcastFromMap } from '../stores/nowcast';
  import { formatLead } from '../utils/nowcastLogic';
  import ProvenanceChip from './ProvenanceChip.svelte';

  const tag = $derived($mosaicResult?.forecast ?? null);
  const shown = $derived(!!tag && !!$mosaicLayer?.visible);
  const res = $derived($nowcastResult);
  const playing = $derived($nowcastView.playing);
  let skillOpen = $state(false);

  // The map under this overlay pans and zooms with the mouse: the controls keep their events.
  const stop = (e: Event) => e.stopPropagation();

  function onSlider(e: Event) {
    setLeadIndex(Number((e.currentTarget as HTMLInputElement).value));
  }
  function onKey(e: KeyboardEvent) {
    if (e.key === 'Home') { stepLead('first'); e.preventDefault(); }
    else if (e.key === 'End') { stepLead('last'); e.preventDefault(); }
  }
</script>

{#if shown && tag}
  <div class="nc-root" aria-label="Nowcast: a forecast, not an observation">
    <!-- svelte-ignore a11y_no_static_element_interactions, a11y_click_events_have_key_events -->
    <div class="nc-label" onmousedown={stop} onclick={stop} ondblclick={stop} onwheel={stop} oncontextmenu={stop} onmousemove={stop}>
      <div class="nc-head">
        {#if res?.provenance}
          <ProvenanceChip provenance={res.provenance} title="Nowcast" />
        {:else}
          <span class="nc-chip" title="Forecast: a statement about a future time, not an observation">{tag.chip}</span>
        {/if}
        <b class="nc-lead">{tag.headline}</b>
      </div>
      <div class="nc-times mono"><span>{tag.origin}</span><span class="nc-sep" aria-hidden="true">|</span><span>{tag.valid}</span></div>
      <div class="nc-line">{tag.product} · {tag.sigma} · {tag.ensemble}</div>
      <div class="nc-guide">{tag.guidance}</div>
      <div class="nc-skill" class:open={skillOpen} title={tag.skill}>{tag.skill}</div>
      <button type="button" class="nc-more" onclick={() => (skillOpen = !skillOpen)} aria-expanded={skillOpen}>{skillOpen ? 'Less' : 'More'}</button>
    </div>

    <!-- svelte-ignore a11y_no_static_element_interactions, a11y_click_events_have_key_events -->
    <div class="nc-bar" onmousedown={stop} onclick={stop} ondblclick={stop} onwheel={stop} oncontextmenu={stop} onmousemove={stop}>
      <button type="button" class="nc-btn" onclick={() => stepLead(-1)} disabled={tag.leadIndex <= 0} aria-label="Earlier lead" title="Earlier lead (Left arrow on the slider)">&lsaquo;</button>
      <button type="button" class="nc-btn" onclick={togglePlaying} disabled={tag.leadCount < 2} aria-label={playing ? 'Pause the lead loop' : 'Play through the leads'} title={playing ? 'Pause' : 'Play through the leads'}>{playing ? '❚❚' : '▶'}</button>
      <button type="button" class="nc-btn" onclick={() => stepLead(1)} disabled={tag.leadIndex >= tag.leadCount - 1} aria-label="Later lead" title="Later lead (Right arrow on the slider)">&rsaquo;</button>
      <input
        class="nc-slider"
        type="range"
        min="0"
        max={Math.max(0, tag.leadCount - 1)}
        step="1"
        value={tag.leadIndex}
        oninput={onSlider}
        onkeydown={onKey}
        disabled={tag.leadCount < 2}
        aria-label="Lead time of the forecast"
        aria-valuetext="{tag.lead} after {tag.origin.replace('Origin ', '')}"
      />
      <span class="nc-readout mono"><b>{tag.lead}</b>{res ? ` of ${formatLead(res.leads[res.leads.length - 1])}` : ''}</span>
      <button type="button" class="nc-btn wide" onclick={hideNowcastFromMap} title="Take the forecast off the map">Observations</button>
    </div>
  </div>
{/if}

<style>
  .nc-root { position: absolute; inset: 0; pointer-events: none; z-index: 26; }
  .nc-label, .nc-bar { pointer-events: auto; background: rgba(var(--c-surface-1-rgb), 0.93); border: 1px solid var(--c-border-strong); color: var(--c-text-dim); }
  .nc-label {
    position: absolute; left: var(--space-2); top: var(--space-2);
    width: min(520px, calc(100% - 2 * var(--space-2))); box-sizing: border-box;
    padding: 5px 8px 4px; border-radius: var(--radius-md); font-size: var(--fs-xs); line-height: 1.35;
    border-left: 3px solid var(--c-alarm);
  }
  .nc-head { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 8px; color: var(--c-text); }
  .nc-chip {
    display: inline-block; padding: 0 6px; font-size: var(--fs-2xs); font-weight: 700; letter-spacing: 0.06em;
    color: var(--c-alarm); border: 1px double var(--c-alarm); border-radius: 9px;
  }
  .nc-lead { color: var(--c-alarm); letter-spacing: 0.04em; }
  .nc-sep { color: var(--c-muted); margin: 0 6px; }
  .nc-times { margin-top: 1px; color: var(--c-text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .nc-line { margin-top: 2px; }
  .nc-guide { margin-top: 2px; font-weight: 600; color: var(--c-text); }
  .nc-skill {
    margin-top: 2px; color: var(--c-muted); font-size: var(--fs-2xs);
    display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }
  .nc-skill.open { display: block; -webkit-line-clamp: unset; line-clamp: unset; overflow: visible; }
  .nc-more {
    height: auto; padding: 0; margin-top: 1px; font-size: var(--fs-2xs); font-weight: 600; color: var(--c-accent);
    background: transparent; border: none; box-shadow: none; cursor: pointer;
  }
  .nc-more:hover { background: transparent; text-decoration: underline; }
  .nc-bar {
    position: absolute; left: 50%; bottom: 52px; transform: translateX(-50%);
    display: flex; align-items: center; gap: var(--space-1);
    width: min(620px, calc(100% - 2 * var(--space-2))); box-sizing: border-box;
    padding: 4px 8px; border-radius: var(--radius-md); font-size: var(--fs-xs);
  }
  .nc-slider { flex: 1; min-width: 80px; accent-color: var(--c-alarm); }
  .nc-readout { color: var(--c-text-dim); white-space: nowrap; font-size: var(--fs-xs); }
  .nc-readout b { color: var(--c-alarm); }
  .nc-btn {
    height: 24px; min-width: 24px; padding: 0 6px; font-size: var(--fs-xs); color: var(--c-text-dim);
    background: transparent; border: 1px solid var(--c-border-strong); border-radius: var(--radius-sm); cursor: pointer; white-space: nowrap;
  }
  .nc-btn.wide { padding: 0 var(--space-2); }
  .nc-btn:hover:not(:disabled) { color: var(--c-text); border-color: var(--c-alarm); background: transparent; }
  .nc-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .nc-btn:focus-visible, .nc-slider:focus-visible, .nc-more:focus-visible { outline: 2px solid var(--c-accent); outline-offset: 1px; }
  .mono { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
</style>
