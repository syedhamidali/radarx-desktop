import test from 'node:test';
import assert from 'node:assert/strict';
import { validateReport, honeypotFilled, isEmailShape, LIMITS } from './validate.ts';

const good = (over = {}) => ({ source: 'app', kind: 'bug', title: 'Crash on open', description: 'The app closes when I open a file.', ...over });
const field = (body) => {
  const r = validateReport(body);
  assert.equal(r.ok, false, String(JSON.stringify(body)).slice(0, 80));
  return r.field;
};

test('a minimal report is valid and optional fields come back as null', () => {
  const r = validateReport(good());
  assert.equal(r.ok, true);
  assert.deepEqual(r.value, {
    source: 'app', kind: 'bug', title: 'Crash on open', description: 'The app closes when I open a file.',
    contact_email: null, app_version: null, os: null, engine_version: null, diagnostics: null, turnstile_token: null,
  });
});

test('a full report keeps every field', () => {
  const r = validateReport(good({
    source: 'web', kind: 'idea', contact_email: 'ada@example.org', app_version: '0.3.0', os: 'macOS', engine_version: '1.2', diagnostics: 'line1\nline2', turnstile_token: 'tok',
  }));
  assert.equal(r.ok, true);
  assert.equal(r.value.contact_email, 'ada@example.org');
  assert.equal(r.value.diagnostics, 'line1\nline2');
  assert.equal(r.value.turnstile_token, 'tok');
});

test('the body must be a JSON object', () => {
  for (const b of [null, undefined, 'x', 5, [], [good()]]) assert.equal(field(b), 'body');
});

test('source and kind are allow-listed', () => {
  assert.equal(field(good({ source: 'cli' })), 'source');
  assert.equal(field(good({ source: undefined })), 'source');
  assert.equal(field(good({ source: ['app'] })), 'source');
  assert.equal(field(good({ kind: 'rant' })), 'kind');
  assert.equal(field(good({ kind: 'BUG' })), 'kind');
  assert.equal(field(good({ kind: undefined })), 'kind');
});

test('title: 3 to 140 characters, required, single line', () => {
  assert.equal(field(good({ title: 'ab' })), 'title');
  assert.equal(field(good({ title: '   a   ' })), 'title');
  assert.equal(field(good({ title: undefined })), 'title');
  assert.equal(field(good({ title: 42 })), 'title');
  assert.equal(field(good({ title: 'x'.repeat(141) })), 'title');
  assert.equal(validateReport(good({ title: 'x'.repeat(140) })).ok, true);
  assert.equal(validateReport(good({ title: 'abc' })).ok, true);
  assert.equal(validateReport(good({ title: 'Two\nlines here' })).value.title, 'Two lines here');
  // 140 emoji are 140 code points although 280 UTF-16 units
  assert.equal(validateReport(good({ title: '\u{1F600}'.repeat(140) })).ok, true);
  assert.equal(field(good({ title: '\u{1F600}'.repeat(141) })), 'title');
});

test('description: 10 to 5000 characters, required, newlines kept', () => {
  assert.equal(field(good({ description: 'too short' })), 'description');
  assert.equal(field(good({ description: undefined })), 'description');
  assert.equal(field(good({ description: {} })), 'description');
  assert.equal(field(good({ description: 'd'.repeat(5001) })), 'description');
  assert.equal(validateReport(good({ description: 'd'.repeat(5000) })).ok, true);
  assert.equal(validateReport(good({ description: '0123456789' })).ok, true);
  assert.equal(validateReport(good({ description: 'first\n\nsecond line' })).value.description, 'first\n\nsecond line');
});

test('length is checked after cleaning (padding and control characters do not count)', () => {
  assert.equal(field(good({ description: 'short\u0001\u0002\u0003\u0004\u0005\u0006' })), 'description');
  assert.equal(field(good({ title: 'a' + ' '.repeat(20) })), 'title');
});

test('NUL anywhere is rejected, with the field name', () => {
  assert.equal(field(good({ title: 'bad\u0000title' })), 'title');
  assert.equal(field(good({ description: 'bad\u0000 description here' })), 'description');
  assert.equal(field(good({ contact_email: 'a@b.co\u0000' })), 'contact_email');
  assert.equal(field(good({ app_version: '1\u0000' })), 'app_version');
  assert.equal(field(good({ os: '\u0000' })), 'os');
  assert.equal(field(good({ engine_version: '1\u0000' })), 'engine_version');
  assert.equal(field(good({ diagnostics: 'x\u0000y' })), 'diagnostics');
  assert.equal(field(good({ turnstile_token: 'x\u0000y' })), 'turnstile_token');
});

test('contact_email is optional, empty means absent, must look like an address', () => {
  assert.equal(validateReport(good({ contact_email: '' })).value.contact_email, null);
  assert.equal(validateReport(good({ contact_email: '   ' })).value.contact_email, null);
  assert.equal(validateReport(good({ contact_email: null })).value.contact_email, null);
  assert.equal(validateReport(good({ contact_email: ' ada@example.org ' })).value.contact_email, 'ada@example.org');
  for (const bad of ['nope', 'a@b', 'a@@b.co', '@b.co', 'a@.co', 'a b@example.org', 'a@b.c', '<a@b.co>', 'a@b.co>', '"a"@b.co', 'a@b..co', '.a@b.co', 'a.@b.co', 'a@-b.co', 'a,b@c.co', 'a@b.co,c@d.co']) {
    assert.equal(field(good({ contact_email: bad })), 'contact_email', bad);
  }
  assert.equal(field(good({ contact_email: 5 })), 'contact_email');
  assert.equal(field(good({ contact_email: 'a'.repeat(195) + '@b.org' })), 'contact_email');
  assert.equal(isEmailShape('first.last+tag@sub.example.co.uk'), true);
  assert.equal(isEmailShape('ada@xn--bcher-kva.example'), true);
  assert.equal(isEmailShape('\u00fcser@b\u00fccher.example'), true);
});

test('e-mail header injection attempts are not addresses', () => {
  assert.equal(field(good({ contact_email: 'a@b.co\nBcc: x@y.co' })), 'contact_email');
  assert.equal(field(good({ contact_email: 'a@b.co\r\nBcc: x@y.co' })), 'contact_email');
});

test('versions, system name and diagnostics: optional, limited, text only', () => {
  assert.equal(field(good({ app_version: 'v'.repeat(41) })), 'app_version');
  assert.equal(validateReport(good({ app_version: 'v'.repeat(40) })).ok, true);
  assert.equal(field(good({ os: 'o'.repeat(61) })), 'os');
  assert.equal(validateReport(good({ os: 'o'.repeat(60) })).ok, true);
  assert.equal(field(good({ engine_version: 'e'.repeat(41) })), 'engine_version');
  assert.equal(field(good({ diagnostics: 'x'.repeat(20001) })), 'diagnostics');
  assert.equal(validateReport(good({ diagnostics: 'x'.repeat(20000) })).ok, true);
  assert.equal(field(good({ app_version: 3 })), 'app_version');
  assert.equal(field(good({ os: ['macOS'] })), 'os');
  assert.equal(field(good({ diagnostics: { a: 1 } })), 'diagnostics');
  assert.equal(validateReport(good({ app_version: '', os: '', engine_version: '', diagnostics: '' })).value.diagnostics, null);
  assert.equal(validateReport(good({ os: 'mac\nOS' })).value.os, 'mac OS');
});

test('turnstile_token is opaque but bounded', () => {
  assert.equal(validateReport(good({ source: 'web', turnstile_token: 't'.repeat(2048) })).ok, true);
  assert.equal(field(good({ source: 'web', turnstile_token: 't'.repeat(2049) })), 'turnstile_token');
  assert.equal(field(good({ turnstile_token: 7 })), 'turnstile_token');
  assert.equal(validateReport(good({ turnstile_token: '' })).value.turnstile_token, null);
});

test('unknown fields are ignored and never returned', () => {
  const r = validateReport(good({ user_id: 'someone-else', status: 'closed', id: 'x', is_admin: true }));
  assert.equal(r.ok, true);
  assert.deepEqual(Object.keys(r.value).sort(), ['app_version', 'contact_email', 'description', 'diagnostics', 'engine_version', 'kind', 'os', 'source', 'title', 'turnstile_token']);
});

test('error messages never repeat what was sent', () => {
  const secret = 'SENTINEL-VALUE-123';
  for (const body of [good({ title: secret + '\u0000' }), good({ contact_email: secret }), good({ kind: secret }), good({ source: secret })]) {
    const r = validateReport(body);
    assert.equal(r.ok, false);
    assert.equal(r.message.includes(secret), false);
  }
});

test('the honeypot: empty or absent is fine, anything else is a bot', () => {
  assert.equal(honeypotFilled(good()), false);
  assert.equal(honeypotFilled(good({ website: '' })), false);
  assert.equal(honeypotFilled(good({ website: '   ' })), false);
  assert.equal(honeypotFilled(good({ website: null })), false);
  assert.equal(honeypotFilled(good({ website: 'http://spam.example' })), true);
  assert.equal(honeypotFilled(good({ website: 'x' })), true);
  assert.equal(honeypotFilled(good({ website: 0 })), true);
  assert.equal(honeypotFilled(good({ website: false })), true);
  assert.equal(honeypotFilled(good({ website: [] })), true);
  assert.equal(honeypotFilled(null), false);
  assert.equal(LIMITS.title.max, 140);
});
