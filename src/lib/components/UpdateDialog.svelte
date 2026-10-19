<script lang="ts">
  /**
   * "Update available" dialog: version, release notes (safe markdown), download progress, and the
   * three choices: Install and restart / Later / Skip this version. The restart never interrupts a
   * running job: while one is running the install button is disabled and says what it is waiting for.
   */
  import { fade, fly } from 'svelte/transition';
  import { focusTrap } from '../utils/focusTrap';
  import { updater, visibleUpdate } from '../stores/updater';
  import {
    jobBusy,
    installAndRestart,
    later,
    skipVersion,
    openExternal,
    restartApp,
  } from '../utils/updater';
  import { formatBytes, progressPercent } from '../utils/updateLogic';
  import ReleaseNotes from './ReleaseNotes.svelte';

  const u = $derived($updater);
  const update = $derived($visibleUpdate ?? u.update); // an opened dialog keeps its version even if skipped meanwhile
  const open = $derived(u.dialogOpen && !!update && !!u.status?.configured);
  const manual = $derived(u.status?.install.mode === 'manual');
  const busy = $derived($jobBusy);
  const pct = $derived(progressPercent(u.received, u.total));
  const downloading = $derived(u.phase === 'downloading');
  const installing = $derived(u.phase === 'installing');
  const ready = $derived(u.phase === 'ready');
  const canInstall = $derived(!busy.busy && !downloading && !installing && !manual);
  const dateText = $derived.by(() => {
    if (!update?.date) return '';
    const d = new Date(update.date);
    return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  });

  // Escape means "Later". Captured so it does not also close a Settings dialog underneath.
  $effect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || installing) return;
      e.preventDefault();
      e.stopPropagation();
      later();
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  });

  function overlayClick(e: MouseEvent) {
    if ((e.target as HTMLElement).classList.contains('upd-overlay') && !installing) later();
  }
</script>

{#if open && update}
  <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
  <div class="upd-overlay" onclick={overlayClick} transition:fade={{ duration: 150 }}>
    <div
      class="upd-dialog brackets-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="upd-title"
      transition:fly={{ y: 16, duration: 220 }}
      use:focusTrap={{ initial: '.upd-primary' }}
    >
      <header class="upd-head">
        <h2 id="upd-title">{ready ? 'Update ready to install' : 'Update available'}</h2>
        <div class="upd-versions mono">
          <span>v{u.status?.currentVersion}</span><span class="arrow">&rarr;</span><span class="new">v{update.version}</span>
          {#if dateText}<span class="date">{dateText}</span>{/if}
        </div>
      </header>

      <section class="upd-notes" aria-label="Release notes">
        {#if update.notes.trim()}
          <ReleaseNotes markdown={update.notes} />
        {:else}
          <p class="none">No release notes were published for this version.</p>
        {/if}
      </section>

      {#if downloading}
        <div class="upd-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow={pct ?? undefined} aria-label="Download progress">
          <div class="bar"><div class="fill" class:indeterminate={pct === null} style={pct !== null ? `width: ${pct}%` : ''}></div></div>
          <div class="msg mono">
            Downloading{pct !== null ? ` ${pct}%` : ''}{u.total ? ` (${formatBytes(u.received)} of ${formatBytes(u.total)})` : u.received ? ` (${formatBytes(u.received)})` : ''}
          </div>
        </div>
      {/if}

      {#if installing}
        <p class="upd-note">Installing. The app restarts by itself in a moment.</p>
      {:else if busy.busy && !manual}
        <p class="upd-note busy" role="status">
          {busy.reason ? `Waiting for ${busy.reason} to finish.` : 'A job is running.'}
          {ready ? 'The restart is on hold so nothing is interrupted; you will be asked again when it is done.' : 'Installing is paused so nothing is interrupted.'}
        </p>
      {/if}

      {#if manual}
        <p class="upd-note">
          {u.status?.install.note ?? 'This installation cannot update itself. Download the new version from the release page.'}
        </p>
      {/if}

      {#if u.actionError}
        <p class="upd-error" role="alert">{u.actionError}</p>
      {/if}

      <footer class="upd-actions">
        {#if manual}
          {#if u.status?.releasesUrl}
            <button class="primary upd-primary" onclick={() => openExternal(u.status!.releasesUrl!)}>Open the release page</button>
          {/if}
        {:else if u.engineStopped}
          <button class="primary upd-primary" onclick={() => restartApp()}>Restart now</button>
        {:else}
          <button class="primary upd-primary" disabled={!canInstall} onclick={() => installAndRestart()}>
            {downloading ? 'Downloading...' : installing ? 'Installing...' : 'Install and restart'}
          </button>
        {/if}
        <button onclick={later} disabled={installing}>Later</button>
        <button class="quiet" onclick={skipVersion} disabled={installing || downloading} title="Do not offer v{update.version} again. A newer version will still be offered.">Skip this version</button>
      </footer>
    </div>
  </div>
{/if}

<style>
  .upd-overlay {
    position: fixed;
    inset: 0;
    z-index: 960; /* above Settings (900) and About (950) */
    background: var(--c-scrim);
    display: flex;
    align-items: center;
    justify-content: center;
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
  }
  .upd-dialog {
    width: 520px;
    max-width: 92vw;
    max-height: 86vh;
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    padding: var(--space-5);
    background: var(--c-overlay);
    border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm);
    box-shadow: var(--shadow-3);
  }
  .upd-head h2 { font-size: var(--fs-lg); font-weight: 600; color: var(--c-text); margin: 0; }
  .upd-versions {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 4px;
    font-size: var(--fs-xs);
    color: var(--c-muted);
  }
  .upd-versions .new { color: var(--c-accent); font-weight: 600; }
  .upd-versions .arrow { color: var(--c-border-strong); }
  .upd-versions .date { margin-left: auto; }

  .upd-notes {
    flex: 1 1 auto;
    min-height: 80px;
    max-height: 40vh;
    overflow-y: auto;
    padding: var(--space-3);
    background: var(--c-field);
    border: 1px solid var(--c-border);
    border-radius: var(--radius-sm);
  }
  .upd-notes .none { margin: 0; color: var(--c-muted); font-size: var(--fs-sm); }

  .upd-progress .bar {
    height: 6px;
    background: rgba(var(--c-accent-rgb), 0.12);
    border-radius: 3px;
    overflow: hidden;
  }
  .upd-progress .fill {
    height: 100%;
    background: var(--c-accent);
    transition: width var(--dur-base) var(--ease-in-out);
  }
  .upd-progress .fill.indeterminate { width: 35%; animation: upd-slide 1.2s var(--ease-in-out) infinite; }
  @keyframes upd-slide { from { margin-left: -35%; } to { margin-left: 100%; } }
  .upd-progress .msg { margin-top: 4px; font-size: var(--fs-xs); color: var(--c-text-dim); }
  @media (prefers-reduced-motion: reduce) {
    .upd-progress .fill.indeterminate { animation: none; width: 100%; opacity: 0.5; }
  }

  .upd-note { margin: 0; font-size: var(--fs-sm); color: var(--c-text-dim); line-height: 1.5; }
  .upd-note.busy {
    padding: var(--space-2) var(--space-3);
    color: var(--c-text);
    background: rgba(var(--c-caution-rgb), 0.12);
    border: 1px solid rgba(var(--c-caution-rgb), 0.4);
    border-radius: var(--radius-sm);
  }
  .upd-error {
    margin: 0;
    padding: var(--space-2) var(--space-3);
    font-size: var(--fs-sm);
    color: var(--c-alarm);
    background: rgba(var(--c-alarm-rgb), 0.1);
    border: 1px solid rgba(var(--c-alarm-rgb), 0.4);
    border-radius: var(--radius-sm);
  }

  .upd-actions { display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: center; }
  .upd-actions .quiet { margin-left: auto; background: transparent; border-color: transparent; color: var(--c-muted); }
  .upd-actions .quiet:hover { color: var(--c-text); border-color: var(--c-border); }
</style>
