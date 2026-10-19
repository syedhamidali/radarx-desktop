/**
 * Layers: everything the app currently holds that is drawn or tabulated, each with its provenance.
 *
 * The engine attaches a `provenance` object to every layer message (radar moments, derived
 * variables, mosaic, winds, cells, cross-section, 3D volume, sounding, fetched environment data).
 * The WebSocket manager hands every incoming JSON message to `noteLayerMessage`; the rules live in
 * `utils/layerRegistry.ts` (pure, unit tested). This store keeps the state, drops the layers of a
 * file when it is closed and computes each layer's time offset to the active radar scan. The Layers
 * panel (LayersPanel.svelte) only reads it.
 */
import { writable, derived, get } from 'svelte/store';
import { activeFileId, openFiles } from './fileManager';
import {
  STYLISED_LOOK,
  applyMessage,
  rowsOf,
  scanTimeOf,
  withLayer,
  withoutFile,
  withoutLayer,
  type LayerRow,
  type LayerState,
} from '../utils/layerRegistry';

export type { LayerRow };

export const layerMap = writable<LayerState>({});

/** Called by the WebSocket manager for every incoming JSON message. Cheap when there is nothing to note. */
export function noteLayerMessage(type: string, msg: any): void {
  layerMap.update((s) => applyMessage(s, type, msg));
}

export function removeLayer(id: string): void {
  layerMap.update((s) => withoutLayer(s, id));
}

/** Set by the 3D viewer while its realistic (stylised) look is on. */
export function setStylisedLook(on: boolean): void {
  layerMap.update((s) => (on ? withLayer(s, STYLISED_LOOK, STYLISED_LOOK.provenance) : withoutLayer(s, STYLISED_LOOK.id)));
}

/** Valid time of the active file's radar moments. */
export const activeScanTime = derived([layerMap, activeFileId], ([$m, $id]) => scanTimeOf($m, $id));

export const layerList = derived([layerMap, activeScanTime], ([$m, $scan]) => rowsOf($m, $scan));

// A closed file takes its layers with it. Only files that were once open are pruned, so a layer
// announced just before its file is registered is not lost.
let known = new Set<string>();
openFiles.subscribe((files) => {
  const now = new Set(files.map((f) => f.id));
  for (const id of known) if (!now.has(id)) layerMap.update((s) => withoutFile(s, id));
  known = now;
});

/** For tests and "close everything". */
export function resetLayers(): void {
  layerMap.set({});
  known = new Set(get(openFiles).map((f) => f.id));
}
