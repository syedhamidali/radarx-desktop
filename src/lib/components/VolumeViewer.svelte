<script lang="ts">
  /**
   * VolumeViewer — 3D volume rendering view component.
   *
   * Creates a container div and instantiates VolumeRenderer from the utils
   * module. Shows a loading spinner when data is being fetched, a placeholder
   * when no data, and a toolbar with controls.
   */
  import { onMount, onDestroy } from 'svelte';
  import { VolumeRenderer } from '../utils/volumeRenderer';
  import type { GovernorState, QualityMode } from '../utils/qualityGovernor';

  export let volumeData: Float32Array | null = null;
  export let nx: number = 0;
  export let ny: number = 0;
  export let nz: number = 0;
  export let bounds: { xMin: number; xMax: number; yMin: number; yMax: number; zMin: number; zMax: number } = {
    xMin: 0, xMax: 1, yMin: 0, yMax: 1, zMin: 0, zMax: 1,
  };
  export let vmin: number = 0;
  export let vmax: number = 60;
  export let variable: string = '';

  let container: HTMLDivElement;
  let renderer: VolumeRenderer | null = null;
  let alphaScale: number = 1.0;
  let isLoading: boolean = false;
  let qState: GovernorState | null = null;
  let unsubQuality: (() => void) | null = null;
  /** Set when the renderer stopped for good (draw error / unrecoverable context loss). */
  let fatalMsg = '';

  const QUALITY_OPTIONS: { value: QualityMode; label: string }[] = [
    { value: 'auto', label: 'Auto' },
    { value: 'high', label: 'High' },
    { value: 'medium', label: 'Medium' },
    { value: 'low', label: 'Low' },
  ];

  function onQualityChange(e: Event) {
    if (!renderer) return;
    renderer.governor.setMode((e.target as HTMLSelectElement).value as QualityMode);
    renderer.refreshQuality();
  }

  $: hasData = volumeData !== null && volumeData.length > 0;
  $: gridLabel = hasData ? `${nx}x${ny}x${nz}` : '';

  // Update renderer when data changes
  $: if (renderer && volumeData && nx > 0 && ny > 0 && nz > 0) {
    renderer.setValueRange(vmin, vmax);
    renderer.setVolumeData(volumeData, nx, ny, nz, bounds);
  }

  // Update alpha scale on slider change
  $: if (renderer) {
    renderer.setAlphaScale(alphaScale);
  }

  onMount(() => {
    if (container) {
      try {
        renderer = new VolumeRenderer(container);
        renderer.onFatal = (m) => { fatalMsg = m; };
        unsubQuality = renderer.governor.state.subscribe((s) => { qState = s; });
      } catch (e) {
        fatalMsg = e instanceof Error ? e.message : String(e);
      }
    }
  });

  onDestroy(() => {
    unsubQuality?.();
    unsubQuality = null;
    if (renderer) {
      renderer.dispose();
      renderer = null;
    }
  });

  function resetCamera() {
    if (renderer) {
      renderer.resetCamera();
    }
  }

  function formatAlpha(val: number): string {
    return val.toFixed(1) + 'x';
  }

  /** Expose loading state for parent */
  export function setLoading(v: boolean) {
    isLoading = v;
  }
</script>

<div class="volume-viewer-root">
  <!-- Toolbar -->
  <div class="vv-toolbar">
    <div class="vv-toolbar-left">
      {#if variable}
        <span class="vv-badge">{variable}</span>
      {/if}
      {#if gridLabel}
        <span class="vv-dims">{gridLabel}</span>
      {/if}
    </div>
    <div class="vv-toolbar-right">
      <select id="vv-quality-select" class="vv-select" aria-label="Render quality" value={qState?.mode ?? 'auto'} on:change={onQualityChange}
              title="Auto adapts to your GPU to keep the app responsive.">
        {#each QUALITY_OPTIONS as o (o.value)}
          <option value={o.value}>{o.label}</option>
        {/each}
      </select>
      {#if qState}
        <span class="vv-dims" title={`GPU: ${qState.gpu || 'unknown'}\nFrame: ${qState.frameMsEma.toFixed(0)} ms`}>{qState.idleLevel}</span>
      {/if}
      <label class="vv-alpha-label" for="vv-alpha-slider">
        Opacity
        <span class="vv-alpha-value">{formatAlpha(alphaScale)}</span>
      </label>
      <input
        id="vv-alpha-slider"
        type="range"
        class="vv-slider"
        min="0"
        max="2"
        step="0.05"
        bind:value={alphaScale}
      />
      <button class="vv-btn" on:click={resetCamera} disabled={!hasData} title="Reset camera">
        Reset
      </button>
    </div>
  </div>

  <!-- Viewport -->
  <div class="vv-viewport" bind:this={container}>
    {#if fatalMsg}
      <div class="vv-overlay">
        <span class="vv-overlay-text">{fatalMsg}</span>
        <span class="vv-overlay-text placeholder">Use the 2D view to keep working.</span>
      </div>
    {:else if isLoading}
      <div class="vv-overlay">
        <div class="vv-spinner"></div>
        <span class="vv-overlay-text">Loading volume data...</span>
      </div>
    {:else if !hasData}
      <div class="vv-overlay">
        <span class="vv-overlay-text placeholder">
          Select a region on the PPI to render a 3D volume
        </span>
      </div>
    {/if}
  </div>

  {#if qState?.hint}
    <div class="vv-quality-hint">
      <span>Reduced 3D quality to keep the app responsive</span>
      <button class="vv-btn" on:click={() => renderer?.governor.keepMySetting()}>Keep my setting</button>
      <button class="vv-btn" aria-label="Dismiss" on:click={() => renderer?.governor.dismissHint()}>x</button>
    </div>
  {/if}

  <!-- Hint bar -->
  <div class="vv-hint">
    Drag to orbit &middot; Scroll to zoom &middot; Shift+drag to pan
  </div>
</div>

<style>
  .volume-viewer-root {
    display: flex;
    flex-direction: column;
    height: 100%;
    background: var(--bg-primary, #0f0f1a);
    border: 1px solid var(--border-color, rgba(255,255,255,0.1));
    border-radius: var(--radius-sm, 4px);
    overflow: hidden;
  }

  /* Toolbar */
  .vv-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-xs, 4px) var(--spacing-sm, 6px);
    background: rgba(17, 22, 40, 0.6);
    border-bottom: 1px solid var(--border-color, rgba(255,255,255,0.1));
    flex-shrink: 0;
    flex-wrap: wrap;
    gap: var(--spacing-sm, 6px);
    min-height: 32px;
  }

  .vv-toolbar-left {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm, 6px);
  }

  .vv-toolbar-right {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm, 6px);
  }

  .vv-badge {
    font-size: 10px;
    font-weight: 700;
    font-family: var(--font-mono, monospace);
    text-transform: uppercase;
    color: var(--accent-hover, #7b8aff);
    background: rgba(91, 108, 247, 0.12);
    padding: 2px 8px;
    border-radius: 10px;
    border: 1px solid rgba(91, 108, 247, 0.15);
    letter-spacing: 0.04em;
  }

  .vv-dims {
    font-size: 10px;
    font-family: var(--font-mono, monospace);
    color: var(--text-muted, rgba(255,255,255,0.4));
  }

  .vv-alpha-label {
    font-size: 10px;
    font-weight: 600;
    color: var(--text-muted, rgba(255,255,255,0.4));
    text-transform: uppercase;
    letter-spacing: 0.03em;
    display: flex;
    align-items: center;
    gap: 4px;
    white-space: nowrap;
  }

  .vv-alpha-value {
    font-family: var(--font-mono, monospace);
    color: var(--text-secondary, rgba(255,255,255,0.6));
    min-width: 28px;
    text-align: right;
  }

  .vv-slider {
    width: 60px;
    height: 4px;
    -webkit-appearance: none;
    appearance: none;
    background: var(--border-color, rgba(255,255,255,0.1));
    border-radius: 2px;
    outline: none;
    cursor: pointer;
  }

  .vv-slider::-webkit-slider-thumb {
    -webkit-appearance: none;
    appearance: none;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--accent-primary, #5b6cf7);
    border: none;
    cursor: pointer;
  }

  .vv-btn {
    padding: 3px 10px;
    font-size: 10px;
    font-weight: 600;
    border-radius: var(--radius-sm, 4px);
    border: 1px solid var(--border-color, rgba(255,255,255,0.1));
    background: rgba(15, 15, 26, 0.6);
    color: var(--text-secondary, rgba(255,255,255,0.7));
    cursor: pointer;
    transition: all 150ms ease;
    white-space: nowrap;
  }

  .vv-btn:hover:not(:disabled) {
    background: rgba(91, 108, 247, 0.1);
    border-color: rgba(91, 108, 247, 0.2);
    color: var(--text-primary, #fff);
  }

  .vv-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  /* Viewport */
  .vv-viewport {
    flex: 1;
    position: relative;
    min-height: 200px;
    overflow: hidden;
  }

  .vv-overlay {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--spacing-sm, 6px);
    background: rgba(15, 15, 26, 0.85);
    z-index: 5;
  }

  .vv-overlay-text {
    font-size: 12px;
    color: var(--text-secondary, rgba(255,255,255,0.6));
  }

  .vv-overlay-text.placeholder {
    color: var(--text-muted, rgba(255,255,255,0.35));
    font-size: 11px;
    text-align: center;
    padding: 0 var(--spacing-md, 12px);
  }

  .vv-spinner {
    width: 20px;
    height: 20px;
    border: 2px solid rgba(91, 108, 247, 0.25);
    border-top-color: var(--accent-primary, #5b6cf7);
    border-radius: 50%;
    animation: vv-spin 600ms linear infinite;
  }

  @keyframes vv-spin {
    to { transform: rotate(360deg); }
  }

  .vv-select {
    font-size: 10px;
    padding: 1px 4px;
    border: 1px solid var(--border-color, rgba(255,255,255,0.1));
    border-radius: var(--radius-sm, 4px);
    background: rgba(15, 15, 26, 0.6);
    color: var(--text-secondary, rgba(255,255,255,0.7));
  }

  .vv-quality-hint {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    padding: 3px var(--spacing-sm, 6px);
    font-size: 10px;
    color: var(--text-secondary, rgba(255,255,255,0.7));
    background: rgba(17, 22, 40, 0.8);
    border-top: 1px solid var(--border-color, rgba(255,255,255,0.1));
    flex-shrink: 0;
  }

  /* Hint bar */
  .vv-hint {
    padding: 3px var(--spacing-sm, 6px);
    font-size: 10px;
    color: var(--text-muted, rgba(255,255,255,0.3));
    text-align: center;
    border-top: 1px solid var(--border-color, rgba(255,255,255,0.06));
    background: rgba(17, 22, 40, 0.4);
    flex-shrink: 0;
  }
</style>
