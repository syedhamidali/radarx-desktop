<script lang="ts">
  import { COLORMAP_DATA, COLORMAP_CATEGORIES } from '../utils/colormaps';
  import { getColormapRGB } from '../utils/ppiRenderer';
  import { customColormaps } from '../stores/customColormaps';
  import ColormapEditor from './ColormapEditor.svelte';
  import { portal } from '../utils/portal';
  import { applyTypedLimit, colorbarTicks } from '../utils/colorRange';

  export let cmapName: string = 'turbo';
  export let vmin: number = 0;
  export let vmax: number = 1;
  export let units: string = '';
  export let onCmapChange: ((name: string) => void) | null = null;
  export let onRangeChange: ((vmin: number, vmax: number) => void) | null = null;

  let showPicker = false;
  let showEditor = false;
  let editingVmin = false;
  let editingVmax = false;
  let vminInput = '';
  let vmaxInput = '';

  // $customColormaps: re-saving a custom map under the same name must refresh the swatch too
  $: gradientCSS = buildGradient(cmapName, $customColormaps);
  $: tickValues = computeTicks(vmin, vmax);
  // Force reactive updates when custom colormaps change
  $: allCategories = buildAllCategories($customColormaps);

  function buildGradient(name: string, _custom?: unknown): string {
    const rgb = COLORMAP_DATA[name];
    if (!rgb) return 'linear-gradient(to right, var(--c-surface-3), var(--c-text))';
    const stops: string[] = [];
    const nStops = 16;
    for (let i = 0; i < nStops; i++) {
      const idx = Math.round((i / (nStops - 1)) * 255);
      const r = rgb[idx * 3], g = rgb[idx * 3 + 1], b = rgb[idx * 3 + 2];
      stops.push(`rgb(${r},${g},${b}) ${(i / (nStops - 1) * 100).toFixed(0)}%`);
    }
    return `linear-gradient(to right, ${stops.join(', ')})`;
  }

  const computeTicks = colorbarTicks;

  function buildAllCategories(_customCmaps: any[]): Record<string, string[]> {
    const cats = { ...COLORMAP_CATEGORIES };
    if (_customCmaps.length > 0) {
      cats['Custom'] = _customCmaps.map((c) => c.name);
    }
    return cats;
  }

  function selectCmap(name: string) {
    showPicker = false;
    if (onCmapChange) onCmapChange(name);
  }

  function startEditVmin() {
    editingVmin = true;
    vminInput = vmin.toString();
  }

  function startEditVmax() {
    editingVmax = true;
    vmaxInput = vmax.toString();
  }

  function commitVmin() {
    if (!editingVmin) return; // Enter already committed; the input's blur fires again as it unmounts
    editingVmin = false;
    const next = applyTypedLimit(vminInput, 'vmin', vmin, vmax);
    if (next && onRangeChange) onRangeChange(next[0], next[1]);
  }

  function commitVmax() {
    if (!editingVmax) return;
    editingVmax = false;
    const next = applyTypedLimit(vmaxInput, 'vmax', vmin, vmax);
    if (next && onRangeChange) onRangeChange(next[0], next[1]);
  }

  function handleVminKey(e: KeyboardEvent) {
    if (e.key === 'Enter') commitVmin();
    if (e.key === 'Escape') { e.stopPropagation(); editingVmin = false; }
  }

  function handleVmaxKey(e: KeyboardEvent) {
    if (e.key === 'Enter') commitVmax();
    if (e.key === 'Escape') { e.stopPropagation(); editingVmax = false; }
  }

  /** Keyboard: arrow left/right shift the range by ~1% of its span (1 dBZ on a 95 dBZ scale). */
  function handleBarKeydown(e: KeyboardEvent) {
    if (!onRangeChange) return;
    if ((e.target as HTMLElement)?.tagName === 'INPUT') return; // caret movement while typing a limit
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    e.preventDefault();
    e.stopPropagation(); // the arrows also step through sweeps/frames at app level
    const span = vmax - vmin;
    const step = span > 0 ? Math.pow(10, Math.floor(Math.log10(span)) - 1) : 1;
    const d = e.key === 'ArrowLeft' ? -step : step;
    const round = (v: number) => Math.round(v / step) * step;
    onRangeChange(+round(vmin + d).toFixed(6), +round(vmax + d).toFixed(6));
  }

  function cmapPreviewGradient(name: string): string {
    const rgb = COLORMAP_DATA[name];
    if (!rgb) return 'var(--c-surface-3)';
    const stops: string[] = [];
    for (let i = 0; i < 8; i++) {
      const idx = Math.round((i / 7) * 255);
      stops.push(`rgb(${rgb[idx * 3]},${rgb[idx * 3 + 1]},${rgb[idx * 3 + 2]})`);
    }
    return `linear-gradient(to right, ${stops.join(', ')})`;
  }

  function openEditor() {
    showPicker = false;
    showEditor = true;
  }

  function onEditorApply(newCmapName: string) {
    showEditor = false;
    if (onCmapChange) onCmapChange(newCmapName);
  }

  function onEditorClose() {
    showEditor = false;
  }
</script>

<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
<div class="colorbar-container" role="group" aria-label="Colour scale: arrow keys shift the range" on:keydown={handleBarKeydown} tabindex="0">
  <!-- Colormap name button (opens picker) -->
  <button class="cmap-label" on:click={() => showPicker = !showPicker} title="Change colormap">
    {cmapName}
  </button>

  <!-- Gradient bar with ticks -->
  <div class="colorbar-strip">
    <div class="gradient" style="background: {gradientCSS}"></div>
    <div class="ticks">
      {#each tickValues as tick}
        <span class="tick" style="left: {tick.pos}%">{tick.label}</span>
      {/each}
    </div>
  </div>

  <!-- Units label -->
  {#if units}
    <span class="units-label">{units}</span>
  {/if}

  <!-- Editable vmin/vmax -->
  <div class="range-inputs">
    {#if editingVmin}
      <!-- svelte-ignore a11y_autofocus -->
      <input class="range-input" type="text" bind:value={vminInput}
             on:blur={commitVmin} on:keydown={handleVminKey} autofocus />
    {:else}
      <button class="range-btn" on:click={startEditVmin} title="Click to edit min">{vmin}</button>
    {/if}
    <span class="range-sep">--</span>
    {#if editingVmax}
      <!-- svelte-ignore a11y_autofocus -->
      <input class="range-input" type="text" bind:value={vmaxInput}
             on:blur={commitVmax} on:keydown={handleVmaxKey} autofocus />
    {:else}
      <button class="range-btn" on:click={startEditVmax} title="Click to edit max">{vmax}</button>
    {/if}
  </div>

  <!-- Edit button -->
  <button class="edit-btn" on:click={openEditor} title="Open colormap editor">
    Edit
  </button>

  <!-- Colormap picker dropdown -->
  {#if showPicker}
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="cmap-picker" on:mouseleave={() => showPicker = false}>
      {#each Object.entries(allCategories) as [category, names]}
        <div class="cmap-category">{category}</div>
        {#each names as name}
          <button
            class="cmap-option"
            class:active={name === cmapName}
            on:click={() => selectCmap(name)}
          >
            <div class="cmap-preview" style="background: {cmapPreviewGradient(name)}"></div>
            <span class="cmap-name">{name}</span>
          </button>
        {/each}
      {/each}

      <!-- Link to open editor from picker -->
      <div class="cmap-category">Tools</div>
      <button class="cmap-option" on:click={openEditor}>
        <span class="cmap-name" style="color: var(--accent-primary)">Open Colormap Editor...</span>
      </button>
    </div>
  {/if}
</div>

<!-- Colormap editor modal -->
{#if showEditor}
  <!-- On <body>: this bar has a backdrop-filter, which would otherwise trap the fixed overlay. -->
  <div use:portal style="display: contents">
    <ColormapEditor onApply={onEditorApply} onClose={onEditorClose} />
  </div>
{/if}

<style>
  .colorbar-container {
    position: relative;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 10px;
    background: var(--c-overlay);
    border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm);
    backdrop-filter: blur(8px);
    z-index: 20;
    outline: none;
  }
  /* accent tick on the top-left edge, like the other HUD plates */
  .colorbar-container::before {
    content: '';
    position: absolute;
    left: -1px;
    top: -1px;
    width: 14px;
    height: 2px;
    background: var(--c-accent);
  }
  .colorbar-container:focus-visible {
    box-shadow: 0 0 0 2px var(--accent-primary);
  }

  .cmap-label {
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--text-muted);
    background: var(--bg-surface);
    border: 1px solid var(--border-color);
    border-radius: 3px;
    padding: 2px 6px;
    cursor: pointer;
    white-space: nowrap;
    transition: color var(--transition-fast), border-color var(--transition-fast);
  }
  .cmap-label:hover { color: var(--text-primary); border-color: var(--accent-muted); }

  .colorbar-strip {
    position: relative;
    width: 180px;
    flex-shrink: 0;
  }

  .gradient {
    height: 12px;
    border-radius: 2px;
    border: 1px solid var(--c-border-strong);
  }

  .ticks {
    position: relative;
    height: 14px;
    margin-top: 1px;
  }

  .tick {
    position: absolute;
    transform: translateX(-50%);
    font-family: var(--font-mono);
    font-size: 9.5px;
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .units-label {
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--text-muted);
    white-space: nowrap;
  }

  .range-inputs {
    display: flex;
    align-items: center;
    gap: 4px;
  }

  .range-btn {
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--text-secondary);
    background: transparent;
    border: 1px solid transparent;
    border-radius: 2px;
    padding: 1px 4px;
    cursor: pointer;
    min-width: 28px;
    text-align: center;
    transition: border-color var(--transition-fast);
  }
  .range-btn:hover { border-color: var(--accent-muted); color: var(--text-primary); }

  .range-sep {
    font-size: 10px;
    color: var(--border-light);
  }

  .range-input {
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--text-primary);
    background: var(--bg-surface);
    border: 1px solid var(--accent-primary);
    border-radius: 2px;
    padding: 1px 4px;
    width: 40px;
    text-align: center;
    outline: none;
  }

  .edit-btn {
    font-family: var(--font-mono);
    font-size: 9px;
    color: var(--text-muted);
    background: transparent;
    border: 1px solid var(--border-color);
    border-radius: 3px;
    padding: 2px 6px;
    cursor: pointer;
    transition: color var(--transition-fast), border-color var(--transition-fast);
  }
  .edit-btn:hover {
    color: var(--accent-primary);
    border-color: var(--accent-primary);
  }

  /* Colormap picker */
  .cmap-picker {
    position: absolute;
    bottom: calc(100% + 6px);
    left: 0;
    width: 260px;
    max-height: 360px;
    overflow-y: auto;
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    box-shadow: var(--shadow-lg);
    padding: 4px;
    z-index: 100;
  }

  .cmap-category {
    font-size: 10px;
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 6px 8px 2px;
  }

  .cmap-option {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    padding: 4px 8px;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 3px;
    cursor: pointer;
    transition: background var(--transition-fast);
  }
  .cmap-option:hover { background: var(--bg-hover); }
  .cmap-option.active { border-color: var(--accent-primary); background: var(--bg-active); }

  .cmap-preview {
    width: 80px;
    height: 10px;
    border-radius: 2px;
    flex-shrink: 0;
    border: 1px solid var(--glass-border);
  }

  .cmap-name {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text-secondary);
  }
</style>
