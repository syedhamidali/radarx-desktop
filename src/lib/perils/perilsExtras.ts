/**
 * The PERiLS movie as a `ClipExtras` of the storm-clip renderer: loads the converted bundle
 * (perils.json, timeline.json, camera_path.json), and per frame sets the ground (base map + MRMS),
 * the lightning light, draws the scene annotations and the heads-up display. See docs/PERILS_MOVIE.md.
 *
 * Honesty rules that live here: the radar volume (drawn by the renderer) is the truth for echo; real
 * LMA sources are drawn at their measured places and times; layers appear only where the bundle has
 * their data; a frame between two observed volumes says INTERPOLATED; stylised things say STYLISED.
 */
import '../styles/provenance.css';
import type { ClipApi, ClipExtras, ExtrasPose, FrameInfo, OverlayCam } from './clipExtras';
import { COLORMAP_DATA } from '../utils/colormaps';
import {
  type PerilsData, type ProvKind, type EnvSounding, pickEnvSounding, nearestSounding, observedStepAt, seriesAt, stationReadout, tornadoAt, rotationAt, interpWeights,
} from './perilsData';
import { Director, type CamEntry, type TimelineFile, nearestStation } from './perilsDirector';
import { LayerVis, crossfade, type LayerId } from './perilsLayers';
import { GroundComposer, contourSegments, type PixelFn } from './perilsGround';
import { decodeSurface, sampleGrid, blendFields, tempColour, coldPoolLevel, swathUpTo, type SurfaceMeta } from './perilsSurface';
import { compileFlashes, flashFrame, flashesInWindow, lightFrom, longestFlash, rateSeries, drawLma, LMA_DEFAULTS, type CompiledFlash, type FlashFrame, type LmaParams } from './lmaFx';
import { projRaw, drawRadars, drawStations, drawTornadoTrack, drawFunnel, drawRotation, drawTds, drawHail, drawZdrColumns, drawBalloons, drawIsotherms, drawWinds, drawColdPool, drawEventMarker, type SceneEnv } from './perilsScene';
import { drawStripCharts, drawDsdInset, drawTimeHeight, drawRainStreaks } from './perilsInsets';
import { type HudModel, drawCompass, drawTopLeft, drawLmaPanel, drawEnvPanel, type EnvPanel, drawStationCard, drawTornadoCard, drawCaption, drawTimeline, drawLegend, drawEndCard, endCardAlpha, drawGroundKey } from './perilsHud';
import { activeCaption, captionAlpha, captionOf, scheduleCaptions, type ScheduledCaption, type ScienceEvent } from './perilsCaptions';
import { surfaceParcel } from './perilsThermo';
import { provColour } from './perilsStyle';
import { fmtOffset, clockUtc } from './perilsData';

export interface PerilsOptions {
  /** Switch layers on (1) or off (0) for the whole film. */
  layers?: Partial<Record<LayerId, number>>;
  lma?: Partial<LmaParams>;
  lmaColourBy?: 'time' | 'power';
  /** Title and subtitle at the top left (default: the bundle's). */
  title?: string;
  subtitle?: string;
  /** Cloud base height (km) of the stylised funnel. */
  cloudBaseKm?: number;
  /** Fov of the first-person camera, degrees. */
  fov?: number;
}

async function getJson<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return (await r.json()) as T;
}

export class PerilsExtras implements ClipExtras {
  readonly data: PerilsData;
  readonly director: Director;
  private base: string;
  private opts: PerilsOptions;
  private vis: LayerVis;
  private api!: ClipApi;
  private ground: GroundComposer | null = null;
  private mrmsCodes: (Uint8Array | null)[] = [];
  private azCodes: (Uint8Array | null)[] = [];
  private swathCache = new Map<number, Uint8Array | null>();
  private surf: (Record<string, Float32Array> | null)[] = [];
  private tRange: [number, number] = [10, 30];
  private coldLevel = NaN;
  private flashes: CompiledFlash[] = [];
  private maxDur = 0;
  private firstTimes: number[] = [];
  private lma: LmaParams;
  private lmaNow: FlashFrame = { sprites: [], links: [], active: 0 };
  private events: ScienceEvent[] = [];
  private sched: ScheduledCaption[] = [];
  private lut: Uint8Array = new Uint8Array(768);
  private frameNo = 0;
  private insetEl: HTMLDivElement | null = null;
  private insetId = '';
  private insetUnmount: (() => void) | null = null;
  private pending: Promise<void> | null = null;
  private lastNearest: string | null = null;
  groundMissing = true;
  rainStreaks = 0;
  lmaDrawn = 0;
  maxSimultaneousFlashes = 0;

  constructor(data: PerilsData, director: Director, base: string, opts: PerilsOptions = {}) {
    this.data = data;
    this.director = director;
    this.base = base;
    this.opts = opts;
    this.vis = new LayerVis(director.tl.shots);
    for (const [k, v] of Object.entries(opts.layers ?? {})) this.vis.set(k as LayerId, v);
    this.lma = { ...LMA_DEFAULTS, ...(opts.lma ?? {}) };
    this.events = ((data.science as ScienceEvent[] | undefined) ?? []).slice().sort((a, b) => a.t_s - b.t_s);
    this.sched = scheduleCaptions(this.events, (td) => director.clipTimeOfData(td), director.duration);
    if (data.lma) {
      this.flashes = compileFlashes(data.lma.flashes, (td) => director.clipTimeOfData(td));
      this.maxDur = longestFlash(this.flashes);
      this.firstTimes = data.lma.flashes.map((f) => f.t0).sort((a, b) => a - b);
    }
  }

  static async load(base: string, opts: PerilsOptions = {}): Promise<PerilsExtras> {
    const [data, tl, cam] = await Promise.all([
      getJson<PerilsData>(`${base}/perils.json`),
      getJson<TimelineFile>(`${base}/timeline.json`),
      getJson<CamEntry[]>(`${base}/camera_path.json`),
    ]);
    return new PerilsExtras(data, new Director(tl, cam), base, opts);
  }

  // ── ClipExtras ────────────────────────────────────────────────────────────

  dataTime(t: number): number {
    return this.director.dataTime(t);
  }

  pose(t: number): ExtrasPose {
    const p = this.director.pose(t);
    return { x: p.x, y: p.y, zKm: p.zKm, yawDeg: p.yawDeg, pitchDeg: p.pitchDeg, vexag: p.vexag, fov: this.opts.fov };
  }

  cropBox(): { xMin: number; xMax: number; yMin: number; yMax: number } | null {
    const g = this.ground?.spec;
    return g ? { xMin: g.cx - g.half, xMax: g.cx + g.half, yMin: g.cy - g.half, yMax: g.cy + g.half } : null;
  }

  /**
   * The echo threshold of the stylised look changes with the shot (24 dBZ in the wide views, 36 in the tornado shot, where
   * the whole domain is raining and the strong cores must stand out of the haze). A display choice, said on screen; the
   * data are not changed. Quantised to 1 dBZ so that the transfer function is rebuilt rarely.
   */
  look(t: number): Record<string, number> | null {
    const c = crossfade(this.director.tl.shots, [24, 30, 36, 30, 28, 24], t);
    return { lowDbzCutoff: Math.round(c) };
  }

  async prepare(api: ClipApi): Promise<void> {
    this.api = api;
    this.lut = COLORMAP_DATA['NWSRef'];
    const g = (this.data.ground as { file?: string; half: number; cx: number; cy: number; size?: number } | undefined) ?? null;
    let img: ImageBitmap | null = null;
    if (g?.file) {
      try {
        const r = await fetch(`${this.base}/${g.file}`);
        if (r.ok) img = await createImageBitmap(await r.blob());
      } catch (e) {
        console.warn('[perils] ground image failed:', e);
      }
    }
    this.groundMissing = !img;
    const half = g?.half ?? this.guessHalf();
    const spec = { size: g?.size ?? 2048, half, cx: g?.cx ?? 0, cy: g?.cy ?? 0 };
    if (!img) img = await this.gridImage(spec.size, spec.half, spec.cx, spec.cy);
    this.ground = new GroundComposer(spec, img);
    // MRMS composites (small 8-bit fields)
    this.mrmsCodes = await Promise.all(
      this.data.mrms.map(async (m) => {
        if (!m) return null;
        const r = await fetch(`${this.base}/${m.bin}`);
        return r.ok ? new Uint8Array(await r.arrayBuffer()) : null;
      }),
    );
    // MRMS azimuthal shear (the rotation swath) and the surface analysis
    this.azCodes = await Promise.all(
      this.data.mrms.map(async (m) => {
        if (!m || !m.az) return null;
        const r = await fetch(`${this.base}/${m.az}`);
        return r.ok ? new Uint8Array(await r.arrayBuffer()) : null;
      }),
    );
    const surfMeta = (this.data.surface ?? []) as (SurfaceMeta | null)[];
    this.surf = await Promise.all(
      surfMeta.map(async (m) => {
        if (!m) return null;
        const r = await fetch(`${this.base}/${m.bin}`);
        return r.ok ? decodeSurface(m, new Uint8Array(await r.arrayBuffer())) : null;
      }),
    );
    let lo = Infinity;
    let hi = -Infinity;
    for (const f of this.surf) {
      const t = f?.temperature_c;
      if (!t) continue;
      for (const v of t) if (Number.isFinite(v)) {
        lo = Math.min(lo, v);
        hi = Math.max(hi, v);
      }
      if (!Number.isFinite(this.coldLevel)) this.coldLevel = coldPoolLevel(t, 2.0);
    }
    if (Number.isFinite(lo)) this.tRange = [lo, Math.max(hi, lo + 1)];
    this.composeGround({ t: 0, td: 0, k: 0, s: 0, vexag: 1, eye: [0, 0, 0], yawDeg: 0, pitchDeg: 0 });
    this.mountInset();
  }

  /** Ground box: the MRMS extent when there is one, else a generous square around the grid. */
  private guessHalf(): number {
    let h = 0;
    for (const m of this.data.mrms) if (m) h = Math.max(h, Math.abs(m.x0), Math.abs(m.y0), Math.abs(m.x0 + m.nx * m.dx), Math.abs(m.y0 + m.ny * m.dx));
    return Math.max(h + 10, 180);
  }

  /** A plain dark ground with a 50 km grid when the bundle has no base map (never imagery). */
  private async gridImage(size: number, half: number, cx: number, cy: number): Promise<ImageBitmap> {
    const c = document.createElement('canvas');
    c.width = c.height = size;
    const x = c.getContext('2d')!;
    x.fillStyle = 'rgb(20,24,34)';
    x.fillRect(0, 0, size, size);
    x.strokeStyle = 'rgba(120,150,185,0.22)';
    x.lineWidth = 1.5;
    const step = 50;
    for (let v = Math.ceil((cx - half) / step) * step; v <= cx + half; v += step) {
      const px = ((v - cx) / (2 * half) + 0.5) * size;
      x.beginPath();
      x.moveTo(px, 0);
      x.lineTo(px, size);
      x.stroke();
    }
    for (let v = Math.ceil((cy - half) / step) * step; v <= cy + half; v += step) {
      const py = (0.5 - (v - cy) / (2 * half)) * size;
      x.beginPath();
      x.moveTo(0, py);
      x.lineTo(size, py);
      x.stroke();
    }
    return createImageBitmap(c);
  }

  private composeGround(info: FrameInfo): void {
    if (!this.ground) return;
    const stepT = this.data.steps.map((s) => s.t_s);
    const w = interpWeights(stepT, info.td);
    const k = w.k;
    const mo = this.data.motion[k];
    const g = this.data.grid;
    const box = { xMin: g.x0, xMax: g.x0 + (g.nx - 1) * g.dx, yMin: g.y0, yMax: g.y0 + (g.ny - 1) * g.dx };
    const pick = (i: number) => (this.mrmsCodes[i] ? i : -1);
    // an MRMS analysis missing at a step falls back to the nearest one that exists
    const nearest = (i: number): number => {
      for (let d = 0; d < this.mrmsCodes.length; d++) for (const j of [i - d, i + d]) if (j >= 0 && j < this.mrmsCodes.length && pick(j) >= 0) return j;
      return -1;
    };
    const ia = nearest(k);
    const ib = nearest(k + 1);
    const rgba = this.ground.compose({
      metaA: ia >= 0 ? this.data.mrms[ia] : null, metaB: ib >= 0 ? this.data.mrms[ib] : null,
      codesA: ia >= 0 ? this.mrmsCodes[ia] : null, codesB: ib >= 0 ? this.mrmsCodes[ib] : null,
      s: w.s, dt: mo ? mo.dt_s : 1, motion: mo ? [mo.dx_km / mo.dt_s, mo.dy_km / mo.dt_s] : [0, 0],
      box, lut: this.lut, vmin: this.api.g0.vmin, vmax: this.api.g0.vmax, gain: 0.6 * this.vis.at('mrms', info.t),
      under: this.surfaceLayer(k, w.s, this.vis.at('surfaceModels', info.t)),
      over: this.swathLayer(w.s < 0.5 ? k : k + 1, this.vis.at('rotation', info.t)),
    });
    this.api.renderer.setGround(this.ground.spec.size, rgba, this.ground.spec.half, [this.ground.spec.cx, this.ground.spec.cy]);
  }

  /** The surface temperature analysis (ANALYSIS) as a translucent ground layer, cross-faded between the two step analyses. */
  private surfaceLayer(k: number, s: number, gain: number): PixelFn | null {
    const metaA = (this.data.surface ?? [])[k] as SurfaceMeta | null;
    const metaB = (this.data.surface ?? [])[k + 1] as SurfaceMeta | null;
    const meta = metaA ?? metaB;
    const f = blendFields(this.surf[k]?.temperature_c ?? null, this.surf[k + 1]?.temperature_c ?? null, s);
    if (!meta || !f || gain <= 0.02) return null;
    const [lo, hi] = this.tRange;
    return (x, y) => {
      const v = sampleGrid(f, meta, x, y);
      if (!Number.isFinite(v)) return null;
      const c = tempColour(v, lo, hi);
      return [c[0], c[1], c[2], 0.26 * gain];
    };
  }

  /** The rotation swath so far (MRMS azimuthal shear, RETRIEVED): the maximum up to step `k`, yellow, opacity by strength. */
  private swathLayer(k: number, gain: number): PixelFn | null {
    const kk = Math.max(0, Math.min(k, this.azCodes.length - 1));
    if (gain <= 0.02 || !this.azCodes.some(Boolean)) return null;
    if (!this.swathCache.has(kk)) this.swathCache.set(kk, swathUpTo(this.azCodes, kk));
    const sw = this.swathCache.get(kk);
    const meta = this.data.mrms.find((m) => m);
    if (!sw || !meta) return null;
    return (x, y) => {
      const i = Math.round((x - meta.x0) / meta.dx);
      const j = Math.round((y - meta.y0) / meta.dx);
      if (i < 0 || j < 0 || i >= meta.nx || j >= meta.ny) return null;
      const c = sw[j * meta.nx + i];
      if (c < 40) return null; // below about 6e-3 s-1 nothing is drawn
      return [255, 225, 70, Math.min(0.75, 0.25 + (c - 40) / 300) * gain];
    };
  }

  /** Segments of the cold-pool edge at the current time, from the blended surface temperature analysis; [] without a cold pool. */
  private coldPoolSegments(td: number): number[][] {
    if (!Number.isFinite(this.coldLevel)) return [];
    const w = interpWeights(this.data.steps.map((q) => q.t_s), td);
    const meta = ((this.data.surface ?? [])[w.k] ?? (this.data.surface ?? [])[w.k + 1]) as SurfaceMeta | null;
    const f = blendFields(this.surf[w.k]?.temperature_c ?? null, this.surf[w.k + 1]?.temperature_c ?? null, w.s);
    if (!meta || !f) return [];
    return contourSegments(f, meta.nx, meta.ny, meta.x0, meta.y0, meta.dx, this.coldLevel);
  }

  beforeFrame(info: FrameInfo): void {
    this.frameNo++;
    this.lmaNow = this.flashes.length ? flashFrame(this.flashes, info.t, this.maxDur, this.lma) : { sprites: [], links: [], active: 0 };
    this.maxSimultaneousFlashes = Math.max(this.maxSimultaneousFlashes, this.lmaNow.active);
    this.composeGround(info);
  }

  flashLight(info: FrameInfo): { points: { x: number; y: number; z: number; i: number }[]; sky: number } | null {
    return lightFrom(this.lmaNow, info.vexag);
  }

  private nearStep(td: number): number {
    const st = this.data.steps.map((s) => s.t_s);
    let best = 0;
    for (let i = 1; i < st.length; i++) if (Math.abs(st[i] - td) < Math.abs(st[best] - td)) best = i;
    return best;
  }

  private nearestId(info: FrameInfo): string | null {
    const eye: [number, number, number] = [info.eye[0], info.eye[1], info.eye[2] / Math.max(info.vexag, 1e-3)];
    const s = nearestStation(this.data.stations, eye);
    this.lastNearest = s ? s.id : null;
    return this.lastNearest;
  }

  drawScene(ctx: CanvasRenderingContext2D, cam: OverlayCam, info: FrameInfo): void {
    const u = cam.h / 1080;
    this.lastTanX = cam.tanX;
    const centre = this.stormCentre(info.td);
    const env: SceneEnv = {
      ctx, cam, u, ve: info.vexag, td: info.td, tc: info.t, data: this.data, nearestStation: this.nearestId(info), centre, nearStep: this.nearStep(info.td),
    };
    const v = (id: LayerId) => this.vis.at(id, info.t);
    drawIsotherms(env, v('isotherms'), centre, 55);
    drawTornadoTrack(env, v('tornadoTrack'));
    drawRadars(env, v('radars'));
    drawBalloons(env, v('balloon'));
    drawZdrColumns(env, v('zdr'));
    drawHail(env, v('hail'));
    drawWinds(env, v('flow'));
    drawColdPool(env, v('surfaceModels'), this.coldPoolSegments(info.td));
    // where the science event on screen happened (when the data gives a place)
    const sc = activeCaption(this.sched, info.t);
    if (sc && typeof sc.ev.x === 'number' && typeof sc.ev.y === 'number') {
      const cap = captionOf(sc.ev);
      drawEventMarker(env, captionAlpha(info.t, sc.t0, sc.hold), sc.ev.x, sc.ev.y, cap.title, provColour(cap.prov));
    }
    drawTds(env, v('rotation'));
    drawRotation(env, v('rotation'));
    drawStations(env, v('stations'));
    drawFunnel(env, v('funnel'), this.opts.cloudBaseKm ?? 1.0);
    this.lmaDrawn = drawLma(ctx, cam, this.lmaNow, projRaw, info.vexag, this.opts.lmaColourBy ?? this.lma.colourBy, 1);
  }

  private interp(tr: { t: number; x: number; y: number }[], t: number, k: 'x' | 'y'): number {
    if (tr.length === 1) return tr[0][k];
    let i = 0;
    while (i < tr.length - 2 && t > tr[i + 1].t) i++;
    const f = (t - tr[i].t) / (tr[i + 1].t - tr[i].t || 1);
    return tr[i][k] + (tr[i + 1][k] - tr[i][k]) * f;
  }

  // ── HUD ───────────────────────────────────────────────────────────────────

  private model(info: FrameInfo, W: number, H: number): HudModel {
    const d = this.data;
    const td = info.td;
    const stepT = d.steps.map((s) => s.t_s);
    const obs = observedStepAt(stepT, td, 3);
    const w = interpWeights(stepT, td);
    const mo = d.motion[Math.min(w.k, d.motion.length - 1)];
    const motionNote = mo ? `carried ${mo.fallback ? '(median motion) ' : ''}along the line motion ${Math.hypot(mo.u_ms, mo.v_ms).toFixed(0)} m/s toward ${((Math.atan2(mo.u_ms, mo.v_ms) * 180 / Math.PI + 360) % 360).toFixed(0)}° (phase correlation of the echo); not new observations` : 'blended; not new observations';
    const binS = 15;
    const win = stepT[stepT.length - 1] - stepT[0];
    const series = rateSeries(this.firstTimes, 0, win, binS);
    const eye: [number, number, number] = [info.eye[0], info.eye[1], info.eye[2] / Math.max(info.vexag, 1e-3)];
    const st = nearestStation(d.stations, eye);
    let station: HudModel['station'] = null;
    if (st) {
      const s = st.series ?? {};
      const ts = (s.t_s as number[] | undefined) ?? [];
      station = { st, ro: stationReadout(st, td), spark: ts.length > 1 ? { t: ts, v: (s.rain_rate_mmh as (number | null)[]) ?? [] } : null };
    }
    // tornado card
    let tornado: HudModel['tornado'] = null;
    const tor = d.tornadoes[0];
    if (tor) {
      const cur = tornadoAt(tor, td);
      let vrot: number | null = null;
      let radar: string | null = null;
      let res: number | null = null;
      let tds: boolean | null = null;
      for (const tr of d.rotation) {
        const p = rotationAt(tr, td, 90);
        if (p && (vrot === null || (typeof p.vrot_ms === 'number' && p.vrot_ms > vrot))) {
          vrot = typeof p.vrot_ms === 'number' ? p.vrot_ms : vrot;
          radar = tr.radar ?? null;
          res = tr.resolution_m ?? null;
          if (typeof p.tds === 'boolean') tds = p.tds;
        }
      }
      const tdsPts = d.tds[this.nearStep(td)];
      if (tds === null && tdsPts) tds = tdsPts.length > 0;
      tornado = { rating: tor.rating ?? 'Tornado', active: !!cur, vrotMs: vrot, radar, resolutionM: res, debris: tds, widthM: tor.width_m ?? null, interpolated: !!cur && cur.interpolated };
    }
    // caption: the science event on screen, else the feature in view
    const sc = activeCaption(this.sched, info.t);
    let caption: HudModel['caption'] = null;
    if (sc) caption = { ...captionOf(sc.ev), alpha: captionAlpha(info.t, sc.t0, sc.hold) };
    const nowFlash = this.lmaNow.active;
    let sees = '';
    if (tornado?.active) sees = `${tornado.rating} tornado on the ground (surveyed path)${tornado.vrotMs != null ? ` · radar rotation ${tornado.vrotMs.toFixed(0)} m/s` : ''}`;
    else if ((d.hail[this.nearStep(td)]?.n ?? 0) > 200 && this.vis.at('hail', info.t) > 0.5) sees = 'Hail core (radar hydrometeor classification)';
    else if (nowFlash > 0) sees = `Lightning: ${nowFlash} flash${nowFlash > 1 ? 'es' : ''} mapped now`;
    else sees = '';
    const sources = d.sources ?? [];
    const highUp = info.eye[2] / Math.max(info.vexag, 1e-3) > 3.0; // the ground colours matter from above
    const gc = (d.ground as { credit?: string } | undefined)?.credit;
    const credits = [sources.length ? `Data: ${sources.length} datasets and instruments, credited in the closing card` : '', gc ?? ''].filter(Boolean).join(' · ');
    return {
      data: d, title: this.opts.title ?? (d.title || 'PERiLS'), subtitle: this.opts.subtitle ?? (d.case || ''), td, tc: info.t, duration: this.director.duration, speedup: this.director.speedup(info.t), shotLabel: this.director.shotAt(info.t).label,
      observed: obs, interval: { k: w.k, s: w.s }, motionNote,
      flash: { perMin: flashesInWindow(this.firstTimes, td, 60), series, binS, total: this.firstTimes.length, nowIdx: Math.min(series.length - 1, Math.floor(td / binS)) },
      station, tornado, caption, sees, vis: (id) => this.vis.at(id as LayerId, info.t) * (1 - endCardAlpha(info.t, this.director.duration)), yawDeg: info.yawDeg, tanX: this.api ? this.lastTanX : 0.7, env: this.envPanel(td),
      legendKinds: ['observed', 'model', 'analysis', 'retrieved', 'interpolated', 'stylised'], credits, shots: this.director.tl.shots, layersOn: [], vexag: info.vexag,
      groundKey: {
        temp: highUp && this.surf.some(Boolean) && this.vis.at('surfaceModels', info.t) > 0.12 ? this.tRange : null,
        mrms: highUp && this.mrmsCodes.some(Boolean) && this.vis.at('mrms', info.t) > 0.12,
        swath: highUp && this.azCodes.some(Boolean) && this.vis.at('rotation', info.t) > 0.12,
      },
    };
  }

  private lastTanX = 0.7;

  /** Centre of the line at data time `td` (km), from the pseudo-track of the echo motion. */
  private stormCentre(td: number): [number, number] {
    const tr = this.data.track;
    return tr.length ? [this.interp(tr, td, 'x'), this.interp(tr, td, 'y')] : [0, 0];
  }

  /**
   * The environment panel: the numbers of the radiosonde nearest in time and place (engine diagnostics at its launch
   * site, OBSERVED; there is no model or reanalysis field in this bundle), or a flat set of values (the synthetic bundle).
   */
  private envPanel(td: number): EnvPanel | null {
    const env = this.data.environment as Record<string, unknown> | null;
    if (!env) return null;
    const fmt = (v: unknown, unit: string, d = 0) => (typeof v === 'number' && Number.isFinite(v) ? `${v.toFixed(d)} ${unit}` : null);
    const list = env.soundings as EnvSounding[] | undefined;
    if (Array.isArray(list)) {
      const e = pickEnvSounding(list, td, this.stormCentre(td));
      if (!e) return null;
      const rows = ([
        ['surface-based CAPE', fmt(e.sbcape, 'J/kg')],
        ['mixed-layer CAPE', fmt(e.mlcape, 'J/kg')],
        ['LCL', fmt(e.lcl_agl_m, 'm AGL')],
        ['0-6 km shear', fmt(e.shear_0_6km_ms, 'm/s')],
        ['0-1 km helicity', fmt(e.srh_0_1km_right_m2s2, 'm²/s²')],
        ['freezing level', typeof e.freezing_level_asl_m === 'number' ? `${(e.freezing_level_asl_m / 1000).toFixed(1)} km` : null],
      ] as [string, string | null][]).filter((r): r is [string, string] => r[1] !== null);
      const dt = e.launch_t_s - td;
      return { title: `${e.site} sonde ${clockUtc(this.data.time0, e.launch_t_s).slice(0, 5)} UTC`, prov: 'observed', rows, note: `launched ${fmtOffset(dt)} from now; not at the storm` };
    }
    const flat = ([
      ['CAPE', fmt(env.cape_jkg, 'J/kg')],
      ['CIN', fmt(env.cin_jkg, 'J/kg')],
      ['0-6 km shear', fmt(env.shear06_ms, 'm/s')],
      ['0-1 km SRH', fmt(env.srh01_m2s2, 'm²/s²')],
      ['freezing level', fmt(env.freezing_level_m, 'm')],
      ['LCL', fmt(env.lcl_m, 'm')],
    ] as [string, string | null][]).filter((r): r is [string, string] => r[1] !== null);
    const cls = (['model', 'analysis', 'observed', 'retrieved'].includes(String(env.class)) ? env.class : 'analysis') as ProvKind;
    return flat.length ? { title: 'environment', prov: cls, rows: flat } : null;
  }

  drawHud(ctx: CanvasRenderingContext2D, W: number, H: number, info: FrameInfo): void {
    const u = H / 1080;
    const m = this.model(info, W, H);
    drawCompass(ctx, W, u, info.yawDeg, this.lastTanX);
    drawTopLeft(ctx, u, m);
    let y = 28 * u;
    y = drawLmaPanel(ctx, W, u, m, m.vis('lmaPanel'));
    y = drawEnvPanel(ctx, W, u, y, m.env, m.vis('envPanel'));
    const yEnv = y;
    const win = this.data.steps[this.data.steps.length - 1].t_s;
    const ix = W - 34 * u - 330 * u;
    if (m.station) {
      const gf = this.data.science.filter((e) => e.kind === 'gust_front' || e.kind === 'pressure_jump').map((e) => e.t_s);
      const sa = m.vis('stripCharts');
      let h = drawStripCharts(ctx, ix, y, 330 * u, u, m.station.st, info.td, win, sa, gf);
      if (h) y += h + 10 * u;
      h = drawDsdInset(ctx, ix, y, 330 * u, u, m.station.st, info.td, m.vis('dsd'));
      if (h) y += h + 10 * u;
    }
    const prof = this.data.profiles;
    const th = prof.mrr;
    if (th && th.fields.dbz) {
      const h = drawTimeHeight(ctx, ix, y, 330 * u, u, 'MRR above RaDAPS', th, 'dbz', 'reflectivity, dBZ', info.td, win, m.vis('timeHeight'));
      if (h) y += h + 10 * u;
    }
    if (m.station && info.eye[2] / Math.max(info.vexag, 1e-3) < 0.05) {
      const ro = m.station.ro;
      this.rainStreaks = drawRainStreaks(ctx, W, H, u, ro.rain, ro.dm, info.t, m.vis('rainStreaks'));
    }
    drawStationCard(ctx, H, u, m, m.vis('stationCard'));
    const cardH = drawTornadoCard(ctx, u, m, 1, 280 * u);
    drawGroundKey(ctx, u, m, 280 * u + (cardH ? cardH + 10 * u : 0), m.vis('captions'));
    drawCaption(ctx, W, H, u, m);
    drawTimeline(ctx, W, H, u, m);
    drawLegend(ctx, W, H, u, m);
    drawEndCard(ctx, W, H, u, m, info.t, this.director.duration);
    this.updateInset(info, yEnv, m.vis('skewt'));
  }

  // ── Skew-T inset (a real Svelte component, in the page) ───────────────────

  private mountInset(): void {
    const el = document.createElement('div');
    el.style.cssText = 'position:fixed;right:34px;top:200px;z-index:3;opacity:0;pointer-events:none;';
    document.body.appendChild(el);
    this.insetEl = el;
  }

  private updateInset(info: FrameInfo, top: number, alpha: number): void {
    const el = this.insetEl;
    if (!el) return;
    const near = nearestSounding(this.data.soundings.filter((s) => s.levels), info.td, this.stormCentre(info.td));
    if (!near || alpha <= 0.02) {
      el.style.opacity = '0';
      return;
    }
    el.style.top = `${Math.round(top)}px`;
    el.style.opacity = String(alpha);
    const snd = near.snd;
    if (this.insetId !== snd.id) {
      this.insetId = snd.id;
      this.insetUnmount?.();
      this.pending = this.mountSounding(el, snd);
    }
    const off = near.offsetS;
    const when = `launched ${clockUtc(this.data.time0, snd.launch)} UTC · ${fmtOffset(off)} ${off < 0 ? '(before' : '(after'} the clip time at this moment)`;
    const whenEl = el.querySelector('.when');
    if (whenEl) whenEl.textContent = when;
  }

  private async mountSounding(el: HTMLDivElement, snd: PerilsData['soundings'][number]): Promise<void> {
    const [{ mount, unmount }, { default: Inset }] = await Promise.all([import('svelte'), import('../components/PerilsInset.svelte')]);
    const lv = snd.levels!;
    const first = lv.height_msl_m.find((v) => v != null) ?? 0;
    const diag = (snd.diagnostics ?? {}) as Record<string, any>;
    const sb = diag.parcels?.['surface-based'] ?? diag;
    const num = (v: unknown, unit: string, dg = 0) => (typeof v === 'number' && Number.isFinite(v) ? `${v.toFixed(dg)} ${unit}` : null);
    const rows: [string, string][] = ([
      ['CAPE', num(sb.cape ?? sb.cape_jkg, 'J/kg')],
      ['CIN', num(sb.cin ?? sb.cin_jkg, 'J/kg')],
      ['LCL', num(sb.lcl_agl_m, 'm AGL')],
      ['LFC', num(sb.lfc_agl_m, 'm AGL')],
      ['EL', num(sb.el_agl_m, 'm AGL')],
      ['0-6 km shear', num(diag.shear?.['0-6km']?.magnitude_ms ?? diag.shear06_ms, 'm/s')],
      ['0-1 km SRH', num(diag.srh?.['0-1km']?.srh ?? diag.srh01_m2s2, 'm²/s²')],
    ] as [string, string | null][]).filter((r): r is [string, string] => r[1] !== null);
    const data = {
      station: snd.site, station_name: snd.site, latitude: null, longitude: null, launch_time: snd.launchIso, source: 'PERiLS sounding', format: '', file: '',
      surface_height_m: first, notes: [], drift_note: '', drift: null, levels: { ...lv, n_levels: lv.pressure_hpa.length, thinned: false },
      provenance: { kind: 'observed' as const, source: snd.site }, diagnostics: diag,
    };
    const comp = mount(Inset, { target: el, props: { data, parcel: surfaceParcel(lv), title: `${snd.site} sounding`, when: '', rows } });
    this.insetUnmount = () => {
      void unmount(comp);
    };
  }

  async settle(): Promise<void> {
    if (this.pending) {
      await this.pending;
      this.pending = null;
    }
    const { tick } = await import('svelte');
    await tick();
    await new Promise<void>((r) => requestAnimationFrame(() => r()));
  }

  report(): Record<string, unknown> {
    return {
      frames: this.frameNo, flashes: this.firstTimes.length, lmaSources: this.data.lma?.nSources ?? 0, maxSimultaneousFlashes: this.maxSimultaneousFlashes,
      stations: this.data.stations.length, groundMissing: this.groundMissing, speedup: { mean: this.director.tl.speedup_mean, min: this.director.tl.speedup_min, max: this.director.tl.speedup_max },
      nearestStation: this.lastNearest,
    };
  }
}

export type { ProvKind };
export { seriesAt };
