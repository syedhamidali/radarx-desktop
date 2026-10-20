/**
 * Annotations drawn into the 3-D scene of the PERiLS movie (on the overlay canvas, projected with
 * the frame's camera): radar sites, stations with live read-outs, the tornado's surveyed track and
 * marker, the stylised funnel, radar rotation and debris-signature markers, hail, ZDR columns,
 * balloon tracks and isotherm references. Every function draws one layer at opacity `a` (the layer's
 * relevance in the current shot) and says in its comment which provenance class its content has.
 *
 * Nothing is drawn without evidence in the data: a funnel only inside the surveyed time span of a
 * tornado, rotation and debris markers only where the bundle has them at that time.
 */
import type { OverlayCam } from './clipExtras';
import {
  type PerilsData, type RotationTrack, type TornadoRec, balloonAt, balloonFlight, heightOfTemp, rotationAt, stationReadout, stationXY, tornadoAt, clockUtc,
} from './perilsData.ts';
import { type RGB, drawText, measure, rgba, ui, provColour, chip, HAIL_COLOUR, TDS_COLOUR, ROTATION_COLOUR, ZDR_COLOUR, TORNADO_COLOUR } from './perilsStyle.ts';
import { recentHail } from './perilsInsets.ts';
import type { LabelReq } from './perilsLayout.ts';

export interface SceneEnv {
  ctx: CanvasRenderingContext2D;
  cam: OverlayCam;
  /** Pixel scale (height / 1080). */
  u: number;
  ve: number;
  /** Data time, s after the first step; clip time, s. */
  td: number;
  tc: number;
  data: PerilsData;
  nearestStation: string | null;
  /** Centre of the line (km) at this time. */
  centre: [number, number];
  /** The pre-storm sounding the Skew-T and the isotherm references use. */
  sounding: PerilsData['soundings'][number] | null;
  /** Index of the observed step nearest in time. */
  nearStep: number;
  /** Data seconds per clip second at this frame. */
  speedup: number;
  /** Where the labels go: they are not drawn here but placed (cap, priority, no overlap) and drawn by the HUD (perilsLayout.ts). */
  labels: { add(r: LabelReq): void };
}

/** Project a scene point (km, height km un-exaggerated) with a near plane of 20 m: [px, py, depth km] or null. */
export function pj(cam: OverlayCam, x: number, y: number, zKm: number, ve: number): [number, number, number] | null {
  const vx = x - cam.eye[0];
  const vy = y - cam.eye[1];
  const vz = zKm * ve - cam.eye[2];
  const d = vx * cam.fwd[0] + vy * cam.fwd[1] + vz * cam.fwd[2];
  if (d < 0.02) return null;
  const px = vx * cam.right[0] + vy * cam.right[1] + vz * cam.right[2];
  const py = vx * cam.up[0] + vy * cam.up[1] + vz * cam.up[2];
  return [cam.w / 2 + (px / d / cam.tanX) * (cam.w / 2), cam.h / 2 - (py / d / cam.tanY) * (cam.h / 2), d];
}

const onScreen = (p: [number, number, number] | null, w: number, h: number, m = 60): p is [number, number, number] =>
  !!p && p[0] > -m && p[0] < w + m && p[1] > -m && p[1] < h + m;

/** Pixels per km at depth `d` (horizontal). */
const pxPerKm = (cam: OverlayCam, d: number) => (cam.w / 2) / (d * cam.tanX);

function polyline(ctx: CanvasRenderingContext2D, pts: ([number, number, number] | null)[], close = false): void {
  ctx.beginPath();
  let pen = false;
  for (const p of pts) {
    if (!p) {
      pen = false;
      continue;
    }
    if (!pen) ctx.moveTo(p[0], p[1]);
    else ctx.lineTo(p[0], p[1]);
    pen = true;
  }
  if (close) ctx.closePath();
}

// ─── radars ─────────────────────────────────────────────────────────────────

/** Radar sites as masts with their names. OBSERVED sites (coordinates of the instruments). */
export function drawRadars(e: SceneEnv, a: number): void {
  if (a <= 0.02) return;
  const { ctx, cam, u } = e;
  for (const r of e.data.radars) {
    const base = pj(cam, r.x, r.y, 0, e.ve);
    if (!onScreen(base, cam.w, cam.h)) continue;
    const d = base[2];
    const hKm = Math.max(0.35, d * 0.018);
    const top = pj(cam, r.x, r.y, hKm, e.ve);
    if (!top) continue;
    const mobile = /mobile|DOW|COW|NOXP|SMART|MAX|X-band/i.test(`${r.type} ${r.name}`);
    const col: RGB = mobile ? [255, 190, 70] : ui().accent;
    ctx.save();
    ctx.globalAlpha = a;
    ctx.strokeStyle = rgba(col, 0.9);
    ctx.lineWidth = 2 * u;
    ctx.beginPath();
    ctx.moveTo(base[0], base[1]);
    ctx.lineTo(top[0], top[1]);
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(top[0], top[1], 4.5 * u, 0, Math.PI * 2);
    ctx.fillStyle = rgba(col, 1);
    ctx.fill();
    if (d < 260) e.labels.add({ id: `radar:${r.name}`, text: r.name, ax: top[0], ay: top[1], size: 15, colour: rgba(col, 1), priority: (mobile ? 50 : 45) - Math.min(20, d / 12), alpha: a });
    ctx.restore();
  }
}

// ─── stations ───────────────────────────────────────────────────────────────

function groundCircle(cam: OverlayCam, x: number, y: number, rKm: number, ve: number, n = 28): ([number, number, number] | null)[] {
  const out: ([number, number, number] | null)[] = [];
  for (let i = 0; i <= n; i++) {
    const a = (i / n) * Math.PI * 2;
    out.push(pj(cam, x + rKm * Math.sin(a), y + rKm * Math.cos(a), 0.01, ve));
  }
  return out;
}

/**
 * Stations (disdrometers, surface stations) standing on the ground: a pole, a ring whose size follows
 * the rain rate, a wind arrow along the ground, and for the station nearest the camera a highlight.
 * The numbers come from the station's own series at the current data time (OBSERVED); where the series
 * has no value (a gap, outside its span) nothing is shown.
 */
export function drawStations(e: SceneEnv, a: number): void {
  if (a <= 0.02) return;
  const { ctx, cam, u } = e;
  for (const s of e.data.stations) {
    if (s.moving && !s.track) continue; // a vehicle without a track has no place to draw
    const [sx, sy] = stationXY(s, e.td);
    const base = pj(cam, sx, sy, 0, e.ve);
    if (!onScreen(base, cam.w, cam.h, 120)) continue;
    const d = base[2];
    const near = s.id === e.nearestStation;
    const hKm = Math.max(0.1, d * 0.022);
    const top = pj(cam, sx, sy, hKm, e.ve);
    const ro = stationReadout(s, e.td);
    const rain = ro.rain ?? 0;
    const col = near ? ui().text : provColour('observed');
    ctx.save();
    ctx.globalAlpha = a * (near ? 1 : 0.85);
    // rain ring on the ground
    const rKm = Math.max(0.25, d * 0.009) * (0.7 + Math.min(2.2, Math.sqrt(rain) / 3));
    if (rain > 0.2) {
      polyline(ctx, groundCircle(cam, sx, sy, rKm, e.ve), true);
      ctx.fillStyle = rgba([60, 140, 255], Math.min(0.45, 0.12 + rain / 120));
      ctx.fill();
      ctx.strokeStyle = rgba([120, 190, 255], 0.9);
      ctx.lineWidth = 2 * u;
      ctx.stroke();
    }
    // wind arrow on the ground (toward where the wind goes)
    if (ro.windMs != null && ro.windDir != null && ro.windMs > 0.5) {
      const to = ((ro.windDir + 180) * Math.PI) / 180;
      const L = Math.max(0.5, d * 0.014) * (0.6 + ro.windMs / 18);
      const tip = pj(cam, sx + L * Math.sin(to), sy + L * Math.cos(to), 0.01, e.ve);
      if (tip) {
        ctx.strokeStyle = rgba([255, 255, 255], 0.9);
        ctx.lineWidth = 2.2 * u;
        ctx.beginPath();
        ctx.moveTo(base[0], base[1]);
        ctx.lineTo(tip[0], tip[1]);
        ctx.stroke();
        const ang = Math.atan2(tip[1] - base[1], tip[0] - base[0]);
        ctx.beginPath();
        ctx.moveTo(tip[0], tip[1]);
        ctx.lineTo(tip[0] - 9 * u * Math.cos(ang - 0.4), tip[1] - 9 * u * Math.sin(ang - 0.4));
        ctx.lineTo(tip[0] - 9 * u * Math.cos(ang + 0.4), tip[1] - 9 * u * Math.sin(ang + 0.4));
        ctx.closePath();
        ctx.fillStyle = rgba([255, 255, 255], 0.9);
        ctx.fill();
      }
    }
    // a hail candidate at this station in the last seconds (OBSERVED size of the largest particle): an expanding ring
    const hail = recentHail(s, e.td, 25);
    if (hail) {
      const f = hail.age / 25;
      polyline(ctx, groundCircle(cam, sx, sy, rKm * (1.5 + 3 * f), e.ve), true);
      ctx.strokeStyle = rgba(HAIL_COLOUR, 0.9 * (1 - f));
      ctx.lineWidth = 3 * u;
      ctx.stroke();
      if (top) e.labels.add({ id: `hail:${s.id}`, text: `hail candidate · largest ${hail.maxD.toFixed(0)} mm`, ax: top[0], ay: top[1] - 20 * u, size: 14, colour: rgba(HAIL_COLOUR, 1), priority: 65, alpha: a });
    }
    // pole and head
    if (top) {
      ctx.strokeStyle = rgba(col, 0.95);
      ctx.lineWidth = (near ? 3 : 2) * u;
      ctx.beginPath();
      ctx.moveTo(base[0], base[1]);
      ctx.lineTo(top[0], top[1]);
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(top[0], top[1], (near ? 6.5 : 4.2) * u, 0, Math.PI * 2);
      ctx.fillStyle = rgba(col, 1);
      ctx.fill();
      if (near) {
        ctx.strokeStyle = rgba(ui().caution, 0.95);
        ctx.lineWidth = 2 * u;
        ctx.beginPath();
        ctx.arc(top[0], top[1], 12 * u, 0, Math.PI * 2);
        ctx.stroke();
      }
      if (d < 25 || near) {
        const short = s.id.slice(s.id.lastIndexOf('_') + 1);
        const txt = near && ro.rain != null ? `${short}  ${ro.rain.toFixed(1)} mm/h${ro.dm != null ? `  Dm ${ro.dm.toFixed(1)} mm` : ''}` : short;
        e.labels.add({ id: `station:${s.id}`, text: txt, ax: top[0], ay: top[1], size: near ? 17 : 14, colour: rgba(col, 1), priority: near ? 75 : 30 - Math.min(20, d), alpha: a * (near ? 1 : 0.85) });
      }
    }
    ctx.restore();
  }
}

// ─── tornado ────────────────────────────────────────────────────────────────

/**
 * The damage swath on the ground up to the tornado's position: a ribbon of the SURVEYED width (a few hundred metres: a thin
 * line from far away, a real band from a few km) along the surveyed path (INTERPOLATED between the surveyed ends).
 * Drawn on the ground as projected quads; the part behind the near plane is skipped.
 */
export function drawSwath(e: SceneEnv, a: number): void {
  if (a <= 0.02) return;
  const { ctx, cam, u } = e;
  for (const tor of e.data.tornadoes) {
    const p = tor.path;
    if (p.length < 2 || !tor.width_m) continue;
    const cur = tornadoAt(tor, e.td);
    const upto = cur ?? (e.td > p[p.length - 1].t ? { x: p[p.length - 1].x, y: p[p.length - 1].y } : null);
    if (!upto) continue;
    // sample the travelled path every ~250 m
    const pts: [number, number][] = [[p[0].x, p[0].y]];
    for (let i = 1; i < p.length; i++) {
      const ax = p[i - 1].x;
      const ay = p[i - 1].y;
      const bx = p[i].x;
      const by = p[i].y;
      const len2 = (bx - ax) ** 2 + (by - ay) ** 2 || 1;
      const f = Math.max(0, Math.min(1, ((upto.x - ax) * (bx - ax) + (upto.y - ay) * (by - ay)) / len2));
      const done = f < 1 || (upto.x === bx && upto.y === by);
      const ex = f < 1 ? ax + (bx - ax) * f : bx;
      const ey = f < 1 ? ay + (by - ay) * f : by;
      const n = Math.max(1, Math.ceil(Math.hypot(ex - ax, ey - ay) / 0.25));
      for (let k = 1; k <= n; k++) pts.push([ax + ((ex - ax) * k) / n, ay + ((ey - ay) * k) / n]);
      if (done && f < 1) break;
    }
    if (pts.length < 2) continue;
    const hw = tor.width_m / 2000; // km
    ctx.save();
    ctx.globalAlpha = a;
    ctx.fillStyle = 'rgba(255,140,60,0.5)';
    for (let i = 1; i < pts.length; i++) {
      const [x0, y0] = pts[i - 1];
      const [x1, y1] = pts[i];
      const dx = x1 - x0;
      const dy = y1 - y0;
      const l = Math.hypot(dx, dy) || 1;
      const nx = (-dy / l) * hw;
      const ny = (dx / l) * hw;
      const q = [pj(cam, x0 + nx, y0 + ny, 0.03, e.ve), pj(cam, x1 + nx, y1 + ny, 0.03, e.ve), pj(cam, x1 - nx, y1 - ny, 0.03, e.ve), pj(cam, x0 - nx, y0 - ny, 0.03, e.ve)];
      if (q.some((c) => !c)) continue;
      ctx.beginPath();
      q.forEach((c, k) => (k ? ctx.lineTo(c![0], c![1]) : ctx.moveTo(c![0], c![1])));
      ctx.closePath();
      ctx.fill();
    }
    // the edges of the swath, thin
    ctx.strokeStyle = 'rgba(255,190,120,0.7)';
    ctx.lineWidth = 1.2 * u;
    for (const s of [1, -1]) {
      const edge = pts.map(([x, y], i) => {
        const [xa, ya] = pts[Math.max(0, i - 1)];
        const [xb, yb] = pts[Math.min(pts.length - 1, i + 1)];
        const l = Math.hypot(xb - xa, yb - ya) || 1;
        return pj(cam, x + (-(yb - ya) / l) * hw * s, y + ((xb - xa) / l) * hw * s, 0.03, e.ve);
      });
      polyline(ctx, edge);
      ctx.stroke();
    }
    ctx.restore();
  }
}

/** Surveyed track on the ground with start and end labels (OBSERVED, NWS survey). */
export function drawTornadoTrack(e: SceneEnv, a: number): void {
  if (a <= 0.02) return;
  const { ctx, cam, u } = e;
  for (const tor of e.data.tornadoes) {
    if (tor.path.length < 2) continue;
    const pts = tor.path.map((q) => pj(cam, q.x, q.y, 0.04, e.ve));
    const done = tor.path.map((q) => (q.t <= e.td ? 1 : 0));
    ctx.save();
    ctx.globalAlpha = a;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    // dark underlay, faint future, bright travelled part
    polyline(ctx, pts);
    ctx.strokeStyle = 'rgba(4,8,16,0.7)';
    ctx.lineWidth = 6 * u;
    ctx.stroke();
    polyline(ctx, pts);
    ctx.strokeStyle = rgba(TORNADO_COLOUR, 0.45);
    ctx.lineWidth = 3 * u;
    ctx.setLineDash([8 * u, 6 * u]);
    ctx.stroke();
    ctx.setLineDash([]);
    const upto = pts.filter((_, i) => done[i] || (i > 0 && done[i - 1]));
    polyline(ctx, upto);
    ctx.strokeStyle = rgba(TORNADO_COLOUR, 0.98);
    ctx.lineWidth = 3.4 * u;
    ctx.stroke();
    // start and end labels
    const lab = (q: TornadoRec['path'][number], text: string) => {
      const p = pj(cam, q.x, q.y, 0.04, e.ve);
      if (!onScreen(p, cam.w, cam.h)) return;
      ctx.beginPath();
      ctx.arc(p[0], p[1], 5 * u, 0, Math.PI * 2);
      ctx.fillStyle = rgba(TORNADO_COLOUR, 1);
      ctx.fill();
      e.labels.add({ id: `track:${text.split(' ')[0]}`, text, ax: p[0], ay: p[1], size: 15, colour: rgba([255, 200, 200], 1), priority: text.includes('starts') ? 100 : 90, alpha: a, slide: true, offsets: (w) => [[9, 5], [9, -14], [-(w + 9), 5], [-(w + 9), -14], [-w / 2, -22], [-w / 2, 28]] });
    };
    const rating = tor.rating ?? 'tornado';
    lab(tor.path[0], `${rating} starts ${clockUtc(e.data.time0, tor.path[0].t)}`);
    lab(tor.path[tor.path.length - 1], `ends ${clockUtc(e.data.time0, tor.path[tor.path.length - 1].t)}`);
    if (tor.path_interpolated) {
      const p0 = tor.path[0];
      const p1 = tor.path[tor.path.length - 1];
      const pm = pj(cam, (p0.x + p1.x) / 2, (p0.y + p1.y) / 2, 0.04, e.ve);
      const note = 'surveyed begin and end, path between them interpolated';
      if (onScreen(pm, cam.w, cam.h)) {
        const nx = Math.max(12 * u, Math.min(pm[0] - 100 * u, cam.w - 24 * u - measure(ctx, note, u, { size: 12, weight: 500 })));
        e.labels.add({ id: 'track:note', text: note, ax: nx, ay: pm[1] + 32 * u, size: 12, weight: 500, colour: rgba([255, 205, 200], 0.95), priority: 15, alpha: a, offsets: [[0, 0], [0, 22], [0, -22], [0, 44]] });
      }
    }
    // the marker at the clip-time position
    const cur = tornadoAt(tor, e.td);
    if (cur) {
      const p = pj(cam, cur.x, cur.y, 0.04, e.ve);
      if (p) {
        const pulse = 1 + 0.12 * Math.sin(e.tc * 6);
        ctx.beginPath();
        ctx.arc(p[0], p[1], 12 * u * pulse, 0, Math.PI * 2);
        ctx.strokeStyle = 'rgba(4,8,16,0.8)';
        ctx.lineWidth = 6 * u;
        ctx.stroke();
        ctx.strokeStyle = rgba([255, 255, 255], 1);
        ctx.lineWidth = 3 * u;
        ctx.stroke();
      }
    }
    ctx.restore();
  }
}

// ─── radar evidence ─────────────────────────────────────────────────────────

/**
 * Low-level rotation tracks (RETRIEVED from the radar's Doppler velocities): the swath travelled so
 * far on the ground and a glowing vertical column at the position of the current time. Only where the
 * bundle has a rotation track point within 90 s of the data time.
 */
export function drawRotation(e: SceneEnv, a: number): void {
  if (a <= 0.02) return;
  const { ctx, cam, u } = e;
  const now: { tr: RotationTrack; cur: NonNullable<ReturnType<typeof rotationAt>> }[] = [];
  for (const tr of e.data.rotation) {
    if (!tr.points.length) continue;
    ctx.save();
    ctx.globalAlpha = a;
    ctx.lineCap = 'round';
    const trail = tr.points.filter((p) => p.t <= e.td).map((p) => pj(cam, p.x, p.y, 0.06, e.ve));
    if (trail.length > 1) {
      polyline(ctx, trail);
      ctx.strokeStyle = rgba(ROTATION_COLOUR, 0.3);
      ctx.lineWidth = 8 * u;
      ctx.stroke();
      polyline(ctx, trail);
      ctx.strokeStyle = rgba(ROTATION_COLOUR, 0.8);
      ctx.lineWidth = 1.8 * u;
      ctx.stroke();
    }
    const cur = rotationAt(tr, e.td, 90);
    if (cur) {
      now.push({ tr, cur });
      const v = typeof cur.vrot_ms === 'number' ? cur.vrot_ms : 20;
      const b = pj(cam, cur.x, cur.y, 0.05, e.ve);
      const t = pj(cam, cur.x, cur.y, 1.6, e.ve);
      if (b && t) {
        const k = Math.max(0.3, Math.min(1, v / 40));
        const g = ctx.createLinearGradient(b[0], b[1], t[0], t[1]);
        g.addColorStop(0, rgba(ROTATION_COLOUR, 0.95 * k));
        g.addColorStop(1, rgba(ROTATION_COLOUR, 0.05));
        ctx.strokeStyle = g;
        ctx.lineWidth = (3 + 5 * k) * u;
        ctx.beginPath();
        ctx.moveTo(b[0], b[1]);
        ctx.lineTo(t[0], t[1]);
        ctx.stroke();
        ctx.strokeStyle = rgba([255, 255, 255], 0.7 * k);
        ctx.lineWidth = 1.3 * u;
        ctx.stroke();
      }
    }
    ctx.restore();
  }
  // only the strongest is named, so that a line of mesovortices does not turn into a wall of text
  now.sort((x, y) => ((y.cur.vrot_ms as number) ?? 0) - ((x.cur.vrot_ms as number) ?? 0));
  ctx.save();
  ctx.globalAlpha = a;
  for (const { tr, cur } of now.slice(0, 1)) {
    const t = pj(cam, cur.x, cur.y, 1.6, e.ve);
    if (!onScreen(t, cam.w, cam.h)) continue;
    const sh = typeof cur.as === 'number' ? cur.as : null;
    e.labels.add({ id: 'vortex', text: sh != null ? `vortex candidate · shear ${sh.toFixed(3)} s⁻¹` : 'vortex candidate', ax: t[0], ay: t[1] + 6 * u, size: 15, colour: rgba(ROTATION_COLOUR, 1), priority: 70, alpha: a });
  }
  ctx.restore();
}

/** Tornadic debris signature: radar cells flagged by low RhoHV with high reflectivity (RETRIEVED), as a small magenta cloud. */
export function drawTds(e: SceneEnv, a: number): void {
  if (a <= 0.02) return;
  // only the cells the data tool flags as near a vortex (2): low RhoHV with high Z alone (1) is weak evidence
  const pts = (e.data.tds[e.nearStep] ?? []).filter((q) => q[3] >= 2);
  if (!pts.length) return;
  const { ctx, cam, u } = e;
  ctx.save();
  ctx.globalAlpha = a;
  ctx.globalCompositeOperation = 'lighter';
  let cx = 0;
  let cy = 0;
  let n = 0;
  for (const q of pts) {
    const p = pj(cam, q[0], q[1], q[2], e.ve);
    if (!onScreen(p, cam.w, cam.h)) continue;
    ctx.fillStyle = rgba(TDS_COLOUR, 0.55);
    ctx.beginPath();
    ctx.arc(p[0], p[1], Math.min(5 * u, Math.max(1.6 * u, 0.12 * pxPerKm(cam, p[2]))), 0, Math.PI * 2);
    ctx.fill();
    cx += p[0];
    cy += p[1];
    n++;
  }
  ctx.restore();
  if (n > 3) {
    ctx.save();
    ctx.globalAlpha = a;
    e.labels.add({ id: 'debris', text: 'possible debris (weak evidence)', ax: cx / n + 4 * u, ay: cy / n - 6 * u, size: 15, colour: rgba(TDS_COLOUR, 1), priority: 60, alpha: a });
    ctx.restore();
  }
}

// ─── hail, ZDR columns ──────────────────────────────────────────────────────

/**
 * Hail cells of the radar's hydrometeor classification (RETRIEVED, weak to moderate evidence: no hail particle of 8 mm
 * or more was measured at the surface, so this is a classification, not observed hail): the cells drawn as warm sprites
 * and one label. No falling streaks and no sizes are drawn.
 */
export function drawHail(e: SceneEnv, a: number): void {
  if (a <= 0.02) return;
  const h = e.data.hail[e.nearStep];
  if (!h || !h.p.length) return;
  const { ctx, cam, u } = e;
  ctx.save();
  ctx.globalAlpha = a;
  ctx.globalCompositeOperation = 'lighter';
  // at most ~900 points are drawn (an even stride through the cells), small and soft
  const stride = Math.max(1, Math.ceil(h.p.length / 900));
  let sx = 0;
  let sy = 0;
  let n = 0;
  for (let i = 0; i < h.p.length; i += stride) {
    const q = h.p[i];
    const p = pj(cam, q[0], q[1], q[2], e.ve);
    if (!onScreen(p, cam.w, cam.h)) continue;
    const r = Math.min(2.4 * u, Math.max(1.2 * u, 0.06 * pxPerKm(cam, p[2])));
    ctx.fillStyle = rgba(HAIL_COLOUR, 0.5);
    ctx.beginPath();
    ctx.arc(p[0], p[1], r, 0, Math.PI * 2);
    ctx.fill();
    sx += p[0];
    sy += p[1];
    n++;
  }
  ctx.restore();
  if (n > 20) {
    ctx.save();
    ctx.globalAlpha = a;
    e.labels.add({ id: 'hailclass', text: 'hail class (weak evidence)', ax: sx / n + 2 * u, ay: sy / n - 4 * u, size: 14, colour: rgba(HAIL_COLOUR, 1), priority: 55, alpha: a });
    ctx.restore();
  }
}

/** ZDR columns (RETRIEVED): a thin cyan line from the ground to the column's top (the updraft lofting liquid drops above the melting level). */
export function drawZdrColumns(e: SceneEnv, a: number): void {
  if (a <= 0.02) return;
  const cols = e.data.zdrColumns[e.nearStep];
  if (!cols) return;
  const { ctx, cam, u } = e;
  // the column stands on the melting level: from the freezing level of the nearest sounding (4 km if none)
  let fl = 4.0;
  let best: PerilsData['soundings'][number] | null = null;
  for (const s of e.data.soundings) if (s.levels && (!best || Math.abs(s.launch - e.td) < Math.abs(best.launch - e.td))) best = s;
  const hz = best ? heightOfTemp(best.levels, 0) : null;
  if (hz != null) fl = hz / 1000;
  ctx.save();
  ctx.globalAlpha = a;
  ctx.lineCap = 'round';
  for (const c of cols.slice(0, 14)) {
    const b = pj(cam, c[0], c[1], fl, e.ve);
    const t = pj(cam, c[0], c[1], fl + Math.min(4, c[2]), e.ve);
    if (!onScreen(b, cam.w, cam.h) || !t) continue;
    const g = ctx.createLinearGradient(b[0], b[1], t[0], t[1]);
    g.addColorStop(0, rgba(ZDR_COLOUR, 0.25));
    g.addColorStop(1, rgba(ZDR_COLOUR, 0.95));
    ctx.strokeStyle = g;
    ctx.lineWidth = 3.2 * u;
    ctx.beginPath();
    ctx.moveTo(b[0], b[1]);
    ctx.lineTo(t[0], t[1]);
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(t[0], t[1], 3.6 * u, 0, Math.PI * 2);
    ctx.fillStyle = rgba(ZDR_COLOUR, 1);
    ctx.fill();
  }
  ctx.restore();
}

// ─── winds ──────────────────────────────────────────────────────────────────

/** Colour of a wind arrow by speed (m/s): blue-violet slow to white-yellow fast. */
export function windColour(sp: number): RGB {
  const f = Math.max(0, Math.min(1, sp / 25));
  return [Math.round(110 + 145 * f), Math.round(130 + 120 * f), Math.round(255 - 60 * f * f)];
}

/**
 * Storm-relative wind from the multi-Doppler retrieval (RETRIEVED): horizontal arrows at one level
 * (inflow ahead of the line, rear inflow behind it, outflow) coloured by speed, and the cells of strong
 * vertical motion (updraft warm, downdraft cool). Cells the retrieval masked (fewer than two radars, small
 * beam-crossing angle) are not in the data and are not drawn. The arrow length is a display scale, not a distance travelled.
 */
export function drawWinds(e: SceneEnv, a: number): void {
  if (a <= 0.02) return;
  const viz = e.data.winds[e.nearStep];
  if (!viz) return;
  const { ctx, cam, u } = e;
  ctx.save();
  ctx.globalAlpha = a;
  ctx.lineCap = 'round';
  const n = viz.glyphs.length;
  const eyeD = Math.hypot(cam.eye[0], cam.eye[1], cam.eye[2]);
  const stride = eyeD > 90 ? 3 : eyeD > 40 ? 2 : 1;
  for (let i = 0; i < n; i += stride) {
    const g = viz.glyphs[i];
    const sp = Math.hypot(g[2], g[3]);
    if (sp < 4) continue;
    const p0 = pj(cam, g[0], g[1], viz.zKm, e.ve);
    if (!onScreen(p0, cam.w, cam.h)) continue;
    const L = Math.min(2.6, 0.12 * sp);
    const p1 = pj(cam, g[0] + (g[2] / sp) * L, g[1] + (g[3] / sp) * L, viz.zKm, e.ve);
    if (!p1) continue;
    const col = windColour(sp);
    ctx.strokeStyle = rgba(col, 0.85);
    ctx.lineWidth = 1.8 * u;
    ctx.beginPath();
    ctx.moveTo(p0[0], p0[1]);
    ctx.lineTo(p1[0], p1[1]);
    ctx.stroke();
    const ang = Math.atan2(p1[1] - p0[1], p1[0] - p0[0]);
    const hs = 6 * u;
    ctx.beginPath();
    ctx.moveTo(p1[0], p1[1]);
    ctx.lineTo(p1[0] - hs * Math.cos(ang - 0.45), p1[1] - hs * Math.sin(ang - 0.45));
    ctx.lineTo(p1[0] - hs * Math.cos(ang + 0.45), p1[1] - hs * Math.sin(ang + 0.45));
    ctx.closePath();
    ctx.fillStyle = rgba(col, 0.9);
    ctx.fill();
  }
  ctx.globalCompositeOperation = 'lighter';
  const wstride = Math.max(1, Math.ceil(viz.w.length / 900));
  for (let i = 0; i < viz.w.length; i += wstride) {
    const c = viz.w[i];
    const p = pj(cam, c[0], c[1], c[2], e.ve);
    if (!onScreen(p, cam.w, cam.h)) continue;
    const up = c[3] > 0;
    ctx.fillStyle = up ? 'rgba(255,150,60,0.5)' : 'rgba(80,170,255,0.5)';
    ctx.beginPath();
    ctx.arc(p[0], p[1], Math.min(5 * u, Math.max(1.6 * u, 0.09 * pxPerKm(cam, p[2]))) * Math.min(2, Math.abs(c[3]) / 8), 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.restore();
}

/** A pulsing ring and a label at the place of the science event on screen (ground, km). `a` fades it in and out with its caption. */
export function drawEventMarker(e: SceneEnv, a: number, x: number, y: number, label: string, colour: RGB): void {
  if (a <= 0.02) return;
  const { ctx, cam, u } = e;
  const base = pj(cam, x, y, 0.05, e.ve);
  if (!onScreen(base, cam.w, cam.h, 200)) return;
  ctx.save();
  ctx.globalAlpha = a;
  const d = base[2];
  for (let k = 0; k < 3; k++) {
    const f = (e.tc * 0.7 + k / 3) % 1;
    const rKm = Math.max(0.6, d * 0.012) * (1 + 5 * f);
    polyline(ctx, groundCircle(cam, x, y, rKm, e.ve), true);
    ctx.strokeStyle = rgba(colour, 0.9 * (1 - f));
    ctx.lineWidth = 3 * u;
    ctx.stroke();
  }
  const top = pj(cam, x, y, Math.max(0.8, d * 0.03), e.ve);
  if (top) {
    ctx.strokeStyle = rgba(colour, 0.95);
    ctx.lineWidth = 2.4 * u;
    ctx.beginPath();
    ctx.moveTo(base[0], base[1]);
    ctx.lineTo(top[0], top[1]);
    ctx.stroke();
    e.labels.add({ id: 'event', text: label, ax: top[0], ay: top[1] + 2 * u, size: 16, weight: 700, colour: rgba(colour, 1), priority: 95, alpha: a, slide: true });
  }
  ctx.restore();
}

// ─── cold pool ──────────────────────────────────────────────────────────────

/**
 * The edge of the cold pool on the ground: the contour of the surface temperature analysis 2 K below the
 * warm sector (ANALYSIS, an interpolation of station observations: smooth, defined only near stations). Drawn only
 * when the analysis has such a region.
 */
export function drawColdPool(e: SceneEnv, a: number, segs: number[][]): void {
  if (a <= 0.02 || !segs.length) return;
  const { ctx, cam, u } = e;
  ctx.save();
  ctx.globalAlpha = a;
  ctx.lineCap = 'round';
  ctx.strokeStyle = 'rgba(120,200,255,0.95)';
  ctx.lineWidth = 2.6 * u;
  ctx.setLineDash([9 * u, 5 * u]);
  ctx.beginPath();
  let last: [number, number, number] | null = null;
  for (const s of segs) {
    const p = pj(cam, s[0], s[1], 0.07, e.ve);
    const q = pj(cam, s[2], s[3], 0.07, e.ve);
    if (!p || !q) continue;
    ctx.moveTo(p[0], p[1]);
    ctx.lineTo(q[0], q[1]);
    last = q;
  }
  ctx.stroke();
  ctx.setLineDash([]);
  if (last && onScreen(last, cam.w, cam.h)) e.labels.add({ id: 'coldpool', text: 'cold-pool edge (surface analysis, 2 K cooler)', ax: last[0] - 2 * u, ay: last[1] - 2 * u, size: 13, colour: 'rgba(170,220,255,1)', priority: 25, alpha: a });
  ctx.restore();
}

// ─── soundings ──────────────────────────────────────────────────────────────

/**
 * The radiosondes in flight, nothing attached to them (no names, times or read-outs): a small balloon at its real
 * GPS position (OBSERVED position, STYLISED glyph) climbing along its measured track from its real launch time, with a
 * faint thin trail behind it. At the last point of its track (where the data end: the burst or the loss of the signal)
 * the balloon fades out within about 1.5 s of clip time and the trail about 3.5 s later. Nothing is extrapolated:
 * before the first point of the track and after the fade there is nothing; only soundings with a track AND a profile.
 */
export function drawBalloons(e: SceneEnv, a: number): void {
  if (a <= 0.02) return;
  const { ctx, cam, u } = e;
  for (const snd of e.data.soundings) {
    const b = balloonFlight(snd, e.td, e.speedup);
    if (!b) continue;
    const past = b.trail.map((q) => pj(cam, q[0], q[1], q[2], e.ve));
    const now = pj(cam, b.x, b.y, b.z, e.ve);
    past.push(now);
    ctx.save();
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    if (past.length > 1 && b.trailAlpha > 0.02) {
      ctx.globalAlpha = a * 0.32 * b.trailAlpha;
      polyline(ctx, past);
      ctx.strokeStyle = 'rgb(235,240,250)';
      ctx.lineWidth = 1.1 * u;
      ctx.stroke();
    }
    if (now && b.bodyAlpha > 0.02 && onScreen(now, cam.w, cam.h, 20)) {
      const r = Math.min(13 * u, Math.max(2.6 * u, 0.006 * pxPerKm(cam, now[2])));
      ctx.globalAlpha = a * b.bodyAlpha;
      ctx.beginPath();
      ctx.arc(now[0], now[1], r, 0, Math.PI * 2);
      ctx.fillStyle = 'rgb(240,244,252)';
      ctx.fill();
      ctx.strokeStyle = 'rgba(8,12,22,0.7)';
      ctx.lineWidth = Math.max(0.8, 0.9 * u);
      ctx.stroke();
    }
    ctx.restore();
  }
}

/**
 * Thin horizontal reference rings at the freezing level and the -10 / -20 C heights of a sounding,
 * centred on `centre` (km), radius `radiusKm`: where hail grows and where charge separates relative to the
 * echo. OBSERVED at the sounding site (the label says which sounding and when); the rings extend the
 * site's levels over the storm, which assumes the environment is horizontally uniform.
 */
export function drawIsotherms(e: SceneEnv, a: number, centre: [number, number], radiusKm: number): void {
  if (a <= 0.02) return;
  const { ctx, cam, u } = e;
  const best = e.sounding;
  if (!best) return;
  ctx.save();
  ctx.globalAlpha = a * 0.7;
  for (const [tC, name] of [[0, '0 °C'], [-10, '-10 °C'], [-20, '-20 °C']] as const) {
    const hm = heightOfTemp(best.levels, tC);
    if (hm == null) continue;
    const hk = hm / 1000;
    const ring: ([number, number, number] | null)[] = [];
    for (let i = 0; i <= 64; i++) {
      const an = (i / 64) * Math.PI * 2;
      ring.push(pj(cam, centre[0] + radiusKm * Math.sin(an), centre[1] + radiusKm * Math.cos(an), hk, e.ve));
    }
    polyline(ctx, ring);
    ctx.strokeStyle = rgba(tC === 0 ? [255, 255, 255] : tC === -10 ? [150, 205, 255] : [190, 150, 255], 0.55);
    ctx.lineWidth = 1.4 * u;
    ctx.setLineDash([10 * u, 8 * u]);
    ctx.stroke();
    ctx.setLineDash([]);
    const lp = pj(cam, centre[0] + radiusKm, centre[1], hk, e.ve);
    if (lp && onScreen(lp, cam.w, cam.h)) e.labels.add({ id: `iso:${name}`, text: `${name} ${hk.toFixed(1)} km`, ax: lp[0] - 4 * u, ay: lp[1] + 2 * u, size: 13, colour: 'rgba(225,235,250,0.95)', priority: 20, alpha: a * 0.7 });
  }
  ctx.restore();
}


/** Projection of an already-exaggerated scene point with the 20 m near plane (the signature `drawLma` wants). */
export function projRaw(cam: OverlayCam, p: [number, number, number]): [number, number, number] | null {
  const vx = p[0] - cam.eye[0];
  const vy = p[1] - cam.eye[1];
  const vz = p[2] - cam.eye[2];
  const d = vx * cam.fwd[0] + vy * cam.fwd[1] + vz * cam.fwd[2];
  if (d < 0.02) return null;
  const px = vx * cam.right[0] + vy * cam.right[1] + vz * cam.right[2];
  const py = vx * cam.up[0] + vy * cam.up[1] + vz * cam.up[2];
  return [cam.w / 2 + (px / d / cam.tanX) * (cam.w / 2), cam.h / 2 - (py / d / cam.tanY) * (cam.h / 2), d];
}
