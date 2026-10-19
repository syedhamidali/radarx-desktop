/**
 * The rotation of the tornado in the PERiLS movie: the numbers (from the data) and the particle vortex that is
 * animated with them. Same formulas as tools/perils_movie/render/vortex.py (tested to agree).
 *
 * What is observed and what is not (the merged grid is 500 m and the MRMS rotation tracks are 1 km products, so the
 * tornado itself, a few hundred metres wide, is NOT resolved; only the mesocyclone-scale couplet around it is):
 *  - RETRIEVED, radar: Vrot and azimuthal shear AS of the rotation tracks near the tornado. Solid-body mesocyclone:
 *    angular rate AS, radius Vrot / AS, cyclonic when AS > 0.
 *  - OBSERVED, survey: path width (the funnel and the debris ring are as wide as half of it) and the rating, whose
 *    damage-based wind range is the only measure of the tornado-scale wind speed the data have.
 *  - ASSUMED, STYLISED: the tangential speed at the radius is the middle of that range; omega = Vt / r, period
 *    2 pi r / Vt, counter-clockwise seen from above (cyclonic). The rotation phase is omega times the DATA time, so
 *    the time-lapse speeds the rotation by the speed-up of the frame, and in the real-time core it turns at nature's rate.
 *
 * `TornadoRenderer` is the seam: the funnel below is a particle vortex drawn on the overlay canvas; a
 * physically based ray-marched tornado (feat/tornado-realism) replaces it by implementing the same interface.
 */
import type { SceneEnv } from './perilsScene.ts';
import { pj } from './perilsScene.ts';
import { type RotationTrack, type TornadoRec, tornadoAt } from './perilsData.ts';
import { drawText, measure, rgba, ui } from './perilsStyle.ts';

const MPH = 0.44704;

/** Damage-based wind estimate ranges of the EF scale (mph, 3-s gust). */
export const EF_RANGE_MPH: Record<string, [number, number]> = {
  EF0: [65, 85], EF1: [86, 110], EF2: [111, 135], EF3: [136, 165], EF4: [166, 200], EF5: [201, 250],
};

export function efRangeMs(rating: string | undefined | null): [number, number] | null {
  const r = EF_RANGE_MPH[(rating ?? '').toUpperCase().replace('-', '')];
  return r ? [r[0] * MPH, r[1] * MPH] : null;
}

export interface VortexParams {
  radiusM: number;
  vtMs: number;
  /** rad/s, counter-clockwise seen from above. */
  omega: number;
  periodS: number;
  vtBasis: string;
  radiusBasis: string;
  vrotRadarMs: number | null;
  azShear: number | null;
  mesoRadiusM: number | null;
  mesoOmega: number | null;
  mesoPeriodS: number | null;
  rating: string;
  windRangeMph: [number, number] | null;
}

/** The strongest rotation-track point within `nearKm` of (x, y) and `winS` of `td`. */
export function rotationNear(tracks: RotationTrack[], td: number, x: number, y: number, nearKm = 6, winS = 90):
  { vrot: number; as: number | null; x: number; y: number } | null {
  let best: { vrot: number; as: number | null; x: number; y: number } | null = null;
  for (const tr of tracks ?? []) {
    let q: RotationTrack['points'][number] | null = null;
    let bd = winS;
    for (const p of tr.points ?? []) {
      const d = Math.abs(p.t - td);
      if (d <= bd) {
        bd = d;
        q = p;
      }
    }
    if (!q || typeof q.vrot_ms !== 'number') continue;
    if (Math.hypot(q.x - x, q.y - y) > nearKm) continue;
    if (!best || q.vrot_ms > best.vrot) best = { vrot: q.vrot_ms, as: typeof q.as === 'number' ? q.as : null, x: q.x, y: q.y };
  }
  return best;
}

export function vortexParams(tor: TornadoRec, tracks: RotationTrack[], td: number, x: number, y: number): VortexParams | null {
  const width = tor.width_m;
  const radiusM = width ? Math.min(800, Math.max(30, 0.5 * width)) : 75;
  const obs = rotationNear(tracks, td, x, y);
  const rng = efRangeMs(tor.rating);
  let vt: number;
  let basis: string;
  if (rng) {
    vt = 0.5 * (rng[0] + rng[1]);
    basis = `middle of the ${tor.rating} damage-based wind range (${(rng[0] / MPH).toFixed(0)}-${(rng[1] / MPH).toFixed(0)} mph): an assumption, not a measurement`;
  } else if (obs) {
    vt = obs.vrot;
    basis = 'radar Vrot of the nearest rotation track (1 km scale, a lower bound)';
  } else return null;
  const as = obs?.as && obs.as > 0 ? obs.as : null;
  return {
    radiusM, vtMs: vt, omega: vt / radiusM, periodS: (2 * Math.PI * radiusM) / vt,
    vtBasis: basis,
    radiusBasis: width ? 'half the surveyed path width (an upper bound of the core radius)' : 'assumed 75 m (the survey gives no width)',
    vrotRadarMs: obs ? obs.vrot : null,
    azShear: as,
    mesoRadiusM: as && obs ? obs.vrot / as : null,
    mesoOmega: as,
    mesoPeriodS: as ? (2 * Math.PI) / as : null,
    rating: tor.rating ?? 'tornado',
    windRangeMph: rng ? [rng[0] / MPH, rng[1] / MPH] : null,
  };
}

/** Angular rate (rad/s) of a Rankine vortex at radius `r` (same unit as `R`): rigid inside the core, v = Vt R / r outside. */
export function rankineOmega(omega: number, R: number, r: number): number {
  return r <= R ? omega : (omega * R * R) / (r * r);
}

/** Deterministic hash to [0, 1). */
export function hash01(i: number, salt = 0): number {
  let h = (Math.imul(i + 1, 0x9e3779b1) ^ Math.imul(salt + 7, 0x85ebca6b)) >>> 0;
  h ^= h >>> 15;
  h = Math.imul(h, 0x2c1b3c6d) >>> 0;
  h ^= h >>> 12;
  h = Math.imul(h, 0x297a2d39) >>> 0;
  h ^= h >>> 15;
  return (h >>> 0) / 4294967296;
}

/** Azimuth (rad, counter-clockwise from east, seen from above) of a rigidly rotating pattern at data time `td`. */
export function azimuthAt(phase0: number, omega: number, td: number): number {
  return phase0 + omega * td;
}

/** The rotation indicator: ring radius in units of the core radius, and the azimuth of its arrowhead. */
export const RING_FACTOR = 3.4;
export const INDICATOR_PHASE0 = 0.6;
export const INDICATOR_COLOUR: [number, number, number] = [0, 255, 230];

export function indicatorAzimuth(omega: number, td: number): number {
  return azimuthAt(INDICATOR_PHASE0, omega, td);
}

/** What a tornado renderer needs to draw one frame. */
export interface VortexFrame {
  params: VortexParams;
  /** Surveyed position (km) at the frame's data time. */
  x: number;
  y: number;
  /** 0..1: how immersive the shot is (labels off). */
  immersion: number;
}

/**
 * The seam for the tornado: the particle funnel below implements it; a physically based tornado
 * (feat/tornado-realism's TornadoScene) can be wrapped to implement it too.
 */
export interface TornadoRenderer {
  draw(e: SceneEnv, a: number, f: VortexFrame): void;
}

/** Everything the funnel needs about its seeds is derived from indices, so a frame is a pure function of the data time. */
interface Sprite { d: number; x: number; y: number; s: number; kind: 0 | 1 | 2; a: number }

function softDisc(rgb: [number, number, number]): HTMLCanvasElement | null {
  if (typeof document === 'undefined') return null;
  const c = document.createElement('canvas');
  c.width = c.height = 64;
  const x = c.getContext('2d')!;
  const g = x.createRadialGradient(32, 32, 0, 32, 32, 32);
  g.addColorStop(0, `rgba(${rgb[0]},${rgb[1]},${rgb[2]},1)`);
  g.addColorStop(0.45, `rgba(${rgb[0]},${rgb[1]},${rgb[2]},0.55)`);
  g.addColorStop(1, `rgba(${rgb[0]},${rgb[1]},${rgb[2]},0)`);
  x.fillStyle = g;
  x.fillRect(0, 0, 64, 64);
  return c;
}

export interface FunnelOptions {
  /** Condensation funnel top (cloud base), km. */
  cloudBaseKm: number;
  /** Assumed updraft that lifts the debris (m/s), written on the screen as an assumption. */
  updraftMs: number;
  arms: number;
}

export const FUNNEL_DEFAULTS: FunnelOptions = { cloudBaseKm: 1.0, updraftMs: 12, arms: 5 };

/**
 * A particle vortex: condensation funnel (spiral arms of soft sprites that turn with omega), a debris ring with short
 * tangential streaks (Rankine angular rate, lifted by an assumed updraft), a dust skirt, a thin circular arrow on the
 * ground with its arrowhead at the azimuth omega * td. STYLISED; only where the survey and the rotation numbers exist.
 */
export class ParticleTornado implements TornadoRenderer {
  private opts: FunnelOptions;
  private sprites: (HTMLCanvasElement | null)[] | null = null;
  private buf: Sprite[] = [];

  constructor(opts: Partial<FunnelOptions> = {}) {
    this.opts = { ...FUNNEL_DEFAULTS, ...opts };
  }

  /** Funnel radius (km) at height fraction f (0 ground .. 1 cloud base) for core radius R (km). */
  static funnelRadius(R: number, f: number): number {
    return R * (0.28 + 1.35 * Math.pow(f, 1.9));
  }

  draw(e: SceneEnv, a: number, f: VortexFrame): void {
    if (a <= 0.02) return;
    const { ctx, cam, u } = e;
    const R = f.params.radiusM / 1000;
    const td = e.td;
    const o = this.opts;
    if (!this.sprites) this.sprites = [softDisc([214, 218, 226]), softDisc([84, 68, 54]), softDisc([150, 132, 108])];
    const base = pj(cam, f.x, f.y, 0, e.ve);
    if (!base || base[0] < -1500 || base[0] > cam.w + 1500) return;
    const pxKm = (d: number) => cam.w / 2 / (d * cam.tanX);
    const out = this.buf;
    out.length = 0;
    const push = (x: number, y: number, z: number, sizeKm: number, kind: 0 | 1 | 2, alpha: number) => {
      const p = pj(cam, x, y, z, e.ve);
      if (!p) return;
      out.push({ d: p[2], x: p[0], y: p[1], s: Math.max(1.2 * u, sizeKm * pxKm(p[2])), kind, a: alpha });
    };
    const H = o.cloudBaseKm;
    // condensation funnel: spiral arms that turn rigidly with omega (the pattern's twist lags aloft)
    for (let k = 0; k < o.arms; k++) {
      const M = 46;
      for (let j = 0; j < M; j++) {
        const fr = (j + 0.5) / M;
        const jit = hash01(k * 97 + j, 1);
        const rr = ParticleTornado.funnelRadius(R, fr) * (0.86 + 0.28 * jit);
        const phi = azimuthAt((2 * Math.PI * k) / o.arms - 2.3 * fr + 0.4 * jit, f.params.omega, td);
        push(f.x + rr * Math.cos(phi), f.y + rr * Math.sin(phi), fr * H, ParticleTornado.funnelRadius(R, fr) * (0.34 + 0.3 * hash01(k * 97 + j, 2)), 0, 0.17);
      }
    }
    // the body of the funnel (a few hundred sprites inside it; they turn with the same omega)
    for (let i = 0; i < 380; i++) {
      const fr = Math.pow(hash01(i, 3), 0.8);
      const rr = ParticleTornado.funnelRadius(R, fr) * Math.sqrt(hash01(i, 4)) * 0.95;
      const phi = azimuthAt(2 * Math.PI * hash01(i, 5), f.params.omega, td);
      push(f.x + rr * Math.cos(phi), f.y + rr * Math.sin(phi), fr * H, ParticleTornado.funnelRadius(R, fr) * 0.42, 0, 0.075);
    }
    // debris ring: Rankine angular rate, lifted and recycled by the assumed updraft
    const streaks: { a: [number, number]; b: [number, number]; w: number; al: number; light: boolean }[] = [];
    const lift = (o.updraftMs / 1000) * td; // km risen by data time td
    for (let i = 0; i < 260; i++) {
      const r = R * (0.5 + 2.2 * Math.pow(hash01(i, 6), 1.5));
      const top = 0.07 + 0.3 * Math.exp(-r / (1.8 * R)) + 0.05 * hash01(i, 7);
      const z = (hash01(i, 8) * top + lift) % top;
      const w = rankineOmega(f.params.omega, R, r);
      const phi = azimuthAt(2 * Math.PI * hash01(i, 9), w, td);
      const x = f.x + r * Math.cos(phi);
      const y = f.y + r * Math.sin(phi);
      const light = hash01(i, 10) > 0.6;
      push(x, y, z, 0.018 + 0.03 * hash01(i, 11) + 0.01 * (r / R), light ? 2 : 1, light ? 0.45 : 0.55);
      // a short arc back along the motion: 0.35 s of data time
      const pa = pj(cam, x, y, z, e.ve);
      const phiB = phi - w * 0.35;
      const pb = pj(cam, f.x + r * Math.cos(phiB), f.y + r * Math.sin(phiB), z, e.ve);
      if (pa && pb) streaks.push({ a: [pa[0], pa[1]], b: [pb[0], pb[1]], w: Math.max(1, 0.012 * pxKm(pa[2])), al: light ? 0.5 : 0.4, light });
    }
    // dust skirt at the ground
    for (let i = 0; i < 70; i++) {
      const r = R * (1.2 + 2.2 * hash01(i, 12));
      const phi = azimuthAt(2 * Math.PI * hash01(i, 13), rankineOmega(f.params.omega, R, r), td);
      push(f.x + r * Math.cos(phi), f.y + r * Math.sin(phi), 0.012 + 0.05 * hash01(i, 14), R * (0.9 + 1.2 * hash01(i, 15)), 2, 0.1);
    }
    out.sort((p, q) => q.d - p.d); // far to near
    ctx.save();
    ctx.globalAlpha = a;
    for (const sp of out) {
      const img = this.sprites[sp.kind];
      if (!img) continue;
      ctx.globalAlpha = a * sp.a;
      ctx.drawImage(img, sp.x - sp.s, sp.y - sp.s, 2 * sp.s, 2 * sp.s);
    }
    ctx.lineCap = 'round';
    for (const s of streaks) {
      ctx.globalAlpha = a * s.al;
      ctx.strokeStyle = s.light ? 'rgb(196,178,150)' : 'rgb(58,46,38)';
      ctx.lineWidth = s.w;
      ctx.beginPath();
      ctx.moveTo(s.a[0], s.a[1]);
      ctx.lineTo(s.b[0], s.b[1]);
      ctx.stroke();
    }
    ctx.restore();
    this.drawIndicator(e, a, f, R);
  }

  /** A thin circular arrow on the ground around the vortex; the arrowhead sits at azimuth omega * td. */
  private drawIndicator(e: SceneEnv, a: number, f: VortexFrame, R: number): void {
    const { ctx, cam, u } = e;
    const rr = R * RING_FACTOR;
    const az = indicatorAzimuth(f.params.omega, e.td);
    const ring: ([number, number, number] | null)[] = [];
    // the ring is an arc of 300 degrees trailing the arrowhead (the gap is where it points)
    for (let i = 0; i <= 90; i++) {
      const phi = az - (i / 90) * (Math.PI * 1.66);
      ring.push(pj(cam, f.x + rr * Math.cos(phi), f.y + rr * Math.sin(phi), 0.012, e.ve));
    }
    ctx.save();
    ctx.globalAlpha = a * (0.55 + 0.25 * (1 - f.immersion));
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    for (let pass = 0; pass < 2; pass++) {
      ctx.beginPath();
      let pen = false;
      for (const p of ring) {
        if (!p) {
          pen = false;
          continue;
        }
        if (!pen) ctx.moveTo(p[0], p[1]);
        else ctx.lineTo(p[0], p[1]);
        pen = true;
      }
      ctx.strokeStyle = pass === 0 ? 'rgba(4,8,16,0.55)' : 'rgba(225,240,255,0.85)';
      ctx.lineWidth = (pass === 0 ? 4.2 : 1.8) * u;
      ctx.stroke();
    }
    // arrowhead: a triangle in the ground plane at the azimuth, pointing in the direction of rotation (counter-clockwise)
    const tip = pj(cam, f.x + rr * Math.cos(az + 0.16), f.y + rr * Math.sin(az + 0.16), 0.012, e.ve);
    const b1 = pj(cam, f.x + (rr * 1.07) * Math.cos(az - 0.04), f.y + (rr * 1.07) * Math.sin(az - 0.04), 0.012, e.ve);
    const b2 = pj(cam, f.x + (rr * 0.93) * Math.cos(az - 0.04), f.y + (rr * 0.93) * Math.sin(az - 0.04), 0.012, e.ve);
    if (tip && b1 && b2) {
      ctx.globalAlpha = a;
      ctx.beginPath();
      ctx.moveTo(tip[0], tip[1]);
      ctx.lineTo(b1[0], b1[1]);
      ctx.lineTo(b2[0], b2[1]);
      ctx.closePath();
      ctx.fillStyle = `rgb(${INDICATOR_COLOUR[0]},${INDICATOR_COLOUR[1]},${INDICATOR_COLOUR[2]})`;
      ctx.fill();
    }
    ctx.restore();
    if (f.immersion < 0.5) {
      const top = pj(cam, f.x, f.y, this.opts.cloudBaseKm, e.ve);
      if (top && top[0] > 0 && top[0] < cam.w) {
        ctx.save();
        ctx.globalAlpha = a * (1 - f.immersion * 2);
        const txt = `STYLISED FUNNEL · turns ${f.params.periodS.toFixed(0)} s per revolution (counter-clockwise from above)`;
        const w = measure(ctx, txt, u, { size: 14, weight: 600 });
        drawText(ctx, txt, Math.max(12 * u, Math.min(top[0] - w / 2, cam.w - 12 * u - w)), Math.max(24 * u, top[1] - 12 * u), u, { size: 14, weight: 600, color: rgba(ui().text, 0.95) });
        ctx.restore();
      }
    }
  }
}

/**
 * The mesocyclone-scale rotation the radar does resolve (RETRIEVED): tracers on a ring of radius Vrot / AS around the
 * strongest nearby rotation track point, turning at the observed azimuthal shear (so slowly: minutes per turn).
 */
export function drawMesoRotation(e: SceneEnv, a: number, f: VortexFrame, label: boolean): void {
  const p = f.params;
  if (a <= 0.02 || !p.mesoRadiusM || !p.mesoOmega || p.vrotRadarMs == null) return;
  const near = rotationNear(e.data.rotation, e.td, f.x, f.y);
  if (!near) return;
  const { ctx, cam, u } = e;
  const R = Math.min(2.5, p.mesoRadiusM / 1000);
  ctx.save();
  ctx.globalAlpha = a;
  for (let i = 0; i < 36; i++) {
    const phi = azimuthAt((2 * Math.PI * i) / 36, p.mesoOmega, e.td);
    const z = 0.35 + 1.1 * hash01(i, 21);
    const q = pj(cam, near.x + R * Math.cos(phi), near.y + R * Math.sin(phi), z, e.ve);
    if (!q) continue;
    ctx.beginPath();
    ctx.arc(q[0], q[1], Math.max(1.4 * u, 0.05 * (cam.w / 2 / (q[2] * cam.tanX))), 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(255,235,90,0.8)';
    ctx.fill();
  }
  if (label) {
    const t = pj(cam, near.x, near.y, 1.9, e.ve);
    if (t && t[0] > 0 && t[0] < cam.w && t[1] > 0 && t[1] < cam.h) {
      drawText(ctx, `mesocyclone-scale rotation (radar): Vrot ${p.vrotRadarMs.toFixed(0)} m/s, ${p.azShear?.toFixed(3)} s⁻¹, radius ${(p.mesoRadiusM / 1000).toFixed(1)} km, one turn per ${((p.mesoPeriodS ?? 0) / 60).toFixed(1)} min`, t[0] + 10 * u, t[1], u, { size: 13, weight: 600, color: 'rgba(255,235,90,1)' });
    }
  }
  ctx.restore();
}

/** The vortex frame for the first tornado with a survey at data time `td`, or null. */
export function vortexFrame(tornadoes: TornadoRec[], tracks: RotationTrack[], td: number, immersion: number): VortexFrame | null {
  for (const tor of tornadoes) {
    const cur = tornadoAt(tor, td);
    if (!cur) continue;
    const params = vortexParams(tor, tracks, td, cur.x, cur.y);
    if (params) return { params, x: cur.x, y: cur.y, immersion };
  }
  return null;
}
