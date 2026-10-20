<script lang="ts">
  import { onMount } from 'svelte';
  import { listApiTokens, createApiToken, revokeApiToken } from '../stores/auth';
  import {
    DEFAULT_DAYS, EXPIRY_CHOICES, USAGE, createProblem, daysLeft, expiresSoon, friendlyTokenError, hintText, nameProblem, tokenStatus,
    type ApiTokenRow,
  } from '../auth/apiTokens';
  import { addToast } from '../stores/toasts';

  let rows: ApiTokenRow[] = $state([]);
  let loaded = $state(false);
  let loadError = $state('');
  let name = $state('');
  let days = $state(DEFAULT_DAYS);
  let busy = $state(false);
  let error = $state('');
  /** The new token, shown once. It lives in this variable only and is dropped when the panel is closed. */
  let fresh: { id: string; token: string; name: string; expires_at: string } | null = $state(null);
  let copied = $state(false);
  let confirming: string | null = $state(null);
  let tokenField: HTMLTextAreaElement | undefined = $state();
  const now = $derived(Date.now());

  const problem = $derived(createProblem(rows, now));
  const nameIssue = $derived(name.trim() ? nameProblem(name) : null);

  async function load() {
    try {
      rows = await listApiTokens();
      loadError = '';
    } catch (e) {
      loadError = friendlyTokenError(e);
    }
    loaded = true;
  }
  onMount(load);

  async function create() {
    error = '';
    const issue = nameProblem(name);
    if (issue) return void (error = issue);
    if (problem) return void (error = problem);
    busy = true;
    try {
      fresh = await createApiToken(name, days);
      copied = false;
      name = '';
      await load();
      queueMicrotask(() => tokenField?.select());
    } catch (e) {
      error = friendlyTokenError(e);
    } finally {
      busy = false;
    }
  }

  async function copy() {
    if (!fresh) return;
    try {
      await navigator.clipboard.writeText(fresh.token);
      copied = true;
      addToast('success', 'Token copied. Paste it where the job can read it.');
    } catch {
      tokenField?.select(); // no clipboard permission: the text is selected, copy it by hand
      addToast('info', 'Press Ctrl+C (Command+C on a Mac) to copy the selected token.');
    }
  }

  async function revoke(id: string) {
    confirming = null;
    busy = true;
    error = '';
    try {
      await revokeApiToken(id);
      await load();
      addToast('success', 'Token revoked. It stops working within the hour.');
    } catch (e) {
      error = friendlyTokenError(e);
    } finally {
      busy = false;
    }
  }

  const day = (iso: string) => new Date(iso).toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' });
  const used = (iso: string | null) => (iso ? day(iso) : 'never');
</script>

<h3>Access tokens</h3>
<p class="hint">
  For batch jobs, cron, CI and clusters that cannot type a password. Each token signs in as you, so treat it like a password; revoke it when it is no longer needed.
</p>

{#if fresh}
  <div class="fresh" role="alert">
    <strong>Copy this token now: it is shown only once.</strong>
    <div class="tokrow">
      <textarea bind:this={tokenField} class="tok" readonly rows="2" spellcheck="false" aria-label="New access token" onfocus={(e) => e.currentTarget.select()}>{fresh.token}</textarea>
      <button class="primary" onclick={copy}>{copied ? 'Copied' : 'Copy'}</button>
    </div>
    <p class="hint">
      "{fresh.name}" works until {day(fresh.expires_at)}. We keep only a fingerprint, so a lost token cannot be shown again: make a new one.
      Anyone who has it can use your account's radarx features, so do not paste it into chats or commit it to a repository.
    </p>
    <ul class="how" aria-label="How to use it">
      {#each USAGE as u (u.cmd)}<li><code>{u.cmd}</code><small>{u.note}</small></li>{/each}
    </ul>
    <div class="row"><button onclick={() => { fresh = null; copied = false; }}>I have saved it</button></div>
  </div>
{/if}

<div class="make">
  <label class="grow"><span>Name</span><input bind:value={name} maxlength="64" placeholder="nightly job" autocomplete="off" aria-invalid={!!nameIssue} /></label>
  <label>
    <span>Expires after</span>
    <select bind:value={days}>{#each EXPIRY_CHOICES as c (c.days)}<option value={c.days}>{c.label}</option>{/each}</select>
  </label>
  <button class="primary" onclick={create} disabled={busy || !name.trim() || !!nameIssue || !!problem}>Create token</button>
</div>
{#if nameIssue}<p class="err" role="alert">{nameIssue}</p>{:else if problem}<p class="hint">{problem}</p>{/if}
{#if error}<p class="err" role="alert">{error}</p>{/if}

{#if loadError}
  <p class="hint">{loadError}</p>
{:else if !loaded}
  <p class="hint">Loading...</p>
{:else if rows.length === 0}
  <p class="hint">No tokens yet.</p>
{:else}
  <ul class="tokens" aria-label="Access tokens">
    {#each rows as r (r.id)}
      {@const status = tokenStatus(r, now)}
      <li class:off={status !== 'active'}>
        <div class="line">
          <span class="nm">{r.name}</span>
          <span class="badge {status}">{status}</span>
        </div>
        <small class="meta">
          <code>{hintText(r)}</code> · created {day(r.created_at)} ·
          {#if status === 'active'}expires {day(r.expires_at)} ({daysLeft(r.expires_at, now)} d){:else if status === 'expired'}expired {day(r.expires_at)}{:else}revoked {day(r.revoked_at ?? r.expires_at)}{/if}
          · last used {used(r.last_used_at)}
        </small>
        {#if expiresSoon(r, now)}<small class="soon">Ends soon: make a new token before it stops working.</small>{/if}
        {#if status === 'active'}
          {#if confirming === r.id}
            <span class="confirm">
              Jobs using it stop within the hour.
              <button class="danger-btn" disabled={busy} onclick={() => revoke(r.id)}>Revoke</button>
              <button class="linkish" onclick={() => (confirming = null)}>Cancel</button>
            </span>
          {:else}
            <button class="linkish danger" onclick={() => (confirming = r.id)}>Revoke</button>
          {/if}
        {/if}
      </li>
    {/each}
  </ul>
{/if}

<style>
  h3 { margin: var(--space-2) 0 0; font-size: var(--fs-xs); letter-spacing: 0.08em; text-transform: uppercase; color: var(--c-muted); }
  .hint { margin: 0; font-size: var(--fs-xs); color: var(--c-muted); line-height: 1.5; }
  .err { color: var(--c-alarm); font-size: var(--fs-xs); margin: 0; }
  label { display: flex; flex-direction: column; align-items: stretch; gap: 4px; font-size: var(--fs-xs); color: var(--c-text-dim); text-align: left; }
  input, select, textarea {
    background: var(--c-field); border: 1px solid var(--c-border-strong); border-radius: var(--radius-sm);
    color: var(--c-text); padding: 8px 10px; font: inherit; font-size: var(--fs-md); width: 100%; box-sizing: border-box; min-height: 36px;
  }
  input[aria-invalid='true'] { border-color: var(--c-alarm); }
  .make { display: flex; gap: var(--space-2); align-items: flex-end; flex-wrap: wrap; }
  .make .grow { flex: 1 1 140px; }
  .make select { width: auto; }
  .fresh {
    display: flex; flex-direction: column; gap: var(--space-2); padding: var(--space-3);
    border: 1px solid rgba(var(--c-caution-rgb), 0.6); background: rgba(var(--c-caution-rgb), 0.08); border-radius: var(--radius-sm);
  }
  .fresh strong { font-size: var(--fs-sm); color: var(--c-text); }
  .tokrow { display: flex; gap: var(--space-2); }
  .tok { font-family: var(--font-mono); font-size: var(--fs-xs); resize: none; word-break: break-all; line-height: 1.4; }
  ul.how { list-style: none; margin: 0; padding: var(--space-2); background: var(--c-field); border: 1px solid var(--c-border); border-radius: var(--radius-sm); display: flex; flex-direction: column; gap: 6px; }
  ul.how li { display: flex; flex-direction: column; align-items: flex-start; gap: 0; }
  ul.how code { font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-text); word-break: break-all; }
  ul.how small { color: var(--c-muted); font-size: var(--fs-2xs); }
  .row { display: flex; justify-content: flex-end; gap: var(--space-2); }
  ul.tokens { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
  ul.tokens li { display: flex; flex-direction: column; align-items: flex-start; gap: 2px; padding: var(--space-2); border: 1px solid var(--c-border); border-radius: var(--radius-sm); font-size: var(--fs-sm); }
  ul.tokens li.off { opacity: 0.65; }
  .line { display: flex; gap: var(--space-2); align-items: center; width: 100%; justify-content: space-between; }
  .nm { font-weight: 600; overflow-wrap: anywhere; }
  .meta { color: var(--c-muted); font-size: var(--fs-2xs); line-height: 1.5; }
  .meta code { font-family: var(--font-mono); }
  .soon { color: var(--c-caution); font-size: var(--fs-2xs); }
  .badge { font-size: var(--fs-2xs); letter-spacing: 0.06em; text-transform: uppercase; padding: 1px 6px; border-radius: var(--radius-sm); border: 1px solid var(--c-border-strong); color: var(--c-text-dim); }
  .badge.active { color: var(--c-nominal); border-color: rgba(var(--c-nominal-rgb), 0.5); }
  .badge.revoked { color: var(--c-alarm); border-color: rgba(var(--c-alarm-rgb), 0.5); }
  .confirm { display: inline-flex; gap: var(--space-2); align-items: center; flex-wrap: wrap; font-size: var(--fs-xs); color: var(--c-text-dim); }
  .linkish { background: none; border: none; padding: 0; color: var(--c-accent); cursor: pointer; font: inherit; font-size: var(--fs-xs); }
  .linkish.danger { color: var(--c-alarm); }
  .danger-btn { background: var(--c-alarm); color: var(--c-on-accent); border: none; border-radius: var(--radius-sm); padding: 4px 10px; cursor: pointer; font: inherit; font-size: var(--fs-xs); }
  .danger-btn:disabled { opacity: 0.4; cursor: default; }
</style>
