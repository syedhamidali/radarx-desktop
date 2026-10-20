import test from 'node:test';
import assert from 'node:assert/strict';
import { shareKind, composeUrl, defaultCaption, isAllowedShareUrl, instagramAdvice, SHARE_URL } from './share.ts';

test('which saved files can be shared', () => {
  assert.equal(shareKind('/a/b/storm.PNG'), 'image');
  assert.equal(shareKind('c:\\x\\clip.mp4'), 'video');
  assert.equal(shareKind('/a/volume.nc'), null);
  assert.equal(shareKind('/a/report.pdf'), null);
  assert.equal(shareKind('noext'), null);
});
test('compose links carry the caption, encoded', () => {
  const c = 'Look: 50 dBZ & hail #radar';
  const x = new URL(composeUrl('x', c));
  assert.equal(x.hostname, 'twitter.com');
  assert.equal(x.searchParams.get('text'), c);
  assert.equal(new URL(composeUrl('bluesky', c)).searchParams.get('text').startsWith(c), true);
  assert.equal(new URL(composeUrl('facebook', c)).searchParams.get('quote'), c);
});
test('every generated link is on the allow list', () => {
  for (const t of ['x', 'facebook', 'linkedin', 'bluesky']) assert.ok(isAllowedShareUrl(composeUrl(t, 'hi')), t);
  assert.ok(isAllowedShareUrl('https://www.instagram.com/'));
  assert.ok(!isAllowedShareUrl('http://twitter.com/intent/tweet'));
  assert.ok(!isAllowedShareUrl('https://evil.example/intent/tweet'));
  assert.ok(!isAllowedShareUrl('javascript:alert(1)'));
});
test('captions and Instagram advice depend on the kind', () => {
  assert.match(defaultCaption('video'), /animation/);
  assert.match(defaultCaption('image'), /image/);
  assert.match(instagramAdvice('video'), /9:16/);
  assert.match(instagramAdvice('image'), /4:5/);
});
test('the shared project link is the website, never a code host', () => {
  assert.equal(SHARE_URL, 'https://radarx.labshope.com');
  const x = new URL(composeUrl('x', 'hi'));
  assert.equal(x.searchParams.get('url'), SHARE_URL);
  assert.ok(!composeUrl('bluesky', 'hi').includes('github'));
});
