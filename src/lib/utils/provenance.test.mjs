// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  KINDS,
  KIND_INFO,
  parseProvenance,
  offsetSeconds,
  formatOffset,
  formatUtc,
  formatValue,
  layerKeyFromMessage,
  derivedLayerKeys,
  provenanceOf,
} from './provenance.ts';
import {
  applyMessage,
  withLayer,
  withoutFile,
  withoutLayer,
  scanTimeOf,
  rowsOf,
  STYLISED_LOOK,
} from './layerRegistry.ts';

const obs = (extra = {}) => ({ kind: 'observed', source: 'KTLX', valid_time: '2013-05-20T20:16:43Z', ...extra });

test('six classes, each with a short label, a name and a definition', () => {
  assert.deepEqual([...KINDS], ['observed', 'model', 'retrieved', 'simulated', 'stylised', 'forecast']);
  for (const k of KINDS) {
    const i = KIND_INFO[k];
    assert.ok(i.short.length >= 3 && i.label && i.definition.length > 40, k);
  }
});

test('parseProvenance accepts a good object and drops what is malformed', () => {
  const p = parseProvenance({
    kind: 'retrieved',
    source: ' radarx ',
    method: 'x'.repeat(10),
    parameters: { a: 1, b: [1, 2] },
    references: ['ref 1', '', 42, 'ref 2'],
    inputs: 'not a list',
    junk: 'ignored',
    valid_time: '',
  });
  assert.equal(p.kind, 'retrieved');
  assert.equal(p.source, 'radarx');
  assert.deepEqual(p.references, ['ref 1', 'ref 2']);
  assert.equal(p.inputs, undefined);
  assert.equal(p.valid_time, undefined);
  assert.equal(p.junk, undefined);
  assert.deepEqual(p.parameters, { a: 1, b: [1, 2] });
});

test('parseProvenance refuses what is not provenance (never guesses a class)', () => {
  for (const bad of [null, undefined, 'observed', 5, [], {}, { kind: 'measured' }, { source: 'x' }, { kind: 7 }]) {
    assert.equal(parseProvenance(bad), null, JSON.stringify(bad));
  }
  assert.equal(parseProvenance({ kind: 'model' }).source, 'unknown');
});

test('parseProvenance bounds what it keeps (a hostile or huge object cannot flood the panel)', () => {
  const p = parseProvenance({
    kind: 'model',
    source: 's'.repeat(5000),
    method: 'm'.repeat(50000),
    references: Array.from({ length: 100 }, (_, i) => `r${i}`),
    parameters: Object.fromEntries(Array.from({ length: 200 }, (_, i) => [`k${i}`, i])),
  });
  assert.ok(p.source.length <= 300 && p.method.length <= 4000);
  assert.equal(p.references.length, 20);
  assert.equal(Object.keys(p.parameters).length, 40);
});

test('time offsets: sign, units and unknowns', () => {
  assert.equal(offsetSeconds('2022-03-30T01:00:00Z', '2022-03-30T00:48:00Z'), 720);
  assert.equal(offsetSeconds('2022-03-30T00:00:00Z', '2022-03-30T00:30:00Z'), -1800);
  assert.equal(offsetSeconds('', '2022-03-30T00:30:00Z'), null);
  assert.equal(offsetSeconds('2022-03-30T00:30:00Z', null), null);
  assert.equal(offsetSeconds('garbage', '2022-03-30T00:30:00Z'), null);
  const cases = [
    [null, 'unknown'],
    [undefined, 'unknown'],
    [NaN, 'unknown'],
    [0, 'same time'],
    [20, 'same time'],
    [-300, '-5 min'],
    [720, '+12 min'],
    [3900, '+1 h 05 min'],
    [-7200, '-2 h'],
    [86400 * 3, '+3 d'],
  ];
  for (const [s, text] of cases) assert.equal(formatOffset(s), text, String(s));
});

test('times are shown in UTC and never converted', () => {
  assert.equal(formatUtc('2022-03-30T00:05:00Z'), '2022-03-30 00:05 UTC');
  assert.equal(formatUtc('2022-03-30T00:05:00+00:00'), '2022-03-30 00:05 UTC');
  assert.equal(formatUtc(''), 'unknown');
  assert.equal(formatUtc('nope'), 'unknown');
});

test('formatValue renders parameters compactly', () => {
  assert.equal(formatValue(4000), '4000');
  assert.equal(formatValue(0.123456789), '0.12346');
  assert.equal(formatValue([1, 2.5]), '[1, 2.5]');
  assert.equal(formatValue({ a: 1, b: 'x' }), '{a=1, b=x}');
  assert.equal(formatValue(null), '');
});

test('each engine message maps to its layer', () => {
  const p = parseProvenance(obs());
  assert.deepEqual(layerKeyFromMessage('file_opened', { file_id: 'f1' }, p), {
    id: 'file:f1', group: 'Radar', title: 'Radar moments', fileId: 'f1',
  });
  assert.equal(layerKeyFromMessage('file_opened', {}, p), null);
  // raw sweeps are the file's layer; only derived ones are layers of their own
  assert.equal(layerKeyFromMessage('sweep_data', { file_id: 'f1', variable: 'DBZH' }, p), null);
  assert.equal(layerKeyFromMessage('sweep_data', { file_id: 'f1', variable: 'm:x', derived: true }, p).id, 'derived:f1:m:x');
  assert.equal(layerKeyFromMessage('mosaic_result', { mosaic_id: 'm1' }, p).id, 'mosaic:m1');
  assert.equal(layerKeyFromMessage('multidoppler_result', { wind_id: 'w1' }, p).id, 'winds:w1');
  assert.equal(layerKeyFromMessage('wind_volume', { wind_id: 'w1' }, p).id, 'winds:w1'); // same analysis
  assert.equal(layerKeyFromMessage('cells_tracked', {}, p).id, 'cells');
  assert.equal(layerKeyFromMessage('cross_section_result', {}, p).group, 'Sections');
  assert.equal(layerKeyFromMessage('volume_data', {}, p).id, 'volume3d');
  assert.equal(layerKeyFromMessage('sounding_result', {}, p).group, 'Environment');
  assert.equal(layerKeyFromMessage('env_fetch_result', { request_id: 'r1' }, p).id, 'env:r1');
  assert.equal(layerKeyFromMessage('pong', {}, p), null);
  assert.equal(layerKeyFromMessage('module_field', { module: 'm', name: 'f', file_id: 'f1' }, p).id, 'module:f1:m:f');
});

test('provenance sits at the top level, under sounding or under result', () => {
  assert.equal(provenanceOf({ provenance: 1 }), 1);
  assert.equal(provenanceOf({ sounding: { provenance: 2 } }), 2);
  assert.equal(provenanceOf({ result: { provenance: 3 } }), 3);
  assert.equal(provenanceOf({}), null);
});

test('registry: a file layer, derived layers, replacement and removal', () => {
  let s = {};
  s = applyMessage(s, 'file_opened', { file_id: 'f1', provenance: obs() }, 1);
  assert.equal(s['file:f1'].provenance.source, 'KTLX');
  s = applyMessage(s, 'mosaic_result', { mosaic_id: 'm1', provenance: { kind: 'retrieved', source: 'Mosaic', valid_time: '2013-05-20T20:20:00Z' } }, 2);
  s = applyMessage(s, 'derived_variables', {
    file_id: 'f1',
    variables: [
      { name: 'hid:cls', label: 'HID', provenance: { kind: 'retrieved', source: 'radarx_hid' } },
      { name: 'qc:z', label: 'QC Z', provenance: { kind: 'retrieved', source: 'qc' } },
    ],
  }, 3);
  assert.deepEqual(Object.keys(s).sort(), ['derived:f1:hid:cls', 'derived:f1:qc:z', 'file:f1', 'mosaic:m1']);
  // the list is complete: a variable that is gone from it is gone from the layers
  s = applyMessage(s, 'derived_variables', { file_id: 'f1', variables: [{ name: 'hid:cls', label: 'HID', provenance: { kind: 'retrieved', source: 'radarx_hid' } }] }, 4);
  assert.deepEqual(Object.keys(s).sort(), ['derived:f1:hid:cls', 'file:f1', 'mosaic:m1']);
  // an empty list (all cleared) removes them all but leaves the mosaic
  s = applyMessage(s, 'derived_variables', { file_id: 'f1', variables: [] }, 5);
  assert.deepEqual(Object.keys(s).sort(), ['file:f1', 'mosaic:m1']);
  s = withoutFile(s, 'f1');
  assert.deepEqual(Object.keys(s), ['mosaic:m1']);
  assert.equal(withoutLayer(s, 'nope'), s);
});

test('registry: messages without provenance change nothing, and never erase what is known', () => {
  let s = applyMessage({}, 'file_opened', { file_id: 'f1', provenance: obs() });
  assert.equal(applyMessage(s, 'progress', { percent: 10 }), s);
  assert.equal(applyMessage(s, 'sweep_data', { file_id: 'f1', variable: 'DBZH' }), s);
  const key = { id: 'file:f1', group: 'Radar', title: 'Radar moments', fileId: 'f1' };
  const again = withLayer(s, key, null);
  assert.equal(again['file:f1'].provenance.source, 'KTLX'); // kept
  const bad = withLayer(s, key, { kind: 'bogus' });
  assert.equal(bad['file:f1'].provenance.source, 'KTLX');
  const unknown = withLayer({}, { id: 'x', group: 'Cells', title: 'Cells' }, undefined);
  assert.equal(unknown.x.provenance, null); // listed, shown as unknown
});

test('registry: time offset to the active radar scan', () => {
  let s = {};
  s = applyMessage(s, 'file_opened', { file_id: 'f1', provenance: obs() });
  s = applyMessage(s, 'file_opened', { file_id: 'f2', provenance: obs({ valid_time: '2013-05-20T20:30:00Z' }) });
  s = applyMessage(s, 'mosaic_result', { mosaic_id: 'm1', provenance: { kind: 'retrieved', source: 'Mosaic', valid_time: '2013-05-20T20:22:43Z' } });
  s = applyMessage(s, 'cells_identified', { provenance: { kind: 'retrieved', source: 'cells' } }); // no time known
  assert.equal(scanTimeOf(s, 'f1'), '2013-05-20T20:16:43Z');
  assert.equal(scanTimeOf(s, 'f2'), '2013-05-20T20:30:00Z');
  assert.equal(scanTimeOf(s, null), null);
  const rows = rowsOf(s, scanTimeOf(s, 'f1'));
  const byId = Object.fromEntries(rows.map((r) => [r.id, r]));
  assert.equal(byId['file:f1'].offset, 0);
  assert.equal(byId['file:f2'].offset, 13 * 60 + 17);
  assert.equal(byId['mosaic:m1'].offset, 360);
  assert.equal(byId.cells.offset, null);
  assert.deepEqual(rows.map((r) => r.group), ['Radar', 'Radar', 'Mosaic', 'Cells']); // grouped in a fixed order
  assert.equal(rowsOf(s, null).find((r) => r.id === 'mosaic:m1').offset, null); // no radar scan: unknown
});

test('the realistic 3D look is listed as stylised', () => {
  const s = withLayer({}, STYLISED_LOOK, STYLISED_LOOK.provenance);
  assert.equal(s['look:realistic'].provenance.kind, 'stylised');
  assert.equal(s['look:realistic'].group, 'Appearance');
});

test('derivedLayerKeys ignores malformed lists', () => {
  assert.deepEqual(derivedLayerKeys({}), []);
  assert.deepEqual(derivedLayerKeys({ file_id: 'f', variables: 'x' }), []);
  assert.equal(derivedLayerKeys({ file_id: 'f', variables: [null, { name: 'a' }, {}] }).length, 1);
});
