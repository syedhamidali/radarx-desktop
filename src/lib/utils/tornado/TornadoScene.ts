/**
 * TornadoScene: a physically based, simulated tornado for a first-person camera (WebGL2).
 *
 *   const scene = new TornadoScene({ canvas, width: 1920, height: 1080 });
 *   scene.setSeedData({ vt, coreRadius, width, ef, track, thermo, lightningFlashes, meso, echo });
 *   scene.setShot(t0, t1);          // data-clock span of the shot (debris warm-up, landscape focus)
 *   await scene.loadGround();       // satellite imagery + terrain from the tile cache if present
 *   scene.setTime(t);               // data seconds since the window start; deterministic
 *   scene.render({ pos, target, fovDeg });   // camera in local metres east / north / up
 *
 * This is a SIMULATION seeded by observations (survey: rating, width, path, time; radar couplet; station
 * T / Td; lightning positions and times), not a video of the event. See docs/TORNADO_SIM.md for what is
 * observed and what is modelled.
 *
 * Passes: (1) opaque scene (ground, trees, buildings, debris, near rain) into an HDR target with a
 * distance buffer; (2) a volume ray-march (funnel, cloud, rain, dust, haze) against that distance;
 * (3) bloom and film response to the canvas.
 */

import {
  resolveSeed, axisAt, intensityAt, rotationPeriod, type SeedData, type ResolvedSeed, fromLocal,
} from './seed.ts';
import { pressureShape, tangential, omega, coreRadius } from './vortexField.ts';
import { vortexOnlyRadius, heightDeficit } from './funnel.ts';
import { DebrisSim, DEBRIS_CLASSES, windField } from './debris.ts';
import { buildNoiseVolume } from './noise3d.ts';
import { buildLandscape, forestFromImagery, placeFeatures, clearAround, type Landscape } from './landscape.ts';
import { loadGroundTiles, type GroundTileResult } from './groundTiles.ts';
import { treeState, type TreeState } from './treeDamage.ts';
import { makeFlashEnv, flashLight, flashEnd, type FlashEnv } from './flashes.ts';
import { sunPosition } from '../sunPosition.ts';
import { makeEchoSeries, echoRainAt, type EchoSeries } from './echoField.ts';
import {
  program, buffer, makeTex2D, basis, viewProj, type Program, type Vec3,
} from './glUtil.ts';
import * as S from './shaders.ts';

export interface TornadoCamera {
  pos: Vec3;
  target: Vec3;
  fovDeg: number;
  roll?: number;
  /** Camera velocity (m/s), for the rain streaks' motion blur. */
  vel?: Vec3;
  /** Exposure multiplier of the shot (a darker, moodier grade for the view from inside the vortex). */
  exposure?: number;
}

export type AmbientPreset = 'dusk' | 'night' | 'overcast';

export interface SceneOptions {
  canvas?: HTMLCanvasElement | OffscreenCanvas;
  width?: number;
  height?: number;
  ambient?: AmbientPreset;
  /** Base URL of the tile server ('' = same origin). */
  tileBase?: string;
  /** Try to load imagery and terrain tiles. */
  useTiles?: boolean;
  debrisCount?: number;
  /** Internal render scale (0.5 = half resolution) for drafts. */
  renderScale?: number;
  /** Wall-cloud lowering (m) and radius (m): modelled. */
  wallCloud?: { lowerM: number; radiusM: number };
  /** Half width (m) of the landscape and of the ground mosaic around the shot's focus (default 3600) and its raster size (default 1536). */
  landHalf?: number;
  landSize?: number;
  /** How far around the focus trees are generated (m, default 2200). */
  treeRadius?: number;
}

export interface FrameStats {
  ms: number;
  frame: number;
  treeCount: number;
  debrisAirborne: number;
  gpuMB: number;
  jsHeapMB: number | null;
  active: boolean;
}

const AMBIENT: Record<AmbientPreset, { amb: Vec3; glow: number; fog: Vec3; exposure: number; label: string }> = {
  dusk: { amb: [0.092, 0.104, 0.128], glow: 0.7, fog: [0.046, 0.053, 0.064], exposure: 2.2, label: 'stylised dusk light' },
  night: { amb: [0.0016, 0.0020, 0.0030], glow: 0.0, fog: [0.0035, 0.0045, 0.0065], exposure: 2.6, label: 'night (physical light level)' },
  overcast: { amb: [0.14, 0.155, 0.18], glow: 0.2, fog: [0.16, 0.18, 0.21], exposure: 1.6, label: 'overcast day' },
};

/** A forward jump of the time longer than this (s) restarts the debris with a warm-up of `WARMUP_S` seconds before the new time. */
export const JUMP_S = 20;
export const WARMUP_S = 12;

/** Smoothing window of the flash light (s): the comfort limit on flicker. */
export const FLASH_SMOOTH_S = 0.24;

function mulberry(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export class TornadoScene {
  readonly canvas: HTMLCanvasElement | OffscreenCanvas;
  readonly gl: WebGL2RenderingContext;
  readonly width: number;
  readonly height: number;
  seed: ResolvedSeed | null = null;
  ambient: AmbientPreset;
  groundInfo: { mode: 'procedural' | 'imagery'; terrain: boolean; log: string[]; attribution: string[] } = { mode: 'procedural', terrain: false, log: [], attribution: [] };
  stats: FrameStats = { ms: 0, frame: 0, treeCount: 0, debrisAirborne: 0, gpuMB: 0, jsHeapMB: null, active: false };

  private opts: SceneOptions;
  private rw: number;
  private rh: number;
  private progs: Record<string, Program> = {};
  private noiseTex!: WebGLTexture;
  private landTex!: WebGLTexture;
  private landTexL!: WebGLTexture;
  private imageryTex!: WebGLTexture;
  private terrainTex!: WebGLTexture;
  private echoTex!: WebGLTexture;
  private sceneFbo!: WebGLFramebuffer;
  private sceneColor!: WebGLTexture;
  private sceneDist!: WebGLTexture;
  private sceneDepth!: WebGLRenderbuffer;
  private volFbo!: WebGLFramebuffer;
  private volColor!: WebGLTexture;
  private bloomFbo: WebGLFramebuffer[] = [];
  private bloomTex: WebGLTexture[] = [];
  private bw = 0;
  private bh = 0;
  private vaoEmpty!: WebGLVertexArrayObject;
  private groundVao!: WebGLVertexArrayObject;
  private groundIdx = 0;
  private debrisVao!: WebGLVertexArrayObject;
  private debrisDyn!: WebGLBuffer;
  private debrisStatic!: WebGLBuffer;
  private treeVao: WebGLVertexArrayObject | null = null;
  private treeIdxCount = 0;
  private treeInst: WebGLBuffer | null = null;
  private treeBend: WebGLBuffer | null = null;
  private treeN = 0;
  private boxVao: WebGLVertexArrayObject | null = null;
  private boxIdxCount = 0;
  private boxInst: WebGLBuffer | null = null;
  private boxN = 0;
  private rainVao!: WebGLVertexArrayObject;
  private rainSeed!: WebGLBuffer;
  private rainN = 5000;
  private flashSum = 0;
  private syncPx = new Uint8Array(4);
  /** Diagnostics: bit mask of the volume components drawn (63 = all). */
  mask = 63;
  /** Multiplier on the exposure (1 = the preset). */
  exposureMul = 1;
  /** Multiplier on the lightning illumination. */
  flashMul = 1;
  /** Multiplier on the extinction of the volume rain (1 = the physical value for the rain rate of the echo): a display choice, for seeing a funnel through heavy rain from far. */
  rainGain = 1;

  private land: Landscape | null = null;
  private landFocus: [number, number] = [0, 0];
  private terrain: { size: number; cx: number; cy: number; half: number; metres: Float32Array; ref: number } | null = null;
  private imagery = false;
  private echoGrid: [number, number, number, number] = [0, 0, 1, 1];
  private echoOn = false;
  private echoMean = 20;
  private echoSeries: EchoSeries | null = null;
  private echoRate: Float32Array | null = null;
  private echoBytes: Uint8Array | null = null;
  private echoT = NaN;

  private sim: DebrisSim | null = null;
  private simStart = 0;
  private debrisDynArr!: Float32Array;
  private flashes: FlashEnv[] = [];
  private t = 0;
  private treeStates: TreeState = { tilt: 0, dir: [1, 0], fall: 0, peak: 0 };
  private treeBendArr = new Float32Array(0);
  private treePine: Uint8Array = new Uint8Array(0);
  private frameNo = 0;
  private gpuBytes = 0;
  private gTable = new Float32Array(64);
  private sunInfo = { elevDeg: 0, azDeg: 270, label: '' };
  private glowDir: Vec3 = [-1, 0, 0];

  constructor(o: SceneOptions = {}) {
    this.opts = o;
    this.width = o.width ?? 1920;
    this.height = o.height ?? 1080;
    this.ambient = o.ambient ?? 'dusk';
    const rs = o.renderScale ?? 1;
    this.rw = Math.max(16, Math.round(this.width * rs));
    this.rh = Math.max(16, Math.round(this.height * rs));
    let canvas = o.canvas;
    if (!canvas) {
      canvas = document.createElement('canvas');
    }
    canvas.width = this.width;
    canvas.height = this.height;
    this.canvas = canvas;
    const gl = (canvas as HTMLCanvasElement).getContext('webgl2', { antialias: false, preserveDrawingBuffer: true, alpha: true, premultipliedAlpha: true, powerPreference: 'high-performance' }) as WebGL2RenderingContext | null;
    if (!gl) throw new Error('WebGL2 is not available');
    if (!gl.getExtension('EXT_color_buffer_float')) throw new Error('EXT_color_buffer_float is not available');
    this.gl = gl;
    this.build();
  }

  // ---- construction ------------------------------------------------------------------------------
  private tex2D(w: number, h: number, internal: number, format: number, type: number, data: ArrayBufferView | null, filter: number, wrap: number = this.gl.CLAMP_TO_EDGE): WebGLTexture {
    const gl = this.gl;
    const bytesPer = internal === gl.RGBA32F ? 16 : internal === gl.RGBA16F ? 8 : internal === gl.R32F ? 4 : internal === gl.R8 ? 1 : 4;
    this.gpuBytes += w * h * bytesPer;
    return makeTex2D(gl, w, h, internal, format, type, data, filter, wrap);
  }

  private build(): void {
    const gl = this.gl;
    const names = [
      'uT0', 'uT1', 'uT2', 'uInt', 'uG', 'uAxis', 'uTrans', 'uM0', 'uM1', 'uTime', 'uNoise', 'uFlash', 'uFlashGain',
      'uScene', 'uDepth', 'uEcho', 'uEchoGrid', 'uEchoOn', 'uCam', 'uRight', 'uUp', 'uFwd', 'uTan', 'uRes', 'uFrame',
      'uCloud', 'uFunnel', 'uFunnel2', 'uMask', 'uOverlay', 'uAxisZ', 'uAmb', 'uGlowDir', 'uFog', 'uFogK', 'uDust', 'uDebug',
      'uViewProj', 'uGroundCentre', 'uMaxR', 'uTerrain', 'uTerrainGrid', 'uTerrainSize', 'uTerrainRef',
      'uLand', 'uLandL', 'uLandGrid', 'uImagery', 'uImageryGrid', 'uWet', 'uRainRate',
      'uCls', 'uShutter', 'uCamFwd', 'uBox', 'uCamVel',
      'uSrc', 'uDir', 'uThresh', 'uFirst', 'uHdr', 'uBloom', 'uExposure', 'uBloomK', 'uGrain', 'uRainK', 'uRainCol',
    ];
    this.progs.volume = program(gl, S.FULLSCREEN_VS, S.VOLUME_FS, names);
    this.progs.ground = program(gl, S.GROUND_VS, S.GROUND_FS, names);
    this.progs.debris = program(gl, S.DEBRIS_VS, S.DEBRIS_FS, names);
    this.progs.tree = program(gl, S.TREE_VS, S.TREE_FS, names);
    this.progs.box = program(gl, S.BOX_VS, S.BOX_FS, names);
    this.progs.rain = program(gl, S.RAIN_VS, S.RAIN_FS, names);
    this.progs.blur = program(gl, S.FULLSCREEN_VS, S.BLUR_FS, names);
    this.progs.post = program(gl, S.FULLSCREEN_VS, S.POST_FS, names);

    // noise volume
    const nz = 64;
    const nd = buildNoiseVolume(nz);
    this.noiseTex = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_3D, this.noiseTex);
    gl.texImage3D(gl.TEXTURE_3D, 0, gl.RGBA8, nz, nz, nz, 0, gl.RGBA, gl.UNSIGNED_BYTE, nd);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    for (const w of [gl.TEXTURE_WRAP_S, gl.TEXTURE_WRAP_T, gl.TEXTURE_WRAP_R]) gl.texParameteri(gl.TEXTURE_3D, w, gl.REPEAT);
    this.gpuBytes += nz * nz * nz * 4;

    // placeholder textures
    const px4 = new Uint8Array([0, 0, 0, 0]);
    this.landTex = this.tex2D(1, 1, gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE, px4, gl.NEAREST);
    this.landTexL = this.tex2D(1, 1, gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE, px4, gl.LINEAR);
    this.imageryTex = this.tex2D(1, 1, gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE, px4, gl.LINEAR);
    this.terrainTex = this.tex2D(1, 1, gl.R32F, gl.RED, gl.FLOAT, new Float32Array([0]), gl.NEAREST);
    this.echoTex = this.tex2D(1, 1, gl.R8, gl.RED, gl.UNSIGNED_BYTE, new Uint8Array([0]), gl.LINEAR);

    // render targets
    const mk = (w: number, h: number, internal: number, format: number, type: number, filter: number) => this.tex2D(w, h, internal, format, type, null, filter);
    this.sceneColor = mk(this.rw, this.rh, gl.RGBA16F, gl.RGBA, gl.HALF_FLOAT, gl.LINEAR);
    this.sceneDist = mk(this.rw, this.rh, gl.R32F, gl.RED, gl.FLOAT, gl.NEAREST);
    this.sceneDepth = gl.createRenderbuffer()!;
    gl.bindRenderbuffer(gl.RENDERBUFFER, this.sceneDepth);
    gl.renderbufferStorage(gl.RENDERBUFFER, gl.DEPTH_COMPONENT24, this.rw, this.rh);
    this.sceneFbo = gl.createFramebuffer()!;
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.sceneFbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, this.sceneColor, 0);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT1, gl.TEXTURE_2D, this.sceneDist, 0);
    gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.DEPTH_ATTACHMENT, gl.RENDERBUFFER, this.sceneDepth);
    if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) throw new Error('scene framebuffer incomplete');
    this.volColor = mk(this.rw, this.rh, gl.RGBA16F, gl.RGBA, gl.HALF_FLOAT, gl.LINEAR);
    this.volFbo = gl.createFramebuffer()!;
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.volFbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, this.volColor, 0);
    this.bw = Math.max(8, this.rw >> 2);
    this.bh = Math.max(8, this.rh >> 2);
    for (let i = 0; i < 2; i++) {
      const t = mk(this.bw, this.bh, gl.RGBA16F, gl.RGBA, gl.HALF_FLOAT, gl.LINEAR);
      const f = gl.createFramebuffer()!;
      gl.bindFramebuffer(gl.FRAMEBUFFER, f);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, t, 0);
      this.bloomTex.push(t);
      this.bloomFbo.push(f);
    }
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);

    this.vaoEmpty = gl.createVertexArray()!;
    this.buildGround();
    this.buildDebrisBuffers();
    this.buildRain();
    this.buildMeshes();
    // G table for the pressure shape (64 points of ln x from 1e-3 to 40)
    const LMIN = Math.log(1e-3), LMAX = Math.log(40);
    for (let i = 0; i < 64; i++) this.gTable[i] = pressureShape(Math.exp(LMIN + ((LMAX - LMIN) * i) / 63));
  }

  private buildGround(): void {
    const gl = this.gl;
    const nR = 220, nA = 256;
    const verts = new Float32Array((nR + 1) * nA * 2);
    let o = 0;
    for (let i = 0; i <= nR; i++) for (let j = 0; j < nA; j++) { verts[o++] = i / nR; verts[o++] = (j / nA) * Math.PI * 2; }
    const idx = new Uint32Array(nR * nA * 6);
    o = 0;
    for (let i = 0; i < nR; i++) {
      for (let j = 0; j < nA; j++) {
        const a = i * nA + j, b = i * nA + ((j + 1) % nA), c = (i + 1) * nA + j, d = (i + 1) * nA + ((j + 1) % nA);
        idx[o++] = a; idx[o++] = c; idx[o++] = b; idx[o++] = b; idx[o++] = c; idx[o++] = d;
      }
    }
    this.groundVao = gl.createVertexArray()!;
    gl.bindVertexArray(this.groundVao);
    buffer(gl, verts);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    buffer(gl, idx, gl.STATIC_DRAW, gl.ELEMENT_ARRAY_BUFFER);
    this.groundIdx = idx.length;
    gl.bindVertexArray(null);
  }

  private buildDebrisBuffers(): void {
    const gl = this.gl;
    this.debrisVao = gl.createVertexArray()!;
    gl.bindVertexArray(this.debrisVao);
    buffer(gl, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]));
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    const n = this.opts.debrisCount ?? 12000;
    this.debrisDynArr = new Float32Array(n * 8);
    this.debrisDyn = buffer(gl, this.debrisDynArr, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(1);
    gl.vertexAttribPointer(1, 4, gl.FLOAT, false, 32, 0);
    gl.vertexAttribDivisor(1, 1);
    gl.enableVertexAttribArray(2);
    gl.vertexAttribPointer(2, 4, gl.FLOAT, false, 32, 16);
    gl.vertexAttribDivisor(2, 1);
    this.debrisStatic = buffer(gl, new Float32Array(n * 6), gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(3);
    gl.vertexAttribPointer(3, 4, gl.FLOAT, false, 24, 0);
    gl.vertexAttribDivisor(3, 1);
    gl.enableVertexAttribArray(4);
    gl.vertexAttribPointer(4, 2, gl.FLOAT, false, 24, 16);
    gl.vertexAttribDivisor(4, 1);
    gl.bindVertexArray(null);
  }

  private buildRain(): void {
    const gl = this.gl;
    this.rainVao = gl.createVertexArray()!;
    gl.bindVertexArray(this.rainVao);
    buffer(gl, new Float32Array([0, -1, 0, 1, 1, -1, 1, 1]));
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    const rng = mulberry(777);
    const d = new Float32Array(this.rainN * 4);
    for (let i = 0; i < this.rainN; i++) { d[i * 4] = rng(); d[i * 4 + 1] = rng(); d[i * 4 + 2] = rng(); d[i * 4 + 3] = 0.35 + 0.65 * rng(); }
    this.rainSeed = buffer(gl, d);
    gl.enableVertexAttribArray(1);
    gl.vertexAttribPointer(1, 4, gl.FLOAT, false, 16, 0);
    gl.vertexAttribDivisor(1, 1);
    gl.bindVertexArray(null);
  }

  private buildMeshes(): void {
    const gl = this.gl;
    // ---- tree: trunk prism + crown sphere (aVert xyz + part, aNormal)
    const V: number[] = [];
    const N: number[] = [];
    const I: number[] = [];
    const sides = 6;
    for (let k = 0; k < 2; k++) {
      for (let s = 0; s < sides; s++) {
        const a = (s / sides) * Math.PI * 2;
        V.push(Math.cos(a), Math.sin(a), k, 0);
        N.push(Math.cos(a), Math.sin(a), 0);
      }
    }
    for (let s = 0; s < sides; s++) {
      const s2 = (s + 1) % sides;
      I.push(s, s2, sides + s, s2, sides + s2, sides + s);
    }
    const lobes: [number, number, number, number][] = [[0, 0, 0.1, 0.62], [0.52, 0.1, -0.1, 0.5], [-0.45, 0.3, -0.05, 0.52], [0.1, -0.55, 0.0, 0.5], [-0.2, -0.3, 0.55, 0.46], [0.25, 0.35, 0.5, 0.42]];
    const nLat = 5, nLon = 8;
    for (const [ox, oy, oz, lr] of lobes) {
      const base = V.length / 4;
      for (let i = 0; i <= nLat; i++) {
        const phi = -Math.PI / 2 + (i / nLat) * Math.PI;
        for (let j = 0; j < nLon; j++) {
          const th = (j / nLon) * Math.PI * 2;
          const x = Math.cos(phi) * Math.cos(th), y = Math.cos(phi) * Math.sin(th), z = Math.sin(phi);
          V.push(ox + x * lr, oy + y * lr, oz + z * lr, 1);
          N.push(x, y, z);
        }
      }
      for (let i = 0; i < nLat; i++) {
        for (let j = 0; j < nLon; j++) {
          const a = base + i * nLon + j, b = base + i * nLon + ((j + 1) % nLon), c = base + (i + 1) * nLon + j, d = base + (i + 1) * nLon + ((j + 1) % nLon);
          I.push(a, c, b, b, c, d);
        }
      }
    }
    this.treeVao = gl.createVertexArray()!;
    gl.bindVertexArray(this.treeVao);
    buffer(gl, new Float32Array(V));
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 4, gl.FLOAT, false, 0, 0);
    buffer(gl, new Float32Array(N));
    gl.enableVertexAttribArray(1);
    gl.vertexAttribPointer(1, 3, gl.FLOAT, false, 0, 0);
    buffer(gl, new Uint16Array(I), gl.STATIC_DRAW, gl.ELEMENT_ARRAY_BUFFER);
    this.treeIdxCount = I.length;
    gl.bindVertexArray(null);

    // ---- box with gable roof: walls 0, roof 1
    const bv: number[] = [];
    const bn: number[] = [];
    const bi: number[] = [];
    const quad = (p: number[][], n: number[], part: number) => {
      const b0 = bv.length / 4;
      for (const q of p) { bv.push(q[0], q[1], q[2], part); bn.push(n[0], n[1], n[2]); }
      bi.push(b0, b0 + 1, b0 + 2, b0, b0 + 2, b0 + 3);
    };
    // sides: +x, -x end walls are triangles at the top -> use quads whose top vertices at y=0 are roof (raised)
    quad([[-0.5, -0.5, 0], [0.5, -0.5, 0], [0.5, -0.5, 1], [-0.5, -0.5, 1]], [0, -1, 0], 0);
    quad([[0.5, 0.5, 0], [-0.5, 0.5, 0], [-0.5, 0.5, 1], [0.5, 0.5, 1]], [0, 1, 0], 0);
    // end walls with the apex: split into a quad (lower) and a triangle (apex, roof part)
    for (const sx of [-1, 1]) {
      const x = 0.5 * sx;
      const b0 = bv.length / 4;
      bv.push(x, -0.5, 0, 0, x, 0.5, 0, 0, x, 0.5, 1, 1, x, 0, 1, 1, x, -0.5, 1, 1);
      for (let k = 0; k < 5; k++) bn.push(sx, 0, 0);
      if (sx > 0) bi.push(b0, b0 + 1, b0 + 2, b0, b0 + 2, b0 + 4, b0 + 2, b0 + 3, b0 + 4);
      else bi.push(b0, b0 + 2, b0 + 1, b0, b0 + 4, b0 + 2, b0 + 2, b0 + 4, b0 + 3);
    }
    // roof: two slopes
    quad([[-0.5, -0.5, 1], [0.5, -0.5, 1], [0.5, 0, 1], [-0.5, 0, 1]], [0, -0.6, 0.8], 1);
    quad([[-0.5, 0.5, 1], [-0.5, 0, 1], [0.5, 0, 1], [0.5, 0.5, 1]], [0, 0.6, 0.8], 1);
    this.boxVao = gl.createVertexArray()!;
    gl.bindVertexArray(this.boxVao);
    buffer(gl, new Float32Array(bv));
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 4, gl.FLOAT, false, 0, 0);
    buffer(gl, new Float32Array(bn));
    gl.enableVertexAttribArray(1);
    gl.vertexAttribPointer(1, 3, gl.FLOAT, false, 0, 0);
    buffer(gl, new Uint16Array(bi), gl.STATIC_DRAW, gl.ELEMENT_ARRAY_BUFFER);
    this.boxIdxCount = bi.length;
    gl.bindVertexArray(null);
  }

  // ---- seed ---------------------------------------------------------------------------------------
  /** Provide the observations; resolves the physical parameters (survey -> core radius, track -> translation, T/Td -> LCL). */
  setSeedData(seed: SeedData & { epochUtc?: string }): ResolvedSeed {
    const r = resolveSeed(seed);
    this.seed = r;
    this.flashes = r.flashes.map((f, i) => makeFlashEnv(i + 1, f.t, 0.55 + 0.45 * (((i * 2654435761) >>> 0) % 100) / 100));
    this.sim = new DebrisSim(r, { count: this.opts.debrisCount ?? 12000, seed: 20220330 });
    this.uploadDebrisStatic();
    // sun for the label and the glow direction
    if (seed.epochUtc) {
      const d = new Date(new Date(seed.epochUtc).getTime() + (r.span[0] + r.span[1]) * 500);
      const [la, lo] = fromLocal(r.origin, 0, 0);
      const sp = sunPosition(d, la, lo);
      this.sunInfo = { elevDeg: sp.elevationDeg, azDeg: sp.azimuthDeg, label: `sun ${sp.elevationDeg.toFixed(0)} deg at the time` };
    } else {
      this.sunInfo = { elevDeg: -12, azDeg: 272, label: '' };
    }
    const az = (this.sunInfo.azDeg * Math.PI) / 180;
    this.glowDir = [Math.sin(az), Math.cos(az), 0];
    this.setShot(r.span[0] + 180, r.span[0] + 260);
    this.echoFromSeed();
    return r;
  }

  private echoFromSeed(): void {
    const gl = this.gl;
    const rs = this.seed?.echoSeries ?? [];
    this.echoSeries = rs.length ? makeEchoSeries(rs.map((g) => ({ ...g, t: g.t ?? 0 })), this.seed!.echoMotion) : null;
    this.echoT = NaN;
    if (this.echoSeries) {
      // the echo through time: the grid is the series' (it covers the whole track), the texture is rewritten as the time moves
      const g = this.echoSeries.grids[0];
      this.echoGrid = [g.x0, g.y0, 1 / (g.nx * g.dx), 1 / (g.ny * g.dx)];
      this.echoRate = new Float32Array(g.nx * g.ny);
      this.echoBytes = new Uint8Array(g.nx * g.ny);
      this.echoOn = true;
      this.updateEcho(this.t);
      return;
    }
    const e = this.seed?.echo;
    if (!e) { this.echoOn = false; return; }
    const data = new Uint8Array(e.nx * e.ny);
    let sum = 0, n = 0;
    for (let i = 0; i < data.length; i++) {
      const z = e.dbz[i];
      let R = 0;
      if (Number.isFinite(z) && z > 5) R = Math.pow(Math.pow(10, z / 10) / 200, 1 / 1.6); // Marshall-Palmer Z = 200 R^1.6
      R = Math.min(120, R);
      data[i] = Math.round((R / 120) * 255);
      if (R > 0.5) { sum += R; n++; }
    }
    this.echoMean = n ? sum / n : 10;
    gl.bindTexture(gl.TEXTURE_2D, this.echoTex);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R8, e.nx, e.ny, 0, gl.RED, gl.UNSIGNED_BYTE, data);
    this.echoGrid = [e.x0, e.y0, 1 / (e.nx * e.dx), 1 / (e.ny * e.dx)];
    this.echoOn = true;
  }

  /** Rewrite the rain-rate texture for data time `t` from the echo series (a no-op without one, or for the same time). */
  private updateEcho(t: number): void {
    const s = this.echoSeries;
    if (!s || !this.echoRate || !this.echoBytes || t === this.echoT) return;
    this.echoT = t;
    const g = s.grids[0];
    echoRainAt(s, t, this.echoRate);
    let sum = 0, n = 0;
    for (let i = 0; i < this.echoRate.length; i++) {
      const R = Math.min(120, this.echoRate[i]);
      this.echoBytes[i] = Math.round((R / 120) * 255);
      if (R > 0.5) { sum += R; n++; }
    }
    this.echoMean = n ? sum / n : 10;
    const gl = this.gl;
    gl.bindTexture(gl.TEXTURE_2D, this.echoTex);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R8, g.nx, g.ny, 0, gl.RED, gl.UNSIGNED_BYTE, this.echoBytes);
  }

  private uploadDebrisStatic(): void {
    const gl = this.gl;
    const s = this.sim!;
    const a = new Float32Array(s.n * 6);
    for (let i = 0; i < s.n; i++) {
      a[i * 6] = s.attr[i * 6]; a[i * 6 + 1] = s.attr[i * 6 + 1]; a[i * 6 + 2] = s.attr[i * 6 + 2];
      a[i * 6 + 3] = s.attr[i * 6 + 4]; // spin rate
      a[i * 6 + 4] = s.attr[i * 6 + 5]; // size scale
      a[i * 6 + 5] = ((i * 2654435761) >>> 0) % 1000 / 1000; // tone variation
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, this.debrisStatic);
    gl.bufferData(gl.ARRAY_BUFFER, a, gl.DYNAMIC_DRAW);
  }

  /** The class of a particle can change when it is recycled: refresh the static attributes each frame (cheap). */

  /** Tell the scene which part of the data clock the shot covers: the debris warm-up and the landscape focus. */
  setShot(t0: number, t1: number, focusRadius = 1500): void {
    if (!this.seed) return;
    this.simStart = t0 - 12;
    const a0 = axisAt(this.seed, t0), a1 = axisAt(this.seed, t1);
    this.landFocus = [(a0.x + a1.x) / 2, (a0.y + a1.y) / 2];
    this.buildLand(focusRadius + Math.hypot(a1.x - a0.x, a1.y - a0.y) / 2);
    this.sim?.reset(this.simStart);
    this.t = this.simStart;
  }

  private buildLand(radius: number): void {
    const gl = this.gl;
    const half = this.opts.landHalf ?? 3600;
    this.land = buildLandscape({ cx: this.landFocus[0], cy: this.landFocus[1], half, size: this.opts.landSize ?? 1536, seed: 20220330, focus: this.landFocus, treeRadius: Math.min(this.opts.treeRadius ?? 2200, radius) });
    this.uploadLand();
    void gl;
  }

  private uploadLand(): void {
    const gl = this.gl;
    const L = this.land!;
    for (const [tex, filter] of [[this.landTex, gl.NEAREST], [this.landTexL, gl.LINEAR]] as const) {
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, L.size, L.size, 0, gl.RGBA, gl.UNSIGNED_BYTE, L.data);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filter);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filter);
    }
    this.gpuBytes += L.size * L.size * 8;
    this.uploadFeatures();
  }

  /** Keep the observer's standing places free of trees and buildings (call with the camera positions of the shot). */
  clearPositions(points: [number, number][], r = 28): void {
    if (!this.land) return;
    for (const [x, y] of points) clearAround(this.land, x, y, r);
    this.uploadFeatures();
  }

  private groundZ(x: number, y: number): number {
    const T = this.terrain;
    if (!T) return 0;
    const u = (x - (T.cx - T.half)) / (2 * T.half) * T.size - 0.5;
    const v = (y - (T.cy - T.half)) / (2 * T.half) * T.size - 0.5;
    const i0 = Math.max(0, Math.min(T.size - 2, Math.floor(u))), j0 = Math.max(0, Math.min(T.size - 2, Math.floor(v)));
    const fu = Math.max(0, Math.min(1, u - i0)), fv = Math.max(0, Math.min(1, v - j0));
    const h = (i: number, j: number) => T.metres[j * T.size + i];
    return (h(i0, j0) * (1 - fu) + h(i0 + 1, j0) * fu) * (1 - fv) + (h(i0, j0 + 1) * (1 - fu) + h(i0 + 1, j0 + 1) * fu) * fv - T.ref;
  }

  /** Height of the ground (m above the reference) at a local position; 0 without terrain data. */
  groundHeight(x: number, y: number): number {
    return this.groundZ(x, y);
  }

  private uploadFeatures(): void {
    const gl = this.gl;
    const L = this.land!;
    // trees
    const nT = L.trees.length / 6;
    this.treeN = nT;
    const inst = new Float32Array(nT * 8);
    this.treePine = new Uint8Array(nT);
    for (let i = 0; i < nT; i++) {
      const x = L.trees[i * 6], y = L.trees[i * 6 + 1];
      inst[i * 8] = x; inst[i * 8 + 1] = y; inst[i * 8 + 2] = this.groundZ(x, y); inst[i * 8 + 3] = L.trees[i * 6 + 2];
      inst[i * 8 + 4] = L.trees[i * 6 + 3]; inst[i * 8 + 5] = L.trees[i * 6 + 4]; inst[i * 8 + 6] = L.trees[i * 6 + 5]; inst[i * 8 + 7] = 0;
      this.treePine[i] = L.trees[i * 6 + 5] > 0.5 ? 1 : 0;
    }
    this.treeBendArr = new Float32Array(nT * 4);
    gl.bindVertexArray(this.treeVao);
    this.treeInst = buffer(gl, inst);
    gl.enableVertexAttribArray(2);
    gl.vertexAttribPointer(2, 4, gl.FLOAT, false, 32, 0);
    gl.vertexAttribDivisor(2, 1);
    gl.enableVertexAttribArray(3);
    gl.vertexAttribPointer(3, 4, gl.FLOAT, false, 32, 16);
    gl.vertexAttribDivisor(3, 1);
    this.treeBend = buffer(gl, this.treeBendArr, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(4);
    gl.vertexAttribPointer(4, 4, gl.FLOAT, false, 16, 0);
    gl.vertexAttribDivisor(4, 1);
    gl.bindVertexArray(null);
    // buildings and poles
    const nB = L.buildings.length / 8, nP = L.poles.length / 3;
    const bi = new Float32Array((nB + nP) * 12);
    for (let i = 0; i < nB; i++) {
      const o = i * 12, s = i * 8;
      const x = L.buildings[s], y = L.buildings[s + 1];
      bi[o] = x; bi[o + 1] = y; bi[o + 2] = this.groundZ(x, y); bi[o + 3] = L.buildings[s + 5];
      bi[o + 4] = L.buildings[s + 2]; bi[o + 5] = L.buildings[s + 3]; bi[o + 6] = L.buildings[s + 4]; bi[o + 7] = L.buildings[s + 6];
      bi[o + 8] = L.buildings[s + 7];
    }
    for (let i = 0; i < nP; i++) {
      const o = (nB + i) * 12, s = i * 3;
      const x = L.poles[s], y = L.poles[s + 1];
      bi[o] = x; bi[o + 1] = y; bi[o + 2] = this.groundZ(x, y); bi[o + 3] = 0;
      bi[o + 4] = 0.35; bi[o + 5] = 0.35; bi[o + 6] = L.poles[s + 2]; bi[o + 7] = 4;
      bi[o + 8] = 0.5;
    }
    this.boxN = nB + nP;
    gl.bindVertexArray(this.boxVao);
    this.boxInst = buffer(gl, bi);
    for (const [loc, off] of [[2, 0], [3, 16], [4, 32]] as const) {
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, 4, gl.FLOAT, false, 48, off);
      gl.vertexAttribDivisor(loc, 1);
    }
    gl.bindVertexArray(null);
  }

  /**
   * Load satellite imagery and terrain from the tile cache (same sources as the storm clip). Without tiles the
   * scene keeps its procedural ground. Call after setSeedData / setShot.
   */
  async loadGround(): Promise<void> {
    if (!this.seed || this.opts.useTiles === false) return;
    const gl = this.gl;
    const half = this.opts.landHalf ?? 3600;
    const res: GroundTileResult = await loadGroundTiles({
      origin: this.seed.origin, cx: this.landFocus[0], cy: this.landFocus[1], half,
      imagerySize: 2048, heightSize: 512, maxZoom: 15, base: this.opts.tileBase ?? '',
    });
    this.groundInfo.log = res.log;
    this.groundInfo.attribution = res.attribution;
    if (res.imagery) {
      gl.bindTexture(gl.TEXTURE_2D, this.imageryTex);
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, res.imagery.size, res.imagery.size, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array(res.imagery.rgba.buffer));
      gl.generateMipmap(gl.TEXTURE_2D);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
      this.gpuBytes += res.imagery.size * res.imagery.size * 5;
      this.imagery = true;
      this.groundInfo.mode = 'imagery';
      if (this.land) {
        forestFromImagery(this.land, res.imagery.rgba, res.imagery.size, half, this.landFocus[0], this.landFocus[1]);
        placeFeatures(this.land, this.landFocus[0], this.landFocus[1], 1800);
        this.uploadLand();
      }
    }
    if (res.terrain) {
      const T = res.terrain;
      const ref = this.sampleTerrainRef(T.metres, T.size, half);
      this.terrain = { size: T.size, cx: this.landFocus[0], cy: this.landFocus[1], half, metres: T.metres, ref };
      gl.bindTexture(gl.TEXTURE_2D, this.terrainTex);
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, T.size, T.size, 0, gl.RED, gl.FLOAT, T.metres);
      this.gpuBytes += T.size * T.size * 4;
      this.groundInfo.terrain = true;
      this.uploadFeatures();
    }
  }

  private sampleTerrainRef(m: Float32Array, size: number, half: number): number {
    // reference elevation: at the focus (centre of the mosaic)
    void half;
    return m[(size >> 1) * size + (size >> 1)];
  }

  // ---- time ---------------------------------------------------------------------------------------
  /** Set the simulation time (seconds on the data clock). Deterministic: the state is a pure function of t and the shot start. */
  setTime(t: number): void {
    if (!this.seed || !this.sim) return;
    if (t < this.sim.time - 1e-6 || t < this.simStart) {
      this.sim.reset(Math.min(this.simStart, t - 1));
    } else if (t - this.sim.time > JUMP_S) {
      // a jump forward (a time-lapse in the film): restart with the warm-up before `t`, a pure function of `t`, instead of stepping through all of it
      this.sim.reset(t - WARMUP_S);
    }
    this.sim.stepTo(t);
    this.t = t;
    this.updateEcho(t);
  }

  /** Is there evidence of a tornado at time t? (survey span) */
  isActive(t: number = this.t): boolean {
    return !!this.seed && intensityAt(this.seed, t) > 0;
  }

  get time(): number { return this.t; }

  axis(t: number = this.t): { x: number; y: number } {
    return axisAt(this.seed!, t);
  }

  /**
   * Camera from geographic pose (the movie's camera_path.json: lat, lon, altitude above sea level, heading degrees
   * clockwise from north, pitch degrees up, vertical field of view). The movie's `alt_m` is height above ITS ground
   * (a flat map in the clip), so by default it is used as height above the ground of this scene; pass `groundElevM`
   * when the path is in metres above sea level (the scene ground is then `groundElevM`, or the terrain tile height).
   */
  cameraFromGeo(g: { lat: number; lon: number; altM: number; headingDeg: number; pitchDeg: number; fovDeg?: number; groundElevM?: number; rollDeg?: number }): TornadoCamera {
    const [x, y] = this.toLocal(g.lat, g.lon);
    const agl = Math.max(1.5, g.altM - (g.groundElevM ?? 0));
    const z = agl + this.groundZ(x, y);
    const h = (g.headingDeg * Math.PI) / 180, p = (g.pitchDeg * Math.PI) / 180;
    const d: Vec3 = [Math.sin(h) * Math.cos(p), Math.cos(h) * Math.cos(p), Math.sin(p)];
    return { pos: [x, y, z], target: [x + d[0] * 100, y + d[1] * 100, z + d[2] * 100], fovDeg: g.fovDeg ?? 50, roll: ((g.rollDeg ?? 0) * Math.PI) / 180 };
  }

  /** Local metres of a latitude / longitude. */
  toLocal(lat: number, lon: number): [number, number] {
    const o = this.seed!.origin;
    return [(lon - o.lon) * 111320 * Math.cos((o.lat * Math.PI) / 180), (lat - o.lat) * 110574];
  }

  /** Numbers for the labels: derived from the resolved seed. */
  info(t: number = this.t) {
    const r = this.seed!;
    const z = 60;
    const rc = coreRadius(r.vortex, z);
    const rf = vortexOnlyRadius(r.condition, r.vortex, z);
    return {
      ef: r.ef, vt: r.vt, coreRadius: r.rm, width: r.width, coreSource: r.coreSource,
      speed: r.speed, headingDeg: r.headingDeg,
      periodCore: rotationPeriod(r), periodFunnel: rf > 0 ? (2 * Math.PI) / Math.abs(omega(r.vortex, rf, z)) : null,
      funnelRadius60: rf, coreRadius60: rc,
      lcl: r.condition.lcl, cloudBase: r.cloudCondition.lcl, dpCrit: r.condition.dpCrit,
      peakWind: tangential(r.vortex, r.rm, 60), intensity: intensityAt(r, t),
      sun: this.sunInfo, ambient: AMBIENT[this.ambient].label, ground: this.groundInfo.mode,
      summary: r.summary,
    };
  }

  // ---- render -------------------------------------------------------------------------------------
  private setCommon(p: Program, cam: ReturnType<typeof basis>): void {
    const gl = this.gl;
    const r = this.seed!;
    const u = p.u;
    const v = r.vortex;
    gl.uniform4f(u.uT0, v.vt, v.rm, v.hf, v.hv);
    gl.uniform4f(u.uT1, v.hs, v.gNorm, v.a, v.ha);
    gl.uniform4f(u.uT2, v.ra, v.beta, v.delta, v.sense);
    gl.uniform1f(u.uInt, intensityAt(r, this.t));
    if (u.uG) gl.uniform1fv(u.uG, this.gTable);
    const ax = axisAt(r, this.t);
    gl.uniform2f(u.uAxis, ax.x, ax.y);
    gl.uniform2f(u.uTrans, r.translation[0], r.translation[1]);
    const m = r.meso;
    if (m) {
      gl.uniform4f(u.uM0, m.vt, m.rm, m.hv, m.sense);
      gl.uniform4f(u.uM1, m.hs, m.gNorm, m.hf, 0);
    } else {
      gl.uniform4f(u.uM0, 0, 1000, 1000, 1);
      gl.uniform4f(u.uM1, 100, 1, 1000, 0);
    }
    gl.uniform1f(u.uTime, this.t);
    // lightning: the four most effective flashes
    const act: { x: number; y: number; z: number; I: number; score: number }[] = [];
    for (let i = 0; i < this.flashes.length; i++) {
      const f = this.flashes[i];
      if (this.t < f.t || this.t > flashEnd(f) + FLASH_SMOOTH_S) continue;
      // comfortable lightning: the light is the mean of the flash envelope over the last FLASH_SMOOTH_S seconds (a pure function of t),
      // so a cluster of flashes does not strobe (rise time ~0.12 s, no more than about 3 distinct bursts per second)
      let lv = 0;
      for (let k = 0; k < 6; k++) lv += flashLight(f, this.t - (k * FLASH_SMOOTH_S) / 5);
      lv = Math.min(1, lv / 6);
      if (lv < 0.01) continue;
      const fs = r.flashes[i];
      const d = Math.hypot(fs.x - cam.pos[0], fs.y - cam.pos[1], fs.z - cam.pos[2]);
      act.push({ x: fs.x, y: fs.y, z: fs.z, I: lv, score: lv / (1 + (d / 6000) ** 2) });
    }
    act.sort((a, b) => b.score - a.score);
    const fl = new Float32Array(16);
    for (let i = 0; i < Math.min(4, act.length); i++) { fl[i * 4] = act[i].x; fl[i * 4 + 1] = act[i].y; fl[i * 4 + 2] = act[i].z; fl[i * 4 + 3] = act[i].I; }
    gl.uniform4fv(u.uFlash, fl);
    this.flashSum = Math.min(4, act.reduce((a, f) => a + f.I / (1 + (Math.hypot(f.x - cam.pos[0], f.y - cam.pos[1], f.z - cam.pos[2]) / 6000) ** 2), 0) * 3);
    gl.uniform1f(u.uFlashGain, 1.6 * this.flashMul);
    gl.uniform3f(u.uCam, cam.pos[0], cam.pos[1], cam.pos[2]);
    const a = AMBIENT[this.ambient];
    gl.uniform4f(u.uAmb, a.amb[0], a.amb[1], a.amb[2], a.glow);
    gl.uniform3f(u.uGlowDir, this.glowDir[0], this.glowDir[1], this.glowDir[2]);
    gl.uniform1f(u.uWet, 0.85);
    gl.uniform1f(u.uRainRate, this.echoOn ? this.echoMean : 25);
    this.bindTex(p, 'uNoise', gl.TEXTURE_3D, this.noiseTex, 5);
  }

  private bindTex(p: Program, name: string, target: number, tex: WebGLTexture, unit: number): void {
    const gl = this.gl;
    if (p.u[name] == null) return;
    gl.activeTexture(gl.TEXTURE0 + unit);
    gl.bindTexture(target, tex);
    gl.uniform1i(p.u[name], unit);
  }

  private updateTreeStates(): void {
    const gl = this.gl;
    const L = this.land;
    if (!L || !this.seed || !this.treeN) return;
    const a = this.treeBendArr;
    const st = this.treeStates;
    for (let i = 0; i < this.treeN; i++) {
      const x = L.trees[i * 6], y = L.trees[i * 6 + 1];
      treeState(this.seed, x, y, this.treePine[i] === 1, this.t, st);
      a[i * 4] = st.dir[0]; a[i * 4 + 1] = st.dir[1]; a[i * 4 + 2] = st.tilt; a[i * 4 + 3] = st.fall;
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, this.treeBend);
    gl.bufferSubData(gl.ARRAY_BUFFER, 0, a);
  }

  private updateDebris(): number {
    const gl = this.gl;
    const s = this.sim!;
    const a = this.debrisDynArr;
    const frac = Math.max(0, this.t - s.time);
    const stat = new Float32Array(s.n * 6);
    for (let i = 0; i < s.n; i++) {
      const o = i * 8;
      const vis = !s.onGround[i];
      a[o] = s.pos[i * 3] + s.vel[i * 3] * frac;
      a[o + 1] = s.pos[i * 3 + 1] + s.vel[i * 3 + 1] * frac;
      a[o + 2] = s.pos[i * 3 + 2] + s.vel[i * 3 + 2] * frac + this.groundZ(s.pos[i * 3], s.pos[i * 3 + 1]);
      a[o + 3] = vis ? s.cls[i] : -1;
      a[o + 4] = s.vel[i * 3]; a[o + 5] = s.vel[i * 3 + 1]; a[o + 6] = s.vel[i * 3 + 2];
      a[o + 7] = s.attr[i * 6 + 3];
      stat[i * 6] = s.attr[i * 6]; stat[i * 6 + 1] = s.attr[i * 6 + 1]; stat[i * 6 + 2] = s.attr[i * 6 + 2];
      stat[i * 6 + 3] = s.attr[i * 6 + 4]; stat[i * 6 + 4] = s.attr[i * 6 + 5]; stat[i * 6 + 5] = ((i * 2654435761) >>> 0) % 1000 / 1000;
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, this.debrisDyn);
    gl.bufferSubData(gl.ARRAY_BUFFER, 0, a);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.debrisStatic);
    gl.bufferSubData(gl.ARRAY_BUFFER, 0, stat);
    return s.airborne();
  }

  /** The dither / grain seed: a pure function of the simulation time, so the same frame is always identical. */
  private frameKey(): number {
    return Math.round(this.t * 997) % 65521;
  }

  /** Screen-space slope of the falling rain (dx per dy) from the wind at the camera and the camera roll. */
  private rainSlope(cam: ReturnType<typeof basis>): number {
    const r = this.seed!;
    const w = windField(r, cam.pos[0], cam.pos[1], Math.max(2, cam.pos[2]), this.t);
    const vx = w[0] * 0.7, vy = w[1] * 0.7, vz = -9;
    const sx = vx * cam.right[0] + vy * cam.right[1];
    const sy = vx * cam.up[0] + vy * cam.up[1] + vz * cam.up[2];
    return Math.max(-1.5, Math.min(1.5, -sx / (Math.abs(sy) + 1e-3)));
  }

  /** Render one frame of the current time for the camera into the canvas. */
  render(camera: TornadoCamera, opts: { overlay?: boolean } = {}): FrameStats {
    const overlay = !!opts.overlay;
    const gl = this.gl;
    if (!this.seed || !this.sim) throw new Error('setSeedData first');
    const t0 = performance.now();
    const cam = basis(camera.pos, camera.target, camera.roll ?? 0);
    const aspect = this.rw / this.rh;
    const fovY = (camera.fovDeg * Math.PI) / 180;
    const vp = viewProj(cam, fovY, aspect, 0.25, 80000);
    const tanY = Math.tan(fovY / 2), tanX = tanY * aspect;
    const amb = AMBIENT[this.ambient];
    const air = this.updateDebris();
    this.updateTreeStates();
    const r = this.seed;
    const inten = intensityAt(r, this.t);

    // ---- pass 1: opaque scene
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.sceneFbo);
    gl.drawBuffers([gl.COLOR_ATTACHMENT0, gl.COLOR_ATTACHMENT1]);
    gl.viewport(0, 0, this.rw, this.rh);
    gl.clearBufferfv(gl.COLOR, 0, overlay ? [0, 0, 0, 0] : [amb.fog[0], amb.fog[1], amb.fog[2], 1]);
    gl.clearBufferfv(gl.COLOR, 1, [1e6, 0, 0, 0]);
    gl.clearBufferfi(gl.DEPTH_STENCIL, 0, 1, 0);
    gl.enable(gl.DEPTH_TEST);
    gl.depthFunc(gl.LEQUAL);
    gl.depthMask(true);
    gl.disable(gl.BLEND);
    gl.disable(gl.CULL_FACE);

    // ground
    if (!overlay) {
      const p = this.progs.ground;
      gl.useProgram(p.prog);
      this.setCommon(p, cam);
      gl.uniformMatrix4fv(p.u.uViewProj, false, vp);
      gl.uniform1f(p.u.uMaxR, 30000);
      const L = this.land!;
      gl.uniform4f(p.u.uLandGrid, L.cx - L.half, L.cy - L.half, 1 / (2 * L.half), 1);
      this.bindTex(p, 'uLand', gl.TEXTURE_2D, this.landTex, 0);
      this.bindTex(p, 'uLandL', gl.TEXTURE_2D, this.landTexL, 1);
      this.bindTex(p, 'uImagery', gl.TEXTURE_2D, this.imageryTex, 2);
      gl.uniform4f(p.u.uImageryGrid, L.cx - L.half, L.cy - L.half, 1 / (2 * L.half), this.imagery ? 1 : 0);
      this.bindTex(p, 'uTerrain', gl.TEXTURE_2D, this.terrainTex, 3);
      const T = this.terrain;
      if (T) {
        gl.uniform4f(p.u.uTerrainGrid, T.cx - T.half, T.cy - T.half, 1 / (2 * T.half), 1);
        gl.uniform2f(p.u.uTerrainSize, T.size, T.size);
        gl.uniform1f(p.u.uTerrainRef, T.ref);
      } else {
        gl.uniform4f(p.u.uTerrainGrid, 0, 0, 1, 0);
        gl.uniform2f(p.u.uTerrainSize, 1, 1);
        gl.uniform1f(p.u.uTerrainRef, 0);
      }
      gl.bindVertexArray(this.groundVao);
      gl.drawElements(gl.TRIANGLES, this.groundIdx, gl.UNSIGNED_INT, 0);
    }
    // buildings and poles
    if (!overlay && this.boxN && this.boxVao) {
      const p = this.progs.box;
      gl.useProgram(p.prog);
      this.setCommon(p, cam);
      gl.uniformMatrix4fv(p.u.uViewProj, false, vp);
      gl.bindVertexArray(this.boxVao);
      gl.drawElementsInstanced(gl.TRIANGLES, this.boxIdxCount, gl.UNSIGNED_SHORT, 0, this.boxN);
    }
    // trees
    if (!overlay && this.treeN && this.treeVao) {
      const p = this.progs.tree;
      gl.useProgram(p.prog);
      this.setCommon(p, cam);
      gl.uniformMatrix4fv(p.u.uViewProj, false, vp);
      gl.bindVertexArray(this.treeVao);
      gl.drawElementsInstanced(gl.TRIANGLES, this.treeIdxCount, gl.UNSIGNED_SHORT, 0, this.treeN);
    }
    // debris (stochastic transparency: alpha-tested, depth-written, so the volume pass attenuates it correctly)
    {
      const p = this.progs.debris;
      gl.useProgram(p.prog);
      this.setCommon(p, cam);
      gl.uniform1f(p.u.uFrame, this.frameKey());
      gl.uniformMatrix4fv(p.u.uViewProj, false, vp);
      const cls = new Float32Array(24);
      DEBRIS_CLASSES.forEach((c, i) => { cls[i * 4] = c.a; cls[i * 4 + 1] = c.b; cls[i * 4 + 2] = c.tone; cls[i * 4 + 3] = i === 0 ? 0 : i === 1 ? 1 : 2; });
      gl.uniform4fv(p.u.uCls, cls);
      gl.uniform1f(p.u.uShutter, 1 / 60);
      gl.bindVertexArray(this.debrisVao);
      gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, this.sim.n);
    }
    gl.bindVertexArray(null);
    gl.disable(gl.DEPTH_TEST);

    // ---- pass 2: volume
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.volFbo);
    gl.drawBuffers([gl.COLOR_ATTACHMENT0]);
    gl.viewport(0, 0, this.rw, this.rh);
    {
      const p = this.progs.volume;
      gl.useProgram(p.prog);
      this.setCommon(p, cam);
      this.bindTex(p, 'uScene', gl.TEXTURE_2D, this.sceneColor, 0);
      this.bindTex(p, 'uDepth', gl.TEXTURE_2D, this.sceneDist, 1);
      this.bindTex(p, 'uEcho', gl.TEXTURE_2D, this.echoTex, 2);
      gl.uniform4f(p.u.uEchoGrid, this.echoGrid[0], this.echoGrid[1], this.echoGrid[2], this.echoGrid[3]);
      gl.uniform1f(p.u.uEchoOn, this.echoOn ? 1 : 0);
      gl.uniform3f(p.u.uRight, cam.right[0], cam.right[1], cam.right[2]);
      gl.uniform3f(p.u.uUp, cam.up[0], cam.up[1], cam.up[2]);
      gl.uniform3f(p.u.uFwd, cam.fwd[0], cam.fwd[1], cam.fwd[2]);
      gl.uniform2f(p.u.uTan, tanX, tanY);
      gl.uniform2f(p.u.uRes, this.rw, this.rh);
      gl.uniform1f(p.u.uFrame, this.frameKey());
      const wc = this.opts.wallCloud ?? { lowerM: 60, radiusM: 700 };
      gl.uniform4f(p.u.uCloud, r.cloudCondition.lcl, wc.lowerM, wc.radiusM, 100);
      gl.uniform4f(p.u.uFunnel, r.condition.dpCrit, Math.max(60, 0.08 * r.condition.dpCrit), 0.55, 0.11);
      gl.uniform4f(p.u.uFunnel2, heightDeficit(r.condition, 500) / 500, 0, 0, 0);
      gl.uniform3f(p.u.uFog, amb.fog[0], amb.fog[1], amb.fog[2]);
      gl.uniform4f(p.u.uFogK, 1 / 4500, 800, 0.00006 * this.rainGain, this.echoOn ? this.echoMean : 25);
      gl.uniform4f(p.u.uDust, 32, 0.035, 0.06, 1);
      gl.uniform1f(p.u.uDebug, 0);
      gl.uniform1f(p.u.uMask, overlay ? (this.mask & 52) : this.mask);
      gl.uniform1f(p.u.uOverlay, overlay ? 1 : 0);
      const axg = axisAt(r, this.t);
      gl.uniform1f(p.u.uAxisZ, this.groundZ(axg.x, axg.y));
      gl.bindVertexArray(this.vaoEmpty);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }

    // ---- bloom
    {
      const p = this.progs.blur;
      gl.useProgram(p.prog);
      gl.viewport(0, 0, this.bw, this.bh);
      gl.bindFramebuffer(gl.FRAMEBUFFER, this.bloomFbo[0]);
      this.bindTex(p, 'uSrc', gl.TEXTURE_2D, this.volColor, 0);
      gl.uniform2f(p.u.uDir, 2 / this.rw, 0);
      gl.uniform1f(p.u.uThresh, 0.9);
      gl.uniform1f(p.u.uFirst, 1);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      gl.bindFramebuffer(gl.FRAMEBUFFER, this.bloomFbo[1]);
      this.bindTex(p, 'uSrc', gl.TEXTURE_2D, this.bloomTex[0], 0);
      gl.uniform2f(p.u.uDir, 0, 3 / this.rh);
      gl.uniform1f(p.u.uFirst, 0);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      gl.bindFramebuffer(gl.FRAMEBUFFER, this.bloomFbo[0]);
      this.bindTex(p, 'uSrc', gl.TEXTURE_2D, this.bloomTex[1], 0);
      gl.uniform2f(p.u.uDir, 6 / this.rw, 0);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      gl.bindFramebuffer(gl.FRAMEBUFFER, this.bloomFbo[1]);
      this.bindTex(p, 'uSrc', gl.TEXTURE_2D, this.bloomTex[0], 0);
      gl.uniform2f(p.u.uDir, 0, 8 / this.rh);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }

    // ---- post to the canvas
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, this.width, this.height);
    {
      const p = this.progs.post;
      gl.useProgram(p.prog);
      this.bindTex(p, 'uHdr', gl.TEXTURE_2D, this.volColor, 0);
      this.bindTex(p, 'uBloom', gl.TEXTURE_2D, this.bloomTex[1], 1);
      gl.uniform1f(p.u.uExposure, amb.exposure * this.exposureMul * (camera.exposure ?? 1));
      gl.uniform1f(p.u.uBloomK, 0.5);
      gl.uniform1f(p.u.uFrame, this.frameKey());
      gl.uniform2f(p.u.uRes, this.width, this.height);
      gl.uniform1f(p.u.uGrain, 0.018);
      gl.uniform1f(p.u.uTime, this.t);
      const rr = this.echoOn ? this.echoMean : 25;
      gl.uniform3f(p.u.uRainK, Math.min(1, rr / 45) * 0.9, this.rainSlope(cam), this.flashSum);
      gl.uniform3f(p.u.uRainCol, amb.amb[0] * 1.6 + 0.02, amb.amb[1] * 1.6 + 0.02, amb.amb[2] * 1.6 + 0.03);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }
    gl.bindVertexArray(null);
    gl.finish();
    gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, this.syncPx); // forces the GPU to finish: honest timing
    this.frameNo++;
    void inten;
    const ms = performance.now() - t0;
    const mem = (performance as unknown as { memory?: { usedJSHeapSize: number } }).memory;
    this.stats = {
      ms, frame: this.frameNo, treeCount: this.treeN, debrisAirborne: air, gpuMB: this.gpuBytes / 1048576,
      jsHeapMB: mem ? mem.usedJSHeapSize / 1048576 : null, active: inten > 0,
    };
    return this.stats;
  }

  /** Diagnostic: draw the top-down slice of the swirl-advected noise (see tools/tornado_sim/check_rotation.py). */
  renderSwirlSlice(single = false): void {
    const gl = this.gl;
    if (!this.seed) return;
    const cam = basis([0, 0, 100], [0, 1, 0], 0);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, this.width, this.height);
    const p = this.progs.volume;
    gl.useProgram(p.prog);
    this.setCommon(p, cam);
    this.bindTex(p, 'uScene', gl.TEXTURE_2D, this.sceneColor, 0);
    this.bindTex(p, 'uDepth', gl.TEXTURE_2D, this.sceneDist, 1);
    gl.uniform2f(p.u.uTan, 1, 1);
    gl.uniform1f(p.u.uDebug, single ? 2 : 1);
    gl.uniform1f(p.u.uMask, 63);
    gl.uniform1f(p.u.uOverlay, 0);
    gl.bindVertexArray(this.vaoEmpty);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    gl.finish();
  }

  /** Raw access for tests: read the canvas pixels. */
  readPixels(): Uint8Array {
    const gl = this.gl;
    const px = new Uint8Array(this.width * this.height * 4);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.readPixels(0, 0, this.width, this.height, gl.RGBA, gl.UNSIGNED_BYTE, px);
    return px;
  }

  dispose(): void {
    const ext = this.gl.getExtension('WEBGL_lose_context');
    ext?.loseContext();
  }
}
