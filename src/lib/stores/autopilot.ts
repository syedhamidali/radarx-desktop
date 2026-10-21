/**
 * Autopilot ("Analyst"): inspect any set of inputs, plan the analyses that make sense, run the
 * approved ones.
 *
 *   -> auto_inspect {request_id, inputs}            <- auto_inspect_result {inventory}
 *   -> auto_plan {request_id, inventory, budget_gb} <- auto_plan_result {inventory, plan}
 *   -> auto_run {request_id, inventory, plan, step_ids, out_dir}
 *                                                   <- auto_run_result {record, gallery}
 *   -> auto_cancel {request_id}                     <- auto_cancelled
 *   <- auto_error {request_id, kind, message}
 *
 * Nothing runs, is downloaded or written until the user ticks steps and presses Run
 * (python/engine/autopilot, docs/SCIENCE.md). The panel (AutopilotPanel, loaded on demand)
 * is opened by dropping many files, or from the command palette.
 */

import { writable, derived, get } from 'svelte/store';
import { wsManager } from '../utils/websocket';

/** Dropping at least this many paths (or any folder) opens Autopilot: utils/dropRoute.ts. */
export { AUTOPILOT_DROP_MIN } from '../utils/dropRoute';

export type Phase = 'idle' | 'inspecting' | 'planning' | 'planned' | 'running' | 'done' | 'error';

export interface Reduction { what: string; from: number; to: number; every?: number; reason: string }

export interface Estimate { seconds: number; memory_bytes: number; volumes: number; time_steps: number; kind: string; method: string }

export interface Step {
  id: string;
  order: number;
  kind: string;
  title: string;
  why: string;
  needs: string;
  produces: string;
  provenance: string;
  inputs: string[];
  rxd_command: string;
  estimate: Estimate;
  reductions: Reduction[];
  chunking: string;
  evidence: string[];
  enabled: boolean;
  refused?: string;
  manual?: boolean;
  rule: string;
}

export interface RuleRow { id: string; title: string; rank: number; fired: boolean; evidence: string[]; note?: string }

export interface Plan {
  situation: { kind: string; label: string; evidence: string[] };
  rules: RuleRow[];
  steps: Step[];
  refusals: { what: string; missing: string; rule: string }[];
  notes: string[];
  governor: {
    machine: { ram_bytes: number | null; available_bytes: number | null; cpus: number | null };
    budget_bytes: number;
    total_seconds_estimate: number;
    peak_memory_bytes_estimate: number;
    reductions: Reduction[];
    note: string;
  };
  consent: string;
}

export interface RadarInfo {
  name: string; n_files: number; bytes: number; t0: string | null; t1: string | null; span_s: number;
  cadence_s: number | null; gaps: { after: string; seconds: number }[]; formats: string[]; kinds: string[];
  variables: string[]; dual_pol: boolean; has_velocity: boolean; band: string | null;
  latitude: number | null; longitude: number | null; max_range_km: number | null; n_sweeps: number | null;
}

export interface Inventory {
  n_files: number; n_opened: number; total_bytes: number; radars: Record<string, RadarInfo>;
  pairs: { a: string; b: string; distance_km: number; overlap_space: boolean; time_overlap_s: number | null; both_velocity: boolean; dual_doppler: { good_fraction: number } | null }[];
  checks: Record<string, { counts?: Record<string, number>; findings?: { title: string; severity: string; summary: string }[]; error?: string }>;
  unreadable: { name: string; reason: string }[];
  reductions: Reduction[];
  elapsed_ms: number;
  note: string;
}

export interface GalleryItem { step: string; name: string; path: string; png?: string }

export interface RunRecord {
  steps: { id: string; title: string; status: string; error?: string; files?: string[]; notes?: string[]; elapsed_s?: number }[];
  out_dir: string; summary_path: string; record_path: string;
}

export interface State {
  phase: Phase;
  inputs: string[];
  inventory: Inventory | null;
  plan: Plan | null;
  selected: Record<string, boolean>;
  outDir: string;
  budgetGb: number | null;
  progress: { pct: number; text: string } | null;
  record: RunRecord | null;
  gallery: GalleryItem[];
  error: string | null;
  requestId: string | null;
}

const initial: State = {
  phase: 'idle', inputs: [], inventory: null, plan: null, selected: {}, outDir: '', budgetGb: null,
  progress: null, record: null, gallery: [], error: null, requestId: null,
};

export const state = writable<State>({ ...initial });
export const panelOpen = writable(false);
export const busy = derived(state, ($s) => $s.phase === 'inspecting' || $s.phase === 'planning' || $s.phase === 'running');

let seq = 0;
const rid = () => `auto-${Date.now().toString(36)}-${++seq}`;

function defaultOutDir(inputs: string[]): string {
  const first = inputs[0] ?? '';
  const sep = first.includes('\\') ? '\\' : '/';
  const parent = first.slice(0, first.lastIndexOf(sep)) || first;
  const stamp = new Date().toISOString().replace(/[-:]/g, '').slice(0, 15);
  return `${parent}${sep}autopilot_${stamp}`;
}

/** Open the panel for these paths and start the inspection (the plan follows automatically). */
export function openAutopilot(inputs: string[]): void {
  panelOpen.set(true);
  const list = inputs.filter(Boolean);
  if (list.length === 0) return;
  const cur = get(state);
  if (cur.phase !== 'idle' && cur.phase !== 'error' && cur.phase !== 'done' && cur.phase !== 'planned') return;
  inspect(list);
}

export function inspect(inputs: string[]): void {
  if (!wsManager.isConnected) {
    state.update((s) => ({ ...s, phase: 'error', error: 'The engine is not connected' }));
    return;
  }
  const requestId = rid();
  state.set({ ...initial, phase: 'inspecting', inputs, outDir: defaultOutDir(inputs), requestId, progress: { pct: 0, text: 'Inspecting...' } });
  void wsManager.send({ type: 'auto_inspect', request_id: requestId, inputs });
}

export function planNow(): void {
  const s = get(state);
  if (!s.inventory) return;
  const requestId = rid();
  state.update((x) => ({ ...x, phase: 'planning', requestId, error: null }));
  void wsManager.send({ type: 'auto_plan', request_id: requestId, inventory: s.inventory, budget_gb: s.budgetGb, out_dir: s.outDir });
}

export function toggleStep(id: string, on: boolean): void {
  state.update((s) => ({ ...s, selected: { ...s.selected, [id]: on } }));
}

export function setOutDir(dir: string): void {
  state.update((s) => ({ ...s, outDir: dir }));
}

export function setBudget(gb: number | null): void {
  state.update((s) => ({ ...s, budgetGb: gb }));
}

/** Run the ticked steps: the only thing that ever runs heavy work or writes files. */
export function run(): void {
  const s = get(state);
  if (!s.inventory || !s.plan) return;
  const stepIds = s.plan.steps.filter((st) => s.selected[st.id]).map((st) => st.id);
  if (stepIds.length === 0 || !s.outDir) return;
  const requestId = rid();
  state.update((x) => ({ ...x, phase: 'running', requestId, error: null, record: null, gallery: [], progress: { pct: 0, text: 'Starting...' } }));
  void wsManager.send({ type: 'auto_run', request_id: requestId, inventory: s.inventory, plan: s.plan, step_ids: stepIds, out_dir: s.outDir });
}

export function cancel(): void {
  const s = get(state);
  if (s.requestId) void wsManager.send({ type: 'auto_cancel', request_id: s.requestId });
}

export function reset(): void {
  state.set({ ...initial });
}

export function close(): void {
  panelOpen.set(false);
}

function mine(msg: any): boolean {
  return !!msg?.request_id && msg.request_id === get(state).requestId;
}

let started = false;
export function initAutopilot(): void {
  if (started) return;
  started = true;
  wsManager.onMessage('auto_inspect_result', (msg) => {
    if (!mine(msg)) return;
    state.update((s) => ({ ...s, inventory: msg.inventory, phase: 'planning', progress: null }));
    planNow();
  });
  wsManager.onMessage('auto_plan_result', (msg) => {
    if (!mine(msg)) return;
    const plan = msg.plan as Plan;
    const selected: Record<string, boolean> = {};
    for (const st of plan.steps) selected[st.id] = st.enabled;
    state.update((s) => ({ ...s, inventory: msg.inventory ?? s.inventory, plan, selected, phase: 'planned', progress: null }));
  });
  wsManager.onMessage('auto_run_result', (msg) => {
    if (!mine(msg)) return;
    state.update((s) => ({ ...s, record: msg.record, gallery: msg.gallery ?? [], phase: 'done', progress: null }));
  });
  wsManager.onMessage('auto_cancelled', (msg) => {
    if (!mine(msg)) return;
    state.update((s) => ({ ...s, phase: s.plan ? 'planned' : 'idle', progress: null, error: 'Cancelled' }));
  });
  wsManager.onMessage('auto_error', (msg) => {
    if (!mine(msg)) return;
    state.update((s) => ({ ...s, phase: s.plan && msg.kind === 'auto_run' ? 'planned' : 'error', error: String(msg.message ?? 'Autopilot failed'), progress: null }));
  });
  wsManager.onMessage('progress', (msg) => {
    const s = get(state);
    if (s.phase !== 'inspecting' && s.phase !== 'running') return;
    if (typeof msg?.progress === 'number' || typeof msg?.percent === 'number') {
      const pct = Number(msg.progress ?? msg.percent);
      state.update((x) => ({ ...x, progress: { pct, text: String(msg.message ?? msg.text ?? '') } }));
    }
  });
}
initAutopilot();
