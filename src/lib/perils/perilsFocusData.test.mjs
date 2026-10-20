// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { focusNow, offsetStats, offsetText, focusCaption, bearingOf, SHEAR_CANDIDATE } from './perilsFocusData.ts';

const pt = (t, as, vrot, x, y, ref, extra = {}) => ({
  t, as, vrot, x, y,
  offset_km: x == null ? null : Math.hypot(x - ref[0], y - ref[1]),
  bearing_deg: x == null ? null : bearingOf(x - ref[0], y - ref[1]),
  radar: 'NOXP', ref_x: ref[0], ref_y: ref[1], phase: t >= 240 ? 'on' : 'before',
  core50_km: 1.3, core50_bearing_deg: 305, cmax_at_dbz: 47, sr_wind: { speed_ms: 19, from_deg: 186, valid_fraction: 1 },
  ...extra,
});

const data = {
  vsr: [], shear: [], vsrLim: 35, shearLim: 0.04, motion_ms: [10.6, 0], radiusKm: 3,
  series: [
    pt(180, 0.018, 17, 6.0, -51.5, [7.4, -52.8]),
    pt(240, 0.018, 17, 6.0, -51.5, [7.4, -52.8]),
    pt(300, 0.018, 17, 6.0, -51.5, [8.5, -51.8]),
    pt(360, 0.020, 18, 9.5, -49.0, [9.5, -50.8]),
    pt(420, 0.026, 19, 10.5, -48.0, [10.6, -49.8]),
  ],
};

test('the bearing of a vector is clockwise from north', () => {
  assert.equal(Math.round(bearingOf(0, 1)), 0);
  assert.equal(Math.round(bearingOf(1, 0)), 90);
  assert.equal(Math.round(bearingOf(0, -1)), 180);
  assert.equal(Math.round(bearingOf(-1, 1)), 315);
});

test('the rotation at a data time is interpolated between the steps and the offset is from the surveyed position of the moment', () => {
  const ref = { x: 9.0, y: -51.3 };
  const n = focusNow(data, 330, ref); // half way between the steps at 300 s and 360 s
  assert.ok(Math.abs(n.as - 0.019) < 1e-9 && Math.abs(n.vrot - 17.5) < 1e-9);
  assert.ok(Math.abs(n.cx - 7.75) < 1e-9 && Math.abs(n.cy - (-50.25)) < 1e-9);
  assert.ok(Math.abs(n.offsetKm - Math.hypot(7.75 - 9.0, -50.25 + 51.3)) < 1e-9);
  assert.equal(Math.round(n.bearingDeg), Math.round(bearingOf(7.75 - 9.0, -50.25 + 51.3)));
  assert.equal(n.radar, 'NOXP');
  assert.equal(n.core50Km, 1.3);
  assert.equal(n.srWindFrom, 186);
  // outside the series the nearest end holds
  assert.ok(Math.abs(focusNow(data, 9999, ref).as - 0.026) < 1e-9 && Math.abs(focusNow(data, -5, ref).as - 0.018) < 1e-9);
  assert.equal(focusNow(null, 0, ref), null);
  assert.equal(focusNow({ ...data, series: [] }, 0, ref), null);
});

test('a step without a rotation maximum gives no offset and does not poison the neighbours', () => {
  const d = { ...data, series: [pt(240, null, null, null, null, [7.4, -52.8]), pt(300, 0.02, 15, 6, -51, [8.5, -51.8])] };
  const near = focusNow(d, 290, { x: 8, y: -52 }); // nearer the step that has a value
  assert.ok(Math.abs(near.as - 0.02) < 1e-9 && near.cx === 6 && near.cy === -51);
  const m = focusNow(d, 250, { x: 8, y: -52 }); // nearer the step that has none: nothing is shown
  assert.equal(m.as, null);
  assert.equal(offsetText(m), '');
});

test('the offsets of the maxima over the survey are summarised with their mean direction', () => {
  const st = offsetStats(data);
  assert.equal(st.n, 4); // the 'before' step (180 s) is not part of it
  assert.ok(st.minKm > 1 && st.maxKm < 3 && st.minKm <= st.medianKm && st.medianKm <= st.maxKm);
  assert.ok(st.meanBearing > 270 && st.meanBearing < 360); // north-west of the surveyed position
  assert.equal(offsetStats({ ...data, series: [] }), null);
  assert.ok(offsetText(focusNow(data, 300, { x: 8.5, y: -51.8 })).includes('km WNW of the surveyed position') || offsetText(focusNow(data, 300, { x: 8.5, y: -51.8 })).includes('km W of'));
});

const info = {
  rating: 'EF2', widthM: 366, touchdown: '01:08:00', end: '01:20:00', trackKm: 17.7, doneKm: 5.1, speedMs: 24.6, headingDeg: 46, distanceM: 450,
  periodCoreS: 7.9, station: { name: 'TTU StickNet 0102A', km: 9.9, bearing: 285, tempC: 16, dewC: 15.7, windMs: 9, windFrom: 292, dTempC: -0.2 },
};

test('every angle has a title and lines made of the data, and says what is simulated, estimated or interpolated', () => {
  const now = focusNow(data, 330, { x: 9.0, y: -51.3 });
  const st = offsetStats(data);
  const all = {};
  for (const b of ['focus_wide', 'focus_chaser', 'focus_close', 'focus_low', 'focus_swath', 'focus_top']) {
    const c = focusCaption(b, now, info, st);
    assert.ok(c.title.length > 5 && c.lines.length >= 1, b);
    all[b] = c.lines.join(' ');
  }
  assert.match(all.focus_wide, /01:08:00-01:20:00/);
  assert.match(all.focus_wide, /INTERPOLATED/);
  assert.match(all.focus_wide, /squall-line \(QLCS\) segment/);
  assert.match(all.focus_wide, /not an isolated supercell/);
  assert.match(all.focus_wide, /1\.3 km to the NW/); // the strong echo, from the data
  assert.match(all.focus_wide, /from the S/); // the inflow
  assert.match(all.focus_chaser, /SIMULATION/);
  assert.match(all.focus_chaser, /strongest .* of the surveyed position/);
  assert.match(all.focus_chaser, /survey position is the one used/);
  assert.match(all.focus_close, /25 m\/s toward the NE/);
  assert.match(all.focus_close, /7\.9 s/);
  assert.match(all.focus_close, /unresolved/);
  assert.match(all.focus_low, /StickNet 0102A/);
  assert.match(all.focus_low, /rain-cooled air already/);
  assert.match(all.focus_swath, /MODELLED/);
  assert.match(all.focus_swath, /not the surveyed damage/);
  assert.match(all.focus_top, /5\.1 of 17\.7 km/);
  assert.match(all.focus_top, /median/);
  assert.equal(focusCaption('nothing', now, info, st).title, '');
});

test('the captions never call the storm a supercell', () => {
  const now = focusNow(data, 330, { x: 9.0, y: -51.3 });
  for (const b of ['focus_wide', 'focus_chaser', 'focus_close', 'focus_low', 'focus_swath', 'focus_top']) {
    const c = focusCaption(b, now, info, offsetStats(data));
    const text = `${c.title} ${c.lines.join(' ')}`;
    assert.ok(!/is a supercell|a supercell\b(?! with)/.test(text.replace('isolated supercell', '')) || /not|no /.test(text), b);
    assert.ok(!/mesocyclone/i.test(text), b);
  }
});

test('a cold-pool claim is made only when the station shows a change', () => {
  const now = focusNow(data, 330, { x: 9.0, y: -51.3 });
  const cold = { ...info, station: { ...info.station, dTempC: -3.2 } };
  assert.match(focusCaption('focus_low', now, cold, null).lines.join(' '), /-3\.2 K in six minutes: a change of the air mass/);
  const none = focusCaption('focus_low', now, { ...info, station: null }, null);
  assert.equal(none.lines.length, 1);
});

test('the rotation threshold is the data tool\'s vortex threshold', () => {
  assert.equal(SHEAR_CANDIDATE, 0.012);
});
