<script lang="ts">
  /**
   * After an image or movie is saved, a quiet bar offers "Share". The dialog opens each network's
   * own compose page with the caption filled in (nothing is posted for the user, and no file leaves
   * the machine until they attach it themselves). See utils/share.ts.
   */
  import { onMount } from 'svelte';
  import { fade } from 'svelte/transition';
  import { wsManager } from '../utils/websocket';
  import { inTauri } from '../utils/env';
  import { addToast } from '../stores/toasts';
  import { focusTrap } from '../utils/focusTrap';
  import {
    shareKind, defaultCaption, composeUrl, instagramAdvice, isAllowedShareUrl,
    TARGET_LABEL, type ShareKind, type ShareTarget,
  } from '../utils/share';

  let saved = $state<{ path: string; kind: ShareKind } | null>(null);
  let dialog = $state(false);
  let caption = $state('');
  let timer: ReturnType<typeof setTimeout> | undefined;

  const name = $derived(saved ? saved.path.split(/[\\/]/).pop() : '');
  const targets: ShareTarget[] = ['x', 'facebook', 'linkedin', 'bluesky'];

  onMount(() => {
    const off = wsManager.onMessage('file_saved', (m: any) => {
      const path = typeof m?.path === 'string' ? m.path : '';
      const kind = shareKind(path);
      if (!kind) return;
      saved = { path, kind };
      caption = defaultCaption(kind);
      clearTimeout(timer);
      timer = setTimeout(() => { if (!dialog) saved = null; }, 20000);
    });
    return () => { off(); clearTimeout(timer); };
  });

  async function openUrl(url: string) {
    if (!isAllowedShareUrl(url)) return;
    try {
      const { open } = await import('@tauri-apps/plugin-shell');
      await open(url);
    } catch {
      window.open(url, '_blank', 'noopener');
    }
  }

  async function reveal() {
    if (!saved || !inTauri()) return;
    try {
      const { invoke } = await import('@tauri-apps/api/core');
      await invoke('reveal_in_folder', { path: saved.path });
    } catch (e: any) {
      addToast('warning', typeof e === 'string' ? e : 'Could not show the file.');
    }
  }

  async function copyCaption(): Promise<boolean> {
    try {
      await navigator.clipboard.writeText(caption);
      return true;
    } catch {
      return false;
    }
  }

  async function share(t: ShareTarget) {
    await openUrl(composeUrl(t, caption));
    await reveal();
    addToast('info', `Attach ${name} in the ${TARGET_LABEL[t]} window: links cannot attach files.`, 9000);
  }

  async function instagram() {
    const ok = await copyCaption();
    await reveal();
    await openUrl('https://www.instagram.com/');
    addToast('info', ok ? 'Caption copied. Upload the saved file in Instagram.' : 'Upload the saved file in Instagram.', 9000);
  }

  function close() { dialog = false; saved = null; }
  function key(e: KeyboardEvent) {
    if (dialog && e.key === 'Escape') { close(); e.stopPropagation(); }
  }
</script>

<svelte:window onkeydown={key} />

{#if saved && !dialog}
  <div class="offer" transition:fade={{ duration: 150 }} role="status">
    <span class="file" title={saved.path}>Saved {name}</span>
    <button class="primary" onclick={() => (dialog = true)}>Share</button>
    <button class="x" aria-label="Dismiss" onclick={() => (saved = null)}>&times;</button>
  </div>
{/if}

{#if saved && dialog}
  <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
  <div class="scrim" onclick={(e) => e.target === e.currentTarget && close()} transition:fade={{ duration: 120 }}>
    <div class="dlg brackets-4" role="dialog" aria-modal="true" aria-label="Share" use:focusTrap={{ initial: 'textarea' }}>
      <h2>Share {name}</h2>
      <label>
        <span>Caption</span>
        <textarea rows="3" bind:value={caption}></textarea>
      </label>
      <div class="grid">
        {#each targets as t (t)}
          <button onclick={() => share(t)}>{TARGET_LABEL[t]}</button>
        {/each}
        <button onclick={instagram}>Instagram</button>
      </div>
      <p class="hint">
        Each button opens that network with the caption filled in{inTauri() ? ' and shows the saved file, ready to attach' : ''}. radarx never posts for you.
        <br />{instagramAdvice(saved.kind)}
      </p>
      <p class="hint">Before you share: say which data and radar you used, and that it is research output, not an official warning.</p>
      <div class="row">
        {#if inTauri()}<button onclick={reveal}>Show file</button>{/if}
        <button onclick={async () => addToast((await copyCaption()) ? 'success' : 'warning', (await copyCaption()) ? 'Caption copied.' : 'Could not copy.')}>Copy caption</button>
        <button class="primary" onclick={close}>Done</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .offer {
    position: fixed; right: 16px; bottom: 44px; z-index: 900;
    display: flex; align-items: center; gap: var(--space-2);
    background: var(--c-overlay); border: 1px solid var(--c-border-strong); border-radius: var(--radius-sm);
    padding: 6px 8px 6px 12px; box-shadow: var(--shadow-3); font-size: var(--fs-sm);
  }
  .file { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--c-text-dim); }
  .x { background: none; border: none; color: var(--c-muted); font-size: 18px; cursor: pointer; padding: 0 4px; }
  .scrim { position: fixed; inset: 0; z-index: 950; background: var(--c-scrim); display: grid; place-items: center; }
  .dlg {
    width: 440px; max-width: 92vw; background: var(--c-overlay); border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm); box-shadow: var(--shadow-3); padding: var(--space-5);
    display: flex; flex-direction: column; gap: var(--space-3);
  }
  h2 { margin: 0; font-size: 16px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; }
  label { display: flex; flex-direction: column; gap: 4px; font-size: var(--fs-xs); color: var(--c-text-dim); }
  textarea {
    background: var(--c-field); border: 1px solid var(--c-border-strong); border-radius: var(--radius-sm);
    color: var(--c-text); padding: 8px 10px; font: inherit; font-size: var(--fs-md); resize: vertical; width: 100%; box-sizing: border-box;
  }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-2); }
  .hint { margin: 0; font-size: var(--fs-xs); color: var(--c-muted); line-height: 1.5; }
  .row { display: flex; justify-content: flex-end; gap: var(--space-2); }
</style>
