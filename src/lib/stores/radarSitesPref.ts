/**
 * The one persisted switch of the "Radar sites" layer. Kept apart from stores/radarSites.ts on
 * purpose: this file is tiny and is imported by the viewers, while the catalogue, the engine
 * calls and the UI of the layer load lazily the first time the layer is switched on.
 */
import { writable } from 'svelte/store';

const KEY = 'xradar-radar-sites-show';

function load(): boolean {
  try {
    return localStorage.getItem(KEY) === '1';
  } catch {
    return false;
  }
}

/** Show the radar-site markers (2D map and 3D ground). Off by default; persisted. */
export const showRadarSites = writable<boolean>(load());

showRadarSites.subscribe((on) => {
  try {
    localStorage.setItem(KEY, on ? '1' : '0');
  } catch {
    /* private mode / blocked storage: the switch simply is not remembered */
  }
});
