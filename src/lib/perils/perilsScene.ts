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
  type PerilsData, type Station, type TornadoRec, balloonAt, heightOfTemp, rotationAt, stationReadout, tornadoAt, clockUtc,
} from './perilsData.ts';
import { type RGB, drawText, rgba, ui, provColour, chip, HAIL_COLOUR, TDS_COLOUR, ROTATION_COLOUR, ZDR_COLOUR, TORNADO_COLOUR, GRAUPEL_COLOUR } from './perilsStyle.ts';
import { recentHail } from './perilsInsets.ts';

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
  /** Index of the observed step nearest in time. */
  nearStep: number;
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
    if (d < 260) drawText(ctx, r.name, top[0] + 8 * u, top[1] - 5 * u, u, { size: 15, color: rgba(col, 1) });
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
    const base = pj(cam, s.x, s.y, 0, e.ve);
    if (!onScreen(base, cam.w, cam.h, 120)) continue;
    const d = base[2];
    const near = s.id === e.nearestStation;
    const hKm = Math.max(0.1, d * 0.022);
    const top = pj(cam, s.x, s.y, hKm, e.ve);
    const ro = stationReadout(s, e.td);
    const rain = ro.rain ?? 0;
    const col = near ? ui().text : provColour('observed');
    ctx.save();
    ctx.globalAlpha = a * (near ? 1 : 0.85);
    // rain ring on the ground
    const rKm = Math.max(0.25, d * 0.009) * (0.7 + Math.min(2.2, Math.sqrt(rain) / 3));
    if (rain > 0.2) {
      polyline(ctx, groundCircle(cam, s.x, s.y, rKm, e.ve), true);
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
      const tip = pj(cam, s.x + L * Math.sin(to), s.y + L * Math.cos(to), 0.01, e.ve);
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
      polyline(ctx, groundCircle(cam, s.x, s.y, rKm * (1.5 + 3 * f), e.ve), true);
      ctx.strokeStyle = rgba(HAIL_COLOUR, 0.9 * (1 - f));
      ctx.lineWidth = 3 * u;
      ctx.stroke();
      if (top) drawText(ctx, `hail candidate · largest ${hail.maxD.toFixed(0)} mm`, top[0] + 10 * u, top[1] - 26 * u, u, { size: 14, color: rgba(HAIL_COLOUR, 1) });
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
      if (d < 140 || near) {
        const txt = near && ro.rain != null ? `${s.id}  ${ro.rain.toFixed(1)} mm/h${ro.dm != null ? `  Dm ${ro.dm.toFixed(1)} mm` : ''}` : s.id;
        drawText(ctx, txt, top[0] + 10 * u, top[1] - 6 * u, u, { size: near ? 17 : 14, color: rgba(col, 1) });
      }
    }
    ctx.restore();
  }
}

// ─── tornado ────────────────────────────────────────────────────────────────

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
      drawText(ctx, text, p[0] + 9 * u, p[1] + 5 * u, u, { size: 15, color: rgba([255, 200, 200], 1) });
    };
    const rating = tor.rating ?? 'tornado';
    lab(tor.path[0], `${rating} starts ${clockUtc(e.data.time0, tor.path[0].t)}`);
    lab(tor.path[tor.path.length - 1], `ends ${clockUtc(e.data.time0, tor.path[tor.path.length - 1].t)}`);
    if (tor.path_interpolated) {
      const mid = tor.path[Math.floor(tor.path.length / 2)];
      const pm = pj(cam, mid.x, mid.y, 0.04, e.ve);
      if (onScreen(pm, cam.w, cam.h)) drawText(ctx, 'surveyed begin and end, path between them interpolated', pm[0] + 10 * u, pm[1] + 32 * u, u, { size: 12, weight: 500, color: rgba([255, 205, 200], 0.95) });
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

/**
 * The condensation funnel and debris cloud, STYLISED: drawn only while a surveyed tornado is active
 * (inside the surveyed time span), at the surveyed position, as wide at the ground as the survey's
 * path width and with the debris ball twice that. The shape is a generic cone; the survey gives
 * where, when and how wide, nothing else. Cloud base height: `cloudBaseKm`.
 */
export function drawFunnel(e: SceneEnv, a: number, cloudBaseKm: number): void {
  if (a <= 0.02) return;
  const { ctx, cam, u } = e;
  for (const tor of e.data.tornadoes) {
    const cur = tornadoAt(tor, e.td);
    if (!cur) continue;
    const wKm = Math.max(0.05, (tor.width_m ?? 150) / 1000);
    const N = 14;
    const pts: { l: [number, number]; r: [number, number]; z: number }[] = [];
    const base = pj(cam, cur.x, cur.y, 0, e.ve);
    if (!onScreen(base, cam.w, cam.h, 400)) continue;
    const right = cam.right;
    for (let i = 0; i <= N; i++) {
      const f = i / N;
      const z = f * cloudBaseKm;
      // narrow rope near the ground, flaring toward the cloud base, with a slow deterministic wobble
      const rad = wKm * (0.35 + 1.4 * Math.pow(f, 2.2) * 2.6) * 0.5;
      const sway = 0.05 * wKm * Math.sin(e.tc * 3 + f * 5) * f;
      const cxk = cur.x + sway;
      const pl = pj(cam, cxk - right[0] * rad, cur.y - right[1] * rad, z, e.ve);
      const pr = pj(cam, cxk + right[0] * rad, cur.y + right[1] * rad, z, e.ve);
      if (!pl || !pr) {
        pts.length = 0;
        break;
      }
      pts.push({ l: [pl[0], pl[1]], r: [pr[0], pr[1]], z });
    }
    if (pts.length < 3) continue;
    ctx.save();
    ctx.globalAlpha = a;
    ctx.beginPath();
    ctx.moveTo(pts[0].l[0], pts[0].l[1]);
    for (const q of pts) ctx.lineTo(q.l[0], q.l[1]);
    for (let i = pts.length - 1; i >= 0; i--) ctx.lineTo(pts[i].r[0], pts[i].r[1]);
    ctx.closePath();
    const g = ctx.createLinearGradient(0, pts[0].l[1], 0, pts[pts.length - 1].l[1]);
    g.addColorStop(0, 'rgba(70,64,58,0.88)');
    g.addColorStop(0.45, 'rgba(150,152,158,0.8)');
    g.addColorStop(1, 'rgba(205,210,218,0.6)');
    ctx.fillStyle = g;
    ctx.fill();
    // debris ball at the foot: twice the width of the damage path
    const bp = pj(cam, cur.x, cur.y, 0.04, e.ve);
    if (bp) {
      const rpx = Math.max(3 * u, wKm * pxPerKm(cam, bp[2]));
      const gb = ctx.createRadialGradient(bp[0], bp[1] - rpx * 0.3, 0, bp[0], bp[1] - rpx * 0.3, rpx * 1.8);
      gb.addColorStop(0, 'rgba(40,34,30,0.85)');
      gb.addColorStop(1, 'rgba(40,34,30,0)');
      ctx.fillStyle = gb;
      ctx.beginPath();
      ctx.ellipse(bp[0], bp[1] - rpx * 0.3, rpx * 1.8, rpx * 1.1, 0, 0, Math.PI * 2);
      ctx.fill();
    }
    const top = pts[pts.length - 1];
    chip(ctx, 'stylised', top.r[0] + 14 * u, top.r[1] - 6 * u, u, 'STYLISED FUNNEL', 12);
    drawText(ctx, 'position and width from the survey', top.r[0] + 14 * u, top.r[1] + 14 * u, u, { size: 13, color: rgba(ui().dim, 1) });
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
  for (const tr of e.data.rotation) {
    if (!tr.points.length) continue;
    ctx.save();
    ctx.globalAlpha = a;
    ctx.lineCap = 'round';
    const trail = tr.points.filter((p) => p.t <= e.td).map((p) => pj(cam, p.x, p.y, 0.06, e.ve));
    if (trail.length > 1) {
      polyline(ctx, trail);
      ctx.strokeStyle = rgba(ROTATION_COLOUR, 0.35);
      ctx.lineWidth = 9 * u;
      ctx.stroke();
      polyline(ctx, trail);
      ctx.strokeStyle = rgba(ROTATION_COLOUR, 0.85);
      ctx.lineWidth = 2 * u;
      ctx.stroke();
    }
    const cur = rotationAt(tr, e.td, 90);
    if (cur) {
      const v = typeof cur.vrot_ms === 'number' ? cur.vrot_ms : 20;
      const hKm = 2.6;
      const b = pj(cam, cur.x, cur.y, 0.05, e.ve);
      const t = pj(cam, cur.x, cur.y, hKm, e.ve);
      if (b && t) {
        const k = Math.max(0.35, Math.min(1, v / 40));
        const g = ctx.createLinearGradient(b[0], b[1], t[0], t[1]);
        g.addColorStop(0, rgba(ROTATION_COLOUR, 0.95 * k));
        g.addColorStop(1, rgba(ROTATION_COLOUR, 0.05));
        ctx.strokeStyle = g;
        ctx.lineWidth = (4 + 6 * k) * u;
        ctx.beginPath();
        ctx.moveTo(b[0], b[1]);
        ctx.lineTo(t[0], t[1]);
        ctx.stroke();
        ctx.strokeStyle = rgba([255, 255, 255], 0.8 * k);
        ctx.lineWidth = 1.5 * u;
        ctx.stroke();
        drawText(ctx, `rotation ${v.toFixed(0)} m/s${tr.radar ? ` · ${tr.radar}` : ''}`, t[0] + 10 * u, t[1], u, { size: 15, color: rgba(ROTATION_COLOUR, 1) });
      }
    }
    ctx.restore();
  }
}

/** Tornadic debris signature: radar cells flagged by low RhoHV with high reflectivity (RETRIEVED), as a small magenta cloud. */
export function drawTds(e: SceneEnv, a: number): void {
  if (a <= 0.02) return;
  const pts = e.data.tds[e.nearStep];
  if (!pts || !pts.length) return;
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
    ctx.arc(p[0], p[1], Math.max(1.6 * u, 0.12 * pxPerKm(cam, p[2])), 0, Math.PI * 2);
    ctx.fill();
    cx += p[0];
    cy += p[1];
    n++;
  }
  ctx.restore();
  if (n > 3) {
    ctx.save();
    ctx.globalAlpha = a;
    drawText(ctx, 'debris signature (radar)', cx / n + 14 * u, cy / n - 12 * u, u, { size: 15, color: rgba(TDS_COLOUR, 1) });
    ctx.restore();
  }
}

// ─── hail, ZDR columns ──────────────────────────────────────────────────────

/**
 * Hail cores from the radar's hydrometeor classification (RETRIEVED): the cells classified as hail,
 * drawn as warm sprites, with short vertical streaks falling beneath a subset of them (the streaks are
 * STYLISED: they show where hail is falling from, not drop trajectories).
 */
export function drawHail(e: SceneEnv, a: number): void {
  if (a <= 0.02) return;
  const h = e.data.hail[e.nearStep];
  if (!h || !h.p.length) return;
  const { ctx, cam, u } = e;
  ctx.save();
  ctx.globalAlpha = a;
  ctx.globalCompositeOperation = 'lighter';
  const phase = (e.tc * 1.7) % 1;
  // at most ~900 points are drawn (an even stride through the cells), small and soft
  const stride = Math.max(1, Math.ceil(h.p.length / 900));
  let k = 0;
  for (let n = 0; n < h.p.length; n += stride) {
    const q = h.p[n];
    k++;
    const p = pj(cam, q[0], q[1], q[2], e.ve);
    if (!onScreen(p, cam.w, cam.h)) continue;
    const r = Math.min(2.4 * u, Math.max(1.2 * u, 0.06 * pxPerKm(cam, p[2])));
    ctx.fillStyle = rgba(HAIL_COLOUR, 0.5);
    ctx.beginPath();
    ctx.arc(p[0], p[1], r, 0, Math.PI * 2);
    ctx.fill();
    if (k % 6 === 0 && q[2] > 0.5) {
      // a streak below the core, sliding down the column with the clip clock
      const f = (phase + (k % 7) / 7) % 1;
      const z0 = q[2] * (1 - f * 0.9);
      const a0 = pj(cam, q[0], q[1], z0, e.ve);
      const a1 = pj(cam, q[0], q[1], Math.max(0.02, z0 - 0.7), e.ve);
      if (a0 && a1) {
        ctx.strokeStyle = rgba([255, 240, 220], 0.45 * (1 - f));
        ctx.lineWidth = 1.3 * u;
        ctx.beginPath();
        ctx.moveTo(a0[0], a0[1]);
        ctx.lineTo(a1[0], a1[1]);
        ctx.stroke();
      }
    }
  }
  ctx.restore();
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
    if (sp < 1.5) continue;
    const p0 = pj(cam, g[0], g[1], viz.zKm, e.ve);
    if (!onScreen(p0, cam.w, cam.h)) continue;
    const L = Math.min(3.2, 0.18 * sp);
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
  if (last && onScreen(last, cam.w, cam.h)) drawText(ctx, 'cold-pool edge (surface analysis, 2 K cooler)', last[0] + 8 * u, last[1] - 8 * u, u, { size: 13, color: 'rgba(170,220,255,1)' });
  ctx.restore();
}

// ─── soundings ──────────────────────────────────────────────────────────────

/**
 * Sounding sites with the balloon's real drift track (OBSERVED) and a column at the site with tick
 * marks at the freezing level and the -10 and -20 C levels of that sounding.
 */
export function drawBalloons(e: SceneEnv, a: number): void {
  if (a <= 0.02) return;
  const { ctx, cam, u } = e;
  // the isotherm ticks are labelled for the sounding nearest in time only
  let nearest: PerilsData['soundings'][number] | null = null;
  for (const s of e.data.soundings) if (s.levels && (!nearest || Math.abs(s.launch - e.td) < Math.abs(nearest.launch - e.td))) nearest = s;
  for (const s of e.data.soundings) {
    ctx.save();
    ctx.globalAlpha = a;
    const base = pj(cam, s.x, s.y, 0, e.ve);
    if (onScreen(base, cam.w, cam.h, 100)) {
      const top = pj(cam, s.x, s.y, 12, e.ve);
      if (top) {
        ctx.strokeStyle = rgba(provColour('observed'), 0.5);
        ctx.lineWidth = 1.5 * u;
        ctx.setLineDash([4 * u, 4 * u]);
        ctx.beginPath();
        ctx.moveTo(base[0], base[1]);
        ctx.lineTo(top[0], top[1]);
        ctx.stroke();
        ctx.setLineDash([]);
      }
      for (const [tC, name] of [[0, '0 °C'], [-10, '-10 °C'], [-20, '-20 °C']] as const) {
        const hm = heightOfTemp(s.levels, tC);
        if (hm == null) continue;
        const hk = hm / 1000;
        const p = pj(cam, s.x, s.y, hk, e.ve);
        if (!p) continue;
        ctx.strokeStyle = rgba(provColour('observed'), 0.95);
        ctx.lineWidth = 2.2 * u;
        ctx.beginPath();
        ctx.moveTo(p[0] - 9 * u, p[1]);
        ctx.lineTo(p[0] + 9 * u, p[1]);
        ctx.stroke();
        if (s === nearest) drawText(ctx, `${name} ${hk.toFixed(1)} km`, p[0] + 13 * u, p[1] + 5 * u, u, { size: 13, color: rgba(provColour('observed'), 1) });
      }
      drawText(ctx, `${s.site} · launch ${clockUtc(e.data.time0, s.launch)}`, base[0] + 10 * u, base[1] + 18 * u, u, { size: 14, color: rgba(provColour('observed'), 1) });
    }
    // the balloon track: ascended part solid, the rest faint
    if (s.drift.length > 1) {
      const past = s.drift.filter((q) => q[0] <= e.td).map((q) => pj(cam, q[1], q[2], q[3], e.ve));
      const all = s.drift.map((q) => pj(cam, q[1], q[2], q[3], e.ve));
      polyline(ctx, all);
      ctx.strokeStyle = rgba(provColour('observed'), 0.18);
      ctx.lineWidth = 1.5 * u;
      ctx.stroke();
      polyline(ctx, past);
      ctx.strokeStyle = rgba(provColour('observed'), 0.9);
      ctx.lineWidth = 2.4 * u;
      ctx.stroke();
      const b = balloonAt(s, e.td);
      if (b) {
        const p = pj(cam, b[0], b[1], b[2], e.ve);
        if (p) {
          ctx.beginPath();
          ctx.arc(p[0], p[1], 6 * u, 0, Math.PI * 2);
          ctx.fillStyle = rgba([255, 255, 255], 1);
          ctx.fill();
        }
      }
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
  let best: PerilsData['soundings'][number] | null = null;
  for (const s of e.data.soundings) if (s.levels && (!best || Math.abs(s.launch - e.td) < Math.abs(best.launch - e.td))) best = s;
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
    if (lp && onScreen(lp, cam.w, cam.h)) drawText(ctx, `${name} ${hk.toFixed(1)} km`, lp[0] + 6 * u, lp[1] - 4 * u, u, { size: 13, color: 'rgba(225,235,250,0.95)' });
  }
  ctx.restore();
}

export { GRAUPEL_COLOUR };

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
