/**
 * Draws a cross-section (height above distance along a line) onto any 2D canvas context: the
 * on-screen view at the device pixel ratio, and the PNG export at whatever size is asked for.
 */
import { COLORMAP_DATA } from './colormaps';
import {
  computeLayout,
  MARGINS,
  formatTick,
  niceStep,
  niceTicks,
  type PlotLayout,
  type SectionCells,
  type View,
} from './crossSectionGeometry';
import { resampleGrid, type Grid, type Interp } from './crossSectionRaster';

export interface Palette {
  bg: string;
  plotBg: string;
  fg: string;
  muted: string;
  grid: string;
  axis: string;
}

export const PALETTES: Record<'dark' | 'light', Palette> = {
  dark: {
    bg: '#0b0d18',
    plotBg: '#10121f',
    fg: 'rgba(255,255,255,0.9)',
    muted: 'rgba(255,255,255,0.58)',
    grid: 'rgba(255,255,255,0.09)',
    axis: 'rgba(255,255,255,0.5)',
  },
  light: {
    bg: '#ffffff',
    plotBg: '#f2f3f7',
    fg: '#14161f',
    muted: '#4a4e61',
    grid: 'rgba(20,22,31,0.12)',
    axis: '#2a2d3b',
  },
};

export const SANS = 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
export const MONO = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';

export interface DrawSpec {
  cells: SectionCells;
  cmapName: string;
  vmin: number;
  vmax: number;
  units: string;
  /** The window on screen (zoom/pan) and the full extent of the section (km, above the radar). */
  view: View;
  full: View;
  /** Vertical exaggeration, or null to fill the canvas. */
  ve: number | null;
  /** Added to heights for the axis (radar altitude when showing heights above sea level). */
  heightOffsetKm: number;
  title: string;
  subtitle: string;
  /** Small caption at the bottom of the figure (the smoothing that was applied). */
  note?: string | null;
  /** Smoothed section on a regular grid; null draws the cells as measured. */
  grid?: Grid | null;
  interp?: Interp;
  xLabel: string;
  yLabel: string;
  theme: 'dark' | 'light';
  /** Device pixel ratio the context was scaled by (cell edges snap to device pixels). */
  dpr: number;
}

function lut(cmapName: string): string[] {
  const rgb = COLORMAP_DATA[cmapName] ?? COLORMAP_DATA['turbo'];
  const out: string[] = new Array(256);
  for (let i = 0; i < 256; i++) out[i] = `rgb(${rgb[i * 3]},${rgb[i * 3 + 1]},${rgb[i * 3 + 2]})`;
  return out;
}

/** Layout the section would have in a canvas of this size (shared by drawing and hit-testing). */
export function layoutFor(
  width: number,
  height: number,
  spec: Pick<DrawSpec, 'full' | 've'> & { note?: string | null },
): PlotLayout {
  // A caption line under the x label needs a little more room at the bottom.
  const margins = spec.note ? { ...MARGINS, bottom: MARGINS.bottom + 18 } : MARGINS;
  return computeLayout(
    width,
    height,
    spec.full.d1 - spec.full.d0,
    spec.full.h1 - spec.full.h0,
    spec.ve,
    margins,
  );
}

/**
 * Draw the whole figure into a `width` x `height` (CSS pixel) area of `ctx`, which must already be
 * scaled by the device pixel ratio. Returns the plot layout so callers can map the mouse.
 */
export function drawCrossSection(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  spec: DrawSpec,
): PlotLayout {
  const pal = PALETTES[spec.theme];
  const layout = layoutFor(width, height, spec);
  const { plot, colorbar } = layout;
  const { view, dpr } = spec;
  const snap = (v: number) => Math.round(v * dpr) / dpr;

  ctx.save();
  ctx.fillStyle = pal.bg;
  ctx.fillRect(0, 0, width, height);
  ctx.fillStyle = pal.plotBg;
  ctx.fillRect(plot.x, plot.y, plot.w, plot.h);

  const dSpan = view.d1 - view.d0;
  const hSpan = view.h1 - view.h0;
  const X = (d: number) => plot.x + ((d - view.d0) / dSpan) * plot.w;
  const Y = (h: number) => plot.y + plot.h - ((h - view.h0) / hSpan) * plot.h;

  // --- cells ---
  const colors = lut(spec.cmapName);
  const range = spec.vmax - spec.vmin || 1;
  const px = 1 / dpr;
  ctx.save();
  ctx.beginPath();
  ctx.rect(plot.x, plot.y, plot.w, plot.h);
  ctx.clip();
  let lastColor = -1;
  if (spec.grid) drawRaster(ctx, plot, spec, dpr);
  else for (const c of spec.cells.cells) {
    if (c.v < spec.vmin) continue; // like the PPI: gates below the colour range are not drawn
    if (c.x1 < view.d0 || c.x0 > view.d1 || c.y1 < view.h0 || c.y0 > view.h1) continue;
    const t = Math.min(1, Math.max(0, (c.v - spec.vmin) / range));
    const ci = Math.round(t * 255);
    if (ci !== lastColor) {
      ctx.fillStyle = colors[ci];
      lastColor = ci;
    }
    const x0 = snap(X(c.x0));
    let x1 = snap(X(c.x1));
    const yTop = snap(Y(c.y1));
    let yBot = snap(Y(c.y0));
    if (x1 - x0 < px) x1 = x0 + px;
    if (yBot - yTop < px) yBot = yTop + px;
    ctx.fillRect(x0, yTop, x1 - x0, yBot - yTop);
  }
  ctx.restore();

  // --- grid, ticks ---
  ctx.lineWidth = 1;
  ctx.font = `11px ${MONO}`;
  ctx.fillStyle = pal.muted;

  const xTicks = niceTicks(view.d0, view.d1, Math.max(2, plot.w / 70));
  const xStep = niceStep(dSpan, Math.max(2, plot.w / 70));
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  for (const d of xTicks) {
    const x = snap(X(d)) + 0.5 / dpr;
    ctx.strokeStyle = pal.grid;
    ctx.beginPath();
    ctx.moveTo(x, plot.y);
    ctx.lineTo(x, plot.y + plot.h);
    ctx.stroke();
    ctx.strokeStyle = pal.axis;
    ctx.beginPath();
    ctx.moveTo(x, plot.y + plot.h);
    ctx.lineTo(x, plot.y + plot.h + 5);
    ctx.stroke();
    ctx.fillText(formatTick(d, xStep), x, plot.y + plot.h + 8);
  }

  const off = spec.heightOffsetKm;
  const yTicks = niceTicks(view.h0 + off, view.h1 + off, Math.max(2, plot.h / 32));
  const yStep = niceStep(hSpan, Math.max(2, plot.h / 32));
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (const hv of yTicks) {
    const y = snap(Y(hv - off)) + 0.5 / dpr;
    ctx.strokeStyle = pal.grid;
    ctx.beginPath();
    ctx.moveTo(plot.x, y);
    ctx.lineTo(plot.x + plot.w, y);
    ctx.stroke();
    ctx.strokeStyle = pal.axis;
    ctx.beginPath();
    ctx.moveTo(plot.x - 5, y);
    ctx.lineTo(plot.x, y);
    ctx.stroke();
    ctx.fillText(formatTick(hv, yStep), plot.x - 8, y);
  }

  // --- frame ---
  ctx.strokeStyle = pal.axis;
  ctx.strokeRect(plot.x - 0.5 / dpr, plot.y - 0.5 / dpr, plot.w + 1 / dpr, plot.h + 1 / dpr);

  // --- axis labels ---
  ctx.fillStyle = pal.fg;
  ctx.font = `12px ${SANS}`;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'alphabetic';
  ctx.fillText(fit(ctx, spec.xLabel, plot.w), plot.x + plot.w / 2, plot.y + plot.h + 42);
  ctx.save();
  ctx.translate(16, plot.y + plot.h / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.textBaseline = 'alphabetic';
  ctx.fillText(fit(ctx, spec.yLabel, plot.h), 0, 0);
  ctx.restore();

  // --- title ---
  ctx.textAlign = 'left';
  ctx.textBaseline = 'alphabetic';
  ctx.fillStyle = pal.fg;
  ctx.font = `600 15px ${SANS}`;
  ctx.fillText(fit(ctx, spec.title, width - 24), 12, 22);
  ctx.fillStyle = pal.muted;
  ctx.font = `12px ${SANS}`;
  ctx.fillText(fit(ctx, spec.subtitle, width - 24), 12, 42);

  if (spec.note) {
    ctx.fillStyle = pal.muted;
    ctx.font = `11px ${SANS}`;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
    ctx.fillText(fit(ctx, spec.note, width - 24), 12, height - 9);
  }

  // --- colour bar ---
  drawColorbar(ctx, colorbar, spec, pal, snap, dpr);

  ctx.restore();
  return layout;
}

const MAX_RASTER_PIXELS = 6_000_000;
let rasterCanvas: HTMLCanvasElement | null = null;

/** The smoothed section: resampled to the plot's pixels, coloured, and drawn over the plot area. */
export function drawRaster(
  ctx: CanvasRenderingContext2D,
  plot: { x: number; y: number; w: number; h: number },
  spec: Pick<DrawSpec, 'grid' | 'view' | 'cmapName' | 'vmin' | 'vmax' | 'interp'>,
  dpr: number,
): void {
  const grid = spec.grid;
  if (!grid) return;
  let outW = Math.max(1, Math.round(plot.w * dpr));
  let outH = Math.max(1, Math.round(plot.h * dpr));
  const over = (outW * outH) / MAX_RASTER_PIXELS;
  if (over > 1) {
    const k = 1 / Math.sqrt(over);
    outW = Math.max(1, Math.floor(outW * k));
    outH = Math.max(1, Math.floor(outH * k));
  }
  const vals = resampleGrid(grid, spec.view, outW, outH, spec.interp ?? 'bilinear');
  const rgb = COLORMAP_DATA[spec.cmapName] ?? COLORMAP_DATA['turbo'];
  const img = new ImageData(outW, outH);
  const out = img.data;
  const range = spec.vmax - spec.vmin || 1;
  for (let i = 0; i < vals.length; i++) {
    const v = vals[i];
    // no data, or below the colour range (like the PPI): transparent
    if (v !== v || v < spec.vmin) continue;
    const ci = Math.round(Math.min(1, Math.max(0, (v - spec.vmin) / range)) * 255);
    out[i * 4] = rgb[ci * 3];
    out[i * 4 + 1] = rgb[ci * 3 + 1];
    out[i * 4 + 2] = rgb[ci * 3 + 2];
    out[i * 4 + 3] = 255;
  }
  rasterCanvas ??= document.createElement('canvas');
  rasterCanvas.width = outW;
  rasterCanvas.height = outH;
  const rc = rasterCanvas.getContext('2d');
  if (!rc) return;
  rc.putImageData(img, 0, 0);
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(rasterCanvas, plot.x, plot.y, plot.w, plot.h);
}

/** Shorten `text` with an ellipsis so it fits `maxWidth`. */
function fit(ctx: CanvasRenderingContext2D, text: string, maxWidth: number): string {
  if (ctx.measureText(text).width <= maxWidth) return text;
  let lo = 0;
  let hi = text.length;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (ctx.measureText(text.slice(0, mid) + '…').width <= maxWidth) lo = mid;
    else hi = mid - 1;
  }
  return text.slice(0, lo) + '…';
}

function drawColorbar(
  ctx: CanvasRenderingContext2D,
  bar: { x: number; y: number; w: number; h: number },
  spec: DrawSpec,
  pal: Palette,
  snap: (v: number) => number,
  dpr: number,
): void {
  const colors = lut(spec.cmapName);
  const steps = Math.max(2, Math.round(bar.h * dpr));
  const sh = bar.h / steps;
  for (let i = 0; i < steps; i++) {
    const t = 1 - (i + 0.5) / steps;
    ctx.fillStyle = colors[Math.round(t * 255)];
    ctx.fillRect(bar.x, bar.y + i * sh, bar.w, sh + 0.5 / dpr);
  }
  ctx.strokeStyle = pal.axis;
  ctx.lineWidth = 1;
  ctx.strokeRect(bar.x - 0.5 / dpr, bar.y - 0.5 / dpr, bar.w + 1 / dpr, bar.h + 1 / dpr);

  const span = spec.vmax - spec.vmin || 1;
  const step = niceStep(span, Math.max(2, bar.h / 34));
  ctx.font = `11px ${MONO}`;
  ctx.fillStyle = pal.muted;
  ctx.textAlign = 'left';
  ctx.textBaseline = 'middle';
  for (const v of niceTicks(spec.vmin, spec.vmax, Math.max(2, bar.h / 34))) {
    const y = snap(bar.y + bar.h - ((v - spec.vmin) / span) * bar.h) + 0.5 / dpr;
    ctx.strokeStyle = pal.axis;
    ctx.beginPath();
    ctx.moveTo(bar.x + bar.w, y);
    ctx.lineTo(bar.x + bar.w + 4, y);
    ctx.stroke();
    ctx.fillText(formatTick(v, step), bar.x + bar.w + 7, y);
  }
  if (spec.units) {
    ctx.fillStyle = pal.fg;
    ctx.font = `12px ${SANS}`;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
    ctx.fillText(spec.units, bar.x, bar.y - 8);
  }
}
