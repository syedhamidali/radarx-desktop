/**
 * Heads-up pieces of the observer ("window") clip drawn on the 2-D overlay: the compass strip of
 * the camera heading and the inset map (satellite image, the storm echo, the storm track, the
 * observer and the view cone). Drawing only; the numbers come from StormClip.
 */
import { bearingDiff, compassLabel } from './observerCam';

function outlined(ctx: CanvasRenderingContext2D, str: string, x: number, y: number, size: number, color: string, weight = 600, align: CanvasTextAlign = 'center', u = 1): void {
  ctx.font = `${weight} ${Math.round(size * u)}px system-ui, -apple-system, "Segoe UI", sans-serif`;
  ctx.textAlign = align;
  ctx.textBaseline = 'alphabetic';
  ctx.lineJoin = 'round';
  ctx.lineWidth = 4 * u;
  ctx.strokeStyle = 'rgba(5,8,16,0.8)';
  ctx.strokeText(str, x, y);
  ctx.fillStyle = color;
  ctx.fillText(str, x, y);
}

/** A strip at the top of the frame: the compass bearings that are in view, and the heading in the middle. */
export function drawCompassStrip(ctx: CanvasRenderingContext2D, w: number, h: number, yawDeg: number, tanX: number, u: number): void {
  const y0 = 22 * u;
  const stripW = Math.min(w * 0.46, 860 * u);
  const x0 = (w - stripW) / 2;
  ctx.save();
  ctx.fillStyle = 'rgba(8,12,22,0.42)';
  ctx.fillRect(x0, y0, stripW, 30 * u);
  ctx.beginPath();
  ctx.rect(x0, y0, stripW, 30 * u);
  ctx.clip();
  for (let b = Math.floor((yawDeg - 60) / 5) * 5; b <= yawDeg + 60; b += 5) {
    const d = bearingDiff(yawDeg, b);
    // a bearing's place on the screen (horizontal perspective)
    const x = w / 2 + (Math.tan((d * Math.PI) / 180) / tanX) * (w / 2);
    if (x < x0 || x > x0 + stripW) continue;
    const bb = ((b % 360) + 360) % 360;
    const major = bb % 45 === 0;
    ctx.strokeStyle = major ? 'rgba(255,255,255,0.95)' : 'rgba(255,255,255,0.45)';
    ctx.lineWidth = (major ? 2 : 1) * u;
    ctx.beginPath();
    ctx.moveTo(x, y0 + 30 * u);
    ctx.lineTo(x, y0 + (major ? 14 : 22) * u);
    ctx.stroke();
    if (major) outlined(ctx, compassLabel(bb), x, y0 + 12 * u, 15, 'rgba(255,255,255,0.97)', 700, 'center', u);
  }
  ctx.restore();
  ctx.fillStyle = 'rgba(255,214,90,0.95)';
  ctx.beginPath();
  ctx.moveTo(w / 2, y0 + 31 * u);
  ctx.lineTo(w / 2 - 6 * u, y0 + 41 * u);
  ctx.lineTo(w / 2 + 6 * u, y0 + 41 * u);
  ctx.closePath();
  ctx.fill();
  outlined(ctx, `${compassLabel(yawDeg)} ${Math.round(yawDeg)}°`, w / 2, y0 + 62 * u, 17, 'rgba(255,235,170,0.98)', 700, 'center', u);
}

export interface InsetState {
  /** Where the inset is drawn (px) and its size. */
  x: number;
  y: number;
  size: number;
  u: number;
  /** Km at the centre and half width of the map. */
  cx: number;
  cy: number;
  half: number;
  /** The satellite (or flat map) image as a size x size canvas; null = plain background. */
  base: CanvasImageSource | null;
  /** The storm echo, drawn over the map (a size x size RGBA canvas), or null. */
  echo: CanvasImageSource | null;
  track: [number, number][];
  /** Storm centre now and the part of the track already passed. */
  storm: [number, number];
  passed: number;
  observer: [number, number];
  yawDeg: number;
  hfovDeg: number;
  title: string;
  caption: string;
}

/** The inset map of the observer clip. */
export function drawInset(ctx: CanvasRenderingContext2D, s: InsetState): void {
  const { x, y, size, u } = s;
  const px = (km: number, c: number, flip: boolean) => {
    const f = (km - c) / (2 * s.half) + 0.5;
    return flip ? (1 - f) * size : f * size;
  };
  const X = (kx: number) => x + px(kx, s.cx, false);
  const Y = (ky: number) => y + px(ky, s.cy, true);
  ctx.save();
  ctx.fillStyle = 'rgba(8,12,22,0.75)';
  ctx.fillRect(x - 5 * u, y - 26 * u, size + 10 * u, size + 52 * u);
  ctx.beginPath();
  ctx.rect(x, y, size, size);
  ctx.clip();
  ctx.fillStyle = '#1c2230';
  ctx.fillRect(x, y, size, size);
  if (s.base) ctx.drawImage(s.base, x, y, size, size);
  if (s.echo) ctx.drawImage(s.echo, x, y, size, size);
  // the storm track: passed part solid, the rest dashed
  const pts = s.track.map(([kx, ky]) => [X(kx), Y(ky)] as const);
  if (pts.length > 1) {
    ctx.lineCap = 'round';
    ctx.strokeStyle = 'rgba(5,8,16,0.6)';
    ctx.lineWidth = 4.5 * u;
    ctx.beginPath();
    pts.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
    ctx.stroke();
    ctx.strokeStyle = 'rgba(255,255,255,0.55)';
    ctx.setLineDash([6 * u, 5 * u]);
    ctx.lineWidth = 2 * u;
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.strokeStyle = 'rgba(255,235,150,0.97)';
    ctx.lineWidth = 3 * u;
    ctx.beginPath();
    const n = Math.max(0, Math.min(pts.length - 1, s.passed));
    for (let i = 0; i <= n; i++) i ? ctx.lineTo(pts[i][0], pts[i][1]) : ctx.moveTo(pts[i][0], pts[i][1]);
    ctx.lineTo(X(s.storm[0]), Y(s.storm[1]));
    ctx.stroke();
  }
  // the view cone
  const ox = X(s.observer[0]), oy = Y(s.observer[1]);
  const R = size * 0.62;
  const a0 = ((s.yawDeg - s.hfovDeg / 2) * Math.PI) / 180, a1 = ((s.yawDeg + s.hfovDeg / 2) * Math.PI) / 180;
  ctx.fillStyle = 'rgba(120,200,255,0.22)';
  ctx.strokeStyle = 'rgba(160,220,255,0.9)';
  ctx.lineWidth = 1.4 * u;
  ctx.beginPath();
  ctx.moveTo(ox, oy);
  ctx.lineTo(ox + Math.sin(a0) * R, oy - Math.cos(a0) * R);
  ctx.arc(ox, oy, R, a0 - Math.PI / 2, a1 - Math.PI / 2);
  ctx.lineTo(ox, oy);
  ctx.fill();
  ctx.stroke();
  // observer and storm centre
  ctx.fillStyle = '#7fd0ff';
  ctx.strokeStyle = 'rgba(5,8,16,0.9)';
  ctx.lineWidth = 2 * u;
  ctx.beginPath();
  ctx.arc(ox, oy, 5.5 * u, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();
  const sx = X(s.storm[0]), sy = Y(s.storm[1]);
  ctx.beginPath();
  ctx.arc(sx, sy, 8 * u, 0, Math.PI * 2);
  ctx.lineWidth = 3.4 * u;
  ctx.strokeStyle = 'rgba(5,8,16,0.85)';
  ctx.stroke();
  ctx.lineWidth = 2 * u;
  ctx.strokeStyle = 'rgba(255,255,255,0.98)';
  ctx.stroke();
  // north arrow and a 10 km bar
  ctx.restore();
  outlined(ctx, 'N', x + size - 14 * u, y + 20 * u, 15, 'rgba(255,255,255,0.97)', 700, 'center', u);
  const barKm = s.half > 40 ? 20 : 10;
  const barPx = (barKm / (2 * s.half)) * size;
  ctx.strokeStyle = 'rgba(255,255,255,0.95)';
  ctx.lineWidth = 2.2 * u;
  ctx.beginPath();
  ctx.moveTo(x + 10 * u, y + size - 12 * u);
  ctx.lineTo(x + 10 * u + barPx, y + size - 12 * u);
  ctx.stroke();
  outlined(ctx, `${barKm} km`, x + 10 * u + barPx / 2, y + size - 18 * u, 13, 'rgba(255,255,255,0.97)', 600, 'center', u);
  outlined(ctx, s.title, x, y - 8 * u, 15, 'rgba(255,255,255,0.97)', 700, 'left', u);
  outlined(ctx, s.caption, x, y + size + 18 * u, 13, 'rgba(210,222,245,0.95)', 500, 'left', u);
}
