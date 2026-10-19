<script lang="ts">
  /**
   * The consent step of environment data. Shows, before anything is downloaded: where the data
   * comes from, how many files and how many bytes, where they will be stored, who to credit and
   * the licence. Only the "Download" button starts the transfer; closing the dialog downloads
   * nothing. While downloading it shows progress and a Cancel button; failures are written out.
   */
  import { fade, fly } from 'svelte/transition';
  import { focusTrap } from '../utils/focusTrap';
  import { envFlow, envStatus, approveEnvFetch, cancelEnvFlow, dismissEnvFlow } from '../stores/environment';
  import { needsDialog, sizeText, cachedText, progressText, hostsOf } from '../utils/envFlow';
  import ProvenanceChip from './ProvenanceChip.svelte';

  const flow = $derived($envFlow);
  const open = $derived(needsDialog(flow));
  const est = $derived(flow.estimate);
  const provider = $derived($envStatus?.providers.find((p) => p.id === est?.provider) ?? null);
  const hosts = $derived(est ? hostsOf(est) : []);
  const cached = $derived(est ? cachedText(est) : '');
  const downloading = $derived(flow.phase === 'downloading');
  const failed = $derived(flow.phase === 'error');
  const pct = $derived(flow.progress ? Math.max(0, Math.min(100, flow.progress.percent)) : null);

  $effect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      e.preventDefault();
      e.stopPropagation();
      cancelEnvFlow();
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  });
</script>

{#if open}
  <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
  <div class="env-overlay" transition:fade={{ duration: 150 }}>
    <div
      class="env-dialog brackets-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="env-title"
      data-testid="env-approval"
      transition:fly={{ y: 16, duration: 220 }}
      use:focusTrap={{ initial: '.env-primary' }}
    >
      <header>
        <h2 id="env-title">{failed ? 'This data cannot be fetched' : downloading ? 'Downloading' : 'Download data?'}</h2>
        {#if est}
          <div class="who">
            {#if provider}<ProvenanceChip provenance={{ kind: provider.kind as any, source: provider.name }} compact title={provider.name} />{/if}
            <span class="name">{est.provider_name}</span>
          </div>
        {/if}
      </header>

      {#if failed}
        <p class="error" role="alert">{flow.error}</p>
        {#if flow.code === 'offline'}<p class="note">Offline mode is on. Data already in the cache keeps working; switch offline mode off in the Sounding panel's data settings to download.</p>{/if}
      {/if}

      {#if est}
        <dl>
          <dt>From</dt>
          <dd>{#if hosts.length}<span class="mono">{hosts.join(', ')}</span>{:else}{est.source}{/if}</dd>
          <dt>Download</dt>
          <dd class="mono" data-testid="env-size">{sizeText(est)}</dd>
          {#if cached}<dt>Cached</dt><dd>{cached}</dd>{/if}
          <dt>Saved to</dt>
          <dd class="mono path" data-testid="env-destination">{est.destination}</dd>
          <dt>Credit</dt>
          <dd>{est.attribution || 'not stated'}</dd>
          <dt>Licence</dt>
          <dd>{est.licence || 'not stated'}</dd>
        </dl>
        {#if est.files.length}
          <details>
            <summary>{est.files.length} file{est.files.length === 1 ? '' : 's'}</summary>
            <ul class="mono">
              {#each est.files.slice(0, 40) as f (f.name)}
                <li class:cached={f.cached}>{f.name}{f.cached ? ' (cached)' : ''}</li>
              {/each}
              {#if est.files.length > 40}<li>... and {est.files.length - 40} more</li>{/if}
            </ul>
          </details>
        {/if}
        {#each est.notes as n (n)}<p class="note">{n}</p>{/each}
      {/if}

      {#if downloading}
        <div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow={pct ?? undefined} aria-label="Download progress">
          <div class="bar"><div class="fill" class:indeterminate={pct === null} style={pct !== null ? `width:${pct}%` : ''}></div></div>
          <div class="msg mono">{progressText(flow.progress)}</div>
        </div>
      {:else if !failed}
        <p class="promise">Nothing is downloaded until you press Download. Closing this window downloads nothing.</p>
      {/if}

      <footer>
        {#if failed}
          <button class="primary env-primary" onclick={dismissEnvFlow}>Close</button>
        {:else if downloading}
          <button class="env-primary" onclick={cancelEnvFlow}>Cancel download</button>
        {:else}
          <button class="primary env-primary" onclick={approveEnvFetch}>Download</button>
          <button onclick={cancelEnvFlow}>Cancel</button>
        {/if}
      </footer>
    </div>
  </div>
{/if}

<style>
  .env-overlay {
    position: fixed;
    inset: 0;
    z-index: 955;
    background: var(--c-scrim);
    display: flex;
    align-items: center;
    justify-content: center;
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
  }
  .env-dialog {
    width: 540px;
    max-width: 92vw;
    max-height: 88vh;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    padding: var(--space-5);
    background: var(--c-overlay);
    border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm);
    box-shadow: var(--shadow-3);
  }
  h2 { margin: 0; font-size: var(--fs-lg); font-weight: 600; color: var(--c-text); }
  .who { display: flex; align-items: center; gap: var(--space-2); margin-top: 4px; color: var(--c-text-dim); font-size: var(--fs-sm); }
  dl { display: grid; grid-template-columns: max-content 1fr; gap: 6px var(--space-3); margin: 0; font-size: var(--fs-sm); }
  dt { color: var(--c-muted); text-transform: uppercase; letter-spacing: var(--ls-label); font-size: 10.5px; padding-top: 2px; }
  dd { margin: 0; color: var(--c-text); overflow-wrap: anywhere; }
  .mono { font-family: var(--font-mono); font-size: 12px; }
  .path { word-break: break-all; }
  details { font-size: var(--fs-xs); color: var(--c-text-dim); }
  summary { cursor: pointer; }
  ul { margin: 4px 0 0; padding-left: 16px; max-height: 120px; overflow-y: auto; }
  li.cached { color: var(--c-muted); }
  .note, .promise { margin: 0; font-size: var(--fs-xs); color: var(--c-text-dim); line-height: 1.5; }
  .promise { color: var(--c-text); }
  .error {
    margin: 0;
    padding: var(--space-2) var(--space-3);
    font-size: var(--fs-sm);
    color: var(--c-alarm);
    background: rgba(var(--c-alarm-rgb), 0.1);
    border: 1px solid rgba(var(--c-alarm-rgb), 0.4);
    border-radius: var(--radius-sm);
  }
  .progress .bar { height: 6px; background: rgba(var(--c-accent-rgb), 0.12); border-radius: 3px; overflow: hidden; }
  .progress .fill { height: 100%; background: var(--c-accent); transition: width var(--dur-base) var(--ease-in-out); }
  .progress .fill.indeterminate { width: 35%; animation: env-slide 1.2s var(--ease-in-out) infinite; }
  @keyframes env-slide { from { margin-left: -35%; } to { margin-left: 100%; } }
  .progress .msg { margin-top: 4px; font-size: var(--fs-xs); color: var(--c-text-dim); }
  @media (prefers-reduced-motion: reduce) {
    .progress .fill.indeterminate { animation: none; width: 100%; opacity: 0.5; }
  }
  footer { display: flex; gap: var(--space-2); flex-wrap: wrap; }
</style>
