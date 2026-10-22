<script lang="ts">
  /**
   * Settings > Engine: use the app's own engine, or connect to a remote one (`rxd serve` on a cluster,
   * through `ssh -L`). The token is typed here, handed to the connection and cleared from this field at
   * once; it is never stored (no settings file, no localStorage), never shown again, never put in a URL.
   */
  import { remoteEngine } from '../stores/remoteEngine';
  import { parseRemoteAddress, cleanToken, remoteStatusText } from '../utils/remoteEngine';
  import { wsManager } from '../utils/websocket';

  let address = $state('127.0.0.1:');
  let token = $state('');
  let touched = $state(false);

  const st = $derived($remoteEngine);
  const check = $derived(parseRemoteAddress(address));
  const tokenOk = $derived(cleanToken(token) !== null);
  const ready = $derived(check.ok && tokenOk);
  const remote = $derived(st.mode === 'remote');
  const problem = $derived(st.phase === 'refused' || st.phase === 'unreachable' || st.phase === 'ended' || st.phase === 'busy');

  function connect(e: Event) {
    e.preventDefault();
    touched = true;
    const t = cleanToken(token);
    if (!check.ok || !t) return;
    wsManager.connectRemoteEngine(check.value, t);
    token = ''; // the field never keeps it
    touched = false;
  }

  function back() {
    token = '';
    void wsManager.useBuiltinEngine();
  }
</script>

<div class="engine-settings">
  <h3>Engine</h3>

  <div class="row">
    <span class="label">In use</span>
    <span class="value" class:remote data-testid="engine-in-use">
      {#if remote}
        <strong>{remoteStatusText(st.phase, st.hostPort)}</strong>
      {:else}
        Built-in engine (on this computer)
      {/if}
    </span>
  </div>

  {#if remote && st.message}
    <p class="note" class:problem role="status">{st.message}</p>
  {/if}
  {#if remote}
    <div class="buttons">
      <button onclick={back}>Use the built-in engine</button>
    </div>
  {/if}

  <h3 class="sub">Connect to a remote engine</h3>
  <p class="hint">
    For data that stays on a cluster or server. On that machine run <code>rxd serve</code>; it prints an address, a token and
    an <code>ssh -L</code> command. Run that command in a terminal on this computer and keep it open, then enter the address and
    the token here. Open files by their path on that machine.
  </p>

  <form onsubmit={connect} autocomplete="off">
    <div class="field">
      <label for="remote-address">Address (the forwarded port on this computer)</label>
      <input
        id="remote-address"
        type="text"
        bind:value={address}
        placeholder="127.0.0.1:8765"
        spellcheck="false"
        autocapitalize="off"
        autocomplete="off"
        aria-invalid={touched && !check.ok}
      />
      {#if (touched || address.trim().length > 10) && !check.ok}
        <p class="err" role="alert">{check.error}</p>
      {/if}
    </div>
    <div class="field">
      <label for="remote-token">Token (printed once by <code>rxd serve</code>)</label>
      <input
        id="remote-token"
        type="password"
        bind:value={token}
        spellcheck="false"
        autocapitalize="off"
        autocomplete="off"
        data-1p-ignore
        data-lpignore="true"
        aria-invalid={touched && !tokenOk}
      />
      {#if touched && !tokenOk}
        <p class="err" role="alert">Paste the token exactly as printed.</p>
      {/if}
    </div>
    <div class="buttons">
      <button class="primary" type="submit" disabled={!ready}>Connect</button>
    </div>
  </form>

  <p class="hint small">
    The token stays in this app's memory only: it is not saved, so after a restart you enter it again. The address must be
    this computer (127.0.0.1 or localhost): the engine on the cluster is never reached directly.
  </p>
</div>

<style>
  .engine-settings h3 {
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: var(--ls-label);
    color: var(--c-accent);
    margin: 0 0 var(--space-3);
  }
  .engine-settings h3.sub { margin-top: var(--space-4); }
  .row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    min-height: 32px;
  }
  .label { font-size: 12px; color: var(--c-text-dim); flex-shrink: 0; min-width: 110px; }
  .value { font-size: 12px; color: var(--c-text); text-align: right; min-width: 0; overflow-wrap: anywhere; }
  .value.remote strong { color: var(--c-caution); letter-spacing: 0.04em; font-family: var(--font-mono); font-weight: 600; }
  .note { font-size: 12px; color: var(--c-text-dim); margin: 0 0 var(--space-2); line-height: 1.5; }
  .note.problem { color: var(--c-caution); }
  .hint { font-size: 12px; color: var(--c-muted); margin: 0 0 var(--space-3); line-height: 1.5; }
  .hint.small { font-size: 11px; margin-top: var(--space-3); }
  code { font-family: var(--font-mono); font-size: 11px; color: var(--c-text); }
  .field { display: flex; flex-direction: column; gap: 4px; margin-bottom: var(--space-3); }
  .field label { font-size: 12px; color: var(--c-text-dim); }
  .field input {
    width: 100%;
    box-sizing: border-box;
    min-height: 34px;
    padding: 6px 10px;
    background: var(--c-field);
    border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm);
    color: var(--c-text);
    font-family: var(--font-mono);
    font-size: 12px;
  }
  .field input[aria-invalid='true'] { border-color: var(--c-alarm); }
  .err { font-size: 11px; color: var(--c-alarm); margin: 0; line-height: 1.4; }
  .buttons { display: flex; gap: var(--space-2); margin: var(--space-2) 0 var(--space-3); }
</style>
