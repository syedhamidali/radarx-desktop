<script lang="ts">
  // Boot / link-lost state of the analysis engine. Everything shown here is real: the connection
  // store, the web engine phases, the discovered sidecar port and any error the Tauri shell raised.
  import { onMount, onDestroy } from 'svelte';
  import RadarSweep from './RadarSweep.svelte';
  import { connectionStatus } from '../stores/radarData';
  import { engineState, engineStatusMessage } from '../stores/webInfo';
  import { sidecarError } from '../stores/boot';
  import { wsManager } from '../utils/websocket';
  import { isWebMode } from '../utils/env';

  /** True once the engine has been reachable in this session (so this is a lost link, not a boot). */
  export let everConnected = false;

  let seconds = 0;
  let port: number | null = null;
  let timer: ReturnType<typeof setInterval> | null = null;

  function readPort() {
    const p = (window as any).__XRADAR_PORT__;
    port = typeof p === 'number' && p > 0 ? p : null;
  }

  onMount(() => {
    readPort();
    timer = setInterval(() => {
      seconds += 1;
      readPort();
    }, 1000);
  });
  onDestroy(() => timer && clearInterval(timer));

  $: web = isWebMode();
  $: phase = $engineState.phase;
  $: fault =
    phase === 'rejected' || phase === 'unreachable' || phase === 'unconfigured' || !!$sidecarError;
  $: located = web ? phase !== 'unconfigured' : port !== null;
  $: linking = $connectionStatus === 'connecting';
  $: title = fault ? 'ENGINE FAULT' : everConnected ? 'LINK LOST' : 'INITIALISING ENGINE';
  $: detail =
    $sidecarError ??
    (web ? engineStatusMessage($engineState) : null) ??
    (everConnected ? 'Reconnecting to the analysis engine.' : 'Starting the analysis engine (Python, xradar, xarray).');
  $: attempt = $engineState.attempt;
</script>

<div class="boot hud-grid" role="status" aria-live="polite" data-testid="engine-boot">
  <div class="boot-inner">
    <RadarSweep size={168} period={fault ? 9 : 4} tone={fault ? 'alarm' : 'accent'} labels={false} />
    <div class="boot-title" class:fault>
      {title}{#if !fault}<span class="dots" aria-hidden="true"></span>{/if}
    </div>
    <p class="boot-detail">{detail}</p>

    <ul class="steps mono">
      <li class="ok"><span class="tag">OK</span>Interface shell loaded</li>
      <li class={located ? 'ok' : 'run'}>
        <span class="tag">{located ? 'OK' : '..'}</span>{web ? 'Engine address configured' : 'Locating analysis engine'}
        {#if port}<span class="meta">port {port}</span>{/if}
      </li>
      <li class={fault ? 'bad' : $connectionStatus === 'connected' ? 'ok' : located ? 'run' : 'wait'}>
        <span class="tag">{fault ? '!!' : $connectionStatus === 'connected' ? 'OK' : located ? '..' : '--'}</span>
        {linking ? 'Handshake with engine' : 'Opening link to engine'}
        {#if attempt > 0}<span class="meta">attempt {attempt}</span>{/if}
        <span class="meta">T+{seconds}s</span>
      </li>
    </ul>

    {#if seconds >= 12 && !fault}
      <p class="hint">The first start can take up to a minute while the Python runtime loads.</p>
    {/if}
    {#if fault || seconds >= 30}
      <button class="primary retry" on:click={() => wsManager.reconnect()}>Retry connection</button>
    {/if}
  </div>
</div>

<style>
  .boot {
    height: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--c-surface-0);
    animation: fade-in var(--dur-slow) var(--ease-out);
  }
  .boot-inner {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-3);
    max-width: 440px;
    padding: var(--space-5);
    text-align: center;
  }
  .boot-title {
    margin-top: var(--space-3);
    font-size: var(--fs-lg);
    font-weight: 600;
    letter-spacing: var(--ls-wide);
    color: var(--c-accent);
  }
  .boot-title.fault { color: var(--c-alarm); }
  .dots::after {
    content: '';
    display: inline-block;
    width: 1.4em;
    text-align: left;
    animation: ellipsis 1.4s steps(4, end) infinite;
  }
  @keyframes ellipsis {
    0% { content: ''; }
    25% { content: '.'; }
    50% { content: '..'; }
    75% { content: '...'; }
  }
  .boot-detail { color: var(--c-text-dim); font-size: var(--fs-sm); max-width: 360px; }
  .steps {
    list-style: none;
    align-self: stretch;
    margin-top: var(--space-2);
    padding: var(--space-3);
    text-align: left;
    font-size: var(--fs-xs);
    display: flex;
    flex-direction: column;
    gap: 6px;
    background: var(--c-panel);
    border: 1px solid var(--c-border);
  }
  .steps li { display: flex; align-items: baseline; gap: 10px; color: var(--c-text-dim); }
  .steps .tag { min-width: 3.2em; font-weight: 500; letter-spacing: 0.08em; }
  .steps .meta { margin-left: auto; color: var(--c-muted); }
  .steps .meta + .meta { margin-left: 12px; }
  .steps li.ok .tag { color: var(--c-nominal); }
  .steps li.run .tag { color: var(--c-accent); animation: pulse-glow 1.2s ease-in-out infinite; }
  .steps li.run { color: var(--c-text); }
  .steps li.wait { color: var(--c-muted); }
  .steps li.bad .tag { color: var(--c-alarm); }
  .steps .tag::before { content: '['; color: var(--c-muted); }
  .steps .tag::after { content: ']'; color: var(--c-muted); }
  .hint { font-size: var(--fs-xs); color: var(--c-muted); }
  .retry { margin-top: var(--space-1); }
</style>
