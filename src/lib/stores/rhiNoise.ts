import { writable } from 'svelte/store';
import { DEFAULT_NOISE, normalizeNoise, type RhiNoiseSettings } from '../utils/rhiClean';

/** Noise handling of the RHI view (quality masks, despeckle, smoothing, "Raw data"): remembered. */
const KEY = 'xradar.rhi.noise.v1';

function load(): RhiNoiseSettings {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? normalizeNoise(JSON.parse(raw)) : normalizeNoise(DEFAULT_NOISE);
  } catch {
    return normalizeNoise(DEFAULT_NOISE);
  }
}

export const rhiNoise = writable<RhiNoiseSettings>(load());

rhiNoise.subscribe((s) => {
  try {
    localStorage.setItem(KEY, JSON.stringify(s));
  } catch {
    /* storage unavailable: ignore */
  }
});

/** Merge a (possibly partial, nested) change into the settings. */
export function setNoise(patch: Partial<RhiNoiseSettings>): void {
  rhiNoise.update((s) => normalizeNoise({ ...s, ...patch }));
}

export function resetNoise(): void {
  rhiNoise.set(normalizeNoise(DEFAULT_NOISE));
}
