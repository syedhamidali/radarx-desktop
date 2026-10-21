<script lang="ts">
  import { onMount, onDestroy, untrack, tick } from 'svelte';
  import PpiOnlyNotice from './lib/components/PpiOnlyNotice.svelte';
  import { source3d } from './lib/stores/source3d';
  import DataInspector from './lib/components/DataInspector.svelte';
  import ToastContainer from './lib/components/ToastContainer.svelte';
  import WorkspaceSwitcher from './lib/components/WorkspaceSwitcher.svelte';
  import PanelManager from './lib/components/PanelManager.svelte';
  import WelcomeScreen from './lib/components/WelcomeScreen.svelte';
  import { lazy, whenIdle } from './lib/utils/lazy.svelte';
  import LogoMark from './lib/components/LogoMark.svelte';
  import EngineBoot from './lib/components/EngineBoot.svelte';
  import FileTabs from './lib/components/FileTabs.svelte';
  import NotificationCenter from './lib/components/NotificationCenter.svelte';
  import OperationsTray from './lib/components/OperationsTray.svelte';
  import UpdateChip from './lib/components/UpdateChip.svelte';
  import AccountChip from './lib/components/AccountChip.svelte';
  import DataCheckChip from './lib/components/DataCheckChip.svelte';
  import { panelOpen as dataCheckOpen, openDataCheck } from './lib/stores/dataCheck';
  import { movieStudioOpen, openMovieStudio } from './lib/stores/movieStudio';
  import { panelOpen as autopilotOpen, openAutopilot } from './lib/stores/autopilot';
  import { routeDrop, AUTOPILOT_DROP_MIN } from './lib/utils/dropRoute';
  import { updater } from './lib/stores/updater';
  import { reportIssueOpen, openReportIssue, closeReportIssue } from './lib/stores/reportIssue';
  import { commandStudioOpen, openCommandStudio } from './lib/stores/commandStudio';
  import { SIDEBAR_MIN, SIDEBAR_MAX, SIDEBAR_DEFAULT, clampSidebarWidth, sidebarWidthAfterKey } from './lib/utils/sidebarResize';
  import { initUpdater } from './lib/utils/updater';
  import { crossSectionStore } from './lib/stores/crossSection';
  import { connectionStatus, radarData, selectedSweep, selectedVariable, processingProgress } from './lib/stores/radarData';
  import { diffMode, toggleDiffMode } from './lib/stores/temporal';
  import { get } from 'svelte/store';
  import { sidecarPort, settings, toggleMapHidden } from './lib/stores/settings';
  import { recentFiles } from './lib/stores/recentFiles';
  import { openFiles, setActiveFile } from './lib/stores/fileManager';
  import { matchesCombo, SHORTCUT_REGISTRY } from './lib/utils/shortcuts';
  import { wsManager } from './lib/utils/websocket';
  import { beginOp, cancelOp, dismissOp, type OpHandle } from './lib/utils/requestScheduler';
  import { startUiHealth, stopUiHealth } from './lib/utils/uiHealth';
  import { addToast } from './lib/stores/toasts';
  import { workspaceStore, type WorkspaceConfig } from './lib/stores/workspace';
  import { isWebMode, basename } from './lib/utils/env';
  import { webUi, engineState, engineStatusMessage } from './lib/stores/webInfo';
  import { uploadFiles } from './lib/utils/upload';
  import { applyThemeMode } from './lib/utils/theme';
  import { sidecarError } from './lib/stores/boot';
  import { uiHealth } from './lib/utils/uiHealth';

  // Code that is not needed for the first paint is fetched on demand (each becomes its own chunk).
  // The main view and the timeline are prefetched while the browser is idle, so opening a file
  // does not wait for them; the 3D / dual-pol / diff views, dialogs and the command palette load
  // when first asked for.
  const radarViewer = lazy(() => import('./lib/components/RadarViewer.svelte'));
  const viewer3d = lazy(() => import('./lib/components/Viewer3D.svelte'));
  const dualPol = lazy(() => import('./lib/components/DualPolDashboard.svelte'));
  const diffViewer = lazy(() => import('./lib/components/DiffViewer.svelte'));
  const timeline = lazy(() => import('./lib/components/Timeline.svelte'));
  const crossDock = lazy(() => import('./lib/components/CrossSectionDock.svelte'));
  const crossDialog = lazy(() => import('./lib/components/CrossSectionDialog.svelte'));
  const movieStudio = lazy(() => import('./lib/components/MovieStudio.svelte'));
  const palette = lazy(() => import('./lib/components/CommandPalette.svelte'));
  const dataCheckPanel = lazy(() => import('./lib/components/DataCheckPanel.svelte'));
  const autopilotPanel = lazy(() => import('./lib/components/AutopilotPanel.svelte'));
  const tour = lazy(() => import('./lib/components/OnboardingTour.svelte'));
  const shortcutsSheet = lazy(() => import('./lib/components/ShortcutsSheet.svelte'));
  const aboutDialog = lazy(() => import('./lib/components/AboutDialog.svelte'));
  const settingsDialog = lazy(() => import('./lib/components/SettingsDialog.svelte'));
  const reportDialog = lazy(() => import('./lib/components/ReportIssueDialog.svelte'));
  // Command Studio: every rxd command as a form (stores/commandStudio.ts).
  const commandStudio = lazy(() => import('./lib/components/CommandStudio.svelte'));
  // The consent step of environment data (soundings, models, ...): mounted once the browser is idle,
  // it opens itself when a download needs the user's approval.
  const envDialog = lazy(() => import('./lib/components/EnvApprovalDialog.svelte'));
  const updateDialog = lazy(() => import('./lib/components/UpdateDialog.svelte'));
  // "Share" offer after an image or movie is saved.
  const shareHost = lazy(() => import('./lib/components/ShareHost.svelte'));
  // The live map (its store, the tile map and the radar chooser) loads the first time it is asked for.
  const liveView = lazy(() => import('./lib/components/LiveMapView.svelte'));
  const liveChooser = lazy(() => import('./lib/components/LiveRadarChooser.svelte'));

  // --- Derived from stores (Svelte 5 runes) ---
  const filePath = $derived($radarData.filePath);
  const totalSweeps = $derived($radarData.sweeps.length);
  /** Every sweep of the file is a range-height scan: nothing for the 3D volume to stack. */
  const rhiOnly = $derived($radarData.sweeps.length > 0 && $radarData.sweeps.every((s) => s.kind === 'rhi'));
  const currentSweep = $derived($selectedSweep);
  const variableCount = $derived($radarData.variables.length);
  const progress = $derived($processingProgress);
  /** Status LED variant for the engine link (see .led in styles/theme.css). */
  const ledClass = $derived(
    $connectionStatus === 'connected' ? 'nominal pulse'
    : $connectionStatus === 'connecting' ? 'caution blink'
    : 'alarm'
  );
  const statusText = $derived(
    $connectionStatus === 'connected' ? 'Connected'
    : $engineState.phase === 'waking' ? 'Waking engine...'
    : $connectionStatus === 'connecting' ? 'Connecting...'
    : 'Disconnected'
  );
  const engineWord = $derived(
    $connectionStatus === 'connected' ? 'NOMINAL'
    : $connectionStatus === 'connecting' ? 'LINKING'
    : 'OFFLINE'
  );
  // The boot panel replaces the welcome screen until the engine answers (and again if the link drops).
  let everConnected = $state(false);
  $effect(() => { if ($connectionStatus === 'connected') everConnected = true; });

  // UTC mission clock (the radar world runs on UTC) and a few real system readouts.
  let nowMs = $state(Date.now());
  let heapMb = $state<number | null>(null);
  const pad2 = (n: number) => String(n).padStart(2, '0');
  const utc = $derived.by(() => {
    const d = new Date(nowMs);
    return {
      date: `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())}`,
      time: `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}:${pad2(d.getUTCSeconds())}`,
    };
  });
  let clockTimer: ReturnType<typeof setInterval> | null = null;
  function tickClock() {
    nowMs = Date.now();
    // Chromium / WebView2 only; WKWebView has no heap stats and the readout is simply hidden there.
    const mem = (performance as any).memory?.usedJSHeapSize;
    heapMb = typeof mem === 'number' ? Math.round(mem / 1048576) : null;
  }
  // Web mode only: why the remote engine is not reachable right now (null = nothing to say).
  const engineMsg = $derived(
    isWebMode() && $connectionStatus !== 'connected' ? engineStatusMessage($engineState) : null,
  );
  let demoBannerDismissed = $state(false);
  let fileInput = $state<HTMLInputElement | null>(null);

  // --- Local reactive state ---
  let leftSidebarOpen = $state(true);
  let rightSidebarOpen = $state(true);
  let viewMode = $state<'2d' | '3d' | 'dualpol' | 'live'>('2d');
  let liveChooserOpen = $state(false);
  /** After the chooser saves: start the live map (set when the chooser was opened by the Live button). */
  let liveStartAfterChoose = false;
  $effect(() => { if (viewMode === 'live') void liveView.load(); });
  $effect(() => { if (liveChooserOpen) void liveChooser.load(); });
  /** The cross-section pane under the PPI (only beside the 2D view). */
  const crossSectionDocked = $derived($crossSectionStore.layout === 'dock' && viewMode === '2d' && $diffMode !== 'diff');
  let commandPaletteVisible = $state(false);
  let shortcutsSheetVisible = $state(false);
  let aboutDialogVisible = $state(false);
  let notificationCenterOpen = $state(false);
  let onboardingVisible = $state(false);
  let settingsOpen = $state(false);
  /** Any modal layer open: global plain-key shortcuts must not act behind it. */
  const modalOpen = $derived(
    commandPaletteVisible || shortcutsSheetVisible || aboutDialogVisible || settingsOpen || onboardingVisible || $reportIssueOpen || liveChooserOpen,
  );
  // Fetch the chunks the current state needs (idempotent; see the `lazy(...)` declarations above).
  $effect(() => { if (filePath) { radarViewer.load(); timeline.load(); } });
  $effect(() => { if (filePath && $diffMode === 'diff') diffViewer.load(); });
  $effect(() => { if (filePath && $diffMode !== 'diff' && viewMode === '3d') viewer3d.load(); });
  $effect(() => { if (filePath && $diffMode !== 'diff' && viewMode === 'dualpol') dualPol.load(); });
  $effect(() => { if (crossSectionDocked) crossDock.load(); });
  $effect(() => { if ($crossSectionStore.layout === 'modal') crossDialog.load(); });
  $effect(() => { if (commandPaletteVisible) palette.load(); });
  $effect(() => { if ($dataCheckOpen) dataCheckPanel.load(); });
  $effect(() => { if ($autopilotOpen) autopilotPanel.load(); });
  $effect(() => { if (onboardingVisible) tour.load(); });
  $effect(() => { if (shortcutsSheetVisible) shortcutsSheet.load(); });
  $effect(() => { if (aboutDialogVisible) aboutDialog.load(); });
  $effect(() => { if (settingsOpen) settingsDialog.load(); });
  $effect(() => { if ($reportIssueOpen) reportDialog.load(); });
  $effect(() => { if ($commandStudioOpen) commandStudio.load(); });
  $effect(() => { whenIdle(() => envDialog.load()); });
  // Not needed for the first paint: fetched when idle (so its fade-in plays), or at once if it is asked for.
  $effect(() => { whenIdle(() => updateDialog.load()); });
  $effect(() => { if ($updater.dialogOpen) updateDialog.load(); });
  $effect(() => { whenIdle(() => shareHost.load()); });
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
    // The main view is needed the moment a file opens: fetch it as soon as the first paint is done.
    whenIdle(() => { void radarViewer.load(); void timeline.load(); void crossDock.load(); });
    startUiHealth();
    void initUpdater(); // desktop only; a no-op in a browser or when updates are not configured
    tickClock();
    clockTimer = setInterval(tickClock, 1000);

    window.addEventListener('keydown', handleKeyDown);
    window.addEventListener('xradar:set-view-mode', handleSetViewMode);
    window.addEventListener('xradar:cloud-site', onCloudSite);
    window.addEventListener('xradar:live-chooser', onLiveChooser);
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
          const paths = ((event.payload as any).paths as string[] | undefined) ?? [];
          // Many files, or any folder: Autopilot inspects and plans; nothing opens by itself.
          void routeDroppedPaths(paths);
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
    window.removeEventListener('xradar:cloud-site', onCloudSite);
    window.removeEventListener('xradar:live-chooser', onLiveChooser);
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
    if (clockTimer) clearInterval(clockTimer);
  });

  function reportSidecarError(message: string) {
    if (message === lastSidecarError) return;
    lastSidecarError = message;
    sidecarError.set(message);
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
        case 'toggle-map': toggleMapHidden(); break;
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

  /** Desktop drop: ask the shell which paths are folders, then open or hand to the Autopilot. */
  async function routeDroppedPaths(paths: string[]) {
    let kinds: string[] | null = null;
    if (paths.length > 0 && paths.length < AUTOPILOT_DROP_MIN) {
      try {
        const { invoke } = await import('@tauri-apps/api/core');
        kinds = await invoke<string[]>('path_kinds', { paths });
      } catch (err) {
        console.warn('[App] path_kinds unavailable:', err);
      }
    }
    const route = routeDrop(paths, kinds);
    if (route === 'autopilot') openAutopilot(paths);
    else if (route === 'open') openPaths(paths);
  }

  /** Welcome screen: open the Cloud Data panel and fetch the latest scan of its station. */
  async function openLatestFromCloud() {
    await showPanel('cloud');
    window.dispatchEvent(new CustomEvent('xradar:cloud-open-latest'));
  }

  /** Radar sites card: bring the Cloud panel into view, then let it act on the queued request. */
  async function onCloudSite() {
    await showPanel('cloud');
    window.dispatchEvent(new CustomEvent('xradar:cloud-site-apply'));
  }

  /** Live button / palette / welcome card: show the live map; without default radars, choose them first. */
  async function openLive() {
    if (get(settings).live.radars.length === 0) {
      liveStartAfterChoose = true;
      liveChooserOpen = true;
      return;
    }
    viewMode = 'live';
    syncWorkspace();
    await liveView.load();
    const live = await import('./lib/stores/live');
    live.startLive();
  }

  /** Settings > Data > "Choose on the map…" and the live view's "Radars…" button. */
  function onLiveChooser() {
    liveStartAfterChoose = false;
    liveChooserOpen = true;
  }

  async function onLiveRadarsSaved(ids: string[]) {
    if (ids.length === 0) return;
    const live = await import('./lib/stores/live');
    const wasRunning = get(live.liveState).running;
    if (liveStartAfterChoose || wasRunning || viewMode === 'live') {
      // A new choice restarts the session with the new radars (the consent given this session holds).
      if (wasRunning) live.stopLive();
      liveStartAfterChoose = false;
      await openLive();
    }
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
    const name = basename(path);
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
    const name = basename(path);
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

  // --- Theme: mode (Mission control / Clean room / follow OS), accent colour and base font size ---
  const DEFAULT_ACCENTS = ['#2bd9f5', '#5b6cf7']; // current default and the pre-redesign indigo (= "not customised")

  function adjust(hex: string, amount: number): string {
    // amount > 0 lightens toward white, < 0 darkens toward black.
    const n = parseInt(hex.slice(1), 16);
    const ch = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((c) =>
      Math.round(amount >= 0 ? c + (255 - c) * amount : c * (1 + amount)),
    );
    return `#${ch.map((c) => c.toString(16).padStart(2, '0')).join('')}`;
  }

  /** Relative luminance (0..1) of a #rrggbb colour, to choose readable text on an accent fill. */
  function luminance(hex: string): number {
    const n = parseInt(hex.slice(1), 16);
    const lin = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((c) => {
      const v = c / 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
  }

  function applyTheme(mode: 'dark' | 'light' | 'auto', accent: string, fontSize: number) {
    const root = document.documentElement;
    const isLight = applyThemeMode(mode) === 'light';
    // A custom accent overrides the token pair every alias derives from (see styles/tokens.css).
    const props = ['--c-accent', '--c-accent-rgb', '--c-accent-strong', '--c-on-accent'];
    if (!/^#[0-9a-f]{6}$/i.test(accent) || DEFAULT_ACCENTS.includes(accent.toLowerCase())) {
      props.forEach((p) => root.style.removeProperty(p));
    } else {
      const n = parseInt(accent.slice(1), 16);
      root.style.setProperty('--c-accent', accent);
      root.style.setProperty('--c-accent-rgb', `${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}`);
      root.style.setProperty('--c-accent-strong', adjust(accent, isLight ? -0.2 : 0.3));
      root.style.setProperty('--c-on-accent', luminance(accent) > 0.35 ? '#04141c' : '#ffffff');
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
      leftSidebarWidth = clampSidebarWidth(resizeStartWidth + delta);
    } else if (isResizingRight) {
      const delta = resizeStartX - e.clientX;
      rightSidebarWidth = clampSidebarWidth(resizeStartWidth + delta);
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

  // A double click is two mouse-downs and mouse-ups first (each one saved the old width), so the
  // reset has to be saved too, or the next launch comes back with the width that was just reset.
  function resetLeftWidth() { leftSidebarWidth = SIDEBAR_DEFAULT; syncWorkspace(); }
  function resetRightWidth() { rightSidebarWidth = SIDEBAR_DEFAULT; syncWorkspace(); }

  /** Keyboard resizing of a sidebar (the handles are focusable separators). */
  function onResizeKey(side: 'left' | 'right', e: KeyboardEvent) {
    const cur = side === 'left' ? leftSidebarWidth : rightSidebarWidth;
    const next = sidebarWidthAfterKey(side, e.key, e.shiftKey, cur);
    if (next === null) return;
    e.preventDefault();
    e.stopPropagation(); // the arrows / Home / End also step through sweeps at app level
    if (side === 'left') leftSidebarWidth = next;
    else rightSidebarWidth = next;
    syncWorkspace();
  }

  function handleWorkspaceSwitch(e: CustomEvent<{ config: WorkspaceConfig }>) {
    applyWorkspace(e.detail.config);
    addToast('info', `Workspace loaded: ${e.detail.config.name}`);
  }
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
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
      <div class="app-title" title="Radarx {$webUi ? 'web' : 'desktop'} v{__APP_VERSION__}">
        <LogoMark size={28} animate={$connectionStatus === 'connecting'} />
        <span class="title-word">RADAR<span class="title-x">X</span></span>
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
        <span class="sweep-counter" title="Sweep"><span class="hud-label">SWP</span><span class="mono">{#if totalSweeps > 0}{pad2(currentSweep + 1)}/{pad2(totalSweeps)}{:else}--/--{/if}</span></span>
        <div class="speed-group" title="Animation speed">
          <button class="speed-btn" class:active={animSpeed === 2000} on:click={() => setSpeed(2000)} disabled={totalSweeps === 0}>0.5x</button>
          <button class="speed-btn" class:active={animSpeed === 1000} on:click={() => setSpeed(1000)} disabled={totalSweeps === 0}>1x</button>
          <button class="speed-btn" class:active={animSpeed === 500} on:click={() => setSpeed(500)} disabled={totalSweeps === 0}>2x</button>
        </div>
      </div>

      <div class="toolbar-separator"></div>

      <div class="view-toggle">
        <button class="view-btn" class:active={viewMode === '2d'} on:click={() => { viewMode = '2d'; syncWorkspace(); }} title="2D PPI View">2D</button>
        <button class="view-btn" class:active={viewMode === '3d'} on:pointerenter={() => viewer3d.load()} on:focus={() => viewer3d.load()} on:click={() => { viewMode = '3d'; syncWorkspace(); }} title="3D Volume View">3D</button>
        <button class="view-btn" class:active={viewMode === 'dualpol'} on:pointerenter={() => dualPol.load()} on:focus={() => dualPol.load()} on:click={() => { viewMode = 'dualpol'; syncWorkspace(); }} title="Dual-Pol Analysis Dashboard">DualPol</button>
      </div>

      <button class="toolbar-btn" on:pointerenter={() => movieStudio.load()} on:focus={() => movieStudio.load()} on:click={() => { void movieStudio.load(); openMovieStudio(); }} title="Movie Studio: make a storm movie (MP4)" aria-label="Movie Studio">Movie</button>
      {#if !isWebMode()}
        <button class="toolbar-btn" class:active={viewMode === 'live'} on:pointerenter={() => liveView.load()} on:focus={() => liveView.load()} on:click={openLive} title="Live map: the latest scans of your default radars (asks before downloading)" aria-label="Live map" data-testid="toolbar-live">Live</button>
      {/if}
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
      <div class="utc-clock" title="Coordinated Universal Time" aria-label="UTC time {utc.time}">
        <span class="hud-label">UTC</span>
        <span class="clock-date mono">{utc.date}</span>
        <span class="clock-time mono">{utc.time}<span class="clock-z">Z</span></span>
      </div>
      <div class="toolbar-separator"></div>
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
        <span class="led {ledClass}"></span>
        <span class="status-text hud-label">{engineWord}</span>
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
      <!-- A focusable separator is a widget (ARIA window splitter): svelte only knows the static role. -->
      <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
      <div
        class="resize-handle resize-handle-left"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize the data inspector"
        aria-valuemin={SIDEBAR_MIN}
        aria-valuemax={SIDEBAR_MAX}
        aria-valuenow={leftSidebarWidth}
        tabindex="0"
        on:mousedown={startResizeLeft}
        on:dblclick={resetLeftWidth}
        on:keydown={(e) => onResizeKey('left', e)}
        title="Drag to resize, double-click to reset (arrow keys when focused)"
      ></div>
    {/if}
    <div class="viewer-and-timeline">
      <FileTabs on:openfile={openFile} />
      <section class="viewer-area" class:with-dock={crossSectionDocked}>
        {#if viewMode === 'live'}
          {#if liveView.component}<liveView.component onchoose={onLiveChooser} />{/if}
        {:else if filePath}
          {#if $diffMode === 'diff'}
            {#if diffViewer.component}<diffViewer.component />{/if}
          {:else if viewMode === '2d'}
            {#if radarViewer.component}<radarViewer.component />{/if}
            {#if crossSectionDocked && crossDock.component}
              <crossDock.component />
            {/if}
          {:else if viewMode === '3d'}
            {#if rhiOnly && $source3d === 'radar'}
              <PpiOnlyNotice title="3D needs PPI sweeps" />
            {:else if viewer3d.component}<viewer3d.component />{/if}
          {:else}
            {#if dualPol.component}<dualPol.component />{/if}
          {/if}
        {:else if $connectionStatus !== 'connected'}
          <EngineBoot {everConnected} />
        {:else}
          <WelcomeScreen
            on:open-file={openFile}
            on:open-cloud={openLatestFromCloud}
            on:open-live={openLive}
            on:open-recent={(e) => openPaths([e.detail.path])}
            on:load-workspace={(e) => {
              const config = workspaceStore.loadWorkspace(e.detail.name);
              if (config) applyWorkspace(config);
            }}
          />
        {/if}
      </section>
      {#if filePath && timeline.component && viewMode !== 'live'}
        <timeline.component />
      {/if}
    </div>
    {#if rightSidebarOpen}
      <!-- A focusable separator is a widget (ARIA window splitter): svelte only knows the static role. -->
      <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
      <div
        class="resize-handle resize-handle-right"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize the tools panel"
        aria-valuemin={SIDEBAR_MIN}
        aria-valuemax={SIDEBAR_MAX}
        aria-valuenow={rightSidebarWidth}
        tabindex="0"
        on:mousedown={startResizeRight}
        on:dblclick={resetRightWidth}
        on:keydown={(e) => onResizeKey('right', e)}
        title="Drag to resize, double-click to reset (arrow keys when focused)"
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
      <span class="led {ledClass}" title={statusText}></span>
      <span class="hud-label sb-key">ENGINE</span>
      <span class="sb-val mono" class:ok={$connectionStatus === 'connected'}>{engineWord}</span>
      {#if progress && progress.percent > 0 && progress.percent < 100}
        <span class="statusbar-divider">/</span>
        <span class="progress-text">{progress.message} ({Math.round(progress.percent)}%)</span>
      {/if}
    </div>
    <div class="statusbar-center">
      {#if filePath}
        <span class="hud-label sb-key">FILE</span>
        <span class="file-path mono" title={filePath}>{basename(filePath)}</span>
        {#if variableCount > 0}<span class="statusbar-divider">/</span><span class="mono sb-dim">{variableCount} vars</span>{/if}
      {:else}
        <span class="sb-dim">No file loaded. Drag and drop, or {typeof navigator !== 'undefined' && navigator.platform?.includes('Mac') ? '\u2318' : 'Ctrl'}+O</span>
      {/if}
    </div>
    <DataCheckChip />
    <UpdateChip />
    <AccountChip />
    <OperationsTray />
    <div class="statusbar-right">
      <span class="hud-label sb-key">FILES</span><span class="mono sb-val">{pad2($openFiles.length)}</span>
      {#if $uiHealth.fps}
        <span class="statusbar-divider">/</span>
        <span class="hud-label sb-key">FPS</span><span class="mono sb-val">{$uiHealth.fps}</span>
      {/if}
      {#if heapMb !== null}
        <span class="statusbar-divider">/</span>
        <span class="hud-label sb-key">MEM</span><span class="mono sb-val">{heapMb}M</span>
      {/if}
      <span class="statusbar-divider hint-part">/</span>
      <span class="sb-dim hint-part">{typeof navigator !== 'undefined' && navigator.platform?.includes('Mac') ? '\u2318' : 'Ctrl'}+K commands</span>
      <span class="statusbar-divider hint-part">/</span>
      <span class="sb-dim hint-part">? shortcuts</span>
    </div>
  </footer>

  <ToastContainer />
  {#if updateDialog.component}<updateDialog.component />{/if}
  {#if $dataCheckOpen && dataCheckPanel.component}<dataCheckPanel.component onshowpanel={showPanel} />{/if}
  {#if $autopilotOpen && autopilotPanel.component}<autopilotPanel.component onopen={openPaths} />{/if}
  {#if palette.component}
  <palette.component bind:visible={commandPaletteVisible} on:close={() => (commandPaletteVisible = false)} on:action={(e) => {
    const { type, value } = e.detail;
    if (type === 'open-file') openFile();
    else if (type === 'toggle-left-sidebar') toggleLeftSidebar();
    else if (type === 'toggle-right-sidebar') toggleRightSidebar();
    else if (type === 'toggle-animation') toggleAnimation();
    else if (type === 'toggle-map') toggleMapHidden();
    else if (type === 'set-view') { viewMode = value; syncWorkspace(); }
    else if (type === 'open-settings') settingsOpen = true;
    else if (type === 'show-shortcuts') shortcutsSheetVisible = true;
    else if (type === 'show-tour') onboardingVisible = true;
    else if (type === 'about') aboutDialogVisible = true;
    else if (type === 'report-issue') openReportIssue();
    else if (type === 'command-studio') openCommandStudio();
    else if (type === 'export') showPanel('export');
    else if (type === 'data-check') openDataCheck();
    else if (type === 'movie-studio') { void movieStudio.load(); openMovieStudio(); }
    else if (type === 'autopilot') openAutopilot(get(openFiles).map((f) => f.path));
    else if (type === 'live-map') void openLive();
    else if (type === 'live-radars') onLiveChooser();
  }} />
  {/if}
  {#if liveChooserOpen && liveChooser.component}
    <liveChooser.component onclose={() => (liveChooserOpen = false)} onsaved={onLiveRadarsSaved} />
  {/if}
  {#if $movieStudioOpen && movieStudio.component}<movieStudio.component />{/if}
  {#if tour.component}<tour.component bind:visible={onboardingVisible} />{/if}
  {#if shortcutsSheet.component}<shortcutsSheet.component bind:visible={shortcutsSheetVisible} />{/if}
  {#if aboutDialog.component}<aboutDialog.component bind:visible={aboutDialogVisible} />{/if}
  {#if crossDialog.component}<crossDialog.component />{/if}
  {#if envDialog.component}<envDialog.component />{/if}
  {#if shareHost.component}<shareHost.component />{/if}
  {#if $reportIssueOpen && reportDialog.component}<reportDialog.component onclose={closeReportIssue} />{/if}
  {#if $commandStudioOpen && commandStudio.component}<commandStudio.component onopen={openPaths} />{/if}
  {#if settingsOpen && settingsDialog.component}
    <settingsDialog.component open={true} on:close={() => (settingsOpen = false)} />
  {/if}
</div>

<style>
  .visually-hidden-input { position: fixed; left: -9999px; width: 1px; height: 1px; opacity: 0; pointer-events: none; }
  .web-banner {
    display: flex; align-items: center; justify-content: center; gap: 8px; flex-shrink: 0;
    padding: 3px var(--space-4); font-size: var(--fs-xs); color: var(--c-text-dim);
    background: var(--c-overlay); border-bottom: 1px solid var(--c-border);
  }
  .engine-banner { color: var(--c-caution); border-bottom-color: rgba(var(--c-caution-rgb), 0.4); }
  .engine-banner.bad { color: var(--c-alarm); border-bottom-color: rgba(var(--c-alarm-rgb), 0.45); }
  .banner-dim { color: var(--c-muted); font-variant-numeric: tabular-nums; }
  .banner-close {
    background: transparent; border: none; color: var(--c-muted); cursor: pointer;
    font-size: 14px; line-height: 1; padding: 0 4px; height: auto; box-shadow: none;
  }
  .banner-close:hover { color: var(--c-text); background: transparent; }
  .spinner {
    width: 10px; height: 10px; border-radius: 50%; border: 2px solid currentColor;
    border-right-color: transparent; animation: banner-spin 0.9s linear infinite;
  }
  @keyframes banner-spin { to { transform: rotate(360deg); } }
  .app-layout {
    display: flex; flex-direction: column; height: 100vh; width: 100vw;
    overflow: hidden; background: var(--c-surface-0); position: relative;
  }

  .drop-overlay {
    position: fixed; inset: 0; z-index: 1000;
    background: var(--c-scrim); backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
    display: flex; align-items: center; justify-content: center; pointer-events: none;
    animation: fade-in var(--dur-fast) ease-out;
  }
  .drop-overlay-inner {
    display: flex; flex-direction: column; align-items: center; gap: 16px;
    padding: 48px 64px; border: 1px dashed var(--c-accent); border-radius: var(--radius-md);
    color: var(--c-accent); background: rgba(var(--c-accent-rgb), 0.06);
    box-shadow: var(--glow-accent);
  }
  .drop-label { font-size: var(--fs-lg); font-weight: 600; letter-spacing: var(--ls-label); text-transform: uppercase; color: var(--c-accent); }

  /* ---- Top bar ---- */
  .toolbar {
    display: flex; align-items: center; justify-content: space-between;
    height: var(--toolbar-height); padding: 0 var(--space-3);
    background: linear-gradient(180deg, var(--c-surface-2), var(--c-panel-solid));
    border-bottom: 1px solid var(--c-border-strong); flex-shrink: 0;
    -webkit-app-region: drag; position: relative; z-index: 10;
  }
  /* segmented hairline under the bar: accent ticks at the corners */
  .toolbar::after {
    content: ''; position: absolute; left: 0; right: 0; bottom: -1px; height: 1px; pointer-events: none;
    background: linear-gradient(90deg, var(--c-accent) 0 56px, transparent 56px calc(100% - 56px), var(--c-accent) calc(100% - 56px));
    opacity: 0.8;
  }
  .toolbar-left, .toolbar-center, .toolbar-right {
    display: flex; align-items: center; gap: var(--space-2); -webkit-app-region: no-drag;
  }
  .toolbar-left { min-width: 260px; gap: var(--space-3); }
  .toolbar-right { min-width: 160px; justify-content: flex-end; }

  .app-title { display: flex; align-items: center; gap: 9px; white-space: nowrap; user-select: none; }
  .title-word { font-size: var(--fs-lg); font-weight: 600; letter-spacing: 0.22em; color: var(--c-text); }
  .title-x { color: var(--c-accent); }
  .title-desktop {
    font-family: var(--font-mono); font-size: var(--fs-2xs); letter-spacing: 0.14em; text-transform: uppercase;
    color: var(--c-muted); padding: 1px 5px; border: 1px solid var(--c-border); border-radius: var(--radius-xs);
  }

  .cmd-trigger {
    display: flex; align-items: center; gap: 6px; padding: 4px 10px; height: 28px;
    font-size: var(--fs-sm); color: var(--c-muted); background: var(--c-field);
    border: 1px solid var(--c-border); border-radius: var(--radius-sm);
    cursor: pointer; min-width: 140px;
  }
  .cmd-trigger:hover { border-color: var(--c-accent); color: var(--c-text-dim); background: var(--c-field); }
  .cmd-trigger-label { flex: 1; text-align: left; font-size: var(--fs-xs); }
  .cmd-trigger :global(kbd) { font-size: 9px; padding: 0 4px; }

  .toolbar-btn { display: inline-flex; align-items: center; gap: 6px; padding: var(--space-1) var(--space-3); font-size: var(--fs-sm); height: 28px; }
  .toolbar-btn.icon-btn { padding: var(--space-1) var(--space-2); }
  .toolbar-btn.active { background: rgba(var(--c-accent-rgb), 0.14); border-color: rgba(var(--c-accent-rgb), 0.55); color: var(--c-accent); }
  .toolbar-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .toolbar-separator { width: 1px; height: 18px; background: var(--c-border-strong); margin: 0 var(--space-1); opacity: 0.6; }

  .anim-controls { display: flex; align-items: center; gap: var(--space-2); }
  .anim-controls.disabled { opacity: 0.4; }
  .sweep-counter {
    display: inline-flex; align-items: baseline; gap: 6px; min-width: 70px; justify-content: center;
    font-size: var(--fs-sm); color: var(--c-text); user-select: none;
  }
  .speed-group { display: flex; align-items: center; border: 1px solid var(--c-border); border-radius: var(--radius-sm); overflow: hidden; background: var(--c-field); }
  .speed-btn {
    background: transparent; border: none; border-right: 1px solid var(--c-border);
    color: var(--c-text-dim); font-family: var(--font-mono); font-size: var(--fs-2xs); padding: 0 7px; cursor: pointer;
    white-space: nowrap; border-radius: 0; height: 26px;
  }
  .speed-btn:last-child { border-right: none; }
  .speed-btn:hover:not(:disabled) { background: var(--bg-hover); color: var(--c-text); }
  .speed-btn.active { background: rgba(var(--c-accent-rgb), 0.16); color: var(--c-accent); box-shadow: inset 0 -2px 0 var(--c-accent); }
  .speed-btn:disabled { cursor: not-allowed; }

  .view-toggle { display: flex; border: 1px solid var(--c-border); border-radius: var(--radius-sm); overflow: hidden; background: var(--c-field); }
  .view-btn {
    background: transparent; border: none; border-right: 1px solid var(--c-border);
    color: var(--c-text-dim); font-size: var(--fs-xs); font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase;
    padding: 0 11px; cursor: pointer; border-radius: 0; height: 26px;
  }
  .view-btn:last-child { border-right: none; }
  .view-btn:hover { background: var(--bg-hover); color: var(--c-text); }
  .view-btn.active { background: rgba(var(--c-accent-rgb), 0.16); color: var(--c-accent); box-shadow: inset 0 -2px 0 var(--c-accent); }

  .utc-clock { display: flex; align-items: baseline; gap: 7px; padding: 0 var(--space-2); user-select: none; white-space: nowrap; }
  .clock-date { font-size: var(--fs-xs); color: var(--c-muted); }
  .clock-time { font-size: var(--fs-md); font-weight: 500; color: var(--c-text); letter-spacing: 0.02em; }
  .clock-z { color: var(--c-accent); margin-left: 2px; font-size: var(--fs-xs); }

  .connection-indicator { display: flex; align-items: center; gap: 7px; padding-left: var(--space-1); }
  .status-text { white-space: nowrap; color: var(--c-text-dim); }

  /* ---- Body ---- */
  .main-content { display: flex; flex: 1; overflow: hidden; }
  .sidebar {
    flex-shrink: 0; overflow: hidden;
    background: var(--c-panel);
    transition: width 300ms var(--ease-in-out), opacity 250ms ease;
  }
  .sidebar.collapsed { width: 0 !important; opacity: 0; pointer-events: none; }
  .sidebar-left { border-right: 1px solid var(--c-border); }
  .sidebar-right { border-left: 1px solid var(--c-border); }
  .viewer-and-timeline { display: flex; flex-direction: column; flex: 1; overflow: hidden; min-width: 0; background: var(--c-surface-0); }
  .viewer-area { flex: 1; overflow: hidden; min-width: 0; }
  /* the cross-section dock sits under the PPI, which keeps whatever height is left */
  .viewer-area.with-dock { display: flex; flex-direction: column; }
  .viewer-area.with-dock > :global(.radar-viewer) { flex: 1 1 0; height: auto; min-height: 0; }

  /* ---- Status bar ---- */
  .statusbar {
    display: flex; align-items: center; justify-content: space-between;
    height: var(--statusbar-height); padding: 0 var(--space-3);
    background: var(--c-panel-solid);
    border-top: 1px solid var(--c-border-strong); font-size: var(--fs-xs); color: var(--c-muted);
    flex-shrink: 0; gap: var(--space-4); position: relative; overflow: hidden;
  }
  .statusbar-progress { position: absolute; top: 0; left: 0; right: 0; height: 2px; background: rgba(var(--c-accent-rgb), 0.1); z-index: 1; }
  .statusbar-progress-fill { height: 100%; background: linear-gradient(90deg, var(--c-accent), var(--c-accent-strong)); transition: width var(--dur-base) var(--ease-in-out); box-shadow: 0 0 8px rgba(var(--c-accent-rgb), 0.6); }
  .progress-text { color: var(--c-accent); font-family: var(--font-mono); font-size: var(--fs-2xs); }
  .statusbar-left, .statusbar-center, .statusbar-right { display: flex; align-items: center; gap: 6px; white-space: nowrap; }
  .statusbar-left { min-width: 140px; }
  .statusbar-center { flex: 1; justify-content: center; min-width: 0; overflow: hidden; }
  .statusbar-right { min-width: 100px; justify-content: flex-end; }
  .statusbar-divider { color: var(--c-border-strong); margin: 0 3px; }
  .sb-key { font-size: 9.5px; }
  .sb-val { font-size: var(--fs-xs); color: var(--c-text-dim); margin-right: 2px; }
  .sb-val.ok { color: var(--c-nominal); }
  .sb-dim { color: var(--c-muted); }
  .file-path { font-size: var(--fs-xs); color: var(--c-text); max-width: 400px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  /* Narrow windows: drop the secondary toolbar labels first, then let the sidebars shrink so the
     viewer keeps a usable width (sidebar px widths are a preference, not a minimum). */
  @media (max-width: 1700px) {
    .clock-date, .title-desktop { display: none; }
  }
  @media (max-width: 1500px) {
    .cmd-trigger-label { display: none; }
    .cmd-trigger { min-width: 0; padding: 4px 8px; }
    .toolbar-left { min-width: 0; }
    .toolbar-right { min-width: 0; }
  }
  @media (max-width: 1250px) {
    .title-word, .title-desktop, .cmd-trigger-label, .cmd-trigger :global(kbd), .status-text, .utc-clock .hud-label { display: none; }
    .toolbar :global(.ws-name) { display: none; }
    .toolbar-center { flex: 1 1 auto; min-width: 0; justify-content: center; overflow: hidden; }
    .cmd-trigger { min-width: 0; padding: 4px 8px; }
    .toolbar { padding: 0 var(--space-2); }
    .toolbar-left { min-width: 0; }
    .toolbar-right { min-width: 0; }
    .sidebar { max-width: 28vw; }
    .hint-part { display: none; }
  }
  @media (max-width: 1180px) {
    .speed-group { display: none; }
  }
  @media (max-width: 1000px) {
    .toolbar-separator { display: none; }
    .toolbar-btn { padding-left: var(--space-2); padding-right: var(--space-2); }
    .sidebar { max-width: 25vw; }
    .statusbar-right { display: none; }
  }

  /* Resize handles */
  .resize-handle {
    width: 4px; cursor: col-resize; flex-shrink: 0; position: relative; z-index: 5;
    background: transparent; transition: background var(--dur-fast);
  }
  .resize-handle:hover, .resize-handle:active {
    background: var(--c-accent);
    box-shadow: 0 0 8px rgba(var(--c-accent-rgb), 0.4);
  }
  .resize-handle::after {
    content: ''; position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
    width: 2px; height: 24px; background: var(--c-muted); opacity: 0;
    transition: opacity var(--dur-fast);
  }
  .resize-handle:hover::after { opacity: 0.5; }
  .resize-handle:focus-visible {
    outline: none; background: var(--c-accent);
    box-shadow: 0 0 8px rgba(var(--c-accent-rgb), 0.4);
  }
  .resize-handle:focus-visible::after { opacity: 0.5; }
</style>
