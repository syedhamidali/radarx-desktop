/**
 * Offline renderer of a storm-centred 3D clip, built on the app's own 3D viewer code
 * (CartesianRenderer: the same ray-marched volume, transfer function, lighting, map ground and
 * overlay as the 3D tab).
 *
 * It is driven frame by frame by a script (tools/storm_clip/render_clip.py): every call of
 * `renderFrame(i)` draws ONE complete full-quality frame for the clip time i / fps (no real-time
 * clock anywhere), after which the script takes the pixels. The inputs are a data bundle written by
 * tools/storm_clip/clipdata.py: several time steps of a merged 3D reflectivity mosaic, the tracked
 * storm and the radar sites.
 *
 *  - Time: the cloud is interpolated between the observed volumes. The earlier volume is carried
 *    along the storm motion (measured from the tracked cell centre) and the later one is carried back,
 *    then they are cross-faded. The clip says so on screen; it is interpolation, not new observations.
 *  - Storm: the camera target follows the tracked cell centre (Hermite curve through the
 *    observations) and the volume / ground are cropped to a box around it.
 *  - Camera: keyframes (clipCamera.ts), eased.
 *  - No winds, no flow: the clip shows the reflectivity cloud over the map.
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
import { buildRealisticTf, lookFor, realisticSample } from './realisticStyle';
import { sunLight, type SunLight } from './sunPosition';
import {
  cameraAt, stormAt, dataTimeAt, locate, morphShifts, intervalMotion, utcLabel, stormChoreography, trackFrame, lagTarget,
  type CamKey,
} from './clipCamera';

export interface ClipManifest {
  version: number;
  title: string;
  case: string;
  variable: string;
  units: string;
  origin: { lat: number; lon: number };
  crop_half_km: number;
  steps: { index: number; time_utc: string; t_s: number; files: Record<string, { name: string; offset_s: number }>; mosaic: { header: string; bin: string } }[];
  storm: { track_km: [number, number][]; track_lonlat: [number, number][]; motion_ms: [number, number]; speed_ms: number; heading_deg: number };
  sites: any[];
}

export interface ClipSettings {
  fps: number;
  duration: number;
  /** Vertical exaggeration of the scene. */
  vexag: number;
  /** Camera keyframes; default: stormChoreography(). */
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
  /** 'map': the flat map (default). 'realistic': terrain with satellite imagery. */
  ground?: 'map' | 'realistic';
  /** 'scientific': the colour scale (default). 'realistic': the stylised precipitation look with sun light. */
  stormStyle?: 'scientific' | 'realistic';
  /**
   * Where tiles come from: '' = the tile servers directly, or a base URL of a caching proxy that
   * serves /eox/{z}/{x}/{y} and /terrarium/{z}/{x}/{y} (render_clip.py does this, with a disk cache).
   */
  tileBase?: string;
}

const DEFAULTS: ClipSettings = { fps: 30, duration: 36, vexag: 4, targetLag: 0.3 };

export class StormClip {
  private renderer: CartesianRenderer | null = null;
  private ctx: CanvasRenderingContext2D;
  private manifest!: ClipManifest;
  private vols: CartVolume[] = [];
  private ts: number[] = [];
  private track: { t: number; x: number; y: number }[] = [];
  private keys: CamKey[] = [];
  private pair = -1;
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
  private tf: Uint8Array = new Uint8Array(1024);
  private tfShift = 0;
  settings: ClipSettings = { ...DEFAULTS };
  fatal = '';
  mapOk = false;
  /** State of the frame being drawn (for the banner). */
  private cur = { t: 0, td: 0, k: 0, s: 0 };

  constructor(private container: HTMLElement, private overlay: HTMLCanvasElement) {
    this.ctx = overlay.getContext('2d')!;
  }

  get frameCount(): number {
    return Math.round(this.settings.duration * this.settings.fps);
  }

  /** Load the bundle served at `baseUrl` (manifest.json and the binary volumes). */
  async load(baseUrl: string, settings: Partial<ClipSettings> = {}): Promise<{ steps: number; grid: number[]; title: string }> {
    this.settings = { ...DEFAULTS, ...settings };
    const get = async (u: string) => {
      const r = await fetch(`${baseUrl}/${u}`);
      if (!r.ok) throw new Error(`${u}: HTTP ${r.status}`);
      return r;
    };
    this.manifest = await (await get('manifest.json')).json();
    const m = this.manifest;
    this.vols = [];
    for (const st of m.steps) {
      const h = await (await get(st.mosaic.header)).json();
      const bytes = new Uint8Array(await (await get(st.mosaic.bin)).arrayBuffer());
      const v = decodeMosaicVolume(h, bytes);
      if (!v) throw new Error(`step ${st.index}: the volume does not match its header`);
      this.vols.push(v);
    }
    const g0 = this.vols[0];
    for (const v of this.vols) {
      if (v.nx !== g0.nx || v.ny !== g0.ny || v.nz !== g0.nz || v.x0 !== g0.x0 || v.y0 !== g0.y0 || v.dx !== g0.dx) {
        throw new Error('the time steps are not on the same grid');
      }
    }
    this.ts = m.steps.map((s) => s.t_s);
    this.track = m.storm.track_km.map((p, i) => ({ t: this.ts[i], x: p[0], y: p[1] }));
    this.keys = this.settings.keys ?? stormChoreography(this.settings.duration, m.storm.heading_deg, 4);
    this.fullBox = gridBox(g0);
    // The shown box is FIXED around the middle of the track (the storm travels across it and past the
    // map's landmarks); it is wide enough that the storm stays well inside for the whole clip.
    const fr = trackFrame(this.track, m.crop_half_km);
    this.anchor = fr.anchor;
    this.cropHalf = fr.cropHalf;
    return { steps: this.vols.length, grid: [g0.nx, g0.ny, g0.nz], title: m.title };
  }

  /** Create the renderer, the transfer function and the ground map. */
  async prepare(mapProvider = 'osm'): Promise<void> {
    const m = this.manifest;
    const g0 = this.vols[0];
    const r = new CartesianRenderer(this.container, {
      onFatal: (msg) => { this.fatal = msg; },
      onFrame: (f) => this.drawOverlay(f),
    });
    this.renderer = r;
    r.enterExport();

    // the same default transfer function as the 3D tab (colours of the colormap, opacity curve of
    // reflectivity, slid for the strength of the scene)
    const hist = new Float64Array(256);
    for (const v of this.vols) for (let i = 0; i < v.data.length; i += 3) hist[v.data[i]]++;
    let sum = 0;
    for (let i = 1; i < 256; i++) sum += hist[i];
    let p995 = g0.vmax;
    let acc = 0;
    for (let i = 1; i < 256; i++) {
      acc += hist[i];
      if (acc >= 0.995 * sum) { p995 = g0.vmin + ((i - 1) / 254) * (g0.vmax - g0.vmin); break; }
    }
    this.tfShift = adaptiveShiftDb(p995);
    const realisticStorm = this.settings.stormStyle === 'realistic';
    this.tf = realisticStorm
      ? buildRealisticTf(g0.vmin, g0.vmax, 0.35)
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
    await r.setVolumePair(this.vols[0], this.vols[1]);
    this.pair = 0;

    // ground map: a high resolution image centred on the storm track, big enough for the whole clip
    const xs = this.track.map((p) => p.x), ys = this.track.map((p) => p.y);
    const cx = (Math.min(...xs) + Math.max(...xs)) / 2, cy = (Math.min(...ys) + Math.max(...ys)) / 2;
    const half = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys)) / 2 + m.crop_half_km + 14;
    // lat / lon of that centre (inverse of the spherical AEQD offset, by bisection on the forward map)
    const [lat, lon] = this.latLonOf(cx, cy);
    if (this.settings.ground === 'realistic') {
      await this.prepareRealisticGround(lat, lon, half + 35, [cx, cy]);
      return;
    }
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
      this.mapOk = true;
    } else {
      r.setParams({ groundMode: 'grid' });
    }
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

  /** The state of clip time `t`: data time, interval and position inside it, storm centre, camera. */
  stateAt(t: number) {
    const td = dataTimeAt(t, this.settings.duration, this.ts[0], this.ts[this.ts.length - 1]);
    const { i: k, s } = locate(this.ts, td);
    const [cx, cy] = stormAt(this.track, td);
    // camera target: the middle of the track plus a fraction of the storm's displacement from it
    const lag = this.settings.targetLag ?? 0.3;
    const [tx, ty] = lagTarget(this.anchor, [cx, cy], lag);
    return { t, td, k, s, cx, cy, tx, ty, cam: cameraAt(this.keys, t) };
  }

  /** Light of the realistic look at data time `td` (s): the real sun, or the documented dusk light after sunset. */
  private updateLight(td: number): void {
    const o = this.manifest.origin;
    const when = new Date(new Date(this.manifest.steps[0].time_utc).getTime() + td * 1000);
    const key = `${Math.round(when.getTime() / 60000)}`;
    if (key === this.sunKey && this.sun) return;
    this.sunKey = key;
    this.sun = sunLight(when, o.lat, o.lon);
    this.renderer!.setRealistic(lookFor(this.sun, this.lookSteps));
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
    return (this.vols[0]?.sites ?? []).map((q: any) => [q.name ?? q.id ?? '', q.x, q.y]);
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
    if (st.k !== this.pair) {
      await r.setVolumePair(this.vols[st.k], this.vols[st.k + 1]);
      this.pair = st.k;
    }
    const m = intervalMotion(this.track, st.k);
    const { shiftA, shiftB } = morphShifts(m, this.ts[st.k + 1] - this.ts[st.k], st.s);
    r.setMorph(st.s, shiftA, shiftB);
    const half = this.cropHalf;
    const [ax, ay] = this.anchor;
    this.crop = { xMin: ax - half, xMax: ax + half, yMin: ay - half, yMax: ay + half, zMin: this.fullBox.zMin, zMax: this.fullBox.zMax };
    r.setCropBox(this.crop);
    if (this.settings.stormStyle === 'realistic') this.updateLight(st.td);
    this.cur = { t: st.t, td: st.td, k: st.k, s: st.s };
    const ve = this.settings.vexag;
    r.exportRender({
      theta: st.cam.theta, phi: st.cam.phi, dist: st.cam.dist,
      target: [st.tx + st.cam.dx, st.ty + st.cam.dy, st.cam.dz * ve],
    });
    this.drawBanner();
  }

  // ── Overlay ─────────────────────────────────────────────────────────────

  /** Frame, compass letters, radar sites, height axis and scale bar (the 3D tab's overlay). */
  private drawOverlay(f: CamFrame): void {
    const c = this.overlay;
    const crop = this.crop;
    if (!crop) return;
    const cam = { eye: f.eye, fwd: f.fwd, right: f.right, up: f.up, tanX: f.tanX, tanY: f.tanY, w: c.width, h: c.height };
    drawCartesianOverlay(this.ctx, cam, {
      box: crop, vexag: this.settings.vexag, sites: this.vols[0].sites, lightScene: false, showFrame: this.settings.ground !== 'realistic',
      showHeightAxis: false, showExaggeration: false,
    });
    drawOffBoxSites(this.ctx, cam, crop, this.vols[0].sites, false);
    this.lastCam = cam;
    this.drawTrack(cam);
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

  private drawBanner(): void {
    const ctx = this.ctx;
    const W = this.overlay.width, H = this.overlay.height;
    const u = H / 1080;
    const m = this.manifest;
    const t0 = m.steps[0].time_utc;
    const first = this.ts[0], last = this.ts[this.ts.length - 1];
    const { td, k, s } = this.cur;
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

    // title and time
    const pad = 34 * u;
    text(this.settings.title ?? m.title, pad, pad + 34 * u, 38, 'rgba(255,255,255,0.98)', 700);
    text(this.settings.subtitle ?? 'merged 3-D radar reflectivity', pad, pad + 64 * u, 19, 'rgba(200,215,240,0.95)', 500);
    text(utcLabel(t0, td), pad, pad + 128 * u, 58, 'rgba(255,255,255,0.99)', 700);
    const date = new Date(new Date(t0).getTime() + td * 1000);
    const lapse = (last - first) / Math.max(1, this.settings.duration - 3);
    text(`${date.toUTCString().slice(5, 16)} · time-lapse ×${lapse.toFixed(0)}`, pad, pad + 156 * u, 19, 'rgba(200,215,240,0.95)', 500);
    const [sx0, sy0] = stormAt(this.track, first);
    const [sx1, sy1] = stormAt(this.track, td);
    const moved = Math.hypot(sx1 - sx0, sy1 - sy0);
    text(`storm centre moved ${moved.toFixed(1)} km since ${utcLabel(t0, 0, false)}`, pad, pad + 184 * u, 19, 'rgba(255,235,150,0.98)', 600);

    // progress bar with the observation times
    const lift = this.settings.ground === 'realistic' || this.settings.stormStyle === 'realistic' ? 34 : 0;
    const bw = W * 0.56, bx = (W - bw) / 2, by = H - (92 + lift) * u;
    const px = (t: number) => bx + ((t - first) / (last - first)) * bw;
    ctx.fillStyle = 'rgba(8,12,22,0.55)';
    ctx.fillRect(bx - 16 * u, by - 44 * u, bw + 32 * u, 112 * u);
    ctx.fillStyle = 'rgba(255,255,255,0.22)';
    ctx.fillRect(bx, by, bw, 4 * u);
    ctx.fillStyle = 'rgba(120,200,255,0.95)';
    ctx.fillRect(bx, by, px(td) - bx, 4 * u);
    const near = this.ts.findIndex((t) => Math.abs(t - td) < 6);
    this.ts.forEach((t, i) => {
      const on = i === near;
      ctx.beginPath();
      ctx.arc(px(t), by + 2 * u, (on ? 9 : 6) * u, 0, Math.PI * 2);
      ctx.fillStyle = on ? 'rgba(255,214,90,1)' : td >= t ? 'rgba(120,200,255,1)' : 'rgba(255,255,255,0.8)';
      ctx.fill();
      text(utcLabel(t0, t, false).replace(' UTC', ''), px(t), by - 14 * u, 17, on ? 'rgba(255,214,90,1)' : 'rgba(220,230,250,0.95)', 600, 'center');
    });
    ctx.beginPath();
    ctx.arc(px(td), by + 2 * u, 8 * u, 0, Math.PI * 2);
    ctx.fillStyle = '#fff';
    ctx.fill();
    const motion = `${m.storm.speed_ms.toFixed(0)} m/s toward ${m.storm.heading_deg.toFixed(0)}°`;
    const status = near >= 0 ? 'OBSERVED VOLUME' : 'INTERPOLATED between observed volumes';
    text(status, bx, by + 34 * u, 19, near >= 0 ? 'rgba(255,214,90,1)' : 'rgba(220,230,250,0.98)', 700);
    text(
      near >= 0 ? `${Object.keys(m.steps[near].files).join(' ')} · nearest volume of each radar` : `carried along the storm motion (${motion}) and cross-faded; not new observations`,
      bx, by + 58 * u, 16, 'rgba(200,215,240,0.95)', 500,
    );
    void k; void s;

    // colour bar (the scientific scale) or, in the realistic look, what a dBZ looks like as a shaft
    const cbx = W - pad - 22 * u, cby = H * 0.28, cbh = H * 0.36;
    const lut = COLORMAP_DATA['NWSRef'];
    const stylised = this.settings.stormStyle === 'realistic';
    if (stylised) {
      ctx.fillStyle = 'rgba(70,82,110,0.9)';
      ctx.fillRect(cbx, cby, 22 * u, cbh);
    }
    for (let y = 0; y < cbh; y++) {
      const i = Math.min(255, Math.round((1 - y / cbh) * 255));
      if (stylised) {
        const dbz = this.vols[0].vmin + (i / 255) * (this.vols[0].vmax - this.vols[0].vmin);
        const smp = realisticSample(dbz);
        ctx.fillStyle = `rgba(${Math.round(smp.rgb[0] * 255)},${Math.round(smp.rgb[1] * 255)},${Math.round(smp.rgb[2] * 255)},${(1 - Math.exp(-smp.sigma * 5)).toFixed(3)})`;
      } else {
        ctx.fillStyle = `rgb(${lut[i * 3]},${lut[i * 3 + 1]},${lut[i * 3 + 2]})`;
      }
      ctx.fillRect(cbx, cby + y, 22 * u, 1.5);
    }
    ctx.strokeStyle = 'rgba(255,255,255,0.5)';
    ctx.lineWidth = 1;
    ctx.strokeRect(cbx, cby, 22 * u, cbh);
    const v0 = this.vols[0];
    for (const dbz of [0, 20, 40, 60]) {
      const f = (dbz - v0.vmin) / (v0.vmax - v0.vmin);
      text(String(dbz), cbx - 8 * u, cby + cbh * (1 - f) + 6 * u, 16, 'rgba(235,240,255,0.97)', 600, 'right');
    }
    text('dBZ', cbx + 11 * u, cby - 12 * u, 17, 'rgba(235,240,255,0.97)', 700, 'center');

    // realistic look: what is stylised, and the attributions that must stay on screen
    if (this.settings.ground === 'realistic' || stylised) {
      const l2: string[] = [];
      if (stylised) {
        l2.push(this.sun?.stylised
          ? `Stylised lighting, not a photograph: dusk light at the sunset azimuth (the real sun was ${Math.round(Math.abs(this.sun.realElevationDeg))}° ${this.sun.realElevationDeg < 0 ? 'below' : 'above'} the horizon)`
          : `Stylised rendering, not a photograph (${this.sun?.label ?? 'sun light'})`);
      }
      if (this.settings.ground === 'realistic') l2.push(`vertical scale ×${this.settings.vexag} for terrain and storm (relief exaggerated)`);
      text(l2.join(' · '), W / 2, H - 38 * u, 15, 'rgba(235,240,255,0.97)', 600, 'center');
      const l1 = [
        this.settings.ground === 'realistic' ? IMAGERY_EOX_S2.attribution : '',
        this.settings.ground === 'realistic' ? TERRAIN_ATTRIBUTION : '',
        'Radar: NEXRAD Level II (NOAA)',
      ].filter(Boolean).join(' · ');
      text(l1, W / 2, H - 14 * u, 12, 'rgba(215,225,245,0.9)', 500, 'center');
    } else {
      text(`Height shown up to ${this.fullBox.zMax.toFixed(0)} km · vertical scale ×${this.settings.vexag}`, W - pad, H - 20 * u, 15, 'rgba(200,215,240,0.9)', 500, 'right');
    }
  }

  dispose(): void {
    this.renderer?.dispose();
    this.renderer = null;
  }
}
