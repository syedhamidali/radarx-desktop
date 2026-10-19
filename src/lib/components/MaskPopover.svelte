<script lang="ts">
  /**
   * MaskPopover -- one toolbar button that opens the value-mask control of a variable
   * ("Hide below / above / between / outside"). The same shared setting (stores/valueMask) is read by
   * the 2D view, the 3D volumes, the mosaic, the wind view, the cross-section and the exports, so
   * changing it here changes it everywhere. The popover is fixed-position, opaque, and kept inside
   * the window.
   */
  import ValueMaskControl from './ValueMaskControl.svelte';
  import { isMaskActive, maskDescription, maskOf, valueMasks } from '../stores/valueMask';

  let {
    variable,
    units = '',
    label = 'Mask',
    testid = 'mask-popover',
  }: { variable: string; units?: string; label?: string; testid?: string } = $props();

  const mask = $derived(maskOf($valueMasks, variable));
  const active = $derived(isMaskActive(mask));
  const note = $derived(maskDescription(mask, units));

  let open = $state(false);
  let host: HTMLDivElement | undefined = $state();
  let popStyle = $state('');

  function place() {
    if (!host) return;
    const r = host.getBoundingClientRect();
    const margin = 8;
    const width = Math.min(320, window.innerWidth - 2 * margin);
    const left = Math.max(margin, Math.min(r.left, window.innerWidth - width - margin));
    const top = r.bottom + 6;
    const maxHeight = Math.max(140, window.innerHeight - top - margin);
    popStyle = `left:${left}px;top:${top}px;width:${width}px;max-height:${maxHeight}px;`;
  }

  $effect(() => {
    if (!open) return;
    place();
    const away = (e: PointerEvent) => {
      if (host && !host.contains(e.target as Node)) open = false;
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') open = false;
    };
    window.addEventListener('pointerdown', away, true);
    window.addEventListener('keydown', esc);
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    return () => {
      window.removeEventListener('pointerdown', away, true);
      window.removeEventListener('keydown', esc);
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, true);
    };
  });
</script>

<div class="mp-host" bind:this={host}>
  <button
    type="button"
    class="mp-btn"
    class:on={active}
    aria-expanded={open}
    aria-haspopup="dialog"
    disabled={!variable}
    title={active ? `Value mask: ${note}. Click to change.` : 'Value mask: hide values below / above / between / outside some limits'}
    onclick={() => (open = !open)}
    data-testid={testid}
  >
    {label}{active ? ': on' : ''} <span aria-hidden="true">{open ? '▴' : '▾'}</span>
  </button>
  {#if open}
    <div class="mp-pop" style={popStyle} role="dialog" aria-label="Value mask of {variable}" data-testid="{testid}-panel">
      <ValueMaskControl {variable} {units} />
      <p class="mp-note">Hides the values in the 2D view, the 3D volume, mosaics, wind, cross-sections and exports.</p>
    </div>
  {/if}
</div>

<style>
  .mp-host {
    position: relative;
    display: inline-flex;
  }
  .mp-btn {
    height: 24px;
    padding: 0 8px;
    font-size: var(--fs-xs, 11px);
    white-space: nowrap;
  }
  .mp-btn.on {
    color: var(--c-accent);
    border-color: var(--c-accent);
    background: rgba(var(--c-accent-rgb), 0.12);
  }
  .mp-pop {
    position: fixed; /* left / top / width / max-height come from place() */
    z-index: 200;
    overflow: auto;
    padding: 10px 12px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    font-size: var(--fs-xs, 11px);
    color: var(--c-text-dim);
    background: var(--c-panel-solid); /* opaque: it floats over the side panels */
    border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-lg, 6px);
    box-shadow: var(--shadow-3);
    text-transform: none;
    letter-spacing: 0;
    font-weight: 400;
  }
  .mp-note {
    margin: 0;
    font-size: var(--fs-2xs, 10px);
    color: var(--c-muted);
  }
</style>
