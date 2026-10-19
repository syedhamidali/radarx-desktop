/**
 * The heads-up display of the PERiLS movie (canvas, 1080p layout scaled by `u = height / 1080`).
 *
 * Layout (the 3-D scene is the star; insets are small and fade with their relevance in the shot):
 *   top left      title, UTC clock, speed-up, observed / interpolated status with radar time offsets
 *   top centre    compass strip of the camera heading
 *   top right     flash counter and time series (LMA), environment panel; the Skew-T / hodograph DOM
 *                 inset sits below them
 *   left          tornado card (while a surveyed tornado is active)
 *   bottom left   nearest-station card (rain rate, Dm, wind, temperature, pressure + strip chart)
 *   bottom centre caption strip ("what you see" and the science caption), timeline bar, legend, credits
 */
import {
  type PerilsData, type ProvKind, type Station, type Readout, clockUtc, fmtOffset, compass16,
} from './perilsData.ts';
import type { ParentCirculation } from './perilsVortex.ts';
import { PROV, chip, drawText, fit, measure, panel, provColour, rgba, ui } from './perilsStyle.ts';
import { wrapText } from './perilsCaptions.ts';
import { tempColour } from './perilsSurface.ts';
import { COLORMAP_DATA } from '../utils/colormaps';

export interface TornadoCard {
  rating: string;
  active: boolean;
  widthM: number | null;
  /** The surveyed position between begin and end is a straight line (INTERPOLATED). */
  interpolated: boolean;
  /** What the radars measured: the parent circulation (the couplet), never the tornado itself. */
  parent: ParentCirculation | null;
  /** Estimated spin of the funnel from the survey: wind range (m/s), period range (s), the animated period (s). */
  windRangeMs: [number, number] | null;
  periodRangeS: [number, number] | null;
  periodS: number | null;
  /** Debris-signature cells (flag 2) near the vortex at the observed step nearest in time: weak evidence only. */
  debris: boolean | null;
  /** Finest effective resolution of the radars (m). */
  resolutionM: number | null;
}

export interface HudModel {
  data: PerilsData;
  title: string;
  subtitle: string;
  td: number;
  tc: number;
  duration: number;
  speedup: number;
  shotLabel: string;
  /** Index of the observed step if the frame is one, else -1; the interval otherwise. */
  observed: number;
  interval: { k: number; s: number };
  motionNote: string;
  flash: { perMin: number; series: number[]; binS: number; total: number; nowIdx: number };
  station: { st: Station; ro: Readout; spark: { t: number[]; v: (number | null)[] } | null } | null;
  tornado: TornadoCard | null;
  caption: { title: string; numbers: string; text: string; prov: ProvKind; alpha: number } | null;
  sees: string;
  vis: (id: string) => number;
  yawDeg: number;
  tanX: number;
  env: EnvPanel | null;
  legendKinds: ProvKind[];
  credits: string;
  shots: { name: string; label: string; t0: number; t1: number }[];
  layersOn: string[];
  /** What the colours on the ground mean (only the layers that are visible and have data). */
  groundKey: { temp: [number, number] | null; mrms: boolean; swath: boolean };
  vexag: number;
  /** 0..1: the immersive grade of the tornado shot. */
  immersion: number;
}

function line(ctx: CanvasRenderingContext2D, x0: number, y0: number, x1: number, y1: number, w: number, col: string): void {
  ctx.strokeStyle = col;
  ctx.lineWidth = w;
  ctx.beginPath();
  ctx.moveTo(x0, y0);
  ctx.lineTo(x1, y1);
  ctx.stroke();
}

/** A compact compass strip: the bearings in view, the heading in the middle. */
export function drawCompass(ctx: CanvasRenderingContext2D, w: number, u: number, yaw: number, tanX: number): void {
  const y0 = 22 * u;
  const stripW = Math.min(w * 0.3, 560 * u);
  const x0 = (w - stripW) / 2;
  const U = ui();
  panel(ctx, x0, y0, stripW, 28 * u, u, 0.4);
  ctx.save();
  ctx.beginPath();
  ctx.rect(x0, y0, stripW, 28 * u);
  ctx.clip();
  for (let b = Math.floor((yaw - 60) / 5) * 5; b <= yaw + 60; b += 5) {
    const d = (((b - yaw) % 360) + 540) % 360 - 180;
    const x = w / 2 + (Math.tan((d * Math.PI) / 180) / tanX) * (w / 2);
    if (x < x0 || x > x0 + stripW) continue;
    const bb = ((b % 360) + 360) % 360;
    const major = bb % 45 === 0;
    line(ctx, x, y0 + 28 * u, x, y0 + (major ? 13 : 21) * u, (major ? 2 : 1) * u, rgba(U.text, major ? 0.95 : 0.45));
    if (major) drawText(ctx, compass16(bb), x, y0 + 12 * u, u, { size: 14, weight: 700, align: 'center', halo: 3 });
  }
  ctx.restore();
  ctx.fillStyle = rgba(U.caution, 0.95);
  ctx.beginPath();
  ctx.moveTo(w / 2, y0 + 29 * u);
  ctx.lineTo(w / 2 - 5 * u, y0 + 37 * u);
  ctx.lineTo(w / 2 + 5 * u, y0 + 37 * u);
  ctx.closePath();
  ctx.fill();
  drawText(ctx, `${compass16(yaw)} ${Math.round(((yaw % 360) + 360) % 360)}°`, w / 2, y0 + 56 * u, u, { size: 15, align: 'center', color: rgba(U.caution, 1) });
}

/** Title, clock, speed-up and the observed / interpolated status. */
export function drawTopLeft(ctx: CanvasRenderingContext2D, u: number, m: HudModel): void {
  const U = ui();
  const x = 34 * u;
  let y = 52 * u;
  drawText(ctx, m.title, x, y, u, { size: 34, weight: 700 });
  y += 26 * u;
  drawText(ctx, m.subtitle, x, y, u, { size: 17, weight: 500, color: rgba(U.dim, 1) });
  y += 62 * u;
  drawText(ctx, `${clockUtc(m.data.time0, m.td)} UTC`, x, y, u, { size: 54, weight: 700, mono: true });
  y += 28 * u;
  const date = new Date(new Date(m.data.time0).getTime() + m.td * 1000);
  const realTime = m.speedup <= 1.3;
  drawText(ctx, `${date.toUTCString().slice(5, 16)} · ${realTime ? `REAL TIME ×${m.speedup.toFixed(1)} (not time-lapse)` : `time-lapse ×${m.speedup.toFixed(1)}`}`, x, y, u, { size: 18, weight: realTime ? 700 : 500, color: realTime ? rgba(U.caution, 1) : rgba(U.dim, 1) });
  y += 14 * u;
  // status
  const stepsT = m.data.steps.map((s) => s.t_s);
  if (m.observed >= 0) {
    const st = m.data.steps[m.observed];
    chip(ctx, 'observed', x, y + 20 * u, u, 'OBSERVED VOLUME', 14);
    const radars = st.radars.map((r) => `${r} ${fmtOffset(st.offsets_s[r] ?? 0)}`).join('  ');
    for (const [i, ln] of wrapText(radars, 54).entries()) drawText(ctx, ln, x, y + (44 + i * 19) * u, u, { size: 15, weight: 500, mono: true, color: rgba(U.text, 0.92) });
  } else {
    chip(ctx, 'interpolated', x, y + 20 * u, u, 'INTERPOLATED', 14);
    const k = Math.max(0, Math.min(stepsT.length - 2, m.interval.k));
    drawText(ctx, `between observed volumes ${clockUtc(m.data.time0, stepsT[k])} and ${clockUtc(m.data.time0, stepsT[k + 1])}`, x, y + 44 * u, u, { size: 15, weight: 500, color: rgba(U.text, 0.92) });
    drawText(ctx, m.motionNote, x, y + 63 * u, u, { size: 14, weight: 500, color: rgba(U.dim, 1) });
  }
}

/** Flash counter and a small time series of the real LMA flashes. */
export function drawLmaPanel(ctx: CanvasRenderingContext2D, W: number, u: number, m: HudModel, a: number, y0 = 28 * u): number {
  if (a <= 0.02) return y0;
  const w = 330 * u;
  const h = 128 * u;
  const x = W - 34 * u - w;
  const y = y0;
  ctx.save();
  ctx.globalAlpha = a;
  panel(ctx, x, y, w, h, u);
  chip(ctx, 'observed', x + 12 * u, y + 30 * u, u, 'OBSERVED · LMA', 13);
  drawText(ctx, 'lightning mapping array', x + w - 12 * u, y + 24 * u, u, { size: 13, weight: 500, align: 'right', color: rgba(ui().dim, 1), halo: 0 });
  drawText(ctx, `${m.flash.perMin}`, x + 14 * u, y + 76 * u, u, { size: 40, weight: 700, mono: true, halo: 0 });
  drawText(ctx, `flashes in the last minute`, x + 14 * u + measure(ctx, `${m.flash.perMin}`, u, { size: 40, weight: 700, mono: true }) + 8 * u, y + 76 * u, u, { size: 14, weight: 500, color: rgba(ui().dim, 1), halo: 0 });
  // sparkline
  const sx = x + 14 * u;
  const sy = y + 92 * u;
  const sw = w - 28 * u;
  const sh = 28 * u;
  const s = m.flash.series;
  const mx = Math.max(1, ...s);
  ctx.fillStyle = rgba(provColour('observed'), 0.75);
  const bw = sw / Math.max(1, s.length);
  s.forEach((v, i) => {
    const bh = (v / mx) * sh;
    ctx.fillRect(sx + i * bw, sy + sh - bh, Math.max(1, bw - 1), bh);
  });
  const nx = sx + ((m.flash.nowIdx + 0.5) / Math.max(1, s.length)) * sw;
  line(ctx, nx, sy - 3 * u, nx, sy + sh + 2 * u, 2 * u, rgba(ui().text, 0.95));
  ctx.restore();
  return y + h + 10 * u;
}

/** A small panel of labelled numbers with its provenance chip. */
export interface EnvPanel {
  title: string;
  prov: ProvKind;
  rows: [string, string][];
  note?: string;
}

/** The environment of the storm (the radiosonde nearest in time and place, numbers from the engine's diagnostics), small. */
export function drawEnvPanel(ctx: CanvasRenderingContext2D, W: number, u: number, y: number, env: EnvPanel | null, a: number): number {
  if (a <= 0.02 || !env || !env.rows.length) return y;
  const w = 330 * u;
  const h = (50 + env.rows.length * 21 + (env.note ? 18 : 0)) * u;
  const x = W - 34 * u - w;
  ctx.save();
  ctx.globalAlpha = a;
  panel(ctx, x, y, w, h, u);
  chip(ctx, env.prov, x + 12 * u, y + 30 * u, u, PROV[env.prov].short, 12);
  drawText(ctx, fit(ctx, env.title, w - 140 * u, u, { size: 13, weight: 600 }), x + w - 12 * u, y + 26 * u, u, { size: 13, weight: 600, align: 'right', halo: 0, color: rgba(ui().dim, 1) });
  env.rows.forEach(([k, v], i) => {
    drawText(ctx, k, x + 14 * u, y + (58 + i * 21) * u, u, { size: 15, weight: 500, color: rgba(ui().dim, 1), halo: 0 });
    drawText(ctx, v, x + w - 14 * u, y + (58 + i * 21) * u, u, { size: 15, weight: 700, mono: true, align: 'right', halo: 0 });
  });
  if (env.note) drawText(ctx, env.note, x + 14 * u, y + h - 10 * u, u, { size: 11, weight: 500, color: rgba(ui().muted, 1), halo: 0 });
  ctx.restore();
  return y + h + 10 * u;
}

/** The station nearest the camera: live read-outs and a small rain-rate strip with a "now" line. */
export function drawStationCard(ctx: CanvasRenderingContext2D, H: number, u: number, m: HudModel, a: number): void {
  if (a <= 0.02 || !m.station) return;
  const { st, ro, spark } = m.station;
  const w = 360 * u;
  const h = 176 * u;
  const x = 34 * u;
  const y = H - 200 * u - h;
  ctx.save();
  ctx.globalAlpha = a;
  panel(ctx, x, y, w, h, u);
  chip(ctx, 'observed', x + 12 * u, y + 30 * u, u, `OBSERVED · ${st.type || 'station'}`, 13);
  drawText(ctx, fit(ctx, st.name, w - 28 * u, u, { size: 17, weight: 700 }), x + 14 * u, y + 56 * u, u, { size: 17, weight: 700, halo: 0 });
  const f = (v: number | null, d = 1, unit = '') => (v == null ? '–' : `${v.toFixed(d)}${unit}`);
  const col = (i: number, j: number) => ({ x: x + 14 * u + i * 118 * u, y: y + (80 + j * 40) * u });
  const cells: [string, string][] = [
    ['rain', f(ro.rain, 1, ' mm/h')], ['Dm', f(ro.dm, 2, ' mm')], ['dBZ', f(ro.dbz, 0)],
    ['wind', ro.windMs == null ? '–' : `${ro.windMs.toFixed(1)} m/s${ro.windDir != null ? ` ${compass16(ro.windDir)}` : ''}`], ['T', f(ro.tempC, 1, ' °C')], ['p', f(ro.pressHpa, 1, ' hPa')],
  ];
  cells.forEach(([k, v], i) => {
    const p = col(i % 3, Math.floor(i / 3));
    drawText(ctx, k, p.x, p.y, u, { size: 12, weight: 500, color: rgba(ui().muted, 1), halo: 0 });
    drawText(ctx, v, p.x, p.y + 15 * u, u, { size: 14, weight: 700, mono: true, halo: 0 });
  });
  if (spark && spark.t.length > 1) {
    const sx = x + 14 * u;
    const sy = y + h - 14 * u;
    const sw = w - 28 * u;
    const sh = 22 * u;
    const vals = spark.v.map((v) => (v == null ? 0 : v));
    const mx = Math.max(1, ...vals);
    const t0 = spark.t[0];
    const t1 = spark.t[spark.t.length - 1];
    ctx.beginPath();
    spark.t.forEach((t, i) => {
      const px = sx + ((t - t0) / (t1 - t0)) * sw;
      const py = sy - (vals[i] / mx) * sh;
      if (i === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
    });
    ctx.strokeStyle = rgba([120, 190, 255], 0.95);
    ctx.lineWidth = 1.8 * u;
    ctx.stroke();
    const nx = sx + Math.max(0, Math.min(1, (m.td - t0) / (t1 - t0))) * sw;
    line(ctx, nx, sy - sh - 2 * u, nx, sy + 2 * u, 2 * u, rgba(ui().text, 0.95));
  }
  ctx.restore();
}

/**
 * The tornado card (wide shots): what the survey says, what the radars measured (the parent circulation) and that the
 * tornado itself is unresolved, the debris evidence (weak) and the estimated spin of the stylised funnel.
 */
export function drawTornadoCard(ctx: CanvasRenderingContext2D, u: number, m: HudModel, a: number, y: number): number {
  const t = m.tornado;
  if (a <= 0.02 || !t || !t.active) return 0;
  const w = 392 * u;
  const par = t.parent;
  const rows: [string, string, ProvKind][] = [
    ['survey', `${t.rating}${t.widthM ? ` · damage width ${Math.round(t.widthM)} m` : ''}`, 'observed'],
    [
      'radar: parent circulation',
      par && par.medianVrotMs != null
        ? `Vrot ${par.medianVrotMs.toFixed(0)} m/s${par.vrotRange ? ` (${par.vrotRange[0].toFixed(0)}-${par.vrotRange[1].toFixed(0)})` : ''}, couplet ${par.medianDiameterKm?.toFixed(1) ?? '–'} km`
        : 'no radar couplet at this time',
      'retrieved',
    ],
    ['tornado vortex', `unresolved: ${t.widthM ? `${Math.round(t.widthM)} m` : 'narrow'} wide, grid ${t.resolutionM ? Math.round(t.resolutionM) : 500} m`, 'retrieved'],
    ['debris signature', t.debris == null ? 'not assessed' : t.debris ? 'possible (weak evidence)' : 'none flagged', 'retrieved'],
    ['funnel spin', t.periodRangeS ? `one turn per ${t.periodRangeS[0].toFixed(0)}-${t.periodRangeS[1].toFixed(0)} s: an estimate` : 'not estimated', 'stylised'],
  ];
  const h = (46 + rows.length * 38) * u;
  const x = 34 * u;
  ctx.save();
  ctx.globalAlpha = a;
  panel(ctx, x, y, w, h, u);
  drawText(ctx, `${t.rating} tornado`, x + 14 * u, y + 32 * u, u, { size: 22, weight: 700, color: rgba([255, 120, 110], 1), halo: 0 });
  if (t.interpolated) chip(ctx, 'interpolated', x + w - 14 * u - 150 * u, y + 30 * u, u, 'POSITION INTERPOLATED', 11);
  rows.forEach(([k, v, kind], i) => {
    const yy = y + (58 + i * 38) * u;
    drawText(ctx, k, x + 14 * u, yy, u, { size: 12, weight: 500, color: rgba(ui().muted, 1), halo: 0 });
    drawText(ctx, fit(ctx, v, w - 44 * u, u, { size: 15, weight: 600 }), x + 14 * u, yy + 17 * u, u, { size: 15, weight: 600, halo: 0 });
    ctx.fillStyle = rgba(provColour(kind), 1);
    ctx.beginPath();
    ctx.arc(x + w - 18 * u, yy + 10 * u, 4 * u, 0, Math.PI * 2);
    ctx.fill();
  });
  ctx.restore();
  return h;
}

/**
 * The one lower third of the immersive tornado shot: what the viewer sees, how it is paced and what is measured. It
 * fades down (not out) after a few seconds so that the view is left alone, while the honesty line stays.
 */
export function drawTornadoLowerThird(ctx: CanvasRenderingContext2D, H: number, u: number, m: HudModel, a: number): void {
  const t = m.tornado;
  if (a <= 0.02 || !t || !t.active) return;
  const par = t.parent;
  const real = m.speedup <= 1.3;
  const lines = [
    t.periodRangeS
      ? `Funnel: one turn per ${t.periodS ? t.periodS.toFixed(0) : t.periodRangeS[0].toFixed(0)} s, counter-clockwise from above. ESTIMATED from the survey (${t.rating} wind ${t.windRangeMs ? `${t.windRangeMs[0].toFixed(0)}-${t.windRangeMs[1].toFixed(0)} m/s` : ''}, ${t.widthM ? Math.round(t.widthM) : '–'} m wide).`
      : 'Funnel: spin not estimated.',
    `Radar measured only the parent circulation${par && par.medianVrotMs != null ? `: Vrot ${par.medianVrotMs.toFixed(0)} m/s, couplet ${par.medianDiameterKm?.toFixed(1)} km` : ''}; the tornado is unresolved (grid ${t.resolutionM ? Math.round(t.resolutionM) : 500} m).`,
    `Position from the survey (straight line, INTERPOLATED)${t.debris ? '; possible debris signature (weak evidence)' : ''}. Funnel and debris: STYLISED.`,
  ];
  const w = 860 * u;
  const h = (54 + lines.length * 20) * u;
  const x = 34 * u;
  const y = H - 172 * u - h;
  ctx.save();
  ctx.globalAlpha = a;
  panel(ctx, x, y, w, h, u, 0.62);
  drawText(ctx, `${t.rating} tornado`, x + 14 * u, y + 30 * u, u, { size: 21, weight: 700, color: rgba([255, 120, 110], 1), halo: 0 });
  let cx = x + 14 * u + measure(ctx, `${t.rating} tornado`, u, { size: 21, weight: 700 }) + 14 * u;
  cx += chip(ctx, 'stylised', cx, y + 28 * u, u, 'STYLISED FUNNEL', 11) + 8 * u;
  if (real) {
    ctx.fillStyle = rgba(ui().caution, 0.95);
    const label = `REAL TIME ×${m.speedup.toFixed(1)}`;
    const lw = measure(ctx, label, u, { size: 12, weight: 800, mono: true }) + 16 * u;
    ctx.beginPath();
    ctx.roundRect(cx, y + 14 * u, lw, 22 * u, 11 * u);
    ctx.fill();
    drawText(ctx, label, cx + 8 * u, y + 30 * u, u, { size: 12, weight: 800, mono: true, color: 'rgba(14,16,24,1)', halo: 0 });
  } else {
    drawText(ctx, `time-lapse ×${m.speedup.toFixed(1)}`, cx, y + 30 * u, u, { size: 13, weight: 600, color: rgba(ui().dim, 1), halo: 0 });
  }
  lines.forEach((ln, i) => {
    drawText(ctx, fit(ctx, ln, w - 28 * u, u, { size: 13, weight: 500 }), x + 14 * u, y + (56 + i * 20) * u, u, { size: 13, weight: 500, halo: 0, color: rgba(ui().text, 0.95) });
  });
  ctx.restore();
}

/** The caption strip: the feature in view (data-derived label) and the science caption of the latest event. */
export function drawCaption(ctx: CanvasRenderingContext2D, W: number, H: number, u: number, m: HudModel): void {
  const a = m.vis('captions');
  if (a <= 0.02) return;
  const cx = W / 2;
  const w = Math.min(W * 0.56, 1060 * u);
  const baseY = H - 150 * u;
  ctx.save();
  ctx.globalAlpha = a;
  if (m.caption && m.caption.alpha > 0.02) {
    const c = m.caption;
    const data = c.numbers ? wrapText(c.numbers, 112) : [];
    const lines = wrapText(c.text, 112);
    const h = (44 + (data.length + lines.length) * 21 + (data.length && lines.length ? 4 : 0)) * u;
    ctx.globalAlpha = a * c.alpha;
    panel(ctx, cx - w / 2, baseY - h, w, h, u, 0.7);
    const cw = chip(ctx, c.prov, cx - w / 2 + 14 * u, baseY - h + 32 * u, u, undefined, 13);
    drawText(ctx, c.title, cx - w / 2 + 14 * u + cw + 12 * u, baseY - h + 28 * u, u, { size: 20, weight: 700, halo: 0 });
    let yy = baseY - h + 54 * u;
    data.forEach((ln) => {
      drawText(ctx, ln, cx - w / 2 + 14 * u, yy, u, { size: 15, weight: 600, halo: 0, color: rgba(ui().caution, 1) });
      yy += 21 * u;
    });
    if (data.length && lines.length) yy += 4 * u;
    lines.forEach((ln) => {
      drawText(ctx, ln, cx - w / 2 + 14 * u, yy, u, { size: 15, weight: 500, halo: 0, color: rgba(ui().text, 0.95) });
      yy += 21 * u;
    });
  } else if (m.sees) {
    ctx.globalAlpha = a * 0.95;
    drawText(ctx, m.sees, cx, baseY - 6 * u, u, { size: 18, weight: 600, align: 'center', color: rgba(ui().text, 0.95) });
  }
  ctx.restore();
}

/** Timeline: the data window, the observed volumes (ticks), the shots (segments) and the playhead. */
export function drawTimeline(ctx: CanvasRenderingContext2D, W: number, H: number, u: number, m: HudModel): void {
  const bw = Math.min(W * 0.5, 960 * u);
  const bx = (W - bw) / 2;
  const by = H - 118 * u;
  const win = m.data.steps[m.data.steps.length - 1].t_s - m.data.steps[0].t_s || 1;
  const px = (t: number) => bx + (t / win) * bw;
  const U = ui();
  ctx.save();
  ctx.fillStyle = rgba(U.line, 0.28);
  ctx.fillRect(bx, by, bw, 4 * u);
  ctx.fillStyle = rgba(U.accent, 0.95);
  ctx.fillRect(bx, by, px(m.td) - bx, 4 * u);
  m.data.steps.forEach((s, i) => {
    const on = i === m.observed;
    ctx.beginPath();
    ctx.arc(px(s.t_s), by + 2 * u, (on ? 6.5 : 3.5) * u, 0, Math.PI * 2);
    ctx.fillStyle = on ? rgba(provColour('observed'), 1) : s.t_s <= m.td ? rgba(U.accent, 1) : rgba(U.text, 0.7);
    ctx.fill();
  });
  ctx.beginPath();
  ctx.arc(px(m.td), by + 2 * u, 7.5 * u, 0, Math.PI * 2);
  ctx.fillStyle = '#fff';
  ctx.fill();
  drawText(ctx, clockUtc(m.data.time0, 0), bx, by - 10 * u, u, { size: 14, weight: 600, mono: true, color: rgba(U.dim, 1) });
  drawText(ctx, clockUtc(m.data.time0, win), bx + bw, by - 10 * u, u, { size: 14, weight: 600, mono: true, align: 'right', color: rgba(U.dim, 1) });
  drawText(ctx, m.shotLabel, bx + bw / 2, by - 10 * u, u, { size: 15, weight: 600, align: 'center', color: rgba(U.text, 0.9) });
  ctx.restore();
}

/** Provenance legend and the credits line. */
export function drawLegend(ctx: CanvasRenderingContext2D, W: number, H: number, u: number, m: HudModel): void {
  const kinds = m.legendKinds;
  let total = 0;
  for (const k of kinds) total += measure(ctx, PROV[k].short, u, { size: 12, weight: 700, mono: true }) + 24 * u + 8 * u;
  let x = (W - total) / 2;
  const y = H - 60 * u;
  for (const k of kinds) x += chip(ctx, k, x, y, u, undefined, 12) + 8 * u;
  drawText(ctx, fit(ctx, m.credits, W * 0.9, u, { size: 12, weight: 500 }), W / 2, H - 22 * u, u, { size: 12, weight: 500, align: 'center', color: rgba(ui().dim, 0.95), halo: 3 });
  drawText(ctx, `Vertical scale ×${m.vexag.toFixed(1)} · flat ground, no terrain · cloud look and lighting STYLISED, echo position and strength are radar data`, W / 2, H - 40 * u, u, { size: 12, weight: 500, align: 'center', color: rgba(ui().dim, 0.95), halo: 3 });
}

/**
 * The key of the ground colours: surface temperature (ANALYSIS), MRMS reflectivity outside the radar volume
 * (OBSERVED), and the rotation swath (RETRIEVED). Only the layers that are on and have data get a row.
 */
export function drawGroundKey(ctx: CanvasRenderingContext2D, u: number, m: HudModel, y: number, a: number): void {
  const k = m.groundKey;
  const rows = [k.temp && 'temp', k.mrms && 'mrms', k.swath && 'swath'].filter(Boolean) as string[];
  if (!rows.length || a <= 0.02) return;
  const w = 330 * u;
  const h = (14 + rows.length * 40) * u;
  const x = 34 * u;
  ctx.save();
  ctx.globalAlpha = a;
  panel(ctx, x, y, w, h, u, 0.5);
  rows.forEach((r, i) => {
    const yy = y + (30 + i * 40) * u;
    const bx = x + 14 * u;
    const bw = 120 * u;
    if (r === 'temp' && k.temp) {
      const g = ctx.createLinearGradient(bx, 0, bx + bw, 0);
      for (let q = 0; q <= 4; q++) {
        const c = tempColour(k.temp[0] + ((k.temp[1] - k.temp[0]) * q) / 4, k.temp[0], k.temp[1]);
        g.addColorStop(q / 4, `rgb(${c[0]},${c[1]},${c[2]})`);
      }
      ctx.fillStyle = g;
      ctx.fillRect(bx, yy - 12 * u, bw, 12 * u);
      drawText(ctx, `${k.temp[0].toFixed(0)} to ${k.temp[1].toFixed(0)} °C`, bx, yy + 14 * u, u, { size: 12, weight: 600, halo: 0 });
      chip(ctx, 'analysis', bx + bw + 12 * u, yy + 2 * u, u, 'ANALYSIS', 11);
      drawText(ctx, 'surface temperature', bx + bw + 12 * u, yy + 22 * u, u, { size: 12, weight: 500, halo: 0, color: rgba(ui().dim, 1) });
    } else if (r === 'mrms') {
      const lut = COLORMAP_DATA['NWSRef'];
      for (let q = 0; q < bw; q += 2) {
        const i2 = Math.round((q / bw) * 255);
        ctx.fillStyle = `rgb(${lut[i2 * 3]},${lut[i2 * 3 + 1]},${lut[i2 * 3 + 2]})`;
        ctx.fillRect(bx + q, yy - 12 * u, 2, 12 * u);
      }
      drawText(ctx, '-10 to 70 dBZ', bx, yy + 14 * u, u, { size: 12, weight: 600, halo: 0 });
      chip(ctx, 'observed', bx + bw + 12 * u, yy + 2 * u, u, 'OBSERVED', 11);
      drawText(ctx, 'MRMS, outside the volume', bx + bw + 12 * u, yy + 22 * u, u, { size: 12, weight: 500, halo: 0, color: rgba(ui().dim, 1) });
    } else {
      ctx.fillStyle = 'rgba(255,225,70,0.85)';
      ctx.fillRect(bx, yy - 12 * u, bw, 12 * u);
      drawText(ctx, 'rotation so far', bx, yy + 14 * u, u, { size: 12, weight: 600, halo: 0 });
      chip(ctx, 'retrieved', bx + bw + 12 * u, yy + 2 * u, u, 'RETRIEVED', 11);
      drawText(ctx, 'MRMS azimuthal shear', bx + bw + 12 * u, yy + 22 * u, u, { size: 12, weight: 500, halo: 0, color: rgba(ui().dim, 1) });
    }
  });
  ctx.restore();
}

/** Opacity of the end card at clip time `tc` of a clip of `duration` s: it comes in over 1.2 s, 9 s before the end. */
export function endCardAlpha(tc: number, duration: number, span = 9): number {
  return Math.max(0, Math.min(1, (tc - (duration - span)) / 1.2));
}

/**
 * The closing card: every data source and instrument of the manifest with its credit and class, and
 * what the six labels mean. It covers the right-hand part of the last seconds of the top-down shot.
 */
export function drawEndCard(ctx: CanvasRenderingContext2D, W: number, H: number, u: number, m: HudModel, tc: number, duration: number): void {
  const a = endCardAlpha(tc, duration);
  if (a <= 0.01) return;
  const src = m.data.sources ?? [];
  const w = Math.min(W * 0.56, 1040 * u);
  const x = W - 34 * u - w;
  const y = 200 * u;
  const rows = Math.min(src.length, 14);
  const h = (86 + rows * 36 + 6 * 22 + 40) * u;
  ctx.save();
  ctx.globalAlpha = a;
  panel(ctx, x, y, w, h, u, 0.94);
  drawText(ctx, 'Data and credits', x + 18 * u, y + 36 * u, u, { size: 24, weight: 700, halo: 0 });
  src.slice(0, rows).forEach((s, i) => {
    const yy = y + (74 + i * 36) * u;
    const cls = (['observed', 'model', 'analysis', 'retrieved'].includes(s.class) ? s.class : 'observed') as ProvKind;
    chip(ctx, cls, x + 18 * u, yy + 8 * u, u, PROV[cls].short, 11);
    const t = `${s.name}${s.instrument ? ` · ${s.instrument}` : ''}`;
    drawText(ctx, t.length > 62 ? `${t.slice(0, 61)}…` : t, x + 150 * u, yy, u, { size: 15, weight: 700, halo: 0 });
    const cr = (s.credit ?? '').length > 92 ? `${(s.credit ?? '').slice(0, 91)}…` : s.credit ?? '';
    drawText(ctx, cr, x + 150 * u, yy + 16 * u, u, { size: 12, weight: 500, halo: 0, color: rgba(ui().dim, 1) });
  });
  if (src.length > rows) drawText(ctx, `and ${src.length - rows} more (see docs/PERILS_MOVIE.md)`, x + 150 * u, y + (74 + rows * 36 + 6) * u, u, { size: 13, weight: 500, halo: 0, color: rgba(ui().dim, 1) });
  let yy = y + (74 + rows * 36 + 30) * u;
  const kinds: ProvKind[] = ['observed', 'model', 'analysis', 'retrieved', 'interpolated', 'stylised'];
  for (const k of kinds) {
    chip(ctx, k, x + 18 * u, yy + 4 * u, u, PROV[k].short, 11);
    drawText(ctx, PROV[k].label, x + 150 * u, yy, u, { size: 14, weight: 500, halo: 0 });
    yy += 22 * u;
  }
  ctx.restore();
}

