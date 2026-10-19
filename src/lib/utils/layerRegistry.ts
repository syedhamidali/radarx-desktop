/**
 * The layer registry as pure functions on a plain object (no Svelte, no DOM), so the rules can be
 * unit tested with node. `stores/layers.ts` wraps them in a Svelte store.
 *
 * A layer is anything the app draws or tabulates. Its entry keeps the latest provenance the engine
 * sent for it; a reply without provenance never erases what an earlier reply said.
 */
import {
  derivedLayerKeys,
  groupRank,
  layerKeyFromMessage,
  offsetSeconds,
  parseProvenance,
  provenanceOf,
  type LayerGroup,
  type LayerKey,
  type Provenance,
} from './provenance.ts';

export interface LayerEntry {
  id: string;
  group: LayerGroup;
  title: string;
  provenance: Provenance | null;
  fileId?: string;
  updated: number;
}

export interface LayerRow extends LayerEntry {
  /** Seconds from the active radar scan to this layer's valid time (null = unknown). */
  offset: number | null;
}

export type LayerState = Record<string, LayerEntry>;

export function withLayer(state: LayerState, key: LayerKey, rawProvenance: unknown, now = Date.now()): LayerState {
  const old = state[key.id];
  return {
    ...state,
    [key.id]: {
      id: key.id,
      group: key.group,
      title: key.title,
      provenance: parseProvenance(rawProvenance) ?? old?.provenance ?? null,
      fileId: key.fileId,
      updated: now,
    },
  };
}

export function withoutLayer(state: LayerState, id: string): LayerState {
  if (!(id in state)) return state;
  const { [id]: _gone, ...rest } = state;
  return rest;
}

export function withoutFile(state: LayerState, fileId: string): LayerState {
  const rest = Object.fromEntries(Object.entries(state).filter(([, l]) => l.fileId !== fileId));
  return Object.keys(rest).length === Object.keys(state).length ? state : rest;
}

/** Apply one engine message. Messages that carry no layer provenance leave the state untouched. */
export function applyMessage(state: LayerState, type: string, msg: any, now = Date.now()): LayerState {
  if (type === 'derived_variables') {
    const fid = typeof msg?.file_id === 'string' ? msg.file_id : null;
    const keys = derivedLayerKeys(msg);
    let next = state;
    if (fid) {
      // The list is the complete set for the file: layers of dropped or evicted variables go.
      const keep = new Set(keys.map((k) => k.key.id));
      next = Object.fromEntries(
        Object.entries(state).filter(([id, l]) => !(l.fileId === fid && id.startsWith('derived:') && !keep.has(id))),
      );
    }
    for (const { key, raw } of keys) next = withLayer(next, key, raw, now);
    return next;
  }
  const raw = provenanceOf(msg);
  if (!raw) return state;
  const key = layerKeyFromMessage(type, msg, parseProvenance(raw));
  return key ? withLayer(state, key, raw, now) : state;
}

/** Valid time of the active file's radar moments: the reference of every "time offset". */
export function scanTimeOf(state: LayerState, activeFileId: string | null): string | null {
  const own = activeFileId ? state[`file:${activeFileId}`] : undefined;
  return own?.provenance?.valid_time ?? null;
}

export function rowsOf(state: LayerState, scanTime: string | null): LayerRow[] {
  return Object.values(state)
    .map((l) => ({
      ...l,
      offset: offsetSeconds(l.provenance?.valid_time, scanTime),
    }))
    .sort((a, b) => groupRank(a.group) - groupRank(b.group) || a.title.localeCompare(b.title));
}

export const STYLISED_LOOK: LayerKey & { provenance: Provenance } = {
  id: 'look:realistic',
  group: 'Appearance',
  title: '3D realistic look',
  provenance: {
    kind: 'stylised',
    source: 'radarx-desktop realistic 3D renderer',
    title: '3D realistic look',
    method:
      'Clouds, lighting, haze and ground are drawn for appearance. The radar values drive the density and colour, but the picture is not a measurement.',
    uncertainty: 'Do not read values from the shading; use the probe or the data table.',
  },
};
