// Tests of the entitlement-token module under plain Node (no Deno): node --test supabase/functions/*/*.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { createPublicKey, createVerify, createHash, generateKeyPairSync } from 'node:crypto';
import {
  AUDIENCE, ISSUER, LIFETIME_SECONDS, EntitlementError, b64urlDecode, b64urlEncode, loadSigningKey, loadVerifyKey,
  pemToPkcs8, sha256Hex, signEntitlement, thumbprint, verifyEntitlement,
} from './entitlement.ts';

/** A made-up key pair for the test (never a real key): PKCS8 PEM like `openssl`/cryptography print. */
function testPem() {
  const { privateKey } = generateKeyPairSync('ec', { namedCurve: 'P-256' });
  return privateKey.export({ type: 'pkcs8', format: 'pem' });
}

const NOW = 1_800_000_000;
const claims = (extra = {}) => ({
  iss: ISSUER, aud: AUDIENCE, sub: '11111111-2222-3333-4444-555555555555', src: 'pat', jti: 'j-1',
  iat: NOW, exp: NOW + 3600, ...extra,
});

test('a PKCS8 PEM loads and gives a thumbprint kid', async () => {
  const pem = testPem();
  const key = await loadSigningKey(pem);
  assert.equal(key.publicJwk.kty, 'EC');
  assert.equal(key.publicJwk.crv, 'P-256');
  assert.match(key.kid, /^[A-Za-z0-9_-]{43}$/);
  // independent computation of the RFC 7638 thumbprint with node:crypto
  const jwk = createPublicKey(pem).export({ format: 'jwk' });
  const expected = createHash('sha256').update(`{"crv":"${jwk.crv}","kty":"${jwk.kty}","x":"${jwk.x}","y":"${jwk.y}"}`).digest('base64url');
  assert.equal(key.kid, expected);
  assert.equal(await thumbprint(key.publicJwk), expected);
});

test('only a P-256 PKCS8 private key is accepted', async () => {
  assert.throws(() => pemToPkcs8(''), EntitlementError);
  assert.throws(() => pemToPkcs8('-----BEGIN EC PRIVATE KEY-----\nAAAA\n-----END EC PRIVATE KEY-----'), EntitlementError);
  assert.throws(() => pemToPkcs8('-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----'), EntitlementError);
  assert.throws(() => pemToPkcs8('not a pem'), EntitlementError);
  await assert.rejects(loadSigningKey('-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----'), EntitlementError);
  const { privateKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
  await assert.rejects(loadSigningKey(privateKey.export({ type: 'pkcs8', format: 'pem' })), EntitlementError);
  const { privateKey: p384 } = generateKeyPairSync('ec', { namedCurve: 'P-384' });
  await assert.rejects(loadSigningKey(p384.export({ type: 'pkcs8', format: 'pem' })), EntitlementError);
});

test('a signed token verifies, and an independent verifier agrees (ES256, r||s)', async () => {
  const pem = testPem();
  const key = await loadSigningKey(pem);
  const token = await signEntitlement(claims(), key);
  const out = await verifyEntitlement(token, key, NOW + 10);
  assert.equal(out.sub, '11111111-2222-3333-4444-555555555555');
  const [h, p, s] = token.split('.');
  const header = JSON.parse(Buffer.from(h, 'base64url').toString());
  assert.deepEqual(header, { alg: 'ES256', typ: 'JWT', kid: key.kid });
  const ok = createVerify('SHA256').update(`${h}.${p}`).verify({ key: createPublicKey(pem), dsaEncoding: 'ieee-p1363' }, Buffer.from(s, 'base64url'));
  assert.equal(ok, true);
  assert.equal(b64urlDecode(s).length, 64);
});

test('verification refuses everything it should', async () => {
  const key = await loadSigningKey(testPem());
  const other = await loadSigningKey(testPem());
  const good = await signEntitlement(claims(), key);
  const refuse = async (token, reason, now = NOW + 10) => {
    await assert.rejects(verifyEntitlement(token, key, now), (e) => e instanceof EntitlementError && (reason === undefined || e.reason === reason), String(reason));
  };
  await refuse(await signEntitlement(claims(), other), 'header'); // another key: kid differs
  const [h, p, s] = good.split('.');
  await refuse(`${h}.${p}.${b64urlEncode(new Uint8Array(64))}`, 'signature');
  await refuse(`${h}.${b64urlEncode(new TextEncoder().encode(JSON.stringify(claims({ sub: '99999999-2222-3333-4444-555555555555' }))))}.${s}`, 'signature');
  await refuse(`${h}.${p}.${s.slice(0, -4)}`, 'signature');
  await refuse(good, 'expired', NOW + 3601);
  await refuse(await signEntitlement(claims({ iss: 'x' }), key), 'claims');
  await refuse(await signEntitlement(claims({ aud: 'authenticated' }), key), 'claims');
  await refuse(await signEntitlement(claims({ src: 'oauth' }), key), 'claims');
  await refuse(await signEntitlement(claims({ sub: 'not-a-uuid' }), key), 'claims');
  await refuse(await signEntitlement(claims({ jti: '' }), key), 'claims');
  await refuse(await signEntitlement(claims({ exp: NOW + 7200 }), key), 'claims'); // longer than one hour
  await refuse(await signEntitlement(claims({ iat: NOW + 3600, exp: NOW + 3700 }), key), 'claims'); // issued in the future
  await refuse(await signEntitlement(claims({ exp: '1' }), key), 'claims');
  // algorithm confusion: alg none, HS256, and a header naming another algorithm
  const enc = (o) => b64urlEncode(new TextEncoder().encode(JSON.stringify(o)));
  await refuse(`${enc({ alg: 'none', kid: key.kid })}.${p}.`, 'header');
  await refuse(`${enc({ alg: 'HS256', kid: key.kid })}.${p}.${s}`, 'header');
  await refuse(`${enc({ alg: 'ES256' })}.${p}.${s}`, 'header');
  for (const junk of ['', 'a.b', 'a.b.c.d', 'x'.repeat(5000), '...', null, 5]) await refuse(junk);
});

test('helpers', async () => {
  assert.equal(b64urlEncode(Uint8Array.from([251, 255, 254])), '-__-');
  assert.deepEqual([...b64urlDecode('-__-')], [251, 255, 254]);
  assert.throws(() => b64urlDecode('a+b'), EntitlementError);
  assert.equal(await sha256Hex('abc'), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
  assert.equal(LIFETIME_SECONDS, 3600);
});

test('log-activity verifies with the PUBLIC jwk alone, and a private jwk is refused loudly', async () => {
  const pem = testPem();
  const key = await loadSigningKey(pem);
  const publicJson = JSON.stringify({ ...key.publicJwk, kid: key.kid, alg: 'ES256', use: 'sig' });
  const verifyKey = await loadVerifyKey(publicJson);
  assert.equal(verifyKey.kid, key.kid);
  const token = await signEntitlement(claims(), key);
  assert.equal((await verifyEntitlement(token, verifyKey, NOW + 10)).src, 'pat');
  // not JSON, not a JWK, a private half, a wrong curve, a wrong kid
  await assert.rejects(loadVerifyKey('not json'), EntitlementError);
  await assert.rejects(loadVerifyKey('5'), EntitlementError);
  await assert.rejects(loadVerifyKey(JSON.stringify({ ...key.publicJwk, d: 'AAAA' })), /private key/);
  await assert.rejects(loadVerifyKey(JSON.stringify({ ...key.publicJwk, crv: 'P-384' })), EntitlementError);
  await assert.rejects(loadVerifyKey(JSON.stringify({ ...key.publicJwk, x: 'AAAA' })), EntitlementError);
  await assert.rejects(loadVerifyKey(JSON.stringify({ ...key.publicJwk, kid: 'forged' })), /thumbprint/);
  await assert.rejects(loadVerifyKey(''), EntitlementError);
  // a public key from ANOTHER pair does not verify this token
  const other = await loadSigningKey(testPem());
  await assert.rejects(verifyEntitlement(token, await loadVerifyKey(JSON.stringify(other.publicJwk)), NOW + 10), EntitlementError);
});
