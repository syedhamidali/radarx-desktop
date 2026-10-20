import test from 'node:test';
import assert from 'node:assert/strict';
import { hourBucket, dayBucket, secondsToNextHour, secondsToNextDay, normalizeIp, clientIp, hashIp, LIMITS } from './limits.ts';

const h = (o) => ({ get: (n) => o[n.toLowerCase()] ?? null });

test('the limits are the agreed ones', () => {
  assert.equal(LIMITS.appPerUserDay, 10);
  assert.equal(LIMITS.webPerIpHour, 5);
});

test('buckets are the start of the UTC hour / day', () => {
  const t = new Date('2026-10-24T13:47:12.345Z');
  assert.equal(hourBucket(t), '2026-10-24T13:00:00.000Z');
  assert.equal(dayBucket(t), '2026-10-24T00:00:00.000Z');
  assert.equal(secondsToNextHour(t), 768); // 12 min 47.655 s, rounded up
  assert.equal(secondsToNextDay(new Date('2026-10-24T23:59:59.500Z')), 1);
  assert.equal(secondsToNextHour(new Date('2026-10-24T13:00:00.000Z')), 3600);
});

test('IPv4 is kept, IPv6 is cut to its /64, mapped IPv4 is the IPv4 address', () => {
  assert.equal(normalizeIp('203.0.113.7'), '203.0.113.7');
  assert.equal(normalizeIp(' 203.0.113.7 '), '203.0.113.7');
  assert.equal(normalizeIp('203.0.113.7:51234'), '203.0.113.7');
  assert.equal(normalizeIp('2001:db8:aaaa:bbbb:1:2:3:4'), '2001:db8:aaaa:bbbb::/64');
  assert.equal(normalizeIp('2001:db8:aaaa:bbbb:ffff:ffff:ffff:ffff'), '2001:db8:aaaa:bbbb::/64');
  assert.equal(normalizeIp('2001:DB8::1'), '2001:db8:0:0::/64');
  assert.equal(normalizeIp('::1'), '0:0:0:0::/64');
  assert.equal(normalizeIp('[2001:db8::1]:443'), '2001:db8:0:0::/64');
  assert.equal(normalizeIp('fe80::1%eth0'), 'fe80:0:0:0::/64');
  assert.equal(normalizeIp('::ffff:203.0.113.7'), '203.0.113.7');
  assert.equal(normalizeIp('::ffff:cb00:7107'), '203.0.113.7');
});

test('things that are not addresses are null', () => {
  for (const bad of ['', 'abc', '999.1.1.1', '1.2.3', '1.2.3.4.5', '2001:db8::1::2', '12345::1', 'g::1', '1:2:3:4:5:6:7:8:9', '203.0.113.7; DROP TABLE x', '<script>', ':::']) {
    assert.equal(normalizeIp(bad), null, bad);
  }
});

test('the client address comes from cf-connecting-ip first, then x-real-ip, then x-forwarded-for', () => {
  assert.equal(clientIp(h({ 'cf-connecting-ip': '198.51.100.1', 'x-forwarded-for': '1.1.1.1', 'x-real-ip': '2.2.2.2' })), '198.51.100.1');
  assert.equal(clientIp(h({ 'x-real-ip': '2.2.2.2', 'x-forwarded-for': '1.1.1.1' })), '2.2.2.2');
  assert.equal(clientIp(h({ 'x-forwarded-for': '1.1.1.1, 10.0.0.1' })), '1.1.1.1');
  assert.equal(clientIp(h({ 'cf-connecting-ip': 'garbage', 'x-forwarded-for': '1.1.1.1' })), '1.1.1.1');
  assert.equal(clientIp(h({})), null);
  assert.equal(clientIp(h({ 'x-forwarded-for': 'unknown' })), null);
});

test('the salted hash is stable, hex, and depends on the salt and the address', async () => {
  const a = await hashIp('203.0.113.7', 'salt-one');
  assert.match(a, /^[0-9a-f]{64}$/);
  assert.equal(a, await hashIp('203.0.113.7', 'salt-one'));
  assert.notEqual(a, await hashIp('203.0.113.7', 'salt-two'));
  assert.notEqual(a, await hashIp('203.0.113.8', 'salt-one'));
});
