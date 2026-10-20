<script lang="ts">
  import { onMount, onDestroy, untrack } from 'svelte';
  import { wsManager } from '../utils/websocket';
  import { portal } from '../utils/portal';

  let { visible = $bindable(false) }: { visible?: boolean } = $props();

  interface MetaNode {
    name: string;
    type: 'group' | 'variable' | 'coordinate' | 'attribute';
    children?: MetaNode[];
    dtype?: string;
    shape?: number[];
    units?: string;
    long_name?: string;
    min?: number | null;
    max?: number | null;
    mean?: number | null;
    value?: any;
    dims?: string[];
    attributes?: Record<string, any>;
  }

  let tree = $state<MetaNode[]>([]);
  let loading = $state(false);
  let errorMsg = $state('');
  let searchQuery = $state('');
  let expandedNodes = $state<Set<string>>(new Set());
  let copiedPath = $state('');
  let copyFeedbackTimeout: ReturnType<typeof setTimeout> | null = null;

  const filteredTree = $derived(searchQuery.trim() ? filterTree(tree, searchQuery.trim().toLowerCase()) : tree);

  /** Pure: nodes matching the query, plus the ancestors that lead to a match. */
  function filterTree(nodes: MetaNode[], query: string): MetaNode[] {
    const result: MetaNode[] = [];
    for (const node of nodes) {
      const nameMatch = node.name.toLowerCase().includes(query);
      const longNameMatch = (node.long_name || '').toLowerCase().includes(query);
      const unitsMatch = (node.units || '').toLowerCase().includes(query);
      const valueMatch = node.value !== undefined && formatValue(node.value).toLowerCase().includes(query);
      const filteredChildren = node.children ? filterTree(node.children, query) : [];
      if (nameMatch || longNameMatch || unitsMatch || valueMatch || filteredChildren.length > 0) {
        result.push({
          ...node,
          children: filteredChildren.length > 0 ? filteredChildren : node.children,
        });
      }
    }
    return result;
  }

  /** Paths of every node that has a matching descendant (so a search opens the way to its hits). */
  function matchAncestorPaths(nodes: MetaNode[], parent: string, out: Set<string>) {
    for (const node of nodes) {
      const path = parent ? `${parent}/${node.name}` : node.name;
      if (node.children?.length) {
        out.add(path);
        matchAncestorPaths(node.children, path, out);
      }
    }
  }

  // Expand along the matches when the search changes (state is written here, not inside $derived)
  $effect(() => {
    if (!searchQuery.trim()) return;
    const open = new Set<string>();
    matchAncestorPaths(filteredTree, '', open);
    untrack(() => {
      const next = new Set(expandedNodes);
      open.forEach((p) => next.add(p));
      expandedNodes = next;
    });
  });

  function requestMetadata() {
    loading = true;
    errorMsg = '';
    wsManager.send({ type: 'get_metadata_tree' });
  }

  let unsubMeta: (() => void) | null = null;

  onMount(() => {
    unsubMeta = wsManager.onMessage('metadata_tree_result', (msg: any) => {
      loading = false;
      if (msg.error) {
        errorMsg = msg.error;
        return;
      }
      tree = msg.tree || [];
      // Auto-expand root
      expandedNodes = new Set(tree.map((n: MetaNode) => n.name));
    });
  });

  onDestroy(() => {
    if (unsubMeta) unsubMeta();
    if (copyFeedbackTimeout) clearTimeout(copyFeedbackTimeout);
  });

  $effect(() => {
    if (visible) {
      requestMetadata();
    }
  });

  function toggleNode(path: string) {
    const next = new Set(expandedNodes);
    if (!next.delete(path)) next.add(path);
    expandedNodes = next;
  }

  function copyValue(value: any, path: string) {
    const text = typeof value === 'object' ? JSON.stringify(value) : String(value);
    navigator.clipboard.writeText(text).then(() => {
      copiedPath = path;
      if (copyFeedbackTimeout) clearTimeout(copyFeedbackTimeout);
      copyFeedbackTimeout = setTimeout(() => { copiedPath = ''; }, 1500);
    }).catch(() => {
      // Clipboard access denied: say so instead of failing silently
      errorMsg = '';
      copiedPath = '';
    });
  }

  function formatValue(value: any): string {
    if (value === null || value === undefined) return 'null';
    if (typeof value === 'object') return JSON.stringify(value);
    return String(value);
  }

  function truncate(s: string, max: number = 80): string {
    return s.length > max ? s.substring(0, max) + '...' : s;
  }

  function nodeIcon(type: string): string {
    switch (type) {
      case 'group': return 'G';
      case 'variable': return 'V';
      case 'coordinate': return 'C';
      case 'attribute': return 'A';
      default: return '?';
    }
  }

  function nodeIconClass(type: string): string {
    switch (type) {
      case 'group': return 'icon-group';
      case 'variable': return 'icon-var';
      case 'coordinate': return 'icon-coord';
      case 'attribute': return 'icon-attr';
      default: return '';
    }
  }

  function close() {
    visible = false;
  }
</script>


{#snippet treeNode(node: MetaNode, path: string, depth: number)}
  {@const expandable = !!node.children && node.children.length > 0}
  {@const isExpanded = expandable && expandedNodes.has(path)}
  <div class="tree-node" role="treeitem" aria-selected="false" aria-expanded={expandable ? isExpanded : undefined}>
    <button
      class="tree-row"
      class:expandable
      class:leaf={!expandable}
      on:click={() => {
        if (expandable) toggleNode(path);
        if (node.value !== undefined) copyValue(node.value, path);
      }}
      title={node.value !== undefined ? `Click to copy: ${truncate(formatValue(node.value), 120)}` : node.name}
    >
      {#if expandable}
        <span class="tree-chevron" class:open={isExpanded}>
          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
            <polyline points="9 6 15 12 9 18" />
          </svg>
        </span>
      {:else}
        <span class="tree-spacer"></span>
      {/if}

      <span class="tree-icon {nodeIconClass(node.type)}">{nodeIcon(node.type)}</span>
      <span class="tree-name">{node.name}</span>

      {#if node.dtype}
        <span class="tree-badge dtype">{node.dtype}</span>
      {/if}
      {#if node.shape}
        <span class="tree-badge shape">[{node.shape.join(' x ')}]</span>
      {/if}
      {#if node.units}
        <span class="tree-badge units">{node.units}</span>
      {/if}
      {#if node.min !== null && node.min !== undefined}
        <span class="tree-stats">
          {node.min.toFixed(2)} .. {node.max?.toFixed(2)}
          {#if node.mean !== null && node.mean !== undefined}
            (avg {node.mean.toFixed(2)})
          {/if}
        </span>
      {/if}
      {#if node.value !== undefined}
        <span class="tree-value" class:copied={copiedPath === path}>
          {copiedPath === path ? 'Copied!' : truncate(formatValue(node.value), 50)}
        </span>
      {/if}
      {#if node.long_name}
        <span class="tree-longname">{truncate(node.long_name, 40)}</span>
      {/if}
    </button>

    {#if isExpanded && node.children}
      <div class="tree-children" role="group">
        {#each node.children as child, i (`${i}:${child.name}`)}
          {@render treeNode(child, `${path}/${child.name}`, depth + 1)}
        {/each}
      </div>
    {/if}
  </div>
{/snippet}

<svelte:window on:keydown={(e) => { if (visible && e.key === 'Escape') { e.stopPropagation(); close(); } }} />

{#if visible}
<!-- On <body>: the sidebar this is declared in has a backdrop-filter, which would trap the fixed overlay. -->
<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
<div class="meta-overlay" use:portal on:click|self={close}>
  <div class="meta-panel" role="dialog" aria-modal="true" aria-label="Metadata tree">
    <div class="meta-header">
      <div class="meta-title">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z" />
        </svg>
        <span>Metadata Tree</span>
      </div>
      <div class="meta-actions">
        <button class="meta-btn" on:click={close} title="Close">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
      </div>
    </div>

    <div class="meta-search-bar">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
      </svg>
      <input
        type="text"
        placeholder="Search metadata..."
        bind:value={searchQuery}
        class="meta-search-input"
      />
      {#if searchQuery}
        <button class="meta-clear-btn" on:click={() => { searchQuery = ''; }} aria-label="Clear the search" title="Clear the search">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
      {/if}
    </div>

    <div class="meta-content">
      {#if loading}
        <div class="meta-loading">
          <div class="meta-spinner"></div>
          <span>Loading metadata...</span>
        </div>
      {:else if errorMsg}
        <div class="meta-error">{errorMsg}</div>
      {:else if filteredTree.length === 0}
        <div class="meta-empty">
          {searchQuery ? 'No results found' : 'No metadata available. Open a file first.'}
        </div>
      {:else}
        <div class="meta-tree" role="tree">
          {#each filteredTree as node (node.name)}
            {@render treeNode(node, node.name, 0)}
          {/each}
        </div>
      {/if}
    </div>
  </div>
</div>
{/if}

<style>
  .meta-overlay {
    position: fixed;
    inset: 0;
    background: rgba(var(--c-shadow-rgb), 0.6);
    backdrop-filter: blur(4px);
    -webkit-backdrop-filter: blur(4px);
    z-index: 1000;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .meta-panel {
    width: 70vw;
    height: 75vh;
    max-width: 900px;
    background: var(--bg-primary);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-lg, 12px);
    display: flex;
    flex-direction: column;
    overflow: hidden;
    box-shadow: 0 24px 80px rgba(var(--c-shadow-rgb), 0.5);
  }

  .meta-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-sm, 8px) var(--spacing-md, 12px);
    background: rgba(var(--c-surface-2-rgb), 0.8);
    border-bottom: 1px solid var(--glass-border);
    flex-shrink: 0;
  }

  .meta-title {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm, 8px);
    font-size: 13px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .meta-actions {
    display: flex;
    gap: var(--spacing-xs, 4px);
  }

  .meta-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    padding: 0; /* the global button padding would squeeze the icon to 2px */
    background: transparent;
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm, 4px);
    color: var(--text-muted);
    cursor: pointer;
    transition: all 120ms ease;
  }

  .meta-btn:hover {
    background: rgba(var(--c-accent-rgb), 0.1);
    color: var(--text-primary);
  }

  .meta-search-bar {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm, 8px);
    padding: var(--spacing-xs, 4px) var(--spacing-md, 12px);
    background: rgba(var(--c-surface-2-rgb), 0.5);
    border-bottom: 1px solid var(--glass-border);
    color: var(--text-muted);
    flex-shrink: 0;
  }

  .meta-search-input {
    flex: 1;
    padding: 4px 8px;
    font-size: 12px;
    background: rgba(var(--c-shadow-rgb), 0.3);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm, 4px);
    color: var(--text-primary);
    outline: none;
  }

  .meta-search-input:focus {
    border-color: var(--accent-primary);
  }

  .meta-clear-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 20px;
    padding: 0;
    background: transparent;
    border: none;
    color: var(--text-muted);
    cursor: pointer;
    border-radius: 50%;
  }

  .meta-clear-btn:hover {
    background: rgba(var(--c-line-rgb), 0.1);
  }

  .meta-content {
    flex: 1;
    overflow-y: auto;
    overflow-x: hidden;
  }

  .meta-loading, .meta-error, .meta-empty {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--spacing-sm, 8px);
    padding: var(--spacing-2xl, 32px);
    color: var(--text-muted);
    font-size: 13px;
  }

  .meta-error {
    color: var(--error);
  }

  .meta-spinner {
    width: 20px;
    height: 20px;
    border: 2px solid rgba(var(--c-accent-rgb), 0.2);
    border-top-color: var(--accent-primary);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  /* Tree */
  .meta-tree {
    padding: var(--spacing-sm, 8px) 0;
  }

  .tree-node {
    display: flex;
    flex-direction: column;
  }

  .tree-row {
    display: flex;
    align-items: center;
    justify-content: flex-start; /* global button styles centre their content */
    height: auto;
    backdrop-filter: none; /* hundreds of rows: no per-row blur */
    gap: 4px;
    width: 100%;
    padding: 4px var(--spacing-sm, 8px);
    background: transparent;
    border: none;
    color: var(--text-primary);
    font-size: 12px;
    text-align: left;
    cursor: default;
    transition: background 80ms ease;
    white-space: nowrap;
    overflow: hidden;
  }

  .tree-row.expandable {
    cursor: pointer;
  }

  .tree-row:hover {
    background: rgba(var(--c-accent-rgb), 0.04);
  }

  .tree-chevron {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 14px;
    height: 14px;
    flex-shrink: 0;
    transition: transform 200ms ease;
    color: var(--text-muted);
  }

  .tree-chevron.open {
    transform: rotate(90deg);
  }

  .tree-spacer {
    width: 14px;
    flex-shrink: 0;
  }

  .tree-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 18px;
    height: 18px;
    font-size: 9px;
    font-weight: 700;
    border-radius: 3px;
    flex-shrink: 0;
  }

  .icon-group {
    background: rgba(var(--c-accent-rgb), 0.15);
    color: var(--c-accent);
    border: 1px solid rgba(var(--c-accent-rgb), 0.15);
  }

  .icon-var {
    background: rgba(var(--c-accent-rgb), 0.15);
    color: var(--c-accent);
    border: 1px solid rgba(var(--c-accent-rgb), 0.15);
  }

  .icon-coord {
    background: rgba(var(--c-nominal-rgb), 0.15);
    color: var(--c-nominal);
    border: 1px solid rgba(var(--c-nominal-rgb), 0.15);
  }

  .icon-attr {
    background: rgba(var(--c-caution-rgb), 0.15);
    color: var(--c-caution);
    border: 1px solid rgba(var(--c-caution-rgb), 0.15);
  }

  .tree-name {
    font-family: var(--font-mono, monospace);
    font-weight: 500;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
  }

  .tree-badge {
    font-size: 9px;
    padding: 1px 5px;
    border-radius: 3px;
    flex-shrink: 0;
    font-family: var(--font-mono, monospace);
  }

  .tree-badge.dtype {
    background: rgba(var(--c-derived-rgb), 0.12);
    color: var(--c-derived);
    border: 1px solid rgba(var(--c-derived-rgb), 0.12);
  }

  .tree-badge.shape {
    background: rgba(var(--c-accent-rgb), 0.1);
    color: var(--c-accent);
    border: 1px solid rgba(var(--c-accent-rgb), 0.1);
  }

  .tree-badge.units {
    background: rgba(var(--c-caution-rgb), 0.12);
    color: var(--c-caution);
    border: 1px solid rgba(var(--c-caution-rgb), 0.12);
  }

  .tree-stats {
    font-size: 10px;
    color: var(--text-muted);
    font-family: var(--font-mono, monospace);
    flex-shrink: 0;
  }

  .tree-value {
    font-size: 10px;
    color: var(--text-muted);
    cursor: pointer;
    max-width: 200px;
    overflow: hidden;
    text-overflow: ellipsis;
    flex-shrink: 1;
    transition: color 150ms ease;
  }

  .tree-value:hover {
    color: var(--accent-primary);
  }

  .tree-value.copied {
    color: var(--c-nominal);
  }

  .tree-longname {
    font-size: 10px;
    color: var(--text-muted);
    font-style: italic;
    opacity: 0.7;
    flex-shrink: 1;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .tree-children {
    padding-left: 20px;
  }
</style>
