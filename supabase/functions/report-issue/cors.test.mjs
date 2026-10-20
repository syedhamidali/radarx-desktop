import test from 'node:test';
import assert from 'node:assert/strict';
import { corsHeaders, originAllowed, allowedOrigins, normalizeOrigin, DEFAULT_ORIGIN, TAURI_ORIGINS } from './cors.ts';

test('the default allowed origin is the website', () => {
  assert.deepEqual(allowedOrigins({}), ['https://radarx.labshope.com']);
  assert.equal(DEFAULT_ORIGIN, 'https://radarx.labshope.com');
  assert.equal(originAllowed('https://radarx.labshope.com', 'web', {}), true);
  assert.equal(originAllowed('https://evil.example', 'web', {}), false);
  assert.equal(originAllowed('https://radarx.labshope.com.evil.example', 'web', {}), false);
  assert.equal(originAllowed('http://radarx.labshope.com', 'web', {}), false);
});

test('REPORT_ALLOWED_ORIGINS replaces the default; a wildcard is ignored', () => {
  const env = { REPORT_ALLOWED_ORIGINS: 'https://a.example, https://b.example/ ,*, https://*.example, not-an-origin' };
  assert.deepEqual(allowedOrigins(env), ['https://a.example', 'https://b.example']);
  assert.equal(originAllowed('https://a.example', 'web', env), true);
  assert.equal(originAllowed('https://b.example', 'app', env), true);
  assert.equal(originAllowed('https://radarx.labshope.com', 'web', env), false);
  assert.equal(originAllowed('https://anything.example', 'web', { REPORT_ALLOWED_ORIGINS: '*' }), false);
});

test('the desktop app origins are allowed for "app" and the preflight, not for "web"', () => {
  for (const o of TAURI_ORIGINS) {
    assert.equal(originAllowed(o, 'app', {}), true, o);
    assert.equal(originAllowed(o, undefined, {}), true, o);
    assert.equal(originAllowed(o, 'web', {}), false, o);
  }
  assert.deepEqual(TAURI_ORIGINS, ['tauri://localhost', 'http://tauri.localhost', 'https://tauri.localhost']);
});

test('localhost only with REPORT_ALLOW_LOCALHOST=1', () => {
  assert.equal(originAllowed('http://localhost:5173', 'web', {}), false);
  assert.equal(originAllowed('http://127.0.0.1:8080', 'app', {}), false);
  const on = { REPORT_ALLOW_LOCALHOST: '1' };
  assert.equal(originAllowed('http://localhost:5173', 'web', on), true);
  assert.equal(originAllowed('http://localhost', 'web', on), true);
  assert.equal(originAllowed('http://127.0.0.1:8080', 'app', on), true);
  assert.equal(originAllowed('https://localhost:5173', 'web', on), false);
  assert.equal(originAllowed('http://localhost.evil.example', 'web', on), false);
  assert.equal(originAllowed('http://localhost:5173@evil.example', 'web', on), false);
  assert.equal(originAllowed('http://evil.example/http://localhost:1', 'web', on), false);
  assert.equal(originAllowed('http://localhost:5173', 'web', { REPORT_ALLOW_LOCALHOST: 'true' }), false);
});

test('headers: exact origin echoed, the three request headers allowed, never "*", no credentials', () => {
  const h = corsHeaders('https://radarx.labshope.com', 'web', {});
  assert.equal(h['Access-Control-Allow-Origin'], 'https://radarx.labshope.com');
  assert.equal(h['Access-Control-Allow-Headers'], 'Authorization, Content-Type, apikey');
  assert.equal(h['Access-Control-Allow-Methods'], 'POST, OPTIONS');
  assert.equal(h.Vary, 'Origin');
  assert.equal('Access-Control-Allow-Credentials' in h, false);
  for (const o of ['https://radarx.labshope.com', 'tauri://localhost', 'https://evil.example', null]) {
    const x = corsHeaders(o, undefined, { REPORT_ALLOWED_ORIGINS: '*' });
    assert.notEqual(x['Access-Control-Allow-Origin'], '*');
  }
});

test('a disallowed origin, or no origin, gets no allow-origin header', () => {
  assert.equal('Access-Control-Allow-Origin' in corsHeaders('https://evil.example', 'web', {}), false);
  assert.equal('Access-Control-Allow-Origin' in corsHeaders(null, 'web', {}), false);
  assert.equal(corsHeaders(null, 'web', {}).Vary, 'Origin');
});

test('origins are normalised', () => {
  assert.equal(normalizeOrigin('HTTPS://Radarx.LabsHope.com/'), 'https://radarx.labshope.com');
  assert.equal(normalizeOrigin('https://x.example/path'), null);
  assert.equal(normalizeOrigin('x'), null);
});
