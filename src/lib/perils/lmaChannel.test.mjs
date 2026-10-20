// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  compileChannel, compileChannels, longestChannel, visibleChannels, edgeGrowth, edgeBrightness, edgeLevel, strokePulse, planStreaks,
  splineAt, tessellate, gauss2, channelWidthPx, drawChannels, CHANNEL_DEFAULTS,
} from './lmaChannel.ts';

const P = CHANNEL_DEFAULTS;
const toClip = (td) => td / 4;

// a flash with a trunk going up and east and one real fork: [t, x, y, z, power]
const trunk = [];
for (let i = 0; i < 12; i++) trunk.push([i * 0.01, i * 0.4, 0.1 * Math.sin(i), 6 + i * 0.25, 5]);
const fork = [];
for (let i = 1; i <= 4; i++) fork.push([0.07 + i * 0.01, 2.0 + 0.1 * i, i * 0.5, 7.5 + i * 0.1, 3]);
const F = { id: 7, t0: 0, t1: 0.2, n: trunk.length + fork.length, s: [...trunk, ...fork] };

test('a source is joined to its nearest EARLIER source within the gate; the fork comes from the data', () => {
  const ch = compileChannel(F, toClip);
  assert.ok(ch);
  assert.equal(ch.parent[0], -1);
  for (let i = 1; i < ch.n; i++) {
    const q = ch.parent[i];
    assert.ok(q < i, 'the parent is earlier');
    const d = (a, b) => Math.hypot(ch.x[a] - ch.x[b], ch.y[a] - ch.y[b], ch.z[a] - ch.z[b]);
    for (let k = 0; k < i; k++) assert.ok(q < 0 ? d(i, k) > P.gateKm - 1e-9 : d(i, q) <= d(i, k) + 1e-6, 'nearest earlier');
  }
  // the fork leaves the trunk: some node has two children
  const kids = new Map();
  for (let i = 0; i < ch.n; i++) if (ch.parent[i] >= 0) kids.set(ch.parent[i], (kids.get(ch.parent[i]) ?? 0) + 1);
  assert.ok([...kids.values()].some((c) => c >= 2), 'a branch point exists');
  assert.ok(ch.chains.length >= 2, 'the trunk and the branch are separate chains');
});

test('no channel is invented: a lone source or a source beyond the gate has no line', () => {
  assert.equal(compileChannel({ id: 1, t0: 0, t1: 0, n: 1, s: [[0, 0, 0, 5, 1]] }, toClip), null);
  assert.equal(compileChannel({ id: 2, t0: 0, t1: 0, n: 2, s: [[0, 0, 0, 5, 1], [0.01, 30, 0, 5, 1]] }, toClip), null);
  // two sources 40 m apart are one node of the skeleton (the LMA's own location error)
  assert.equal(compileChannel({ id: 3, t0: 0, t1: 0, n: 2, s: [[0, 0, 0, 5, 1], [0.01, 0.04, 0, 5, 1]] }, toClip), null);
});

test('the trunk is the most important edge and a twig is fainter; every edge is in exactly one chain', () => {
  const ch = compileChannel(F, toClip);
  const edges = new Set();
  for (const c of ch.chains) for (let j = 1; j < c.length; j++) edges.add(c[j]);
  let nEdges = 0;
  for (let i = 0; i < ch.n; i++) if (ch.parent[i] >= 0) nEdges++;
  assert.equal(edges.size, nEdges);
  const total = ch.chains.reduce((a, c) => a + c.length - 1, 0);
  assert.equal(total, nEdges, 'no edge twice');
  const imps = [...edges].map((i) => ch.imp[i]);
  assert.ok(Math.max(...imps) > 0.8 && Math.min(...imps) < Math.max(...imps));
});

test('growth follows the time order of the sources; nothing is lit before its source', () => {
  const ch = compileChannel(F, toClip);
  for (let i = 1; i < ch.n; i++) {
    assert.equal(edgeGrowth(ch, i, ch.tc[i] + 1e-9, P), 1);
    assert.equal(edgeGrowth(ch, i, ch.tc[i] - P.growMaxS - 1e-6, P), 0);
    assert.equal(edgeBrightness(ch, i, ch.tc[i] - P.growMaxS - 1e-6, P), 0);
    let prev = 0;
    for (let k = 0; k <= 20; k++) {
      const g = edgeGrowth(ch, i, ch.tc[i] - P.growMaxS + (k / 20) * P.growMaxS, P);
      assert.ok(g >= prev - 1e-12);
      prev = g;
    }
  }
  // a later edge never starts before the edge it hangs from has appeared
  for (let i = 1; i < ch.n; i++) assert.ok(ch.tc[i] >= ch.tc[ch.parent[i]]);
});

test('return strokes: two to four, at the real source bursts, each lights the existing channel; the glow ends 0.3 to 0.8 s later', () => {
  const ch = compileChannel(F, toClip);
  assert.ok(ch.strokes.length >= 2 && ch.strokes.length <= P.maxStrokes);
  for (const s of ch.strokes) assert.ok(s.tc >= ch.tc0 - 1e-9 && (s.modelled || s.tc <= ch.tcEnd + 1e-9), 'a real stroke is inside the flash; a modelled one comes after the last source');
  assert.ok(ch.strokes.some((s) => !s.modelled), 'the first brightening is at the real source rate');
  assert.ok(ch.strokes.at(-1).tc - ch.strokes[0].tc <= 0.5 + (ch.tcEnd - ch.tc0), 'the strokes span at most 0.5 s after the growth');
  assert.ok(strokePulse(-0.1) === 0 && strokePulse(0.04) > strokePulse(0.4));
  const s = ch.strokes[0];
  const edge = 1;
  const before = edgeBrightness(ch, edge, Math.max(ch.tc[edge] + 0.0, s.tc - 0.2), P);
  const at = edgeBrightness(ch, edge, s.tc + 0.04, P);
  assert.ok(at > before, 'the stroke brightens the channel');
  assert.equal(edgeBrightness(ch, edge, ch.tc[edge] + P.afterglowS + 0.01, P), 0);
  assert.ok(P.afterglowS >= 0.3 && P.afterglowS <= 0.8);
  assert.ok(edgeLevel(ch, 1, s.tc + 0.04, P) <= edgeBrightness(ch, 1, s.tc + 0.04, P) + 1e-12);
});

test('the visible channels are the ones that started within the span; plan streaks are line segments between real nodes', () => {
  const chs = compileChannels([F, { ...F, id: 8, s: F.s.map((r) => [r[0] + 40, r[1], r[2], r[3], r[4]]) }], toClip);
  assert.equal(chs.length, 2);
  const span = longestChannel(chs);
  assert.equal(visibleChannels(chs, chs[0].tc0 + 0.1, span).length, 1);
  assert.equal(visibleChannels(chs, chs[0].tc0 - 1, span).length, 0);
  const st = planStreaks(chs, chs[0].tcEnd + 0.01, span);
  assert.ok(st.length > 5);
  for (const q of st) assert.ok(q.b > 0 && q.b <= 1 && Number.isFinite(q.ax + q.ay + q.bx + q.by));
  assert.equal(planStreaks(chs, chs[0].tcEnd + 5, span).length, 0, 'gone after the afterglow');
});

test('the spline passes exactly through the real positions and stays near the chord', () => {
  const ch = compileChannel(F, toClip);
  const c = ch.chains[0];
  for (let j = 0; j + 1 < c.length; j++) {
    const t = tessellate(ch, 0, j, 4, 1, P);
    assert.deepEqual([t[0], t[1], t[2]].map(Math.fround), [ch.x[c[j]], ch.y[c[j]], ch.z[c[j]]]);
    const e = t.length - 4;
    assert.deepEqual([t[e], t[e + 1], t[e + 2]].map(Math.fround), [ch.x[c[j + 1]], ch.y[c[j + 1]], ch.z[c[j + 1]]]);
  }
  const a = [0, 0, 0];
  const b = [1, 0, 0];
  const s = splineAt([-1, 0.2, 0], a, b, [2, -0.2, 0], 0.5);
  assert.ok(Math.abs(s[1]) < 0.1 && s[0] > 0.4 && s[0] < 0.6);
});

test('fine jaggedness: seeded, bounded by 50 m, finer levels never move the coarse points', () => {
  const ch = compileChannel(F, toClip);
  const a = tessellate(ch, 0, 3, 3, 1, P);
  const b = tessellate(ch, 0, 3, 6, 1, P);
  assert.deepEqual(tessellate(ch, 0, 3, 3, 1, P), a, 'same input, same line (no flicker)');
  const bset = new Map();
  for (let k = 0; k < b.length; k += 4) bset.set(b[k + 3], [b[k], b[k + 1], b[k + 2]]);
  for (let k = 0; k < a.length; k += 4) {
    const q = bset.get(a[k + 3]);
    assert.ok(q, 'a coarse point is also in the fine line');
    assert.ok(Math.hypot(q[0] - a[k], q[1] - a[k + 1], q[2] - a[k + 2]) < 1e-9);
  }
  assert.ok(b.length > a.length);
  // distance of every point from the spline is at most a few sigma of the cap (km)
  const c = ch.chains[0];
  const pts = [[...[2 * ch.x[c[0]] - ch.x[c[1]], 2 * ch.y[c[0]] - ch.y[c[1]], 2 * ch.z[c[0]] - ch.z[c[1]]]], ...Array.from(c, (i) => [ch.x[i], ch.y[i], ch.z[i]])];
  const n = c.length;
  pts.push([2 * pts[n][0] - pts[n - 1][0], 2 * pts[n][1] - pts[n - 1][1], 2 * pts[n][2] - pts[n - 1][2]]);
  for (let k = 0; k < b.length; k += 4) {
    const s = splineAt(pts[3], pts[4], pts[5], pts[6], b[k + 3]);
    assert.ok(Math.hypot(b[k] - s[0], b[k + 1] - s[1], b[k + 2] - s[2]) < 4.5 * P.jitterCapKm, 'inside the LMA location uncertainty (about 100 m)');
  }
  const g = gauss2(1, 2, 3, 4);
  assert.deepEqual(gauss2(1, 2, 3, 4), g);
  let s1 = 0;
  let s2 = 0;
  const N = 4000;
  for (let i = 0; i < N; i++) {
    const q = gauss2(i, 3, 5, 7);
    s1 += q[0];
    s2 += q[0] * q[0];
  }
  assert.ok(Math.abs(s1 / N) < 0.06 && Math.abs(s2 / N - 1) < 0.1, 'standard normal');
});

test('a cut tessellation ends exactly at the growth fraction', () => {
  const ch = compileChannel(F, toClip);
  const t = tessellate(ch, 0, 2, 5, 0.4, P);
  assert.ok(Math.abs(t[t.length - 1] - 0.4) < 1e-12);
  for (let k = 3; k < t.length; k += 4) assert.ok(t[k] <= 0.4 + 1e-12);
  for (let k = 7; k < t.length; k += 4) assert.ok(t[k] > t[k - 4], 'u increases');
});

test('width: thin hairs far away, thicker close up, a twig thinner than the trunk', () => {
  const f = 1663;
  const far = channelWidthPx(40, 1, f, 1).w;
  const mid = channelWidthPx(4, 1, f, 1).w;
  const near = channelWidthPx(0.6, 1, f, 1).w;
  assert.ok(far >= 1 && far <= 3 && mid > far && near > mid && near <= P.maxPx);
  assert.ok(channelWidthPx(4, 0.2, f, 1).w < mid);
});

test('drawing: lines only, never a disc, a gradient or a sprite', () => {
  const calls = { arc: 0, radial: 0, line: 0, stroke: 0, fillRect: 0, fill: 0, ellipse: 0 };
  const mk = () => ({
    clearRect() {}, beginPath() {}, moveTo() {}, lineTo() { calls.line++; }, stroke() { calls.stroke++; }, save() {}, restore() {},
    drawImage() {}, arc() { calls.arc++; }, ellipse() { calls.ellipse++; }, createRadialGradient() { calls.radial++; }, fill() { calls.fill++; }, fillRect() { calls.fillRect++; },
    set strokeStyle(v) {}, set lineWidth(v) {}, set globalCompositeOperation(v) {}, set lineCap(v) {}, set lineJoin(v) {}, set filter(v) {}, set globalAlpha(v) {},
  });
  const ctxs = [];
  globalThis.document = { createElement: () => ({ width: 0, height: 0, getContext: () => { const c = mk(); ctxs.push(c); return c; } }) };
  const ch = compileChannels([F], toClip);
  const eye = [-6, -9, 0.5];
  const tg = [2, 2, 7.5];
  const fl = Math.hypot(tg[0] - eye[0], tg[1] - eye[1], tg[2] - eye[2]);
  const fwd = tg.map((v, i) => (v - eye[i]) / fl);
  const rl = Math.hypot(fwd[1], fwd[0]);
  const right = [fwd[1] / rl, -fwd[0] / rl, 0];
  const up = [right[1] * fwd[2] - right[2] * fwd[1], right[2] * fwd[0] - right[0] * fwd[2], right[0] * fwd[1] - right[1] * fwd[0]];
  const cam = { eye, fwd, right, up, tanX: 0.577, tanY: 0.325, w: 1920, h: 1080 };
  const proj = (c, p) => {
    const vx = p[0] - c.eye[0];
    const vy = p[1] - c.eye[1];
    const vz = p[2] - c.eye[2];
    const d = vx * c.fwd[0] + vy * c.fwd[1] + vz * c.fwd[2];
    if (d < 0.02) return null;
    const px = vx * c.right[0] + vy * c.right[1] + vz * c.right[2];
    const py = vx * c.up[0] + vy * c.up[1] + vz * c.up[2];
    return [c.w / 2 + (px / d / c.tanX) * (c.w / 2), c.h / 2 - (py / d / c.tanY) * (c.h / 2), d];
  };
  const out = { arc: 0, radialGradient: 0 };
  const rootCtx = { ...mk(), createRadialGradient() { out.radialGradient++; }, arc() { out.arc++; } };
  const n = drawChannels(rootCtx, cam, ch, ch[0].tcEnd + 0.05, longestChannel(ch), proj, 1, 1);
  assert.ok(n > 5, 'pieces drawn');
  assert.ok(calls.line > 50, 'many line segments');
  assert.equal(calls.arc + calls.radial + calls.fill + calls.fillRect + calls.ellipse, 0);
  assert.equal(out.arc + out.radialGradient, 0);
  assert.equal(drawChannels(rootCtx, cam, ch, ch[0].tc0 - 5, longestChannel(ch), proj, 1, 1), 0, 'nothing before the flash');
});
