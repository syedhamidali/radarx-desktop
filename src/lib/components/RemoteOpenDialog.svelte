<script lang="ts">
  /**
   * Open a file that lives on the remote engine's computer. The system file picker shows this computer's
   * disk, which the engine on the cluster cannot see, so the path on the cluster is typed instead.
   */
  import { fade } from 'svelte/transition';
  import { focusTrap } from '../utils/focusTrap';
  import { remoteEngine } from '../stores/remoteEngine';
  import { t } from '../i18n';

  let { onopen, onclose }: { onopen: (path: string) => void; onclose: () => void } = $props();

  let path = $state('');
  const trimmed = $derived(path.trim());

  function submit(e: Event) {
    e.preventDefault();
    if (!trimmed) return;
    onopen(trimmed);
    onclose();
  }

  function key(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.stopPropagation();
      onclose();
    }
  }
</script>

<svelte:window onkeydown={key} />

<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
<div class="scrim" onclick={(e) => e.target === e.currentTarget && onclose()} transition:fade={{ duration: 120 }}>
  <div class="dlg brackets-4" role="dialog" aria-modal="true" aria-labelledby="ro-title" use:focusTrap={{ initial: 'input[name=path]' }}>
    <h2 id="ro-title">{$t('remoteOpen.title')}</h2>
    <p class="lead">
      {$t('remoteOpen.leadBefore')} <strong>{$t('remoteOpen.leadWhere')}</strong>
      {#if $remoteEngine.hostPort}<span class="mono">{$t('remoteOpen.reachedAt', { hostPort: $remoteEngine.hostPort })}</span>{/if}.
      {$t('remoteOpen.leadAfter')}
    </p>
    <form onsubmit={submit}>
    <label class="field">
      <span>{$t('remoteOpen.pathLabel')}</span>
      <input name="path" type="text" bind:value={path} placeholder="/scratch/me/radar/KHPX20211211_023231_V06" spellcheck="false" autocapitalize="off" autocomplete="off" />
    </label>
    <div class="row">
      <button type="button" onclick={onclose}>{$t('remoteOpen.cancel')}</button>
      <button class="primary" type="submit" disabled={!trimmed}>{$t('remoteOpen.open')}</button>
    </div>
    </form>
  </div>
</div>

<style>
  .scrim { position: fixed; inset: 0; z-index: 950; background: var(--c-scrim); display: grid; place-items: center; overflow-y: auto; padding: 16px; }
  .dlg {
    width: 480px; max-width: 100%; background: var(--c-overlay); border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm); box-shadow: var(--shadow-3); padding: var(--space-5);
    display: flex; flex-direction: column; gap: var(--space-3);
  }
  h2 { margin: 0; font-size: 16px; font-weight: 600; }
  .lead { margin: 0; font-size: var(--fs-md); color: var(--c-text); line-height: 1.5; }
  code, .mono { font-family: var(--font-mono); font-size: var(--fs-sm); }
  .field { display: flex; flex-direction: column; gap: 4px; font-size: var(--fs-xs); color: var(--c-text-dim); }
  input {
    background: var(--c-field); border: 1px solid var(--c-border-strong); border-radius: var(--radius-sm);
    color: var(--c-text); padding: 8px 10px; font-family: var(--font-mono); font-size: var(--fs-sm); width: 100%; box-sizing: border-box; min-height: 36px;
  }
  form { display: flex; flex-direction: column; gap: var(--space-3); }
  .row { display: flex; justify-content: flex-end; gap: var(--space-2); }
</style>
