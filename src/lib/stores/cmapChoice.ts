/**
 * The colormap the user picked per variable, shared by the 2D view and both 3D views (polar and
 * Cartesian): picking one in either shows up in the other, and survives restarts (localStorage,
 * like the other per-variable display settings).
 */

import { writable, get } from 'svelte/store';
import { COLORMAP_DATA } from '../utils/colormaps';
import { getDefaultCmap } from '../utils/ppiRenderer';
import { styleFor } from '../utils/variableStyles';
import { parseOverrides, resolveCmap, withChoice, type CmapOverrides } from '../utils/cmapChoice';

const KEY = 'xradar.cmap.choice.v1';

function load(): CmapOverrides {
  try {
    return parseOverrides(localStorage.getItem(KEY));
  } catch {
    return {};
  }
}

export const cmapOverrides = writable<CmapOverrides>(load());
cmapOverrides.subscribe((o) => {
  try {
    localStorage.setItem(KEY, JSON.stringify(o));
  } catch {
    /* storage unavailable: the choice lasts for this session */
  }
});

/** Categorical variables keep their class colours. */
export function isCategoricalVariable(variable: string): boolean {
  return !!styleFor(variable)?.categories;
}

/** The colormap `variable` should be drawn with now (user's choice, else the variable's default). */
export function effectiveCmap(variable: string): string {
  return resolveCmap(variable, get(cmapOverrides), getDefaultCmap, (n) => n in COLORMAP_DATA, isCategoricalVariable(variable));
}

/** Remember the user's colormap for `variable` (ignored for categorical variables). */
export function chooseCmap(variable: string, name: string): void {
  if (!variable || isCategoricalVariable(variable) || !(name in COLORMAP_DATA)) return;
  cmapOverrides.update((o) => withChoice(o, variable, name, getDefaultCmap(variable)));
}
