/**
 * Drawing of lightning on the 2-D overlay of a frame, and the light points a flash gives the 3-D
 * scene (cloud, ground and sky brighten, see CartesianRenderer.setFlashLight).
 *
 * The channels are thin, bright, jagged and branching (lightning.ts), drawn as a hot white core with
 * two blurred halos (the bloom) in additive blending. Brightness follows the return strokes of the
 * flash (2 to 4 re-strikes in 0.15 to 0.5 s): consecutive video frames flicker.
 *
 * Limits, stated plainly: the channel is drawn over the picture, so a bolt that would be hidden
 * behind a dense core or a hill is not hidden (the cloud around it does light up from inside, which
 * carries the impression); a bolt is a 3-D polyline of synthetic shape located at the flash position.
 */
import { project } from './scene3d';
import { flashLevels, lightPoints, activeFlashes, type Flash } from './lightning';

export interface FxCam {
  eye: [number, number, number];
  fwd: [number, number, number];
  right: [number, number, number];
  up: [number, number, number];
  tanX: number;
  tanY: number;
  w: number;
  h: number;
}

let scratch: HTMLCanvasElement | null = null;

function scratchCanvas(w: number, h: number): HTMLCanvasElement {
  if (!scratch) scratch = document.createElement('canvas');
  if (scratch.width !== w || scratch.height !== h) {
    scratch.width = w;
    scratch.height = h;
  }
  return scratch;
}

const BASE_WIDTH = [3.4, 2.1, 1.3];

/** Distance (km) from the camera to a flash's channel middle. */
export function flashDistanceKm(f: Flash, eye: [number, number, number], vexag: number): number {
  const m = lightPoints(f)[1];
  return Math.hypot(m[0] - eye[0], m[1] - eye[1], (m[2] * vexag - eye[2]) / Math.max(vexag, 1e-3));
}

/** Draw the flashes active at clip time `t`; returns how many were drawn. */
export function drawFlashes(ctx: CanvasRenderingContext2D, cam: FxCam, flashes: Flash[], t: number, vexag: number, gain = 1, colour = ''): number {
  const tint = /^#[0-9a-f]{6}$/i.test(colour) ? [parseInt(colour.slice(1, 3), 16), parseInt(colour.slice(3, 5), 16), parseInt(colour.slice(5, 7), 16)] : [255, 253, 248];
  const core = `rgb(${Math.round(255 * 0.65 + tint[0] * 0.35)},${Math.round(255 * 0.65 + tint[1] * 0.35)},${Math.round(255 * 0.65 + tint[2] * 0.35)})`;
  const act = activeFlashes(flashes, t);
  let drawn = 0;
  const u = cam.h / 1080;
  for (const f of act) {
    const lv = flashLevels(f, t);
    if (lv.channel < 0.04) continue;
    const dist = flashDistanceKm(f, cam.eye, vexag);
    const far = 0.55 + 0.45 / (1 + (dist / 40) ** 2);
    const sc = scratchCanvas(cam.w, cam.h);
    const sx = sc.getContext('2d')!;
    sx.clearRect(0, 0, sc.width, sc.height);
    sx.lineCap = 'round';
    sx.lineJoin = 'round';
    const s = f.segments;
    const n = s.length / 6;
    let any = false;
    for (let level = 0; level < 3; level++) {
      sx.beginPath();
      let has = false;
      for (let i = 0; i < n; i++) {
        if (f.level[i] !== level) continue;
        const a = project(cam, [s[i * 6], s[i * 6 + 1], s[i * 6 + 2] * vexag]);
        const b = project(cam, [s[i * 6 + 3], s[i * 6 + 4], s[i * 6 + 5] * vexag]);
        if (!a || !b) continue;
        if ((a[0] < -200 && b[0] < -200) || (a[0] > cam.w + 200 && b[0] > cam.w + 200)) continue;
        sx.moveTo(a[0], a[1]);
        sx.lineTo(b[0], b[1]);
        has = true;
      }
      if (!has) continue;
      any = true;
      const dMid = Math.max(1.5, dist);
      const wpx = BASE_WIDTH[level] * u * Math.max(0.55, Math.min(2.2, Math.pow(10 / dMid, 0.45)));
      sx.strokeStyle = core;
      sx.lineWidth = wpx;
      sx.stroke();
    }
    if (!any) continue;
    drawn++;
    const a = Math.min(1, Math.pow(lv.channel, 0.8) * far * gain);
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    // wide halo, soft halo, then the channel itself
    ctx.filter = `blur(${Math.round(26 * u)}px) brightness(1.4)`;
    ctx.globalAlpha = Math.min(1, 0.5 * a);
    ctx.drawImage(sc, 0, 0);
    ctx.filter = `blur(${Math.round(8 * u)}px) brightness(1.2)`;
    ctx.globalAlpha = Math.min(1, 0.8 * a);
    ctx.drawImage(sc, 0, 0);
    ctx.filter = 'none';
    ctx.globalAlpha = Math.min(1, 0.4 + 0.6 * a);
    ctx.drawImage(sc, 0, 0);
    ctx.restore();
  }
  return drawn;
}

export interface FlashLight {
  points: { x: number; y: number; z: number; i: number }[];
  /** Flash light on the sky and the haze. */
  sky: number;
}

/**
 * Light points for the 3-D scene from the active flashes (scene km, z exaggerated by `vexag`): the
 * three points of the strongest flash and the middle of the next one; the sky brightens with the
 * total light of all flashes, weaker for distant ones.
 */
export function flashLight(flashes: Flash[], t: number, eye: [number, number, number], vexag: number): FlashLight {
  const act = activeFlashes(flashes, t)
    .map((f) => ({ f, lv: flashLevels(f, t) }))
    .filter((e) => e.lv.light > 0.02)
    .sort((a, b) => b.lv.light - a.lv.light);
  const points: FlashLight['points'] = [];
  let sky = 0;
  act.forEach((e, k) => {
    const pts = lightPoints(e.f);
    const d = flashDistanceKm(e.f, eye, vexag);
    sky += (e.lv.light * 0.2) / (1 + (d / 14) ** 2);
    const use = k === 0 ? pts : [pts[1]];
    for (const p of use) {
      if (points.length < 4) points.push({ x: p[0], y: p[1], z: p[2] * vexag, i: e.lv.light * (k === 0 ? 0.75 : 0.5) });
    }
  });
  return { points, sky: Math.min(0.7, sky) };
}
