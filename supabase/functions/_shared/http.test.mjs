import test from 'node:test';
import assert from 'node:assert/strict';
import { BodyTooLarge, parseJson, readLimitedText, trustedAddress } from './http.ts';

const headers = (o = {}) => ({ get: (n) => o[n.toLowerCase()] ?? null });
const streamOf = (...parts) =>
  new ReadableStream({
    start(c) {
      for (const p of parts) c.enqueue(typeof p === 'string' ? new TextEncoder().encode(p) : p);
      c.close();
    },
  });

test('a body within the cap is read whole', async () => {
  assert.equal(await readLimitedText({ body: streamOf('{"a":', '1}'), headers: headers() }, 100), '{"a":1}');
  assert.equal(await readLimitedText({ body: null, headers: headers() }, 10), '');
  assert.equal(await readLimitedText({ body: streamOf('x'.repeat(10)), headers: headers() }, 10), 'x'.repeat(10));
});

test('a chunked body with no content-length is cut off at the cap', async () => {
  let pulled = 0;
  const endless = new ReadableStream({
    pull(c) {
      pulled++;
      c.enqueue(new Uint8Array(1024));
    },
  });
  await assert.rejects(readLimitedText({ body: endless, headers: headers() }, 4096), BodyTooLarge);
  assert.ok(pulled < 20, `kept reading (${pulled} chunks)`);
});

test('a lying, missing, NaN or negative content-length does not decide anything', async () => {
  for (const claim of ['5', '', 'NaN', 'abc', '-1', '1e999']) {
    await assert.rejects(
      readLimitedText({ body: streamOf('y'.repeat(300)), headers: headers({ 'content-length': claim }) }, 100),
      BodyTooLarge,
      claim,
    );
  }
  await assert.rejects(readLimitedText({ body: streamOf('z'), headers: headers({ 'content-length': '1000000' }) }, 100), BodyTooLarge);
});

test('invalid UTF-8 is refused and bad JSON is null', async () => {
  await assert.rejects(readLimitedText({ body: streamOf(new Uint8Array([0xff, 0xfe, 0x00])), headers: headers() }, 100));
  assert.equal(parseJson('{'), null);
  assert.deepEqual(parseJson('{"a":1}'), { a: 1 });
});

test('only a well-formed cf-connecting-ip is a trusted address; x-forwarded-for never is', () => {
  assert.equal(trustedAddress(headers({ 'cf-connecting-ip': '203.0.113.7' })), '203.0.113.7');
  assert.equal(trustedAddress(headers({ 'cf-connecting-ip': ' 2001:db8::1 ' })), '2001:db8::1');
  for (const bad of ['', 'unknown', '999.1.1.1', '1.2.3', '1.2.3.4, 5.6.7.8', 'x'.repeat(60), '<script>', '1.2.3.4:80']) {
    assert.equal(trustedAddress(headers({ 'cf-connecting-ip': bad })), null, bad);
  }
  assert.equal(trustedAddress(headers({ 'x-forwarded-for': '1.2.3.4' })), null);
  assert.equal(trustedAddress(headers({ 'x-real-ip': '1.2.3.4' })), null);
  assert.equal(trustedAddress(headers()), null);
});
