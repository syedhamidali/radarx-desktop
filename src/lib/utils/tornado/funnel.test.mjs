// @ts-nocheck - plain node:test file, not part of the app bundle
import test from 'node:test';
import assert from 'node:assert/strict';
import { makeVortex, pressureDeficit } from './vortexField.ts';
import {
  makeCondition, lclEspy, lclBolton, parcelAt, funnelBase, funnelRadius, totalDeficit, heightDeficit, baseHeight,
  condensationPressureRatio, satVaporPressure, dewpointFromVapor, condensate, shapeClass,
} from './funnel.ts';

// UIUC SONDE4 near the tornado track (33.06 N, 88.42 W), launched 00:31 UTC: T 20.9 C, Td 19.5 C, 995.4 hPa
const TH = { t0: 20.9, td0: 19.5, p0: 995.4 };
const V = makeVortex({ vt: 55, rm: 70 });

test('vapour pressure and dew point are inverse functions', () => {
  for (const t of [-5, 5, 15, 25]) assert.ok(Math.abs(dewpointFromVapor(satVaporPressure(t)) - t) < 1e-9);
});

test('a parcel is saturated exactly at the critical pressure ratio', () => {
  const pi = condensationPressureRatio(TH);
  const p = parcelAt(TH, pi * TH.p0);
  assert.ok(Math.abs(p.t - p.td) < 1e-6, `T - Td = ${p.t - p.td}`);
  // just above the critical pressure the parcel is unsaturated, just below saturated
  const above = parcelAt(TH, (pi + 0.01) * TH.p0), below = parcelAt(TH, (pi - 0.01) * TH.p0);
  assert.ok(above.t > above.td && below.t < below.td);
});

test('the cloud base without a vortex matches the LCL formula (Espy and Bolton)', () => {
  const c = makeCondition(TH);
  const espy = lclEspy(TH.t0, TH.td0), bolton = lclBolton(TH.t0, TH.td0);
  assert.ok(Math.abs(c.lcl - espy) / espy < 0.05, `solved ${c.lcl} m vs Espy ${espy} m`);
  assert.ok(Math.abs(c.lcl - bolton) / bolton < 0.05, `solved ${c.lcl} m vs Bolton ${bolton} m`);
  // a drier inflow raises the base proportionally (T - Td = 6 K -> about 750 m)
  const dry = makeCondition({ t0: 25, td0: 19, p0: 990 });
  assert.ok(Math.abs(dry.lcl - lclEspy(25, 19)) / lclEspy(25, 19) < 0.05, `${dry.lcl}`);
  assert.ok(dry.lcl > 3 * c.lcl);
});

test('funnel base: LCL for no vortex, lower with a deficit, on the ground when the deficit exceeds the critical one', () => {
  const dry = makeCondition({ t0: 25, td0: 19, p0: 990 }); // LCL ~ 750 m
  const weak = makeVortex({ vt: 25, rm: 70 });
  const strong = makeVortex({ vt: 55, rm: 70 });
  assert.ok(Math.abs(baseHeight(dry, 0) - dry.lcl) < 1e-6);
  const bWeak = funnelBase(dry, weak), bStrong = funnelBase(dry, strong);
  assert.ok(bWeak < dry.lcl && bWeak > 0, `weak vortex: base ${bWeak} m below LCL ${dry.lcl}`);
  assert.ok(bStrong < bWeak, 'a stronger vortex lowers the base more');
  // the lowering equals the vortex deficit expressed as height (dp / rho g)
  const dz = dry.lcl - bWeak;
  const dpAtBase = pressureDeficit(weak, 0, bWeak);
  assert.ok(Math.abs(dz * 1.15 * 9.80665 - dpAtBase) / dpAtBase < 0.15, `dz=${dz}, dp/rho g=${dpAtBase / 1.15 / 9.80665}`);
  // the low-LCL inflow near the track: funnel reaches the ground for EF2
  const c = makeCondition(TH);
  assert.equal(funnelBase(c, V), 0);
  assert.ok(funnelBase(c, makeVortex({ vt: 12, rm: 70 })) > 0, 'a feeble vortex does not reach the ground');
});

test('funnel boundary: where the total deficit equals the critical one, radius shrinks upward-flare consistent', () => {
  const dry = makeCondition(TH);
  const strong = makeVortex({ vt: 55, rm: 70 });
  const zb = funnelBase(dry, strong);
  assert.equal(zb, 0, 'EF2 reaches the ground in the moist inflow of the track');
  const drier = makeCondition({ t0: 25, td0: 19, p0: 990 });
  const zb2 = funnelBase(drier, strong);
  assert.ok(zb2 > 100 && zb2 < drier.lcl, `in drier air (LCL ${drier.lcl} m) the funnel hangs at ${zb2} m`);
  for (const z of [250, 400, 600]) {
    const r = funnelRadius(drier, strong, z);
    assert.ok(r > 0, `z=${z}`);
    // on the boundary the total deficit equals the critical one; inside it is larger, outside smaller
    assert.ok(Math.abs(totalDeficit(drier, strong, r, z) - drier.dpCrit) / drier.dpCrit < 1e-6);
    assert.ok(totalDeficit(drier, strong, 0.5 * r, z) > drier.dpCrit);
    assert.ok(totalDeficit(drier, strong, 1.5 * r + 1, z) < drier.dpCrit);
  }
  assert.equal(funnelRadius(drier, strong, 100), 0, 'no condensation below the funnel base');
  assert.ok(funnelRadius(drier, strong, 300) < funnelRadius(drier, strong, 250) * 3);
});

test('hydrostatic height term and condensate', () => {
  const c = makeCondition(TH);
  assert.equal(heightDeficit(c, 0), 0);
  assert.ok(Math.abs(heightDeficit(c, 1000) - 1.15 * 9.80665 * 1000) / (1.15 * 9.80665 * 1000) < 0.06);
  assert.equal(condensate(c, c.dpCrit * 0.9), 0);
  assert.ok(condensate(c, c.dpCrit + 1000) > 0);
  assert.equal(shapeClass(20, 8, 400), 'rope');
  assert.equal(shapeClass(200, 150, 300), 'wedge');
});
