<script lang="ts">
  /**
   * Status-bar chip of the data check: how many findings the file has, by severity. A click opens
   * the panel (loaded on demand); the x hides the chip for this file until it is closed.
   */
  import { current, panelOpen, hideForNow } from '../stores/dataCheck';
  import { chipLevel, chipText } from '../utils/dataCheckLogic';

  const c = $derived($current);
  const counts = $derived(c.entry?.report?.counts);
  const running = $derived(c.entry?.status === 'running' && !c.entry.report);
  const level = $derived(chipLevel(counts));
  const text = $derived(chipText(counts, running));
</script>

{#if c.visible && c.id}
  <span class="dc-chip {level}">
    <button
      class="dc-main"
      aria-haspopup="dialog"
      aria-expanded={$panelOpen}
      title="What the data check found in this file. Nothing is changed unless you apply a suggestion."
      onclick={() => panelOpen.update((v) => !v)}
    >
      <span class="dot" aria-hidden="true"></span>
      <span class="txt mono">{text}</span>
    </button>
    <button class="dc-x" aria-label="Hide the data check for this file" title="Hide for this file" onclick={() => c.id && hideForNow(c.id)}>&times;</button>
  </span>
{/if}

<style>
  .dc-chip {
    display: inline-flex;
    align-items: stretch;
    height: 18px;
    font-size: 11px;
    border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm);
    color: var(--c-text-dim);
    flex-shrink: 0;
    overflow: hidden;
  }
  .dc-chip.warning { color: var(--c-caution); background: rgba(var(--c-caution-rgb), 0.1); border-color: rgba(var(--c-caution-rgb), 0.45); }
  .dc-chip.notice { color: var(--c-accent); background: rgba(var(--c-accent-rgb), 0.08); border-color: rgba(var(--c-accent-rgb), 0.35); }
  .dc-chip.ok { color: var(--c-nominal); background: rgba(var(--c-nominal-rgb), 0.08); border-color: rgba(var(--c-nominal-rgb), 0.35); }
  button { all: unset; cursor: pointer; display: inline-flex; align-items: center; color: inherit; }
  button:focus-visible { outline: 2px solid var(--c-accent); outline-offset: -2px; }
  .dc-main { padding: 0 6px 0 8px; gap: 6px; }
  .dc-main:hover, .dc-x:hover { background: rgba(var(--c-accent-rgb), 0.14); }
  .dc-x { padding: 0 6px; font-size: 13px; line-height: 1; opacity: 0.7; }
  .dc-x:hover { opacity: 1; }
  .dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; flex-shrink: 0; }
  .txt { white-space: nowrap; }
</style>
