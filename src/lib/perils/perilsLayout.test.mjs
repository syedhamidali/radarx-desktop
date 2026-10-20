// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  FrameLayout, LabelPlacer, VISIBLE, FADE_S, ENTER_FRAMES, findViolations, firstFit, rectInside, rectsOverlap, yieldAlpha, defaultOffsets,
} from './perilsLayout.ts';
import {
  compassRect, legendRect, timelineRect, stationCardRect, tornadoCardRect, lmaRect, envRect, focusLowerThirdRect, tornadoLowerThirdRect, captionRect, seesRect, topLeftRects, endCardRect, groundKeyRect,
} from './perilsHud.ts';
import { insetHeight } from './perilsFocusInset.ts';

const measureFn = (text, px) => text.length * px * 0.5;
// a canvas context that only measures text (0.5 em per character), for the sizes of the cards
const ctx = {
  font: '',
  measureText(t) {
    const px = parseFloat(/(\d+(?:\.\d+)?)px/.exec(this.font)?.[1] ?? '16');
    return { width: t.length * px * 0.5 };
  },
};
// a small deterministic random generator
const rng = (seed) => () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296);

const model = (over = {}) => ({
  data: { time0: '2022-03-31T01:04:00Z', steps: [{ t_s: 0, radars: ['KGWX', 'NOXP', 'DOW7'], offsets_s: { KGWX: 4, NOXP: -16, DOW7: 4 } }, { t_s: 60, radars: [], offsets_s: {} }], sources: new Array(13).fill({}) },
  title: 'PERiLS IOP2: squall line and EF2 tornado',
  subtitle: 'Mississippi / Alabama, 30 March 2022 local evening · real radar, lightning, surface and sounding data',
  td: 195, speedup: 13.2, observed: -1, interval: { k: 0, s: 0.5 },
  motionNote: 'carried along the line motion 11 m/s toward 90° (phase correlation of the echo); not new observations',
  caption: null, sees: '',
  tornado: { rating: 'EF2', active: true, widthM: 366, interpolated: true, parent: null, windRangeMs: [50, 60], periodRangeS: [19, 23], periodS: 21, debris: true, resolutionM: 500, sim: { vt: 55, rm: 80, periodCore: 7.9, periodFunnel: 21, ground: 'procedural' } },
  groundKey: { temp: [15, 20], mrms: true, swath: true },
  ...over,
});

// ── the rules themselves ─────────────────────────────────────────────────────────────────────────────────────────────

test('rectangles overlap only when they share area; the frame check has a tolerance of half a pixel', () => {
  const a = { x: 0, y: 0, w: 10, h: 10 };
  assert.ok(rectsOverlap(a, { x: 9, y: 9, w: 5, h: 5 }));
  assert.ok(!rectsOverlap(a, { x: 10, y: 0, w: 5, h: 5 }));
  assert.ok(rectsOverlap(a, { x: 10, y: 0, w: 5, h: 5 }, 1), 'a margin counts');
  assert.ok(rectInside(a, 10, 10));
  assert.ok(!rectInside({ x: 0, y: 5, w: 10, h: 6 }, 10, 10));
  assert.ok(!rectInside(a, 10, 10, 2), 'the safe area');
});

test('the checker reports two visible items that overlap and an item that leaves the frame', () => {
  const item = (id, x, y, w, h, alpha = 1) => ({ id, kind: 'card', rect: { x, y, w, h }, alpha });
  assert.deepEqual(findViolations([item('a', 0, 0, 100, 100), item('b', 50, 50, 100, 100)], 1920, 1080), ['a overlaps b']);
  assert.deepEqual(findViolations([item('a', 0, 0, 100, 100), item('b', 100, 0, 100, 100)], 1920, 1080), []);
  assert.deepEqual(findViolations([item('c', 1900, 1000, 330, 128)], 1920, 1080), ['c leaves the frame']);
  assert.deepEqual(findViolations([item('a', 0, 0, 100, 100), item('b', 50, 50, 100, 100, 0.1)], 1920, 1080), [], 'a faint item is not counted');
  assert.deepEqual(findViolations([item('c', 1900, 1000, 330, 128, 0.1)], 1920, 1080), []);
});

test('the lower item gives way: it is gone when the higher one reaches a quarter of its opacity', () => {
  assert.equal(yieldAlpha(0.8, []), 0.8);
  assert.ok(yieldAlpha(1, [0.25]) < 1e-9);
  assert.ok(yieldAlpha(1, [0.1]) > 0.5);
  assert.equal(yieldAlpha(1, [1]), 0);
});

test('two cross-fading cards are never both visible over the same place, whatever the fade', () => {
  for (let k = 0; k <= 100; k++) {
    for (let j = 0; j <= 100; j++) {
      const L = new FrameLayout(1920, 1080, 1);
      L.card('high', { x: 100, y: 100, w: 400, h: 200 }, k / 100);
      L.card('low', { x: 300, y: 150, w: 400, h: 200 }, j / 100);
      assert.deepEqual(findViolations(L.items, 1920, 1080), [], `high ${k / 100}, low ${j / 100}`);
    }
  }
});

test('the lines of one card do not count against each other', () => {
  const L = new FrameLayout(1920, 1080, 1);
  L.card('title', [{ x: 0, y: 0, w: 300, h: 50 }, { x: 0, y: 40, w: 200, h: 50 }], 1);
  assert.deepEqual(findViolations(L.items, 1920, 1080), []);
  const a = L.card('other', { x: 100, y: 80, w: 50, h: 50 }, 1);
  assert.ok(a < 1e-9, 'another card that touches one of the lines gives way');
});

// ── the real cards of the HUD ────────────────────────────────────────────────────────────────────────────────────────

for (const [W, H] of [[1920, 1080], [1280, 720], [3840, 2160]]) {
  test(`the fixed cards of the HUD lie inside the ${W}x${H} frame and keep clear of each other`, () => {
    const u = H / 1080;
    const m = model();
    const L = new FrameLayout(W, H, u);
    L.card('legend', legendRect(W, H, u), 1);
    L.card('timeline', timelineRect(W, H, u), 1);
    L.card('title', topLeftRects(ctx, u, m), 1);
    L.card('compass', compassRect(W, u), 1);
    L.card('focusInset', { x: W - 34 * u - 408 * u, y: 46 * u, w: 416 * u, h: insetHeight(400 * u, u, false) }, 1);
    const cap = { title: 'EF2 tornado under a squall-line vortex', lines: ['a '.repeat(150), 'b '.repeat(150), 'c '.repeat(100)] };
    L.card('focusLT', focusLowerThirdRect(H, u, cap), 1);
    // nothing else may overlap what is registered: the compass is clear of the title block and of the insets
    assert.deepEqual(findViolations(L.items, W, H), []);
    const compass = L.items.find((i) => i.id === 'compass#0');
    assert.equal(compass.alpha, 1, 'the compass is not suppressed by the title block');
  });
}

test('the title block is as wide as its widest line, so that the compass can stand beside it', () => {
  const m = model();
  const u = 1;
  const rects = topLeftRects(ctx, u, m);
  const right = Math.max(...rects.map((r) => r.x + r.w));
  assert.ok(right > 600 && right < 900);
  // the compass strip is beside the title's line and above the subtitle's: no box of the block touches it
  assert.ok(compassRect(1920, u).every((c) => rects.every((r) => !rectsOverlap(r, c))));
});

test('the compass strip is a box with ticks: its rectangle holds the heading read-out too, inside the frame', () => {
  for (const W of [1280, 1920, 3840]) {
    const u = W / 1920;
    const [strip, text] = compassRect(W, u);
    assert.ok(rectInside(strip, W, 1080 * u) && rectInside(text, W, 1080 * u));
    assert.ok(Math.abs(strip.x + strip.w / 2 - W / 2) < 1 && Math.abs(text.x + text.w / 2 - W / 2) < 1);
  }
});

test('the counter goes to the first column where it fits entirely, never half out of the frame', () => {
  const u = 1;
  const H = 1080;
  const lmaH = 128;
  // the focus insets end at y = 1008: no room on the right above the credits, the left column takes it
  const yRight = 46 + insetHeight(400, 1, false) + 10;
  const fit = firstFit(lmaH, [{ side: 'right', y: yRight, bottom: H - 56 }, { side: 'left', y: 300 + 118 + 10, bottom: 690 }]);
  assert.equal(fit.side, 'left');
  const r = lmaRect(1920, u, fit.y, 34);
  assert.ok(rectInside(r, 1920, H));
  // with room, the right-hand column keeps it
  assert.equal(firstFit(lmaH, [{ side: 'right', y: 500, bottom: H - 56 }, { side: 'left', y: 428, bottom: 690 }]).side, 'right');
  // no room anywhere: not drawn
  assert.equal(firstFit(lmaH, [{ side: 'right', y: 1000, bottom: 1024 }, { side: 'left', y: 600, bottom: 690 }]), null);
  // and the old place (under the insets) was outside the frame
  assert.ok(!rectInside(lmaRect(1920, u, yRight), 1920, H));
});

test('the station card gives way to the lower thirds on the same ground, in every cross-fade', () => {
  const u = 1;
  const H = 1080;
  const m = model();
  for (let k = 0; k <= 20; k++) {
    for (let j = 0; j <= 20; j++) {
      const L = new FrameLayout(1920, H, u);
      L.card('tornadoLT', tornadoLowerThirdRect(H, u, m), k / 20);
      L.card('station', stationCardRect(H, u), j / 20);
      assert.deepEqual(findViolations(L.items, 1920, H), []);
    }
  }
  assert.ok(rectsOverlap(tornadoLowerThirdRect(H, u, m), stationCardRect(H, u)), 'they do share the place');
});

test('every card rectangle of the HUD is inside the frame', () => {
  const m = model({ caption: { title: 'x', numbers: 'n '.repeat(60), text: 't '.repeat(120), prov: 'observed', alpha: 1 }, sees: 'EF2 tornado on the ground (surveyed path) · radar: parent circulation Vrot 17 m/s' });
  for (const [W, H] of [[1920, 1080], [1280, 720]]) {
    const u = H / 1080;
    const rects = {
      legend: legendRect(W, H, u), timeline: timelineRect(W, H, u), station: stationCardRect(H, u), tornadoCard: tornadoCardRect(u, 280 * u), lma: lmaRect(W, u, 28 * u),
      env: envRect(W, u, 400 * u, { rows: new Array(6).fill(['a', 'b']), note: 'n' }), tornadoLT: tornadoLowerThirdRect(H, u, m), caption: captionRect(W, H, u, m),
      endCard: endCardRect(W, u, m), groundKey: groundKeyRect(u, m, 280 * u),
    };
    for (const [id, r] of Object.entries(rects)) assert.ok(r && rectInside(r, W, H), `${id} at ${W}x${H}: ${JSON.stringify(r)}`);
    const s = seesRect(ctx, W, H, u, model({ sees: m.sees }));
    assert.ok(s && rectInside(s, W, H));
  }
});

// ── the labels ───────────────────────────────────────────────────────────────────────────────────────────────────────

const req = (id, ax, ay, over = {}) => ({ id, text: id.padEnd(12, 'x'), ax, ay, size: 15, colour: 'rgba(255,255,255,1)', priority: 50, alpha: 1, ...over });

test('a label is placed beside its anchor and inside the frame; the importance decides who keeps a place', () => {
  const pl = new LabelPlacer(measureFn);
  const L = new FrameLayout(1920, 1080, 1);
  const out = pl.place([req('low', 500, 500, { priority: 10 }), req('high', 500, 500, { priority: 90 })], L, 0);
  const high = out.find((d) => d.id === 'high');
  assert.ok(high && high.alpha > 0.99);
  assert.equal(high.x, 510);
  const low = out.find((d) => d.id === 'low');
  assert.ok(!low || !rectsOverlap(low.rect, high.rect), 'the lower one was moved or dropped');
});

test('a label near the frame edge is moved inside, and one with its anchor far outside is dropped', () => {
  const pl = new LabelPlacer(measureFn);
  const L = new FrameLayout(1920, 1080, 1);
  const out = pl.place([req('edge', 1900, 400), req('gone', 3000, 400)], L, 0);
  const e = out.find((d) => d.id === 'edge');
  assert.ok(e && rectInside(e.rect, 1920, 1080));
  assert.ok(!out.some((d) => d.id === 'gone'));
});

test('a label never overlaps a visible card; it takes another place around its anchor or is dropped', () => {
  const pl = new LabelPlacer(measureFn);
  const L = new FrameLayout(1920, 1080, 1);
  L.card('inset', { x: 500, y: 400, w: 400, h: 300 }, 1);
  const out = pl.place([req('a', 495, 500), req('b', 600, 500), req('c', 700, 395)], L, 0);
  for (const d of out) assert.ok(!rectsOverlap(d.rect, { x: 500, y: 400, w: 400, h: 300 }), `${d.id} is clear of the card`);
});

test('at most `maxLabels` labels are shown, the most important first', () => {
  const pl = new LabelPlacer(measureFn, 3);
  const L = new FrameLayout(1920, 1080, 1);
  const reqs = [];
  for (let i = 0; i < 10; i++) reqs.push(req(`l${i}`, 100 + i * 150, 300, { priority: i }));
  const out = pl.place(reqs, L, 0);
  assert.deepEqual(out.map((d) => d.id).sort(), ['l7', 'l8', 'l9']);
});

test('a label that has no free place near its anchor slides clear of the card and is tied to its anchor (the track label over the timeline)', () => {
  const pl = new LabelPlacer(measureFn);
  const L = new FrameLayout(1920, 1080, 1);
  L.card('timeline', timelineRect(1920, 1080, 1), 1);
  const out = pl.place([req('EF2 starts', 1113, 955, { slide: true, priority: 100 }), req('plain', 1500, 955, { priority: 50 })], L, 0);
  const s = out.find((d) => d.id === 'EF2 starts');
  assert.ok(s && s.leader, 'it slid up and has a leader line');
  assert.ok(!rectsOverlap(s.rect, timelineRect(1920, 1080, 1)));
  assert.ok(rectInside(s.rect, 1920, 1080));
  assert.ok(!out.some((d) => d.id === 'plain' && rectsOverlap(d.rect, timelineRect(1920, 1080, 1))), 'a plain label is dropped rather than slid');
});

test('labels fade at a fixed rate: no pop in, no pop out, and a head start for the label that is shown', () => {
  const pl = new LabelPlacer(measureFn);
  const dt = 1 / 30;
  let alpha = [];
  for (let f = 0; f < 30; f++) {
    const L = new FrameLayout(1920, 1080, 1);
    const out = pl.place([req('a', 500, 500)], L, f * dt);
    alpha.push(out[0]?.alpha ?? 0);
  }
  // the first frame of a run (a jump) takes the target; to see the fade start a second label later
  const pl2 = new LabelPlacer(measureFn);
  const seq = [];
  for (let f = 0; f < 40; f++) {
    const L = new FrameLayout(1920, 1080, 1);
    const out = pl2.place(f < 3 ? [req('base', 100, 100)] : [req('base', 100, 100), req('late', 900, 500)], L, f * dt);
    seq.push(out.find((d) => d.id === 'late')?.alpha ?? 0);
  }
  // the late label waits ENTER_FRAMES frames for its place (no flashes), then fades in at dt / FADE_S per frame
  const t0 = 3 + ENTER_FRAMES - 1;
  for (let f = 0; f < t0; f++) assert.equal(seq[f], 0, `frame ${f}`);
  assert.ok(seq[t0] > 0 && seq[t0] < 0.2, 'it starts at 1/9 of the way (the step is dt / FADE_S)');
  for (let f = t0 + 1; f < t0 + 10; f++) assert.ok(seq[f] >= seq[f - 1] - 1e-9, 'monotone in');
  assert.ok(Math.abs(seq[t0 + Math.ceil(FADE_S / dt)] - 1) < 1e-9, 'full after FADE_S');
  void alpha;
});

test('two labels that compete for one place do not flicker: a label that is shown keeps it against one of the same importance', () => {
  const pl = new LabelPlacer(measureFn);
  const dt = 1 / 30;
  let toggles = 0;
  let last = null;
  for (let f = 0; f < 120; f++) {
    const L = new FrameLayout(1920, 1080, 1);
    // 'b' is sometimes a little more important by chance, 'a' wanders a pixel or two: the winner stays the winner
    const wob = Math.sin(f * 0.9) * 2;
    const out = pl.place([req('a', 500 + wob, 500, { priority: 50, offsets: [[10, -6]], slide: false }), req('b', 510, 502, { priority: 50 + (f % 2), offsets: [[10, -6]] })], L, f * dt);
    const winner = out.filter((d) => d.alpha > 0.5).map((d) => d.id).join();
    if (last !== null && winner !== last) toggles++;
    last = winner;
  }
  assert.ok(toggles <= 2, `winner changed ${toggles} times`);
});

test('whatever the scene and the cards do, no frame has two visible labels or a label and a card overlapping, or an item outside the frame', () => {
  const R = rng(7);
  const pl = new LabelPlacer(measureFn);
  const dt = 1 / 30;
  for (let f = 0; f < 600; f++) {
    const L = new FrameLayout(1920, 1080, 1);
    // cards that come and go with random opacity
    const cards = [
      ['inset', { x: 1478, y: 46, w: 416, h: 946 }],
      ['focusLT', { x: 34, y: 766, w: 900, h: 164 }],
      ['timeline', { x: 476, y: 930, w: 968, h: 48 }],
      ['station', { x: 34, y: 704, w: 360, h: 176 }],
    ];
    for (const [id, r] of cards) L.card(id, r, R() < 0.3 ? R() : 1);
    // labels: anchors that wander over the whole frame and a bit outside it, random importance and opacity
    const reqs = [];
    const n = 4 + Math.floor(R() * 14);
    for (let i = 0; i < n; i++) {
      reqs.push(req(`l${i}`, -100 + R() * 2100 + Math.sin(f / 15 + i) * 20, -50 + R() * 1180, { priority: Math.floor(R() * 100), alpha: R() < 0.2 ? R() : 1, slide: R() < 0.2 }));
    }
    pl.place(reqs, L, f < 300 ? f * dt : f * dt + 7); // a jump of 7 s halfway
    const v = findViolations(L.items, 1920, 1080);
    // a label's leader is not a box; the checker looks at boxes only
    assert.deepEqual(v, [], `frame ${f}`);
  }
});

test('the default places are around the anchor, right first, and the text width is taken into account for the left ones', () => {
  const o = defaultOffsets(100, 15);
  assert.deepEqual(o[0], [10, -6]);
  assert.equal(o[2][0], -110);
  assert.ok(VISIBLE > 0 && VISIBLE < 0.5);
});
