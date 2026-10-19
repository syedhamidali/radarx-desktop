/**
 * GLSL of the "Realistic ground" (terrain height field ray-marched in the same pass as the volume, so
 * the cloud stops at the terrain and nothing shows under hills) and of the "Realistic (stylised)"
 * storm look (sun light, soft self-shadow by a short secondary march, silver lining, sky, haze).
 *
 * The pieces belong to the Cartesian volume shader (cartesianRenderer.ts) only; the polar shader
 * text is untouched. They read uniforms declared in GLSL_REALISTIC_UNIFORMS plus u_ground*,
 * u_vexag, u_transferFn, u_opacityScale, u_stepUser, u_boxMax and the helpers sampleAdvected /
 * thresholdFactor of that shader.
 */

export const REALISTIC_UNIFORM_NAMES = [
  'u_dem', 'u_terrOn', 'u_terrZ', 'u_demTexel', 'u_style', 'u_sunDir', 'u_sunCol', 'u_skyCol',
  'u_horizonCol', 'u_zenithCol', 'u_sunSteps', 'u_fog', 'u_exposure', 'u_groundGain', 'u_seed',
] as const;

export const GLSL_REALISTIC_UNIFORMS = `
    uniform highp sampler2D u_dem;    // R16F: terrain height, km ASL, over the ground image's square
    uniform int   u_terrOn;           // 1: the ground is the terrain height field
    uniform vec2  u_terrZ;            // lowest / highest terrain, scene z (km x vertical exaggeration)
    uniform float u_demTexel;         // km per height texel
    uniform int   u_style;            // 0 scientific colour scale, 1 realistic (stylised)
    uniform vec3  u_sunDir;           // unit vector towards the light, scene coordinates
    uniform vec3  u_sunCol;
    uniform vec3  u_skyCol;
    uniform vec3  u_horizonCol;
    uniform vec3  u_zenithCol;
    uniform int   u_sunSteps;         // secondary march toward the light (0 = no shadows)
    uniform float u_fog;              // haze per km
    uniform float u_exposure;
    uniform float u_groundGain;       // brightness of the imagery
    uniform float u_seed;             // changes every frame: the sampling noise does not stick to the screen
`;

export const GLSL_REALISTIC = `
    vec3 toneMap(vec3 c) { return 1.0 - exp(-c * 1.15); }
    vec2 demUv(vec2 xy) {
      vec2 q = xy - u_groundCenter;
      return vec2(q.x / (2.0 * u_groundHalf) + 0.5, 0.5 - q.y / (2.0 * u_groundHalf));
    }
    float terrainZ(vec2 xy) {
      float h = textureLod(u_dem, demUv(xy), 0.0).r * u_vexag;
      // past the height map the edge heights are not extruded outward: they ease to a flat level
      vec2 q = abs(xy - u_groundCenter);
      float outside = max(max(q.x, q.y) - u_groundHalf, 0.0);
      float flatZ = u_terrZ.x + 0.2 * (u_terrZ.y - u_terrZ.x);
      return mix(h, flatZ, smoothstep(0.0, 25.0, outside));
    }

    const float TERR_EXT = 300.0;

    // unstructured noise for the sampling jitter (the interleaved-gradient hash leaves diagonal lines)
    float whiteNoise(vec2 p) { return fract(sin(dot(p + vec2(u_seed * 0.37, u_seed * 0.71), vec2(12.9898, 78.233))) * 43758.5453); }

    // First hit of a ray with the terrain height field (scene units); the field is only searched
    // inside the slab between its lowest and highest points, so flat country costs few steps.
    bool traceTerrain(vec3 o, vec3 d, out float tHit) {
      tHit = 0.0;
      // the ground goes on past the image (clamped) for a while and fades into haze: no floating card
      vec2 lo = u_groundCenter - vec2(u_groundHalf + TERR_EXT);
      vec2 hi = u_groundCenter + vec2(u_groundHalf + TERR_EXT);
      vec2 inv = 1.0 / mix(d.xy, vec2(1e-6), lessThan(abs(d.xy), vec2(1e-6)));
      vec2 ta = (lo - o.xy) * inv;
      vec2 tb = (hi - o.xy) * inv;
      float tn = max(max(min(ta.x, tb.x), min(ta.y, tb.y)), 0.0);
      float tf = min(max(ta.x, tb.x), max(ta.y, tb.y));
      float zlo = u_terrZ.x - 0.02;
      float zhi = u_terrZ.y + 0.02;
      if (abs(d.z) < 1e-6) {
        if (o.z > zhi || o.z < zlo) return false;
      } else {
        float t0 = (zhi - o.z) / d.z;
        float t1 = (zlo - o.z) / d.z;
        tn = max(tn, min(t0, t1));
        tf = min(tf, max(t0, t1));
      }
      if (tn >= tf) return false;
      vec3 p = o + d * tn;
      if (p.z <= terrainZ(p.xy)) { tHit = tn; return true; }
      const int N = 192;
      float dt = (tf - tn) / float(N);
      float tp = tn;
      for (int i = 1; i <= N; i++) {
        float t = tn + dt * float(i);
        p = o + d * t;
        if (p.z <= terrainZ(p.xy)) {
          float a = tp;
          float b = t;
          for (int k = 0; k < 7; k++) {
            float m = 0.5 * (a + b);
            vec3 q = o + d * m;
            if (q.z <= terrainZ(q.xy)) b = m; else a = m;
          }
          tHit = 0.5 * (a + b);
          return true;
        }
        tp = t;
      }
      return false;
    }

    // Optical depth of the cloud from p toward the light (a few jittered steps over len scene km).
    float sunTau(vec3 p, float len, int n, float jit) {
      float tau = 0.0;
      float ds = len / float(n);
      // path length in true km per scene km (heights are stretched by the vertical exaggeration)
      float tl = length(vec3(u_sunDir.xy, u_sunDir.z / max(u_vexag, 1e-3)));
      for (int i = 0; i < 32; i++) {
        if (i >= n) break;
        vec3 q = p + u_sunDir * (ds * (float(i) + jit));
        if (q.z > u_boxMax.z) break;
        float v, c;
        if (!sampleAdvected(q, v, c)) continue;
        float a = texture(u_transferFn, vec2((v * 255.0 + 0.5) / 256.0, 0.5)).a * u_opacityScale
                * smoothstep(0.0, 1.0, c) * thresholdFactor(v);
        tau += -log(max(1.0 - min(a, 0.985), 0.015)) / u_stepUser * ds * tl;
      }
      return tau;
    }

    // Light of a cloud sample: ambient sky (brighter higher up), sun with soft self-shadow, a forward
    // scattering lobe (silver lining on back-lit edges) and a little multiple scattering in cores.
    vec3 cloudLight(vec3 pos, vec3 d, vec3 alb, float jit) {
      float tau = 0.0;
      if (u_sunSteps > 0) tau = sunTau(pos, 26.0, u_sunSteps, 0.25 + 0.5 * jit);
      float T = exp(-tau);
      float fwd = pow(max(dot(d, u_sunDir), 0.0), 4.0);
      float phase = 0.55 + 1.5 * fwd;
      float hz = clamp(pos.z / max(u_boxMax.z, 1.0), 0.0, 1.0);
      vec3 amb = u_skyCol * (0.5 + 0.55 * hz);
      vec3 lit = amb + u_sunCol * (T * phase * 0.85 + 0.11 * pow(T, 0.3));
      return toneMap(alb * lit * u_exposure);
    }

    vec3 hazeColor(vec3 d) {
      float glow = pow(max(dot(normalize(d.xy + 1e-5), normalize(u_sunDir.xy + 1e-5)), 0.0), 3.0);
      return u_horizonCol * (0.82 + 0.32 * glow);
    }

    vec3 skyColor(vec3 d) {
      float h = d.z;
      vec3 col = mix(u_horizonCol, u_zenithCol, pow(clamp(h, 0.0, 1.0), 0.45));
      float s = max(dot(normalize(vec3(d.xy, 1e-4)), normalize(vec3(u_sunDir.xy, 1e-4))), 0.0);
      col += u_sunCol * 0.3 * pow(s, 6.0) * exp(-5.0 * max(h, 0.0));
      if (h < 0.0) col = hazeColor(d);   // the haze the ground fades into
      return col;
    }

    // The terrain surface seen along d from distance t: satellite image, relief shading from the
    // height field, the cloud's shadow, haze towards the horizon colour and a fade at the field's edge.
    vec3 terrainColor(vec3 gp, float t, vec3 d, float jit) {
      vec2 uv = demUv(gp.xy);
      float pixAng = 2.0 * u_tanHalfFovY / u_resolution.y;
      float foot = t * pixAng / max(0.05, abs(d.z));
      float lod = log2(max(1.0, foot / (2.0 * u_groundHalf / u_groundSize)));
      vec2 q = abs(gp.xy - u_groundCenter);
      float outside = max(max(q.x, q.y) - u_groundHalf, 0.0);
      vec3 alb = vec3(0.3, 0.34, 0.28);
      if (u_groundMode == 2) {
        // past the image the edge is replaced by the image's mean colour (no stretched streaks)
        vec3 avg = textureLod(u_ground, vec2(0.5), 12.0).rgb;
        alb = mix(textureLod(u_ground, uv, lod).rgb, avg, smoothstep(30.0, 140.0, outside));
      }
      float e = max(u_demTexel, 0.05);
      float hx = terrainZ(gp.xy + vec2(e, 0.0)) - terrainZ(gp.xy - vec2(e, 0.0));
      float hy = terrainZ(gp.xy + vec2(0.0, e)) - terrainZ(gp.xy - vec2(0.0, e));
      vec3 n = normalize(vec3(-hx / (2.0 * e), -hy / (2.0 * e), 1.0));
      float flatD = max(u_sunDir.z, 0.2);
      float relief = clamp(max(dot(n, u_sunDir), 0.0) / flatD, 0.0, 1.8);
      relief = mix(relief, 1.0, smoothstep(0.0, 10.0, outside));
      float sh = 1.0;
      if (u_sunSteps > 0) sh = exp(-sunTau(gp + n * 0.05, 90.0, 24, jit) * 0.8);
      vec3 lit = u_skyCol * 0.6 + u_sunCol * (0.62 * relief * sh);
      vec3 col = toneMap(alb * u_groundGain * lit * u_exposure);
      float haze = 1.0 - exp(-t * u_fog);
      haze = max(haze, smoothstep(10.0, 130.0, outside));
      return mix(col, hazeColor(d), clamp(haze, 0.0, 1.0));
    }

`;
