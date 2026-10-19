/**
 * GLSL for the tornado renderer (WebGL2). See TornadoScene.ts for the passes.
 *
 * The vortex functions here are the same formulas as vortexField.ts (Lamb-Oseen swirl with height, Burgers
 * core plus surface-layer inflow, cyclostrophic pressure from the tabulated G(x)); the tests check the
 * TypeScript, the on-screen rotation check (tools/tornado_sim/check_rotation.py) checks this copy.
 */

export const GLSL_HEAD = `#version 300 es
precision highp float;
precision highp int;
precision highp sampler3D;
precision highp sampler2D;
`;

/** Vortex field, noise helpers, flash lighting: shared by the passes that need them. */
export const GLSL_COMMON = `
const float PI = 3.14159265359;
const float LO_B = 1.2564;
const float LO_C = 0.71532;
const float RHO = 1.15;

uniform vec4 uT0;   // vt, rm, hf, hv
uniform vec4 uT1;   // hs, gNorm, a, ha
uniform vec4 uT2;   // ra, beta, delta, sense
uniform float uInt; // tornado intensity 0..1
uniform float uG[64];
uniform vec2 uAxis; // vortex axis, world metres
uniform vec2 uTrans; // storm translation m/s
uniform vec4 uM0;   // meso: vt, rm, hv, sense
uniform vec4 uM1;   // meso: hs, gNorm, hf, -
uniform float uTime; // simulation seconds
uniform sampler3D uNoise;

float swirlShape(float x) {
  if (x < 1e-4) return LO_B * x / LO_C;
  return (1.0 - exp(-LO_B * x * x)) / (LO_C * x);
}
float Gshape(float x) {
  const float LMIN = -6.907755;
  const float LMAX = 3.688879;
  if (x <= 1e-3) return uG[0];
  if (x >= 40.0) return 1.0 / (2.0 * LO_C * LO_C * x * x);
  float h = (LMAX - LMIN) / 63.0;
  float u = (log(x) - LMIN) / h;
  int i = min(62, int(floor(u)));
  float t = u - float(i);
  float x0 = exp(LMIN + h * float(i));
  float x1 = exp(LMIN + h * float(i + 1));
  float f0 = swirlShape(x0);
  float f1 = swirlShape(x1);
  float m0 = -f0 * f0 * h;
  float m1 = -f1 * f1 * h;
  float t2 = t * t;
  float t3 = t2 * t;
  return (2.0 * t3 - 3.0 * t2 + 1.0) * uG[i] + (t3 - 2.0 * t2 + t) * m0 + (-2.0 * t3 + 3.0 * t2) * uG[i + 1] + (t3 - t2) * m1;
}
float heightFactor(float z) {
  z = max(z, 0.0);
  float q = z / uT0.w;
  return (1.0 - 0.4 * exp(-z / uT1.x)) * exp(-q * q) * uT1.y;
}
float coreR(float z) {
  float q = max(z, 0.0) / uT0.z;
  return uT0.y * sqrt(1.0 + q * q);
}
float vtanAt(float r, float z) {
  return uT0.x * heightFactor(z) * swirlShape(r / coreR(z));
}
// angular velocity (rad/s, signed)
float omegaAt(float r, float z) {
  float rc = coreR(z);
  float x = r / rc;
  float fr = x < 1e-4 ? LO_B / LO_C : swirlShape(x) / x;
  return uT2.w * uT0.x * heightFactor(z) * fr / rc;
}
float dpAt(float r, float z) {
  float vz = uT0.x * heightFactor(z);
  return RHO * vz * vz * Gshape(r / coreR(z));
}
float surfSwirl(float r) { return uT0.x * swirlShape(r / uT0.y); }
float urAt(float r, float z) {
  z = max(z, 0.0);
  float s = 1.0 / cosh(z / uT1.w);
  float core = -0.5 * uT1.z * r * s * s * exp(-(r * r) / (uT2.x * uT2.x));
  float bl = -uT2.y * surfSwirl(r) * exp(-z / uT2.z);
  return core + bl;
}
float wAt(float r, float z) {
  z = max(z, 0.0);
  float e = exp(-(r * r) / (uT2.x * uT2.x));
  float core = uT1.z * uT1.w * tanh(z / uT1.w) * e * (1.0 - (r * r) / (uT2.x * uT2.x));
  float x = r / uT0.y;
  float conv = (2.0 * LO_B * uT0.x / (LO_C * uT0.y)) * exp(-LO_B * x * x);
  float bl = uT2.y * uT2.z * (1.0 - exp(-z / uT2.z)) * conv;
  return core + bl;
}
// meso-scale (radar couplet) angular velocity at distance r from the axis, height z
float omegaMeso(float r, float z) {
  z = max(z, 0.0);
  float qf = z / uM1.z;
  float rc = uM0.y * sqrt(1.0 + qf * qf);
  float x = r / rc;
  float fr = x < 1e-4 ? LO_B / LO_C : swirlShape(x) / x;
  float q = z / uM0.z;
  float hf = (1.0 - 0.4 * exp(-z / uM1.x)) * exp(-q * q) * uM1.y;
  return uM0.w * uM0.x * hf * fr / rc;
}
// wind (ground frame, m/s) at world position p: vortex + couplet + carried environment
vec3 windWorld(vec3 p) {
  vec2 d = p.xy - uAxis;
  float r = length(d);
  float z = max(p.z, 0.0);
  float carry = 0.5 + 0.5 * min(1.0, z / 300.0);
  vec3 w = vec3(uTrans * carry, 0.0);
  vec2 er = r > 1e-3 ? d / r : vec2(1.0, 0.0);
  vec2 et = vec2(-er.y, er.x);
  float vt = vtanAt(r, z) * uT2.w * uInt;
  float ur = urAt(r, z) * uInt;
  w.xy += er * ur + et * vt;
  w.z += wAt(r, z) * uInt;
  float om = omegaMeso(r, z);
  w.xy += et * om * r;
  return w;
}

// ---- noise ------------------------------------------------------------------------------------------
vec4 noiseAt(vec3 p) { return texture(uNoise, p); }

// position traced back by tau seconds along the vortex flow (relative to the axis, metres)
vec3 traceBack(vec3 p, float tau) {
  float r = length(p.xy);
  float z = max(p.z, 0.0);
  float ang = (omegaAt(r, z) * uInt + omegaMeso(r, z)) * tau;
  float c = cos(-ang);
  float s = sin(-ang);
  vec2 q = vec2(c * p.x - s * p.y, s * p.x + c * p.y);
  float r0 = max(r - urAt(r, z) * uInt * tau, 0.0);
  q *= r > 1e-3 ? r0 / r : 1.0;
  return vec3(q, p.z - wAt(r, z) * uInt * tau);
}
// flow-advected noise: two phases of a re-seeding cycle, blended (a pure function of time and position)
const float CYCLE = 3.0;
vec4 flowNoise(vec3 p, float scale) {
  float ph = fract(uTime / CYCLE);
  float ph2 = fract(uTime / CYCLE + 0.5);
  float w1 = 1.0 - abs(2.0 * ph - 1.0);
  float w2 = 1.0 - w1;
  vec4 a = noiseAt(traceBack(p, ph * CYCLE) * scale);
  vec4 b = noiseAt(traceBack(p, ph2 * CYCLE) * scale + vec3(0.37, 0.61, 0.13));
  return a * w1 + b * w2;
}

// ---- lightning --------------------------------------------------------------------------------------
uniform vec4 uFlash[4]; // xyz world metres, w = intensity (0 = off)
uniform float uFlashGain;
vec3 flashLight(vec3 p, float reach) {
  vec3 acc = vec3(0.0);
  for (int i = 0; i < 4; i++) {
    float I = uFlash[i].w;
    if (I <= 0.0) continue;
    vec3 d = uFlash[i].xyz - p;
    float dist2 = dot(d, d);
    float w = I * uFlashGain / (1.0 + dist2 / (reach * reach));
    acc += w * vec3(0.78, 0.86, 1.0);
  }
  return acc;
}
float hash12(vec2 p) {
  vec3 p3 = fract(vec3(p.xyx) * 0.1031);
  p3 += dot(p3, p3.yzx + 33.33);
  return fract((p3.x + p3.y) * p3.z);
}
`;

export const FULLSCREEN_VS = `${GLSL_HEAD}
out vec2 vUv;
void main() {
  vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  vUv = p;
  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}`;

/** The volume pass: funnel, cloud, rain, dust, haze, marched against the opaque scene. */
export const VOLUME_FS = `${GLSL_HEAD}
${GLSL_COMMON}
in vec2 vUv;
layout(location = 0) out vec4 outColor;

uniform sampler2D uScene;   // opaque colour (HDR)
uniform sampler2D uDepth;   // opaque distance along the ray (m)
uniform sampler2D uEcho;    // rain rate field (mm/h) R, grid in world metres
uniform vec4 uEchoGrid;     // x0, y0, 1/(nx dx), 1/(ny dy)
uniform float uEchoOn;
uniform vec3 uCam;
uniform vec3 uRight;
uniform vec3 uUp;
uniform vec3 uFwd;
uniform vec2 uTan;          // tan(half fov) x, y
uniform vec2 uRes;
uniform float uFrame;
uniform vec4 uCloud;        // base height (AGL), wall lowering, wall radius, ragged amplitude
uniform vec4 uFunnel;       // dpCrit (Pa), dp scale, eye fraction, funnel strength
uniform vec4 uFunnel2;      // hydrostatic deficit per metre (Pa/m), key light strength, -, -
uniform vec4 uAmb;          // ambient rgb, glow strength
uniform vec3 uGlowDir;      // horizon glow direction (unit, horizontal)
uniform vec3 uFog;          // distance haze colour
uniform vec4 uFogK;         // density at ground (1/m), height scale, rain extinction scale, fallback rain mm/h
uniform vec4 uDust;         // damage onset speed, dust strength, debris dark strength, scale
uniform float uDebug;       // 1 = top-down swirl-noise slice
uniform float uMask;        // bit mask of the components drawn (1 haze, 2 cloud, 4 funnel, 8 rain, 16 dust, 32 debris): diagnostics

float cloudBaseAt(vec2 pw, out float lowerAmt) {
  vec2 rel = pw - uAxis;
  vec4 n = noiseAt(vec3((pw - uTrans * uTime * 0.7) / 2600.0, 0.31));
  vec4 n2 = noiseAt(vec3((pw - uTrans * uTime * 0.7) / 640.0, 0.77));
  lowerAmt = uCloud.y * exp(-dot(rel, rel) / (uCloud.z * uCloud.z)) * uInt;
  float rag = (n.r - 0.5) * 2.0 * uCloud.w + (n2.g - 0.5) * uCloud.w * 0.6;
  return uCloud.x + rag - lowerAmt;
}

float rainRateAt(vec2 pw) {
  if (uEchoOn < 0.5) return uFogK.w;
  vec2 uv = (pw - uEchoGrid.xy) * uEchoGrid.zw;
  if (uv.x < 0.0 || uv.y < 0.0 || uv.x > 1.0 || uv.y > 1.0) {
    vec2 c = clamp(uv, 0.002, 0.998);
    return texture(uEcho, c).r * 96.0;
  }
  return texture(uEcho, uv).r * 120.0;
}

struct Med {
  float sCloud;
  float sFunnel;
  float sRain;
  float sDust;
  float sDebris;
  float sHaze;
};

void sampleMedium(vec3 p, out Med m, out float occl) {
  vec3 rel = vec3(p.xy - uAxis, p.z);
  float r = length(rel.xy);
  float z = max(p.z, 0.0);
  m = Med(0.0, 0.0, 0.0, 0.0, 0.0, 0.0);
  occl = 1.0;

  // ---- ambient haze (humid air, rain-cooled)
  m.sHaze = uFogK.x * exp(-z / uFogK.y);

  // ---- cloud base and the cloud above it
  float lowerAmt;
  float zb = cloudBaseAt(p.xy, lowerAmt);
  float above = p.z - zb;
  // the sky seen from here: open above the cloud top, mostly closed under the base (overcast)
  occl = mix(1.0, 0.55, smoothstep(zb + 60.0, zb - 120.0, p.z));
  if (above > -260.0) {
    vec4 cn = noiseAt(vec3((p.xy - uTrans * uTime * 0.7) / 900.0, p.z / 700.0));
    vec4 cn2 = noiseAt(vec3((p.xy - uTrans * uTime * 0.7) / 230.0, p.z / 190.0 + 0.5));
    float ragged = (cn.r * 0.65 + cn2.b * 0.35);
    float edge = smoothstep(-40.0, 120.0 + 160.0 * cn.g, above + 100.0 * (cn2.r - 0.5));
    float body = 0.045 * edge * (0.55 + 0.7 * ragged);
    // scud: fragments below the base, drawn into the vortex by the inflow
    vec3 relp = vec3(p.xy - uAxis, p.z);
    vec4 fn = flowNoise(relp, 1.0 / 520.0);
    float scud = smoothstep(0.58, 0.82, fn.r * 0.6 + fn.b * 0.4) * smoothstep(-260.0, -20.0, above) * smoothstep(8.0, 60.0, p.z) * 0.012;
    scud *= exp(-r / 1300.0) * uInt;
    m.sCloud = body + scud;
    occl *= exp(-clamp(above, 0.0, 400.0) * 0.004);
  }

  // ---- the condensation funnel: where the vortex's own pressure deficit saturates the surface air
  if (uInt > 0.0 && r < 5.0 * coreR(z) + 80.0 && z < 4000.0) {
    float dp = dpAt(r, z) * uInt;
    vec4 fn1 = flowNoise(rel, 1.0 / 130.0);
    vec4 fn2 = flowNoise(rel * vec3(1.0, 1.0, 0.45), 1.0 / 46.0);
    vec4 fn3 = flowNoise(rel * vec3(1.0, 1.0, 0.3), 1.0 / 17.0);
    float tur = (fn1.r - 0.5) * 0.7 + (fn1.g - 0.5) * 0.35 + (fn2.b - 0.5) * 0.45 + (fn3.g - 0.5) * 0.4 + (fn3.b - 0.5) * 0.25;
    float dpe = dp * (1.0 + 0.8 * tur);
    // total deficit: the ambient pressure drop with height plus the vortex's cyclostrophic deficit
    float ex = (dpe + uFunnel2.x * z - uFunnel.x) / uFunnel.y;
    float body = smoothstep(0.0, 1.0, ex + 1.1 * (fn3.g - 0.5) + 0.7 * (fn2.g - 0.5));
    // two-cell core: a clear eye on the axis (modelled)
    float eye = smoothstep(uFunnel.z * 0.6, uFunnel.z * 1.1, r / coreR(z));
    float band = fn1.b * 0.6 + fn2.g * 0.5 + (fn3.r - 0.5) * 0.4;
    float streak = 0.22 + 1.6 * smoothstep(0.32, 0.72, band);
    float top = 1.0 - smoothstep(zb - 20.0, zb + 80.0, p.z);
    m.sFunnel = uFunnel.w * body * (0.03 + 0.97 * eye) * streak;
  }

  // ---- debris and dust skirt where the surface wind exceeds the damage onset
  if (uInt > 0.0 && r < 6.0 * uT0.y + 60.0 && z < 450.0) {
    float vs = uT0.x * swirlShape(r / uT0.y) * 0.8 * uInt;
    float amt = smoothstep(uDust.x * 0.7, uDust.x * 1.7, vs);
    // the updraft carries the dust up the axis even where the swirl is weak
    amt = max(amt, 0.75 * smoothstep(6.0, 28.0, wAt(r, 20.0) * uInt) * (1.0 - smoothstep(1.0 * uT0.y, 2.2 * uT0.y, r)));
    // skirt height grows toward the core and with the swirl, lofted by the updraft
    float hs = 10.0 + 34.0 * smoothstep(0.0, 1.0, vs / uT0.x) + 0.5 * max(wAt(r, 30.0), 0.0);
    vec4 dn = flowNoise(rel, 1.0 / 150.0);
    vec4 dn2 = flowNoise(rel * vec3(1.0, 1.0, 0.7), 1.0 / 47.0);
    float prof = exp(-z / hs);
    float dens = amt * prof * (0.35 + 1.1 * dn.r) * (0.6 + 0.8 * dn2.g);
    float edgeR = 1.0 - smoothstep(3.2 * uT0.y, 6.0 * uT0.y, r);
    m.sDust = uDust.y * dens * edgeR;
    // dark debris wrapped around the base: chunky, high contrast
    float ringR = smoothstep(0.35 * uT0.y, 1.0 * uT0.y, r) * (1.0 - smoothstep(1.6 * uT0.y, 3.4 * uT0.y, r));
    float dh = 30.0 + 40.0 * smoothstep(0.0, 1.0, vs / uT0.x);
    float dd = smoothstep(0.52, 0.78, dn.g * 0.6 + dn2.r * 0.5) * exp(-z / dh) * amt;
    m.sDebris = uDust.z * dd * ringR;
  }

  // ---- rain: curtains from the radar echo, streaked vertically, slanted by the wind
  {
    float R = rainRateAt(p.xy) ;
    if (R > 0.3 && p.z < zb + 500.0) {
      vec2 slant = windWorld(vec3(p.xy, 40.0)).xy * 0.35;
      vec2 q = p.xy - slant * p.z / 8.0 - uTrans * uTime * 0.7;
      vec4 rn = noiseAt(vec3(q / 420.0, p.z / 2600.0 - uTime * 0.012));
      vec4 rn2 = noiseAt(vec3(q / 130.0, p.z / 1700.0 - uTime * 0.02 + 0.3));
      float curt = smoothstep(0.30, 0.78, rn.r * 0.65 + rn2.g * 0.45);
      float k = pow(R, 0.8) * uFogK.z;
      m.sRain = k * (0.25 + 1.25 * curt);
      // the rain-cooled air under the base is the darkest part of the scene
      m.sRain *= smoothstep(zb + 500.0, zb - 80.0, p.z);
    }
  }
}

// cheap density for the shadow taps toward the key light (funnel and dust, one noise tap each)
float lightDens(vec3 p) {
  vec3 rel = vec3(p.xy - uAxis, p.z);
  float r = length(rel.xy);
  float z = max(p.z, 0.0);
  float s = 0.0;
  if (uInt > 0.0 && r < 5.0 * coreR(z) + 80.0 && z < 1500.0) {
    float dp = dpAt(r, z) * uInt;
    vec4 n = flowNoise(rel, 1.0 / 60.0);
    float dpe = dp * (1.0 + 0.8 * (n.r - 0.5));
    float ex = (dpe + uFunnel2.x * z - uFunnel.x) / uFunnel.y;
    s += uFunnel.w * smoothstep(0.0, 1.0, ex) * 0.8;
    if (z < 400.) {
      float vs = uT0.x * swirlShape(r / uT0.y) * 0.8 * uInt;
      float amt = smoothstep(uDust.x * 0.7, uDust.x * 1.7, vs);
      float hs = 10.0 + 34.0 * smoothstep(0.0, 1.0, vs / uT0.x);
      s += uDust.y * amt * exp(-z / hs) * (0.35 + 1.1 * n.g);
    }
  }
  return s;
}

// Henyey-Greenstein
float hg(float c, float g) {
  float g2 = g * g;
  return (1.0 - g2) / (4.0 * PI * pow(1.0 + g2 - 2.0 * g * c, 1.5));
}

void main() {
  vec2 ndc = vUv * 2.0 - 1.0;
  vec3 rd = normalize(uFwd + uRight * (ndc.x * uTan.x) + uUp * (ndc.y * uTan.y));
  vec3 ro = uCam;
  vec3 scene = texture(uScene, vUv).rgb;
  float sceneT = texture(uDepth, vUv).r;

  if (uDebug > 0.5) {
    // diagnostic: top-down slice of the swirl-advected noise at z = uDebug height (orthographic): used to measure the on-screen rotation
    vec2 p2 = (ndc * uTan) * 600.0;
    vec3 rel = vec3(p2, 60.0);
    // uDebug 1: the noise as the volume uses it (two re-seeding phases blended); 2: one phase only (rigid transport, exact)
    vec4 fn = uDebug > 1.5 ? noiseAt(traceBack(rel, fract(uTime / CYCLE) * CYCLE) / 130.0) : flowNoise(rel, 1.0 / 130.0);
    outColor = vec4(vec3(fn.r), 1.0);
    return;
  }

  float tMax = min(sceneT, 18000.0);
  float jitter = hash12(gl_FragCoord.xy + vec2(fract(uFrame * 0.618034) * 173.0, fract(uFrame * 0.414214) * 97.0));

  vec3 col = vec3(0.0);
  float T = 1.0;
  float t = 0.0;
  float cosGlow = dot(rd, uGlowDir);
  vec3 up = vec3(0.0, 0.0, 1.0);

  float t0 = 0.0;
  for (int i = 0; i < 220; i++) {
    if (t >= tMax || T < 0.012) break;
    vec3 p = ro + rd * t;
    float dAxis = length(p.xy - uAxis);
    float dt = clamp(0.014 * t, 2.5, 110.0);
    // refine near the tornado and below the cloud base where the detail is
    if (dAxis < 800.0 && p.z < 900.0) dt = min(dt, 3.0 + 0.05 * dAxis);
    else if (p.z < 600.0) dt = min(dt, 24.0);
    float ts = t + dt * (0.15 + 0.85 * jitter);
    if (ts > tMax) { dt = tMax - t; ts = t + 0.5 * dt; }
    vec3 ps = ro + rd * ts;

    Med m;
    float occl;
    sampleMedium(ps, m, occl);
    int mk = int(uMask + 0.5);
    if ((mk & 1) == 0) m.sHaze = 0.0;
    if ((mk & 2) == 0) m.sCloud = 0.0;
    if ((mk & 4) == 0) m.sFunnel = 0.0;
    if ((mk & 8) == 0) m.sRain = 0.0;
    if ((mk & 16) == 0) m.sDust = 0.0;
    if ((mk & 32) == 0) m.sDebris = 0.0;
    float sFun = m.sFunnel;
    float sum = m.sCloud + sFun + m.sRain + m.sDust + m.sDebris + m.sHaze;
    if (sum > 1e-7) {
      float z = max(ps.z, 0.0);
      // albedo-weighted colour of the mixture
      vec3 cCloud = vec3(0.42, 0.44, 0.49);
      vec3 cFun = vec3(0.78, 0.80, 0.84);
      vec3 cRain = vec3(0.50, 0.54, 0.60);
      vec3 cDust = vec3(0.34, 0.27, 0.20);
      vec3 cDeb = vec3(0.05, 0.045, 0.04);
      vec3 cHaze = vec3(0.70, 0.74, 0.80);
      float sO = sum - m.sHaze;
      vec3 alb = (m.sCloud * cCloud + sFun * cFun + m.sRain * cRain + m.sDust * cDust + m.sDebris * cDeb) / max(sO, 1e-9);
      // self shadowing of the ambient light: by the local density (the thick parts are dark inside) and the cloud above
      float selfOcc = exp(-min(sO * 55.0, 5.0) * 0.55);
      float sky = occl * (0.35 + 0.65 * selfOcc);
      // dim overcast light plus a glow low in the direction of the (stylised) sunset
      vec3 amb = uAmb.rgb * (0.10 + 0.90 * sky);
      // key light: the low glow in the direction of the (stylised) sunset, shadowed by the funnel and the dust
      float shadow = 1.0;
      if (sO > 2e-4 && dAxis < 650.0 && z < 500.0) {
        vec3 Lg = normalize(vec3(uGlowDir.xy, 0.10));
        float tau = lightDens(ps + Lg * 7.0) * 7.0 + lightDens(ps + Lg * 20.0) * 14.0 + lightDens(ps + Lg * 52.0) * 33.0 + lightDens(ps + Lg * 120.0) * 70.0;
        shadow = exp(-tau * 0.9);
      }
      float glowVis = exp(-z / 800.0);
      amb += uAmb.rgb * uAmb.w * (0.2 + 0.8 * hg(cosGlow, 0.45) * 4.0) * 1.3 * glowVis * shadow * vec3(1.0, 0.82, 0.62);
      // lightning: lights the cloud from within and around
      vec3 fl = flashLight(ps, 3500.0) * (0.45 + 0.55 * (1.0 - selfOcc * 0.6));
      vec3 hazeL = uFog * (0.55 + 0.45 * hg(cosGlow, 0.25) * 4.0) + flashLight(ps, 6000.0) * 0.35;
      vec3 Ls = (m.sHaze * hazeL + sO * alb * (amb + fl)) / sum;
      float stepT = exp(-sum * dt);
      vec3 S = Ls * (1.0 - stepT);
      col += T * S;
      T *= stepT;
    }
    t += dt;
  }
  // beyond the march: distance haze colour (far cloud deck / horizon)
  float haze = T;
  vec3 farc = uFog * (0.55 + 0.45 * hg(cosGlow, 0.25) * 4.0) + flashLight(ro + rd * 6000.0, 9000.0) * 0.5;
  if (sceneT >= 17999.0) {
    col += T * farc;
    T = 0.0;
  }
  vec3 outc = col + T * scene;
  outColor = vec4(outc, 1.0);
}
`;

/** Ground: a displaced disc around the camera, procedural fields or satellite imagery. */
export const GROUND_VS = `${GLSL_HEAD}
${GLSL_COMMON}
layout(location = 0) in vec2 aPolar; // radius-fraction (0..1), angle
uniform vec3 uCam;
uniform mat4 uViewProj;
uniform vec2 uGroundCentre;
uniform float uMaxR;
uniform sampler2D uTerrain;
uniform vec4 uTerrainGrid; // x0, y0, 1/size, terrain on
uniform vec2 uTerrainSize;
uniform float uTerrainRef;
out vec3 vWorld;
out float vDist;

float terrainH(vec2 p) {
  if (uTerrainGrid.w < 0.5) return 0.0;
  vec2 uv = (p - uTerrainGrid.xy) * uTerrainGrid.z;
  vec2 f = clamp(uv, 0.0, 0.9999) * uTerrainSize - 0.5;
  ivec2 i0 = ivec2(floor(f));
  vec2 w = f - floor(f);
  ivec2 hi = ivec2(uTerrainSize) - 1;
  float a = texelFetch(uTerrain, clamp(i0, ivec2(0), hi), 0).r;
  float b = texelFetch(uTerrain, clamp(i0 + ivec2(1, 0), ivec2(0), hi), 0).r;
  float c = texelFetch(uTerrain, clamp(i0 + ivec2(0, 1), ivec2(0), hi), 0).r;
  float d = texelFetch(uTerrain, clamp(i0 + ivec2(1, 1), ivec2(0), hi), 0).r;
  return mix(mix(a, b, w.x), mix(c, d, w.x), w.y) - uTerrainRef;
}

void main() {
  float r = 1.5 * pow(uMaxR / 1.5, aPolar.x);
  vec2 xy = uCam.xy + r * vec2(cos(aPolar.y), sin(aPolar.y));
  float h = terrainH(xy);
  vWorld = vec3(xy, h);
  vDist = length(vWorld - uCam);
  gl_Position = uViewProj * vec4(vWorld, 1.0);
}`;

export const GROUND_FS = `${GLSL_HEAD}
${GLSL_COMMON}
in vec3 vWorld;
in float vDist;
layout(location = 0) out vec4 outColor;
layout(location = 1) out vec4 outDist;

uniform vec3 uCam;
uniform sampler2D uLand;      // R class, G variation, B road, A tree density (nearest)
uniform sampler2D uLandL;     // the same, linear (road mask, tree density)
uniform vec4 uLandGrid;       // x0, y0, 1/(2 half), land on
uniform sampler2D uImagery;   // satellite imagery
uniform vec4 uImageryGrid;    // x0, y0, 1/(2 half), imagery on
uniform vec4 uAmb;
uniform vec3 uGlowDir;
uniform float uWet;           // 0 dry .. 1 soaked
uniform float uRainRate;
uniform vec4 uTerrainGrid;
uniform sampler2D uTerrain;
uniform vec2 uTerrainSize;
uniform float uTerrainRef;

float terrainH2(vec2 p) {
  if (uTerrainGrid.w < 0.5) return 0.0;
  vec2 uv = (p - uTerrainGrid.xy) * uTerrainGrid.z;
  vec2 f = clamp(uv, 0.0, 0.9999) * uTerrainSize - 0.5;
  ivec2 i0 = ivec2(floor(f));
  vec2 w = f - floor(f);
  ivec2 hi = ivec2(uTerrainSize) - 1;
  float a = texelFetch(uTerrain, clamp(i0, ivec2(0), hi), 0).r;
  float b = texelFetch(uTerrain, clamp(i0 + ivec2(1, 0), ivec2(0), hi), 0).r;
  float c = texelFetch(uTerrain, clamp(i0 + ivec2(0, 1), ivec2(0), hi), 0).r;
  float d = texelFetch(uTerrain, clamp(i0 + ivec2(1, 1), ivec2(0), hi), 0).r;
  return mix(mix(a, b, w.x), mix(c, d, w.x), w.y) - uTerrainRef;
}

vec3 srgb2lin(vec3 c) { return pow(c, vec3(2.2)); }

void main() {
  vec2 xy = vWorld.xy;
  vec4 n1 = noiseAt(vec3(xy / 3.1, 0.13));
  vec4 n2 = noiseAt(vec3(xy / 0.9, 0.47));
  vec4 n3 = noiseAt(vec3(xy / 38.0, 0.71));
  vec4 n4 = noiseAt(vec3(xy / 260.0, 0.29));
  // distance fade of the fine detail
  float fine = 1.0 - smoothstep(30.0, 260.0, vDist);

  vec3 albedo;
  float rough = 0.85;
  float treeD = 0.0;
  float road = 0.0;
  float cls = 0.0;
  if (uImageryGrid.w > 0.5) {
    vec2 uv = (xy - uImageryGrid.xy) * uImageryGrid.z;
    vec3 im = texture(uImagery, clamp(uv, 0.001, 0.999)).rgb;
    albedo = srgb2lin(im) * 0.9;
    // sub-pixel detail the 10 m imagery cannot show
    float lum = dot(albedo, vec3(0.33));
    albedo *= 0.78 + 0.5 * (n2.r * 0.5 + n3.g * 0.5);
    albedo = mix(albedo, albedo * (0.85 + 0.3 * n1.g), fine);
  } else {
    vec2 uv = (xy - uLandGrid.xy) * uLandGrid.z;
    vec2 uvj = uv + (vec2(n2.g, n2.b) - 0.5) * 7.0 * uLandGrid.z;
    vec4 L = texture(uLand, clamp(uvj, 0.001, 0.999));
    vec4 LL = texture(uLandL, clamp(uv, 0.001, 0.999));
    cls = floor(L.r * 255.0 + 0.5);
    float vr = L.g;
    road = smoothstep(0.35, 0.65, LL.b);
    treeD = LL.a;
    // base colours (dusk, linear): pasture, hay, ploughed, crop, woodland, wet, yard
    vec3 pasture = mix(vec3(0.075, 0.105, 0.04), vec3(0.11, 0.13, 0.05), vr);
    vec3 hay = mix(vec3(0.20, 0.17, 0.07), vec3(0.24, 0.20, 0.08), vr);
    vec3 plough = mix(vec3(0.115, 0.075, 0.05), vec3(0.15, 0.10, 0.07), vr);
    vec3 crop = mix(vec3(0.06, 0.10, 0.04), vec3(0.09, 0.125, 0.05), vr);
    vec3 wood = vec3(0.025, 0.045, 0.02);
    vec3 wet = vec3(0.04, 0.055, 0.05);
    vec3 yard = vec3(0.09, 0.135, 0.05);
    albedo = cls < 0.5 ? pasture : cls < 1.5 ? hay : cls < 2.5 ? plough : cls < 3.5 ? crop : cls < 4.5 ? wood : cls < 5.5 ? wet : yard;
    // texture: mowing / grass tufts / clods at several scales
    float grass = 0.72 + 0.55 * n2.r + 0.35 * (n3.g - 0.5);
    float patchy = 0.8 + 0.4 * n4.b;
    albedo *= mix(1.0, grass, fine) * patchy;
    // rows of the crop and the furrows of the ploughed field (angle from the field's variation)
    float ang = vr * 3.14159;
    vec2 rdir = vec2(cos(ang), sin(ang));
    float rowc = dot(xy, vec2(-rdir.y, rdir.x)) / 0.9;
    float rows = 0.5 + 0.5 * sin(rowc * 6.2831);
    if (cls > 1.5 && cls < 3.5) albedo *= mix(1.0, 0.62 + 0.55 * rows, fine * (cls < 2.5 ? 0.9 : 0.55));
    // woodland canopy seen from far: clumpy dark texture
    if (cls > 3.5 && cls < 4.5) albedo *= 0.6 + 0.9 * n3.b * n2.g;
    // roads: asphalt or gravel, with wear, plus the paler shoulder
    vec3 asphalt = vec3(0.045, 0.045, 0.05) * (0.8 + 0.4 * n2.g);
    vec3 gravel = vec3(0.16, 0.14, 0.11) * (0.75 + 0.5 * n2.r);
    vec3 roadc = vr > 0.35 ? asphalt : gravel;
    albedo = mix(albedo, roadc, road);
    if (road > 0.02 && road < 0.98) albedo = mix(albedo, vec3(0.13, 0.11, 0.08), 0.35);
  }

  // wet ground: darker, glossy; puddles in the low spots
  float puddle = smoothstep(0.70, 0.82, n3.r * 0.6 + n1.g * 0.4) * uWet;
  float wet = clamp(uWet * (0.25 + 0.3 * n1.r) + puddle, 0.0, 1.0);
  albedo *= 2.6 * mix(1.0, 0.6, uWet * 0.7);

  vec3 N = vec3(0.0, 0.0, 1.0);
  // terrain normal
  if (uTerrainGrid.w > 0.5) {
    float e = max(6.0, vDist * 0.004);
    float hx = terrainH2(xy + vec2(e, 0.0)) - terrainH2(xy - vec2(e, 0.0));
    float hy = terrainH2(xy + vec2(0.0, e)) - terrainH2(xy - vec2(0.0, e));
    N = normalize(vec3(-hx, -hy, 2.0 * e));
  }
  // fine bump from the noise
  N = normalize(N + fine * 0.35 * vec3(n2.r - 0.5, n2.g - 0.5, 0.0) * (1.0 - wet * 0.8));

  vec3 V = normalize(uCam - vWorld);
  float up = N.z;
  vec3 amb = uAmb.rgb * 1.3 * (0.35 + 0.65 * up);
  vec3 fl = flashLight(vWorld + vec3(0.0, 0.0, 2.0), 3500.0) * (0.35 + 0.65 * max(up, 0.0));
  vec3 colr = albedo * (amb + fl);
  // glossy sheen of wet ground: reflects the dim sky and the lightning
  float fres = pow(1.0 - clamp(dot(N, V), 0.0, 1.0), 4.0);
  vec3 sheen = (uAmb.rgb * 0.18 + fl * 1.2) * (0.02 + 0.16 * fres) * wet;
  colr += sheen;
  outColor = vec4(colr, 1.0);
  outDist = vec4(vDist, 0.0, 0.0, 1.0);
}`;

/** Debris sprites: instanced oriented quads with a motion-blur stretch. */
export const DEBRIS_VS = `${GLSL_HEAD}
${GLSL_COMMON}
layout(location = 0) in vec2 aCorner;   // -1..1
layout(location = 1) in vec4 iPosCls;   // x y z, class
layout(location = 2) in vec4 iVelPhase; // vx vy vz, phase
layout(location = 3) in vec4 iAxisSpin; // axis xyz, spin rate
layout(location = 4) in vec2 iSizeTone; // size scale, tone variation
uniform mat4 uViewProj;
uniform vec3 uCam;
uniform vec4 uCls[6]; // a, b, tone, kind
uniform float uShutter;
out vec2 vUv;
out vec3 vWorld;
out float vKind;
out float vTone;
out float vStretch;
out vec3 vN;
out float vCls;

vec3 rodrigues(vec3 v, vec3 k, float a) {
  float c = cos(a), s = sin(a);
  return v * c + cross(k, v) * s + k * dot(k, v) * (1.0 - c);
}

void main() {
  if (iPosCls.w < -0.5) { gl_Position = vec4(2.0, 2.0, 2.0, 1.0); return; }
  int ci = int(iPosCls.w + 0.5);
  vec4 cl = uCls[ci];
  float ang = iVelPhase.w + iAxisSpin.w * uTime;
  vec3 axis = normalize(iAxisSpin.xyz);
  vec3 lx = vec3(aCorner.x * cl.x * iSizeTone.x, aCorner.y * cl.y * iSizeTone.x, 0.0);
  vec3 off = rodrigues(lx, axis, ang);
  vec3 nrm = rodrigues(vec3(0.0, 0.0, 1.0), axis, ang);
  vec3 vel = iVelPhase.xyz;
  float sp = length(vel);
  vec3 vd = sp > 0.1 ? vel / sp : vec3(0.0);
  // motion blur: the trailing corners are pulled back along the velocity by the shutter time
  float trail = step(0.0, -dot(off, vd) + 1e-4);
  vec3 pos = iPosCls.xyz + off - vel * uShutter * trail;
  float size = max(cl.x, cl.y) * iSizeTone.x;
  vStretch = 1.0 / (1.0 + sp * uShutter / max(size, 0.04));
  vWorld = pos;
  vUv = aCorner;
  vKind = cl.w;
  vTone = cl.z * (0.7 + 0.6 * iSizeTone.y);
  vN = nrm;
  vCls = iPosCls.w;
  gl_Position = uViewProj * vec4(pos, 1.0);
}`;

export const DEBRIS_FS = `${GLSL_HEAD}
${GLSL_COMMON}
in vec2 vUv;
in vec3 vWorld;
in float vKind;
in float vTone;
in float vStretch;
in vec3 vN;
in float vCls;
layout(location = 0) out vec4 outColor;
layout(location = 1) out vec4 outDist;
uniform vec3 uCam;
uniform vec4 uAmb;
uniform float uFrame;
void main() {
  float a;
  if (vKind < 0.5) {            // dust grain: soft round
    a = (1.0 - smoothstep(0.2, 1.0, length(vUv))) * 0.55;
  } else if (vKind < 1.5) {     // leaf / insulation: ragged ellipse
    vec4 n = noiseAt(vec3(vWorld * 1.7));
    a = smoothstep(1.0, 0.7, length(vUv * vec2(1.0, 1.25))) * smoothstep(0.25, 0.5, n.r + 0.2);
  } else {                      // boards, sheet, timber: crisp rectangle with a soft edge
    a = smoothstep(1.0, 0.85, max(abs(vUv.x), abs(vUv.y)));
  }
  a *= mix(0.55, 1.0, vStretch);
  if (vKind > 1.5) a = a > 0.45 ? 1.0 : 0.0; // boards and sheets are crisp
  if (a < 0.04 || a < hash12(gl_FragCoord.xy + fract(uFrame * 0.37) * 131.0)) discard;
  vec3 N = normalize(vN);
  float lam = 0.55 + 0.45 * abs(N.z);
  vec3 L = vec3(vTone) * (uAmb.rgb * 0.6 * lam * 1.2 + flashLight(vWorld, 3500.0) * (0.5 + 0.5 * abs(N.y)));
  // metal catches the lightning
  if (vCls > 2.5 && vCls < 3.5) L += flashLight(vWorld, 3500.0) * 0.8;
  outColor = vec4(L, a);
  outDist = vec4(length(vWorld - uCam), 0.0, 0.0, 1.0);
}`;

/** Trees: instanced crowns (alpha-tested leafy noise) on trunks, bending in the wind. */
export const TREE_VS = `${GLSL_HEAD}
${GLSL_COMMON}
layout(location = 0) in vec4 aVert;    // xyz unit-model position, w = part (0 trunk, 1 crown)
layout(location = 1) in vec3 aNormal;
layout(location = 2) in vec4 iBase;    // x y z0 height
layout(location = 3) in vec4 iShape;   // crown radius, variation, species, snap
layout(location = 4) in vec4 iBend;    // dir x, dir y, tilt angle (rad), fall 0..1
uniform mat4 uViewProj;
uniform vec3 uCam;
out vec3 vWorld;
out vec3 vN;
out float vPart;
out float vVar;
out float vSpecies;
out float vHeight;

void main() {
  float H = iBase.w;
  float cr = iShape.x;
  vec3 lp;
  float part = aVert.w;
  if (part < 0.5) {
    // trunk: tapered cylinder
    float t = aVert.z; // 0..1
    lp = vec3(aVert.xy * (0.22 + 0.25 * (1.0 - t)) * max(0.35, cr * 0.18), t * H * 0.7);
  } else if (iShape.z < 0.5) {
    // hardwood crown: a lumpy ellipsoid
    lp = vec3(aVert.xy * cr * (1.0 + 0.18 * sin(aVert.z * 5.0 + iShape.y * 20.0)), H * 0.58 + aVert.z * cr * 0.95);
  } else {
    // pine: tall narrow cone-ellipsoid
    float k = 1.0 - clamp((aVert.z + 1.0) * 0.5, 0.0, 1.0) * 0.85;
    lp = vec3(aVert.xy * cr * 0.8 * k, H * 0.55 + aVert.z * H * 0.42);
  }
  // wind flutter and the sway of the whole tree
  vec3 base = vec3(iBase.xy, iBase.z);
  vec3 w = windWorld(base + vec3(0.0, 0.0, H));
  float sp = length(w.xy);
  float flutter = 0.12 * sp / (sp + 20.0) * cr;
  lp.xy += flutter * vec2(sin(uTime * 5.0 + lp.z * 1.3 + iShape.y * 30.0), cos(uTime * 4.3 + lp.z * 1.1 + iShape.y * 17.0)) * step(0.5, part);
  // bend about the base: tilt angle from the CPU (damage state), plus a smooth lean with the wind
  float lean = min(0.5, (sp * sp) / 2600.0);
  float tilt = max(iBend.z, lean * (1.0 - iBend.w));
  vec2 bd = normalize(length(iBend.xy) > 1e-4 ? iBend.xy : (sp > 0.1 ? w.xy : vec2(1.0, 0.0)));
  if (iBend.z < 0.001) bd = sp > 0.1 ? normalize(w.xy) : vec2(1.0, 0.0);
  float c = cos(tilt), s = sin(tilt);
  vec3 rotated;
  // rotate lp about the horizontal axis perpendicular to bd (in the plane (bd, up))
  float along = dot(lp.xy, bd);
  vec2 perp = lp.xy - along * bd;
  float z2 = lp.z * c - along * s;
  float a2 = lp.z * s + along * c;
  rotated = vec3(perp + a2 * bd, z2);
  vec3 pos = base + rotated;
  pos.z = max(pos.z, iBase.z - 0.3);
  vWorld = pos;
  vN = vec3(aNormal.xy, aNormal.z);
  vPart = part;
  vVar = iShape.y;
  vSpecies = iShape.z;
  vHeight = lp.z;
  gl_Position = uViewProj * vec4(pos, 1.0);
}`;

export const TREE_FS = `${GLSL_HEAD}
${GLSL_COMMON}
in vec3 vWorld;
in vec3 vN;
in float vPart;
in float vVar;
in float vSpecies;
in float vHeight;
layout(location = 0) out vec4 outColor;
layout(location = 1) out vec4 outDist;
uniform vec3 uCam;
uniform vec4 uAmb;
uniform float uWet;
void main() {
  vec3 N = normalize(vN);
  vec3 col;
  if (vPart < 0.5) {
    col = vec3(0.05, 0.04, 0.035);
  } else {
    vec4 n = noiseAt(vWorld * 0.7);
    vec4 n2 = noiseAt(vWorld * 2.1 + n.rgb * 0.4);
    float leaf = n.r * 0.55 + n2.g * 0.5;
    float dist = length(vWorld - uCam);
    float cut = mix(0.44, 0.52, smoothstep(40.0, 400.0, dist));
    if (leaf < cut) discard;
    vec3 hw = mix(vec3(0.030, 0.052, 0.020), vec3(0.050, 0.075, 0.028), vVar);
    vec3 pine = mix(vec3(0.016, 0.03, 0.018), vec3(0.025, 0.042, 0.022), vVar);
    col = (vSpecies < 0.5 ? hw : pine) * (0.7 + 0.8 * n2.b);
  }
  col *= (1.0 - 0.35 * uWet);
  float up = 0.45 + 0.55 * N.z;
  vec3 L = col * (uAmb.rgb * 0.6 * up * 1.3 + flashLight(vWorld, 3500.0) * (0.6 + 0.4 * N.y));
  outColor = vec4(L, 1.0);
  outDist = vec4(length(vWorld - uCam), 0.0, 0.0, 1.0);
}`;

/** Buildings and poles: instanced boxes with a gable roof. */
export const BOX_VS = `${GLSL_HEAD}
${GLSL_COMMON}
layout(location = 0) in vec4 aVert;    // xyz in unit box (x,y -0.5..0.5, z 0..1), w = part (0 wall, 1 roof)
layout(location = 1) in vec3 aNormal;
layout(location = 2) in vec4 iPos;     // x y z0 rotation
layout(location = 3) in vec4 iSize;    // w d h kind
layout(location = 4) in vec4 iTone;    // tone, pad, pad, pad
uniform mat4 uViewProj;
out vec3 vWorld;
out vec3 vN;
out float vPart;
out float vKind;
out float vTone;
void main() {
  vec3 s = iSize.xyz;
  vec3 lp = aVert.xyz;
  float roofRise = iSize.w < 3.5 ? (iSize.w > 1.5 && iSize.w < 2.5 ? 0.12 : 0.32) : 0.0; // gable height as a fraction of the width; none for poles
  float isRoof = aVert.w;
  // gable: raise the ridge (y = 0) of the roof vertices
  float ridge = 1.0 - abs(lp.y) * 2.0;
  vec3 p = vec3(lp.x * s.x, lp.y * s.y, lp.z * s.z);
  if (isRoof > 0.5) p.z += ridge * roofRise * s.y;
  float c = cos(iPos.w), sn = sin(iPos.w);
  vec2 xy = vec2(c * p.x - sn * p.y, sn * p.x + c * p.y);
  vec3 pos = vec3(iPos.xy + xy, iPos.z + p.z);
  vec3 n = aNormal;
  vN = vec3(c * n.x - sn * n.y, sn * n.x + c * n.y, n.z);
  vWorld = pos;
  vPart = isRoof;
  vKind = iSize.w;
  vTone = iTone.x;
  gl_Position = uViewProj * vec4(pos, 1.0);
}`;

export const BOX_FS = `${GLSL_HEAD}
${GLSL_COMMON}
in vec3 vWorld;
in vec3 vN;
in float vPart;
in float vKind;
in float vTone;
layout(location = 0) out vec4 outColor;
layout(location = 1) out vec4 outDist;
uniform vec3 uCam;
uniform vec4 uAmb;
uniform float uWet;
void main() {
  vec3 N = normalize(vN);
  vec3 alb;
  float metal = 0.0;
  if (vKind > 3.5) alb = vec3(0.05, 0.04, 0.035);               // pole
  else if (vKind > 1.5 && vKind < 2.5) { alb = vec3(0.34, 0.35, 0.37) * (0.8 + 0.4 * vTone); metal = vPart; } // chicken house, metal roof
  else if (vPart > 0.5) alb = vec3(0.07, 0.06, 0.06) * (0.7 + 0.8 * vTone);
  else alb = mix(vec3(0.22, 0.2, 0.17), vec3(0.3, 0.3, 0.29), vTone);
  vec4 n = noiseAt(vWorld * 0.5);
  alb *= 0.85 + 0.3 * n.g;
  vec3 fl = flashLight(vWorld, 3500.0);
  vec3 L = alb * (uAmb.rgb * 0.6 * (0.5 + 0.5 * max(N.z, 0.0)) * 1.2 + fl * (0.6 + 0.4 * abs(N.y)));
  L += metal * fl * 0.5 * pow(max(N.z, 0.0), 2.0);
  outColor = vec4(L * (1.0 - 0.15 * uWet), 1.0);
  outDist = vec4(length(vWorld - uCam), 0.0, 0.0, 1.0);
}`;

/** Rain streaks close to the lens: instanced lines in a box around the camera, with motion blur. */
export const RAIN_VS = `${GLSL_HEAD}
${GLSL_COMMON}
layout(location = 0) in vec2 aEnd;      // 0 or 1 (along the streak), side -1..1
layout(location = 1) in vec4 iSeed;
uniform mat4 uViewProj;
uniform vec3 uCam;
uniform vec3 uCamFwd;
uniform float uBox;
uniform float uRainRate;
uniform vec3 uCamVel;
out float vAlpha;
out vec3 vWorld;
void main() {
  vec3 r = iSeed.xyz;
  // positions wrap inside a box that follows the camera; time-dependent fall
  float fall = 9.0;
  vec3 wind = windWorld(vec3(uCam.xy, max(uCam.z, 2.0)));
  vec3 vel = vec3(wind.xy * 0.8, -fall);
  vec3 p0 = fract(r + vec3(0.0, 0.0, 0.0) + (vel * uTime) / uBox) ;
  vec3 pos = uCam + (p0 - 0.5) * 2.0 * uBox;
  // bias toward the view direction so most streaks are in front
  vec3 vdir = normalize(vel);
  float len = 0.045 * length(vel - uCamVel) * 1.0 + 0.2;
  vec3 a = pos - vdir * len * aEnd.x;
  vec3 toCam = normalize(uCam - pos);
  vec3 side = normalize(cross(vdir, toCam));
  vec3 wp = a + side * 0.01 * aEnd.y * (1.0 + 0.3 * length(pos - uCam));
  vWorld = wp;
  vAlpha = iSeed.w * (1.0 - aEnd.x * 0.6) * smoothstep(1.2, 5.0, length(pos - uCam)) * clamp(uRainRate / 40.0, 0.2, 1.0);
  gl_Position = uViewProj * vec4(wp, 1.0);
}`;

export const RAIN_FS = `${GLSL_HEAD}
${GLSL_COMMON}
in float vAlpha;
in vec3 vWorld;
layout(location = 0) out vec4 outColor;
layout(location = 1) out vec4 outDist;
uniform vec3 uCam;
uniform vec4 uAmb;
uniform float uFrame;
void main() {
  if (vAlpha * 0.3 < hash12(gl_FragCoord.xy + fract(uFrame * 0.37) * 131.0)) discard;
  vec3 L = vec3(0.6, 0.65, 0.72) * (uAmb.rgb * 2.2 + flashLight(vWorld, 3500.0) * 0.9);
  outColor = vec4(L, 1.0);
  outDist = vec4(length(vWorld - uCam), 0.0, 0.0, 1.0);
}`;

export const BLUR_FS = `${GLSL_HEAD}
in vec2 vUv;
layout(location = 0) out vec4 outColor;
uniform sampler2D uSrc;
uniform vec2 uDir;      // texel step
uniform float uThresh;  // bright pass threshold (first pass only)
uniform float uFirst;
void main() {
  vec3 acc = vec3(0.0);
  float wsum = 0.0;
  for (int i = -6; i <= 6; i++) {
    float w = exp(-float(i * i) / 18.0);
    vec3 c = texture(uSrc, vUv + uDir * float(i)).rgb;
    if (uFirst > 0.5) c = max(c - vec3(uThresh), 0.0);
    acc += c * w;
    wsum += w;
  }
  outColor = vec4(acc / wsum, 1.0);
}`;

export const POST_FS = `${GLSL_HEAD}
in vec2 vUv;
layout(location = 0) out vec4 outColor;
uniform sampler2D uHdr;
uniform sampler2D uBloom;
uniform float uExposure;
uniform float uBloomK;
uniform float uFrame;
uniform vec2 uRes;
uniform float uGrain;
uniform float uTime;
uniform vec3 uRainK;    // intensity 0..1, screen slope (dx per dy), flash brightness
uniform vec3 uRainCol;
float hash12(vec2 p) {
  vec3 p3 = fract(vec3(p.xyx) * 0.1031);
  p3 += dot(p3, p3.yzx + 33.33);
  return fract((p3.x + p3.y) * p3.z);
}
vec3 aces(vec3 x) {
  const float a = 2.51, b = 0.03, c = 2.43, d = 0.59, e = 0.14;
  return clamp((x * (a * x + b)) / (x * (c * x + d) + e), 0.0, 1.0);
}
float rainLayer(vec2 uv, float aspect, float scale, float speed, float dens, float width, float slope) {
  vec2 p = vec2(uv.x * aspect * scale, uv.y * scale);
  p.x += p.y * slope;
  p.y += uTime * speed;
  vec2 id = floor(p);
  vec2 f = fract(p);
  float h = hash12(id);
  if (h > dens) return 0.0;
  float x0 = 0.15 + 0.7 * hash12(id + 7.3);
  float len = 0.45 + 0.55 * hash12(id + 3.1);
  float line = smoothstep(width, 0.0, abs(f.x - x0));
  return line * smoothstep(len, len * 0.2, f.y) * smoothstep(0.0, 0.08, f.y) * (0.5 + 0.5 * hash12(id + 1.7));
}
void main() {
  vec3 c = texture(uHdr, vUv).rgb + texture(uBloom, vUv).rgb * uBloomK;
  float aspect = uRes.x / uRes.y;
  float rn = rainLayer(vUv, aspect, 14.0, 17.0, 0.10, 0.030, uRainK.y)
           + rainLayer(vUv, aspect, 30.0, 26.0, 0.16, 0.040, uRainK.y) * 0.8
           + rainLayer(vUv, aspect, 64.0, 34.0, 0.20, 0.055, uRainK.y) * 0.6;
  c += rn * uRainK.x * uRainCol * (1.0 + uRainK.z);
  // gentle vignette
  vec2 q = vUv - 0.5;
  float vig = 1.0 - 0.35 * dot(q, q) * 2.2;
  c *= vig * uExposure;
  c += vec3(0.004, 0.0048, 0.0065);   // a lifted toe: the dark parts of a graded storm scene keep some texture
  c = aces(c);
  // film-like: lifted blacks, a little cool shadow tint
  c = mix(c, c * vec3(0.96, 1.0, 1.05), 0.5);
  c = pow(c, vec3(1.0 / 2.2));
  float g = hash12(gl_FragCoord.xy + fract(uFrame * 0.1234) * 311.0) - 0.5;
  c += g * uGrain;
  outColor = vec4(c, 1.0);
}`;
