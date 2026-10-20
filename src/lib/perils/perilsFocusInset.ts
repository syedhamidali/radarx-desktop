/**
 * The tornado-focus inset of the PERiLS movie: two plan-view panels centred on the SURVEYED tornado, 16-25 km across,
 * drawn in the film's own colour language.
 *
 *   top panel     the merged column-maximum reflectivity (the same colours as the 3-D echo), the camera and its view cone,
 *                 the surveyed path with the damage swath travelled so far, the tornado marker, the radar rotation maximum
 *                 (a ring, with its offset from the survey position stated in the footer), the motion of the echo, the
 *                 lightning of the last moments, the radar sites, a scale bar and a north arrow;
 *   second panel  the storm-relative radial velocity at 0.5 km (OBSERVED radial velocity of the nearest radar minus the
 *                 echo motion along its beam: green toward the radar, red away from it), the low-level azimuthal shear
 *                 outline (the data tool's 0.012 s-1 vortex threshold), the same markers, and a line to the radar that sees
 *                 the tornado, with its distance.
 *
 * Pure drawing; the numbers come in through `FocusInsetState`. Unit-tested where it has logic (perilsFocusInset.test.mjs).
 */
import { chip, drawText, measure, panel, rgba, ui } from './perilsStyle.ts';
import { compass16, type ProvKind } from './perilsData.ts';
import { scaleBarKm, headingOf } from './perilsMinimap.ts';

export interface FocusImages {
  a: CanvasImageSource | null;
  b: CanvasImageSource | null;
  /** Weight of `b` (cross-fade between the two observed volumes around the time). */
  s: number;
}

export interface FocusInsetState {
  /** Top left of the first panel and the side of one square map (px). */
  x: number;
  y: number;
  size: number;
  u: number;
  alpha: number;
  /** Km at the centre of the maps and half of their width. */
  cx: number;
  cy: number;
  half: number;
  grid: { x0: number; y0: number; dx: number; nx: number; ny: number };
  echo: FocusImages;
  vsr: FocusImages;
  base: { img: CanvasImageSource; cx: number; cy: number; half: number } | null;
  /** Outline of the azimuthal shear threshold, segments [x0, y0, x1, y1] in km. */
  shearSegs: number[][];
  tornado: { path: [number, number][]; cur: [number, number] | null; ghost: [number, number] | null; widthKm: number; headingDeg: number | null; speedMs: number | null } | null;
  candidate: { x: number; y: number } | null;
  offsetText: string;
  camera: { x: number; y: number; yawDeg: number; hfovDeg: number };
  /** Motion of the echo pattern (m/s east, north). */
  motionMs: [number, number];
  radars: { name: string; x: number; y: number }[];
  /** The radar of the velocity panel: the one that sees the tornado. */
  bestRadar: { name: string; x: number; y: number } | null;
  /** Lightning of the last moments as short plan-view line segments (the channel's edges): no markers. */
  streaks: { ax: number; ay: number; bx: number; by: number; b: number }[];
  tag: { kind: ProvKind; text: string };
  clock: string;
  /** Smaller panels without footers (the immersive angles). */
  compact: boolean;
}

/** Pixel of a km position in a map whose top left is (mx, my), `size` px wide, centred on (cx, cy) km with half-width `half` km. */
export function mapPixel(kx: number, ky: number, mx: number, my: number, size: number, cx: number, cy: number, half: number): [number, number] {
  const sc = size / (2 * half);
  return [mx + (kx - cx) * sc + size / 2, my - (ky - cy) * sc + size / 2];
}

/** Height (px) of one panel with its header and footer. */
export function panelHeight(size: number, u: number, compact: boolean): number {
  return size + (compact ? 22 : 28) * u + (compact ? 22 : 40) * u;
}

/** Total height of the two panels and the gap between them. */
export function insetHeight(size: number, u: number, compact: boolean): number {
  return 2 * panelHeight(size, u, compact) + 10 * u;
}

function drawImages(ctx: CanvasRenderingContext2D, im: FocusImages, st: FocusInsetState, x: number, y: number, gain: number): void {
  const g = st.grid;
  const sc = st.size / (2 * st.half);
  const [ex, ey] = mapPixel(g.x0 - g.dx / 2, g.y0 + (g.ny - 0.5) * g.dx, x, y, st.size, st.cx, st.cy, st.half);
  const eW = g.nx * g.dx * sc;
  const eH = g.ny * g.dx * sc;
  ctx.imageSmoothingEnabled = true;
  if (im.a) {
    ctx.globalAlpha = st.alpha * (im.b ? 1 - im.s : 1) * gain;
    ctx.drawImage(im.a, ex, ey, eW, eH);
  }
  if (im.b) {
    ctx.globalAlpha = st.alpha * (im.a ? im.s : 1) * gain;
    ctx.drawImage(im.b, ex, ey, eW, eH);
  }
  ctx.globalAlpha = st.alpha;
}

function arrow(ctx: CanvasRenderingContext2D, x0: number, y0: number, x1: number, y1: number, u: number, col: string): void {
  const a = Math.atan2(y1 - y0, x1 - x0);
  ctx.strokeStyle = 'rgba(4,8,16,0.8)';
  ctx.lineWidth = 5 * u;
  ctx.beginPath();
  ctx.moveTo(x0, y0);
  ctx.lineTo(x1, y1);
  ctx.stroke();
  ctx.strokeStyle = col;
  ctx.fillStyle = col;
  ctx.lineWidth = 2.4 * u;
  ctx.beginPath();
  ctx.moveTo(x0, y0);
  ctx.lineTo(x1, y1);
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(x1 + Math.cos(a) * 6 * u, y1 + Math.sin(a) * 6 * u);
  ctx.lineTo(x1 + Math.cos(a + 2.5) * 6 * u, y1 + Math.sin(a + 2.5) * 6 * u);
  ctx.lineTo(x1 + Math.cos(a - 2.5) * 6 * u, y1 + Math.sin(a - 2.5) * 6 * u);
  ctx.closePath();
  ctx.fill();
}

type Which = 'echo' | 'velocity';

function drawMap(ctx: CanvasRenderingContext2D, st: FocusInsetState, which: Which, x: number, y: number): void {
  const { size, u } = st;
  const U = ui();
  const P = (kx: number, ky: number) => mapPixel(kx, ky, x, y, size, st.cx, st.cy, st.half);
  const sc = size / (2 * st.half);
  ctx.save();
  ctx.beginPath();
  ctx.rect(x, y, size, size);
  ctx.clip();
  ctx.fillStyle = rgba(U.panel, 1);
  ctx.fillRect(x, y, size, size);
  if (st.base) {
    const b = st.base;
    const [bx, by] = P(b.cx - b.half, b.cy + b.half);
    ctx.globalAlpha = st.alpha * 0.5;
    ctx.drawImage(b.img, bx, by, 2 * b.half * sc, 2 * b.half * sc);
    ctx.globalAlpha = st.alpha;
  }
  if (which === 'echo') drawImages(ctx, st.echo, st, x, y, 0.94);
  else drawImages(ctx, st.vsr, st, x, y, 0.96);
  // the outline of the azimuthal shear threshold (the data tool's vortex threshold)
  if (st.shearSegs.length) {
    ctx.strokeStyle = which === 'echo' ? 'rgba(255,90,230,0.55)' : 'rgba(255,90,230,0.95)';
    ctx.lineWidth = (which === 'echo' ? 1.1 : 1.6) * u;
    ctx.beginPath();
    for (const s of st.shearSegs) {
      const a = P(s[0], s[1]);
      const b = P(s[2], s[3]);
      ctx.moveTo(a[0], a[1]);
      ctx.lineTo(b[0], b[1]);
    }
    ctx.stroke();
  }
  // radar sites
  for (const r of st.radars) {
    const [rx, ry] = P(r.x, r.y);
    if (rx < x - 4 || rx > x + size + 4 || ry < y - 4 || ry > y + size + 4) continue;
    ctx.fillStyle = 'rgba(120,200,255,0.95)';
    ctx.strokeStyle = 'rgba(4,8,16,0.9)';
    ctx.lineWidth = 1.6 * u;
    ctx.beginPath();
    ctx.moveTo(rx, ry - 5 * u);
    ctx.lineTo(rx + 4.4 * u, ry + 3 * u);
    ctx.lineTo(rx - 4.4 * u, ry + 3 * u);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
    if (!st.compact) drawText(ctx, r.name, rx + 6 * u, ry - 3 * u, u, { size: 10, weight: 600, color: 'rgba(160,215,255,1)', halo: 2.4 });
  }
  // the surveyed path: the swath travelled so far at its true width (at least a few pixels), the rest dashed
  const tor = st.tornado;
  if (tor && tor.path.length > 1) {
    const pts = tor.path.map(([kx, ky]) => P(kx, ky));
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.strokeStyle = 'rgba(4,8,16,0.6)';
    ctx.lineWidth = 4 * u;
    ctx.beginPath();
    pts.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
    ctx.stroke();
    ctx.strokeStyle = 'rgba(255,255,255,0.5)';
    ctx.setLineDash([5 * u, 4 * u]);
    ctx.lineWidth = 1.5 * u;
    ctx.stroke();
    ctx.setLineDash([]);
    if (tor.cur) {
      const c = P(tor.cur[0], tor.cur[1]);
      // travelled part: from the first point to the current position
      const done: [number, number][] = [pts[0]];
      for (let i = 1; i < tor.path.length; i++) {
        const a = tor.path[i - 1];
        const b = tor.path[i];
        const t = tor.cur;
        const abx = b[0] - a[0];
        const aby = b[1] - a[1];
        const f = (abx * (t[0] - a[0]) + aby * (t[1] - a[1])) / (abx * abx + aby * aby || 1);
        if (f >= 1) done.push(pts[i]);
        else {
          done.push(c);
          break;
        }
      }
      const wpx = Math.max(3 * u, tor.widthKm * sc);
      ctx.strokeStyle = 'rgba(255,150,60,0.62)';
      ctx.lineWidth = wpx;
      ctx.lineCap = 'butt';
      ctx.beginPath();
      done.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
      ctx.stroke();
      ctx.strokeStyle = 'rgba(255,90,90,0.98)';
      ctx.lineWidth = 2 * u;
      ctx.beginPath();
      done.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
      ctx.stroke();
      ctx.lineCap = 'round';
    }
  }
  // lightning of the last moments: faint short line segments, nothing at the sources
  ctx.lineCap = 'butt';
  ctx.strokeStyle = 'rgb(255,248,210)';
  ctx.lineWidth = 1.1 * u;
  for (const f of st.streaks ?? []) {
    const [ax, ay] = P(f.ax, f.ay);
    const [bx, by] = P(f.bx, f.by);
    if ((ax < x && bx < x) || (ax > x + size && bx > x + size) || (ay < y && by < y) || (ay > y + size && by > y + size)) continue;
    ctx.globalAlpha = st.alpha * Math.min(0.8, 0.1 + 0.6 * f.b);
    ctx.beginPath();
    ctx.moveTo(ax, ay);
    ctx.lineTo(bx, by);
    ctx.stroke();
  }
  ctx.globalAlpha = st.alpha;
  // the radar that sees the tornado, and the line to it (velocity panel)
  if (which === 'velocity' && st.bestRadar && tor?.cur) {
    const [rx, ry] = P(st.bestRadar.x, st.bestRadar.y);
    const [tx, ty] = P(tor.cur[0], tor.cur[1]);
    ctx.setLineDash([4 * u, 4 * u]);
    ctx.strokeStyle = 'rgba(160,215,255,0.8)';
    ctx.lineWidth = 1.2 * u;
    ctx.beginPath();
    ctx.moveTo(rx, ry);
    ctx.lineTo(tx, ty);
    ctx.stroke();
    ctx.setLineDash([]);
  }
  // radar rotation maximum: a ring, joined to the surveyed position by a thin line (the offset is in the footer)
  const at = tor?.cur ?? tor?.ghost ?? null;
  if (st.candidate && at) {
    const [qx, qy] = P(st.candidate.x, st.candidate.y);
    const [tx, ty] = P(at[0], at[1]);
    ctx.setLineDash([2 * u, 3 * u]);
    ctx.strokeStyle = 'rgba(255,235,90,0.9)';
    ctx.lineWidth = 1.2 * u;
    ctx.beginPath();
    ctx.moveTo(tx, ty);
    ctx.lineTo(qx, qy);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.beginPath();
    ctx.arc(qx, qy, 8 * u, 0, Math.PI * 2);
    ctx.strokeStyle = 'rgba(4,8,16,0.85)';
    ctx.lineWidth = 4.4 * u;
    ctx.stroke();
    ctx.strokeStyle = 'rgba(255,235,90,1)';
    ctx.lineWidth = 2.2 * u;
    ctx.stroke();
  }
  // the surveyed tornado
  if (tor?.cur) {
    const [tx, ty] = P(tor.cur[0], tor.cur[1]);
    ctx.beginPath();
    ctx.arc(tx, ty, 7 * u, 0, Math.PI * 2);
    ctx.strokeStyle = 'rgba(4,8,16,0.85)';
    ctx.lineWidth = 4 * u;
    ctx.stroke();
    ctx.strokeStyle = 'rgba(255,255,255,0.98)';
    ctx.lineWidth = 2 * u;
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(tx, ty, 2.6 * u, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(255,90,90,1)';
    ctx.fill();
    if (tor.headingDeg != null && !st.compact) {
      const a = (tor.headingDeg * Math.PI) / 180;
      arrow(ctx, tx + Math.sin(a) * 10 * u, ty - Math.cos(a) * 10 * u, tx + Math.sin(a) * 30 * u, ty - Math.cos(a) * 30 * u, u, 'rgba(255,255,255,0.95)');
    }
  }
  // the surveyed position before the touchdown and after the end of the track: an open ring
  if (!tor?.cur && tor?.ghost) {
    const [gx, gy] = P(tor.ghost[0], tor.ghost[1]);
    ctx.setLineDash([3 * u, 3 * u]);
    ctx.beginPath();
    ctx.arc(gx, gy, 7 * u, 0, Math.PI * 2);
    ctx.strokeStyle = 'rgba(255,255,255,0.9)';
    ctx.lineWidth = 1.8 * u;
    ctx.stroke();
    ctx.setLineDash([]);
  }
  // the camera and its view cone
  const [ox, oy] = P(st.camera.x, st.camera.y);
  const R = size * 0.4;
  const a0 = ((st.camera.yawDeg - st.camera.hfovDeg / 2) * Math.PI) / 180;
  const a1 = ((st.camera.yawDeg + st.camera.hfovDeg / 2) * Math.PI) / 180;
  ctx.fillStyle = which === 'echo' ? 'rgba(120,200,255,0.22)' : 'rgba(120,200,255,0.14)';
  ctx.strokeStyle = 'rgba(170,225,255,0.92)';
  ctx.lineWidth = 1.3 * u;
  ctx.beginPath();
  ctx.moveTo(ox, oy);
  ctx.lineTo(ox + Math.sin(a0) * R, oy - Math.cos(a0) * R);
  ctx.arc(ox, oy, R, a0 - Math.PI / 2, a1 - Math.PI / 2);
  ctx.lineTo(ox, oy);
  ctx.fill();
  ctx.stroke();
  ctx.fillStyle = 'rgb(127,208,255)';
  ctx.strokeStyle = 'rgba(5,8,16,0.9)';
  ctx.lineWidth = 2 * u;
  ctx.beginPath();
  ctx.arc(ox, oy, 4.4 * u, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();
  // the motion of the echo
  const hd = headingOf(st.motionMs[0], st.motionMs[1]);
  if (hd != null && which === 'echo') {
    const a = (hd * Math.PI) / 180;
    const bx = x + 26 * u;
    const by = y + size - (st.compact ? 22 : 40) * u;
    arrow(ctx, bx, by, bx + Math.sin(a) * 34 * u, by - Math.cos(a) * 34 * u, u, rgba(U.caution, 0.97));
    if (!st.compact) drawText(ctx, `echo ${Math.hypot(st.motionMs[0], st.motionMs[1]).toFixed(0)} m/s ${compass16(hd)}`, bx - 8 * u, by + 17 * u, u, { size: 11, weight: 700, halo: 2.6 });
  }
  // north arrow and scale bar (echo panel)
  if (which === 'echo') {
    const nx = x + size - 16 * u;
    const ny = y + 10 * u;
    ctx.fillStyle = 'rgba(255,255,255,0.95)';
    ctx.strokeStyle = 'rgba(4,8,16,0.8)';
    ctx.lineWidth = 1.2 * u;
    ctx.beginPath();
    ctx.moveTo(nx, ny);
    ctx.lineTo(nx + 5 * u, ny + 16 * u);
    ctx.lineTo(nx, ny + 12.5 * u);
    ctx.lineTo(nx - 5 * u, ny + 16 * u);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
    drawText(ctx, 'N', nx, ny + 30 * u, u, { size: 12, weight: 700, align: 'center', halo: 2.6 });
    const bar = scaleBarKm(st.half);
    const barPx = bar * sc;
    const sy = y + size - 11 * u;
    ctx.strokeStyle = 'rgba(4,8,16,0.8)';
    ctx.lineWidth = 4.4 * u;
    ctx.beginPath();
    ctx.moveTo(x + size - 10 * u - barPx, sy);
    ctx.lineTo(x + size - 10 * u, sy);
    ctx.stroke();
    ctx.strokeStyle = 'rgba(255,255,255,0.97)';
    ctx.lineWidth = 2.2 * u;
    ctx.stroke();
    drawText(ctx, `${bar} km`, x + size - 10 * u - barPx / 2, sy - 6 * u, u, { size: 11, weight: 700, align: 'center', halo: 2.6 });
  }
  ctx.restore();
  ctx.strokeStyle = rgba(U.line, 0.7);
  ctx.lineWidth = 1.2 * u;
  ctx.strokeRect(x, y, size, size);
}

/** The two panels; `st.x`, `st.y` is the top left of the first, the second is under it. Returns the y below the second. */
export function drawFocusInset(ctx: CanvasRenderingContext2D, st: FocusInsetState, vsrLegend: CanvasImageSource | null): number {
  if (st.alpha <= 0.02) return st.y;
  const { u, size } = st;
  const U = ui();
  const headH = (st.compact ? 22 : 28) * u;
  const ph = panelHeight(size, u, st.compact);
  ctx.save();
  ctx.globalAlpha = st.alpha;
  // panel 1: the echo
  let y0 = st.y;
  panel(ctx, st.x - 8 * u, y0 - 6 * u, size + 16 * u, ph, u, 0.66);
  drawText(ctx, st.compact ? 'Echo' : 'Echo, column maximum', st.x + 2 * u, y0 + 14 * u, u, { size: st.compact ? 12 : 14, weight: 700, halo: 0 });
  const tw = measure(ctx, st.tag.text, u, { size: 11, weight: 700, mono: true }) + 24 * u;
  chip(ctx, st.tag.kind, st.x + size - tw, y0 + 14 * u, u, st.tag.text, 11);
  drawMap(ctx, st, 'echo', st.x, y0 + headH - 8 * u);
  let fy = y0 + headH - 8 * u + size;
  if (!st.compact) {
    drawText(ctx, `${st.clock} UTC`, st.x + size - 2 * u, fy + 22 * u, u, { size: 15, weight: 700, mono: true, align: 'right', halo: 0 });
    drawText(ctx, 'ring: radar rotation maximum', st.x + 2 * u, fy + 20 * u, u, { size: 11, weight: 600, halo: 0, color: 'rgba(255,235,90,1)' });
    drawText(ctx, st.offsetText || 'no rotation maximum within 3 km', st.x + 2 * u, fy + 35 * u, u, { size: 11, weight: 500, halo: 0, color: rgba(U.dim, 1) });
  } else {
    drawText(ctx, st.offsetText ? `ring: rotation max, ${st.offsetText.replace(' of the surveyed position', '')}` : 'ring: radar rotation maximum', st.x + 2 * u, fy + 16 * u, u, { size: 10, weight: 600, halo: 0, color: 'rgba(255,235,90,1)' });
  }
  // panel 2: the storm-relative velocity
  y0 += ph + 10 * u;
  panel(ctx, st.x - 8 * u, y0 - 6 * u, size + 16 * u, ph, u, 0.66);
  drawText(ctx, st.compact ? 'Velocity' : 'Storm-relative velocity, 0.5 km', st.x + 2 * u, y0 + 14 * u, u, { size: st.compact ? 12 : 14, weight: 700, halo: 0 });
  const vt = 'OBSERVED';
  const vw = measure(ctx, vt, u, { size: 11, weight: 700, mono: true }) + 24 * u;
  chip(ctx, 'observed', st.x + size - vw, y0 + 14 * u, u, vt, 11);
  drawMap(ctx, st, 'velocity', st.x, y0 + headH - 8 * u);
  fy = y0 + headH - 8 * u + size;
  if (!st.compact) {
    const lw = size * 0.46;
    if (vsrLegend) ctx.drawImage(vsrLegend, st.x + 2 * u, fy + 8 * u, lw, 9 * u);
    drawText(ctx, 'toward', st.x + 2 * u, fy + 30 * u, u, { size: 11, weight: 600, halo: 0, color: rgba(U.dim, 1) });
    drawText(ctx, 'away from the radar', st.x + 2 * u + lw, fy + 30 * u, u, { size: 11, weight: 600, halo: 0, align: 'right', color: rgba(U.dim, 1) });
    const br = st.bestRadar;
    const tor = st.tornado?.cur;
    const dist = br && tor ? Math.hypot(br.x - tor[0], br.y - tor[1]) : null;
    drawText(ctx, br && dist != null ? `${br.name}, ${dist.toFixed(1)} km ${compass16((Math.atan2(br.x - (tor as [number, number])[0], br.y - (tor as [number, number])[1]) * 180) / Math.PI)} of the tornado` : 'radar not available', st.x + size - 2 * u, fy + 20 * u, u, { size: 11, weight: 500, halo: 0, align: 'right', color: rgba(U.dim, 1) });
    drawText(ctx, 'magenta: shear 0.012 s⁻¹', st.x + size - 2 * u, fy + 35 * u, u, { size: 11, weight: 500, halo: 0, align: 'right', color: 'rgba(255,120,235,1)' });
  } else {
    drawText(ctx, `${st.bestRadar ? st.bestRadar.name + ': ' : ''}green toward, red away from the radar`, st.x + 2 * u, fy + 16 * u, u, { size: 10, weight: 600, halo: 0, color: rgba(U.dim, 1) });
  }
  ctx.restore();
  return y0 + ph;
}

export interface RotSeriesState {
  x: number;
  y: number;
  w: number;
  h: number;
  u: number;
  alpha: number;
  /** Data window (s) and the surveyed life of the tornado (s), the data time now. */
  window: [number, number];
  life: [number, number] | null;
  td: number;
  /** Steps: time and shear (s-1), Vrot (m/s). */
  pts: { t: number; as: number | null; vrot: number | null }[];
  now: { as: number | null; vrot: number | null };
  /** Clock text of the window ends. */
  from: string;
  to: string;
}

/** The small time series: azimuthal shear of the radar rotation next to the surveyed life of the tornado. */
export function drawRotSeries(ctx: CanvasRenderingContext2D, s: RotSeriesState): void {
  if (s.alpha <= 0.02 || !s.pts.length) return;
  const { u } = s;
  const U = ui();
  ctx.save();
  ctx.globalAlpha = s.alpha;
  panel(ctx, s.x, s.y, s.w, s.h, u, 0.6);
  const px = s.x + 12 * u;
  const pw = s.w - 24 * u;
  const py = s.y + 38 * u;
  const ph = s.h - 74 * u;
  const T = (t: number) => px + ((t - s.window[0]) / (s.window[1] - s.window[0] || 1)) * pw;
  const lim = 0.03;
  const V = (v: number) => py + ph - (Math.min(v, lim) / lim) * ph;
  drawText(ctx, 'Radar rotation near the tornado', s.x + 12 * u, s.y + 19 * u, u, { size: 12, weight: 700, halo: 0 });
  chip(ctx, 'retrieved', s.x + s.w - 12 * u - 92 * u, s.y + 19 * u, u, 'RETRIEVED', 10);
  // the surveyed life
  if (s.life) {
    ctx.fillStyle = 'rgba(255,90,90,0.16)';
    ctx.fillRect(T(s.life[0]), py, T(s.life[1]) - T(s.life[0]), ph);
    ctx.fillStyle = 'rgba(255,90,90,0.9)';
    ctx.fillRect(T(s.life[0]), py + ph + 4 * u, T(s.life[1]) - T(s.life[0]), 3 * u);
    drawText(ctx, 'tornado on the ground (survey)', T(s.life[0]) + 2 * u, py + ph + 20 * u, u, { size: 10, weight: 600, halo: 0, color: 'rgba(255,170,160,1)' });
  }
  // the vortex threshold
  ctx.setLineDash([3 * u, 3 * u]);
  ctx.strokeStyle = rgba(U.line, 0.8);
  ctx.lineWidth = 1 * u;
  ctx.beginPath();
  ctx.moveTo(px, V(0.012));
  ctx.lineTo(px + pw, V(0.012));
  ctx.stroke();
  ctx.setLineDash([]);
  // shear
  const pts = s.pts.filter((p) => p.as != null);
  ctx.strokeStyle = 'rgba(255,225,70,0.95)';
  ctx.lineWidth = 2 * u;
  ctx.beginPath();
  pts.forEach((p, i) => (i ? ctx.lineTo(T(p.t), V(p.as as number)) : ctx.moveTo(T(p.t), V(p.as as number))));
  ctx.stroke();
  ctx.fillStyle = 'rgba(255,225,70,1)';
  for (const p of pts) {
    ctx.beginPath();
    ctx.arc(T(p.t), V(p.as as number), 2.4 * u, 0, Math.PI * 2);
    ctx.fill();
  }
  // now
  const nx = T(Math.max(s.window[0], Math.min(s.window[1], s.td)));
  ctx.strokeStyle = 'rgba(255,255,255,0.95)';
  ctx.lineWidth = 1.8 * u;
  ctx.beginPath();
  ctx.moveTo(nx, py - 3 * u);
  ctx.lineTo(nx, py + ph + 3 * u);
  ctx.stroke();
  drawText(ctx, s.from, px, s.y + s.h - 6 * u, u, { size: 10, weight: 600, mono: true, halo: 0, color: rgba(U.dim, 1) });
  drawText(ctx, s.to, px + pw, s.y + s.h - 6 * u, u, { size: 10, weight: 600, mono: true, halo: 0, align: 'right', color: rgba(U.dim, 1) });
  const txt = s.now.as != null ? `${s.now.as.toFixed(3)} s⁻¹${s.now.vrot != null ? ` · Vrot ${s.now.vrot.toFixed(0)} m/s` : ''}` : '-';
  drawText(ctx, txt, px + pw / 2, s.y + s.h - 6 * u, u, { size: 11, weight: 700, mono: true, halo: 0, align: 'center', color: 'rgba(255,225,70,1)' });
  ctx.restore();
}
