<script lang="ts">
  /** Settings > Updates: version, check now, last checked, the two toggles, skipped version. */
  import { settings } from '../stores/settings';
  import { updater, visibleUpdate } from '../stores/updater';
  import { checkForUpdates, openUpdateDialog, openExternal } from '../utils/updater';

  const u = $derived($updater);
  const prefs = $derived($settings.updates);
  const update = $derived($visibleUpdate);
  const configured = $derived(!!u.status?.configured);
  const manual = $derived(u.status?.install.mode === 'manual');
  const checking = $derived(u.phase === 'checking');
  const working = $derived(u.phase === 'downloading' || u.phase === 'installing');

  const lastChecked = $derived.by(() => {
    if (!prefs.lastCheckedAt) return 'Never';
    const d = new Date(prefs.lastCheckedAt);
    return Number.isNaN(d.getTime())
      ? 'Never'
      : d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  });

  function setCheckOnLaunch(e: Event) {
    settings.updateSetting('updates', { checkOnLaunch: (e.currentTarget as HTMLInputElement).checked });
  }
  function setBackground(e: Event) {
    settings.updateSetting('updates', { downloadInBackground: (e.currentTarget as HTMLInputElement).checked });
  }
  function unskip() {
    settings.updateSetting('updates', { skippedVersion: '' });
  }
</script>

<div class="upd-settings">
  <h3>Updates</h3>

  {#if !u.loaded}
    <p class="hint">Reading the update status...</p>
  {:else if !configured}
    <p class="hint">Updates are not configured in this build.</p>
    <p class="hint small">This is normal for a development or self-built copy. Install a release from the download page to get updates.</p>
  {:else}
    <div class="row">
      <span class="label">Current version</span>
      <span class="value mono">v{u.status?.currentVersion}</span>
    </div>
    <div class="row">
      <span class="label">Channel</span>
      <span class="value">Stable releases</span>
    </div>
    <div class="row">
      <span class="label">Last checked</span>
      <span class="value">{lastChecked}</span>
    </div>

    <div class="row">
      <span class="label">Status</span>
      <span class="value status" aria-live="polite">
        {#if checking}
          Checking...
        {:else if update}
          Update available: <strong>v{update.version}</strong>
        {:else if u.lastResult === 'failed'}
          <span class="fail">{u.checkError ?? 'Could not check for updates.'}</span>
        {:else if u.lastResult === 'up-to-date'}
          You are up to date.
        {:else if prefs.skippedVersion && u.update}
          v{u.update.version} is available (skipped)
        {:else}
          Not checked yet in this session.
        {/if}
      </span>
    </div>

    <div class="buttons">
      <button disabled={checking || working} onclick={() => checkForUpdates(true)}>{checking ? 'Checking...' : 'Check now'}</button>
      {#if update}
        <button class="primary" onclick={openUpdateDialog}>{manual ? 'See what is new' : 'View update'}</button>
      {/if}
    </div>

    {#if manual}
      <p class="hint small">{u.status?.install.note}</p>
      {#if u.status?.releasesUrl}
        <p class="hint small"><button class="link" onclick={() => openExternal(u.status!.releasesUrl!)}>Open the download page</button></p>
      {/if}
    {/if}

    <div class="row">
      <label class="label" for="upd-launch">Check automatically on launch</label>
      <label class="toggle">
        <input id="upd-launch" type="checkbox" checked={prefs.checkOnLaunch} onchange={setCheckOnLaunch} />
        <span>{prefs.checkOnLaunch ? 'On' : 'Off'}</span>
      </label>
    </div>
    <p class="hint small">At most once every 24 hours. It only asks the release server whether a newer version exists.</p>

    <div class="row">
      <label class="label" for="upd-bg">Download in the background</label>
      <label class="toggle">
        <input id="upd-bg" type="checkbox" checked={prefs.downloadInBackground} disabled={manual} onchange={setBackground} />
        <span>{prefs.downloadInBackground && !manual ? 'On' : 'Off'}</span>
      </label>
    </div>
    <p class="hint small">
      Off (default): the app asks first. On: a new version is downloaded quietly, then you are asked to restart.
      The restart never interrupts a running export, pipeline, module, mosaic or wind job.
    </p>

    {#if prefs.skippedVersion}
      <div class="row">
        <span class="label">Skipped version</span>
        <span class="value"><span class="mono">v{prefs.skippedVersion}</span> <button class="link" onclick={unskip}>Offer it again</button></span>
      </div>
    {/if}
  {/if}
</div>

<style>
  .upd-settings h3 {
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: var(--ls-label);
    color: var(--c-accent);
    margin: 0 0 var(--space-3);
  }
  .row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    min-height: 32px;
    padding: 2px 0;
  }
  .label { font-size: 12px; color: var(--c-text-dim); flex-shrink: 0; min-width: 150px; }
  .value { font-size: 12px; color: var(--c-text); text-align: right; min-width: 0; overflow-wrap: anywhere; }
  .value strong { color: var(--c-accent); }
  .fail { color: var(--c-caution); }
  .buttons { display: flex; gap: var(--space-2); margin: var(--space-2) 0 var(--space-3); }
  .hint { font-size: 12px; color: var(--c-muted); margin: 0 0 var(--space-2); line-height: 1.5; }
  .hint.small { font-size: 11px; margin: 0 0 var(--space-3); }
  .toggle { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--c-text); cursor: pointer; }
  .link {
    height: auto;
    padding: 0;
    background: none;
    border: none;
    color: var(--c-accent);
    text-decoration: underline;
    text-underline-offset: 2px;
    font-size: inherit;
  }
  .link:hover { background: none; color: var(--c-accent-strong); }
</style>
