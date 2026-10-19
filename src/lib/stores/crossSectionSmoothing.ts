import { writable } from 'svelte/store';
import { NO_SMOOTHING, normalizeSmoothing, type Smoothing } from '../utils/crossSectionRaster';

/** Smoothing of the cross-section picture: shared by the small panel, the dock and the large view. */
const KEY = 'xradar.cs.smoothing.v1';

function load(): Smoothing {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? normalizeSmoothing(JSON.parse(raw)) : { ...NO_SMOOTHING };
  } catch {
    return { ...NO_SMOOTHING };
  }
}

export const smoothingStore = writable<Smoothing>(load());

smoothingStore.subscribe((s) => {
  try {
    localStorage.setItem(KEY, JSON.stringify(s));
  } catch {
    /* storage unavailable: ignore */
  }
});

export function setSmoothing(patch: Partial<Smoothing>): void {
  smoothingStore.update((s) => normalizeSmoothing({ ...s, ...patch }));
}
