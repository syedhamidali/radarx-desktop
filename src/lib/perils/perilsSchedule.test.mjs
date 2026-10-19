// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { scheduleCaptions, activeCaption, captionOf, CAPTION_PRIORITY } from './perilsCaptions.ts';

const evs = (arr) => arr.map(([t_s, kind]) => ({ t_s, kind }));

test('captions are scheduled one at a time, in order, never into the closing card', () => {
  const ev = evs([[100, 'rain_dsd'], [104, 'tornado'], [106, 'hail'], [108, 'gust_front'], [700, 'rotation'], [960, 'updraft']]);
  const s = scheduleCaptions(ev, (td) => td / 8, 120);
  for (let i = 1; i < s.length; i++) assert.ok(s[i].t0 >= s[i - 1].t0 + s[i - 1].hold - 1e-9, 'no overlap');
  for (const q of s) assert.ok(q.t0 >= 0.5 && q.t0 + q.hold <= 120 - 9, 'inside the film, before the end card');
  // the important tornado caption is at its own time; less important ones gave way or moved
  const tor = s.find((q) => q.ev.kind === 'tornado');
  assert.ok(tor && Math.abs(tor.t0 - 13) < 1e-9);
  assert.ok(CAPTION_PRIORITY.tornado > CAPTION_PRIORITY.rain_dsd);
});

test('only a few captions of one kind, and events with no room are left out', () => {
  const many = evs(Array.from({ length: 6 }, (_, i) => [100 + i * 10, 'rain_dsd']));
  const s = scheduleCaptions(many, (td) => td / 8, 120);
  assert.equal(s.filter((q) => q.ev.kind === 'rain_dsd').length, 1);
  const crowd = evs(Array.from({ length: 30 }, (_, i) => [i * 4, ['tornado', 'hail', 'rotation'][i % 3]]));
  const c = scheduleCaptions(crowd, (td) => td / 8, 120);
  assert.ok(c.length < 30 && c.length >= 3);
});

test('the active caption is the scheduled one on screen', () => {
  const s = scheduleCaptions(evs([[160, 'tornado']]), (td) => td / 8, 120);
  assert.equal(activeCaption(s, 19.9), null);
  assert.equal(activeCaption(s, 21).ev.kind, 'tornado');
  assert.equal(activeCaption(s, 40), null);
});

test('the data tool sentence is the numbers line; a candidate says so', () => {
  const c = captionOf({ t_s: 1, kind: 'debris_signature', caption: 'Low-RHO voxels near the track: 1718.', candidate: true, class: 'retrieved' });
  assert.equal(c.title, 'Debris signature (candidate)');
  assert.equal(c.numbers, 'Low-RHO voxels near the track: 1718.');
  assert.equal(c.prov, 'retrieved');
});
