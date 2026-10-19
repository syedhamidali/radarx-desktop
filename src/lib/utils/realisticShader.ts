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
  'u_horizonCol', 'u_zenithCol', 'u_sunSteps', 'u_fog', 'u_exposure', 'u_groundGain', 'u_seed', 'u_flashP', 'u_flashN', 'u_flashCol', 'u_flashSky', 'u_noiseOff', 'u_noiseAmt', 'u_glowCol', 'u_bounceCol', 'u_sunGain', 'u_skyGain',
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
    uniform vec4  u_flashP[4];        // lightning: scene position (km) and strength of up to four light points
    uniform int   u_flashN;
    uniform vec3  u_flashCol;
    uniform vec3  u_bounceCol;        // light bounced up from the ground
    uniform float u_sunGain;
    uniform float u_skyGain;
    uniform vec3  u_glowCol;          // warm colour near the horizon in the light's direction only
    uniform float u_flashSky;         // the flash light that reaches the sky and the haze
    uniform vec3  u_noiseOff;         // km: the storm's translation since the start, so the cloud texture rides with the storm
    uniform float u_noiseAmt;         // 0 = smooth, 1 = billowy cloud texture (stylised)
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

    // light thrown by lightning at a position: points along the channel, about 6 km reach
    vec3 flashLight(vec3 pos) {
      vec3 acc = vec3(0.0);
      for (int i = 0; i < 4; i++) {
        if (i >= u_flashN) break;
        vec3 d = pos - u_flashP[i].xyz;
        d.z /= max(u_vexag, 1e-3);
        acc += u_flashCol * u_flashP[i].w / (1.0 + dot(d, d) / 36.0);
      }
      return acc;
    }

    // haze: a bluish neutral, warm only toward the light
    vec3 hazeColor(vec3 d) {
      float glow = pow(max(dot(normalize(d.xy + 1e-5), normalize(u_sunDir.xy + 1e-5)), 0.0), 4.0);
      return u_horizonCol + u_glowCol * (0.42 * glow) + u_flashCol * u_flashSky * 0.3;
    }

    float vnoise(vec2 p) {
      vec2 i = floor(p);
      vec2 f = fract(p);
      vec2 u = f * f * (3.0 - 2.0 * f);
      float a = fract(sin(dot(i, vec2(12.9898, 78.233))) * 43758.5453);
      float b = fract(sin(dot(i + vec2(1.0, 0.0), vec2(12.9898, 78.233))) * 43758.5453);
      float c = fract(sin(dot(i + vec2(0.0, 1.0), vec2(12.9898, 78.233))) * 43758.5453);
      float e = fract(sin(dot(i + vec2(1.0, 1.0), vec2(12.9898, 78.233))) * 43758.5453);
      return mix(mix(a, b, u.x), mix(c, e, u.x), u.y);
    }

    float vnoise3(vec3 p) {
      vec3 i = floor(p);
      vec3 f = fract(p);
      vec3 u = f * f * (3.0 - 2.0 * f);
      float n000 = fract(sin(dot(i, vec3(12.9898, 78.233, 37.719))) * 43758.5453);
      float n100 = fract(sin(dot(i + vec3(1.0, 0.0, 0.0), vec3(12.9898, 78.233, 37.719))) * 43758.5453);
      float n010 = fract(sin(dot(i + vec3(0.0, 1.0, 0.0), vec3(12.9898, 78.233, 37.719))) * 43758.5453);
      float n110 = fract(sin(dot(i + vec3(1.0, 1.0, 0.0), vec3(12.9898, 78.233, 37.719))) * 43758.5453);
      float n001 = fract(sin(dot(i + vec3(0.0, 0.0, 1.0), vec3(12.9898, 78.233, 37.719))) * 43758.5453);
      float n101 = fract(sin(dot(i + vec3(1.0, 0.0, 1.0), vec3(12.9898, 78.233, 37.719))) * 43758.5453);
      float n011 = fract(sin(dot(i + vec3(0.0, 1.0, 1.0), vec3(12.9898, 78.233, 37.719))) * 43758.5453);
      float n111 = fract(sin(dot(i + vec3(1.0, 1.0, 1.0), vec3(12.9898, 78.233, 37.719))) * 43758.5453);
      return mix(mix(mix(n000, n100, u.x), mix(n010, n110, u.x), u.y), mix(mix(n001, n101, u.x), mix(n011, n111, u.x), u.y), u.z);
    }

    // Fractal (four octave) detail in the storm's own frame (true km, deterministic, riding with the
    // storm). detailShade scales the brightness; erodeOpacity thins the faint parts of the echo into
    // wisps and billows and leaves dense echo whole. Neither adds echo where the radar has none
    // (opacity is only ever reduced, and only where it is low) nor moves the cores.
    float fbm(vec3 q) {
      float n = 0.5 * vnoise3(q);
      n += 0.25 * vnoise3(q * 2.07 + 5.3);
      n += 0.125 * vnoise3(q * 4.19 + 11.7);
      n += 0.0625 * vnoise3(q * 8.3 + 2.9);
      return n / 0.9375;
    }
    vec3 detailCoord(vec3 pos) {
      return vec3(pos.xy - u_noiseOff.xy, pos.z / max(u_vexag, 1e-3));
    }
    float detailShade(vec3 pos) {
      if (u_noiseAmt <= 0.0) return 1.0;
      vec3 q = detailCoord(pos);
      float n = fbm(vec3(q.xy * 1.5, q.z * 0.7));
      return 1.0 + u_noiseAmt * 0.8 * (n - 0.5);
    }
    float erodeOpacity(vec3 pos, float a0) {
      if (u_noiseAmt <= 0.0) return 1.0;
      vec3 q = detailCoord(pos);
      float n = fbm(vec3(q.xy * 0.7, q.z * 0.35) + 17.0);
      float thin = 1.0 - smoothstep(0.12, 0.55, a0);          // 1 where the echo is faint
      return 1.0 - u_noiseAmt * thin * (1.0 - smoothstep(0.3, 0.7, n)) * 0.85;
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

    float hg(float c, float g) {
      float g2 = g * g;
      return (1.0 - g2) / (4.0 * 3.14159265 * pow(1.0 + g2 - 2.0 * g * c, 1.5));
    }

    // Light of a cloud sample (dusk and daylight): sun with soft self-shadow and a multiple-scattering
    // floor (shadows stay blue-grey, not black), Henyey-Greenstein forward and back lobes (silver
    // lining toward the sun, bright body away from it), powder darkening in dense cores, a blue fill
    // from the sky above and a green-grey bounce from the ground below. a0 = opacity of the step.
    vec3 cloudLight(vec3 pos, vec3 d, vec3 alb, float a0, float jit, float dist) {
      float tau = 0.0;
      if (u_sunSteps > 0) tau = sunTau(pos, 26.0, u_sunSteps, 0.25 + 0.5 * jit);
      // multiple scattering keeps a fraction of the light inside dense cloud
      float T = max(exp(-tau * 0.3), 0.6 * exp(-0.07 * tau));
      float cs = dot(d, u_sunDir);
      float phase = 0.8 + 4.0 * mix(hg(cs, 0.62), hg(cs, -0.25), 0.4);
      float hz = clamp(pos.z / max(u_boxMax.z, 1.0), 0.0, 1.0);
      // powder: thin cloud edges are not darkened, dense cores are
      float powder = 1.0 - 0.55 * smoothstep(0.1, 0.7, a0) * (1.0 - exp(-tau * 0.6));
      vec3 sun = u_sunCol * u_sunGain * T * phase * powder;
      vec3 sky = u_skyCol * u_skyGain * (0.34 + 0.66 * hz);
      vec3 bounce = u_bounceCol * (1.0 - hz) * 0.5;
      vec3 fl = u_flashN > 0 ? flashLight(pos) : vec3(0.0);
      vec3 lit = sun + sky + bounce + fl * 1.8;
      // the underside and the base are in the shade of the cloud above: blue-grey
      float base = 0.78 + 0.22 * smoothstep(0.0, 0.5, hz);
      vec3 col = toneMap(alb * lit * u_exposure * base + fl * 0.22);
      // aerial perspective: far cloud takes the colour of the haze
      return mix(col, hazeColor(d), 0.9 * (1.0 - exp(-dist * u_fog)));
    }

    vec3 skyColor(vec3 d) {
      float h = d.z;
      vec3 col = mix(u_horizonCol, u_zenithCol, pow(clamp(h, 0.0, 1.0), 0.45)) + u_flashCol * u_flashSky * (0.35 + 0.65 * clamp(h, 0.0, 1.0));
      float s = max(dot(normalize(vec3(d.xy, 1e-4)), normalize(vec3(u_sunDir.xy, 1e-4))), 0.0);
      col += u_glowCol * 0.5 * pow(s, 3.0) * exp(-6.0 * max(h, 0.0));
      // the sun itself: a soft bright disc glow
      float sd = max(dot(d, u_sunDir), 0.0);
      col += u_sunCol * (0.5 * pow(sd, 90.0) + 0.12 * pow(sd, 12.0));
      if (h < 0.0) col = hazeColor(d);   // the haze the ground fades into
      return col;
    }

    // The terrain surface seen along d from distance t: satellite image, relief shading from the
    // height field, the cloud's shadow, haze towards the horizon colour and a fade at the field's edge.
    vec3 terrainColor(vec3 gp, float t, vec3 d, float jit) {
      vec2 uv = demUv(gp.xy);
      float pixAng = 2.0 * u_tanHalfFovY / u_resolution.y;
      float foot = t * pixAng / max(0.22, abs(d.z));   // grazing views: the filter (anisotropic) does the rest
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
      vec3 lit = u_skyCol * 0.6 + u_sunCol * (0.62 * relief * sh) + (u_flashN > 0 ? flashLight(gp) * 0.9 : vec3(0.0));
      // close to the eye the 55 m imagery would be a blur: add fine, low-contrast texture
      float near = 1.0 - smoothstep(1.0, 12.0, t);
      if (near > 0.0) {
        float dn = 0.6 * vnoise(gp.xy * 40.0) + 0.4 * vnoise(gp.xy * 9.0);
        alb *= 1.0 + 0.5 * near * (dn - 0.5);
      }
      vec3 col = toneMap(alb * u_groundGain * lit * u_exposure);
      float haze = 1.0 - exp(-t * u_fog);
      haze = max(haze, smoothstep(10.0, 130.0, outside));
      return mix(col, hazeColor(d), clamp(haze, 0.0, 1.0));
    }

`;
