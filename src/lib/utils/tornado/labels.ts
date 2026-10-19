/**
 * Honest, unobtrusive labels for the tornado simulation (2D canvas drawing; no DOM beyond the context).
 *
 * - a small corner tag on every frame: SIMULATION seeded by observations ...
 * - immersive shots keep only a small lower third that fades out (rating, Vrot of the parent circulation, spin)
 * - the in-the-vortex view adds "simulated view"
 * - the ground tag says whether the satellite imagery was available
 */

export interface LabelInfo {
  ef: string;
  width: number;
  vt: number;
  coreRadius: number;
  periodCore: number;
  periodFunnel: number | null;
  lcl: number;
  cloudBase: number;
  speed: number;
  headingDeg: number;
  ground: 'procedural' | 'imagery';
  ambient: string;
  sunLabel?: string;
  mesoVrot?: number;
  mesoRadar?: string;
  stationLabel?: string;
  attribution?: string[];
  rotationNote?: string;
}

export interface LabelState {
  mode: 'chaser' | 'debris' | 'inside';
  /** Seconds since the start of the shot. */
  tau: number;
  duration: number;
  timeLabel?: string;
}

export const CORNER_TAG = 'SIMULATION seeded by observations: funnel position/size/rotation from the NWS/NSSL survey and radar Vrot; shape and debris are modelled';

function wrap(ctx: CanvasRenderingContext2D, text: string, maxW: number): string[] {
  const words = text.split(' ');
  const lines: string[] = [];
  let cur = '';
  for (const w of words) {
    const test = cur ? `${cur} ${w}` : w;
    if (ctx.measureText(test).width > maxW && cur) { lines.push(cur); cur = w; } else cur = test;
  }
  if (cur) lines.push(cur);
  return lines;
}

/** Draw the labels over a frame (the canvas is cleared first). */
export function drawLabels(ctx: CanvasRenderingContext2D, w: number, h: number, info: LabelInfo, st: LabelState): void {
  ctx.clearRect(0, 0, w, h);
  const u = h / 1080;
  const pad = 18 * u;
  ctx.textBaseline = 'top';
  // ---- corner tag (top left, small)
  ctx.font = `${Math.round(12.5 * u)}px system-ui, -apple-system, sans-serif`;
  const tag = wrap(ctx, CORNER_TAG, Math.min(w * 0.42, 560 * u));
  const lh = 16 * u;
  ctx.fillStyle = 'rgba(0,0,0,0.38)';
  ctx.fillRect(pad - 6 * u, pad - 5 * u, Math.min(w * 0.42, 560 * u) + 12 * u, tag.length * lh + 10 * u);
  ctx.fillStyle = 'rgba(235,238,244,0.88)';
  tag.forEach((l, i) => ctx.fillText(l, pad, pad + i * lh));
  if (st.mode === 'inside') {
    ctx.font = `600 ${Math.round(15 * u)}px system-ui, -apple-system, sans-serif`;
    ctx.fillStyle = 'rgba(255,214,120,0.95)';
    ctx.fillText('SIMULATED VIEW: this position would be lethal; no camera could record it', pad, pad + tag.length * lh + 12 * u);
  }
  // ---- ground tag (bottom right, tiny)
  ctx.font = `${Math.round(11 * u)}px system-ui, -apple-system, sans-serif`;
  ctx.textAlign = 'right';
  const gt = info.ground === 'imagery'
    ? (info.attribution?.join(' | ') ?? 'satellite imagery')
    : 'no satellite imagery available offline: procedural farmland (not the real land cover)';
  ctx.fillStyle = 'rgba(210,214,222,0.7)';
  ctx.fillText(`${gt} | ${info.ambient}${info.sunLabel ? ` (${info.sunLabel})` : ''}`, w - pad, h - pad - 12 * u);
  ctx.textAlign = 'left';
  // ---- lower third: fades in at 0.4 s, out after 6 s
  const a = Math.max(0, Math.min(1, (st.tau - 0.4) / 0.8)) * Math.max(0, Math.min(1, (6.8 - st.tau) / 1.2));
  if (a > 0.01) {
    const bx = pad, by = h - pad - 78 * u - 14 * u;
    ctx.globalAlpha = a;
    ctx.fillStyle = 'rgba(0,0,0,0.42)';
    ctx.fillRect(bx - 6 * u, by - 6 * u, 560 * u, 80 * u);
    ctx.fillStyle = 'rgba(245,247,250,0.95)';
    ctx.font = `600 ${Math.round(17 * u)}px system-ui, -apple-system, sans-serif`;
    ctx.fillText(`${info.ef} tornado, damage swath ${Math.round(info.width)} m (NWS survey)${st.timeLabel ? `  ${st.timeLabel}` : ''}`, bx, by);
    ctx.font = `${Math.round(13.5 * u)}px system-ui, -apple-system, sans-serif`;
    ctx.fillStyle = 'rgba(218,223,232,0.95)';
    const l2 = `parent circulation (radar, 500 m grid) ${info.mesoVrot != null ? `Vrot ${info.mesoVrot.toFixed(0)} m/s` : 'n/a'}${info.mesoRadar ? ` ${info.mesoRadar}` : ''}; tornado vortex unresolved`;
    const l3 = `spin: core ${info.periodCore.toFixed(0)} s per turn (modelled), funnel surface ${info.periodFunnel ? info.periodFunnel.toFixed(0) : 'n/a'} s; survey-scale estimate 19-23 s`;
    ctx.fillText(l2, bx, by + 26 * u);
    ctx.fillText(l3, bx, by + 46 * u);
    ctx.globalAlpha = 1;
  }
}
