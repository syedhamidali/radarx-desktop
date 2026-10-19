/**
 * WebGL-based PPI (Plan Position Indicator) radar renderer.
 *
 * Uses real matplotlib colormaps (24 palettes including NWS standards),
 * triangle-mesh polar-to-Cartesian rendering, and 1D colormap GPU textures.
 *
 * Enhanced features:
 *  - Bilinear interpolation (smooth mode) between adjacent gates
 *  - Edge detection / contour lines at configurable dBZ intervals
 *  - Transparent no-data regions (map backgrounds show through)
 *  - Radial vignette effect
 *  - Cross-fade sweep transitions via requestAnimationFrame
 *  - Optional FPS counter
 *  - Sin/cos lookup tables for fast polar-to-Cartesian conversion
 */

import {
  COLORMAP_DATA,
  COLORMAP_NAMES,
  VARIABLE_CMAP_DEFAULTS,
  VARIABLE_RANGE_DEFAULTS,
} from './colormaps';
import { renderCache, type CachedMesh } from './renderCache';
import { perfStats } from '../stores/perfStats';
import { buildSharpMesh, buildSmoothMesh, type SmoothMesh } from './meshBuilder';
import type { MeshWorkerOutput } from '../workers/meshWorker';
import { styleFor } from './variableStyles';
import { GLSL_VALUE_MASK, MASK_OFF, maskVec, type MaskVec4 } from './valueMaskGl';
import type { ValueMask } from '../stores/valueMask';

// ─── Sweep data cache (moved to ./sweepCache; re-exported for existing imports) ──
import { cacheSweepData, getCachedSweep, evictCachedVariable, clearSweepCache, type SweepDataEntry } from './sweepCache';
export { cacheSweepData, getCachedSweep, evictCachedVariable, clearSweepCache };
export type { SweepDataEntry };

// ─── Colormap helpers ───────────────────────────────────────────────────

/** Get the default colormap name for a radar variable. */
export function getDefaultCmap(variable: string): string {
  const styled = styleFor(variable);
  if (styled) return styled.cmap;
  if (VARIABLE_CMAP_DEFAULTS[variable]) return VARIABLE_CMAP_DEFAULTS[variable];
  const vl = variable.toLowerCase();
  if (vl.includes('dbz') || vl.includes('reflectivity') || vl.includes('power')) return 'NWSRef';
  if (vl.includes('vel') || vl.includes('vrad')) return 'NWSVel';
  if (vl.includes('width') || vl.includes('wrad')) return 'hot';
  if (vl.includes('zdr') || vl.includes('differential_reflectivity')) return 'Spectral_r';
  if (vl.includes('rhohv') || vl.includes('correlation')) return 'viridis';
  if (vl.includes('phidp') || vl.includes('phase')) return 'plasma';
  if (vl.includes('kdp')) return 'YlGnBu';
  return 'turbo';
}

/** Get default vmin/vmax for a variable. */
export function getDefaultRange(variable: string): [number, number] | null {
  const styled = styleFor(variable);
  if (styled) return [styled.vmin, styled.vmax];
  return VARIABLE_RANGE_DEFAULTS[variable] ?? null;
}

/** Build RGBA Uint8Array (256x4) from a named colormap for GPU upload. */
export function buildColormapRGBA(cmapName: string): Uint8Array {
  const rgb = COLORMAP_DATA[cmapName] ?? COLORMAP_DATA['turbo'];
  const rgba = new Uint8Array(256 * 4);
  for (let i = 0; i < 256; i++) {
    rgba[i * 4] = rgb[i * 3];
    rgba[i * 4 + 1] = rgb[i * 3 + 1];
    rgba[i * 4 + 2] = rgb[i * 3 + 2];
    rgba[i * 4 + 3] = 255;
  }
  return rgba;
}

/** Get RGB [r,g,b] for index i from a named colormap. */
export function getColormapRGB(cmapName: string, index: number): [number, number, number] {
  const rgb = COLORMAP_DATA[cmapName] ?? COLORMAP_DATA['turbo'];
  const i = Math.max(0, Math.min(255, Math.round(index)));
  return [rgb[i * 3], rgb[i * 3 + 1], rgb[i * 3 + 2]];
}

// ─── Shaders ────────────────────────────────────────────────────────────

// --- Sharp (original flat-color) vertex + fragment ---
// Extended with vignette, transparent bg, and transition alpha.

const VERTEX_SHADER_SHARP = `
  attribute vec2 a_position;
  attribute float a_value;
  uniform mat3 u_matrix;
  varying float v_value;
  varying vec2 v_pos;
  void main() {
    vec3 pos = u_matrix * vec3(a_position, 1.0);
    gl_Position = vec4(pos.xy, 0.0, 1.0);
    v_value = a_value;
    v_pos = a_position;
  }
`;

const FRAGMENT_SHADER_SHARP = `
  #ifdef GL_FRAGMENT_PRECISION_HIGH
  precision highp float;
  #else
  precision mediump float;
  #endif
  varying float v_value;
  varying vec2 v_pos;
  uniform sampler2D u_colormap;
  uniform float u_vmin;
  uniform float u_vmax;
  uniform float u_nodata;
  uniform float u_vignetteIntensity;
  uniform float u_maxRange;
  uniform float u_transitionAlpha;
  uniform vec4 u_mask;
${GLSL_VALUE_MASK}
  void main() {
    if (v_value <= u_nodata + 0.5 || v_value < u_vmin || valueHidden(v_value, u_mask)) {
      gl_FragColor = vec4(0.0, 0.0, 0.0, 0.0);
      return;
    }
    float t = clamp((v_value - u_vmin) / (u_vmax - u_vmin), 0.0, 1.0);
    vec4 color = texture2D(u_colormap, vec2(t, 0.5));
    // Vignette
    if (u_vignetteIntensity > 0.0 && u_maxRange > 0.0) {
      float dist = length(v_pos) / u_maxRange;
      float vig = 1.0 - u_vignetteIntensity * smoothstep(0.4, 1.0, dist);
      color.rgb *= vig;
    }
    color.a *= u_transitionAlpha;
    gl_FragColor = color;
  }
`;

// --- Smooth (bilinear interpolation) vertex + fragment ---

const VERTEX_SHADER_SMOOTH = `
  attribute vec2 a_position;
  attribute vec2 a_polar;
  attribute vec4 a_values4;
  uniform mat3 u_matrix;
  varying vec2 v_polar;
  varying vec4 v_values4;
  varying vec2 v_pos;
  void main() {
    vec3 pos = u_matrix * vec3(a_position, 1.0);
    gl_Position = vec4(pos.xy, 0.0, 1.0);
    v_polar = a_polar;
    v_values4 = a_values4;
    v_pos = a_position;
  }
`;

const FRAGMENT_SHADER_SMOOTH = `
  #ifdef GL_FRAGMENT_PRECISION_HIGH
  precision highp float;
  #else
  precision mediump float;
  #endif
  varying vec2 v_polar;
  varying vec4 v_values4;
  varying vec2 v_pos;
  uniform sampler2D u_colormap;
  uniform float u_vmin;
  uniform float u_vmax;
  uniform float u_nodata;
  uniform float u_vignetteIntensity;
  uniform float u_maxRange;
  uniform float u_transitionAlpha;
  uniform vec4 u_mask;
${GLSL_VALUE_MASK}

  float bilinear(vec4 v, vec2 f) {
    float top = mix(v.x, v.y, f.y);
    float bot = mix(v.z, v.w, f.y);
    return mix(top, bot, f.x);
  }

  void main() {
    float nd = u_nodata + 0.5;
    bool allNodata = (v_values4.x <= nd && v_values4.y <= nd &&
                      v_values4.z <= nd && v_values4.w <= nd);
    if (allNodata) {
      gl_FragColor = vec4(0.0, 0.0, 0.0, 0.0);
      return;
    }
    // Replace nodata corners with average of valid corners
    float sum = 0.0;
    float cnt = 0.0;
    vec4 v = v_values4;
    if (v.x > nd) { sum += v.x; cnt += 1.0; }
    if (v.y > nd) { sum += v.y; cnt += 1.0; }
    if (v.z > nd) { sum += v.z; cnt += 1.0; }
    if (v.w > nd) { sum += v.w; cnt += 1.0; }
    float avg = sum / max(cnt, 1.0);
    if (v.x <= nd) v.x = avg;
    if (v.y <= nd) v.y = avg;
    if (v.z <= nd) v.z = avg;
    if (v.w <= nd) v.w = avg;

    float val = bilinear(v, v_polar);
    if (val < u_vmin || valueHidden(val, u_mask)) {
      gl_FragColor = vec4(0.0, 0.0, 0.0, 0.0);
      return;
    }
    float t = clamp((val - u_vmin) / (u_vmax - u_vmin), 0.0, 1.0);
    vec4 color = texture2D(u_colormap, vec2(t, 0.5));
    // Vignette
    if (u_vignetteIntensity > 0.0 && u_maxRange > 0.0) {
      float dist = length(v_pos) / u_maxRange;
      float vig = 1.0 - u_vignetteIntensity * smoothstep(0.4, 1.0, dist);
      color.rgb *= vig;
    }
    color.a *= u_transitionAlpha;
    gl_FragColor = color;
  }
`;

// --- Contour (second pass) shader ---

const CONTOUR_VERTEX_SHADER = `
  attribute vec2 a_position;
  attribute float a_value;
  uniform mat3 u_matrix;
  varying float v_value;
  void main() {
    vec3 pos = u_matrix * vec3(a_position, 1.0);
    gl_Position = vec4(pos.xy, 0.0, 1.0);
    v_value = a_value;
  }
`;

const CONTOUR_FRAGMENT_SHADER = `
  #extension GL_OES_standard_derivatives : enable
  #ifdef GL_FRAGMENT_PRECISION_HIGH
  precision highp float;
  #else
  precision mediump float;
  #endif
  varying float v_value;
  uniform float u_vmin;
  uniform float u_vmax;
  uniform float u_nodata;
  uniform float u_contourInterval;
  uniform vec3 u_contourColor;
  uniform float u_contourWidth;
  uniform vec4 u_mask;
${GLSL_VALUE_MASK}
  void main() {
    if (v_value <= u_nodata + 0.5 || valueHidden(v_value, u_mask)) {
      discard;
    }
    float interval = u_contourInterval;
    float wrapped = mod(v_value, interval);
    float dv = fwidth(v_value);
    float halfWidth = u_contourWidth * 0.5;
    float edge = smoothstep(halfWidth + dv, max(halfWidth - dv, 0.0),
                            min(wrapped, interval - wrapped));
    if (edge < 0.01) {
      discard;
    }
    gl_FragColor = vec4(u_contourColor, edge * 0.85);
  }
`;

// ─── Ring overlay shader (draws range rings + crosshair) ────────────────

const RING_VERTEX_SHADER = `
  attribute vec2 a_position;
  uniform mat3 u_matrix;
  void main() {
    vec3 pos = u_matrix * vec3(a_position, 1.0);
    gl_Position = vec4(pos.xy, 0.0, 1.0);
  }
`;

const RING_FRAGMENT_SHADER = `
  precision mediump float;
  uniform vec4 u_color;
  void main() {
    gl_FragColor = u_color;
  }
`;

// ─── Helper: compile & link a WebGL program ─────────────────────────────

function _compileShader(gl: WebGLRenderingContext, type: number, source: string): WebGLShader | null {
  const shader = gl.createShader(type)!;
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    console.error('[PPI] Shader compile error:', gl.getShaderInfoLog(shader));
    gl.deleteShader(shader);
    return null;
  }
  return shader;
}

function _linkProgram(gl: WebGLRenderingContext, vsSrc: string, fsSrc: string): WebGLProgram | null {
  const vs = _compileShader(gl, gl.VERTEX_SHADER, vsSrc);
  const fs = _compileShader(gl, gl.FRAGMENT_SHADER, fsSrc);
  if (!vs || !fs) {
    if (vs) gl.deleteShader(vs);
    if (fs) gl.deleteShader(fs);
    return null;
  }
  const prog = gl.createProgram()!;
  gl.attachShader(prog, vs);
  gl.attachShader(prog, fs);
  gl.linkProgram(prog);
  // The linked program keeps its own copy; the shader objects are no longer needed.
  gl.detachShader(prog, vs);
  gl.detachShader(prog, fs);
  gl.deleteShader(vs);
  gl.deleteShader(fs);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    console.error('[PPI] Program link failed:', gl.getProgramInfoLog(prog));
    gl.deleteProgram(prog);
    return null;
  }
  return prog;
}

// ─── Uniform location bags ──────────────────────────────────────────────

interface SharpLocs {
  aPosition: number;
  aValue: number;
  uMatrix: WebGLUniformLocation | null;
  uVmin: WebGLUniformLocation | null;
  uVmax: WebGLUniformLocation | null;
  uNodata: WebGLUniformLocation | null;
  uColormap: WebGLUniformLocation | null;
  uVignetteIntensity: WebGLUniformLocation | null;
  uMaxRange: WebGLUniformLocation | null;
  uTransitionAlpha: WebGLUniformLocation | null;
  uMask: WebGLUniformLocation | null;
}

interface SmoothLocs {
  aPosition: number;
  aPolar: number;
  aValues4: number;
  uMatrix: WebGLUniformLocation | null;
  uVmin: WebGLUniformLocation | null;
  uVmax: WebGLUniformLocation | null;
  uNodata: WebGLUniformLocation | null;
  uColormap: WebGLUniformLocation | null;
  uVignetteIntensity: WebGLUniformLocation | null;
  uMaxRange: WebGLUniformLocation | null;
  uTransitionAlpha: WebGLUniformLocation | null;
  uMask: WebGLUniformLocation | null;
}

interface ContourLocs {
  aPosition: number;
  aValue: number;
  uMatrix: WebGLUniformLocation | null;
  uVmin: WebGLUniformLocation | null;
  uVmax: WebGLUniformLocation | null;
  uNodata: WebGLUniformLocation | null;
  uContourInterval: WebGLUniformLocation | null;
  uContourColor: WebGLUniformLocation | null;
  uContourWidth: WebGLUniformLocation | null;
  uMask: WebGLUniformLocation | null;
}

// ─── WebGL PPI Renderer ─────────────────────────────────────────────────

/**
 * Meshes are cached per "variable:sweep", but the same key can carry different
 * data (e.g. a QC'd or difference result replacing the raw sweep). This records
 * which entry a cached mesh was built from so stale meshes are never reused.
 */
const meshOwner = new WeakMap<Float32Array, SweepDataEntry>();

/** CPU-side copy (reference) of the sharp mesh, kept for context restore. */
interface SharpMeshData {
  positions: Float32Array;
  vals: Float32Array;
  vertexCount: number;
}

interface PendingMesh {
  resolve: (r: MeshWorkerOutput) => void;
  reject: (e: unknown) => void;
}

/** Minimum interval between perfStats publications (avoids a store update per frame). */
const PERF_PUBLISH_INTERVAL_MS = 200;

export class PPIRenderer {
  private gl: WebGLRenderingContext | null = null;
  private canvas: HTMLCanvasElement | null = null;

  // --- Shader programs ---
  private sharpProgram: WebGLProgram | null = null;
  private sharpLocs: SharpLocs | null = null;
  private smoothProgram: WebGLProgram | null = null;
  private smoothLocs: SmoothLocs | null = null;
  private contourProgram: WebGLProgram | null = null;
  private contourLocs: ContourLocs | null = null;
  private ringProgram: WebGLProgram | null = null;
  private ruMatrix: WebGLUniformLocation | null = null;
  private ruColor: WebGLUniformLocation | null = null;
  private raPosition = -1;

  // --- Sharp (flat) buffers ---
  private posBuffer: WebGLBuffer | null = null;
  private valBuffer: WebGLBuffer | null = null;
  private vertexCount = 0;

  // --- Smooth (bilinear) buffers ---
  private smoothPosBuffer: WebGLBuffer | null = null;
  private smoothPolarBuffer: WebGLBuffer | null = null;
  private smoothVal4Buffer: WebGLBuffer | null = null;
  private smoothVertexCount = 0;

  // --- Transition: secondary buffer set for cross-fade ---
  private transPosBuffer: WebGLBuffer | null = null;
  private transValBuffer: WebGLBuffer | null = null;
  private transVertexCount = 0;
  private transSmoothPosBuffer: WebGLBuffer | null = null;
  private transSmoothPolarBuffer: WebGLBuffer | null = null;
  private transSmoothVal4Buffer: WebGLBuffer | null = null;
  private transSmoothVertexCount = 0;
  private transitionActive = false;
  private transitionAlpha = 1.0;
  private transitionStart = 0;
  private transitionDuration = 0;
  private transitionRafId = 0;

  // Colormap / ring / view state
  private colormapTex: WebGLTexture | null = null;
  /** The value mask as the shader reads it (mode, lower, upper, inclusive); off = all zeros. */
  private maskUniform: MaskVec4 = MASK_OFF;
  /** Theme colours for the map-hidden background and rings (null = the original dark navy). */
  private plainSurface: { bg: [number, number, number]; ring: [number, number, number, number] } | null = null;
  /** Name of the colormap currently resident in the GPU texture ('' = none). */
  private currentCmapName = '';
  /** Data array that was uploaded for currentCmapName (detects re-registered colormaps). */
  private currentCmapData: Uint8Array | null = null;
  /** Last colormap requested (re-uploaded after a context restore). */
  private wantedCmapName = '';
  private ringBuffer: WebGLBuffer | null = null;
  private ringVertexCount = 0;
  private ringRange = -1;
  private scale = 1;
  private panX = 0;
  private panY = 0;
  private maxRange = 1;
  private readonly matrixBuf = new Float32Array(9);

  // Mesh worker
  private meshWorker: Worker | null = null;
  private meshWorkerReady = false;
  private meshReqSeq = 0;
  private pendingMesh = new Map<number, PendingMesh>();

  // Upload bookkeeping
  /** Entry most recently passed to uploadSweepData (applied or still building). */
  private requestedEntry: SweepDataEntry | null = null;
  /** Bumped per upload; async mesh results for older uploads are discarded. */
  private uploadSeq = 0;
  private smoothSeq = 0;
  /** Entry whose sharp / smooth mesh is currently resident (and drawn). */
  private sharpEntry: SweepDataEntry | null = null;
  private smoothEntry: SweepDataEntry | null = null;
  private cpuSharp: SharpMeshData | null = null;
  private cpuSmooth: SmoothMesh | null = null;
  /** positions array currently in posBuffer (skips redundant re-uploads). */
  private gpuSharpSrc: Float32Array | null = null;

  // Feature toggles
  private interpolationMode: 'sharp' | 'smooth' = 'sharp';
  private contoursEnabled = false;
  private contourInterval = 5;
  private contourColor: [number, number, number] = [1, 1, 1];
  private contourWidthVal = 1.5;
  private vignetteIntensity = 0.0;
  private transparentBg = false;

  // FPS counter
  private fpsEnabled = false;
  private fpsFrameCount = 0;
  private fpsLastTime = 0;
  private fpsValue = 0;
  private fpsCallback: ((fps: number) => void) | null = null;

  // ANGLE_instanced_arrays (stored for future instanced rendering)
  private instancedExt: ANGLE_instanced_arrays | null = null;

  // Stored render params
  private lastVmin = 0;
  private lastVmax = 1;
  private hasRenderParams = false;

  // Frame scheduling: all state changes just mark the renderer dirty; at most
  // one draw happens per animation frame.
  private dirty = false;
  private rafId = 0;
  private destroyed = false;
  private contextLost = false;
  private lastPerfPublish = 0;

  // CSS size (for devicePixelRatio changes)
  private cssWidth = 0;
  private cssHeight = 0;
  private dprQuery: MediaQueryList | null = null;

  // ─── Initialization ───────────────────────────────────────────────────

  init(canvas: HTMLCanvasElement): boolean {
    this.canvas = canvas;
    this.destroyed = false;
    const gl = canvas.getContext('webgl', {
      antialias: true,
      premultipliedAlpha: false,
      preserveDrawingBuffer: true,
      alpha: true,
    });
    if (!gl) return false;
    this.gl = gl;

    canvas.addEventListener('webglcontextlost', this.onContextLost, false);
    canvas.addEventListener('webglcontextrestored', this.onContextRestored, false);

    if (!this.initGLResources(gl)) return false;

    // Initialize mesh worker
    this.initMeshWorker();
    this.watchDpr();

    return true;
  }

  /** Create programs, buffers, texture and GL state. Also used after a context restore. */
  private initGLResources(gl: WebGLRenderingContext): boolean {
    // Extensions
    this.instancedExt = gl.getExtension('ANGLE_instanced_arrays');
    gl.getExtension('OES_standard_derivatives'); // for fwidth() in contour shader

    // --- Build shader programs ---

    // Sharp program (enhanced version of the original)
    const sharpProg = _linkProgram(gl, VERTEX_SHADER_SHARP, FRAGMENT_SHADER_SHARP);
    if (!sharpProg) return false;
    this.sharpProgram = sharpProg;
    this.sharpLocs = {
      aPosition: gl.getAttribLocation(sharpProg, 'a_position'),
      aValue: gl.getAttribLocation(sharpProg, 'a_value'),
      uMatrix: gl.getUniformLocation(sharpProg, 'u_matrix'),
      uVmin: gl.getUniformLocation(sharpProg, 'u_vmin'),
      uVmax: gl.getUniformLocation(sharpProg, 'u_vmax'),
      uNodata: gl.getUniformLocation(sharpProg, 'u_nodata'),
      uColormap: gl.getUniformLocation(sharpProg, 'u_colormap'),
      uVignetteIntensity: gl.getUniformLocation(sharpProg, 'u_vignetteIntensity'),
      uMaxRange: gl.getUniformLocation(sharpProg, 'u_maxRange'),
      uTransitionAlpha: gl.getUniformLocation(sharpProg, 'u_transitionAlpha'),
      uMask: gl.getUniformLocation(sharpProg, 'u_mask'),
    };

    // Smooth program (bilinear interpolation)
    this.smoothProgram = null;
    this.smoothLocs = null;
    const smoothProg = _linkProgram(gl, VERTEX_SHADER_SMOOTH, FRAGMENT_SHADER_SMOOTH);
    if (smoothProg) {
      this.smoothProgram = smoothProg;
      this.smoothLocs = {
        aPosition: gl.getAttribLocation(smoothProg, 'a_position'),
        aPolar: gl.getAttribLocation(smoothProg, 'a_polar'),
        aValues4: gl.getAttribLocation(smoothProg, 'a_values4'),
        uMatrix: gl.getUniformLocation(smoothProg, 'u_matrix'),
        uVmin: gl.getUniformLocation(smoothProg, 'u_vmin'),
        uVmax: gl.getUniformLocation(smoothProg, 'u_vmax'),
        uNodata: gl.getUniformLocation(smoothProg, 'u_nodata'),
        uColormap: gl.getUniformLocation(smoothProg, 'u_colormap'),
        uVignetteIntensity: gl.getUniformLocation(smoothProg, 'u_vignetteIntensity'),
        uMaxRange: gl.getUniformLocation(smoothProg, 'u_maxRange'),
        uTransitionAlpha: gl.getUniformLocation(smoothProg, 'u_transitionAlpha'),
        uMask: gl.getUniformLocation(smoothProg, 'u_mask'),
      };
    }

    // Contour program
    this.contourProgram = null;
    this.contourLocs = null;
    const contourProg = _linkProgram(gl, CONTOUR_VERTEX_SHADER, CONTOUR_FRAGMENT_SHADER);
    if (contourProg) {
      this.contourProgram = contourProg;
      this.contourLocs = {
        aPosition: gl.getAttribLocation(contourProg, 'a_position'),
        aValue: gl.getAttribLocation(contourProg, 'a_value'),
        uMatrix: gl.getUniformLocation(contourProg, 'u_matrix'),
        uVmin: gl.getUniformLocation(contourProg, 'u_vmin'),
        uVmax: gl.getUniformLocation(contourProg, 'u_vmax'),
        uNodata: gl.getUniformLocation(contourProg, 'u_nodata'),
        uContourInterval: gl.getUniformLocation(contourProg, 'u_contourInterval'),
        uContourColor: gl.getUniformLocation(contourProg, 'u_contourColor'),
        uContourWidth: gl.getUniformLocation(contourProg, 'u_contourWidth'),
        uMask: gl.getUniformLocation(contourProg, 'u_mask'),
      };
    }

    // Ring program
    this.ringProgram = null;
    const ringProg = _linkProgram(gl, RING_VERTEX_SHADER, RING_FRAGMENT_SHADER);
    if (ringProg) {
      this.ringProgram = ringProg;
      this.raPosition = gl.getAttribLocation(ringProg, 'a_position');
      this.ruMatrix = gl.getUniformLocation(ringProg, 'u_matrix');
      this.ruColor = gl.getUniformLocation(ringProg, 'u_color');
    }

    // --- Create all buffers ---
    this.posBuffer = gl.createBuffer();
    this.valBuffer = gl.createBuffer();
    this.smoothPosBuffer = gl.createBuffer();
    this.smoothPolarBuffer = gl.createBuffer();
    this.smoothVal4Buffer = gl.createBuffer();
    this.transPosBuffer = gl.createBuffer();
    this.transValBuffer = gl.createBuffer();
    this.transSmoothPosBuffer = gl.createBuffer();
    this.transSmoothPolarBuffer = gl.createBuffer();
    this.transSmoothVal4Buffer = gl.createBuffer();
    this.ringBuffer = gl.createBuffer();
    this.colormapTex = gl.createTexture();
    this.currentCmapName = '';
    this.currentCmapData = null;
    this.gpuSharpSrc = null;
    this.ringRange = -1;

    // Dark background matching the app theme
    gl.clearColor(15 / 255, 15 / 255, 26 / 255, 1);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

    return true;
  }

  // ─── Context loss ─────────────────────────────────────────────────────

  private onContextLost = (e: Event): void => {
    // preventDefault is what allows 'webglcontextrestored' to fire.
    e.preventDefault();
    this.contextLost = true;
    this.cancelFrame();
    this.endTransition();
  };

  private onContextRestored = (): void => {
    const gl = this.gl;
    if (!gl || this.destroyed) return;
    this.contextLost = false;
    if (!this.initGLResources(gl)) {
      console.error('[PPI] Failed to rebuild GL resources after context restore');
      return;
    }
    // Everything on the GPU is gone: re-upload from the CPU-side meshes.
    const sharp = this.cpuSharp;
    this.vertexCount = 0;
    this.smoothVertexCount = 0;
    this.transVertexCount = 0;
    this.transSmoothVertexCount = 0;
    if (sharp) this.uploadMeshToGPU(sharp.positions, sharp.vals, sharp.vertexCount);
    if (this.cpuSmooth) this.uploadSmoothToGPU(this.cpuSmooth);
    this.buildRingGeometry(this.maxRange);
    if (this.wantedCmapName) this.setColormap(this.wantedCmapName);
    this.markDirty();
  };

  /** True while the WebGL context is lost (nothing is drawn until it is restored). */
  isContextLost(): boolean {
    return this.contextLost;
  }

  // ─── Public API: feature toggles ──────────────────────────────────────

  /** Switch between 'sharp' (flat per-gate) and 'smooth' (bilinear interpolation). */
  setInterpolation(mode: 'sharp' | 'smooth'): void {
    if (mode === this.interpolationMode) return;
    this.interpolationMode = mode;
    if (mode === 'smooth') {
      // The smooth mesh is only built on demand: it is ~3x the size of the sharp mesh.
      if (this.requestedEntry) this.ensureSmooth(this.requestedEntry);
    } else {
      this.releaseSmooth();
    }
    this.markDirty();
  }

  /** Enable/disable contour lines with configurable interval and color. */
  setContours(enabled: boolean, interval: number = 5, color: [number, number, number] = [1, 1, 1]): void {
    const changed =
      enabled !== this.contoursEnabled ||
      interval !== this.contourInterval ||
      color[0] !== this.contourColor[0] || color[1] !== this.contourColor[1] || color[2] !== this.contourColor[2];
    this.contoursEnabled = enabled;
    this.contourInterval = interval;
    this.contourColor = color;
    if (changed) this.markDirty();
  }

  /** Set contour line width (default 1.5). */
  setContourWidth(width: number): void {
    if (width === this.contourWidthVal) return;
    this.contourWidthVal = width;
    this.markDirty();
  }

  /** Set vignette intensity (0 = off, 1 = strong darkening at edges). */
  setVignette(intensity: number): void {
    const v = Math.max(0, Math.min(1, intensity));
    if (v === this.vignetteIntensity) return;
    this.vignetteIntensity = v;
    this.markDirty();
  }

  /**
   * Hide the gates the value mask hides (same rules as everywhere, see stores/valueMask). Applied
   * in the shader, so changing it needs no new mesh and no data transfer.
   */
  setValueMask(mask: ValueMask | null): void {
    const next = maskVec(mask);
    if (next.every((x, i) => x === this.maskUniform[i])) return;
    this.maskUniform = next;
    this.markDirty();
  }

  /**
   * Colours of the plain background used while the map is hidden (not transparent): the canvas
   * colour and the range-ring colour, both taken from the active theme by the viewer. `null` keeps
   * the original dark navy.
   */
  setPlainSurface(s: { bg: [number, number, number]; ring: [number, number, number, number] } | null): void {
    const a = this.plainSurface;
    if (a === s || (a && s && a.bg.every((x, i) => x === s.bg[i]) && a.ring.every((x, i) => x === s.ring[i]))) return;
    this.plainSurface = s;
    this.markDirty();
  }

  /** Enable/disable transparent background (no-data regions become see-through). */
  setTransparentBackground(enabled: boolean): void {
    if (enabled === this.transparentBg) return;
    this.transparentBg = enabled;
    this.markDirty();
  }

  /** Enable/disable FPS counter. Provide a callback to receive FPS updates. */
  setFpsCounter(enabled: boolean, callback?: (fps: number) => void): void {
    this.fpsEnabled = enabled;
    this.fpsCallback = callback ?? null;
    if (enabled) {
      this.fpsFrameCount = 0;
      this.fpsLastTime = performance.now();
    }
  }

  /** Get the current FPS value (0 if counter is disabled). */
  getFps(): number {
    return this.fpsEnabled ? this.fpsValue : 0;
  }

  // ─── Mesh worker ──────────────────────────────────────────────────────

  /** Spawn the mesh computation Web Worker. */
  private initMeshWorker(): void {
    if (this.meshWorker) return;
    try {
      const worker = new Worker(
        new URL('../workers/meshWorker.ts', import.meta.url),
        { type: 'module' }
      );
      this.meshWorker = worker;
      this.meshWorkerReady = true;

      worker.onmessage = (e: MessageEvent<MeshWorkerOutput>) => {
        // Results are matched by id, never by arrival order.
        const pending = this.pendingMesh.get(e.data.id);
        if (pending) {
          this.pendingMesh.delete(e.data.id);
          pending.resolve(e.data);
        }
      };

      worker.onerror = (err) => {
        console.warn('[PPI] Mesh worker error, falling back to main thread:', err);
        this.meshWorkerReady = false;
        this.failPendingMeshes(err);
      };
    } catch (err) {
      console.warn('[PPI] Could not create mesh worker, using main thread:', err);
      this.meshWorkerReady = false;
    }
  }

  private failPendingMeshes(reason: unknown): void {
    const pending = [...this.pendingMesh.values()];
    this.pendingMesh.clear();
    for (const p of pending) p.reject(reason);
  }

  /** Send mesh work to the worker and return a promise for the result. */
  private buildMeshInWorker(entry: SweepDataEntry, mode: 'sharp' | 'smooth'): Promise<MeshWorkerOutput> {
    return new Promise((resolve, reject) => {
      const worker = this.meshWorker;
      if (!worker || !this.meshWorkerReady) {
        reject(new Error('mesh worker unavailable'));
        return;
      }
      const id = ++this.meshReqSeq;
      this.pendingMesh.set(id, { resolve, reject });
      try {
        // Structured clone: the cached entry keeps its own arrays. If the three
        // arrays are views over one buffer (the websocket path), that buffer is
        // cloned once.
        worker.postMessage({
          id,
          mode,
          azimuth: entry.azimuth,
          range: entry.range,
          values: entry.values,
          nAz: entry.nAz,
          nRange: entry.nRange,
        });
      } catch (err) {
        this.pendingMesh.delete(id);
        reject(err);
      }
    });
  }

  // ─── Sweep data upload ────────────────────────────────────────────────

  /** Return a cached sharp mesh only if it was built from exactly this entry. */
  private lookupMesh(entry: SweepDataEntry): SharpMeshData | null {
    const cached = renderCache.get(entry.variable, entry.sweep);
    if (cached && meshOwner.get(cached.positions) === entry) return cached;
    return null;
  }

  private storeMesh(entry: SweepDataEntry, positions: Float32Array, vals: Float32Array, vertexCount: number): void {
    meshOwner.set(positions, entry);
    renderCache.put(entry.variable, entry.sweep, {
      positions,
      vals,
      vertexCount,
      key: `${entry.variable}:${entry.sweep}`,
    });
  }

  /**
   * Generate triangle mesh from polar data and upload to GPU.
   * Uses the render cache when available, otherwise computes via Web Worker
   * (falling back to main thread if the worker is unavailable). The draw
   * happens automatically when the mesh is ready, so callers do not need to
   * re-render after an asynchronous build.
   * The bilinear mesh is only built while interpolation mode is 'smooth'.
   */
  uploadSweepData(entry: SweepDataEntry, force = false): void {
    if (!this.gl || this.destroyed) return;

    // Same entry as last time: the mesh is already resident (or being built).
    if (!force && entry === this.requestedEntry) {
      this.markDirty();
      return;
    }
    this.requestedEntry = entry;
    const seq = ++this.uploadSeq;

    const cached = this.lookupMesh(entry);
    if (cached) {
      perfStats.update(s => ({ ...s, meshBuildTimeMs: 0 }));
      this.applySharpMesh(entry, cached.positions, cached.vals, cached.vertexCount);
    } else if (this.meshWorkerReady && this.meshWorker) {
      const requestedAt = performance.now();
      this.buildMeshInWorker(entry, 'sharp').then((result) => {
        if (this.destroyed) return;
        const roundTrip = performance.now() - requestedAt;
        perfStats.update(s => ({
          ...s,
          meshBuildTimeMs: result.buildTimeMs,
          dataTransferTimeMs: Math.max(0, roundTrip - result.buildTimeMs),
        }));
        // Cache even if superseded: it is still a valid mesh for this entry.
        this.storeMesh(entry, result.positions, result.vals, result.vertexCount);
        if (seq === this.uploadSeq) {
          this.applySharpMesh(entry, result.positions, result.vals, result.vertexCount);
        }
      }).catch(() => {
        // Worker died or was unavailable: build on the main thread instead.
        if (!this.destroyed && seq === this.uploadSeq) this.buildSharpOnMainThread(entry);
      });
    } else {
      this.buildSharpOnMainThread(entry);
    }

    if (this.interpolationMode === 'smooth') this.ensureSmooth(entry);
  }

  private buildSharpOnMainThread(entry: SweepDataEntry): void {
    const t0 = performance.now();
    const mesh = buildSharpMesh(entry.azimuth, entry.range, entry.values, entry.nAz, entry.nRange);
    perfStats.update(s => ({ ...s, meshBuildTimeMs: performance.now() - t0, dataTransferTimeMs: 0 }));
    this.storeMesh(entry, mesh.positions, mesh.vals, mesh.vertexCount);
    this.applySharpMesh(entry, mesh.positions, mesh.vals, mesh.vertexCount);
  }

  /** Make a sharp mesh the one that is drawn. */
  private applySharpMesh(entry: SweepDataEntry, positions: Float32Array, vals: Float32Array, vertexCount: number): void {
    this.cpuSharp = { positions, vals, vertexCount };
    this.sharpEntry = entry;
    this.setMaxRange(entry.maxRange);
    this.uploadMeshToGPU(positions, vals, vertexCount);
    // A smooth mesh of a different sweep must not be drawn over the new data.
    if (this.smoothEntry !== entry) this.dropSmoothMesh();
    this.markDirty();
  }

  /** Upload pre-computed sharp mesh arrays to WebGL buffers. */
  private uploadMeshToGPU(positions: Float32Array, vals: Float32Array, vertexCount: number): void {
    const gl = this.gl;
    if (!gl || this.contextLost) return;

    this.vertexCount = vertexCount;
    // Identical mesh already on the GPU (e.g. same sweep re-selected): nothing to do.
    if (this.gpuSharpSrc === positions) return;
    this.gpuSharpSrc = positions;

    gl.bindBuffer(gl.ARRAY_BUFFER, this.posBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, positions, gl.STATIC_DRAW);

    gl.bindBuffer(gl.ARRAY_BUFFER, this.valBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, vals, gl.STATIC_DRAW);
  }

  // ─── Smooth mesh (on demand) ──────────────────────────────────────────

  /** Build the bilinear mesh for `entry` (worker first, main thread as fallback). */
  private ensureSmooth(entry: SweepDataEntry): void {
    if (!this.smoothProgram || this.destroyed) return;
    if (this.smoothEntry === entry) return;
    const seq = ++this.smoothSeq;
    const accept = () => seq === this.smoothSeq && !this.destroyed
      && this.interpolationMode === 'smooth' && this.requestedEntry === entry;

    const buildSync = () => {
      if (!accept()) return;
      const m = buildSmoothMesh(entry.azimuth, entry.range, entry.values, entry.nAz, entry.nRange);
      this.applySmoothMesh(entry, m);
    };

    if (this.meshWorkerReady && this.meshWorker) {
      this.buildMeshInWorker(entry, 'smooth').then((r) => {
        if (!accept()) return;
        this.applySmoothMesh(entry, {
          positions: r.positions,
          polars: r.polars!,
          values4: r.values4!,
          vertexCount: r.vertexCount,
        });
      }).catch(buildSync);
    } else {
      buildSync();
    }
  }

  private applySmoothMesh(entry: SweepDataEntry, mesh: SmoothMesh): void {
    this.cpuSmooth = mesh;
    this.smoothEntry = entry;
    this.setMaxRange(entry.maxRange);
    this.uploadSmoothToGPU(mesh);
    // Do not draw (or contour) a sharp mesh that belongs to a different sweep.
    if (this.sharpEntry !== entry) {
      this.vertexCount = 0;
      this.gpuSharpSrc = null;
    }
    this.markDirty();
  }

  private uploadSmoothToGPU(mesh: SmoothMesh): void {
    const gl = this.gl;
    if (!gl || this.contextLost) return;
    this.smoothVertexCount = mesh.vertexCount;

    gl.bindBuffer(gl.ARRAY_BUFFER, this.smoothPosBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, mesh.positions, gl.STATIC_DRAW);

    gl.bindBuffer(gl.ARRAY_BUFFER, this.smoothPolarBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, mesh.polars, gl.STATIC_DRAW);

    gl.bindBuffer(gl.ARRAY_BUFFER, this.smoothVal4Buffer);
    gl.bufferData(gl.ARRAY_BUFFER, mesh.values4, gl.STATIC_DRAW);
  }

  /** Forget the smooth mesh (CPU and GPU memory) without cancelling a pending build. */
  private dropSmoothMesh(): void {
    if (!this.cpuSmooth && this.smoothVertexCount === 0) {
      this.smoothEntry = null;
      return; // nothing resident (the common case in sharp mode)
    }
    this.cpuSmooth = null;
    this.smoothEntry = null;
    this.smoothVertexCount = 0;
    const gl = this.gl;
    if (gl && !this.contextLost) {
      for (const buf of [this.smoothPosBuffer, this.smoothPolarBuffer, this.smoothVal4Buffer]) {
        gl.bindBuffer(gl.ARRAY_BUFFER, buf);
        gl.bufferData(gl.ARRAY_BUFFER, 0, gl.STATIC_DRAW); // free GPU memory
      }
    }
  }

  private releaseSmooth(): void {
    this.smoothSeq++; // cancels any in-flight smooth build
    this.dropSmoothMesh();
  }

  /** Update the data extent and rebuild the range rings if it changed. */
  private setMaxRange(maxRange: number): void {
    this.maxRange = maxRange;
    if (this.ringRange !== maxRange) this.buildRingGeometry(maxRange);
  }

  /** Upload a colormap texture by name. */
  setColormap(cmapName: string): void {
    const data = COLORMAP_DATA[cmapName];
    if (!data) return;
    this.wantedCmapName = cmapName;
    // Skip only if the same *data* is already resident (a re-registered custom
    // colormap with the same name gets a new array and must be re-uploaded).
    if (cmapName === this.currentCmapName && data === this.currentCmapData) return;

    const gl = this.gl;
    if (!gl || this.contextLost) return;
    this.currentCmapName = cmapName;
    this.currentCmapData = data;
    const rgba = buildColormapRGBA(cmapName);

    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.colormapTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, rgba);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    this.markDirty();
  }

  private buildRingGeometry(maxRange: number): void {
    const gl = this.gl;
    if (!gl || !this.ringProgram || this.contextLost) return;
    this.ringRange = maxRange;

    const segments = 128;

    const ringSpacingKm = maxRange > 300000 ? 100 : maxRange > 100000 ? 50 : maxRange > 30000 ? 25 : 10;
    const ringSpacing = ringSpacingKm * 1000;
    const numRings = Math.max(0, Math.floor(maxRange / ringSpacing));

    // rings: 2 vertices per segment; crosshair: 4 vertices
    const verts = new Float32Array((numRings * segments * 2 + 4) * 2);
    let o = 0;

    for (let ri = 1; ri <= numRings; ri++) {
      const r = ri * ringSpacing;
      for (let s = 0; s < segments; s++) {
        const a0 = (s / segments) * Math.PI * 2;
        const a1 = ((s + 1) / segments) * Math.PI * 2;
        verts[o++] = r * Math.sin(a0); verts[o++] = r * Math.cos(a0);
        verts[o++] = r * Math.sin(a1); verts[o++] = r * Math.cos(a1);
      }
    }

    // Crosshair (N-S and E-W lines)
    verts[o++] = 0; verts[o++] = -maxRange; verts[o++] = 0; verts[o++] = maxRange;
    verts[o++] = -maxRange; verts[o++] = 0; verts[o++] = maxRange; verts[o++] = 0;

    this.ringVertexCount = verts.length / 2;
    gl.bindBuffer(gl.ARRAY_BUFFER, this.ringBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, verts, gl.STATIC_DRAW);
  }

  // ─── View matrix ──────────────────────────────────────────────────────

  /** Fill (and return) the reusable 3x3 column-major view matrix. */
  private buildMatrix(): Float32Array {
    const w = this.canvas!.width;
    const h = this.canvas!.height;
    const mr = this.maxRange;
    const aspect = w / h;
    const base = this.scale / mr;
    const sx = aspect >= 1 ? base / aspect : base;
    const sy = aspect >= 1 ? base : base * aspect;
    const m = this.matrixBuf;
    m[0] = sx; m[1] = 0; m[2] = 0;
    m[3] = 0; m[4] = sy; m[5] = 0;
    m[6] = this.panX * sx; m[7] = this.panY * sy; m[8] = 1;
    return m;
  }

  // ─── Shared uniform setter ────────────────────────────────────────────

  private setCommonUniforms(
    gl: WebGLRenderingContext,
    locs: {
      uVignetteIntensity: WebGLUniformLocation | null;
      uMaxRange: WebGLUniformLocation | null; uTransitionAlpha: WebGLUniformLocation | null;
      uMask: WebGLUniformLocation | null;
      uVmin: WebGLUniformLocation | null; uVmax: WebGLUniformLocation | null;
      uNodata: WebGLUniformLocation | null; uColormap: WebGLUniformLocation | null;
      uMatrix: WebGLUniformLocation | null;
    },
    matrix: Float32Array,
    vmin: number,
    vmax: number,
    alpha: number,
  ): void {
    gl.uniformMatrix3fv(locs.uMatrix, false, matrix);
    gl.uniform1f(locs.uVmin, vmin);
    gl.uniform1f(locs.uVmax, vmax);
    gl.uniform1f(locs.uNodata, -9999);
    gl.uniform1f(locs.uVignetteIntensity, this.vignetteIntensity);
    gl.uniform1f(locs.uMaxRange, this.maxRange);
    gl.uniform1f(locs.uTransitionAlpha, alpha);
    gl.uniform4fv(locs.uMask, this.maskUniform);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.colormapTex);
    gl.uniform1i(locs.uColormap, 0);
  }

  // ─── Render scheduling ────────────────────────────────────────────────

  /**
   * Request a render with the given colour range. Cheap and idempotent: the
   * frame is drawn on the next animation frame, and repeated calls within a
   * frame (pan/zoom/wheel bursts) collapse into one draw. Calls that change
   * nothing since the last draw are no-ops.
   */
  render(vmin: number, vmax: number): void {
    const changed = !this.hasRenderParams || vmin !== this.lastVmin || vmax !== this.lastVmax;
    this.lastVmin = vmin;
    this.lastVmax = vmax;
    this.hasRenderParams = true;
    if (changed) this.requestRender();
  }

  /** Force a redraw on the next frame even if no tracked state changed. */
  invalidate(): void {
    this.requestRender();
  }

  /** Draw immediately if a draw is pending (e.g. before reading the canvas back). */
  flush(): void {
    if (!this.dirty) return;
    this.cancelFrame();
    this.drawFrame();
  }

  /** State changed: schedule a draw (only once a first render() has supplied vmin/vmax). */
  private markDirty(): void {
    if (this.hasRenderParams) this.requestRender();
  }

  private requestRender(): void {
    if (!this.gl || this.destroyed || !this.hasRenderParams) return;
    this.dirty = true;
    if (!this.rafId) this.rafId = requestAnimationFrame(this.onFrame);
  }

  private onFrame = (): void => {
    this.rafId = 0;
    if (!this.dirty) return;
    this.dirty = false;
    this.drawFrame();
  };

  private cancelFrame(): void {
    if (this.rafId) {
      cancelAnimationFrame(this.rafId);
      this.rafId = 0;
    }
    this.dirty = false;
  }

  /** Draw the PPI with overlays. Always call through render()/markDirty(). */
  private drawFrame(): void {
    const gl = this.gl;
    if (!gl || !this.sharpProgram || !this.canvas || this.contextLost || this.destroyed) return;
    const w = this.canvas.width;
    const h = this.canvas.height;
    if (w <= 0 || h <= 0) return;

    const vmin = this.lastVmin;
    const vmax = this.lastVmax;
    const renderStart = performance.now();

    // FPS tracking
    if (this.fpsEnabled) {
      this.fpsFrameCount++;
      const now = renderStart;
      if (now - this.fpsLastTime >= 1000) {
        this.fpsValue = Math.round(this.fpsFrameCount * 1000 / (now - this.fpsLastTime));
        this.fpsFrameCount = 0;
        this.fpsLastTime = now;
        if (this.fpsCallback) this.fpsCallback(this.fpsValue);
      }
    }

    gl.viewport(0, 0, w, h);

    if (this.transparentBg) {
      gl.clearColor(0, 0, 0, 0);
    } else if (this.plainSurface) {
      const [r, g, b] = this.plainSurface.bg;
      gl.clearColor(r, g, b, 1);
    } else {
      gl.clearColor(15 / 255, 15 / 255, 26 / 255, 1);
    }
    gl.clear(gl.COLOR_BUFFER_BIT);

    const matrix = this.buildMatrix();

    // --- If transition is active, draw old data first with (1 - alpha) ---
    if (this.transitionActive) {
      const oldAlpha = 1.0 - this.transitionAlpha;
      this.drawPPIPass(gl, matrix, vmin, vmax, oldAlpha,
        this.transPosBuffer!, this.transValBuffer!, this.transVertexCount,
        this.transSmoothPosBuffer!, this.transSmoothPolarBuffer!, this.transSmoothVal4Buffer!, this.transSmoothVertexCount,
        this.transitionSmooth,
      );
    }

    // --- Draw current data ---
    const alpha = this.transitionActive ? this.transitionAlpha : 1.0;
    if (this.vertexCount > 0 || this.smoothVertexCount > 0) {
      this.drawPPIPass(gl, matrix, vmin, vmax, alpha,
        this.posBuffer!, this.valBuffer!, this.vertexCount,
        this.smoothPosBuffer!, this.smoothPolarBuffer!, this.smoothVal4Buffer!, this.smoothVertexCount,
        this.interpolationMode === 'smooth',
      );
    }

    // --- Contour pass ---
    if (this.contoursEnabled && this.contourProgram && this.contourLocs && this.vertexCount > 0) {
      this.drawContourPass(gl, matrix, vmin, vmax);
    }

    // --- Range rings ---
    if (this.ringProgram && this.ringVertexCount > 0) {
      gl.useProgram(this.ringProgram);
      gl.uniformMatrix3fv(this.ruMatrix, false, matrix);
      const ring = !this.transparentBg && this.plainSurface ? this.plainSurface.ring : [1, 1, 1, 0.15];
      gl.uniform4f(this.ruColor, ring[0], ring[1], ring[2], ring[3]);

      gl.bindBuffer(gl.ARRAY_BUFFER, this.ringBuffer);
      gl.enableVertexAttribArray(this.raPosition);
      gl.vertexAttribPointer(this.raPosition, 2, gl.FLOAT, false, 0, 0);

      gl.drawArrays(gl.LINES, 0, this.ringVertexCount);
      gl.disableVertexAttribArray(this.raPosition);
    }

    // Publish timing at a low rate: a store update per frame re-renders subscribers.
    if (renderStart - this.lastPerfPublish >= PERF_PUBLISH_INTERVAL_MS) {
      this.lastPerfPublish = renderStart;
      const renderTimeMs = performance.now() - renderStart;
      perfStats.update(s => ({ ...s, renderTimeMs }));
    }
  }

  /** Draw PPI triangles using either sharp or smooth mode. */
  private drawPPIPass(
    gl: WebGLRenderingContext,
    matrix: Float32Array,
    vmin: number,
    vmax: number,
    alpha: number,
    sharpPosBuf: WebGLBuffer,
    sharpValBuf: WebGLBuffer,
    sharpCount: number,
    smoothPosBuf: WebGLBuffer,
    smoothPolarBuf: WebGLBuffer,
    smoothVal4Buf: WebGLBuffer,
    smoothCount: number,
    wantSmooth: boolean,
  ): void {
    const useSmooth = wantSmooth
      && this.smoothProgram && this.smoothLocs && smoothCount > 0;

    if (useSmooth) {
      const locs = this.smoothLocs!;
      gl.useProgram(this.smoothProgram!);
      this.setCommonUniforms(gl, locs, matrix, vmin, vmax, alpha);

      gl.bindBuffer(gl.ARRAY_BUFFER, smoothPosBuf);
      gl.enableVertexAttribArray(locs.aPosition);
      gl.vertexAttribPointer(locs.aPosition, 2, gl.FLOAT, false, 0, 0);

      gl.bindBuffer(gl.ARRAY_BUFFER, smoothPolarBuf);
      gl.enableVertexAttribArray(locs.aPolar);
      gl.vertexAttribPointer(locs.aPolar, 2, gl.FLOAT, false, 0, 0);

      gl.bindBuffer(gl.ARRAY_BUFFER, smoothVal4Buf);
      gl.enableVertexAttribArray(locs.aValues4);
      gl.vertexAttribPointer(locs.aValues4, 4, gl.FLOAT, false, 0, 0);

      gl.drawArrays(gl.TRIANGLES, 0, smoothCount);

      gl.disableVertexAttribArray(locs.aPosition);
      gl.disableVertexAttribArray(locs.aPolar);
      gl.disableVertexAttribArray(locs.aValues4);
    } else if (sharpCount > 0) {
      const locs = this.sharpLocs!;
      gl.useProgram(this.sharpProgram!);
      this.setCommonUniforms(gl, locs, matrix, vmin, vmax, alpha);

      gl.bindBuffer(gl.ARRAY_BUFFER, sharpPosBuf);
      gl.enableVertexAttribArray(locs.aPosition);
      gl.vertexAttribPointer(locs.aPosition, 2, gl.FLOAT, false, 0, 0);

      gl.bindBuffer(gl.ARRAY_BUFFER, sharpValBuf);
      gl.enableVertexAttribArray(locs.aValue);
      gl.vertexAttribPointer(locs.aValue, 1, gl.FLOAT, false, 0, 0);

      gl.drawArrays(gl.TRIANGLES, 0, sharpCount);

      gl.disableVertexAttribArray(locs.aPosition);
      gl.disableVertexAttribArray(locs.aValue);
    }
  }

  /** Draw contour lines as a second pass over the sharp mesh. */
  private drawContourPass(gl: WebGLRenderingContext, matrix: Float32Array, vmin: number, vmax: number): void {
    const locs = this.contourLocs!;
    gl.useProgram(this.contourProgram!);

    gl.uniformMatrix3fv(locs.uMatrix, false, matrix);
    gl.uniform1f(locs.uVmin, vmin);
    gl.uniform1f(locs.uVmax, vmax);
    gl.uniform1f(locs.uNodata, -9999);
    gl.uniform1f(locs.uContourInterval, this.contourInterval);
    gl.uniform3fv(locs.uContourColor, this.contourColor);
    gl.uniform1f(locs.uContourWidth, this.contourWidthVal);
    gl.uniform4fv(locs.uMask, this.maskUniform);

    gl.bindBuffer(gl.ARRAY_BUFFER, this.posBuffer);
    gl.enableVertexAttribArray(locs.aPosition);
    gl.vertexAttribPointer(locs.aPosition, 2, gl.FLOAT, false, 0, 0);

    gl.bindBuffer(gl.ARRAY_BUFFER, this.valBuffer);
    gl.enableVertexAttribArray(locs.aValue);
    gl.vertexAttribPointer(locs.aValue, 1, gl.FLOAT, false, 0, 0);

    gl.drawArrays(gl.TRIANGLES, 0, this.vertexCount);

    gl.disableVertexAttribArray(locs.aPosition);
    gl.disableVertexAttribArray(locs.aValue);
  }

  // ─── Animation: cross-fade transition ─────────────────────────────────

  /** Whether the outgoing mesh of the running transition should be drawn smooth. */
  private transitionSmooth = false;

  /** Stop a running cross-fade (keeps the current mesh, drops the outgoing one). */
  private endTransition(): void {
    if (this.transitionRafId) {
      cancelAnimationFrame(this.transitionRafId);
      this.transitionRafId = 0;
    }
    this.transitionActive = false;
    this.transitionAlpha = 1.0;
    this.transVertexCount = 0;
    this.transSmoothVertexCount = 0;
  }

  /**
   * Smoothly cross-fade from the current sweep to new sweep data.
   * @param newData  The new sweep data to transition to.
   * @param duration Transition duration in milliseconds (default 500).
   */
  transitionToSweep(newData: SweepDataEntry, duration: number = 500): void {
    const gl = this.gl;
    if (!gl || this.contextLost || this.destroyed) return;

    // Cancel any in-progress transition
    if (this.transitionRafId) {
      cancelAnimationFrame(this.transitionRafId);
      this.transitionRafId = 0;
    }

    // Swap buffer references: current -> transition (old), upload new -> current
    let tmp: WebGLBuffer | null;
    let tmpN: number;

    tmp = this.posBuffer;
    this.posBuffer = this.transPosBuffer;
    this.transPosBuffer = tmp;

    tmp = this.valBuffer;
    this.valBuffer = this.transValBuffer;
    this.transValBuffer = tmp;

    tmpN = this.vertexCount;
    this.vertexCount = 0;
    this.transVertexCount = tmpN;

    tmp = this.smoothPosBuffer;
    this.smoothPosBuffer = this.transSmoothPosBuffer;
    this.transSmoothPosBuffer = tmp;

    tmp = this.smoothPolarBuffer;
    this.smoothPolarBuffer = this.transSmoothPolarBuffer;
    this.transSmoothPolarBuffer = tmp;

    tmp = this.smoothVal4Buffer;
    this.smoothVal4Buffer = this.transSmoothVal4Buffer;
    this.transSmoothVal4Buffer = tmp;

    tmpN = this.smoothVertexCount;
    this.smoothVertexCount = 0;
    this.transSmoothVertexCount = tmpN;
    this.transitionSmooth = this.interpolationMode === 'smooth';

    // The resident meshes now live in the transition buffers.
    this.gpuSharpSrc = null;
    this.sharpEntry = null;
    this.smoothEntry = null;
    this.cpuSharp = null;
    this.cpuSmooth = null;

    // Upload new data into the (now-swapped) current buffers
    this.uploadSweepData(newData, true);

    // Start transition animation
    this.transitionActive = true;
    this.transitionAlpha = 0;
    this.transitionStart = performance.now();
    this.transitionDuration = duration;

    const animate = () => {
      if (this.destroyed || !this.transitionActive) return;
      const elapsed = performance.now() - this.transitionStart;
      const t = Math.min(elapsed / this.transitionDuration, 1.0);
      // Smooth ease-in-out (smoothstep)
      this.transitionAlpha = t * t * (3 - 2 * t);

      // Already inside an animation frame: draw directly.
      this.cancelFrame();
      this.drawFrame();

      if (t < 1.0) {
        this.transitionRafId = requestAnimationFrame(animate);
      } else {
        this.transitionActive = false;
        this.transitionAlpha = 1.0;
        this.transitionRafId = 0;
        this.transVertexCount = 0;
        this.transSmoothVertexCount = 0;
      }
    };

    this.transitionRafId = requestAnimationFrame(animate);
  }

  // ─── View / resize / lifecycle ────────────────────────────────────────

  setView(scale: number, panX: number, panY: number): void {
    if (scale === this.scale && panX === this.panX && panY === this.panY) return;
    this.scale = scale;
    this.panX = panX;
    this.panY = panY;
    this.markDirty();
  }

  resize(width: number, height: number): void {
    if (!this.canvas) return;
    this.cssWidth = width;
    this.cssHeight = height;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.round(width * dpr));
    const h = Math.max(1, Math.round(height * dpr));
    // Assigning width/height clears the drawing buffer even when unchanged.
    if (w === this.canvas.width && h === this.canvas.height) return;
    this.canvas.width = w;
    this.canvas.height = h;

    // The buffer was just cleared and a ResizeObserver callback runs after this
    // frame's rAF phase: redraw now so no blank frame is ever presented.
    if (this.hasRenderParams && !this.contextLost) {
      this.cancelFrame();
      this.drawFrame();
    }
  }

  /** Re-apply the canvas size when devicePixelRatio changes (monitor switch, zoom). */
  private watchDpr(): void {
    this.unwatchDpr();
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return;
    const q = window.matchMedia(`(resolution: ${window.devicePixelRatio || 1}dppx)`);
    q.addEventListener?.('change', this.onDprChange);
    this.dprQuery = q;
  }

  private unwatchDpr(): void {
    this.dprQuery?.removeEventListener?.('change', this.onDprChange);
    this.dprQuery = null;
  }

  private onDprChange = (): void => {
    if (this.destroyed) return;
    this.watchDpr(); // the query is bound to the old ratio: re-arm for the new one
    if (this.cssWidth > 0 && this.cssHeight > 0) this.resize(this.cssWidth, this.cssHeight);
  };

  destroy(): void {
    this.destroyed = true;
    this.cancelFrame();
    this.endTransition();
    this.unwatchDpr();
    this.failPendingMeshes(new Error('renderer destroyed'));

    if (this.canvas) {
      this.canvas.removeEventListener('webglcontextlost', this.onContextLost, false);
      this.canvas.removeEventListener('webglcontextrestored', this.onContextRestored, false);
    }

    const gl = this.gl;
    if (gl && !this.contextLost) {
      const buffers = [
        this.posBuffer, this.valBuffer,
        this.smoothPosBuffer, this.smoothPolarBuffer, this.smoothVal4Buffer,
        this.transPosBuffer, this.transValBuffer,
        this.transSmoothPosBuffer, this.transSmoothPolarBuffer, this.transSmoothVal4Buffer,
        this.ringBuffer,
      ];
      for (const buf of buffers) {
        if (buf) gl.deleteBuffer(buf);
      }

      if (this.colormapTex) gl.deleteTexture(this.colormapTex);

      const programs = [this.sharpProgram, this.smoothProgram, this.contourProgram, this.ringProgram];
      for (const prog of programs) {
        if (prog) gl.deleteProgram(prog);
      }
      // Release the context now (the 2D view is rebuilt on every 2D/3D switch and file open)
      // instead of whenever the garbage collector reaches the canvas.
      try { gl.getExtension('WEBGL_lose_context')?.loseContext(); } catch { /* already lost */ }
    }

    this.gl = null;
    this.cpuSharp = null;
    this.cpuSmooth = null;
    this.requestedEntry = null;
    this.sharpEntry = null;
    this.smoothEntry = null;
    this.gpuSharpSrc = null;

    if (this.meshWorker) {
      this.meshWorker.terminate();
      this.meshWorker = null;
      this.meshWorkerReady = false;
    }
  }

  getMaxRange(): number {
    return this.maxRange;
  }
}
