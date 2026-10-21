/**
 * Small pure helpers for the labels and the sound listener of the storm clip (unit tests:
 * clipLabels.test.mjs). No drawing, no DOM.
 */

/**
 * The line that says how the night picture is lit. It names modelled lightning only when modelled
 * flashes are drawn; without drawn flashes it must not claim any lightning light.
 */
export function nightLightingText(lightning: 'observed' | 'modelled' | 'off' | 'file'): string {
  if (lightning === 'file') return 'Night version: no sun, a faint ambient light; lit by lightning drawn at the flash places of the file you gave (stylised)';
  if (lightning === 'modelled') return 'Night version: no sun, a faint ambient light; the storm is lit by modelled lightning (stylised, not a photograph)';
  if (lightning === 'observed') return 'Night version: no sun, a faint ambient light; lit by lightning drawn at observed flash places (stylised, not a photograph)';
  return 'Night version: no sun, a faint ambient light; no lightning is drawn (stylised, not a photograph)';
}

/**
 * Where the sound label rows go: baseline offsets from the bottom edge (units of `u` pixels), top
 * row first, above the attribution line and above the lighting / scale rows already drawn
 * (`hasLa`, `hasLb`), and the height the progress bar is lifted by so that its panel stays clear.
 */
export function soundRowLayout(rows: number, hasLa: boolean, hasLb: boolean): { fromBottom: number[]; lift: number } {
  const first = hasLa ? 76 : hasLb ? 56 : 36;
  const fromBottom: number[] = [];
  for (let i = 0; i < rows; i++) fromBottom.push(first + 20 * (rows - 1 - i));
  // the progress bar panel ends 24 + lift above the bottom; the top row's text reaches about 15 above its baseline
  const lift = rows ? Math.max(50, fromBottom[0] - 5) : 50;
  return { fromBottom, lift };
}

/**
 * Heading (degrees clockwise from north) of a virtual listener standing at (x, y) km and facing the
 * storm centre (cx, cy): the panning of the thunder is relative to it.
 */
export function facingStorm(x: number, y: number, cx: number, cy: number): number {
  const dx = cx - x, dy = cy - y;
  if (Math.hypot(dx, dy) < 1e-9) return 0;
  return ((Math.atan2(dx, dy) * 180) / Math.PI + 360) % 360;
}
