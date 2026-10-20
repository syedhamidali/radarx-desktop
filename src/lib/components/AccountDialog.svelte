<script lang="ts">
  import { onMount } from 'svelte';
  import { fade } from 'svelte/transition';
  import { auth, saveProfile, retryProfile, signOut, deleteAccount, listDevices, removeDevice, listActivity, deviceId, USE_CASES, type DeviceRow, type ActivityRow } from '../stores/auth';
  import { focusTrap } from '../utils/focusTrap';
  import { addToast } from '../stores/toasts';

  let { onclose }: { onclose: () => void } = $props();

  const a = $derived($auth);
  let name = $state('');
  let affiliation = $state('');
  let useCase = $state('research');
  let devices: DeviceRow[] = $state([]);
  let devicesError = $state(false);
  let activity: ActivityRow[] = $state([]);
  let activityLoaded = $state(false);
  let activityError = $state(false);
  let confirmText = $state('');
  let deleting = $state(false);
  const me = deviceId();

  onMount(async () => {
    name = a.profile?.display_name ?? '';
    affiliation = a.profile?.affiliation ?? '';
    useCase = a.profile?.use_case ?? 'research';
    try {
      devices = await listDevices();
    } catch {
      devicesError = true;
    }
    try {
      activity = await listActivity();
    } catch {
      activityError = true;
    }
    activityLoaded = true;
  });

  async function save() {
    if (await saveProfile({ display_name: name, affiliation, use_case: useCase })) addToast('success', 'Profile saved.');
  }
  async function drop(d: DeviceRow) {
    await removeDevice(d.id);
    devices = devices.filter((x) => x.id !== d.id);
  }
  async function wipe() {
    if (confirmText !== 'DELETE') return;
    if (await deleteAccount()) onclose();
  }
  const when = (iso: string) => new Date(iso).toLocaleDateString();
  const whenFull = (iso: string) => new Date(iso).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' });
  const EVENT_LABELS: Record<string, string> = {
    app_start: 'Started the app',
    sign_in: 'Signed in',
    sign_out: 'Signed out',
    command: 'Ran a command',
    module_run: 'Ran an algorithm',
    file_opened: 'Opened a file',
    export: 'Exported',
    mosaic_run: 'Made a mosaic',
    winds_run: 'Ran multi-Doppler winds',
    animate_run: 'Made an animation',
    qpe_run: 'Ran rain estimation',
    error: 'An error occurred',
  };
  const SURFACE_LABELS: Record<string, string> = { app: 'App', cli: 'Terminal', api: 'Python', engine: 'Engine' };
  const detailText = (d: Record<string, string> | null) => (d ? Object.values(d).join(', ') : '');
</script>

<svelte:window onkeydown={(e) => e.key === 'Escape' && (e.stopPropagation(), onclose())} />

<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
<div class="scrim" onclick={(e) => e.target === e.currentTarget && onclose()} transition:fade={{ duration: 120 }}>
  <div class="dlg brackets-4" role="dialog" aria-modal="true" aria-label="Account" use:focusTrap={{ initial: 'input' }}>
    <h2>Your account</h2>
    <p class="mail">{a.email ?? ''}</p>
    {#if a.profileState === 'failed'}
      <p class="hint" role="status">
        Your profile could not be loaded (no connection or the server is busy).
        <button class="linkish" onclick={() => retryProfile()}>Try again</button>
      </p>
    {/if}

    <label><span>Name</span><input bind:value={name} autocomplete="name" /></label>
    <label><span>Affiliation</span><input bind:value={affiliation} autocomplete="organization" /></label>
    <label>
      <span>Mostly used for</span>
      <select bind:value={useCase}>{#each USE_CASES as u (u.id)}<option value={u.id}>{u.label}</option>{/each}</select>
    </label>
    {#if a.error}<p class="err" role="alert">{a.error}</p>{/if}
    <div class="row"><button class="primary" onclick={save} disabled={a.busy}>Save</button></div>

    <h3>Your computers</h3>
    {#if devicesError}
      <p class="hint">Cannot load the list right now.</p>
    {:else if devices.length === 0}
      <p class="hint">None recorded yet.</p>
    {:else}
      <ul>
        {#each devices as d (d.id)}
          <li>
            <span>{d.name ?? 'Computer'}{#if d.device_id === me} <em>(this one)</em>{/if}<small> · last used {when(d.last_seen)}</small></span>
            {#if d.device_id !== me}<button class="linkish" onclick={() => drop(d)}>Remove</button>{/if}
          </li>
        {/each}
      </ul>
    {/if}

    <h3>Activity</h3>
    <p class="hint">
      The features this account has used, exactly as sent (newest first, last 100). Never file names, places, radar sites or your data.
    </p>
    {#if activityError}
      <p class="hint">Cannot load the list right now.</p>
    {:else if !activityLoaded}
      <p class="hint">Loading...</p>
    {:else if activity.length === 0}
      <p class="hint">Nothing recorded yet.</p>
    {:else}
      <ul class="activity" aria-label="Recent activity">
        {#each activity as r (r.id)}
          <li>
            <span class="what">{EVENT_LABELS[r.event] ?? r.event}{#if detailText(r.detail)}<small>: {detailText(r.detail)}</small>{/if}</span>
            <small class="meta">{SURFACE_LABELS[r.surface] ?? r.surface} · {whenFull(r.occurred_at)}</small>
          </li>
        {/each}
      </ul>
    {/if}

    <h3>Data we keep</h3>
    <p class="hint">Your name, e-mail, affiliation, what you use radarx for, the computers listed above and the activity above. Radar files and results stay on your machine.</p>

    <div class="row between">
      <button onclick={async () => { await signOut(); onclose(); }}>Sign out</button>
      <button class="linkish danger" onclick={() => (deleting = !deleting)}>Delete my account</button>
    </div>
    {#if deleting}
      <div class="del">
        <p class="hint">This removes your account and everything above, and cannot be undone. Type <strong>DELETE</strong> to confirm.</p>
        <input bind:value={confirmText} aria-label="Type DELETE to confirm" />
        <button class="danger-btn" disabled={confirmText !== 'DELETE' || a.busy} onclick={wipe}>Delete permanently</button>
      </div>
    {/if}
    <div class="row"><button onclick={onclose}>Close</button></div>
  </div>
</div>

<style>
  .scrim { position: fixed; inset: 0; z-index: 950; background: var(--c-scrim); display: grid; place-items: center; overflow-y: auto; padding: 16px; }
  .dlg {
    width: 440px; max-width: 100%; background: var(--c-overlay); border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm); box-shadow: var(--shadow-3); padding: var(--space-5);
    display: flex; flex-direction: column; gap: var(--space-3);
  }
  h2 { margin: 0; font-size: 16px; font-weight: 600; }
  h3 { margin: var(--space-2) 0 0; font-size: var(--fs-xs); letter-spacing: 0.08em; text-transform: uppercase; color: var(--c-muted); }
  .mail { margin: 0; color: var(--c-text-dim); font-size: var(--fs-sm); }
  label { display: flex; flex-direction: column; align-items: stretch; gap: 4px; font-size: var(--fs-xs); color: var(--c-text-dim); text-align: left; }
  input, select {
    background: var(--c-field); border: 1px solid var(--c-border-strong); border-radius: var(--radius-sm);
    color: var(--c-text); padding: 8px 10px; font: inherit; font-size: var(--fs-md); width: 100%; box-sizing: border-box; min-height: 36px;
  }
  ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
  li { display: flex; justify-content: space-between; gap: var(--space-2); font-size: var(--fs-sm); }
  li small { color: var(--c-muted); }
  ul.activity { max-height: 180px; overflow-y: auto; border: 1px solid var(--c-border); border-radius: var(--radius-sm); padding: var(--space-2); gap: 4px; }
  ul.activity li { flex-direction: column; gap: 0; font-size: var(--fs-xs); }
  ul.activity .meta { font-size: var(--fs-2xs); }
  .hint { margin: 0; font-size: var(--fs-xs); color: var(--c-muted); line-height: 1.5; }
  .err { color: var(--c-alarm); font-size: var(--fs-xs); margin: 0; }
  .row { display: flex; justify-content: flex-end; gap: var(--space-2); }
  .row.between { justify-content: space-between; align-items: center; }
  .linkish { background: none; border: none; padding: 0; color: var(--c-accent); cursor: pointer; font: inherit; font-size: var(--fs-xs); }
  .linkish.danger { color: var(--c-alarm); }
  .del { display: flex; flex-direction: column; gap: var(--space-2); border: 1px solid rgba(var(--c-alarm-rgb), 0.4); border-radius: var(--radius-sm); padding: var(--space-3); }
  .danger-btn { background: var(--c-alarm); color: var(--c-on-accent); border: none; border-radius: var(--radius-sm); padding: 8px; cursor: pointer; }
  .danger-btn:disabled { opacity: 0.4; cursor: default; }
</style>
