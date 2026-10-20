<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { recentFiles } from '../stores/recentFiles';
  import type { RecentFile } from '../stores/settings';
  import { workspaceStore, PRESET_WORKSPACES } from '../stores/workspace';
  import { webUi } from '../stores/webInfo';
  import RadarSweep from './RadarSweep.svelte';
  import LogoMark from './LogoMark.svelte';

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

  const APP_VERSION = __APP_VERSION__;

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

  /** Short file-type tag shown in the recent list (extension, or RAW for extension-less volumes). */
  function getFileIcon(name: string): string {
    const m = /\.([A-Za-z0-9]{1,4})$/.exec(name);
    return m ? m[1].toUpperCase() : 'RAW';
  }
</script>

<div class="welcome-screen hud-grid">
  <div class="welcome-inner">
    <!-- Hero -->
    <div class="welcome-hero">
      <div class="hero-mark">
        <RadarSweep size={176} period={6} hub={false} />
        <span class="hero-icon"><LogoMark size={92} title="Radarx" /></span>
      </div>
      <h1 class="welcome-logo"><span class="logo-x">X</span>RADAR</h1>
      <p class="welcome-subtitle">Weather radar research workstation{$webUi ? ' in your browser' : ''}</p>
      <div class="welcome-meta">
        <span class="chip">v{APP_VERSION}</span>
        <span class="chip derived">{$webUi ? 'web' : 'desktop'}</span>
        <span class="chip nominal"><span class="led nominal"></span>engine online</span>
      </div>
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
  }

  .welcome-inner {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-5);
    padding: var(--space-6);
    max-width: 800px;
    width: 100%;
    margin: auto;
    animation: hud-rise var(--dur-slow) var(--ease-out);
  }

  /* ---- Hero ---- */
  .welcome-hero {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-2);
    text-align: center;
  }
  .hero-mark { position: relative; width: 176px; height: 176px; }
  .hero-icon { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; }
  .welcome-logo {
    margin-top: var(--space-3);
    font-size: 34px;
    font-weight: 600;
    letter-spacing: 0.34em;
    padding-left: 0.34em; /* optically centre the tracked word */
    color: var(--c-text);
    line-height: 1.1;
  }
  .logo-x { color: var(--c-accent); }
  .welcome-subtitle {
    font-size: var(--fs-xs);
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: var(--ls-wide);
    color: var(--c-text-dim);
  }
  .welcome-meta { display: flex; gap: var(--space-2); margin-top: var(--space-1); }

  /* ---- Quick actions ---- */
  .welcome-actions {
    display: flex;
    gap: var(--space-3);
    width: 100%;
    justify-content: center;
    flex-wrap: wrap;
  }
  .action-card {
    position: relative;
    display: flex;
    align-items: center;
    gap: var(--space-3);
    flex: 1 1 280px;
    max-width: 380px;
    height: auto;
    padding: var(--space-3) var(--space-4);
    text-align: left;
    background: var(--c-panel);
    border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm);
    transition: border-color var(--dur-fast), box-shadow var(--dur-fast), background var(--dur-fast);
  }
  .action-card::before,
  .action-card::after {
    content: '';
    position: absolute;
    width: 9px;
    height: 9px;
    border: 1px solid var(--c-accent);
    pointer-events: none;
  }
  .action-card::before { top: -1px; left: -1px; border-right: 0; border-bottom: 0; }
  .action-card::after { bottom: -1px; right: -1px; border-left: 0; border-top: 0; }
  .action-card:hover {
    background: var(--c-panel);
    border-color: var(--c-accent);
    box-shadow: var(--glow-accent);
  }
  .action-icon {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 40px;
    height: 40px;
    flex-shrink: 0;
    color: var(--c-accent);
    background: rgba(var(--c-accent-rgb), 0.1);
    border: 1px solid rgba(var(--c-accent-rgb), 0.3);
    border-radius: var(--radius-sm);
  }
  .action-text { display: flex; flex-direction: column; gap: 2px; flex: 1; min-width: 0; }
  .action-title { font-size: var(--fs-md); font-weight: 600; color: var(--c-text); letter-spacing: 0.02em; }
  .action-desc { font-size: var(--fs-xs); color: var(--c-muted); font-weight: 400; white-space: normal; }
  .action-card kbd { margin-left: auto; }

  /* ---- Cards ---- */
  .welcome-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--space-3);
    width: 100%;
    align-items: start;
  }
  .welcome-grid.single { grid-template-columns: minmax(0, 480px); justify-content: center; }
  .welcome-right-col { display: flex; flex-direction: column; gap: var(--space-3); }
  .welcome-card {
    background: var(--c-panel);
    border: 1px solid var(--c-border);
    border-radius: var(--radius-sm);
    overflow: hidden;
  }
  .card-header {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-3);
    font-size: var(--fs-xs);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: var(--ls-label);
    color: var(--c-text-dim);
    background: var(--c-panel-head);
    border-bottom: 1px solid var(--c-border);
  }
  .card-header svg { color: var(--c-accent); }
  .card-body { padding: var(--space-2) var(--space-3); }

  /* Recent files */
  .recent-list { display: flex; flex-direction: column; margin: 0 calc(-1 * var(--space-3)); }
  .recent-item {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    width: 100%;
    height: auto;
    padding: 6px var(--space-3);
    text-align: left;
    background: transparent;
    border: none;
    border-left: 2px solid transparent;
    border-radius: 0;
  }
  .recent-item:hover { background: var(--bg-hover); border-left-color: var(--c-accent); }
  .recent-icon {
    flex-shrink: 0;
    min-width: 34px;
    padding: 1px 0;
    text-align: center;
    font-family: var(--font-mono);
    font-size: 9.5px;
    letter-spacing: 0.06em;
    color: var(--c-accent);
    border: 1px solid rgba(var(--c-accent-rgb), 0.3);
    border-radius: var(--radius-xs);
  }
  .recent-info { display: flex; flex-direction: column; min-width: 0; flex: 1; }
  .recent-name { font-family: var(--font-mono); font-size: var(--fs-xs); color: var(--c-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .recent-path { font-size: var(--fs-2xs); color: var(--c-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; direction: rtl; text-align: left; }
  .recent-time { font-family: var(--font-mono); font-size: var(--fs-2xs); color: var(--c-muted); flex-shrink: 0; }

  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-1);
    padding: var(--space-5) 0;
    font-size: var(--fs-sm);
    color: var(--c-text-dim);
  }
  .empty-icon { color: var(--c-muted); margin-bottom: var(--space-1); }
  .empty-hint { font-size: var(--fs-xs); color: var(--c-muted); }

  /* Tips */
  .tips-list { list-style: none; display: flex; flex-direction: column; gap: 2px; }
  .tip-item {
    position: relative;
    padding: 5px 0 5px var(--space-4);
    font-size: var(--fs-xs);
    color: var(--c-text-dim);
  }
  .tip-item::before {
    content: '';
    position: absolute;
    left: 2px;
    top: 50%;
    width: 5px;
    height: 5px;
    margin-top: -2.5px;
    border: 1px solid var(--c-accent);
    transform: rotate(45deg);
  }

  /* Shortcuts */
  .shortcuts-list { display: flex; flex-direction: column; gap: 6px; padding: var(--space-1) 0; }
  .shortcut-row { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); }
  .shortcut-key { white-space: nowrap; }
  .shortcut-desc { font-size: var(--fs-xs); color: var(--c-text-dim); text-align: right; }

  @media (max-width: 900px) {
    .welcome-grid { grid-template-columns: 1fr; }
  }
</style>
