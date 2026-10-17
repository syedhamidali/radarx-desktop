<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { fade } from 'svelte/transition';

  export let text = '';
  export let shortcut = '';
  export let position: 'top' | 'bottom' | 'left' | 'right' = 'top';
  export let delay = 400;

  let show = false;
  let wrapper: HTMLDivElement;
  let tooltipEl: HTMLDivElement;
  let timeoutId: ReturnType<typeof setTimeout> | null = null;
  let tooltipStyle = '';
  let actualPosition = position;

  function enter() {
    if (timeoutId) clearTimeout(timeoutId); // focus after hover must not leave a stray timer
    timeoutId = setTimeout(() => {
      show = true;
      requestAnimationFrame(reposition);
    }, delay);
  }

  function leave() {
    if (timeoutId) { clearTimeout(timeoutId); timeoutId = null; }
    show = false;
  }

  function reposition() {
    if (!wrapper || !tooltipEl) return;
    const triggerRect = wrapper.getBoundingClientRect();
    const ttRect = tooltipEl.getBoundingClientRect();
    const gap = 8;
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    actualPosition = position;

    // Check if preferred position fits, otherwise flip
    if (position === 'top' && triggerRect.top - ttRect.height - gap < 0) actualPosition = 'bottom';
    if (position === 'bottom' && triggerRect.bottom + ttRect.height + gap > vh) actualPosition = 'top';
    if (position === 'left' && triggerRect.left - ttRect.width - gap < 0) actualPosition = 'right';
    if (position === 'right' && triggerRect.right + ttRect.width + gap > vw) actualPosition = 'left';

    let top = 0;
    let left = 0;

    switch (actualPosition) {
      case 'top':
        top = triggerRect.top - ttRect.height - gap;
        left = triggerRect.left + triggerRect.width / 2 - ttRect.width / 2;
        break;
      case 'bottom':
        top = triggerRect.bottom + gap;
        left = triggerRect.left + triggerRect.width / 2 - ttRect.width / 2;
        break;
      case 'left':
        top = triggerRect.top + triggerRect.height / 2 - ttRect.height / 2;
        left = triggerRect.left - ttRect.width - gap;
        break;
      case 'right':
        top = triggerRect.top + triggerRect.height / 2 - ttRect.height / 2;
        left = triggerRect.right + gap;
        break;
    }

    // Clamp to viewport
    left = Math.max(4, Math.min(left, vw - ttRect.width - 4));
    top = Math.max(4, Math.min(top, vh - ttRect.height - 4));

    tooltipStyle = `top: ${top}px; left: ${left}px;`;
  }

  onDestroy(() => {
    if (timeoutId) clearTimeout(timeoutId);
  });
</script>

<!-- svelte-ignore a11y-no-static-element-interactions -->
<div
  class="tooltip-wrapper"
  bind:this={wrapper}
  on:mouseenter={enter}
  on:mouseleave={leave}
  on:focusin={enter}
  on:focusout={leave}
  on:mousedown={leave}
  on:keydown={(e) => { if (e.key === 'Escape') leave(); }}
>
  <slot />
</div>

{#if show && (text || shortcut)}
  <div
    class="tooltip-popup tooltip-{actualPosition}"
    role="tooltip"
    style={tooltipStyle}
    bind:this={tooltipEl}
    transition:fade={{ duration: 120 }}
  >
    {#if text}<span class="tooltip-text">{text}</span>{/if}
    {#if shortcut}<kbd class="tooltip-kbd">{shortcut}</kbd>{/if}
    <div class="tooltip-arrow tooltip-arrow-{actualPosition}"></div>
  </div>
{/if}

<style>
  .tooltip-wrapper {
    display: inline-flex;
  }

  .tooltip-popup {
    position: fixed;
    z-index: 3000;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 5px 10px;
    background: var(--bg-surface);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
    box-shadow: var(--shadow-md);
    font-size: 11px;
    color: var(--text-primary);
    white-space: nowrap;
    pointer-events: none;
    max-width: 280px;
  }

  .tooltip-text {
    line-height: 1.4;
  }

  .tooltip-kbd {
    font-size: 10px;
    padding: 1px 5px;
    background: rgba(8, 10, 20, 0.6);
    border: 1px solid var(--glass-border);
    border-radius: 3px;
    color: var(--text-accent);
    font-family: var(--font-mono);
  }

  .tooltip-arrow {
    position: absolute;
    width: 8px;
    height: 8px;
    background: var(--bg-surface);
    border: 1px solid var(--glass-border);
    transform: rotate(45deg);
  }

  .tooltip-arrow-top {
    bottom: -5px;
    left: 50%;
    margin-left: -4px;
    border-top: none;
    border-left: none;
  }

  .tooltip-arrow-bottom {
    top: -5px;
    left: 50%;
    margin-left: -4px;
    border-bottom: none;
    border-right: none;
  }

  .tooltip-arrow-left {
    right: -5px;
    top: 50%;
    margin-top: -4px;
    border-bottom: none;
    border-left: none;
  }

  .tooltip-arrow-right {
    left: -5px;
    top: 50%;
    margin-top: -4px;
    border-top: none;
    border-right: none;
  }
</style>
