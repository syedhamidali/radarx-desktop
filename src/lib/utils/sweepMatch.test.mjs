// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { findRequestForReply, isForOtherFile } from './sweepMatch.ts';

const jobA = { variable: 'DBZH', sweep: 0, fileId: 'A' };
const jobB = { variable: 'DBZH', sweep: 0, fileId: 'B' };
const jobAny = { variable: 'DBZH', sweep: 0, fileId: '' };

test('a reply that names its file is matched to that file even when the other is older', () => {
  const pending = [jobA, jobB]; // A was sent first
  assert.equal(findRequestForReply(pending, { variable: 'DBZH', sweep: 0, fileId: 'B' }), jobB);
  assert.equal(findRequestForReply(pending, { variable: 'DBZH', sweep: 0, fileId: 'A' }), jobA);
});

test('a reply for a file with no pending request matches nothing (it is not B-for-A)', () => {
  assert.equal(findRequestForReply([jobA], { variable: 'DBZH', sweep: 0, fileId: 'B' }), undefined);
});

test('a request without a file id is answered by the file the engine picked', () => {
  assert.equal(findRequestForReply([jobAny], { variable: 'DBZH', sweep: 0, fileId: 'A' }), jobAny);
  // ... but an explicit request of the right file wins over it
  assert.equal(findRequestForReply([jobAny, jobA], { variable: 'DBZH', sweep: 0, fileId: 'A' }), jobA);
});

test('an engine that does not name the file keeps the old first-match behaviour', () => {
  assert.equal(findRequestForReply([jobA, jobB], { variable: 'DBZH', sweep: 0 }), jobA);
  assert.equal(findRequestForReply([jobA, jobB], { variable: 'DBZH', sweep: 0, fileId: null }), jobA);
});

test('variable and sweep must match too', () => {
  assert.equal(findRequestForReply([jobA], { variable: 'VRADH', sweep: 0, fileId: 'A' }), undefined);
  assert.equal(findRequestForReply([jobA], { variable: 'DBZH', sweep: 1, fileId: 'A' }), undefined);
});

test('stale means: a named file that is not the one on screen', () => {
  assert.equal(isForOtherFile('A', 'B'), true);
  assert.equal(isForOtherFile('A', 'A'), false);
  assert.equal(isForOtherFile('', 'B'), false);
  assert.equal(isForOtherFile('A', null), false);
  assert.equal(isForOtherFile(undefined, 'B'), false);
});
