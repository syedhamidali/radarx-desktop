/**
 * Multi-file manager store for xradar-desktop.
 *
 * Tracks all open radar files, the active file, and synchronises changes
 * with the legacy radarData / selectedVariable / selectedSweep stores so
 * that existing components (RadarViewer, DataInspector, etc.) continue to
 * work without modification.
 */

import { writable, derived, get } from 'svelte/store';
import { radarData, selectedVariable, selectedSweep, type SweepInfo } from './radarData';
import { clearSweepCache } from '../utils/sweepCache';

// ── Types ──────────────────────────────────────────────────────────────────

export interface FileEntry {
  /** Server-assigned UUID for this file session. */
  id: string;
  /** Absolute path on the local filesystem. */
  path: string;
  /** Short display name (basename). */
  filename: string;
  /** Radar moment variable names. */
  variables: string[];
  /** Sweep list (index + elevation). */
  sweeps: SweepInfo[];
  /** Dimension sizes from the first sweep. */
  dimensions: Record<string, number>;
  /** Global attributes from the root DataTree node. */
  attributes: Record<string, any>;
  /** Currently selected variable for this file. */
  selectedVariable: string | null;
  /** Currently selected sweep index for this file. */
  selectedSweep: number;
}

// ── Stores ─────────────────────────────────────────────────────────────────

/** All currently open files. */
export const openFiles = writable<FileEntry[]>([]);

/** ID of the active (focused) file tab. */
export const activeFileId = writable<string | null>(null);

/** Derived: the active FileEntry, or null. */
export const activeFile = derived(
  [openFiles, activeFileId],
  ([$files, $id]) => $files.find((f) => f.id === $id) ?? null,
);

// ── Actions ────────────────────────────────────────────────────────────────

/**
 * Register a newly-opened file and make it active.
 * Called after the server responds to `open_file` with a `file_id`.
 */
export function addFile(entry: FileEntry, opts: { skipSaveOutgoing?: boolean } = {}): void {
  openFiles.update((files) => {
    // Replace if same id already exists (re-open)
    const idx = files.findIndex((f) => f.id === entry.id);
    if (idx >= 0) {
      const updated = [...files];
      updated[idx] = entry;
      return updated;
    }
    return [...files, entry];
  });
  setActiveFile(entry.id, { force: true, skipSaveOutgoing: opts.skipSaveOutgoing });
}

/**
 * Close a file tab. If it was active, activate an adjacent tab.
 * Returns the id that was removed (or null if not found).
 */
export function removeFile(id: string): string | null {
  const files = get(openFiles);
  const idx = files.findIndex((f) => f.id === id);
  if (idx < 0) return null;

  const newFiles = files.filter((f) => f.id !== id);
  openFiles.set(newFiles);

  // If removing the active tab, switch to an adjacent one
  if (get(activeFileId) === id) {
    if (newFiles.length === 0) {
      activeFileId.set(null);
      _syncToLegacyStores(null);
    } else {
      const nextIdx = Math.min(idx, newFiles.length - 1);
      setActiveFile(newFiles[nextIdx].id);
    }
  }

  return id;
}

/**
 * Switch the active tab and sync the legacy stores.
 */
export function setActiveFile(
  id: string,
  opts: { force?: boolean; skipSaveOutgoing?: boolean } = {},
): void {
  // Re-selecting the already-active tab must not drop the sweep cache and reload.
  if (!opts.force && get(activeFileId) === id) return;

  // Save current variable/sweep selection back to the outgoing file entry
  if (!opts.skipSaveOutgoing) _saveCurrentSelection();

  activeFileId.set(id);
  const files = get(openFiles);
  const entry = files.find((f) => f.id === id) ?? null;
  _syncToLegacyStores(entry);
}

/**
 * Get the currently active FileEntry (snapshot).
 */
export function getActiveFile(): FileEntry | null {
  const id = get(activeFileId);
  if (!id) return null;
  return get(openFiles).find((f) => f.id === id) ?? null;
}

/**
 * Persist the live variable/sweep selection into the active file's entry.
 * Call before overwriting the legacy stores for a different file.
 */
export function saveActiveSelection(): void {
  _saveCurrentSelection();
}

/**
 * Update the stored variable/sweep selection for a file.
 */
export function updateFileSelection(id: string, variable: string | null, sweep: number): void {
  openFiles.update((files) =>
    files.map((f) =>
      f.id === id ? { ...f, selectedVariable: variable, selectedSweep: sweep } : f,
    ),
  );
}

// ── Internal helpers ───────────────────────────────────────────────────────

/** Persist the current selectedVariable/selectedSweep back into the active FileEntry. */
function _saveCurrentSelection(): void {
  const currentId = get(activeFileId);
  if (!currentId) return;
  const variable = get(selectedVariable);
  const sweep = get(selectedSweep);
  updateFileSelection(currentId, variable, sweep);
}

/** Push a FileEntry (or null) into the legacy singleton stores. */
function _syncToLegacyStores(entry: FileEntry | null): void {
  if (entry) {
    clearSweepCache();
    radarData.set({
      variables: entry.variables,
      dimensions: entry.dimensions,
      attributes: entry.attributes,
      sweeps: entry.sweeps,
      filePath: entry.path,
    });
    selectedVariable.set(entry.selectedVariable);
    selectedSweep.set(entry.selectedSweep);
  } else {
    clearSweepCache();
    radarData.set({
      variables: [],
      dimensions: {},
      attributes: {},
      sweeps: [],
      filePath: null,
    });
    selectedVariable.set(null);
    selectedSweep.set(0);
  }
}
