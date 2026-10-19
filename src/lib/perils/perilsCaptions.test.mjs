// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { PHYSICS, captionOf, activeEvent, captionAlpha, wrapText } from './perilsCaptions.ts';

const toClip = (td) => td / 8;

test('every physics entry has a title, a sentence and a known provenance class', () => {
  for (const [k, p] of Object.entries(PHYSICS)) {
    assert.ok(p.title.length > 2, k);
    assert.ok(p.text.endsWith('.'), k);
    assert.ok(p.text.length < 260, `${k} is one short sentence`);
    assert.ok(['observed', 'model', 'analysis', 'retrieved', 'interpolated', 'stylised'].includes(p.prov), k);
  }
});

test('a caption carries the numbers of the event and never makes up its own', () => {
  const c = captionOf({ t_s: 100, kind: 'gust_front', numbers: ['+2.1 hPa', '-4.3 K'], class: 'observed' });
  assert.equal(c.title, 'Gust front');
  assert.equal(c.numbers, '+2.1 hPa · -4.3 K');
  assert.equal(c.prov, 'observed');
  assert.ok(c.text.includes('cold pool'));
  const bare = captionOf({ t_s: 1, kind: 'gust_front' });
  assert.equal(bare.numbers, '');
});

test('the data can override title, text and class; unknown kinds still caption', () => {
  const c = captionOf({ t_s: 1, kind: 'weird_thing', title: 'Own title', text: 'Own text.', class: 'model' });
  assert.deepEqual([c.title, c.text, c.prov], ['Own title', 'Own text.', 'model']);
  assert.equal(captionOf({ t_s: 1, kind: 'weird_thing' }).title, 'weird thing');
  assert.equal(captionOf({ t_s: 1, kind: 'x', class: 'bogus' }).prov, 'observed');
});

test('the event shown is the latest one that has started within its hold, nothing before it starts', () => {
  const ev = [{ t_s: 80, kind: 'a' }, { t_s: 120, kind: 'b' }];
  assert.equal(activeEvent(ev, toClip, 9.9), null);
  assert.equal(activeEvent(ev, toClip, 10.1).kind, 'a');
  assert.equal(activeEvent(ev, toClip, 15.5).kind, 'b'); // b started at 15, a (10 to 17) still holds: latest wins
  assert.equal(activeEvent(ev, toClip, 22.1), null);
});

test('captions fade in and out', () => {
  assert.equal(captionAlpha(10, 10, 7), 0);
  assert.ok(captionAlpha(10.2, 10, 7) > 0.4 && captionAlpha(10.2, 10, 7) < 0.6);
  assert.equal(captionAlpha(13, 10, 7), 1);
  assert.ok(captionAlpha(16.6, 10, 7) < 0.6);
  assert.equal(captionAlpha(17, 10, 7), 0);
});

test('wrap keeps words whole and lines short', () => {
  const l = wrapText('the quick brown fox jumps over the lazy dog', 15);
  assert.ok(l.every((s) => s.length <= 15));
  assert.equal(l.join(' '), 'the quick brown fox jumps over the lazy dog');
  assert.deepEqual(wrapText('', 10), []);
});
