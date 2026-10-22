/**
 * WebGL2 renderer of the radar globe: an orthographic sphere whose surface is looked up per pixel from a
 * flat image of the world (Web Mercator map tiles composited into one image, or a plate carree image of
 * the bundled land outlines). One full-screen triangle, one fragment shader:
 *
 *  - each pixel inside the disc is turned into a point of the unit sphere (orthographic, see globeMath.ts),
 *    then into longitude and latitude, then into image coordinates; so there is no mesh, no seam in the
 *    geometry and no pinching at the poles;
 *  - the longitude seam and the pole are handled in the texture lookup: the sampler gets explicit
 *    derivatives that ignore the jump of the wrapped longitude (else the filter picks the coarsest level
 *    along the date line and draws a line), and beyond the last Mercator row the polar cap is painted with
 *    the mean colour of the last row instead of the stretched row (see poleBlend);
 *  - the dark map theme is the same colour filter the flat map applies with CSS, as an affine map
 *    (parseColourFilter), so both views look alike in both themes.
 *
 * Lifecycle and style follow ppiRenderer.ts / cartesianRenderer.ts: constructed on a canvas, `resize`
 * and `render` on demand (nothing runs while idle), context loss is survived (the textures are rebuilt from
 * the source canvas), `dispose` releases everything. Markers, lines and text are drawn by the caller on a
 * 2D canvas over this one; this class only draws the globe's surface.
 */
import { viewBasis, MERCATOR_MAX_LAT, MAX_FILTER_STAGES, type GlobeView, type ColourAffine } from './globeMath';

export type GlobeTextureKind = 'mercator' | 'equirect';

const VERT = `#version 300 es
  void main() {
    vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
  }
`;

const FRAG = `#version 300 es
  precision highp float;
  uniform vec2  u_center;   // device pixels, y up
  uniform float u_radius;   // device pixels
  uniform vec3  u_R;        // screen right, up and towards-viewer axes in world space
  uniform vec3  u_U;
  uniform vec3  u_F;
  uniform sampler2D u_tex;
  uniform int   u_mode;     // 0 plate carree, 1 Web Mercator, 2 no image (flat colour)
  uniform vec3  u_poleN;
  uniform vec3  u_poleS;
  uniform vec3  u_flat;
  uniform int   u_nf;       // colour filter stages: rgb = clamp(u_fm[i] * rgb + u_ft[i], 0, 1), in order
  uniform mat3  u_fm[${MAX_FILTER_STAGES}];
  uniform vec3  u_ft[${MAX_FILTER_STAGES}];
  out vec4 outColor;

  const float PI = 3.14159265358979;
  const float MAX_SIN = ${Math.sin((MERCATOR_MAX_LAT * Math.PI) / 180).toFixed(12)};

  void main() {
    vec2 p = (gl_FragCoord.xy - u_center) / u_radius;
    float r = length(p);
    // one device pixel of anti-aliasing at the limb, no discard (derivatives stay defined)
    float cover = clamp((1.0 - r) * u_radius + 0.5, 0.0, 1.0);
    float rc = min(r, 0.9999);
    p *= rc / max(r, 1e-6);
    float d = sqrt(max(0.0, 1.0 - rc * rc));
    vec3 w = p.x * u_R + p.y * u_U + d * u_F;
    float s = clamp(w.z, -1.0, 1.0);
    vec3 col = u_flat;
    if (u_mode != 2) {
      float u = atan(w.y, w.x) / (2.0 * PI) + 0.5;
      float ua = fract(u + 0.5);
      float v;
      float poleW = 0.0;
      if (u_mode == 1) {
        float sc = clamp(s, -MAX_SIN, MAX_SIN);
        v = 0.5 - 0.5 * log((1.0 + sc) / (1.0 - sc)) / (2.0 * PI);
        poleW = smoothstep(83.5, ${MERCATOR_MAX_LAT.toFixed(6)}, degrees(abs(asin(s))));
      } else {
        v = 0.5 - asin(s) / PI;
      }
      // derivatives of the wrapped longitude: take the continuous one of u and u shifted by half a turn
      float dux = dFdx(u), duax = dFdx(ua), duy = dFdy(u), duay = dFdy(ua);
      vec2 gx = vec2(abs(duax) < abs(dux) ? duax : dux, dFdx(v));
      vec2 gy = vec2(abs(duay) < abs(duy) ? duay : duy, dFdy(v));
      col = textureGrad(u_tex, vec2(u, v), gx, gy).rgb;
      if (u_mode == 1) col = mix(col, s > 0.0 ? u_poleN : u_poleS, poleW);
      for (int i = 0; i < ${MAX_FILTER_STAGES}; i++) { if (i >= u_nf) break; col = clamp(u_fm[i] * col + u_ft[i], 0.0, 1.0); }
    }
    outColor = vec4(col * cover, cover);
  }
`;

export interface GlobeRendererHooks {
  /** The GL context was lost (the globe is blank until it is restored). */
  onLost?: () => void;
  /** The context is back and the surface was rebuilt: draw again. */
  onRestored?: () => void;
}

export interface GlobeFrame {
  view: GlobeView;
  /** Disc radius and centre in CSS pixels. */
  radius: number;
  cx: number;
  cy: number;
}

export function webgl2Available(): boolean {
  try {
    if (typeof document === 'undefined') return false;
    const c = document.createElement('canvas');
    const gl = c.getContext('webgl2');
    if (!gl) return false;
    gl.getExtension('WEBGL_lose_context')?.loseContext();
    return true;
  } catch {
    return false;
  }
}

export class GlobeRenderer {
  readonly canvas: HTMLCanvasElement;
  private gl: WebGL2RenderingContext;
  private prog: WebGLProgram | null = null;
  private tex: WebGLTexture | null = null;
  private uni: Record<string, WebGLUniformLocation | null> = {};
  private source: HTMLCanvasElement | null = null;
  private mode: 0 | 1 | 2 = 2;
  private poleN: [number, number, number] = [0.5, 0.5, 0.5];
  private poleS: [number, number, number] = [0.5, 0.5, 0.5];
  private flat: [number, number, number] = [0.1, 0.1, 0.1];
  private filter: ColourAffine[] = [];
  private dpr = 1;
  private cssW = 1;
  private cssH = 1;
  private disposed = false;
  private _lost = false;
  private readonly hooks: GlobeRendererHooks;
  private readonly onLostEv: (e: Event) => void;
  private readonly onRestoredEv: () => void;
  /** Milliseconds the CPU spent issuing the last frame. */
  lastCpuMs = 0;

  constructor(canvas: HTMLCanvasElement, hooks: GlobeRendererHooks = {}) {
    this.canvas = canvas;
    this.hooks = hooks;
    const gl = canvas.getContext('webgl2', { antialias: false, alpha: true, premultipliedAlpha: true, depth: false, stencil: false, powerPreference: 'default' });
    if (!gl) throw new Error('WebGL2 is not available on this system.');
    this.gl = gl;
    this.setupGl();
    this.onLostEv = (e) => {
      e.preventDefault(); // allows 'webglcontextrestored' to fire
      this._lost = true;
      this.hooks.onLost?.();
    };
    this.onRestoredEv = () => {
      if (this.disposed) return;
      this.prog = null;
      this.tex = null;
      this.setupGl();
      this.uploadTexture();
      this._lost = false;
      this.hooks.onRestored?.();
    };
    canvas.addEventListener('webglcontextlost', this.onLostEv, false);
    canvas.addEventListener('webglcontextrestored', this.onRestoredEv, false);
  }

  get lost(): boolean {
    return this._lost;
  }

  private setupGl(): void {
    const gl = this.gl;
    const mk = (type: number, src: string) => {
      const sh = gl.createShader(type)!;
      gl.shaderSource(sh, src);
      gl.compileShader(sh);
      if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
        const log = gl.getShaderInfoLog(sh);
        gl.deleteShader(sh);
        throw new Error(`Globe shader failed to compile: ${log}`);
      }
      return sh;
    };
    const vs = mk(gl.VERTEX_SHADER, VERT);
    const fs = mk(gl.FRAGMENT_SHADER, FRAG);
    const prog = gl.createProgram()!;
    gl.attachShader(prog, vs);
    gl.attachShader(prog, fs);
    gl.linkProgram(prog);
    gl.deleteShader(vs);
    gl.deleteShader(fs);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      const log = gl.getProgramInfoLog(prog);
      gl.deleteProgram(prog);
      throw new Error(`Globe program failed to link: ${log}`);
    }
    this.prog = prog;
    this.uni = {};
    for (const n of ['u_center', 'u_radius', 'u_R', 'u_U', 'u_F', 'u_tex', 'u_mode', 'u_poleN', 'u_poleS', 'u_flat', 'u_nf', 'u_fm', 'u_ft']) {
      this.uni[n] = gl.getUniformLocation(prog, n);
    }
    gl.disable(gl.DEPTH_TEST);
    gl.disable(gl.BLEND);
  }

  /** Size the drawing buffer to the CSS size of the container (capped device pixel ratio). */
  resize(cssW: number, cssH: number, dpr: number): void {
    this.cssW = Math.max(1, cssW);
    this.cssH = Math.max(1, cssH);
    this.dpr = Math.max(1, Math.min(2, dpr));
    const w = Math.round(this.cssW * this.dpr);
    const h = Math.round(this.cssH * this.dpr);
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w;
      this.canvas.height = h;
    }
  }

  /**
   * Set the surface image. `source` is a canvas (already painted); it is uploaded now and again on
   * {@link refreshTexture}. `kind` says how longitude/latitude map into it. `poleNorth` / `poleSouth` are the
   * 0..1 RGB the Mercator polar caps are painted with. `filter` are the stages of the dark-theme colour map (empty for none).
   */
  setSurface(source: HTMLCanvasElement | null, kind: GlobeTextureKind, poleNorth: [number, number, number], poleSouth: [number, number, number], filter: ColourAffine[]): void {
    this.source = source;
    this.mode = source ? (kind === 'mercator' ? 1 : 0) : 2;
    this.poleN = poleNorth;
    this.poleS = poleSouth;
    this.filter = filter;
    this.uploadTexture();
  }

  /** Colour of the sphere when there is no image at all. */
  setFlatColour(rgb: [number, number, number]): void {
    this.flat = rgb;
  }

  /** Upload the source canvas again (its pixels changed). */
  refreshTexture(): void {
    this.uploadTexture();
  }

  private uploadTexture(): void {
    if (this._lost || this.disposed) return;
    const gl = this.gl;
    if (!this.source) {
      if (this.tex) gl.deleteTexture(this.tex);
      this.tex = null;
      return;
    }
    if (!this.tex) {
      this.tex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, this.tex);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.REPEAT);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      const aniso = gl.getExtension('EXT_texture_filter_anisotropic');
      if (aniso) {
        const max = gl.getParameter(aniso.MAX_TEXTURE_MAX_ANISOTROPY_EXT) as number;
        gl.texParameterf(gl.TEXTURE_2D, aniso.TEXTURE_MAX_ANISOTROPY_EXT, Math.min(4, max));
      }
    } else {
      gl.bindTexture(gl.TEXTURE_2D, this.tex);
    }
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE, this.source);
    gl.generateMipmap(gl.TEXTURE_2D);
  }

  /** Draw the globe's surface for a frame. Pixels outside the disc stay transparent. */
  render(f: GlobeFrame): void {
    if (this._lost || this.disposed || !this.prog) return;
    const t0 = performance.now();
    const gl = this.gl;
    const W = this.canvas.width;
    const H = this.canvas.height;
    gl.viewport(0, 0, W, H);
    gl.disable(gl.SCISSOR_TEST);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    const k = W / this.cssW;
    const rad = f.radius * k;
    const cxd = f.cx * k;
    const cyd = H - f.cy * (H / this.cssH);
    // Only the disc's bounding box needs shading.
    const x0 = Math.max(0, Math.floor(cxd - rad - 2));
    const y0 = Math.max(0, Math.floor(cyd - rad - 2));
    const x1 = Math.min(W, Math.ceil(cxd + rad + 2));
    const y1 = Math.min(H, Math.ceil(cyd + rad + 2));
    if (x1 <= x0 || y1 <= y0) return;
    gl.enable(gl.SCISSOR_TEST);
    gl.scissor(x0, y0, x1 - x0, y1 - y0);
    gl.useProgram(this.prog);
    const b = viewBasis(f.view);
    gl.uniform2f(this.uni.u_center, cxd, cyd);
    gl.uniform1f(this.uni.u_radius, rad);
    gl.uniform3f(this.uni.u_R, b.r[0], b.r[1], b.r[2]);
    gl.uniform3f(this.uni.u_U, b.u[0], b.u[1], b.u[2]);
    gl.uniform3f(this.uni.u_F, b.f[0], b.f[1], b.f[2]);
    gl.uniform1i(this.uni.u_mode, this.tex ? this.mode : 2);
    gl.uniform3f(this.uni.u_poleN, this.poleN[0], this.poleN[1], this.poleN[2]);
    gl.uniform3f(this.uni.u_poleS, this.poleS[0], this.poleS[1], this.poleS[2]);
    gl.uniform3f(this.uni.u_flat, this.flat[0], this.flat[1], this.flat[2]);
    const nf = Math.min(MAX_FILTER_STAGES, this.filter.length);
    gl.uniform1i(this.uni.u_nf, nf);
    if (nf > 0) {
      // GLSL mat3 is column major: pass the transpose of our row-major matrices.
      const fm = new Float32Array(MAX_FILTER_STAGES * 9);
      const ft = new Float32Array(MAX_FILTER_STAGES * 3);
      for (let i = 0; i < nf; i++) {
        const m = this.filter[i].m;
        fm.set([m[0], m[3], m[6], m[1], m[4], m[7], m[2], m[5], m[8]], i * 9);
        ft.set(this.filter[i].t, i * 3);
      }
      gl.uniformMatrix3fv(this.uni.u_fm, false, fm);
      gl.uniform3fv(this.uni.u_ft, ft);
    }
    if (this.tex) {
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, this.tex);
      gl.uniform1i(this.uni.u_tex, 0);
    }
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    this.lastCpuMs = performance.now() - t0;
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.canvas.removeEventListener('webglcontextlost', this.onLostEv, false);
    this.canvas.removeEventListener('webglcontextrestored', this.onRestoredEv, false);
    const gl = this.gl;
    if (!this._lost) {
      if (this.tex) gl.deleteTexture(this.tex);
      if (this.prog) gl.deleteProgram(this.prog);
      try {
        gl.getExtension('WEBGL_lose_context')?.loseContext();
      } catch {
        /* already lost */
      }
    }
    this.tex = null;
    this.prog = null;
    this.source = null;
  }
}
