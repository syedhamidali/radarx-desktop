<script lang="ts">
  /**
   * Data check panel: findings with evidence and location, the suggested QC recipe(s) with the
   * reason per step, and a first view. Nothing runs by itself: "Apply" sends the recipe to the
   * pipeline exactly as the Pipelines panel would, when the user clicks it.
   */
  import { onMount, tick } from 'svelte';
  import { fly } from 'svelte/transition';
  import {
    current,
    panelOpen,
    requestCheck,
    hideForNow,
    neverShowFor,
    type Finding,
    type RecipeItem,
    type Evidence,
  } from '../stores/dataCheck';
  import { selectedSweep, selectedVariable } from '../stores/radarData';
  import { formatValue, whereText } from '../utils/dataCheckLogic';
  import { initPipeline, loadRecipe, runPipeline, pipelineBusy, type Recipe } from '../stores/pipeline';

  let { onshowpanel = (_id: string) => {} }: { onshowpanel?: (id: string) => void } = $props();

  const c = $derived($current);
  const entry = $derived(c.entry);
  const report = $derived(entry?.report ?? null);
  const attention = $derived((report?.findings ?? []).filter((f) => f.severity !== 'info'));
  const facts = $derived((report?.findings ?? []).filter((f) => f.severity === 'info'));
  const fileName = $derived((c.path ?? report?.file ?? '').split(/[\\/]/).pop() ?? '');
  let applied = $state<Record<string, boolean>>({});
  let root: HTMLElement | undefined = $state();

  onMount(() => {
    initPipeline();
    void tick().then(() => root?.focus());
  });

  function close() {
    panelOpen.set(false);
  }
  function onKey(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.stopPropagation();
      close();
    }
  }

  const fmt = (e: Evidence) => formatValue(e.value, e.unit);
  const whereOf = (f: Finding) => whereText(f.scope, f.sweeps, f.where);

  function goToSweep(s: number) {
    selectedSweep.set(s);
  }

  function showFirstView() {
    const v = report?.recommendation.first_view;
    if (!v) return;
    selectedSweep.set(v.sweep);
    selectedVariable.set(v.variable);
  }

  function apply(item: RecipeItem) {
    loadRecipe(item.recipe as unknown as Recipe);
    runPipeline(true);
    applied[item.role] = true;
  }
  function review(item: RecipeItem) {
    loadRecipe(item.recipe as unknown as Recipe);
    close();
    onshowpanel('pipeline');
  }

  const SEV_LABEL = { warning: 'Warning', notice: 'Notice', info: 'Info' } as const;
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions, a11y_no_noninteractive_tabindex -->
<div
  class="dcp"
  role="dialog"
  aria-label="Data check"
  tabindex="-1"
  bind:this={root}
  onkeydown={onKey}
  transition:fly={{ y: 12, duration: 160 }}
>
  <header class="head">
    <div class="titles">
      <h2>Data check</h2>
      <div class="sub mono" title={c.path ?? ''}>{fileName}</div>
    </div>
    <button class="icon" aria-label="Close the data check" onclick={close}>&times;</button>
  </header>

  <div class="body">
    {#if !entry}
      <p class="muted">No check has run for this file.</p>
      {#if c.id}<button onclick={() => c.id && requestCheck(c.id)}>Run the check</button>{/if}
    {:else if entry.status === 'error'}
      <p class="err" role="alert">{entry.error}</p>
      {#if c.id}<button onclick={() => c.id && requestCheck(c.id)}>Try again</button>{/if}
    {:else if !report}
      <p class="muted" role="status">Looking at the data in memory... this takes about a second.</p>
    {:else}
      <p class="lede">
        <strong>{report.counts.warning}</strong> warning{report.counts.warning === 1 ? '' : 's'},
        <strong>{report.counts.notice}</strong> notice{report.counts.notice === 1 ? '' : 's'},
        {report.counts.info} fact{report.counts.info === 1 ? '' : 's'}.
        Nothing was changed. <span class="tag est">ESTIMATE</span> numbers come from a stated method, <span class="tag ret">RETRIEVED</span> numbers are measured in the data.
        <span class="mono dim">checked in {report.elapsed_ms < 100 ? `${Math.round(report.elapsed_ms)} ms` : `${(report.elapsed_ms / 1000).toFixed(1)} s`}</span>
      </p>

      {#if attention.length === 0}
        <p class="ok">No warning or notice. The data look sound for what was checked.</p>
      {/if}

      {#each attention as f (f.id)}
        <details class="finding {f.severity}">
          <summary>
            <span class="sev {f.severity}">{SEV_LABEL[f.severity]}</span>
            <span class="ftitle">{f.title}</span>
          </summary>
          <p class="fsum">{f.summary}</p>
          <p class="where mono">{whereOf(f)} <span class="dim">&middot; confidence {f.confidence.toFixed(2)}</span></p>
          {#if f.scope === 'sweep' && f.sweeps.length}
            <p class="row">
              {#each f.sweeps.slice(0, 4) as s}
                <button class="link" onclick={() => goToSweep(s)} aria-label="Go to sweep {s}">sweep {s}</button>
              {/each}
            </p>
          {/if}
          <table class="ev">
            <caption class="sr">Evidence</caption>
            <tbody>
              {#each f.evidence as e}
                <tr title={e.note ?? ''}>
                  <th scope="row">{e.label}</th>
                  <td class="mono">{fmt(e)}</td>
                  <td><span class="tag {e.kind === 'ESTIMATE' ? 'est' : 'ret'}">{e.kind}</span></td>
                </tr>
              {/each}
            </tbody>
          </table>
          <p class="method"><strong>Method.</strong> {f.method}</p>
          {#if f.advice}<p class="advice"><strong>Next.</strong> {f.advice}</p>{/if}
        </details>
      {/each}

      {#if report.recommendation.recipes.length}
        <h3>Suggested clean-up</h3>
        <p class="muted small">Built from the existing pipeline steps. Review it in the Pipelines panel, or apply it to all sweeps; the result is one new variable and the original data stay as they are.</p>
        {#each report.recommendation.recipes as item (item.role)}
          <div class="recipe">
            <div class="rname">{item.recipe.name} <span class="dim mono">on {item.recipe.target}</span></div>
            <ol>
              {#each item.reasons as r}
                <li><span class="mono step">{r.step_id}</span> {r.reason.split(' Because:')[0]}</li>
              {/each}
            </ol>
            <div class="row">
              <button class="primary" disabled={!!$pipelineBusy} onclick={() => apply(item)}>
                {applied[item.role] ? 'Apply again' : 'Apply to all sweeps'}
              </button>
              <button onclick={() => review(item)}>Review in Pipelines</button>
              {#if applied[item.role]}<span class="dim small" role="status">Sent. The new variable appears in the variable list.</span>{/if}
            </div>
          </div>
        {/each}
      {/if}

      {#if report.recommendation.advice.length}
        <h3>Other things to do</h3>
        <ul class="plain">
          {#each report.recommendation.advice as a}<li>{a.text}</li>{/each}
        </ul>
      {/if}

      {#if report.recommendation.first_view}
        {@const v = report.recommendation.first_view}
        <h3>Where to look first</h3>
        <p class="small">
          <span class="mono">{v.variable}</span> on sweep {v.sweep}{v.scale ? `, scale ${v.scale.vmin} to ${v.scale.vmax} ${v.scale.unit}` : ''}
          <span class="dim">({v.reason}{v.scale ? `; ${v.scale.note}` : ''})</span>
        </p>
        <div class="row"><button onclick={showFirstView}>Show it</button></div>
      {/if}

      {#if facts.length}
        <details class="facts">
          <summary>About this file ({facts.length})</summary>
          {#each facts as f (f.id)}
            <div class="fact">
              <div class="ftitle">{f.title}</div>
              <p class="fsum">{f.summary}</p>
              {#if f.evidence.length}
                <p class="dim mono small">{f.evidence.slice(0, 4).map((e) => `${e.label} ${fmt(e)}`).join(' · ')}</p>
              {/if}
              <p class="method small"><strong>Method.</strong> {f.method}</p>
            </div>
          {/each}
        </details>
      {/if}

      {#if report.not_checked.length}
        <details class="facts">
          <summary>Not assessed ({report.not_checked.length})</summary>
          <ul class="plain">{#each report.not_checked as t}<li>{t}</li>{/each}</ul>
        </details>
      {/if}
      {#if report.errors.length}
        <p class="err small" role="alert">Some checks failed and were skipped: {report.errors.join('; ')}</p>
      {/if}
    {/if}
  </div>

  <footer class="foot">
    <button onclick={() => c.id && requestCheck(c.id)} disabled={!c.id || entry?.status === 'running'}>Run again</button>
    <button class="quiet" onclick={() => c.id && hideForNow(c.id)}>Hide for now</button>
    <button class="quiet" onclick={() => c.id && neverShowFor(c.id, c.path)} title="The chip stays hidden for this file path, also after a restart. Command palette > Data check brings it back.">Don't show again for this file</button>
  </footer>
</div>

<style>
  .dcp {
    position: fixed;
    left: var(--space-3);
    bottom: calc(var(--statusbar-height) + var(--space-2));
    z-index: 940;
    width: min(460px, calc(100vw - 2 * var(--space-3)));
    max-height: min(74vh, 720px);
    display: flex;
    flex-direction: column;
    background: var(--c-overlay);
    border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm);
    box-shadow: var(--shadow-3);
    color: var(--c-text);
    font-size: var(--fs-sm);
    outline: none;
  }
  .head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-3); padding: var(--space-3) var(--space-4); border-bottom: 1px solid var(--c-border); background: var(--c-panel-head); }
  h2 { margin: 0; font-size: var(--fs-md); font-weight: 600; }
  h3 { margin: var(--space-4) 0 var(--space-1); font-size: var(--fs-sm); font-weight: 600; color: var(--c-text); }
  .sub { font-size: var(--fs-xs); color: var(--c-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 340px; }
  .body { overflow-y: auto; padding: var(--space-3) var(--space-4); display: flex; flex-direction: column; gap: var(--space-2); }
  .foot { display: flex; flex-wrap: wrap; gap: var(--space-2); padding: var(--space-2) var(--space-4); border-top: 1px solid var(--c-border); }
  .foot .quiet { background: transparent; border-color: transparent; color: var(--c-muted); }
  .foot .quiet:hover { color: var(--c-text); border-color: var(--c-border); }
  .icon { background: transparent; border: none; font-size: 20px; line-height: 1; color: var(--c-muted); padding: 0 var(--space-1); cursor: pointer; }
  .icon:hover { color: var(--c-text); }
  .lede { margin: 0; color: var(--c-text-dim); line-height: 1.5; }
  .ok { margin: 0; color: var(--c-nominal); }
  .muted, .dim { color: var(--c-muted); }
  .small { font-size: var(--fs-xs); }
  .err { color: var(--c-alarm); margin: 0; }
  .finding { border: 1px solid var(--c-border); border-left-width: 3px; border-radius: var(--radius-sm); padding: var(--space-2) var(--space-3); background: var(--c-field); }
  .finding.warning { border-left-color: var(--c-caution); }
  .finding.notice { border-left-color: var(--c-accent); }
  summary { cursor: pointer; display: flex; align-items: center; gap: var(--space-2); list-style: none; }
  summary::-webkit-details-marker { display: none; }
  summary:focus-visible { outline: 2px solid var(--c-accent); outline-offset: 2px; }
  .ftitle { font-weight: 600; }
  .sev { font-size: var(--fs-2xs); text-transform: uppercase; letter-spacing: 0.06em; padding: 1px 6px; border-radius: 3px; border: 1px solid currentColor; flex-shrink: 0; }
  .sev.warning { color: var(--c-caution); }
  .sev.notice { color: var(--c-accent); }
  .sev.info { color: var(--c-muted); }
  .fsum { margin: var(--space-2) 0 var(--space-1); line-height: 1.5; color: var(--c-text-dim); }
  .where { margin: 0 0 var(--space-1); font-size: var(--fs-xs); color: var(--c-text); }
  .row { display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: center; margin: var(--space-1) 0; }
  button.link { background: transparent; border: none; padding: 0; color: var(--c-accent); text-decoration: underline; cursor: pointer; font-size: var(--fs-xs); }
  table.ev { width: 100%; border-collapse: collapse; margin: var(--space-2) 0; font-size: var(--fs-xs); }
  table.ev th { text-align: left; font-weight: 400; color: var(--c-text-dim); padding: 2px var(--space-2) 2px 0; }
  table.ev td { padding: 2px 0; text-align: right; white-space: nowrap; }
  table.ev td:last-child { padding-left: var(--space-2); }
  .tag { font-size: 9.5px; padding: 0 4px; border-radius: 3px; border: 1px solid; letter-spacing: 0.04em; }
  .tag.ret { color: var(--c-nominal); border-color: rgba(var(--c-nominal-rgb), 0.5); }
  .tag.est { color: var(--c-derived); border-color: rgba(var(--c-derived-rgb), 0.55); }
  .method, .advice { margin: var(--space-1) 0 0; font-size: var(--fs-xs); color: var(--c-text-dim); line-height: 1.5; }
  .recipe { border: 1px solid var(--c-border); border-radius: var(--radius-sm); padding: var(--space-2) var(--space-3); margin-bottom: var(--space-2); }
  .rname { font-weight: 600; }
  ol { margin: var(--space-1) 0 var(--space-2); padding-left: 1.3em; line-height: 1.5; color: var(--c-text-dim); font-size: var(--fs-xs); }
  .step { color: var(--c-text); }
  ul.plain { margin: 0; padding-left: 1.2em; font-size: var(--fs-xs); color: var(--c-text-dim); line-height: 1.5; }
  .facts { margin-top: var(--space-2); }
  .facts summary { color: var(--c-muted); font-size: var(--fs-xs); }
  .fact { border-top: 1px solid var(--c-border); padding: var(--space-2) 0; }
  .sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
  button.primary { background: var(--c-accent); color: var(--c-on-accent); border-color: var(--c-accent); }
  button.primary:disabled { opacity: 0.5; }
  @media (prefers-reduced-motion: reduce) { .dcp { transition: none; } }
</style>
