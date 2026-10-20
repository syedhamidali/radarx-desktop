<script lang="ts">
  /**
   * Report an issue (docs/REPORTING.md): a bug, an idea or a question, sent to the radarx team with the signed-in
   * account. The optional diagnostics are OFF by default, scrubbed, and shown in full before anything is sent.
   * All decisions (limits, payload, scrubbing, error sentences) live in utils/reportIssue.ts and reportScrub.ts.
   */
  import { onMount } from 'svelte';
  import { fade } from 'svelte/transition';
  import { focusTrap } from '../utils/focusTrap';
  import { inTauri } from '../utils/env';
  import { engineLogLines } from '../utils/engineLog';
  import { KINDS, LIMITS, buildPayload, checkDraft, cpLength, reportUrl, submitReport, systemName, type Environment, type FailureKind } from '../utils/reportIssue';
  import type { ScrubContext } from '../utils/reportScrub';
  import { accessToken } from '../auth/accessToken';
  import { SUPABASE_URL, SUPABASE_KEY } from '../auth/config';
  import { reportDraft, clearReportDraft } from '../stores/reportIssue';
  import { addToast } from '../stores/toasts';

  let { onclose }: { onclose: () => void } = $props();

  // the draft lives in a store (memory only) so a failed send, a closed dialog or a reopened one keeps the text
  let kind = $state($reportDraft.kind);
  let title = $state($reportDraft.title);
  let description = $state($reportDraft.description);
  let includeDiagnostics = $state($reportDraft.includeDiagnostics);
  $effect(() => {
    reportDraft.set({ kind, title, description, includeDiagnostics });
  });

  let env: Environment = $state({ appVersion: __APP_VERSION__, os: 'Unknown system', engineVersion: null, engineLines: [] });
  let ctx: ScrubContext = $state({});
  let phase: 'form' | 'sending' | 'sent' = $state('form');
  let sentId = $state('');
  let failure = $state<{ kind: FailureKind; message: string; field?: string } | null>(null);
  let showProblems = $state(false);

  const draft = $derived({ kind, title, description, includeDiagnostics });
  const problems = $derived(checkDraft(draft));
  const payload = $derived(buildPayload(draft, env, ctx));
  const preview = $derived(JSON.stringify(payload, null, 2));
  const titleLen = $derived(cpLength(title.trim()));
  const descLen = $derived(cpLength(description.trim()));
  const titleError = $derived((showProblems && problems.title) || (failure?.field === 'title' ? failure.message : ''));
  const descError = $derived((showProblems && problems.description) || (failure?.field === 'description' ? failure.message : ''));

  onMount(async () => {
    let arch: string | null = null;
    try {
      const hv = await (navigator as any).userAgentData?.getHighEntropyValues?.(['architecture']);
      arch = typeof hv?.architecture === 'string' && hv.architecture ? hv.architecture : null;
    } catch {
      /* not offered by this webview */
    }
    env = {
      appVersion: __APP_VERSION__,
      os: systemName({ userAgent: navigator.userAgent, arch, desktop: inTauri() }),
      engineVersion: null, // the engine does not report a version to the app today
      engineLines: engineLogLines(),
    };
    if (inTauri()) {
      try {
        const { homeDir } = await import('@tauri-apps/api/path');
        const home = await homeDir();
        ctx = { home, user: home.split(/[\\/]+/).filter(Boolean).pop() ?? null };
      } catch {
        /* without the home folder only the built-in patterns apply */
      }
    }
  });

  async function send() {
    showProblems = true;
    failure = null;
    if (problems.title || problems.description) return;
    phase = 'sending';
    const token = await accessToken();
    const result = await submitReport({
      url: reportUrl(SUPABASE_URL),
      apiKey: SUPABASE_KEY,
      token,
      payload,
      fetchFn: (url, init) => fetch(url, init),
      isOnline: () => (typeof navigator === 'undefined' ? true : navigator.onLine),
    });
    if (result.ok) {
      sentId = result.id;
      phase = 'sent';
      clearReportDraft();
      return;
    }
    failure = result;
    phase = 'form'; // the dialog stays open with everything still typed
  }

  async function copyId() {
    try {
      await navigator.clipboard.writeText(sentId);
      addToast('success', 'Reference copied.');
    } catch {
      addToast('warning', 'Could not copy. Select the reference and copy it by hand.');
    }
  }

  function close() {
    if (phase !== 'sending') onclose(); // a report on its way is not abandoned
  }

  function key(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.stopPropagation();
      close();
    }
  }
</script>

<svelte:window onkeydown={key} />

<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
<div class="scrim" onclick={(e) => e.target === e.currentTarget && close()} transition:fade={{ duration: 120 }}>
  <div class="dlg brackets-4" role="dialog" aria-modal="true" aria-labelledby="ri-title" use:focusTrap={{ initial: 'input[name=title]' }}>
    {#if phase === 'sent'}
      <h2 id="ri-title">Report sent</h2>
      <p class="lead">Thank you. The radarx team will read it.</p>
      <p class="hint">Your reference, in case you need to ask about it:</p>
      <p class="ref"><code>{sentId}</code> <button class="linkish" onclick={copyId}>Copy</button></p>
      <div class="row"><button class="primary" onclick={onclose}>Close</button></div>
    {:else}
      <h2 id="ri-title">Report an issue</h2>

      <fieldset class="kinds">
        <legend>What is it?</legend>
        <div class="seg">
          {#each KINDS as k (k.id)}
            <label class:on={kind === k.id} title={k.hint}>
              <input type="radio" name="kind" value={k.id} bind:group={kind} />
              <span>{k.label}</span>
            </label>
          {/each}
        </div>
      </fieldset>

      <label class="field">
        <span>Title <small class="count" class:over={titleLen > LIMITS.title.max}>{titleLen}/{LIMITS.title.max}</small></span>
        <input name="title" bind:value={title} autocomplete="off" aria-invalid={!!titleError} aria-describedby={titleError ? 'ri-title-err' : undefined} placeholder="A few words" />
        {#if titleError}<em class="err" id="ri-title-err" role="alert">{titleError}</em>{/if}
      </label>

      <label class="field">
        <span>What happened? <small class="count" class:over={descLen > LIMITS.description.max}>{descLen}/{LIMITS.description.max}</small></span>
        <textarea rows="6" bind:value={description} aria-invalid={!!descError} aria-describedby={descError ? 'ri-desc-err' : undefined} placeholder="What you did, what you expected, what you saw instead"></textarea>
        {#if descError}<em class="err" id="ri-desc-err" role="alert">{descError}</em>{/if}
      </label>

      <label class="check">
        <input type="checkbox" bind:checked={includeDiagnostics} />
        <span>
          <strong>Include diagnostics</strong> (optional)
          <small>The engine version if known, and the last warnings and errors the engine gave this session. Folder paths, file names, e-mail addresses, keys and IP addresses are removed first.</small>
        </span>
      </label>

      <details class="preview">
        <summary>Show exactly what will be sent</summary>
        <pre aria-label="The report exactly as it will be sent">{preview}</pre>
        <p class="hint">Nothing else is sent with it, apart from your signed-in account, which tells us who wrote it.</p>
      </details>

      <p class="hint">
        No radar files, file names or locations are sent. What you type is sent as you wrote it, so leave out anything private.
        We may reply to the e-mail address of your account.
      </p>

      {#if failure && !failure.field}
        <p class="fail" role="alert">{failure.message}</p>
      {/if}

      <div class="row">
        <button onclick={close}>{failure ? 'Close, keep my text' : 'Cancel'}</button>
        <button class="primary" onclick={send} disabled={phase === 'sending'}>{phase === 'sending' ? 'Sending...' : failure ? 'Try again' : 'Send report'}</button>
      </div>
    {/if}
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
  .lead { margin: 0; font-size: var(--fs-md); color: var(--c-text); }
  fieldset { border: none; margin: 0; padding: 0; }
  legend { font-size: var(--fs-xs); color: var(--c-text-dim); padding: 0; margin-bottom: 4px; }
  .seg { display: grid; grid-template-columns: repeat(3, 1fr); border: 1px solid var(--c-border-strong); border-radius: var(--radius-sm); overflow: hidden; }
  .seg label { position: relative; display: flex; align-items: center; justify-content: center; text-align: center; padding: 8px 6px; font-size: var(--fs-sm); color: var(--c-text-dim); cursor: pointer; background: var(--c-field); border-left: 1px solid var(--c-border); }
  .seg label:first-child { border-left: none; }
  .seg label.on { background: rgba(var(--c-accent-rgb), 0.16); color: var(--c-text); box-shadow: inset 0 -2px 0 var(--c-accent); }
  .seg label:focus-within { outline: 2px solid var(--c-accent); outline-offset: -2px; }
  .seg input { position: absolute; opacity: 0; inset: 0; margin: 0; cursor: pointer; }
  .field { display: flex; flex-direction: column; align-items: stretch; gap: 4px; font-size: var(--fs-xs); color: var(--c-text-dim); text-align: left; }
  .field > span { display: flex; justify-content: space-between; align-items: baseline; gap: var(--space-2); }
  .count { color: var(--c-muted); font-variant-numeric: tabular-nums; }
  .count.over { color: var(--c-alarm); }
  input:not([type]), textarea {
    background: var(--c-field); border: 1px solid var(--c-border-strong); border-radius: var(--radius-sm);
    color: var(--c-text); padding: 8px 10px; font: inherit; font-size: var(--fs-md); width: 100%; box-sizing: border-box; min-height: 36px;
  }
  textarea { resize: vertical; line-height: 1.45; min-height: 110px; }
  input[aria-invalid='true'], textarea[aria-invalid='true'] { border-color: var(--c-alarm); }
  .err { color: var(--c-alarm); font-style: normal; font-size: var(--fs-xs); }
  .check { display: flex; gap: var(--space-2); align-items: flex-start; font-size: var(--fs-sm); color: var(--c-text); cursor: pointer; }
  .check input { margin-top: 3px; accent-color: var(--c-accent); }
  .check small { display: block; color: var(--c-muted); font-size: var(--fs-xs); line-height: 1.5; margin-top: 2px; }
  .preview { border: 1px solid var(--c-border); border-radius: var(--radius-sm); padding: var(--space-2) var(--space-3); }
  .preview summary { cursor: pointer; font-size: var(--fs-sm); color: var(--c-accent); }
  .preview summary:focus-visible { outline: 2px solid var(--c-accent); outline-offset: 2px; }
  .preview pre {
    margin: var(--space-2) 0; padding: var(--space-2); max-height: 220px; overflow: auto; background: var(--c-field);
    border: 1px solid var(--c-border); border-radius: var(--radius-sm); font-size: var(--fs-xs); line-height: 1.45;
    white-space: pre-wrap; overflow-wrap: anywhere; color: var(--c-text);
  }
  .hint { margin: 0; font-size: var(--fs-xs); color: var(--c-muted); line-height: 1.5; }
  .fail { margin: 0; padding: var(--space-2) var(--space-3); font-size: var(--fs-sm); color: var(--c-alarm); border: 1px solid rgba(var(--c-alarm-rgb), 0.4); border-radius: var(--radius-sm); background: rgba(var(--c-alarm-rgb), 0.08); }
  .ref { margin: 0; display: flex; gap: var(--space-2); align-items: center; font-size: var(--fs-sm); }
  .ref code { user-select: all; background: var(--c-field); border: 1px solid var(--c-border); border-radius: var(--radius-sm); padding: 4px 8px; font-size: var(--fs-xs); overflow-wrap: anywhere; }
  .row { display: flex; justify-content: flex-end; gap: var(--space-2); }
  .linkish { background: none; border: none; padding: 0; color: var(--c-accent); cursor: pointer; font: inherit; font-size: var(--fs-xs); }
</style>
