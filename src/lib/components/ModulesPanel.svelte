<script lang="ts">
  /**
   * ModulesPanel — generic UI for auto-discovered plugin modules.
   *
   * Everything is rendered from the module declaration: a parameter form from
   * `params`, availability from `requires`, and results from the typed output
   * blocks (scalars / table / overlay / text).
   */
  import { onMount, tick } from 'svelte';
  import { connectionStatus, radarData, selectedSweep, selectedVariable } from '../stores/radarData';
  import {
    modulesState,
    moduleParams,
    moduleResults,
    runningModules,
    overlayVisibility,
    selectedFeature,
    moduleColors,
    moduleFields,
    displayedField,
    radarSite,
    fieldKey,
    cleanCategories,
    resolveFieldCmap,
    cmapGradientCSS,
    showFieldOnPpi,
    hideField,
    autoShowResults,
    lastFinishedModule,
    exportTableCsv,
    exportOverlayGeoJson,
    initModules,
    requestModules,
    runModule,
    runModuleAllSweeps,
    runningVolume,
    moduleVolumeResults,
    showDerivedVariable,
    removeDerived,
    cancelModuleRun,
    clearResult,
    setParam,
    resetParams,
    coerceParam,
    selectFeature,
    setOverlayVisible,
    availableVariables,
    missingRequirements,
    formatElapsed,
    type ModuleSpec,
    type ModuleParam,
    type ModuleResult,
    type ModuleBlock,
    type FieldBlock,
    type TableColumn,
    type VolumeRunResult,
  } from '../stores/modules';
  import { activeDerived, type DerivedVariable } from '../stores/derived';
  import { roleAvailability, missingRequirementsMapped, pairedHint } from '../stores/variableMapping';
  import CollapsiblePanel from './CollapsiblePanel.svelte';

  const SEARCH_THRESHOLD = 6;
  const ROW_CAP = 500;

  // After a run finishes, bring that module's card (numbers, tables, legend) into view.
  $effect(() => {
    const done = $lastFinishedModule;
    if (!done) return;
    void tick().then(() => {
      document
        .getElementById(`card-body-${done.id}`)
        ?.closest('.card')
        ?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    });
  });

  onMount(() => {
    initModules();
  });

  // ── Derived state ────────────────────────────────────────────────────────
  const isConnected = $derived($connectionStatus === 'connected');
  const hasData = $derived($radarData.variables.length > 0);
  const available = $derived(availableVariables($radarData.variables, $radarData.sweeps, $selectedSweep));

  let query = $state('');
  let expanded = $state<Record<string, boolean>>({});
  let sorts = $state<Record<string, { key: string; dir: 1 | -1 }>>({});

  const modules = $derived($modulesState.modules);
  const showSearch = $derived(modules.length > SEARCH_THRESHOLD);

  const filtered = $derived.by(() => {
    const q = query.trim().toLowerCase();
    if (!showSearch || !q) return modules;
    return modules.filter((m) =>
      [m.title, m.id, m.description, m.category].some((s) => (s ?? '').toLowerCase().includes(q)),
    );
  });

  const groups = $derived.by(() => {
    const map = new Map<string, ModuleSpec[]>();
    for (const m of filtered) {
      const list = map.get(m.category) ?? [];
      list.push(m);
      map.set(m.category, list);
    }
    return [...map.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  });

  function isOpen(id: string): boolean {
    return expanded[id] ?? modules.length <= 3;
  }

  function toggleOpen(id: string) {
    expanded = { ...expanded, [id]: !isOpen(id) };
  }

  /** Why the module cannot run right now, or null if it can. */
  function blockedReason(m: ModuleSpec): string | null {
    if (!m.available) return m.unavailable_reason ?? 'Module unavailable (missing Python package)';
    if (!isConnected) return 'Sidecar not connected';
    if (!hasData) return 'Open a radar file first';
    const missing = missingRequirementsMapped(m, available, $roleAvailability);
    if (missing.length > 0) {
      return `Missing required variable${missing.length > 1 ? 's' : ''} in sweep ${$selectedSweep}: ${missing.join(', ')}${pairedHint(missing, $roleAvailability)}`;
    }
    return null;
  }

  /**
   * Why the module cannot be run on all sweeps right now, or null if it can. Unlike a single run
   * this does not depend on the sweep on screen: one sweep carrying the inputs is enough.
   */
  function volumeBlockedReason(m: ModuleSpec): string | null {
    if (!m.available) return m.unavailable_reason ?? 'Module unavailable (missing Python package)';
    if (!isConnected) return 'Sidecar not connected';
    if (!hasData) return 'Open a radar file first';
    const sweeps = $radarData.sweeps;
    if (sweeps.length > 0) {
      const ok = sweeps.some(
        (sw) => missingRequirements(m, availableVariables($radarData.variables, sweeps, sw.index)).length === 0,
      );
      if (!ok) return `No sweep in this file carries: ${(m.requires ?? []).join(', ')}`;
    }
    return null;
  }

  const ALL_SWEEPS_TIP = 'Run on every sweep and add the result as a variable you can view in 3D';
  const VOLUME_SCOPE_TIP = 'This module analyses the whole volume in one run, so it cannot be run per sweep';

  /** Derived variables of the open file made by one module. */
  function derivedOf(list: DerivedVariable[], moduleId: string): DerivedVariable[] {
    return list.filter((d) => d.module === moduleId);
  }

  function sweepsTitle(d: DerivedVariable): string {
    return `Sweeps: ${d.sweeps.join(', ') || 'none'}`;
  }

  function evictedLabels(vol: VolumeRunResult): string {
    return vol.evicted.join(', ');
  }

  /** Scalar columns shown in the per-sweep summary (first few keys that occur). */
  function summaryCols(vol: VolumeRunResult): string[] {
    const cols: string[] = [];
    for (const s of vol.summaries) {
      for (const k of Object.keys(s.scalars)) if (!cols.includes(k)) cols.push(k);
    }
    return cols.slice(0, 5);
  }

  // ── Param helpers ────────────────────────────────────────────────────────
  function domId(m: ModuleSpec, p: ModuleParam): string {
    return `mod-${m.id}-${p.name}`.replace(/[^A-Za-z0-9_-]/g, '_');
  }

  function optValue(o: any): any {
    return o !== null && typeof o === 'object' && 'value' in o ? o.value : o;
  }

  function optLabel(o: any): string {
    return o !== null && typeof o === 'object' ? String(o.label ?? o.value) : String(o);
  }

  function parseNum(raw: string): number | null {
    if (raw.trim() === '') return null;
    const n = Number(raw);
    return Number.isFinite(n) ? n : null;
  }

  function stepFor(p: ModuleParam): number | string {
    if (typeof p.step === 'number') return p.step;
    return p.type === 'int' ? 1 : 'any';
  }

  function hasRange(p: ModuleParam): boolean {
    return typeof p.min === 'number' && typeof p.max === 'number';
  }

  // ── Result formatting ────────────────────────────────────────────────────
  function fmtNumber(n: number, digits?: number): string {
    if (typeof digits === 'number') return n.toFixed(digits);
    if (Number.isInteger(n)) return String(n);
    const a = Math.abs(n);
    if (a !== 0 && (a < 0.01 || a >= 1e6)) return n.toExponential(2);
    return n.toFixed(a >= 100 ? 1 : 2);
  }

  function fmtCell(v: any, digits?: number): string {
    if (v === null || v === undefined || (typeof v === 'number' && !Number.isFinite(v))) return '–';
    if (typeof v === 'number') return fmtNumber(v, digits);
    if (typeof v === 'boolean') return v ? 'yes' : 'no';
    if (typeof v === 'object') return JSON.stringify(v);
    return String(v);
  }

  function sortRows(rows: Record<string, any>[], s: { key: string; dir: 1 | -1 } | undefined) {
    if (!s) return rows;
    const { key, dir } = s;
    return [...rows].sort((ra, rb) => {
      const a = ra[key];
      const b = rb[key];
      const an = a === null || a === undefined;
      const bn = b === null || b === undefined;
      if (an || bn) return an === bn ? 0 : an ? 1 : -1; // nulls always last
      if (typeof a === 'number' && typeof b === 'number') return (a - b) * dir;
      return String(a).localeCompare(String(b), undefined, { numeric: true }) * dir;
    });
  }

  function toggleSort(tableKey: string, colKey: string) {
    const cur = sorts[tableKey];
    if (!cur || cur.key !== colKey) sorts = { ...sorts, [tableKey]: { key: colKey, dir: 1 } };
    else if (cur.dir === 1) sorts = { ...sorts, [tableKey]: { key: colKey, dir: -1 } };
    else {
      const { [tableKey]: _drop, ...rest } = sorts;
      sorts = rest;
    }
  }

  function ariaSort(tableKey: string, colKey: string): 'ascending' | 'descending' | 'none' {
    const s = sorts[tableKey];
    if (!s || s.key !== colKey) return 'none';
    return s.dir === 1 ? 'ascending' : 'descending';
  }

  function colLabel(c: TableColumn): string {
    return c.unit ? `${c.label} (${c.unit})` : c.label;
  }

  function featureIdSet(res: ModuleResult): Set<string> {
    return new Set(res.features.map((f) => f.id));
  }

  function rowId(row: Record<string, any>, idKey: string | undefined): string | null {
    if (!idKey) return null;
    const v = row[idKey];
    return v === null || v === undefined ? null : String(v);
  }

  function onRowKey(e: KeyboardEvent, moduleId: string, id: string) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      selectFeature(moduleId, id);
    }
  }

  /** Why a field cannot be shown right now, or null if it can. */
  function fieldWhy(
    fields: Record<string, { sweep: number }>,
    moduleId: string,
    name: string,
    sweepNow: number,
  ): string | null {
    const f = fields[fieldKey(moduleId, name)];
    if (!f) return 'Loading field...';
    if (f.sweep !== sweepNow) return `Computed for sweep ${f.sweep}; viewing sweep ${sweepNow}`;
    return null;
  }

  function pct(frac: number | undefined): string {
    return typeof frac === 'number' && Number.isFinite(frac) ? `${(frac * 100).toFixed(1)}%` : '–';
  }

  function fmtLegend(n: number | null | undefined): string {
    return typeof n === 'number' && Number.isFinite(n) ? fmtNumber(n) : '–';
  }

  function pipHint(m: ModuleSpec): string | null {
    if (m.available || m.needs.length === 0) return null;
    if ((m.unavailable_reason ?? '').includes('pip install')) return null;
    return `pip install ${m.needs.join(' ')}`;
  }

  function onClear(m: ModuleSpec) {
    clearResult(m.id);
  }

  function paramsSummaryCount(m: ModuleSpec): string {
    return m.params.length ? `${m.params.length} param${m.params.length > 1 ? 's' : ''}` : '';
  }
</script>

{#snippet paramControl(m: ModuleSpec, p: ModuleParam)}
  {@const id = domId(m, p)}
  {@const val = $moduleParams[m.id]?.[p.name]}
  {@const label = p.label ?? p.name}
  {#if p.type === 'bool'}
    <div class="field bool-field">
      <label class="checkbox-label" for={id}>
        <input
          {id}
          type="checkbox"
          checked={!!val}
          aria-describedby={p.help ? `${id}-help` : undefined}
          onchange={(e) => setParam(m.id, p.name, e.currentTarget.checked)}
        />
        <span>{label}</span>
      </label>
      {#if p.help}<p class="help" id="{id}-help">{p.help}</p>{/if}
    </div>
  {:else}
    <div class="field">
      <label for={id} title={p.help}>{label}</label>
      {#if p.type === 'select'}
        <select
          {id}
          value={val}
          aria-describedby={p.help ? `${id}-help` : undefined}
          onchange={(e) => {
            const opt = (p.options ?? []).map(optValue).find((o) => String(o) === e.currentTarget.value);
            setParam(m.id, p.name, opt ?? e.currentTarget.value);
          }}
        >
          {#each p.options ?? [] as o}
            <option value={optValue(o)}>{optLabel(o)}</option>
          {/each}
        </select>
      {:else if p.type === 'string'}
        <input
          {id}
          type="text"
          value={val ?? ''}
          aria-describedby={p.help ? `${id}-help` : undefined}
          oninput={(e) => setParam(m.id, p.name, e.currentTarget.value)}
        />
      {:else}
        <div class="num-row" class:with-range={hasRange(p)}>
          {#if hasRange(p)}
            <input
              type="range"
              class="range"
              aria-label="{label} slider"
              min={p.min}
              max={p.max}
              step={stepFor(p)}
              value={typeof val === 'number' ? val : p.default}
              oninput={(e) => setParam(m.id, p.name, Number(e.currentTarget.value))}
            />
          {/if}
          <input
            {id}
            type="number"
            class="num"
            min={p.min}
            max={p.max}
            step={stepFor(p)}
            value={val ?? ''}
            aria-describedby={p.help ? `${id}-help` : undefined}
            oninput={(e) => setParam(m.id, p.name, parseNum(e.currentTarget.value))}
            onchange={(e) => setParam(m.id, p.name, coerceParam(p, parseNum(e.currentTarget.value)))}
          />
        </div>
      {/if}
      {#if p.help}<p class="help" id="{id}-help">{p.help}</p>{/if}
    </div>
  {/if}
{/snippet}

{#snippet blockView(m: ModuleSpec, res: ModuleResult, block: ModuleBlock, bi: number)}
  <div class="block">
    {#if block.title || block.type === 'table' || block.type === 'overlay'}
      <div class="block-head">
        <div class="block-title">{block.title ?? ''}</div>
        {#if block.type === 'table'}
          <button
            type="button"
            class="mini-btn"
            onclick={() => exportTableCsv(m.id, res, block as any)}
            title="Save this table as CSV"
          >
            CSV
          </button>
        {:else if block.type === 'overlay'}
          <span title={$radarSite ? 'Save the overlay as GeoJSON (WGS84)' : 'Radar site location is unknown in this file'}>
            <button
              type="button"
              class="mini-btn"
              disabled={!$radarSite}
              onclick={() => exportOverlayGeoJson(m.id, res)}
            >
              GeoJSON
            </button>
          </span>
        {/if}
      </div>
    {/if}

    {#if block.type === 'scalars'}
      <dl class="scalars">
        {#each block.items ?? [] as item}
          <div class="scalar">
            <dt>{item.label}</dt>
            <dd>
              {fmtCell(item.value)}{#if item.unit}<span class="unit">{item.unit}</span>{/if}
            </dd>
          </div>
        {/each}
      </dl>
    {:else if block.type === 'table'}
      {@const tableKey = `${m.id}#${bi}`}
      {@const cols = (block.columns ?? []) as TableColumn[]}
      {@const rows = sortRows(block.rows ?? [], sorts[tableKey])}
      {@const ids = featureIdSet(res)}
      {@const idKey = block.id_key as string | undefined}
      <div class="table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              {#each cols as c (c.key)}
                <th scope="col" aria-sort={ariaSort(tableKey, c.key)}>
                  <button type="button" class="sort-btn" onclick={() => toggleSort(tableKey, c.key)} title="Sort by {c.label}">
                    {colLabel(c)}
                    {#if sorts[tableKey]?.key === c.key}
                      <span aria-hidden="true">{sorts[tableKey].dir === 1 ? '▲' : '▼'}</span>
                    {/if}
                  </button>
                </th>
              {/each}
            </tr>
          </thead>
          <tbody>
            {#each rows.slice(0, ROW_CAP) as row}
              {@const rid = rowId(row, idKey)}
              {#if rid !== null && ids.has(rid)}
                <tr
                  class="row-select"
                  class:selected={$selectedFeature?.module === m.id && $selectedFeature.id === rid}
                  role="button"
                  tabindex="0"
                  aria-pressed={$selectedFeature?.module === m.id && $selectedFeature.id === rid}
                  onclick={() => selectFeature(m.id, rid)}
                  onkeydown={(e) => onRowKey(e, m.id, rid)}
                >
                  {#each cols as c (c.key)}
                    <td class="mono">{fmtCell(row[c.key], c.digits)}</td>
                  {/each}
                </tr>
              {:else}
                <tr>
                  {#each cols as c (c.key)}
                    <td class="mono">{fmtCell(row[c.key], c.digits)}</td>
                  {/each}
                </tr>
              {/if}
            {/each}
          </tbody>
        </table>
      </div>
      {#if (block.rows ?? []).length > ROW_CAP}
        <p class="help">Showing first {ROW_CAP} of {block.rows.length} rows.</p>
      {:else if (block.rows ?? []).length === 0}
        <p class="help">No rows.</p>
      {/if}
    {:else if block.type === 'overlay'}
      <p class="help">
        {(block.features ?? []).length} overlay feature{(block.features ?? []).length === 1 ? '' : 's'} drawn on the PPI.
      </p>
    {:else if block.type === 'field'}
      {@const fb = block as FieldBlock}
      {@const cats = cleanCategories(fb.categories)}
      {@const why = fieldWhy($moduleFields, m.id, fb.name, $selectedSweep)}
      {@const shown = $displayedField?.module === m.id && $displayedField?.name === fb.name}
      <div class="field-block">
        {#if cats}
          <ul class="cat-list" aria-label="{fb.title ?? fb.name} categories">
            {#each cats as c (c.value)}
              <li><span class="cat-sw" style="background: {c.color}"></span>{c.label}</li>
            {/each}
          </ul>
        {:else}
          <div class="grad-legend">
            <div class="grad-bar" style="background: {cmapGradientCSS(resolveFieldCmap(fb))}"></div>
            <div class="grad-labels mono">
              <span>{fmtLegend(fb.vmin)}</span>
              {#if fb.units}<span class="unit">{fb.units}</span>{/if}
              <span>{fmtLegend(fb.vmax)}</span>
            </div>
          </div>
        {/if}
        <div class="field-actions">
          <span class="help">
            {fb.shape?.[0] ?? '?'} × {fb.shape?.[1] ?? '?'} gates, {pct(fb.valid_fraction)} valid
          </span>
          <span title={shown ? 'Show the normal variable again' : (why ?? 'Replace the PPI variable with this product')}>
            {#if shown}
              <button type="button" class="mini-btn active" onclick={() => hideField()}>
                Back to {$selectedVariable ?? 'variable'}
              </button>
            {:else}
              <button
                type="button"
                class="mini-btn"
                disabled={!!why}
                onclick={() => showFieldOnPpi(m.id, fb.name)}
              >
                Show on PPI
              </button>
            {/if}
          </span>
        </div>
        {#if why && !shown}<p class="help">{why}</p>{/if}
      </div>
    {:else if block.type === 'text'}
      <pre class="text-block">{block.body ?? ''}</pre>
    {:else}
      <p class="help">Unsupported block type: {block.type}</p>
    {/if}
  </div>
{/snippet}

{#snippet derivedSection(m: ModuleSpec, list: DerivedVariable[], vol: VolumeRunResult | undefined)}
  <div class="derived" data-testid="derived-{m.id}">
    <div class="result-head">
      <span class="result-meta">Derived variables{#if vol}{' \u00b7 '}{formatElapsed(vol.elapsed_ms)}{/if}</span>
    </div>
    {#if vol && vol.evicted.length > 0}
      <p class="help evicted" role="status">
        Removed {vol.evicted.length} older derived variable{vol.evicted.length === 1 ? '' : 's'} to make room: {evictedLabels(vol)}
      </p>
    {/if}
    {#each list as d (d.name)}
      {@const isCurrent = $selectedVariable === d.name}
      {@const skipped = vol?.derived.find((x) => x.name === d.name)?.skipped ?? []}
      <div class="derived-chip" class:current={isCurrent}>
        <div class="chip-head">
          <span class="chip-name" title={d.name}>{d.label}</span>
          <span class="chip-meta mono" title={sweepsTitle(d)}>
            {d.sweeps.length} sweep{d.sweeps.length === 1 ? '' : 's'}{#if d.units}{' \u00b7 '}{d.units}{/if}
          </span>
        </div>
        {#if d.categories}
          <ul class="cat-list" aria-label="{d.label} categories">
            {#each d.categories as c (c.value)}
              <li><span class="cat-sw" style="background: {c.color}"></span>{c.label}</li>
            {/each}
          </ul>
        {/if}
        {#if skipped.length > 0}
          <p class="help" title={skipped.map((x) => `sweep ${x.sweep}: ${x.reason}`).join('\n')}>
            Skipped {skipped.length} sweep{skipped.length === 1 ? '' : 's'}: {skipped.map((x) => x.sweep).join(', ')}
          </p>
        {/if}
        <div class="chip-actions">
          <button type="button" class="mini-btn" onclick={() => showDerivedVariable(d, '2d')}
                  title="Select it in the 2D view (switches to a sweep that carries it)">View in 2D</button>
          <button type="button" class="mini-btn" onclick={() => showDerivedVariable(d, '3d')}
                  title="Select it and switch to the 3D volume view">View in 3D</button>
          <button type="button" class="mini-btn danger" onclick={() => removeDerived(d)}
                  title="Remove this derived variable from the file">Remove</button>
        </div>
      </div>
    {/each}
    {#if vol && vol.summaries.length > 0}
      {@const cols = summaryCols(vol)}
      <details class="sum-details">
        <summary>Per-sweep summary ({vol.summaries.length} sweeps)</summary>
        <div class="table-wrap">
          <table class="data-table">
            <thead>
              <tr>
                <th scope="col">Sweep</th>
                {#each cols as c (c)}<th scope="col">{c}</th>{/each}
              </tr>
            </thead>
            <tbody>
              {#each vol.summaries as row (row.sweep)}
                <tr>
                  <td class="mono">{row.sweep}</td>
                  {#each cols as c (c)}<td class="mono">{fmtCell(row.scalars[c])}</td>{/each}
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </details>
    {/if}
  </div>
{/snippet}

<CollapsiblePanel title="Modules" badge={modules.length > 0 ? modules.length : null}>
  <div class="modules-root">
    <div class="toolbar">
      {#if showSearch}
        <input
          type="search"
          class="search"
          placeholder="Search modules"
          aria-label="Search modules"
          bind:value={query}
        />
      {:else}
        <span class="toolbar-note">
          {#if $modulesState.loading}Loading...{:else}{modules.length} module{modules.length === 1 ? '' : 's'}{/if}
        </span>
      {/if}
      <label class="auto-show" title="When a run finishes, draw its result (gridded product or outlines) on the map right away">
        <input type="checkbox" bind:checked={$autoShowResults} />
        Show on map
      </label>
      <button
        type="button"
        class="mini-btn"
        onclick={() => requestModules(true)}
        disabled={!isConnected || $modulesState.loading}
        title={$modulesState.dirs.length
          ? `Re-scan plugin directories:\n${$modulesState.dirs.join('\n')}`
          : 'Re-scan plugin directories'}
      >
        {$modulesState.loading ? 'Loading...' : 'Reload'}
      </button>
    </div>

    {#if !isConnected}
      <p class="empty-hint">Sidecar not connected</p>
    {:else if $modulesState.loaded && modules.length === 0}
      <p class="empty-hint">
        No modules found.
        {#if $modulesState.dirs.length}
          Add Python files to:
          {#each $modulesState.dirs as d}<code class="dir">{d}</code>{/each}
        {/if}
      </p>
    {:else if showSearch && filtered.length === 0}
      <p class="empty-hint">No modules match "{query}"</p>
    {/if}

    {#each groups as [category, list] (category)}
      <section class="group" aria-label={category}>
        <h3 class="group-title">{category}</h3>

        {#each list as m (m.id)}
          {@const reason = blockedReason(m)}
          {@const running = !!$runningModules[m.id]}
          {@const volRunning = !!$runningVolume[m.id]}
          {@const volReason = m.per_sweep ? volumeBlockedReason(m) : null}
          {@const derivedMine = derivedOf($activeDerived, m.id)}
          {@const vol = $moduleVolumeResults[m.id]}
          {@const res = $moduleResults[m.id]}
          {@const open = isOpen(m.id)}
          <div class="card" class:unavailable={!m.available} style="--mod-color: {$moduleColors[m.id]}">
            <div class="card-head">
              <button
                type="button"
                class="card-toggle"
                aria-expanded={open}
                aria-controls="card-body-{m.id}"
                onclick={() => toggleOpen(m.id)}
              >
                <span class="swatch" aria-hidden="true"></span>
                <span class="card-title">{m.title}</span>
                {#if m.version}<span class="ver">v{m.version}</span>{/if}
                <span class="chev" class:open aria-hidden="true">
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                    <polyline points="6 9 12 15 18 9" />
                  </svg>
                </span>
              </button>
              <span class="run-wrap" class:two-row={m.per_sweep}>
                <span title={reason ?? (running ? 'Running...' : `Run ${m.title} on the sweep on screen`)}>
                  <button
                    type="button"
                    class="run-btn"
                    onclick={() => runModule(m.id)}
                    disabled={!!reason || running}
                    aria-describedby={reason ? `reason-${m.id}` : undefined}
                  >
                    {#if running && !volRunning}<span class="spinner"></span>Running{:else}Run{/if}
                  </button>
                </span>
                {#if m.per_sweep}
                  <span title={volReason ?? (volRunning ? 'Running on all sweeps...' : ALL_SWEEPS_TIP)}>
                    <button
                      type="button"
                      class="run-btn secondary all-btn"
                      data-testid="run-all-{m.id}"
                      onclick={() => runModuleAllSweeps(m.id)}
                      disabled={!!volReason || running}
                      aria-label="{m.title}: run on all sweeps and add the result as a 3D variable"
                    >
                      {#if volRunning}<span class="spinner"></span>All sweeps{:else}All sweeps &rarr; 3D{/if}
                    </button>
                  </span>
                {:else if m.scope === 'volume'}
                  <span class="scope-hint" title={VOLUME_SCOPE_TIP} aria-label={VOLUME_SCOPE_TIP}>volume</span>
                {/if}
                {#if running}
                  <button type="button" class="run-btn cancel-btn" onclick={() => cancelModuleRun(m.id)} title="Stop waiting for this run">Cancel</button>
                {/if}
              </span>
            </div>

            {#if reason}
              <p class="reason" id="reason-{m.id}">{reason}</p>
            {/if}
            {#if pipHint(m)}
              <p class="reason"><code class="pip">{pipHint(m)}</code></p>
            {/if}

            {#if open}
              <div class="card-body" id="card-body-{m.id}">
                {#if m.description}<p class="desc">{m.description}</p>{/if}

                {#if m.requires.length > 0 || m.optional.length > 0}
                  <p class="meta">
                    {#if m.requires.length}Requires: <span class="mono">{m.requires.join(', ')}</span>{/if}
                    {#if m.optional.length}
                      {m.requires.length ? ' · ' : ''}Optional: <span class="mono">{m.optional.join(', ')}</span>
                    {/if}
                  </p>
                {/if}

                {#if m.params.length > 0}
                  <form class="params" onsubmit={(e) => e.preventDefault()}>
                    <fieldset class="params-set" disabled={!m.available}>
                      {#each m.params as p (p.name)}
                        {@render paramControl(m, p)}
                      {/each}
                      <button type="button" class="link-btn" onclick={() => resetParams(m.id)}>
                        Reset {paramsSummaryCount(m)} to defaults
                      </button>
                    </fieldset>
                  </form>
                {/if}
              </div>
            {/if}

            {#if derivedMine.length > 0}
              {@render derivedSection(m, derivedMine, vol)}
            {/if}

            {#if res}
              <div class="result">
                <div class="result-head">
                  <span class="result-meta">
                    {formatElapsed(res.elapsed_ms)} &middot; sweep {res.sweep}
                  </span>
                  <div class="result-actions">
                    {#if res.features.length > 0}
                      <label class="checkbox-label small">
                        <input
                          type="checkbox"
                          checked={$overlayVisibility[m.id] !== false}
                          onchange={(e) => setOverlayVisible(m.id, e.currentTarget.checked)}
                        />
                        <span>Show overlay</span>
                      </label>
                    {/if}
                    <button type="button" class="mini-btn danger" onclick={() => onClear(m)} title="Clear result">
                      Clear
                    </button>
                  </div>
                </div>
                {#if res.features.length > 0 && res.sweep !== $selectedSweep}
                  <p class="help">Overlay hidden: result is for sweep {res.sweep}, viewing sweep {$selectedSweep}.</p>
                {/if}
                {#each res.blocks as block, bi}
                  {@render blockView(m, res, block, bi)}
                {/each}
              </div>
            {/if}
          </div>
        {/each}
      </section>
    {/each}

    {#if $modulesState.errors.length > 0}
      <details class="load-errors">
        <summary>
          {$modulesState.errors.length} module{$modulesState.errors.length === 1 ? '' : 's'} failed to load
        </summary>
        <ul>
          {#each $modulesState.errors as e}
            <li>
              <span class="mono err-file">{e.file}</span>
              <span class="err-msg">{e.error}</span>
            </li>
          {/each}
        </ul>
        {#if $modulesState.dirs.length}
          <p class="help">
            Fix the file, then press Reload. Plugin directories:
            {#each $modulesState.dirs as d}<code class="dir">{d}</code>{/each}
          </p>
        {/if}
      </details>
    {/if}
  </div>
</CollapsiblePanel>

<style>
  .modules-root {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm, 8px);
  }

  .toolbar {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm, 8px);
  }

  .toolbar-note {
    flex: 1;
    font-size: 11px;
    color: var(--text-muted, #5c6088);
  }

  .search {
    flex: 1;
    min-width: 0;
    padding: 4px 8px;
    font-size: 12px;
    background: var(--bg-primary, #080a14);
    border: 1px solid var(--border-color, #2a2a3a);
    border-radius: var(--radius-sm, 4px);
    color: var(--text-primary, #e4e6f4);
  }

  .auto-show {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 11px;
    color: var(--text-secondary);
    white-space: nowrap;
    cursor: pointer;
  }

  .auto-show input {
    margin: 0;
  }

  .mini-btn {
    padding: 2px 8px;
    font-size: 11px;
    color: var(--text-secondary, #9da2c2);
    background: transparent;
    border: 1px solid var(--border-light, #2a2a3a);
    border-radius: 3px;
    cursor: pointer;
    white-space: nowrap;
    transition: color 150ms ease, border-color 150ms ease;
  }

  .mini-btn:hover:not(:disabled) {
    color: var(--text-primary, #e4e6f4);
    border-color: var(--accent-primary, #5b6cf7);
  }

  .mini-btn.danger:hover:not(:disabled) {
    color: var(--accent-danger, #f87171);
    border-color: var(--accent-danger, #f87171);
  }

  .mini-btn:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }

  .empty-hint {
    font-size: 12px;
    color: var(--text-muted, #5c6088);
    text-align: center;
    padding: var(--spacing-md, 12px) 0;
    margin: 0;
  }

  .group {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs, 4px);
  }

  .group-title {
    margin: var(--spacing-xs, 4px) 0 0;
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-muted, #5c6088);
  }

  .card {
    border: 1px solid var(--border-light, #2a2a3a);
    border-left: 3px solid var(--mod-color, var(--accent-primary, #5b6cf7));
    border-radius: var(--radius-sm, 4px);
    background: var(--bg-tertiary, #111628);
    padding: var(--spacing-xs, 4px) var(--spacing-sm, 8px) var(--spacing-sm, 8px);
  }

  .card-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--spacing-sm, 8px);
  }

  .card-toggle {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    justify-content: flex-start;
    gap: 6px;
    padding: 4px 0;
    font-size: 12px;
    font-weight: 600;
    color: var(--text-primary, #e4e6f4);
    background: transparent;
    border: none;
    cursor: pointer;
    text-align: left;
  }

  .card-title {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .swatch {
    flex: none;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--mod-color, var(--accent-primary, #5b6cf7));
  }

  .ver {
    flex: none;
    font-size: 10px;
    font-weight: 400;
    color: var(--text-muted, #5c6088);
  }

  .chev {
    flex: none;
    margin-left: auto;
    display: inline-flex;
    color: var(--text-muted, #5c6088);
    transition: transform 200ms ease;
    transform: rotate(-90deg);
  }

  .chev.open {
    transform: rotate(0deg);
  }

  /* Two actions (Run + All sweeps): own row under the title so the title is not squeezed out. */
  .run-wrap.two-row {
    flex: 1 0 100%;
    justify-content: flex-end;
    margin-bottom: 2px;
  }

  .run-wrap {
    flex: none;
    display: inline-flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 4px;
  }

  .run-btn.secondary {
    color: var(--accent-primary, #5b6cf7);
    background: transparent;
  }

  .run-btn.secondary:hover:not(:disabled) {
    color: #fff;
  }

  .run-btn.secondary .spinner {
    border-color: rgba(91, 108, 247, 0.35);
    border-top-color: var(--accent-primary, #5b6cf7);
  }

  .scope-hint {
    align-self: center;
    font-size: 10px;
    color: var(--text-muted, #5c6088);
    cursor: help;
    white-space: nowrap;
  }

  .derived {
    margin-top: var(--spacing-xs, 4px);
    padding-top: var(--spacing-xs, 4px);
    border-top: 1px solid var(--border-light, #2a2a3a);
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .derived-chip {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 5px 7px;
    background: var(--bg-primary, #080a14);
    border: 1px solid var(--border-light, #2a2a3a);
    border-radius: var(--radius-sm, 4px);
  }

  .derived-chip.current {
    border-color: var(--accent-primary, #5b6cf7);
  }

  .chip-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 8px;
  }

  .chip-name {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-primary, #e4e6f4);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .chip-meta {
    flex: none;
    font-size: 10px;
    color: var(--text-muted, #5c6088);
  }

  .chip-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }

  .evicted {
    color: var(--accent-warning, #fbbf24);
  }

  .sum-details summary {
    font-size: 11px;
    color: var(--text-secondary, #9da2c2);
    cursor: pointer;
  }

  .cancel-btn {
    color: var(--text-secondary);
    background: transparent;
    border-color: var(--border-light, var(--border-color));
  }

  .cancel-btn:hover:not(:disabled) {
    background: var(--bg-hover);
    color: var(--text-primary);
  }

  .run-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 3px 12px;
    font-size: 11px;
    font-weight: 600;
    color: #fff;
    background: var(--accent-primary, #5b6cf7);
    border: 1px solid var(--accent-primary, #5b6cf7);
    border-radius: var(--radius-sm, 4px);
    cursor: pointer;
    transition: background 150ms ease, opacity 150ms ease;
  }

  .run-btn:hover:not(:disabled) {
    background: var(--accent-hover, #7c8cf8);
  }

  .run-btn:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }

  .spinner {
    width: 10px;
    height: 10px;
    border: 2px solid rgba(255, 255, 255, 0.3);
    border-top-color: #fff;
    border-radius: 50%;
    animation: spin 600ms linear infinite;
    display: inline-block;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  .reason {
    margin: 0 0 2px;
    font-size: 10px;
    color: var(--accent-warning, #fbbf24);
  }

  .card-body {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs, 4px);
    margin-top: 2px;
  }

  .desc {
    margin: 0;
    font-size: 11px;
    line-height: 1.4;
    color: var(--text-secondary, #9da2c2);
  }

  .meta {
    margin: 0;
    font-size: 10px;
    color: var(--text-muted, #5c6088);
  }

  .params {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm, 8px);
    margin-top: var(--spacing-xs, 4px);
  }

  .params-set {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm, 8px);
    margin: 0;
    padding: 0;
    border: none;
    min-width: 0;
  }

  .card.unavailable {
    opacity: 0.7;
    border-left-style: dashed;
  }

  .pip {
    font-family: var(--font-mono, monospace);
    color: var(--text-primary, #e4e6f4);
    background: var(--bg-primary, #080a14);
    padding: 1px 5px;
    border-radius: 3px;
  }

  .field-block {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .cat-list {
    display: flex;
    flex-wrap: wrap;
    gap: 2px 10px;
    margin: 0;
    padding: 0;
    list-style: none;
    font-size: 11px;
    color: var(--text-secondary, #9da2c2);
  }

  .cat-list li {
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }

  .cat-sw {
    width: 11px;
    height: 11px;
    border-radius: 2px;
    border: 1px solid rgba(255, 255, 255, 0.25);
  }

  .grad-bar {
    height: 10px;
    border-radius: 2px;
    border: 1px solid var(--border-color, #2a2a3a);
  }

  .grad-labels {
    display: flex;
    justify-content: space-between;
    margin-top: 2px;
    font-size: 10px;
    color: var(--text-muted, #5c6088);
  }

  .field-actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-sm, 8px);
  }

  .mini-btn.active {
    color: var(--accent-primary, #5b6cf7);
    border-color: var(--accent-primary, #5b6cf7);
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .field label {
    font-size: 11px;
    color: var(--text-muted, #5c6088);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }

  .field input[type='number'],
  .field input[type='text'],
  .field select {
    width: 100%;
    box-sizing: border-box;
    padding: 4px 8px;
    font-family: var(--font-mono, monospace);
    font-size: 12px;
    background: var(--bg-primary, #080a14);
    border: 1px solid var(--border-light, #2a2a3a);
    border-radius: var(--radius-sm, 4px);
    color: var(--text-primary, #e4e6f4);
  }

  .num-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm, 8px);
  }

  .num-row .range {
    flex: 1;
    min-width: 0;
    accent-color: var(--accent-primary, #5b6cf7);
  }

  .num-row .num {
    width: 100%;
  }

  .num-row.with-range .num {
    width: 72px;
    flex: none;
  }

  .checkbox-label {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: var(--text-secondary, #9da2c2);
    cursor: pointer;
  }

  .field .checkbox-label {
    text-transform: none;
    letter-spacing: normal;
    font-weight: 500;
  }

  .checkbox-label.small {
    font-size: 11px;
  }

  .help {
    margin: 0;
    font-size: 10px;
    line-height: 1.35;
    color: var(--text-muted, #5c6088);
  }

  .link-btn {
    align-self: flex-start;
    padding: 0;
    font-size: 10px;
    color: var(--text-muted, #5c6088);
    background: transparent;
    border: none;
    cursor: pointer;
    text-decoration: underline;
  }

  .link-btn:hover {
    color: var(--text-primary, #e4e6f4);
  }

  /* Result */
  .result {
    margin-top: var(--spacing-sm, 8px);
    padding-top: var(--spacing-sm, 8px);
    border-top: 1px solid var(--border-color, #2a2a3a);
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm, 8px);
  }

  .result-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-sm, 8px);
    flex-wrap: wrap;
  }

  .result-meta {
    font-size: 10px;
    font-family: var(--font-mono, monospace);
    color: var(--text-muted, #5c6088);
  }

  .result-actions {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm, 8px);
  }

  .block {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .block-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--spacing-sm, 8px);
  }

  .block-title {
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--mod-color, var(--accent-primary, #5b6cf7));
  }

  .scalars {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(88px, 1fr));
    gap: 4px;
    margin: 0;
  }

  .scalar {
    padding: 4px 6px;
    background: var(--bg-primary, #080a14);
    border: 1px solid var(--border-color, #2a2a3a);
    border-radius: var(--radius-sm, 4px);
    min-width: 0;
  }

  .scalar dt {
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted, #5c6088);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .scalar dd {
    margin: 0;
    font-size: 12px;
    font-family: var(--font-mono, monospace);
    font-variant-numeric: tabular-nums;
    color: var(--text-primary, #e4e6f4);
  }

  .unit {
    margin-left: 3px;
    font-size: 9px;
    color: var(--text-muted, #5c6088);
  }

  .table-wrap {
    max-height: 220px;
    overflow: auto;
    border: 1px solid var(--border-color, #2a2a3a);
    border-radius: var(--radius-sm, 4px);
  }

  .data-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 11px;
  }

  .data-table th {
    position: sticky;
    top: 0;
    padding: 0;
    text-align: left;
    background: var(--bg-tertiary, #111628);
    border-bottom: 1px solid var(--border-light, #2a2a3a);
  }

  .sort-btn {
    width: 100%;
    padding: 4px 6px;
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    text-align: left;
    white-space: nowrap;
    color: var(--text-muted, #5c6088);
    background: transparent;
    border: none;
    border-radius: 0;
    cursor: pointer;
  }

  .sort-btn:hover {
    color: var(--text-primary, #e4e6f4);
  }

  .data-table td {
    padding: 3px 6px;
    border-bottom: 1px solid var(--border-color, #2a2a3a);
    color: var(--text-secondary, #9da2c2);
    white-space: nowrap;
  }

  .row-select {
    cursor: pointer;
  }

  .row-select:hover {
    background: var(--bg-hover, rgba(91, 108, 247, 0.08));
  }

  .row-select.selected {
    background: var(--bg-active, rgba(91, 108, 247, 0.15));
  }

  .row-select.selected td {
    color: var(--text-primary, #e4e6f4);
  }

  .row-select:focus-visible {
    outline: 1px solid var(--accent-primary, #5b6cf7);
    outline-offset: -1px;
  }

  .mono {
    font-family: var(--font-mono, monospace);
    font-variant-numeric: tabular-nums;
  }

  .text-block {
    margin: 0;
    padding: 6px 8px;
    max-height: 200px;
    overflow: auto;
    font-family: var(--font-mono, monospace);
    font-size: 11px;
    line-height: 1.4;
    white-space: pre-wrap;
    word-break: break-word;
    color: var(--text-secondary, #9da2c2);
    background: var(--bg-primary, #080a14);
    border: 1px solid var(--border-color, #2a2a3a);
    border-radius: var(--radius-sm, 4px);
  }

  /* Load errors */
  .load-errors {
    border: 1px solid var(--border-light, #2a2a3a);
    border-radius: var(--radius-sm, 4px);
    padding: 4px 8px;
    font-size: 11px;
    color: var(--text-secondary, #9da2c2);
  }

  .load-errors summary {
    cursor: pointer;
    color: var(--accent-warning, #fbbf24);
  }

  .load-errors ul {
    margin: 6px 0;
    padding-left: 14px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .err-file {
    display: block;
    color: var(--text-primary, #e4e6f4);
  }

  .err-msg {
    display: block;
    color: var(--accent-danger, #f87171);
    word-break: break-word;
  }

  .dir {
    display: block;
    margin-top: 2px;
    font-family: var(--font-mono, monospace);
    font-size: 10px;
    word-break: break-all;
    color: var(--text-secondary, #9da2c2);
  }
</style>
