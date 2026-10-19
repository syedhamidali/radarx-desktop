/** Small WebGL2 helpers for the tornado renderer (shader program, buffers, matrices). */

export function compile(gl: WebGL2RenderingContext, type: number, src: string): WebGLShader {
  const sh = gl.createShader(type)!;
  gl.shaderSource(sh, src);
  gl.compileShader(sh);
  if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(sh) ?? '';
    const lines = src.split('\n');
    const m = /ERROR: \d+:(\d+)/.exec(log);
    const ln = m ? Number(m[1]) : 0;
    const ctx = ln ? lines.slice(Math.max(0, ln - 3), ln + 2).join('\n') : '';
    gl.deleteShader(sh);
    throw new Error(`shader compile failed: ${log}\n${ctx}`);
  }
  return sh;
}

export interface Program {
  prog: WebGLProgram;
  u: Record<string, WebGLUniformLocation | null>;
}

export function program(gl: WebGL2RenderingContext, vs: string, fs: string, names: string[]): Program {
  const p = gl.createProgram()!;
  gl.attachShader(p, compile(gl, gl.VERTEX_SHADER, vs));
  gl.attachShader(p, compile(gl, gl.FRAGMENT_SHADER, fs));
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(`program link failed: ${gl.getProgramInfoLog(p)}`);
  const u: Record<string, WebGLUniformLocation | null> = {};
  for (const n of names) u[n] = gl.getUniformLocation(p, n);
  return { prog: p, u };
}

export type Vec3 = [number, number, number];

export const sub3 = (a: Vec3, b: Vec3): Vec3 => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
export const add3 = (a: Vec3, b: Vec3): Vec3 => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
export const mul3 = (a: Vec3, k: number): Vec3 => [a[0] * k, a[1] * k, a[2] * k];
export const dot3 = (a: Vec3, b: Vec3) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
export const cross3 = (a: Vec3, b: Vec3): Vec3 => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
export const len3 = (a: Vec3) => Math.hypot(a[0], a[1], a[2]);
export const norm3 = (a: Vec3): Vec3 => {
  const l = len3(a) || 1;
  return [a[0] / l, a[1] / l, a[2] / l];
};

export interface CamBasis {
  pos: Vec3;
  fwd: Vec3;
  right: Vec3;
  up: Vec3;
}

/** Camera basis from a position, target and roll (rad); world up is +z. */
export function basis(pos: Vec3, target: Vec3, roll = 0): CamBasis {
  const fwd = norm3(sub3(target, pos));
  let right = cross3(fwd, [0, 0, 1]);
  if (len3(right) < 1e-6) right = [1, 0, 0];
  right = norm3(right);
  let up = norm3(cross3(right, fwd));
  if (roll) {
    const c = Math.cos(roll), s = Math.sin(roll);
    const r2 = add3(mul3(right, c), mul3(up, s));
    const u2 = add3(mul3(up, c), mul3(right, -s));
    right = r2;
    up = u2;
  }
  return { pos, fwd, right, up };
}

/** Column-major view-projection matrix from a basis (right-handed, -z forward in clip). */
export function viewProj(b: CamBasis, fovYRad: number, aspect: number, near: number, far: number): Float32Array {
  const f = 1 / Math.tan(fovYRad / 2);
  const m = new Float32Array(16);
  // view = rows (right, up, -fwd) with translation
  const tx = -dot3(b.right, b.pos), ty = -dot3(b.up, b.pos), tz = dot3(b.fwd, b.pos);
  const v = [
    b.right[0], b.up[0], -b.fwd[0], 0,
    b.right[1], b.up[1], -b.fwd[1], 0,
    b.right[2], b.up[2], -b.fwd[2], 0,
    tx, ty, tz, 1,
  ];
  const p00 = f / aspect, p11 = f, p22 = (far + near) / (near - far), p23 = -1, p32 = (2 * far * near) / (near - far);
  // P * V (column-major)
  for (let c = 0; c < 4; c++) {
    const x = v[c * 4], y = v[c * 4 + 1], z = v[c * 4 + 2], w = v[c * 4 + 3];
    m[c * 4] = p00 * x;
    m[c * 4 + 1] = p11 * y;
    m[c * 4 + 2] = p22 * z + p32 * w;
    m[c * 4 + 3] = p23 * z;
  }
  return m;
}

export function makeTex2D(gl: WebGL2RenderingContext, w: number, h: number, internal: number, format: number, type: number, data: ArrayBufferView | null, filter: number, wrap = gl.CLAMP_TO_EDGE): WebGLTexture {
  const t = gl.createTexture()!;
  gl.bindTexture(gl.TEXTURE_2D, t);
  gl.texImage2D(gl.TEXTURE_2D, 0, internal, w, h, 0, format, type, data);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filter);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filter === gl.NEAREST ? gl.NEAREST : gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, wrap);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, wrap);
  return t;
}

export function buffer(gl: WebGL2RenderingContext, data: ArrayBufferView, usage: number = gl.STATIC_DRAW, target: number = gl.ARRAY_BUFFER): WebGLBuffer {
  const b = gl.createBuffer()!;
  gl.bindBuffer(target, b);
  gl.bufferData(target, data, usage);
  return b;
}
