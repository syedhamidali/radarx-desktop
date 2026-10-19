/**
 * Per-variable colormap choice shared by the 2D and 3D views (pure helpers; the store that
 * persists it is stores/cmapChoice.ts).
 *
 * A choice is stored only when it differs from the variable's default colormap; categorical
 * variables (hydrometeor classes ...) never take one: their class colours are part of the data.
 */

export type CmapOverrides = Record<string, string>;

/** Parse the persisted JSON; anything malformed yields no overrides. */
export function parseOverrides(raw: string | null | undefined): CmapOverrides {
  if (!raw) return {};
  try {
    const obj = JSON.parse(raw);
    if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return {};
    const out: CmapOverrides = {};
    for (const [k, v] of Object.entries(obj)) if (typeof v === 'string' && v) out[k] = v;
    return out;
  } catch {
    return {};
  }
}

/** The colormap to draw `variable` with: the user's choice if it still exists, else the default. */
export function resolveCmap(
  variable: string,
  overrides: CmapOverrides,
  defaultFor: (variable: string) => string,
  exists: (name: string) => boolean,
  categorical = false,
): string {
  const def = defaultFor(variable);
  if (categorical) return def;
  const o = overrides[variable];
  return o && exists(o) ? o : def;
}

/** New overrides with `name` chosen for `variable` (choosing the default removes the entry). */
export function withChoice(overrides: CmapOverrides, variable: string, name: string, defaultName: string): CmapOverrides {
  const next = { ...overrides };
  if (name === defaultName) delete next[variable];
  else next[variable] = name;
  return next;
}
