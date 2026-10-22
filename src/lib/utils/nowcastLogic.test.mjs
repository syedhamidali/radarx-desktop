// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  NOWCAST_MAX_LEAD_MIN,
  NOWCAST_MAX_MEMBERS,
  DEFAULT_NOWCAST_SETTINGS,
  GUIDANCE_FALLBACK,
  SKILL_FALLBACK,
  VERIFIED_LIMITS,
  leadChoices,
  snapStep,
  snapLead,
  clampMembers,
  sanitizeSettings,
  withStep,
  leadCount,
  inputFromFiles,
  inputFromLive,
  inputFromGrid,
  buildRunMessage,
  productKey,
  parseProduct,
  productChoices,
  parseResult,
  parseLayer,
  layerKey,
  stepIndex,
  leadIndexNear,
  formatLead,
  formatSigma,
  frameLabel,
  captionLines,
  wrapText,
  exportBaseName,
  sigmaChart,
} from './nowcastLogic.ts';

const T0 = Date.UTC(2021, 11, 11, 2, 0, 0);
const row = (id, min, selected = true) => ({ id, name: id.toUpperCase(), timeMs: min === null ? null : T0 + min * 60_000, selected });

test('leads are whole numbers of steps and never exceed 120 minutes', () => {
  assert.deepEqual(leadChoices(15).slice(-2), [105, 120]);
  assert.equal(leadChoices(5).length, 24);
  assert.equal(leadChoices(5)[0], 5);
  assert.equal(snapLead(500, 5), NOWCAST_MAX_LEAD_MIN);
  assert.equal(snapLead(0, 5), 5);
  assert.equal(snapLead(47, 10), 50);
  assert.equal(snapLead('x', 10), 60);
  assert.equal(snapLead(120, 15), 120);
});

test('the step snaps to the offered steps', () => {
  assert.equal(snapStep(7), 5);
  assert.equal(snapStep(8), 10);
  assert.equal(snapStep(100), 15);
  assert.equal(snapStep(undefined), 5);
});

test('members are 0 to 8', () => {
  assert.equal(clampMembers(-3), 0);
  assert.equal(clampMembers(20), NOWCAST_MAX_MEMBERS);
  assert.equal(clampMembers(3.6), 4);
  assert.equal(clampMembers('many'), 0);
});

test('stored settings are made valid, unknown keys are dropped', () => {
  assert.deepEqual(sanitizeSettings(null), DEFAULT_NOWCAST_SETTINGS);
  const s = sanitizeSettings({ source: 'moon', leadMin: 999, stepMin: 10, members: 99, resolutionM: 500, gridPath: 7, extra: 1 });
  assert.equal(s.source, 'files');
  assert.equal(s.leadMin, 120);
  assert.equal(s.stepMin, 10);
  assert.equal(s.members, 8);
  assert.equal(s.resolutionM, 1000);
  assert.equal(s.gridPath, '');
  assert.ok(!('extra' in s));
  assert.equal(sanitizeSettings({ source: 'live' }).source, 'live');
});

test('changing the step keeps the lead a whole number of steps', () => {
  const s = withStep({ ...DEFAULT_NOWCAST_SETTINGS, leadMin: 50, stepMin: 5 }, 15);
  assert.equal(s.stepMin, 15);
  assert.equal(s.leadMin % 15, 0);
  assert.equal(leadCount({ leadMin: 60, stepMin: 5 }), 12);
  assert.equal(leadCount({ leadMin: 120, stepMin: 5 }), 24);
});

test('open files: selected volumes in time order', () => {
  const r = inputFromFiles([row('b', 10), row('a', 0), row('c', 5), row('x', 20, false)]);
  assert.equal(r.problem, null);
  assert.deepEqual(r.ids, ['a', 'c', 'b']);
  assert.match(r.summary, /3 volumes, 02:00 to 02:10 UTC/);
  assert.equal(r.caution, null);
});

test('open files: nothing selected, no scan time, one time, a long gap', () => {
  assert.match(inputFromFiles([]).problem, /Open radar volumes/);
  assert.match(inputFromFiles([row('a', 0, false)]).problem, /Open radar volumes/);
  assert.match(inputFromFiles([row('a', 0), row('b', null)]).problem, /No scan time for B/);
  const one = inputFromFiles([row('a', 0)]);
  assert.equal(one.problem, null);
  assert.match(one.caution, /persistence/);
  // two radars of one scan are one time
  assert.match(inputFromFiles([row('a', 0), row('b', 1)]).caution, /persistence/);
  assert.match(inputFromFiles([row('a', 0), row('b', 5), row('c', 50)]).caution, /gap of 45 min/);
});

const frame = (id, min) => ({ id, time: new Date(T0 + min * 60_000).toISOString(), mosaicId: `m-${id}` });

test('live loop: the newest frames the engine still holds, oldest first', () => {
  const frames = [0, 5, 10, 15, 20, 25].map((m, i) => frame(`f${i}`, m));
  const r = inputFromLive(frames);
  assert.equal(r.problem, null);
  assert.deepEqual(r.ids, ['m-f3', 'm-f4', 'm-f5']);
  assert.match(r.summary, /last 3 live frames/);
  assert.deepEqual(inputFromLive(frames, 10).ids.length, 4); // capped at what the engine keeps
  assert.equal(inputFromLive(frames, 0).ids.length, 1);
});

test('live loop: empty, single and repeated frames', () => {
  assert.match(inputFromLive([]).problem, /no frames yet/);
  assert.match(inputFromLive([{ id: 'a', time: null, mosaicId: '' }]).problem, /no frames yet/);
  assert.match(inputFromLive([frame('a', 0)]).caution, /One live frame only/);
  assert.match(inputFromLive([frame('a', 0), frame('b', 0)]).caution, /same time/);
  assert.match(inputFromLive([frame('a', 0), frame('b', 45)]).caution, /gap of 45 min/);
});

test('grid source needs a path', () => {
  assert.match(inputFromGrid('  ').problem, /gridded frames file/);
  const g = inputFromGrid('/data/nowcast/V1_frames.nc');
  assert.equal(g.problem, null);
  assert.equal(g.summary, 'V1_frames.nc');
});

test('the run message names its source and asks only for the nowcast layers', () => {
  const s = { ...DEFAULT_NOWCAST_SETTINGS, leadMin: 90, stepMin: 10, members: 6 };
  const files = buildRunMessage(s, inputFromFiles([row('a', 0), row('b', 5)]), 'r1');
  assert.equal(files.type, 'nowcast_run');
  assert.equal(files.request_id, 'r1');
  assert.deepEqual(files.file_ids, ['a', 'b']);
  assert.ok(!('mosaic_ids' in files) && !('frames_path' in files));
  assert.deepEqual(files.options, { lead_min: 90, step_min: 10, n_members: 6, n_frames: 3, layers: 'deterministic', resolution_m: 1000 });
  const live = buildRunMessage(s, inputFromLive([frame('a', 0), frame('b', 5)]), 'r2');
  assert.deepEqual(live.mosaic_ids, ['m-a', 'm-b']);
  assert.ok(!('file_ids' in live) && !('resolution_m' in live.options));
  const grid = buildRunMessage(s, inputFromGrid('/x/f.nc'), 'r3');
  assert.equal(grid.frames_path, '/x/f.nc');
  // out-of-range settings never reach the engine
  const bad = buildRunMessage({ ...s, leadMin: 4000, members: 400, stepMin: 7 }, inputFromGrid('/x/f.nc'), 'r4');
  assert.equal(bad.options.lead_min, 120);
  assert.equal(bad.options.n_members, 8);
  assert.equal(bad.options.step_min, 5);
});

test('product keys round trip', () => {
  assert.equal(productKey('deterministic'), 'det');
  assert.equal(productKey('probability', 35), 'p:35');
  assert.equal(productKey('probability', 0.5, true), 'r:0.5');
  assert.deepEqual(parseProduct('p:35'), { kind: 'probability', threshold: 35, rate: false });
  assert.deepEqual(parseProduct('r:0.5'), { kind: 'probability', threshold: 0.5, rate: true });
  assert.deepEqual(parseProduct('det'), { kind: 'deterministic', threshold: null, rate: false });
  assert.equal(parseProduct('p:x'), null);
  assert.equal(parseProduct(''), null);
});

const RESULT = {
  type: 'nowcast_result',
  nowcast_id: 'abc123',
  origin_time: '2021-12-11T02:10:00Z',
  leads_min: [5, 10, 15],
  valid_times: ['2021-12-11T02:15:00Z', '2021-12-11T02:20:00Z', '2021-12-11T02:25:00Z'],
  field: 'reflectivity',
  units: 'dBZ',
  n_members: 4,
  thresholds: [20, 35, 45],
  rate_thresholds: [0.5, 2, 10],
  corners: [[-90, 38], [-86, 38], [-86, 34], [-90, 34]],
  motion: { u_mean_ms: 14.2, v_mean_ms: -0.4, method: 'lucas_kanade' },
  stats: {
    leads: [
      { lead_min: 5, position_sigma_km: 1.1, forecast_fraction: 0.97 },
      { lead_min: 10, position_sigma_km: 2.2, forecast_fraction: 0.93 },
      { lead_min: 15, position_sigma_km: 3.3, forecast_fraction: 0.9 },
    ],
  },
  notes: ['a note'],
  sources: ['a', 'b', 'c'],
  n_input_frames: 5,
  provenance: { kind: 'forecast', source: 'mosaic: KPAH', uncertainty: 'x' },
  record: { skill: 'Verified on 8 public NEXRAD cases.', guidance: 'Experimental guidance, not an official forecast.' },
};

test('the result is parsed with its skill statement, guidance and per-lead uncertainty', () => {
  const r = parseResult(RESULT);
  assert.equal(r.nowcastId, 'abc123');
  assert.deepEqual(r.leads, [5, 10, 15]);
  assert.deepEqual(r.sigmaKm, [1.1, 2.2, 3.3]);
  assert.equal(r.skill, 'Verified on 8 public NEXRAD cases.');
  assert.equal(r.guidance, 'Experimental guidance, not an official forecast.');
  assert.equal(r.provenance.kind, 'forecast');
  assert.equal(r.members, 4);
  assert.equal(r.corners.length, 4);
  assert.equal(r.motion.u, 14.2);
});

test('a result without the wording still carries a guidance sentence and says the skill is unknown', () => {
  const r = parseResult({ ...RESULT, record: {}, provenance: null });
  assert.equal(r.guidance, GUIDANCE_FALLBACK);
  assert.equal(r.skill, SKILL_FALLBACK);
  assert.equal(r.provenance, null);
  assert.match(GUIDANCE_FALLBACK, /not an official forecast/);
});

test('a malformed result is refused, a partial one degrades', () => {
  assert.equal(parseResult(null), null);
  assert.equal(parseResult({}), null);
  assert.equal(parseResult({ ...RESULT, leads_min: [] }), null);
  assert.equal(parseResult({ ...RESULT, nowcast_id: '' }), null);
  const r = parseResult({ ...RESULT, stats: {}, valid_times: undefined, corners: [1] });
  assert.deepEqual(r.sigmaKm, [null, null, null]);
  assert.deepEqual(r.validTimes, ['', '', '']);
  assert.deepEqual(r.corners, []);
});

test('a layer needs an image, a lead and a known kind', () => {
  const ok = parseLayer({ lead_min: 10, kind: 'probability', threshold: 35, rate: false, png_b64: 'AAAA', vmin: 0, vmax: 1, legend: [[0, '#000'], [1, '#fff']], label: 'P(>= 35 dBZ) +10 min [FORECAST]' });
  assert.equal(ok.lead, 10);
  assert.equal(layerKey(ok), 'p:35');
  assert.equal(ok.legend.length, 2);
  assert.equal(parseLayer({ lead_min: 10, kind: 'probability', png_b64: '' }), null);
  assert.equal(parseLayer({ lead_min: 'x', kind: 'deterministic', png_b64: 'A' }), null);
  assert.equal(parseLayer({ lead_min: 5, kind: 'mean', png_b64: 'A' }), null);
  assert.equal(layerKey(parseLayer({ lead_min: 5, kind: 'deterministic', png_b64: 'A' })), 'det');
  assert.equal(layerKey(parseLayer({ lead_min: 5, kind: 'probability', threshold: 2, rate: true, png_b64: 'A' })), 'r:2');
});

test('the product selector offers the nowcast, the probabilities and the rain-rate probabilities', () => {
  const choices = productChoices(parseResult(RESULT));
  assert.deepEqual(choices.map((c) => c.key), ['det', 'p:20', 'p:35', 'p:45', 'r:0.5', 'r:2', 'r:10']);
  assert.match(choices[2].label, /at least 35 dBZ/);
  assert.match(choices[5].label, /2 mm\/h/);
  const rate = productChoices({ field: 'rain_rate', units: 'mm/h', thresholds: [0.5, 2], rateThresholds: [0.5, 2] });
  assert.deepEqual(rate.map((c) => c.key), ['det', 'p:0.5', 'p:2']);
});

test('the slider steps and clamps', () => {
  assert.equal(stepIndex(0, 4, -1), 0);
  assert.equal(stepIndex(3, 4, 1), 3);
  assert.equal(stepIndex(1, 4, 1), 2);
  assert.equal(stepIndex(2, 4, 'first'), 0);
  assert.equal(stepIndex(2, 4, 'last'), 3);
  assert.equal(stepIndex(2, 0, 1), 0);
  assert.equal(leadIndexNear([5, 10, 15], 11), 1);
  assert.equal(leadIndexNear([], 11), 0);
});

test('every frame label says FORECAST, the origin, the valid time, the lead and the guidance', () => {
  const r = parseResult(RESULT);
  const l = frameLabel(r, 1);
  assert.equal(l.chip, 'FCST');
  assert.equal(l.headline, 'FORECAST +10 min');
  assert.equal(l.origin, 'Origin 2021-12-11 02:10 UTC');
  assert.equal(l.valid, 'Valid 2021-12-11 02:20 UTC');
  assert.equal(l.sigma, 'Position uncertainty 2.2 km (1 sigma)');
  assert.match(l.guidance, /not an official forecast/);
  assert.match(l.skill, /Verified on 8 public NEXRAD cases/);
  assert.equal(l.ensemble, '4 members');
  assert.match(frameLabel(r, 0, 'p:35').product, /Probability of at least 35 dBZ/);
  assert.match(frameLabel(r, 0, 'r:2').product, /2 mm\/h/);
  // an index out of range falls back to the ends, never to an unlabelled frame
  assert.equal(frameLabel(r, 99).lead, '+15 min');
  assert.equal(frameLabel(r, -3).lead, '+5 min');
  assert.equal(frameLabel({ ...r, members: 0 }, 0).ensemble, 'single run (no ensemble)');
});

test('the saved picture carries all of the label', () => {
  const lines = captionLines(frameLabel(parseResult(RESULT), 2, 'p:35')).join('\n');
  for (const part of ['FCST', 'FORECAST +15 min', 'Origin 2021-12-11 02:10 UTC', 'Valid 2021-12-11 02:25 UTC', 'not an official forecast', 'Verified on 8 public NEXRAD cases', 'Probability of at least 35 dBZ']) {
    assert.ok(lines.includes(part), part);
  }
});

test('text wraps at words', () => {
  assert.deepEqual(wrapText('aa bb cc dd', 5), ['aa bb', 'cc dd']);
  assert.deepEqual(wrapText('', 10), []);
  assert.deepEqual(wrapText('longerthanthewidth x', 5), ['longerthanthewidth', 'x']);
});

test('file names say forecast, origin, product and lead', () => {
  const r = parseResult(RESULT);
  assert.equal(exportBaseName(r, 1, 'det'), 'fcst_nowcast_20211211T0210_plus010min');
  assert.equal(exportBaseName(r, 2, 'p:35'), 'fcst_prob_dbz35_20211211T0210_plus015min');
  assert.equal(exportBaseName(r, 0, 'r:0.5'), 'fcst_prob_rate0p5_20211211T0210_plus005min');
});

test('lead and sigma wording', () => {
  assert.equal(formatLead(30), '+30 min');
  assert.equal(formatLead(7.5), '+7.5 min');
  assert.equal(formatSigma(3.14), '3.1 km');
  assert.equal(formatSigma(12.4), '12 km');
  assert.equal(formatSigma(null), 'not given');
});

test('the sigma chart puts the marker on the shown lead and skips unknown values', () => {
  const c = sigmaChart([5, 10, 15], [1, null, 3], 2);
  assert.equal(c.points.length, 2);
  assert.ok(c.markX > c.points[0].x);
  assert.equal(c.points[1].x, c.markX);
  assert.ok(c.max >= 3);
  assert.equal(sigmaChart([5, 10], [1, null], 1).markX, null);
  const empty = sigmaChart([], [], 0);
  assert.equal(empty.points.length, 0);
  assert.equal(empty.markX, null);
});

test('the limits text states the main verified limits', () => {
  const all = VERIFIED_LIMITS.join(' ');
  assert.match(all, /no storm grows, decays or forms/);
  assert.match(all, /not beat it anywhere beyond an hour/);
  assert.match(all, /never no rain/);
  assert.ok(VERIFIED_LIMITS.length <= 6);
});
