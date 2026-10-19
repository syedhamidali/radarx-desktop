/**
 * The "Look" of the realistic storm: every control, its range, the presets, and reading / writing
 * configuration files (JSON, or a small TOML subset) with the same key names in the viewer, in
 * the storm-clip CLI and in saved presets.
 *
 * The look changes how the radar echo is drawn, never the data: where echo exists and how strong it is
 * comes from the radar volume in every setting (the detail noise only reshapes brightness and thins
 * faint echo). The scientific dBZ colours are the preset "Scientific (dBZ)" and stay the default of
 * the viewer. Pure functions (no DOM, no GL); unit-tested in lookConfig.test.mjs.
 */

export type LookValue = number | string;
export type LookConfig = Record<string, LookValue>;

export interface Control {
  key: string;
  label: string;
  group: string;
  kind: 'number' | 'color' | 'choice';
  min?: number;
  max?: number;
  step?: number;
  /** Default. A colour or a number of -1 / '' (auto) means "chosen by the light and palette". */
  def: LookValue;
  choices?: string[];
  unit?: string;
  help: string;
}

const n = (key: string, label: string, group: string, min: number, max: number, step: number, def: number, help: string, unit?: string): Control => ({ key, label, group, kind: 'number', min, max, step, def, help, unit });
const c = (key: string, label: string, group: string, help: string, def = ''): Control => ({ key, label, group, kind: 'color', def, help });
const ch = (key: string, label: string, group: string, choices: string[], def: string, help: string): Control => ({ key, label, group, kind: 'choice', choices, def, help });

export const GROUPS = ['Scene', 'Exposure and colour', 'Cloud', 'Rain and eyewall colours', 'Light', 'Sky and haze', 'Ground and ocean', 'Lightning', 'Finish', 'Camera'] as const;

export const CONTROLS: Control[] = [
  ch('style', 'Style', 'Scene', ['realistic', 'scientific'], 'realistic', 'realistic: the stylised storm look. scientific: the dBZ colour scale (the data colours, unchanged).'),
  ch('light', 'Light', 'Scene', ['daylight', 'dusk', 'overcast', 'night'], 'daylight', 'daylight (the real sun when it is up, otherwise an assumed daytime sun, labelled), dusk, thick overcast, or night (lit by lightning).'),
  ch('palette', 'Cloud palette', 'Scene', ['white', 'soft', 'stormy', 'cyclone'], 'white', 'Colours and density of the echo as cloud: white cumulonimbus, soft, stormy grey, or a dense hurricane wall.'),

  n('exposure', 'Exposure', 'Exposure and colour', 0.3, 3, 0.01, 1.25, 'Overall light level (multiplies the light of the chosen scene).'),
  n('brightness', 'Brightness', 'Exposure and colour', -0.4, 0.4, 0.005, 0, 'Added to the final colour: lifts or lowers everything, shadows included.'),
  n('contrast', 'Contrast', 'Exposure and colour', 0.5, 1.8, 0.01, 1, 'Spreads the tones around mid grey.'),
  n('saturation', 'Saturation', 'Exposure and colour', 0, 2, 0.01, 1, '0 is grey, 1 as lit, 2 vivid.'),
  n('gamma', 'Gamma', 'Exposure and colour', 0.6, 1.8, 0.01, 1, 'Mid-tone brightness (above 1 lifts the mid tones and shadows).'),
  n('temperature', 'White balance (cool - warm)', 'Exposure and colour', -1, 1, 0.01, 0, 'Negative cools (bluer), positive warms (yellower).'),
  n('tint', 'Tint (green - magenta)', 'Exposure and colour', -1, 1, 0.01, 0, 'Negative greener, positive more magenta.'),

  n('cloudAlbedo', 'Cloud whiteness', 'Cloud', 0.5, 1.3, 0.01, 1, 'Reflectivity of the cloud colour (above 1 brightens the body, below greys it).'),
  n('densityScale', 'Cloud density', 'Cloud', 0.2, 4, 0.01, 1, 'Opacity of the echo as cloud (multiplies its extinction).'),
  n('lowDbzCutoff', 'Faint-echo cutoff', 'Cloud', 0, 40, 0.5, 12, 'Echo weaker than this is not drawn; just above it the echo shows as thin haze.', 'dBZ'),
  n('detail', 'Fine detail', 'Cloud', 0, 1.5, 0.01, 1, 'Stylised fractal detail: brightness of billows and thinning of faint echo. Never adds echo or moves cores.'),
  n('rainStreaks', 'Rain streaks', 'Cloud', 0, 1, 0.01, 0.25, 'Vertical translucent streaks in the rain below the cloud base.'),

  n('rampLowDbz', 'Haze to rain at', 'Rain and eyewall colours', 5, 40, 0.5, 16, 'Reflectivity where the pale haze colour ends.', 'dBZ'),
  n('rampMidDbz', 'Rain to heavy at', 'Rain and eyewall colours', 20, 55, 0.5, 38, 'Reflectivity where the heavy-rain colour starts.', 'dBZ'),
  n('rampHeavyDbz', 'Heavy to core at', 'Rain and eyewall colours', 35, 65, 0.5, 52, 'Reflectivity where the core colour starts.', 'dBZ'),
  n('rampHailDbz', 'Hail hint from', 'Rain and eyewall colours', 50, 75, 0.5, 58, 'Reflectivity above which the hail tint blends in.', 'dBZ'),
  c('rampLow', 'Colour: thin / low', 'Rain and eyewall colours', 'Colour of faint echo (empty = the palette).'),
  c('rampMid', 'Colour: moderate', 'Rain and eyewall colours', 'Colour of moderate rain.'),
  c('rampHeavy', 'Colour: heavy', 'Rain and eyewall colours', 'Colour of heavy rain and the eyewall.'),
  c('rampCore', 'Colour: core', 'Rain and eyewall colours', 'Colour of the strongest cores.'),
  c('rampHail', 'Colour: hail hint', 'Rain and eyewall colours', 'Tint of the very strongest cores.'),
  c('rainColor', 'Rain streak colour', 'Rain and eyewall colours', 'Colour of the rain streaks (empty = grey-blue).'),

  n('sunElevation', 'Sun elevation', 'Light', -1, 80, 0.5, -1, '-1 = chosen by the light. Degrees above the horizon.', '°'),
  n('sunAzimuth', 'Sun azimuth', 'Light', -1, 360, 1, -1, '-1 = chosen by the light. Degrees clockwise from north.', '°'),
  c('sunColor', 'Sun colour', 'Light', 'Colour of the direct light (empty = the light kind).'),
  n('sunIntensity', 'Sun intensity', 'Light', 0, 3, 0.01, 1, 'Strength of the direct light on cloud and ground.'),
  n('ambient', 'Ambient (sky fill)', 'Light', 0, 2.5, 0.01, 1, 'Light from the sky: lifts the shadows and the cloud base.'),
  n('shadowStrength', 'Shadow strength', 'Light', 0, 1.5, 0.01, 1, 'How dark the cloud shades itself and the ground (0 = none).'),
  n('shadowSoftness', 'Shadow softness', 'Light', 0, 1, 0.01, 0.5, 'Higher keeps more light inside dense cloud: softer, lighter shadows.'),
  n('rim', 'Rim / silver lining', 'Light', 0, 2.5, 0.01, 1, 'Brightening of cloud edges lit from behind.'),

  c('skyZenith', 'Sky: overhead', 'Sky and haze', 'Sky colour overhead (empty = the light kind).'),
  c('skyHorizon', 'Sky: horizon', 'Sky and haze', 'Sky colour at the horizon.'),
  n('sunGlow', 'Sun glow', 'Sky and haze', 0, 2, 0.01, 1, 'Warm glow of the sky toward the sun.'),
  c('glowColor', 'Glow colour', 'Sky and haze', 'Colour of that glow.'),
  c('hazeColor', 'Haze colour', 'Sky and haze', 'Colour distant cloud and ground fade into (empty = the sky at the horizon).'),
  n('hazeDistanceKm', 'Haze distance', 'Sky and haze', 0, 2000, 5, 0, 'Distance at which haze reaches 63% (0 = chosen by the scene size; smaller = hazier).', 'km'),

  n('terrainBrightness', 'Terrain brightness', 'Ground and ocean', 0.3, 2.2, 0.01, 1, 'Brightness of the satellite imagery.'),
  n('terrainSaturation', 'Terrain saturation', 'Ground and ocean', 0, 2, 0.01, 1, 'Colour of the satellite imagery.'),
  c('oceanColor', 'Ocean colour', 'Ground and ocean', 'Colour of open water (empty = dark grey-green).'),
  n('whitecaps', 'Whitecaps and spray', 'Ground and ocean', 0, 2, 0.01, 1, 'Foam and spray streaks; they follow the retrieved surface wind (none where no wind was retrieved).'),

  n('lightningBrightness', 'Lightning brightness', 'Lightning', 0, 3, 0.01, 1, 'Brightness of the channels and of the light they throw.'),
  c('lightningColor', 'Lightning colour', 'Lightning', 'Colour of the flash (empty = blue-white).'),

  n('bloom', 'Bloom', 'Finish', 0, 1, 0.01, 0.15, 'Glow of very bright areas.'),
  n('vignette', 'Vignette', 'Finish', 0, 1, 0.01, 0.2, 'Darkening toward the frame corners.'),
  n('grain', 'Film grain', 'Finish', 0, 0.15, 0.002, 0.02, 'Fine noise (changes every frame).'),

  n('fov', 'Field of view', 'Camera', 0, 100, 1, 0, 'Vertical field of view (0 = the camera\'s own: 45° orbit, 60° window and flight; useful values 30 to 100).', '°'),
  n('shake', 'Camera shake', 'Camera', 0, 3, 0.05, 1, 'Strength of the shake of the flight camera in heavy rain (0 = none).'),
];

const BY_KEY = new Map(CONTROLS.map((q) => [q.key, q]));

export class LookError extends Error {}

export function defaultLook(): LookConfig {
  const out: LookConfig = {};
  for (const q of CONTROLS) out[q.key] = q.def;
  return out;
}

function distance(a: string, b: string): number {
  const d: number[][] = Array.from({ length: a.length + 1 }, (_, i) => [i, ...new Array(b.length).fill(0)]);
  for (let j = 0; j <= b.length; j++) d[0][j] = j;
  for (let i = 1; i <= a.length; i++) {
    for (let j = 1; j <= b.length; j++) d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
  }
  return d[a.length][b.length];
}

/** The known key nearest to `key` (case-insensitive), or null if none is close. */
export function suggestKey(key: string): string | null {
  let best: string | null = null, bd = Infinity;
  for (const q of CONTROLS) {
    const dd = distance(key.toLowerCase(), q.key.toLowerCase());
    if (dd < bd) { bd = dd; best = q.key; }
  }
  return best !== null && bd <= Math.max(2, Math.floor(key.length / 3)) ? best : null;
}

const HEX = /^#[0-9a-fA-F]{6}$/;

/**
 * Validate a plain object of settings (any subset of the keys) and return it on top of the defaults.
 * Unknown keys, wrong types, out-of-range numbers, bad choices and bad colours throw a LookError that
 * names the key (and the nearest known key for a typo).
 */
export function validateLook(obj: Record<string, unknown>, base: LookConfig = defaultLook()): LookConfig {
  const out: LookConfig = { ...base };
  for (const [key, raw] of Object.entries(obj)) {
    const q = BY_KEY.get(key);
    if (!q) {
      const sug = suggestKey(key);
      throw new LookError(`unknown look key "${key}"${sug ? ` (did you mean "${sug}"?)` : ''}; see docs/REALISTIC_3D.md for the list`);
    }
    if (q.kind === 'number') {
      if (typeof raw !== 'number' || !Number.isFinite(raw)) throw new LookError(`look key "${key}" must be a number (got ${JSON.stringify(raw)})`);
      const lo = q.min!, hi = q.max!;
      if (raw < lo - 1e-9 || raw > hi + 1e-9) throw new LookError(`look key "${key}" = ${raw} is out of range ${lo} to ${hi}${q.unit ? ' ' + q.unit : ''}`);
      out[key] = raw;
    } else if (q.kind === 'color') {
      if (typeof raw !== 'string' || !(raw === '' || HEX.test(raw))) throw new LookError(`look key "${key}" must be a colour like "#a0b0c0" or "" for automatic (got ${JSON.stringify(raw)})`);
      out[key] = raw.toLowerCase();
    } else {
      if (typeof raw !== 'string' || !q.choices!.includes(raw)) throw new LookError(`look key "${key}" must be one of ${q.choices!.join(', ')} (got ${JSON.stringify(raw)})`);
      out[key] = raw;
    }
  }
  return out;
}

/** Parse JSON, or a small TOML subset (`key = value`, `# comments`, `[section]` headings that are only for reading). */
export function parseLookText(text: string, format: 'json' | 'toml' = 'json'): Record<string, unknown> {
  if (format === 'json') {
    let v: unknown;
    try { v = JSON.parse(text); } catch (e) { throw new LookError(`look file is not valid JSON: ${(e as Error).message}`); }
    if (!v || typeof v !== 'object' || Array.isArray(v)) throw new LookError('look file must be a JSON object of settings');
    const o = v as Record<string, unknown>;
    // a saved preset has { name, look: {...} }
    if (o.look && typeof o.look === 'object') return o.look as Record<string, unknown>;
    return o;
  }
  const out: Record<string, unknown> = {};
  text.split(/\r?\n/).forEach((line0, i) => {
    const line = line0.replace(/\s+#.*$/, '').trim();
    if (!line || line.startsWith('#') || /^\[.*\]$/.test(line)) return;
    const m = /^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$/.exec(line);
    if (!m) throw new LookError(`look file line ${i + 1}: cannot read "${line0.trim()}" (expected key = value)`);
    const v = m[2].trim();
    let val: unknown;
    if (/^"[^"]*"$|^'[^']*'$/.test(v)) val = v.slice(1, -1);
    else if (v === 'true' || v === 'false') val = v === 'true';
    else if (/^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(v)) val = Number(v);
    else throw new LookError(`look file line ${i + 1}: value of "${m[1]}" must be a number or a quoted string (got ${v})`);
    out[m[1]] = val;
  });
  return out;
}

/** Parse and validate a look file's text on top of `base`. */
export function loadLookText(text: string, format: 'json' | 'toml' = 'json', base: LookConfig = defaultLook()): LookConfig {
  return validateLook(parseLookText(text, format), base);
}

/** The settings that differ from the defaults (a short, shareable preset). */
export function changedLook(cfg: LookConfig, base: LookConfig = defaultLook()): Record<string, LookValue> {
  const out: Record<string, LookValue> = {};
  for (const q of CONTROLS) if (cfg[q.key] !== base[q.key]) out[q.key] = cfg[q.key];
  return out;
}

export function lookToJson(cfg: LookConfig, name?: string): string {
  return JSON.stringify(name ? { name, look: changedLook(cfg) } : changedLook(cfg), null, 2) + '\n';
}

export function lookToToml(cfg: LookConfig): string {
  const lines = ['# radarx-desktop look: every key is optional; the others keep their defaults'];
  let group = '';
  const ch2 = changedLook(cfg);
  for (const q of CONTROLS) {
    if (!(q.key in ch2)) continue;
    if (q.group !== group) { group = q.group; lines.push('', `[${group.toLowerCase().replace(/[^a-z]+/g, '_')}]`); }
    const v = ch2[q.key];
    lines.push(`${q.key} = ${typeof v === 'number' ? v : JSON.stringify(v)}`);
  }
  return lines.join('\n') + '\n';
}

// ─── Presets ────────────────────────────────────────────────────────────────

export interface Preset {
  name: string;
  about: string;
  look: Partial<LookConfig>;
}

export const PRESETS: Preset[] = [
  { name: 'Daylight cumulonimbus', about: 'Bright white towering cloud, shadowed base, saturated blue sky (an assumed daytime sun when the real one was down).', look: { style: 'realistic', light: 'daylight', palette: 'white', exposure: 1.25 } },
  { name: 'Golden hour', about: 'A low warm sun, long shadows, warm haze toward the sun.', look: { style: 'realistic', light: 'daylight', palette: 'white', exposure: 1.2, sunElevation: 9, sunColor: '#ffb870', temperature: 0.28, sunGlow: 1.5, glowColor: '#ff9a4a', shadowStrength: 1.15, saturation: 1.12 } },
  { name: 'Dusk', about: 'Low warm light, blue-grey sky, orange glow only toward the light.', look: { style: 'realistic', light: 'dusk', palette: 'white', exposure: 1.3, ambient: 1.2 } },
  { name: 'Stormy overcast', about: 'Heavy grey-green storm under thick cloud; readable, not black.', look: { style: 'realistic', light: 'overcast', palette: 'stormy', exposure: 1.55, ambient: 1.35, shadowSoftness: 0.65, brightness: 0.03, contrast: 1.05, saturation: 0.9, rainStreaks: 0.35 } },
  { name: 'Night + lightning', about: 'No sun; a faint ambient light; the storm is lit by lightning.', look: { style: 'realistic', light: 'night', palette: 'stormy', exposure: 2.0, lightningBrightness: 1.8, ambient: 1.4 } },
  { name: 'Hurricane (daylight)', about: 'A dense eyewall and rainbands in bright, hazy daylight; ocean with whitecaps.', look: { style: 'realistic', light: 'daylight', palette: 'cyclone', exposure: 1.3, ambient: 1.3, shadowSoftness: 0.6, detail: 1.1, rainStreaks: 0, whitecaps: 1.2, saturation: 0.95, sunElevation: 42 } },
  { name: 'Hurricane (stormy)', about: 'The same wall under thick overcast: dim, desaturated, but readable.', look: { style: 'realistic', light: 'overcast', palette: 'cyclone', exposure: 1.75, ambient: 1.45, shadowSoftness: 0.7, brightness: 0.04, detail: 1.1, rainStreaks: 0, whitecaps: 1.3, saturation: 0.88, vignette: 0.28 } },
  { name: 'Satellite white', about: 'Bright white cloud over the imagery, no dramatic shading: like a satellite picture.', look: { style: 'realistic', light: 'daylight', palette: 'white', exposure: 1.4, shadowStrength: 0.35, shadowSoftness: 0.9, rim: 0.4, rainStreaks: 0, detail: 0.7, vignette: 0, grain: 0, bloom: 0.08, ambient: 1.4, cloudAlbedo: 1.1 } },
  { name: 'Scientific (dBZ)', about: 'The data colour scale, unchanged (the default of the viewer).', look: { style: 'scientific' } },
];

export function presetNamed(name: string): Preset | undefined {
  return PRESETS.find((p) => p.name === name);
}

/** A preset's settings on top of the defaults. */
export function applyPreset(name: string, userPresets: Preset[] = []): LookConfig {
  const p = presetNamed(name) ?? userPresets.find((q) => q.name === name);
  if (!p) throw new LookError(`unknown look preset "${name}"; presets: ${[...PRESETS, ...userPresets].map((q) => q.name).join(', ')}`);
  return validateLook(p.look as Record<string, unknown>);
}

/** The name of the built-in or saved preset a config equals, or null (it is a custom look). */
export function matchingPreset(cfg: LookConfig, userPresets: Preset[] = []): string | null {
  for (const p of [...PRESETS, ...userPresets]) {
    try {
      const full = validateLook(p.look as Record<string, unknown>);
      if (CONTROLS.every((q) => full[q.key] === cfg[q.key])) return p.name;
    } catch { /* a bad saved preset never matches */ }
  }
  return null;
}

// ─── Saved presets (local storage) ──────────────────────────────────────────

export interface StorageLike {
  getItem(k: string): string | null;
  setItem(k: string, v: string): void;
}

const STORE_KEY = 'xradar.look.presets';

export function loadUserPresets(st: StorageLike): Preset[] {
  try {
    const arr = JSON.parse(st.getItem(STORE_KEY) ?? '[]');
    if (!Array.isArray(arr)) return [];
    const out: Preset[] = [];
    for (const p of arr) {
      if (!p || typeof p.name !== 'string' || typeof p.look !== 'object') continue;
      try { validateLook(p.look); out.push({ name: p.name, about: typeof p.about === 'string' ? p.about : 'saved look', look: p.look }); } catch { /* skip a preset that no longer validates */ }
    }
    return out;
  } catch {
    return [];
  }
}

export function saveUserPreset(st: StorageLike, name: string, cfg: LookConfig, about = 'saved look'): Preset[] {
  const nm = name.trim();
  if (!nm) throw new LookError('a saved look needs a name');
  if (presetNamed(nm)) throw new LookError(`"${nm}" is a built-in preset name; choose another`);
  const list = loadUserPresets(st).filter((p) => p.name !== nm);
  list.push({ name: nm, about, look: changedLook(cfg) });
  st.setItem(STORE_KEY, JSON.stringify(list));
  return list;
}

export function deleteUserPreset(st: StorageLike, name: string): Preset[] {
  const list = loadUserPresets(st).filter((p) => p.name !== name);
  st.setItem(STORE_KEY, JSON.stringify(list));
  return list;
}

// ─── Colours ────────────────────────────────────────────────────────────────

export function hexToRgb(hex: string): [number, number, number] {
  return [parseInt(hex.slice(1, 3), 16) / 255, parseInt(hex.slice(3, 5), 16) / 255, parseInt(hex.slice(5, 7), 16) / 255];
}

export function rgbToHex(rgb: number[]): string {
  const h = (v: number) => Math.round(Math.max(0, Math.min(1, v)) * 255).toString(16).padStart(2, '0');
  return `#${h(rgb[0])}${h(rgb[1])}${h(rgb[2])}`;
}

/** A colour setting as rgb, or `fallback` when it is automatic (empty). */
export function colourOr(cfg: LookConfig, key: string, fallback: [number, number, number]): [number, number, number] {
  const v = cfg[key];
  return typeof v === 'string' && HEX.test(v) ? hexToRgb(v) : fallback;
}

// ─── The finishing stage (mirrors the shader's `postProcess`) ───────────────

export interface PostParams {
  brightness: number;
  contrast: number;
  saturation: number;
  gamma: number;
  /** Multipliers of the white balance (temperature, tint). */
  wb: [number, number, number];
  bloom: number;
  vignette: number;
}

export function whiteBalance(temperature: number, tint: number): [number, number, number] {
  return [1 + 0.25 * temperature + 0.08 * tint, 1 - 0.15 * tint, 1 - 0.25 * temperature + 0.08 * tint];
}

export function postParams(cfg: LookConfig): PostParams {
  return {
    brightness: cfg.brightness as number, contrast: cfg.contrast as number, saturation: cfg.saturation as number, gamma: cfg.gamma as number,
    wb: whiteBalance(cfg.temperature as number, cfg.tint as number), bloom: cfg.bloom as number, vignette: cfg.vignette as number,
  };
}

/** The finishing stage on one colour (0..1 rgb) at screen position `uv` (0..1): the same maths as the shader (no grain). */
export function postProcess(rgb: [number, number, number], p: PostParams, uv: [number, number] = [0.5, 0.5]): [number, number, number] {
  let [r, g, b] = rgb.map((v, i) => v * p.wb[i]) as [number, number, number];
  // bloom: a soft lift of the very bright part
  const lum0 = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  const glow = Math.max(0, lum0 - 0.78) * p.bloom * 1.5;
  r += glow; g += glow; b += glow;
  r += p.brightness; g += p.brightness; b += p.brightness;
  r = (r - 0.5) * p.contrast + 0.5; g = (g - 0.5) * p.contrast + 0.5; b = (b - 0.5) * p.contrast + 0.5;
  const l = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  r = l + (r - l) * p.saturation; g = l + (g - l) * p.saturation; b = l + (b - l) * p.saturation;
  const cl = (v: number) => Math.max(0, Math.min(1, v));
  const ig = 1 / p.gamma;
  r = Math.pow(cl(r), ig); g = Math.pow(cl(g), ig); b = Math.pow(cl(b), ig);
  const dx = uv[0] - 0.5, dy = uv[1] - 0.5;
  const vg = 1 - p.vignette * Math.min(1, (dx * dx + dy * dy) * 2.2);
  return [r * vg, g * vg, b * vg];
}
