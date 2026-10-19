<script lang="ts">
  /**
   * ValueMaskControl -- hide the values of one variable below / above / between / outside some
   * limits. Reads and writes the shared per-variable settings in stores/valueMask, so every view
   * that uses the mask agrees. Compact enough for a toolbar popover.
   */
  import {
    MASK_MODES,
    isMaskActive,
    maskDescription,
    maskOf,
    maskPresets,
    resetMask,
    setMask,
    valueMasks,
    type MaskMode,
  } from '../stores/valueMask';

  let { variable, units = '', compact = false }: { variable: string; units?: string; compact?: boolean } = $props();

  const mask = $derived(maskOf($valueMasks, variable));
  const active = $derived(isMaskActive(mask));
  const needsMin = $derived(mask.mode === 'below' || mask.mode === 'between' || mask.mode === 'outside');
  const needsMax = $derived(mask.mode === 'above' || mask.mode === 'between' || mask.mode === 'outside');
  const presets = $derived(maskPresets(variable, units));
  const summary = $derived(maskDescription(mask, units));
  const uid = $derived(`vm-${variable}`.replace(/[^A-Za-z0-9_-]/g, '_'));

  function onMode(e: Event) {
    const mode = (e.currentTarget as HTMLSelectElement).value as MaskMode;
    setMask(variable, { mode });
  }
  function onNum(which: 'min' | 'max', e: Event) {
    const raw = (e.currentTarget as HTMLInputElement).value;
    setMask(variable, { [which]: raw === '' ? null : Number(raw) });
  }
</script>

<div class="vm" class:compact data-testid="value-mask">
  <div class="vm-row">
    <label class="vm-label" for="{uid}-mode">Mask</label>
    <select id="{uid}-mode" value={mask.mode} onchange={onMode} disabled={!variable} data-testid="mask-mode"
            title="Hide values of {variable || 'the variable'}">
      {#each MASK_MODES as m (m.id)}<option value={m.id}>{m.label}</option>{/each}
    </select>
    {#if needsMin}
      <input type="number" class="vm-num" value={mask.min ?? ''} step="any" placeholder={mask.mode === 'below' ? 'limit' : 'from'}
             aria-label="Lower limit{units ? ` in ${units}` : ''}" onchange={(e) => onNum('min', e)} data-testid="mask-min" />
    {/if}
    {#if mask.mode === 'between' || mask.mode === 'outside'}<span class="vm-unit">to</span>{/if}
    {#if needsMax}
      <input type="number" class="vm-num" value={mask.max ?? ''} step="any" placeholder={mask.mode === 'above' ? 'limit' : 'to'}
             aria-label="Upper limit{units ? ` in ${units}` : ''}" onchange={(e) => onNum('max', e)} data-testid="mask-max" />
    {/if}
    {#if mask.mode !== 'off' && units}<span class="vm-unit">{units}</span>{/if}
  </div>

  {#if mask.mode !== 'off'}
    <div class="vm-row">
      <label class="vm-check" title="Also hide the limit values themselves">
        <input type="checkbox" checked={mask.inclusive} onchange={(e) => setMask(variable, { inclusive: e.currentTarget.checked })}
               data-testid="mask-inclusive" /> Inclusive
      </label>
      <button class="vm-btn" onclick={() => resetMask(variable)} title="Show all values again" data-testid="mask-reset">Reset</button>
    </div>
  {/if}

  {#if presets.length}
    <div class="vm-row vm-presets" role="group" aria-label="Quick masks">
      <span class="vm-label">Quick</span>
      {#each presets as p (p.label)}
        <button class="vm-btn" onclick={() => setMask(variable, p.mask)} title={maskDescription(p.mask, units) ?? ''}
                data-testid="mask-preset">{p.label}</button>
      {/each}
    </div>
  {/if}

  {#if summary}
    <div class="vm-summary" data-testid="mask-summary">{summary}</div>
  {:else if mask.mode !== 'off' && !active}
    <div class="vm-summary muted">Enter the limit{mask.mode === 'between' || mask.mode === 'outside' ? 's' : ''} to apply the mask.</div>
  {/if}
</div>

<style>
  .vm {
    display: flex;
    flex-direction: column;
    gap: 7px;
    min-width: 280px;
  }
  .vm.compact {
    gap: 5px;
    min-width: 0;
  }
  .vm-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }
  .vm-label {
    flex: 0 0 52px;
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted);
  }
  .vm select,
  .vm-num {
    height: 24px;
    font-size: 11px;
  }
  .vm-num {
    width: 64px;
    padding: 2px 6px;
    font-family: var(--font-mono, monospace);
  }
  .vm-unit {
    font-size: 11px;
    color: var(--text-muted);
  }
  .vm-check {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-size: 11px;
  }
  .vm-btn {
    height: 24px;
    padding: 0 8px;
    font-size: 11px;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }
  .vm-summary {
    font-size: 11px;
    font-family: var(--font-mono, monospace);
    color: var(--accent-hover);
  }
  .vm-summary.muted {
    color: var(--text-muted);
    font-family: inherit;
  }
</style>
