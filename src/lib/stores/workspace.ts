/**
 * Workspace management store for xradar-desktop.
 *
 * Tracks layout state, sidebar widths, open files, panel ordering,
 * and provides save/load/delete plus built-in presets.
 * Persists to localStorage with debounced auto-save.
 */

import { writable, get } from 'svelte/store';

// ── Types ─────────────────────────────────────────────────────────────────

export type SplitLayout = '1x1' | '1x2' | '2x2';

export interface PanelConfig {
  id: string;
  label: string;
  collapsed: boolean;
}

export interface WorkspaceConfig {
  name: string;
  layout: SplitLayout;
  leftSidebarOpen: boolean;
  rightSidebarOpen: boolean;
  leftSidebarWidth: number;
  rightSidebarWidth: number;
  openFiles: string[];
  activeFileIndex: number;
  viewMode: '2d' | '3d' | 'dualpol';
  selectedVariable: string | null;
  selectedSweep: number;
  zoom: number;
  panX: number;
  panY: number;
  rightPanelOrder: string[];
  rightPanelCollapsed: Record<string, boolean>;
  isPreset?: boolean;
}

interface WorkspaceManagerState {
  currentWorkspaceName: string;
  workspaces: Record<string, WorkspaceConfig>;
  autoSave: boolean;
}

// ── Defaults & Presets ────────────────────────────────────────────────────

const DEFAULT_RIGHT_PANELS = ['cloud', 'volume', 'timeseries', 'crosssection', 'qc', 'storm', 'processing', 'export'];

function defaultConfig(name: string): WorkspaceConfig {
  return {
    name,
    layout: '1x1',
    leftSidebarOpen: true,
    rightSidebarOpen: true,
    leftSidebarWidth: 300,
    rightSidebarWidth: 300,
    openFiles: [],
    activeFileIndex: 0,
    viewMode: '2d',
    selectedVariable: null,
    selectedSweep: 0,
    zoom: 1,
    panX: 0,
    panY: 0,
    rightPanelOrder: [...DEFAULT_RIGHT_PANELS],
    rightPanelCollapsed: {},
  };
}

export const PRESET_WORKSPACES: Record<string, WorkspaceConfig> = {
  Default: {
    ...defaultConfig('Default'),
    isPreset: true,
  },
  Research: {
    ...defaultConfig('Research'),
    leftSidebarOpen: true,
    rightSidebarOpen: true,
    leftSidebarWidth: 340,
    rightSidebarWidth: 340,
    isPreset: true,
  },
  Presentation: {
    ...defaultConfig('Presentation'),
    leftSidebarOpen: false,
    rightSidebarOpen: false,
    layout: '1x1',
    isPreset: true,
  },
  Comparison: {
    ...defaultConfig('Comparison'),
    leftSidebarOpen: false,
    rightSidebarOpen: false,
    layout: '1x2',
    isPreset: true,
  },
};

// ── Persistence ───────────────────────────────────────────────────────────

const STORAGE_KEY = 'xradar-workspaces';

function loadState(): WorkspaceManagerState {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as WorkspaceManagerState;
      // Ensure presets are always present
      for (const [key, preset] of Object.entries(PRESET_WORKSPACES)) {
        if (!parsed.workspaces[key]) {
          parsed.workspaces[key] = preset;
        }
      }
      // Inject any newly added panels missing from saved workspaces
      for (const ws of Object.values(parsed.workspaces)) {
        const order = ws.rightPanelOrder ?? [];
        const missing = DEFAULT_RIGHT_PANELS.filter((id) => !order.includes(id));
        if (missing.length) ws.rightPanelOrder = [...missing, ...order];
      }
      return parsed;
    }
  } catch (e) {
    console.warn('[workspace] Failed to load from localStorage:', e);
  }
  return {
    currentWorkspaceName: 'Default',
    workspaces: { ...PRESET_WORKSPACES },
    autoSave: true,
  };
}

function saveState(state: WorkspaceManagerState): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch (e) {
    console.warn('[workspace] Failed to save to localStorage:', e);
  }
}

// ── Store ─────────────────────────────────────────────────────────────────

function createWorkspaceStore() {
  const initial = loadState();
  const { subscribe, set, update } = writable<WorkspaceManagerState>(initial);

  // Debounced auto-save
  let saveTimer: ReturnType<typeof setTimeout> | null = null;

  function debouncedSave() {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      saveTimer = null;
      const state = get({ subscribe });
      if (state.autoSave) {
        saveState(state);
      }
    }, 2000);
  }

  // Flush a pending debounced save when the window closes.
  if (typeof window !== 'undefined') {
    const flush = () => {
      if (!saveTimer) return;
      clearTimeout(saveTimer);
      saveTimer = null;
      const state = get({ subscribe });
      if (state.autoSave) saveState(state);
    };
    window.addEventListener('pagehide', flush);
    window.addEventListener('beforeunload', flush);
  }

  // Persist on every change (debounced)
  let skipFirst = true;
  subscribe((value) => {
    if (skipFirst) {
      skipFirst = false;
      return;
    }
    debouncedSave();
  });

  return {
    subscribe,
    set,
    update,

    /** Get the current workspace config */
    getCurrentConfig(): WorkspaceConfig {
      const state = get({ subscribe });
      return (
        state.workspaces[state.currentWorkspaceName] ??
        PRESET_WORKSPACES.Default
      );
    },

    /** Get the current workspace name */
    getCurrentName(): string {
      return get({ subscribe }).currentWorkspaceName;
    },

    /** Save current workspace state under a given name */
    saveWorkspace(name: string, config: Partial<WorkspaceConfig> = {}): void {
      update((s) => {
        const current = s.workspaces[s.currentWorkspaceName] ?? defaultConfig(name);
        const merged: WorkspaceConfig = {
          ...current,
          ...config,
          name,
          isPreset: false,
        };
        return {
          ...s,
          currentWorkspaceName: name,
          workspaces: { ...s.workspaces, [name]: merged },
        };
      });
      // Force immediate persist on explicit save
      saveState(get({ subscribe }));
    },

    /** Load a workspace by name */
    loadWorkspace(name: string): WorkspaceConfig | null {
      const state = get({ subscribe });
      const ws = state.workspaces[name];
      if (!ws) return null;
      update((s) => ({ ...s, currentWorkspaceName: name }));
      saveState(get({ subscribe }));
      return ws;
    },

    /** Delete a workspace (cannot delete presets) */
    deleteWorkspace(name: string): boolean {
      const state = get({ subscribe });
      if (PRESET_WORKSPACES[name]) return false; // can't delete presets
      if (!state.workspaces[name]) return false;
      update((s) => {
        const { [name]: _, ...rest } = s.workspaces;
        const newName = s.currentWorkspaceName === name ? 'Default' : s.currentWorkspaceName;
        return { ...s, currentWorkspaceName: newName, workspaces: rest };
      });
      saveState(get({ subscribe }));
      return true;
    },

    /** List all workspace names */
    listWorkspaces(): string[] {
      return Object.keys(get({ subscribe }).workspaces);
    },

    /** List preset workspace names */
    listPresets(): string[] {
      return Object.keys(PRESET_WORKSPACES);
    },

    /** List user-saved workspace names */
    listUserWorkspaces(): string[] {
      const state = get({ subscribe });
      return Object.keys(state.workspaces).filter((n) => !PRESET_WORKSPACES[n]);
    },

    /** Get auto-save setting */
    getAutoSave(): boolean {
      return get({ subscribe }).autoSave;
    },

    /** Set auto-save */
    setAutoSave(enabled: boolean): void {
      update((s) => ({ ...s, autoSave: enabled }));
    },

    /** Update the current workspace config (partial) */
    updateCurrentConfig(partial: Partial<WorkspaceConfig>): void {
      update((s) => {
        const current = s.workspaces[s.currentWorkspaceName] ?? defaultConfig(s.currentWorkspaceName);
        const updated = { ...current, ...partial };
        return {
          ...s,
          workspaces: { ...s.workspaces, [s.currentWorkspaceName]: updated },
        };
      });
    },
  };
}

export const workspaceStore = createWorkspaceStore();

/** Reactive current workspace config */
export const currentWorkspace = {
  subscribe: (fn: (value: WorkspaceConfig) => void) => {
    return workspaceStore.subscribe((state) => {
      fn(state.workspaces[state.currentWorkspaceName] ?? PRESET_WORKSPACES.Default);
    });
  },
};

/** Reactive current workspace name */
export const currentWorkspaceName = {
  subscribe: (fn: (value: string) => void) => {
    return workspaceStore.subscribe((state) => {
      fn(state.currentWorkspaceName);
    });
  },
};
