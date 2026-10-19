/**
 * Canonical (upper-case legacy) names of radar variables, shared by the Modules and variable-mapping
 * stores. Kept in its own tiny module so the always-loaded variable-mapping store does not pull the
 * whole Modules store (and the renderer behind it) into the first-paint bundle.
 */

const ALIAS_GROUPS: string[][] = [
  // Client-side fallback only. The sidecar (python/engine/varmap.py) is the source of truth;
  // stores/variableMapping.ts adds what it resolves (CF names, patterns, overrides, paired sweeps).
  ['DBZH', 'DBZ', 'REFLECTIVITY', 'Z', 'CORRECTED_REFLECTIVITY_HORIZONTAL', 'DBZHCC', 'DBZHC'],
  ['ZDR', 'DIFFERENTIAL_REFLECTIVITY', 'ZDRC'],
  ['PHIDP', 'DIFFERENTIAL_PHASE', 'UPHIDP'],
  ['KDP', 'SPECIFIC_DIFFERENTIAL_PHASE'],
  ['RHOHV', 'CROSS_CORRELATION_RATIO', 'CCORH'],
  ['VRADH', 'VRAD', 'VELOCITY', 'VEL', 'V', 'RADIAL_VELOCITY'],
  ['WRADH', 'WIDTH', 'SPECTRUM_WIDTH'],
];

const ALIAS_MAP: Map<string, string> = new Map(
  ALIAS_GROUPS.flatMap((g) => g.map((n) => [n, g[0]] as [string, string])),
);

export function canonicalVariable(name: string): string {
  const up = name.trim().toUpperCase();
  return ALIAS_MAP.get(up) ?? up;
}
