/**
 * Data check ("smart open"): what is in the file and what to do next, with evidence.
 *
 *   -> data_check {file_id}      <- data_check {file_id, report}
 *
 * The sidecar's report (python/engine/quality.py, docs/DATA_CHECK.md) lists findings with
 * evidence, a suggested QC recipe and a first view. Nothing is applied here: the panel
 * (DataCheckPanel, loaded on demand) runs a recipe only when the user clicks.
 *
 * This file stays small on purpose: it is part of the entry chunk (the status-bar chip).
 */

import { writable, derived, get } from 'svelte/store';
import { wsManager } from '../utils/websocket';
import { activeFileId, openFiles } from './fileManager';
import { connectionStatus, radarData } from './radarData';
import { parseHidden, withHidden } from '../utils/dataCheckLogic';

export type Severity = 'info' | 'notice' | 'warning';

export interface Evidence {
  label: string;
  value: number | string | null;
  unit: string;
  /** RETRIEVED = measured in the data; ESTIMATE = model based (see the finding's method). */
  kind: 'RETRIEVED' | 'ESTIMATE';
  note?: string;
}

export interface Finding {
  id: string;
  title: string;
  severity: Severity;
  summary: string;
  scope: 'file' | 'sweep';
  sweeps: number[];
  where: { azimuth_deg?: [number, number]; range_km?: [number, number] | null; sweeps?: number[] } | null;
  evidence: Evidence[];
  confidence: number;
  method: string;
  per_sweep: Record<string, any>[];
  steps: string[];
  advice: string | null;
}

export interface RecipeItem {
  role: string;
  recipe: { name: string; description: string; target: string; apply_to_all: boolean; steps: any[]; builtin?: boolean };
  reasons: { step_id: string; reason: string; findings: string[] }[];
  sweeps: number[];
}

export interface FirstView {
  role: string;
  variable: string;
  sweep: number;
  reason: string;
  scale?: { vmin: number; vmax: number; unit: string; note: string };
  also: { role: string; variable: string; sweep: number }[];
}

export interface Report {
  version: number;
  file: string | null;
  format: string | null;
  n_sweeps: number;
  band: string | null;
  counts: Record<Severity, number>;
  findings: Finding[];
  recommendation: { recipes: RecipeItem[]; first_view: FirstView | null; advice: { id: string; text: string; severity: Severity }[] };
  not_checked: string[];
  errors: string[];
  elapsed_ms: number;
  note: string;
}

export interface CheckEntry {
  status: 'running' | 'ready' | 'error';
  report: Report | null;
  error: string | null;
}

const HIDE_KEY = 'xradar.datacheck.hidden.v1';
const MAX_HIDDEN = 300;

function loadHidden(): string[] {
  try {
    return parseHidden(localStorage.getItem(HIDE_KEY), MAX_HIDDEN);
  } catch {
    return [];
  }
}

/** Per open file: the check's state (by file id). */
export const checks = writable<Record<string, CheckEntry>>({});
/** Is the findings panel open? */
export const panelOpen = writable(false);
/** File paths the user said "don't show again" for (persisted). */
export const hiddenPaths = writable<string[]>(loadHidden());
const sessionHidden = writable<Record<string, true>>({});

hiddenPaths.subscribe((v) => {
  try {
    localStorage.setItem(HIDE_KEY, JSON.stringify(v.slice(-MAX_HIDDEN)));
  } catch {
    /* storage unavailable: the choice lasts for this session */
  }
});

/** The active file's check, whether its chip may show, and which file/path it is. */
export const current = derived(
  [checks, activeFileId, openFiles, sessionHidden, hiddenPaths],
  ([$c, $id, $files, $s, $h]) => {
    const path = $files.find((f) => f.id === $id)?.path ?? null;
    const entry = $id ? ($c[$id] ?? null) : null;
    const hidden = !!$id && (!!$s[$id] || (path !== null && $h.includes(path)));
    return { id: $id, path, entry, hidden, visible: !!entry && !hidden && entry.status !== 'error' };
  },
);

/** Ask the sidecar for the check of a file (the only thing that ever starts one). */
export function requestCheck(id: string): void {
  if (!wsManager.isConnected) return;
  checks.update((c) => ({ ...c, [id]: { status: 'running', report: c[id]?.report ?? null, error: null } }));
  void wsManager.send({ type: 'data_check', file_id: id });
}

/** Hide the chip until this file is closed. */
export function hideForNow(id: string): void {
  panelOpen.set(false);
  sessionHidden.update((s) => ({ ...s, [id]: true }));
}

/** Never show the chip (or run the automatic check) for this file path again. */
export function neverShowFor(id: string, path: string | null): void {
  hideForNow(id);
  if (path) hiddenPaths.update((h) => withHidden(h, path, MAX_HIDDEN));
}

/** Bring the chip back for the active file and open the panel (the command palette entry). */
export function openDataCheck(): void {
  const id = get(activeFileId);
  if (!id) return;
  const path = get(openFiles).find((f) => f.id === id)?.path ?? null;
  sessionHidden.update((s) => {
    const { [id]: _drop, ...rest } = s;
    return rest;
  });
  if (path) hiddenPaths.update((h) => h.filter((p) => p !== path));
  if (!get(checks)[id]) requestCheck(id);
  panelOpen.set(true);
}

function onReport(msg: any): void {
  const id: string | undefined = msg?.file_id;
  if (!id || !msg.report) return;
  checks.update((c) => ({ ...c, [id]: { status: 'ready', report: msg.report as Report, error: null } }));
}

function onError(msg: any): void {
  if (typeof msg?.message !== 'string' || !msg.message.startsWith('data_check')) return;
  const text = msg.message;
  checks.update((c) => {
    const next = { ...c };
    for (const [id, e] of Object.entries(next)) {
      if (e.status === 'running') next[id] = { ...e, status: 'error', error: text };
    }
    return next;
  });
}

let started = false;
let timer: ReturnType<typeof setTimeout> | null = null;

/** Subscribe once: start the check a moment after a file has opened (never blocks opening). */
export function initDataCheck(): void {
  if (started) return;
  started = true;
  wsManager.onMessage('data_check', onReport);
  wsManager.onMessage('error', onError);
  const maybe = () => {
    const id = get(activeFileId);
    if (!id || get(connectionStatus) !== 'connected' || get(radarData).variables.length === 0) return;
    if (get(checks)[id]) return;
    const path = get(openFiles).find((f) => f.id === id)?.path ?? null;
    if (path && get(hiddenPaths).includes(path)) return;
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      if (get(activeFileId) === id && !get(checks)[id]) requestCheck(id);
    }, 900);
  };
  for (const s of [activeFileId, radarData, connectionStatus]) s.subscribe(maybe);
  // forget the files that were closed
  openFiles.subscribe((files) => {
    const ids = new Set(files.map((f) => f.id));
    const c = get(checks);
    const stale = Object.keys(c).filter((id) => !ids.has(id));
    if (stale.length) {
      checks.update((x) => Object.fromEntries(Object.entries(x).filter(([id]) => ids.has(id))));
      sessionHidden.update((x) => Object.fromEntries(Object.entries(x).filter(([id]) => ids.has(id))));
    }
  });
}
initDataCheck();
