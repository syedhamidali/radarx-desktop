/**
 * The "Realistic (stylised)" storm look: reflectivity drawn like precipitation shafts instead of a
 * colour scale. It is an artistic rendering, not a physical simulation: the opacity and brightness
 * follow the reflectivity, the light comes from a sun direction (real or dusk, sunPosition.ts) with a
 * short secondary march for soft self-shadowing, an ambient sky term and a silver lining on
 * back-lit edges. The scientific colour scale stays the default.
 *
 * Palette (from how storms look in photographs, not from the colour scale): the upper, sunlit cloud
 * (high tops, low reflectivity) is white to pale warm grey, the mid levels are neutral grey, rain
 * shafts and heavy cores are dark slate grey, near black in the strongest, with a faint green-grey
 * tint only in the very strongest cores (a hail hint). No purple, violet or pink in the storm.
 * The sky is a neutral blue-grey gradient with warm colour only near the horizon at the light's
 * azimuth; distant ground and cloud fade into a bluish haze.
 */
import { sunVector, type SunLight } from './sunPosition.ts';

export type Palette = 'white' | 'soft' | 'stormy';
export const PALETTES: Palette[] = ['white', 'soft', 'stormy'];

interface PaletteColours {
  haze: number[];
  rain: number[];
  dense: number[];
  core: number[];
  hail: number[];
}

const PALETTE_COLOURS: Record<Palette, PaletteColours> = {
  // brilliant white cloud, slate rain cores
  white: { haze: [1.0, 1.0, 1.0], rain: [1.0, 1.0, 1.0], dense: [0.86, 0.88, 0.92], core: [0.34, 0.37, 0.42], hail: [0.3, 0.38, 0.34] },
  // a touch less white, cores a little lighter (more ambient fill)
  soft: { haze: [0.97, 0.98, 1.0], rain: [0.93, 0.95, 0.98], dense: [0.7, 0.74, 0.8], core: [0.34, 0.37, 0.42], hail: [0.3, 0.36, 0.33] },
  // a heavier storm: grey-white body, dark cores
  stormy: { haze: [0.95, 0.96, 0.98], rain: [0.82, 0.85, 0.9], dense: [0.46, 0.5, 0.56], core: [0.17, 0.2, 0.24], hail: [0.17, 0.23, 0.2] },
};

/** Colour and opacity of one reflectivity (dBZ): albedo rgb 0..1 and extinction per km (scene units). */
export function realisticSample(dbz: number, palette: Palette = 'white'): { rgb: [number, number, number]; sigma: number } {
  const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
  const mix3 = (a: number[], b: number[], t: number): [number, number, number] => [lerp(a[0], b[0], t), lerp(a[1], b[1], t), lerp(a[2], b[2], t)];
  const sm = (a: number, b: number, x: number) => {
    const t = Math.max(0, Math.min(1, (x - a) / (b - a)));
    return t * t * (3 - 2 * t);
  };
  // extinction: a steep power of the reflectivity factor; faint echo is a thin haze, heavy echo is dense
  const rho = Math.pow(10, (dbz - 55) / 14);
  const sigma = 3.6 * rho * sm(12, 26, dbz);
  const c = PALETTE_COLOURS[palette];
  let rgb = mix3(c.haze, c.rain, sm(16, 34, dbz));
  rgb = mix3(rgb, c.dense, sm(38, 52, dbz));
  rgb = mix3(rgb, c.core, sm(52, 60, dbz));
  rgb = mix3(rgb, c.hail, 0.75 * sm(58, 66, dbz)); // green-grey hint in the strongest cores only
  return { rgb, sigma };
}

/** The 256-entry transfer function (RGBA8) of the realistic look; `stepKm` is the renderer's user step. */
export function buildRealisticTf(vmin: number, vmax: number, stepKm: number, palette: Palette = 'white'): Uint8Array {
  const out = new Uint8Array(256 * 4);
  for (let i = 0; i < 256; i++) {
    const dbz = vmin + (i / 255) * (vmax - vmin);
    const { rgb, sigma } = realisticSample(dbz, palette);
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
  /** Sky at the horizon away from the light (neutral blue-grey) and overhead. */
  horizonCol: [number, number, number];
  zenithCol: [number, number, number];
  /** Warm colour added near the horizon in the light's direction only (the dusk or sunset glow). */
  glowCol: [number, number, number];
  /** Light bounced up from the ground onto the underside of the cloud. */
  bounceCol: [number, number, number];
  /** Strength of the sun and of the sky fill on the cloud. */
  sunGain: number;
  skyGain: number;
  /** Secondary-march steps toward the sun (0 = no shadows). */
  sunSteps: number;
  /** Haze: 1 - exp(-d * fog) over a distance d (km). */
  fog: number;
  exposure: number;
  /** Stylised detail (streaks and billows in the shading, never in where the echo is or how strong): 0..1. */
  detail: number;
}

/** Light colours for a sun (real or dusk). */
export function lookFor(sun: SunLight, sunSteps = 5, fogScale = 1): RealisticLook {
  const look = lookFor0(sun, sunSteps);
  return { ...look, fog: look.fog * fogScale };
}

function lookFor0(sun: SunLight, sunSteps: number): RealisticLook {
  const e = sun.elevationDeg;
  if (sun.stylised && !sun.daylightAssumed) {
    // dusk: a low, warm sun, a neutral blue-grey sky and an orange glow only toward the light
    return {
      sunAzimuthDeg: sun.azimuthDeg, sunElevationDeg: e,
      sunCol: [1.5, 1.1, 0.78], skyCol: [0.56, 0.6, 0.67],
      horizonCol: [0.6, 0.66, 0.74], zenithCol: [0.15, 0.21, 0.31], glowCol: [1.0, 0.6, 0.36],
      bounceCol: [0.3, 0.3, 0.28], sunGain: 1.0, skyGain: 1.0,
      sunSteps, fog: 0.0022, exposure: 1.25, detail: 1,
    };
  }
  // daylight (the real sun when it is up, or the assumed daylight): brilliant warm sunlight, a
  // saturated blue sky with a lighter horizon, a blue fill from the sky above, a greenish bounce from below
  const t = Math.max(0, Math.min(1, (e - 20) / 30));
  const mix = (a: number, b: number) => a + (b - a) * t;
  return {
    sunAzimuthDeg: sun.azimuthDeg, sunElevationDeg: e,
    sunCol: [mix(1.55, 1.4), mix(1.35, 1.3), mix(1.05, 1.15)], skyCol: [0.68, 0.76, 0.92],
    horizonCol: [0.72, 0.83, 0.96], zenithCol: [0.13, 0.34, 0.78], glowCol: [1.0, 0.82, 0.55],
    bounceCol: [0.28, 0.34, 0.2], sunGain: 1.0, skyGain: 1.0,
    sunSteps, fog: 0.0028, exposure: 1.0, detail: 1,
  };
}

/** Unit light vector in the scene (heights stretched by `vexag`). */
export function lightVector(look: RealisticLook, vexag: number): [number, number, number] {
  return sunVector(look.sunAzimuthDeg, look.sunElevationDeg, vexag);
}
