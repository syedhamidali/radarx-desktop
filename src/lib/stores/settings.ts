import { writable, derived, get } from 'svelte/store';

// ─── Types ───────────────────────────────────────────────────────────────────

export interface RecentFile {
  path: string;
  name: string;
  openedAt: number; // epoch ms
  size?: number;
}

export interface DisplaySettings {
  defaultCmapPerVariable: Record<string, string>;
  bgColor: string;
  showRangeRings: boolean;
  showCrosshair: boolean;
  rangeRingColor: string;
}

export interface RenderingSettings {
  antialias: boolean;
  interpolation: 'nearest' | 'bilinear';
}

export interface ThemeSettings {
  mode: 'dark' | 'light' | 'auto';
  accentColor: string;
  fontSize: number; // px
}

export interface DataSettings {
  defaultDirectory: string;
  recentFiles: RecentFile[];
  autoLoadLast: boolean;
}

export interface ExportSettings {
  defaultFormat: 'png' | 'svg' | 'pdf';
  dpi: number;
  outputDirectory: string;
}

export interface ShortcutBinding {
  key: string; // e.g. "ctrl+o", "meta+k", "space"
}

export interface AppSettings {
  display: DisplaySettings;
  rendering: RenderingSettings;
  theme: ThemeSettings;
  data: DataSettings;
  export_: ExportSettings;
  shortcuts: Record<string, ShortcutBinding>;
  mapStyle: string;
  colormap: string;
  sidecarPort: number | null;
}

// ─── Defaults ────────────────────────────────────────────────────────────────

export const DEFAULT_SETTINGS: AppSettings = {
  display: {
    defaultCmapPerVariable: {
      DBZH: 'NWSRef',
      VRADH: 'NWSVel',
      ZDR: 'viridis',
      RHOHV: 'plasma',
      PHIDP: 'twilight',
      KDP: 'coolwarm',
    },
    bgColor: '#0f0f1a',
    showRangeRings: true,
    showCrosshair: false,
    rangeRingColor: '#3a3a5a',
  },
  rendering: {
    antialias: true,
    interpolation: 'nearest',
  },
  theme: {
    mode: 'dark',
    accentColor: '#5b6cf7',
    fontSize: 13,
  },
  data: {
    defaultDirectory: '',
    recentFiles: [],
    autoLoadLast: false,
  },
  export_: {
    defaultFormat: 'png',
    dpi: 150,
    outputDirectory: '',
  },
  shortcuts: {
    'open-file': { key: 'mod+o' },
    'command-palette': { key: 'mod+k' },
    'play-pause': { key: 'space' },
    'toggle-left-sidebar': { key: 'mod+b' },
    'toggle-right-sidebar': { key: 'mod+e' },
    'settings': { key: 'mod+,' },
  },
  mapStyle: 'dark',
  colormap: 'viridis',
  sidecarPort: null,
};

// ─── Deep merge utility ──────────────────────────────────────────────────────

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/**
 * Merge saved overrides onto the defaults. A saved value is only accepted when it has the same
 * kind as the default (object / array / string / number / boolean), so hand-edited or corrupted
 * storage (`"theme": null`, `"recentFiles": "x"`) can never put a wrong type in the store.
 * A `null` default (e.g. sidecarPort) accepts null or a number.
 */
function deepMerge<T>(defaults: T, overrides: Partial<T>): T {
  const result: any = { ...defaults };
  if (!isPlainObject(overrides)) return result;
  for (const key of Object.keys(overrides as any)) {
    const defaultVal = (defaults as any)[key];
    const overrideVal = (overrides as any)[key];
    if (overrideVal === undefined) continue;
    if (isPlainObject(defaultVal)) {
      if (isPlainObject(overrideVal)) result[key] = deepMerge(defaultVal, overrideVal);
    } else if (Array.isArray(defaultVal)) {
      if (Array.isArray(overrideVal)) result[key] = overrideVal;
    } else if (defaultVal === null) {
      if (overrideVal === null || typeof overrideVal === 'number') result[key] = overrideVal;
    } else if (typeof overrideVal === typeof defaultVal) {
      if (typeof overrideVal !== 'number' || Number.isFinite(overrideVal)) result[key] = overrideVal;
    } else if (defaultVal === undefined) {
      result[key] = overrideVal;
    }
  }
  return result;
}

// ─── Persistence ─────────────────────────────────────────────────────────────

const STORAGE_KEY = 'xradar-settings';

function loadSettings(): AppSettings {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const merged = deepMerge(structuredClone(DEFAULT_SETTINGS), JSON.parse(raw));
      // Entries inside open-ended collections need their own shape check.
      merged.data.recentFiles = merged.data.recentFiles
        .filter((f: any) => f && typeof f.path === 'string' && f.path)
        .map((f: any) => ({
          path: f.path,
          name: typeof f.name === 'string' && f.name ? f.name : f.path.split('/').pop() ?? f.path,
          openedAt: Number.isFinite(f.openedAt) ? f.openedAt : 0,
          ...(Number.isFinite(f.size) ? { size: f.size } : {}),
        }));
      for (const [id, b] of Object.entries(merged.shortcuts)) {
        if (!b || typeof (b as any).key !== 'string' || !(b as any).key) {
          merged.shortcuts[id] = DEFAULT_SETTINGS.shortcuts[id] ?? { key: '' };
        }
      }
      for (const [v, c] of Object.entries(merged.display.defaultCmapPerVariable)) {
        if (typeof c !== 'string') delete merged.display.defaultCmapPerVariable[v];
      }
      merged.theme.fontSize = Math.min(18, Math.max(11, merged.theme.fontSize));
      merged.export_.dpi = Math.min(600, Math.max(72, Math.round(merged.export_.dpi)));
      if (!['dark', 'light', 'auto'].includes(merged.theme.mode)) merged.theme.mode = 'dark';
      if (!/^#[0-9a-f]{6}$/i.test(merged.theme.accentColor)) merged.theme.accentColor = DEFAULT_SETTINGS.theme.accentColor;
      return merged;
    }
  } catch (e) {
    console.warn('[settings] Failed to load from localStorage:', e);
  }
  return structuredClone(DEFAULT_SETTINGS);
}

function saveSettings(s: AppSettings): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
  } catch (e) {
    console.warn('[settings] Failed to save to localStorage:', e);
  }
}

// ─── Store ───────────────────────────────────────────────────────────────────

function createSettingsStore() {
  const initial = loadSettings();
  const { subscribe, set, update } = writable<AppSettings>(initial);

  // Auto-persist on change, debounced: sliders / pickers can update dozens of
  // times per second and each save is a synchronous JSON.stringify + localStorage write.
  let skipFirst = true;
  let latest: AppSettings = initial;
  let dirty = false;
  let saveTimer: ReturnType<typeof setTimeout> | null = null;
  const flush = () => {
    if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
    if (dirty) { dirty = false; saveSettings(latest); }
  };
  subscribe((value) => {
    if (skipFirst) {
      skipFirst = false;
      return;
    }
    latest = value;
    dirty = true;
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(flush, 250);
  });
  if (typeof window !== 'undefined') {
    // Never lose the last edit when the window closes.
    window.addEventListener('pagehide', flush);
    window.addEventListener('beforeunload', flush);
  }

  return {
    subscribe,
    set,
    update,

    /** Get a nested setting value */
    getSetting<K extends keyof AppSettings>(section: K): AppSettings[K] {
      return get({ subscribe })[section];
    },

    /** Update a specific section */
    updateSetting<K extends keyof AppSettings>(
      section: K,
      value: Partial<AppSettings[K]>,
    ): void {
      update((s) => {
        const current = s[section];
        if (isPlainObject(current) && isPlainObject(value)) {
          return { ...s, [section]: { ...(current as any), ...(value as any) } };
        }
        return { ...s, [section]: value };
      });
    },

    /** Reset a section to defaults */
    resetSection<K extends keyof AppSettings>(section: K): void {
      update((s) => ({ ...s, [section]: DEFAULT_SETTINGS[section] }));
    },

    /** Reset everything */
    resetToDefaults(): void {
      set(structuredClone(DEFAULT_SETTINGS));
    },
  };
}

export const settings = createSettingsStore();

// ─── Convenience derived stores (backward compat) ────────────────────────────

// These provide both subscribe (derived) and set (write-through) for compat
function derivedWritable<T>(key: keyof AppSettings) {
  const d = derived(settings, ($s) => $s[key] as T);
  return {
    subscribe: d.subscribe,
    set(value: T) {
      settings.update((s) => ({ ...s, [key]: value }));
    },
  };
}

export const mapStyle = derivedWritable<string>('mapStyle');
export const colormap = derivedWritable<string>('colormap');
export const sidecarPort = derivedWritable<number | null>('sidecarPort');
export const themeMode = derived(settings, ($s) => $s.theme.mode);
export const accentColor = derived(settings, ($s) => $s.theme.accentColor);
