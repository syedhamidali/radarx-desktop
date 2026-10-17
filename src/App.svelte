<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import RadarViewer from './lib/components/RadarViewer.svelte';
  import RadarViewer3D from './lib/components/RadarViewer3D.svelte';
  import DualPolDashboard from './lib/components/DualPolDashboard.svelte';
  import DataInspector from './lib/components/DataInspector.svelte';
  import ProcessingPanel from './lib/components/ProcessingPanel.svelte';
  import ExportPanel from './lib/components/ExportPanel.svelte';
  import QCPanel from './lib/components/QCPanel.svelte';
  import ToastContainer from './lib/components/ToastContainer.svelte';
  import CommandPalette from './lib/components/CommandPalette.svelte';
  import WorkspaceSwitcher from './lib/components/WorkspaceSwitcher.svelte';
  import PanelManager from './lib/components/PanelManager.svelte';
  import WelcomeScreen from './lib/components/WelcomeScreen.svelte';
  import OnboardingTour from './lib/components/OnboardingTour.svelte';
  import ShortcutsSheet from './lib/components/ShortcutsSheet.svelte';
  import AboutDialog from './lib/components/AboutDialog.svelte';
  import NotificationCenter from './lib/components/NotificationCenter.svelte';
  import TimeSeriesChart from './lib/components/TimeSeriesChart.svelte';
  import Timeline from './lib/components/Timeline.svelte';
  import DiffViewer from './lib/components/DiffViewer.svelte';
  import CrossSectionPanel from './lib/components/CrossSectionPanel.svelte';
  import { connectionStatus, radarData, selectedSweep, selectedVariable, processingProgress } from './lib/stores/radarData';
  import { diffMode, toggleDiffMode } from './lib/stores/temporal';
  import { sidecarPort } from './lib/stores/settings';
  import { wsManager } from './lib/utils/websocket';
  import { addToast } from './lib/stores/toasts';
  import { workspaceStore, type WorkspaceConfig } from './lib/stores/workspace';

  // --- Derived from stores (Svelte 5 runes) ---
  const filePath = $derived($radarData.filePath);
  const totalSweeps = $derived($radarData.sweeps.length);
  const currentSweep = $derived($selectedSweep);
  const variableCount = $derived($radarData.variables.length);
  const progress = $derived($processingProgress);
  const statusColor = $derived(
    $connectionStatus === 'connected' ? 'var(--accent-success)'
    : $connectionStatus === 'connecting' ? 'var(--accent-warning)'
    : 'var(--accent-danger)'
  );
  const statusText = $derived(
    $connectionStatus === 'connected' ? 'Connected'
    : $connectionStatus === 'connecting' ? 'Connecting...'
    : 'Disconnected'
  );

  // --- Local reactive state ---
  let leftSidebarOpen = $state(true);
  let rightSidebarOpen = $state(true);
  let viewMode = $state<'2d' | '3d' | 'dualpol'>('2d');
  let commandPaletteVisible = $state(false);
  let shortcutsSheetVisible = $state(false);
  let aboutDialogVisible = $state(false);
  let notificationCenterOpen = $state(false);
  let onboardingVisible = $state(false);
  let isDragOver = $state(false);
  let isPlaying = $state(false);
  let animSpeed = $state(1000);
  let leftSidebarWidth = $state(300);
  let rightSidebarWidth = $state(300);

  // --- Non-reactive locals ---
  let unlistenDragDrop: (() => void) | null = null;
  let destroyed = false;
  let unsubFileOpened: (() => void) | null = null;
  let animTimer: ReturnType<typeof setInterval> | null = null;
  let isResizingLeft = false;
  let isResizingRight = false;
  let resizeStartX = 0;
  let resizeStartWidth = 0;
  const SIDEBAR_MIN = 200;
  const SIDEBAR_MAX = 500;
  const SIDEBAR_DEFAULT = 300;

  // --- Workspace sync ---
  function applyWorkspace(config: WorkspaceConfig) {
    leftSidebarOpen = config.leftSidebarOpen;
    rightSidebarOpen = config.rightSidebarOpen;
    leftSidebarWidth = config.leftSidebarWidth;
    rightSidebarWidth = config.rightSidebarWidth;
    viewMode = config.viewMode;
  }

  function syncWorkspace() {
    workspaceStore.updateCurrentConfig({
      leftSidebarOpen,
      rightSidebarOpen,
      leftSidebarWidth,
      rightSidebarWidth,
      viewMode,
    });
  }

  onMount(async () => {
    // Apply saved workspace on startup
    const wsConfig = workspaceStore.getCurrentConfig();
    applyWorkspace(wsConfig);

    // Stop animation whenever any file (including cloud) finishes opening
    unsubFileOpened = wsManager.onMessage('file_opened', () => stopAnimation());

    window.addEventListener('keydown', handleKeyDown);
    // Check for first-launch onboarding
    if (!localStorage.getItem('xradar-onboarding-done')) {
      onboardingVisible = true;
    }
    try {
      const { getCurrentWindow } = await import('@tauri-apps/api/window');
      const unlisten = await getCurrentWindow().onDragDropEvent((event) => {
        if (destroyed) return;
        const type = event.payload.type;
        if (type === 'over' || type === 'enter') {
          isDragOver = true;
        } else if (type === 'leave') {
          isDragOver = false;
        } else if (type === 'drop') {
          isDragOver = false;
          const paths = (event.payload as any).paths as string[] | undefined;
          if (paths && paths.length > 0) {
            stopAnimation();
            sendOpenFile(paths[0]);
          }
        }
      });
      // The component may have been destroyed while the listener was registering.
      if (destroyed) unlisten();
      else unlistenDragDrop = unlisten;
    } catch (err) {
      console.warn('[App] Tauri drag-drop listener unavailable:', err);
    }

  });

  onDestroy(() => {
    destroyed = true;
    window.removeEventListener('keydown', handleKeyDown);
    // Sidebar drag may still be in progress (listeners are attached lazily).
    window.removeEventListener('mousemove', onResizeMove);
    window.removeEventListener('mouseup', onResizeEnd);
    stopAnimation();
    if (unlistenDragDrop) unlistenDragDrop();
    unsubFileOpened?.();
  });

  /** True when keystrokes are meant for a form control / editable element. */
  function isEditableTarget(t: EventTarget | null): boolean {
    const el = t as HTMLElement | null;
    if (!el || typeof el.tagName !== 'string') return false;
    const tag = el.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
  }

  async function handleKeyDown(e: KeyboardEvent) {
    const ctrl = e.ctrlKey || e.metaKey;
    // Plain (unmodified) shortcuts must not hijack typing or button activation.
    const typing = isEditableTarget(e.target);
    const onButton = (e.target as HTMLElement | null)?.tagName === 'BUTTON';
    if (ctrl && e.key === 'k') {
      e.preventDefault();
      commandPaletteVisible = !commandPaletteVisible;
    } else if (ctrl && e.key === 'o') {
      e.preventDefault();
      openFile();
    } else if (ctrl && e.key === 'b') {
      e.preventDefault();
      leftSidebarOpen = !leftSidebarOpen;
      syncWorkspace();
    } else if (ctrl && e.key === 'e') {
      e.preventDefault();
      rightSidebarOpen = !rightSidebarOpen;
      syncWorkspace();
    } else if (e.key === ' ' && !typing && !onButton && totalSweeps > 0) {
      e.preventDefault();
      toggleAnimation();
    } else if (e.key === '?' && !ctrl && !e.altKey && !typing) {
      e.preventDefault();
      shortcutsSheetVisible = !shortcutsSheetVisible;
    } else if (ctrl && e.shiftKey && e.key.toLowerCase() === 'a') {
      e.preventDefault();
      aboutDialogVisible = !aboutDialogVisible;
    } else if (e.key === 'F12' || (e.altKey && ctrl && e.key.toLowerCase() === 'i')) {
      e.preventDefault();
      try {
        // The JS API has no openDevtools(); Tauri exposes it as a webview plugin
        // command (granted by core:default). Only available in debug builds or
        // when the Rust `devtools` feature is on, so failure is expected in release.
        const { invoke } = await import('@tauri-apps/api/core');
        await invoke('plugin:webview|internal_toggle_devtools');
      } catch {
        /* devtools unavailable in this build */
      }
    }
  }

  async function openFile() {
    try {
      const { open } = await import('@tauri-apps/plugin-dialog');
      const selected = await open({
        multiple: false,
        title: 'Open Radar File',
        filters: [
          {
            name: 'Radar Files',
            extensions: [
              'nc', 'ncf', 'cdf', 'h5', 'hdf5', 'hdf',
              'gz', 'bz2', 'vol', 'azi', 'raw', 'RAW',
              'scn', 'scnx', 'sigmet', 'iris',
              'ar2v', 'nexrad', 'zarr',
              'nc.1', 'nc.2', 'nc.3', 'nc.4', 'nc.5',
              'nc.6', 'nc.7', 'nc.8', 'nc.9',
              'h5.1', 'h5.2', 'h5.3', 'h5.4', 'h5.5',
            ],
          },
          { name: 'All Files', extensions: ['*'] },
        ],
      });
      if (!selected) return;
      const fp = typeof selected === 'string'
        ? selected
        : Array.isArray(selected)
          ? selected[0]
          : typeof selected === 'object' && 'path' in selected
            ? (selected as any).path
            : null;
      if (fp) { stopAnimation(); sendOpenFile(fp); }
    } catch (err) {
      const path = prompt('Enter the path to a radar file:');
      if (path) { stopAnimation(); sendOpenFile(path); }
    }
  }

  function sendOpenFile(path: string) {
    selectedVariable.set(null); // Clear before open so stale variable doesn't trigger a render on the old reader
    radarData.update((d) => ({ ...d, filePath: path }));
    selectedSweep.set(0);
    wsManager.openFile(path);
    addToast('info', `Opening ${path.split('/').pop() ?? path}...`);
  }

  function toggleAnimation() {
    isPlaying ? stopAnimation() : startAnimation();
  }

  function startAnimation() {
    if (totalSweeps === 0) return;
    isPlaying = true;
    animTimer = setInterval(() => {
      selectedSweep.update((s) => (s + 1) % totalSweeps);
    }, animSpeed);
  }

  function stopAnimation() {
    isPlaying = false;
    if (animTimer !== null) { clearInterval(animTimer); animTimer = null; }
  }

  function setSpeed(ms: number) {
    animSpeed = ms;
    if (isPlaying) { stopAnimation(); startAnimation(); }
  }

  function toggleLeftSidebar() { leftSidebarOpen = !leftSidebarOpen; syncWorkspace(); }
  function toggleRightSidebar() { rightSidebarOpen = !rightSidebarOpen; syncWorkspace(); }

  // --- Resizable sidebar handlers ---
  function startResizeLeft(e: MouseEvent) {
    isResizingLeft = true;
    resizeStartX = e.clientX;
    resizeStartWidth = leftSidebarWidth;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    window.addEventListener('mousemove', onResizeMove);
    window.addEventListener('mouseup', onResizeEnd);
  }

  function startResizeRight(e: MouseEvent) {
    isResizingRight = true;
    resizeStartX = e.clientX;
    resizeStartWidth = rightSidebarWidth;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    window.addEventListener('mousemove', onResizeMove);
    window.addEventListener('mouseup', onResizeEnd);
  }

  function onResizeMove(e: MouseEvent) {
    if (isResizingLeft) {
      const delta = e.clientX - resizeStartX;
      leftSidebarWidth = Math.max(SIDEBAR_MIN, Math.min(SIDEBAR_MAX, resizeStartWidth + delta));
    } else if (isResizingRight) {
      const delta = resizeStartX - e.clientX;
      rightSidebarWidth = Math.max(SIDEBAR_MIN, Math.min(SIDEBAR_MAX, resizeStartWidth + delta));
    }
  }

  function onResizeEnd() {
    isResizingLeft = false;
    isResizingRight = false;
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    window.removeEventListener('mousemove', onResizeMove);
    window.removeEventListener('mouseup', onResizeEnd);
    syncWorkspace();
  }

  function resetLeftWidth() { leftSidebarWidth = SIDEBAR_DEFAULT; }
  function resetRightWidth() { rightSidebarWidth = SIDEBAR_DEFAULT; }

  function handleWorkspaceSwitch(e: CustomEvent<{ config: WorkspaceConfig }>) {
    applyWorkspace(e.detail.config);
    addToast('info', `Workspace loaded: ${e.detail.config.name}`);
  }
</script>

<!-- svelte-ignore a11y-no-static-element-interactions -->
<div class="app-layout">
  {#if isDragOver}
    <div class="drop-overlay">
      <div class="drop-overlay-inner">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
          <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
          <line x1="12" y1="11" x2="12" y2="17" />
          <polyline points="9 14 12 17 15 14" />
        </svg>
        <span class="drop-label">Drop radar file to open</span>
      </div>
    </div>
  {/if}

  <header class="toolbar">
    <div class="toolbar-left">
      <div class="app-title">
        <span class="title-x">x</span><span class="title-rest">radar</span>
        <span class="title-desktop">desktop</span>
      </div>
      <WorkspaceSwitcher on:switch={handleWorkspaceSwitch} />
      <button class="cmd-trigger" on:click={() => (commandPaletteVisible = true)} title="Command Palette">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
        </svg>
        <span class="cmd-trigger-label">Search...</span>
        <kbd>{typeof navigator !== 'undefined' && navigator.platform?.includes('Mac') ? '\u2318' : 'Ctrl'}K</kbd>
      </button>
    </div>

    <div class="toolbar-center">
      <button class="toolbar-btn" on:click={openFile} title="Open File (Ctrl+O)">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
        </svg>
        Open
      </button>

      <div class="toolbar-separator"></div>

      <div class="anim-controls" class:disabled={totalSweeps === 0}>
        <button class="toolbar-btn icon-btn" class:active={isPlaying} on:click={toggleAnimation} disabled={totalSweeps === 0} title={isPlaying ? 'Pause (Space)' : 'Play (Space)'}>
          {#if isPlaying}
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>
          {:else}
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><polygon points="5,3 19,12 5,21"/></svg>
          {/if}
        </button>
        <span class="sweep-counter">{#if totalSweeps > 0}{currentSweep + 1}/{totalSweeps}{:else}---{/if}</span>
        <div class="speed-group" title="Animation speed">
          <button class="speed-btn" class:active={animSpeed === 2000} on:click={() => setSpeed(2000)} disabled={totalSweeps === 0}>0.5x</button>
          <button class="speed-btn" class:active={animSpeed === 1000} on:click={() => setSpeed(1000)} disabled={totalSweeps === 0}>1x</button>
          <button class="speed-btn" class:active={animSpeed === 500} on:click={() => setSpeed(500)} disabled={totalSweeps === 0}>2x</button>
        </div>
      </div>

      <div class="toolbar-separator"></div>

      <div class="view-toggle">
        <button class="view-btn" class:active={viewMode === '2d'} on:click={() => { viewMode = '2d'; syncWorkspace(); }} title="2D PPI View">2D</button>
        <button class="view-btn" class:active={viewMode === '3d'} on:click={() => { viewMode = '3d'; syncWorkspace(); }} title="3D Volume View">3D</button>
        <button class="view-btn" class:active={viewMode === 'dualpol'} on:click={() => { viewMode = 'dualpol'; syncWorkspace(); }} title="Dual-Pol Analysis Dashboard">DualPol</button>
      </div>

      <button class="toolbar-btn icon-btn" class:active={$diffMode === 'diff'} on:click={toggleDiffMode} title="Toggle Diff View">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="9" cy="12" r="6" /><circle cx="15" cy="12" r="6" />
        </svg>
        Diff
      </button>

      <div class="toolbar-separator"></div>

      <button class="toolbar-btn icon-btn" class:active={leftSidebarOpen} on:click={toggleLeftSidebar} title="Toggle Data Inspector (Ctrl+B)">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="3" x2="9" y2="21"/></svg>
      </button>
      <button class="toolbar-btn icon-btn" class:active={rightSidebarOpen} on:click={toggleRightSidebar} title="Toggle Tools Panel (Ctrl+E)">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="15" y1="3" x2="15" y2="21"/></svg>
      </button>
    </div>

    <div class="toolbar-right">
      <NotificationCenter bind:open={notificationCenterOpen} />
      <div class="toolbar-separator"></div>
      <button class="toolbar-btn icon-btn" on:click={() => (shortcutsSheetVisible = true)} title="Keyboard Shortcuts (?)">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h0m4 0h0m4 0h0m4 0h0m-10 4h4"/>
        </svg>
      </button>
      <button class="toolbar-btn icon-btn" on:click={() => (aboutDialogVisible = true)} title="About">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>
        </svg>
      </button>
      <div class="toolbar-separator"></div>
      <div class="connection-indicator" title="{statusText} to Python sidecar">
        <span class="status-dot" style="background: {statusColor}"></span>
        <span class="status-text">{statusText}</span>
      </div>
    </div>
  </header>

  <main class="main-content">
    <aside
      class="sidebar sidebar-left"
      class:collapsed={!leftSidebarOpen}
      style="width: {leftSidebarOpen ? leftSidebarWidth + 'px' : '0'}"
    >
      <DataInspector />
    </aside>
    {#if leftSidebarOpen}
      <!-- svelte-ignore a11y-no-static-element-interactions -->
      <div
        class="resize-handle resize-handle-left"
        on:mousedown={startResizeLeft}
        on:dblclick={resetLeftWidth}
        title="Drag to resize, double-click to reset"
      ></div>
    {/if}
    <div class="viewer-and-timeline">
      <section class="viewer-area">
        {#if filePath}
          {#if $diffMode === 'diff'}
            <DiffViewer />
          {:else if viewMode === '2d'}
            <RadarViewer />
          {:else if viewMode === '3d'}
            <RadarViewer3D />
          {:else}
            <DualPolDashboard />
          {/if}
        {:else}
          <WelcomeScreen
            on:open-file={openFile}
            on:open-recent={(e) => { stopAnimation(); sendOpenFile(e.detail.path); }}
            on:load-workspace={(e) => {
              const config = workspaceStore.loadWorkspace(e.detail.name);
              if (config) applyWorkspace(config);
            }}
          />
        {/if}
      </section>
      {#if filePath}
        <Timeline />
      {/if}
    </div>
    {#if rightSidebarOpen}
      <!-- svelte-ignore a11y-no-static-element-interactions -->
      <div
        class="resize-handle resize-handle-right"
        on:mousedown={startResizeRight}
        on:dblclick={resetRightWidth}
        title="Drag to resize, double-click to reset"
      ></div>
    {/if}
    <aside
      class="sidebar sidebar-right"
      class:collapsed={!rightSidebarOpen}
      style="width: {rightSidebarOpen ? rightSidebarWidth + 'px' : '0'}"
    >
      <PanelManager />
    </aside>
  </main>

  <footer class="statusbar">
    {#if progress && progress.percent > 0 && progress.percent < 100}
      <div class="statusbar-progress"><div class="statusbar-progress-fill" style="width: {progress.percent}%"></div></div>
    {/if}
    <div class="statusbar-left">
      <span class="status-dot-sm" class:pulse={$connectionStatus === 'connected'} style="background: {statusColor}" title="{statusText}"></span>
      <span>{statusText}</span>
      {#if progress && progress.percent > 0 && progress.percent < 100}
        <span class="statusbar-divider">|</span>
        <span class="progress-text">{progress.message} ({Math.round(progress.percent)}%)</span>
      {/if}
    </div>
    <div class="statusbar-center">
      {#if filePath}
        <span class="file-path mono" title={filePath}>{filePath.split('/').pop() ?? filePath}</span>
        {#if variableCount > 0}<span class="statusbar-divider">|</span><span class="text-muted">{variableCount} variables</span>{/if}
      {:else}
        <span class="text-muted">No file loaded -- drag and drop or Ctrl+O</span>
      {/if}
    </div>
    <div class="statusbar-right">
      <span class="text-muted">{typeof navigator !== 'undefined' && navigator.platform?.includes('Mac') ? '\u2318' : 'Ctrl'}+K commands</span>
      <span class="statusbar-divider">|</span>
      <span class="text-muted">? shortcuts</span>
    </div>
  </footer>

  <ToastContainer />
  <CommandPalette bind:visible={commandPaletteVisible} on:close={() => (commandPaletteVisible = false)} on:action={(e) => {
    const { type, value } = e.detail;
    if (type === 'open-file') openFile();
    else if (type === 'toggle-left-sidebar') leftSidebarOpen = !leftSidebarOpen;
    else if (type === 'toggle-right-sidebar') rightSidebarOpen = !rightSidebarOpen;
    else if (type === 'toggle-animation') toggleAnimation();
    else if (type === 'set-view') { viewMode = value; syncWorkspace(); }
  }} />
  <OnboardingTour bind:visible={onboardingVisible} />
  <ShortcutsSheet bind:visible={shortcutsSheetVisible} />
  <AboutDialog bind:visible={aboutDialogVisible} />
</div>

<style>
  .app-layout {
    display: flex; flex-direction: column; height: 100vh; width: 100vw;
    overflow: hidden; background: var(--bg-primary); position: relative;
  }

  .drop-overlay {
    position: fixed; inset: 0; z-index: 1000;
    background: rgba(0, 0, 0, 0.65); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    display: flex; align-items: center; justify-content: center; pointer-events: none;
    animation: fade-in 150ms ease-out;
  }
  .drop-overlay-inner {
    display: flex; flex-direction: column; align-items: center; gap: 16px;
    padding: 48px 64px; border: 2px dashed var(--accent-primary); border-radius: 20px;
    color: var(--accent-primary); background: rgba(91, 108, 247, 0.06);
    box-shadow: 0 0 80px rgba(91, 108, 247, 0.1);
  }
  .drop-label { font-size: 18px; font-weight: 600; color: var(--accent-primary); }

  .toolbar {
    display: flex; align-items: center; justify-content: space-between;
    height: var(--toolbar-height); padding: 0 var(--spacing-lg);
    background: var(--glass-bg); backdrop-filter: blur(var(--glass-blur)); -webkit-backdrop-filter: blur(var(--glass-blur));
    border-bottom: 1px solid var(--glass-border); flex-shrink: 0;
    -webkit-app-region: drag; position: relative; z-index: 10;
  }
  .toolbar-left, .toolbar-center, .toolbar-right {
    display: flex; align-items: center; gap: var(--spacing-sm); -webkit-app-region: no-drag;
  }
  .toolbar-left { min-width: 260px; gap: var(--spacing-md); }
  .toolbar-right { min-width: 160px; justify-content: flex-end; }

  .app-title { font-size: 15px; font-weight: 600; letter-spacing: -0.02em; user-select: none; }
  .title-x { color: var(--accent-primary); font-weight: 700; text-shadow: 0 0 12px rgba(91, 108, 247, 0.4); }
  .title-rest { color: var(--text-primary); }
  .title-desktop { color: var(--text-muted); font-weight: 400; margin-left: 4px; font-size: 13px; }

  .cmd-trigger {
    display: flex; align-items: center; gap: 6px; padding: 4px 10px; height: 28px;
    font-size: 12px; color: var(--text-muted); background: rgba(8, 10, 20, 0.5);
    border: 1px solid var(--glass-border); border-radius: var(--radius-md);
    cursor: pointer; transition: all var(--transition-fast); min-width: 140px;
  }
  .cmd-trigger:hover { border-color: var(--border-light); color: var(--text-secondary); background: rgba(91, 108, 247, 0.06); }
  .cmd-trigger-label { flex: 1; text-align: left; opacity: 0.5; font-size: 11px; }
  .cmd-trigger :global(kbd) { font-size: 9px; padding: 1px 4px; opacity: 0.6; }

  .toolbar-btn { display: inline-flex; align-items: center; gap: 6px; padding: var(--spacing-xs) var(--spacing-md); font-size: 12px; }
  .toolbar-btn.icon-btn { padding: var(--spacing-xs) var(--spacing-sm); }
  .toolbar-btn.active { background: var(--bg-active); border-color: rgba(91, 108, 247, 0.2); color: var(--accent-hover); box-shadow: 0 0 8px rgba(91, 108, 247, 0.08); }
  .toolbar-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .toolbar-separator { width: 1px; height: 18px; background: var(--glass-border); margin: 0 var(--spacing-xs); }

  .anim-controls { display: flex; align-items: center; gap: var(--spacing-xs); }
  .anim-controls.disabled { opacity: 0.35; }
  .sweep-counter { font-size: 11px; font-variant-numeric: tabular-nums; color: var(--text-secondary); min-width: 36px; text-align: center; user-select: none; }
  .speed-group { display: flex; align-items: center; border: 1px solid var(--glass-border); border-radius: var(--radius-sm); overflow: hidden; }
  .speed-btn {
    background: transparent; border: none; border-right: 1px solid var(--glass-border);
    color: var(--text-secondary); font-size: 10px; padding: 2px 7px; cursor: pointer;
    transition: all var(--transition-fast); white-space: nowrap; border-radius: 0;
    backdrop-filter: none; -webkit-backdrop-filter: none; height: auto;
  }
  .speed-btn:last-child { border-right: none; }
  .speed-btn:hover:not(:disabled) { background: var(--bg-hover); color: var(--text-primary); }
  .speed-btn.active { background: var(--bg-active); color: var(--accent-hover); }
  .speed-btn:disabled { cursor: not-allowed; }

  .view-toggle { display: flex; border: 1px solid var(--glass-border); border-radius: var(--radius-sm); overflow: hidden; }
  .view-btn {
    background: transparent; border: none; border-right: 1px solid var(--glass-border);
    color: var(--text-secondary); font-size: 11px; font-weight: 600; padding: 3px 10px;
    cursor: pointer; transition: all var(--transition-fast); border-radius: 0;
    backdrop-filter: none; -webkit-backdrop-filter: none; height: auto;
  }
  .view-btn:last-child { border-right: none; }
  .view-btn:hover { background: var(--bg-hover); color: var(--text-primary); }
  .view-btn.active { background: var(--accent-primary); color: #fff; box-shadow: 0 0 12px rgba(91, 108, 247, 0.25); }

  .connection-indicator { display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--text-secondary); }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; transition: background var(--transition-normal); animation: pulse-glow 2.5s ease-in-out infinite; }
  .status-text { white-space: nowrap; }

  .main-content { display: flex; flex: 1; overflow: hidden; }
  .sidebar {
    flex-shrink: 0; overflow: hidden;
    background: var(--glass-bg); backdrop-filter: blur(var(--glass-blur)); -webkit-backdrop-filter: blur(var(--glass-blur));
    transition: width 300ms cubic-bezier(0.4, 0, 0.2, 1), opacity 250ms ease;
  }
  .sidebar.collapsed { width: 0 !important; opacity: 0; pointer-events: none; }
  .sidebar-left { border-right: 1px solid var(--glass-border); }
  .sidebar-right { border-left: 1px solid var(--glass-border); }
  .viewer-and-timeline { display: flex; flex-direction: column; flex: 1; overflow: hidden; min-width: 0; }
  .viewer-area { flex: 1; overflow: hidden; min-width: 0; }
  .right-panel-stack { display: flex; flex-direction: column; height: 100%; overflow-y: auto; }

  .statusbar {
    display: flex; align-items: center; justify-content: space-between;
    height: var(--statusbar-height); padding: 0 var(--spacing-lg);
    background: var(--glass-bg-heavy); backdrop-filter: blur(var(--glass-blur)); -webkit-backdrop-filter: blur(var(--glass-blur));
    border-top: 1px solid var(--glass-border); font-size: 11px; color: var(--text-muted);
    flex-shrink: 0; gap: var(--spacing-lg); position: relative; overflow: hidden;
  }
  .statusbar-progress { position: absolute; top: 0; left: 0; right: 0; height: 2px; background: rgba(91, 108, 247, 0.06); z-index: 1; }
  .statusbar-progress-fill { height: 100%; background: linear-gradient(90deg, var(--accent-primary), var(--accent-hover)); transition: width var(--transition-normal); box-shadow: 0 0 8px var(--accent-primary), 0 0 20px rgba(91, 108, 247, 0.2); }
  .progress-text { color: var(--accent-hover); font-size: 10px; animation: subtle-breathe 2s ease-in-out infinite; }
  .statusbar-left, .statusbar-center, .statusbar-right { display: flex; align-items: center; gap: 6px; }
  .statusbar-left { min-width: 120px; }
  .statusbar-center { flex: 1; justify-content: center; }
  .statusbar-right { min-width: 100px; justify-content: flex-end; }
  .statusbar-divider { color: var(--glass-border); margin: 0 4px; }
  .status-dot-sm { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; transition: background var(--transition-normal); }
  .status-dot-sm.pulse { animation: pulse-glow 2.5s ease-in-out infinite; }
  .file-path { font-size: 11px; color: var(--text-secondary); max-width: 400px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  /* Resize handles */
  .resize-handle {
    width: 4px; cursor: col-resize; flex-shrink: 0; position: relative; z-index: 5;
    background: transparent; transition: background var(--transition-fast);
  }
  .resize-handle:hover, .resize-handle:active {
    background: var(--accent-primary);
    box-shadow: 0 0 8px rgba(91, 108, 247, 0.3);
  }
  .resize-handle::after {
    content: ''; position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
    width: 2px; height: 24px; border-radius: 1px; background: var(--text-muted); opacity: 0;
    transition: opacity var(--transition-fast);
  }
  .resize-handle:hover::after { opacity: 0.5; }
</style>
