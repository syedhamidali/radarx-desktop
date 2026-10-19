/**
 * 2D overlay drawn above the Cartesian 3D canvas: the frame of the scene, N/E/S/W letters, a
 * north arrow, a ground scale bar, a height axis (ASL), the radar sites and the vertical
 * exaggeration note. Colours follow the scene (dark or light background) so the labels contrast
 * with it. Works in scene units like the shader: km, z exaggerated by `vexag`.
 */

import { project, type Cam, type Vec3 } from './scene3d';
import type { SceneBox, SceneSite } from './cartesianVolume';

export interface CartOverlayOptions {
  box: SceneBox;
  vexag: number;
  sites: SceneSite[];
  lightScene: boolean;
  showFrame: boolean;
  /** Extra line at the bottom right (e.g. the arrow scale). */
  note?: string;
  /** Draw the height axis (default true). */
  showHeightAxis?: boolean;
  /** Draw the exaggeration note at the bottom right (default true). */
  showExaggeration?: boolean;
}

const FONT = '600 11px system-ui, -apple-system, "Segoe UI", sans-serif';

interface Ink {
  text: string;
  halo: string;
  line: string;
  faint: string;
  north: string;
  site: string;
  siteText: string;
}

function ink(light: boolean): Ink {
  return light
    ? {
        text: 'rgba(25,32,48,0.95)',
        halo: 'rgba(255,255,255,0.9)',
        line: 'rgba(30,40,64,0.75)',
        faint: 'rgba(40,55,90,0.28)',
        north: '#c8281b',
        site: 'rgba(255,170,0,0.95)',
        siteText: 'rgba(110,60,0,0.98)',
      }
    : {
        text: 'rgba(225,232,250,0.95)',
        halo: 'rgba(6,8,16,0.85)',
        line: 'rgba(235,240,255,0.7)',
        faint: 'rgba(170,190,230,0.2)',
        north: '#ff7a6b',
        site: 'rgba(255,200,60,0.95)',
        siteText: 'rgba(255,225,140,0.98)',
      };
}

function halo(ctx: CanvasRenderingContext2D, k: Ink, text: string, x: number, y: number, color: string, align: CanvasTextAlign = 'center') {
  ctx.textAlign = align;
  ctx.textBaseline = 'middle';
  ctx.lineWidth = 3;
  ctx.strokeStyle = k.halo;
  ctx.lineJoin = 'round';
  ctx.strokeText(text, x, y);
  ctx.fillStyle = color;
  ctx.fillText(text, x, y);
}

function line(ctx: CanvasRenderingContext2D, c: Cam, pts: Vec3[], close = false) {
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

/** A scale-bar length (km) of roughly a fifth of `extentKm`: 1, 2, 5 x 10^n. */
export function niceLengthKm(extentKm: number): number {
  const raw = Math.max(1, extentKm / 5);
  const p = 10 ** Math.floor(Math.log10(raw));
  const m = raw / p;
  return (m >= 5 ? 5 : m >= 2 ? 2 : 1) * p;
}

export function drawCartesianOverlay(ctx: CanvasRenderingContext2D, c: Cam, o: CartOverlayOptions): void {
  ctx.clearRect(0, 0, c.w, c.h);
  const k = ink(o.lightScene);
  const b = o.box;
  const ve = o.vexag;
  const H = b.zMax * ve;
  const w = b.xMax - b.xMin;
  const d = b.yMax - b.yMin;
  const cx = (b.xMin + b.xMax) / 2;
  const cy = (b.yMin + b.yMax) / 2;
  ctx.font = FONT;
  ctx.lineCap = 'round';
  const groundness = Math.abs(c.fwd[2]);

  const corners: [number, number][] = [[b.xMin, b.yMin], [b.xMax, b.yMin], [b.xMax, b.yMax], [b.xMin, b.yMax]];
  if (o.showFrame) {
    ctx.strokeStyle = k.faint;
    ctx.lineWidth = 1;
    line(ctx, c, corners.map(([x, y]) => [x, y, H] as Vec3), true);
    for (const [x, y] of corners) line(ctx, c, [[x, y, 0], [x, y, H]]);
  }

  // N / E / S / W just outside the footprint
  const ext = Math.max(w, d) * 0.04;
  const letters: [string, number, number, string][] = [
    ['N', cx, b.yMax + ext, k.north],
    ['E', b.xMax + ext, cy, k.text],
    ['S', cx, b.yMin - ext, k.text],
    ['W', b.xMin - ext, cy, k.text],
  ];
  ctx.font = '700 13px system-ui, -apple-system, sans-serif';
  for (const [t, x, y, col] of letters) {
    const s = project(c, [x, y, 0]);
    if (s) halo(ctx, k, t, s[0], s[1], col);
  }
  ctx.font = FONT;

  // north arrow on the ground beyond the northern edge
  {
    const a0 = project(c, [cx, b.yMax, 0]);
    const a1 = project(c, [cx, b.yMax + ext * 0.8, 0]);
    const a2 = project(c, [cx, b.yMax + ext * 2.6, 0]);
    if (a0 && a1 && a2) {
      void a1;
      ctx.strokeStyle = k.north;
      ctx.fillStyle = k.north;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(a0[0], a0[1]);
      ctx.lineTo(a2[0], a2[1]);
      ctx.stroke();
      const dx = a2[0] - a0[0];
      const dy = a2[1] - a0[1];
      const len = Math.hypot(dx, dy);
      if (len > 6) {
        const ux = dx / len;
        const uy = dy / len;
        ctx.beginPath();
        ctx.moveTo(a2[0] + ux * 5, a2[1] + uy * 5);
        ctx.lineTo(a2[0] - ux * 2 - uy * 4, a2[1] - uy * 2 + ux * 4);
        ctx.lineTo(a2[0] - ux * 2 + uy * 4, a2[1] - uy * 2 - ux * 4);
        ctx.closePath();
        ctx.fill();
      }
    }
  }

  // ground scale bar along the southern edge
  {
    const km = niceLengthKm(w);
    const y = b.yMin - ext * 0.9;
    const x0 = b.xMin;
    const s0 = project(c, [x0, y, 0]);
    const s1 = project(c, [x0 + km, y, 0]);
    if (s0 && s1 && Math.hypot(s1[0] - s0[0], s1[1] - s0[1]) > 18) {
      ctx.strokeStyle = k.line;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(s0[0], s0[1]);
      ctx.lineTo(s1[0], s1[1]);
      ctx.stroke();
      halo(ctx, k, `${km} km`, (s0[0] + s1[0]) / 2, (s0[1] + s1[1]) / 2 + 11, k.text);
    }
  }

  // radar sites: a mast with a ground ring, and the name
  for (const s of o.sites) {
    if (s.x < b.xMin || s.x > b.xMax || s.y < b.yMin || s.y > b.yMax) continue;
    const g = project(c, [s.x, s.y, 0]);
    const t = project(c, [s.x, s.y, Math.max(1.2, s.alt + 1) * ve]);
    if (!g) continue;
    ctx.strokeStyle = o.lightScene ? 'rgba(20,28,48,0.95)' : 'rgba(255,255,255,0.95)';
    ctx.fillStyle = k.site;
    ctx.lineWidth = 1.5;
    if (t) { ctx.beginPath(); ctx.moveTo(g[0], g[1]); ctx.lineTo(t[0], t[1]); ctx.stroke(); }
    ctx.beginPath();
    ctx.arc(g[0], g[1], 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    halo(ctx, k, s.name || 'Radar', g[0] + 9, g[1] + 11, k.siteText, 'left');
  }

  // height axis (ASL) on the leftmost bottom corner
  if (groundness < 0.93 && o.showHeightAxis !== false) {
    let best: [number, number] | null = null;
    let bestX = Infinity;
    for (const [x, y] of corners) {
      const s = project(c, [x, y, 0]);
      if (s && s[0] < bestX) { bestX = s[0]; best = [x, y]; }
    }
    if (best) {
      const base = project(c, [best[0], best[1], 0]);
      const top = project(c, [best[0], best[1], H]);
      if (base && top) {
        ctx.strokeStyle = k.line;
        ctx.lineWidth = 1.25;
        ctx.beginPath();
        ctx.moveTo(base[0], base[1]);
        ctx.lineTo(top[0], top[1]);
        ctx.stroke();
        const tick = b.zMax <= 12 ? 2 : 5;
        const labelRight = base[0] < 64;
        const dir = labelRight ? 1 : -1;
        for (let km = 0; km <= b.zMax + 1e-6; km += tick) {
          const s = project(c, [best[0], best[1], km * ve]);
          if (!s) continue;
          ctx.strokeStyle = k.line;
          ctx.beginPath();
          ctx.moveTo(s[0], s[1]);
          ctx.lineTo(s[0] + dir * 6, s[1]);
          ctx.stroke();
          if (Math.abs(top[1] - base[1]) > 24 || km === 0) {
            halo(ctx, k, km === 0 ? '0 km' : `${km} km`, s[0] + dir * 9, s[1], k.text, labelRight ? 'left' : 'right');
          }
        }
        if (Math.abs(top[1] - base[1]) > 40) halo(ctx, k, 'height ASL', Math.max(44, top[0]), top[1] - 11, k.text);
      }
    }
  }

  ctx.font = FONT;
  if (o.showExaggeration !== false) halo(ctx, k, `Vertical exaggeration ${ve % 1 === 0 ? ve : ve.toFixed(1)}×`, c.w - 12, c.h - 14, k.text, 'right');
  if (o.note) halo(ctx, k, o.note, c.w - 12, c.h - 30, k.text, 'right');
}

const COMPASS8 = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];

/** "94 km S" of site (sx, sy) seen from (cx, cy), all km. */
export function siteDirection(cx: number, cy: number, sx: number, sy: number): string {
  const dx = sx - cx;
  const dy = sy - cy;
  const brg = ((Math.atan2(dx, dy) * 180) / Math.PI + 360) % 360;
  return `${Math.round(Math.hypot(dx, dy))} km ${COMPASS8[Math.round(brg / 45) % 8]}`;
}

/**
 * Radar sites outside the shown box: a marker at the box edge in their direction, with their name
 * and distance, so the viewer knows where the data comes from.
 */
export function drawOffBoxSites(ctx: CanvasRenderingContext2D, c: Cam, box: SceneBox, sites: SceneSite[], lightScene: boolean): void {
  const k = ink(lightScene);
  const cx = (box.xMin + box.xMax) / 2;
  const cy = (box.yMin + box.yMax) / 2;
  const hx = (box.xMax - box.xMin) / 2;
  const hy = (box.yMax - box.yMin) / 2;
  ctx.font = FONT;
  for (const s of sites) {
    if (s.x >= box.xMin && s.x <= box.xMax && s.y >= box.yMin && s.y <= box.yMax) continue;
    const dx = s.x - cx;
    const dy = s.y - cy;
    const f = Math.min(hx / Math.max(1e-9, Math.abs(dx)), hy / Math.max(1e-9, Math.abs(dy)));
    const e = project(c, [cx + dx * f * 1.04, cy + dy * f * 1.04, 0]);
    if (!e) continue;
    const t = project(c, [cx + dx * f * 1.04, cy + dy * f * 1.04, 1.5]);
    ctx.fillStyle = k.site;
    ctx.strokeStyle = lightScene ? 'rgba(20,28,48,0.95)' : 'rgba(255,255,255,0.95)';
    ctx.lineWidth = 1.5;
    if (t) { ctx.beginPath(); ctx.moveTo(e[0], e[1]); ctx.lineTo(t[0], t[1] - 6); ctx.stroke(); }
    ctx.beginPath();
    ctx.arc(e[0], e[1], 4.5, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    halo(ctx, k, `${s.name} · ${siteDirection(cx, cy, s.x, s.y)}`, e[0] + 9, e[1] + 12, k.siteText, 'left');
  }
}
