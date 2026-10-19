import { get } from 'svelte/store';
import { settings } from '../stores/settings';

// ─── Types ───────────────────────────────────────────────────────────────────

export interface ShortcutDef {
  id: string;
  description: string;
  defaultKey: string; // e.g. "mod+o" where mod = Cmd on Mac, Ctrl elsewhere
  category: 'file' | 'view' | 'playback' | 'app';
}

// ─── Registry ────────────────────────────────────────────────────────────────

export const SHORTCUT_REGISTRY: ShortcutDef[] = [
  { id: 'open-file', description: 'Open file', defaultKey: 'mod+o', category: 'file' },
  { id: 'command-palette', description: 'Command palette', defaultKey: 'mod+k', category: 'app' },
  { id: 'play-pause', description: 'Play / Pause animation', defaultKey: 'space', category: 'playback' },
  { id: 'toggle-left-sidebar', description: 'Toggle left sidebar', defaultKey: 'mod+b', category: 'view' },
  { id: 'toggle-right-sidebar', description: 'Toggle right sidebar', defaultKey: 'mod+e', category: 'view' },
  { id: 'settings', description: 'Open settings', defaultKey: 'mod+,', category: 'app' },
  { id: 'toggle-map', description: 'Hide / show the map (2D and 3D)', defaultKey: 'mod+shift+m', category: 'view' },
];

// ─── Platform detection ──────────────────────────────────────────────────────

const isMac =
  typeof navigator !== 'undefined' && /Mac|iPhone|iPad|iPod/.test(navigator.platform ?? '');

/** Convert internal key notation to platform-specific display string */
export function formatKeyCombo(combo: string): string {
  return combo
    .split('+')
    .map((part) => {
      const p = part.trim().toLowerCase();
      if (p === 'mod') return isMac ? '\u2318' : 'Ctrl';
      if (p === 'shift') return isMac ? '\u21E7' : 'Shift';
      if (p === 'alt') return isMac ? '\u2325' : 'Alt';
      if (p === 'ctrl') return isMac ? '\u2303' : 'Ctrl';
      if (p === 'space') return 'Space';
      if (p === ',') return ',';
      if (p === '.') return '.';
      return p.toUpperCase();
    })
    .join(isMac ? '' : '+');
}

/** Get the currently bound key for a shortcut id */
export function getBoundKey(id: string): string {
  const s = get(settings);
  const binding = s.shortcuts[id];
  if (binding) return binding.key;
  const def = SHORTCUT_REGISTRY.find((d) => d.id === id);
  return def?.defaultKey ?? '';
}

/** Get the display string for a shortcut id */
export function getShortcutDisplay(id: string): string {
  return formatKeyCombo(getBoundKey(id));
}

// ─── Matching ────────────────────────────────────────────────────────────────

/** Check if a KeyboardEvent matches a key combo string like "mod+o" */
export function matchesCombo(e: KeyboardEvent, combo: string): boolean {
  const parts = combo.toLowerCase().split('+').map((s) => s.trim());

  let needMod = false;
  let needShift = false;
  let needAlt = false;
  let keyPart = '';

  for (const p of parts) {
    if (p === 'mod') needMod = true;
    else if (p === 'shift') needShift = true;
    else if (p === 'alt') needAlt = true;
    else if (p === 'ctrl') needMod = true; // treat ctrl same as mod in combo
    else keyPart = p;
  }

  const mod = isMac ? e.metaKey : e.ctrlKey;
  if (needMod && !mod) return false;
  if (!needMod && mod) return false;
  if (needShift !== e.shiftKey) return false;
  if (needAlt !== e.altKey) return false;

  const eventKey = e.key.toLowerCase();
  if (keyPart === 'space') return eventKey === ' ';
  if (keyPart === ',') return eventKey === ',';
  if (keyPart === '.') return eventKey === '.';
  return eventKey === keyPart;
}

/** Find which shortcut id matches the given keyboard event, if any */
export function matchShortcut(e: KeyboardEvent): string | null {
  const s = get(settings);
  for (const def of SHORTCUT_REGISTRY) {
    const combo = s.shortcuts[def.id]?.key ?? def.defaultKey;
    if (matchesCombo(e, combo)) return def.id;
  }
  return null;
}

// ─── Conflict detection ──────────────────────────────────────────────────────

/** Check if a key combo conflicts with any existing binding (excluding excludeId) */
export function detectConflict(
  combo: string,
  excludeId?: string,
): ShortcutDef | null {
  const s = get(settings);
  const normalized = combo.toLowerCase().trim();
  for (const def of SHORTCUT_REGISTRY) {
    if (def.id === excludeId) continue;
    const bound = (s.shortcuts[def.id]?.key ?? def.defaultKey).toLowerCase().trim();
    if (bound === normalized) return def;
  }
  return null;
}

/** Update a shortcut binding */
export function updateShortcut(id: string, key: string): void {
  settings.update((s) => ({
    ...s,
    shortcuts: {
      ...s.shortcuts,
      [id]: { key },
    },
  }));
}

/** Reset a shortcut to its default */
export function resetShortcut(id: string): void {
  const def = SHORTCUT_REGISTRY.find((d) => d.id === id);
  if (!def) return;
  updateShortcut(id, def.defaultKey);
}

/** Reset all shortcuts to defaults */
export function resetAllShortcuts(): void {
  settings.update((s) => ({
    ...s,
    shortcuts: Object.fromEntries(
      SHORTCUT_REGISTRY.map((d) => [d.id, { key: d.defaultKey }]),
    ),
  }));
}
