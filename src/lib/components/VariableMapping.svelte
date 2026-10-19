<script lang="ts">
  import { selectedSweep } from '../stores/radarData';
  import {
    variableMapping,
    requestVariableMapping,
    setVariableOverride,
  } from '../stores/variableMapping';

  const vm = $derived($variableMapping);
  const current = $derived(vm.sweep === $selectedSweep);

  const KIND_LABEL: Record<string, string> = {
    override: 'your choice',
    cf: 'CF standard name',
    alias: 'known name',
    pattern: 'name pattern',
  };

  function onChange(role: string, event: Event) {
    const value = (event.currentTarget as HTMLSelectElement).value;
    setVariableOverride(role, value === '' ? null : value);
  }

  function kindText(kind: string | null, confidence: number): string {
    if (!kind) return '';
    return `${KIND_LABEL[kind] ?? kind} (${Math.round(confidence * 100)}%)`;
  }
</script>

<div class="varmap">
  <p class="hint">
    Which variable plays each role for modules and dual-pol tools. Automatic matching uses CF names,
    known names and patterns; pick a variable to override it for this file.
  </p>

  {#if vm.error}
    <p class="error" role="alert">{vm.error}</p>
  {/if}

  {#if vm.roles.length === 0}
    <p class="empty">
      {vm.loading ? 'Reading the file...' : 'No mapping yet.'}
      <button type="button" class="link" onclick={() => requestVariableMapping()}>Refresh</button>
    </p>
  {:else}
    <table class="map-table" class:stale={!current || vm.loading}>
      <thead>
        <tr>
          <th>Role</th>
          <th>Resolved</th>
          <th>Override</th>
        </tr>
      </thead>
      <tbody>
        {#each vm.roles as r (r.role)}
          <tr>
            <td title={r.role}>{r.label}</td>
            <td>
              {#if r.variable}
                <span class="mono">{r.variable}</span>
                {#if r.sweep !== null && r.sweep !== vm.sweep}
                  <span class="badge paired" title="Taken from the paired sweep at the same elevation">
                    sweep {r.sweep}
                  </span>
                {/if}
                <span class="kind">{kindText(r.kind, r.confidence)}{r.corrected ? ', corrected' : ''}</span>
              {:else}
                <span class="none">not found</span>
              {/if}
            </td>
            <td>
              <select
                aria-label="Override variable for {r.label}"
                value={r.override ?? ''}
                onchange={(e) => onChange(r.role, e)}
              >
                <option value="">Automatic</option>
                {#each vm.variables as v (v)}
                  <option value={v}>{v}</option>
                {/each}
                {#if r.override && !vm.variables.includes(r.override)}
                  <option value={r.override}>{r.override} (not in file)</option>
                {/if}
              </select>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
    <p class="foot">Resolved for sweep {vm.sweep}. Saved per file path.</p>
  {/if}
</div>

<style>
  .varmap {
    padding: var(--spacing-xs, 4px) var(--spacing-sm, 8px) var(--spacing-sm, 8px);
    font-size: 12px;
  }

  .hint,
  .foot,
  .empty {
    margin: 0 0 var(--spacing-xs, 4px);
    color: var(--text-muted);
    font-size: 11px;
    line-height: 1.4;
  }

  .error {
    margin: 0 0 var(--spacing-xs, 4px);
    color: var(--color-error);
    font-size: 11px;
  }

  .map-table {
    width: 100%;
    border-collapse: collapse;
  }

  .map-table.stale {
    opacity: 0.6;
  }

  .map-table th {
    text-align: left;
    font-weight: 600;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-muted);
    padding: var(--spacing-xs, 4px);
    border-bottom: 1px solid var(--glass-border);
  }

  .map-table td {
    padding: var(--spacing-xs, 4px);
    border-bottom: 1px solid var(--glass-border);
    vertical-align: top;
    color: var(--text-secondary);
  }

  .mono {
    font-family: var(--font-mono);
    color: var(--text-primary, inherit);
  }

  .kind {
    display: block;
    color: var(--text-muted);
    font-size: 10px;
  }

  .none {
    color: var(--text-muted);
    font-style: italic;
  }

  .badge {
    margin-left: 4px;
    padding: 0 5px;
    border-radius: 8px;
    font-size: 10px;
    background: rgba(var(--c-accent-rgb), 0.14);
    color: var(--text-accent, inherit);
  }

  select {
    width: 100%;
    max-width: 140px;
    font-size: 11px;
    color: var(--text-secondary);
    background: rgba(var(--c-surface-2-rgb), 0.5);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm, 4px);
    padding: 2px 4px;
  }

  .link {
    background: none;
    border: none;
    padding: 0;
    color: var(--text-accent, inherit);
    cursor: pointer;
    font-size: 11px;
    text-decoration: underline;
  }
</style>
