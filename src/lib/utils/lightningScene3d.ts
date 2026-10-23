/**
 * Observed lightning in the 3D scene: drawn into the 2D overlay canvas above the GL canvas, with the
 * same camera projection as the range rings and radar markers (`project`). LMA sources are at their
 * measured longitude, latitude and height (km above the radar ground, times the vertical
 * exaggeration like the echoes); GLM groups have no height and sit on the ground. The points of one
 * flash are joined in time order; brightness is strength times the fade with age, computed in
 * lightningLayer.ts. Nothing here invents a position: a flash with no point in the window is not drawn.
 */
import { project, type Cam } from './scene3d';
import type { ScenePoint } from './lightningLayer';
import { drawLightningBatched } from './lightningDraw';

/** At most this many points are drawn in one frame (a dense LMA window); the panel says when more exist. */
export const MAX_DRAWN = 40000;

/** The words that say what the dots are, bottom left of the scene (the 3D view has no HTML badge). */
export function drawLightningLabel(ctx: CanvasRenderingContext2D, text: string, cssH: number): void {
  ctx.save();
  ctx.font = '600 11px system-ui, -apple-system, "Segoe UI", sans-serif';
  ctx.textAlign = 'left';
  ctx.textBaseline = 'bottom';
  ctx.lineWidth = 3;
  ctx.lineJoin = 'round';
  ctx.strokeStyle = 'rgba(6,8,16,0.85)';
  ctx.strokeText(text, 68, cssH - 12);
  ctx.fillStyle = 'rgba(255,240,170,0.96)';
  ctx.fillText(text, 68, cssH - 12);
  ctx.restore();
}

export function drawLightning(ctx: CanvasRenderingContext2D, cam: Cam, pts: ScenePoint[], vexag: number, width: number, height: number): number {
  const n = Math.min(pts.length, MAX_DRAWN);
  if (!n) return 0;
  const xs = new Float32Array(n);
  const ys = new Float32Array(n);
  const bs = new Float32Array(n);
  const fl = new Int32Array(n);
  for (let i = 0; i < n; i++) {
    const p = pts[i];
    const s = project(cam, [p.x, p.y, p.z * vexag]);
    xs[i] = s ? s[0] : NaN;
    ys[i] = s ? s[1] : NaN;
    bs[i] = p.b;
    fl[i] = p.flash;
  }
  drawLightningBatched(ctx, n, xs, ys, bs, fl, width, height);
  return n;
}
