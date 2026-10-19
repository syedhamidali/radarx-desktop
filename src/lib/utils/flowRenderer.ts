/**
 * GPU wind flow: animated particles with fading trails, advected by the retrieved 3D wind.
 *
 * Everything here mirrors the CPU reference in windFlow.ts (which the unit tests pin down):
 *  - the wind is an RGBA32F 3D texture (u, v, w in m/s, confidence; confidence 0 = no wind there)
 *    sampled with a manual NaN-free trilinear fetch: if ANY of the 8 cells has no wind the sample is
 *    invalid. Nothing is extrapolated, filled or smoothed, and there is no noise or turbulence;
 *  - particles move in physical space (km, m/s, u v AND w) with a midpoint (RK2) integrator; the
 *    step is the frame time times the visual speed, split in sub-steps of at most half a cell;
 *  - a particle that reaches a sample without wind stops, fades out and is reborn inside a cell all
 *    of whose corners have wind;
 *  - a trail is a piece of the streamline through the head: the field integrated BACKWARDS from the
 *    head (so there is no history to store and a paused frame is exactly reproducible); it is cut
 *    where the path leaves the wind;
 *  - the vertical exaggeration is applied only when drawing.
 *
 * One transform-feedback pass per frame updates every particle and its trail points; the state of
 * a particle (position, age, lifetime, colour value) and its NPTS trail points are interleaved in
 * one buffer that is ping-ponged. Trails are drawn as instanced screen-space ribbons: a light core
 * with a dark halo on dark scenes, the opposite on light ones.
 */

import { seedParticles, type FlowField } from './windFlow.ts';

/** Points per trail (head included). */
export const NPTS = 10;
/** Floats per particle: state(4) + aux(4) + NPTS * vec4. */
const FLOATS = 8 + 4 * NPTS;
const STRIDE = FLOATS * 4;
const SEED_W = 2048;

export interface FlowParams {
  /** Particles drawn and updated this frame (<= the allocated count). */
  active: number;
  /** Animation seconds a particle lives (a bit less / more per particle). */
  lifeSec: number;
  /** Simulated seconds per animation second. */
  visualSpeed: number;
  /** Trail length in animation seconds. */
  trailSec: number;
  widthPx: number;
  haloPx: number;
  opacity: number;
  /** 0 flat colour, 1 speed table, 2 w table (the value is computed on the GPU from u, v, w). */
  colorMode: 0 | 1 | 2;
  /** m/s at the top of the colour table (speed) or at +/- the ends (w). */
  colorMax: number;
  core: [number, number, number];
  halo: [number, number, number];
}

const PCG = `
  uint pcg(uint v) {
    uint s = v * 747796405u + 2891336453u;
    uint w = ((s >> ((s >> 28u) + 4u)) ^ s) * 277803737u;
    return (w >> 22u) ^ w;
  }
  float rnd(uint s) { return float(pcg(s)) * (1.0 / 4294967296.0); }
`;

const SAMPLE = `
  uniform highp sampler3D u_field;     // RGBA32F: u, v, w (m/s), confidence (0 = no wind)
  uniform ivec3 u_dims;
  uniform vec3  u_origin;              // km
  uniform vec3  u_cell;                // km
  // Trilinear sample; (0,0,0,0) when outside or when ANY of the 8 cells has no wind.
  vec4 sampleWind(vec3 p) {
    vec3 f = (p - u_origin) / u_cell;
    vec3 hi = vec3(u_dims - 1);
    if (!(f.x >= 0.0 && f.y >= 0.0 && f.z >= 0.0 && f.x <= hi.x && f.y <= hi.y && f.z <= hi.z)) return vec4(0.0);
    ivec3 i0 = min(ivec3(floor(f)), u_dims - 2);
    vec3 t = f - vec3(i0);
    vec4 acc = vec4(0.0);
    for (int c = 0; c < 8; c++) {
      ivec3 o = ivec3(c & 1, (c >> 1) & 1, (c >> 2) & 1);
      vec4 s = texelFetch(u_field, i0 + o, 0);
      if (s.a <= 0.0) return vec4(0.0);
      float wt = (o.x == 1 ? t.x : 1.0 - t.x) * (o.y == 1 ? t.y : 1.0 - t.y) * (o.z == 1 ? t.z : 1.0 - t.z);
      acc += s * wt;
    }
    return acc;
  }
`;

function updateVert(): string {
  const outs = Array.from({ length: NPTS }, (_, i) => `out vec4 v_t${i};`).join('\n  ');
  const assign = Array.from({ length: NPTS }, (_, i) => `v_t${i} = T[${i}];`).join('\n    ');
  return `#version 300 es
  precision highp float;
  precision highp int;
  precision highp sampler3D;
  precision highp sampler2D;
  ${SAMPLE}
  ${PCG}
  in vec4 a_state;   // x, y, z (km), age (animation s)
  in vec4 a_aux;     // life (animation s), frozen (-1 flowing, else s since it stopped), colour value, respawn count
  uniform highp sampler2D u_seeds;     // R32F: index of a cell all of whose corners have wind
  uniform int   u_seedCount;
  uniform float u_dtReal;              // animation seconds this frame
  uniform float u_dtSim;               // simulated seconds this frame
  uniform float u_lifeBase;
  uniform float u_fadeOut;
  uniform float u_trailH;              // simulated seconds per trail segment
  uniform float u_cellM;               // smallest cell, m
  uniform int   u_colorMode;
  uniform float u_colorMax;
  out vec4 v_state;
  out vec4 v_aux;
  ${outs}

  float colorVal(vec3 w) {
    if (u_colorMode == 1) return clamp(length(w.xy) / u_colorMax, 0.0, 1.0);
    if (u_colorMode == 2) return clamp(0.5 + w.z / (2.0 * u_colorMax), 0.0, 1.0);
    return 0.0;
  }

  void main() {
    vec3 p = a_state.xyz;
    float age = a_state.w;
    float life = a_aux.x;
    float frozen = a_aux.y;
    float cval = a_aux.z;
    float cnt = a_aux.w;
    bool respawn = false;

    if (frozen >= 0.0) {
      frozen += u_dtReal;
      if (frozen >= u_fadeOut) respawn = true;
    } else {
      age += u_dtReal;
      if (age >= life) respawn = true;
      else if (u_dtSim > 0.0) {
        vec4 s0 = sampleWind(p);
        if (s0.a <= 0.0) frozen = 0.0;
        else {
          float spd = length(s0.xyz);
          int n = int(clamp(ceil(spd * u_dtSim / (0.5 * u_cellM)), 1.0, 8.0));
          float h = u_dtSim / float(n);
          for (int i = 0; i < 8; i++) {
            if (i >= n) break;
            vec4 a = sampleWind(p);
            if (a.a <= 0.0) { frozen = 0.0; break; }
            vec3 pm = p + 0.5 * h * a.xyz / 1000.0;
            vec4 b = sampleWind(pm);
            if (b.a <= 0.0) { frozen = 0.0; break; }
            vec3 pn = p + h * b.xyz / 1000.0;
            if (sampleWind(pn).a <= 0.0) { frozen = 0.0; break; }
            p = pn;
          }
        }
      }
    }

    if (respawn && u_seedCount > 0) {
      uint s = pcg(uint(gl_VertexID) * 2654435761u ^ pcg(uint(cnt) + 1u));
      float r1 = rnd(s); s = pcg(s);
      float r2 = rnd(s); s = pcg(s);
      float r3 = rnd(s); s = pcg(s);
      float r4 = rnd(s); s = pcg(s);
      float r5 = rnd(s);
      int idx = min(int(floor(r1 * float(u_seedCount))), u_seedCount - 1);
      int ci = int(texelFetch(u_seeds, ivec2(idx % ${SEED_W}, idx / ${SEED_W}), 0).r + 0.5);
      int cx = u_dims.x - 1;
      int cy = u_dims.y - 1;
      int ii = ci % cx;
      int jj = (ci / cx) % cy;
      int kk = ci / (cx * cy);
      p = u_origin + (vec3(float(ii), float(jj), float(kk)) + vec3(r2, r3, r4)) * u_cell;
      life = u_lifeBase * (0.6 + 0.8 * r5);
      age = 0.0;
      frozen = -1.0;
      cnt += 1.0;
    }

    vec4 sh = sampleWind(p);
    bool ok = sh.a > 0.0;
    if (ok) cval = colorVal(sh.xyz);

    // the trail: the streamline through the head, integrated backwards
    vec4 T[${NPTS}];
    vec3 q = p;
    T[0] = vec4(p, ok ? sh.a : 0.0);
    for (int k = 1; k < ${NPTS}; k++) {
      if (ok) {
        vec4 a0 = sampleWind(q);
        float spd = length(a0.xyz);
        int ns = int(clamp(ceil(spd * u_trailH / (0.75 * u_cellM)), 1.0, 3.0));
        float hh = -u_trailH / float(ns);
        vec3 c = q;
        for (int s2 = 0; s2 < 3; s2++) {
          if (s2 >= ns) break;
          vec4 a = sampleWind(c);
          if (a.a <= 0.0) { ok = false; break; }
          vec3 cm = c + 0.5 * hh * a.xyz / 1000.0;
          vec4 b = sampleWind(cm);
          if (b.a <= 0.0) { ok = false; break; }
          vec3 cn = c + hh * b.xyz / 1000.0;
          if (sampleWind(cn).a <= 0.0) { ok = false; break; }
          c = cn;
        }
        if (ok) q = c;
      }
      vec4 sq = ok ? sampleWind(q) : vec4(0.0);
      if (sq.a <= 0.0) ok = false;
      T[k] = vec4(q, ok ? sq.a : 0.0);
    }

    v_state = vec4(p, age);
    v_aux = vec4(life, frozen, cval, cnt);
    ${assign}
    gl_Position = vec4(0.0);
    gl_PointSize = 1.0;
  }
`;
}

const UPDATE_FRAG = `#version 300 es
  precision highp float;
  out vec4 o;
  void main() { o = vec4(0.0); }
`;

function renderVert(): string {
  const ins = Array.from({ length: NPTS }, (_, i) => `in vec4 a_t${i};`).join('\n  ');
  const arr = Array.from({ length: NPTS }, (_, i) => `a_t${i}`).join(', ');
  return `#version 300 es
  precision highp float;
  precision highp int;
  in vec4 a_state;
  in vec4 a_aux;
  ${ins}
  uniform mat4  u_viewProj;
  uniform vec2  u_res;
  uniform float u_widthPx;
  uniform float u_haloPx;
  uniform float u_vexag;
  uniform vec2  u_offset;      // km: wind grid frame -> scene frame
  uniform float u_fadeIn;
  uniform float u_fadeOut;
  uniform float u_opacity;
  uniform int   u_colorMode;
  uniform sampler2D u_lut;
  uniform vec3  u_core;
  out vec4  v_col;
  out float v_across;
  out float v_coreFrac;

  float sm(float a, float b, float x) { float t = clamp((x - a) / (b - a), 0.0, 1.0); return t * t * (3.0 - 2.0 * t); }

  void main() {
    int seg = gl_VertexID / 6;
    int cv = gl_VertexID - seg * 6;
    // corners of a segment quad: end (0 = tail side a, 1 = b) and side (-1 / +1)
    int endIdx = (cv == 2 || cv == 3 || cv == 5) ? 1 : 0;
    float side = (cv == 0 || cv == 2 || cv == 3) ? -1.0 : 1.0;
    vec4 pts[${NPTS}] = vec4[${NPTS}](${arr});
    vec4 pa = pts[seg];
    vec4 pb = pts[seg + 1];
    float life = a_aux.x;
    float age = a_state.w;
    float frozen = a_aux.y;
    float fade = sm(0.0, u_fadeIn, age) * (1.0 - sm(life - u_fadeOut, life, age));
    if (frozen >= 0.0) fade *= 1.0 - sm(0.0, u_fadeOut, frozen);
    if (life <= 0.0 || pa.w <= 0.0 || pb.w <= 0.0 || fade <= 0.001) {
      gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
      v_col = vec4(0.0); v_across = 0.0; v_coreFrac = 1.0;
      return;
    }
    vec3 wa = vec3(pa.xy + u_offset, pa.z * u_vexag);
    vec3 wb = vec3(pb.xy + u_offset, pb.z * u_vexag);
    vec4 ca = u_viewProj * vec4(wa, 1.0);
    vec4 cb = u_viewProj * vec4(wb, 1.0);
    if (ca.w < 0.01 || cb.w < 0.01) {
      gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
      v_col = vec4(0.0); v_across = 0.0; v_coreFrac = 1.0;
      return;
    }
    vec2 sa = ca.xy / ca.w * u_res * 0.5;
    vec2 sb = cb.xy / cb.w * u_res * 0.5;
    vec2 d = sb - sa;
    float l = length(d);
    vec2 dir = l > 1e-4 ? d / l : vec2(1.0, 0.0);
    vec2 nrm = vec2(-dir.y, dir.x);
    float tpos = (float(seg) + float(endIdx)) / float(${NPTS - 1});
    vec4 c = endIdx == 0 ? ca : cb;
    vec2 s = endIdx == 0 ? sa : sb;
    float taper = mix(1.0, 0.5, tpos);
    float halfW = 0.5 * (u_widthPx + 2.0 * u_haloPx) * taper;
    vec2 ndc = (s + nrm * side * halfW) / (u_res * 0.5);
    gl_Position = vec4(ndc * c.w, c.z, c.w);
    float conf = endIdx == 0 ? pa.w : pb.w;
    float a = u_opacity * fade * pow(1.0 - tpos * 0.92, 1.5) * mix(0.25, 1.0, conf);
    vec3 col = u_colorMode == 0 ? u_core : texture(u_lut, vec2((a_aux.z * 255.0 + 0.5) / 256.0, 0.5)).rgb;
    v_col = vec4(col, a);
    v_across = side;
    v_coreFrac = u_widthPx / (u_widthPx + 2.0 * u_haloPx);
  }
`;
}

const RENDER_FRAG = `#version 300 es
  precision highp float;
  in vec4 v_col;
  in float v_across;
  in float v_coreFrac;
  uniform vec3 u_halo;
  out vec4 fragColor;
  void main() {
    float ax = abs(v_across);
    bool core = ax <= v_coreFrac;
    float edge = 1.0 - smoothstep(0.82, 1.0, ax);
    float a = v_col.a * (core ? 1.0 : 0.75) * edge;
    if (a < 0.003) discard;
    fragColor = vec4(core ? v_col.rgb : u_halo, a);
  }
`;

interface Buf {
  buf: WebGLBuffer;
  updVao: WebGLVertexArrayObject;
  drawVao: WebGLVertexArrayObject;
}

export class FlowLayer {
  private gl: WebGL2RenderingContext;
  private updProg: WebGLProgram;
  private drawProg: WebGLProgram;
  private uu: Record<string, WebGLUniformLocation | null> = {};
  private du: Record<string, WebGLUniformLocation | null> = {};
  private tf: WebGLTransformFeedback;
  private bufs: Buf[] = [];
  private cur = 0;
  private capacity = 0;
  private fieldTex: WebGLTexture | null = null;
  private seedTex: WebGLTexture | null = null;
  private lutTex: WebGLTexture;
  private field: FlowField | null = null;
  private cells: Uint32Array = new Uint32Array(0);
  private lifeSec = 6;
  private dirtyTrail = true;
  /** Frame the next update must run even when paused (new data / parameters). */
  hasData = false;

  constructor(gl: WebGL2RenderingContext) {
    this.gl = gl;
    this.updProg = this.link(updateVert(), UPDATE_FRAG, ['v_state', 'v_aux', ...Array.from({ length: NPTS }, (_, i) => `v_t${i}`)]);
    this.drawProg = this.link(renderVert(), RENDER_FRAG, null);
    const names = ['u_field', 'u_dims', 'u_origin', 'u_cell', 'u_seeds', 'u_seedCount', 'u_dtReal', 'u_dtSim', 'u_lifeBase', 'u_fadeOut', 'u_trailH', 'u_cellM', 'u_colorMode', 'u_colorMax'];
    for (const n of names) this.uu[n] = gl.getUniformLocation(this.updProg, n);
    const dn = ['u_viewProj', 'u_res', 'u_widthPx', 'u_haloPx', 'u_vexag', 'u_offset', 'u_fadeIn', 'u_fadeOut', 'u_opacity', 'u_colorMode', 'u_lut', 'u_core', 'u_halo'];
    for (const n of dn) this.du[n] = gl.getUniformLocation(this.drawProg, n);
    this.tf = gl.createTransformFeedback()!;
    this.lutTex = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, this.lutTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array(256 * 4).fill(255));
  }

  private link(vs: string, fs: string, varyings: string[] | null): WebGLProgram {
    const gl = this.gl;
    const mk = (type: number, src: string) => {
      const sh = gl.createShader(type)!;
      gl.shaderSource(sh, src);
      gl.compileShader(sh);
      if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
        const log = gl.getShaderInfoLog(sh);
        gl.deleteShader(sh);
        throw new Error(`flow shader compile error: ${log}`);
      }
      return sh;
    };
    const v = mk(gl.VERTEX_SHADER, vs);
    const f = mk(gl.FRAGMENT_SHADER, fs);
    const p = gl.createProgram()!;
    gl.attachShader(p, v);
    gl.attachShader(p, f);
    if (varyings) gl.transformFeedbackVaryings(p, varyings, gl.INTERLEAVED_ATTRIBS);
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(`flow program link error: ${gl.getProgramInfoLog(p)}`);
    gl.deleteShader(v);
    gl.deleteShader(f);
    return p;
  }

  /** The particle buffers exist and there is a wind field. */
  get ready(): boolean {
    return this.hasData && this.capacity > 0 && !!this.fieldTex;
  }

  setLut(rgba: Uint8Array): void {
    const gl = this.gl;
    gl.bindTexture(gl.TEXTURE_2D, this.lutTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, rgba);
  }

  /** Upload the wind and the cells particles may be born in; null removes them. Reseeds the particles. */
  setField(f: FlowField | null, cells: Uint32Array, capacity: number, lifeSec: number): void {
    const gl = this.gl;
    this.field = f;
    this.cells = cells;
    this.lifeSec = lifeSec;
    if (this.fieldTex) { gl.deleteTexture(this.fieldTex); this.fieldTex = null; }
    if (this.seedTex) { gl.deleteTexture(this.seedTex); this.seedTex = null; }
    this.hasData = !!f;
    if (!f) return;
    this.fieldTex = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_3D, this.fieldTex);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_R, gl.CLAMP_TO_EDGE);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
    gl.texImage3D(gl.TEXTURE_3D, 0, gl.RGBA32F, f.nx, f.ny, f.nz, 0, gl.RGBA, gl.FLOAT, f.data);
    // seeds: R32F, SEED_W wide
    const rows = Math.max(1, Math.ceil(cells.length / SEED_W));
    const seeds = new Float32Array(SEED_W * rows);
    for (let i = 0; i < cells.length; i++) seeds[i] = cells[i];
    this.seedTex = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, this.seedTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, SEED_W, rows, 0, gl.RED, gl.FLOAT, seeds);
    this.reseed(capacity);
  }

  /** (Re)allocate the buffers for `capacity` particles and fill them with fresh seeds. */
  reseed(capacity: number, seed = 1): void {
    const gl = this.gl;
    this.freeBuffers();
    if (!this.field || capacity <= 0) { this.capacity = 0; return; }
    const init = seedParticles(this.field, this.cells, capacity, this.lifeSec, seed);
    const data = new Float32Array(capacity * FLOATS);
    for (let i = 0; i < capacity; i++) {
      data.set(init.state.subarray(i * 4, i * 4 + 4), i * FLOATS);
      data.set(init.aux.subarray(i * 4, i * 4 + 4), i * FLOATS + 4);
    }
    for (let b = 0; b < 2; b++) {
      const buf = gl.createBuffer()!;
      gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      if (b === 0) gl.bufferData(gl.ARRAY_BUFFER, data, gl.DYNAMIC_COPY);
      else gl.bufferData(gl.ARRAY_BUFFER, capacity * STRIDE, gl.DYNAMIC_COPY);
      this.bufs.push({ buf, updVao: this.makeVao(this.updProg, buf, false), drawVao: this.makeVao(this.drawProg, buf, true) });
    }
    this.cur = 0;
    this.capacity = capacity;
    this.dirtyTrail = true;
  }

  private makeVao(prog: WebGLProgram, buf: WebGLBuffer, instanced: boolean): WebGLVertexArrayObject {
    const gl = this.gl;
    const vao = gl.createVertexArray()!;
    gl.bindVertexArray(vao);
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    const attr = (name: string, floatOffset: number) => {
      const l = gl.getAttribLocation(prog, name);
      if (l < 0) return;
      gl.enableVertexAttribArray(l);
      gl.vertexAttribPointer(l, 4, gl.FLOAT, false, STRIDE, floatOffset * 4);
      if (instanced) gl.vertexAttribDivisor(l, 1);
    };
    attr('a_state', 0);
    attr('a_aux', 4);
    if (instanced) for (let i = 0; i < NPTS; i++) attr(`a_t${i}`, 8 + 4 * i);
    gl.bindVertexArray(null);
    gl.bindBuffer(gl.ARRAY_BUFFER, null); // a buffer written by transform feedback must not stay bound elsewhere
    return vao;
  }

  private freeBuffers(): void {
    const gl = this.gl;
    for (const b of this.bufs) {
      gl.deleteBuffer(b.buf);
      gl.deleteVertexArray(b.updVao);
      gl.deleteVertexArray(b.drawVao);
    }
    this.bufs = [];
    this.capacity = 0;
  }

  /** Drop every GPU object that holds data (the tab is hidden); `setField` brings them back. */
  release(): void {
    const gl = this.gl;
    this.freeBuffers();
    if (this.fieldTex) gl.deleteTexture(this.fieldTex);
    if (this.seedTex) gl.deleteTexture(this.seedTex);
    this.fieldTex = null;
    this.seedTex = null;
    this.hasData = false;
  }

  get allocated(): number {
    return this.capacity;
  }

  /** Force the next `step` to recompute the trails even with dt = 0 (parameters changed). */
  markDirty(): void {
    this.dirtyTrail = true;
  }

  get needsRefresh(): boolean {
    return this.dirtyTrail;
  }

  /**
   * One update pass: advance every active particle by `dtReal` animation seconds
   * (`dtReal * visualSpeed` simulated seconds) and recompute its trail points.
   */
  step(dtReal: number, p: FlowParams): void {
    const f = this.field;
    if (!this.ready || !f) return;
    const gl = this.gl;
    const n = Math.min(p.active, this.capacity);
    if (n <= 0) return;
    const src = this.bufs[this.cur];
    const dst = this.bufs[1 - this.cur];
    gl.useProgram(this.updProg);
    const u = this.uu;
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_3D, this.fieldTex);
    gl.uniform1i(u.u_field, 0);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, this.seedTex);
    gl.uniform1i(u.u_seeds, 1);
    gl.activeTexture(gl.TEXTURE0);
    gl.uniform3i(u.u_dims, f.nx, f.ny, f.nz);
    gl.uniform3f(u.u_origin, f.x0, f.y0, f.z0);
    gl.uniform3f(u.u_cell, f.dx, f.dy, f.dz);
    gl.uniform1i(u.u_seedCount, this.cells.length);
    gl.uniform1f(u.u_dtReal, dtReal);
    gl.uniform1f(u.u_dtSim, dtReal * p.visualSpeed);
    gl.uniform1f(u.u_lifeBase, p.lifeSec);
    gl.uniform1f(u.u_fadeOut, Math.min(0.6, p.lifeSec * 0.25));
    gl.uniform1f(u.u_trailH, (p.trailSec * p.visualSpeed) / (NPTS - 1));
    gl.uniform1f(u.u_cellM, 1000 * Math.min(f.dx, f.dy, f.dz));
    gl.uniform1i(u.u_colorMode, p.colorMode);
    gl.uniform1f(u.u_colorMax, Math.max(1, p.colorMax));
    gl.bindBuffer(gl.ARRAY_BUFFER, null);
    gl.bindVertexArray(src.updVao);
    gl.bindTransformFeedback(gl.TRANSFORM_FEEDBACK, this.tf);
    gl.bindBufferBase(gl.TRANSFORM_FEEDBACK_BUFFER, 0, dst.buf);
    gl.enable(gl.RASTERIZER_DISCARD);
    gl.beginTransformFeedback(gl.POINTS);
    gl.drawArrays(gl.POINTS, 0, n);
    gl.endTransformFeedback();
    gl.disable(gl.RASTERIZER_DISCARD);
    gl.bindBufferBase(gl.TRANSFORM_FEEDBACK_BUFFER, 0, null);
    gl.bindTransformFeedback(gl.TRANSFORM_FEEDBACK, null);
    gl.bindVertexArray(null);
    // particles beyond `n` keep their state: carry it over so they resume where they were
    if (n < this.capacity) {
      gl.bindBuffer(gl.COPY_READ_BUFFER, src.buf);
      gl.bindBuffer(gl.COPY_WRITE_BUFFER, dst.buf);
      gl.copyBufferSubData(gl.COPY_READ_BUFFER, gl.COPY_WRITE_BUFFER, n * STRIDE, n * STRIDE, (this.capacity - n) * STRIDE);
      gl.bindBuffer(gl.COPY_READ_BUFFER, null);
      gl.bindBuffer(gl.COPY_WRITE_BUFFER, null);
    }
    this.cur = 1 - this.cur;
    this.dirtyTrail = false;
  }

  /** Draw the trails (call with the depth buffer enabled and blending on). */
  draw(
    viewProj: Float32Array,
    res: [number, number],
    p: FlowParams,
    vexag: number,
    offset: [number, number],
    pxScale: number,
  ): void {
    if (!this.ready) return;
    const n = Math.min(p.active, this.capacity);
    if (n <= 0) return;
    const gl = this.gl;
    gl.useProgram(this.drawProg);
    const u = this.du;
    gl.uniformMatrix4fv(u.u_viewProj, false, viewProj);
    gl.uniform2f(u.u_res, res[0], res[1]);
    gl.uniform1f(u.u_widthPx, p.widthPx * pxScale);
    gl.uniform1f(u.u_haloPx, p.haloPx * pxScale);
    gl.uniform1f(u.u_vexag, vexag);
    gl.uniform2f(u.u_offset, offset[0], offset[1]);
    gl.uniform1f(u.u_fadeIn, Math.min(0.5, p.lifeSec * 0.2));
    gl.uniform1f(u.u_fadeOut, Math.min(0.6, p.lifeSec * 0.25));
    gl.uniform1f(u.u_opacity, p.opacity);
    gl.uniform1i(u.u_colorMode, p.colorMode === 0 ? 0 : 1);
    gl.uniform3f(u.u_core, p.core[0], p.core[1], p.core[2]);
    gl.uniform3f(u.u_halo, p.halo[0], p.halo[1], p.halo[2]);
    gl.activeTexture(gl.TEXTURE6);
    gl.bindTexture(gl.TEXTURE_2D, this.lutTex);
    gl.uniform1i(u.u_lut, 6);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindVertexArray(this.bufs[this.cur].drawVao);
    gl.drawArraysInstanced(gl.TRIANGLES, 0, (NPTS - 1) * 6, n);
    gl.bindVertexArray(null);
  }

  /** Test hook: the first `n` particles of the current buffer (state, aux, trail points). */
  readBack(n: number): Float32Array {
    const gl = this.gl;
    const out = new Float32Array(n * FLOATS);
    gl.bindBuffer(gl.COPY_READ_BUFFER, this.bufs[this.cur].buf);
    gl.getBufferSubData(gl.COPY_READ_BUFFER, 0, out);
    gl.bindBuffer(gl.COPY_READ_BUFFER, null);
    return out;
  }

  dispose(): void {
    const gl = this.gl;
    this.release();
    gl.deleteTexture(this.lutTex);
    gl.deleteTransformFeedback(this.tf);
    gl.deleteProgram(this.updProg);
    gl.deleteProgram(this.drawProg);
  }
}

export const FLOW_FLOATS = FLOATS;
