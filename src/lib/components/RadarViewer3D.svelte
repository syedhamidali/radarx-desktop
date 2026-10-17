<script lang="ts">
  import { onMount, onDestroy, tick } from 'svelte';
  import { radarData, selectedVariable, connectionStatus } from '../stores/radarData';
  import { selectedBox } from '../stores/volumeSelect';
  import { wsManager } from '../utils/websocket';
  import { getCachedSweep, getDefaultCmap, getDefaultRange, type SweepDataEntry } from '../utils/ppiRenderer';
  import { COLORMAP_DATA } from '../utils/colormaps';
  import TransferFunctionEditor from './TransferFunctionEditor.svelte';

  // ── Reactive state ────────────────────────────────────────────────────────
  let container = $state<HTMLDivElement | null>(null);
  let canvas    = $state<HTMLCanvasElement | null>(null);
  let gl: WebGL2RenderingContext | null = null;
  let prog: WebGLProgram | null = null;
  let isLoading = $state(false);
  let hasRendered = $state(false);
  let errorMsg = $state('');

  const variable  = $derived($selectedVariable);
  const sweeps    = $derived($radarData.sweeps);
  const connected = $derived($connectionStatus === 'connected');
  const hasData   = $derived($radarData.filePath !== null || $radarData.variables.length > 0);

  let vmin = $state(0);
  let vmax = $state(65);
  let cmap = $state('turbo');
  let stepKm       = $state(0.5);
  let opacityScale = $state(0.8);
  let compassAngle = $state(0);    // degrees CW from screen-up to North
  let showTfe      = $state(false);
  let tfData       = $state(buildTfData('turbo'));

  function buildTfData(cmapName: string): Uint8Array {
    const rgb = COLORMAP_DATA[cmapName] ?? COLORMAP_DATA['turbo'];
    const tf = new Uint8Array(256 * 4);
    for (let i = 0; i < 256; i++) {
      const t = i / 255;
      tf[i*4]   = rgb[i*3];
      tf[i*4+1] = rgb[i*3+1];
      tf[i*4+2] = rgb[i*3+2];
      tf[i*4+3] = Math.round(Math.pow(t, 0.5) * 0.85 * 255);
    }
    return tf;
  }

  // Colorbar gradient reflects live tfData (updates when TFE presets change colors)
  const cmapGradient = $derived.by(() => {
    const stops: string[] = [];
    for (let i = 0; i <= 12; i++) {
      const idx = Math.min(Math.floor(i / 12 * 255), 255);
      stops.push(`rgb(${tfData[idx*4]},${tfData[idx*4+1]},${tfData[idx*4+2]}) ${(i/12*100).toFixed(1)}%`);
    }
    return `linear-gradient(to top, ${stops.join(', ')})`;
  });

  let loadedSweeps   = $state(new Map<number, SweepDataEntry>());
  let requestedSweeps = new Set<string>();
  let unsubData: (() => void) | null = null;

  // Camera (orbit in degrees / km)
  let camTheta = $state(-30);   // azimuthal angle (°)
  let camPhi   = $state(25);    // elevation above horizon (°), [-85, 89]
  let camDist  = $state(800);   // km from target
  let camTgt: [number,number,number] = [0,0,0];

  // Animation targets — renderFrame lerps toward these each frame
  let camTgtTarget: [number,number,number] = [0, 0, 0];
  let camDistTarget = 800;

  let isDragging  = false;
  let isShiftDrag = false;
  let lastMouse   = [0, 0];
  let dirty       = true;
  let animId      = 0;
  let resizeObs: ResizeObserver | null = null;
  let buildTimer: ReturnType<typeof setTimeout> | null = null;
  let lastBuildMs = 0;
  let destroyed   = false;

  /** Request a redraw. Draws happen on demand: no rAF loop runs while the view is idle. */
  function markDirty() {
    dirty = true;
    if (!animId && gl && !destroyed) animId = requestAnimationFrame(renderFrame);
  }
  let prevVariable = '';

  // GL resources
  let quadVAO:     WebGLVertexArrayObject | null = null;
  let volumeTex:   WebGLTexture | null = null;
  let angleIdxTex: WebGLTexture | null = null;
  let sweepMetaTex:WebGLTexture | null = null;
  let tfTex:       WebGLTexture | null = null;
  let floatLinear  = false;  // OES_texture_float_linear available

  // Cached uniform locations (set after link)
  let uloc: Record<string, WebGLUniformLocation | null> = {};

  // Volume parameters set after texture build
  let volMaxRange  = 460;   // km
  let volTotalAz   = 1;     // total rows in volume texture
  let volMaxNRange = 1;     // width of volume texture
  let volNSweeps   = 0;

  // ── Shaders ───────────────────────────────────────────────────────────────

  const VERT = `#version 300 es
    in vec2 a_pos;
    void main() { gl_Position = vec4(a_pos, 0.0, 1.0); }
  `;

  // Polar-space ray marching.
  // Coordinate system: radar at origin, X=East, Y=North, Z=up (km).
  // Each ray step is converted to spherical coords → elevation lookup → polar texture sample.
  const FRAG = `#version 300 es
    precision highp float;

    // Camera
    uniform vec3  u_camPos;
    uniform vec3  u_camRight;
    uniform vec3  u_camUp;
    uniform vec3  u_camFwd;
    uniform float u_tanHalfFovX;
    uniform float u_tanHalfFovY;
    uniform vec2  u_resolution;

    // Textures
    uniform highp sampler2D u_volume;    // polar data R32F
    uniform highp sampler2D u_angleIdx;  // elevation→fractional sweep index R32F
    uniform highp sampler2D u_sweepMeta; // per-sweep metadata RGBA32F (2×nSweeps)
    uniform sampler2D       u_transferFn;// value→RGBA RGBA8

    // Volume params
    uniform float u_maxRange;      // km
    uniform float u_stepSize;      // km per step
    uniform float u_vmin;
    uniform float u_vmax;
    uniform float u_nodata;
    uniform int   u_nSweeps;
    uniform float u_volTotalAz;    // height of volume texture
    uniform float u_volMaxNRange;  // width of volume texture
    uniform float u_opacityScale;

    // Elevation range covered by angleIdx [elevMin, elevMax] degrees
    uniform float u_elevMin;
    uniform float u_elevMax;

    // Optional XY clip box (km, radar-relative East/North)
    uniform int   u_useClip;
    uniform float u_clipMinX;
    uniform float u_clipMinY;
    uniform float u_clipMaxX;
    uniform float u_clipMaxY;

    out vec4 fragColor;

    const float PI  = 3.14159265358979;
    const float PI2 = 6.28318530717959;

    // Ray–sphere intersection. Returns (tNear, tFar); tNear > tFar means miss.
    vec2 raySphere(vec3 o, vec3 d, float r) {
      float b = dot(o, d);
      float c = dot(o, o) - r * r;
      float disc = b * b - c;
      if (disc < 0.0) return vec2(1e10, -1e10);
      float sq = sqrt(disc);
      return vec2(-b - sq, -b + sq);
    }

    // Sample one sweep with bilinear range + azimuth interpolation (wrap-around safe).
    float sampleSweep(int si, float r, float azNorm) {
      vec4 m  = texelFetch(u_sweepMeta, ivec2(0, si), 0);
      vec4 m2 = texelFetch(u_sweepMeta, ivec2(1, si), 0);
      float rangeStepKm = m.w;
      float nAz         = m.z;
      float azOffset    = m.y;
      float nRange      = m2.x;

      float rangeBin = r / rangeStepKm;
      if (rangeBin < 0.0 || rangeBin >= nRange) return u_nodata;

      float texX = (floor(rangeBin) + 0.5) / u_volMaxNRange;

      // Manual azimuth interpolation with wrap-around
      float azF    = azNorm * nAz;
      float azI0   = floor(azF);
      float azI1   = mod(azI0 + 1.0, nAz);
      float azFrac = fract(azF);

      float texY0 = (azOffset + azI0 + 0.5) / u_volTotalAz;
      float texY1 = (azOffset + azI1 + 0.5) / u_volTotalAz;

      float v0 = texture(u_volume, vec2(texX, texY0)).r;
      float v1 = texture(u_volume, vec2(texX, texY1)).r;

      bool nd0 = v0 <= u_nodata + 0.5;
      bool nd1 = v1 <= u_nodata + 0.5;
      if (nd0 && nd1) return u_nodata;
      if (nd0) return v1;
      if (nd1) return v0;
      return mix(v0, v1, azFrac);
    }

    void main() {
      // Reconstruct per-pixel ray direction
      vec2 uv = (gl_FragCoord.xy / u_resolution - 0.5) * 2.0;
      vec3 rayDir = normalize(
        u_camFwd +
        uv.x * u_tanHalfFovX * u_camRight +
        uv.y * u_tanHalfFovY * u_camUp
      );

      // Intersect bounding sphere (radar coverage radius)
      vec2 tHit = raySphere(u_camPos, rayDir, u_maxRange);
      if (tHit.x > tHit.y) { fragColor = vec4(0.07, 0.07, 0.10, 1.0); return; }

      float tOrig  = max(tHit.x, 0.0);
      float tStart = tOrig;
      float tEnd   = tHit.y;

      // Interleaved gradient noise — better spatial distribution than sin hash
      float jitter = fract(52.9829189 * fract(0.06711056 * gl_FragCoord.x + 0.00583715 * gl_FragCoord.y));
      tStart += jitter * u_stepSize;

      vec4 color = vec4(0.0);

      float elevRange = u_elevMax - u_elevMin;

      for (float t = tStart; t < tEnd && color.a < 0.99; t += u_stepSize) {
        vec3 pos = u_camPos + rayDir * t;

        float groundR = sqrt(pos.x * pos.x + pos.y * pos.y);
        float r       = length(pos);
        if (r < 0.01 || r > u_maxRange) continue;

        // XY clip box
        if (u_useClip != 0) {
          if (pos.x < u_clipMinX || pos.x > u_clipMaxX ||
              pos.y < u_clipMinY || pos.y > u_clipMaxY) continue;
        }

        // Elevation angle above horizontal (degrees)
        float elevDeg = degrees(atan(pos.z, groundR));

        // Look up fractional sweep index from elevation
        float elevNorm = (elevDeg - u_elevMin) / elevRange;
        if (elevNorm < 0.0 || elevNorm > 1.0) continue;

        float sweepF = texture(u_angleIdx, vec2(elevNorm, 0.5)).r;
        if (sweepF < 0.0) continue;   // below radar floor

        // Two bracketing sweeps for elevation interpolation
        int   s0 = int(sweepF);
        int   s1 = min(s0 + 1, u_nSweeps - 1);
        float sw = fract(sweepF);

        // Azimuth in [0,1) from North (Y+), clockwise toward East (X+)
        float az     = atan(pos.x, pos.y);  // atan2(East, North) = met. azimuth
        float azNorm = az / PI2;
        if (azNorm < 0.0) azNorm += 1.0;

        float val0 = sampleSweep(s0, r, azNorm);
        float val1 = (s0 == s1) ? val0 : sampleSweep(s1, r, azNorm);

        bool nd0 = val0 <= u_nodata + 0.5;
        bool nd1 = val1 <= u_nodata + 0.5;
        if (nd0 && nd1) continue;

        float val = nd0 ? val1 : (nd1 ? val0 : mix(val0, val1, sw));
        if (val < u_vmin) continue;

        // Map value through transfer function
        float tNorm = clamp((val - u_vmin) / (u_vmax - u_vmin), 0.0, 1.0);
        vec4 tf = texture(u_transferFn, vec2(tNorm, 0.5));
        tf.a *= u_opacityScale;

        // Front-to-back compositing
        float a = tf.a * (1.0 - color.a);
        color.rgb += tf.rgb * a;
        color.a   += a;
      }

      // Premultiplied → straight alpha
      vec4 fc = color.a > 0.001
        ? vec4(color.rgb / color.a, color.a)
        : vec4(0.07, 0.07, 0.10, 1.0);

      // Ground-plane range rings (50 km spacing), composited under data
      if (u_camPos.z > 0.0 && rayDir.z < -0.0001) {
        float tGround = -u_camPos.z / rayDir.z;
        if (tGround >= 0.0 && tGround <= tEnd) {
          vec3  gp = u_camPos + rayDir * tGround;
          float gd = length(gp.xy);
          if (gd <= u_maxRange) {
            float md = mod(gd, 50.0);
            float rw = max(0.3, 0.7 * (1.0 - gd / u_maxRange));
            if (md < rw || md > 50.0 - rw) {
              float rb = 0.5 * (1.0 - fc.a);
              fc.rgb   = mix(fc.rgb, vec3(0.28, 0.28, 0.45), rb);
              fc.a     = min(1.0, fc.a + rb * 0.35);
            }
          }
        }
      }

      // Clip box wireframe — ground footprint edges + 4 vertical corner lines
      if (u_useClip != 0) {
        float ew = max(0.25, 0.004 * length(u_camPos));

        // Ground footprint: 4 edges of the XY rectangle
        if (u_camPos.z > 0.0 && rayDir.z < -0.0001) {
          float tG = -u_camPos.z / rayDir.z;
          if (tG >= 0.0 && tG <= tEnd) {
            vec3 gp = u_camPos + rayDir * tG;
            bool inX = gp.x >= u_clipMinX && gp.x <= u_clipMaxX;
            bool inY = gp.y >= u_clipMinY && gp.y <= u_clipMaxY;
            float ex = min(abs(gp.x - u_clipMinX), abs(gp.x - u_clipMaxX));
            float ey = min(abs(gp.y - u_clipMinY), abs(gp.y - u_clipMaxY));
            if ((inY && ex < ew) || (inX && ey < ew)) {
              float cb = 0.7 * (1.0 - fc.a);
              fc.rgb = mix(fc.rgb, vec3(0.0, 0.85, 1.0), cb);
              fc.a   = min(1.0, fc.a + cb * 0.45);
            }
          }
        }

        // Vertical corner lines: closest point on ray to each corner (in XY)
        float d2r = dot(rayDir.xy, rayDir.xy);
        if (d2r > 1e-6) {
          vec2 corners[4];
          corners[0] = vec2(u_clipMinX, u_clipMinY);
          corners[1] = vec2(u_clipMaxX, u_clipMinY);
          corners[2] = vec2(u_clipMaxX, u_clipMaxY);
          corners[3] = vec2(u_clipMinX, u_clipMaxY);
          for (int i = 0; i < 4; i++) {
            float tC = ((corners[i].x - u_camPos.x) * rayDir.x +
                        (corners[i].y - u_camPos.y) * rayDir.y) / d2r;
            tC = clamp(tC, tOrig, tEnd);
            vec3  cp     = u_camPos + rayDir * tC;
            float dist2d = length(cp.xy - corners[i]);
            if (dist2d < ew && cp.z >= 0.0) {
              float fade = 1.0 - dist2d / ew;
              float cb = 0.45 * fade * (1.0 - fc.a);
              fc.rgb = mix(fc.rgb, vec3(0.0, 0.85, 1.0), cb);
              fc.a   = min(1.0, fc.a + cb * 0.3);
            }
          }
        }
      }

      fragColor = fc;
    }
  `;

  // ── GL helpers ────────────────────────────────────────────────────────────

  function compileShader(g: WebGL2RenderingContext, type: number, src: string): WebGLShader | null {
    const s = g.createShader(type)!;
    g.shaderSource(s, src);
    g.compileShader(s);
    if (!g.getShaderParameter(s, g.COMPILE_STATUS)) {
      console.error('[3D] Shader error:', g.getShaderInfoLog(s));
      return null;
    }
    return s;
  }

  function initGL(): boolean {
    if (!canvas) return false;
    gl = canvas.getContext('webgl2', { antialias: false, alpha: true, premultipliedAlpha: false });
    if (!gl) { errorMsg = 'WebGL2 not available'; return false; }

    floatLinear = !!gl.getExtension('OES_texture_float_linear');

    const vs = compileShader(gl, gl.VERTEX_SHADER, VERT);
    const fs = compileShader(gl, gl.FRAGMENT_SHADER, FRAG);
    if (!vs || !fs) { errorMsg = 'Shader compile failed'; return false; }

    prog = gl.createProgram()!;
    gl.attachShader(prog, vs);
    gl.attachShader(prog, fs);
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      errorMsg = 'Shader link failed: ' + gl.getProgramInfoLog(prog);
      console.error('[3D]', errorMsg);
      return false;
    }

    // Cache all uniform locations once
    const names = [
      'u_camPos','u_camRight','u_camUp','u_camFwd',
      'u_tanHalfFovX','u_tanHalfFovY','u_resolution',
      'u_maxRange','u_stepSize','u_vmin','u_vmax','u_nodata',
      'u_nSweeps','u_volTotalAz','u_volMaxNRange','u_opacityScale',
      'u_elevMin','u_elevMax',
      'u_volume','u_angleIdx','u_sweepMeta','u_transferFn',
      'u_useClip','u_clipMinX','u_clipMinY','u_clipMaxX','u_clipMaxY',
    ];
    for (const n of names) uloc[n] = gl.getUniformLocation(prog, n);

    // Full-screen quad (2 triangles)
    quadVAO = gl.createVertexArray()!;
    gl.bindVertexArray(quadVAO);
    const qb = gl.createBuffer()!;
    gl.bindBuffer(gl.ARRAY_BUFFER, qb);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([
      -1,-1,  1,-1,  1, 1,
      -1,-1,  1, 1, -1, 1,
    ]), gl.STATIC_DRAW);
    const posLoc = gl.getAttribLocation(prog, 'a_pos');
    gl.enableVertexAttribArray(posLoc);
    gl.vertexAttribPointer(posLoc, 2, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);

    // Pre-allocate transfer function texture
    tfTex = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, tfTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);

    gl.clearColor(0.05, 0.05, 0.1, 1.0);
    return true;
  }

  // ── Transfer function ─────────────────────────────────────────────────────

  function uploadTransferFn() {
    if (!gl || !tfTex) return;
    gl.bindTexture(gl.TEXTURE_2D, tfTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, tfData);
  }

  // ── Volume texture packer ─────────────────────────────────────────────────

  interface SweepMeta {
    elevDeg:    number;
    azOffset:   number;   // starting row in volume texture
    nAz:        number;
    rangeStepKm:number;
    nRange:     number;
  }

  function buildVolumeTexture() {
    if (!gl || loadedSweeps.size === 0) return;

    // Sort loaded sweeps by elevation (ascending)
    const entries = Array.from(loadedSweeps.entries()).sort((a, b) => {
      const ea = sweeps.find(s => s.index === a[0])?.elevation ?? 0;
      const eb = sweeps.find(s => s.index === b[0])?.elevation ?? 0;
      return ea - eb;
    });
    if (entries.length === 0) return;

    // Compute texture dimensions
    let maxNRange = 0;
    let totalAz   = 0;
    let maxRangeKm = 1;
    for (const [, e] of entries) {
      if (e.nRange > maxNRange) maxNRange = e.nRange;
      totalAz += e.nAz;
      const rKm = e.maxRange / 1000;
      if (rKm > maxRangeKm) maxRangeKm = rKm;
    }

    volMaxNRange = maxNRange;
    volTotalAz   = totalAz;
    volNSweeps   = entries.length;
    volMaxRange  = maxRangeKm;

    // Volume data: R32F, width=maxNRange, height=totalAz
    const volData = new Float32Array(maxNRange * totalAz).fill(-9999);

    // Sweep metadata: RGBA32F, width=2, height=nSweeps
    // col 0: (elevDeg, azOffset, nAz, rangeStepKm)
    // col 1: (nRange, 0, 0, 0)
    const metaData = new Float32Array(2 * 4 * entries.length).fill(0);

    const sweepMetas: SweepMeta[] = [];
    let azOffset = 0;

    for (let si = 0; si < entries.length; si++) {
      const [sweepIdx, entry] = entries[si];
      const { azimuth, range, values, nAz, nRange } = entry;

      const sweepInfo   = sweeps.find(s => s.index === sweepIdx);
      const elevDeg     = sweepInfo?.elevation ?? 0;
      const rangeStepKm = range.length > 1
        ? (range[range.length - 1] - range[0]) / (range.length - 1) / 1000
        : entry.maxRange / nRange / 1000;

      // Remap rays into a uniform azimuth grid [0°, 360°) with nAz slots, writing each
      // ray straight into its row of the packed volume (rows shorter than maxNRange
      // keep the -9999 padding from the initial fill).
      for (let ai = 0; ai < nAz; ai++) {
        let gridIdx = Math.round((azimuth[ai] / 360) * nAz) % nAz;
        if (gridIdx < 0) gridIdx += nAz;
        const src = ai * nRange;
        volData.set(values.subarray(src, src + nRange), (azOffset + gridIdx) * maxNRange);
      }

      // Pack metadata
      const mi = si * 8;
      metaData[mi + 0] = elevDeg;
      metaData[mi + 1] = azOffset;
      metaData[mi + 2] = nAz;
      metaData[mi + 3] = rangeStepKm;
      metaData[mi + 4] = nRange;

      sweepMetas.push({ elevDeg, azOffset, nAz, rangeStepKm, nRange });
      azOffset += nAz;
    }

    // LINEAR requires OES_texture_float_linear; fall back to NEAREST
    const volFilter = floatLinear ? gl.LINEAR : gl.NEAREST;
    if (volumeTex) gl.deleteTexture(volumeTex);
    volumeTex = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, volumeTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, volFilter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, volFilter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, maxNRange, totalAz, 0, gl.RED, gl.FLOAT, volData);

    // Upload sweep metadata texture (RGBA32F, 2 × nSweeps)
    if (sweepMetaTex) gl.deleteTexture(sweepMetaTex);
    sweepMetaTex = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, sweepMetaTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA32F, 2, entries.length, 0, gl.RGBA, gl.FLOAT, metaData);

    // Build angle→sweep index lookup
    buildAngleIndex(sweepMetas);

    markDirty();
  }

  // ── AngleIndex texture ────────────────────────────────────────────────────
  // Maps elevation in [elevMin, elevMax] → fractional sweep index.
  // floor(value) = lower sweep, fract(value) = interpolation weight.
  // -1 = below radar floor.

  function buildAngleIndex(metas: SweepMeta[]) {
    if (!gl || metas.length === 0) return;

    const N       = 2048;
    const elevMin = metas[0].elevDeg - 1.0;   // 1° below lowest sweep
    const elevMax = metas[metas.length - 1].elevDeg + 5.0;  // 5° above highest

    const data = new Float32Array(N);

    for (let i = 0; i < N; i++) {
      const elevDeg = elevMin + (i / (N - 1)) * (elevMax - elevMin);

      if (elevDeg < metas[0].elevDeg) {
        data[i] = -1.0;
        continue;
      }
      if (elevDeg >= metas[metas.length - 1].elevDeg) {
        // Above highest sweep: use highest sweep, no further interpolation
        data[i] = metas.length - 1.0;
        continue;
      }

      // Find bracketing sweeps (metas is already sorted by elevation)
      let lo = 0;
      for (let j = 0; j < metas.length - 1; j++) {
        if (metas[j].elevDeg <= elevDeg && elevDeg < metas[j+1].elevDeg) {
          lo = j;
          break;
        }
      }
      const t = (elevDeg - metas[lo].elevDeg) / (metas[lo+1].elevDeg - metas[lo].elevDeg);
      data[i] = lo + t;
    }

    if (angleIdxTex) gl.deleteTexture(angleIdxTex);
    angleIdxTex = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, angleIdxTex);
    // Use LINEAR so interpolation between elevation samples is smooth
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, N, 1, 0, gl.RED, gl.FLOAT, data);

    // Store elevMin/elevMax for shader uniform
    _elevMin = elevMin;
    _elevMax = elevMax;
  }

  let _elevMin = -1;
  let _elevMax = 25;

  // ── Camera vectors ────────────────────────────────────────────────────────

  function getCamVectors(): { eye:[number,number,number]; fwd:[number,number,number]; right:[number,number,number]; up:[number,number,number] } {
    const th = (camTheta * Math.PI) / 180;
    const ph = (camPhi   * Math.PI) / 180;
    const eye: [number,number,number] = [
      camTgt[0] + camDist * Math.cos(ph) * Math.sin(th),
      camTgt[1] + camDist * Math.cos(ph) * Math.cos(th),
      camTgt[2] + camDist * Math.sin(ph),
    ];
    const fwd   = norm3(diff3(camTgt, eye));
    const upHint: [number,number,number] = camPhi < 0 ? [0,0,-1] : [0,0,1];
    const right = norm3(cross3(upHint, fwd));
    const up    = cross3(fwd, right);
    return { eye, fwd, right, up };
  }

  function diff3(a:number[], b:number[]): [number,number,number] { return [a[0]-b[0],a[1]-b[1],a[2]-b[2]]; }
  function dot3(a:number[], b:number[]): number { return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]; }
  function cross3(a:number[], b:number[]): [number,number,number] {
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]];
  }
  function norm3(v:number[]): [number,number,number] {
    const l = Math.sqrt(dot3(v,v)) || 1;
    return [v[0]/l, v[1]/l, v[2]/l];
  }

  // ── Render ────────────────────────────────────────────────────────────────

  function renderFrame() {
    animId = 0;
    if (destroyed || !dirty || !gl || !prog || !quadVAO || !canvas) return;
    // Textures not uploaded yet: buildVolumeTexture() calls markDirty() once they are.
    if (!volumeTex || !angleIdxTex || !sweepMetaTex || !tfTex) return;
    dirty = false;

    const w = canvas.width, h = canvas.height;
    if (w === 0 || h === 0) return;

    gl.viewport(0, 0, w, h);
    gl.clear(gl.COLOR_BUFFER_BIT);

    gl.useProgram(prog);
    gl.bindVertexArray(quadVAO);

    // Smooth camera animation — lerp toward targets each frame
    const lf = 0.12;
    const dTgt = Math.hypot(
      camTgt[0] - camTgtTarget[0],
      camTgt[1] - camTgtTarget[1],
      camTgt[2] - camTgtTarget[2]
    );
    const dDist = Math.abs(camDist - camDistTarget);
    if (dTgt > 0.05 || dDist > 0.5) {
      camTgt = [
        camTgt[0] + (camTgtTarget[0] - camTgt[0]) * lf,
        camTgt[1] + (camTgtTarget[1] - camTgt[1]) * lf,
        camTgt[2] + (camTgtTarget[2] - camTgt[2]) * lf,
      ];
      camDist += (camDistTarget - camDist) * lf;
      dirty = true; // keep animating: scheduled again at the end of this frame
    }

    const { eye, fwd, right, up } = getCamVectors();

    // Compass: project world North (0,1,0) onto screen axes
    const nx = dot3([0,1,0], right);
    const ny = dot3([0,1,0], up);
    compassAngle = Math.atan2(nx, ny) * 180 / Math.PI;

    const fov = Math.PI / 4;
    const asp = w / h;
    const tanY = Math.tan(fov / 2);
    const tanX = tanY * asp;

    const u = (n: string) => uloc[n];

    gl.uniform3f(u('u_camPos'),        eye[0],   eye[1],   eye[2]);
    gl.uniform3f(u('u_camRight'),    right[0], right[1], right[2]);
    gl.uniform3f(u('u_camUp'),          up[0],    up[1],    up[2]);
    gl.uniform3f(u('u_camFwd'),        fwd[0],   fwd[1],   fwd[2]);
    gl.uniform1f(u('u_tanHalfFovX'), tanX);
    gl.uniform1f(u('u_tanHalfFovY'), tanY);
    gl.uniform2f(u('u_resolution'),  w, h);

    gl.uniform1f(u('u_maxRange'),     volMaxRange);
    gl.uniform1f(u('u_stepSize'),     stepKm);
    gl.uniform1f(u('u_vmin'),         vmin);
    gl.uniform1f(u('u_vmax'),         vmax);
    gl.uniform1f(u('u_nodata'),       -9999);
    gl.uniform1i(u('u_nSweeps'),      volNSweeps);
    gl.uniform1f(u('u_volTotalAz'),   volTotalAz);
    gl.uniform1f(u('u_volMaxNRange'), volMaxNRange);
    gl.uniform1f(u('u_opacityScale'), opacityScale);
    gl.uniform1f(u('u_elevMin'),      _elevMin);
    gl.uniform1f(u('u_elevMax'),      _elevMax);

    const clip = $selectedBox;
    gl.uniform1i(u('u_useClip'), clip ? 1 : 0);
    if (clip) {
      gl.uniform1f(u('u_clipMinX'), clip.xMin / 1000);
      gl.uniform1f(u('u_clipMinY'), clip.yMin / 1000);
      gl.uniform1f(u('u_clipMaxX'), clip.xMax / 1000);
      gl.uniform1f(u('u_clipMaxY'), clip.yMax / 1000);
    }

    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, volumeTex);    gl.uniform1i(u('u_volume'), 0);
    gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, angleIdxTex);  gl.uniform1i(u('u_angleIdx'), 1);
    gl.activeTexture(gl.TEXTURE2); gl.bindTexture(gl.TEXTURE_2D, sweepMetaTex); gl.uniform1i(u('u_sweepMeta'), 2);
    gl.activeTexture(gl.TEXTURE3); gl.bindTexture(gl.TEXTURE_2D, tfTex);        gl.uniform1i(u('u_transferFn'), 3);

    gl.drawArrays(gl.TRIANGLES, 0, 6);
    gl.bindVertexArray(null);

    if (dirty && !animId) animId = requestAnimationFrame(renderFrame);
  }

  // ── Resize ────────────────────────────────────────────────────────────────

  function resizeCanvas() {
    if (!canvas || !container) return;
    const rect = container.getBoundingClientRect();
    const dpr  = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.round(rect.width  * dpr);
    const h = Math.round(rect.height * dpr);
    // Assigning width/height clears the canvas even if unchanged: skip no-op resizes.
    if (w === canvas.width && h === canvas.height) return;
    canvas.width  = w;
    canvas.height = h;
    canvas.style.width  = rect.width  + 'px';
    canvas.style.height = rect.height + 'px';
    gl?.viewport(0, 0, canvas.width, canvas.height);
    dirty = true;
    // ResizeObserver callbacks run after this frame's rAF phase; draw now so the
    // cleared canvas is never presented blank.
    if (animId) { cancelAnimationFrame(animId); animId = 0; }
    renderFrame();
  }

  // ── Data loading ──────────────────────────────────────────────────────────

  $effect(() => {
    if (variable && connected && sweeps.length > 0) {
      if (variable !== prevVariable) {
        prevVariable = variable;
        cmap = getDefaultCmap(variable);
        const range = getDefaultRange(variable);
        if (range) { vmin = range[0]; vmax = range[1]; }
        tfData = buildTfData(cmap);
        loadedSweeps = new Map();
        requestedSweeps.clear();
        hasRendered = false;
        if (volumeTex && gl) { gl.deleteTexture(volumeTex); volumeTex = null; }
      }

      // Load lowest-elevation sweeps first — they cover the most ground area
      const sweepsByElev = [...sweeps].sort((a, b) => (a.elevation ?? 0) - (b.elevation ?? 0));
      let anyRequested = false;
      // Split-cut volumes (NEXRAD) carry a variable on only some sweeps; waiting for the others
      // would keep the viewer "loading" forever.
      const applicable = sweepsByElev.filter(
        (s) => !s.variables || s.variables.length === 0 || s.variables.includes(variable),
      );
      for (const s of applicable) {
        const key = `${variable}:${s.index}`;
        if (loadedSweeps.has(s.index) || requestedSweeps.has(key)) continue;
        const cached = getCachedSweep(variable, s.index);
        if (cached) {
          loadedSweeps.set(s.index, cached);
        } else {
          requestedSweeps.add(key);
          wsManager.requestSweepData(variable, s.index);
          anyRequested = true;
        }
      }

      if (loadedSweeps.size > 0) buildAndRender();
      isLoading = anyRequested || loadedSweeps.size < applicable.length;
    }
  });

  /**
   * Sweeps stream in one by one, and every rebuild repacks and re-uploads the whole
   * volume (tens of MB). Coalesce arrivals: wait at least twice as long as the last
   * rebuild took, and rebuild immediately once the last sweep is in.
   */
  function scheduleBuild() {
    if (loadedSweeps.size >= sweeps.length) { buildAndRender(); return; }
    if (buildTimer) return;
    buildTimer = setTimeout(() => {
      buildTimer = null;
      buildAndRender();
    }, Math.max(100, lastBuildMs * 2));
  }

  function onContextLost(e: Event) {
    e.preventDefault(); // lets 'webglcontextrestored' fire
    if (animId) { cancelAnimationFrame(animId); animId = 0; }
    gl = null;
    prog = null; quadVAO = null;
    volumeTex = angleIdxTex = sweepMetaTex = tfTex = null;
  }

  function onContextRestored() {
    if (destroyed || !canvas) return;
    if (!initGL()) return;
    uploadTransferFn();
    buildVolumeTexture();
    markDirty();
  }

  async function buildAndRender() {
    if (loadedSweeps.size === 0 || !container) return;
    await tick();

    if (!gl) {
      if (!canvas || !initGL()) return;
      resizeCanvas();
      resizeObs?.disconnect();
      resizeObs = new ResizeObserver(resizeCanvas);
      resizeObs.observe(container);
      canvas.addEventListener('webglcontextlost', onContextLost);
      canvas.addEventListener('webglcontextrestored', onContextRestored);
      markDirty();
    }

    if (buildTimer) { clearTimeout(buildTimer); buildTimer = null; }
    const firstBuild = volNSweeps === 0;
    const t0 = performance.now();
    uploadTransferFn();
    buildVolumeTexture();
    lastBuildMs = performance.now() - t0;
    if (firstBuild && volNSweeps > 0) resetCamera();
    hasRendered = true;
    errorMsg    = '';
    isLoading   = loadedSweeps.size < sweeps.length;
  }

  // ── Mouse / touch controls ────────────────────────────────────────────────

  function onMouseDown(e: MouseEvent) {
    isDragging = true; isShiftDrag = e.shiftKey;
    lastMouse = [e.clientX, e.clientY];
    // Snap targets to current state to stop any in-flight animation
    camTgtTarget  = [...camTgt];
    camDistTarget = camDist;
  }
  function onMouseMove(e: MouseEvent) {
    if (!isDragging) return;
    const dx = e.clientX - lastMouse[0];
    const dy = e.clientY - lastMouse[1];
    lastMouse = [e.clientX, e.clientY];
    if (isShiftDrag) {
      const sc = camDist * 0.002;
      const th = (camTheta * Math.PI) / 180;
      camTgt = [
        camTgt[0] - dx * sc * Math.cos(th),
        camTgt[1] + dx * sc * Math.sin(th),
        camTgt[2] + dy * sc,
      ];
      camTgtTarget = [...camTgt];  // don't fight manual pan
    } else {
      camTheta += dx * 0.4;
      camPhi    = Math.max(-85, Math.min(89, camPhi - dy * 0.4));
    }
    markDirty();
  }
  function onMouseUp() { isDragging = false; isShiftDrag = false; }
  function onWheel(e: WheelEvent) {
    e.preventDefault();
    e.stopPropagation();
    camDist = Math.max(10, Math.min(5000, camDist * (1 + e.deltaY * 0.001)));
    camDistTarget = camDist;  // don't fight manual zoom
    markDirty();
  }
  function onKeyDown(e: KeyboardEvent) {
    const step = e.shiftKey ? 15 : 5;
    switch (e.key) {
      case 'ArrowLeft':  camTheta -= step; break;
      case 'ArrowRight': camTheta += step; break;
      case 'ArrowUp':    camPhi = Math.min(89, camPhi + step); break;
      case 'ArrowDown':  camPhi = Math.max(-85, camPhi - step); break;
      case '+': case '=': camDist = Math.max(10, camDist * 0.85); camDistTarget = camDist; break;
      case '-':           camDist = Math.min(5000, camDist * 1.15); camDistTarget = camDist; break;
      case 'r': case 'R': resetCamera(); return;
      default: return;
    }
    e.preventDefault();
    markDirty();
  }

  function resetCamera() {
    camTheta      = -30; camPhi = 25;
    camDistTarget = volMaxRange * 1.8;
    camTgtTarget  = [0, 0, 0];
    markDirty();
  }

  // ── Lifecycle ─────────────────────────────────────────────────────────────

  $effect(() => {
    const clip = $selectedBox;
    markDirty();
    if (clip) {
      camTgtTarget = [
        (clip.xMin + clip.xMax) / 2 / 1000,
        (clip.yMin + clip.yMax) / 2 / 1000,
        0,
      ];
      camDistTarget = Math.max(50, Math.hypot(
        (clip.xMax - clip.xMin) / 1000,
        (clip.yMax - clip.yMin) / 1000,
      ) * 2);
      if (camPhi < 15) camPhi = 15;
    } else {
      camTgtTarget  = [0, 0, 0];
      camDistTarget = volMaxRange * 1.8;
    }
  });

  onMount(() => {
    unsubData = wsManager.onMessage('sweep_data_ready', (entry: SweepDataEntry) => {
      if (entry.variable !== variable) return;
      requestedSweeps.delete(`${entry.variable}:${entry.sweep}`);
      loadedSweeps.set(entry.sweep, entry);
      loadedSweeps = loadedSweeps;
      isLoading = loadedSweeps.size < sweeps.length;
      scheduleBuild();
    });
  });

  onDestroy(() => {
    destroyed = true;
    unsubData?.();
    if (animId) { cancelAnimationFrame(animId); animId = 0; }
    if (buildTimer) { clearTimeout(buildTimer); buildTimer = null; }
    resizeObs?.disconnect();
    resizeObs = null;
    canvas?.removeEventListener('webglcontextlost', onContextLost);
    canvas?.removeEventListener('webglcontextrestored', onContextRestored);
    if (gl) {
      [volumeTex, angleIdxTex, sweepMetaTex, tfTex].forEach(t => t && gl!.deleteTexture(t));
      if (prog)    gl.deleteProgram(prog);
      if (quadVAO) gl.deleteVertexArray(quadVAO);
    }
  });
</script>

<div class="viewer-3d">
  {#if hasData}
    <div class="controls-3d">
      <span class="label-3d">3D Volume</span>
      {#if variable}<span class="var-badge">{variable}</span>{/if}

      <div class="ctrl-group">
        <label for="step-ctrl">Step</label>
        <input id="step-ctrl" type="range" min="0.25" max="3" step="0.25"
               bind:value={stepKm} oninput={() => { markDirty(); }} />
        <span class="ctrl-val">{stepKm}km</span>
      </div>

      <div class="ctrl-group">
        <label for="opacity-ctrl">Opacity</label>
        <input id="opacity-ctrl" type="range" min="0.1" max="1" step="0.05"
               bind:value={opacityScale} oninput={() => { markDirty(); }} />
        <span class="ctrl-val">{Math.round(opacityScale * 100)}%</span>
      </div>

      <button class="reset-btn" onclick={resetCamera} title="Reset camera (R)">Reset</button>
      <button class="reset-btn" class:active={showTfe} onclick={() => showTfe = !showTfe} title="Transfer function editor">TF</button>

      {#if $selectedBox}
        {@const bw = Math.round(Math.abs($selectedBox.xMax - $selectedBox.xMin) / 1000)}
        {@const bh = Math.round(Math.abs($selectedBox.yMax - $selectedBox.yMin) / 1000)}
        <span class="clip-badge" title="Active clip region">{bw}×{bh} km</span>
      {/if}

      {#if loadedSweeps.size > 0}
        <span class="sweep-count">{loadedSweeps.size}/{sweeps.length} sweeps</span>
      {/if}
    </div>

    {#if showTfe}
      <div class="tfe-panel">
        <TransferFunctionEditor
          bind:transferFunction={tfData}
          onchange={() => { uploadTransferFn(); markDirty(); }}
        />
      </div>
    {/if}

    <!-- svelte-ignore a11y-no-noninteractive-tabindex -->
    <div class="canvas-wrap" bind:this={container}
         tabindex="0"
         onmousedown={onMouseDown}
         onmousemove={onMouseMove}
         onmouseup={onMouseUp}
         onmouseleave={onMouseUp}
         onwheel={onWheel}
         onkeydown={onKeyDown}>
      <canvas bind:this={canvas}></canvas>

      {#if hasRendered}
        <!-- Colorbar -->
        <div class="colorbar-wrap">
          <span class="cb-label">{vmax.toFixed(1)}</span>
          <div class="cb-bar" style="background: {cmapGradient}"></div>
          <span class="cb-label">{vmin.toFixed(1)}</span>
          <span class="cb-var">{variable}</span>
        </div>

        <!-- Compass -->
        <div class="compass-wrap">
          <svg viewBox="-20 -20 40 40" width="44" height="44">
            <circle r="18" fill="rgba(0,0,0,0.45)" stroke="rgba(255,255,255,0.12)" stroke-width="0.5"/>
            <g transform="rotate({compassAngle})">
              <polygon points="0,-15 -3,0 0,-6 3,0" fill="#e05252"/>
              <polygon points="0,15 -3,0 0,6 3,0" fill="#555"/>
            </g>
            <text transform="rotate({compassAngle}) translate(0,-22)"
                  text-anchor="middle" dominant-baseline="middle"
                  font-size="6.5" font-weight="bold" fill="#e05252"
                  font-family="system-ui,sans-serif">N</text>
          </svg>
        </div>
      {/if}

      {#if isLoading}
        <div class="overlay">
          <div class="spinner"></div>
          <p>Loading {loadedSweeps.size}/{sweeps.length} sweeps…</p>
        </div>
      {:else if errorMsg}
        <div class="overlay error"><p>{errorMsg}</p></div>
      {:else if !hasRendered}
        <div class="overlay"><p>Select a variable to render in 3D</p></div>
      {/if}
    </div>

    <div class="hint-3d">Drag · Scroll to zoom · Shift+drag to pan · Arrow keys · R to reset</div>
  {:else}
    <div class="overlay full"><p>Open a radar file to view in 3D</p></div>
  {/if}
</div>

<style>
  .viewer-3d {
    display: flex;
    flex-direction: column;
    height: 100%;
    background: var(--bg-primary);
    overflow: hidden;
  }

  .controls-3d {
    display: flex;
    align-items: center;
    gap: var(--spacing-md);
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-secondary);
    border-bottom: 1px solid var(--border-color);
    flex-shrink: 0;
    flex-wrap: wrap;
  }

  .label-3d {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .var-badge {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--accent-primary);
    background: rgba(91,108,247,0.1);
    padding: 2px 8px;
    border-radius: var(--radius-sm);
    border: 1px solid rgba(91,108,247,0.2);
  }

  .ctrl-group {
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .ctrl-group label {
    font-size: 10px;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.03em;
    font-weight: 600;
    white-space: nowrap;
  }
  .ctrl-group input[type="range"] { width: 70px; accent-color: var(--accent-primary); }
  .ctrl-val {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text-secondary);
    min-width: 36px;
  }

  .reset-btn {
    font-size: 10px;
    padding: 2px 8px;
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    background: var(--bg-tertiary);
    color: var(--text-secondary);
    cursor: pointer;
  }
  .reset-btn:hover { background: var(--accent-primary); color: white; border-color: var(--accent-primary); }
  .reset-btn.active { background: var(--accent-primary); color: white; border-color: var(--accent-primary); }

  .clip-badge {
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 600;
    color: rgb(0, 220, 255);
    background: rgba(0, 220, 255, 0.1);
    border: 1px solid rgba(0, 220, 255, 0.35);
    border-radius: var(--radius-sm);
    padding: 2px 7px;
    white-space: nowrap;
  }

  .tfe-panel {
    padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-secondary);
    border-bottom: 1px solid var(--border-color);
    flex-shrink: 0;
  }

  .sweep-count {
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--text-muted);
    margin-left: auto;
  }

  .canvas-wrap {
    flex: 1;
    position: relative;
    overflow: hidden;
    cursor: grab;
    outline: none;
  }
  .canvas-wrap:active { cursor: grabbing; }
  .canvas-wrap canvas { display: block; }

  .colorbar-wrap {
    position: absolute;
    right: 10px;
    top: 50%;
    transform: translateY(-50%);
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 3px;
    pointer-events: none;
    z-index: 3;
  }
  .cb-bar {
    width: 13px;
    height: 150px;
    border-radius: 2px;
    border: 1px solid rgba(255,255,255,0.12);
  }
  .cb-label {
    font-family: var(--font-mono);
    font-size: 10px;
    color: rgba(255,255,255,0.85);
    text-shadow: 0 1px 3px rgba(0,0,0,0.9);
    line-height: 1;
  }
  .cb-var {
    font-family: var(--font-mono);
    font-size: 9px;
    color: rgba(255,255,255,0.5);
    text-shadow: 0 1px 3px rgba(0,0,0,0.9);
    margin-top: 2px;
    writing-mode: vertical-rl;
    transform: rotate(180deg);
  }

  .compass-wrap {
    position: absolute;
    bottom: 10px;
    left: 10px;
    pointer-events: none;
    z-index: 3;
  }

  .hint-3d {
    padding: 3px var(--spacing-md);
    font-size: 10px;
    color: var(--text-muted);
    text-align: center;
    background: var(--bg-tertiary);
    border-top: 1px solid var(--border-color);
  }

  .overlay {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--spacing-md);
    color: var(--text-muted);
    pointer-events: none;
    z-index: 5;
  }
  .overlay.full { position: relative; height: 100%; }
  .overlay.error { color: var(--accent-danger); }

  .spinner {
    width: 32px; height: 32px;
    border: 3px solid var(--border-color);
    border-top-color: var(--accent-primary);
    border-radius: 50%;
    animation: spin 800ms linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
