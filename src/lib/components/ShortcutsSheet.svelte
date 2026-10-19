<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { fade, fly } from 'svelte/transition';
  import { getBoundKey } from '../utils/shortcuts';
  import { focusTrap } from '../utils/focusTrap';
  import { settings } from '../stores/settings';

  let { visible = $bindable(false), onclose }: { visible?: boolean; onclose?: () => void } = $props();

  const isMac =
    typeof navigator !== 'undefined' && /Mac|iPhone|iPad|iPod/.test(navigator.platform ?? '');

  let searchQuery = $state('');
  let searchInput = $state<HTMLInputElement>(undefined as any);

  // Extended shortcuts list including ones not in the registry
  interface DisplayShortcut {
    description: string;
    keys: string;
    category: string;
  }

  // Configurable bindings are read from the settings so a rebinding shows up here too.
  const bound = (id: string) => ($settings.shortcuts[id]?.key ?? getBoundKey(id));
  const extraShortcuts: DisplayShortcut[] = $derived([
    { description: 'Open file', keys: bound('open-file'), category: 'File' },
    { description: 'Command palette', keys: bound('command-palette'), category: 'Navigation' },
    { description: 'Toggle left sidebar', keys: bound('toggle-left-sidebar'), category: 'View' },
    { description: 'Toggle right sidebar', keys: bound('toggle-right-sidebar'), category: 'View' },
    { description: 'Open settings', keys: bound('settings'), category: 'Navigation' },
    { description: 'Play / Pause animation', keys: bound('play-pause'), category: 'Playback' },
    { description: 'Keyboard shortcuts', keys: '?', category: 'Navigation' },
    { description: 'About dialog', keys: 'mod+shift+a', category: 'Navigation' },
    { description: 'Close dialog / Cancel', keys: 'Escape', category: 'Navigation' },
    { description: 'Switch to 2D view', keys: '2', category: 'View' },
    { description: 'Switch to 3D view', keys: '3', category: 'View' },
    { description: 'Show export panel', keys: 'mod+shift+e', category: 'Tools' },
    { description: 'Next sweep', keys: 'ArrowRight', category: 'Playback' },
    { description: 'Previous sweep', keys: 'ArrowLeft', category: 'Playback' },
    { description: 'First sweep', keys: 'Home', category: 'Playback' },
    { description: 'Last sweep', keys: 'End', category: 'Playback' },
  ]);

  const categories = ['Navigation', 'File', 'View', 'Tools', 'Playback'];

  function formatDisplay(keys: string): string {
    return keys
      .split('+')
      .map((part) => {
        const p = part.trim().toLowerCase();
        if (p === 'mod') return isMac ? '\u2318' : 'Ctrl';
        if (p === 'shift') return isMac ? '\u21E7' : 'Shift';
        if (p === 'alt') return isMac ? '\u2325' : 'Alt';
        if (p === 'space') return 'Space';
        if (p === 'arrowright') return '\u2192';
        if (p === 'arrowleft') return '\u2190';
        if (p === 'escape') return 'Esc';
        if (p === 'home') return 'Home';
        if (p === 'end') return 'End';
        return p.toUpperCase();
      })
      .join(isMac ? '' : '+');
  }

  const filteredByCategory = $derived(categories.map((cat) => ({
    name: cat,
    shortcuts: extraShortcuts.filter((s) => {
      if (s.category !== cat) return false;
      if (!searchQuery) return true;
      const q = searchQuery.toLowerCase();
      return s.description.toLowerCase().includes(q) || s.keys.toLowerCase().includes(q);
    }),
  })).filter((c) => c.shortcuts.length > 0));

  function close() {
    visible = false;
    searchQuery = '';
    onclose?.();
  }

  function handleOverlayClick(e: MouseEvent) {
    if ((e.target as HTMLElement).classList.contains('shortcuts-overlay')) {
      close();
    }
  }

  function handleKeyDown(e: KeyboardEvent) {
    if (!visible) return;
    if (e.key === 'Escape') {
      close();
      e.preventDefault();
      e.stopPropagation();
    }
  }

  $effect(() => {
    if (visible && searchInput) {
      setTimeout(() => searchInput?.focus(), 50);
    }
  });
</script>

<svelte:window on:keydown={handleKeyDown} />

{#if visible}
  <!-- svelte-ignore a11y-click-events-have-key-events -->
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div class="shortcuts-overlay" on:click={handleOverlayClick} transition:fade={{ duration: 150 }}>
    <div class="shortcuts-dialog" role="dialog" aria-modal="true" aria-label="Keyboard shortcuts" transition:fly={{ y: -20, duration: 200 }} use:focusTrap={{ initial: '.search-input' }}>
      <div class="shortcuts-header">
        <h2>Keyboard Shortcuts</h2>
        <div class="shortcuts-search">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
          </svg>
          <input
            bind:this={searchInput}
            bind:value={searchQuery}
            type="text"
            placeholder="Filter shortcuts..."
            aria-label="Filter shortcuts"
            class="search-input"
          />
        </div>
        <button class="close-btn" on:click={close} title="Close (Esc)" aria-label="Close">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
          </svg>
        </button>
      </div>

      <div class="shortcuts-body">
        <div class="shortcuts-grid">
          {#each filteredByCategory as category}
            <div class="shortcut-category">
              <h3 class="category-title">{category.name}</h3>
              {#each category.shortcuts as shortcut}
                <div class="shortcut-item">
                  <span class="shortcut-label">{shortcut.description}</span>
                  <kbd class="shortcut-keys">{formatDisplay(shortcut.keys)}</kbd>
                </div>
              {/each}
            </div>
          {/each}
        </div>
        {#if filteredByCategory.length === 0}
          <div class="no-results">No shortcuts matching "{searchQuery}"</div>
        {/if}
      </div>

      <div class="shortcuts-footer">
        <span class="footer-hint">Press <kbd>?</kbd> to toggle this sheet</span>
      </div>
    </div>
  </div>
{/if}

<style>
  .shortcuts-overlay {
    position: fixed;
    inset: 0;
    z-index: 950;
    background: rgba(0, 0, 0, 0.6);
    display: flex;
    align-items: center;
    justify-content: center;
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
  }

  .shortcuts-dialog {
    width: 680px;
    max-width: 90vw;
    max-height: 80vh;
    background: var(--overlay-bg);
    backdrop-filter: blur(40px);
    -webkit-backdrop-filter: blur(40px);
    border: 1px solid rgba(var(--c-line-rgb), 0.15);
    border-radius: var(--radius-xl);
    box-shadow: var(--shadow-lg), 0 0 60px rgba(var(--c-accent-rgb), 0.08);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .shortcuts-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-md);
    padding: var(--spacing-lg);
    border-bottom: 1px solid var(--glass-border);
  }

  .shortcuts-header h2 {
    font-size: 15px;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0;
    white-space: nowrap;
  }

  .shortcuts-search {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 1;
    padding: 6px 10px;
    background: var(--field-bg);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-md);
    color: var(--text-muted);
  }

  .shortcuts-search:focus-within {
    border-color: var(--accent-primary);
    box-shadow: 0 0 0 2px rgba(var(--c-accent-rgb), 0.15);
  }

  .search-input {
    background: transparent;
    border: none;
    outline: none;
    color: var(--text-primary);
    font-size: 12px;
    flex: 1;
    height: auto;
    padding: 0;
  }

  .close-btn {
    background: transparent;
    border: none;
    color: var(--text-muted);
    padding: 4px;
    height: auto;
    cursor: pointer;
    border-radius: var(--radius-sm);
    flex-shrink: 0;
  }

  .close-btn:hover {
    color: var(--text-primary);
    background: var(--bg-hover);
  }

  .shortcuts-body {
    flex: 1;
    overflow-y: auto;
    padding: var(--spacing-lg);
  }

  .shortcuts-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--spacing-xl);
  }

  .shortcut-category {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .category-title {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--accent-primary);
    margin-bottom: var(--spacing-sm);
    padding-bottom: var(--spacing-xs);
    border-bottom: 1px solid rgba(var(--c-accent-rgb), 0.15);
  }

  .shortcut-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 5px var(--spacing-sm);
    border-radius: var(--radius-sm);
    transition: background var(--transition-fast);
  }

  .shortcut-item:hover {
    background: var(--bg-hover);
  }

  .shortcut-label {
    font-size: 12px;
    color: var(--text-secondary);
  }

  .shortcut-keys {
    font-size: 11px;
    padding: 2px 8px;
    background: var(--field-bg);
    border: 1px solid var(--glass-border);
    border-radius: 4px;
    color: var(--text-primary);
    font-family: var(--font-mono);
    white-space: nowrap;
  }

  .no-results {
    text-align: center;
    color: var(--text-muted);
    font-size: 13px;
    padding: var(--spacing-2xl);
  }

  .shortcuts-footer {
    padding: var(--spacing-sm) var(--spacing-lg);
    border-top: 1px solid var(--glass-border);
    text-align: center;
  }

  .footer-hint {
    font-size: 11px;
    color: var(--text-muted);
  }

  .footer-hint kbd {
    font-size: 10px;
    padding: 1px 5px;
  }
</style>
