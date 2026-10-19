/**
 * Scene helpers for the 3D radar volume: persisted view preferences, camera presets / fitting,
 * perspective projection and the 2D overlay (height scale, range-ring labels, compass letters,
 * radar marker, north arrow) that is drawn above the WebGL canvas.
 *
 * Coordinates: radar at the origin, X = East, Y = North, Z = up. The scene is vertically
 * exaggerated: scene Z = physical height (km) x exaggeration. Everything in here works in scene
 * units (km), exactly like the shader.
 */

export type Vec3 = [number, number, number];

// ─── Persisted preferences ─────────────────────────────────────────────────

const PREF_PREFIX = 'xradar.3d.';

export function loadPref<T extends string | number | boolean>(key: string, fallback: T, valid?: (v: unknown) => boolean): T {
  try {
    const raw = localStorage.getItem(PREF_PREFIX + key);
    if (raw === null) return fallback;
    const v = typeof fallback === 'number' ? Number(raw) : typeof fallback === 'boolean' ? raw === 'true' : raw;
    if (typeof fallback === 'number' && !Number.isFinite(v as number)) return fallback;
    if (valid && !valid(v)) return fallback;
    return v as T;
  } catch {
    return fallback;
  }
}

export function savePref(key: string, value: string | number | boolean): void {
  try {
    localStorage.setItem(PREF_PREFIX + key, String(value));
  } catch {
    /* storage blocked */
  }
}

// ─── Defaults ──────────────────────────────────────────────────────────────

export const VEXAG_MIN = 1;
export const VEXAG_MAX = 20;
export const VEXAG_DEFAULT = 6;
/** Physical height shown (km): data above is cut off. Storm tops rarely exceed 18-20 km. */
export const Z_TOP_KM = 20;
/** Height the default camera frames (km): the part of the volume that normally holds echoes. */
export const Z_FIT_KM = 15;
/** Default horizontal crop when there is no echo-based estimate. */
export const RANGE_DEFAULT_KM = 200;

export type CameraPreset = 'oblique' | 'top' | 'side';

export interface CameraPose {
  theta: number; // azimuth of the eye around the target (deg, 180 = looking north from the south)
  phi: number; // elevation of the eye above the horizon (deg)
  dist: number; // km from target
  target: Vec3; // scene units
}

/** Distance at which a ground disc of radius `rangeKm` (plus `heightKm` of exaggerated height) fits the view. */
export function fitDistance(rangeKm: number, zScene: number, phiDeg: number, aspect: number, fovY: number): number {
  const tanY = Math.tan(fovY / 2);
  const tanX = tanY * Math.max(0.5, aspect);
  const ph = (phiDeg * Math.PI) / 180;
  const needW = rangeKm * 1.2;   // a little room for the height-scale labels
  const needH = rangeKm * Math.sin(ph) * 1.06 + zScene * Math.cos(ph) * 0.6 + zScene * 0.05;
  return Math.max(needW / tanX, needH / tanY) * 1.12;
}

export function presetPose(preset: CameraPreset, rangeKm: number, vexag: number, aspect: number, fovY: number): CameraPose {
  const zFit = Z_FIT_KM * vexag;
  switch (preset) {
    case 'top': {
      const dist = fitDistance(rangeKm, 0, 89, aspect, fovY);
      return { theta: 180, phi: 89, dist, target: [0, 0, 0] };
    }
    case 'side': {
      // Looking north from the south along the ground: the full height of the volume is the subject.
      const phi = 3;
      const dist = fitDistance(rangeKm * 0.88, zFit, phi, aspect, fovY);
      return { theta: 180, phi, dist, target: [0, 0, zFit * 0.42] };
    }
    default: {
      const phi = 34;
      const dist = fitDistance(rangeKm, zFit, phi, aspect, fovY);
      return { theta: 192, phi, dist, target: [0, -rangeKm * 0.04, zFit * 0.18] };
    }
  }
}

// ─── Camera maths ──────────────────────────────────────────────────────────

export interface Cam {
  eye: Vec3;
  fwd: Vec3;
  right: Vec3;
  up: Vec3;
  tanX: number;
  tanY: number;
  /** CSS pixels of the overlay canvas. */
  w: number;
  h: number;
}

/** Project a scene point to CSS pixels; null when behind the camera. Third value is the view depth. */
export function project(c: Cam, p: Vec3): [number, number, number] | null {
  const vx = p[0] - c.eye[0];
  const vy = p[1] - c.eye[1];
  const vz = p[2] - c.eye[2];
  const d = vx * c.fwd[0] + vy * c.fwd[1] + vz * c.fwd[2];
  if (d < 0.5) return null;
  const x = vx * c.right[0] + vy * c.right[1] + vz * c.right[2];
  const y = vx * c.up[0] + vy * c.up[1] + vz * c.up[2];
  return [c.w / 2 + (x / d / c.tanX) * (c.w / 2), c.h / 2 - (y / d / c.tanY) * (c.h / 2), d];
}

// ─── Overlay ───────────────────────────────────────────────────────────────

/** A radar site on the ground of the 3D scene (km east / north of the radar), drawn by `drawOverlay`. */
export interface SceneMarker {
  x: number;
  y: number;
  label: string;
  /** CSS colour of the ring (and of the fill when `filled`). */
  color: string;
  /** Filled = the latest scan is known and recent; half = known and older; hollow = not checked. */
  fill: 'none' | 'half' | 'full';
}

export interface OverlayOptions {
  rangeKm: number;
  zTopKm: number;
  vexag: number;
  ringStepKm: number;
  siteName: string;
  /** Draw the ground rings' labels (the rings themselves are drawn by the shader). */
  showRings: boolean;
  /** Frame the volume with a faint wire cylinder. */
  showFrame: boolean;
  /** Other radar sites on the ground (the "Radar sites" layer). */
  markers?: SceneMarker[];
}

/** Ring spacing that gives 3-5 rings across the crop radius. */
export function ringStepFor(rangeKm: number): number {
  if (rangeKm <= 60) return 10;
  if (rangeKm <= 120) return 25;
  if (rangeKm <= 260) return 50;
  return 100;
}

const FONT = '600 11px system-ui, -apple-system, "Segoe UI", sans-serif';

function haloText(ctx: CanvasRenderingContext2D, text: string, x: number, y: number, color: string, align: CanvasTextAlign = 'center') {
  ctx.textAlign = align;
  ctx.textBaseline = 'middle';
  ctx.lineWidth = 3;
  ctx.strokeStyle = 'rgba(6,8,16,0.85)';
  ctx.lineJoin = 'round';
  ctx.strokeText(text, x, y);
  ctx.fillStyle = color;
  ctx.fillText(text, x, y);
}

function polyline(ctx: CanvasRenderingContext2D, c: Cam, pts: Vec3[], close = false) {
  ctx.beginPath();
  let started = false;
  for (const p of pts) {
    const s = project(c, p);
    if (!s) { started = false; continue; }
    if (!started) { ctx.moveTo(s[0], s[1]); started = true; } else ctx.lineTo(s[0], s[1]);
  }
  if (close && started) ctx.closePath();
  ctx.stroke();
}

function circlePts(r: number, z: number, n = 96): Vec3[] {
  const out: Vec3[] = [];
  for (let i = 0; i <= n; i++) {
    const a = (i / n) * Math.PI * 2;
    out.push([r * Math.sin(a), r * Math.cos(a), z]);
  }
  return out;
}

/** Clear and draw the overlay. `ctx` is already scaled so one unit = one CSS pixel. */
export function drawOverlay(ctx: CanvasRenderingContext2D, c: Cam, o: OverlayOptions): void {
  ctx.clearRect(0, 0, c.w, c.h);
  const R = o.rangeKm;
  const ve = o.vexag;
  const H = o.zTopKm * ve;
  ctx.font = FONT;
  ctx.lineCap = 'round';

  // How edge-on is the ground? (1 = top-down, 0 = along the horizon)
  const groundness = Math.abs(c.fwd[2]);

  // ── Faint frame: top rim + the four cardinal edges
  if (o.showFrame) {
    ctx.strokeStyle = 'rgba(170,190,230,0.16)';
    ctx.lineWidth = 1;
    polyline(ctx, c, circlePts(R, H));
    for (const [x, y] of [[0, R], [R, 0], [0, -R], [-R, 0]] as const) polyline(ctx, c, [[x, y, 0], [x, y, H]]);
  }

  // ── Range-ring labels along the screen-horizontal direction through the radar
  if (o.showRings && groundness > 0.14) {
    let dx = c.right[0];
    let dy = c.right[1];
    const l = Math.hypot(dx, dy);
    if (l < 1e-3) { dx = 1; dy = 0; } else { dx /= l; dy /= l; }
    for (let g = o.ringStepKm; g <= R + 1e-6; g += o.ringStepKm) {
      const s = project(c, [dx * g, dy * g, 0]);
      if (s) haloText(ctx, `${g} km`, s[0], s[1] - 7, 'rgba(190,205,235,0.85)');
    }
  }

  // ── N / E / S / W letters just outside the rim
  const rl = R * 1.07;
  const letters: [string, number, number, string][] = [
    ['N', 0, rl, '#ff7a6b'], ['E', rl, 0, 'rgba(225,232,250,0.9)'],
    ['S', 0, -rl, 'rgba(225,232,250,0.9)'], ['W', -rl, 0, 'rgba(225,232,250,0.9)'],
  ];
  ctx.font = '700 13px system-ui, -apple-system, sans-serif';
  for (const [t, x, y, col] of letters) {
    const s = project(c, [x, y, 0]);
    if (s) haloText(ctx, t, s[0], s[1], col);
  }
  ctx.font = FONT;

  // ── North arrow on the ground, pointing outward from the rim
  {
    const a0 = project(c, [0, R * 1.0, 0]);
    const a1 = project(c, [0, R * 1.13, 0]);
    if (a0 && a1) {
      ctx.strokeStyle = '#ff7a6b';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(a0[0], a0[1]);
      ctx.lineTo(a1[0], a1[1]);
      ctx.stroke();
      const dxs = a1[0] - a0[0];
      const dys = a1[1] - a0[1];
      const len = Math.hypot(dxs, dys);
      if (len > 4) {
        const ux = dxs / len;
        const uy = dys / len;
        ctx.fillStyle = '#ff7a6b';
        ctx.beginPath();
        ctx.moveTo(a1[0] + ux * 5, a1[1] + uy * 5);
        ctx.lineTo(a1[0] - ux * 2 - uy * 4, a1[1] - uy * 2 + ux * 4);
        ctx.lineTo(a1[0] - ux * 2 + uy * 4, a1[1] - uy * 2 - ux * 4);
        ctx.closePath();
        ctx.fill();
      }
    }
  }

  // ── Radar site: a small mast with a ground ring
  {
    const g = project(c, [0, 0, 0]);
    const t = project(c, [0, 0, 1.2 * ve]); // a 1.2 km mast, exaggerated like everything else
    if (g) {
      ctx.strokeStyle = 'rgba(255,255,255,0.95)';
      ctx.fillStyle = 'rgba(255,200,60,0.95)';
      ctx.lineWidth = 1.5;
      if (t) { ctx.beginPath(); ctx.moveTo(g[0], g[1]); ctx.lineTo(t[0], t[1]); ctx.stroke(); }
      ctx.beginPath();
      ctx.arc(g[0], g[1], 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      haloText(ctx, o.siteName || 'Radar', g[0] + 9, g[1] + 11, 'rgba(255,225,140,0.98)', 'left');
    }
  }

  // ── Other radar sites: ground markers with a thinned id label (hollow = scan age not checked)
  if (o.markers && o.markers.length) {
    const boxes: Array<[number, number, number, number]> = [];
    ctx.font = '500 10px ui-monospace, "IBM Plex Mono", monospace';
    for (const m of o.markers) {
      const s = project(c, [m.x, m.y, 0]);
      if (!s) continue;
      if (s[0] < -8 || s[1] < -8 || s[0] > c.w + 8 || s[1] > c.h + 8) continue;
      ctx.beginPath();
      ctx.arc(s[0], s[1], 4, 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(6,8,16,0.85)';
      ctx.fill();
      if (m.fill !== 'none') {
        ctx.beginPath();
        if (m.fill === 'full') ctx.arc(s[0], s[1], 4, 0, Math.PI * 2);
        else ctx.arc(s[0], s[1], 4, Math.PI / 2, Math.PI * 1.5);
        ctx.fillStyle = m.color;
        ctx.fill();
      }
      ctx.beginPath();
      ctx.arc(s[0], s[1], 4, 0, Math.PI * 2);
      ctx.strokeStyle = m.color;
      ctx.lineWidth = 1.5;
      ctx.stroke();
      if (groundness > 0.14) {
        const w = ctx.measureText(m.label).width + 6;
        const box: [number, number, number, number] = [s[0] + 6, s[1] - 7, s[0] + 6 + w, s[1] + 7];
        if (!boxes.some((b) => box[0] < b[2] && box[2] > b[0] && box[1] < b[3] && box[3] > b[1]) && boxes.length < 40) {
          boxes.push(box);
          haloText(ctx, m.label, s[0] + 8, s[1], 'rgba(225,232,250,0.92)', 'left');
        }
      }
    }
    ctx.font = FONT;
  }

  // ── Height scale: a vertical axis on the silhouette edge that is leftmost on screen
  // (meaningless, and ugly, when looking straight down)
  if (groundness < 0.93) {
    // Leftmost point of the ground circle that still leaves room for the labels on screen.
    let best: Vec3 | null = null;
    let bestX = Infinity;
    let fallback: Vec3 | null = null;
    let fallbackX = Infinity;
    for (let i = 0; i < 72; i++) {
      const a = (i / 72) * Math.PI * 2;
      const p: Vec3 = [R * Math.sin(a), R * Math.cos(a), 0];
      const s = project(c, p);
      if (!s) continue;
      if (s[0] < fallbackX) { fallbackX = s[0]; fallback = p; }
      const t = project(c, [p[0], p[1], H]);   // the top of the axis leans outwards in perspective
      if (s[0] >= 78 && (!t || t[0] >= 60) && s[0] < bestX) { bestX = s[0]; best = p; }
    }
    if (!best) best = fallback;
    if (best) {
      const base = project(c, best);
      const top = project(c, [best[0], best[1], H]);
      if (base && top) {
        ctx.strokeStyle = 'rgba(235,240,255,0.7)';
        ctx.lineWidth = 1.25;
        ctx.beginPath();
        ctx.moveTo(base[0], base[1]);
        ctx.lineTo(top[0], top[1]);
        ctx.stroke();
        const tickKm = o.zTopKm <= 12 ? 2 : 5;
        const labelRight = base[0] < 64;
        for (let km = 0; km <= o.zTopKm + 1e-6; km += tickKm) {
          const s = project(c, [best[0], best[1], km * ve]);
          if (!s) continue;
          const dir = labelRight ? 1 : -1;
          ctx.strokeStyle = 'rgba(235,240,255,0.7)';
          ctx.beginPath();
          ctx.moveTo(s[0], s[1]);
          ctx.lineTo(s[0] + dir * 6, s[1]);
          ctx.stroke();
          if (Math.abs(top[1] - base[1]) > 24 || km === 0) {
            haloText(ctx, km === 0 ? '0 km' : `${km} km`, s[0] + dir * 9, s[1], 'rgba(225,232,250,0.95)', labelRight ? 'left' : 'right');
          }
        }
        if (Math.abs(top[1] - base[1]) > 40) {
          haloText(ctx, 'height AGL', Math.max(44, top[0]), top[1] - 11, 'rgba(180,195,225,0.9)');
        }
      }
    }
  }

  // ── Exaggeration note
  ctx.font = FONT;
  haloText(ctx, `Vertical exaggeration ${ve % 1 === 0 ? ve : ve.toFixed(1)}×`, c.w - 12, c.h - 14, 'rgba(190,205,235,0.9)', 'right');
}
