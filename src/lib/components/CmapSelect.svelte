<script lang="ts">
  /**
   * Colormap dropdown of the 3D toolbars. Same names and groups as the 2D colour bar picker
   * (built-in groups plus the user's custom maps); disabled for categorical variables, whose
   * class colours are fixed.
   */
  import { COLORMAP_DATA, COLORMAP_CATEGORIES } from '../utils/colormaps';
  import { customColormaps } from '../stores/customColormaps';

  let {
    value,
    disabled = false,
    onchange,
  }: { value: string; disabled?: boolean; onchange: (name: string) => void } = $props();

  const groups = $derived.by(() => {
    const g: Record<string, string[]> = {};
    const known = new Set<string>();
    for (const [cat, names] of Object.entries(COLORMAP_CATEGORIES)) {
      const present = names.filter((n) => n in COLORMAP_DATA);
      present.forEach((n) => known.add(n));
      if (present.length) g[cat] = present;
    }
    const custom = $customColormaps.map((c) => c.name).filter((n) => n in COLORMAP_DATA);
    if (custom.length) {
      custom.forEach((n) => known.add(n));
      g['Custom'] = custom;
    }
    // a name chosen elsewhere that is in no group (e.g. a registered one) must stay selectable
    if (value && !known.has(value) && value in COLORMAP_DATA) g['Current'] = [value];
    return g;
  });
</script>

<select
  class="cmap-sel"
  {value}
  {disabled}
  aria-label="Colormap"
  title={disabled
    ? 'This product is categorical: its class colours are fixed'
    : 'Colormap of the volume and the colour bar (shared with the 2D view)'}
  onchange={(e) => onchange((e.target as HTMLSelectElement).value)}
>
  {#each Object.entries(groups) as [cat, names] (cat)}
    <optgroup label={cat}>
      {#each names as n (n)}<option value={n}>{n}</option>{/each}
    </optgroup>
  {/each}
</select>

<style>
  .cmap-sel {
    font-size: 10px;
    padding: 1px 4px;
    max-width: 120px;
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    background: var(--bg-tertiary);
    color: var(--text-secondary);
  }
  .cmap-sel:disabled { opacity: 0.5; cursor: not-allowed; }
</style>
