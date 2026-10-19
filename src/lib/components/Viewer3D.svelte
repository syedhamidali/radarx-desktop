<script lang="ts">
  /**
   * The 3D tab: a source selector over the polar radar volume of the open file, the merged
   * mosaic volume and the multi-Doppler wind. The polar view is untouched (RadarViewer3D); the
   * gridded sources are drawn by CartesianViewer3D.
   */
  import RadarViewer3D from './RadarViewer3D.svelte';
  import CartesianViewer3D from './CartesianViewer3D.svelte';
  import { mosaicResult } from '../stores/mosaic';
  import { windResult } from '../stores/wind';
  import {
    source3d, mosaicVolume, windVolume, volume3dLoading, fetchMosaicVolume, fetchWindVolume, ensureMosaicBehindWind, type Source3d,
  } from '../stores/volume3d';

  const src = $derived($source3d);
  const mosaicReady = $derived(!!$mosaicVolume || !!$mosaicResult?.has_volume);
  const windReady = $derived(!!$windVolume || !!$windResult);

  async function pick(s: Source3d) {
    if (s === 'mosaic' && !$mosaicVolume && $mosaicResult?.has_volume) await fetchMosaicVolume($mosaicResult.mosaic_id);
    if (s === 'wind' && !$windVolume && $windResult) await fetchWindVolume($windResult.wind_id);
    if (s === 'wind') await ensureMosaicBehindWind();
    source3d.set(s);
  }

  const mosaicTitle = $derived(
    mosaicReady
      ? 'The merged 3D field of the mosaic'
      : $mosaicResult && !$mosaicResult.has_volume
        ? ($mosaicResult.volume_note ?? 'This mosaic has no 3D volume')
        : 'Create a mosaic first (Mosaic panel)',
  );
</script>

<div class="host">
  <div class="source-bar" role="group" aria-label="3D source">
    <span class="lbl">Source</span>
    <button class="seg" class:active={src === 'radar'} onclick={() => pick('radar')} title="The polar volume of the open radar file">Open file (polar)</button>
    <button class="seg" class:active={src === 'mosaic'} disabled={!mosaicReady} onclick={() => pick('mosaic')} title={mosaicTitle}>Mosaic (merged volume)</button>
    <button class="seg" class:active={src === 'wind'} disabled={!windReady} onclick={() => pick('wind')}
            title={windReady ? '3D wind arrows of the multi-Doppler analysis' : 'Run a wind analysis first (Wind panel)'}>Wind (multi-Doppler)</button>
    {#if $volume3dLoading.mosaic || $volume3dLoading.wind}<span class="busy">loading…</span>{/if}
  </div>
  <div class="body">
    {#if src === 'radar'}
      <RadarViewer3D />
    {:else}
      <CartesianViewer3D />
    {/if}
  </div>
</div>

<style>
  .host { display: flex; flex-direction: column; height: 100%; min-height: 0; flex: 1; }
  .body { flex: 1; min-height: 0; display: flex; flex-direction: column; }
  .body > :global(.viewer-3d) { flex: 1; min-height: 0; }
  .source-bar {
    display: flex; align-items: center; gap: 4px; padding: 3px var(--spacing-md); flex-shrink: 0;
    background: var(--bg-tertiary); border-bottom: 1px solid var(--border-color);
  }
  .lbl { font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em; color: var(--text-muted); margin-right: 4px; }
  .seg {
    font-size: 11px; padding: 2px 10px; border: 1px solid var(--border-color); border-radius: var(--radius-sm);
    background: var(--bg-secondary); color: var(--text-secondary); cursor: pointer;
  }
  .seg:hover:not(:disabled) { border-color: var(--accent-primary); }
  .seg.active { background: var(--accent-primary); color: var(--c-on-accent); border-color: var(--accent-primary); }
  .seg:disabled { opacity: 0.45; cursor: not-allowed; }
  .busy { font-size: 10px; color: var(--text-muted); font-family: var(--font-mono); margin-left: 6px; }
</style>
