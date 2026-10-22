/**
 * A saved picture of one nowcast frame (PNG), drawn in the browser: the engine's map image with a
 * FORECAST band above it and the full label below (origin, valid time, lead, position uncertainty,
 * the engine's skill statement and the guidance sentence). The label is part of the pixels, so it
 * cannot be cropped away by mistake and survives being pasted into a slide or a message.
 */
import { captionLines, wrapText, type FrameLabel } from './nowcastLogic';

export interface PictureInput {
  image: HTMLImageElement;
  label: FrameLabel;
  legend: [number, string][];
  vmin: number;
  vmax: number;
  units: string;
}

const PAD = 14;
const BAND_H = 34;
const FONT = '12px system-ui, -apple-system, "Segoe UI", sans-serif';

/** The picture as a PNG blob. Throws when the browser cannot make a canvas. */
export async function renderFramePng(inp: PictureInput): Promise<Blob> {
  const { image, label, legend, vmin, vmax, units } = inp;
  const iw = image.naturalWidth || image.width;
  const ih = image.naturalHeight || image.height;
  // small images are enlarged by whole factors so the cells stay crisp
  const k = Math.max(1, Math.min(4, Math.floor(720 / Math.max(1, iw))));
  const w = iw * k;
  const h = ih * k;
  const width = Math.max(w + 2 * PAD, 560);
  const lines = captionLines(label).flatMap((l, i) => (i === 0 ? [l] : wrapText(l, Math.floor((width - 2 * PAD) / 6.4))));
  const legendH = 34;
  const textH = lines.length * 17 + PAD;
  const height = BAND_H + PAD + h + PAD + legendH + textH;
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('This browser cannot draw the picture');
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, width, height);

  // band: class chip, headline, times
  ctx.fillStyle = '#b3261e';
  ctx.fillRect(0, 0, width, BAND_H);
  ctx.fillStyle = '#ffffff';
  ctx.font = `700 14px system-ui, -apple-system, "Segoe UI", sans-serif`;
  ctx.textBaseline = 'middle';
  ctx.fillText(`${label.chip}  ${label.headline}`, PAD, BAND_H / 2);
  ctx.font = FONT;
  ctx.textAlign = 'right';
  ctx.fillText(`${label.origin}  |  ${label.valid}`, width - PAD, BAND_H / 2);
  ctx.textAlign = 'left';

  // the map image on a light neutral ground (transparent = not observed / not forecastable)
  const x0 = Math.round((width - w) / 2);
  const y0 = BAND_H + PAD;
  ctx.fillStyle = '#eceef2';
  ctx.fillRect(x0, y0, w, h);
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(image, x0, y0, w, h);
  ctx.strokeStyle = '#8a8f98';
  ctx.lineWidth = 1;
  ctx.strokeRect(x0 - 0.5, y0 - 0.5, w + 1, h + 1);

  // colour bar
  const by = y0 + h + PAD;
  const bw = Math.min(360, width - 2 * PAD - 120);
  const bx = PAD + 30;
  const grad = ctx.createLinearGradient(bx, 0, bx + bw, 0);
  const span = vmax - vmin || 1;
  const stops = [...legend].sort((a, b) => a[0] - b[0]);
  if (stops.length === 0) {
    grad.addColorStop(0, '#dddddd');
    grad.addColorStop(1, '#444444');
  } else {
    for (const [v, c] of stops) grad.addColorStop(Math.max(0, Math.min(1, (v - vmin) / span)), c);
  }
  ctx.fillStyle = grad;
  ctx.fillRect(bx, by, bw, 12);
  ctx.strokeRect(bx - 0.5, by - 0.5, bw + 1, 13);
  ctx.fillStyle = '#222222';
  ctx.font = '11px ui-monospace, Menlo, Consolas, monospace';
  ctx.textAlign = 'right';
  ctx.fillText(String(vmin), bx - 6, by + 7);
  ctx.textAlign = 'left';
  ctx.fillText(`${vmax} ${units}`, bx + bw + 6, by + 7);

  // the full label
  ctx.font = FONT;
  ctx.fillStyle = '#111111';
  let y = by + legendH - 6 + 12;
  lines.forEach((l, i) => {
    ctx.font = i === 0 ? `600 ${FONT}` : FONT;
    ctx.fillText(l, PAD, y);
    y += 17;
  });

  return await new Promise<Blob>((resolve, reject) => {
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('The picture could not be encoded'))), 'image/png');
  });
}

/** Decode a base64 PNG to an image element (through a blob URL, which the page policy allows). */
export function imageFromBase64(b64: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    let url: string;
    try {
      const bin = atob(b64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      url = URL.createObjectURL(new Blob([bytes as BlobPart], { type: 'image/png' }));
    } catch {
      reject(new Error('The map image could not be decoded'));
      return;
    }
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      resolve(img);
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error('The map image could not be decoded'));
    };
    img.src = url;
  });
}
