<script lang="ts">
  /**
   * Autopilot panel (mission-control look): the inventory, the explained plan with checkboxes
   * and estimates, Run with progress and cancel, the results gallery, and the rule table with
   * its evidence. Nothing runs until the user presses Run (stores/autopilot.ts).
   */
  import { budgetLine } from '../utils/autopilotText';
  import { isWebMode } from '../utils/env';
  import { state as auto, busy, close, reset, run, cancel, toggleStep, setOutDir, setBudget, inspect, planNow, type Step } from '../stores/autopilot';

  let { onopen }: { onopen?: (paths: string[]) => void } = $props();
  let showRules = $state(false);
  let showFiles = $state(false);
  let pathText = $state('');

  const mb = (b: number | null | undefined) => (b == null ? '-' : b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.round(b / 1e6)} MB`);
  const mins = (s: number) => (s >= 90 ? `~${Math.round(s / 60)} min` : `~${Math.round(s)} s`);
  const when = (t: string | null) => (t ? t.replace('T', ' ').slice(0, 16) + 'Z' : '-');
  const provClass = (p: string) => (p.startsWith('OBSERVED') ? 'obs' : p.startsWith('RETRIEVED') ? 'ret' : p.startsWith('MODEL') ? 'mod' : 'sty');

  const selectedCount = $derived(Object.values($auto.selected).filter(Boolean).length);
  const selectedSeconds = $derived(($auto.plan?.steps ?? []).filter((s) => $auto.selected[s.id]).reduce((a, s) => a + s.estimate.seconds, 0));
  const radars = $derived(Object.values($auto.inventory?.radars ?? {}).filter((r) => r.name !== 'UNKNOWN'));

  async function reveal(path: string) {
    try {
      const { invoke } = await import('@tauri-apps/api/core');
      await invoke('reveal_in_folder', { path });
    } catch (err) {
      console.warn('[Autopilot] show in folder failed:', err);
    }
  }
  function startFromText() {
    const list = pathText.split('\n').map((s) => s.trim()).filter(Boolean);
    if (list.length) inspect(list);
  }
  function stepState(s: Step): string {
    if (s.refused) return 'refused';
    if (s.manual) return 'manual';
    return $auto.selected[s.id] ? 'on' : 'off';
  }
</script>

<div class="ap" role="dialog" aria-label="Autopilot" tabindex="-1">
  <div class="head">
    <div>
      <div class="kicker">AUTOPILOT <span class="dot" class:live={$busy}></span></div>
      <h2>{$auto.plan ? $auto.plan.situation.label : $auto.phase === 'inspecting' ? 'Inspecting inputs' : 'Analyst'}</h2>
      <div class="sub">
        {#if $auto.inventory}
          {$auto.inventory.n_files} files, {mb($auto.inventory.total_bytes)} on disk, {radars.length} radar{radars.length === 1 ? '' : 's'}; metadata read in {($auto.inventory.elapsed_ms / 1000).toFixed(1)} s
        {:else}
          Give files, folders or a whole case: it inspects, plans, and runs only what you approve.
        {/if}
      </div>
    </div>
    <button class="icon" onclick={close} aria-label="Close">×</button>
  </div>

  <div class="body">
    {#if $auto.error}<p class="err">{$auto.error}</p>{/if}

    {#if $auto.phase === 'idle' || ($auto.phase === 'error' && !$auto.inventory)}
      <p class="lede">Drop several files or a folder on the window, or paste paths (one per line):</p>
      <textarea bind:value={pathText} rows="4" placeholder="/data/case/KHPX ..."></textarea>
      <div class="row"><button class="primary" onclick={startFromText} disabled={!pathText.trim()}>Inspect</button></div>
    {/if}

    {#if $auto.progress && ($auto.phase === 'inspecting' || $auto.phase === 'running')}
      <div class="prog">
        <div class="bar"><div class="fill" style:width={`${$auto.progress.pct}%`}></div></div>
        <div class="ptext"><span class="mono">{String($auto.progress.pct).padStart(3, ' ')}%</span> {$auto.progress.text}</div>
        <button class="quiet" onclick={cancel} disabled={$auto.cancelling}>{$auto.cancelling ? 'Cancelling...' : 'Cancel'}</button>
      </div>
    {/if}

    {#if $auto.inventory}
      <h3>Inventory <span class="tag ret">metadata only</span></h3>
      <div class="tiles">
        {#each radars as r (r.name)}
          <div class="tile">
            <div class="tname">{r.name} <span class="dim">{r.formats.join(', ')}</span></div>
            <div class="trow"><span>{r.n_files} vol</span><span>{mb(r.bytes)}</span><span>{r.kinds.join('+').toUpperCase()}</span></div>
            <div class="trow dim">{when(r.t0)}{r.t1 && r.t1 !== r.t0 ? ` to ${when(r.t1).slice(11)}` : ''}{r.cadence_s ? `, every ${Math.round(r.cadence_s / 60)} min` : ''}</div>
            <div class="trow">
              <span class="chip" class:yes={r.has_velocity}>V</span>
              <span class="chip" class:yes={r.dual_pol}>dual-pol</span>
              <span class="chip" class:yes={!!r.band}>{r.band ? `${r.band} band` : 'band ?'}</span>
              {#if r.gaps.length}<span class="chip warn">{r.gaps.length} gap{r.gaps.length === 1 ? '' : 's'}</span>{/if}
            </div>
            {#if $auto.inventory.checks[r.name]}
              {@const c = $auto.inventory.checks[r.name]}
              <div class="trow dim">
                {#if c.error}check failed{:else}check: {c.counts?.warning ?? 0} warning, {c.counts?.notice ?? 0} notice{/if}
              </div>
            {/if}
          </div>
        {/each}
      </div>
      {#if $auto.inventory.pairs.length}
        <ul class="plain">
          {#each $auto.inventory.pairs as p}
            <li>{p.a}-{p.b}: {p.distance_km} km{p.overlap_space ? ', coverage overlaps' : ', no overlap'}{p.dual_doppler ? `, beam crossing ok on ${Math.round(p.dual_doppler.good_fraction * 100)} % of the shared area` : ''}</li>
          {/each}
        </ul>
      {/if}
      {#if $auto.inventory.unreadable.length}
        <p class="small warn-text">{$auto.inventory.unreadable.length} file(s) could not be opened: {$auto.inventory.unreadable.map((u) => u.name).join(', ')}</p>
      {/if}
      <button class="link" onclick={() => (showFiles = !showFiles)}>{showFiles ? 'Hide' : 'Show'} the {$auto.inputs.length} input path(s)</button>
      {#if showFiles}<ul class="plain mono small">{#each $auto.inputs as p}<li>{p}</li>{/each}</ul>{/if}
    {/if}

    {#if $auto.plan}
      {@const g = $auto.plan.governor}
      <h3>Plan <span class="dim small">ranked; estimates are rough (ESTIMATE)</span></h3>
      <p class="lede small"><span class="mono">{budgetLine(g)}</span>. {g.reductions.length ? `${g.reductions.length} reduction(s) decided by the governor, each stated below and in the run record.` : 'No reductions needed for this data.'}</p>
      {#each $auto.plan.steps as s (s.id)}
        <details class="step {stepState(s)}">
          <summary>
            <input type="checkbox" checked={!!$auto.selected[s.id]} disabled={!!s.refused || !!s.manual || $busy} onchange={(e) => toggleStep(s.id, (e.currentTarget as HTMLInputElement).checked)} onclick={(e) => e.stopPropagation()} />
            <span class="order mono">{s.order}</span>
            <span class="stitle">{s.title}</span>
            <span class="est mono">{mins(s.estimate.seconds)} · {mb(s.estimate.memory_bytes)} · {s.estimate.volumes} vol</span>
            <span class="tag {provClass(s.provenance)}">{s.provenance.split(' ')[0]}</span>
          </summary>
          <p class="why">{s.why}</p>
          <table class="ev">
            <tbody>
              <tr><th>needs</th><td>{s.needs}</td></tr>
              <tr><th>produces</th><td>{s.produces}</td></tr>
              <tr><th>provenance</th><td>{s.provenance}</td></tr>
              <tr><th>how</th><td>{s.chunking}</td></tr>
              <tr><th>command</th><td><code>{s.rxd_command}</code></td></tr>
            </tbody>
          </table>
          {#each s.reductions as r}<p class="reduce">Reduced: {r.what} {r.from} → {r.to}. {r.reason}</p>{/each}
          {#if s.refused}<p class="refuse">Refused: {s.refused}</p>{/if}
          {#if s.manual}<p class="reduce">Not run here; use the command above.</p>{/if}
          <ul class="plain small">{#each s.evidence as e}<li>{e}</li>{/each}</ul>
        </details>
      {/each}
      {#if $auto.plan.refusals.length}
        <h3>Not possible with this data</h3>
        <ul class="plain">{#each $auto.plan.refusals as r}<li><b>{r.what}</b>: missing {r.missing}</li>{/each}</ul>
      {/if}
      {#if $auto.plan.notes.length}
        <h3>Be aware</h3>
        <ul class="plain">{#each $auto.plan.notes as n}<li>{n}</li>{/each}</ul>
      {/if}

      <h3>Run</h3>
      <label class="field">Results folder <input type="text" value={$auto.outDir} oninput={(e) => setOutDir((e.currentTarget as HTMLInputElement).value)} disabled={$busy} /></label>
      <label class="field">Memory budget (GB; blank = automatic: the smaller of 60 % of RAM and 90 % of the memory available now)
        <input type="number" min="0.5" step="0.5" value={$auto.budgetGb ?? ''} oninput={(e) => setBudget((e.currentTarget as HTMLInputElement).value ? Number((e.currentTarget as HTMLInputElement).value) : null)} onchange={planNow} disabled={$busy} />
      </label>
      <p class="small dim">{$auto.plan.consent} Each output carries its provenance and the reductions; a run record and a one-page summary are written next to the results.</p>

      <button class="link" onclick={() => (showRules = !showRules)}>{showRules ? 'Hide' : 'Show'} the decision rules and their evidence</button>
      {#if showRules}
        <table class="rules">
          <tbody>
            {#each $auto.plan.rules as r}
              <tr class:fired={r.fired}><td class="mono">{r.fired ? 'fired' : 'quiet'}</td><td><b>{r.title}</b><ul class="plain small">{#each r.evidence as e}<li>{e}</li>{/each}</ul></td></tr>
            {/each}
          </tbody>
        </table>
      {/if}
    {/if}

    {#if $auto.record}
      <h3>Results</h3>
      {#if $auto.notice}<p class="notice">{$auto.notice}</p>{/if}
      <ul class="plain">
        {#each $auto.record.steps as s}
          <li><span class="status {s.status.split(' ')[0]}">{s.status}</span> {s.title}{s.elapsed_s ? ` (${s.elapsed_s} s)` : ''}{s.error && s.error !== 'cancelled' ? `: ${s.error}` : ''}{s.status === 'cancelled' && s.files?.length ? `: ${s.files.length} file${s.files.length === 1 ? '' : 's'} written before the cancel kept` : ''}</li>
        {/each}
      </ul>
      <p class="small">Written to <code>{$auto.record.out_dir}</code> (SUMMARY.md, run_record.json).</p>
      {#if $auto.gallery.length}
        <div class="gallery">
          {#each $auto.gallery as g, i (`${i}:${g.path}`)}
            <figure>
              {#if g.png}<img src={`data:image/png;base64,${g.png}`} alt={g.name} />{:else}<div class="file mono">{g.name.split('.').pop()}</div>{/if}
              {#if onopen && g.viewer}<button class="link" onclick={() => onopen?.([g.path])}>open in viewer</button>
              {:else if /\.(nc|csv)$/i.test(g.name) && !isWebMode()}<button class="link" onclick={() => reveal(g.path)} title="A map grid, not a radar volume: open it in a GIS or xarray">show in folder</button>{/if}
              <figcaption class="small">{g.name}</figcaption>
            </figure>
          {/each}
        </div>
      {/if}
    {/if}
  </div>

  <div class="foot">
    {#if $auto.plan}
      <button class="primary" onclick={run} disabled={$busy || selectedCount === 0 || !$auto.outDir}>{$auto.notice ? 'Run again (resumes)' : `Run ${selectedCount} step${selectedCount === 1 ? '' : 's'} (${mins(selectedSeconds)})`}</button>
      <button class="quiet" onclick={() => inspect($auto.inputs)} disabled={$busy}>Inspect again</button>
    {/if}
    <button class="quiet" onclick={reset} disabled={$busy}>Clear</button>
    <span class="grow"></span>
    <span class="small dim">rxd auto INPUT... --plan-only</span>
  </div>
</div>

<style>
  .ap {
    position: fixed; right: var(--space-3); top: calc(var(--toolbar-height) + var(--space-2)); z-index: 940;
    width: min(620px, calc(100vw - 2 * var(--space-3))); max-height: calc(100vh - var(--toolbar-height) - var(--statusbar-height) - 2 * var(--space-3));
    display: flex; flex-direction: column; background: var(--c-overlay); border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm); box-shadow: var(--shadow-3); color: var(--c-text); font-size: var(--fs-sm); outline: none;
  }
  .head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-3); padding: var(--space-3) var(--space-4); border-bottom: 1px solid var(--c-border); background: var(--c-panel-head); }
  .kicker { font-family: var(--font-mono); font-size: var(--fs-2xs); letter-spacing: var(--ls-wide); color: var(--c-accent); display: flex; align-items: center; gap: var(--space-2); }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--c-muted); }
  .dot.live { background: var(--c-nominal); box-shadow: var(--glow-nominal); animation: pulse 1.2s infinite; }
  @keyframes pulse { 50% { opacity: 0.4; } }
  h2 { margin: 2px 0 0; font-size: var(--fs-md); font-weight: 600; }
  h3 { margin: var(--space-4) 0 var(--space-1); font-size: var(--fs-sm); font-weight: 600; display: flex; align-items: center; gap: var(--space-2); }
  .sub { font-size: var(--fs-xs); color: var(--c-muted); }
  .body { overflow-y: auto; padding: var(--space-3) var(--space-4); display: flex; flex-direction: column; gap: var(--space-2); }
  .foot { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); padding: var(--space-2) var(--space-4); border-top: 1px solid var(--c-border); }
  .grow { flex: 1; }
  .icon { background: transparent; border: none; font-size: 20px; line-height: 1; color: var(--c-muted); padding: 0 var(--space-1); cursor: pointer; }
  .icon:hover { color: var(--c-text); }
  .lede { margin: 0; color: var(--c-text-dim); line-height: 1.5; }
  .dim { color: var(--c-muted); }
  .small { font-size: var(--fs-xs); }
  .mono { font-family: var(--font-mono); }
  .err { color: var(--c-alarm); margin: 0; }
  .notice { margin: 0; padding: var(--space-1) var(--space-2); border-left: 3px solid var(--c-caution); background: var(--c-field); color: var(--c-text-dim); line-height: 1.5; font-size: var(--fs-xs); }
  .warn-text { color: var(--c-caution); margin: 0; }
  textarea, input[type='text'], input[type='number'] { width: 100%; box-sizing: border-box; background: var(--c-field); color: var(--c-text); border: 1px solid var(--c-border); border-radius: var(--radius-xs); padding: var(--space-1) var(--space-2); font-family: var(--font-mono); font-size: var(--fs-xs); }
  .field { display: flex; flex-direction: column; gap: 2px; font-size: var(--fs-xs); color: var(--c-text-dim); }
  .row { display: flex; gap: var(--space-2); }
  button.primary { background: var(--c-accent); color: var(--c-on-accent); border: 1px solid var(--c-accent-strong); border-radius: var(--radius-xs); padding: var(--space-1) var(--space-3); cursor: pointer; font-weight: 600; }
  button.primary:disabled { opacity: 0.5; cursor: default; }
  button.quiet { background: transparent; border: 1px solid transparent; color: var(--c-muted); border-radius: var(--radius-xs); padding: var(--space-1) var(--space-2); cursor: pointer; }
  button.quiet:hover { color: var(--c-text); border-color: var(--c-border); }
  button.link { background: transparent; border: none; padding: 0; color: var(--c-accent); text-decoration: underline; cursor: pointer; font-size: var(--fs-xs); text-align: left; }
  .prog { display: grid; grid-template-columns: 1fr auto; gap: var(--space-1) var(--space-2); align-items: center; }
  .bar { grid-column: 1 / -1; height: 6px; background: var(--c-field); border-radius: 3px; overflow: hidden; border: 1px solid var(--c-border); }
  .fill { height: 100%; background: var(--c-accent); transition: width var(--dur-base) var(--ease-out); }
  .ptext { font-size: var(--fs-xs); color: var(--c-text-dim); }
  .tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: var(--space-2); }
  .tile { border: 1px solid var(--c-border); border-radius: var(--radius-sm); padding: var(--space-2); background: var(--c-field); display: flex; flex-direction: column; gap: 2px; font-size: var(--fs-xs); }
  .tname { font-weight: 600; font-size: var(--fs-sm); }
  .trow { display: flex; flex-wrap: wrap; gap: var(--space-2); }
  .chip { font-size: 9.5px; padding: 0 5px; border-radius: 3px; border: 1px solid var(--c-border); color: var(--c-muted); }
  .chip.yes { color: var(--c-nominal); border-color: rgba(var(--c-nominal-rgb), 0.5); }
  .chip.warn { color: var(--c-caution); border-color: rgba(var(--c-caution-rgb), 0.5); }
  ul.plain { margin: 0; padding-left: 1.2em; font-size: var(--fs-xs); color: var(--c-text-dim); line-height: 1.5; }
  .step { border: 1px solid var(--c-border); border-left-width: 3px; border-radius: var(--radius-sm); padding: var(--space-2) var(--space-3); background: var(--c-field); }
  .step.on { border-left-color: var(--c-nominal); }
  .step.off { border-left-color: var(--c-border-strong); }
  .step.refused { border-left-color: var(--c-alarm); opacity: 0.8; }
  .step.manual { border-left-color: var(--c-derived); }
  summary { cursor: pointer; display: flex; align-items: center; gap: var(--space-2); list-style: none; }
  summary::-webkit-details-marker { display: none; }
  summary:focus-visible { outline: 2px solid var(--c-accent); outline-offset: 2px; }
  .order { color: var(--c-muted); font-size: var(--fs-xs); }
  .stitle { font-weight: 600; flex: 1; }
  .est { font-size: var(--fs-2xs); color: var(--c-muted); white-space: nowrap; }
  .tag { font-size: 9.5px; padding: 0 4px; border-radius: 3px; border: 1px solid; letter-spacing: 0.04em; white-space: nowrap; }
  .tag.ret { color: var(--c-derived); border-color: rgba(var(--c-derived-rgb), 0.55); }
  .tag.obs { color: var(--c-nominal); border-color: rgba(var(--c-nominal-rgb), 0.5); }
  .tag.mod { color: var(--c-accent); border-color: rgba(var(--c-accent-rgb), 0.5); }
  .tag.sty { color: var(--c-caution); border-color: rgba(var(--c-caution-rgb), 0.5); }
  .why { margin: var(--space-2) 0 var(--space-1); line-height: 1.5; color: var(--c-text-dim); }
  table.ev { width: 100%; border-collapse: collapse; margin: var(--space-1) 0; font-size: var(--fs-xs); }
  table.ev th { text-align: left; font-weight: 400; color: var(--c-muted); padding: 2px var(--space-2) 2px 0; vertical-align: top; white-space: nowrap; width: 1%; }
  table.ev td { padding: 2px 0; color: var(--c-text-dim); }
  code { font-family: var(--font-mono); font-size: var(--fs-2xs); word-break: break-all; }
  .reduce { margin: var(--space-1) 0; font-size: var(--fs-xs); color: var(--c-caution); }
  .refuse { margin: var(--space-1) 0; font-size: var(--fs-xs); color: var(--c-alarm); }
  table.rules { width: 100%; border-collapse: collapse; font-size: var(--fs-xs); }
  table.rules td { padding: var(--space-1) var(--space-2) var(--space-1) 0; vertical-align: top; border-top: 1px solid var(--c-border); color: var(--c-muted); }
  table.rules tr.fired td { color: var(--c-text-dim); }
  table.rules tr.fired td:first-child { color: var(--c-nominal); }
  .status { font-family: var(--font-mono); font-size: var(--fs-2xs); text-transform: uppercase; }
  .status.done { color: var(--c-nominal); } .status.failed, .status.refused { color: var(--c-alarm); } .status.cancelled, .status.skipped { color: var(--c-caution); }
  .gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: var(--space-2); }
  figure { margin: 0; border: 1px solid var(--c-border); border-radius: var(--radius-sm); padding: var(--space-1); background: var(--c-field); display: flex; flex-direction: column; gap: 2px; }
  figure img { width: 100%; height: 110px; object-fit: contain; background: var(--c-surface-0); border-radius: var(--radius-xs); }
  .file { height: 110px; display: grid; place-items: center; color: var(--c-muted); background: var(--c-surface-0); border-radius: var(--radius-xs); text-transform: uppercase; }
  figcaption { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--c-text-dim); }
</style>
