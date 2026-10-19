/**
 * WebGL2 renderer of Cartesian 3D scenes: a gridded scalar volume (merged mosaic / wind grid)
 * ray-marched from a 3D texture, with 3D wind arrows drawn on top.
 *
 * It follows the polar radar view (RadarViewer3D) on purpose, so both read the same way:
 *  - one full-screen pass ray-marches the volume with the same transfer-function lookup, per-step
 *    opacity compensation, "hide below" threshold, optional gradient lighting and jittered ray
 *    start; ground (map / grid), rim and background come from the shared GLSL pieces
 *    (volumeShaderParts.ts);
 *  - frames are drawn by the ProgressiveRenderer / QualityGovernor of qualityGovernor.ts (bounded
 *    bands, render scale and ray step chosen from the measured GPU cost, nothing runs while idle);
 *  - the camera orbits / zooms / pans exactly like the polar view (same mouse and key bindings);
 *  - upload is time-sliced and bounded by the governor's texture budget.
 *
 * The volume is an RG8 3D texture (R = 8-bit code, G = 255 where there is data) so that hardware
 * trilinear filtering can be renormalised at the edges of the echo (value = R / G).
 *
 * Wind arrows are instanced meshes drawn after the volume with a depth buffer. Each arrow is
 * drawn twice: an inflated copy with front faces culled ("inverted hull", in the outline colour)
 * and the arrow itself in the fill colour, so it keeps a visible edge over any background.
 */

import {
  QualityGovernor,
  ProgressiveRenderer,
  yieldToMain,
  type ProgressiveJob,
  type QualityParams,
} from './qualityGovernor';
import { GLSL_HASH, GLSL_THRESHOLD, glslGroundColor } from './volumeShaderParts';
import { buildArrowMesh } from './windGlyphs';
import { chooseDownsample, packRG8Slab, type CartVolume, type SceneBox } from './cartesianVolume';

// ─── Shaders ────────────────────────────────────────────────────────────────

const QUAD_VERT = `#version 300 es
  in vec2 a_pos;
  void main() { gl_Position = vec4(a_pos, 0.0, 1.0); }
`;

function volumeFrag(): string {
  return `#version 300 es
    precision highp float;
    precision highp int;
    precision highp sampler3D;

    uniform vec3  u_camPos;
    uniform vec3  u_camRight;
    uniform vec3  u_camUp;
    uniform vec3  u_camFwd;
    uniform float u_tanHalfFovX;
    uniform float u_tanHalfFovY;
    uniform vec2  u_resolution;

    uniform highp sampler3D u_volume;    // RG8: R = code / 255 (0 = no data), G = 1 where there is data
    uniform sampler2D       u_transferFn;// value -> RGBA RGBA8
    uniform sampler2D       u_ground;    // map image over [-half, half]^2, row 0 = north

    uniform int   u_hasVol;
    uniform vec3  u_boxMin;        // volume box, scene units (km, z exaggerated)
    uniform vec3  u_boxMax;
    uniform vec2  u_groundMin;     // ground rectangle (km)
    uniform vec2  u_groundMax;
    uniform float u_stepSize;      // km per step (user step x quality multiplier)
    uniform float u_stepUser;      // km per step the user asked for (opacity reference)
    uniform int   u_maxSteps;
    uniform float u_opacityScale;
    uniform float u_thr;
    uniform float u_thrSoft;
    uniform int   u_thrMode;
    uniform int   u_groundMode;    // 0 off, 1 grid, 2 map
    uniform float u_groundSize;
    uniform float u_groundHalf;
    uniform float u_ringStep;
    uniform int   u_light;
    uniform float u_themeLight;    // 0 dark scene, 1 light scene

    out vec4 fragColor;

    // Field at a scene position: val 0..1 over [vmin, vmax], cov = fraction of the neighbourhood
    // with data (fades at the edges of the echo instead of cutting off).
    bool sampleField(vec3 pos, out float val, out float cov) {
      vec3 uvw = (pos - u_boxMin) / (u_boxMax - u_boxMin);
      val = 0.0; cov = 0.0;
      if (any(lessThan(uvw, vec3(0.0))) || any(greaterThan(uvw, vec3(1.0)))) return false;
      vec2 rg = texture(u_volume, uvw).rg;
      if (rg.g < 0.02) return false;
      float b = rg.r / rg.g;
      val = clamp((b * 255.0 - 1.0) / 254.0, 0.0, 1.0);
      cov = rg.g;
      return true;
    }
${GLSL_THRESHOLD}${GLSL_HASH}${glslGroundColor({
    prelude: 'vec3 bmin = vec3(u_groundMin, 0.0); vec3 bmax = vec3(u_groundMax, 0.0);',
    rimDist: 'min(min(gp.x - bmin.x, bmax.x - gp.x), min(gp.y - bmin.y, bmax.y - gp.y))',
    rings: false,
    themed: true,
  })}
    void main() {
      vec2 uv = (gl_FragCoord.xy / u_resolution - 0.5) * 2.0;
      vec3 rayDir = normalize(
        u_camFwd +
        uv.x * u_tanHalfFovX * u_camRight +
        uv.y * u_tanHalfFovY * u_camUp
      );
      vec3 o = u_camPos;

      float by = gl_FragCoord.y / u_resolution.y;
      vec3 bgDark  = mix(vec3(0.115, 0.135, 0.20), vec3(0.03, 0.037, 0.07), smoothstep(0.0, 1.0, by));
      vec3 bgLight = mix(vec3(0.97, 0.975, 0.985), vec3(0.80, 0.83, 0.89), smoothstep(0.0, 1.0, by));
      vec3 bg = mix(bgDark, bgLight, u_themeLight);

      // Ray against the volume box (above the ground)
      float t0 = 0.0;
      float t1 = 1e9;
      bool  hit = u_hasVol != 0;
      vec3 bmin = vec3(u_boxMin.xy, max(u_boxMin.z, 0.0));
      vec3 bmax = u_boxMax;
      if (hit) {
        vec3 inv = 1.0 / mix(rayDir, vec3(1e-6), lessThan(abs(rayDir), vec3(1e-6)));
        vec3 ta = (bmin - o) * inv;
        vec3 tb = (bmax - o) * inv;
        vec3 tn = min(ta, tb);
        vec3 tf = max(ta, tb);
        t0 = max(0.0, max(tn.x, max(tn.y, tn.z)));
        t1 = min(tf.x, min(tf.y, tf.z));
        if (t0 >= t1) hit = false;
      }

      // Ground plane z = 0 over the scene footprint (only seen from above)
      bool  groundHit = false;
      float tG = 0.0;
      vec3  gp = vec3(0.0);
      if (u_groundMode != 0 && o.z > 0.0 && rayDir.z < -1e-5) {
        tG = -o.z / rayDir.z;
        gp = o + rayDir * tG;
        groundHit = gp.x >= u_groundMin.x && gp.x <= u_groundMax.x && gp.y >= u_groundMin.y && gp.y <= u_groundMax.y;
      }

      vec4 color = vec4(0.0);   // premultiplied
      float tOrig = t0;
      float tEnd  = groundHit ? min(t1, tG) : t1;

      if (hit && tEnd > tOrig) {
        float stepEff = max(u_stepSize, (tEnd - tOrig) / float(u_maxSteps));
        float comp = stepEff / u_stepUser;
        float jitter = hash12(gl_FragCoord.xy);
        float tStart = tOrig + jitter * stepEff;

        for (int i = 0; i < 4096; i++) {
          if (i >= u_maxSteps) break;
          float t = tStart + float(i) * stepEff;
          if (t >= tEnd || color.a >= 0.99) break;

          vec3 pos = o + rayDir * t;
          float val, cov;
          if (!sampleField(pos, val, cov)) continue;

          vec4 tf = texture(u_transferFn, vec2((val * 255.0 + 0.5) / 256.0, 0.5));
          float a0 = tf.a * u_opacityScale * smoothstep(0.0, 1.0, cov) * thresholdFactor(val);
          if (a0 < 0.0004) continue;
          if (comp > 1.001) a0 = 1.0 - pow(max(1.0 - a0, 0.0), comp);

          vec3 rgb = tf.rgb;
          if (u_light != 0 && a0 > 0.02) {
            float e = max(0.6, stepEff * 0.8);
            float gv[6];
            for (int k = 0; k < 6; k++) {
              int ax = k / 2;
              float sgn = (k - ax * 2 == 0) ? -1.0 : 1.0;
              vec3 off = vec3(ax == 0 ? 1.0 : 0.0, ax == 1 ? 1.0 : 0.0, ax == 2 ? 1.0 : 0.0) * (sgn * e);
              float vv, c;
              bool ok = sampleField(pos + off, vv, c);
              gv[k] = ok ? vv : 0.0;
            }
            vec3 gr = vec3(gv[1] - gv[0], gv[3] - gv[2], gv[5] - gv[4]);
            float gm = length(gr);
            if (gm > 0.02) {
              vec3 n = -gr / gm;
              vec3 L = normalize(-rayDir * 0.7 + vec3(-0.35, -0.25, 0.9));
              float dif = max(dot(n, L), 0.0);
              float k = smoothstep(0.02, 0.2, gm);
              rgb *= mix(1.0, 0.55 + 0.75 * dif, k);
            }
          }

          float a = a0 * (1.0 - color.a);
          color.rgb += rgb * a;
          color.a   += a;
        }
      }

      vec3 behind = bg;
      if (groundHit) behind = groundColor(gp, tG, rayDir);
      fragColor = vec4(color.rgb + (1.0 - color.a) * behind, 1.0);
    }
  `;
}

const ARROW_VERT = `#version 300 es
  precision highp float;
  in vec3  a_local;    // x along the arrow in [-0.5, 0.5], cos, sin
  in vec3  a_normal;
  in float a_rmul;
  in vec4  i_a;        // centre.xyz (scene km), length (km)
  in vec4  i_b;        // direction.xyz, colour value 0..1

  uniform mat4  u_viewProj;
  uniform vec3  u_camPos;
  uniform float u_pixPerDist;   // world size of one render pixel per unit distance
  uniform float u_radiusFrac;   // shaft radius as a fraction of the length
  uniform float u_minPx;        // thinnest shaft (render pixels)
  uniform float u_outlinePx;    // outline thickness (render pixels)
  uniform int   u_outline;      // 1: inflated copy for the outline
  uniform int   u_colorMode;    // 0 flat fill, 1 colour table
  uniform sampler2D u_lut;
  uniform vec3  u_fill;
  uniform vec3  u_outlineCol;

  out vec3 v_color;
  out float v_shade;

  void main() {
    vec3 c = i_a.xyz;
    float len = i_a.w;
    vec3 d = i_b.xyz;
    vec3 helper = abs(d.z) > 0.95 ? vec3(1.0, 0.0, 0.0) : vec3(0.0, 0.0, 1.0);
    vec3 p1 = normalize(cross(d, helper));
    vec3 p2 = cross(d, p1);

    float dist = length(c - u_camPos);
    float pix = dist * u_pixPerDist;
    float r = max(u_radiusFrac * len, 0.6 * u_minPx * pix);
    // thin arrows get a thinner edge, so a swarm of them does not turn into one dark blob
    float grow = (u_outline == 1) ? min(u_outlinePx * pix, 0.5 * r) : 0.0;
    float rad = (a_rmul > 0.0) ? r * a_rmul + grow : 0.0;
    float along = a_local.x * len + (a_local.x >= 0.0 ? grow : -grow) * (u_outline == 1 ? 1.0 : 0.0);
    // the tip of the cone has zero radius: nothing to inflate sideways, it is pushed along the axis
    vec3 world = c + d * along + (p1 * a_local.y + p2 * a_local.z) * rad;
    gl_Position = u_viewProj * vec4(world, 1.0);

    vec3 n = normalize(d * a_normal.x + p1 * a_normal.y + p2 * a_normal.z);
    vec3 toCam = normalize(u_camPos - world);
    v_shade = 0.74 + 0.26 * max(dot(n, toCam), 0.0);
    vec3 fill = (u_colorMode == 1) ? texture(u_lut, vec2((i_b.w * 255.0 + 0.5) / 256.0, 0.5)).rgb : u_fill;
    v_color = (u_outline == 1) ? u_outlineCol : fill;
    if (u_outline == 1) v_shade = 1.0;
  }
`;

const ARROW_FRAG = `#version 300 es
  precision highp float;
  in vec3 v_color;
  in float v_shade;
  out vec4 fragColor;
  void main() { fragColor = vec4(v_color * v_shade, 1.0); }
`;

// ─── Matrices ───────────────────────────────────────────────────────────────

type Vec3 = [number, number, number];

function mat4Perspective(fovY: number, aspect: number, near: number, far: number): Float32Array {
  const m = new Float32Array(16);
  const f = 1 / Math.tan(fovY / 2);
  const ri = 1 / (near - far);
  m[0] = f / aspect;
  m[5] = f;
  m[10] = (near + far) * ri;
  m[11] = -1;
  m[14] = 2 * near * far * ri;
  return m;
}

function mat4View(eye: Vec3, right: Vec3, up: Vec3, fwd: Vec3): Float32Array {
  const m = new Float32Array(16);
  const d = (a: Vec3, b: Vec3) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  m[0] = right[0]; m[4] = right[1]; m[8] = right[2]; m[12] = -d(right, eye);
  m[1] = up[0]; m[5] = up[1]; m[9] = up[2]; m[13] = -d(up, eye);
  m[2] = -fwd[0]; m[6] = -fwd[1]; m[10] = -fwd[2]; m[14] = d(fwd, eye);
  m[15] = 1;
  return m;
}

function mat4Mul(a: Float32Array, b: Float32Array): Float32Array {
  const out = new Float32Array(16);
  for (let c = 0; c < 4; c++) {
    for (let r = 0; r < 4; r++) {
      out[c * 4 + r] = a[r] * b[c * 4] + a[4 + r] * b[c * 4 + 1] + a[8 + r] * b[c * 4 + 2] + a[12 + r] * b[c * 4 + 3];
    }
  }
  return out;
}

const dot3 = (a: number[], b: number[]) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross3 = (a: number[], b: number[]): Vec3 => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
function norm3(v: number[]): Vec3 {
  const l = Math.sqrt(dot3(v, v)) || 1;
  return [v[0] / l, v[1] / l, v[2] / l];
}

// ─── Public types ───────────────────────────────────────────────────────────

export interface SceneParams {
  vexag: number;
  stepKm: number;
  opacity: number;
  /** "Hide below" in normalised units (0 = off) and its soft edge / mode, from thresholdToShader. */
  thr: number;
  thrSoft: number;
  thrMode: 0 | 1;
  groundMode: 'off' | 'grid' | 'map';
  light: boolean;
  lightScene: boolean;
  showVolume: boolean;
  /** Spacing of the faint grid on the ground (km). */
  gridStepKm: number;
}

export interface GlyphStyle {
  colorMode: 0 | 1;
  /** 256x4 colour table for colorMode 1. */
  lut: Uint8Array | null;
  fill: [number, number, number];
  outline: [number, number, number];
  /** Arrow thickness multiplier. */
  width: number;
}

export interface CamFrame {
  eye: Vec3;
  fwd: Vec3;
  right: Vec3;
  up: Vec3;
  tanX: number;
  tanY: number;
  /** Degrees clockwise from screen-up to north. */
  compass: number;
  params: QualityParams;
}

export interface CartesianRendererOptions {
  onFatal: (message: string) => void;
  /** Called at the start of every frame with the camera (the host draws its 2D overlay). */
  onFrame?: (f: CamFrame) => void;
  /** The user grabbed the camera. */
  onUserCamera?: () => void;
}

const FOV = Math.PI / 4;

// ─── Renderer ───────────────────────────────────────────────────────────────

export class CartesianRenderer {
  readonly governor: QualityGovernor;
  readonly canvas: HTMLCanvasElement;
  private container: HTMLElement;
  private gl!: WebGL2RenderingContext;
  private pr!: ProgressiveRenderer;
  private opts: CartesianRendererOptions;
  private disposed = false;
  private lost = false;
  private resizeObs: ResizeObserver | null = null;

  // programs
  private volProg!: WebGLProgram;
  private arrowProg!: WebGLProgram;
  private vu: Record<string, WebGLUniformLocation | null> = {};
  private au: Record<string, WebGLUniformLocation | null> = {};
  private quadVAO!: WebGLVertexArrayObject;
  private arrowVAO: WebGLVertexArrayObject | null = null;
  private arrowIndexCount = 0;
  private arrowMeshBuf: WebGLBuffer[] = [];
  private instBuf: WebGLBuffer | null = null;
  private instCount = 0;

  // textures / CPU copies
  private volTex: WebGLTexture | null = null;
  private tfTex: WebGLTexture | null = null;
  private lutTex: WebGLTexture | null = null;
  private groundTex: WebGLTexture | null = null;
  private tfData = new Uint8Array(256 * 4);
  private lutData: Uint8Array | null = null;
  private groundImg: { size: number; data: Uint8ClampedArray; half: number } | null = null;
  private volume: CartVolume | null = null;
  private volGen = 0;
  private volOffset: [number, number] = [0, 0];
  private glyphData: Float32Array | null = null;

  // scene state
  private params: SceneParams = {
    vexag: 6, stepKm: 0.5, opacity: 0.8, thr: 0, thrSoft: 0.03, thrMode: 0, groundMode: 'grid',
    light: false, lightScene: false, showVolume: true, gridStepKm: 50,
  };
  private style: GlyphStyle = { colorMode: 0, lut: null, fill: [1, 1, 1], outline: [0, 0, 0], width: 1 };
  private sceneBox: SceneBox | null = null;
  private volBox: SceneBox | null = null;

  // camera (degrees / km), eased towards the targets
  camTheta = 192;
  camPhi = 34;
  camDist = 800;
  camTgt: Vec3 = [0, 0, 0];
  private tTheta = 192;
  private tPhi = 34;
  private tDist = 800;
  private tTgt: Vec3 = [0, 0, 0];
  private lastAnimT = 0;
  private dragging = false;
  private shiftDrag = false;
  private last = [0, 0];

  private onDown: (e: MouseEvent) => void;
  private onMove: (e: MouseEvent) => void;
  private onUp: () => void;
  private onWheel: (e: WheelEvent) => void;
  private onKey: (e: KeyboardEvent) => void;
  private onLost: (e: Event) => void;
  private onRestored: () => void;

  constructor(container: HTMLElement, opts: CartesianRendererOptions) {
    this.container = container;
    this.opts = opts;
    this.canvas = document.createElement('canvas');
    this.canvas.style.cssText = 'display:block;width:100%;height:100%';
    container.appendChild(this.canvas);
    const gl = this.canvas.getContext('webgl2', { antialias: false, alpha: true, premultipliedAlpha: false, preserveDrawingBuffer: true });
    if (!gl) {
      this.canvas.remove();
      throw new Error('WebGL2 is not available on this system.');
    }
    this.gl = gl;
    this.governor = new QualityGovernor('cartesian3d');
    this.governor.configureGpu(gl);
    this.setupGl();
    this.attachPipeline();
    this.resize();
    this.resizeObs = new ResizeObserver(() => {
      if (this.resize()) {
        this.pr.presentLast();
        this.pr.noteInteraction();
      }
    });
    this.resizeObs.observe(container);

    this.onDown = (e) => {
      if (e.button !== 0) return;
      this.opts.onUserCamera?.();
      this.dragging = true;
      this.shiftDrag = e.shiftKey;
      this.last = [e.clientX, e.clientY];
      this.tTgt = [...this.camTgt];
      this.tDist = this.camDist;
      this.tTheta = this.camTheta;
      this.tPhi = this.camPhi;
    };
    this.onMove = (e) => {
      if (!this.dragging) return;
      const dx = e.clientX - this.last[0];
      const dy = e.clientY - this.last[1];
      this.last = [e.clientX, e.clientY];
      if (this.shiftDrag) {
        const sc = this.camDist * 0.002;
        const th = (this.camTheta * Math.PI) / 180;
        this.camTgt = [
          this.camTgt[0] - dx * sc * Math.cos(th),
          this.camTgt[1] + dx * sc * Math.sin(th),
          this.camTgt[2] + dy * sc,
        ];
        this.tTgt = [...this.camTgt];
      } else {
        this.camTheta += dx * 0.4;
        this.camPhi = Math.max(-85, Math.min(89, this.camPhi - dy * 0.4));
        this.tTheta = this.camTheta;
        this.tPhi = this.camPhi;
      }
      this.pr.noteInteraction();
    };
    this.onUp = () => {
      this.dragging = false;
      this.shiftDrag = false;
    };
    this.onWheel = (e) => {
      e.preventDefault();
      e.stopPropagation();
      this.opts.onUserCamera?.();
      this.camDist = Math.max(5, Math.min(8000, this.camDist * (1 + e.deltaY * 0.001)));
      this.tDist = this.camDist;
      this.pr.noteInteraction();
    };
    this.onKey = (e) => {
      const step = e.shiftKey ? 15 : 5;
      if (e.key !== 'r' && e.key !== 'R') this.opts.onUserCamera?.();
      switch (e.key) {
        case 'ArrowLeft': this.camTheta -= step; break;
        case 'ArrowRight': this.camTheta += step; break;
        case 'ArrowUp': this.camPhi = Math.min(89, this.camPhi + step); break;
        case 'ArrowDown': this.camPhi = Math.max(-85, this.camPhi - step); break;
        case '+': case '=': this.camDist = Math.max(5, this.camDist * 0.85); this.tDist = this.camDist; break;
        case '-': this.camDist = Math.min(8000, this.camDist * 1.15); this.tDist = this.camDist; break;
        default: return;
      }
      this.tTheta = this.camTheta;
      this.tPhi = this.camPhi;
      e.preventDefault();
      this.pr.noteInteraction();
    };
    this.onLost = (e) => {
      e.preventDefault();
      this.lost = true;
      this.pr.setLost(true);
    };
    this.onRestored = () => {
      if (this.disposed) return;
      this.pr.dispose();
      this.setupGl();
      this.attachPipeline();
      this.resize();
      this.lost = false;
      void this.rebuildGpuData();
    };
    this.canvas.addEventListener('mousedown', this.onDown);
    window.addEventListener('mousemove', this.onMove);
    window.addEventListener('mouseup', this.onUp);
    this.canvas.addEventListener('wheel', this.onWheel, { passive: false });
    this.canvas.addEventListener('contextmenu', (e) => e.preventDefault());
    container.addEventListener('keydown', this.onKey);
    this.canvas.addEventListener('webglcontextlost', this.onLost);
    this.canvas.addEventListener('webglcontextrestored', this.onRestored);
  }

  // ── GL setup ────────────────────────────────────────────────────────────

  private link(vs: string, fs: string): WebGLProgram {
    const gl = this.gl;
    const mk = (type: number, src: string) => {
      const sh = gl.createShader(type)!;
      gl.shaderSource(sh, src);
      gl.compileShader(sh);
      if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
        const log = gl.getShaderInfoLog(sh);
        gl.deleteShader(sh);
        throw new Error(`shader compile error: ${log}`);
      }
      return sh;
    };
    const v = mk(gl.VERTEX_SHADER, vs);
    const f = mk(gl.FRAGMENT_SHADER, fs);
    const p = gl.createProgram()!;
    gl.attachShader(p, v);
    gl.attachShader(p, f);
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(`program link error: ${gl.getProgramInfoLog(p)}`);
    gl.deleteShader(v);
    gl.deleteShader(f);
    return p;
  }

  private setupGl(): void {
    const gl = this.gl;
    this.volProg = this.link(QUAD_VERT, volumeFrag());
    this.arrowProg = this.link(ARROW_VERT, ARROW_FRAG);
    this.vu = {};
    for (const n of [
      'u_camPos', 'u_camRight', 'u_camUp', 'u_camFwd', 'u_tanHalfFovX', 'u_tanHalfFovY', 'u_resolution',
      'u_volume', 'u_transferFn', 'u_ground', 'u_hasVol', 'u_boxMin', 'u_boxMax', 'u_groundMin', 'u_groundMax',
      'u_stepSize', 'u_stepUser', 'u_maxSteps', 'u_opacityScale', 'u_thr', 'u_thrSoft', 'u_thrMode',
      'u_groundMode', 'u_groundSize', 'u_groundHalf', 'u_ringStep', 'u_light', 'u_themeLight',
    ]) this.vu[n] = gl.getUniformLocation(this.volProg, n);
    this.au = {};
    for (const n of [
      'u_viewProj', 'u_camPos', 'u_pixPerDist', 'u_radiusFrac', 'u_minPx', 'u_outlinePx', 'u_outline',
      'u_colorMode', 'u_lut', 'u_fill', 'u_outlineCol',
    ]) this.au[n] = gl.getUniformLocation(this.arrowProg, n);

    // full-screen quad
    this.quadVAO = gl.createVertexArray()!;
    gl.bindVertexArray(this.quadVAO);
    const qb = gl.createBuffer()!;
    gl.bindBuffer(gl.ARRAY_BUFFER, qb);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, 1, 1, -1, -1, 1, 1, -1, 1]), gl.STATIC_DRAW);
    const pl = gl.getAttribLocation(this.volProg, 'a_pos');
    gl.enableVertexAttribArray(pl);
    gl.vertexAttribPointer(pl, 2, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);
    this.arrowMeshBuf = [qb];

    // arrow mesh + instance buffer
    const mesh = buildArrowMesh(6);
    this.arrowVAO = gl.createVertexArray()!;
    gl.bindVertexArray(this.arrowVAO);
    const mb = gl.createBuffer()!;
    gl.bindBuffer(gl.ARRAY_BUFFER, mb);
    gl.bufferData(gl.ARRAY_BUFFER, mesh.vertices, gl.STATIC_DRAW);
    const stride = mesh.floatsPerVertex * 4;
    const loc = (n: string) => gl.getAttribLocation(this.arrowProg, n);
    gl.enableVertexAttribArray(loc('a_local'));
    gl.vertexAttribPointer(loc('a_local'), 3, gl.FLOAT, false, stride, 0);
    gl.enableVertexAttribArray(loc('a_normal'));
    gl.vertexAttribPointer(loc('a_normal'), 3, gl.FLOAT, false, stride, 12);
    gl.enableVertexAttribArray(loc('a_rmul'));
    gl.vertexAttribPointer(loc('a_rmul'), 1, gl.FLOAT, false, stride, 24);
    const ib = gl.createBuffer()!;
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ib);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, mesh.indices, gl.STATIC_DRAW);
    this.arrowIndexCount = mesh.indices.length;
    this.instBuf = gl.createBuffer()!;
    gl.bindBuffer(gl.ARRAY_BUFFER, this.instBuf);
    gl.bufferData(gl.ARRAY_BUFFER, 32, gl.DYNAMIC_DRAW);
    for (const [name, off] of [['i_a', 0], ['i_b', 16]] as const) {
      const l = loc(name);
      gl.enableVertexAttribArray(l);
      gl.vertexAttribPointer(l, 4, gl.FLOAT, false, 32, off);
      gl.vertexAttribDivisor(l, 1);
    }
    gl.bindVertexArray(null);
    this.arrowMeshBuf.push(mb, ib, this.instBuf);

    // textures that do not depend on data
    const tex2d = (data: Uint8Array | null, w: number, h: number, lin = true) => {
      const t = gl.createTexture()!;
      gl.bindTexture(gl.TEXTURE_2D, t);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, lin ? gl.LINEAR : gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, lin ? gl.LINEAR : gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, data);
      return t;
    };
    this.tfTex = tex2d(this.tfData, 256, 1);
    this.lutTex = tex2d(this.lutData ?? new Uint8Array(256 * 4), 256, 1);
    this.groundTex = tex2d(new Uint8Array([20, 24, 34, 255]), 1, 1);
    this.volTex = null;
  }

  private attachPipeline(): void {
    const job: ProgressiveJob = {
      begin: (params, w, h) => this.beginFrame(params, w, h),
      drawBand: () => this.drawBand(),
    };
    this.pr = new ProgressiveRenderer({
      gl: this.gl,
      canvas: this.canvas,
      governor: this.governor,
      job,
      depth: true,
      onError: (e) => {
        console.error('[Cartesian3D] render failed:', e);
        this.opts.onFatal(`3D rendering stopped (${e instanceof Error ? e.message : String(e)}).`);
      },
    });
  }

  private resize(): boolean {
    const rect = this.container.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, this.governor.dprCap);
    const w = Math.max(1, Math.round(rect.width * dpr));
    const h = Math.max(1, Math.round(rect.height * dpr));
    if (w === this.canvas.width && h === this.canvas.height) return false;
    this.canvas.width = w;
    this.canvas.height = h;
    return true;
  }

  // ── Data ────────────────────────────────────────────────────────────────

  /** Request a redraw (coalesced). */
  markDirty(): void {
    if (!this.disposed && !this.lost) this.pr.request();
  }
  interact(): void {
    if (!this.disposed && !this.lost) this.pr.noteInteraction();
  }
  stats(): Record<string, unknown> {
    return { ...this.pr.stats, gov: this.governor.snapshot(), glyphs: this.instCount, volumeReady: !!this.volTex && !!this.volBox, volume: this.volume ? [this.volume.nx, this.volume.ny, this.volume.nz] : null };
  }
  resume(): void {
    this.pr.resume();
  }

  /**
   * Upload `vol` (null removes the volume). `offset` = km shift from the volume's own frame into
   * the scene frame (a wind grid placed into the mosaic frame). Time-sliced; a newer call
   * supersedes one still running.
   */
  async setVolume(vol: CartVolume | null, offset: [number, number] = [0, 0]): Promise<void> {
    const gen = ++this.volGen;
    this.volume = vol;
    this.volOffset = offset;
    if (!vol) {
      this.volBox = null;
      if (this.volTex && !this.lost) this.gl.deleteTexture(this.volTex);
      this.volTex = null;
      this.markDirty();
      return;
    }
    await this.uploadVolume(vol, gen);
  }

  private async uploadVolume(vol: CartVolume, gen: number): Promise<void> {
    const gl = this.gl;
    if (this.lost) return;
    const maxTex = (gl.getParameter(gl.MAX_3D_TEXTURE_SIZE) as number) || 256;
    const maxVoxels = Math.floor((this.governor.textureBudgetMB() * 1048576) / 2);
    const f = chooseDownsample([vol.nx, vol.ny, vol.nz], maxVoxels, maxTex);
    const onx = Math.ceil(vol.nx / f[0]);
    const ony = Math.ceil(vol.ny / f[1]);
    const onz = Math.ceil(vol.nz / f[2]);
    const tex = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_3D, tex);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_R, gl.CLAMP_TO_EDGE);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texImage3D(gl.TEXTURE_3D, 0, gl.RG8, onx, ony, onz, 0, gl.RG, gl.UNSIGNED_BYTE, null);
    const slab = new Uint8Array(onx * ony * 2);
    let t0 = performance.now();
    for (let oz = 0; oz < onz; oz++) {
      packRG8Slab(vol.data, vol.nx, vol.ny, vol.nz, f, oz, slab);
      gl.bindTexture(gl.TEXTURE_3D, tex);
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
      gl.texSubImage3D(gl.TEXTURE_3D, 0, 0, 0, oz, onx, ony, 1, gl.RG, gl.UNSIGNED_BYTE, slab);
      if (performance.now() - t0 > 6) {
        await yieldToMain();
        if (gen !== this.volGen || this.disposed || this.lost) {
          if (!this.lost) gl.deleteTexture(tex);
          return;
        }
        t0 = performance.now();
      }
    }
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
    gl.bindTexture(gl.TEXTURE_3D, null);
    if (gen !== this.volGen || this.disposed) {
      gl.deleteTexture(tex);
      return;
    }
    if (this.volTex) gl.deleteTexture(this.volTex);
    this.volTex = tex;
    const b = {
      xMin: vol.x0 - vol.dx / 2, xMax: vol.x0 + (vol.nx - 0.5) * vol.dx,
      yMin: vol.y0 - vol.dy / 2, yMax: vol.y0 + (vol.ny - 0.5) * vol.dy,
      zMin: 0, zMax: 0,
    };
    const dz = vol.nz > 1 ? (vol.z[vol.nz - 1] - vol.z[0]) / (vol.nz - 1) : 0.5;
    b.zMin = vol.z[0] - dz / 2;
    b.zMax = vol.z[vol.nz - 1] + dz / 2;
    this.volBox = {
      ...b,
      xMin: b.xMin + this.volOffset[0], xMax: b.xMax + this.volOffset[0],
      yMin: b.yMin + this.volOffset[1], yMax: b.yMax + this.volOffset[1],
    };
    this.governor.setVolumeMB((onx * ony * onz * 2) / 1048576);
    this.markDirty();
  }

  private async rebuildGpuData(): Promise<void> {
    const vol = this.volume;
    const gen = ++this.volGen;
    if (vol) await this.uploadVolume(vol, gen);
    this.setTransfer(this.tfData);
    if (this.lutData) this.setGlyphLut(this.lutData);
    if (this.glyphData) this.setGlyphs(this.glyphData, this.instCount);
    if (this.groundImg) this.setGround(this.groundImg.size, this.groundImg.data, this.groundImg.half);
    this.markDirty();
  }

  setTransfer(rgba: Uint8Array): void {
    this.tfData = rgba.slice();
    if (this.lost) return;
    const gl = this.gl;
    gl.bindTexture(gl.TEXTURE_2D, this.tfTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, this.tfData);
    this.markDirty();
  }

  private setGlyphLut(rgba: Uint8Array): void {
    this.lutData = rgba;
    if (this.lost) return;
    const gl = this.gl;
    gl.bindTexture(gl.TEXTURE_2D, this.lutTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, rgba);
  }

  /** Box of the whole scene (volume and wind grid), unexaggerated km; drives the ground and the camera. */
  setSceneBox(b: SceneBox | null): void {
    this.sceneBox = b;
    this.markDirty();
  }

  setParams(p: Partial<SceneParams>): void {
    this.params = { ...this.params, ...p };
    this.markDirty();
  }

  /** `data` = 8 floats per arrow (windGlyphs.ts); null or count 0 hides the arrows. */
  setGlyphs(data: Float32Array | null, count: number): void {
    this.glyphData = data;
    this.instCount = data ? count : 0;
    if (this.lost) return;
    const gl = this.gl;
    gl.bindBuffer(gl.ARRAY_BUFFER, this.instBuf);
    gl.bufferData(gl.ARRAY_BUFFER, data && count ? data.subarray(0, count * 8) : new Float32Array(8), gl.DYNAMIC_DRAW);
    this.markDirty();
  }

  setGlyphStyle(s: Partial<GlyphStyle>): void {
    this.style = { ...this.style, ...s };
    if (s.lut) this.setGlyphLut(s.lut);
    this.markDirty();
  }

  /** Map image over [-half, half]^2 km around the scene origin (RGBA, row 0 = north); null = none. */
  setGround(size: number, data: Uint8ClampedArray | null, half: number): void {
    if (!data) {
      this.groundImg = null;
      if (!this.lost) {
        const gl = this.gl;
        gl.bindTexture(gl.TEXTURE_2D, this.groundTex);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array([20, 24, 34, 255]));
      }
      this.markDirty();
      return;
    }
    this.groundImg = { size, data, half };
    if (this.lost) return;
    const gl = this.gl;
    gl.activeTexture(gl.TEXTURE4);
    gl.bindTexture(gl.TEXTURE_2D, this.groundTex);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, size, size, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array(data.buffer, data.byteOffset, data.byteLength));
    gl.generateMipmap(gl.TEXTURE_2D);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    const an = gl.getExtension('EXT_texture_filter_anisotropic');
    if (an) gl.texParameterf(gl.TEXTURE_2D, an.TEXTURE_MAX_ANISOTROPY_EXT, Math.min(8, gl.getParameter(an.MAX_TEXTURE_MAX_ANISOTROPY_EXT) as number));
    gl.activeTexture(gl.TEXTURE0);
    this.markDirty();
  }

  get hasGroundMap(): boolean {
    return this.groundImg !== null;
  }

  // ── Camera ──────────────────────────────────────────────────────────────

  /** Move the camera to a pose (animated unless `instant`). */
  setPose(p: { theta: number; phi: number; dist: number; target: Vec3 }, instant = false): void {
    this.tTheta = p.theta;
    this.tPhi = p.phi;
    this.tDist = p.dist;
    this.tTgt = [...p.target];
    if (instant) {
      this.camTheta = p.theta;
      this.camPhi = p.phi;
      this.camDist = p.dist;
      this.camTgt = [...p.target];
    } else {
      this.camTheta = this.tTheta + ((this.camTheta - this.tTheta + 540) % 360) - 180;
    }
    this.pr.noteInteraction();
  }

  /** Re-scale the camera target height when the vertical exaggeration changes. */
  scaleTargetZ(k: number): void {
    this.camTgt = [this.camTgt[0], this.camTgt[1], this.camTgt[2] * k];
    this.tTgt = [this.tTgt[0], this.tTgt[1], this.tTgt[2] * k];
  }

  private camVectors(): { eye: Vec3; fwd: Vec3; right: Vec3; up: Vec3 } {
    const th = (this.camTheta * Math.PI) / 180;
    const ph = (this.camPhi * Math.PI) / 180;
    const eye: Vec3 = [
      this.camTgt[0] + this.camDist * Math.cos(ph) * Math.sin(th),
      this.camTgt[1] + this.camDist * Math.cos(ph) * Math.cos(th),
      this.camTgt[2] + this.camDist * Math.sin(ph),
    ];
    const fwd = norm3([this.camTgt[0] - eye[0], this.camTgt[1] - eye[1], this.camTgt[2] - eye[2]]);
    const upHint: Vec3 = this.camPhi < 0 ? [0, 0, -1] : [0, 0, 1];
    const right = norm3(cross3(fwd, upHint));
    const up = cross3(right, fwd);
    return { eye, fwd, right, up };
  }

  // ── Frame ───────────────────────────────────────────────────────────────

  private frame!: { viewProj: Float32Array; eye: Vec3; w: number; h: number; params: QualityParams; tanY: number };

  private beginFrame(params: QualityParams, w: number, h: number): boolean {
    if (this.disposed || this.lost || !this.sceneBox) return false;
    const gl = this.gl;
    // ease the camera towards its targets by elapsed time
    const nowT = performance.now();
    const gap = nowT - this.lastAnimT;
    this.lastAnimT = nowT;
    const dt = gap > 250 ? 16 : gap;
    const lf = 1 - Math.exp(-dt / 90);
    const dTgt = Math.hypot(this.camTgt[0] - this.tTgt[0], this.camTgt[1] - this.tTgt[1], this.camTgt[2] - this.tTgt[2]);
    const dTheta = ((this.tTheta - this.camTheta + 540) % 360) - 180;
    const dPhi = this.tPhi - this.camPhi;
    if (dTgt > 0.05 || Math.abs(this.camDist - this.tDist) > 0.5 || Math.abs(dTheta) > 0.05 || Math.abs(dPhi) > 0.05) {
      this.camTgt = [
        this.camTgt[0] + (this.tTgt[0] - this.camTgt[0]) * lf,
        this.camTgt[1] + (this.tTgt[1] - this.camTgt[1]) * lf,
        this.camTgt[2] + (this.tTgt[2] - this.camTgt[2]) * lf,
      ];
      this.camDist += (this.tDist - this.camDist) * lf;
      this.camTheta += dTheta * lf;
      this.camPhi += dPhi * lf;
      this.pr.noteInteraction();
    }
    const { eye, fwd, right, up } = this.camVectors();
    const tanY = Math.tan(FOV / 2);
    const tanX = tanY * (w / h);
    const near = Math.max(0.5, this.camDist * 0.01);
    const far = this.camDist * 6 + 4000;
    const viewProj = mat4Mul(mat4Perspective(FOV, w / h, near, far), mat4View(eye, right, up, fwd));
    this.frame = { viewProj, eye, w, h, params, tanY };
    const compass = (Math.atan2(dot3([0, 1, 0], right), dot3([0, 1, 0], up)) * 180) / Math.PI;
    this.opts.onFrame?.({ eye, fwd, right, up, tanX, tanY, compass, params });

    const p = this.params;
    const ve = p.vexag;
    const sb = this.sceneBox;
    const u = this.vu;
    gl.useProgram(this.volProg);
    gl.uniform3f(u.u_camPos, eye[0], eye[1], eye[2]);
    gl.uniform3f(u.u_camRight, right[0], right[1], right[2]);
    gl.uniform3f(u.u_camUp, up[0], up[1], up[2]);
    gl.uniform3f(u.u_camFwd, fwd[0], fwd[1], fwd[2]);
    gl.uniform1f(u.u_tanHalfFovX, tanX);
    gl.uniform1f(u.u_tanHalfFovY, tanY);
    gl.uniform2f(u.u_resolution, w, h);
    const vb = this.volBox;
    const hasVol = !!(p.showVolume && this.volTex && vb);
    gl.uniform1i(u.u_hasVol, hasVol ? 1 : 0);
    if (vb) {
      gl.uniform3f(u.u_boxMin, vb.xMin, vb.yMin, vb.zMin * ve);
      gl.uniform3f(u.u_boxMax, vb.xMax, vb.yMax, vb.zMax * ve);
    }
    gl.uniform2f(u.u_groundMin, sb.xMin, sb.yMin);
    gl.uniform2f(u.u_groundMax, sb.xMax, sb.yMax);
    gl.uniform1f(u.u_stepSize, p.stepKm * params.stepMul);
    gl.uniform1f(u.u_stepUser, p.stepKm);
    gl.uniform1i(u.u_maxSteps, params.maxSteps);
    gl.uniform1f(u.u_opacityScale, p.opacity);
    gl.uniform1f(u.u_thr, p.thr);
    gl.uniform1f(u.u_thrSoft, p.thrSoft);
    gl.uniform1i(u.u_thrMode, p.thrMode);
    const mapOn = p.groundMode === 'map' && this.groundImg !== null;
    gl.uniform1i(u.u_groundMode, p.groundMode === 'off' ? 0 : mapOn ? 2 : 1);
    gl.uniform1f(u.u_groundSize, this.groundImg?.size ?? 1);
    gl.uniform1f(u.u_groundHalf, this.groundImg?.half ?? 1);
    gl.uniform1f(u.u_ringStep, p.gridStepKm * 2);
    gl.uniform1i(u.u_light, p.light ? 1 : 0);
    gl.uniform1f(u.u_themeLight, p.lightScene ? 1 : 0);
    return true;
  }

  private drawBand(): void {
    const gl = this.gl;
    const p = this.params;
    const fr = this.frame;
    gl.clearColor(0.05, 0.05, 0.1, 1);
    gl.clearDepth(1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.disable(gl.DEPTH_TEST);
    gl.disable(gl.CULL_FACE);

    gl.useProgram(this.volProg);
    gl.bindVertexArray(this.quadVAO);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_3D, this.volTex);
    gl.uniform1i(this.vu.u_volume, 0);
    gl.activeTexture(gl.TEXTURE3);
    gl.bindTexture(gl.TEXTURE_2D, this.tfTex);
    gl.uniform1i(this.vu.u_transferFn, 3);
    gl.activeTexture(gl.TEXTURE4);
    gl.bindTexture(gl.TEXTURE_2D, this.groundTex);
    gl.uniform1i(this.vu.u_ground, 4);
    gl.activeTexture(gl.TEXTURE0);
    gl.drawArrays(gl.TRIANGLES, 0, 6);
    gl.bindVertexArray(null);

    if (this.instCount === 0 || !this.arrowVAO) return;
    gl.useProgram(this.arrowProg);
    const a = this.au;
    const st = this.style;
    gl.uniformMatrix4fv(a.u_viewProj, false, fr.viewProj);
    gl.uniform3f(a.u_camPos, fr.eye[0], fr.eye[1], fr.eye[2]);
    gl.uniform1f(a.u_pixPerDist, (2 * fr.tanY) / fr.h);
    gl.uniform1f(a.u_radiusFrac, 0.06 * st.width);
    gl.uniform1f(a.u_minPx, 2.2 * Math.sqrt(st.width));
    gl.uniform1f(a.u_outlinePx, 1.3);
    gl.uniform1i(a.u_colorMode, st.colorMode);
    gl.uniform3f(a.u_fill, st.fill[0], st.fill[1], st.fill[2]);
    gl.uniform3f(a.u_outlineCol, st.outline[0], st.outline[1], st.outline[2]);
    gl.activeTexture(gl.TEXTURE5);
    gl.bindTexture(gl.TEXTURE_2D, this.lutTex);
    gl.uniform1i(a.u_lut, 5);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindVertexArray(this.arrowVAO);
    gl.enable(gl.DEPTH_TEST);
    gl.depthFunc(gl.LESS);
    gl.depthMask(true);
    gl.enable(gl.CULL_FACE);
    // outline: inflated hull, only its far side shows around the arrow
    gl.uniform1i(a.u_outline, 1);
    gl.cullFace(gl.FRONT);
    gl.drawElementsInstanced(gl.TRIANGLES, this.arrowIndexCount, gl.UNSIGNED_SHORT, 0, this.instCount);
    // fill
    gl.uniform1i(a.u_outline, 0);
    gl.cullFace(gl.BACK);
    gl.drawElementsInstanced(gl.TRIANGLES, this.arrowIndexCount, gl.UNSIGNED_SHORT, 0, this.instCount);
    gl.bindVertexArray(null);
    gl.disable(gl.CULL_FACE);
    gl.disable(gl.DEPTH_TEST);
    void p;
  }

  // ── Teardown ────────────────────────────────────────────────────────────

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.volGen++;
    this.pr.dispose();
    this.governor.release();
    this.resizeObs?.disconnect();
    this.canvas.removeEventListener('mousedown', this.onDown);
    window.removeEventListener('mousemove', this.onMove);
    window.removeEventListener('mouseup', this.onUp);
    this.canvas.removeEventListener('wheel', this.onWheel);
    this.container.removeEventListener('keydown', this.onKey);
    this.canvas.removeEventListener('webglcontextlost', this.onLost);
    this.canvas.removeEventListener('webglcontextrestored', this.onRestored);
    if (!this.lost) {
      const gl = this.gl;
      for (const t of [this.volTex, this.tfTex, this.lutTex, this.groundTex]) if (t) gl.deleteTexture(t);
      for (const b of this.arrowMeshBuf) gl.deleteBuffer(b);
      if (this.arrowVAO) gl.deleteVertexArray(this.arrowVAO);
      gl.deleteVertexArray(this.quadVAO);
      gl.deleteProgram(this.volProg);
      gl.deleteProgram(this.arrowProg);
    }
    this.volume = null;
    this.glyphData = null;
    this.canvas.remove();
  }
}
