<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { recentFiles } from '../stores/recentFiles';
  import type { RecentFile } from '../stores/settings';
  import { workspaceStore, PRESET_WORKSPACES } from '../stores/workspace';
  import { webUi } from '../stores/webInfo';

  const dispatch = createEventDispatcher<{
    'open-file': void;
    'open-cloud': void;
    'open-recent': { path: string };
    'load-workspace': { name: string };
  }>();

  const recent = $derived($recentFiles);

  // User-saved layouts only (the presets are in the toolbar switcher); follows the store.
  const savedWorkspaces = $derived(
    Object.keys($workspaceStore.workspaces).filter((n) => !PRESET_WORKSPACES[n]),
  );

  const APP_VERSION = '0.1.0';

  const shortcuts = [
    { keys: 'Ctrl/\u2318 + O', desc: 'Open file' },
    { keys: 'Ctrl/\u2318 + K', desc: 'Command palette' },
    { keys: 'Ctrl/\u2318 + B', desc: 'Toggle left sidebar' },
    { keys: 'Ctrl/\u2318 + E', desc: 'Toggle right sidebar' },
    { keys: 'Space', desc: 'Play/Pause animation' },
  ];

  const webTips = [
    'Open the latest scan from the cloud: no file needed',
    'Or upload your own radar file (any extension, e.g. NEXRAD Level II)',
    'Drag & drop files anywhere on the page to upload them',
    'Exports are downloaded by your browser',
  ];

  const tips = [
    'Drag & drop radar files directly onto the window',
    'Use the command palette for quick access to any action',
    'Save workspaces to preserve your preferred layout',
    'Switch between 2D PPI and 3D volume views',
  ];

  function formatTime(epoch: number): string {
    const d = new Date(epoch);
    const now = Date.now();
    const diff = now - epoch;
    if (diff < 60_000) return 'Just now';
    if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
    if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
    if (diff < 604_800_000) return `${Math.floor(diff / 86_400_000)}d ago`;
    return d.toLocaleDateString();
  }

  function getFileIcon(name: string): string {
    const ext = name.split('.').pop()?.toLowerCase() ?? '';
    if (['nc', 'ncf', 'cdf'].includes(ext)) return '\u{1F4CA}';
    if (['h5', 'hdf5', 'hdf'].includes(ext)) return '\u{1F5C4}';
    if (['gz', 'bz2'].includes(ext)) return '\u{1F4E6}';
    return '\u{1F4C1}';
  }
</script>

<div class="welcome-screen">
  <div class="welcome-inner">
    <!-- Hero -->
    <div class="welcome-hero">
      <div class="welcome-logo">
        <span class="logo-x">x</span><span class="logo-rest">radar</span>
        <span class="logo-desktop">desktop</span>
      </div>
      <p class="welcome-subtitle">Open-source radar data visualization{$webUi ? ' in your browser' : ''}</p>
      <span class="welcome-version">v{APP_VERSION}</span>
    </div>

    <!-- Quick actions -->
    <div class="welcome-actions">
      {#if $webUi}
        <button class="action-card" on:click={() => dispatch('open-cloud')} data-testid="welcome-open-cloud">
          <div class="action-icon">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
              <path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z" />
            </svg>
          </div>
          <div class="action-text">
            <span class="action-title">Open latest from the cloud</span>
            <span class="action-desc">Most recent NEXRAD scan, e.g. KTLX</span>
          </div>
        </button>
      {/if}
      <button class="action-card" on:click={() => dispatch('open-file')} data-testid="welcome-open-file">
        <div class="action-icon">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
            <line x1="12" y1="11" x2="12" y2="17" />
            <line x1="9" y1="14" x2="15" y2="14" />
          </svg>
        </div>
        <div class="action-text">
          <span class="action-title">{$webUi ? 'Upload a file' : 'Open File'}</span>
          <span class="action-desc">{$webUi ? 'Choose a radar file from your computer' : 'Browse for a radar data file'}</span>
        </div>
        {#if !$webUi}<kbd>Ctrl+O</kbd>{/if}
      </button>

      {#if savedWorkspaces.length > 0}
        <button class="action-card" on:click={() => dispatch('load-workspace', { name: savedWorkspaces[0] })}>
          <div class="action-icon">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
              <rect x="3" y="3" width="7" height="7" rx="1" />
              <rect x="14" y="3" width="7" height="7" rx="1" />
              <rect x="3" y="14" width="7" height="7" rx="1" />
              <rect x="14" y="14" width="7" height="7" rx="1" />
            </svg>
          </div>
          <div class="action-text">
            <span class="action-title">Load Workspace</span>
            <span class="action-desc">Restore “{savedWorkspaces[0]}”</span>
          </div>
        </button>
      {/if}
    </div>

    <div class="welcome-grid" class:single={$webUi}>
      <!-- Recent files (local paths: desktop only) -->
      {#if !$webUi}
      <div class="welcome-card">
        <div class="card-header">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" />
          </svg>
          Recent Files
        </div>
        <div class="card-body">
          {#if recent.length > 0}
            <div class="recent-list">
              {#each recent.slice(0, 8) as file}
                <button class="recent-item" on:click={() => dispatch('open-recent', { path: file.path })}>
                  <span class="recent-icon">{getFileIcon(file.name)}</span>
                  <div class="recent-info">
                    <span class="recent-name">{file.name}</span>
                    <span class="recent-path" title={file.path}>{file.path}</span>
                  </div>
                  <span class="recent-time">{formatTime(file.openedAt)}</span>
                </button>
              {/each}
            </div>
          {:else}
            <div class="empty-state">
              <span class="empty-icon">
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.2">
                  <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
                </svg>
              </span>
              <span>No recent files</span>
              <span class="empty-hint">Open a file to get started</span>
            </div>
          {/if}
        </div>
      </div>

      {/if}

      <!-- Right column -->
      <div class="welcome-right-col">
        <!-- Getting started -->
        <div class="welcome-card">
          <div class="card-header">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="12" cy="12" r="10" /><line x1="12" y1="16" x2="12" y2="12" /><line x1="12" y1="8" x2="12.01" y2="8" />
            </svg>
            Getting Started
          </div>
          <div class="card-body">
            <ul class="tips-list">
              {#each ($webUi ? webTips : tips) as tip}
                <li class="tip-item">{tip}</li>
              {/each}
            </ul>
          </div>
        </div>

        <!-- Keyboard shortcuts -->
        <div class="welcome-card">
          <div class="card-header">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="2" y="4" width="20" height="16" rx="2" /><line x1="6" y1="8" x2="6.01" y2="8" />
              <line x1="10" y1="8" x2="10.01" y2="8" /><line x1="14" y1="8" x2="14.01" y2="8" />
              <line x1="18" y1="8" x2="18.01" y2="8" /><line x1="8" y1="12" x2="16" y2="12" />
            </svg>
            Keyboard Shortcuts
          </div>
          <div class="card-body">
            <div class="shortcuts-list">
              {#each shortcuts as sc}
                <div class="shortcut-row">
                  <kbd class="shortcut-key">{sc.keys}</kbd>
                  <span class="shortcut-desc">{sc.desc}</span>
                </div>
              {/each}
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<style>
  .welcome-screen {
    display: flex;
    /* Centre with auto margins on the child (not align-items: center), so content taller than the
       window starts at the top and scrolls instead of being clipped above the viewport. */
    width: 100%;
    height: 100%;
    overflow-y: auto;
    background: radial-gradient(ellipse at 50% 30%, rgba(91, 108, 247, 0.06) 0%, transparent 70%);
  }

  .welcome-inner {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--spacing-xl);
    padding: var(--spacing-2xl);
    max-width: 780px;
    width: 100%;
    margin: auto;
    animation: fade-in 400ms ease-out;
  }

  /* Hero */
  .welcome-hero {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--spacing-sm);
    padding-bottom: var(--spacing-md);
  }

  .welcome-logo {
    font-size: 32px;
    font-weight: 600;
    letter-spacing: -0.03em;
    user-select: none;
  }

  .logo-x {
    color: var(--accent-primary);
    font-weight: 700;
    text-shadow: 0 0 24px rgba(91, 108, 247, 0.5);
  }

  .logo-rest {
    color: var(--text-primary);
  }

  .logo-desktop {
    color: var(--text-muted);
    font-weight: 400;
    margin-left: 6px;
    font-size: 22px;
  }

  .welcome-subtitle {
    font-size: 13px;
    color: var(--text-muted);
    letter-spacing: 0.02em;
  }

  .welcome-version {
    font-size: 10px;
    color: var(--text-muted);
    background: rgba(91, 108, 247, 0.08);
    border: 1px solid rgba(91, 108, 247, 0.12);
    border-radius: 10px;
    padding: 1px 8px;
    font-family: var(--font-mono);
  }

  /* Quick actions */
  .welcome-actions {
    display: flex;
    gap: var(--spacing-md);
    width: 100%;
    max-width: 540px;
  }

  .action-card {
    flex: 1;
    display: flex;
    align-items: center;
    gap: var(--spacing-md);
    padding: var(--spacing-lg);
    background: var(--card-bg);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-lg);
    cursor: pointer;
    transition: all var(--transition-normal);
    text-align: left;
    height: auto;
  }

  .action-card:hover {
    border-color: rgba(91, 108, 247, 0.25);
    background: rgba(91, 108, 247, 0.06);
    box-shadow: 0 0 30px rgba(91, 108, 247, 0.08), var(--shadow-md);
    transform: translateY(-1px);
  }

  .action-icon {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 44px;
    height: 44px;
    border-radius: var(--radius-md);
    background: rgba(91, 108, 247, 0.1);
    border: 1px solid rgba(91, 108, 247, 0.15);
    color: var(--accent-primary);
    flex-shrink: 0;
  }

  .action-text {
    display: flex;
    flex-direction: column;
    gap: 2px;
    flex: 1;
  }

  .action-title {
    font-size: 13px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .action-desc {
    font-size: 11px;
    color: var(--text-muted);
  }

  .action-card kbd {
    font-size: 9px;
    opacity: 0.5;
  }

  /* Grid */
  .welcome-grid.single {
    grid-template-columns: minmax(0, 560px);
    justify-content: center;
  }

  .welcome-grid {
    display: grid;
    grid-template-columns: 1.2fr 1fr;
    gap: var(--spacing-lg);
    width: 100%;
  }

  .welcome-right-col {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-lg);
  }

  /* Cards */
  .welcome-card {
    background: var(--card-bg);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-lg);
    overflow: hidden;
    transition: border-color var(--transition-fast);
  }

  .welcome-card:hover {
    border-color: var(--border-light);
  }

  .card-header {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-sm) var(--spacing-md);
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-secondary);
    border-bottom: 1px solid var(--glass-border);
    background: var(--panel-header-bg);
  }

  .card-body {
    padding: var(--spacing-sm);
  }

  /* Recent files list */
  .recent-list {
    display: flex;
    flex-direction: column;
  }

  .recent-item {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    padding: var(--spacing-xs) var(--spacing-sm);
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: all var(--transition-fast);
    background: transparent;
    border: none;
    text-align: left;
    height: auto;
    width: 100%;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }

  .recent-item:hover {
    background: var(--bg-hover);
    border: none;
    box-shadow: none;
  }

  .recent-icon {
    font-size: 14px;
    flex-shrink: 0;
  }

  .recent-info {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-width: 0;
  }

  .recent-name {
    font-size: 12px;
    font-weight: 500;
    color: var(--text-primary);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .recent-path {
    font-size: 10px;
    color: var(--text-muted);
    font-family: var(--font-mono);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .recent-time {
    font-size: 10px;
    color: var(--text-muted);
    white-space: nowrap;
    flex-shrink: 0;
  }

  /* Empty state */
  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--spacing-xs);
    padding: var(--spacing-xl) var(--spacing-md);
    color: var(--text-muted);
    font-size: 12px;
  }

  .empty-icon {
    opacity: 0.3;
    margin-bottom: var(--spacing-xs);
  }

  .empty-hint {
    font-size: 11px;
    opacity: 0.6;
  }

  /* Tips */
  .tips-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .tip-item {
    font-size: 11px;
    color: var(--text-secondary);
    padding: var(--spacing-xs) var(--spacing-sm);
    border-left: 2px solid rgba(91, 108, 247, 0.2);
    transition: border-color var(--transition-fast);
  }

  .tip-item:hover {
    border-color: var(--accent-primary);
  }

  /* Shortcuts */
  .shortcuts-list {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs);
  }

  .shortcut-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 3px var(--spacing-sm);
  }

  .shortcut-key {
    font-size: 10px;
    min-width: 80px;
  }

  .shortcut-desc {
    font-size: 11px;
    color: var(--text-secondary);
  }
</style>
