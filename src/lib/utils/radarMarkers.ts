/**
 * Marker shapes of the radar network map (flat and globe): one shape per access class, so nothing
 * depends on colour alone. circle (loadable, filled), square (open data, link), triangle (key required),
 * cross (restricted), ring (unknown).
 */
import type { DataAccess } from './radarCatalogue';

export interface MarkerInk {
  ink: string;
  dim: string;
  accent: string;
  bg: string;
}

export function drawMarker(ctx: CanvasRenderingContext2D, a: DataAccess, x: number, y: number, r: number, c: MarkerInk, selected: boolean): void {
  ctx.beginPath();
  ctx.strokeStyle = selected ? c.accent : a === 'open-loadable' ? c.accent : a === 'unknown' ? c.dim : c.ink;
  ctx.fillStyle = a === 'open-loadable' ? c.accent : c.bg;
  switch (a) {
    case 'open-loadable':
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = c.bg;
      ctx.stroke();
      break;
    case 'open-link':
      ctx.rect(x - r, y - r, 2 * r, 2 * r);
      ctx.fill();
      ctx.stroke();
      break;
    case 'key-required':
      ctx.moveTo(x, y - r * 1.15);
      ctx.lineTo(x + r * 1.1, y + r * 0.85);
      ctx.lineTo(x - r * 1.1, y + r * 0.85);
      ctx.closePath();
      ctx.fill();
      ctx.stroke();
      break;
    case 'restricted':
      ctx.moveTo(x - r, y - r);
      ctx.lineTo(x + r, y + r);
      ctx.moveTo(x + r, y - r);
      ctx.lineTo(x - r, y + r);
      ctx.stroke();
      break;
    default:
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
  }
}
