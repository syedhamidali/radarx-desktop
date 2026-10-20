/**
 * The lightning channels of the movie: lines only, no dots. The real LMA sources of a flash are the skeleton; the
 * look between them is a model, and it is said so on the film.
 *
 *  OBSERVED  the source positions (x, y, height) and times, the flash they belong to, the order in which the
 *            channel grew (time order), where it branched (a source is joined to its nearest EARLIER source:
 *            where two later sources are both nearest to the same earlier one, the channel forks there).
 *  MODELLED  the smooth path between the real positions (a centripetal Catmull-Rom spline through them: it passes
 *            exactly through every kept source), the fine-scale jaggedness (seeded midpoint displacement at
 *            scales from 100 m down to 8 m, a fixed amplitude of at most 50 m, hash-seeded by the flash, so the
 *            same flash has the same shape in every frame: no flicker), the brightness (leader, return strokes
 *            timed at the peaks of the real source rate, afterglow) and the width.
 *  NOT DONE  no channel is invented: no flash, no fork and no stroke without sources behind it. A source with
 *            no neighbour within `gateKm` is not joined to anything. Sources closer than `mergeKm` to a kept
 *            source (the LMA locates a source to about 100 m) are one node of the skeleton.
 *
 * The level of detail follows the camera (a piece is split until it is about `pxPerPiece` px long), and the finer
 * levels never move the coarser points, so a camera that approaches only reveals more of the same channel.
 *
 * Pure part (compile, tree, spline, jitter, brightness, streaks): unit-tested in lmaChannel.test.mjs. Drawing at the end.
 */
import type { LmaFlash } from './perilsData.ts';

export interface ChannelParams {
  /** Sources closer than this to a kept source are merged into it (km); about the LMA's own location error. */
  mergeKm: number;
  /** A source is joined to its nearest earlier source within this distance (km); further, it starts a new tree. */
  gateKm: number;
  /** Clip seconds the channel stays visible after it has grown (fading over the last third). */
  afterglowS: number;
  /** Brightness of the growing leader channel and of the first moment after it. */
  leader: number;
  /** An edge is drawn as growing at most this long (clip seconds) before its far source appears. */
  growMaxS: number;
  /** Midpoint displacement: sigma = jitterK x the length of the piece, at most jitterCapKm. */
  jitterK: number;
  jitterCapKm: number;
  /** No refinement below this piece length (km). */
  minPieceKm: number;
  minDepth: number;
  maxDepth: number;
  /** Refine until a piece is about this long on the screen (px at 1080 p). */
  pxPerPiece: number;
  /** Visual width of the main channel (m): the width in px follows from the distance (perspective). */
  widthM: number;
  minPx: number;
  maxPx: number;
  /** Return strokes: bins of the source rate (data s), the least spacing (data s) and the most strokes. */
  strokeBinS: number;
  strokeSepS: number;
  maxStrokes: number;
}

export const CHANNEL_DEFAULTS: ChannelParams = {
  mergeKm: 0.12,
  gateKm: 2.5,
  afterglowS: 0.75,
  leader: 0.5,
  growMaxS: 0.15,
  jitterK: 0.12,
  jitterCapKm: 0.05,
  minPieceKm: 0.008,
  minDepth: 1,
  maxDepth: 7,
  pxPerPiece: 6,
  widthM: 5,
  minPx: 1.1,
  maxPx: 11,
  strokeBinS: 0.03,
  strokeSepS: 0.06,
  maxStrokes: 4,
};

export interface Stroke {
  /** Clip time. */
  tc: number;
  /** Amplitude 0..1.4 */
  a: number;
  /** False: at a peak of the real source rate. True: a re-brightening after the last source, modelled. */
  modelled: boolean;
}

export interface Channel {
  id: number;
  /** Clip time of the first source and of the last node of the skeleton. */
  tc0: number;
  tcEnd: number;
  /** Nodes of the skeleton (merged sources) and the sources behind them. */
  n: number;
  nSources: number;
  x: Float32Array;
  y: Float32Array;
  z: Float32Array;
  tc: Float64Array;
  /** Index of the nearest earlier node within the gate, -1 for the start of a tree. */
  parent: Int32Array;
  /** 0..1: how much of the flash passes through the edge parent -> node (the trunk is 1). */
  imp: Float32Array;
  /** Branches without forks in them: node lists, each starting at the node it leaves (or at a root). */
  chains: Int32Array[];
  strokes: Stroke[];
}

// ─── build ──────────────────────────────────────────────────────────────────

function strokeTimes(td: number[], toClip: (td: number) => number, p: ChannelParams): Stroke[] {
  const t0 = td[0];
  const t1 = td[td.length - 1];
  const nb = Math.max(1, Math.ceil((t1 - t0) / p.strokeBinS + 1e-9));
  const h = new Array<number>(nb).fill(0);
  for (const t of td) h[Math.min(nb - 1, Math.floor((t - t0) / p.strokeBinS))]++;
  const sm = h.map((_, i) => 0.25 * (h[i - 1] ?? h[i]) + 0.5 * h[i] + 0.25 * (h[i + 1] ?? h[i]));
  const peaks: { t: number; v: number }[] = [];
  for (let i = 0; i < nb; i++) {
    if (sm[i] >= (sm[i - 1] ?? -1) && sm[i] > (sm[i + 1] ?? -1) && sm[i] >= 2) peaks.push({ t: t0 + (i + 0.5) * p.strokeBinS, v: sm[i] });
  }
  peaks.sort((a, b) => b.v - a.v);
  const kept: { t: number; v: number }[] = [];
  for (const q of peaks) {
    if (kept.length >= p.maxStrokes) break;
    if (kept.every((k) => Math.abs(k.t - q.t) >= p.strokeSepS)) kept.push(q);
  }
  kept.sort((a, b) => a.t - b.t);
  const vmax = kept.length ? Math.max(...kept.map((k) => k.v)) : 1;
  // OBSERVED timing: the peaks of the source rate (bursts of the channel's growth)
  const out: Stroke[] = kept.map((k, i) => ({ tc: toClip(Math.min(k.t, t1)), a: Math.min(1.4, (0.5 + 0.5 * (k.v / vmax)) * (i === 0 ? 1.15 : 1)), modelled: false }));
  if (!out.length) out.push({ tc: toClip(t1), a: 1.1, modelled: false });
  // MODELLED: a flash that has fewer than three bright moments gets re-brightenings after its last source (the channel
  // is re-lit at about 80 ms intervals, a little weaker each time), so that every flash flickers over 0.2 to 0.5 s
  const first = out[0].a;
  const tEnd = toClip(t1);
  let t = Math.max(out[out.length - 1].tc, tEnd);
  for (let k = 1; out.length < Math.min(3, p.maxStrokes); k++) {
    t += 0.08 + 0.015 * k;
    out.push({ tc: t, a: first * Math.pow(0.72, k), modelled: true });
  }
  return out;
}

/**
 * Skeleton of one flash: the sources in time order, merged where they are within `mergeKm`, each joined to its nearest
 * earlier node within `gateKm`; the branches (a chain follows the heavier child at a fork) and the importance of every edge.
 * Null for a flash that has no line (fewer than two nodes in one tree).
 */
export function compileChannel(f: LmaFlash, toClip: (td: number) => number, p: ChannelParams = CHANNEL_DEFAULTS): Channel | null {
  const rows = [...f.s].sort((a, b) => a[0] - b[0]);
  if (rows.length < 2) return null;
  const nx: number[] = [];
  const ny: number[] = [];
  const nz: number[] = [];
  const ntd: number[] = [];
  const par: number[] = [];
  const wgt: number[] = [];
  const m2 = p.mergeKm * p.mergeKm;
  const g2 = p.gateKm * p.gateKm;
  for (const r of rows) {
    let best = -1;
    let bd = Infinity;
    for (let i = 0; i < nx.length; i++) {
      const dx = nx[i] - r[1];
      const dy = ny[i] - r[2];
      const dz = nz[i] - r[3];
      const d = dx * dx + dy * dy + dz * dz;
      if (d < bd) {
        bd = d;
        best = i;
      }
    }
    if (best >= 0 && bd < m2) {
      wgt[best]++;
      continue;
    }
    nx.push(r[1]);
    ny.push(r[2]);
    nz.push(r[3]);
    ntd.push(r[0]);
    par.push(best >= 0 && bd <= g2 ? best : -1);
    wgt.push(1);
  }
  const n = nx.length;
  if (n < 2 || par.every((q) => q < 0)) return null;
  // weight of the subtree under every node (children come after their parent)
  const sub = wgt.slice();
  for (let i = n - 1; i >= 0; i--) if (par[i] >= 0) sub[par[i]] += sub[i];
  const heavy = new Array<number>(n).fill(-1);
  for (let i = 0; i < n; i++) {
    const q = par[i];
    if (q >= 0 && (heavy[q] < 0 || sub[i] > sub[heavy[q]])) heavy[q] = i;
  }
  const total = wgt.reduce((a, b) => a + b, 0);
  const imp = new Float32Array(n);
  const lt = Math.log(1 + total);
  for (let i = 0; i < n; i++) imp[i] = Math.max(0.12, Math.min(1, Math.pow(Math.log(1 + sub[i]) / lt, 1.3)));
  const chains: Int32Array[] = [];
  for (let i = 0; i < n; i++) {
    if (par[i] >= 0 && heavy[par[i]] === i) continue; // continues its parent's chain
    const list: number[] = par[i] >= 0 ? [par[i], i] : [i];
    let cur = i;
    while (heavy[cur] >= 0) {
      cur = heavy[cur];
      list.push(cur);
    }
    if (list.length >= 2) chains.push(Int32Array.from(list));
  }
  const ch: Channel = {
    id: f.id, tc0: 0, tcEnd: 0, n, nSources: rows.length,
    x: Float32Array.from(nx), y: Float32Array.from(ny), z: Float32Array.from(nz), tc: new Float64Array(n),
    parent: Int32Array.from(par), imp, chains, strokes: [],
  };
  for (let i = 0; i < n; i++) ch.tc[i] = toClip(ntd[i]);
  ch.tc0 = toClip(rows[0][0]);
  ch.tcEnd = ch.tc[n - 1];
  ch.strokes = strokeTimes(rows.map((r) => r[0]), toClip, p);
  return ch;
}

/** The channels of all flashes that have a line, ordered by their first source. */
export function compileChannels(flashes: LmaFlash[], toClip: (td: number) => number, p: ChannelParams = CHANNEL_DEFAULTS): Channel[] {
  const out: Channel[] = [];
  for (const f of flashes) {
    const c = compileChannel(f, toClip, p);
    if (c) out.push(c);
  }
  return out.sort((a, b) => a.tc0 - b.tc0);
}

/** The longest span (first source to afterglow end), clip seconds: bounds the scan in `visibleChannels`. */
export function longestChannel(chs: Channel[], p: ChannelParams = CHANNEL_DEFAULTS): number {
  let m = 0;
  for (const c of chs) m = Math.max(m, c.tcEnd - c.tc0);
  return m + p.afterglowS;
}

export function visibleChannels(chs: Channel[], t: number, span: number): Channel[] {
  let lo = 0;
  let hi = chs.length;
  while (lo < hi) {
    const m = (lo + hi) >> 1;
    if (chs[m].tc0 < t - span) lo = m + 1;
    else hi = m;
  }
  const out: Channel[] = [];
  for (let k = lo; k < chs.length && chs[k].tc0 <= t; k++) out.push(chs[k]);
  return out;
}

// ─── time ───────────────────────────────────────────────────────────────────

/** Fraction (0..1) of the edge parent -> `node` that has grown at clip time `t`: it ends when the node's source appears. */
export function edgeGrowth(ch: Channel, node: number, t: number, p: ChannelParams = CHANNEL_DEFAULTS): number {
  const end = ch.tc[node];
  const par = ch.parent[node];
  const start = par >= 0 ? Math.max(ch.tc[par], end - p.growMaxS) : end - p.growMaxS;
  if (t >= end) return 1;
  if (t <= start) return 0;
  return (t - start) / Math.max(1e-6, end - start);
}

function smooth01(x: number): number {
  const q = Math.max(0, Math.min(1, x));
  return q * q * (3 - 2 * q);
}

/** A return stroke: a fast rise and a decay (long enough to show in a frame at 30 fps). */
export function strokePulse(x: number): number {
  return x <= 0 ? 0 : (1 - Math.exp(-x / 0.015)) * Math.exp(-x / 0.1);
}

/** Brightness 0..1.25 (before the importance of the edge) of the edge that ends at `node`, at clip time `t`. */
export function edgeBrightness(ch: Channel, node: number, t: number, p: ChannelParams = CHANNEL_DEFAULTS): number {
  const end = ch.tc[node];
  const par = ch.parent[node];
  const start = par >= 0 ? Math.max(ch.tc[par], end - p.growMaxS) : end - p.growMaxS;
  if (t <= start) return 0;
  const age = t - end;
  if (age >= p.afterglowS) return 0;
  const fade = 1 - smooth01((age - p.afterglowS * 0.6) / (p.afterglowS * 0.4));
  let b = age < 0 ? p.leader : p.leader * Math.exp(-age / 0.2);
  for (const s of ch.strokes) if (end <= s.tc + 1e-9) b += s.a * strokePulse(t - s.tc);
  return Math.min(1.25, b) * fade;
}

/** Brightness of the edge including its importance (the trunk full, a twig faint). */
export function edgeLevel(ch: Channel, node: number, t: number, p: ChannelParams = CHANNEL_DEFAULTS): number {
  return edgeBrightness(ch, node, t, p) * (0.25 + 0.75 * ch.imp[node]);
}

export interface Streak {
  ax: number;
  ay: number;
  bx: number;
  by: number;
  /** 0..1 */
  b: number;
}

/** Straight edges (plan view) of the channels at clip time `t`, grown part only, for the maps: short faint line segments, no markers. */
export function planStreaks(chs: Channel[], t: number, span: number, p: ChannelParams = CHANNEL_DEFAULTS, minB = 0.04): Streak[] {
  const out: Streak[] = [];
  for (const ch of visibleChannels(chs, t, span)) {
    for (let i = 0; i < ch.n; i++) {
      const q = ch.parent[i];
      if (q < 0) continue;
      const b = Math.min(1, edgeLevel(ch, i, t, p));
      if (b < minB) continue;
      const g = edgeGrowth(ch, i, t, p);
      if (g <= 0) continue;
      out.push({ ax: ch.x[q], ay: ch.y[q], bx: ch.x[q] + (ch.x[i] - ch.x[q]) * g, by: ch.y[q] + (ch.y[i] - ch.y[q]) * g, b });
    }
  }
  return out;
}

// ─── geometry ───────────────────────────────────────────────────────────────

type V3 = [number, number, number];

/** Centripetal Catmull-Rom (Barry-Goldman) through p1 -> p2 at u in 0..1; p0 and p3 are the neighbours. */
export function splineAt(p0: V3, p1: V3, p2: V3, p3: V3, u: number): V3 {
  const d = (a: V3, b: V3) => Math.max(1e-6, Math.pow(Math.hypot(b[0] - a[0], b[1] - a[1], b[2] - a[2]), 0.5));
  const t0 = 0;
  const t1 = t0 + d(p0, p1);
  const t2 = t1 + d(p1, p2);
  const t3 = t2 + d(p2, p3);
  const t = t1 + u * (t2 - t1);
  const mix = (a: V3, b: V3, ta: number, tb: number): V3 => {
    const w = (t - ta) / (tb - ta);
    return [a[0] + (b[0] - a[0]) * w, a[1] + (b[1] - a[1]) * w, a[2] + (b[2] - a[2]) * w];
  };
  const a1 = mix(p0, p1, t0, t1);
  const a2 = mix(p1, p2, t1, t2);
  const a3 = mix(p2, p3, t2, t3);
  const b1 = mix(a1, a2, t0, t2);
  const b2 = mix(a2, a3, t1, t3);
  return mix(b1, b2, t1, t2);
}

function hash32(a: number, b: number, c: number, d: number): number {
  let h = Math.imul(a | 0, 0x9e3779b1) ^ Math.imul(b | 0, 0x85ebca6b) ^ Math.imul(c | 0, 0xc2b2ae35) ^ Math.imul(d | 0, 0x27d4eb2f);
  h ^= h >>> 15;
  h = Math.imul(h, 0x2c1b3c6d);
  h ^= h >>> 12;
  h = Math.imul(h, 0x297a2d39);
  h ^= h >>> 15;
  return h >>> 0;
}

/** Two standard normals (sum of four uniforms each) from the hash of a point: the same for the same flash, chain, piece and level. */
export function gauss2(a: number, b: number, c: number, d: number): [number, number] {
  let s = hash32(a, b, c, d);
  const u = () => {
    s = Math.imul(s ^ (s >>> 16), 0x45d9f3b) >>> 0;
    s = (s ^ (s >>> 13)) >>> 0;
    return (s & 0xffffff) / 16777216;
  };
  const g = () => (u() + u() + u() + u() - 2) / 0.5774;
  return [g(), g()];
}

/** The nodes of a chain as scene points, with the reflected neighbours at both ends. */
function chainPoints(ch: Channel, c: number): V3[] {
  const nodes = ch.chains[c];
  const pts: V3[] = Array.from(nodes, (i) => [ch.x[i], ch.y[i], ch.z[i]] as V3);
  const k = pts.length;
  const first: V3 = [2 * pts[0][0] - pts[1][0], 2 * pts[0][1] - pts[1][1], 2 * pts[0][2] - pts[1][2]];
  const last: V3 = [2 * pts[k - 1][0] - pts[k - 2][0], 2 * pts[k - 1][1] - pts[k - 2][1], 2 * pts[k - 1][2] - pts[k - 2][2]];
  return [first, ...pts, last];
}

/**
 * Polyline of the j-th piece of chain `c` (node j -> node j+1) refined to `depth` levels, as [x, y, z, u] quadruples in scene km
 * (z is the real height, not yet exaggerated) for u from 0 to `uCut` (the last point is cut exactly there). The ends are the real
 * node positions; every inner point is the spline plus a seeded displacement perpendicular to the piece.
 */
export function tessellate(ch: Channel, c: number, j: number, depth: number, uCut = 1, p: ChannelParams = CHANNEL_DEFAULTS, pts?: V3[]): number[] {
  const P = pts ?? chainPoints(ch, c);
  const p0 = P[j];
  const p1 = P[j + 1];
  const p2 = P[j + 2];
  const p3 = P[j + 3];
  const out: number[] = [p1[0], p1[1], p1[2], 0];
  const seed = ch.id * 131 + c * 17 + j;
  const rec = (ua: number, sa: V3, ub: number, sb: V3, level: number, idx: number) => {
    if (level >= depth || ua >= uCut) return;
    const tx = sb[0] - sa[0];
    const ty = sb[1] - sa[1];
    const tz = sb[2] - sa[2];
    const len = Math.hypot(tx, ty, tz);
    if (len < 2 * p.minPieceKm) return;
    const um = 0.5 * (ua + ub);
    const sm = splineAt(p0, p1, p2, p3, um);
    const sig = Math.min(p.jitterK * len, p.jitterCapKm);
    // basis perpendicular to the chord
    let rx = 0;
    let ry = 0;
    let rz = 1;
    if (Math.abs(tz) > 0.9 * len) {
      rx = 1;
      rz = 0;
    }
    let e1x = ty * rz - tz * ry;
    let e1y = tz * rx - tx * rz;
    let e1z = tx * ry - ty * rx;
    const l1 = Math.hypot(e1x, e1y, e1z) || 1;
    e1x /= l1;
    e1y /= l1;
    e1z /= l1;
    const e2x = (ty * e1z - tz * e1y) / len;
    const e2y = (tz * e1x - tx * e1z) / len;
    const e2z = (tx * e1y - ty * e1x) / len;
    const [g1, g2] = gauss2(seed, level + 1, idx, 7);
    const pm: V3 = [sm[0] + sig * (g1 * e1x + g2 * e2x), sm[1] + sig * (g1 * e1y + g2 * e2y), sm[2] + sig * (g1 * e1z + g2 * e2z)];
    rec(ua, sa, um, sm, level + 1, idx * 2);
    if (um < uCut) out.push(pm[0], pm[1], pm[2], um);
    rec(um, sm, ub, sb, level + 1, idx * 2 + 1);
  };
  rec(0, p1, 1, p2, 0, 0);
  if (uCut >= 1) out.push(p2[0], p2[1], p2[2], 1);
  else {
    // cut exactly at uCut: interpolate between the last point before it and the spline point there
    const s = splineAt(p0, p1, p2, p3, uCut);
    out.push(s[0], s[1], s[2], uCut);
  }
  return out;
}

// ─── drawing ────────────────────────────────────────────────────────────────

export interface Cam {
  eye: [number, number, number];
  fwd: [number, number, number];
  right: [number, number, number];
  up: [number, number, number];
  tanX: number;
  tanY: number;
  w: number;
  h: number;
}

export type Projector = (cam: Cam, p: [number, number, number]) => [number, number, number] | null;

const scratch: (HTMLCanvasElement | null)[] = [null, null];

function scratchCanvas(i: number, w: number, h: number): HTMLCanvasElement {
  let c = scratch[i];
  if (!c) c = scratch[i] = document.createElement('canvas');
  if (c.width !== w || c.height !== h) {
    c.width = w;
    c.height = h;
  }
  return c;
}

/** Core width in px of a channel of importance `imp` at depth `d` km (perspective: the same physical width, clamped). */
export function channelWidthPx(d: number, imp: number, focalPx: number, u: number, p: ChannelParams = CHANNEL_DEFAULTS): { w: number; dim: number } {
  const raw = ((p.widthM / 1000) * Math.pow(imp, 0.6) * focalPx) / Math.max(d, 0.02);
  const w = Math.max(p.minPx * u, Math.min(p.maxPx * u, raw));
  return { w, dim: Math.min(1, 0.72 + 0.28 * (raw / (p.minPx * u))) };
}

/**
 * Draw the channels at clip time `t`: thin white-blue cores with a violet-blue glow, added (no dots, no sprites). The channel grows
 * along its sources in time order, flashes at the return strokes and fades. Returns the number of pieces drawn.
 */
export function drawChannels(
  ctx: CanvasRenderingContext2D, cam: Cam, chs: Channel[], t: number, span: number,
  project: Projector, vexag: number, gain = 1, p: ChannelParams = CHANNEL_DEFAULTS,
): number {
  const vis = visibleChannels(chs, t, span);
  if (!vis.length) return 0;
  const u = cam.h / 1080;
  const focal = cam.w / 2 / cam.tanX;
  const core = scratchCanvas(0, cam.w, cam.h).getContext('2d')!;
  const glow = scratchCanvas(1, cam.w, cam.h).getContext('2d')!;
  for (const c of [core, glow]) {
    c.globalCompositeOperation = 'source-over';
    c.clearRect(0, 0, cam.w, cam.h);
    c.globalCompositeOperation = 'lighter';
    c.lineCap = 'butt';
    c.lineJoin = 'round';
  }
  let drawn = 0;
  let peak = 0;
  const pad = 60;
  for (const ch of vis) {
    for (let c = 0; c < ch.chains.length; c++) {
      const nodes = ch.chains[c];
      let P: V3[] | null = null;
      for (let j = 0; j + 1 < nodes.length; j++) {
        const end = nodes[j + 1];
        const g = edgeGrowth(ch, end, t, p);
        if (g <= 0) continue;
        const b = edgeLevel(ch, end, t, p) * gain;
        if (b < 0.03) continue;
        const a = project(cam, [ch.x[nodes[j]], ch.y[nodes[j]], ch.z[nodes[j]] * vexag]);
        const bb = project(cam, [ch.x[end], ch.y[end], ch.z[end] * vexag]);
        if (!a && !bb) continue;
        const dMid = Math.max(0.03, ((a?.[2] ?? bb![2]) + (bb?.[2] ?? a![2])) / 2);
        const lenKm = Math.hypot(ch.x[end] - ch.x[nodes[j]], ch.y[end] - ch.y[nodes[j]], ch.z[end] - ch.z[nodes[j]]);
        const px = (lenKm * focal) / dMid / u;
        const depth = Math.max(p.minDepth, Math.min(p.maxDepth, Math.ceil(Math.log2(Math.max(1, px / p.pxPerPiece)))));
        P = P ?? chainPoints(ch, c);
        const pts = tessellate(ch, c, j, depth, g, p, P);
        const { w, dim } = channelWidthPx(dMid, ch.imp[end], focal, u, p);
        const al = Math.min(1, b) * dim;
        let moved = false;
        core.beginPath();
        glow.beginPath();
        let any = false;
        let prev: [number, number, number] | null = null;
        for (let k = 0; k < pts.length; k += 4) {
          const q = project(cam, [pts[k], pts[k + 1], pts[k + 2] * vexag]);
          if (q && prev && !(q[0] < -pad && prev[0] < -pad) && !(q[0] > cam.w + pad && prev[0] > cam.w + pad) && !(q[1] < -pad && prev[1] < -pad) && !(q[1] > cam.h + pad && prev[1] > cam.h + pad)) {
            if (!moved) {
              core.moveTo(prev[0], prev[1]);
              glow.moveTo(prev[0], prev[1]);
              moved = true;
            }
            core.lineTo(q[0], q[1]);
            glow.lineTo(q[0], q[1]);
            any = true;
          } else moved = false;
          prev = q;
        }
        if (!any) continue;
        core.strokeStyle = `rgba(${Math.round(222 + 33 * Math.min(1, b))},${Math.round(232 + 23 * Math.min(1, b))},255,${al.toFixed(3)})`;
        core.lineWidth = w;
        core.stroke();
        glow.strokeStyle = `rgba(118,104,255,${(al * 0.55).toFixed(3)})`;
        glow.lineWidth = w * 3 + 2 * u;
        glow.stroke();
        drawn++;
        peak = Math.max(peak, al);
      }
    }
  }
  if (!drawn) return 0;
  ctx.save();
  ctx.globalCompositeOperation = 'lighter';
  const sc = scratchCanvas(0, cam.w, cam.h);
  const sg = scratchCanvas(1, cam.w, cam.h);
  // the glow: a wide soft pass and a tight one, then the bloom of the core, then the core itself
  ctx.filter = `blur(${Math.round(20 * u)}px)`;
  ctx.globalAlpha = Math.min(1, 0.85 * Math.max(0.3, peak));
  ctx.drawImage(sg, 0, 0);
  ctx.filter = `blur(${Math.round(6 * u)}px)`;
  ctx.globalAlpha = Math.min(1, 0.9 * Math.max(0.3, peak));
  ctx.drawImage(sg, 0, 0);
  ctx.filter = `blur(${Math.round(3 * u)}px)`;
  ctx.globalAlpha = Math.min(1, 0.6 * peak);
  ctx.drawImage(sc, 0, 0);
  ctx.filter = 'none';
  ctx.globalAlpha = 1;
  ctx.drawImage(sc, 0, 0);
  ctx.restore();
  return drawn;
}
