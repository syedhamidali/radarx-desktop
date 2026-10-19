/**
 * GLSL pieces shared by the 3D volume shaders: the polar radar volume (RadarViewer3D) and the
 * Cartesian volumes (merged mosaic / wind grids, cartesianRenderer.ts).
 *
 * Only code that is identical in both looks lives here: the dithering hash, the "hide below"
 * threshold and the ground (map / grid) shading. A shader that includes a piece declares the
 * uniforms it reads (named in the comment above each piece).
 */

/** Interleaved-gradient hash used to jitter the ray start (banding -> noise). */
export const GLSL_HASH = `
    float hash12(vec2 p) {
      return fract(52.9829189 * fract(0.06711056 * p.x + 0.00583715 * p.y));
    }
`;

/** "Hide below X" with a soft edge. Reads: u_thr, u_thrSoft, u_thrMode. */
export const GLSL_THRESHOLD = `
    // Opacity multiplier of a value: the "hide below" threshold with a soft edge.
    float thresholdFactor(float val) {
      if (u_thr <= 0.0) return 1.0;
      float m = (u_thrMode == 1) ? abs(val * 2.0 - 1.0) : val;
      return smoothstep(u_thr, u_thr + u_thrSoft, m);
    }
`;

export interface GroundOptions {
  /** A statement declaring what `rimDist` needs (e.g. `float R = u_rangeLimit;`). */
  prelude: string;
  /** GLSL float expression: distance of the ground point `gp` (z = 0) from the edge of the scene. */
  rimDist: string;
  /** Draw range rings and the N-S / E-W axes (polar scenes). */
  rings: boolean;
  /** Colours follow `uniform float u_themeLight` (0 = dark scene, 1 = light scene). */
  themed: boolean;
}

/**
 * `vec3 groundColor(vec3 gp, float t, vec3 d)`: shading of the ground at scene position `gp`
 * (z = 0) seen along `d` from distance `t` (map image, or a grid, plus rings / rim).
 * Reads: u_groundMode, u_groundHalf, u_groundSize, u_ground, u_ringStep, u_tanHalfFovY, u_resolution
 * (+ u_themeLight, u_mapGain, u_groundCenter when themed).
 */
export function glslGroundColor(o: GroundOptions): string {
  // The map of a Cartesian scene may be centred anywhere (uniform u_groundCenter); the polar scene's
  // is always centred on the radar (and its shader text is kept exactly as it was).
  const mapTest = o.themed
    ? 'vec2 gq = gp.xy - u_groundCenter;\n      if (u_groundMode == 2 && abs(gq.x) < u_groundHalf && abs(gq.y) < u_groundHalf) {'
    : 'if (u_groundMode == 2 && abs(gp.x) < u_groundHalf && abs(gp.y) < u_groundHalf) {';
  const mapUv = o.themed
    ? 'vec2 tuv = vec2(gq.x / (2.0 * u_groundHalf) + 0.5, 0.5 - gq.y / (2.0 * u_groundHalf));'
    : 'vec2 tuv = vec2(gp.x / (2.0 * u_groundHalf) + 0.5, 0.5 - gp.y / (2.0 * u_groundHalf));';
  const baseCol = o.themed ? 'mix(vec3(0.07, 0.085, 0.12), vec3(0.84, 0.86, 0.91), u_themeLight)' : 'vec3(0.07, 0.085, 0.12)';
  const gridAdd = o.themed ? 'mix(vec3(0.05, 0.06, 0.09), vec3(-0.07, -0.07, -0.05), u_themeLight)' : 'vec3(0.05, 0.06, 0.09)';
  const mapBoost = o.themed ? 'u_mapGain' : '1.5';
  const lineCol = o.themed ? 'mix(vec3(0.72, 0.8, 0.95), vec3(0.18, 0.24, 0.36), u_themeLight)' : 'vec3(0.72, 0.8, 0.95)';
  const rimCol = o.themed ? 'mix(vec3(0.85, 0.9, 1.0), vec3(0.12, 0.17, 0.28), u_themeLight)' : 'vec3(0.85, 0.9, 1.0)';
  const rings = o.rings
    ? `      // range rings
      float rd = abs(mod(g + 0.5 * u_ringStep, u_ringStep) - 0.5 * u_ringStep);
      float ring = 1.0 - smoothstep(0.5 * w, 1.5 * w, rd);
      col = mix(col, ${lineCol}, ring * (u_groundMode == 2 ? 0.42 : 0.35));
      // N-S / E-W axes through the radar
      float ax = 1.0 - smoothstep(0.5 * w, 1.5 * w, min(abs(gp.x), abs(gp.y)));
      col = mix(col, ${lineCol}, ax * 0.12);
`
    : '';
  return `
    // Ground disc shading at scene position gp (z = 0), seen along d from distance t.
    vec3 groundColor(vec3 gp, float t, vec3 d) {
      ${o.prelude}
      float pixAng = 2.0 * u_tanHalfFovY / u_resolution.y;
      float foot = t * pixAng / max(0.05, -d.z);          // km covered by one pixel on the ground
      float g = length(gp.xy);

      vec3 col;
      ${mapTest}
        ${mapUv}
        float lod = log2(max(1.0, foot / (2.0 * u_groundHalf / u_groundSize)));
        col = min(textureLod(u_ground, tuv, lod).rgb * ${mapBoost}, vec3(1.0));
      } else {
        col = ${baseCol};
        // faint grid every half ring step
        float gs = u_ringStep * 0.5;
        vec2 gm = abs(mod(gp.xy + 0.5 * gs, gs) - 0.5 * gs);
        float gl = 1.0 - smoothstep(0.5 * max(foot, 0.3), 1.5 * max(foot, 0.3), min(gm.x, gm.y));
        col += ${gridAdd} * gl;
      }

      float w = max(foot * 0.9, 0.3);
${rings}      // rim
      float rim = 1.0 - smoothstep(0.5 * w, 2.0 * w, ${o.rimDist});
      col = mix(col, ${rimCol}, rim * 0.6);
      return col;
    }
`;
}

/** The polar scene's ground: a disc of radius `u_rangeLimit` with range rings. */
export const GLSL_GROUND_POLAR = glslGroundColor({
  prelude: 'float R = u_rangeLimit;',
  rimDist: 'abs(g - R)',
  rings: true,
  themed: false,
});
