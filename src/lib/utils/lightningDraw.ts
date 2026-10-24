/**
 * Canvas drawing of lightning points and the lines that join the points of one flash (the 2D map and the 3D
 * overlay share it).
 *
 * Up to {@link DENSE_AT} points each is a round dot with a dark outline and each flash a halo line, which
 * reads on both the dark and the light map. Above that (a GLM window over a big storm has tens of thousands
 * of groups) the drawing is cheaper: squares in eight brightness levels, one pass per level, and one thin
 * line per flash with no halo. Measured in headless Chrome with a software canvas (median of 7 frames at
 * 1200 x 700 px), dots against dense: 10 000 points 42 ms against 5 ms, 30 000 points 123 ms against 14 ms,
 * 100 000 points 405 ms against 48 ms; below 6000 points the dots cost the same as before.
 */

/** Number of brightness levels of the dense drawing. */
export const LEVELS = 8;
/** Above this many points the cheaper dense drawing is used. */
export const DENSE_AT = 6000;

const level = (b: number) => Math.min(LEVELS - 1, Math.max(0, Math.floor(b * LEVELS)));

/**
 * Draw `n` points at pixel positions (x, y) (NaN = not drawn) with brightness b (0..1); the points that share
 * a `flash` number are joined in the order they appear (time order). `width` and `height` are the canvas size
 * in CSS pixels (points outside it are skipped).
 */
export function drawLightningBatched(
  ctx: CanvasRenderingContext2D,
  n: number,
  x: ArrayLike<number>,
  y: ArrayLike<number>,
  b: ArrayLike<number>,
  flash: ArrayLike<number>,
  width: number,
  height: number,
): void {
  if (n <= 0) return;
  const dense = n > DENSE_AT;
  ctx.save();
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';

  // lines: one path per flash
  const byFlash = new Map<number, number[]>();
  for (let i = 0; i < n; i++) {
    if (Number.isNaN(x[i])) continue;
    const l = byFlash.get(flash[i]);
    if (l) l.push(i);
    else byFlash.set(flash[i], [i]);
  }
  for (const list of byFlash.values()) {
    if (list.length < 2) continue;
    let bMax = 0;
    for (const i of list) bMax = Math.max(bMax, b[i]);
    ctx.beginPath();
    ctx.moveTo(x[list[0]], y[list[0]]);
    for (let j = 1; j < list.length; j++) ctx.lineTo(x[list[j]], y[list[j]]);
    if (!dense) {
      ctx.strokeStyle = `rgba(14,18,34,${(0.55 * bMax).toFixed(3)})`;
      ctx.lineWidth = 3;
      ctx.stroke();
    }
    ctx.strokeStyle = `rgba(255,236,150,${(0.85 * bMax).toFixed(3)})`;
    ctx.lineWidth = dense ? 1 : 1.2;
    ctx.stroke();
  }

  if (!dense) {
    for (let i = 0; i < n; i++) {
      const px = x[i];
      const py = y[i];
      if (Number.isNaN(px) || px < -10 || py < -10 || px > width + 10 || py > height + 10) continue;
      const r = 1.6 + 2.6 * b[i];
      ctx.beginPath();
      ctx.arc(px, py, r, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(255,244,176,${Math.min(1, 0.25 + 0.75 * b[i]).toFixed(3)})`;
      ctx.fill();
      ctx.lineWidth = 1;
      ctx.strokeStyle = `rgba(14,18,34,${Math.min(0.9, 0.2 + 0.7 * b[i]).toFixed(3)})`;
      ctx.stroke();
    }
  } else {
    // one pass per brightness level: the colour is set 8 times, not 30 000 times
    const levels = new Int8Array(n);
    for (let i = 0; i < n; i++) levels[i] = level(b[i]);
    for (let k = 0; k < LEVELS; k++) {
      const a = (k + 0.5) / LEVELS;
      ctx.fillStyle = `rgba(255,244,176,${Math.min(1, 0.3 + 0.7 * a).toFixed(3)})`;
      const s = 2 + 2 * a;
      for (let i = 0; i < n; i++) {
        if (levels[i] !== k) continue;
        const px = x[i];
        const py = y[i];
        if (Number.isNaN(px) || px < -10 || py < -10 || px > width + 10 || py > height + 10) continue;
        ctx.fillRect(px - s / 2, py - s / 2, s, s);
      }
    }
  }
  ctx.restore();
}
