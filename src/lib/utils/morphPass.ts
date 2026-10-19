/**
 * GPU time interpolation of two radar volumes along a flow (offline clip export).
 *
 * For a time between two observed volumes A (time 0) and B (time dt) one cell of the interpolated
 * volume is found by following the flow: the position is traced back to the time of A and forward
 * to the time of B along the velocity field (midpoint steps), A is read where the cell came from, B
 * where it is going, and the two are blended with the weights (1 - s) and s (coverage weighted, so a
 * cell missing in one volume takes the other). The velocity is the tracked storm translation plus
 * the storm-relative flow anomaly of an observed wind field (multi-Doppler winds, see flowMorph.ts):
 * the cloud is carried and turned by the winds that were measured, not by an invented swirl. Without
 * a wind field it is the translation alone.
 *
 * The result is written once per frame into a 3-D RG8 texture with the packing of the volume
 * (R = code * coverage, G = coverage), which the ray-marcher then reads like any other volume. The
 * same maths on the CPU (flowMorph.ts `morphCell`) is used by the tests.
 */

const VERT = `#version 300 es
  in vec2 a_pos;
  void main() { gl_Position = vec4(a_pos, 0.0, 1.0); }
`;

const FRAG = `#version 300 es
  precision highp float;
  precision highp sampler3D;
  uniform sampler3D u_a;
  uniform sampler3D u_b;
  uniform sampler3D u_fa;   // RGBA16F: storm-relative u, v (m/s), unused, confidence
  uniform sampler3D u_fb;
  uniform vec3  u_boxMin;   // volume box, km (true heights)
  uniform vec3  u_boxMax;
  uniform vec3  u_dims;     // texels of the volume texture
  uniform int   u_layer;
  uniform float u_s;        // position between A (0) and B (1)
  uniform float u_dt;       // seconds from A to B
  uniform vec2  u_track;    // storm translation, km/s
  uniform int   u_flowOn;
  uniform vec3  u_fOrigin;  // flow grid, km
  uniform vec3  u_fCell;
  uniform vec3  u_fDims;
  uniform vec2  u_fOffset;  // flow frame -> volume frame
  uniform int   u_steps;
  out vec4 o;

  vec2 flowAt(vec3 p) {
    if (u_flowOn == 0) return vec2(0.0);
    vec3 f = (vec3(p.xy - u_fOffset, p.z) - u_fOrigin) / u_fCell;
    if (any(lessThan(f, vec3(-0.5))) || any(greaterThan(f, u_fDims - 0.5))) return vec2(0.0);
    vec3 uvw = (f + 0.5) / u_fDims;
    vec2 va = texture(u_fa, uvw).xy;
    vec2 vb = texture(u_fb, uvw).xy;
    return mix(va, vb, u_s) * 0.001;
  }
  vec2 vel(vec3 p) { return u_track + flowAt(p); }

  // field of one volume at p: coverage-weighted code
  bool fetch(sampler3D tex, vec3 p, out float b, out float cov) {
    vec3 uvw = (p - u_boxMin) / (u_boxMax - u_boxMin);
    b = 0.0; cov = 0.0;
    if (any(lessThan(uvw, vec3(0.0))) || any(greaterThan(uvw, vec3(1.0)))) return false;
    vec2 rg = texture(tex, uvw).rg;
    if (rg.g < 0.02) return false;
    b = rg.r / rg.g;
    cov = rg.g;
    return true;
  }

  void main() {
    vec3 cell = vec3(gl_FragCoord.xy, float(u_layer)) + 0.5;
    vec3 p = u_boxMin + cell / u_dims * (u_boxMax - u_boxMin);
    float h = 1.0 / float(u_steps);
    // back to the time of A
    vec3 pa = p;
    float ta = u_s * u_dt * h;
    for (int i = 0; i < 8; i++) {
      if (i >= u_steps) break;
      vec2 v1 = vel(pa);
      vec2 mid = pa.xy - 0.5 * v1 * ta;
      pa.xy -= vel(vec3(mid, pa.z)) * ta;
    }
    // forward to the time of B
    vec3 pb = p;
    float tb = (1.0 - u_s) * u_dt * h;
    for (int i = 0; i < 8; i++) {
      if (i >= u_steps) break;
      vec2 v1 = vel(pb);
      vec2 mid = pb.xy + 0.5 * v1 * tb;
      pb.xy += vel(vec3(mid, pb.z)) * tb;
    }
    float ba, ca, bb, cb;
    bool ka = fetch(u_a, pa, ba, ca);
    bool kb = fetch(u_b, pb, bb, cb);
    float wa = ka ? (1.0 - u_s) * ca : 0.0;
    float wb = kb ? u_s * cb : 0.0;
    float ss = wa + wb;
    if (ss <= 0.002) { o = vec4(0.0); return; }
    float b = (wa * ba + wb * bb) / ss;
    o = vec4(b * ss, ss, 0.0, 1.0);
  }
`;

export interface FlowTex {
  tex: WebGLTexture;
  origin: [number, number, number];
  cell: [number, number, number];
  dims: [number, number, number];
}

export interface MorphParams {
  s: number;
  dt: number;
  /** Storm translation, km/s. */
  track: [number, number];
  /** Flow frame offset (km) to the volume frame. */
  flowOffset: [number, number];
  steps?: number;
}

export class MorphPass {
  private prog: WebGLProgram | null = null;
  private vao: WebGLVertexArrayObject | null = null;
  private vbo: WebGLBuffer | null = null;
  private fbo: WebGLFramebuffer | null = null;
  private u: Record<string, WebGLUniformLocation | null> = {};
  private out: WebGLTexture | null = null;
  private dims: [number, number, number] = [0, 0, 0];
  private zero: WebGLTexture | null = null;

  constructor(private gl: WebGL2RenderingContext) {
    this.build();
  }

  private compile(type: number, src: string): WebGLShader {
    const gl = this.gl;
    const sh = gl.createShader(type)!;
    gl.shaderSource(sh, src);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) throw new Error('morph shader: ' + gl.getShaderInfoLog(sh));
    return sh;
  }

  private build(): void {
    const gl = this.gl;
    const prog = gl.createProgram()!;
    gl.attachShader(prog, this.compile(gl.VERTEX_SHADER, VERT));
    gl.attachShader(prog, this.compile(gl.FRAGMENT_SHADER, FRAG));
    gl.bindAttribLocation(prog, 0, 'a_pos');
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error('morph program: ' + gl.getProgramInfoLog(prog));
    this.prog = prog;
    for (const n of ['u_a', 'u_b', 'u_fa', 'u_fb', 'u_boxMin', 'u_boxMax', 'u_dims', 'u_layer', 'u_s', 'u_dt', 'u_track', 'u_flowOn', 'u_fOrigin', 'u_fCell', 'u_fDims', 'u_fOffset', 'u_steps']) {
      this.u[n] = gl.getUniformLocation(prog, n);
    }
    this.vao = gl.createVertexArray()!;
    gl.bindVertexArray(this.vao);
    this.vbo = gl.createBuffer()!;
    gl.bindBuffer(gl.ARRAY_BUFFER, this.vbo);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);
    this.fbo = gl.createFramebuffer()!;
    // a 1x1x1 flow texture to bind when there is no flow
    this.zero = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_3D, this.zero);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texImage3D(gl.TEXTURE_3D, 0, gl.RGBA16F, 1, 1, 1, 0, gl.RGBA, gl.FLOAT, new Float32Array(4));
  }

  /** The output texture (created or resized to `dims`). */
  output(dims: [number, number, number]): WebGLTexture {
    const gl = this.gl;
    if (this.out && this.dims[0] === dims[0] && this.dims[1] === dims[1] && this.dims[2] === dims[2]) return this.out;
    if (this.out) gl.deleteTexture(this.out);
    this.out = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_3D, this.out);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_R, gl.CLAMP_TO_EDGE);
    gl.texImage3D(gl.TEXTURE_3D, 0, gl.RG8, dims[0], dims[1], dims[2], 0, gl.RG, gl.UNSIGNED_BYTE, null);
    this.dims = [...dims];
    return this.out;
  }

  /** Upload a storm-relative flow field (4 floats per cell: u, v, w, confidence) as a 3-D texture. */
  uploadFlow(data: Float32Array, nx: number, ny: number, nz: number, origin: [number, number, number], cell: [number, number, number]): FlowTex {
    const gl = this.gl;
    const tex = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_3D, tex);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_R, gl.CLAMP_TO_EDGE);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
    gl.texImage3D(gl.TEXTURE_3D, 0, gl.RGBA16F, nx, ny, nz, 0, gl.RGBA, gl.FLOAT, data);
    return { tex, origin, cell, dims: [nx, ny, nz] };
  }

  deleteFlow(f: FlowTex | null): void {
    if (f) this.gl.deleteTexture(f.tex);
  }

  /**
   * Interpolate volumes `a` and `b` (RG8 3-D textures of `dims` texels over `box`: min and max km) at
   * position `p.s` and return the output texture. Texture units 0 to 3 are used and cleared again.
   */
  run(a: WebGLTexture, b: WebGLTexture, dims: [number, number, number], box: { min: [number, number, number]; max: [number, number, number] }, p: MorphParams, fa: FlowTex | null, fb: FlowTex | null): WebGLTexture {
    const gl = this.gl;
    const out = this.output(dims);
    const prevFbo = gl.getParameter(gl.FRAMEBUFFER_BINDING) as WebGLFramebuffer | null;
    const prevVp = gl.getParameter(gl.VIEWPORT) as Int32Array;
    gl.useProgram(this.prog);
    gl.bindVertexArray(this.vao);
    const bindTex = (unit: number, t: WebGLTexture | null, loc: string) => {
      gl.activeTexture(gl.TEXTURE0 + unit);
      gl.bindTexture(gl.TEXTURE_3D, t);
      gl.uniform1i(this.u[loc], unit);
    };
    bindTex(0, a, 'u_a');
    bindTex(1, b, 'u_b');
    const flow = !!(fa && fb);
    bindTex(2, flow ? fa!.tex : this.zero, 'u_fa');
    bindTex(3, flow ? fb!.tex : this.zero, 'u_fb');
    const u = this.u;
    gl.uniform3f(u.u_boxMin, ...box.min);
    gl.uniform3f(u.u_boxMax, ...box.max);
    gl.uniform3f(u.u_dims, dims[0], dims[1], dims[2]);
    gl.uniform1f(u.u_s, p.s);
    gl.uniform1f(u.u_dt, p.dt);
    gl.uniform2f(u.u_track, p.track[0], p.track[1]);
    gl.uniform1i(u.u_flowOn, flow ? 1 : 0);
    if (flow) {
      gl.uniform3f(u.u_fOrigin, ...fa!.origin);
      gl.uniform3f(u.u_fCell, ...fa!.cell);
      gl.uniform3f(u.u_fDims, ...fa!.dims);
      gl.uniform2f(u.u_fOffset, p.flowOffset[0], p.flowOffset[1]);
    }
    gl.uniform1i(u.u_steps, p.steps ?? 4);
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.fbo);
    gl.viewport(0, 0, dims[0], dims[1]);
    gl.disable(gl.BLEND);
    gl.disable(gl.DEPTH_TEST);
    gl.disable(gl.SCISSOR_TEST);
    for (let k = 0; k < dims[2]; k++) {
      gl.framebufferTextureLayer(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, out, 0, k);
      gl.uniform1i(u.u_layer, k);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }
    gl.bindFramebuffer(gl.FRAMEBUFFER, prevFbo);
    gl.viewport(prevVp[0], prevVp[1], prevVp[2], prevVp[3]);
    gl.bindVertexArray(null);
    for (let unit = 0; unit < 4; unit++) {
      gl.activeTexture(gl.TEXTURE0 + unit);
      gl.bindTexture(gl.TEXTURE_3D, null);
    }
    gl.activeTexture(gl.TEXTURE0);
    return out;
  }

  dispose(): void {
    const gl = this.gl;
    if (this.out) gl.deleteTexture(this.out);
    if (this.zero) gl.deleteTexture(this.zero);
    if (this.prog) gl.deleteProgram(this.prog);
    if (this.vao) gl.deleteVertexArray(this.vao);
    if (this.vbo) gl.deleteBuffer(this.vbo);
    if (this.fbo) gl.deleteFramebuffer(this.fbo);
    this.out = this.zero = this.prog = this.vao = this.vbo = this.fbo = null;
  }
}
