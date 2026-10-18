/**
 * Variable mapping: which file variable plays which radar role (reflectivity, velocity, ...).
 *
 * The sidecar owns the registry and the matching rules (python/engine/varmap.py). This store only
 * asks it, per active file and selected sweep, for the resolved role -> variable map, lets the
 * user override a role, and tells the Modules panel which required fields the server can resolve
 * (CF names, patterns, user overrides, the paired sweep of a split cut) so it does not block a
 * run the sidecar could do.
 */
import { writable, derived, get } from 'svelte/store';
import { wsManager } from '../utils/websocket';
import { activeFileId } from './fileManager';
import { connectionStatus, radarData, selectedSweep } from './radarData';
import { canonicalVariable, type ModuleSpec } from './modules';

export interface RoleInfo {
  role: string;
  /** Canonical legacy name modules use for the role (DBZH, VRADH, ...). */
  legacy: string;
  label: string;
  /** The user's override for this role, if any. */
  override: string | null;
  /** Resolved variable, or null when nothing matches. */
  variable: string | null;
  /** How it matched: override, cf, alias or pattern. */
  kind: string | null;
  confidence: number;
  /** Sweep the variable lives on when it is not the selected one (paired split cut). */
  sweep: number | null;
  corrected: boolean;
}

export interface VariableMappingState {
  fileId: string | null;
  sweep: number;
  roles: RoleInfo[];
  /** All 2-D variables of the file (the override choices). */
  variables: string[];
  overrides: Record<string, string>;
  loading: boolean;
  error: string | null;
}

const EMPTY: VariableMappingState = {
  fileId: null,
  sweep: 0,
  roles: [],
  variables: [],
  overrides: {},
  loading: false,
  error: null,
};

export const variableMapping = writable<VariableMappingState>({ ...EMPTY });

let wanted = '';
let timer: ReturnType<typeof setTimeout> | null = null;

/** Ask the sidecar for the mapping of the active file at `sweep` (defaults to the selected one). */
export function requestVariableMapping(sweep: number = get(selectedSweep)): void {
  if (!wsManager.isConnected) return;
  variableMapping.update((s) => ({ ...s, loading: true, error: null }));
  void wsManager.send({ type: 'get_variable_mapping', sweep });
}

/** Override `role` with `variable` (null or '' = automatic) for the active file. */
export function setVariableOverride(role: string, variable: string | null): void {
  if (!wsManager.isConnected) return;
  variableMapping.update((s) => ({ ...s, loading: true, error: null }));
  void wsManager.send({
    type: 'set_variable_mapping',
    sweep: get(selectedSweep),
    mapping: { [role]: variable ?? '' },
  });
}

function handleMapping(msg: any): void {
  if (msg.file_id && get(activeFileId) && msg.file_id !== get(activeFileId)) return; // stale reply
  variableMapping.set({
    fileId: msg.file_id ?? null,
    sweep: typeof msg.sweep === 'number' ? msg.sweep : 0,
    roles: Array.isArray(msg.roles) ? msg.roles : [],
    variables: Array.isArray(msg.variables) ? msg.variables.map(String) : [],
    overrides: msg.overrides && typeof msg.overrides === 'object' ? msg.overrides : {},
    loading: false,
    error: null,
  });
}

function handleError(msg: any): void {
  if (!get(variableMapping).loading) return;
  variableMapping.update((s) => ({
    ...s,
    loading: false,
    error: typeof msg?.message === 'string' ? msg.message : 'Variable mapping failed',
  }));
}

let started = false;

/** Subscribe once: refresh whenever the active file, selected sweep or variable list changes. */
export function initVariableMapping(): void {
  if (started) return;
  started = true;
  wsManager.onMessage('variable_mapping', handleMapping);
  wsManager.onMessage('error', handleError);
  const refresh = () => {
    const connected = get(connectionStatus) === 'connected';
    const id = get(activeFileId);
    const data = get(radarData);
    if (!connected || !id || data.variables.length === 0) {
      wanted = '';
      return;
    }
    const key = `${id}:${get(selectedSweep)}:${data.variables.length}`;
    if (key === wanted) return;
    wanted = key;
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => requestVariableMapping(), 120);
  };
  for (const store of [activeFileId, selectedSweep, radarData, connectionStatus]) {
    store.subscribe(refresh);
  }
}
initVariableMapping();

/** What the sidecar can resolve for the selected sweep, as canonical (upper-case legacy) names. */
export interface RoleAvailability {
  /** Roles with a variable on the selected sweep itself (override / CF / alias / pattern). */
  own: Set<string>;
  /** Roles found only on the paired same-elevation sweep: canonical name -> that sweep. */
  paired: Map<string, number>;
}

export const roleAvailability = derived(
  [variableMapping, activeFileId, selectedSweep],
  ([$m, $id, $sweep]): RoleAvailability => {
    const own = new Set<string>();
    const paired = new Map<string, number>();
    if ($m.fileId !== $id || $m.sweep !== $sweep) return { own, paired };
    for (const r of $m.roles) {
      if (!r.variable) continue;
      const key = canonicalVariable(r.legacy);
      if (r.sweep === null || r.sweep === $m.sweep) own.add(key);
      else paired.set(key, r.sweep);
    }
    return { own, paired };
  },
);

/**
 * Required variables of a module that are neither on the sweep by name (`available`) nor
 * resolvable by the sidecar: its own mapping, or the paired sweep for modules that accept it.
 */
export function missingRequirementsMapped(
  spec: ModuleSpec,
  available: Set<string>,
  ra: RoleAvailability,
): string[] {
  const acceptsPaired = new Set((spec.paired ?? []).map(canonicalVariable));
  return (spec.requires ?? []).filter((r) => {
    const c = canonicalVariable(r);
    if (available.has(c) || ra.own.has(c)) return false;
    return !(acceptsPaired.has(c) && ra.paired.has(c));
  });
}

/** Hint for a blocked module: "VRADH is on sweep 1 (same elevation)", or '' if there is none. */
export function pairedHint(missing: string[], ra: RoleAvailability): string {
  const parts = missing
    .map((r) => [r, ra.paired.get(canonicalVariable(r))] as const)
    .filter(([, sw]) => sw !== undefined)
    .map(([r, sw]) => `${r} is on sweep ${sw} (same elevation)`);
  return parts.length ? ` - ${parts.join('; ')}` : '';
}
