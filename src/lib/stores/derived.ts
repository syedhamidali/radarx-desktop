/**
 * Derived variables: products a plugin module computed on every sweep of a file and that the
 * sidecar now serves through the normal `get_sweep_data` (so the 2D and 3D viewers can show them
 * like any radar variable).
 *
 *   -> {type:'list_derived', file_id?}            <- {type:'derived_variables', file_id, variables}
 *   -> {type:'clear_derived', file_id?, name?}    <- {type:'derived_variables', file_id, variables}
 *
 * The server's list is the source of truth, kept per file id. For the active file the derived
 * names are merged into each sweep's `variables` list in `radarData` (only on sweeps that carry
 * them), so everything that already filters by the sweeps' variable lists — the 3D "applicable
 * sweeps", the 2D sweep fallback, the prefetcher — works without special cases. The file-level
 * `radarData.variables` is left untouched; the 2D select adds the derived names itself.
 */

import { writable, derived, get } from 'svelte/store';
import { wsManager } from '../utils/websocket';
import { evictCachedVariable } from '../utils/sweepCache';
import { pickDefaultVariable } from '../utils/variables';
import {
  registerVariableStyle,
  styleFor,
  type FieldCategory,
} from '../utils/variableStyles';
import { activeFileId, openFiles } from './fileManager';
import { connectionStatus, radarData, selectedVariable, type SweepInfo } from './radarData';

// ── Types ──────────────────────────────────────────────────────────────────

export interface DerivedVariable {
  /** Server-side name, e.g. 'radarx_hid:hid' (what get_sweep_data is asked for). */
  name: string;
  label: string;
  units: string;
  vmin: number | null;
  vmax: number | null;
  cmap: string | null;
  categories: FieldCategory[] | null;
  /** Module that produced it, and the field name inside that module's result. */
  module: string;
  field: string;
  /** Sweep indices that carry it. */
  sweeps: number[];
}

// ── Stores ─────────────────────────────────────────────────────────────────

/** Derived variables per file id (as last reported by the sidecar). */
export const derivedByFile = writable<Record<string, DerivedVariable[]>>({});

/** Derived variables of the active file. */
export const activeDerived = derived([derivedByFile, activeFileId], ([$by, $id]) =>
  $id ? ($by[$id] ?? []) : [],
);

/** Every derived name ever seen, so they can be stripped from sweep lists before re-merging. */
const knownDerivedNames = new Set<string>();

// ── Normalisation ──────────────────────────────────────────────────────────

function num(v: any): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

/** Validate one server entry; null when it is unusable. */
export function normaliseDerived(raw: any, fallbackModule = ''): DerivedVariable | null {
  if (!raw || typeof raw.name !== 'string' || !raw.name) return null;
  const sweeps = (Array.isArray(raw.sweeps) ? raw.sweeps : [])
    .map((s: any) => Number(s))
    .filter((s: number) => Number.isInteger(s))
    .sort((a: number, b: number) => a - b);
  return {
    name: raw.name,
    label: typeof raw.label === 'string' && raw.label ? raw.label : raw.name,
    units: typeof raw.units === 'string' ? raw.units : '',
    vmin: num(raw.vmin),
    vmax: num(raw.vmax),
    cmap: typeof raw.cmap === 'string' && raw.cmap ? raw.cmap : null,
    categories: Array.isArray(raw.categories) && raw.categories.length > 0 ? raw.categories : null,
    module: typeof raw.module === 'string' && raw.module ? raw.module : fallbackModule,
    field: typeof raw.field === 'string' ? raw.field : '',
    sweeps,
  };
}

/** Make the viewers draw `d` with its own colours / range / legend. */
export function registerDerivedStyle(d: DerivedVariable): void {
  registerVariableStyle(d.name, {
    label: d.label,
    units: d.units,
    vmin: d.vmin,
    vmax: d.vmax,
    cmap: d.cmap,
    categories: d.categories,
  });
}

// ── Merge into radarData ───────────────────────────────────────────────────

function sameList(a: string[] | undefined, b: string[] | undefined): boolean {
  if (a === b) return true;
  if (!a || !b || a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

/** Id of the file whose data radarData currently holds (null when unknown). */
function currentFileId(filePath: string | null): string | null {
  const id = get(activeFileId);
  const files = get(openFiles);
  const active = id ? files.find((f) => f.id === id) : undefined;
  if (active && (filePath === null || active.path === filePath)) return active.id;
  return null;
}

/**
 * Make radarData's sweep lists agree with the derived variables of the file it shows:
 * strip every derived name, then append those that exist on each sweep. Idempotent.
 */
function reconcileRadarData(): void {
  const rd = get(radarData);
  if (rd.sweeps.length === 0) return;
  const fid = currentFileId(rd.filePath);
  const list = fid ? (get(derivedByFile)[fid] ?? []) : [];

  let changed = false;
  const sweeps: SweepInfo[] = rd.sweeps.map((sw) => {
    const base = (sw.variables ?? []).filter((n) => !knownDerivedNames.has(n));
    const mine = list.filter((d) => d.sweeps.includes(sw.index)).map((d) => d.name);
    let next: string[] | undefined;
    if (mine.length === 0 && list.length === 0) {
      // Nothing derived: restore exactly what the server sent (possibly no list at all).
      next = sw.variables && sw.variables.length > 0 ? base : sw.variables;
    } else {
      // A sweep without its own list means "every file variable"; make that explicit once
      // derived names exist, so a derived variable is not assumed to be on every such sweep.
      const root = sw.variables && sw.variables.length > 0 ? base : rd.variables;
      next = [...root, ...mine];
    }
    if (sameList(next, sw.variables)) return sw;
    changed = true;
    return { ...sw, variables: next };
  });
  if (changed) radarData.update((s) => ({ ...s, sweeps }));
}

// ── Protocol ───────────────────────────────────────────────────────────────

/** Ask the sidecar for the derived variables of a file (the active one by default). */
export function requestDerived(fileId?: string): void {
  const id = fileId ?? get(activeFileId) ?? undefined;
  void wsManager.send(id ? { type: 'list_derived', file_id: id } : { type: 'list_derived' });
}

/** Remove one derived variable (or all of them when `name` is omitted) from the sidecar. */
export function clearDerived(name?: string, fileId?: string): void {
  const msg: Record<string, any> = { type: 'clear_derived' };
  const id = fileId ?? get(activeFileId);
  if (id) msg.file_id = id;
  if (name) msg.name = name;
  void wsManager.send(msg);
}

/** Forget cached sweeps of these names (their data changed or is gone). */
export function dropDerivedCaches(names: Iterable<string>): void {
  for (const n of names) {
    evictCachedVariable(n);
    wsManager.clearUnavailable(n);
  }
}

/**
 * Replace the derived variables of one file.
 * Names that disappeared are evicted from the caches; the selection leaves them if needed.
 */
export function setDerivedForFile(fileId: string, variables: DerivedVariable[]): void {
  const prev = get(derivedByFile)[fileId] ?? [];
  const keep = new Set(variables.map((v) => v.name));
  for (const v of variables) {
    knownDerivedNames.add(v.name);
    registerDerivedStyle(v);
  }
  const gone = prev.filter((p) => !keep.has(p.name)).map((p) => p.name);
  for (const g of gone) knownDerivedNames.add(g);
  dropDerivedCaches(gone);
  derivedByFile.update((by) => ({ ...by, [fileId]: variables }));
  reconcileRadarData();
  if (fileId === get(activeFileId)) leaveIfSelected(gone);
}

/** Add / replace some derived variables of a file without touching the others. */
export function mergeDerivedForFile(fileId: string, added: DerivedVariable[]): void {
  const prev = get(derivedByFile)[fileId] ?? [];
  const names = new Set(added.map((a) => a.name));
  setDerivedForFile(fileId, [...prev.filter((p) => !names.has(p.name)), ...added]);
}

/** If the shown variable is one of `names`, go back to a real variable. */
function leaveIfSelected(names: string[]): void {
  const cur = get(selectedVariable);
  if (!cur || !names.includes(cur)) return;
  const real = pickDefaultVariable(get(radarData).variables);
  if (real) selectedVariable.set(real);
}

/**
 * A derived variable was recomputed under the same name while it is on screen: bounce the
 * selection through a real variable so the viewers reload it (the caches were already dropped).
 */
export function refreshIfSelected(names: string[]): void {
  const cur = get(selectedVariable);
  if (!cur || !names.includes(cur)) return;
  const real = pickDefaultVariable(get(radarData).variables);
  if (!real) return;
  selectedVariable.set(real);
  setTimeout(() => {
    if (get(selectedVariable) === real) selectedVariable.set(cur);
  }, 60);
}

function handleDerivedVariables(msg: any): void {
  const fileId: string | undefined =
    typeof msg.file_id === 'string' && msg.file_id ? msg.file_id : (get(activeFileId) ?? undefined);
  if (!fileId) return;
  const vars: DerivedVariable[] = (Array.isArray(msg.variables) ? msg.variables : [])
    .map((v: any) => normaliseDerived(v))
    .filter((v: DerivedVariable | null): v is DerivedVariable => v !== null);
  setDerivedForFile(fileId, vars);
}

/** The derived variable (active file) for a name, if any. */
export function derivedByName(name: string | null | undefined): DerivedVariable | undefined {
  if (!name) return undefined;
  const id = get(activeFileId);
  return id ? get(derivedByFile)[id]?.find((d) => d.name === name) : undefined;
}

/** True when `name` is a derived variable (registered style or known name). */
export function isDerivedName(name: string | null | undefined): boolean {
  return !!name && (knownDerivedNames.has(name) || !!styleFor(name));
}

// ── Initialisation ─────────────────────────────────────────────────────────

let initialised = false;

/** Register handlers once; safe to call repeatedly. Runs on import (see the bottom of the file). */
export function initDerived(): void {
  if (initialised) return;
  initialised = true;

  wsManager.onMessage('derived_variables', handleDerivedVariables);

  // The server forgets nothing when only the socket drops, so ask again once it is back.
  connectionStatus.subscribe((status) => {
    if (status === 'connected' && get(activeFileId)) requestDerived();
  });

  // A different file brings its own derived variables; closed files lose theirs.
  let lastFile: string | null | undefined;
  activeFileId.subscribe((id) => {
    if (id !== lastFile) {
      lastFile = id;
      if (id && get(connectionStatus) === 'connected') requestDerived(id);
    }
    reconcileRadarData();
  });

  openFiles.subscribe((files) => {
    const open = new Set(files.map((f) => f.id));
    const by = get(derivedByFile);
    const stale = Object.keys(by).filter((k) => !open.has(k));
    if (stale.length > 0) {
      derivedByFile.update((cur) => {
        const next = { ...cur };
        for (const k of stale) delete next[k];
        return next;
      });
    }
    reconcileRadarData();
  });

  // radarData is replaced wholesale when a file is (re)opened or switched: merge again.
  radarData.subscribe(() => reconcileRadarData());
  derivedByFile.subscribe(() => reconcileRadarData());
}

initDerived();
