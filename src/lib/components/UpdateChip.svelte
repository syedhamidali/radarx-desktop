<script lang="ts">
  /**
   * Quiet "Update available: vX.Y.Z" chip for the status bar. Appears only when a newer version is
   * known (and not skipped); a click opens the update dialog. Nothing else ever interrupts the user.
   */
  import { updater, visibleUpdate } from '../stores/updater';
  import { openUpdateDialog, jobBusy } from '../utils/updater';
  import { progressPercent } from '../utils/updateLogic';

  const u = $derived($updater);
  const update = $derived($visibleUpdate);
  const pct = $derived(progressPercent(u.received, u.total));
  const manual = $derived(u.status?.install.mode === 'manual');

  const text = $derived.by(() => {
    if (!update) return '';
    if (u.phase === 'downloading') return `Downloading v${update.version}${pct !== null ? ` ${pct}%` : ''}`;
    if (u.phase === 'installing') return 'Installing...';
    if (u.phase === 'ready') return `Update ready: v${update.version}`;
    return `Update available: v${update.version}`;
  });
  const title = $derived.by(() => {
    if (u.phase === 'ready') {
      return $jobBusy.busy
        ? `Downloaded. The restart waits for ${$jobBusy.reason ?? 'the running job'} to finish.`
        : 'Downloaded. Click to restart into the new version.';
    }
    return manual ? 'A new version is available. Click for details.' : 'A new version is available. Click to read the release notes.';
  });
</script>

{#if update && u.status?.configured}
  <button class="upd-chip" class:ready={u.phase === 'ready'} {title} aria-label={text} onclick={openUpdateDialog}>
    <span class="dot" aria-hidden="true"></span>
    <span class="txt mono">{text}</span>
  </button>
{/if}

<style>
  .upd-chip {
    height: 18px;
    padding: 0 8px;
    gap: 6px;
    font-size: 11px;
    font-weight: 500;
    color: var(--c-accent);
    background: rgba(var(--c-accent-rgb), 0.08);
    border: 1px solid rgba(var(--c-accent-rgb), 0.35);
    border-radius: var(--radius-sm);
    flex-shrink: 0;
  }
  .upd-chip:hover { background: rgba(var(--c-accent-rgb), 0.16); border-color: var(--c-accent); }
  .upd-chip.ready { color: var(--c-nominal); background: rgba(var(--c-nominal-rgb), 0.1); border-color: rgba(var(--c-nominal-rgb), 0.4); }
  .upd-chip.ready:hover { background: rgba(var(--c-nominal-rgb), 0.18); border-color: var(--c-nominal); }
  .dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; flex-shrink: 0; }
  .txt { white-space: nowrap; }
</style>
