/**
 * Sun position (NOAA solar calculator, about 0.01 degree) and the light direction of the 3D
 * "Realistic" look.
 *
 * `sunLight` returns the real sun when it is well above the horizon and otherwise a documented,
 * artistic dusk light ("stylised lighting, not a photograph"): a storm scanned at night is not shown
 * in the dark, so the light is placed at the azimuth where the sun set that day, a few degrees above
 * the horizon.
 */

const RAD = Math.PI / 180;
const DEG = 180 / Math.PI;

export interface SunPos {
  /** Degrees clockwise from north. */
  azimuthDeg: number;
  /** Degrees above the horizon (negative = below, no refraction). */
  elevationDeg: number;
  declinationDeg: number;
}

function mod(a: number, n: number): number {
  return ((a % n) + n) % n;
}

/** Julian day of an instant. */
export function julianDay(date: Date): number {
  return date.getTime() / 86400000 + 2440587.5;
}

/** Sun position at an instant for a place (lat north, lon east, degrees). */
export function sunPosition(date: Date, latDeg: number, lonDeg: number): SunPos {
  const jd = julianDay(date);
  const T = (jd - 2451545.0) / 36525.0;
  const L0 = mod(280.46646 + T * (36000.76983 + T * 0.0003032), 360);
  const M = 357.52911 + T * (35999.05029 - 0.0001537 * T);
  const e = 0.016708634 - T * (0.000042037 + 0.0000001267 * T);
  const Mr = M * RAD;
  const C =
    Math.sin(Mr) * (1.914602 - T * (0.004817 + 0.000014 * T)) +
    Math.sin(2 * Mr) * (0.019993 - 0.000101 * T) +
    Math.sin(3 * Mr) * 0.000289;
  const trueLong = L0 + C;
  const omega = 125.04 - 1934.136 * T;
  const lambda = trueLong - 0.00569 - 0.00478 * Math.sin(omega * RAD);
  const eps0 = 23 + (26 + (21.448 - T * (46.815 + T * (0.00059 - T * 0.001813))) / 60) / 60;
  const eps = eps0 + 0.00256 * Math.cos(omega * RAD);
  const decl = Math.asin(Math.sin(eps * RAD) * Math.sin(lambda * RAD));
  const y = Math.tan((eps * RAD) / 2) ** 2;
  const L0r = L0 * RAD;
  const eqTime =
    4 *
    DEG *
    (y * Math.sin(2 * L0r) -
      2 * e * Math.sin(Mr) +
      4 * e * y * Math.sin(Mr) * Math.cos(2 * L0r) -
      0.5 * y * y * Math.sin(4 * L0r) -
      1.25 * e * e * Math.sin(2 * Mr));
  const utcMin = date.getUTCHours() * 60 + date.getUTCMinutes() + date.getUTCSeconds() / 60 + date.getUTCMilliseconds() / 60000;
  const solarTime = mod(utcMin + eqTime + 4 * lonDeg, 1440);
  let ha = solarTime / 4 - 180;
  if (ha < -180) ha += 360;
  const lat = latDeg * RAD;
  const cosZ = Math.sin(lat) * Math.sin(decl) + Math.cos(lat) * Math.cos(decl) * Math.cos(ha * RAD);
  const zen = Math.acos(Math.max(-1, Math.min(1, cosZ)));
  const sinZ = Math.sin(zen);
  let az: number;
  if (sinZ < 1e-9) az = 180;
  else {
    const c = (Math.sin(lat) * cosZ - Math.sin(decl)) / (Math.cos(lat) * sinZ);
    const a = Math.acos(Math.max(-1, Math.min(1, c))) * DEG;
    az = ha > 0 ? mod(a + 180, 360) : mod(540 - a, 360);
  }
  return { azimuthDeg: az, elevationDeg: 90 - zen * DEG, declinationDeg: decl * DEG };
}

/** Azimuth (degrees from north) where the sun sets that day at a place (-0.833 degree: disc and refraction). */
export function sunsetAzimuth(date: Date, latDeg: number): number {
  const decl = sunPosition(date, latDeg, 0).declinationDeg * RAD;
  const h = -0.833 * RAD;
  const lat = latDeg * RAD;
  const c = (Math.sin(decl) - Math.sin(h) * Math.sin(lat)) / (Math.cos(h) * Math.cos(lat));
  if (c >= 1) return 360; // never rises (polar night): north as a placeholder
  if (c <= -1) return 180; // never sets
  return 360 - Math.acos(c) * DEG; // evening: west of south
}

export interface SunLight {
  azimuthDeg: number;
  elevationDeg: number;
  /** True when the light is the artistic dusk light, not the real sun. */
  stylised: boolean;
  realElevationDeg: number;
  realAzimuthDeg: number;
  /** One line for the screen. */
  label: string;
}

/** Elevation above which the real sun is used; below it the dusk light replaces it. */
export const REAL_SUN_MIN_ELEVATION = 6;
/** Elevation of the artistic dusk light. */
export const DUSK_ELEVATION = 9;

export function sunLight(date: Date, latDeg: number, lonDeg: number): SunLight {
  const real = sunPosition(date, latDeg, lonDeg);
  if (real.elevationDeg >= REAL_SUN_MIN_ELEVATION) {
    return {
      azimuthDeg: real.azimuthDeg,
      elevationDeg: real.elevationDeg,
      stylised: false,
      realElevationDeg: real.elevationDeg,
      realAzimuthDeg: real.azimuthDeg,
      label: `sun ${Math.round(real.elevationDeg)}° above the horizon, azimuth ${Math.round(real.azimuthDeg)}°`,
    };
  }
  const az = sunsetAzimuth(date, latDeg);
  const below = real.elevationDeg < 0 ? `the sun is ${Math.round(-real.elevationDeg)}° below the horizon` : 'the sun is very low';
  return {
    azimuthDeg: az,
    elevationDeg: DUSK_ELEVATION,
    stylised: true,
    realElevationDeg: real.elevationDeg,
    realAzimuthDeg: real.azimuthDeg,
    label: `stylised dusk light (${below})`,
  };
}

/**
 * Unit vector towards the light in scene coordinates (x east, y north, z up) where heights are
 * stretched by `vexag`: the direction of true space stretched like the scene, so cast shadows are
 * consistent with the drawn cloud.
 */
export function sunVector(azimuthDeg: number, elevationDeg: number, vexag = 1): [number, number, number] {
  const az = azimuthDeg * RAD;
  const el = elevationDeg * RAD;
  const x = Math.sin(az) * Math.cos(el);
  const y = Math.cos(az) * Math.cos(el);
  const z = Math.sin(el) * vexag;
  const l = Math.hypot(x, y, z) || 1;
  return [x / l, y / l, z / l];
}
