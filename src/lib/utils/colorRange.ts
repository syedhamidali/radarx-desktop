import { formatTick } from './crossSectionGeometry.ts';

/**
 * Colour-range (vmin / vmax) rules shared by the colour bar and the renderers.
 *
 * Every colour scale divides by (vmax - vmin) on the GPU: an empty, inverted or non-finite range
 * gives NaN there and the whole sweep disappears (or flickers) without any message. The user can
 * type a limit, and a file can report a constant or all-NaN field, so the rules live in one place.
 */

/** True for a usable colour range: both limits finite and vmin strictly below vmax. */
export function isValidRange(vmin: number, vmax: number): boolean {
  return Number.isFinite(vmin) && Number.isFinite(vmax) && vmin < vmax;
}

/**
 * A range the shaders can divide by. A valid range is returned as it is (same numbers, so pixels
 * do not change); a degenerate one is widened to a unit span around its finite limit, or to 0..1
 * when neither limit is finite, so the sweep is still drawn.
 */
export function safeRange(vmin: number, vmax: number): [number, number] {
  if (isValidRange(vmin, vmax)) return [vmin, vmax];
  const lo = Number.isFinite(vmin);
  const hi = Number.isFinite(vmax);
  if (lo && hi) {
    // equal (or inverted): centre a unit span on the value(s)
    const mid = (vmin + vmax) / 2;
    return [mid - 0.5, mid + 0.5];
  }
  if (lo) return [vmin, vmin + 1];
  if (hi) return [vmax - 1, vmax];
  return [0, 1];
}

/**
 * Apply a typed limit. `which` is the limit being edited, `other` the one that stays. Returns the new
 * [vmin, vmax], or null when the text is not a finite number or would make the range empty or
 * inverted (the caller then keeps what it had). `Infinity`, `1e999` and `NaN` parse as numbers in
 * JavaScript but must never become a limit.
 */
export function applyTypedLimit(
  text: string,
  which: 'vmin' | 'vmax',
  vmin: number,
  vmax: number,
): [number, number] | null {
  const t = text.trim();
  if (t === '') return null;
  const v = parseFloat(t.replace(',', '.'));
  if (!Number.isFinite(v)) return null;
  const next: [number, number] = which === 'vmin' ? [v, vmax] : [vmin, v];
  return isValidRange(next[0], next[1]) ? next : null;
}

/**
 * Labels of the colour bar: `n` evenly spaced ticks from vmin to vmax. Decimals follow the tick
 * spacing (0..1 reads 0, 0.25, 0.5, 0.75, 1; 0..95 reads 0, 23.75, 47.5, ...), so a label is never
 * rounded away from the value its tick stands for. An empty or non-finite range has no ticks.
 */
export function colorbarTicks(min: number, max: number, n = 5): { pos: number; label: string }[] {
  if (!isValidRange(min, max)) return [];
  const range = max - min;
  const out: { pos: number; label: string }[] = [];
  for (let i = 0; i < n; i++) {
    const frac = i / (n - 1);
    out.push({ pos: frac * 100, label: String(Number(formatTick(min + frac * range, range / (n - 1)))) });
  }
  return out;
}
