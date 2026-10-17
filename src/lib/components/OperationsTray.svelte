<script lang="ts">
  /**
   * Operations tray: a compact status-bar chip that lists everything the app is doing
   * (sweep loads, module / mosaic runs, file opens, exports) with live elapsed time and
   * Cancel / Retry / Dismiss. Failed operations are also summarised in a persistent,
   * dismissible line so a failure never goes unnoticed.
   */
  import { onDestroy, tick } from 'svelte';
  import {
    operations,
    cancelOp,
    cancelKind,
    retryOp,
    dismissOp,
    type Op,
    type OpState,
  } from '../utils/requestScheduler';
  import { uiHealth } from '../utils/uiHealth';

  const FINISHED: OpState[] = ['done', 'failed', 'cancelled'];
  /** Show a grouped "Loading sweeps n/m" row instead of one row each above this many active sweep ops. */
  const GROUP_ABOVE = 4;

  const isActive = (o: Op) => !FINISHED.includes(o.state);
  const isFinished = (o: Op) => FINISHED.includes(o.state);

  let open = $state(false);
  let showBackground = $state(false);
  let now = $state(Date.now());
  let chipEl: HTMLButtonElement | undefined = $state();
  let panelEl: HTMLDivElement | undefined = $state();
  let panelStyle = $state('');

  const all = $derived($operations);
  const health = $derived($uiHealth);

  // The "progress" kind is the status-bar progress watchdog: only interesting once it is slow/failed.
  const visible = $derived(
    all.filter(
      (o) =>
        (showBackground || o.priority !== 'background') &&
        !(o.kind === 'progress' && o.state === 'running'),
    ),
  );
  const backgroundActive = $derived(
    all.filter((o) => o.priority === 'background' && isActive(o)).length,
  );

  const activeSweeps = $derived(visible.filter((o) => o.kind === 'sweep' && isActive(o)));
  const grouped = $derived(activeSweeps.length > GROUP_ABOVE);
  // Finished sweep loads are noise (dozens per animation): keep only active and failed ones,
  // and fold the active ones into one row when there are many.
  const rows = $derived(
    visible.filter((o) => {
      if (o.kind !== 'sweep') return true;
      if (o.state === 'failed') return true;
      return !grouped && isActive(o);
    }),
  );

  const activeOthers = $derived(
    visible.filter((o) => isActive(o) && !(grouped && o.kind === 'sweep')),
  );
  const taskCount = $derived(activeOthers.length + (grouped ? 1 : 0));
  const failed = $derived(visible.filter((o) => o.state === 'failed'));
  const anySlow = $derived(visible.some((o) => o.state === 'slow'));
  const latestFailed = $derived(failed.length ? failed[failed.length - 1] : null);

  const chipVisible = $derived(taskCount > 0 || failed.length > 0 || health.busy || open);
  const tone = $derived(failed.length ? 'failed' : anySlow ? 'slow' : health.busy ? 'busy' : 'ok');
  const spinning = $derived(taskCount > 0);

  const chipText = $derived.by(() => {
    const parts: string[] = [];
    if (taskCount > 0) parts.push(`${taskCount} ${taskCount === 1 ? 'task' : 'tasks'}`);
    if (failed.length > 0) parts.push(`${failed.length} failed`);
    if (parts.length === 0 && health.busy) parts.push('UI busy');
    if (parts.length === 0) parts.push('Tasks');
    return parts.join(' · ');
  });
  const chipLabel = $derived(
    `${chipText}${taskCount > 0 && health.busy ? ', UI busy' : ''}. ${open ? 'Hide' : 'Show'} operations`,
  );

  // ── Sweep batch progress ("Loading sweeps 4/12") ─────────────────────────
  const sweepSeen = new Map<string, OpState>();
  let batchDone = $state(0);
  let batchTotal = $state(0);
  const finishedAt = new Map<string, number>();

  $effect(() => {
    const sweeps = all.filter((o) => o.kind === 'sweep' && (showBackground || o.priority !== 'background'));
    if (!sweeps.some(isActive)) {
      sweepSeen.clear();
      batchDone = 0;
      batchTotal = 0;
      return;
    }
    const shownIds = new Set(sweeps.map((o) => o.id));
    const liveIds = new Set(all.map((o) => o.id));
    for (const o of sweeps) sweepSeen.set(o.id, o.state);
    for (const [id, st] of [...sweepSeen]) {
      if (shownIds.has(id)) continue;
      if (liveIds.has(id)) sweepSeen.delete(id); // filtered out (background hidden)
      else if (st === 'failed' || st === 'cancelled') sweepSeen.delete(id); // dismissed
      else sweepSeen.set(id, 'done'); // vanished while looking active: dismissed after finishing
    }
    let total = 0;
    let done = 0;
    for (const st of sweepSeen.values()) {
      if (st === 'cancelled') continue;
      total++;
      if (st === 'done') done++;
    }
    batchDone = done;
    batchTotal = total;
  });

  // Remember when each op finished so its elapsed time stops growing.
  $effect(() => {
    const ids = new Set(all.map((o) => o.id));
    for (const o of all) {
      if (isFinished(o) && !finishedAt.has(o.id)) finishedAt.set(o.id, Date.now());
    }
    for (const id of [...finishedAt.keys()]) if (!ids.has(id)) finishedAt.delete(id);
  });

  // Live clock only while the list is on screen.
  $effect(() => {
    if (!open) return;
    now = Date.now();
    const t = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(t);
  });

  function elapsed(o: Op): string {
    const end = isFinished(o) ? (finishedAt.get(o.id) ?? now) : now;
    const s = Math.max(0, Math.round((end - o.startedAt) / 1000));
    if (s < 60) return `${s} s`;
    const m = Math.floor(s / 60);
    return `${m} m ${String(s % 60).padStart(2, '0')} s`;
  }

  const STATE_LABEL: Record<OpState, string> = {
    queued: 'Queued',
    running: 'Running',
    slow: 'Still working…',
    failed: 'Failed',
    cancelled: 'Cancelled',
    done: 'Done',
  };

  const groupSlow = $derived(activeSweeps.some((o) => o.state === 'slow'));

  // ── Open / close / position ──────────────────────────────────────────────
  function place() {
    if (!chipEl) return;
    const r = chipEl.getBoundingClientRect();
    const w = Math.min(420, window.innerWidth - 24);
    const left = Math.max(12, Math.min(r.right - w, window.innerWidth - w - 12));
    const bottom = Math.max(8, window.innerHeight - r.top + 6);
    panelStyle = `left:${left}px;bottom:${bottom}px;width:${w}px;max-height:${Math.max(160, r.top - 16)}px`;
  }

  async function toggle() {
    open = !open;
    if (open) {
      place();
      await tick();
      panelEl?.focus();
    }
  }

  function close(refocus = true) {
    if (!open) return;
    open = false;
    if (refocus) chipEl?.focus();
  }

  function onWindowKey(e: KeyboardEvent) {
    if (open && e.key === 'Escape') {
      e.stopPropagation();
      close();
    }
  }
  function onWindowPointer(e: PointerEvent) {
    if (!open) return;
    const t = e.target as Node | null;
    if (t && (panelEl?.contains(t) || chipEl?.contains(t))) return;
    close(false);
  }
  function onResize() {
    if (open) place();
  }

  /** Move the panel to <body>: the status bar clips overflow and has a backdrop-filter. */
  function portal(node: HTMLElement) {
    document.body.appendChild(node);
    return {
      destroy() {
        node.remove();
      },
    };
  }

  onDestroy(() => {
    open = false;
  });
</script>

<svelte:window onkeydown={onWindowKey} onpointerdown={onWindowPointer} onresize={onResize} />

<div class="ops-root">
  <span class="ops-summary" aria-live="polite" aria-atomic="true">
    {#if latestFailed}
      <span class="sum-line">
        <span class="sum-dot"></span>
        <span class="sum-text" title="{latestFailed.label} failed: {latestFailed.message ?? 'error'}">
          {latestFailed.label} failed: {latestFailed.message ?? 'error'}
        </span>
        {#if failed.length > 1}
          <button type="button" class="sum-more" onclick={() => !open && toggle()}>+{failed.length - 1} more</button>
        {/if}
        {#if latestFailed.retryable}
          <button type="button" class="sum-btn" onclick={() => latestFailed && retryOp(latestFailed.id)}>Retry</button>
        {/if}
        <button
          type="button"
          class="sum-x"
          aria-label="Dismiss failure notice"
          title="Dismiss"
          onclick={() => latestFailed && dismissOp(latestFailed.id)}
        >&times;</button>
      </span>
    {/if}
  </span>

  {#if chipVisible}
    <button
      type="button"
      class="ops-chip tone-{tone}"
      bind:this={chipEl}
      onclick={toggle}
      aria-expanded={open}
      aria-controls="ops-panel"
      aria-label={chipLabel}
      title="Operations"
    >
      {#if spinning}<span class="spin" aria-hidden="true"></span>{:else if failed.length}<span class="glyph" aria-hidden="true">!</span>{/if}
      <span class="chip-text">{chipText}</span>
      {#if health.busy && taskCount + failed.length > 0}<span class="busy-pill">UI busy</span>{/if}
      <svg class="chev" class:up={open} width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="18 15 12 9 6 15" /></svg>
    </button>
  {/if}
</div>

{#if open}
  <div
    id="ops-panel"
    class="ops-panel"
    role="region"
    aria-label="Operations"
    tabindex="-1"
    bind:this={panelEl}
    style={panelStyle}
    use:portal
  >
    <div class="panel-head">
      <span class="panel-title">Operations</span>
      <span class="health" class:busy={health.busy} title="Main-thread responsiveness ({health.source})">
        {#if health.busy}UI busy · longest {health.longestMs} ms{:else}UI responsive{/if}
        {#if health.fps !== null} · {health.fps} fps{/if}
        {#if health.longTasksLast10s > 0} · {health.longTasksLast10s} long task{health.longTasksLast10s === 1 ? '' : 's'}/10 s{/if}
      </span>
      <button type="button" class="icon-btn" aria-label="Close operations" title="Close (Esc)" onclick={() => close()}>&times;</button>
    </div>

    <ul class="op-list">
      {#if grouped}
        <li class="op s-{groupSlow ? 'slow' : 'running'}">
          <div class="op-main">
            <span class="badge b-{groupSlow ? 'slow' : 'running'}">{groupSlow ? 'Still working…' : 'Running'}</span>
            <span class="op-label">Loading sweeps {batchDone}/{batchTotal}</span>
          </div>
          <div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax={batchTotal} aria-valuenow={batchDone} aria-label="Sweeps loaded">
            <div class="bar-fill" style="width:{batchTotal ? (batchDone / batchTotal) * 100 : 0}%"></div>
          </div>
          <div class="op-actions">
            <button type="button" class="btn" onclick={() => cancelKind('sweep', showBackground ? undefined : ['visible', 'prefetch'])}>Cancel all</button>
          </div>
        </li>
      {/if}

      {#each rows as o (o.id)}
        <li class="op s-{o.state}">
          <div class="op-main">
            <span class="badge b-{o.state}">{STATE_LABEL[o.state]}</span>
            <span class="op-label" title={o.label}>{o.label}</span>
            <span class="op-time" title={o.state === 'queued' ? 'Waiting' : 'Elapsed'}>{elapsed(o)}</span>
          </div>
          {#if o.message}<div class="op-msg">{o.message}</div>{/if}
          {#if o.state === 'queued'}<div class="op-msg">Waiting · priority {o.priority}</div>{/if}
          <div class="op-actions">
            {#if isActive(o)}
              <button type="button" class="btn" onclick={() => cancelOp(o.id)}>Cancel</button>
            {/if}
            {#if o.state === 'failed' && o.retryable}
              <button type="button" class="btn primary" onclick={() => retryOp(o.id)}>Retry</button>
            {/if}
            {#if isFinished(o)}
              <button type="button" class="btn" onclick={() => dismissOp(o.id)}>Dismiss</button>
            {/if}
          </div>
        </li>
      {:else}
        {#if !grouped}
          <li class="empty">Nothing is running.</li>
        {/if}
      {/each}
    </ul>

    <div class="panel-foot">
      <label class="bg-toggle">
        <input type="checkbox" bind:checked={showBackground} />
        Show background{backgroundActive > 0 && !showBackground ? ` (${backgroundActive})` : ''}
      </label>
      {#if all.some(isFinished)}
        <button
          type="button"
          class="btn"
          onclick={() => all.filter(isFinished).forEach((o) => dismissOp(o.id))}
        >Clear finished</button>
      {/if}
    </div>
  </div>
{/if}

<style>
  .ops-root {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    flex-shrink: 1;
  }

  /* ── Failure summary ── */
  .ops-summary {
    display: flex;
    min-width: 0;
  }
  .sum-line {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
    padding: 1px 4px 1px 8px;
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--accent-danger) 12%, transparent);
    border: 1px solid color-mix(in srgb, var(--accent-danger) 35%, transparent);
    color: var(--text-primary);
    font-size: 11px;
  }
  .sum-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--accent-danger);
    flex-shrink: 0;
  }
  .sum-text {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 38vw;
    min-width: 0;
  }
  .sum-more,
  .sum-btn,
  .sum-x {
    background: none;
    border: none;
    color: var(--accent-hover);
    font-size: 11px;
    padding: 1px 5px;
    height: auto;
    cursor: pointer;
    border-radius: 4px;
    white-space: nowrap;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }
  .sum-btn {
    font-weight: 600;
    color: var(--text-primary);
    background: color-mix(in srgb, var(--accent-danger) 22%, transparent);
  }
  .sum-more { color: var(--text-secondary); }
  .sum-x { color: var(--text-muted); font-size: 14px; line-height: 1; padding: 0 5px; }
  .sum-more:hover,
  .sum-btn:hover,
  .sum-x:hover { background: var(--bg-hover); color: var(--text-primary); }

  /* ── Chip ── */
  .ops-chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    height: 20px;
    padding: 0 8px;
    font-size: 11px;
    font-weight: 600;
    white-space: nowrap;
    color: var(--text-secondary);
    background: var(--bg-hover);
    border: 1px solid var(--border-light);
    border-radius: 999px;
    cursor: pointer;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
    transition: background var(--transition-fast), border-color var(--transition-fast);
  }
  .ops-chip:hover { background: var(--bg-active); }
  .ops-chip:focus-visible,
  .btn:focus-visible,
  .icon-btn:focus-visible,
  .sum-btn:focus-visible,
  .sum-x:focus-visible,
  .sum-more:focus-visible {
    outline: 2px solid var(--accent-primary);
    outline-offset: 1px;
  }
  .ops-chip.tone-ok { color: var(--text-accent); border-color: var(--accent-muted); }
  .ops-chip.tone-busy,
  .ops-chip.tone-slow {
    color: var(--accent-warning);
    border-color: color-mix(in srgb, var(--accent-warning) 55%, transparent);
    background: color-mix(in srgb, var(--accent-warning) 12%, transparent);
  }
  .ops-chip.tone-failed {
    color: var(--accent-danger);
    border-color: color-mix(in srgb, var(--accent-danger) 55%, transparent);
    background: color-mix(in srgb, var(--accent-danger) 12%, transparent);
  }
  .chip-text { overflow: hidden; text-overflow: ellipsis; }
  .busy-pill {
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.02em;
    text-transform: uppercase;
    padding: 0 5px;
    border-radius: 999px;
    color: var(--accent-warning);
    background: color-mix(in srgb, var(--accent-warning) 18%, transparent);
  }
  .glyph {
    width: 12px;
    height: 12px;
    border-radius: 50%;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 9px;
    font-weight: 800;
    color: var(--bg-primary);
    background: var(--accent-danger);
  }
  .chev { transition: transform var(--transition-fast); flex-shrink: 0; }
  .chev.up { transform: rotate(180deg); }
  .spin {
    width: 10px;
    height: 10px;
    border: 2px solid color-mix(in srgb, currentColor 30%, transparent);
    border-top-color: currentColor;
    border-radius: 50%;
    animation: ops-spin 800ms linear infinite;
    flex-shrink: 0;
  }
  @keyframes ops-spin { to { transform: rotate(360deg); } }

  /* ── Panel ── */
  .ops-panel {
    position: fixed;
    z-index: 10001;
    display: flex;
    flex-direction: column;
    background: var(--bg-surface);
    color: var(--text-primary);
    border: 1px solid var(--border-light);
    border-radius: var(--radius-md);
    box-shadow: var(--shadow-lg);
    font-size: 12px;
    outline: none;
    overflow: hidden;
    animation: ops-in 140ms ease-out;
  }
  @keyframes ops-in {
    from { opacity: 0; transform: translateY(6px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .panel-head {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 10px;
    border-bottom: 1px solid var(--border-color);
    flex-shrink: 0;
  }
  .panel-title { font-weight: 700; font-size: 12px; }
  .health {
    flex: 1;
    min-width: 0;
    font-size: 10px;
    color: var(--text-muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .health.busy { color: var(--accent-warning); font-weight: 600; }
  .icon-btn {
    background: none;
    border: none;
    color: var(--text-muted);
    font-size: 18px;
    line-height: 1;
    width: 22px;
    height: 22px;
    padding: 0;
    cursor: pointer;
    border-radius: 4px;
  }
  .icon-btn:hover { background: var(--bg-hover); color: var(--text-primary); }

  .op-list {
    list-style: none;
    margin: 0;
    padding: 4px 0;
    overflow-y: auto;
    flex: 1;
    min-height: 0;
  }
  .op {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 2px 8px;
    padding: 6px 10px;
    border-left: 3px solid transparent;
  }
  .op + .op { border-top: 1px solid var(--border-color); }
  .op.s-slow { border-left-color: var(--accent-warning); }
  .op.s-failed { border-left-color: var(--accent-danger); }
  .op.s-running { border-left-color: var(--accent-primary); }
  .op.s-done { border-left-color: var(--accent-success); opacity: 0.75; }
  .op.s-cancelled, .op.s-queued { border-left-color: var(--text-muted); }
  .op-main {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    grid-column: 1 / -1;
  }
  .op-label {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-weight: 500;
  }
  .op-time {
    font-variant-numeric: tabular-nums;
    color: var(--text-muted);
    font-size: 11px;
    flex-shrink: 0;
  }
  .op-msg {
    grid-column: 1;
    color: var(--text-secondary);
    font-size: 11px;
    overflow-wrap: anywhere;
    line-height: 1.35;
  }
  .s-failed .op-msg { color: var(--accent-danger); }
  .op-actions {
    grid-column: 2;
    grid-row: 2 / span 2;
    align-self: center;
    display: flex;
    gap: 4px;
  }
  .bar {
    grid-column: 1;
    height: 4px;
    border-radius: 2px;
    background: var(--bg-active);
    overflow: hidden;
    align-self: center;
  }
  .bar-fill {
    height: 100%;
    background: var(--accent-primary);
    transition: width var(--transition-normal);
  }

  .badge {
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.03em;
    text-transform: uppercase;
    padding: 1px 6px;
    border-radius: 999px;
    white-space: nowrap;
    flex-shrink: 0;
    color: var(--text-secondary);
    background: var(--bg-active);
  }
  .b-running { color: var(--text-accent); }
  .b-slow {
    color: var(--accent-warning);
    background: color-mix(in srgb, var(--accent-warning) 16%, transparent);
  }
  .b-failed {
    color: var(--accent-danger);
    background: color-mix(in srgb, var(--accent-danger) 16%, transparent);
  }
  .b-done {
    color: var(--accent-success);
    background: color-mix(in srgb, var(--accent-success) 16%, transparent);
  }

  .btn {
    font-size: 11px;
    height: 22px;
    padding: 0 9px;
    color: var(--text-secondary);
    background: transparent;
    border: 1px solid var(--border-light);
    border-radius: var(--radius-sm);
    cursor: pointer;
    white-space: nowrap;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }
  .btn:hover { background: var(--bg-hover); color: var(--text-primary); }
  .btn.primary {
    color: #fff;
    background: var(--accent-primary);
    border-color: var(--accent-primary);
  }
  .btn.primary:hover { background: var(--accent-hover); }

  .empty { padding: 14px 10px; color: var(--text-muted); text-align: center; }

  .panel-foot {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 6px 10px;
    border-top: 1px solid var(--border-color);
    flex-shrink: 0;
  }
  .bg-toggle {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: var(--text-secondary);
    font-size: 11px;
    cursor: pointer;
  }

  @media (max-width: 560px) {
    .sum-text { max-width: 30vw; }
    .op-actions { grid-column: 1; grid-row: auto; justify-self: end; }
  }

  @media (prefers-reduced-motion: reduce) {
    .spin { animation-duration: 2.4s; }
    .ops-panel { animation: none; }
  }
</style>
