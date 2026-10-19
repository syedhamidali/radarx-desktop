/**
 * Small science insets of the PERiLS movie (canvas): surface strip charts of the nearest station with a
 * moving "now" line (the gust front shows as the step in pressure, temperature and wind), the drop-size
 * spectrum of the nearest disdrometer with Dm and the fall-speed-versus-diameter evidence of hail, a
 * time-height field above the instrument site (MRR / wind profiler), and the stylised rain streaks of the
 * ground-level view. Each says what it is made of; stations' numbers are OBSERVED, the reference
 * curves are labelled. Pure drawing, no state.
 */
import { type Station, type TimeHeight, type ProvKind, codeValue, dsdColumn, raindropFall } from './perilsData.ts';
import { chip, drawText, fit, panel, rgba, ui, type RGB } from './perilsStyle.ts';

const line = (ctx: CanvasRenderingContext2D, x0: number, y0: number, x1: number, y1: number, w: number, c: string) => {
  ctx.strokeStyle = c;
  ctx.lineWidth = w;
  ctx.beginPath();
  ctx.moveTo(x0, y0);
  ctx.lineTo(x1, y1);
  ctx.stroke();
};

/** A polyline of a series over [0, win] with gaps where the value is missing. */
function plot(ctx: CanvasRenderingContext2D, ts: number[], vs: (number | null)[], x: number, y: number, w: number, h: number, win: number, lo: number, hi: number, col: string, lw: number): void {
  ctx.strokeStyle = col;
  ctx.lineWidth = lw;
  ctx.lineJoin = 'round';
  ctx.beginPath();
  let pen = false;
  for (let i = 0; i < ts.length; i++) {
    const v = vs[i];
    if (v == null || !Number.isFinite(v)) {
      pen = false;
      continue;
    }
    const px = x + (ts[i] / win) * w;
    const py = y + h - ((v - lo) / (hi - lo)) * h;
    if (!pen) ctx.moveTo(px, py);
    else ctx.lineTo(px, py);
    pen = true;
  }
  ctx.stroke();
}

function range(vals: (number | null)[][], pad = 0.08, minSpan = 1): [number, number] {
  let lo = Infinity;
  let hi = -Infinity;
  for (const a of vals) for (const v of a) if (v != null && Number.isFinite(v)) {
    lo = Math.min(lo, v);
    hi = Math.max(hi, v);
  }
  if (!Number.isFinite(lo)) return [0, 1];
  const span = Math.max(minSpan, hi - lo);
  return [lo - span * pad, hi + span * pad];
}

/**
 * Pressure, temperature / dew point and wind (with gust) of the nearest station over the whole window.
 * OBSERVED (the series of the station). Charts without data are left out.
 */
export function drawStripCharts(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, u: number, st: Station, td: number, win: number, a: number, eventTimes: number[] = []): number {
  if (a <= 0.02) return 0;
  const s = st.series ?? {};
  const ts = (s.t_s as number[] | undefined) ?? [];
  if (ts.length < 2) return 0;
  const charts: { title: string; unit: string; series: { v: (number | null)[]; col: string }[] }[] = [];
  if (s.pressure_hpa) charts.push({ title: 'pressure', unit: 'hPa', series: [{ v: s.pressure_hpa, col: 'rgba(255,255,255,0.95)' }] });
  const tser: { v: (number | null)[]; col: string }[] = [];
  if (s.temperature_c) tser.push({ v: s.temperature_c, col: 'rgba(255,120,110,0.98)' });
  if (s.dewpoint_c) tser.push({ v: s.dewpoint_c, col: 'rgba(90,230,170,0.98)' });
  if (tser.length) charts.push({ title: s.dewpoint_c ? 'temperature / dew point' : 'temperature', unit: '°C', series: tser });
  const wser: { v: (number | null)[]; col: string }[] = [];
  if (s.wind_gust_ms) wser.push({ v: s.wind_gust_ms, col: 'rgba(255,200,90,0.9)' });
  if (s.wind_speed_ms) wser.push({ v: s.wind_speed_ms, col: 'rgba(120,200,255,0.98)' });
  if (wser.length) charts.push({ title: s.wind_gust_ms ? 'wind / gust' : 'wind', unit: 'm/s', series: wser });
  if (!charts.length) return 0;
  const ch = 62 * u;
  const h = (44 + charts.length * (ch + 22 * u) / u) * u;
  ctx.save();
  ctx.globalAlpha = a;
  panel(ctx, x, y, w, h, u);
  chip(ctx, 'observed', x + 12 * u, y + 30 * u, u, `OBSERVED · ${st.name}`, 12);
  let yy = y + 46 * u;
  for (const c of charts) {
    drawText(ctx, `${c.title} (${c.unit})`, x + 14 * u, yy + 12 * u, u, { size: 12, weight: 600, halo: 0, color: rgba(ui().dim, 1) });
    const px = x + 14 * u;
    const pw = w - 28 * u;
    const py = yy + 18 * u;
    // the range of what is plotted: the window only (the series run some minutes before and after it)
    const inWin = (vs: (number | null)[]) => vs.map((v, i) => (ts[i] >= 0 && ts[i] <= win ? v : null));
    const [lo, hi] = range(c.series.map((q) => inWin(q.v)), 0.1, c.unit === 'hPa' ? 1.5 : 2);
    ctx.fillStyle = rgba(ui().panel, 0.35);
    ctx.fillRect(px, py, pw, ch);
    ctx.save();
    ctx.beginPath();
    ctx.rect(px, py, pw, ch);
    ctx.clip();
    for (const et of eventTimes) line(ctx, px + (et / win) * pw, py, px + (et / win) * pw, py + ch, 1 * u, rgba(ui().caution, 0.5));
    for (const q of c.series) plot(ctx, ts, q.v, px, py, pw, ch, win, lo, hi, q.col, 1.8 * u);
    ctx.restore();
    drawText(ctx, hi.toFixed(hi > 100 ? 0 : 1), px + 3 * u, py + 10 * u, u, { size: 10, weight: 600, halo: 0, align: 'left', color: rgba(ui().dim, 1), mono: true });
    drawText(ctx, lo.toFixed(lo > 100 ? 0 : 1), px + 3 * u, py + ch - 2 * u, u, { size: 10, weight: 600, halo: 0, align: 'left', color: rgba(ui().dim, 1), mono: true });
    const nx = px + Math.max(0, Math.min(1, td / win)) * pw;
    line(ctx, nx, py - 2 * u, nx, py + ch + 2 * u, 2 * u, rgba(ui().text, 0.95));
    yy += ch + 22 * u + 0;
  }
  ctx.restore();
  return h;
}

/**
 * The drop-size spectrum of the station's disdrometer at the current time (OBSERVED counts of the
 * instrument): log10 N(D) against diameter, the spectra of the last minute faint behind, Dm marked, rain
 * rate and the largest drop. Below, if the station recorded hail candidates, the measured fall speed of the
 * large particles against the raindrop curve (a reference, not data).
 */
export function drawDsdInset(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, u: number, st: Station, td: number, a: number): number {
  const dsd = st.dsd;
  if (!dsd || a <= 0.02) return 0;
  const j = dsdColumn(dsd, td, 40);
  const ev = (st.events ?? []).filter((e) => e.kind === 'hail_candidate' && typeof e.mean_d_large_mm === 'number' && typeof e.mean_fall_ms_large === 'number');
  const hh = 168 * u;
  const h = hh + (ev.length ? 150 * u : 0);
  ctx.save();
  ctx.globalAlpha = a;
  panel(ctx, x, y, w, h, u);
  chip(ctx, 'observed', x + 12 * u, y + 30 * u, u, 'OBSERVED · drop sizes', 12);
  drawText(ctx, fit(ctx, st.name, w - 200 * u, u, { size: 12, weight: 500 }), x + w - 12 * u, y + 24 * u, u, { size: 12, weight: 500, align: 'right', halo: 0, color: rgba(ui().dim, 1) });
  const px = x + 40 * u;
  const py = y + 46 * u;
  const pw = w - 56 * u;
  const ph = 84 * u;
  const dmax = dsd.d[dsd.d.length - 1];
  const lo = 0;
  const hi = 9;
  ctx.fillStyle = rgba(ui().panel, 0.35);
  ctx.fillRect(px, py, pw, ph);
  const yv = (lg: number) => py + ph - ((lg - lo) / (hi - lo)) * ph;
  for (let g = 2; g <= 8; g += 2) {
    line(ctx, px, yv(g), px + pw, yv(g), 1 * u, rgba(ui().line, 0.18));
    drawText(ctx, `1e${g - 2}`, px - 4 * u, yv(g) + 4 * u, u, { size: 10, weight: 500, halo: 0, align: 'right', mono: true, color: rgba(ui().muted, 1) });
  }
  const trace = (col: number, c: string, lw: number) => {
    ctx.strokeStyle = c;
    ctx.lineWidth = lw;
    ctx.beginPath();
    let pen = false;
    dsd.d.forEach((d, i) => {
      const v = dsd.logN[col]?.[i];
      if (v == null) {
        pen = false;
        return;
      }
      const X = px + (d / dmax) * pw;
      const Y = yv(Math.max(lo, v + 2));
      if (!pen) ctx.moveTo(X, Y);
      else ctx.lineTo(X, Y);
      pen = true;
    });
    ctx.stroke();
  };
  if (j >= 0) {
    for (let k = Math.max(0, j - 6); k < j; k++) trace(k, rgba([120, 190, 255], 0.14 + 0.03 * (k - j + 6)), 1.2 * u);
    trace(j, rgba([120, 200, 255], 0.98), 2.4 * u);
    const dm = dsd.dm_mm?.[j];
    if (dm != null) {
      const X = px + (dm / dmax) * pw;
      line(ctx, X, py, X, py + ph, 1.6 * u, rgba(ui().caution, 0.95));
      drawText(ctx, `Dm ${dm.toFixed(2)} mm`, X + 5 * u, py + 12 * u, u, { size: 12, weight: 700, halo: 0, color: rgba(ui().caution, 1) });
    }
    const rr = dsd.rain_rate_mmh?.[j];
    let big = 0;
    dsd.d.forEach((d, i) => {
      if ((dsd.logN[j]?.[i] ?? null) != null) big = d;
    });
    drawText(ctx, `${rr != null ? `${rr.toFixed(1)} mm/h · ` : ''}largest drops ${big.toFixed(1)} mm`, x + 14 * u, y + 148 * u, u, { size: 12, weight: 600, halo: 0 });
  } else {
    drawText(ctx, 'no drops counted', px + pw / 2, py + ph / 2, u, { size: 13, weight: 500, align: 'center', halo: 0, color: rgba(ui().muted, 1) });
  }
  drawText(ctx, 'diameter (mm)', px + pw, py + ph + 14 * u, u, { size: 10, weight: 500, align: 'right', halo: 0, color: rgba(ui().muted, 1) });
  drawText(ctx, 'log N(D)', x + 12 * u, py + 8 * u, u, { size: 10, weight: 500, halo: 0, color: rgba(ui().muted, 1) });
  if (ev.length) {
    const y2 = y + hh;
    drawText(ctx, 'large particles: measured fall speed vs raindrop curve', x + 14 * u, y2 + 4 * u, u, { size: 12, weight: 600, halo: 0, color: rgba(ui().dim, 1) });
    const qx = x + 40 * u;
    const qy = y2 + 14 * u;
    const qw = w - 56 * u;
    const qh = 100 * u;
    ctx.fillStyle = rgba(ui().panel, 0.35);
    ctx.fillRect(qx, qy, qw, qh);
    const D = (d: number) => qx + (d / 20) * qw;
    const V = (v: number) => qy + qh - (v / 25) * qh;
    ctx.strokeStyle = rgba([120, 190, 255], 0.9);
    ctx.lineWidth = 1.6 * u;
    ctx.setLineDash([5 * u, 4 * u]);
    ctx.beginPath();
    for (let d = 0.5; d <= 9; d += 0.25) {
      if (d === 0.5) ctx.moveTo(D(d), V(raindropFall(d)));
      else ctx.lineTo(D(d), V(raindropFall(d)));
    }
    ctx.stroke();
    ctx.setLineDash([]);
    for (const e of ev) {
      const near = Math.abs((e.t_s as number) - td) < 30;
      ctx.beginPath();
      ctx.arc(D(e.mean_d_large_mm as number), V(e.mean_fall_ms_large as number), (near ? 6 : 3.4) * u, 0, Math.PI * 2);
      ctx.fillStyle = rgba([255, 130, 40], near ? 1 : 0.6);
      ctx.fill();
    }
    drawText(ctx, 'rain curve (reference)', qx + 6 * u, V(raindropFall(8)) - 6 * u, u, { size: 10, weight: 500, halo: 0, color: rgba([150, 205, 255], 1) });
    drawText(ctx, 'particle diameter (mm)', qx + qw, qy + qh + 13 * u, u, { size: 10, weight: 500, halo: 0, align: 'right', color: rgba(ui().muted, 1) });
    drawText(ctx, 'm/s', qx - 4 * u, qy + 10 * u, u, { size: 10, weight: 500, halo: 0, align: 'right', color: rgba(ui().muted, 1) });
  }
  ctx.restore();
  return h;
}

/** Colour of a time-height cell: a blue-to-red ramp in [0, 1] (reflectivity-like). */
export function rampColour(f: number): RGB {
  const c = Math.max(0, Math.min(1, f));
  const stops: [number, RGB][] = [[0, [30, 60, 140]], [0.35, [40, 190, 200]], [0.6, [250, 230, 80]], [0.82, [250, 130, 40]], [1, [230, 40, 60]]];
  for (let i = 1; i < stops.length; i++) {
    if (c <= stops[i][0]) {
      const [a0, ca] = stops[i - 1];
      const [a1, cb] = stops[i];
      const f2 = (c - a0) / (a1 - a0);
      return [0, 1, 2].map((k) => Math.round(ca[k] + (cb[k] - ca[k]) * f2)) as RGB;
    }
  }
  return stops[stops.length - 1][1];
}

/** A time-height field above an instrument site with the "now" line (OBSERVED: the profiler's own data). */
export function drawTimeHeight(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, u: number, name: string, th: TimeHeight, field: string, label: string, td: number, win: number, a: number, prov: ProvKind = 'observed'): number {
  const f = th.fields[field];
  if (!f || a <= 0.02) return 0;
  const h = 172 * u;
  ctx.save();
  ctx.globalAlpha = a;
  panel(ctx, x, y, w, h, u);
  chip(ctx, prov, x + 12 * u, y + 30 * u, u, 'OBSERVED', 12);
  drawText(ctx, `${name} · ${label}`, x + w - 12 * u, y + 24 * u, u, { size: 12, weight: 500, align: 'right', halo: 0, color: rgba(ui().dim, 1) });
  const px = x + 44 * u;
  const py = y + 42 * u;
  const pw = w - 60 * u;
  const ph = h - 62 * u;
  const hTop = th.h[th.h.length - 1];
  const nt = th.t.length;
  const cw = Math.max(1, (pw / Math.max(1, th.t.filter((q) => q >= 0 && q <= win).length)));
  ctx.save();
  ctx.beginPath();
  ctx.rect(px, py, pw, ph);
  ctx.clip();
  for (let i = 0; i < nt; i++) {
    const t = th.t[i];
    if (t < 0 || t > win) continue;
    const X = px + (t / win) * pw;
    for (let k = 0; k < th.h.length; k++) {
      const code = f.codes[i]?.[k] ?? 0;
      const v = codeValue(code, f.min, f.max);
      if (v == null) continue;
      const ca = rampColour((v - f.min) / (f.max - f.min));
      const y0 = py + ph - (th.h[k] / hTop) * ph;
      const y1 = k > 0 ? py + ph - (th.h[k - 1] / hTop) * ph : py + ph;
      ctx.fillStyle = rgba(ca, 0.95);
      ctx.fillRect(X, y0, cw + 0.6, y1 - y0 + 0.6);
    }
  }
  ctx.restore();
  const nx = px + Math.max(0, Math.min(1, td / win)) * pw;
  line(ctx, nx, py - 2 * u, nx, py + ph + 2 * u, 2 * u, rgba(ui().text, 0.95));
  drawText(ctx, `${(hTop / 1000).toFixed(1)} km`, px - 4 * u, py + 9 * u, u, { size: 10, weight: 500, halo: 0, align: 'right', mono: true, color: rgba(ui().muted, 1) });
  drawText(ctx, '0', px - 4 * u, py + ph, u, { size: 10, weight: 500, halo: 0, align: 'right', mono: true, color: rgba(ui().muted, 1) });
  drawText(ctx, `${f.min.toFixed(0)} to ${f.max.toFixed(0)}`, x + 14 * u, y + h - 8 * u, u, { size: 11, weight: 500, halo: 0, color: rgba(ui().muted, 1) });
  ctx.restore();
  return h;
}

/**
 * STYLISED rain streaks for the ground-level camera: their number follows the OBSERVED rain rate of the
 * station, their length and width its Dm (bigger drops: longer, thicker, faster streaks). Deterministic
 * in the clip time; they are a picture of the rain rate, not of individual drops.
 */
export function drawRainStreaks(ctx: CanvasRenderingContext2D, W: number, H: number, u: number, rainMmh: number | null, dmMm: number | null, tc: number, a: number): number {
  if (!rainMmh || rainMmh < 0.5 || a <= 0.02) return 0;
  const n = Math.round(Math.min(900, 60 + 140 * Math.log10(1 + rainMmh) * 2.2) * a);
  const dm = dmMm ?? 1.6;
  const len = (24 + 36 * Math.min(2.5, dm)) * u;
  const wd = (0.8 + 0.9 * Math.min(2.5, dm)) * u;
  const speed = 900 * (0.7 + dm / 3) * u;
  ctx.save();
  ctx.lineCap = 'round';
  for (let i = 0; i < n; i++) {
    // a cheap hash of the index: x, y phase and depth
    const hx = Math.sin(i * 12.9898) * 43758.5453;
    const hy = Math.sin(i * 78.233) * 12345.6789;
    const hz = Math.sin(i * 39.346) * 24634.6345;
    const fx = hx - Math.floor(hx);
    const fy = hy - Math.floor(hy);
    const fz = hz - Math.floor(hz);
    const depth = 0.35 + 0.65 * fz;
    const x = fx * (W + 60 * u) - 30 * u + (tc * speed * 0.12 * depth) % (30 * u);
    const y = ((fy * H + tc * speed * depth) % (H + len)) - len;
    ctx.strokeStyle = `rgba(205,222,245,${(0.10 + 0.22 * depth).toFixed(3)})`;
    ctx.lineWidth = wd * depth;
    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineTo(x - 0.12 * len * depth, y + len * depth);
    ctx.stroke();
  }
  ctx.restore();
  return n;
}

/** The value of a station series at `td` for the hail pulse test: the most recent hail candidate within `windowS` seconds. */
export function recentHail(st: Station, td: number, windowS = 20): { maxD: number; age: number } | null {
  let best: { maxD: number; age: number } | null = null;
  for (const e of st.events ?? []) {
    if (e.kind !== 'hail_candidate' || typeof e.t_s !== 'number') continue;
    const age = td - e.t_s;
    if (age >= 0 && age <= windowS && (best === null || age < best.age)) best = { maxD: typeof e.max_d_mm === 'number' ? e.max_d_mm : 0, age };
  }
  return best;
}

