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
  ctx.strokeText(text, 12, cssH - 12);
  ctx.fillStyle = 'rgba(255,240,170,0.96)';
  ctx.fillText(text, 12, cssH - 12);
  ctx.restore();
}

export function drawLightning(ctx: CanvasRenderingContext2D, cam: Cam, pts: ScenePoint[], vexag: number): number {
  if (!pts.length) return 0;
  const n = Math.min(pts.length, MAX_DRAWN);
  const byFlash = new Map<number, number[]>();
  const sp: ([number, number, number] | null)[] = new Array(n);
  for (let i = 0; i < n; i++) {
    const p = pts[i];
    sp[i] = project(cam, [p.x, p.y, p.z * vexag]);
    const l = byFlash.get(p.flash);
    if (l) l.push(i);
    else byFlash.set(p.flash, [i]);
  }
  ctx.save();
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  for (const list of byFlash.values()) {
    if (list.length < 2) continue;
    let bMax = 0;
    for (const i of list) bMax = Math.max(bMax, pts[i].b);
    ctx.beginPath();
    let started = false;
    for (const i of list) {
      const s = sp[i];
      if (!s) {
        started = false;
        continue;
      }
      if (!started) {
        ctx.moveTo(s[0], s[1]);
        started = true;
      } else ctx.lineTo(s[0], s[1]);
    }
    ctx.strokeStyle = `rgba(10,14,30,${(0.5 * bMax).toFixed(3)})`;
    ctx.lineWidth = 2.6;
    ctx.stroke();
    ctx.strokeStyle = `rgba(255,236,150,${(0.85 * bMax).toFixed(3)})`;
    ctx.lineWidth = 1.1;
    ctx.stroke();
  }
  for (let i = 0; i < n; i++) {
    const s = sp[i];
    if (!s) continue;
    const b = pts[i].b;
    // a little smaller with distance so near sources read as nearer
    const r = Math.max(1, (1.4 + 2.4 * b) * Math.min(1.6, Math.max(0.5, 60 / Math.max(10, s[2]))));
    ctx.beginPath();
    ctx.arc(s[0], s[1], r, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(255,244,176,${Math.min(1, 0.3 + 0.7 * b).toFixed(3)})`;
    ctx.fill();
    ctx.lineWidth = 0.9;
    ctx.strokeStyle = `rgba(10,14,30,${Math.min(0.85, 0.2 + 0.65 * b).toFixed(3)})`;
    ctx.stroke();
  }
  ctx.restore();
  return n;
}
