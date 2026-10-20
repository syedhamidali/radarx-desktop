// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { scaleBarKm, headingOf, hfovFromTan } from './perilsMinimap.ts';
import { balloonFlight, BALLOON_FADE_S, BALLOON_TRAIL_FADE_S } from './perilsData.ts';
import { crossfade, LAYER_TABLE } from './perilsLayers.ts';

test('the scale bar is a round length of about a quarter of the map', () => {
  assert.equal(scaleBarKm(62), 20);
  assert.equal(scaleBarKm(24), 10);
  assert.equal(scaleBarKm(3.2), 2);
  assert.equal(scaleBarKm(200), 100);
});

test('heading of a motion vector, and none when it is still', () => {
  assert.equal(headingOf(10, 0), 90);
  assert.equal(headingOf(0, -5), 180);
  assert.equal(headingOf(0.1, 0.1), null);
  assert.ok(Math.abs(headingOf(-3, 3) - 315) < 1e-9);
});

test('the field of view of the cone from the half-width tangent of the frame', () => {
  assert.ok(Math.abs(hfovFromTan(1) - 90) < 1e-9);
  assert.ok(Math.abs(hfovFromTan(Math.tan((73 * Math.PI) / 360)) - 73) < 1e-9);
});

const lv = { pressure_hpa: [1000], height_msl_m: [0], temperature_c: [20], dewpoint_c: [10], u_ms: [0], v_ms: [0] };
const sonde = { drift: [[100, 0, 0, 0.1], [300, 4, 2, 3.0]], levels: lv };

test('a radiosonde appears at the first point of its track, flies the measured track, and fades after the last point', () => {
  assert.equal(balloonFlight(sonde, 99, 8), null);
  const a = balloonFlight(sonde, 200, 8);
  assert.ok(Math.abs(a.x - 2) < 1e-9 && Math.abs(a.y - 1) < 1e-9 && Math.abs(a.z - 1.55) < 1e-9);
  assert.equal(a.ended, false);
  assert.equal(a.bodyAlpha, 1);
  assert.equal(a.trail.length, 1); // only the points up to now
  // after the last point: it stays at the top, the body fades within 1.5 clip seconds, the trail later, then nothing
  const e = balloonFlight(sonde, 300 + 0.5 * BALLOON_FADE_S * 8, 8);
  assert.equal(e.ended, true);
  assert.ok(e.z === 3.0 && e.bodyAlpha > 0.4 && e.bodyAlpha < 0.6);
  const f = balloonFlight(sonde, 300 + 1.01 * BALLOON_FADE_S * 8, 8);
  assert.equal(f.bodyAlpha, 0);
  assert.ok(f.trailAlpha > 0);
  assert.equal(balloonFlight(sonde, 300 + 1.01 * BALLOON_TRAIL_FADE_S * 8, 8), null);
});

test('no track or no profile: nothing is drawn, and nothing is extrapolated', () => {
  assert.equal(balloonFlight({ drift: [], levels: lv }, 100, 1), null);
  assert.equal(balloonFlight({ drift: sonde.drift, levels: null }, 200, 1), null);
  assert.equal(balloonFlight(sonde, 5000, 1), null);
});

const shots = [
  { name: 'establishing', t0: 0, t1: 15 }, { name: 'flyby', t0: 15, t1: 46 }, { name: 'tornado', t0: 46, t1: 70 },
  { name: 'window', t0: 70, t1: 90 }, { name: 'rise', t0: 90, t1: 105 }, { name: 'topdown', t0: 105, t1: 120 },
];

test('the immersive grade takes the labels, arrows and cards out of the tornado shot only; the plan-view inset stays', () => {
  for (const id of ['stations', 'radars', 'rotation', 'tornadoTrack', 'flow', 'zdr', 'balloon', 'isotherms', 'captions', 'lmaPanel', 'stationCard', 'envPanel', 'stripCharts', 'skewt', 'surfaceModels']) {
    assert.equal(crossfade(shots, LAYER_TABLE[id], 58), 0, id);
  }
  assert.equal(crossfade(shots, LAYER_TABLE.immersion, 58), 1);
  assert.equal(crossfade(shots, LAYER_TABLE.immersion, 30), 0);
  assert.equal(crossfade(shots, LAYER_TABLE.sim, 58), 1);
  assert.ok(crossfade(shots, LAYER_TABLE.minimap, 58) > 0.3 && crossfade(shots, LAYER_TABLE.minimap, 58) < 1);
  assert.equal(crossfade(shots, LAYER_TABLE.minimap, 20), 1);
  assert.equal(crossfade(shots, LAYER_TABLE.minimap, 110), 1);
});
