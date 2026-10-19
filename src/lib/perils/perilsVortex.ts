/**
 * The rotation of the tornado in the PERiLS movie: the numbers (from the data) and the particle vortex that is
 * animated with them. Same formulas as tools/perils_movie/render/vortex.py (tested to agree).
 *
 * What is observed and what is not (the merged grid is 500 m and the tornado is 366 m wide: it is NOT resolved by any
 * radar; only the parent circulation around it is):
 *  - RETRIEVED, radar (context/vortex_kinematics.json): the couplet of the parent circulation, Vrot 8-15 m/s (median 11),
 *    about 1.8 km across, mostly cyclonic, one turn per about 8 minutes.
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
import { type TornadoRec, type VortexData, type VortexStep, tornadoAt } from './perilsData.ts';
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

export interface ParentCirculation {
  /** The couplet of the best radar at the step nearest in time: the parent circulation, wider than the tornado. */
  vrotMs: number | null;
  diameterKm: number | null;
  sense: string | null;
  radar: string | null;
  resolutionM: number | null;
  /** Medians over the steps (cyclonic ones for omega). */
  medianVrotMs: number | null;
  medianDiameterKm: number | null;
  medianOmega: number | null;
  vrotRange: [number, number] | null;
  cyclonicSteps: number;
  nSteps: number;
}

export interface VortexParams {
  radiusM: number;
  /** Tangential speed at the radius used for the spin of the funnel (ESTIMATE from the survey, not measured). */
  vtMs: number;
  /** rad/s, counter-clockwise seen from above. */
  omega: number;
  periodS: number;
  vtBasis: string;
  radiusBasis: string;
  rating: string;
  windRangeMs: [number, number] | null;
  periodRangeS: [number, number] | null;
  /** What the radars actually measured (the parent circulation), or null without the kinematics file. */
  parent: ParentCirculation | null;
  /** Always false in this data: the tornado is narrower than twice the effective resolution of every radar. */
  tornadoResolved: boolean;
}

function nearestStep(v: VortexData | null | undefined, td: number): VortexStep | null {
  if (!v || !v.steps?.length) return null;
  let best = v.steps[0];
  for (const s of v.steps) if (Math.abs(s.t - td) < Math.abs(best.t - td)) best = s;
  return best;
}

export function parentCirculation(v: VortexData | null | undefined, td: number): ParentCirculation | null {
  const st = nearestStep(v, td);
  if (!v || !st) return null;
  const sm = v.summary;
  return {
    vrotMs: st.vrot_ms, diameterKm: st.diameter_km, sense: st.sense, radar: st.radar, resolutionM: st.effective_resolution_m,
    medianVrotMs: sm.vrot_ms_min_median_max?.[1] ?? null, medianDiameterKm: sm.couplet_diameter_km_min_median_max?.[1] ?? null,
    medianOmega: sm.omega_s1_median_cyclonic, vrotRange: sm.vrot_ms_min_median_max ? [sm.vrot_ms_min_median_max[0], sm.vrot_ms_min_median_max[2]] : null,
    cyclonicSteps: sm.cyclonic_steps, nSteps: sm.n_steps,
  };
}

/**
 * The spin of the funnel and the numbers on the card. The tangential speed is the middle of the survey's wind range
 * (the data's `survey_scale_estimate`, else the EF range of the rating), the radius half the surveyed damage width.
 */
export function vortexParams(tor: TornadoRec, vortex: VortexData | null | undefined, td: number): VortexParams | null {
  const est = vortex?.survey_scale_estimate ?? null;
  const width = est?.damage_width_m ?? tor.width_m;
  const radiusM = width ? Math.min(800, Math.max(30, 0.5 * width)) : 75;
  const rng: [number, number] | null = est?.wind_range_ms ?? efRangeMs(tor.rating);
  if (!rng) return null;
  const vt = 0.5 * (rng[0] + rng[1]);
  return {
    radiusM, vtMs: vt, omega: vt / radiusM, periodS: (2 * Math.PI * radiusM) / vt,
    vtBasis: `middle of the ${tor.rating ?? 'survey'} damage-based wind range (${rng[0].toFixed(0)}-${rng[1].toFixed(0)} m/s): an estimate, not a measurement`,
    radiusBasis: width ? 'half the surveyed damage width' : 'assumed 75 m (the survey gives no width)',
    rating: tor.rating ?? 'tornado',
    windRangeMs: rng,
    periodRangeS: est?.rotation_period_s_range ?? null,
    parent: parentCirculation(vortex, td),
    tornadoResolved: false,
  };
}

/** Angular rate (rad/s) of a Rankine vortex at radius `r` (same unit as `R`): rigid inside the core, v = Vt R / r outside. */
export function rankineOmega(omega: number, R: number, r: number): number {
  return r <= R ? omega : (omega * R * R) / (r * r);
}

function smooth01(x: number): number {
  const c = Math.max(0, Math.min(1, x));
  return c * c * (3 - 2 * c);
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
export const RING_FACTOR = 1.25;
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

/** One thing to draw: a soft disc (`seg` unset) or a line segment; all sorted far to near. */
interface Item { d: number; x: number; y: number; s: number; kind: 0 | 1 | 2 | 3; a: number; seg?: { x2: number; y2: number; w: number; col: string }; quad?: { p: number[]; col: string } }

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
  private buf: Item[] = [];
  private quads: Item[] = [];
  private fx: HTMLCanvasElement | null = null;

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
    if (!this.sprites) this.sprites = [softDisc([176, 182, 192]), softDisc([70, 56, 44]), softDisc([140, 122, 98]), softDisc([74, 82, 96])];
    const base = pj(cam, f.x, f.y, 0, e.ve);
    if (!base || base[0] < -1500 || base[0] > cam.w + 1500) return;
    const pxKm = (d: number) => cam.w / 2 / (d * cam.tanX);
    const out = this.buf;
    out.length = 0;
    const quads = this.quads;
    quads.length = 0;
    const disc = (x: number, y: number, z: number, sizeKm: number, kind: 0 | 1 | 2 | 3, alpha: number) => {
      const p = pj(cam, x, y, z, e.ve);
      if (!p || p[2] < 0.04) return; // nothing is drawn within 40 m of the lens
      out.push({ d: p[2], x: p[0], y: p[1], s: Math.min(90 * u, Math.max(1.2 * u, sizeKm * pxKm(p[2]))), kind, a: alpha });
    };
    const H = o.cloudBaseKm;
    // condensation funnel: a surface of revolution whose texture (spiral streaks and billows) is fixed in the rotating
    // frame, so that it turns with omega (phase = azimuth - omega * td); the far side is drawn too, dimmer
    const NA = 96;
    const NZ = 56;
    const verts: ([number, number, number] | null)[] = new Array((NZ + 1) * (NA + 1));
    const meander = (fr: number) => 0.28 * R * Math.pow(fr, 1.3) * Math.sin(td * 0.31 + fr * 3.1);
    for (let jz = 0; jz <= NZ; jz++) {
      const fr = jz / NZ;
      const rf = ParticleTornado.funnelRadius(R, fr);
      const cx = f.x + meander(fr);
      const cy = f.y + 0.6 * meander(fr + 0.5);
      for (let ia = 0; ia <= NA; ia++) {
        const phi = (2 * Math.PI * ia) / NA;
        verts[jz * (NA + 1) + ia] = pj(cam, cx + rf * Math.cos(phi), cy + rf * Math.sin(phi), fr * H, e.ve);
      }
    }
    const ex = cam.eye[0];
    const ey = cam.eye[1];
    for (let jz = 0; jz < NZ; jz++) {
      const fm = (jz + 0.5) / NZ;
      const rf = ParticleTornado.funnelRadius(R, fm);
      const cx = f.x + meander(fm);
      const cy = f.y + 0.6 * meander(fm + 0.5);
      const top = 1 - smooth01((fm - 0.86) / 0.14);
      for (let ia = 0; ia < NA; ia++) {
        const a0 = verts[jz * (NA + 1) + ia];
        const a1 = verts[jz * (NA + 1) + ia + 1];
        const b0 = verts[(jz + 1) * (NA + 1) + ia];
        const b1 = verts[(jz + 1) * (NA + 1) + ia + 1];
        if (!a0 || !a1 || !b0 || !b1 || a0[2] < 0.04 || b0[2] < 0.04) continue;
        const phim = (2 * Math.PI * (ia + 0.5)) / NA;
        const dx = ex - (cx + rf * Math.cos(phim));
        const dy = ey - (cy + rf * Math.sin(phim));
        const cosT = (Math.cos(phim) * dx + Math.sin(phim) * dy) / (Math.hypot(dx, dy) || 1);
        const front = cosT > 0;
        const rot = phim - f.params.omega * td;
        const tex = 0.5 + 0.22 * Math.sin(4 * rot + 7 * fm) + 0.16 * Math.sin(9 * rot - 11 * fm + 1.3) + 0.1 * Math.sin(15 * rot + 23 * fm);
        const L = Math.max(0, Math.min(1, 0.12 + 0.84 * tex * (0.55 + 0.45 * fm))) * (front ? 1 : 0.6);
        const col = `rgb(${Math.round(58 + 160 * L)},${Math.round(64 + 160 * L)},${Math.round(76 + 158 * L)})`;
        const alpha = (front ? 0.62 * Math.pow(Math.abs(cosT), 0.85) : 0.26) * top;
        quads.push({ d: 0.25 * (a0[2] + a1[2] + b0[2] + b1[2]), x: 0, y: 0, s: 0, kind: 0, a: alpha, quad: { p: [a0[0], a0[1], a1[0], a1[1], b1[0], b1[1], b0[0], b0[1]], col } });
      }
    }
    // debris: Rankine angular rate, lifted and recycled by the assumed updraft; hard specks with a blur of 1/12 s, a few dust puffs
    const lift = (o.updraftMs / 1000) * td; // km risen by data time td
    for (let i = 0; i < 520; i++) {
      const r = R * (0.5 + 1.5 * Math.pow(hash01(i, 6), 1.5));
      const top = 0.07 + 0.3 * Math.exp(-r / (1.8 * R)) + 0.05 * hash01(i, 7);
      const z = (hash01(i, 8) * top + lift) % top;
      const w = rankineOmega(f.params.omega, R, r);
      const phi = azimuthAt(2 * Math.PI * hash01(i, 9), w, td);
      const x = f.x + r * Math.cos(phi);
      const y = f.y + r * Math.sin(phi);
      const light = hash01(i, 10) > 0.6;
      const pa = pj(cam, x, y, z, e.ve);
      const pb = pj(cam, f.x + r * Math.cos(phi - w * 0.085), f.y + r * Math.sin(phi - w * 0.085), z, e.ve);
      if (pa && pb && pa[2] > 0.04) {
        out.push({
          d: pa[2], x: pa[0], y: pa[1], s: 0, kind: 1, a: light ? 0.85 : 0.8,
          seg: { x2: pb[0], y2: pb[1], w: Math.min(2.6 * u, Math.max(1.0 * u, 0.0012 * pxKm(pa[2]))), col: light ? 'rgb(206,190,162)' : 'rgb(46,38,32)' },
        });
      }
      if (i % 9 === 0) disc(x, y, z, 0.012 + 0.012 * hash01(i, 11), light ? 2 : 1, 0.14);
    }
    // dust skirt at the ground
    for (let i = 0; i < 60; i++) {
      const r = R * (1.1 + 1.8 * hash01(i, 12));
      const phi = azimuthAt(2 * Math.PI * hash01(i, 13), rankineOmega(f.params.omega, R, r), td);
      disc(f.x + r * Math.cos(phi), f.y + r * Math.sin(phi), 0.012 + 0.05 * hash01(i, 14), R * (0.8 + 1.0 * hash01(i, 15)), 2, 0.09);
    }
    const dC = base[2];
    const paint = (c2: CanvasRenderingContext2D, items: Item[]) => {
      c2.lineCap = 'round';
      for (const it of items) {
        c2.globalAlpha = it.a;
        if (it.quad) {
          const q = it.quad.p;
          c2.fillStyle = it.quad.col;
          c2.strokeStyle = it.quad.col;
          c2.lineWidth = 0.8;
          c2.beginPath();
          c2.moveTo(q[0], q[1]);
          c2.lineTo(q[2], q[3]);
          c2.lineTo(q[4], q[5]);
          c2.lineTo(q[6], q[7]);
          c2.closePath();
          c2.fill();
          c2.stroke();
        } else if (it.seg) {
          c2.strokeStyle = it.seg.col;
          c2.lineWidth = it.seg.w;
          c2.beginPath();
          c2.moveTo(it.x, it.y);
          c2.lineTo(it.seg.x2, it.seg.y2);
          c2.stroke();
        } else {
          const img = this.sprites![it.kind];
          if (img) c2.drawImage(img, it.x - it.s, it.y - it.s, 2 * it.s, 2 * it.s);
        }
      }
    };
    out.sort((p, q) => q.d - p.d); // far to near
    const backN = out.findIndex((it) => it.d <= dC);
    const back = backN < 0 ? out : out.slice(0, backN);
    const front = backN < 0 ? [] : out.slice(backN);
    quads.sort((p, q) => q.d - p.d);
    if (!this.fx || this.fx.width !== cam.w || this.fx.height !== cam.h) {
      this.fx = document.createElement('canvas');
      this.fx.width = cam.w;
      this.fx.height = cam.h;
    }
    const fc = this.fx.getContext('2d')!;
    fc.setTransform(1, 0, 0, 1, 0, 0);
    fc.clearRect(0, 0, cam.w, cam.h);
    paint(fc, quads);
    ctx.save();
    ctx.globalAlpha = a;
    ctx.save();
    ctx.scale(1, 1);
    paint(ctx, back.map((it) => ({ ...it, a: it.a * a })));
    ctx.restore();
    ctx.globalAlpha = a;
    ctx.filter = `blur(${(2.2 * u).toFixed(1)}px)`;
    ctx.drawImage(this.fx, 0, 0);
    ctx.filter = 'none';
    paint(ctx, front.map((it) => ({ ...it, a: it.a * a })));
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
      ctx.lineWidth = (pass === 0 ? 5.2 : 2.4) * u;
      ctx.stroke();
    }
    // arrowhead: a triangle of fixed size on the screen along the ring's tangent at the azimuth, pointing the way it turns (counter-clockwise)
    const p0 = pj(cam, f.x + rr * Math.cos(az), f.y + rr * Math.sin(az), 0.012, e.ve);
    const p1 = pj(cam, f.x + rr * Math.cos(az + 0.04), f.y + rr * Math.sin(az + 0.04), 0.012, e.ve);
    if (p0 && p1) {
      const dx = p1[0] - p0[0];
      const dy = p1[1] - p0[1];
      const L = Math.hypot(dx, dy) || 1;
      const ux = dx / L;
      const uy = dy / L;
      const S = 17 * u;
      ctx.globalAlpha = a;
      ctx.beginPath();
      ctx.moveTo(p0[0] + ux * S, p0[1] + uy * S);
      ctx.lineTo(p0[0] - ux * S * 0.6 - uy * S * 0.62, p0[1] - uy * S * 0.6 + ux * S * 0.62);
      ctx.lineTo(p0[0] - ux * S * 0.6 + uy * S * 0.62, p0[1] - uy * S * 0.6 - ux * S * 0.62);
      ctx.closePath();
      ctx.fillStyle = `rgb(${INDICATOR_COLOUR[0]},${INDICATOR_COLOUR[1]},${INDICATOR_COLOUR[2]})`;
      ctx.fill();
      ctx.strokeStyle = 'rgba(4,8,16,0.7)';
      ctx.lineWidth = 1.5 * u;
      ctx.stroke();
    }
    ctx.restore();
    if (f.immersion < 0.5) {
      const top = pj(cam, f.x, f.y, this.opts.cloudBaseKm, e.ve);
      if (top && top[0] > 0 && top[0] < cam.w) {
        ctx.save();
        ctx.globalAlpha = a * (1 - f.immersion * 2);
        const txt = `stylised funnel · ${f.params.periodS.toFixed(0)} s per turn`;
        const w = measure(ctx, txt, u, { size: 14, weight: 600 });
        drawText(ctx, txt, Math.max(12 * u, Math.min(top[0] - w / 2, cam.w - 430 * u - w)), Math.max(24 * u, top[1] - 12 * u), u, { size: 14, weight: 600, color: rgba(ui().text, 0.95) });
        ctx.restore();
      }
    }
  }
}

/**
 * The parent circulation the radars do resolve (RETRIEVED, 500 m grid): tracers on a ring of the median couplet
 * radius around the surveyed position, turning at the median angular velocity of the cyclonic steps (about
 * 8 minutes per turn: slow, as measured). Not the tornado: that is unresolved.
 */
export function drawParentCirculation(e: SceneEnv, a: number, f: VortexFrame, label: boolean): void {
  const par = f.params.parent;
  if (a <= 0.02 || !par || !par.medianDiameterKm || !par.medianOmega) return;
  const { ctx, cam, u } = e;
  const R = Math.min(2.5, par.medianDiameterKm / 2);
  ctx.save();
  ctx.globalAlpha = a;
  for (let i = 0; i < 36; i++) {
    const phi = azimuthAt((2 * Math.PI * i) / 36, par.medianOmega, e.td);
    const z = 0.3 + 0.9 * hash01(i, 21);
    const q = pj(cam, f.x + R * Math.cos(phi), f.y + R * Math.sin(phi), z, e.ve);
    if (!q) continue;
    ctx.beginPath();
    ctx.arc(q[0], q[1], Math.max(1.4 * u, 0.05 * (cam.w / 2 / (q[2] * cam.tanX))), 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(255,235,90,0.8)';
    ctx.fill();
  }
  if (label) {
    const t = pj(cam, f.x, f.y, 1.6, e.ve);
    if (t && t[0] > 0 && t[0] < cam.w && t[1] > 0 && t[1] < cam.h) {
      const v = par.medianVrotMs != null ? `Vrot ${par.medianVrotMs.toFixed(0)} m/s` : 'Vrot –';
      drawText(ctx, `parent circulation (radar): ${v}, ${par.medianDiameterKm.toFixed(1)} km wide`, t[0] + 10 * u, t[1], u, { size: 13, weight: 600, color: 'rgba(255,235,90,1)' });
    }
  }
  ctx.restore();
}

/** The vortex frame for the first tornado with a survey at data time `td`, or null. */
export function vortexFrame(tornadoes: TornadoRec[], vortex: VortexData | null | undefined, td: number, immersion: number): VortexFrame | null {
  for (const tor of tornadoes) {
    const cur = tornadoAt(tor, td);
    if (!cur) continue;
    const params = vortexParams(tor, vortex, td);
    if (params) return { params, x: cur.x, y: cur.y, immersion };
  }
  return null;
}
