<script lang="ts">
  import { radarData } from '../stores/radarData';
  import CollapsiblePanel from './CollapsiblePanel.svelte';
  import RadarSweep from './RadarSweep.svelte';
  import DataTable from './DataTable.svelte';
  import MetadataViewer from './MetadataViewer.svelte';
  import VariableMapping from './VariableMapping.svelte';

  const data = $derived($radarData);
  const hasData = $derived(data.variables.length > 0);

  let showDataTable = $state(false);
  let showMetadata = $state(false);

  function formatValue(value: any): string {
    if (value === null || value === undefined) return 'null';
    if (typeof value === 'object') return JSON.stringify(value);
    return String(value);
  }

  function truncate(s: string, max: number = 60): string {
    return s.length > max ? s.substring(0, max) + '...' : s;
  }

  function openDataTable() {
    showDataTable = true;
  }

  function openMetadata() {
    showMetadata = true;
  }
</script>

<div class="data-inspector">
  {#if hasData}
    <!-- Action toolbar -->
    <div class="inspector-toolbar">
      <button class="toolbar-btn" on:click={openDataTable} title="Open data table viewer">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <rect x="3" y="3" width="18" height="18" rx="2" />
          <line x1="3" y1="9" x2="21" y2="9" />
          <line x1="3" y1="15" x2="21" y2="15" />
          <line x1="9" y1="3" x2="9" y2="21" />
        </svg>
        <span>Table View</span>
      </button>
      <button class="toolbar-btn" on:click={openMetadata} title="Open metadata tree viewer">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z" />
        </svg>
        <span>Metadata</span>
      </button>
    </div>

    <CollapsiblePanel title="Dimensions" badge={Object.keys(data.dimensions).length}>
      <table class="dim-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Size</th>
          </tr>
        </thead>
        <tbody>
          {#each Object.entries(data.dimensions) as [name, size]}
            <tr>
              <td class="mono">{name}</td>
              <td class="mono dim-size">{size}</td>
            </tr>
          {/each}
          {#if Object.keys(data.dimensions).length === 0}
            <tr><td colspan="2" class="text-muted empty-cell">No dimensions</td></tr>
          {/if}
        </tbody>
      </table>
    </CollapsiblePanel>

    <CollapsiblePanel title="Variable mapping" collapsed={true}>
      <VariableMapping />
    </CollapsiblePanel>

    <CollapsiblePanel title="Attributes" badge={Object.keys(data.attributes).length} collapsed={true}>
      <dl class="attr-list">
        {#each Object.entries(data.attributes) as [key, val]}
          <div class="attr-item">
            <dt>{key}</dt>
            <dd title={formatValue(val)}>{truncate(formatValue(val))}</dd>
          </div>
        {/each}
        {#if Object.keys(data.attributes).length === 0}
          <p class="text-muted empty-row">No attributes</p>
        {/if}
      </dl>
    </CollapsiblePanel>
  {:else}
    <div class="empty-state hud-grid">
      <div class="empty-icon">
        <RadarSweep size={96} period={7} rings={2} labels={false} />
      </div>
      <p class="empty-title">No file loaded</p>
      <p class="empty-hint">Open a radar file to inspect its contents</p>
      <div class="empty-shortcut">
        <kbd>Ctrl</kbd> + <kbd>O</kbd>
      </div>
    </div>
  {/if}
</div>

<!-- Modals -->
<DataTable bind:visible={showDataTable} />
<MetadataViewer bind:visible={showMetadata} />

<style>
  .data-inspector {
    display: flex;
    flex-direction: column;
    height: 100%;
    overflow-y: auto;
    overflow-x: hidden;
    background: transparent;
  }

  /* Inspector toolbar */
  .inspector-toolbar {
    display: flex;
    gap: var(--spacing-xs, 4px);
    padding: var(--spacing-sm, 8px) var(--spacing-md, 12px);
    border-bottom: 1px solid var(--glass-border, var(--border-color));
    background: rgba(var(--c-surface-2-rgb), 0.3);
    flex-shrink: 0;
  }

  .toolbar-btn {
    display: flex;
    align-items: center;
    gap: 5px;
    padding: 4px 10px;
    font-size: 11px;
    font-weight: 500;
    color: var(--text-secondary, #c9d1d9);
    background: rgba(var(--c-accent-rgb), 0.06);
    border: 1px solid rgba(var(--c-accent-rgb), 0.12);
    border-radius: var(--radius-sm, 4px);
    cursor: pointer;
    transition: all 120ms ease;
    white-space: nowrap;
  }

  .toolbar-btn:hover {
    background: rgba(var(--c-accent-rgb), 0.14);
    color: var(--text-primary, #e6edf3);
    border-color: rgba(var(--c-accent-rgb), 0.3);
  }

  /* Dimensions table */
  .dim-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }

  .dim-table th {
    text-align: left;
    font-weight: 600;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-muted);
    padding: var(--spacing-xs) var(--spacing-sm);
    border-bottom: 1px solid var(--glass-border, var(--border-color));
  }

  .dim-table td {
    padding: var(--spacing-xs) var(--spacing-sm);
    border-bottom: 1px solid var(--glass-border, var(--border-color));
  }

  .dim-table tr:last-child td {
    border-bottom: none;
  }

  .dim-table tr:hover td {
    background: rgba(var(--c-accent-rgb), 0.03);
  }

  .dim-size {
    text-align: right;
    color: var(--text-accent);
    font-variant-numeric: tabular-nums;
  }

  /* Attributes */
  .attr-list {
    margin: 0;
    padding: 0;
  }

  .attr-item {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: var(--spacing-xs) var(--spacing-sm);
    border-bottom: 1px solid var(--glass-border, var(--border-color));
    transition: background 120ms ease;
  }

  .attr-item:last-child {
    border-bottom: none;
  }

  .attr-item:hover {
    background: rgba(var(--c-accent-rgb), 0.03);
  }

  .attr-item dt {
    font-size: 11px;
    font-weight: 600;
    color: var(--text-secondary);
    font-family: var(--font-mono);
  }

  .attr-item dd {
    font-size: 11px;
    color: var(--text-muted);
    margin: 0;
    word-break: break-all;
  }

  .empty-row {
    padding: var(--spacing-xs) var(--spacing-sm);
    font-size: 12px;
  }

  .empty-cell {
    font-size: 12px;
    padding: var(--spacing-sm);
  }

  /* Empty state */
  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-2xl);
    color: var(--text-muted);
    text-align: center;
    height: 100%;
  }

  .empty-state .empty-icon {
    margin-bottom: var(--spacing-sm);
    opacity: 0.85;
  }

  .empty-title {
    font-size: 13px;
    color: var(--text-secondary);
    margin: 0;
  }

  .empty-hint {
    font-size: 11px;
    color: var(--text-muted);
    margin: 0;
  }

  .empty-shortcut {
    display: flex;
    align-items: center;
    gap: 4px;
    margin-top: var(--spacing-sm);
    font-size: 11px;
    color: var(--text-muted);
  }

  .empty-shortcut :global(kbd) {
    display: inline-block;
    padding: 2px 6px;
    font-family: var(--font-mono);
    font-size: 10px;
    background: rgba(var(--c-surface-0-rgb), 0.6);
    border: 1px solid var(--glass-border, var(--border-color));
    border-radius: 4px;
    color: var(--text-secondary);
  }
</style>
