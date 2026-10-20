/**
 * Which volume the 3D tab shows. Kept apart from `volume3d.ts` (which needs the mosaic and wind
 * stores) because the app shell reads it before the first paint; see `entryGraph.test.mjs`.
 */
import { writable } from 'svelte/store';

export type Source3d = 'radar' | 'mosaic' | 'wind';

export const source3d = writable<Source3d>('radar');
