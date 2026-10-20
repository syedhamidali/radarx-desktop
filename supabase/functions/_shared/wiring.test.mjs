// The Deno wiring files (index.ts) cannot be run here (no Deno), so the properties that matter are
// checked in their text: which secrets each function reads, and that no body or header is trusted raw.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const src = (name) => readFileSync(new URL(`../${name}/index.ts`, import.meta.url), 'utf8');

test('log-activity reads the PUBLIC jwk only, never the private key', () => {
  const text = src('log-activity');
  assert.match(text, /ENTITLEMENT_PUBLIC_JWK/);
  assert.doesNotMatch(text, /ENTITLEMENT_PRIVATE_KEY/);
  assert.doesNotMatch(text, /loadSigningKey/);
});

test('exchange-api-token reads the private key and nothing else of the entitlement secrets', () => {
  const text = src('exchange-api-token');
  assert.match(text, /ENTITLEMENT_PRIVATE_KEY/);
  assert.doesNotMatch(text, /ENTITLEMENT_PUBLIC_JWK/);
});

test('both functions read the body from the stream with a byte cap, never trusting content-length', () => {
  for (const name of ['exchange-api-token', 'log-activity']) {
    const text = src(name);
    assert.match(text, /readLimitedText\(req, \d+\)/, name);
    assert.doesNotMatch(text, /content-length/i, name);
    assert.doesNotMatch(text, /req\.(text|json)\(\)/, name);
    assert.match(text, /413/, name);
  }
});

test('the client address comes from the trusted helper, never from x-forwarded-for', () => {
  for (const name of ['exchange-api-token', 'log-activity']) {
    assert.doesNotMatch(src(name), /x-forwarded-for|x-real-ip/i, name);
  }
  assert.match(src('exchange-api-token'), /trustedAddress\(req\.headers\)/);
});

test('no function logs a token, a hash, an address or a key', () => {
  for (const name of ['exchange-api-token', 'log-activity']) {
    for (const line of src(name).split('\n').filter((l) => /console\.(log|error|warn)/.test(l))) {
      assert.doesNotMatch(line, /\b(presented|hash|serviceKey|req\.headers|body)\b/, line);
    }
  }
});
