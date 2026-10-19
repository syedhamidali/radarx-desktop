<script lang="ts">
  import { COLORMAP_DATA, COLORMAP_CATEGORIES } from '../utils/colormaps';
  import {
    type ColorStop,
    type CustomColormap,
    interpolateColormap,
    saveCustomColormap,
    deleteCustomColormap,
    exportColormapJSON,
    isBuiltinColormapName,
    importColormapJSON,
    customColormaps,
  } from '../stores/customColormaps';

  let { onApply = null, onClose = null }: {
    onApply?: ((cmapName: string) => void) | null;
    onClose?: (() => void) | null;
  } = $props();

  // ─── Editor state ──────────────────────────────────────────────────
  let name = $state('Custom');
  let stops = $state<ColorStop[]>([
    { position: 0, color: [0, 0, 0] },
    { position: 0.5, color: [255, 128, 0] },
    { position: 1, color: [255, 255, 255] },
  ]);
  let isReversed = $state(false);
  let isDiscrete = $state(false);
  let discreteSteps = $state(16);
  let selectedStopIdx = $state(-1);
  let draggingIdx = $state(-1);

  let saveError = $state('');
  let exportNote = $state('');

  // Import/export
  let showImport = $state(false);
  let importText = $state('');
  let importError = $state('');

  // Tabs
  let activeTab = $state<'editor' | 'presets' | 'custom'>('editor');

  // ─── Derived ───────────────────────────────────────────────────────
  const previewRgb = $derived(interpolateColormap(stops, 256, isReversed, isDiscrete, discreteSteps));
  const previewGradient = $derived(buildPreviewGradient(previewRgb));
  const sortedStops = $derived([...stops].sort((a, b) => a.position - b.position));

  function buildPreviewGradient(rgb: Uint8Array): string {
    const parts: string[] = [];
    const n = 32;
    for (let i = 0; i < n; i++) {
      const idx = Math.round((i / (n - 1)) * 255);
      const r = rgb[idx * 3], g = rgb[idx * 3 + 1], b = rgb[idx * 3 + 2];
      parts.push(`rgb(${r},${g},${b}) ${(i / (n - 1) * 100).toFixed(1)}%`);
    }
    return `linear-gradient(to right, ${parts.join(', ')})`;
  }

  function presetGradient(name: string): string {
    const rgb = COLORMAP_DATA[name];
    if (!rgb) return 'var(--c-surface-3)';
    const parts: string[] = [];
    for (let i = 0; i < 10; i++) {
      const idx = Math.round((i / 9) * 255);
      parts.push(`rgb(${rgb[idx * 3]},${rgb[idx * 3 + 1]},${rgb[idx * 3 + 2]})`);
    }
    return `linear-gradient(to right, ${parts.join(', ')})`;
  }

  // ─── Stop manipulation ─────────────────────────────────────────────
  function addStop(e: MouseEvent) {
    const bar = e.currentTarget as HTMLElement;
    const rect = bar.getBoundingClientRect();
    const pos = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));

    // Interpolate color at position
    const rgb = interpolateColormap(stops, 256, false, false, 16);
    const idx = Math.round(pos * 255);
    const color: [number, number, number] = [rgb[idx * 3], rgb[idx * 3 + 1], rgb[idx * 3 + 2]];

    stops = [...stops, { position: pos, color }];
    selectedStopIdx = stops.length - 1;
  }

  function removeStop(idx: number) {
    if (stops.length <= 2) return;
    stops = stops.filter((_, i) => i !== idx);
    if (selectedStopIdx >= stops.length) selectedStopIdx = stops.length - 1;
  }

  function handleStopMouseDown(idx: number, e: MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    selectedStopIdx = idx;
    draggingIdx = idx;

    const bar = (e.currentTarget as HTMLElement).parentElement!;
    const rect = bar.getBoundingClientRect();

    function onMove(me: MouseEvent) {
      const pos = Math.max(0, Math.min(1, (me.clientX - rect.left) / rect.width));
      stops = stops.map((s, i) => (i === draggingIdx ? { ...s, position: pos } : s));
    }
    function onUp() {
      draggingIdx = -1;
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    }
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }

  function stopColorHex(stop: ColorStop): string {
    const [r, g, b] = stop.color;
    return '#' + [r, g, b].map((c) => c.toString(16).padStart(2, '0')).join('');
  }

  function onColorInput(idx: number, e: Event) {
    const hex = (e.target as HTMLInputElement).value;
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    stops = stops.map((s, i) =>
      i === idx ? { ...s, color: [r, g, b] as [number, number, number] } : s,
    );
  }

  function onPositionInput(idx: number, e: Event) {
    const val = parseFloat((e.target as HTMLInputElement).value);
    if (!isNaN(val)) {
      const clamped = Math.max(0, Math.min(1, val));
      stops = stops.map((s, i) => (i === idx ? { ...s, position: clamped } : s));
    }
  }

  // ─── Reverse ───────────────────────────────────────────────────────
  // Flip the stop positions only: the preview/saved LUT already honours isReversed, so also toggling
  // it would reverse twice and the button would appear to do nothing.
  function reverseStops() {
    stops = stops.map((s) => ({ ...s, position: 1 - s.position }));
  }

  function stopKey(idx: number, e: KeyboardEvent) {
    const step = e.shiftKey ? 0.1 : 0.01;
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault();
      const d = e.key === 'ArrowLeft' ? -step : step;
      stops = stops.map((s, i) => (i === idx ? { ...s, position: Math.max(0, Math.min(1, s.position + d)) } : s));
      selectedStopIdx = idx;
    } else if (e.key === 'Delete' || e.key === 'Backspace') {
      e.preventDefault();
      removeStop(idx);
    }
  }

  // ─── Save / Apply / Export / Import ────────────────────────────────
  function handleSave(): boolean {
    saveError = '';
    const trimmed = name.trim();
    if (!trimmed) {
      saveError = 'Give the colormap a name.';
      return false;
    }
    if (isBuiltinColormapName(trimmed)) {
      saveError = `"${trimmed}" is a built-in colormap. Pick another name.`;
      return false;
    }
    const cm: CustomColormap = {
      name: trimmed,
      stops: [...stops],
      isReversed,
      isDiscrete,
      discreteSteps: Math.max(2, Math.min(256, Math.round(Number(discreteSteps)) || 16)),
    };
    return saveCustomColormap(cm);
  }

  function handleApply() {
    if (handleSave() && onApply) onApply(name.trim());
  }

  function handleExport() {
    const cm: CustomColormap = {
      name: name.trim(),
      stops: [...stops],
      isReversed,
      isDiscrete,
      discreteSteps,
    };
    const json = exportColormapJSON(cm);
    exportNote = '';
    navigator.clipboard
      .writeText(json)
      .then(() => (exportNote = 'JSON copied to clipboard'))
      .catch(() => {
        // Clipboard blocked: fall back to a file download.
        const url = URL.createObjectURL(new Blob([json], { type: 'application/json' }));
        const a = document.createElement('a');
        a.href = url;
        a.download = `${cm.name || 'colormap'}.json`;
        a.click();
        URL.revokeObjectURL(url);
        exportNote = 'Saved as a .json file';
      });
  }

  function handleImport() {
    importError = '';
    const cm = importColormapJSON(importText);
    if (!cm) {
      importError = 'Invalid colormap JSON. Need {name, stops[{position, color}], ...}';
      return;
    }
    name = cm.name;
    saveError = '';
    stops = cm.stops;
    isReversed = cm.isReversed;
    isDiscrete = cm.isDiscrete;
    discreteSteps = cm.discreteSteps;
    showImport = false;
    importText = '';
  }

  function loadCustom(cm: CustomColormap) {
    name = cm.name;
    saveError = '';
    stops = [...cm.stops];
    isReversed = cm.isReversed;
    isDiscrete = cm.isDiscrete;
    discreteSteps = cm.discreteSteps;
    activeTab = 'editor';
  }

  function deleteCustom(cmName: string) {
    deleteCustomColormap(cmName);
  }

  function applyPreset(presetName: string) {
    if (onApply) onApply(presetName);
  }
</script>

<svelte:window on:keydown={(e) => { if (e.key === 'Escape' && onClose) { e.stopPropagation(); onClose(); } }} />

<div class="cmap-editor-overlay">
  <div class="cmap-editor" role="dialog" aria-modal="true" aria-label="Colormap editor">
    <!-- Header -->
    <div class="editor-header">
      <h3>Colormap Editor</h3>
      <button class="close-btn" on:click={() => onClose && onClose()} title="Close">&times;</button>
    </div>

    <!-- Tabs -->
    <div class="tabs">
      <button class="tab" class:active={activeTab === 'editor'} on:click={() => (activeTab = 'editor')}>
        Editor
      </button>
      <button class="tab" class:active={activeTab === 'presets'} on:click={() => (activeTab = 'presets')}>
        Presets
      </button>
      <button class="tab" class:active={activeTab === 'custom'} on:click={() => (activeTab = 'custom')}>
        My Colormaps
      </button>
    </div>

    <!-- Editor tab -->
    {#if activeTab === 'editor'}
      <div class="editor-body">
        <!-- Name input -->
        <div class="field-row">
          <label class="field-label">Name</label>
          <input class="field-input" type="text" bind:value={name} maxlength="48" placeholder="Colormap name" />
        </div>

        <!-- Preview gradient -->
        <div class="preview-section">
          <label class="field-label">Preview</label>
          <div class="preview-bar" style="background: {previewGradient}"></div>
        </div>

        <!-- Gradient editor bar with stops -->
        <div class="field-label">Color Stops (click bar to add)</div>
        <!-- svelte-ignore a11y_click_events_have_key_events -->
        <!-- svelte-ignore a11y_no_static_element_interactions -->
        <div class="gradient-bar-wrapper" on:click={addStop}>
          <div class="gradient-bar" style="background: {previewGradient}"></div>
          {#each stops as stop, idx}
            <!-- svelte-ignore a11y_no_static_element_interactions -->
            <div
              class="stop-handle"
              class:selected={idx === selectedStopIdx}
              style="left: {stop.position * 100}%; background: {stopColorHex(stop)}"
              role="slider"
              tabindex="0"
              aria-label="Color stop {idx + 1}"
              aria-valuemin="0"
              aria-valuemax="1"
              aria-valuenow={stop.position}
              on:mousedown={(e) => handleStopMouseDown(idx, e)}
              on:click|stopPropagation={() => (selectedStopIdx = idx)}
              on:focus={() => (selectedStopIdx = idx)}
              on:keydown={(e) => stopKey(idx, e)}
              title="Drag to move, click to select, arrows to nudge, Delete to remove"
            ></div>
          {/each}
        </div>

        <!-- Stop details -->
        {#if selectedStopIdx >= 0 && selectedStopIdx < stops.length}
          <div class="stop-details">
            <div class="field-row">
              <label class="field-label">Color</label>
              <input
                type="color"
                class="color-picker"
                value={stopColorHex(stops[selectedStopIdx])}
                on:input={(e) => onColorInput(selectedStopIdx, e)}
              />
            </div>
            <div class="field-row">
              <label class="field-label">Position</label>
              <input
                class="field-input narrow"
                type="number"
                min="0"
                max="1"
                step="0.01"
                value={stops[selectedStopIdx].position.toFixed(3)}
                on:change={(e) => onPositionInput(selectedStopIdx, e)}
              />
            </div>
            <button
              class="btn-sm danger"
              on:click={() => removeStop(selectedStopIdx)}
              disabled={stops.length <= 2}
              title="Remove stop"
            >
              Remove
            </button>
          </div>
        {/if}

        <!-- Toggles -->
        <div class="toggle-row">
          <button class="btn-sm" on:click={reverseStops} title="Reverse colormap">
            Reverse
          </button>
          <label class="toggle-label">
            <input type="checkbox" bind:checked={isDiscrete} />
            Discrete
          </label>
          {#if isDiscrete}
            <label class="toggle-label">
              Steps:
              <input
                class="field-input narrow"
                type="number"
                min="2"
                max="64"
                bind:value={discreteSteps}
              />
            </label>
          {/if}
        </div>

        <!-- Action buttons -->
        <div class="action-row">
          <button class="btn primary" on:click={handleApply}>Apply</button>
          <button class="btn" on:click={handleSave}>Save</button>
          <button class="btn" on:click={handleExport} title="Copy JSON to clipboard">Export</button>
          <button class="btn" on:click={() => (showImport = !showImport)}>Import</button>
        </div>

        {#if saveError}
          <div class="import-error" role="alert">{saveError}</div>
        {/if}
        {#if exportNote}
          <div class="export-note" role="status">{exportNote}</div>
        {/if}

        <!-- Import panel -->
        {#if showImport}
          <div class="import-panel">
            <textarea class="import-textarea" bind:value={importText} placeholder="Paste colormap JSON here..."></textarea>
            {#if importError}
              <div class="import-error">{importError}</div>
            {/if}
            <button class="btn-sm" on:click={handleImport}>Load</button>
          </div>
        {/if}
      </div>

    <!-- Presets tab -->
    {:else if activeTab === 'presets'}
      <div class="presets-body">
        {#each Object.entries(COLORMAP_CATEGORIES) as [category, names]}
          <div class="preset-category">{category}</div>
          {#each names as presetName}
            <button class="preset-row" on:click={() => applyPreset(presetName)}>
              <div class="preset-gradient" style="background: {presetGradient(presetName)}"></div>
              <span class="preset-name">{presetName}</span>
            </button>
          {/each}
        {/each}
      </div>

    <!-- Custom colormaps tab -->
    {:else if activeTab === 'custom'}
      <div class="custom-body">
        {#if $customColormaps.length === 0}
          <div class="empty-msg">No custom colormaps yet. Create one in the Editor tab.</div>
        {:else}
          {#each $customColormaps as cm}
            <div class="custom-row">
              <button class="preset-row" on:click={() => loadCustom(cm)}>
                <div class="preset-gradient" style="background: {presetGradient(cm.name)}"></div>
                <span class="preset-name">{cm.name}</span>
              </button>
              <div class="custom-actions">
                <button class="btn-sm" on:click={() => applyPreset(cm.name)}>Apply</button>
                <button class="btn-sm danger" on:click={() => deleteCustom(cm.name)}>Delete</button>
              </div>
            </div>
          {/each}
        {/if}
      </div>
    {/if}
  </div>
</div>

<style>
  .export-note {
    font-size: 11px;
    color: var(--accent-success);
  }

  .cmap-editor-overlay {
    position: fixed;
    inset: 0;
    z-index: 1000;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(var(--c-shadow-rgb), 0.6);
    backdrop-filter: blur(4px);
  }

  .cmap-editor {
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md, 8px);
    box-shadow: var(--shadow-lg);
    width: 420px;
    max-height: 80vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .editor-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 16px;
    border-bottom: 1px solid var(--border-color);
  }

  .editor-header h3 {
    margin: 0;
    font-size: 14px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .close-btn {
    background: none;
    border: none;
    color: var(--text-muted);
    font-size: 20px;
    cursor: pointer;
    padding: 0 4px;
    line-height: 1;
  }
  .close-btn:hover {
    color: var(--text-primary);
  }

  /* Tabs */
  .tabs {
    display: flex;
    border-bottom: 1px solid var(--border-color);
  }

  .tab {
    flex: 1;
    padding: 8px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted);
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    cursor: pointer;
    transition: color 0.15s, border-color 0.15s;
  }
  .tab:hover {
    color: var(--text-secondary);
  }
  .tab.active {
    color: var(--accent-primary);
    border-bottom-color: var(--accent-primary);
  }

  /* Editor body */
  .editor-body {
    padding: 12px 16px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .field-row {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .field-label {
    font-size: 11px;
    font-weight: 600;
    color: var(--text-muted);
    min-width: 50px;
  }

  .field-input {
    flex: 1;
    font-family: var(--font-mono, monospace);
    font-size: 12px;
    color: var(--text-primary);
    background: var(--bg-surface);
    border: 1px solid var(--border-color);
    border-radius: 3px;
    padding: 4px 8px;
    outline: none;
  }
  .field-input:focus {
    border-color: var(--accent-primary);
  }
  .field-input.narrow {
    width: 70px;
    flex: none;
  }

  /* Preview */
  .preview-section {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .preview-bar {
    height: 20px;
    border-radius: 4px;
    border: 1px solid rgba(var(--c-line-rgb), 0.1);
  }

  /* Gradient bar with handles */
  .gradient-bar-wrapper {
    position: relative;
    height: 28px;
    margin-top: 4px;
    cursor: crosshair;
  }

  .gradient-bar {
    position: absolute;
    inset: 4px 0;
    border-radius: 3px;
    border: 1px solid rgba(var(--c-line-rgb), 0.1);
  }

  .stop-handle {
    position: absolute;
    top: 0;
    width: 12px;
    height: 28px;
    transform: translateX(-50%);
    border-radius: 3px;
    border: 2px solid rgba(var(--c-ink-rgb), 0.6);
    cursor: grab;
    z-index: 2;
    transition: border-color 0.1s;
  }
  .stop-handle:active {
    cursor: grabbing;
  }
  .stop-handle.selected {
    border-color: var(--accent-primary);
    box-shadow: 0 0 6px rgba(var(--c-accent-rgb), 0.4);
  }

  /* Stop details */
  .stop-details {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 0;
  }

  .color-picker {
    width: 32px;
    height: 24px;
    border: 1px solid var(--border-color);
    border-radius: 3px;
    cursor: pointer;
    background: none;
    padding: 0;
  }

  /* Toggles */
  .toggle-row {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }

  .toggle-label {
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: 11px;
    color: var(--text-secondary);
    cursor: pointer;
  }

  /* Buttons */
  .btn {
    font-size: 11px;
    font-weight: 600;
    padding: 6px 14px;
    border-radius: 4px;
    border: 1px solid var(--border-color);
    background: var(--bg-surface);
    color: var(--text-secondary);
    cursor: pointer;
    transition: background 0.15s, color 0.15s, border-color 0.15s;
  }
  .btn:hover {
    background: var(--bg-hover);
    color: var(--text-primary);
  }
  .btn.primary {
    background: var(--accent-primary);
    color: var(--c-on-accent);
    border-color: var(--accent-primary);
  }
  .btn.primary:hover {
    filter: brightness(1.1);
  }

  .btn-sm {
    font-size: 10px;
    padding: 3px 8px;
    border-radius: 3px;
    border: 1px solid var(--border-color);
    background: var(--bg-surface);
    color: var(--text-secondary);
    cursor: pointer;
    transition: background 0.15s;
  }
  .btn-sm:hover {
    background: var(--bg-hover);
  }
  .btn-sm.danger {
    color: var(--c-alarm);
    border-color: rgba(var(--c-alarm-rgb), 0.45);
  }
  .btn-sm.danger:hover {
    background: rgba(var(--c-alarm-rgb), 0.14);
  }
  .btn-sm:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .action-row {
    display: flex;
    gap: 8px;
    padding-top: 4px;
  }

  /* Import panel */
  .import-panel {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .import-textarea {
    font-family: var(--font-mono, monospace);
    font-size: 11px;
    color: var(--text-primary);
    background: var(--bg-surface);
    border: 1px solid var(--border-color);
    border-radius: 3px;
    padding: 6px;
    min-height: 60px;
    resize: vertical;
  }

  .import-error {
    font-size: 10px;
    color: var(--c-alarm);
  }

  /* Presets body */
  .presets-body,
  .custom-body {
    padding: 8px;
    overflow-y: auto;
    max-height: 400px;
  }

  .preset-category {
    font-size: 10px;
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 8px 8px 2px;
  }

  .preset-row {
    display: flex;
    align-items: center;
    gap: 10px;
    width: 100%;
    padding: 4px 8px;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 4px;
    cursor: pointer;
    transition: background 0.15s;
  }
  .preset-row:hover {
    background: var(--bg-hover);
  }

  .preset-gradient {
    width: 100px;
    height: 12px;
    border-radius: 2px;
    flex-shrink: 0;
    border: 1px solid rgba(var(--c-line-rgb), 0.06);
  }

  .preset-name {
    font-family: var(--font-mono, monospace);
    font-size: 11px;
    color: var(--text-secondary);
  }

  .custom-row {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .custom-actions {
    display: flex;
    gap: 4px;
    flex-shrink: 0;
  }

  .empty-msg {
    padding: 20px;
    text-align: center;
    font-size: 12px;
    color: var(--text-muted);
  }
</style>
