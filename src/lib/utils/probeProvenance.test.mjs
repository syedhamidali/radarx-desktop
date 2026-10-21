// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { applyMessage } from './layerRegistry.ts';
import { probeTag, probeLayerIds, variableTarget, UNKNOWN_TAG, ADJUSTED_TAG } from './probeProvenance.ts';

const obs = { kind: 'observed', source: 'NEXRAD KTLX' };
const retr = { kind: 'retrieved', source: 'KDP module' };

function state() {
  let s = {};
  s = applyMessage(s, 'file_opened', { file_id: 'f1', provenance: obs });
  s = applyMessage(s, 'derived_variables', { file_id: 'f1', variables: [{ name: 'KDP', provenance: retr }] });
  s = applyMessage(s, 'module_field', { file_id: 'f1', module: 'hid', name: 'class', provenance: { kind: 'retrieved', source: 'HID' } });
  s = applyMessage(s, 'mosaic_result', { mosaic_id: 'm1', provenance: { kind: 'retrieved', source: 'mosaic' } });
  s = applyMessage(s, 'file_opened', { file_id: 'syn', provenance: { kind: 'simulated', source: 'synthetic' } });
  return s;
}

test('a raw moment reads the file layer: OBS', () => {
  const t = probeTag(state(), { type: 'variable', fileId: 'f1', variable: 'DBZH', derived: false });
  assert.equal(t.kind, 'observed');
  assert.equal(t.short, 'OBS');
  assert.match(t.label, /Observed/);
});

test('a derived variable reads its own layer: RETR, not its file', () => {
  const t = probeTag(state(), { type: 'variable', fileId: 'f1', variable: 'KDP', derived: true });
  assert.equal(t.kind, 'retrieved');
  assert.equal(t.short, 'RETR');
});

test('a derived variable without a layer is unknown, never guessed as observed', () => {
  assert.deepEqual(probeTag(state(), { type: 'variable', fileId: 'f1', variable: 'NEW', derived: true }), UNKNOWN_TAG);
});

test('a synthetic file is SIM', () => {
  assert.equal(probeTag(state(), { type: 'variable', fileId: 'syn', variable: 'DBZH', derived: false }).short, 'SIM');
});

test('module field, with and without a file id', () => {
  const s = state();
  assert.equal(probeTag(s, { type: 'module', fileId: 'f1', module: 'hid', name: 'class' }).kind, 'retrieved');
  const s2 = applyMessage({}, 'module_field', { module: 'm', name: 'n', provenance: retr });
  assert.equal(probeTag(s2, { type: 'module', fileId: 'f1', module: 'm', name: 'n' }).kind, 'retrieved');
});

test('map layer: mosaic from the registry, QPE from its own provenance type', () => {
  const s = state();
  assert.equal(probeTag(s, { type: 'map', mosaicId: 'm1' }).kind, 'retrieved');
  assert.deepEqual(probeTag(s, { type: 'map', mosaicId: 'q1', provenanceType: 'ADJUSTED' }), ADJUSTED_TAG);
  assert.equal(probeTag(s, { type: 'map', mosaicId: 'q1', provenanceType: 'RETRIEVED' }).short, 'RETR');
  assert.equal(probeTag(s, { type: 'map', mosaicId: 'zz' }).kind, 'unknown');
});

test('variableTarget tells derived names from raw moments', () => {
  const derived = [{ name: 'KDP' }];
  assert.deepEqual(variableTarget('f1', 'KDP', derived), { type: 'variable', fileId: 'f1', variable: 'KDP', derived: true });
  assert.equal(variableTarget('f1', 'DBZH', derived).derived, false);
  assert.equal(variableTarget('f1', '', derived).variable, null);
  assert.equal(probeTag(state(), variableTarget('f1', 'KDP', derived)).short, 'RETR');
});

test('nothing selected: no ids, unknown', () => {
  assert.deepEqual(probeLayerIds({ type: 'variable', fileId: null, variable: 'DBZH', derived: false }), []);
  assert.deepEqual(probeTag({}, { type: 'variable', fileId: 'f1', variable: null, derived: false }), UNKNOWN_TAG);
});
