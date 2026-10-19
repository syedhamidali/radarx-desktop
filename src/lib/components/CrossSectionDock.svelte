<script lang="ts">
  /**
   * CrossSectionDock -- the cross-section as a pane under the PPI. The height is dragged on the
   * handle between the two (double-click or Home resets it) and remembered.
   */
  import { onMount } from 'svelte';
  import CrossSectionLarge from './CrossSectionLarge.svelte';

  const KEY = 'xradar.cs.dockHeight.v1';
  const DEFAULT_H = 460;
  const MIN_H = 200;
  const MIN_PPI = 220; // keep this much of the PPI visible

  function load(): number {
    try {
      const v = Number(localStorage.getItem(KEY));
      if (Number.isFinite(v) && v >= MIN_H) return v;
    } catch {
      /* no stored height */
    }
    return DEFAULT_H;
  }

  let height = $state(load());
  let dragging = $state(false);
  let rootEl = $state<HTMLDivElement | undefined>();

  /** The tallest the dock may get: whatever leaves the PPI its minimum. */
  function maxHeight(): number {
    const parent = rootEl?.parentElement?.getBoundingClientRect().height ?? window.innerHeight;
    return Math.max(MIN_H, parent - MIN_PPI);
  }
  const clamp = (h: number) => Math.round(Math.min(maxHeight(), Math.max(MIN_H, h)));

  function save() {
    try {
      localStorage.setItem(KEY, String(height));
    } catch {
      /* storage unavailable: ignore */
    }
  }

  let startY = 0;
  let startH = 0;
  function onDown(e: PointerEvent) {
    if (e.button !== 0) return;
    startY = e.clientY;
    startH = height;
    dragging = true;
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    e.preventDefault();
  }
  function onMove(e: PointerEvent) {
    if (!dragging) return;
    height = clamp(startH + (startY - e.clientY));
  }
  function onUp() {
    if (!dragging) return;
    dragging = false;
    save();
  }
  function onKey(e: KeyboardEvent) {
    const step = e.shiftKey ? 80 : 20;
    if (e.key === 'ArrowUp') height = clamp(height + step);
    else if (e.key === 'ArrowDown') height = clamp(height - step);
    else if (e.key === 'Home') height = clamp(DEFAULT_H);
    else return;
    e.preventDefault();
    save();
  }

  // A shorter window must not leave the dock taller than the PPI allows.
  onMount(() => {
    const fit = () => {
      const c = clamp(height);
      if (c !== height) height = c;
    };
    fit();
    window.addEventListener('resize', fit);
    return () => window.removeEventListener('resize', fit);
  });
</script>

<div class="dock" bind:this={rootEl} style="height: {height}px" data-testid="cs-dock-pane">
  <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
  <div
    class="grip"
    class:active={dragging}
    role="separator"
    aria-orientation="horizontal"
    aria-label="Resize the cross-section"
    aria-valuenow={height}
    aria-valuemin={MIN_H}
    tabindex="0"
    title="Drag to resize, double-click to reset"
    onpointerdown={onDown}
    onpointermove={onMove}
    onpointerup={onUp}
    onpointercancel={onUp}
    ondblclick={() => { height = clamp(DEFAULT_H); save(); }}
    onkeydown={onKey}
    data-testid="cs-dock-grip"
  ><span></span></div>
  <div class="body"><CrossSectionLarge mode="dock" /></div>
</div>

<style>
  .dock {
    position: relative;
    flex: 0 0 auto;
    display: flex;
    flex-direction: column;
    min-height: 0;
    border-top: 1px solid var(--glass-border, rgba(140, 160, 250, 0.12));
    background: var(--bg-secondary, #0d1020);
  }
  .grip {
    position: absolute;
    top: -5px;
    left: 0;
    right: 0;
    height: 10px;
    z-index: 30;
    cursor: ns-resize;
    display: flex;
    align-items: center;
    justify-content: center;
    touch-action: none;
    outline: none;
  }
  .grip span {
    width: 44px;
    height: 3px;
    border-radius: 2px;
    background: var(--border-light, rgba(140, 160, 250, 0.3));
    transition: background 120ms ease;
  }
  .grip:hover span,
  .grip.active span,
  .grip:focus-visible span {
    background: var(--accent-primary, #5b6cf7);
  }
  .body {
    flex: 1 1 0;
    min-height: 0;
  }
</style>
