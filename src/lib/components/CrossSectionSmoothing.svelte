<script lang="ts">
  /**
   * CrossSectionSmoothing -- how the cross-section picture is smoothed: interpolation (nearest,
   * bilinear, bicubic), Gaussian strength in gate lengths and a finer grid. The data is never
   * changed, only how it is drawn; the settings are remembered and shared by every view of the section.
   */
  import { smoothingStore, setSmoothing } from '../stores/crossSectionSmoothing';
  import { NO_SMOOTHING, isPlain, MAX_SIGMA, type Interp, type Smoothing } from '../utils/crossSectionRaster';

  /**
   * By default it edits the shared cross-section setting. Another view (the RHI display) passes its
   * own `value` and `onChange` to reuse the same controls for settings of its own; `uid` keeps the
   * element ids apart when two are on screen.
   */
  let {
    compact = false,
    value = undefined,
    onChange = undefined,
    uid = 'cs',
  }: {
    compact?: boolean;
    value?: Smoothing;
    onChange?: (patch: Partial<Smoothing>) => void;
    uid?: string;
  } = $props();

  const s = $derived(value ?? $smoothingStore);
  const apply = (patch: Partial<Smoothing>) => (onChange ? onChange(patch) : setSmoothing(patch));
  const plain = $derived(isPlain(s));
  const INTERPS: { id: Interp; label: string; hint: string }[] = [
    { id: 'nearest', label: 'Nearest', hint: 'Blocky gates, exactly as measured' },
    { id: 'bilinear', label: 'Bilinear', hint: 'Blend between neighbouring gates and elevation angles' },
    { id: 'bicubic', label: 'Bicubic', hint: 'Smoother curves between gates (no overshoot)' },
  ];
</script>

<div class="sm" class:compact data-testid="cs-smoothing">
  <div class="sm-row" role="group" aria-label="Interpolation">
    <span class="sm-label">Interpolation</span>
    <div class="sm-seg">
      {#each INTERPS as o (o.id)}
        <button class:on={s.interp === o.id} aria-pressed={s.interp === o.id} title={o.hint}
                onclick={() => apply({ interp: o.id })} data-testid="cs-interp-{o.id}">{o.label}</button>
      {/each}
    </div>
  </div>
  <div class="sm-row">
    <label class="sm-label" for="{uid}-sigma{compact ? '-c' : ''}">Gaussian</label>
    <input id="{uid}-sigma{compact ? '-c' : ''}" type="range" min="0" max={MAX_SIGMA} step="0.5" value={s.sigma}
           oninput={(e) => apply({ sigma: Number(e.currentTarget.value) })}
           title="Smoothing strength in gate lengths; 0 = off. Missing data is ignored, so echo edges keep their level and shape."
           data-testid="cs-sigma" />
    <span class="sm-val">{s.sigma > 0 ? `${s.sigma} gate${s.sigma === 1 ? '' : 's'}` : 'off'}</span>
  </div>
  <div class="sm-row" role="group" aria-label="Grid resolution">
    <span class="sm-label">Finer grid</span>
    <div class="sm-seg">
      {#each [1, 2, 3, 4] as u (u)}
        <button class:on={s.upsample === u} aria-pressed={s.upsample === u}
                title={u === 1 ? 'One sample per gate' : `${u}× finer than the radar gates`}
                onclick={() => apply({ upsample: u })} data-testid="cs-up-{u}">{u}&times;</button>
      {/each}
    </div>
    <button class="sm-reset" disabled={plain} onclick={() => apply(NO_SMOOTHING)} title="Back to the gates as measured">Reset</button>
  </div>
</div>

<style>
  .sm {
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 280px;
  }
  .sm.compact {
    gap: 5px;
    min-width: 0;
  }
  .sm-row {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .sm-label {
    flex: 0 0 84px;
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted, #5c6088);
  }
  .compact .sm-label {
    flex-basis: 70px;
  }
  .sm-seg {
    display: inline-flex;
  }
  .sm-seg button {
    height: 24px;
    padding: 0 8px;
    font-size: 11px;
    border-radius: 0;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }
  .sm-seg button:first-child {
    border-radius: var(--radius-sm, 6px) 0 0 var(--radius-sm, 6px);
  }
  .sm-seg button:last-child {
    border-radius: 0 var(--radius-sm, 6px) var(--radius-sm, 6px) 0;
  }
  .sm-seg button + button {
    margin-left: -1px;
  }
  .sm-seg button.on {
    background: rgba(91, 108, 247, 0.28);
    border-color: rgba(91, 108, 247, 0.55);
    color: #fff;
    position: relative;
  }
  input[type='range'] {
    flex: 1;
    min-width: 80px;
    accent-color: var(--accent-primary, #5b6cf7);
  }
  .sm-val {
    flex: 0 0 56px;
    font-size: 11px;
    font-family: var(--font-mono, monospace);
    color: var(--text-secondary, #9da2c2);
  }
  .sm-reset {
    height: 24px;
    padding: 0 8px;
    font-size: 11px;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }
</style>
