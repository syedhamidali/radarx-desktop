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
import { hexToRgb, colourOr, whiteBalance, type LookConfig } from './lookConfig.ts';

export type Palette = 'white' | 'soft' | 'stormy' | 'cyclone';
export const PALETTES: Palette[] = ['white', 'soft', 'stormy', 'cyclone'];

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
  // a tropical cyclone: pale grey where thin, charcoal green-grey in the dense wall and cores
  cyclone: { haze: [0.82, 0.84, 0.84], rain: [0.62, 0.66, 0.65], dense: [0.34, 0.38, 0.38], core: [0.14, 0.17, 0.17], hail: [0.13, 0.18, 0.15] },
  // a heavier storm: grey-white body, dark cores
  stormy: { haze: [0.95, 0.96, 0.98], rain: [0.82, 0.85, 0.9], dense: [0.46, 0.5, 0.56], core: [0.17, 0.2, 0.24], hail: [0.17, 0.23, 0.2] },
};

/** Parts of the look that shape the colour and opacity of the echo (all optional). */
export interface SampleOptions {
  /** Colour overrides of the ramp (rgb 0..1) and the dBZ where each step starts. */
  low?: [number, number, number];
  mid?: [number, number, number];
  heavy?: [number, number, number];
  core?: [number, number, number];
  hail?: [number, number, number];
  lowDbz?: number;
  midDbz?: number;
  heavyDbz?: number;
  hailDbz?: number;
  /** Multiplies the extinction; echo below `cutoffDbz` is not drawn. */
  densityScale?: number;
  cutoffDbz?: number;
  /** Multiplies the cloud colour (whiteness). */
  albedo?: number;
}

/** Colour and opacity of one reflectivity (dBZ): albedo rgb 0..1 and extinction per km (scene units). */
export function realisticSample(dbz: number, palette: Palette = 'white', o: SampleOptions = {}): { rgb: [number, number, number]; sigma: number } {
  const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
  const mix3 = (a: number[], b: number[], t: number): [number, number, number] => [lerp(a[0], b[0], t), lerp(a[1], b[1], t), lerp(a[2], b[2], t)];
  const sm = (a: number, b: number, x: number) => {
    const t = Math.max(0, Math.min(1, (x - a) / (b - a)));
    return t * t * (3 - 2 * t);
  };
  const cut = o.cutoffDbz ?? 12;
  const dens = o.densityScale ?? 1;
  // extinction: a steep power of the reflectivity factor; faint echo is a thin haze, heavy echo is dense
  const rho = Math.pow(10, (dbz - 55) / 14);
  // (a cyclone's eyewall and rainbands are a dense, near-opaque wall from about 35 dBZ)
  const sigma = dens * (palette === 'cyclone' ? (7.0 * rho + 1.4 * sm(cut + 2, cut + 22, dbz)) * sm(cut - 2, cut + 8, dbz) : 3.6 * rho * sm(cut, cut + 14, dbz));
  const c = PALETTE_COLOURS[palette];
  const L = o.lowDbz ?? 16, M = o.midDbz ?? 38, H = o.heavyDbz ?? 52, K = o.hailDbz ?? 58;
  let rgb = mix3(o.low ?? c.haze, o.mid ?? c.rain, sm(L, L + 18, dbz));
  rgb = mix3(rgb, o.heavy ?? c.dense, sm(M - 2, M + 14, dbz));
  rgb = mix3(rgb, o.core ?? c.core, sm(H, H + 8, dbz));
  rgb = mix3(rgb, o.hail ?? c.hail, 0.75 * sm(K, K + 8, dbz)); // green-grey hint in the strongest cores only
  const al = o.albedo ?? 1;
  return { rgb: [Math.min(1.2, rgb[0] * al), Math.min(1.2, rgb[1] * al), Math.min(1.2, rgb[2] * al)], sigma };
}

/** The 256-entry transfer function (RGBA8) of the realistic look; `stepKm` is the renderer's user step. */
export function buildRealisticTf(vmin: number, vmax: number, stepKm: number, palette: Palette = 'white', o: SampleOptions = {}): Uint8Array {
  const out = new Uint8Array(256 * 4);
  for (let i = 0; i < 256; i++) {
    const dbz = vmin + (i / 255) * (vmax - vmin);
    const { rgb, sigma } = realisticSample(dbz, palette, o);
    const a = 1 - Math.exp(-sigma * stepKm);
    out[i * 4] = Math.round(Math.min(1, rgb[0]) * 255);
    out[i * 4 + 1] = Math.round(Math.min(1, rgb[1]) * 255);
    out[i * 4 + 2] = Math.round(Math.min(1, rgb[2]) * 255);
    out[i * 4 + 3] = Math.round(Math.min(1, a) * 255);
  }
  return out;
}

/** The options of the echo colours and opacity from a look config. */
export function sampleOptions(cfg: LookConfig): SampleOptions {
  const col = (k: string): [number, number, number] | undefined => {
    const v = cfg[k];
    return typeof v === 'string' && v !== '' ? hexToRgb(v) : undefined;
  };
  return {
    low: col('rampLow'), mid: col('rampMid'), heavy: col('rampHeavy'), core: col('rampCore'), hail: col('rampHail'),
    lowDbz: cfg.rampLowDbz as number, midDbz: cfg.rampMidDbz as number, heavyDbz: cfg.rampHeavyDbz as number, hailDbz: cfg.rampHailDbz as number,
    densityScale: cfg.densityScale as number, cutoffDbz: cfg.lowDbzCutoff as number, albedo: cfg.cloudAlbedo as number,
  };
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
  // ---- the look controls (defaults give the plain scene) ----
  /** Haze colour. */
  hazeCol: [number, number, number];
  shadowStrength: number;
  shadowSoft: number;
  rim: number;
  /** Final stage: brightness, contrast, saturation, gamma, white balance multipliers, bloom, vignette, grain. */
  brightness: number;
  contrast: number;
  saturation: number;
  gamma: number;
  wb: [number, number, number];
  bloom: number;
  vignette: number;
  grain: number;
  terrainGain: number;
  terrainSat: number;
  oceanCol: [number, number, number];
  whitecap: number;
  rainStreak: number;
  rainCol: [number, number, number];
  /** Brightness of lightning and its colour. */
  flashGain: number;
  flashCol: [number, number, number];
  /** Vertical field of view in degrees; 0 = the camera's own. */
  fovDeg: number;
}

/** The look controls at their neutral values. */
const NEUTRAL = {
  shadowStrength: 1, shadowSoft: 0.5, rim: 1, brightness: 0, contrast: 1, saturation: 1, gamma: 1, wb: [1, 1, 1] as [number, number, number],
  bloom: 0.15, vignette: 0.2, grain: 0.02, terrainGain: 1, terrainSat: 1, oceanCol: [0.05, 0.085, 0.08] as [number, number, number],
  whitecap: 1, rainStreak: 0.25, rainCol: [0.7, 0.74, 0.8] as [number, number, number], flashGain: 1, flashCol: [0.75, 0.82, 1] as [number, number, number], fovDeg: 0,
};

/** The scene's light with the user's look on top: every override that is set replaces or scales the scene's value. */
export function lookWith(look: RealisticLook, cfg: LookConfig): RealisticLook {
  const num = (k: string) => cfg[k] as number;
  const out: RealisticLook = { ...look };
  out.exposure = look.exposure * num('exposure');
  if (num('sunElevation') >= 0) out.sunElevationDeg = num('sunElevation');
  if (num('sunAzimuth') >= 0) out.sunAzimuthDeg = num('sunAzimuth');
  out.sunCol = colourOr(cfg, 'sunColor', look.sunCol);
  out.sunGain = look.sunGain * num('sunIntensity');
  out.skyGain = look.skyGain * num('ambient');
  out.zenithCol = colourOr(cfg, 'skyZenith', look.zenithCol);
  out.horizonCol = colourOr(cfg, 'skyHorizon', look.horizonCol);
  out.glowCol = colourOr(cfg, 'glowColor', look.glowCol).map((v) => v * num('sunGlow')) as [number, number, number];
  out.hazeCol = colourOr(cfg, 'hazeColor', out.horizonCol);
  if (num('hazeDistanceKm') > 0) out.fog = 1 / num('hazeDistanceKm');
  out.shadowStrength = num('shadowStrength');
  out.shadowSoft = num('shadowSoftness');
  out.rim = num('rim');
  out.detail = look.detail * num('detail');
  out.brightness = num('brightness');
  out.contrast = num('contrast');
  out.saturation = num('saturation');
  out.gamma = num('gamma');
  out.wb = whiteBalance(num('temperature'), num('tint'));
  out.bloom = num('bloom');
  out.vignette = num('vignette');
  out.grain = num('grain');
  out.terrainGain = num('terrainBrightness');
  out.terrainSat = num('terrainSaturation');
  out.oceanCol = colourOr(cfg, 'oceanColor', NEUTRAL.oceanCol);
  out.whitecap = num('whitecaps');
  out.rainStreak = num('rainStreaks');
  out.rainCol = colourOr(cfg, 'rainColor', NEUTRAL.rainCol);
  out.flashGain = num('lightningBrightness');
  out.flashCol = colourOr(cfg, 'lightningColor', NEUTRAL.flashCol);
  out.fovDeg = num('fov');
  return out;
}

/** The light of the realistic look: real or assumed daylight, dusk, thick overcast, or night (flash-lit). */
export type LightKind = 'daylight' | 'dusk' | 'overcast' | 'night';

/** Light colours for a sun (real or dusk). `kind` overrides it with an overcast or a night look. */
export function lookFor(sun: SunLight, sunSteps = 5, fogScale = 1, kind?: LightKind): RealisticLook {
  let look = lookFor0(sun, sunSteps);
  if (kind === 'overcast') {
    // a low sun filtered by thick cloud: dim, desaturated green-grey, dark haze at the horizon
    look = {
      ...look, sunCol: [0.85, 0.9, 0.88], sunGain: 0.32, skyCol: [0.4, 0.46, 0.46], skyGain: 0.9,
      horizonCol: [0.3, 0.35, 0.35], zenithCol: [0.15, 0.18, 0.19], glowCol: [0.2, 0.2, 0.18],
      bounceCol: [0.1, 0.14, 0.12], fog: 0.0045, exposure: 1.15,
    };
  } else if (kind === 'night') {
    // night: no sun, a faint ambient, the storm is lit by lightning
    look = {
      ...look, sunCol: [0.4, 0.45, 0.6], sunGain: 0, skyCol: [0.22, 0.27, 0.36], skyGain: 0.14,
      horizonCol: [0.05, 0.07, 0.1], zenithCol: [0.012, 0.018, 0.035], glowCol: [0, 0, 0],
      bounceCol: [0.02, 0.03, 0.03], fog: 0.0035, exposure: 1.8,
    };
  }
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
      sunSteps, fog: 0.0022, exposure: 1.25, detail: 1, hazeCol: [0.6, 0.66, 0.74], ...NEUTRAL,
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
    sunSteps, fog: 0.0028, exposure: 1.0, detail: 1, hazeCol: [0.72, 0.83, 0.96], ...NEUTRAL,
  };
}

/** Unit light vector in the scene (heights stretched by `vexag`). */
export function lightVector(look: RealisticLook, vexag: number): [number, number, number] {
  return sunVector(look.sunAzimuthDeg, look.sunElevationDeg, vexag);
}
