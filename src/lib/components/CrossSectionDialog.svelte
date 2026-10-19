<script lang="ts">
  /**
   * CrossSectionDialog -- the cross-section at about 90% of the window, above everything else.
   * Focus stays inside, Esc (or a click on the backdrop) closes it, and "Dock" puts the view back
   * under the PPI.
   */
  import { fade } from 'svelte/transition';
  import { crossSectionStore, closeCrossSectionModal } from '../stores/crossSection';
  import { focusTrap } from '../utils/focusTrap';
  import { portal } from '../utils/portal';
  import CrossSectionLarge from './CrossSectionLarge.svelte';

  const open = $derived($crossSectionStore.layout === 'modal');

  function onKeydown(e: KeyboardEvent) {
    if (!open || e.key !== 'Escape') return;
    e.preventDefault();
    e.stopPropagation();
    closeCrossSectionModal();
  }

  function onBackdrop(e: MouseEvent) {
    if (e.target === e.currentTarget) closeCrossSectionModal();
  }
</script>

<svelte:window onkeydowncapture={onKeydown} />

{#if open}
  <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
  <div class="csd-overlay" use:portal onclick={onBackdrop} transition:fade={{ duration: 120 }} data-testid="cs-dialog-overlay">
    <div class="csd" role="dialog" aria-modal="true" aria-label="Cross-section" use:focusTrap={{ initial: '.csl-close' }}>
      <CrossSectionLarge mode="modal" />
    </div>
  </div>
{/if}

<style>
  .csd-overlay {
    position: fixed;
    inset: 0;
    z-index: 900;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(var(--c-shadow-rgb), 0.62);
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
  }
  .csd {
    width: 92vw;
    height: 90vh;
    max-width: 2400px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    background: var(--bg-secondary);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-lg, 12px);
    box-shadow: var(--shadow-lg);
    outline: none;
  }
</style>
