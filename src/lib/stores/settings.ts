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

function deepMerge<T>(defaults: T, overrides: Partial<T>): T {
  const result: any = { ...defaults };
  for (const key of Object.keys(overrides as any)) {
    const defaultVal = (defaults as any)[key];
    const overrideVal = (overrides as any)[key];
    if (isPlainObject(defaultVal) && isPlainObject(overrideVal)) {
      result[key] = deepMerge(defaultVal, overrideVal);
    } else if (overrideVal !== undefined) {
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
      const parsed = JSON.parse(raw);
      return deepMerge(DEFAULT_SETTINGS, parsed);
    }
  } catch (e) {
    console.warn('[settings] Failed to load from localStorage:', e);
  }
  return { ...DEFAULT_SETTINGS };
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
      set({ ...DEFAULT_SETTINGS });
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
