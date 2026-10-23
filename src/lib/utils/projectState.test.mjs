// @ts-nocheck - plain node:test file, not part of the app bundle
import test from 'node:test';
import assert from 'node:assert/strict';
import { buildView, parseView, planOpen } from './projectState.ts';

const files = [
  { path: '/d/KAAA_V06', filename: 'KAAA_V06', selectedVariable: 'DBZH', selectedSweep: 2 },
  { path: '/d/KBBB_V06', filename: 'KBBB_V06', selectedVariable: null, selectedSweep: 0 },
];

test('build then parse is the identity', () => {
  const view = buildView(files, 1);
  assert.deepEqual(parseView(JSON.parse(JSON.stringify(view))), view);
  assert.equal(view.active, 1);
});

test('parseView survives garbage', () => {
  for (const bad of [null, undefined, 5, 'x', [], { files: 'x' }, { files: [null, 3, { path: 5 }] }]) {
    assert.deepEqual(parseView(bad).files, []);
  }
  const v = parseView({ files: [{ path: '/a/b', sweep: -3, variable: 7 }], active: 9 });
  assert.deepEqual(v.files, [{ path: '/a/b', filename: 'b', variable: null, sweep: 0 }]);
  assert.equal(v.active, 0);
});

test('files found elsewhere are opened where they are now, with their selection', () => {
  const view = buildView(files, 1);
  const plan = planOpen(view, [
    { name: 'KAAA_V06', status: 'found', path: '/moved/KAAA_V06' },
    { name: 'KBBB_V06', status: 'found', path: '/moved/KBBB_V06' },
  ]);
  assert.deepEqual(plan.open.map((o) => [o.path, o.variable, o.sweep]), [
    ['/moved/KAAA_V06', 'DBZH', 2],
    ['/moved/KBBB_V06', null, 0],
  ]);
  assert.equal(plan.activePath, '/moved/KBBB_V06');
});

test('missing files are reported, never replaced; changed files are flagged', () => {
  const plan = planOpen(buildView(files, 0), [
    { name: 'KAAA_V06', status: 'changed', path: '/d/KAAA_V06' },
    { name: 'KBBB_V06', status: 'missing', path: null },
  ]);
  assert.deepEqual(plan.missing, ['KBBB_V06']);
  assert.deepEqual(plan.changed, ['KAAA_V06']);
  assert.equal(plan.open.length, 1);
  assert.equal(plan.activePath, '/d/KAAA_V06');
});

test('when the active file is missing the first opened one becomes active', () => {
  const plan = planOpen(buildView(files, 1), [
    { name: 'KAAA_V06', status: 'found', path: '/d/KAAA_V06' },
    { name: 'KBBB_V06', status: 'missing', path: null },
  ]);
  assert.equal(plan.activePath, '/d/KAAA_V06');
});

test('a project saved without a view still opens its files', () => {
  const plan = planOpen(parseView({}), [{ name: 'K', status: 'found', path: '/x/K' }]);
  assert.deepEqual(plan.open.map((o) => o.path), ['/x/K']);
});
