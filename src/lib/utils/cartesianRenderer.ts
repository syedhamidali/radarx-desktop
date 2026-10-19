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
import { GLSL_REALISTIC, GLSL_REALISTIC_UNIFORMS, REALISTIC_UNIFORM_NAMES } from './realisticShader';
import { lightVector, lookFor, type RealisticLook } from './realisticStyle';
import { buildArrowMesh } from './windGlyphs';
import { FlowLayer, type FlowParams } from './flowRenderer';
import { activeParticles, type FlowField } from './windFlow.ts';
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

    uniform highp sampler3D u_volumeB;   // second volume (time interpolation), same grid
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
    uniform vec2  u_groundCenter;  // km: centre of the map image on the ground
    uniform float u_mapGain;       // brightness of the map image
    uniform vec2  u_cropMin;       // only this part of the volume / ground is shown (km)
    uniform vec2  u_cropMax;
    uniform int   u_morphOn;       // time interpolation between volume A and B
    uniform float u_morphW;        // weight of B
    uniform vec3  u_shiftA;        // km: A is sampled at pos - shiftA, B at pos - shiftB
    uniform vec3  u_shiftB;

    // Optional drift of the echo along the retrieved wind (display only): the volume is sampled
    // where each cell came from, u_animT * phase seconds ago, in two half-period shifted phases
    // that are cross-faded so the loop has no seam. Cells without wind do not move.
    uniform int   u_animOn;
    uniform float u_animAmp;       // fade 0..1
    uniform float u_animT;         // simulated seconds of drift at the end of a cycle
    uniform float u_animPhase;     // 0..1
    uniform highp sampler3D u_windTex;  // RGBA16F: u, v, w (m/s), confidence
    uniform vec3  u_wOrigin;       // wind grid, km (its own frame)
    uniform vec3  u_wCell;
    uniform vec3  u_wDims;
    uniform vec2  u_wOffset;       // wind frame -> scene frame
    uniform float u_vexag;
${GLSL_REALISTIC_UNIFORMS}
    out vec4 fragColor;

    // Field at a scene position: val 0..1 over [vmin, vmax], cov = fraction of the neighbourhood
    // with data (fades at the edges of the echo instead of cutting off).
    bool sampleTex(highp sampler3D tex, vec3 pos, out float val, out float cov) {
      vec3 uvw = (pos - u_boxMin) / (u_boxMax - u_boxMin);
      val = 0.0; cov = 0.0;
      if (any(lessThan(uvw, vec3(0.0))) || any(greaterThan(uvw, vec3(1.0)))) return false;
      vec2 rg = texture(tex, uvw).rg;
      if (rg.g < 0.02) return false;
      float b = rg.r / rg.g;
      val = clamp((b * 255.0 - 1.0) / 254.0, 0.0, 1.0);
      cov = rg.g;
      return true;
    }
    bool sampleField(vec3 pos, out float val, out float cov) { return sampleTex(u_volume, pos, val, cov); }
    vec4 windAt(vec3 pos) {
      vec3 pp = vec3(pos.xy - u_wOffset, pos.z / u_vexag);
      vec3 f = (pp - u_wOrigin) / u_wCell;
      if (any(lessThan(f, vec3(0.0))) || any(greaterThan(f, u_wDims - 1.0))) return vec4(0.0);
      return texture(u_windTex, (f + 0.5) / u_wDims);
    }

    bool sampleAdvected(vec3 pos, out float val, out float cov) {
      if (u_morphOn != 0) {
        // time interpolation: the earlier volume carried forward and the later one carried back
        // (by the storm motion), cross-faded
        float va, ca, vb, cb;
        bool ka = sampleTex(u_volume, pos - u_shiftA, va, ca);
        bool kb = sampleTex(u_volumeB, pos - u_shiftB, vb, cb);
        float aa = ka ? (1.0 - u_morphW) * ca : 0.0;
        float ab = kb ? u_morphW * cb : 0.0;
        float ss = aa + ab;
        val = 0.0; cov = 0.0;
        if (ss <= 0.002) return false;
        val = (aa * va + ab * vb) / ss;
        cov = ss;
        return true;
      }
      if (u_animOn == 0) return sampleField(pos, val, cov);
      vec4 wv = windAt(pos);
      vec3 dir = vec3(wv.xy, wv.z * u_vexag) * (0.001 * u_animT * u_animAmp);
      float p1 = u_animPhase;
      float p2 = fract(p1 + 0.5);
      float w1 = 1.0 - abs(2.0 * p1 - 1.0);
      float w2 = 1.0 - w1;
      float v1, c1, v2, c2;
      bool k1 = sampleField(pos - dir * p1, v1, c1);
      bool k2 = sampleField(pos - dir * p2, v2, c2);
      float a1 = k1 ? w1 * c1 : 0.0;
      float a2 = k2 ? w2 * c2 : 0.0;
      float s = a1 + a2;
      val = 0.0; cov = 0.0;
      if (s <= 0.002) return false;
      val = (a1 * v1 + a2 * v2) / s;
      cov = s;
      return true;
    }
${GLSL_THRESHOLD}${GLSL_HASH}${glslGroundColor({
    prelude: 'vec3 bmin = vec3(u_groundMin, 0.0); vec3 bmax = vec3(u_groundMax, 0.0);',
    rimDist: 'min(min(gp.x - bmin.x, bmax.x - gp.x), min(gp.y - bmin.y, bmax.y - gp.y))',
    rings: false,
    themed: true,
  })}${GLSL_REALISTIC}
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
      if (u_style == 1) bg = skyColor(rayDir);

      // Ray against the volume box (above the ground)
      float t0 = 0.0;
      float t1 = 1e9;
      bool  hit = u_hasVol != 0;
      vec3 bmin = vec3(u_boxMin.xy, max(u_boxMin.z, 0.0));
      vec3 bmax = u_boxMax;
      bmin.xy = max(bmin.xy, u_cropMin);
      bmax.xy = min(bmax.xy, u_cropMax);
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
      bool  terrHit = false;
      if (u_terrOn != 0) {
        // terrain height field (the cloud stops at it: nothing shows under hills)
        if (traceTerrain(o, rayDir, tG)) {
          gp = o + rayDir * tG;
          groundHit = true;
          terrHit = true;
        }
      } else if (u_groundMode != 0 && o.z > 0.0 && rayDir.z < -1e-5) {
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
        float jitter = (u_style == 1 ? whiteNoise(gl_FragCoord.xy) : hash12(gl_FragCoord.xy));
        float tStart = tOrig + jitter * stepEff;

        for (int i = 0; i < 4096; i++) {
          if (i >= u_maxSteps) break;
          float t = tStart + float(i) * stepEff;
          if (t >= tEnd || color.a >= 0.99) break;

          vec3 pos = o + rayDir * t;
          float val, cov;
          if (!sampleAdvected(pos, val, cov)) continue;

          vec4 tf = texture(u_transferFn, vec2((val * 255.0 + 0.5) / 256.0, 0.5));
          float a0 = tf.a * u_opacityScale * smoothstep(0.0, 1.0, cov) * thresholdFactor(val);
          if (a0 < 0.0004) continue;
          if (u_style == 1) {
            // realistic look: opacity per TRUE km along the ray (the scene is stretched vertically)
            a0 = 1.0 - pow(max(1.0 - a0, 0.0), comp * length(vec3(rayDir.xy, rayDir.z / max(u_vexag, 1e-3))));
            // soften the cut where the volume leaves the shown box
            a0 *= smoothstep(0.0, 18.0, min(min(pos.x - bmin.x, bmax.x - pos.x), min(pos.y - bmin.y, bmax.y - pos.y)));
          } else if (comp > 1.001) a0 = 1.0 - pow(max(1.0 - a0, 0.0), comp);

          vec3 rgb = tf.rgb;
          if (u_style == 1) {
            rgb = cloudLight(pos, rayDir, tf.rgb, whiteNoise(gl_FragCoord.xy + float(i) * 3.7));
          } else if (u_light != 0 && a0 > 0.02) {
            float e = max(0.6, stepEff * 0.8);
            float gv[6];
            for (int k = 0; k < 6; k++) {
              int ax = k / 2;
              float sgn = (k - ax * 2 == 0) ? -1.0 : 1.0;
              vec3 off = vec3(ax == 0 ? 1.0 : 0.0, ax == 1 ? 1.0 : 0.0, ax == 2 ? 1.0 : 0.0) * (sgn * e);
              float vv, c;
              bool ok = sampleAdvected(pos + off, vv, c);
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
      if (groundHit) behind = terrHit ? terrainColor(gp, tG, rayDir, whiteNoise(gl_FragCoord.xy)) : groundColor(gp, tG, rayDir);
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

/** What the user sets for the flow; the renderer adds the per-frame particle count (quality). */
export interface FlowUserParams {
  /** Particles allocated (the quality governor draws a share of them). */
  requested: number;
  lifeSec: number;
  visualSpeed: number;
  trailSec: number;
  widthPx: number;
  haloPx: number;
  opacity: number;
  colorMode: 0 | 1 | 2;
  colorMax: number;
  core: [number, number, number];
  halo: [number, number, number];
  /** Additive (glowing) blending, for dark scenes; otherwise alpha blending. */
  additive: boolean;
}

/** Subtle drift of the radar echo along the retrieved wind (display only). */
export interface FieldAnimParams {
  /** Displacement (km) of a cell moving at `refSpeed` over one cycle. */
  ampKm: number;
  /** Seconds per cycle. */
  period: number;
  /** m/s the amplitude refers to (a high percentile of the wind speed). */
  refSpeed: number;
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

/** Light of the terrain when the storm keeps the scientific colour scale: a daytime sun, high and from the south-west. */
const DAY_LOOK: RealisticLook = lookFor({ azimuthDeg: 215, elevationDeg: 38, stylised: false, realElevationDeg: 38, realAzimuthDeg: 215, label: '' }, 0);

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
  private volTexB: WebGLTexture | null = null;
  private morphW = 0;
  private shiftA: [number, number, number] = [0, 0, 0];
  private shiftB: [number, number, number] = [0, 0, 0];
  private cropBox: { xMin: number; xMax: number; yMin: number; yMax: number } | null = null;
  private groundCenter: [number, number] = [0, 0];
  private exporting = false;
  private mapGain = 1.5;
  private tfTex: WebGLTexture | null = null;
  private lutTex: WebGLTexture | null = null;
  private groundTex: WebGLTexture | null = null;
  private demTex: WebGLTexture | null = null;
  private frameSeed = 0;
  /** Terrain height field (km ASL) over the ground image's square, or null (flat ground). */
  private terrain: { size: number; heightsKm: Float32Array; minKm: number; maxKm: number; half: number; center: [number, number] } | null = null;
  /** Realistic storm look (light, sky, haze); null = the scientific colour scale. */
  private look: RealisticLook | null = null;
  private tfData = new Uint8Array(256 * 4);
  private lutData: Uint8Array | null = null;
  private groundImg: { size: number; data: Uint8ClampedArray; half: number } | null = null;
  private volume: CartVolume | null = null;
  private volGen = 0;
  private volOffset: [number, number] = [0, 0];
  private glyphData: Float32Array | null = null;

  // wind flow (particles advected by the retrieved wind)
  private flow: FlowLayer | null = null;
  flowError = '';
  private flowField: FlowField | null = null;
  private flowCells: Uint32Array = new Uint32Array(0);
  private flowPlaying = true;
  private flowOffset: [number, number] = [0, 0];
  private flowLut: Uint8Array | null = null;
  private flowUser: FlowUserParams = {
    requested: 30000, lifeSec: 6, visualSpeed: 600, trailSec: 0.9, widthPx: 1.6, haloPx: 0.9, opacity: 0.9,
    colorMode: 0, colorMax: 40, core: [1, 1, 1], halo: [0.03, 0.04, 0.06], additive: true,
  };
  // flow and field animation fade in / out (alpha ramp) instead of popping
  private flowTarget = 0;
  private flowFade = 0;
  private flowReleased = false;
  private animTarget = 0;
  private animFade = 0;
  private animPhase = 0;
  private animUser: FieldAnimParams = { ampKm: 3, period: 6, refSpeed: 30 };
  private windTex: WebGLTexture | null = null;
  private lastFadeT = 0;
  private lastFlowT = 0;
  private flowActive = 0;

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
        this.nudge();
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
      this.nudge();
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
      this.nudge();
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
      this.nudge();
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
      'u_volumeB', 'u_morphOn', 'u_morphW', 'u_shiftA', 'u_shiftB', 'u_cropMin', 'u_cropMax', 'u_groundCenter', 'u_mapGain',
      'u_animOn', 'u_animAmp', 'u_animT', 'u_animPhase', 'u_windTex', 'u_wOrigin', 'u_wCell', 'u_wDims', 'u_wOffset', 'u_vexag',
      ...REALISTIC_UNIFORM_NAMES,
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
    this.demTex = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, this.demTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R16F, 1, 1, 0, gl.RED, gl.FLOAT, new Float32Array([0]));
    this.volTex = null;
    this.cacheTex = null;
    this.cacheValid = false;
    this.staticDirty = true;
    this.cacheProg = this.link(
      QUAD_VERT,
      `#version 300 es
      precision highp float;
      uniform sampler2D u_cache;
      out vec4 o;
      void main() { o = texelFetch(u_cache, ivec2(gl_FragCoord.xy), 0); }`,
    );
    this.cacheLoc = gl.getUniformLocation(this.cacheProg, 'u_cache');
    try {
      this.flow = new FlowLayer(gl);
      this.flowError = '';
      if (this.flowLut) this.flow.setLut(this.flowLut);
    } catch (e) {
      this.flow = null;
      this.flowError = e instanceof Error ? e.message : String(e);
      console.error('[Cartesian3D] wind flow unavailable:', e);
    }
  }

  // ── Wind flow ───────────────────────────────────────────────────────────

  /**
   * Give the flow its wind (null removes it) and the cells particles are born in. `offset` = km
   * from the wind grid's frame into the scene frame.
   */
  setFlowField(f: FlowField | null, cells: Uint32Array, offset: [number, number]): void {
    this.flowField = f;
    this.flowCells = cells;
    this.flowOffset = offset;
    if (this.flow && !this.lost) {
      if (!f || this.flowPresent) {
        this.flow.setField(f, cells, this.flowUser.requested, this.flowUser.lifeSec);
        this.flowReleased = false;
      } else {
        this.flowReleased = true; // the flow is off: nothing is kept on the GPU
      }
    }
    if (this.windTex) this.freeWindTexture();
    if (this.animPresent) this.uploadWindTexture();
    this.requestFrame();
  }

  private get flowPresent(): boolean {
    return this.flowTarget > 0 || this.flowFade > 0;
  }
  private get animPresent(): boolean {
    return this.animTarget > 0 || this.animFade > 0;
  }

  setFlowParams(p: Partial<FlowUserParams>): void {
    const old = this.flowUser;
    this.flowUser = { ...old, ...p };
    if (this.flow && !this.lost && this.flowField && !this.flowReleased) {
      if (this.flowUser.requested > this.flow.allocated || (p.lifeSec !== undefined && p.lifeSec !== old.lifeSec)) {
        this.flow.setField(this.flowField, this.flowCells, this.flowUser.requested, this.flowUser.lifeSec);
      } else {
        this.flow.markDirty();
      }
    }
    this.requestFrame();
  }

  setFlowLut(rgba: Uint8Array): void {
    this.flowLut = rgba;
    if (this.flow && !this.lost) this.flow.setLut(rgba);
    this.requestFrame();
  }

  /** Fade the flow in (true) or out (false); once it has faded out its GPU data is freed. */
  setFlowEnabled(on: boolean): void {
    this.flowTarget = on ? 1 : 0;
    this.lastFadeT = 0;
    if (on && this.flowReleased && this.flow && !this.lost && this.flowField) {
      this.flow.setField(this.flowField, this.flowCells, this.flowUser.requested, this.flowUser.lifeSec);
      this.flowReleased = false;
      this.lastFlowT = 0;
    }
    this.requestFrame();
  }

  /** Play / pause the flow and the field animation (a paused picture is exactly reproducible). */
  setFlowPlaying(on: boolean): void {
    this.flowPlaying = on;
    this.lastFlowT = 0;
    this.requestFrame();
  }

  /** Fade the field animation in or out; `p` = amplitude and period. */
  setFieldAnim(on: boolean, p?: Partial<FieldAnimParams>): void {
    if (p) this.animUser = { ...this.animUser, ...p };
    this.animTarget = on ? 1 : 0;
    this.lastFadeT = 0;
    if (on && !this.windTex && this.flowField) this.uploadWindTexture();
    this.markDirty();
  }

  private uploadWindTexture(): void {
    const f = this.flowField;
    if (!f || this.lost) return;
    const gl = this.gl;
    this.windTex = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_3D, this.windTex);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_R, gl.CLAMP_TO_EDGE);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
    gl.texImage3D(gl.TEXTURE_3D, 0, gl.RGBA16F, f.nx, f.ny, f.nz, 0, gl.RGBA, gl.FLOAT, f.data);
  }

  private freeWindTexture(): void {
    if (this.windTex && !this.lost) this.gl.deleteTexture(this.windTex);
    this.windTex = null;
  }

  /** Free the particle data on the GPU (the tab is hidden) and bring it back (`resumeFlow`). */
  suspendFlow(): void {
    if (this.flow && !this.lost) this.flow.release();
  }
  resumeFlow(): void {
    if (this.flow && !this.lost && this.flowField && this.flowPresent) {
      this.flow.setField(this.flowField, this.flowCells, this.flowUser.requested, this.flowUser.lifeSec);
      this.lastFlowT = 0;
      this.requestFrame();
    }
  }

  /** Test hook: state, aux and trail points of the first `n` particles. */
  flowReadBack(n: number): Float32Array | null {
    return this.flow && this.flow.ready ? this.flow.readBack(Math.min(n, this.flow.allocated)) : null;
  }

  /** Test hook: one update pass of exactly `dt` animation seconds (all allocated particles). */
  flowStepOnce(dt: number): void {
    if (this.flow && this.flow.ready) this.flow.step(dt, { ...this.flowUser, active: this.flow.allocated });
  }

  private flowParams(levelIndex: number): FlowParams {
    const u = this.flowUser;
    const n = activeParticles(u.requested, levelIndex);
    if (n !== this.flowActive) this.flow?.markDirty(); // particles that join the drawing need their trails
    this.flowActive = n;
    return { ...u, active: this.flowActive };
  }

  private attachPipeline(): void {
    const job: ProgressiveJob = {
      begin: (params, w, h) => this.beginFrame(params, w, h),
      drawBand: (y, rows, w, h) => this.drawBand(y, rows, w, h),
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

  /** The static layer (volume, ground, background) is out of date: draw it again, then animate over it. */
  private staticDirty = true;
  private cacheTex: WebGLTexture | null = null;
  private cacheSize: [number, number] = [0, 0];
  private cacheValid = false;
  private cacheRegion: [number, number] = [0, 0];
  private cacheProg: WebGLProgram | null = null;
  private cacheLoc: WebGLUniformLocation | null = null;
  private useCache = false;
  private capture = false;

  /**
   * Two volumes of the same grid for time interpolation (`b` null = a single volume): see `setMorph`.
   * Used by the clip export; `a` also provides the geometry.
   */
  async setVolumePair(a: CartVolume, b: CartVolume | null, offset: [number, number] = [0, 0]): Promise<void> {
    const gen = ++this.volGen;
    this.volume = a;
    this.volOffset = offset;
    const ta = await this.buildTexture(a, gen);
    if (!ta) return;
    const tb = b ? await this.buildTexture(b, gen) : null;
    if (b && !tb) { this.gl.deleteTexture(ta); return; }
    if (this.volTex) this.gl.deleteTexture(this.volTex);
    if (this.volTexB) this.gl.deleteTexture(this.volTexB);
    this.volTex = ta;
    this.volTexB = tb;
    const dz = a.nz > 1 ? (a.z[a.nz - 1] - a.z[0]) / (a.nz - 1) : 0.5;
    this.volBox = {
      xMin: a.x0 - a.dx / 2 + offset[0], xMax: a.x0 + (a.nx - 0.5) * a.dx + offset[0],
      yMin: a.y0 - a.dy / 2 + offset[1], yMax: a.y0 + (a.ny - 0.5) * a.dy + offset[1],
      zMin: a.z[0] - dz / 2, zMax: a.z[a.nz - 1] + dz / 2,
    };
    this.markDirty();
  }

  /** Weight of volume B and the km shifts (A is read at pos - shiftA, B at pos - shiftB). */
  setMorph(w: number, shiftA: [number, number, number], shiftB: [number, number, number]): void {
    this.morphW = w;
    this.shiftA = shiftA;
    this.shiftB = shiftB;
    this.markDirty();
  }

  /** Brightness of the ground map on a dark scene (1.5 suits the dark-restyled tiles). */
  setMapGain(g: number): void {
    this.mapGain = g;
    this.markDirty();
  }

  /** Show only this part (km, scene frame) of the volume and of the ground; null = all. */
  setCropBox(b: { xMin: number; xMax: number; yMin: number; yMax: number } | null): void {
    this.cropBox = b;
    this.markDirty();
  }

  /**
   * Offline rendering: stop the interactive pipeline; frames are then drawn by `exportRender`, one
   * complete full-quality frame per call, straight into the canvas (deterministic, no bands).
   */
  enterExport(): void {
    this.exporting = true;
    this.pr.setLost(true);
  }

  /** Draw one complete frame with the camera `pose` (degrees / km; z already exaggerated). */
  exportRender(pose: { theta: number; phi: number; dist: number; target: Vec3 }): void {
    const gl = this.gl;
    this.setPose(pose, true);
    const w = this.canvas.width;
    const h = this.canvas.height;
    const q: QualityParams = {
      level: 'ultra', index: 4, interacting: false, renderScale: 1, stepMul: 1, maxSteps: 2400,
      interp: 2, texBudgetMB: 64, weight: 1, relCost: 1,
    };
    this.lastAnimT = 0;
    if (!this.beginFrame(q, w, h)) return;
    this.useCache = false;
    this.capture = false;
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, w, h);
    gl.disable(gl.SCISSOR_TEST);
    this.drawBand(0, h, w, h);
    gl.finish();
  }

  /** Request a redraw (coalesced); the static layer is redrawn. */
  markDirty(): void {
    this.staticDirty = true;
    if (!this.disposed && !this.lost) this.pr.request();
  }
  /** Request a redraw of the moving parts only (arrows, flow): the static layer stays. */
  private requestFrame(): void {
    if (!this.disposed && !this.lost) this.pr.request();
  }
  /** Camera / slider activity: the static layer changes, render coarser until it settles. */
  private nudge(): void {
    this.staticDirty = true;
    if (!this.disposed && !this.lost) this.pr.noteInteraction();
  }
  interact(): void {
    this.nudge();
  }
  stats(): Record<string, unknown> {
    return { ...this.pr.stats, gov: this.governor.snapshot(), glyphs: this.instCount, volumeReady: !!this.volTex && !!this.volBox, flow: this.flow ? { active: this.flowActive, allocated: this.flow.allocated, playing: this.flowPlaying, fade: this.flowFade, animFade: this.animFade, released: this.flowReleased, error: this.flowError } : null, volume: this.volume ? [this.volume.nx, this.volume.ny, this.volume.nz] : null };
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

  /** Pack `vol` into an RG8 3D texture (time-sliced); null when superseded or lost. */
  private async buildTexture(vol: CartVolume, gen: number): Promise<WebGLTexture | null> {
    const gl = this.gl;
    if (this.lost) return null;
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
          return null;
        }
        t0 = performance.now();
      }
    }
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
    gl.bindTexture(gl.TEXTURE_3D, null);
    if (gen !== this.volGen || this.disposed) {
      gl.deleteTexture(tex);
      return null;
    }
    return tex;
  }

  private async uploadVolume(vol: CartVolume, gen: number): Promise<void> {
    const gl = this.gl;
    const tex = await this.buildTexture(vol, gen);
    if (!tex) return;
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
    this.governor.setVolumeMB((vol.nx * vol.ny * vol.nz * 2) / 1048576);
    this.markDirty();
  }

  private async rebuildGpuData(): Promise<void> {
    const vol = this.volume;
    const gen = ++this.volGen;
    if (vol) await this.uploadVolume(vol, gen);
    this.setTransfer(this.tfData);
    if (this.lutData) this.setGlyphLut(this.lutData);
    if (this.glyphData) this.setGlyphs(this.glyphData, this.instCount);
    if (this.flow && this.flowField) this.flow.setField(this.flowField, this.flowCells, this.flowUser.requested, this.flowUser.lifeSec);
    if (this.groundImg) this.setGround(this.groundImg.size, this.groundImg.data, this.groundImg.half, this.groundCenter);
    if (this.terrain) this.setTerrain(this.terrain);
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
    this.requestFrame();
  }

  setGlyphStyle(s: Partial<GlyphStyle>): void {
    this.style = { ...this.style, ...s };
    if (s.lut) this.setGlyphLut(s.lut);
    this.requestFrame();
  }

  /** Map image over [-half, half]^2 km around the scene origin (RGBA, row 0 = north); null = none. */
  setGround(size: number, data: Uint8ClampedArray | null, half: number, center: [number, number] = [0, 0]): void {
    this.groundCenter = center;
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
    // mirrored beyond the image: the terrain ground continues past it without stretched streaks
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.MIRRORED_REPEAT);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.MIRRORED_REPEAT);
    const an = gl.getExtension('EXT_texture_filter_anisotropic');
    if (an) gl.texParameterf(gl.TEXTURE_2D, an.TEXTURE_MAX_ANISOTROPY_EXT, Math.min(8, gl.getParameter(an.MAX_TEXTURE_MAX_ANISOTROPY_EXT) as number));
    gl.activeTexture(gl.TEXTURE0);
    this.markDirty();
  }

  get hasGroundMap(): boolean {
    return this.groundImg !== null;
  }

  /**
   * Terrain: heights (km above sea level, row 0 = north) over the square [-half, half] around `center`
   * (the same square as the ground image, which is draped on it). The volume stops at the terrain.
   * Heights are drawn stretched by the scene's vertical exaggeration, like the volume. null = flat ground.
   */
  setTerrain(t: { size: number; heightsKm: Float32Array; minKm: number; maxKm: number; half: number; center: [number, number] } | null): void {
    this.terrain = t;
    if (t) this.groundCenter = t.center;
    if (this.lost) return;
    const gl = this.gl;
    gl.activeTexture(gl.TEXTURE5);
    gl.bindTexture(gl.TEXTURE_2D, this.demTex);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
    if (t) gl.texImage2D(gl.TEXTURE_2D, 0, gl.R16F, t.size, t.size, 0, gl.RED, gl.FLOAT, t.heightsKm);
    else gl.texImage2D(gl.TEXTURE_2D, 0, gl.R16F, 1, 1, 0, gl.RED, gl.FLOAT, new Float32Array([0]));
    gl.activeTexture(gl.TEXTURE0);
    this.markDirty();
  }

  get hasTerrain(): boolean {
    return this.terrain !== null;
  }

  /**
   * The "Realistic (stylised)" storm look: light, sky and haze; null = the scientific colour scale.
   * The caller also sets the matching transfer function (realisticStyle.ts buildRealisticTf).
   */
  setRealistic(look: RealisticLook | null): void {
    this.look = look;
    this.markDirty();
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
    this.nudge();
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
      this.nudge();
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
    const cb = this.cropBox;
    gl.uniform2f(u.u_groundMin, cb ? cb.xMin : sb.xMin, cb ? cb.yMin : sb.yMin);
    gl.uniform2f(u.u_groundMax, cb ? cb.xMax : sb.xMax, cb ? cb.yMax : sb.yMax);
    gl.uniform2f(u.u_cropMin, cb ? cb.xMin : -1e6, cb ? cb.yMin : -1e6);
    gl.uniform2f(u.u_cropMax, cb ? cb.xMax : 1e6, cb ? cb.yMax : 1e6);
    gl.uniform2f(u.u_groundCenter, this.groundCenter[0], this.groundCenter[1]);
    gl.uniform1f(u.u_mapGain, p.lightScene ? 1.0 : this.mapGain);
    gl.uniform1i(u.u_morphOn, this.volTexB ? 1 : 0);
    gl.uniform1f(u.u_morphW, this.morphW);
    gl.uniform3f(u.u_shiftA, this.shiftA[0], this.shiftA[1], this.shiftA[2]);
    gl.uniform3f(u.u_shiftB, this.shiftB[0], this.shiftB[1], this.shiftB[2]);
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
    gl.uniform1f(u.u_groundHalf, this.groundImg?.half ?? this.terrain?.half ?? 1);
    gl.uniform1f(u.u_ringStep, p.gridStepKm * 2);
    // terrain and the realistic look
    const terr = this.terrain;
    const terrOn = !!terr && p.groundMode !== 'off';
    gl.uniform1i(u.u_terrOn, terrOn ? 1 : 0);
    if (terr) {
      gl.uniform2f(u.u_terrZ, terr.minKm * ve, terr.maxKm * ve);
      gl.uniform1f(u.u_demTexel, (2 * terr.half) / terr.size);
    }
    const lk = this.look ?? DAY_LOOK;
    const sv = lightVector(lk, ve);
    gl.uniform1i(u.u_style, this.look ? 1 : 0);
    gl.uniform3f(u.u_sunDir, sv[0], sv[1], sv[2]);
    gl.uniform3f(u.u_sunCol, lk.sunCol[0], lk.sunCol[1], lk.sunCol[2]);
    gl.uniform3f(u.u_skyCol, lk.skyCol[0], lk.skyCol[1], lk.skyCol[2]);
    gl.uniform3f(u.u_horizonCol, lk.horizonCol[0], lk.horizonCol[1], lk.horizonCol[2]);
    gl.uniform3f(u.u_zenithCol, lk.zenithCol[0], lk.zenithCol[1], lk.zenithCol[2]);
    // soft shadows cost extra samples: fewer while the camera moves in the interactive viewer
    gl.uniform1i(u.u_sunSteps, params.interacting ? Math.min(2, lk.sunSteps) : lk.sunSteps);
    // haze is tuned for a scene about 235 km wide (the storm clip); bigger scenes get proportionally less per km
    gl.uniform1f(u.u_fog, lk.fog * Math.min(1, 117.5 / Math.max(1, terr?.half ?? 117.5)));
    gl.uniform1f(u.u_exposure, lk.exposure);
    gl.uniform1f(u.u_groundGain, this.mapGain);
    this.frameSeed = (this.frameSeed + 17.31) % 211;
    gl.uniform1f(u.u_seed, this.frameSeed);
    gl.uniform1f(u.u_vexag, ve);
    gl.uniform1i(u.u_light, p.light ? 1 : 0);
    gl.uniform1f(u.u_themeLight, p.lightScene ? 1 : 0);

    // fades: the flow and the field animation fade in / out over ~0.4 s
    const fdt = this.lastFadeT === 0 || nowT - this.lastFadeT > 250 ? 1 / 60 : (nowT - this.lastFadeT) / 1000;
    this.lastFadeT = nowT;
    const approach = (v: number, t: number) => (v < t ? Math.min(t, v + fdt / 0.4) : Math.max(t, v - fdt / 0.4));
    this.flowFade = approach(this.flowFade, this.flowTarget);
    this.animFade = approach(this.animFade, this.animTarget);
    if (this.flowFade !== this.flowTarget || this.animFade !== this.animTarget) this.pr.request();
    if (!this.flowPresent && !this.flowReleased && this.flow && this.flow.ready) {
      this.flow.release(); // faded out: free the particles
      this.flowReleased = true;
    }
    if (!this.animPresent && this.windTex) this.freeWindTexture();
    this.lastFadeT = this.flowPresent || this.animPresent ? nowT : 0;

    // wind flow: one update pass per frame (not per band); nothing runs while paused except a refresh
    const fl = this.flow;
    const flowOn = this.flowPresent && !!fl && fl.ready;
    const fieldAnim = this.animPresent && !!this.windTex;
    // While only the flow plays, the volume does not change from frame to frame: it is drawn once
    // (a "capture" frame copies its bands into a texture) and later frames just redraw the texture.
    // The animated field changes the volume every frame: no cache, interactive quality.
    const animating = flowOn && this.flowPlaying && !fieldAnim;
    this.useCache = animating && !params.interacting && !this.staticDirty && this.cacheValid && this.cacheRegion[0] === w && this.cacheRegion[1] === h;
    this.capture = animating && !params.interacting && !this.useCache;
    if (this.capture) {
      this.ensureCache();
      this.cacheValid = false;
      this.staticDirty = false;
    }
    if (fieldAnim) {
      if (this.flowPlaying) {
        const dtp = this.lastFlowT === 0 ? 1 / 60 : Math.min(0.05, (nowT - this.lastFlowT) / 1000);
        this.animPhase = (this.animPhase + dtp / this.animUser.period) % 1;
        if (!flowOn) this.lastFlowT = nowT;
        this.nudge(); // keeps the frames coming; the changing volume is drawn at interactive quality
      }
      this.staticDirty = true;
    }
    if (flowOn && fl) {
      const fp = this.flowParams(params.index);
      if (this.flowPlaying) {
        const gapMs = nowT - this.lastFlowT;
        const dt = this.lastFlowT === 0 || gapMs > 250 ? 1 / 60 : Math.min(gapMs / 1000, 0.05);
        this.lastFlowT = nowT;
        fl.step(dt, fp);
        this.pr.request(); // keep animating
      } else if (fl.needsRefresh) {
        fl.step(0, fp);
      }
    }
    // uniforms of the field animation (the volume program is current again for the draw)
    gl.useProgram(this.volProg);
    gl.uniform1i(u.u_animOn, fieldAnim ? 1 : 0);
    if (fieldAnim && this.flowField) {
      const f = this.flowField;
      gl.uniform1f(u.u_animAmp, this.animFade);
      gl.uniform1f(u.u_animT, (this.animUser.ampKm * 1000) / Math.max(1, this.animUser.refSpeed));
      gl.uniform1f(u.u_animPhase, this.animPhase);
      gl.uniform3f(u.u_wOrigin, f.x0, f.y0, f.z0);
      gl.uniform3f(u.u_wCell, f.dx, f.dy, f.dz);
      gl.uniform3f(u.u_wDims, f.nx, f.ny, f.nz);
      gl.uniform2f(u.u_wOffset, this.flowOffset[0], this.flowOffset[1]);
      gl.uniform1f(u.u_vexag, ve);
    }
    return true;
  }

  private ensureCache(): void {
    const gl = this.gl;
    const W = this.canvas.width;
    const H = this.canvas.height;
    if (this.cacheTex && this.cacheSize[0] === W && this.cacheSize[1] === H) return;
    if (this.cacheTex) gl.deleteTexture(this.cacheTex);
    this.cacheTex = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, this.cacheTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, W, H, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    this.cacheSize = [W, H];
    this.cacheValid = false;
  }

  private drawBand(y: number, rows: number, w: number, h: number): void {
    const gl = this.gl;
    const p = this.params;
    const fr = this.frame;
    gl.clearColor(0.05, 0.05, 0.1, 1);
    gl.clearDepth(1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.disable(gl.DEPTH_TEST);
    gl.disable(gl.CULL_FACE);

    if (this.useCache && this.cacheTex && this.cacheProg) {
      gl.useProgram(this.cacheProg);
      gl.bindVertexArray(this.quadVAO);
      gl.activeTexture(gl.TEXTURE2);
      gl.bindTexture(gl.TEXTURE_2D, this.cacheTex);
      gl.uniform1i(this.cacheLoc, 2);
      gl.activeTexture(gl.TEXTURE0);
      gl.drawArrays(gl.TRIANGLES, 0, 6);
      gl.bindVertexArray(null);
      this.drawDynamic(fr);
      return;
    }

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
    gl.activeTexture(gl.TEXTURE5);
    gl.bindTexture(gl.TEXTURE_2D, this.demTex);
    gl.uniform1i(this.vu.u_dem, 5);
    gl.activeTexture(gl.TEXTURE2);
    gl.bindTexture(gl.TEXTURE_3D, this.windTex);
    gl.uniform1i(this.vu.u_windTex, 2);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_3D, this.volTexB);
    gl.uniform1i(this.vu.u_volumeB, 1);
    gl.activeTexture(gl.TEXTURE0);
    gl.drawArrays(gl.TRIANGLES, 0, 6);
    gl.bindVertexArray(null);
    if (this.capture && this.cacheTex) {
      // keep a clean copy of this band (before arrows / flow are drawn over it)
      gl.activeTexture(gl.TEXTURE2);
      gl.bindTexture(gl.TEXTURE_2D, this.cacheTex);
      gl.copyTexSubImage2D(gl.TEXTURE_2D, 0, 0, y, 0, y, w, rows);
      gl.activeTexture(gl.TEXTURE0);
      if (y + rows >= h) {
        this.cacheValid = true;
        this.cacheRegion = [w, h];
      }
    }
    this.drawDynamic(fr);
  }

  /** Arrows and flow, over the static layer (every frame). */
  private drawDynamic(fr: { viewProj: Float32Array; eye: Vec3; w: number; h: number; params: QualityParams; tanY: number }): void {
    const gl = this.gl;
    const p = this.params;
    if (this.instCount === 0 || !this.arrowVAO) {
      this.drawFlow();
      return;
    }
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
    this.drawFlow();
    void p;
  }

  private drawFlow(): void {
    const fl = this.flow;
    if (!this.flowPresent || !fl || !fl.ready || this.flowActive <= 0 || this.flowFade <= 0) return;
    const gl = this.gl;
    const fr = this.frame;
    const ch = Math.max(1, this.container.clientHeight);
    gl.enable(gl.DEPTH_TEST);
    gl.depthFunc(gl.LESS);
    gl.depthMask(false);
    gl.enable(gl.BLEND);
    // the destination alpha stays 1 (the canvas is composited over the page): colour only
    if (this.flowUser.additive) gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE, gl.ZERO, gl.ONE);
    else gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA, gl.ZERO, gl.ONE);
    fl.draw(fr.viewProj, [fr.w, fr.h], { ...this.flowUser, opacity: this.flowUser.opacity * this.flowFade, active: this.flowActive }, this.params.vexag, this.flowOffset, fr.h / ch);
    gl.disable(gl.BLEND);
    gl.depthMask(true);
    gl.disable(gl.DEPTH_TEST);
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
      for (const t of [this.volTex, this.volTexB, this.tfTex, this.lutTex, this.groundTex, this.demTex]) if (t) gl.deleteTexture(t);
      for (const b of this.arrowMeshBuf) gl.deleteBuffer(b);
      if (this.arrowVAO) gl.deleteVertexArray(this.arrowVAO);
      gl.deleteVertexArray(this.quadVAO);
      gl.deleteProgram(this.volProg);
      gl.deleteProgram(this.arrowProg);
    }
    this.volume = null;
    this.glyphData = null;
    this.flow?.dispose();
    this.flow = null;
    this.freeWindTexture();
    this.canvas.remove();
  }
}
