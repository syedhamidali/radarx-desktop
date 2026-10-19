/**
 * The plan-view inset of the PERiLS movie ("Where the camera is"), in every shot. Same idea as the inset of the
 * storm clips (clipHud.ts: view cone, storm track, scale bar, north arrow), built for this film:
 *
 *  - the merged column-maximum reflectivity of the radar volume at the clip time (the two observed steps around it,
 *    cross-faded: OBSERVED VOLUME on an observed step, INTERPOLATED between), with the colour scale of the 3-D scene,
 *    over the dimmed schematic map;
 *  - the camera position and its view cone (heading and field of view of the frame), the storm's motion arrow, radar
 *    sites with their names, the stations (the one nearest the camera ringed), the surveyed tornado path with the
 *    marker of the clip time (the path between the surveyed ends is a straight line: INTERPOLATED), the lightning
 *    sources of the last moments, and the radiosondes in flight as tiny dots with a faint trail (nothing written on them);
 *  - a scale bar, a north arrow and the clock.
 * The view follows the storm in the wide shots and the camera in the close ones. Pure drawing; the numbers come in
 * through `MinimapState`.
 */
import { drawText, measure, panel, rgba, ui, provColour, chip } from './perilsStyle.ts';
import type { ProvKind } from './perilsData.ts';

export interface MinimapEcho {
  /** Column-maximum images (north up, one pixel per grid cell) of the two steps around the time, and the weight of `b`. */
  a: CanvasImageSource | null;
  b: CanvasImageSource | null;
  s: number;
  /** Km of the west and south edges of the grid, cell size and cell counts. */
  x0: number;
  y0: number;
  dx: number;
  nx: number;
  ny: number;
}

export interface MinimapState {
  x: number;
  y: number;
  size: number;
  u: number;
  alpha: number;
  /** Km at the centre of the map and half of its width. */
  cx: number;
  cy: number;
  half: number;
  echo: MinimapEcho;
  /** The schematic base map (a square image of side 2 * half km centred on (cx, cy)), or null. */
  base: { img: CanvasImageSource; cx: number; cy: number; half: number } | null;
  camera: { x: number; y: number; yawDeg: number; hfovDeg: number };
  storm: { x: number; y: number; motionMs: [number, number] };
  radars: { name: string; x: number; y: number }[];
  stations: { x: number; y: number; near: boolean }[];
  tornado: { path: [number, number][]; cur: [number, number] | null } | null;
  flashes: { x: number; y: number; b: number }[];
  balloons: { x: number; y: number; trail: [number, number][]; a: number }[];
  tag: { kind: ProvKind; text: string };
  clock: string;
  title: string;
}

/** A round scale-bar length (km) for a map of half-width `half`: about a quarter of the map. */
export function scaleBarKm(half: number): number {
  const target = (2 * half) / 4;
  const steps = [1, 2, 5, 10, 20, 50, 100];
  let best = steps[0];
  for (const s of steps) if (Math.abs(s - target) < Math.abs(best - target)) best = s;
  return best;
}

/** Heading (degrees clockwise from north) of a motion vector (east, north), or null when it is (almost) still. */
export function headingOf(u: number, v: number): number | null {
  return Math.hypot(u, v) < 0.3 ? null : (Math.atan2(u, v) * 180 / Math.PI + 360) % 360;
}

/** Horizontal full field of view (degrees) from the half-width tangent of the frame. */
export function hfovFromTan(tanX: number): number {
  return (2 * Math.atan(tanX) * 180) / Math.PI;
}

export function drawMinimap(ctx: CanvasRenderingContext2D, s: MinimapState): void {
  if (s.alpha <= 0.02) return;
  const { x, y, size, u } = s;
  const U = ui();
  const sc = size / (2 * s.half); // px per km
  const X = (kx: number) => x + (kx - s.cx) * sc + size / 2;
  const Y = (ky: number) => y - (ky - s.cy) * sc + size / 2;
  const headH = 26 * u;
  const footH = 26 * u;
  ctx.save();
  ctx.globalAlpha = s.alpha;
  panel(ctx, x - 8 * u, y - headH - 6 * u, size + 16 * u, size + headH + footH + 14 * u, u, 0.66);
  // header: title and the provenance of the echo
  const compact = size < 300 * u;
  if (!compact) drawText(ctx, s.title, x + 2 * u, y - 10 * u, u, { size: 14, weight: 700, halo: 0 });
  chip(ctx, s.tag.kind, x + size - 4 * u - (measure(ctx, s.tag.text, u, { size: 11, weight: 700, mono: true }) + 24 * u), y - 8 * u, u, s.tag.text, 11);
  // the map
  ctx.save();
  ctx.beginPath();
  ctx.rect(x, y, size, size);
  ctx.clip();
  ctx.fillStyle = rgba(U.panel, 1);
  ctx.fillRect(x, y, size, size);
  if (s.base) {
    const b = s.base;
    ctx.globalAlpha = s.alpha * 0.55;
    ctx.drawImage(b.img, X(b.cx - b.half), Y(b.cy + b.half), 2 * b.half * sc, 2 * b.half * sc);
    ctx.globalAlpha = s.alpha;
  }
  const e = s.echo;
  const eW = e.nx * e.dx * sc;
  const eH = e.ny * e.dx * sc;
  const ex = X(e.x0 - e.dx / 2);
  const ey = Y(e.y0 + (e.ny - 0.5) * e.dx);
  ctx.imageSmoothingEnabled = true;
  if (e.a) {
    ctx.globalAlpha = s.alpha * (e.b ? 1 - e.s : 1) * 0.92;
    ctx.drawImage(e.a, ex, ey, eW, eH);
  }
  if (e.b) {
    ctx.globalAlpha = s.alpha * (e.a ? e.s : 1) * 0.92;
    ctx.drawImage(e.b, ex, ey, eW, eH);
  }
  ctx.globalAlpha = s.alpha;
  // the tornado path: faint dashed, travelled part solid, the marker of the clip time
  if (s.tornado && s.tornado.path.length > 1) {
    const pts = s.tornado.path.map(([kx, ky]) => [X(kx), Y(ky)] as const);
    ctx.lineCap = 'round';
    ctx.strokeStyle = 'rgba(4,8,16,0.7)';
    ctx.lineWidth = 4 * u;
    ctx.beginPath();
    pts.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
    ctx.stroke();
    ctx.strokeStyle = 'rgba(255,90,90,0.55)';
    ctx.setLineDash([5 * u, 4 * u]);
    ctx.lineWidth = 2 * u;
    ctx.stroke();
    ctx.setLineDash([]);
    if (s.tornado.cur) {
      ctx.strokeStyle = 'rgba(255,90,90,1)';
      ctx.lineWidth = 2.6 * u;
      ctx.beginPath();
      pts.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
      ctx.stroke();
    }
  }
  // the storm's motion arrow
  const hd = headingOf(s.storm.motionMs[0], s.storm.motionMs[1]);
  if (hd != null) {
    const sx = X(s.storm.x);
    const sy = Y(s.storm.y);
    const L = size * 0.2;
    const a = (hd * Math.PI) / 180;
    const ex2 = sx + Math.sin(a) * L;
    const ey2 = sy - Math.cos(a) * L;
    ctx.strokeStyle = 'rgba(4,8,16,0.75)';
    ctx.lineWidth = 5 * u;
    ctx.beginPath();
    ctx.moveTo(sx, sy);
    ctx.lineTo(ex2, ey2);
    ctx.stroke();
    ctx.strokeStyle = rgba(U.caution, 0.97);
    ctx.lineWidth = 2.4 * u;
    ctx.stroke();
    ctx.fillStyle = rgba(U.caution, 0.97);
    ctx.beginPath();
    ctx.moveTo(ex2 + Math.sin(a) * 7 * u, ey2 - Math.cos(a) * 7 * u);
    ctx.lineTo(ex2 + Math.sin(a + 2.4) * 6 * u, ey2 - Math.cos(a + 2.4) * 6 * u);
    ctx.lineTo(ex2 + Math.sin(a - 2.4) * 6 * u, ey2 - Math.cos(a - 2.4) * 6 * u);
    ctx.closePath();
    ctx.fill();
  }
  // radar sites
  for (const r of s.radars) {
    const rx = X(r.x);
    const ry = Y(r.y);
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
    drawText(ctx, r.name, rx + 6 * u, ry - 3 * u, u, { size: 10, weight: 600, color: 'rgba(160,215,255,1)', halo: 2.4 });
  }
  // stations
  for (const st of s.stations) {
    const px = X(st.x);
    const py = Y(st.y);
    if (px < x || px > x + size || py < y || py > y + size) continue;
    ctx.beginPath();
    ctx.arc(px, py, (st.near ? 3.4 : 2.1) * u, 0, Math.PI * 2);
    ctx.fillStyle = rgba(provColour('observed'), st.near ? 1 : 0.8);
    ctx.fill();
    if (st.near) {
      ctx.beginPath();
      ctx.arc(px, py, 6.5 * u, 0, Math.PI * 2);
      ctx.strokeStyle = 'rgba(255,255,255,0.95)';
      ctx.lineWidth = 1.6 * u;
      ctx.stroke();
    }
  }
  // radiosondes in flight: a tiny dot and a faint trail, nothing written
  for (const b of s.balloons) {
    ctx.globalAlpha = s.alpha * b.a;
    if (b.trail.length > 1) {
      ctx.strokeStyle = 'rgba(235,240,250,0.45)';
      ctx.lineWidth = 1 * u;
      ctx.beginPath();
      b.trail.forEach(([kx, ky], i) => (i ? ctx.lineTo(X(kx), Y(ky)) : ctx.moveTo(X(kx), Y(ky))));
      ctx.stroke();
    }
    ctx.beginPath();
    ctx.arc(X(b.x), Y(b.y), 2.2 * u, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(240,244,252,1)';
    ctx.fill();
  }
  ctx.globalAlpha = s.alpha;
  // lightning sources of the last moments
  for (const f of s.flashes) {
    ctx.globalAlpha = s.alpha * Math.min(1, f.b);
    ctx.fillStyle = 'rgba(255,248,210,1)';
    ctx.beginPath();
    ctx.arc(X(f.x), Y(f.y), 1.7 * u, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.globalAlpha = s.alpha;
  // the surveyed tornado marker
  if (s.tornado?.cur) {
    const tx = X(s.tornado.cur[0]);
    const ty = Y(s.tornado.cur[1]);
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
  }
  // the camera and its view cone
  const ox = X(s.camera.x);
  const oy = Y(s.camera.y);
  const R = size * 0.46;
  const a0 = ((s.camera.yawDeg - s.camera.hfovDeg / 2) * Math.PI) / 180;
  const a1 = ((s.camera.yawDeg + s.camera.hfovDeg / 2) * Math.PI) / 180;
  ctx.fillStyle = 'rgba(120,200,255,0.2)';
  ctx.strokeStyle = 'rgba(170,225,255,0.92)';
  ctx.lineWidth = 1.4 * u;
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
  ctx.arc(ox, oy, 4.6 * u, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();
  ctx.restore();
  // frame
  ctx.strokeStyle = rgba(U.line, 0.7);
  ctx.lineWidth = 1.2 * u;
  ctx.strokeRect(x, y, size, size);
  // north arrow (top left) and the scale bar (bottom left)
  const nx = x + 16 * u;
  const ny = y + 12 * u;
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
  drawText(ctx, 'N', nx, ny + 31 * u, u, { size: 12, weight: 700, align: 'center', halo: 2.6 });
  const bar = scaleBarKm(s.half);
  const barPx = bar * sc;
  const by = y + size - 12 * u;
  ctx.strokeStyle = 'rgba(4,8,16,0.8)';
  ctx.lineWidth = 4.4 * u;
  ctx.beginPath();
  ctx.moveTo(x + 10 * u, by);
  ctx.lineTo(x + 10 * u + barPx, by);
  ctx.stroke();
  ctx.strokeStyle = 'rgba(255,255,255,0.97)';
  ctx.lineWidth = 2.2 * u;
  ctx.stroke();
  drawText(ctx, `${bar} km`, x + 10 * u + barPx / 2, by - 6 * u, u, { size: 11, weight: 700, align: 'center', halo: 2.6 });
  // footer: the clock and what the symbols are
  drawText(ctx, `${s.clock} UTC`, x + size - 2 * u, y + size + 19 * u, u, { size: 14, weight: 700, mono: true, align: 'right', halo: 0 });
  if (!compact) drawText(ctx, 'column-max echo · tornado: survey', x + 2 * u, y + size + 19 * u, u, { size: 11, weight: 500, halo: 0, color: rgba(U.dim, 1) });
  ctx.restore();
}
