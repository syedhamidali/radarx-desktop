<script lang="ts">
  import {
    annotationsStore,
    setMeasureMode,
    setAnnotationMode,
    finishAreaMeasurement,
    clearAll,
    type MeasureToolMode,
    type AnnotationToolMode,
  } from '../stores/annotations';

  export let visible: boolean;

  $: measureMode = $annotationsStore.measureMode;
  $: annotationMode = $annotationsStore.annotationMode;
  $: activeResult = $annotationsStore.activeResult;
  $: pendingCount = $annotationsStore.pendingPoints.length;

  function toggleMeasure(mode: MeasureToolMode) {
    if (measureMode === mode) {
      setMeasureMode('none');
    } else {
      setMeasureMode(mode);
    }
  }

  function toggleAnnotation(mode: AnnotationToolMode) {
    if (annotationMode === mode) {
      setAnnotationMode('none');
    } else {
      setAnnotationMode(mode);
    }
  }

  $: isAnyActive = measureMode !== 'none' || annotationMode !== 'none';
</script>

{#if visible}
  <!-- The toolbar sits inside the viewer canvas: keep its clicks from also adding measurement
       points, starting a pan or resetting the zoom on double-click. -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="measure-toolbar" class:has-result={!!activeResult}
       on:click|stopPropagation on:dblclick|stopPropagation
       on:mousedown|stopPropagation on:contextmenu|stopPropagation>
    <!-- Measurement tools -->
    <div class="toolbar-section">
      <span class="section-label">Measure</span>
      <div class="btn-group">
        <button class="tool-btn" class:active={measureMode === 'distance'}
                on:click={() => toggleMeasure('distance')} title="Distance (click two points)">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M2 18L18 2"/>
            <path d="M15 2h3v3"/>
            <path d="M5 18H2v-3"/>
          </svg>
        </button>
        <button class="tool-btn" class:active={measureMode === 'area'}
                on:click={() => toggleMeasure('area')} title="Area (click polygon vertices, double-click to close)">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M3 8l4-4 10 2 4 6-6 8-10 0z"/>
          </svg>
        </button>
        <button class="tool-btn" class:active={measureMode === 'bearing'}
                on:click={() => toggleMeasure('bearing')} title="Bearing (click two points)">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="9"/>
            <path d="M12 3v4"/>
            <path d="M12 12l4-4"/>
          </svg>
        </button>
      </div>
    </div>

    <div class="toolbar-divider"></div>

    <!-- Annotation tools -->
    <div class="toolbar-section">
      <span class="section-label">Annotate</span>
      <div class="btn-group">
        <button class="tool-btn" class:active={annotationMode === 'text'}
                on:click={() => toggleAnnotation('text')} title="Text (click to place)">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="4 7 4 4 20 4 20 7"/>
            <line x1="9" y1="20" x2="15" y2="20"/>
            <line x1="12" y1="4" x2="12" y2="20"/>
          </svg>
        </button>
        <button class="tool-btn" class:active={annotationMode === 'arrow'}
                on:click={() => toggleAnnotation('arrow')} title="Arrow (click start and end)">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="5" y1="19" x2="19" y2="5"/>
            <polyline points="14 5 19 5 19 10"/>
          </svg>
        </button>
        <button class="tool-btn" class:active={annotationMode === 'circle'}
                on:click={() => toggleAnnotation('circle')} title="Circle (click center, then edge)">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="9"/>
          </svg>
        </button>
        <button class="tool-btn" class:active={annotationMode === 'rectangle'}
                on:click={() => toggleAnnotation('rectangle')} title="Rectangle (click two corners)">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <rect x="3" y="5" width="18" height="14" rx="1"/>
          </svg>
        </button>
      </div>
    </div>

    <div class="toolbar-divider"></div>

    <!-- Actions -->
    <div class="toolbar-section">
      {#if measureMode === 'area' && pendingCount >= 3}
        <button class="tool-btn action" on:click={finishAreaMeasurement} title="Close polygon">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="20 6 9 17 4 12"/>
          </svg>
        </button>
      {/if}
      <button class="tool-btn action" on:click={clearAll} title="Clear all measurements and annotations">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="3 6 5 6 21 6"/>
          <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
          <path d="M10 11v6"/><path d="M14 11v6"/>
        </svg>
      </button>
    </div>

    <!-- Active result display -->
    {#if activeResult}
      <div class="toolbar-result">
        <span class="result-value">{activeResult}</span>
      </div>
    {/if}

    <!-- Active mode hint -->
    {#if isAnyActive}
      <div class="toolbar-hint">
        {#if measureMode === 'distance'}Click two points to measure distance
        {:else if measureMode === 'area'}Click polygon vertices. Double-click to close.
        {:else if measureMode === 'bearing'}Click two points for bearing
        {:else if annotationMode === 'text'}Click to place text
        {:else if annotationMode === 'arrow'}Click start, then end point
        {:else if annotationMode === 'circle'}Click center, then edge
        {:else if annotationMode === 'rectangle'}Click two opposite corners
        {/if}
      </div>
    {/if}
  </div>
{/if}

<style>
  .measure-toolbar {
    position: absolute;
    top: var(--spacing-md);
    left: 50%;
    transform: translateX(-50%);
    z-index: 30;
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 5px 8px;
    background: var(--glass-bg-heavy, rgba(var(--c-surface-1-rgb), 0.9));
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    backdrop-filter: blur(16px);
    box-shadow: var(--shadow-md);
    animation: fade-in 150ms ease;
    white-space: nowrap;
  }

  .toolbar-section {
    display: flex;
    align-items: center;
    gap: 4px;
  }

  .section-label {
    font-size: 9px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-muted);
    margin-right: 2px;
  }

  .btn-group {
    display: flex;
    gap: 2px;
  }

  .tool-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 28px;
    padding: 0;
    background: transparent;
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    color: var(--text-secondary);
    cursor: pointer;
    transition: all 120ms ease;
  }

  .tool-btn:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
    border-color: var(--glass-border);
  }

  .tool-btn.active {
    background: rgba(var(--c-accent-rgb), 0.18);
    border-color: var(--accent-primary);
    color: var(--accent-primary);
    box-shadow: 0 0 8px rgba(var(--c-accent-rgb), 0.15);
  }

  .tool-btn.action {
    color: var(--text-muted);
  }

  .tool-btn.action:hover {
    color: var(--accent-danger);
  }

  .toolbar-divider {
    width: 1px;
    height: 20px;
    background: var(--glass-border);
    margin: 0 2px;
  }

  .toolbar-result {
    margin-left: 4px;
    padding: 2px 8px;
    background: rgba(var(--c-accent-rgb), 0.1);
    border: 1px solid rgba(var(--c-accent-rgb), 0.2);
    border-radius: var(--radius-sm);
  }

  .result-value {
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 600;
    color: var(--accent-primary);
  }

  /* Below the bar rather than inline: inline it made the bar ~220 px wider and covered the
     range/azimuth readout at the viewer's top-left on narrow windows. */
  .toolbar-hint {
    position: absolute;
    top: calc(100% + 4px);
    left: 50%;
    transform: translateX(-50%);
    padding: 2px 8px;
    font-size: 10px;
    color: var(--text-secondary);
    background: var(--glass-bg-heavy, rgba(var(--c-surface-1-rgb), 0.9));
    border-radius: var(--radius-sm);
    font-style: italic;
    pointer-events: none;
  }
</style>
