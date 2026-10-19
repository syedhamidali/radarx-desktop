<script lang="ts">
  import { toasts, dismissToast } from '../stores/toasts';

  const items = $derived($toasts);
</script>

<div class="toast-container" role="region" aria-label="Notifications" aria-live="polite" aria-relevant="additions">
  {#each items as toast (toast.id)}
    <div class="toast toast-{toast.level}" role={toast.level === 'error' ? 'alert' : 'status'}>
      <div class="toast-icon">
        {#if toast.level === 'success'}
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
        {:else if toast.level === 'error'}
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
        {:else if toast.level === 'warning'}
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
        {:else}
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
        {/if}
      </div>
      <span class="toast-message">{toast.message}</span>
      <button class="toast-dismiss" on:click={() => dismissToast(toast.id)} title="Dismiss" aria-label="Dismiss notification">&times;</button>
    </div>
  {/each}
</div>

<style>
  .toast-container {
    position: fixed;
    bottom: 36px;
    right: 12px;
    display: flex;
    flex-direction: column-reverse;
    gap: 6px;
    z-index: 10000;
    pointer-events: none;
    max-width: 380px;
  }

  /* A toast is a status plate: a 3 px level-coloured bar on the left, hairline elsewhere. */
  .toast {
    --lvl: var(--c-accent);
    position: relative;
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-2) var(--space-2) var(--space-3);
    border-radius: var(--radius-sm);
    font-size: var(--fs-sm);
    color: var(--c-text);
    pointer-events: auto;
    animation: toast-in var(--dur-base) var(--ease-out);
    box-shadow: var(--shadow-2);
    border: 1px solid var(--c-border-strong);
    border-left: 3px solid var(--lvl);
    background: var(--c-overlay);
  }

  .toast-success { --lvl: var(--c-nominal); }
  .toast-error { --lvl: var(--c-alarm); }
  .toast-warning { --lvl: var(--c-caution); }
  .toast-icon { color: var(--lvl); flex-shrink: 0; display: flex; align-items: center; }

  .toast-message { flex: 1; line-height: 1.4; }

  .toast-dismiss {
    background: none;
    border: none;
    color: var(--c-muted);
    cursor: pointer;
    font-size: 16px;
    padding: 0;
    width: 20px;
    height: 20px;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    border-radius: var(--radius-xs);
  }
  .toast-dismiss:hover { color: var(--c-text); background: var(--bg-hover); border: none; }

  @keyframes toast-in {
    from { opacity: 0; transform: translateX(12px); }
    to { opacity: 1; transform: translateX(0); }
  }
</style>
