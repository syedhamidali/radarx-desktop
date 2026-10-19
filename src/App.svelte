<script lang="ts">
  import { onMount, onDestroy, untrack, tick } from 'svelte';
  import RadarViewer from './lib/components/RadarViewer.svelte';
  import Viewer3D from './lib/components/Viewer3D.svelte';
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
  import SettingsDialog from './lib/components/SettingsDialog.svelte';
  import FileTabs from './lib/components/FileTabs.svelte';
  import NotificationCenter from './lib/components/NotificationCenter.svelte';
  import OperationsTray from './lib/components/OperationsTray.svelte';
  import TimeSeriesChart from './lib/components/TimeSeriesChart.svelte';
  import Timeline from './lib/components/Timeline.svelte';
  import DiffViewer from './lib/components/DiffViewer.svelte';
  import CrossSectionPanel from './lib/components/CrossSectionPanel.svelte';
  import { connectionStatus, radarData, selectedSweep, selectedVariable, processingProgress } from './lib/stores/radarData';
  import { diffMode, toggleDiffMode } from './lib/stores/temporal';
  import { get } from 'svelte/store';
  import { sidecarPort, settings } from './lib/stores/settings';
  import { recentFiles } from './lib/stores/recentFiles';
  import { openFiles, setActiveFile } from './lib/stores/fileManager';
  import { matchesCombo, SHORTCUT_REGISTRY } from './lib/utils/shortcuts';
  import { wsManager } from './lib/utils/websocket';
  import { beginOp, cancelOp, dismissOp, type OpHandle } from './lib/utils/requestScheduler';
  import { startUiHealth, stopUiHealth } from './lib/utils/uiHealth';
  import { addToast } from './lib/stores/toasts';
  import { workspaceStore, type WorkspaceConfig } from './lib/stores/workspace';
  import { isWebMode } from './lib/utils/env';
  import { webUi, engineState, engineStatusMessage } from './lib/stores/webInfo';
  import { uploadFiles } from './lib/utils/upload';

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
    : $engineState.phase === 'waking' ? 'Waking engine...'
    : $connectionStatus === 'connecting' ? 'Connecting...'
    : 'Disconnected'
  );
  // Web mode only: why the remote engine is not reachable right now (null = nothing to say).
  const engineMsg = $derived(
    isWebMode() && $connectionStatus !== 'connected' ? engineStatusMessage($engineState) : null,
  );
  let demoBannerDismissed = $state(false);
  let fileInput = $state<HTMLInputElement | null>(null);

  // --- Local reactive state ---
  let leftSidebarOpen = $state(true);
  let rightSidebarOpen = $state(true);
  let viewMode = $state<'2d' | '3d' | 'dualpol'>('2d');
  let commandPaletteVisible = $state(false);
  let shortcutsSheetVisible = $state(false);
  let aboutDialogVisible = $state(false);
  let notificationCenterOpen = $state(false);
  let onboardingVisible = $state(false);
  let settingsOpen = $state(false);
  /** Any modal layer open: global plain-key shortcuts must not act behind it. */
  const modalOpen = $derived(
    commandPaletteVisible || shortcutsSheetVisible || aboutDialogVisible || settingsOpen || onboardingVisible,
  );
  let isDragOver = $state(false);
  let isPlaying = $state(false);
  let animSpeed = $state(1000);
  let leftSidebarWidth = $state(300);
  let rightSidebarWidth = $state(300);

  // --- Non-reactive locals ---
  let unlistenDragDrop: (() => void) | null = null;
  let unlistenSidecarError: (() => void) | null = null;
  let unlistenOpenFiles: (() => void) | null = null;
  let lastSidecarError = '';
  let destroyed = false;
  let unsubFileOpened: (() => void) | null = null;
  let unsubOpenError: (() => void) | null = null;
  let unsubConn: (() => void) | null = null;
  let openOp: OpHandle | null = null;
  /** What to put back if the pending open fails or is cancelled. */
  let openRestore: { variable: string | null; sweep: number; filePath: string | null } | null = null;
  /** Local path of the pending open; recorded in Recent files once it succeeds. */
  let pendingRecent: string | null = null;
  /** Further files of a multi-file open, opened one after the other. */
  let openQueue: string[] = [];
  let autoLoadTried = false;
  let progressOp: OpHandle | null = null;
  const OPEN_TIMEOUT_MS = 90_000;
  const PROGRESS_SLOW_MS = 30_000;
  const PROGRESS_STALE_MS = 60_000;
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

  /** Panels (e.g. a module's "View in 3D") ask for a view through a window event. */
  function handleSetViewMode(e: Event) {
    const mode = (e as CustomEvent<{ mode?: string }>).detail?.mode;
    if (mode === '2d' || mode === '3d' || mode === 'dualpol') {
      viewMode = mode;
      syncWorkspace();
    }
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
    unsubFileOpened = wsManager.onMessage('file_opened', () => {
      stopAnimation();
      finishOpenOp();
    });
    // A rejected open (or a dropped connection) must not leave "Opening..." hanging.
    unsubOpenError = wsManager.onMessage('error', (m) => {
      const text = String(m?.message ?? 'Open failed');
      if (isOpenFileError(text)) finishOpenOp(text);
    });
    unsubConn = connectionStatus.subscribe((st) => {
      if (st !== 'connected') finishOpenOp('Connection to the sidecar was lost');
      else if (!autoLoadTried) {
        autoLoadTried = true;
        autoLoadLast();
      }
    });
    startUiHealth();

    window.addEventListener('keydown', handleKeyDown);
    window.addEventListener('xradar:set-view-mode', handleSetViewMode);
    // Check for first-launch onboarding
    if (!localStorage.getItem('xradar-onboarding-done')) {
      onboardingVisible = true;
    }
    // The shell could not start (or restart) the Python engine: say why instead of a silent
    // "Disconnected". Listen first, then ask, so an error raised in between is not missed.
    if ((window as any).__TAURI_INTERNALS__) {
      try {
        const { listen } = await import('@tauri-apps/api/event');
        const { invoke } = await import('@tauri-apps/api/core');
        const unlistenErr = await listen<string>('sidecar-error', (e) => reportSidecarError(String(e.payload)));
        if (destroyed) unlistenErr();
        else unlistenSidecarError = unlistenErr;
        const pending = await invoke<string | null>('get_sidecar_error');
        if (pending) reportSidecarError(pending);
      } catch (err) {
        console.warn('[App] sidecar error channel unavailable:', err);
      }
      // Files the OS asked us to open ("Open with", double click): the ones that arrived while the
      // window was loading, then any later ones as events.
      try {
        const { listen } = await import('@tauri-apps/api/event');
        const { invoke } = await import('@tauri-apps/api/core');
        const unlistenOpen = await listen<string[]>('open-files', (e) => openWhenConnected(e.payload));
        if (destroyed) unlistenOpen();
        else unlistenOpenFiles = unlistenOpen;
        const queued = await invoke<string[]>('take_pending_open_files');
        if (queued.length > 0) openWhenConnected(queued);
      } catch (err) {
        console.warn('[App] open-with channel unavailable:', err);
      }
    }
    if (isWebMode()) {
      window.addEventListener('dragenter', onDomDragEnter);
      window.addEventListener('dragover', onDomDragOver);
      window.addEventListener('dragleave', onDomDragLeave);
      window.addEventListener('drop', onDomDrop);
    }
    if (!isWebMode()) try {
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
          if (paths && paths.length > 0) openPaths(paths);
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
    window.removeEventListener('xradar:set-view-mode', handleSetViewMode);
    window.removeEventListener('dragenter', onDomDragEnter);
    window.removeEventListener('dragover', onDomDragOver);
    window.removeEventListener('dragleave', onDomDragLeave);
    window.removeEventListener('drop', onDomDrop);
    // Sidebar drag may still be in progress (listeners are attached lazily).
    window.removeEventListener('mousemove', onResizeMove);
    window.removeEventListener('mouseup', onResizeEnd);
    stopAnimation();
    if (unlistenDragDrop) unlistenDragDrop();
    unlistenSidecarError?.();
    unlistenOpenFiles?.();
    unsubFileOpened?.();
    unsubOpenError?.();
    unsubConn?.();
    stopUiHealth();
  });

  function reportSidecarError(message: string) {
    if (message === lastSidecarError) return;
    lastSidecarError = message;
    addToast('error', `Analysis engine problem: ${message}`, 0);
  }

  /** True when keystrokes are meant for a form control / editable element. */
  function isEditableTarget(t: EventTarget | null): boolean {
    const el = t as HTMLElement | null;
    if (!el || typeof el.tagName !== 'string') return false;
    const tag = el.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
  }

  /** Which registered shortcut (honouring user rebinding) matches this key event, if any. */
  function shortcutFor(e: KeyboardEvent): string | null {
    const bindings = get(settings).shortcuts;
    // "mod" is Cmd on macOS; Ctrl is accepted there too so the documented Ctrl+K etc. keep working.
    const mod = e.ctrlKey || e.metaKey;
    const ev = { key: e.key, ctrlKey: mod, metaKey: mod, shiftKey: e.shiftKey, altKey: e.altKey } as KeyboardEvent;
    for (const def of SHORTCUT_REGISTRY) {
      if (matchesCombo(ev, bindings[def.id]?.key ?? def.defaultKey)) return def.id;
    }
    return null;
  }

  function stepSweep(delta: number | 'first' | 'last') {
    const n = totalSweeps;
    if (n === 0) return;
    selectedSweep.update((s) =>
      delta === 'first' ? 0 : delta === 'last' ? n - 1 : Math.min(n - 1, Math.max(0, s + delta)),
    );
  }

  /** Expand the right sidebar and ask PanelManager to open (and scroll to) one panel. */
  async function showPanel(id: string) {
    if (!rightSidebarOpen) { rightSidebarOpen = true; syncWorkspace(); }
    await tick();
    window.dispatchEvent(new CustomEvent('xradar:show-panel', { detail: { id } }));
  }

  async function handleKeyDown(e: KeyboardEvent) {
    // The settings dialog records new bindings itself; nothing may fire behind it.
    if (settingsOpen) return;
    const ctrl = e.ctrlKey || e.metaKey;
    // Plain (unmodified) shortcuts must not hijack typing or button activation.
    const typing = isEditableTarget(e.target);
    const onButton = (e.target as HTMLElement | null)?.tagName === 'BUTTON';

    const id = shortcutFor(e);
    if (id) {
      // A rebinding without a modifier behaves like Space: ignored while typing / in a dialog.
      if (!ctrl && (typing || (modalOpen && id !== 'command-palette'))) return;
      // Space on a focused button / tab activates that control instead.
      if (id === 'play-pause' && (onButton || totalSweeps === 0 || (e.target as HTMLElement | null)?.closest?.('[role="tab"]'))) return;
      e.preventDefault();
      switch (id) {
        case 'command-palette': commandPaletteVisible = !commandPaletteVisible; break;
        case 'open-file': openFile(); break;
        case 'toggle-left-sidebar': toggleLeftSidebar(); break;
        case 'toggle-right-sidebar': toggleRightSidebar(); break;
        case 'play-pause': toggleAnimation(); break;
        case 'settings': settingsOpen = !settingsOpen; break;
      }
      return;
    }

    if (e.key === '?' && !ctrl && !e.altKey && !typing && (!modalOpen || shortcutsSheetVisible)) {
      e.preventDefault();
      shortcutsSheetVisible = !shortcutsSheetVisible;
    } else if (ctrl && e.shiftKey && e.key.toLowerCase() === 'a') {
      e.preventDefault();
      aboutDialogVisible = !aboutDialogVisible;
    } else if (ctrl && e.shiftKey && e.key.toLowerCase() === 'e') {
      e.preventDefault();
      showPanel('export');
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
    } else if (!ctrl && !e.altKey && !e.shiftKey && !typing && !modalOpen) {
      // Plain navigation keys (advertised in the shortcuts sheet).
      if (e.key === '2' || e.key === '3') {
        e.preventDefault();
        viewMode = e.key === '2' ? '2d' : '3d';
        syncWorkspace();
      } else if (viewMode === '2d' && !onButton && !(e.target as HTMLElement | null)?.closest?.('.timeline, [role="tab"]')) {
        if (e.key === 'ArrowRight') { e.preventDefault(); stepSweep(1); }
        else if (e.key === 'ArrowLeft') { e.preventDefault(); stepSweep(-1); }
        else if (e.key === 'Home') { e.preventDefault(); stepSweep('first'); }
        else if (e.key === 'End') { e.preventDefault(); stepSweep('last'); }
      }
    }
  }

  async function openFile() {
    if (isWebMode() && get(webUi)) {
      // Browser: pick files with a normal <input type=file>; they are uploaded to the engine.
      fileInput?.click();
      return;
    }
    try {
      const { open } = await import('@tauri-apps/plugin-dialog');
      const dir = get(settings).data.defaultDirectory.trim();
      const selected = await open({
        multiple: true,
        title: 'Open Radar File',
        ...(dir ? { defaultPath: dir } : {}),
        // No file-type filter on purpose: NEXRAD Level II files usually have no extension
        // (KTLX20130520_201643_V06), and macOS greys out everything that is not on the allowed list
        // — an "All Files" entry does not lift that. The reader detects the format from the content.
      });
      if (!selected) return;
      const picked: unknown[] = Array.isArray(selected) ? selected : [selected];
      const paths = picked
        .map((p) => (typeof p === 'string' ? p : p && typeof p === 'object' && 'path' in p ? (p as any).path : null))
        .filter((p): p is string => typeof p === 'string' && p.length > 0);
      openPaths(paths);
    } catch (err) {
      const path = prompt('Enter the path to a radar file:');
      if (path) openPaths([path.trim()]);
    }
  }

  /** Web mode: files picked with the file input are uploaded, then opened by their server path. */
  function handleFilesChosen(e: Event) {
    const input = e.currentTarget as HTMLInputElement;
    const files = input.files ? Array.from(input.files) : [];
    input.value = ''; // picking the same file again must fire `change` again
    uploadWebFiles(files);
  }

  function uploadWebFiles(files: File[]) {
    if (files.length === 0) return;
    stopAnimation();
    uploadFiles(files, { onUploaded: (path) => enqueueOpen(path) });
  }

  /** A server-side path became available (upload finished): open it after any open in flight. */
  function enqueueOpen(path: string) {
    if (destroyed) return;
    if (openOp) { openQueue.push(path); return; }
    if (!sendOpenFile(path)) nextQueued();
  }

  // Browser drag & drop (the Tauri window has its own native drop events, below).
  let dragDepth = 0;
  function hasFiles(e: DragEvent): boolean {
    return !!e.dataTransfer && Array.from(e.dataTransfer.types ?? []).includes('Files');
  }
  function onDomDragEnter(e: DragEvent) {
    if (!get(webUi) || !hasFiles(e)) return;
    e.preventDefault();
    dragDepth++;
    isDragOver = true;
  }
  function onDomDragOver(e: DragEvent) {
    if (!get(webUi) || !hasFiles(e)) return;
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
  }
  function onDomDragLeave(e: DragEvent) {
    if (!get(webUi) || !hasFiles(e)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) isDragOver = false;
  }
  function onDomDrop(e: DragEvent) {
    if (!get(webUi) || !hasFiles(e)) return;
    e.preventDefault();
    dragDepth = 0;
    isDragOver = false;
    uploadWebFiles(Array.from(e.dataTransfer?.files ?? []));
  }

  /** Welcome screen: open the Cloud Data panel and fetch the latest scan of its station. */
  async function openLatestFromCloud() {
    await showPanel('cloud');
    window.dispatchEvent(new CustomEvent('xradar:cloud-open-latest'));
  }

  /** Open files handed over by the OS, waiting (up to a minute) for the engine to be connected. */
  function openWhenConnected(paths: string[]) {
    const list = paths.filter(Boolean);
    if (list.length === 0) return;
    if (get(connectionStatus) === 'connected') {
      openPaths(list);
      return;
    }
    let done = false;
    let unsub: () => void = () => {};
    const finish = (open: boolean) => {
      if (done) return;
      done = true;
      unsub();
      clearTimeout(timer);
      if (open) openPaths(list);
    };
    const timer = setTimeout(() => finish(false), 60_000);
    unsub = connectionStatus.subscribe((s) => {
      if (s === 'connected') queueMicrotask(() => finish(true));
    });
  }

  /** Open one or more files; the first immediately, the rest one after the other. */
  function openPaths(paths: string[]) {
    const list = paths.filter(Boolean);
    if (list.length === 0) return;
    stopAnimation();
    openQueue = list.slice(1);
    if (!sendOpenFile(list[0])) nextQueued();
  }

  /** Start the next file of a multi-file open (skipping ones that are already open). */
  function nextQueued() {
    while (openQueue.length > 0) {
      if (sendOpenFile(openQueue.shift()!)) return;
    }
  }

  /** Put back the view that was on screen before a failed / cancelled open started. */
  function restoreAfterOpen() {
    const r = openRestore;
    openRestore = null;
    pendingRecent = null;
    if (!r) return;
    radarData.update((d) => ({ ...d, filePath: r.filePath }));
    selectedSweep.set(r.sweep);
    selectedVariable.set(r.variable);
  }

  /**
   * Server errors are plain text naming the failed handler ("get_sweep_data failed: ...",
   * "I/O error in render: ..."). An error from another handler (a sweep, a module) must not be
   * taken for a failed file open.
   */
  function isOpenFileError(msg: string): boolean {
    const m = /^([a-z_0-9]+)(?: failed)?:/.exec(msg) ?? /\b(?:in|for) ([a-z_0-9]+)\b/.exec(msg);
    return !m || m[1] === 'open_file';
  }

  /** End the operations-tray entry of the local file open (success, or failure with a reason). */
  function finishOpenOp(error?: string) {
    const op = openOp;
    if (!op) return;
    openOp = null;
    if (error) {
      const failedPath = pendingRecent;
      restoreAfterOpen();
      // A recent file that no longer exists should not stay in the list.
      if (failedPath && /not found|no such file|does not exist|doesn't exist/i.test(error)) {
        recentFiles.remove(failedPath);
      }
      openQueue = [];
      op.fail(error);
    } else {
      if (pendingRecent) recentFiles.add(pendingRecent);
      pendingRecent = null;
      openRestore = null;
      op.done();
      nextQueued();
    }
  }

  function beginOpenOp(path: string) {
    if (openOp) { const prev = openOp; openOp = null; cancelOp(prev.id); }
    const name = path.split('/').pop() ?? path;
    const op: OpHandle = beginOp({
      kind: 'open',
      label: `Opening ${name}`,
      slowMs: 8_000,
      timeoutMs: OPEN_TIMEOUT_MS,
      // The server cannot abort an open; stop waiting (a late file_opened still opens the file).
      onCancel: () => {
        if (openOp === op) { openOp = null; restoreAfterOpen(); openQueue = []; }
      },
      // Timed out: show the previous view again, but keep `openOp` so a late file_opened resolves it.
      onTimeout: () => { if (openOp === op) restoreAfterOpen(); },
      onRetry: () => { stopAnimation(); sendOpenFile(path); },
    });
    openOp = op;
  }

  /** Track global progress messages as an operation so a stalled "xx%" can never hang forever. */
  function trackProgress(p: { percent: number; message: string } | null) {
    if (!p || p.percent >= 100) {
      if (progressOp) { const op = progressOp; progressOp = null; op.done(); }
      return;
    }
    const text = `${p.message || 'Processing'} (${Math.round(p.percent)}%)`;
    if (progressOp) {
      progressOp.start(); // a fresh message restarts the slow / stale clocks
      progressOp.update(text);
      return;
    }
    let userCancelled = false;
    const op: OpHandle = beginOp({
      kind: 'progress',
      label: p.message || 'Processing',
      slowMs: PROGRESS_SLOW_MS,
      timeoutMs: PROGRESS_STALE_MS,
      onCancel: () => {
        userCancelled = true;
        if (progressOp === op) progressOp = null;
        processingProgress.set(null);
      },
    });
    op.signal.addEventListener('abort', () => {
      // cancelOp aborts before onCancel runs: decide after the current call stack settles.
      queueMicrotask(() => {
        if (userCancelled || progressOp !== op) return;
        progressOp = null;
        processingProgress.set(null);
        dismissOp(op.id);
        addToast('warning', 'Progress message timed out');
      });
    });
    progressOp = op;
    op.update(text);
  }

  $effect(() => {
    const p = $processingProgress;
    untrack(() => trackProgress(p));
  });

  /** Request a file open. Returns false when the file is already open (its tab is activated). */
  function sendOpenFile(path: string): boolean {
    const name = path.split('/').pop() ?? path;
    const existing = get(openFiles).find((f) => f.path === path);
    if (existing) {
      // A second reader of the same file would only duplicate its memory and its timeline entry.
      setActiveFile(existing.id);
      addToast('info', `${name} is already open. Close its tab first to reload it.`);
      return false;
    }
    beginOpenOp(path); // also cancels (and restores the view for) a previous pending open
    if (get(openFiles).length === 0) {
      // First file: show the viewer straight away; put the empty state back if the open fails.
      openRestore = { variable: get(selectedVariable), sweep: get(selectedSweep), filePath: get(radarData).filePath };
      selectedVariable.set(null);
      radarData.update((d) => ({ ...d, filePath: path }));
      selectedSweep.set(0);
    } else {
      // A file is already on screen: leave it (and its variable / sweep) untouched until the new
      // one arrives. Clearing them here would be saved as the outgoing file's selection when the
      // new file registers, and that tab would come back blank.
      openRestore = null;
    }
    pendingRecent = path.includes('://') || get(webUi) ? null : path;
    wsManager.openFile(path);
    addToast('info', `Opening ${name}...`);
    return true;
  }

  /** Re-open the most recent file on start-up when "Auto-load last file" is on. */
  function autoLoadLast() {
    const st = get(settings);
    if (get(webUi)) return; // recent files are local paths of another machine
    if (!st.data.autoLoadLast || get(openFiles).length > 0 || get(radarData).filePath) return;
    const last = st.data.recentFiles.find((f) => !f.path.includes('://'));
    if (last) openPaths([last.path]);
  }

  // --- Theme: mode (dark / light / follow OS), accent colour and base font size ---
  const DEFAULT_ACCENT = '#5b6cf7';

  function adjust(hex: string, amount: number): string {
    // amount > 0 lightens toward white, < 0 darkens toward black.
    const n = parseInt(hex.slice(1), 16);
    const ch = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((c) =>
      Math.round(amount >= 0 ? c + (255 - c) * amount : c * (1 + amount)),
    );
    return `#${ch.map((c) => c.toString(16).padStart(2, '0')).join('')}`;
  }

  function applyTheme(mode: 'dark' | 'light' | 'auto', accent: string, fontSize: number) {
    const root = document.documentElement;
    root.classList.toggle('theme-light', mode === 'light');
    root.classList.toggle('theme-auto', mode === 'auto');
    root.style.colorScheme = mode === 'auto' ? 'light dark' : mode;
    const isLight =
      mode === 'light' || (mode === 'auto' && window.matchMedia?.('(prefers-color-scheme: light)').matches);
    const props = ['--accent-primary', '--accent-hover', '--accent-muted', '--text-accent', '--bg-hover', '--bg-active'];
    if (!/^#[0-9a-f]{6}$/i.test(accent) || accent.toLowerCase() === DEFAULT_ACCENT) {
      props.forEach((p) => root.style.removeProperty(p));
    } else {
      const n = parseInt(accent.slice(1), 16);
      const rgb = `${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}`;
      root.style.setProperty('--accent-primary', accent);
      root.style.setProperty('--accent-hover', adjust(accent, isLight ? -0.15 : 0.2));
      root.style.setProperty('--text-accent', adjust(accent, isLight ? -0.15 : 0.25));
      root.style.setProperty('--accent-muted', `rgba(${rgb}, 0.25)`);
      root.style.setProperty('--bg-hover', `rgba(${rgb}, 0.08)`);
      root.style.setProperty('--bg-active', `rgba(${rgb}, 0.15)`);
    }
    const px = Number.isFinite(fontSize) ? Math.min(18, Math.max(11, fontSize)) : 13;
    root.style.fontSize = `${px}px`;
  }

  $effect(() => {
    const t = $settings.theme;
    applyTheme(t.mode, t.accentColor, t.fontSize);
  });

  // "Auto" follows the OS live: the accent shades depend on whether the OS is light or dark.
  $effect(() => {
    if ($settings.theme.mode !== 'auto' || !window.matchMedia) return;
    const mq = window.matchMedia('(prefers-color-scheme: light)');
    const on = () => { const t = get(settings).theme; applyTheme(t.mode, t.accentColor, t.fontSize); };
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  });

  // Nothing left to play (all files closed): the animation timer would otherwise write NaN sweeps.
  $effect(() => {
    if (totalSweeps === 0 && isPlaying) stopAnimation();
  });

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
  <input
    type="file"
    multiple
    class="visually-hidden-input"
    bind:this={fileInput}
    on:change={handleFilesChosen}
    tabindex="-1"
    aria-hidden="true"
    data-testid="web-file-input"
  />
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
        <span class="title-desktop">{$webUi ? 'web' : 'desktop'}</span>
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
      <button class="toolbar-btn icon-btn" on:click={() => (shortcutsSheetVisible = true)} title="Keyboard Shortcuts (?)" aria-label="Keyboard shortcuts">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h0m4 0h0m4 0h0m4 0h0m-10 4h4"/>
        </svg>
      </button>
      <button class="toolbar-btn icon-btn settings-btn" on:click={() => (settingsOpen = true)} title="Settings (Ctrl+,)" aria-label="Settings">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h0a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h0a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
        </svg>
      </button>
      <button class="toolbar-btn icon-btn" on:click={() => (aboutDialogVisible = true)} title="About" aria-label="About">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>
        </svg>
      </button>
      <div class="toolbar-separator"></div>
      <div class="connection-indicator" title={$webUi ? `${statusText} to the engine` : `${statusText} to Python sidecar`}>
        <span class="status-dot" style="background: {statusColor}"></span>
        <span class="status-text">{statusText}</span>
      </div>
    </div>
  </header>

  {#if engineMsg}
    <div class="web-banner engine-banner" class:bad={$engineState.phase === 'rejected' || $engineState.phase === 'unreachable' || $engineState.phase === 'unconfigured'} role="status" data-testid="engine-banner">
      {#if $engineState.phase === 'waking' || $engineState.phase === 'connecting'}<span class="spinner" aria-hidden="true"></span>{/if}
      <span>{engineMsg}</span>
      {#if $engineState.phase === 'waking' && $engineState.since != null}
        <span class="banner-dim">{$engineState.since}s, attempt {$engineState.attempt}</span>
      {/if}
    </div>
  {/if}
  {#if $webUi && !demoBannerDismissed}
    <div class="web-banner demo-banner" data-testid="demo-banner">
      <span>Demo: files are processed on a shared server and deleted when you close the tab.</span>
      <button class="banner-close" on:click={() => (demoBannerDismissed = true)} aria-label="Dismiss">&times;</button>
    </div>
  {/if}
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
      <FileTabs on:openfile={openFile} />
      <section class="viewer-area">
        {#if filePath}
          {#if $diffMode === 'diff'}
            <DiffViewer />
          {:else if viewMode === '2d'}
            <RadarViewer />
          {:else if viewMode === '3d'}
            <Viewer3D />
          {:else}
            <DualPolDashboard />
          {/if}
        {:else}
          <WelcomeScreen
            on:open-file={openFile}
            on:open-cloud={openLatestFromCloud}
            on:open-recent={(e) => openPaths([e.detail.path])}
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
    <OperationsTray />
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
    else if (type === 'toggle-left-sidebar') toggleLeftSidebar();
    else if (type === 'toggle-right-sidebar') toggleRightSidebar();
    else if (type === 'toggle-animation') toggleAnimation();
    else if (type === 'set-view') { viewMode = value; syncWorkspace(); }
    else if (type === 'open-settings') settingsOpen = true;
    else if (type === 'show-shortcuts') shortcutsSheetVisible = true;
    else if (type === 'show-tour') onboardingVisible = true;
    else if (type === 'about') aboutDialogVisible = true;
    else if (type === 'export') showPanel('export');
  }} />
  <OnboardingTour bind:visible={onboardingVisible} />
  <ShortcutsSheet bind:visible={shortcutsSheetVisible} />
  <AboutDialog bind:visible={aboutDialogVisible} />
  {#if settingsOpen}
    <SettingsDialog open={true} on:close={() => (settingsOpen = false)} />
  {/if}
</div>

<style>
  .visually-hidden-input { position: fixed; left: -9999px; width: 1px; height: 1px; opacity: 0; pointer-events: none; }
  .web-banner {
    display: flex; align-items: center; justify-content: center; gap: 8px; flex-shrink: 0;
    padding: 3px var(--spacing-lg); font-size: 11px; color: var(--text-secondary);
    background: var(--glass-bg-heavy); border-bottom: 1px solid var(--glass-border);
  }
  .engine-banner { color: var(--accent-warning); }
  .engine-banner.bad { color: var(--accent-danger); }
  .banner-dim { color: var(--text-muted); font-variant-numeric: tabular-nums; }
  .banner-close {
    background: transparent; border: none; color: var(--text-muted); cursor: pointer;
    font-size: 14px; line-height: 1; padding: 0 4px; height: auto; box-shadow: none;
  }
  .banner-close:hover { color: var(--text-primary); }
  .spinner {
    width: 10px; height: 10px; border-radius: 50%; border: 2px solid currentColor;
    border-right-color: transparent; animation: banner-spin 0.9s linear infinite;
  }
  @keyframes banner-spin { to { transform: rotate(360deg); } }
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

  .app-title { white-space: nowrap; font-size: 15px; font-weight: 600; letter-spacing: -0.02em; user-select: none; }
  .title-x { color: var(--accent-primary); font-weight: 700; text-shadow: 0 0 12px rgba(91, 108, 247, 0.4); }
  .title-rest { color: var(--text-primary); }
  .title-desktop { color: var(--text-muted); font-weight: 400; margin-left: 4px; font-size: 13px; }

  .cmd-trigger {
    display: flex; align-items: center; gap: 6px; padding: 4px 10px; height: 28px;
    font-size: 12px; color: var(--text-muted); background: var(--field-bg);
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
  .statusbar-center { flex: 1; justify-content: center; min-width: 0; overflow: hidden; }
  @media (max-width: 900px) { .statusbar-right { display: none; } }
  .statusbar-right { min-width: 100px; justify-content: flex-end; }
  .statusbar-divider { color: var(--glass-border); margin: 0 4px; }
  .status-dot-sm { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; transition: background var(--transition-normal); }
  .status-dot-sm.pulse { animation: pulse-glow 2.5s ease-in-out infinite; }
  .file-path { font-size: 11px; color: var(--text-secondary); max-width: 400px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  /* Narrow windows: drop the secondary toolbar labels first, then let the sidebars shrink so the
     viewer keeps a usable width (sidebar px widths are a preference, not a minimum). */
  @media (max-width: 1250px) {
    .title-desktop, .cmd-trigger-label, .cmd-trigger :global(kbd), .status-text { display: none; }
    .cmd-trigger { min-width: 0; padding: 4px 8px; }
    .toolbar { padding: 0 var(--spacing-sm); }
    .toolbar-left { min-width: 0; }
    .toolbar-right { min-width: 0; }
    .sidebar { max-width: 28vw; }
  }
  @media (max-width: 1000px) {
    .speed-group, .toolbar-separator { display: none; }
    .toolbar-btn { padding-left: var(--spacing-sm); padding-right: var(--spacing-sm); }
    .sidebar { max-width: 25vw; }
  }

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
