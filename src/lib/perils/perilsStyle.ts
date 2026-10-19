/**
 * Colours and text helpers of the PERiLS movie's heads-up display (canvas drawing).
 *
 * UI colours come from the app's design tokens (src/lib/styles/tokens.css, provenance.css) read from
 * the document at draw time, so the movie follows the app's theme; the literal fallbacks are only
 * used where there is no document (tests). Data colours (hydrometeor classes, lightning) are
 * technical values of their own and are listed here, in one place.
 */
import type { ProvKind } from './perilsData.ts';

export type RGB = [number, number, number];

export const rgba = (c: RGB, a = 1): string => `rgba(${c[0]},${c[1]},${c[2]},${a})`;

/** Provenance classes of the movie, with the short chip text and a one-line meaning. */
export const PROV: Record<ProvKind, { short: string; label: string; token: string; fallback: RGB }> = {
  observed: { short: 'OBSERVED', label: 'measured by an instrument', token: '--c-prov-observed-rgb', fallback: [62, 227, 164] },
  model: { short: 'MODEL', label: 'numerical model output', token: '--c-prov-model-rgb', fallback: [43, 217, 245] },
  analysis: { short: 'ANALYSIS', label: 'objective analysis / reanalysis', token: '--c-accent-rgb', fallback: [90, 160, 255] },
  retrieved: { short: 'RETRIEVED', label: 'derived from observations by an algorithm', token: '--c-prov-retrieved-rgb', fallback: [176, 140, 255] },
  interpolated: { short: 'INTERPOLATED', label: 'carried between observed volumes', token: '--c-caution-rgb', fallback: [255, 190, 70] },
  stylised: { short: 'STYLISED', label: 'drawn for appearance, not a data value', token: '--c-line-rgb', fallback: [160, 175, 195] },
};

let cache: Map<string, RGB> | null = null;

/** `--token-rgb` ("62, 227, 164") of the document as numbers, or the fallback. */
export function tokenRgb(token: string, fallback: RGB): RGB {
  if (typeof document === 'undefined') return fallback;
  cache ??= new Map();
  const hit = cache.get(token);
  if (hit) return hit;
  const raw = getComputedStyle(document.documentElement).getPropertyValue(token).trim();
  const parts = raw.split(',').map((v) => parseFloat(v));
  const out: RGB = parts.length === 3 && parts.every(Number.isFinite) ? [parts[0], parts[1], parts[2]] : fallback;
  cache.set(token, out);
  return out;
}

export function provColour(kind: ProvKind): RGB {
  const p = PROV[kind];
  return tokenRgb(p.token, p.fallback);
}

/** Text and panel colours from the tokens. */
export function ui(): { text: RGB; dim: RGB; muted: RGB; panel: RGB; line: RGB; accent: RGB; alarm: RGB; caution: RGB } {
  const ink = tokenRgb('--c-ink-rgb', [224, 235, 247]);
  const bg = tokenRgb('--c-surface-0-rgb', [3, 6, 12]);
  const mix = (f: number): RGB => [0, 1, 2].map((i) => Math.round(ink[i] * (1 - f) + bg[i] * f)) as RGB;
  return {
    text: ink,
    dim: mix(0.25),
    muted: mix(0.45),
    panel: bg,
    line: tokenRgb('--c-line-rgb', [120, 150, 185]),
    accent: tokenRgb('--c-accent-rgb', [43, 217, 245]),
    alarm: tokenRgb('--c-alarm-rgb', [255, 90, 90]),
    caution: tokenRgb('--c-caution-rgb', [255, 190, 70]),
  };
}

/** Hydrometeor classes drawn in 3-D (Okabe-Ito colours, distinct for common colour-vision deficiencies). */
export const HAIL_COLOUR: RGB = [255, 120, 20];
export const GRAUPEL_COLOUR: RGB = [86, 180, 233];
export const TDS_COLOUR: RGB = [255, 90, 220];
export const ROTATION_COLOUR: RGB = [255, 235, 90];
export const ZDR_COLOUR: RGB = [90, 230, 255];
export const TORNADO_COLOUR: RGB = [255, 70, 70];

export const FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif';
export const MONO = 'ui-monospace, "SF Mono", Menlo, Consolas, monospace';

export interface TextOpts {
  size?: number;
  weight?: number;
  color?: string;
  align?: CanvasTextAlign;
  mono?: boolean;
  halo?: number;
}

/** Text with a dark halo (readable over any background). `u` = H / 1080. */
export function drawText(ctx: CanvasRenderingContext2D, str: string, x: number, y: number, u: number, o: TextOpts = {}): number {
  const size = (o.size ?? 18) * u;
  ctx.font = `${o.weight ?? 600} ${Math.round(size)}px ${o.mono ? MONO : FONT}`;
  ctx.textAlign = o.align ?? 'left';
  ctx.textBaseline = 'alphabetic';
  ctx.lineJoin = 'round';
  const halo = (o.halo ?? 3) * u;
  if (halo > 0) {
    ctx.lineWidth = halo;
    ctx.strokeStyle = 'rgba(4,8,16,0.82)';
    ctx.strokeText(str, x, y);
  }
  ctx.fillStyle = o.color ?? rgba(ui().text, 0.97);
  ctx.fillText(str, x, y);
  return ctx.measureText(str).width;
}

export function measure(ctx: CanvasRenderingContext2D, str: string, u: number, o: TextOpts = {}): number {
  ctx.font = `${o.weight ?? 600} ${Math.round((o.size ?? 18) * u)}px ${o.mono ? MONO : FONT}`;
  return ctx.measureText(str).width;
}

/** `str` shortened with an ellipsis to fit `maxW` pixels (`u`-scaled text options). */
export function fit(ctx: CanvasRenderingContext2D, str: string, maxW: number, u: number, o: TextOpts = {}): string {
  if (measure(ctx, str, u, o) <= maxW) return str;
  let s = str;
  while (s.length > 1 && measure(ctx, `${s}…`, u, o) > maxW) s = s.slice(0, -1);
  return `${s}…`;
}

/** A translucent rounded panel; returns the inner origin. */
export function panel(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, u: number, alpha = 0.58): void {
  const r = 8 * u;
  ctx.save();
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
  ctx.fillStyle = rgba(ui().panel, alpha);
  ctx.fill();
  ctx.lineWidth = 1 * u;
  ctx.strokeStyle = rgba(ui().line, 0.32);
  ctx.stroke();
  ctx.restore();
}

/** A provenance chip (rounded, coloured dot + class text); returns its width. Stylised chips are dashed. */
export function chip(ctx: CanvasRenderingContext2D, kind: ProvKind, x: number, y: number, u: number, label?: string, size = 13): number {
  const col = provColour(kind);
  const txt = label ?? PROV[kind].short;
  const w = measure(ctx, txt, u, { size, weight: 700, mono: true }) + 24 * u;
  const h = (size + 9) * u;
  ctx.save();
  ctx.beginPath();
  ctx.roundRect(x, y - h + 4 * u, w, h, 5 * u);
  ctx.fillStyle = rgba(col, 0.2);
  ctx.fill();
  ctx.lineWidth = 1.2 * u;
  if (kind === 'stylised') ctx.setLineDash([3 * u, 2 * u]);
  ctx.strokeStyle = rgba(col, 0.85);
  ctx.stroke();
  ctx.restore();
  ctx.beginPath();
  ctx.arc(x + 9 * u, y - h / 2 + 4 * u, 3.2 * u, 0, Math.PI * 2);
  ctx.fillStyle = rgba(col, 1);
  ctx.fill();
  drawText(ctx, txt, x + 17 * u, y - 1 * u, u, { size, weight: 700, mono: true, halo: 0, color: rgba(ui().text, 0.98) });
  return w;
}
