/**
 * Offline renderer of a storm clip, built on the app's own 3D viewer code (CartesianRenderer: the
 * same ray-marched volume, transfer function, lighting, map ground and overlay as the 3D tab).
 *
 * It is driven frame by frame by a script (tools/storm_clip/render_clip.py): every call of
 * `renderFrame(i)` draws ONE complete full-quality frame for the clip time i / fps (no real-time
 * clock anywhere), after which the script takes the pixels. The inputs are a data bundle written by
 * tools/storm_clip/clipdata.py: time steps of a merged 3D reflectivity mosaic (streamed one at a
 * time: only the two a frame needs are in memory), the tracked storm, the radar sites and, if the
 * bundle has them, retrieved winds and observed lightning.
 *
 *  - Time: the cloud is interpolated between the observed volumes along a flow (morphPass.ts): the
 *    tracked storm translation plus, where multi-Doppler winds exist, the observed wind anomaly, so
 *    the echo turns and shears as the measured air did. Without winds it is the translation alone.
 *    The clip says which one is used, and that it is interpolation, not new observations.
 *  - Cameras: 'orbit' (the cinematic orbit around the storm, partly following it) or 'observer' (a
 *    fixed camera on the ground, like one on a window sill, with true vertical scale).
 *  - Lightning: observed flashes of the bundle, or a modelled flash source driven by radar echo.
 *    The two are labelled differently on screen; modelled flashes are never presented as observed.
 *  - Sound: wind, rain and thunder synthesised from the data (soundscape.ts), rendered offline for
 *    the observer camera; labelled "not a recording".
 */

import { CartesianRenderer, type CamFrame } from './cartesianRenderer';
import { decodeMosaicVolume, gridBox, aeqdOffsetKm, type CartVolume, type SceneBox } from './cartesianVolume';
import { drawCartesianOverlay, drawOffBoxSites } from './cartesianOverlay';
import { project } from './scene3d';
import { COLORMAP_DATA } from './colormaps';
import { buildTf, thresholdSpec, thresholdToShader, adaptiveShiftDb } from './volumeTf';
import { resolveBasemap, IMAGERY_EOX_S2 } from './basemaps';
import { buildGroundMap } from './groundMap';
import { buildTerrain, TERRAIN_ATTRIBUTION, TERRARIUM_URL } from './terrain';
import { buildRealisticTf, lookFor, lookWith, sampleOptions, realisticSample, type Palette, type LightKind } from './realisticStyle';
import { defaultLook, applyPreset, validateLook, loadLookText, changedLook, type LookConfig } from './lookConfig';
import { sunLight, daylightAssumed, type SunLight } from './sunPosition';
import {
  cameraAt, stormAt, dataTimeAt, clipTimeOf, locate, intervalMotion, utcLabel, stormChoreography, cycloneChoreography, cycloneFlight, flightPose, cameraShake, trackFrame, lagTarget,
  type CamKey, type FlightKey,
} from './clipCamera';
import { placeObserver, observerPose, bearingDeg, compassLabel, horizontalFovDeg, OBSERVER_DEFAULTS, type ObserverSettings } from './observerCam';
import { LruLoader, summarise, dbzAt, dbzOfCode, type StepSummary } from './clipSteps';
import { decodeWindStep, stormRelativeField, type WindStep, type MorphField } from './flowMorph';
import { modelFlashes, makeFlash, mulberry32, type Flash, type FlashCandidate } from './lightning';
import { drawFlashes, flashLight } from './lightningFx';
import { drawCompassStrip, drawInset } from './clipHud';
import { renderSoundscape, AUDIO_LABEL, type SoundInput } from './soundscape';
import type { ClipExtras, ClipApi, FrameInfo } from '../perils/clipExtras';

export interface ClipManifest {
  version: number;
  title: string;
  case: string;
  variable: string;
  units: string;
  origin: { lat: number; lon: number };
  crop_half_km: number;
  steps: {
    index: number; time_utc: string; t_s: number; files: Record<string, { name: string; offset_s: number }>;
    mosaic: { header: string; bin: string };
    wind?: { header: string; bin: string } | null;
    /** The eye of a tropical cyclone as the echo-free hole (centre and radius from the radar echo), or null. */
    eye?: { x_km: number; y_km: number; radius_km: number } | null;
  }[];
  storm: { track_km: [number, number][]; track_lonlat: [number, number][]; motion_ms: [number, number]; speed_ms: number; heading_deg: number };
  sites: any[];
  /** Observed flashes (GLM / LMA) read by clipdata.py, positions in the bundle frame. */
  lightning?: { source: 'observed'; kind: string; file: string; flashes: { t_s: number; x: number; y: number; z?: number; energy?: number }[] } | null;
}

export interface ClipSettings {
  fps: number;
  duration: number;
  /** Vertical exaggeration of the scene (the observer camera uses 1: true scale). */
  vexag: number;
  /** Camera keyframes of the orbit camera; default: stormChoreography(). */
  keys?: CamKey[];
  /** Title and subtitle at the top left (default: the manifest's title). */
  title?: string;
  subtitle?: string;
  /** Ground map restyle: 'dark' (default) or 'light' tiles (brighter, labels easier to read). */
  mapTheme?: 'dark' | 'light';
  /**
   * How much of the storm's displacement the camera target follows (0 = fixed on the middle of the
   * track, 1 = locked on the storm). A partial follow lets the storm visibly travel across the map.
   */
  targetLag?: number;
  /** 'map': the flat map (default). 'realistic': terrain with satellite imagery. 'custom': the extras set the ground themselves (no tiles). */
  ground?: 'map' | 'realistic' | 'custom';
  /** A film built on this renderer (the PERiLS movie): its clock, camera, lightning and display replace the single-storm ones. */
  extras?: ClipExtras;
  /** 'scientific': the colour scale (default). 'realistic': the stylised precipitation look with sun light. */
  stormStyle?: 'scientific' | 'realistic';
  /**
   * The look (lookConfig.ts), lowest to highest priority: a preset name (`lookPreset`), the text of a
   * JSON / TOML look file (`lookText`, `lookFormat`), then explicit keys (`lookOverrides`, e.g. from CLI flags).
   */
  lookPreset?: string;
  lookText?: string;
  lookFormat?: 'json' | 'toml';
  lookOverrides?: Record<string, number | string>;
  /**
   * Where tiles come from: '' = the tile servers directly, or a base URL of a caching proxy that
   * serves /eox/{z}/{x}/{y} and /terrarium/{z}/{x}/{y} (render_clip.py does this, with a disk cache).
   */
  tileBase?: string;
  /** Orbit choreography: 'storm' (a single thunderstorm, default) or 'cyclone' (a wide system); viewKm = its size. */
  preset?: 'storm' | 'cyclone';
  viewKm?: number;
  /** 'orbit' (default), 'observer' (a camera on the ground at a fixed place) or 'flight' (the cyclone tour: in through the eyewall). */
  camera?: 'orbit' | 'observer' | 'flight' | 'path';
  /** Flight keys; default: cycloneFlight(). */
  flightKeys?: FlightKey[];
  /** Detail noise frequency per km: horizontal and vertical (a cyclone is layered: vertical higher). */
  cloudFreq?: [number, number];
  /** Ocean with whitecaps and spray from the retrieved surface wind (needs the realistic ground). */
  sea?: boolean;
  observer?: ObserverSettings;
  /** Carry the echo along the observed winds where the bundle has them (default true). */
  flow?: boolean;
  /** 'auto': observed flashes of the bundle if it has any, otherwise modelled; 'off'. */
  lightning?: 'auto' | 'off';
  lightningSeed?: number;
  /** Most flashes per clip second the model may draw at the strongest echo. */
  lightningRate?: number;
  /** Height (km ASL) of the -10 C level used by the modelled flash rate. */
  t10Km?: number;
  /** Shortens the light-to-sound delay of thunder by this factor in a time-lapse clip (1 = real physics). */
  soundCompress?: number;
  /** Light direction chosen for the shot (degrees); the default is the real sun, or the dusk light at the sunset azimuth. */
  sun?: { azimuthDeg: number; elevationDeg: number };
  /** Stylised detail of the realistic look (shading streaks and billows, never the echo), 0..1 (default 1). */
  cloudTexture?: number;
  /** Palette of the realistic look: 'white' (default), 'soft' or 'stormy'. */
  palette?: Palette;
  /** Light of the realistic look: 'daylight' (assumed: a stylised daytime sun, default) or 'dusk' (the real sun when it is up). */
  light?: LightKind;
  /** Show the sound labels (the audio itself is rendered by `renderAudio`). */
  audio?: boolean;
  /** Credit line at the bottom (default: NEXRAD Level II). `xrd animate` names the radars it was given. */
  radarCredit?: string;
  /** A frame counts as "observed" when its data time is within this many seconds of a real volume (default 6). */
  observedTolS?: number;
}

const DEFAULTS: ClipSettings = { fps: 30, duration: 36, vexag: 4, targetLag: 0.3, camera: 'orbit', flow: true, lightning: 'auto', lightningSeed: 7, lightningRate: 0.8, t10Km: 5.0, soundCompress: 8 };

/** Surface wind from the lowest retrieved level: the wind there is stronger than at the ground. */
export const SURFACE_WIND_FACTOR = 0.65;

export class StormClip {
  private renderer: CartesianRenderer | null = null;
  private ctx: CanvasRenderingContext2D;
  private manifest!: ClipManifest;
  private baseUrl = '';
  private g0!: CartVolume;
  private volumes!: LruLoader<CartVolume>;
  private windLoader!: LruLoader<WindStep | null>;
  private summaries: StepSummary[] = [];
  private ts: number[] = [];
  private track: { t: number; x: number; y: number }[] = [];
  private keys: CamKey[] = [];
  private pair = -1;
  private pairFlow = false;
  private crop: SceneBox | null = null;
  /** Middle of the storm track and half width of the (fixed) shown box, km. */
  private anchor: [number, number] = [0, 0];
  private cropHalf = 60;
  private lastCam: { eye: [number, number, number]; fwd: [number, number, number]; right: [number, number, number]; up: [number, number, number]; tanX: number; tanY: number; w: number; h: number } | null = null;
  private fullBox!: SceneBox;
  private sun: SunLight | null = null;
  private sunKey = '';
  private lookSteps = 7;
  terrainInfo = '';
  private terr: { size: number; h: Float32Array; half: number; cx: number; cy: number } | null = null;
  private groundImg: { size: number; data: Uint8ClampedArray; half: number; cx: number; cy: number } | null = null;
  private insetBase: HTMLCanvasElement | null = null;
  private insetView = { cx: 0, cy: 0, half: 30 };
  private tf: Uint8Array = new Uint8Array(1024);
  private tfShift = 0;
  settings: ClipSettings = { ...DEFAULTS };
  fatal = '';
  mapOk = false;
  /** The observer's place (km) and eye height above sea level (km, true). */
  private obs: [number, number] = [0, 0];
  private obsEyeKm = 0;
  /** Flashes of the clip and where they came from. */
  flashes: Flash[] = [];
  lightningSource: 'observed' | 'modelled' | 'off' = 'off';
  windSource: 'retrieved' | 'proxy' = 'proxy';
  private lastFlow = { active: false, nRadars: 0 };
  private flowOffset: [number, number] = [0, 0];
  /** State of the frame being drawn (for the banner). */
  private cur = { t: 0, td: 0, k: 0, s: 0 };
  private curPose = { yawDeg: 0, pitchDeg: 0 };
  flashesDrawn = 0;
  private lightKind: LightKind = 'daylight';
  private look: LookConfig = defaultLook();
  private look0: LookConfig = defaultLook();
  private flightKeys: FlightKey[] = [];
  private eyeR = 15;
  /** Retrieved wind of every step: maxima over the analysis box (m/s) and the share of cells with wind. */
  windStats: ({ maxAll: number; maxLow: number; p99All: number; p99Low: number; coverage: number; nRadars: number } | null)[] = [];
  private pairVols: [CartVolume, CartVolume] | null = null;
  private surfA: Float32Array | null = null;
  private surfB: Float32Array | null = null;
  private surfBuf: Float32Array | null = null;
  private povNow = { x: 0, y: 0, yawDeg: 0, altKm: 0, rKm: 0 };
  /** Vertical exaggeration of the frame being drawn (a film may change it from shot to shot). */
  private ve = 4;
  private lookKey = '';

  constructor(private container: HTMLElement, private overlay: HTMLCanvasElement) {
    this.ctx = overlay.getContext('2d')!;
  }

  get frameCount(): number {
    return Math.round(this.settings.duration * this.settings.fps);
  }

  private get observer(): boolean {
    return this.settings.camera === 'observer';
  }

  /** A first-person camera: the observer on the ground or the flight. */
  private get pov(): boolean {
    return this.settings.camera === 'observer' || this.settings.camera === 'flight' || this.settings.camera === 'path';
  }

  /** The hooks a film (the PERiLS movie) gives the renderer, or null. */
  private get ex(): ClipExtras | null {
    return this.settings.extras ?? null;
  }

  /** What a film may use of the renderer. */
  private api(): ClipApi {
    return {
      renderer: this.renderer!, g0: this.g0, stepTimes: this.ts, baseUrl: this.baseUrl, width: this.overlay.width, height: this.overlay.height,
      terrainKmAt: (x, y) => this.terrainKmAt(x, y), realistic: this.realistic,
    };
  }

  private frameInfo(): FrameInfo {
    const eye = this.lastCam?.eye ?? ([0, 0, 0] as [number, number, number]);
    return { t: this.cur.t, td: this.cur.td, k: this.cur.k, s: this.cur.s, vexag: this.ve, eye, yawDeg: this.curPose.yawDeg, pitchDeg: this.curPose.pitchDeg };
  }

  /** Load the bundle served at `baseUrl`: the manifest, then every volume once (to summarise it) and the winds. */
  async load(baseUrl: string, settings: Partial<ClipSettings> = {}): Promise<{ steps: number; grid: number[]; title: string }> {
    this.settings = { ...DEFAULTS, ...settings };
    if (this.observer && settings.vexag === undefined) this.settings.vexag = 1;
    if (this.settings.camera === 'flight' && settings.vexag === undefined) this.settings.vexag = 1;
    this.ve = this.settings.vexag;
    this.look = this.resolveLook();
    this.look0 = this.look;
    this.baseUrl = baseUrl;
    this.manifest = await (await this.get('manifest.json')).json();
    const m = this.manifest;
    this.volumes = new LruLoader<CartVolume>(3, (k) => this.fetchVolume(k));
    this.windLoader = new LruLoader<WindStep | null>(3, (k) => this.fetchWind(k));
    this.ts = m.steps.map((s) => s.t_s);
    this.track = m.storm.track_km.map((p, i) => ({ t: this.ts[i], x: p[0], y: p[1] }));
    this.keys = this.settings.keys ?? (this.settings.preset === 'cyclone'
      ? cycloneChoreography(this.settings.duration, this.settings.viewKm ?? 330)
      : stormChoreography(this.settings.duration, m.storm.heading_deg, 4));
    const fr = trackFrame(this.track, m.crop_half_km);
    this.anchor = fr.anchor;
    this.cropHalf = fr.cropHalf;
    this.obs = placeObserver(this.anchor, m.storm.heading_deg, this.settings.observer);

    // stream every step past once: keep only its summary (memory stays bounded for long series)
    this.summaries = [];
    for (let k = 0; k < m.steps.length; k++) {
      const v = await this.volumes.get(k);
      if (k === 0) this.g0 = v;
      else if (v.nx !== this.g0.nx || v.ny !== this.g0.ny || v.nz !== this.g0.nz || v.x0 !== this.g0.x0 || v.y0 !== this.g0.y0 || v.dx !== this.g0.dx) {
        throw new Error('the time steps are not on the same grid');
      }
      this.summaries.push(summarise(v, this.settings.t10Km ?? 5));
      const w = await this.windLoader.get(k);
      this.windStats.push(w ? this.windSummary(w) : null);
    }
    const radii = m.steps.map((st) => st.eye?.radius_km).filter((r): r is number => typeof r === 'number');
    if (radii.length) this.eyeR = radii.reduce((a, b) => a + b, 0) / radii.length;
    this.flightKeys = this.settings.flightKeys ?? cycloneFlight(this.settings.duration, this.eyeR, this.settings.viewKm ?? 330);
    this.fullBox = gridBox(this.g0);
    this.windSource = m.steps.some((s) => s.wind) ? 'retrieved' : 'proxy';
    return { steps: m.steps.length, grid: [this.g0.nx, this.g0.ny, this.g0.nz], title: m.title };
  }

  /**
   * Statistics of the retrieved wind speed (m/s): the 99th percentile over the whole analysis box and in
   * the lowest 3 km (robust: the maximum of a retrieval is often an outlier at the QC limit), the maximum,
   * and the share of cells with a wind.
   */
  private windSummary(w: WindStep): { maxAll: number; maxLow: number; p99All: number; p99Low: number; coverage: number; nRadars: number } {
    const all: number[] = [], low: number[] = [];
    const n = w.nx * w.ny;
    for (let k = 0; k < w.nz; k++) {
      const isLow = w.z0 + k * w.dz <= 3.0;
      for (let i = 0; i < n; i++) {
        const u = w.u[k * n + i], v = w.v[k * n + i];
        if (!Number.isFinite(u) || !Number.isFinite(v)) continue;
        const sp = Math.hypot(u, v);
        all.push(sp);
        if (isLow) low.push(sp);
      }
    }
    const pct = (a: number[], q: number) => {
      if (!a.length) return 0;
      a.sort((x, y) => x - y);
      return a[Math.min(a.length - 1, Math.floor(q * a.length))];
    };
    let maxAll = 0, maxLow = 0;
    for (const v of all) if (v > maxAll) maxAll = v;
    for (const v of low) if (v > maxLow) maxLow = v;
    return { maxAll, maxLow, p99All: pct(all, 0.99), p99Low: pct(low, 0.99), coverage: all.length / (n * w.nz), nRadars: w.nRadars ?? 0 };
  }

  /** The look: defaults, a preset, a look file, then explicit keys; errors name the key and a suggestion. */
  private resolveLook(): LookConfig {
    const sets = this.settings;
    let cfg = sets.lookPreset ? applyPreset(sets.lookPreset) : defaultLook();
    if (sets.lookText) cfg = loadLookText(sets.lookText, sets.lookFormat ?? 'json', cfg);
    const ov: Record<string, number | string> = { ...(sets.lookOverrides ?? {}) };
    if (sets.palette && !('palette' in ov)) ov.palette = sets.palette;
    if (sets.light && !('light' in ov)) ov.light = sets.light;
    if (Object.keys(ov).length) cfg = validateLook(ov, cfg);
    return cfg;
  }

  /**
   * Resolve the look of `settings` (preset, look file, explicit keys) without loading anything: returns
   * the keys that differ from the defaults, or throws the look error that names the key and the nearest
   * known one. `xrd animate` calls it first, so a typo fails before any gridding.
   */
  checkLook(settings: Partial<ClipSettings>): Record<string, unknown> {
    const saved = this.settings;
    try {
      this.settings = { ...DEFAULTS, ...settings };
      return changedLook(this.resolveLook());
    } finally {
      this.settings = saved;
    }
  }

  /** Change the look (relative to the one loaded: `overrides` replace the loaded values); for tests and previews. */
  setLook(overrides: Record<string, number | string>): void {
    this.look = validateLook(overrides, this.look0);
    this.sunKey = '';
    if (this.renderer && this.realistic) {
      const g0 = this.g0;
      this.renderer.setTransfer(buildRealisticTf(g0.vmin, g0.vmax, 0.35, this.look.palette as Palette, sampleOptions(this.look)));
    }
  }

  /** The realistic look is on when asked for and the look's style is not the scientific one. */
  private get realistic(): boolean {
    return this.settings.stormStyle === 'realistic' && this.look.style !== 'scientific';
  }

  private async get(u: string): Promise<Response> {
    const r = await fetch(`${this.baseUrl}/${u}`);
    if (!r.ok) throw new Error(`${u}: HTTP ${r.status}`);
    return r;
  }

  private async fetchVolume(k: number): Promise<CartVolume> {
    const st = this.manifest.steps[k];
    const h = await (await this.get(st.mosaic.header)).json();
    const bytes = new Uint8Array(await (await this.get(st.mosaic.bin)).arrayBuffer());
    const v = decodeMosaicVolume(h, bytes);
    if (!v) throw new Error(`step ${st.index}: the volume does not match its header`);
    return v;
  }

  private async fetchWind(k: number): Promise<WindStep | null> {
    const st = this.manifest.steps[k];
    if (!st.wind) return null;
    const h = await (await this.get(st.wind.header)).json();
    const bytes = new Uint8Array(await (await this.get(st.wind.bin)).arrayBuffer());
    return decodeWindStep(h, bytes);
  }

  /** Create the renderer, the transfer function and the ground map. */
  async prepare(mapProvider = 'osm'): Promise<void> {
    const m = this.manifest;
    const g0 = this.g0;
    const r = new CartesianRenderer(this.container, {
      onFatal: (msg) => { this.fatal = msg; },
      onFrame: (f) => this.drawOverlay(f),
    });
    this.renderer = r;
    r.enterExport();

    // the same default transfer function as the 3D tab (colours of the colormap, opacity curve of
    // reflectivity, slid for the strength of the scene)
    const hist = new Float64Array(256);
    for (const s of this.summaries) for (let i = 0; i < 256; i++) hist[i] += s.hist[i];
    let sum = 0;
    for (let i = 1; i < 256; i++) sum += hist[i];
    let p995 = g0.vmax;
    let acc = 0;
    for (let i = 1; i < 256; i++) {
      acc += hist[i];
      if (acc >= 0.995 * sum) { p995 = g0.vmin + ((i - 1) / 254) * (g0.vmax - g0.vmin); break; }
    }
    this.tfShift = adaptiveShiftDb(p995);
    const realisticStorm = this.realistic;
    this.tf = realisticStorm
      ? buildRealisticTf(g0.vmin, g0.vmax, 0.35, this.look.palette as Palette, sampleOptions(this.look))
      : buildTf('reflectivity', COLORMAP_DATA['NWSRef'], g0.vmin, g0.vmax, 'default', this.tfShift);
    r.setTransfer(this.tf);
    const spec = thresholdSpec('reflectivity', g0.vmin, g0.vmax, 'dBZ')!;
    const ts = thresholdToShader(spec, Math.max(spec.min, 5, 15 - this.tfShift), g0.vmin, g0.vmax);
    r.setParams({
      vexag: this.settings.vexag, stepKm: 0.35, opacity: realisticStorm ? 1.0 : 0.9,
      // the realistic look fades faint echo in its own opacity curve: no extra hide-below threshold
      thr: realisticStorm ? 0 : ts.thr, thrSoft: ts.soft, thrMode: ts.mode,
      groundMode: 'map', light: true, lightScene: false, showVolume: true, gridStepKm: 20,
    });
    r.setSceneBox(this.fullBox);
    await this.ensurePair(0);

    // ground map: a high resolution image centred on the storm track, big enough for the whole clip
    const xs = this.track.map((p) => p.x), ys = this.track.map((p) => p.y);
    const cx = (Math.min(...xs) + Math.max(...xs)) / 2, cy = (Math.min(...ys) + Math.max(...ys)) / 2;
    const half = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys)) / 2 + m.crop_half_km + 14;
    const [lat, lon] = this.latLonOf(cx, cy);
    if (this.settings.ground === 'custom') {
      // the film sets the ground itself (its extras.prepare); no tiles are asked for
      r.setMapGain(1.5);
    } else if (this.settings.ground === 'realistic') {
      await this.prepareRealisticGround(lat, lon, half + 35, [cx, cy]);
    } else {
      const bm = resolveBasemap(mapProvider, 'dark');
      const mt = this.settings.mapTheme ?? 'dark';
      r.setMapGain(mt === 'light' ? 0.82 : 1.7);
      let res = null;
      try {
        res = await buildGroundMap({ lat, lon, radiusKm: half, size: 2048, provider: bm.provider, theme: mt }, () => false);
      } catch (e) {
        console.warn('[clip] ground map failed:', e);
      }
      if (res) {
        r.setGround(res.size, res.data, half, [cx, cy]);
        this.groundImg = { size: res.size, data: res.data, half, cx, cy };
        this.mapOk = true;
      } else {
        r.setParams({ groundMode: 'grid' });
      }
    }
    // the observer stands on the terrain
    this.obsEyeKm = this.terrainKmAt(this.obs[0], this.obs[1]) + (this.settings.observer?.eyeM ?? OBSERVER_DEFAULTS.eyeM) / 1000;
    if (this.settings.sea) r.setSea(true, null);
    if (this.ex) {
      await this.ex.prepare(this.api());
      this.mapOk = this.renderer!.hasGroundMap;
    } else {
      this.prepareInset();
    }
    this.prepareLightning();
  }

  /** Satellite imagery draped on terrain (both cached); anything that fails is reported, never faked. */
  private async prepareRealisticGround(lat: number, lon: number, half: number, center: [number, number]): Promise<void> {
    const r = this.renderer!;
    const tb = this.settings.tileBase ?? '';
    const imagery = { ...IMAGERY_EOX_S2, url: tb ? `${tb}/eox/{z}/{x}/{y}` : IMAGERY_EOX_S2.url };
    const demUrl = tb ? `${tb}/terrarium/{z}/{x}/{y}` : TERRARIUM_URL;
    r.setMapGain(2.1);
    const [img, dem] = await Promise.all([
      buildGroundMap({ lat, lon, radiusKm: half, size: 4096, provider: imagery, theme: 'light', maxZoom: 12, maxTiles: 330 }, () => false).catch((e) => { console.warn('[clip] imagery failed:', e); return null; }),
      buildTerrain({ lat, lon, halfKm: half, size: 1024, url: demUrl }).catch((e) => { console.warn('[clip] terrain failed:', e); return null; }),
    ]);
    if (img) {
      r.setGround(img.size, img.data, half, center);
      this.groundImg = { size: img.size, data: img.data, half, cx: center[0], cy: center[1] };
      this.mapOk = true;
      if (img.missing) console.warn(`[clip] imagery: ${img.missing} tiles missing`);
    } else {
      r.setParams({ groundMode: 'grid' });
    }
    if (dem) {
      r.setTerrain({ size: dem.size, heightsKm: dem.heightsKm, minKm: dem.minKm, maxKm: dem.maxKm, half, center });
      this.terr = { size: dem.size, h: dem.heightsKm, half, cx: center[0], cy: center[1] };
      this.terrainInfo = `terrain ${Math.round(dem.minKm * 1000)} to ${Math.round(dem.maxKm * 1000)} m ASL (zoom ${dem.zoom}, ${dem.missing} tiles missing)`;
    } else {
      console.warn('[clip] no terrain: the ground stays flat');
    }
  }

  /** Lat / lon of a point (km east / north of the bundle's AEQD origin), by Newton iteration. */
  private latLonOf(x: number, y: number): [number, number] {
    const o = this.manifest.origin;
    let lat = o.lat + y / 111.19;
    let lon = o.lon + x / (111.19 * Math.cos((o.lat * Math.PI) / 180));
    for (let i = 0; i < 6; i++) {
      const [ex, ey] = aeqdOffsetKm(o.lat, o.lon, lat, lon);
      lat -= (ey - y) / 111.19;
      lon -= (ex - x) / (111.19 * Math.cos((lat * Math.PI) / 180));
    }
    return [lat, lon];
  }

  /** Where the observer stands, for the report (lat, lon, km in the scene). */
  observerInfo(): { lat: number; lon: number; x: number; y: number; eyeM: number } {
    const [lat, lon] = this.latLonOf(this.obs[0], this.obs[1]);
    return { lat, lon, x: this.obs[0], y: this.obs[1], eyeM: this.settings.observer?.eyeM ?? OBSERVER_DEFAULTS.eyeM };
  }

  /** The state of clip time `t`: data time, interval and position inside it, storm centre, camera. */
  stateAt(t: number) {
    const td = this.ex ? this.ts[0] + this.ex.dataTime(t) : dataTimeAt(t, this.settings.duration, this.ts[0], this.ts[this.ts.length - 1]);
    const { i: k, s } = locate(this.ts, td);
    const [cx, cy] = stormAt(this.track, td);
    // orbit camera target: the middle of the track plus a fraction of the storm's displacement from it
    const lag = this.settings.targetLag ?? 0.3;
    const [tx, ty] = lagTarget(this.anchor, [cx, cy], lag);
    const pose = observerPose(this.obs, [cx, cy], this.anchor, this.settings.observer);
    return { t, td, k, s, cx, cy, tx, ty, cam: cameraAt(this.keys, t), pose };
  }

  /** Light of the realistic look at data time `td` (s): the real sun, or the documented dusk light after sunset. */
  private updateLight(td: number): void {
    const o = this.manifest.origin;
    const when = new Date(new Date(this.manifest.steps[0].time_utc).getTime() + td * 1000);
    const key = `${Math.round(when.getTime() / 60000)}`;
    if (key === this.sunKey && this.sun) return;
    this.sunKey = key;
    this.sun = sunLight(when, o.lat, o.lon);
    // when the real sun is down the default is a stylised daylight; 'dusk' keeps the dusk light
    const kind = this.look.light as LightKind;
    this.lightKind = kind;
    if (this.sun.stylised && (kind === 'daylight' || kind === 'overcast')) this.sun = daylightAssumed(when, o.lat, o.lon);
    if (this.settings.sun) {
      this.sun = { ...this.sun, azimuthDeg: this.settings.sun.azimuthDeg, elevationDeg: this.settings.sun.elevationDeg, stylised: true, chosen: true } as SunLight & { chosen?: boolean };
    }
    // the window camera sees far: more haze than the orbit view
    const lk = lookWith(lookFor(this.sun, this.lookSteps, this.settings.camera === 'flight' ? 0.35 : this.pov ? 2.4 : 1, kind === 'overcast' || kind === 'night' ? kind : undefined), this.look);
    this.renderer!.setRealistic(lk);
  }

  /** The first-person camera at clip time `t`: position (km, eye height ASL in km, true), yaw and pitch. */
  private povAt(t: number, shake = false): { x: number; y: number; zKm: number; yawDeg: number; pitchDeg: number; rKm: number } {
    if (this.ex) {
      const p = this.ex.pose(t);
      return { x: p.x, y: p.y, zKm: p.zKm, yawDeg: p.yawDeg, pitchDeg: p.pitchDeg, rKm: 0 };
    }
    const st = this.stateAt(t);
    if (this.settings.camera === 'flight') {
      const fp = flightPose(this.flightKeys, t, [st.cx, st.cy]);
      let [x, y, z, yaw, pit] = [fp.eye[0], fp.eye[1], fp.eye[2], fp.yawDeg, fp.pitchDeg];
      if (shake && this.pairVols && st.k === this.pair) {
        const [va, vb] = this.pairVols;
        const da = dbzAt(va, x, y, Math.max(0.3, z)), db = dbzAt(vb, x, y, Math.max(0.3, z));
        const lv = (d: number) => (Number.isFinite(d) ? Math.max(0, Math.min(1, (d - 30) / 25)) : 0);
        const sh = cameraShake(t, ((1 - st.s) * lv(da) + st.s * lv(db)) * (this.look.shake as number));
        x += sh.dx; y += sh.dy; z += sh.dz; yaw += sh.yaw; pit += sh.pitch;
      }
      return { x, y, zKm: z, yawDeg: yaw, pitchDeg: pit, rKm: fp.key.r };
    }
    return { x: this.obs[0], y: this.obs[1], zKm: this.obsEyeKm, yawDeg: st.pose.yawDeg, pitchDeg: st.pose.pitchDeg, rKm: 0 };
  }

  /** Lightning light with the look's brightness and colour. */
  private applyFlashLook(r: CartesianRenderer, fl: { points: { x: number; y: number; z: number; i: number }[]; sky: number }): void {
    const g = this.look.lightningBrightness as number;
    const hex = this.look.lightningColor as string;
    const col: [number, number, number] = hex ? [parseInt(hex.slice(1, 3), 16) / 255, parseInt(hex.slice(3, 5), 16) / 255, parseInt(hex.slice(5, 7), 16) / 255] : [0.75, 0.82, 1];
    r.setFlashLight(fl.points.map((p) => ({ ...p, i: p.i * g })), fl.sky * g, col);
  }

  /** What the light is, for the info line (null in the scientific style). */
  get lightInfo(): SunLight | null {
    return this.sun;
  }

  /** Test hook: draw the terrain `k` times higher (checks that the volume hides below the terrain). */
  debugScaleTerrain(k: number): void {
    const t = this.terr;
    if (!t || !this.renderer) return;
    const h = new Float32Array(t.h.length);
    let mn = Infinity, mx = -Infinity;
    for (let i = 0; i < h.length; i++) {
      h[i] = t.h[i] * k;
      mn = Math.min(mn, h[i]);
      mx = Math.max(mx, h[i]);
    }
    this.renderer.setTerrain({ size: t.size, heightsKm: h, minKm: mn, maxKm: mx, half: t.half, center: [t.cx, t.cy] });
  }

  /** Terrain height (km ASL) at a scene position; 0 without terrain. */
  terrainKmAt(x: number, y: number): number {
    const t = this.terr;
    if (!t) return 0;
    const fx = Math.min(t.size - 1.001, Math.max(0, ((x - t.cx) / (2 * t.half) + 0.5) * t.size - 0.5));
    const fy = Math.min(t.size - 1.001, Math.max(0, (0.5 - (y - t.cy) / (2 * t.half)) * t.size - 0.5));
    const x0 = fx | 0, y0 = fy | 0, ax = fx - x0, ay = fy - y0;
    const g = (i: number, j: number) => t.h[j * t.size + i];
    return (g(x0, y0) * (1 - ax) + g(x0 + 1, y0) * ax) * (1 - ay) + (g(x0, y0 + 1) * (1 - ax) + g(x0 + 1, y0 + 1) * ax) * ay;
  }

  /** Radar sites as [name, x km, y km] (scene frame). */
  siteXY(): [string, number, number][] {
    return (this.g0?.sites ?? []).map((q: any) => [q.name ?? q.id ?? '', q.x, q.y]);
  }

  /** Screen position (px) of a ground / scene point (km, z exaggerated) in the last drawn frame. */
  project(x: number, y: number, z = 0): [number, number] | null {
    const c = this.lastCam;
    if (!c) return null;
    const v = [x - c.eye[0], y - c.eye[1], z - c.eye[2]];
    const d = v[0] * c.fwd[0] + v[1] * c.fwd[1] + v[2] * c.fwd[2];
    if (d < 0.5) return null;
    const px = v[0] * c.right[0] + v[1] * c.right[1] + v[2] * c.right[2];
    const py = v[0] * c.up[0] + v[1] * c.up[1] + v[2] * c.up[2];
    return [c.w / 2 + (px / d / c.tanX) * (c.w / 2), c.h / 2 - (py / d / c.tanY) * (c.h / 2)];
  }

  // ── Volumes, flow ───────────────────────────────────────────────────────

  /** Make steps k and k + 1 the pair on the GPU, with the flow between them. */
  private async ensurePair(k: number): Promise<void> {
    const r = this.renderer;
    if (!r || k === this.pair) return;
    const [a, b] = await Promise.all([this.volumes.get(k), this.volumes.get(k + 1)]);
    await r.setVolumePair(a, b);
    this.pairVols = [a, b];
    this.surfA = this.surfB = null;
    let flow = false;
    if (this.settings.flow !== false) {
      const [wa, wb] = await Promise.all([this.windLoader.get(k), this.windLoader.get(k + 1)]);
      if (wa && wb && wa.nx === wb.nx && wa.ny === wb.ny && wa.nz === wb.nz) {
        r.setMorphFlow(this.flowField(wa, a), this.flowField(wb, b));
        this.flowOffset = aeqdOffsetKm(a.originLat, a.originLon, wa.originLat, wa.originLon);
        flow = true;
        this.lastFlow = { active: true, nRadars: wa.nRadars ?? 0 };
      }
      if (this.settings.sea && wa && wb) {
        this.surfA = this.surfaceWind(wa, a);
        this.surfB = this.surfaceWind(wb, b);
      }
    }
    if (!flow) {
      r.setMorphFlow(null, null);
      this.lastFlow = { active: false, nRadars: 0 };
    }
    this.pairFlow = flow;
    this.pair = k;
  }

  /**
   * Near-surface wind over the ground square (256 x 256 texels, u, v in m/s, confidence): for every texel the
   * nearest valid retrieved cell (within 2 cells) of the lowest levels, x 0.65 for the step down to the
   * surface. Texels without a retrieval have confidence 0 and the sea there stays calm.
   */
  private surfaceWind(w: WindStep, vol: CartVolume): Float32Array | null {
    const g = this.groundImg;
    if (!g) return null;
    const N = 256;
    const out = new Float32Array(N * N * 4);
    const off = aeqdOffsetKm(w.originLat, w.originLon, vol.originLat, vol.originLon);
    const nl = Math.min(w.nz, 6);
    for (let j = 0; j < N; j++) {
      const y = g.cy + g.half - ((j + 0.5) / N) * 2 * g.half + off[1];
      for (let i = 0; i < N; i++) {
        const x = g.cx - g.half + ((i + 0.5) / N) * 2 * g.half + off[0];
        const i0 = Math.round((x - w.x0) / w.dx), j0 = Math.round((y - w.y0) / w.dy);
        let done = false;
        for (let k = 0; k < nl && !done; k++) {
          for (let dj = -2; dj <= 2 && !done; dj++) {
            for (let di = -2; di <= 2 && !done; di++) {
              const ii = i0 + di, jj = j0 + dj;
              if (ii < 0 || jj < 0 || ii >= w.nx || jj >= w.ny) continue;
              const idx = (k * w.ny + jj) * w.nx + ii;
              const u = w.u[idx], v = w.v[idx];
              if (!Number.isFinite(u) || !Number.isFinite(v)) continue;
              const o = (j * N + i) * 4;
              out[o] = u * SURFACE_WIND_FACTOR;
              out[o + 1] = v * SURFACE_WIND_FACTOR;
              out[o + 2] = 1 - 0.25 * Math.hypot(di, dj) / 2;
              done = true;
            }
          }
        }
      }
    }
    return out;
  }

  /** Storm-relative flow of a wind step: the mean over the echo is the storm's translation, the rest turns the echo. */
  private flowField(w: WindStep, v: CartVolume): MorphField {
    const off = aeqdOffsetKm(v.originLat, v.originLon, w.originLat, w.originLon);
    return stormRelativeField(w, (x, y, z) => dbzAt(v, x + off[0], y + off[1], z) >= 25);
  }

  // ── Lightning ───────────────────────────────────────────────────────────

  /** Observed flashes of the bundle, or a modelled source driven by the strong ice-phase echo. */
  private prepareLightning(): void {
    const m = this.manifest;
    const sets = this.settings;
    if (sets.lightning === 'off') { this.flashes = []; this.lightningSource = 'off'; return; }
    const first = this.ts[0], last = this.ts[this.ts.length - 1];
    const obsF = m.lightning?.flashes;
    if (obsF && obsF.length) {
      this.lightningSource = 'observed';
      const rng = mulberry32((sets.lightningSeed ?? 7) + 99);
      this.flashes = obsF
        .filter((f) => f.t_s >= first && f.t_s <= last)
        .map((f, i) => {
          const kind = rng() < 0.45 ? 'CG' : 'IC';
          const zTop = f.z ?? 6 + rng() * 3;
          const g = this.terrainKmAt(f.x, f.y);
          const origin: [number, number, number] = [f.x, f.y, zTop];
          const end: [number, number, number] = kind === 'CG'
            ? [f.x + (rng() - 0.5) * 6, f.y + (rng() - 0.5) * 6, g]
            : [f.x + (rng() - 0.5) * 24, f.y + (rng() - 0.5) * 24, zTop + (rng() - 0.5) * 2];
          return makeFlash({ id: i, t: clipTimeOf(f.t_s, sets.duration, first, last), kind, source: 'observed', origin, end, energy: f.energy, seed: sets.lightningSeed });
        })
        .sort((a, b) => a.t - b.t);
      return;
    }
    // modelled: rate from the ice-phase echo volume of the steps, places from its strong columns
    this.lightningSource = 'modelled';
    const vmax = Math.max(...this.summaries.map((s) => s.iceKm3));
    if (!(vmax > 0)) { this.flashes = []; return; }
    const rMax = sets.lightningRate ?? 0.8;
    const volAt = (td: number) => {
      const { i, s } = locate(this.ts, td);
      const a = this.summaries[i]?.iceKm3 ?? 0, b = this.summaries[i + 1]?.iceKm3 ?? a;
      return a * (1 - s) + b * s;
    };
    const dur = sets.duration;
    const rate = (t: number) => {
      const td = dataTimeAt(t, dur, first, last);
      const rel = volAt(td) / vmax;
      return rel < 0.08 ? 0 : rMax * Math.min(1, rel);
    };
    const candidates = (t: number): FlashCandidate[] => {
      const td = dataTimeAt(t, dur, first, last);
      const { i, s } = locate(this.ts, td);
      const k = s < 0.5 ? i : i + 1;
      const mv = intervalMotion(this.track, Math.min(i, this.ts.length - 2));
      const dtk = td - this.ts[k];
      return this.summaries[k].candidates.map((c) => ({ ...c, x: c.x + mv[0] * dtk, y: c.y + mv[1] * dtk }));
    };
    this.flashes = modelFlashes({
      seed: sets.lightningSeed ?? 7, duration: dur, rate, candidates,
      groundKm: (x, y) => this.terrainKmAt(x, y), headingDeg: m.storm.heading_deg,
    });
  }

  // ── Frames ──────────────────────────────────────────────────────────────

  /** Draw frame `i` (0-based) completely; resolves when the pixels are final. */
  async renderFrame(i: number): Promise<void> {
    return this.renderAt(i / this.settings.fps);
  }

  /** Draw the frame of clip time `t` (s); `camOverride` replaces the choreography's camera (tests). */
  async renderAt(t: number, camOverride?: Partial<CamKey>): Promise<void> {
    const r = this.renderer;
    if (!r) throw new Error('prepare() first');
    const st = this.stateAt(t);
    if (camOverride) st.cam = { ...st.cam, ...camOverride };
    await this.ensurePair(st.k);
    const m = intervalMotion(this.track, st.k);
    r.setMorph(st.s, [0, 0, 0], [0, 0, 0]);
    r.setMorphMotion(this.ts[st.k + 1] - this.ts[st.k], m, this.flowOffset);
    const exLook = this.ex?.look?.(t);
    if (exLook) {
      const key = JSON.stringify(exLook);
      if (key !== this.lookKey) {
        this.lookKey = key;
        this.setLook(exLook);
      }
    }
    if (this.realistic) {
      this.updateLight(st.td);
      const fovDeg = (this.look.fov as number) > 0 ? (this.look.fov as number) : this.pov ? 60 : 0;
      r.setFov(fovDeg);
      // the cloud texture rides with the storm: its offset is how far the storm centre has moved
      const [s0x, s0y] = stormAt(this.track, this.ts[0]);
      r.setCloudTexture((this.settings.cloudTexture ?? 1) * (this.look.detail as number), [st.cx - s0x, st.cy - s0y, 0], this.settings.cloudFreq ?? (this.look.palette === 'cyclone' ? [0.9, 1.8] : [1.5, 0.7]));
    }
    this.cur = { t: st.t, td: st.td, k: st.k, s: st.s };
    this.curPose = st.pose;
    if (this.settings.sea && this.surfA && this.surfB && this.groundImg) {
      // blend the two steps' surface wind at the position in the interval (where only one has wind, that one)
      const N = 256;
      const a = this.surfA, b = this.surfB;
      const out = this.surfBuf ?? (this.surfBuf = new Float32Array(N * N * 4));
      const sB = st.s;
      for (let i = 0; i < N * N; i++) {
        const o = i * 4;
        const wa = (1 - sB) * a[o + 2], wb = sB * b[o + 2];
        const sum = wa + wb;
        if (sum <= 1e-6) { out[o] = out[o + 1] = out[o + 2] = 0; continue; }
        out[o] = (wa * a[o] + wb * b[o]) / sum;
        out[o + 1] = (wa * a[o + 1] + wb * b[o + 1]) / sum;
        out[o + 2] = sum;
      }
      r.setSea(true, { size: N, data: out });
    }
    const exPose = this.ex ? this.ex.pose(t) : null;
    const ve = exPose ? exPose.vexag : this.settings.vexag;
    if (ve !== this.ve) {
      this.ve = ve;
      r.setParams({ vexag: ve });
    }
    if (exPose?.fov) r.setFov(exPose.fov);
    if (this.pov) {
      const pv = this.povAt(t, true);
      const eye: [number, number, number] = [pv.x, pv.y, pv.zKm * ve];
      this.povNow = { x: pv.x, y: pv.y, yawDeg: pv.yawDeg, altKm: pv.zKm, rKm: pv.rKm };
      this.curPose = { yawDeg: pv.yawDeg, pitchDeg: pv.pitchDeg };
      this.crop = this.fullBox;
      r.setCropBox(this.ex?.cropBox?.() ?? null);
      let fl = flashLight(this.flashes, t, eye, ve);
      if (this.ex) {
        const info = this.frameInfo();
        info.eye = eye;
        this.ex.beforeFrame?.(info);
        fl = this.ex.flashLight?.(info) ?? { points: [], sky: 0 };
      }
      this.applyFlashLook(r, fl);
      r.exportRenderFP(eye, pv.yawDeg, pv.pitchDeg);
    } else {
      const half = this.cropHalf;
      const [ax, ay] = this.anchor;
      this.crop = { xMin: ax - half, xMax: ax + half, yMin: ay - half, yMax: ay + half, zMin: this.fullBox.zMin, zMax: this.fullBox.zMax };
      r.setCropBox(this.crop);
      r.setExtendDown(false);
      const fl = flashLight(this.flashes, t, [st.tx, st.ty, 0], ve);
      this.applyFlashLook(r, fl);
      if (st.cam.fov !== undefined && st.cam.fov > 0) r.setFov(st.cam.fov);
      r.exportRender({
        theta: st.cam.theta, phi: st.cam.phi, dist: st.cam.dist,
        target: [st.tx + st.cam.dx, st.ty + st.cam.dy, st.cam.dz * ve],
      });
    }
    this.drawBanner();
    if (this.ex?.settle) await this.ex.settle();
  }

  // ── Overlay ─────────────────────────────────────────────────────────────

  /** Frame, compass letters, radar sites, height axis and scale bar (the 3D tab's overlay), lightning. */
  private drawOverlay(f: CamFrame): void {
    const c = this.overlay;
    const crop = this.crop;
    if (!crop) return;
    const cam = { eye: f.eye, fwd: f.fwd, right: f.right, up: f.up, tanX: f.tanX, tanY: f.tanY, w: c.width, h: c.height };
    this.lastCam = cam;
    if (this.pov) {
      this.ctx.clearRect(0, 0, c.width, c.height);
      if (this.ex) this.ex.drawScene?.(this.ctx, cam, { ...this.frameInfo(), eye: f.eye });
      else drawCompassStrip(this.ctx, c.width, c.height, this.curPose.yawDeg, f.tanX, c.height / 1080);
    } else {
      drawCartesianOverlay(this.ctx, cam, {
        box: crop, vexag: this.settings.vexag, sites: this.g0.sites, lightScene: false, showFrame: this.settings.ground !== 'realistic',
        showHeightAxis: false, showExaggeration: false,
      });
      drawOffBoxSites(this.ctx, cam, crop, this.g0.sites, false);
      this.drawTrack(cam);
    }
    this.flashesDrawn = drawFlashes(this.ctx, cam, this.flashes, this.cur.t, this.settings.vexag, this.look.lightningBrightness as number, this.look.lightningColor as string);
  }

  /** The storm centre's path on the map: travelled part solid, the rest faint, observed positions as dots. */
  private drawTrack(cam: NonNullable<StormClip['lastCam']>): void {
    const ctx = this.ctx;
    const u = this.overlay.height / 1080;
    const t0 = this.ts[0], t1 = this.ts[this.ts.length - 1];
    const lift = 0.25 * this.settings.vexag; // just above the ground (and the terrain)
    const proj = (x: number, y: number) => project(cam, [x, y, this.terrainKmAt(x, y) * this.settings.vexag + lift]);
    const N = 90;
    const pts: { p: [number, number, number] | null; t: number }[] = [];
    for (let i = 0; i <= N; i++) {
      const t = t0 + ((t1 - t0) * i) / N;
      const [x, y] = stormAt(this.track, t);
      pts.push({ p: proj(x, y), t });
    }
    const stroke = (from: number, to: number, color: string, width: number, dash: number[]) => {
      ctx.beginPath();
      let started = false;
      for (let i = from; i <= to; i++) {
        const p = pts[i].p;
        if (!p) { started = false; continue; }
        if (!started) { ctx.moveTo(p[0], p[1]); started = true; } else ctx.lineTo(p[0], p[1]);
      }
      ctx.setLineDash(dash);
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      ctx.lineCap = 'round';
      ctx.stroke();
      ctx.setLineDash([]);
    };
    const now = Math.max(0, Math.min(N, Math.round(((this.cur.td - t0) / (t1 - t0)) * N)));
    stroke(0, N, 'rgba(5,8,16,0.55)', 5 * u, []); // dark underlay keeps the thin line readable
    stroke(now, N, 'rgba(255,255,255,0.45)', 2 * u, [7 * u, 6 * u]);
    stroke(0, now, 'rgba(255,235,150,0.97)', 3 * u, []);
    // observed positions (a label is skipped when it would sit on the previous one)
    let lastLabel: [number, number] | null = null;
    this.ts.forEach((t, i) => {
      const [x, y] = [this.track[i].x, this.track[i].y];
      const p = proj(x, y);
      if (!p) return;
      const seen = this.cur.td >= t - 1e-6;
      ctx.beginPath();
      ctx.arc(p[0], p[1], 5 * u, 0, Math.PI * 2);
      ctx.fillStyle = seen ? 'rgba(255,235,150,1)' : 'rgba(255,255,255,0.6)';
      ctx.fill();
      ctx.lineWidth = 2 * u;
      ctx.strokeStyle = 'rgba(5,8,16,0.85)';
      ctx.stroke();
      if (lastLabel && Math.hypot(p[0] - lastLabel[0], p[1] - lastLabel[1]) < 70 * u) return;
      lastLabel = [p[0], p[1]];
      ctx.font = `600 ${Math.round(15 * u)}px system-ui, -apple-system, sans-serif`;
      ctx.textAlign = 'left';
      ctx.lineJoin = 'round';
      ctx.lineWidth = 4 * u;
      ctx.strokeStyle = 'rgba(5,8,16,0.85)';
      const label = utcLabel(this.manifest.steps[0].time_utc, t, false).replace(' UTC', '');
      ctx.strokeText(label, p[0] + 9 * u, p[1] - 8 * u);
      ctx.fillStyle = 'rgba(255,240,190,0.98)';
      ctx.fillText(label, p[0] + 9 * u, p[1] - 8 * u);
    });
    // the storm centre now
    const [cx, cy] = stormAt(this.track, this.cur.td);
    const c = proj(cx, cy);
    if (c) {
      ctx.beginPath();
      ctx.arc(c[0], c[1], 11 * u, 0, Math.PI * 2);
      ctx.lineWidth = 4 * u;
      ctx.strokeStyle = 'rgba(5,8,16,0.8)';
      ctx.stroke();
      ctx.lineWidth = 2.2 * u;
      ctx.strokeStyle = 'rgba(255,255,255,0.98)';
      ctx.stroke();
    }
  }

  // ── Inset map (observer camera) ─────────────────────────────────────────

  private static INSET_PX = 270;
  private echoCanvas: HTMLCanvasElement | null = null;

  /** The base of the inset: the ground image around the observer and the storm track. */
  private prepareInset(): void {
    if (!this.pov) return;
    const S = StormClip.INSET_PX;
    const pts: [number, number][] = [this.obs, ...this.track.map((p) => [p.x, p.y] as [number, number])];
    const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
    const cx = (Math.min(...xs) + Math.max(...xs)) / 2, cy = (Math.min(...ys) + Math.max(...ys)) / 2;
    let half = Math.max(22, (Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys)) / 2) * 1.5 + 6);
    if (this.settings.camera === 'flight') half = Math.max(half, (this.settings.viewKm ?? 330) * 0.4);
    this.insetView = { cx, cy, half };
    const cv = document.createElement('canvas');
    cv.width = S;
    cv.height = S;
    const g = cv.getContext('2d')!;
    const img = this.groundImg;
    if (img) {
      const id = g.createImageData(S, S);
      for (let j = 0; j < S; j++) {
        for (let i = 0; i < S; i++) {
          const x = cx + (i / S - 0.5) * 2 * half, y = cy + (0.5 - j / S) * 2 * half;
          const gx = Math.max(0, Math.min(img.size - 1, Math.round(((x - img.cx) / (2 * img.half) + 0.5) * img.size)));
          const gy = Math.max(0, Math.min(img.size - 1, Math.round((0.5 - (y - img.cy) / (2 * img.half)) * img.size)));
          const o = (gy * img.size + gx) * 4, q = (j * S + i) * 4;
          id.data[q] = img.data[o]; id.data[q + 1] = img.data[o + 1]; id.data[q + 2] = img.data[o + 2]; id.data[q + 3] = 255;
        }
      }
      g.putImageData(id, 0, 0);
    }
    this.insetBase = cv;
  }

  /** The storm echo (column maximum of the interpolated volume) over the inset area, as an RGBA canvas. */
  private insetEcho(): HTMLCanvasElement {
    const S = StormClip.INSET_PX;
    if (!this.echoCanvas) {
      this.echoCanvas = document.createElement('canvas');
      this.echoCanvas.width = S;
      this.echoCanvas.height = S;
    }
    const g = this.echoCanvas.getContext('2d')!;
    const id = g.createImageData(S, S);
    const { k, s } = this.cur;
    const A = this.summaries[k].composite, B = this.summaries[k + 1].composite;
    const v = this.g0;
    const m = intervalMotion(this.track, k);
    const dt = this.ts[k + 1] - this.ts[k];
    const lut = COLORMAP_DATA['NWSRef'];
    const { cx, cy, half } = this.insetView;
    const low = 20; // dBZ: fainter echo is not drawn on the map
    for (let j = 0; j < S; j++) {
      for (let i = 0; i < S; i++) {
        const x = cx + (i / S - 0.5) * 2 * half, y = cy + (0.5 - j / S) * 2 * half;
        const ca = this.codeAt(A, x - m[0] * s * dt, y - m[1] * s * dt, v);
        const cb = this.codeAt(B, x + m[0] * (1 - s) * dt, y + m[1] * (1 - s) * dt, v);
        const wa = ca ? 1 - s : 0, wb = cb ? s : 0;
        if (wa + wb <= 0) continue;
        const code = (wa * ca + wb * cb) / (wa + wb);
        const dbz = dbzOfCode(v, code);
        if (dbz < low) continue;
        const idx = Math.max(0, Math.min(255, Math.round(((dbz - v.vmin) / (v.vmax - v.vmin)) * 255)));
        const q = (j * S + i) * 4;
        id.data[q] = lut[idx * 3]; id.data[q + 1] = lut[idx * 3 + 1]; id.data[q + 2] = lut[idx * 3 + 2];
        id.data[q + 3] = Math.round(255 * Math.min(0.85, 0.35 + (dbz - low) / 60));
      }
    }
    g.putImageData(id, 0, 0);
    return this.echoCanvas;
  }

  private codeAt(comp: Uint8Array, x: number, y: number, v: CartVolume): number {
    const i = Math.round((x - v.x0) / v.dx), j = Math.round((y - v.y0) / v.dy);
    if (i < 0 || j < 0 || i >= v.nx || j >= v.ny) return 0;
    return comp[j * v.nx + i];
  }

  // ── Banner ──────────────────────────────────────────────────────────────

  private drawBanner(): void {
    if (this.ex?.drawHud) {
      this.ex.drawHud(this.ctx, this.overlay.width, this.overlay.height, this.frameInfo());
      return;
    }
    const ctx = this.ctx;
    const W = this.overlay.width, H = this.overlay.height;
    const u = H / 1080;
    const m = this.manifest;
    const t0 = m.steps[0].time_utc;
    const first = this.ts[0], last = this.ts[this.ts.length - 1];
    const { td } = this.cur;
    const font = (px: number, w = 600) => `${w} ${Math.round(px * u)}px system-ui, -apple-system, "Segoe UI", sans-serif`;
    const text = (str: string, x: number, y: number, px: number, color = 'rgba(240,245,255,0.97)', w = 600, align: CanvasTextAlign = 'left') => {
      ctx.font = font(px, w);
      ctx.textAlign = align;
      ctx.textBaseline = 'alphabetic';
      ctx.lineJoin = 'round';
      ctx.lineWidth = 4 * u;
      ctx.strokeStyle = 'rgba(5,8,16,0.8)';
      ctx.strokeText(str, x, y);
      ctx.fillStyle = color;
      ctx.fillText(str, x, y);
    };

    // title and time (the observer clip keeps the top centre for the compass strip)
    const pad = 34 * u;
    const obsMode = this.pov;
    const y0 = pad + (obsMode ? 28 : 0) * u;
    text(this.settings.title ?? m.title, pad, y0 + 34 * u, 38, 'rgba(255,255,255,0.98)', 700);
    text(this.settings.subtitle ?? 'merged 3-D radar reflectivity', pad, y0 + 64 * u, 19, 'rgba(200,215,240,0.95)', 500);
    text(utcLabel(t0, td), pad, y0 + 128 * u, 58, 'rgba(255,255,255,0.99)', 700);
    const date = new Date(new Date(t0).getTime() + td * 1000);
    const lapse = (last - first) / Math.max(1, this.settings.duration - 3);
    text(`${date.toUTCString().slice(5, 16)} · time-lapse ×${lapse.toFixed(0)}`, pad, y0 + 156 * u, 19, 'rgba(200,215,240,0.95)', 500);
    const [sx0, sy0] = stormAt(this.track, first);
    const [sx1, sy1] = stormAt(this.track, td);
    const moved = Math.hypot(sx1 - sx0, sy1 - sy0);
    if (this.settings.camera === 'flight') {
      // numbers from the data only: nothing here is an intensity category or a forecast
      const ws = this.windStats[Math.min(this.cur.k + (this.cur.s > 0.5 ? 1 : 0), this.windStats.length - 1)];
      const eye = m.steps[Math.min(this.cur.k + (this.cur.s > 0.5 ? 1 : 0), m.steps.length - 1)].eye;
      text(
        eye ? `Eye (hole in the radar echo): radius ${eye.radius_km.toFixed(0)} km` : 'Eye: no echo-free hole found in this step',
        pad, y0 + 184 * u, 19, 'rgba(255,235,150,0.98)', 600,
      );
      text(
        ws
          ? `Retrieved Doppler wind (${ws.nRadars}-radar, ${(100 * ws.coverage).toFixed(0)}% of its box): strongest 1% of cells ${ws.p99All.toFixed(0)} m/s (${(ws.p99All * 1.944).toFixed(0)} kt), lowest 3 km ${ws.p99Low.toFixed(0)} m/s; highest cell ${ws.maxAll.toFixed(0)} m/s`
          : 'Retrieved Doppler wind: none for this step',
        pad, y0 + 208 * u, 17, 'rgba(255,235,150,0.98)', 600,
      );
      text(`centre moved ${moved.toFixed(1)} km since ${utcLabel(t0, 0, false)} · camera ${this.povNow.altKm.toFixed(1)} km up, ${this.povNow.rKm.toFixed(0)} km from the eye`, pad, y0 + 230 * u, 16, 'rgba(210,222,245,0.95)', 500);
      text('Winds exist only where two or more radars cross well, from 1.5 km up; an analysis is not the true maximum. No intensity category is inferred.', pad, y0 + 250 * u, 14, 'rgba(210,222,245,0.9)', 500);
    } else if (this.observer) {
      const dx = sx1 - this.obs[0], dy = sy1 - this.obs[1];
      const bd = bearingDeg(dx, dy);
      text(`storm centre ${Math.hypot(dx, dy).toFixed(1)} km ${compassLabel(bd)} of the observer · moved ${moved.toFixed(1)} km`, pad, y0 + 184 * u, 19, 'rgba(255,235,150,0.98)', 600);
    } else {
      text(`storm centre moved ${moved.toFixed(1)} km since ${utcLabel(t0, 0, false)}`, pad, y0 + 184 * u, 19, 'rgba(255,235,150,0.98)', 600);
    }
    // lightning provenance: never ambiguous
    if (this.lightningSource !== 'off') {
      const obsL = this.lightningSource === 'observed';
      const n = this.flashes.length;
      text(
        obsL ? `Lightning: OBSERVED flashes (${m.lightning?.kind ?? 'lightning data'}), channel shape synthetic · ${n}` : `Lightning: MODELLED from radar echo, NOT observed · ${n} flashes, thinned for the time-lapse`,
        pad, y0 + (this.settings.camera === 'flight' ? 276 : 210) * u, 16, obsL ? 'rgba(170,230,255,0.97)' : 'rgba(255,190,150,0.98)', 600,
      );
    }

    // progress bar with the observation times
    const lift = this.settings.ground === 'realistic' || this.realistic || this.settings.audio ? 50 : 0;
    const bw = W * 0.56, bx = (W - bw) / 2, by = H - (92 + lift) * u;
    const px = (t: number) => bx + ((t - first) / (last - first)) * bw;
    ctx.fillStyle = 'rgba(8,12,22,0.55)';
    ctx.fillRect(bx - 16 * u, by - 44 * u, bw + 32 * u, 112 * u);
    ctx.fillStyle = 'rgba(255,255,255,0.22)';
    ctx.fillRect(bx, by, bw, 4 * u);
    ctx.fillStyle = 'rgba(120,200,255,0.95)';
    ctx.fillRect(bx, by, px(td) - bx, 4 * u);
    const near = this.ts.findIndex((t) => Math.abs(t - td) < (this.settings.observedTolS ?? 6));
    this.ts.forEach((t, i) => {
      const on = i === near;
      ctx.beginPath();
      ctx.arc(px(t), by + 2 * u, (on ? 9 : 6) * u, 0, Math.PI * 2);
      ctx.fillStyle = on ? 'rgba(255,214,90,1)' : td >= t ? 'rgba(120,200,255,1)' : 'rgba(255,255,255,0.8)';
      ctx.fill();
      if (this.ts.length <= 14 || i % Math.ceil(this.ts.length / 12) === 0) {
        text(utcLabel(t0, t, false).replace(' UTC', ''), px(t), by - 14 * u, 17, on ? 'rgba(255,214,90,1)' : 'rgba(220,230,250,0.95)', 600, 'center');
      }
    });
    ctx.beginPath();
    ctx.arc(px(td), by + 2 * u, 8 * u, 0, Math.PI * 2);
    ctx.fillStyle = '#fff';
    ctx.fill();
    const motion = `${m.storm.speed_ms.toFixed(0)} m/s toward ${m.storm.heading_deg.toFixed(0)}°`;
    const status = near >= 0 ? 'OBSERVED VOLUME' : 'INTERPOLATED between observed volumes';
    text(status, bx, by + 34 * u, 19, near >= 0 ? 'rgba(255,214,90,1)' : 'rgba(220,230,250,0.98)', 700);
    const carried = this.pairFlow
      ? `carried by the storm motion (${motion}) and the observed Doppler wind (${this.lastFlow.nRadars || 'multi'}-radar), blended; not new observations`
      : `carried along the storm motion (${motion}) only (no wind retrieval for this interval) and blended; not new observations`;
    text(
      near >= 0 ? `${Object.keys(m.steps[near].files).join(' ')} · nearest volume of each radar` : carried,
      bx, by + 58 * u, 16, 'rgba(200,215,240,0.95)', 500,
    );

    // colour bar (the scientific scale) or, in the realistic look, what a dBZ looks like as a shaft
    const cbx = W - pad - 22 * u, cby = H * (obsMode ? 0.46 : 0.28), cbh = H * (obsMode ? 0.24 : 0.36);
    const lut = COLORMAP_DATA['NWSRef'];
    const stylised = this.realistic;
    if (stylised) {
      ctx.fillStyle = 'rgba(70,82,110,0.9)';
      ctx.fillRect(cbx, cby, 22 * u, cbh);
    }
    for (let y = 0; y < cbh; y++) {
      const i = Math.min(255, Math.round((1 - y / cbh) * 255));
      if (stylised) {
        const dbz = this.g0.vmin + (i / 255) * (this.g0.vmax - this.g0.vmin);
        const smp = realisticSample(dbz, this.look.palette as Palette, sampleOptions(this.look));
        ctx.fillStyle = `rgba(${Math.round(smp.rgb[0] * 255)},${Math.round(smp.rgb[1] * 255)},${Math.round(smp.rgb[2] * 255)},${(1 - Math.exp(-smp.sigma * 5)).toFixed(3)})`;
      } else {
        ctx.fillStyle = `rgb(${lut[i * 3]},${lut[i * 3 + 1]},${lut[i * 3 + 2]})`;
      }
      ctx.fillRect(cbx, cby + y, 22 * u, 1.5);
    }
    ctx.strokeStyle = 'rgba(255,255,255,0.5)';
    ctx.lineWidth = 1;
    ctx.strokeRect(cbx, cby, 22 * u, cbh);
    const v0 = this.g0;
    for (const dbz of [0, 20, 40, 60]) {
      const f = (dbz - v0.vmin) / (v0.vmax - v0.vmin);
      text(String(dbz), cbx - 8 * u, cby + cbh * (1 - f) + 6 * u, 16, 'rgba(235,240,255,0.97)', 600, 'right');
    }
    text('dBZ', cbx + 11 * u, cby - 12 * u, 17, 'rgba(235,240,255,0.97)', 700, 'center');

    // the inset map of the observer clip
    if (obsMode && this.insetBase) {
      const S = StormClip.INSET_PX * u;
      const { cx, cy, half } = this.insetView;
      const [scx, scy] = stormAt(this.track, td);
      const passed = this.ts.filter((t) => t <= td + 1e-6).length - 1;
      const oi = this.observerInfo();
      drawInset(ctx, {
        x: W - pad - S, y: pad + 36 * u, size: S, u, cx, cy, half, base: this.insetBase, echo: this.insetEcho(),
        track: this.track.map((p) => [p.x, p.y]), storm: [scx, scy], passed, observer: this.settings.camera === 'flight' ? [this.povNow.x, this.povNow.y] : this.obs,
        yawDeg: this.curPose.yawDeg, hfovDeg: this.lastCam ? horizontalFovDeg(this.lastCam.tanY, W / H) : 73,
        title: this.settings.camera === 'flight' ? 'Where the camera is (the eye in white)' : 'Where the camera stands',
        caption: this.settings.camera === 'flight' ? `${this.povNow.altKm.toFixed(1)} km up · view heading ${Math.round(this.curPose.yawDeg)}°` : `${Math.abs(oi.lat).toFixed(2)}°${oi.lat >= 0 ? 'N' : 'S'} ${Math.abs(oi.lon).toFixed(2)}°${oi.lon >= 0 ? 'E' : 'W'} · eye ${oi.eyeM} m`,
      });
    }

    // realistic look: what is stylised, the sound label, and the attributions that must stay on screen
    if (this.settings.ground === 'realistic' || stylised || this.settings.audio) {
      const la: string[] = [];
      const lb: string[] = [];
      if (stylised) {
        la.push(this.lightKind === 'night'
          ? 'Night version: no sun, a faint ambient light; the storm is lit by modelled lightning (stylised, not a photograph)'
          : this.lightKind === 'overcast'
            ? `Stylised lighting, not a photograph: thick overcast, dim filtered light; the real scene was at night (the real sun was ${Math.round(Math.abs(this.sun?.realElevationDeg ?? 0))}° below the horizon)`
            : this.sun?.daylightAssumed
          ? `Stylised daylight; the real sun was ${Math.round(Math.abs(this.sun.realElevationDeg))}° below the horizon at this time (not a photograph)`
          : this.sun?.stylised
            ? `Stylised lighting, not a photograph: ${this.settings.sun ? 'light direction chosen for the shot' : 'dusk light at the sunset azimuth'} (the real sun was ${Math.round(Math.abs(this.sun.realElevationDeg))}° ${this.sun.realElevationDeg < 0 ? 'below' : 'above'} the horizon)`
            : `Stylised rendering, not a photograph (${this.sun?.label ?? 'sun light'})`);
        la.push('radar sees rain and ice, not cloud droplets: cloud look and fine detail are stylised, echo position and strength are radar data');
      }
      if (this.settings.ground === 'realistic') lb.push(`vertical scale ×${this.settings.vexag}${this.settings.vexag > 1 ? ' for terrain and storm (relief exaggerated)' : ' (true scale)'}`);
      if (this.settings.audio) lb.push(`Sound: ${AUDIO_LABEL}; wind ${this.windSource === 'retrieved' ? 'from the retrieved multi-Doppler wind at the observer' : 'a proxy, not measured'}`);
      if (la.length) text(la.join(' · '), W / 2, H - 56 * u, 15, 'rgba(235,240,255,0.97)', 600, 'center');
      if (lb.length) text(lb.join(' · '), W / 2, H - 36 * u, 15, 'rgba(235,240,255,0.97)', 600, 'center');
      const l1 = [
        this.settings.ground === 'realistic' ? IMAGERY_EOX_S2.attribution : '',
        this.settings.ground === 'realistic' ? TERRAIN_ATTRIBUTION : '',
        this.settings.radarCredit ?? 'Radar: NEXRAD Level II (NOAA)',
      ].filter(Boolean).join(' · ');
      text(l1, W / 2, H - 14 * u, 12, 'rgba(215,225,245,0.9)', 500, 'center');
    } else {
      text(`Height shown up to ${this.fullBox.zMax.toFixed(0)} km · vertical scale ×${this.settings.vexag}`, W - pad, H - 20 * u, 15, 'rgba(200,215,240,0.9)', 500, 'right');
    }
  }

  // ── Sound ───────────────────────────────────────────────────────────────

  /** Wind speed at the observer (m/s) and the rain level 0..1 for clip time `t`. */
  private async observerConditions(t: number): Promise<{ wind: number; rain: number }> {
    const st = this.stateAt(t);
    const { k, s } = st;
    const v = await this.volumes.get(k);
    const vb = await this.volumes.get(k + 1);
    const m = intervalMotion(this.track, k);
    const dt = this.ts[k + 1] - this.ts[k];
    const pv = this.povAt(t);
    const [ox, oy] = [pv.x, pv.y];
    const flight = this.settings.camera === 'flight';
    // rain: the echo near the ground over the observer, carried like the cloud; 20 dBZ none, 55 dBZ full
    const zq = flight ? Math.max(0.5, pv.zKm) : 0.5;
    const da = dbzAt(v, ox - m[0] * s * dt, oy - m[1] * s * dt, zq);
    const db = dbzAt(vb, ox + m[0] * (1 - s) * dt, oy + m[1] * (1 - s) * dt, zq);
    const lv = (d: number) => (Number.isFinite(d) ? Math.max(0, Math.min(1, (d - 20) / 35)) : 0);
    const rain = (1 - s) * lv(da) + s * lv(db);
    let wind: number | null = null;
    if (this.windSource === 'retrieved') {
      const [wa, wb] = await Promise.all([this.windLoader.get(k), this.windLoader.get(k + 1)]);
      const sp = (w: WindStep | null, vol: CartVolume): number | null => {
        if (!w) return null;
        const off = aeqdOffsetKm(w.originLat, w.originLon, vol.originLat, vol.originLon);
        const x = ox + off[0], y = oy + off[1];
        const i0 = Math.round((x - w.x0) / w.dx), j0 = Math.round((y - w.y0) / w.dy);
        // the nearest valid cell of the lowest levels within a few cells
        for (let k2 = 0; k2 < w.nz; k2++) {
          const R = flight ? 8 : 3;
          let best: number | null = null, bd = (R + 1) * (R + 1);
          for (let dj = -R; dj <= R; dj++) {
            for (let di = -R; di <= R; di++) {
              const i = i0 + di, j = j0 + dj;
              if (i < 0 || j < 0 || i >= w.nx || j >= w.ny) continue;
              const idx = (k2 * w.ny + j) * w.nx + i;
              const u = w.u[idx], vv = w.v[idx];
              if (!Number.isFinite(u) || !Number.isFinite(vv)) continue;
              const d = Math.hypot(di, dj);
              if (d < bd) { bd = d; best = Math.hypot(u, vv); }
            }
          }
          if (best !== null) return best * SURFACE_WIND_FACTOR;
        }
        return null;
      };
      const a = sp(wa, v), b = sp(wb, vb);
      wind = a !== null && b !== null ? a * (1 - s) + b * s : a ?? b;
    }
    if (wind === null) {
      // a labelled proxy, not a measurement: a light breeze, more when rain is falling on the observer
      wind = 3 + 6 * rain + 0.2 * this.manifest.storm.speed_ms;
    }
    // inside the heavy rain of the wall the wind roars: its level rises with the echo at the camera
    if (flight && this.windSource === 'retrieved') wind *= 0.6 + 0.4 * rain;
    return { wind, rain };
  }

  /** Render the soundscape of the observer camera offline; returns 16-bit stereo PCM as base64 plus a report. */
  async renderAudio(sampleRate = 48000): Promise<{ pcm: string; sampleRate: number; frames: number; peak: number; thunders: { flash: number; tFlash: number; arrival: number; delay: number; distKm: number; kind: string; source: string }[]; windMs: { min: number; max: number }; windSource: string }> {
    if (!this.pov) throw new Error('sound is rendered for the observer and flight cameras');
    const fps = this.settings.fps, dur = this.settings.duration;
    const nL = Math.ceil(dur * fps) + 1;
    const x = new Float32Array(nL), y = new Float32Array(nL), z = new Float32Array(nL), yaw = new Float32Array(nL), pit = new Float32Array(nL);
    for (let i = 0; i < nL; i++) {
      const p = this.povAt(i / fps);
      x[i] = p.x; y[i] = p.y; z[i] = p.zKm; yaw[i] = p.yawDeg; pit[i] = p.pitchDeg;
    }
    const dtS = 0.1;
    const nS = Math.floor(dur / dtS) + 1;
    const wind = new Float32Array(nS), rain = new Float32Array(nS);
    for (let i = 0; i < nS; i++) {
      const c = await this.observerConditions(i * dtS);
      wind[i] = c.wind;
      rain[i] = c.rain;
    }
    const input: SoundInput = {
      duration: dur, listener: { dt: 1 / fps, x, y, z, yawDeg: yaw, pitchDeg: pit },
      windMs: wind, rain, dtSeries: dtS, flashes: this.flashes, compress: this.settings.soundCompress ?? 8, seed: this.settings.lightningSeed ?? 7,
    };
    const res = await renderSoundscape(input, sampleRate);
    const n = res.left.length;
    const pcm = new Int16Array(n * 2);
    for (let i = 0; i < n; i++) {
      pcm[i * 2] = Math.max(-32768, Math.min(32767, Math.round(res.left[i] * 32767)));
      pcm[i * 2 + 1] = Math.max(-32768, Math.min(32767, Math.round(res.right[i] * 32767)));
    }
    const bytes = new Uint8Array(pcm.buffer);
    let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    const flashById = new Map(this.flashes.map((f) => [f.id, f]));
    const thunders = res.thunders.map((th) => {
      const f = flashById.get(th.flashId)!;
      return { flash: th.flashId, tFlash: f.t, arrival: th.arrival, delay: th.arrival - f.t, distKm: th.distKm, kind: f.kind, source: f.source };
    });
    return { pcm: btoa(bin), sampleRate, frames: n, peak: res.peak, thunders, windMs: { min: Math.min(...wind), max: Math.max(...wind) }, windSource: this.windSource };
  }

  /** Flashes of the clip (time, kind, source, strength): for reports and tests. */
  flashList(): { id: number; t: number; kind: string; source: string; energy: number; origin: number[]; strokes: number[] }[] {
    return this.flashes.map((f) => ({ id: f.id, t: f.t, kind: f.kind, source: f.source, energy: f.energy, origin: [...f.origin], strokes: f.strokes.map((q) => f.t + q.dt) }));
  }

  /** What the page reports about the clip (lightning, flow, wind source). */
  report() {
    return {
      lightning: { source: this.lightningSource, flashes: this.flashes.length, kinds: { CG: this.flashes.filter((f) => f.kind === 'CG').length, IC: this.flashes.filter((f) => f.kind === 'IC').length } },
      flow: { winds: this.windSource, steps: this.manifest.steps.length },
      observer: this.observer ? this.observerInfo() : null,
      cyclone: this.settings.camera === 'flight' ? { eyeRadiusKm: this.eyeR, windStats: this.windStats } : null,
      extras: this.ex?.report?.() ?? null,
    };
  }

  dispose(): void {
    this.renderer?.dispose();
    this.renderer = null;
  }
}
