/**
 * Self-test of the GPU time interpolation (morphPass.ts) against the physics it is meant to
 * implement: a synthetic vortex (a blob on a ring) that a solid-body rotation carries from volume A
 * to volume B must, in the interpolated volume at position s, stand at the angle theta0 + s * omega *
 * dt. It builds the textures itself on a private WebGL2 context, runs the real shader, reads the
 * result back and measures where the echo is. Used by tools/storm_clip/verify_morph.py (a browser is
 * needed, so it is not in the Node unit tests; the CPU reference maths is in flowMorph.test.mjs).
 */
import { MorphPass } from './morphPass';

export interface MorphTestResult {
  s: number;
  expectedDeg: number;
  measuredDeg: number;
  errorDeg: number;
  radiusKm: number;
  peak: number;
  /** The same measurement for a plain cross-fade of A and B (no flow): the echo is a half-strength double image. */
  crossfadePeak: number;
}

/** Run the test at the given positions; the vortex turns `omegaRadPerS` for `dtSec` between A and B. */
export function runMorphSelfTest(positions = [0.25, 0.5, 0.75], omegaRadPerS = 2e-3, dtSec = 300): MorphTestResult[] {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 4;
  const gl = canvas.getContext('webgl2');
  if (!gl) throw new Error('no WebGL2');
  if (!gl.getExtension('EXT_color_buffer_float')) throw new Error('EXT_color_buffer_float missing');
  const N = 96, NZ = 3, CELL = 1; // km
  const R = 22, SIG = 4, TH0 = 0.3;
  const ring = (th: number) => (x: number, y: number) => Math.exp(-((x - R * Math.cos(th)) ** 2 + (y - R * Math.sin(th)) ** 2) / (2 * SIG * SIG));
  const pack = (f: (x: number, y: number) => number): Uint8Array => {
    const d = new Uint8Array(N * N * NZ * 2);
    for (let k = 0; k < NZ; k++) {
      for (let j = 0; j < N; j++) {
        for (let i = 0; i < N; i++) {
          const x = (i + 0.5 - N / 2) * CELL, y = (j + 0.5 - N / 2) * CELL;
          const v = f(x, y);
          const o = ((k * N + j) * N + i) * 2;
          if (v > 0.02) { d[o] = Math.round(255 * Math.min(1, v)); d[o + 1] = 255; }
        }
      }
    }
    return d;
  };
  const tex3 = (data: Uint8Array) => {
    const t = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_3D, t);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_R, gl.CLAMP_TO_EDGE);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texImage3D(gl.TEXTURE_3D, 0, gl.RG8, N, N, NZ, 0, gl.RG, gl.UNSIGNED_BYTE, data);
    return t;
  };
  const th1 = TH0 + omegaRadPerS * dtSec;
  const A = tex3(pack(ring(TH0))), B = tex3(pack(ring(th1)));
  // the flow: solid-body rotation, in m/s (the pass reads storm-relative anomalies in m/s)
  const fl = new Float32Array(N * N * NZ * 4);
  for (let k = 0; k < NZ; k++) {
    for (let j = 0; j < N; j++) {
      for (let i = 0; i < N; i++) {
        const x = (i - N / 2 + 0.5) * CELL, y = (j - N / 2 + 0.5) * CELL;
        const o = ((k * N + j) * N + i) * 4;
        fl[o] = -omegaRadPerS * y * 1000;
        fl[o + 1] = omegaRadPerS * x * 1000;
        fl[o + 3] = 1;
      }
    }
  }
  const pass = new MorphPass(gl);
  const flow = pass.uploadFlow(fl, N, N, NZ, [(0.5 - N / 2) * CELL, (0.5 - N / 2) * CELL, 0], [CELL, CELL, 1]);
  const half = (N * CELL) / 2;
  const box = { min: [-half, -half, 0] as [number, number, number], max: [half, half, NZ] as [number, number, number] };
  const fbo = gl.createFramebuffer()!;
  const out: MorphTestResult[] = [];
  const measure = (tex: WebGLTexture) => {
    gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
    gl.framebufferTextureLayer(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, tex, 0, 1);
    const px = new Uint8Array(N * N * 4);
    gl.readPixels(0, 0, N, N, gl.RGBA, gl.UNSIGNED_BYTE, px);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    let best = -1, bx = 0, by = 0;
    for (let j = 0; j < N; j++) {
      for (let i = 0; i < N; i++) {
        const b = px[(j * N + i) * 4 + 1] > 5 ? px[(j * N + i) * 4] / px[(j * N + i) * 4 + 1] : 0; // R / G = the code
        const v = px[(j * N + i) * 4];
        if (v > best) { best = v; bx = (i + 0.5 - N / 2) * CELL; by = (j + 0.5 - N / 2) * CELL; }
        void b;
      }
    }
    return { x: bx, y: by, peak: best / 255 };
  };
  const dummyFlow = null;
  for (const s of positions) {
    const res = pass.run(A, B, [N, N, NZ], box, { s, dt: dtSec, track: [0, 0], flowOffset: [0, 0] }, flow, flow);
    const m = measure(res);
    const cross = pass.run(A, B, [N, N, NZ], box, { s, dt: dtSec, track: [0, 0], flowOffset: [0, 0] }, dummyFlow, dummyFlow);
    const c = measure(cross);
    const expected = TH0 + omegaRadPerS * dtSec * s;
    const measured = Math.atan2(m.y, m.x);
    out.push({
      s, expectedDeg: (expected * 180) / Math.PI, measuredDeg: (measured * 180) / Math.PI,
      errorDeg: ((measured - expected) * 180) / Math.PI, radiusKm: Math.hypot(m.x, m.y), peak: m.peak, crossfadePeak: c.peak,
    });
  }
  pass.deleteFlow(flow);
  pass.dispose();
  return out;
}
