/** Pure helpers of the data check chip and panel (no Svelte, no stores: unit tested in node). */

export type Level = 'none' | 'ok' | 'notice' | 'warning';

export interface Counts {
  warning: number;
  notice: number;
  info?: number;
}

/** Worst severity that is not just a fact. */
export function chipLevel(counts: Counts | null | undefined): Level {
  if (!counts) return 'none';
  return counts.warning ? 'warning' : counts.notice ? 'notice' : 'ok';
}

/** Text of the status-bar chip. */
export function chipText(counts: Counts | null | undefined, running: boolean): string {
  if (running) return 'Checking data...';
  if (!counts) return '';
  const parts: string[] = [];
  if (counts.warning) parts.push(`${counts.warning} warning${counts.warning === 1 ? '' : 's'}`);
  if (counts.notice) parts.push(`${counts.notice} notice${counts.notice === 1 ? '' : 's'}`);
  return parts.length ? `Data check: ${parts.join(', ')}` : 'Data check: OK';
}

/** An evidence number for display: 3 significant digits, exponent only for tiny / huge values. */
export function formatValue(value: number | string | null | undefined, unit = ''): string {
  if (value === null || value === undefined) return 'n/a';
  let s: string;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return 'n/a';
    const a = Math.abs(value);
    s = a !== 0 && (a >= 1e5 || a < 0.01) ? value.toExponential(2) : Number(value.toPrecision(3)).toString();
  } else {
    s = String(value);
  }
  return unit ? `${s} ${unit}` : s;
}

export interface Where {
  azimuth_deg?: [number, number];
  range_km?: [number, number] | null;
}

/** "sweeps 1, 3, 5 +9 · azimuth 11.5-220.5° · range 33.4-142.9 km" (or "whole file"). */
export function whereText(scope: 'file' | 'sweep', sweeps: number[], where: Where | null): string {
  const parts: string[] = [];
  if (scope === 'sweep' && sweeps.length) {
    const shown = sweeps.slice(0, 5).join(', ');
    parts.push(`sweep${sweeps.length > 1 ? 's' : ''} ${shown}${sweeps.length > 5 ? ` +${sweeps.length - 5}` : ''}`);
  }
  if (where?.azimuth_deg) {
    const [a, b] = where.azimuth_deg;
    parts.push(a === 0 && b === 360 ? 'all azimuths' : `azimuth ${a}–${b}°`);
  }
  if (where?.range_km) parts.push(`range ${where.range_km[0]}–${where.range_km[1]} km`);
  return parts.join(' · ') || 'whole file';
}

/** The file paths remembered as "don't show again", as stored (a JSON array of strings). */
export function parseHidden(raw: string | null, max = 300): string[] {
  try {
    const v = JSON.parse(raw ?? '[]');
    return Array.isArray(v) ? v.filter((x) => typeof x === 'string').slice(-max) : [];
  } catch {
    return [];
  }
}

/** Add a path (once) and keep the newest `max`. */
export function withHidden(list: string[], path: string, max = 300): string[] {
  return (list.includes(path) ? list : [...list, path]).slice(-max);
}
