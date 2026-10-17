/**
 * Choosing which variable to show first when a file opens.
 *
 * File variable lists are alphabetical, so `vars[0]` is often something obscure
 * (new NEXRAD data starts with CCORH, a clutter-correction field) instead of reflectivity.
 */

const PREFERRED = ['DBZH', 'DBZ', 'DBZHC', 'DBZHCC', 'REFL', 'reflectivity', 'corrected_reflectivity'];

/** Prefer a reflectivity-like variable, then fall back to the first one. */
export function pickDefaultVariable(vars: string[]): string | null {
  if (vars.length === 0) return null;
  for (const name of PREFERRED) {
    if (vars.includes(name)) return name;
  }
  const refl = vars.find((v) => /dbz|refl/i.test(v));
  return refl ?? vars[0];
}

/** Index of the first sweep that carries `variable` (sweeps without a variable list count as a match). */
export function firstSweepWith(
  sweeps: { index: number; variables?: string[] }[],
  variable: string | null,
): number {
  if (sweeps.length === 0) return 0;
  if (!variable) return sweeps[0].index;
  const hit = sweeps.find((s) => !s.variables || s.variables.length === 0 || s.variables.includes(variable));
  return (hit ?? sweeps[0]).index;
}
