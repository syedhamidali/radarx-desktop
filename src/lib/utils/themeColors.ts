/**
 * Theme colours for code that cannot use CSS variables directly: canvas drawing (`ctx.fillStyle`
 * takes a colour, not `var(--x)`), WebGL clear colours and chart libraries.
 *
 * Everything here reads the design tokens (src/lib/styles/tokens.css) from the document, so both
 * themes and a custom accent are followed. A drawing component should read `$themeRevision` in the
 * statement that triggers its redraw: it is bumped whenever `data-theme` (or the inline style that
 * sets the accent) changes on <html>.
 */
import { writable } from 'svelte/store';

/** Bumped when the theme or the accent colour changes: canvas views redraw. */
export const themeRevision = writable(0);

if (typeof document !== 'undefined' && typeof MutationObserver !== 'undefined') {
  let queued = false;
  new MutationObserver(() => {
    if (queued) return;
    queued = true;
    queueMicrotask(() => {
      queued = false;
      themeRevision.update((n) => n + 1);
    });
  }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'style', 'class'] });
}

/** The computed value of a custom property (trimmed), or `fallback` outside a browser. */
export function cssVar(name: string, fallback = ''): string {
  if (typeof document === 'undefined') return fallback;
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

/** `rgba(...)` from a token that holds an "r, g, b" triplet. */
function tint(rgbToken: string, alpha: number, fallback: string): string {
  return `rgba(${cssVar(rgbToken, fallback)}, ${alpha})`;
}

/** The foreground colour with an alpha: light on the dark theme, dark on the light one. */
export const ink = (alpha = 1): string => tint('--c-ink-rgb', alpha, '224, 235, 247');

/** The shadow colour with an alpha. */
export const shadow = (alpha = 1): string => tint('--c-shadow-rgb', alpha, '0, 0, 0');

/** A surface colour (0 = canvas ... 3 = most raised), optionally translucent. */
export const surface = (level: 0 | 1 | 2 | 3 = 0, alpha = 1): string =>
  tint(`--c-surface-${level}-rgb`, alpha, ['3, 6, 12', '7, 12, 22', '11, 19, 34', '18, 29, 51'][level]);

/** The accent colour with an alpha. */
export const accent = (alpha = 1): string => tint('--c-accent-rgb', alpha, '43, 217, 245');

/** Text colours. */
export const text = (): string => cssVar('--c-text', '#e0ebf7');
export const textDim = (): string => cssVar('--c-text-dim', '#a6b9cd');
export const muted = (): string => cssVar('--c-muted', '#7d95ae');

/** Colour of chart series `i` (cycles through the 8 series tokens). */
export const series = (i: number): string => cssVar(`--c-series-${(((i % 8) + 8) % 8) + 1}`, '#2bd9f5');

/** "#rrggbb" token as 0..1 channels (for WebGL clear colours). */
export function cssRgb01(name: string, fallback: [number, number, number]): [number, number, number] {
  const m = /^#([0-9a-f]{6})$/i.exec(cssVar(name));
  if (!m) return fallback;
  const n = parseInt(m[1], 16);
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
}
