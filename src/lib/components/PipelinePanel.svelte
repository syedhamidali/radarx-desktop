<script lang="ts">
  /**
   * PipelinePanel - build a processing recipe (ordered cleaning / processing steps on one variable),
   * preview it on the current sweep (before / after / what each step removed) and run it on all
   * sweeps. The result is ONE derived variable that appears in the variable list and can be fed to
   * modules and exports. Recipes can be saved, loaded and exported as a Python script.
   */
  import { onMount } from 'svelte';
  import { connectionStatus, radarData, selectedSweep } from '../stores/radarData';
  import { canonicalVariable } from '../stores/modules';
  import {
    initPipeline,
    stepCatalog,
    catalogById,
    recipes,
    recipeErrors,
    draft,
    previewResult,
    runResult,
    pipelineBusy,
    pipelineView,
    newRecipe,
    loadRecipe,
    setDraftField,
    addStep,
    removeStep,
    moveStep,
    toggleStep,
    setStepParam,
    resetStepParams,
    previewPipeline,
    runPipeline,
    cancelPipeline,
    saveCurrentRecipe,
    deleteRecipe,
    requestScript,
    showPreviewView,
    type PipelineView,
    type StepStat,
  } from '../stores/pipeline';
  import CollapsiblePanel from './CollapsiblePanel.svelte';
  import ParamField from './ParamField.svelte';

  onMount(() => initPipeline());

  const connected = $derived($connectionStatus === 'connected');
  const hasFile = $derived($radarData.variables.length > 0);

  /** File variables the recipe can target (derived ones are results, not inputs). */
  const targets = $derived.by(() => {
    const names = [...$radarData.variables.filter((v) => !v.includes(':'))];
    const cur = $draft.target;
    if (cur && !names.some((n) => canonicalVariable(n) === canonicalVariable(cur))) names.unshift(cur);
    return names;
  });

  const grouped = $derived.by(() => {
    const out = new Map<string, typeof $stepCatalog>();
    for (const s of $stepCatalog) out.set(s.category, [...(out.get(s.category) ?? []), s]);
    return [...out.entries()];
  });

  let expanded = $state<Record<string, boolean>>({});
  let addChoice = $state('');
  let selectedRecipe = $state('');

  const currentRecipe = $derived($recipes.find((r) => r.name === selectedRecipe));

  function pickRecipe(name: string) {
    selectedRecipe = name;
    const r = $recipes.find((x) => x.name === name);
    if (r) loadRecipe(r);
  }

  function statFor(i: number): StepStat | undefined {
    return $previewResult?.steps[i];
  }

  function pct(n: number, base: number): string {
    return base > 0 ? `${((100 * n) / base).toFixed(1)} %` : '-';
  }

  const VIEWS: { id: Exclude<PipelineView, null>; label: string }[] = [
    { id: 'before', label: 'Before' },
    { id: 'after', label: 'After' },
    { id: 'removed', label: 'Removed by' },
  ];
</script>

<CollapsiblePanel title="Pipelines" badge={$draft.steps.length > 0 ? $draft.steps.length : null}>
  {#if !hasFile}
    <p class="empty">Open a radar file to build a processing pipeline.</p>
  {:else}
    <div class="stack">
      <section class="row">
        <label for="pl-recipe">Recipe</label>
        <div class="inline">
          <select id="pl-recipe" value={selectedRecipe} onchange={(e) => pickRecipe(e.currentTarget.value)}>
            <option value="">(unsaved)</option>
            {#each $recipes as r (r.name)}
              <option value={r.name}>{r.builtin ? '* ' : ''}{r.name}</option>
            {/each}
          </select>
          <button type="button" onclick={() => { selectedRecipe = ''; newRecipe($draft.target); }}>New</button>
        </div>
        {#if currentRecipe?.description}<p class="help">{currentRecipe.description}</p>{/if}
      </section>

      <section class="row">
        <label for="pl-name">Name</label>
        <input id="pl-name" type="text" value={$draft.name} oninput={(e) => setDraftField('name', e.currentTarget.value)} />
        <label for="pl-target">Target variable</label>
        <select id="pl-target" value={$draft.target} onchange={(e) => setDraftField('target', e.currentTarget.value)}>
          {#each targets as t (t)}<option value={t}>{t}</option>{/each}
        </select>
        <p class="help">
          The result is this variable after all enabled steps, stored as one new variable. Filters
          shrink one shared mask; transforms (dealias, smooth, fill) change values.
        </p>
      </section>

      <ol class="steps">
        {#each $draft.steps as s, i (s.uid)}
          {@const spec = $catalogById.get(s.step_id)}
          {@const st = statFor(i)}
          <li class="step" class:off={!s.enabled}>
            <div class="step-head">
              <input
                type="checkbox"
                checked={s.enabled}
                aria-label="Enable {spec?.title ?? s.step_id}"
                onchange={(e) => s.uid && toggleStep(s.uid, e.currentTarget.checked)}
              />
              <button
                type="button"
                class="title"
                aria-expanded={!!expanded[s.uid ?? '']}
                onclick={() => s.uid && (expanded[s.uid] = !expanded[s.uid])}
              >
                <span class="num">{i + 1}</span>
                {spec?.title ?? s.step_id}
                <span class="kind" class:transform={spec?.kind === 'transform'}>{spec?.kind ?? '?'}</span>
              </button>
              <span class="mini" title="Gates removed by this step in the last preview">
                {#if st && st.status === 'ok'}-{st.removed}{:else if st}{st.status}{/if}
              </span>
              <button type="button" class="ico" title="Move up" disabled={i === 0} onclick={() => s.uid && moveStep(s.uid, -1)}>&uarr;</button>
              <button type="button" class="ico" title="Move down" disabled={i === $draft.steps.length - 1} onclick={() => s.uid && moveStep(s.uid, 1)}>&darr;</button>
              <button type="button" class="ico" title="Remove" onclick={() => s.uid && removeStep(s.uid)}>&times;</button>
            </div>
            {#if s.uid && expanded[s.uid] && spec}
              <div class="step-body">
                <p class="help">{spec.description}</p>
                {#if spec.needs.length || spec.optional.length}
                  <p class="help">
                    {#if spec.needs.length}Needs {spec.needs.join(', ')}. {/if}
                    {#if spec.optional.length}Uses if present: {spec.optional.join(', ')}.{/if}
                    {#if spec.packages.length && spec.uses_packages.length === 0}
                      Better with {spec.packages.join(', ')} (not installed).
                    {/if}
                  </p>
                {/if}
                {#each spec.params as p (p.name)}
                  <ParamField {p} value={s.params[p.name]} idPrefix={s.uid} onchange={(v) => setStepParam(s.uid!, p.name, v)} />
                {/each}
                {#if spec.params.length > 0}
                  <button type="button" class="link" onclick={() => resetStepParams(s.uid!)}>Reset to defaults</button>
                {/if}
              </div>
            {/if}
          </li>
        {/each}
      </ol>
      {#if $draft.steps.length === 0}<p class="empty">No steps yet. Add the first one below.</p>{/if}

      <div class="inline">
        <select aria-label="Step to add" bind:value={addChoice}>
          <option value="">Add step...</option>
          {#each grouped as [cat, items] (cat)}
            <optgroup label={cat}>
              {#each items as it (it.id)}<option value={it.id}>{it.title}</option>{/each}
            </optgroup>
          {/each}
        </select>
        <button type="button" disabled={!addChoice} onclick={() => { addStep(addChoice); addChoice = ''; }}>Add</button>
      </div>

      <label class="check">
        <input type="checkbox" checked={$draft.apply_to_all} onchange={(e) => setDraftField('apply_to_all', e.currentTarget.checked)} />
        <span>Apply the final mask to all loaded fields (stores one extra variable per field)</span>
      </label>

      <div class="inline actions">
        <button type="button" disabled={!connected || !!$pipelineBusy || $draft.steps.length === 0} onclick={previewPipeline}>
          {$pipelineBusy === 'preview' ? 'Previewing...' : `Preview sweep ${$selectedSweep}`}
        </button>
        <button type="button" class="primary" disabled={!connected || !!$pipelineBusy || $draft.steps.length === 0} onclick={() => runPipeline(true)}>
          {$pipelineBusy === 'run' ? 'Running...' : 'Run on all sweeps'}
        </button>
        {#if $pipelineBusy}<button type="button" onclick={cancelPipeline}>Cancel</button>{/if}
      </div>

      <div class="inline">
        <button type="button" disabled={!connected || !$draft.name.trim() || $draft.steps.length === 0} onclick={saveCurrentRecipe}>Save recipe</button>
        <button type="button" disabled={!currentRecipe || currentRecipe.builtin} onclick={() => currentRecipe && deleteRecipe(currentRecipe.name)}>Delete</button>
        <button type="button" disabled={!connected || $draft.steps.length === 0} onclick={requestScript}>Export script</button>
      </div>
      {#each $recipeErrors as e}<p class="err">{e.file}: {e.error}</p>{/each}

      {#if $previewResult}
        <section class="result">
          <h4>Preview, sweep {$previewResult.sweep}</h4>
          <div class="inline views" role="group" aria-label="Show on the map">
            {#each VIEWS as v}
              <button type="button" class:active={$pipelineView === v.id} onclick={() => showPreviewView(v.id)}>{v.label}</button>
            {/each}
            <button type="button" onclick={() => showPreviewView(null)}>Hide</button>
          </div>
          <p class="help">
            {$previewResult.valid_before} valid gates before, {$previewResult.valid_after} after
            ({pct($previewResult.valid_after, $previewResult.valid_before)} kept) in
            {Math.round($previewResult.elapsed_ms)} ms.
          </p>
          {#each $previewResult.notes as n}<p class="help">{n}</p>{/each}
          {@render statsTable($previewResult.steps)}
        </section>
      {/if}

      {#if $runResult}
        <section class="result">
          <h4>Result: {$runResult.name ?? $runResult.recipe}</h4>
          <p class="help">
            Stored as one variable on {$runResult.sweeps.length} sweep{$runResult.sweeps.length === 1 ? '' : 's'} in
            {($runResult.elapsed_ms / 1000).toFixed(1)} s. Pick it in the variable list; to use it in a
            module send it as the module's input for this role.
          </p>
          {@render statsTable($runResult.steps)}
          {#if $runResult.skipped.length > 0}
            <details>
              <summary>Skipped {$runResult.skipped.length} sweep{$runResult.skipped.length === 1 ? '' : 's'}</summary>
              <ul class="skipped">
                {#each $runResult.skipped as s}<li>Sweep {s.sweep}: {s.reason}</li>{/each}
              </ul>
            </details>
          {/if}
        </section>
      {/if}
    </div>
  {/if}
</CollapsiblePanel>

{#snippet statsTable(rows: StepStat[])}
  <table class="stats">
    <thead>
      <tr><th>#</th><th>Step</th><th>Removed</th><th>Valid</th></tr>
    </thead>
    <tbody>
      {#each rows as r (r.index)}
        <tr class:muted={r.status !== 'ok'} title={[r.reason, ...r.notes].filter(Boolean).join('\n')}>
          <td>{r.index + 1}</td>
          <td>
            {r.title}
            {#if r.status === 'skipped'}<span class="tag">skipped</span>{/if}
            {#if r.status === 'disabled'}<span class="tag">off</span>{/if}
            {#if r.filled > 0}<span class="tag">+{r.filled} filled</span>{/if}
            {#if r.modified > 0}<span class="tag">{r.modified} changed</span>{/if}
          </td>
          <td class="n">{r.removed}</td>
          <td class="n">{r.valid_pct.toFixed(1)} %</td>
        </tr>
      {/each}
    </tbody>
  </table>
{/snippet}

<style>
  .stack {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm, 8px);
  }
  .row {
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  label {
    font-size: 11px;
    color: var(--text-secondary, #9da2c2);
  }
  .check {
    display: flex;
    gap: 6px;
    align-items: flex-start;
    cursor: pointer;
  }
  input[type='text'],
  select {
    width: 100%;
    box-sizing: border-box;
    font-size: 12px;
    padding: 3px 6px;
    color: var(--text-primary, #e4e6f4);
    background: var(--bg-primary, #080a14);
    border: 1px solid var(--border-color, #2a2e4a);
    border-radius: 4px;
  }
  .inline {
    display: flex;
    gap: 6px;
    align-items: center;
    flex-wrap: wrap;
  }
  .inline select {
    flex: 1;
    min-width: 0;
    width: auto;
  }
  button {
    font-size: 11px;
    padding: 3px 8px;
    color: var(--text-primary, #e4e6f4);
    background: var(--bg-tertiary, #1a1d33);
    border: 1px solid var(--border-color, #2a2e4a);
    border-radius: 4px;
    cursor: pointer;
  }
  button:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }
  button.primary {
    background: var(--accent, #4f6bed);
    border-color: var(--accent, #4f6bed);
  }
  button.active {
    outline: 1px solid var(--accent, #4f6bed);
  }
  button.link {
    align-self: flex-start;
    background: none;
    border: none;
    padding: 0;
    color: var(--accent, #4f6bed);
  }
  .actions {
    margin-top: 2px;
  }
  .help {
    margin: 0;
    font-size: 10px;
    color: var(--text-muted, #5c6088);
  }
  .empty {
    margin: 0;
    font-size: 11px;
    color: var(--text-muted, #5c6088);
  }
  .err {
    margin: 0;
    font-size: 10px;
    color: var(--error, #ef4444);
  }
  .steps {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .step {
    border: 1px solid var(--border-color, #2a2e4a);
    border-radius: 6px;
    background: var(--bg-secondary, #10122a);
  }
  .step.off .title {
    opacity: 0.5;
    text-decoration: line-through;
  }
  .step-head {
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 4px 6px;
  }
  .step-head input[type='checkbox'] {
    flex: none;
    width: 14px;
    height: 14px;
    margin: 0;
  }
  .title {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    white-space: nowrap;
    justify-content: flex-start;
    text-align: left;
    background: none;
    border: none;
    padding: 0;
    font-size: 12px;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .num {
    flex: none;
    min-width: 12px;
    color: var(--text-muted, #5c6088);
    font-variant-numeric: tabular-nums;
  }
  .kind {
    flex: 0 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    font-size: 9px;
    padding: 0 4px;
    border-radius: 3px;
    background: var(--bg-primary, #080a14);
    color: var(--text-muted, #5c6088);
  }
  .kind.transform {
    color: var(--accent, #4f6bed);
  }
  .mini {
    flex: none;
    font-size: 10px;
    font-variant-numeric: tabular-nums;
    color: var(--text-muted, #5c6088);
  }
  .ico {
    padding: 0 6px;
    line-height: 18px;
  }
  .step-body {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 4px 8px 8px;
    border-top: 1px solid var(--border-color, #2a2e4a);
  }
  .result h4 {
    margin: 4px 0;
    font-size: 12px;
  }
  .views button {
    flex: 1;
  }
  .stats {
    width: 100%;
    border-collapse: collapse;
    font-size: 11px;
  }
  .stats th {
    text-align: left;
    font-weight: 500;
    color: var(--text-muted, #5c6088);
  }
  .stats td,
  .stats th {
    padding: 2px 4px;
  }
  .stats .n {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .stats .muted {
    color: var(--text-muted, #5c6088);
  }
  .tag {
    font-size: 9px;
    padding: 0 4px;
    margin-left: 4px;
    border-radius: 3px;
    background: var(--bg-primary, #080a14);
  }
  .skipped {
    margin: 4px 0 0;
    padding-left: 16px;
    font-size: 10px;
    color: var(--text-muted, #5c6088);
  }
</style>
