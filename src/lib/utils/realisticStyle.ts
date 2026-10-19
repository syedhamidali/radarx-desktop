/**
 * The "Realistic (stylised)" storm look: reflectivity drawn like precipitation shafts instead of a
 * colour scale. It is an artistic rendering, not a physical simulation: the opacity and brightness
 * follow the reflectivity, the light comes from a sun direction (real or dusk, sunPosition.ts) with a
 * short secondary march for soft self-shadowing, an ambient sky term and a silver lining on
 * back-lit edges. The scientific colour scale stays the default.
 *
 *  - light echo (below about 25 dBZ): thin, pale, translucent haze
 *  - moderate rain (25 to 40 dBZ): grey-white
 *  - heavy cores (45 dBZ and above): dense, dark grey
 *  - the strongest cores (60 dBZ and above): a subtle warm tint
 */
import { sunVector, type SunLight } from './sunPosition.ts';

/** Colour and opacity of one reflectivity (dBZ): albedo rgb 0..1 and extinction per km (scene units). */
export function realisticSample(dbz: number): { rgb: [number, number, number]; sigma: number } {
  const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
  const mix3 = (a: number[], b: number[], t: number): [number, number, number] => [lerp(a[0], b[0], t), lerp(a[1], b[1], t), lerp(a[2], b[2], t)];
  const sm = (a: number, b: number, x: number) => {
    const t = Math.max(0, Math.min(1, (x - a) / (b - a)));
    return t * t * (3 - 2 * t);
  };
  // extinction: a power of the reflectivity factor (Z^0.5), zero below about 4 dBZ
  const rho = Math.pow(10, (dbz - 55) / 14);
  const sigma = 3.6 * rho * sm(12, 28, dbz);
  const haze: number[] = [0.94, 0.96, 0.99];
  const rain: number[] = [0.82, 0.84, 0.87];
  const dense: number[] = [0.46, 0.47, 0.5];
  const core: number[] = [0.27, 0.275, 0.3];
  const warm: number[] = [0.4, 0.3, 0.26];
  let rgb = mix3(haze, rain, sm(18, 36, dbz));
  rgb = mix3(rgb, dense, sm(38, 52, dbz));
  rgb = mix3(rgb, core, sm(50, 58, dbz));
  rgb = mix3(rgb, warm, 0.6 * sm(59, 66, dbz));
  return { rgb, sigma };
}

/** The 256-entry transfer function (RGBA8) of the realistic look; `stepKm` is the renderer's user step. */
export function buildRealisticTf(vmin: number, vmax: number, stepKm: number): Uint8Array {
  const out = new Uint8Array(256 * 4);
  for (let i = 0; i < 256; i++) {
    const dbz = vmin + (i / 255) * (vmax - vmin);
    const { rgb, sigma } = realisticSample(dbz);
    const a = 1 - Math.exp(-sigma * stepKm);
    out[i * 4] = Math.round(rgb[0] * 255);
    out[i * 4 + 1] = Math.round(rgb[1] * 255);
    out[i * 4 + 2] = Math.round(rgb[2] * 255);
    out[i * 4 + 3] = Math.round(Math.min(1, a) * 255);
  }
  return out;
}

/** What the shader needs besides the sun direction. */
export interface RealisticLook {
  /** Light azimuth / elevation (degrees; true space). */
  sunAzimuthDeg: number;
  sunElevationDeg: number;
  sunCol: [number, number, number];
  skyCol: [number, number, number];
  horizonCol: [number, number, number];
  zenithCol: [number, number, number];
  /** Secondary-march steps toward the sun (0 = no shadows). */
  sunSteps: number;
  /** Haze: 1 - exp(-d * fog) over a distance d (km). */
  fog: number;
  exposure: number;
}

/** Light colours for a sun (real or dusk). */
export function lookFor(sun: SunLight, sunSteps = 5): RealisticLook {
  const e = sun.elevationDeg;
  if (sun.stylised) {
    // dusk: warm low sun, blue-violet sky, orange glow at the horizon
    return {
      sunAzimuthDeg: sun.azimuthDeg, sunElevationDeg: e,
      sunCol: [1.7, 1.02, 0.55], skyCol: [0.4, 0.47, 0.68],
      horizonCol: [0.8, 0.58, 0.52], zenithCol: [0.1, 0.16, 0.36],
      sunSteps, fog: 0.0022, exposure: 1.2,
    };
  }
  const t = Math.max(0, Math.min(1, (e - 6) / 30)); // low sun is warmer
  const mix = (a: number, b: number) => a + (b - a) * t;
  return {
    sunAzimuthDeg: sun.azimuthDeg, sunElevationDeg: e,
    sunCol: [mix(1.25, 1.1), mix(0.78, 1.0), mix(0.46, 0.92)], skyCol: [0.5, 0.64, 0.92],
    horizonCol: [mix(0.93, 0.78), mix(0.62, 0.84), mix(0.46, 0.95)], zenithCol: [0.2, 0.36, 0.7],
    sunSteps, fog: 0.0032, exposure: 1.05,
  };
}

/** Unit light vector in the scene (heights stretched by `vexag`). */
export function lightVector(look: RealisticLook, vexag: number): [number, number, number] {
  return sunVector(look.sunAzimuthDeg, look.sunElevationDeg, vexag);
}
