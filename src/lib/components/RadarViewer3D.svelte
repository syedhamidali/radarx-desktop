<script lang="ts">
  import { onMount, onDestroy, tick, untrack } from 'svelte';
  import { radarData, selectedVariable, connectionStatus } from '../stores/radarData';
  import { selectedBox } from '../stores/volumeSelect';
  import { wsManager } from '../utils/websocket';
  import { getCachedSweep, getDefaultCmap, getDefaultRange, type SweepDataEntry } from '../utils/ppiRenderer';
  import { COLORMAP_DATA } from '../utils/colormaps';
  import { styleFor, variableStylesRevision } from '../utils/variableStyles';
  import {
    QualityGovernor,
    ProgressiveRenderer,
    yieldToMain,
    type ProgressiveJob,
    type QualityMode,
    type GovernorState,
  } from '../utils/qualityGovernor';
  import TransferFunctionEditor from './TransferFunctionEditor.svelte';

  // ── Reactive state ────────────────────────────────────────────────────────
  let container = $state<HTMLDivElement | null>(null);
  let canvas    = $state<HTMLCanvasElement | null>(null);
  let gl: WebGL2RenderingContext | null = null;
  let prog: WebGLProgram | null = null;
  let isLoading = $state(false);
  let hasRendered = $state(false);
  let errorMsg = $state('');
  /** Set when drawing threw or the context cannot be recovered: the view stops instead of looping. */
  let fatalMsg = $state('');

  const variable  = $derived($selectedVariable);
  const sweeps    = $derived($radarData.sweeps);
  const connected = $derived($connectionStatus === 'connected');
  const hasData   = $derived($radarData.filePath !== null || $radarData.variables.length > 0);
  /** Presentation of a derived variable (label, class table); undefined for ordinary file variables. */
  const varStyle = $derived.by(() => {
    void $variableStylesRevision;
    return styleFor(variable);
  });
  const variableLabel = $derived(varStyle?.label ?? variable ?? '');
  const categories = $derived(varStyle?.categories ?? null);

  /** Sweeps that actually carry the selected variable, lowest elevation first. */
  const applicable = $derived.by(() => {
    if (!variable) return [];
    // Split-cut volumes (NEXRAD) carry a variable on only some sweeps; waiting for the others
    // would keep the viewer "loading" forever.
    return [...sweeps]
      .sort((a, b) => (a.elevation ?? 0) - (b.elevation ?? 0))
      .filter((s) => !s.variables || s.variables.length === 0 || s.variables.includes(variable));
  });

  let vmin = $state(0);
  let vmax = $state(65);
  let cmap = $state('turbo');
  let stepKm       = $state(0.5);
  let opacityScale = $state(0.8);
  let compassAngle = $state(0);    // degrees CW from screen-up to North
  let showTfe      = $state(false);
  let tfData       = $state(buildTfData('turbo'));

  function buildTfData(cmapName: string, flatAlpha = false): Uint8Array {
    const rgb = COLORMAP_DATA[cmapName] ?? COLORMAP_DATA['turbo'];
    const tf = new Uint8Array(256 * 4);
    for (let i = 0; i < 256; i++) {
      const t = i / 255;
      tf[i*4]   = rgb[i*3];
      tf[i*4+1] = rgb[i*3+1];
      tf[i*4+2] = rgb[i*3+2];
      // Categorical classes are equally important: no value-dependent fade-in
      tf[i*4+3] = flatAlpha ? Math.round(0.85 * 255) : Math.round(Math.pow(t, 0.5) * 0.85 * 255);
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

  // ── Quality governor ──────────────────────────────────────────────────────
  const governor = new QualityGovernor('radar3d');
  let qState = $state<GovernorState>(governor.current());
  let unsubGov: (() => void) | null = null;
  let pr: ProgressiveRenderer | null = null;

  const QUALITY_OPTIONS: { value: QualityMode; label: string }[] = [
    { value: 'auto', label: 'Auto' },
    { value: 'high', label: 'High' },
    { value: 'medium', label: 'Medium' },
    { value: 'low', label: 'Low' },
  ];

  function onQualityChange(e: Event) {
    governor.setMode((e.target as HTMLSelectElement).value as QualityMode);
    // The volume texture budget follows the quality class: repack only when it really changed.
    if (layout && layout.texMB !== governor.textureBudgetMB()) relayoutAll();
    pr?.request();
  }

  // ── Sweep loading ─────────────────────────────────────────────────────────
  const SWEEP_TIMEOUT_MS = 30_000;

  /** Raw sweeps received so far (shared with the sweep cache, not copied). */
  let loadedSweeps = new Map<number, SweepDataEntry>();
  /** sweep index -> time requested, for sweeps still in flight. */
  let outstanding = new Map<number, number>();
  let failed = new Set<number>();
  let applicableSet = new Set<number>();
  let loadKey = '';
  let prevVariable = '';
  let unsubData: (() => void) | null = null;
  let unsubFail: (() => void) | null = null;
  let timeoutTimer: ReturnType<typeof setInterval> | null = null;

  // UI counters (the maps above are not reactive)
  let totalCount    = $state(0);
  let receivedCount = $state(0);
  let failedCount   = $state(0);
  let packedCount   = $state(0);

  function syncCounts() {
    totalCount    = applicableSet.size;
    receivedCount = loadedSweeps.size;
    failedCount   = failed.size;
    packedCount   = packedMeta.size;
    isLoading     = outstanding.size > 0;
  }

  // Camera (orbit in degrees / km)
  let camTheta = $state(-30);   // azimuthal angle (°)
  let camPhi   = $state(25);    // elevation above horizon (°), [-85, 89]
  let camDist  = $state(800);   // km from target
  let camTgt: [number,number,number] = [0,0,0];

  // Animation targets — the frame job lerps toward these each frame
  let camTgtTarget: [number,number,number] = [0, 0, 0];
  let camDistTarget = 800;

  let isDragging  = false;
  let isShiftDrag = false;
  let lastMouse   = [0, 0];
  let resizeObs: ResizeObserver | null = null;
  let destroyed   = false;
  let cameraInitKey = '';
  let lastAnimT = 0;

  /** Request a redraw. Draws are scheduled on demand: nothing runs while the view is idle. */
  function markDirty() { pr?.request(); }
  /** Camera / slider activity: render coarser while it lasts, one settled frame afterwards. */
  function interact() { pr?.noteInteraction(); }

  // GL resources
  let quadVAO:     WebGLVertexArrayObject | null = null;
  let volumeTex:   WebGLTexture | null = null;
  let angleIdxTex: WebGLTexture | null = null;
  let sweepMetaTex:WebGLTexture | null = null;
  let tfTex:       WebGLTexture | null = null;
  let maxTexSize = 4096;

  // Cached uniform locations (set after link)
  let uloc: Record<string, WebGLUniformLocation | null> = {};

  // Volume parameters set after publish
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
  // The volume is an R8 texture: 0 = no data, 1..255 map linearly onto [vmin, vmax].
  const FRAG = `#version 300 es
    precision highp float;
    precision highp int;

    // Camera
    uniform vec3  u_camPos;
    uniform vec3  u_camRight;
    uniform vec3  u_camUp;
    uniform vec3  u_camFwd;
    uniform float u_tanHalfFovX;
    uniform float u_tanHalfFovY;
    uniform vec2  u_resolution;

    // Textures
    uniform highp sampler2D u_volume;    // polar data R8 (quantised)
    uniform highp sampler2D u_angleIdx;  // elevation→fractional sweep index R32F
    uniform highp sampler2D u_sweepMeta; // per-sweep metadata RGBA32F (2×nSweeps)
    uniform sampler2D       u_transferFn;// value→RGBA RGBA8

    // Volume params
    uniform float u_maxRange;      // km
    uniform float u_stepSize;      // km per step (user step x quality multiplier)
    uniform float u_stepUser;      // km per step the user asked for (opacity reference)
    uniform int   u_maxSteps;      // hard cap on iterations; step grows to honour it
    uniform int   u_interp;        // 0 nearest, 1 azimuth, 2 azimuth + elevation
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

    // Decode a stored byte (0..1 after normalisation) to 0..1 over [vmin, vmax]; -1 = no data.
    float decode(float b) {
      return b < 0.002 ? -1.0 : (b * 255.0 - 1.0) / 254.0;
    }

    // Sample one sweep; returns normalised value in [0,1] or -1 for no data.
    float sampleSweep(int si, float r, float azNorm) {
      vec4 m  = texelFetch(u_sweepMeta, ivec2(0, si), 0);
      vec4 m2 = texelFetch(u_sweepMeta, ivec2(1, si), 0);
      float rangeStepKm = m.w;
      float nAz         = m.z;
      float azOffset    = m.y;
      float nRange      = m2.x;

      float rangeBin = r / rangeStepKm;
      if (rangeBin < 0.0 || rangeBin >= nRange) return -1.0;

      float texX = (floor(rangeBin) + 0.5) / u_volMaxNRange;

      float azF  = azNorm * nAz;
      float azI0 = floor(azF);
      float texY0 = (azOffset + azI0 + 0.5) / u_volTotalAz;
      float v0 = decode(texture(u_volume, vec2(texX, texY0)).r);
      if (u_interp == 0) return v0;

      // Azimuth interpolation with wrap-around
      float azI1   = mod(azI0 + 1.0, nAz);
      float azFrac = fract(azF);
      float texY1  = (azOffset + azI1 + 0.5) / u_volTotalAz;
      float v1 = decode(texture(u_volume, vec2(texX, texY1)).r);

      if (v0 < 0.0 && v1 < 0.0) return -1.0;
      if (v0 < 0.0) return v1;
      if (v1 < 0.0) return v0;
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

      // The iteration cap wins over the requested step: long rays take longer steps.
      float stepEff = max(u_stepSize, (tEnd - tStart) / float(u_maxSteps));
      // Per-step opacity is compensated for steps coarser than the user's own setting.
      float comp = stepEff / u_stepUser;

      // Interleaved gradient noise — better spatial distribution than sin hash
      float jitter = fract(52.9829189 * fract(0.06711056 * gl_FragCoord.x + 0.00583715 * gl_FragCoord.y));
      tStart += jitter * stepEff;

      vec4 color = vec4(0.0);

      float elevRange = u_elevMax - u_elevMin;

      for (int i = 0; i < 4096; i++) {
        if (i >= u_maxSteps) break;
        float t = tStart + float(i) * stepEff;
        if (t >= tEnd || color.a >= 0.99) break;

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

        float val;
        if (u_interp < 2) {
          // Cheap path: nearest sweep only
          val = sampleSweep(sw < 0.5 ? s0 : s1, r, azNorm);
          if (val < 0.0) continue;
        } else {
          float val0 = sampleSweep(s0, r, azNorm);
          float val1 = (s0 == s1) ? val0 : sampleSweep(s1, r, azNorm);
          bool nd0 = val0 < 0.0;
          bool nd1 = val1 < 0.0;
          if (nd0 && nd1) continue;
          val = nd0 ? val1 : (nd1 ? val0 : mix(val0, val1, sw));
        }

        // Map value through transfer function
        vec4 tf = texture(u_transferFn, vec2(val, 0.5));
        float a0 = tf.a * u_opacityScale;
        if (comp > 1.001) a0 = 1.0 - pow(max(1.0 - a0, 0.0), comp);

        // Front-to-back compositing
        float a = a0 * (1.0 - color.a);
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

  // ── GL setup ──────────────────────────────────────────────────────────────

  let glInit: Promise<boolean> | null = null;
  let glFailed = false;

  function ensureGl(): Promise<boolean> {
    if (gl && prog) return Promise.resolve(true);
    if (glFailed) return Promise.resolve(false);
    if (!glInit) glInit = initGL().then((ok) => { glInit = null; if (!ok) glFailed = true; return ok; });
    return glInit;
  }

  const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

  async function initGL(): Promise<boolean> {
    await tick();
    if (!canvas || destroyed) return false;
    const g = canvas.getContext('webgl2', { antialias: false, alpha: true, premultipliedAlpha: false, preserveDrawingBuffer: true });
    if (!g) { fatalMsg = 'WebGL2 is not available on this system.'; return false; }

    const vs = g.createShader(g.VERTEX_SHADER)!;
    g.shaderSource(vs, VERT); g.compileShader(vs);
    const fs = g.createShader(g.FRAGMENT_SHADER)!;
    g.shaderSource(fs, FRAG); g.compileShader(fs);
    const p = g.createProgram()!;
    g.attachShader(p, vs);
    g.attachShader(p, fs);
    g.linkProgram(p);

    // Shader compilation of the ray-marching loop can take seconds on software rasterisers; with
    // KHR_parallel_shader_compile it happens off the main thread and we only poll for completion.
    const par = g.getExtension('KHR_parallel_shader_compile');
    if (par) {
      const t0 = performance.now();
      while (!g.getProgramParameter(p, par.COMPLETION_STATUS_KHR)) {
        if (destroyed) return false;
        if (performance.now() - t0 > 60_000) break;
        await sleep(10);
      }
    }
    if (!g.getProgramParameter(p, g.LINK_STATUS)) {
      const log = g.getProgramInfoLog(p) || g.getShaderInfoLog(fs) || g.getShaderInfoLog(vs) || '';
      console.error('[3D] Shader error:', log);
      fatalMsg = 'The 3D shader failed to compile on this GPU.';
      return false;
    }
    g.deleteShader(vs);
    g.deleteShader(fs);

    gl = g;
    prog = p;
    maxTexSize = Math.max(1024, Math.min(16384, g.getParameter(g.MAX_TEXTURE_SIZE) as number));

    // Detect weak renderers (software GL, integrated GPUs) and pick start level + ceiling.
    governor.configureGpu(g);

    const names = [
      'u_camPos','u_camRight','u_camUp','u_camFwd',
      'u_tanHalfFovX','u_tanHalfFovY','u_resolution',
      'u_maxRange','u_stepSize','u_stepUser','u_maxSteps','u_interp','u_nSweeps',
      'u_volTotalAz','u_volMaxNRange','u_opacityScale',
      'u_elevMin','u_elevMax',
      'u_volume','u_angleIdx','u_sweepMeta','u_transferFn',
      'u_useClip','u_clipMinX','u_clipMinY','u_clipMaxX','u_clipMaxY',
    ];
    uloc = {};
    for (const n of names) uloc[n] = g.getUniformLocation(p, n);

    // Full-screen quad (2 triangles)
    quadVAO = g.createVertexArray()!;
    g.bindVertexArray(quadVAO);
    const qb = g.createBuffer()!;
    g.bindBuffer(g.ARRAY_BUFFER, qb);
    g.bufferData(g.ARRAY_BUFFER, new Float32Array([
      -1,-1,  1,-1,  1, 1,
      -1,-1,  1, 1, -1, 1,
    ]), g.STATIC_DRAW);
    const posLoc = g.getAttribLocation(p, 'a_pos');
    g.enableVertexAttribArray(posLoc);
    g.vertexAttribPointer(posLoc, 2, g.FLOAT, false, 0, 0);
    g.bindVertexArray(null);

    tfTex = g.createTexture()!;
    g.bindTexture(g.TEXTURE_2D, tfTex);
    g.texParameteri(g.TEXTURE_2D, g.TEXTURE_MIN_FILTER, g.LINEAR);
    g.texParameteri(g.TEXTURE_2D, g.TEXTURE_MAG_FILTER, g.LINEAR);
    g.texParameteri(g.TEXTURE_2D, g.TEXTURE_WRAP_S, g.CLAMP_TO_EDGE);
    g.texParameteri(g.TEXTURE_2D, g.TEXTURE_WRAP_T, g.CLAMP_TO_EDGE);
    g.clearColor(0.05, 0.05, 0.1, 1.0);
    uploadTransferFn();

    if (!container) return false;
    resizeCanvas();
    resizeObs?.disconnect();
    resizeObs = new ResizeObserver(resizeCanvas);
    resizeObs.observe(container);
    canvas.addEventListener('webglcontextlost', onContextLost);
    canvas.addEventListener('webglcontextrestored', onContextRestored);

    pr = new ProgressiveRenderer({
      gl: g,
      canvas,
      governor,
      job: frameJob,
      onError: (e) => {
        console.error('[3D] render failed:', e);
        fatalMsg = `3D rendering stopped (${e instanceof Error ? e.message : String(e)}).`;
      },
    });
    // Diagnostics hook for tests / the console: renderer counters, no behaviour.
    (globalThis as any).__xr3d = {
      stats: () => ({ ...pr?.stats, gov: governor.snapshot() }),
      // Test-only handles (dev server): lose/restore the context, tamper with the scheduler.
      ...(import.meta.env.DEV ? { gl: () => gl, pr: () => pr } : {}),
    };
    return true;
  }

  function uploadTransferFn() {
    if (!gl || !tfTex) return;
    gl.bindTexture(gl.TEXTURE_2D, tfTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, tfData);
  }

  // ── Volume packing (time-sliced, incremental) ─────────────────────────────
  //
  // The volume texture is an R8 grid: width = range gates, height = one fixed-size slot of azimuth
  // rows per sweep (slot = rank of the sweep's elevation among the sweeps carrying the variable).
  // A sweep arriving later only packs and uploads its own slot, so loading N sweeps costs O(N), not
  // O(N^2), and nothing is repacked while the user watches. The grid is sized to a memory budget:
  // when the data is bigger the range gates and/or azimuth rows are max-pooled down.

  const SLICE_MS = 6;            // max CPU time per packing task
  const SLAB_BYTES = 1_500_000;  // max bytes per texSubImage2D call
  const PUBLISH_MIN_MS = 250;    // coalesce metadata/texture publishes

  interface Layout {
    W: number;          // texture width: range bins
    slotRows: number;   // azimuth rows per sweep slot
    fr: number;         // source gates per texture bin
    nSlots: number;
    slotOf: Map<number, number>;
    elevOf: Map<number, number>;
    texMB: number;      // budget this layout was computed for
  }
  interface PackedMeta {
    slot: number; elevDeg: number; nAzOut: number; rangeStepKm: number; nOut: number; maxRangeKm: number;
  }

  let layout: Layout | null = null;
  let cpuVol: Uint8Array | null = null;
  let texAllocated = false;
  let packedMeta = new Map<number, PackedMeta>();
  let pendingPack: number[] = [];
  let packRunning = false;
  let packGen = 0;
  let packVmin = 0;
  let packVmax = 1;
  let publishTimer: ReturnType<typeof setTimeout> | null = null;
  let lastPublishAt = 0;

  function computeLayout(entries: SweepDataEntry[], texMB: number): Layout {
    let R = 1, azMax = 1;
    for (const e of entries) { R = Math.max(R, e.nRange); azMax = Math.max(azMax, e.nAz); }
    const nSlots = applicable.length;
    let sr = Math.min(Math.max(azMax, 90), 1440);
    let fr = 1;
    const budget = texMB * 1048576;
    for (let i = 0; i < 32; i++) {
      const W = Math.ceil(R / fr), H = nSlots * sr;
      if (W * H <= budget && W <= maxTexSize && H <= maxTexSize) break;
      if (i % 2 === 0 && fr < 8) fr++;
      else if (sr > 90) sr = Math.max(90, Math.floor(sr * 0.75));
      else if (fr < 8) fr++;
      else break;
    }
    const slotOf = new Map<number, number>();
    const elevOf = new Map<number, number>();
    applicable.forEach((s, i) => { slotOf.set(s.index, i); elevOf.set(s.index, s.elevation ?? 0); });
    return { W: Math.ceil(R / fr), slotRows: sr, fr, nSlots, slotOf, elevOf, texMB };
  }

  function dropVolumeGpu() {
    if (gl && volumeTex) gl.deleteTexture(volumeTex);
    volumeTex = null;
    texAllocated = false;
  }

  /** Discard all packed data and start over from the raw sweeps already received. */
  function relayoutAll() {
    packGen++;
    packRunning = false;
    layout = null;
    cpuVol = null;
    packedMeta = new Map();
    pendingPack = [...loadedSweeps.keys()];
    dropVolumeGpu();
    volNSweeps = 0;
    syncCounts();
    kickPack();
  }

  function resetVolume() {
    packGen++;
    packRunning = false;
    layout = null;
    cpuVol = null;
    packedMeta = new Map();
    pendingPack = [];
    loadedSweeps = new Map();
    outstanding = new Map();
    failed = new Set();
    dropVolumeGpu();
    volNSweeps = 0;
    hasRendered = false;
    if (publishTimer) { clearTimeout(publishTimer); publishTimer = null; }
  }

  function ingest(entry: SweepDataEntry) {
    if (!applicableSet.has(entry.sweep)) return;
    loadedSweeps.set(entry.sweep, entry);
    if (!pendingPack.includes(entry.sweep) && !packedMeta.has(entry.sweep)) pendingPack.push(entry.sweep);
    else if (packedMeta.has(entry.sweep)) {
      // Re-delivered (e.g. reprocessed): repack that slot
      packedMeta.delete(entry.sweep);
      if (!pendingPack.includes(entry.sweep)) pendingPack.push(entry.sweep);
    }
    syncCounts();
    kickPack();
  }

  function kickPack() {
    if (packRunning || destroyed) return;
    packRunning = true;
    const g = packGen;
    void packLoop(g);
  }

  async function packLoop(g: number) {
    try {
      if (!(await ensureGl())) { if (g === packGen) packRunning = false; return; }
      while (pendingPack.length > 0 && g === packGen && !destroyed) {
        // Never rebuild while the camera is being dragged: wait until the gesture ends.
        if (isDragging) { await sleep(120); continue; }
        const idx = pendingPack.shift()!;
        const entry = loadedSweeps.get(idx);
        if (!entry) continue;

        const need = layoutFits(entry);
        if (!need) {
          // Layout missing or too small for this sweep: rebuild it (rare) and requeue everything.
          layout = computeLayout([...loadedSweeps.values()], governor.textureBudgetMB());
          cpuVol = new Uint8Array(layout.W * layout.nSlots * layout.slotRows);
          packedMeta = new Map();
          dropVolumeGpu();
          pendingPack = [idx, ...[...loadedSweeps.keys()].filter((k) => k !== idx)];
          governor.setVolumeMB(cpuVol.length / 1048576);
          continue;
        }
        const ok = await packSweep(entry, layout!.slotOf.get(idx)!, g);
        if (!ok || g !== packGen) break;
        await uploadSlot(idx, g);
        if (g !== packGen) break;
        syncCounts();
        schedulePublish();
        await yieldToMain();
      }
    } catch (e) {
      console.error('[3D] volume build failed:', e);
      fatalMsg = `Could not build the 3D volume (${e instanceof Error ? e.message : String(e)}).`;
    } finally {
      if (g === packGen) {
        packRunning = false;
        if (pendingPack.length > 0 && !destroyed) kickPack();
        else schedulePublish();
      }
    }
  }

  function layoutFits(entry: SweepDataEntry): boolean {
    if (!layout || !cpuVol) return false;
    if (!layout.slotOf.has(entry.sweep)) return false;
    return Math.ceil(entry.nRange / layout.fr) <= layout.W;
  }

  /** Quantise one sweep into its slot of the CPU volume, yielding every few ms. */
  async function packSweep(entry: SweepDataEntry, slot: number, g: number): Promise<boolean> {
    const L = layout!;
    const vol = cpuVol!;
    const W = L.W, fr = L.fr;
    const nAzOut = Math.min(entry.nAz, L.slotRows);
    const nOut = Math.ceil(entry.nRange / fr);
    const base = slot * L.slotRows;
    vol.fill(0, base * W, (base + L.slotRows) * W);

    const { azimuth, values, nAz, nRange } = entry;
    const lo = packVmin;
    const sc = 254 / (packVmax - packVmin || 1);
    let t0 = performance.now();

    for (let ai = 0; ai < nAz; ai++) {
      let gi = Math.round((azimuth[ai] / 360) * nAzOut) % nAzOut;
      if (gi < 0) gi += nAzOut;
      const dst = (base + gi) * W;
      const src = ai * nRange;
      if (fr === 1) {
        for (let r = 0; r < nRange; r++) {
          const v = values[src + r];
          if (v >= lo) {               // NaN and -9999 fail this test: stay "no data"
            const t = (v - lo) * sc;
            const b = t >= 254 ? 255 : (t + 1.5) | 0;
            if (b > vol[dst + r]) vol[dst + r] = b;
          }
        }
      } else {
        for (let o = 0; o < nOut; o++) {
          const s0 = src + o * fr;
          const e = Math.min(fr, nRange - o * fr);
          let m = vol[dst + o];
          for (let k = 0; k < e; k++) {
            const v = values[s0 + k];
            if (v >= lo) {
              const t = (v - lo) * sc;
              const b = t >= 254 ? 255 : (t + 1.5) | 0;
              if (b > m) m = b;
            }
          }
          vol[dst + o] = m;
        }
      }
      if ((ai & 7) === 7 && performance.now() - t0 > SLICE_MS) {
        await yieldToMain();
        if (g !== packGen || destroyed) return false;
        t0 = performance.now();
      }
    }

    packedMeta.set(entry.sweep, {
      slot,
      elevDeg: L.elevOf.get(entry.sweep) ?? 0,
      nAzOut,
      rangeStepKm: sweepRangeStepKm(entry) * fr,
      nOut,
      maxRangeKm: entry.maxRange / 1000,
    });
    return true;
  }

  function sweepRangeStepKm(e: SweepDataEntry): number {
    return e.range.length > 1
      ? (e.range[e.range.length - 1] - e.range[0]) / (e.range.length - 1) / 1000
      : e.maxRange / e.nRange / 1000;
  }

  function ensureVolumeTexture(): boolean {
    if (!gl || !layout) return false;
    if (volumeTex && texAllocated) return true;
    if (volumeTex) gl.deleteTexture(volumeTex);
    volumeTex = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, volumeTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R8, layout.W, layout.nSlots * layout.slotRows, 0, gl.RED, gl.UNSIGNED_BYTE, null);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
    texAllocated = true;
    return true;
  }

  /** Upload one slot in bounded slabs, yielding between them. */
  async function uploadSlot(idx: number, g: number) {
    const m = packedMeta.get(idx);
    if (!m || !layout || !cpuVol) return;
    const W = layout.W;
    const rowsPerSlab = Math.max(16, Math.floor(SLAB_BYTES / W));
    const base = m.slot * layout.slotRows;
    for (let r = 0; r < m.nAzOut; r += rowsPerSlab) {
      if (!gl || g !== packGen || destroyed) return;   // context lost: reuploadAll() handles it
      if (!ensureVolumeTexture()) return;
      const n = Math.min(rowsPerSlab, m.nAzOut - r);
      gl.bindTexture(gl.TEXTURE_2D, volumeTex);
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, base + r, W, n, gl.RED, gl.UNSIGNED_BYTE,
        cpuVol.subarray((base + r) * W, (base + r + n) * W));
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
      if (r + rowsPerSlab < m.nAzOut) await yieldToMain();
    }
  }

  /** After a context restore every GPU object is gone: re-upload from the CPU copy. */
  async function reuploadAll() {
    const g = packGen;
    texAllocated = false;
    volumeTex = null;
    for (const idx of [...packedMeta.keys()]) {
      if (g !== packGen || destroyed) return;
      await uploadSlot(idx, g);
      await yieldToMain();
    }
    publish();
  }

  /** At most one publish per PUBLISH_MIN_MS, never mid-drag; immediately once everything landed. */
  function schedulePublish() {
    if (destroyed) return;
    const allDone = pendingPack.length === 0 && outstanding.size === 0;
    if (publishTimer) {
      if (!allDone) return;
      clearTimeout(publishTimer);
      publishTimer = null;
    }
    const wait = allDone ? 0 : Math.max(0, PUBLISH_MIN_MS - (performance.now() - lastPublishAt));
    publishTimer = setTimeout(runPublish, wait);
  }
  function runPublish() {
    publishTimer = null;
    if (isDragging) { publishTimer = setTimeout(runPublish, 150); return; }
    publish();
  }

  /** Make newly packed sweeps visible: tiny metadata + elevation-lookup textures, then redraw. */
  function publish() {
    lastPublishAt = performance.now();
    if (!gl || !layout || !volumeTex || packedMeta.size === 0) return;
    const metas = [...packedMeta.values()].sort((a, b) => a.elevDeg - b.elevDeg);
    const n = metas.length;
    const metaData = new Float32Array(2 * 4 * n);
    let maxKm = 1;
    metas.forEach((m, i) => {
      const o = i * 8;
      metaData[o]     = m.elevDeg;
      metaData[o + 1] = m.slot * layout!.slotRows;
      metaData[o + 2] = m.nAzOut;
      metaData[o + 3] = m.rangeStepKm;
      metaData[o + 4] = m.nOut;
      if (m.maxRangeKm > maxKm) maxKm = m.maxRangeKm;
    });
    volMaxNRange = layout.W;
    volTotalAz   = layout.nSlots * layout.slotRows;
    volNSweeps   = n;
    volMaxRange  = maxKm;

    if (sweepMetaTex) gl.deleteTexture(sweepMetaTex);
    sweepMetaTex = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, sweepMetaTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA32F, 2, n, 0, gl.RGBA, gl.FLOAT, metaData);
    buildAngleIndex(metas);

    if (cameraInitKey !== loadKey) { cameraInitKey = loadKey; resetCamera(); }
    hasRendered = true;
    errorMsg = '';
    governor.setVolumeMB(layout.W * layout.nSlots * layout.slotRows / 1048576);
    syncCounts();
    markDirty();
  }

  // ── AngleIndex texture ────────────────────────────────────────────────────
  // Maps elevation in [elevMin, elevMax] → fractional sweep index.
  // floor(value) = lower sweep, fract(value) = interpolation weight.
  // -1 = below radar floor.

  function buildAngleIndex(metas: PackedMeta[]) {
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

  // ── Render (driven by ProgressiveRenderer: one frame = several banded draws) ─

  const frameJob: ProgressiveJob = {
    begin(params, w, h) {
      if (!gl || !prog || !quadVAO || !volumeTex || !angleIdxTex || !sweepMetaTex || !tfTex || volNSweeps === 0) return false;
      const g = gl;

      // Smooth camera animation — ease toward the targets by elapsed time, not by frame count, so
      // a slow GPU (a few frames per second) still converges in a couple of frames instead of
      // animating for a minute.
      const nowT = performance.now();
      const gap = nowT - lastAnimT;
      lastAnimT = nowT;
      const dt = gap > 250 ? 16 : gap;   // first frame after idle: one ordinary step
      const lf = 1 - Math.exp(-dt / 90);
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
        pr?.noteInteraction(); // still moving: coarse frames now, one settled frame at the end
      }

      const { eye, fwd, right, up } = getCamVectors();

      // Compass: project world North (0,1,0) onto screen axes
      const nx = dot3([0,1,0], right);
      const ny = dot3([0,1,0], up);
      const ang = Math.atan2(nx, ny) * 180 / Math.PI;
      if (Math.abs(ang - compassAngle) > 0.25) compassAngle = ang;

      const fov = Math.PI / 4;
      const asp = w / h;
      const tanY = Math.tan(fov / 2);
      const tanX = tanY * asp;
      const u = (n: string) => uloc[n];

      g.useProgram(prog);
      g.uniform3f(u('u_camPos'),        eye[0],   eye[1],   eye[2]);
      g.uniform3f(u('u_camRight'),    right[0], right[1], right[2]);
      g.uniform3f(u('u_camUp'),          up[0],    up[1],    up[2]);
      g.uniform3f(u('u_camFwd'),        fwd[0],   fwd[1],   fwd[2]);
      g.uniform1f(u('u_tanHalfFovX'), tanX);
      g.uniform1f(u('u_tanHalfFovY'), tanY);
      g.uniform2f(u('u_resolution'),  w, h);

      g.uniform1f(u('u_maxRange'),     volMaxRange);
      g.uniform1f(u('u_stepSize'),     stepKm * params.stepMul);
      g.uniform1f(u('u_stepUser'),     stepKm);
      g.uniform1i(u('u_maxSteps'),     params.maxSteps);
      // Class codes must never be blended between neighbours: categorical variables sample nearest.
      g.uniform1i(u('u_interp'),       categories ? 0 : params.interp);
      g.uniform1i(u('u_nSweeps'),      volNSweeps);
      g.uniform1f(u('u_volTotalAz'),   volTotalAz);
      g.uniform1f(u('u_volMaxNRange'), volMaxNRange);
      g.uniform1f(u('u_opacityScale'), opacityScale);
      g.uniform1f(u('u_elevMin'),      _elevMin);
      g.uniform1f(u('u_elevMax'),      _elevMax);

      const clip = $selectedBox;
      g.uniform1i(u('u_useClip'), clip ? 1 : 0);
      if (clip) {
        g.uniform1f(u('u_clipMinX'), clip.xMin / 1000);
        g.uniform1f(u('u_clipMinY'), clip.yMin / 1000);
        g.uniform1f(u('u_clipMaxX'), clip.xMax / 1000);
        g.uniform1f(u('u_clipMaxY'), clip.yMax / 1000);
      }
      return true;
    },
    drawBand() {
      const g = gl!;
      g.clear(g.COLOR_BUFFER_BIT);
      g.useProgram(prog);
      g.bindVertexArray(quadVAO);
      g.activeTexture(g.TEXTURE0); g.bindTexture(g.TEXTURE_2D, volumeTex);    g.uniform1i(uloc['u_volume'], 0);
      g.activeTexture(g.TEXTURE1); g.bindTexture(g.TEXTURE_2D, angleIdxTex);  g.uniform1i(uloc['u_angleIdx'], 1);
      g.activeTexture(g.TEXTURE2); g.bindTexture(g.TEXTURE_2D, sweepMetaTex); g.uniform1i(uloc['u_sweepMeta'], 2);
      g.activeTexture(g.TEXTURE3); g.bindTexture(g.TEXTURE_2D, tfTex);        g.uniform1i(uloc['u_transferFn'], 3);
      g.drawArrays(g.TRIANGLES, 0, 6);
      g.bindVertexArray(null);
    },
  };

  // ── Resize ────────────────────────────────────────────────────────────────

  function resizeCanvas() {
    if (!canvas || !container) return;
    const rect = container.getBoundingClientRect();
    const dpr  = Math.min(window.devicePixelRatio || 1, governor.dprCap);
    const w = Math.max(1, Math.round(rect.width  * dpr));
    const h = Math.max(1, Math.round(rect.height * dpr));
    // Assigning width/height clears the canvas even if unchanged: skip no-op resizes.
    if (w === canvas.width && h === canvas.height) return;
    canvas.width  = w;
    canvas.height = h;
    canvas.style.width  = rect.width  + 'px';
    canvas.style.height = rect.height + 'px';
    // The resize cleared the canvas: put the last image back right away, refine afterwards.
    pr?.presentLast();
    interact();
  }

  // ── Data loading ──────────────────────────────────────────────────────────

  $effect(() => {
    const v = variable;
    const ok = connected;
    const list = applicable;
    const fp = $radarData.filePath ?? '';
    if (!v || !ok || list.length === 0) return;
    untrack(() => syncLoader(v, fp, list));
  });

  function syncLoader(v: string, fp: string, list: typeof applicable) {
    const key = `${fp}|${v}`;
    if (key !== loadKey) {
      loadKey = key;
      resetVolume();
      if (v !== prevVariable) {
        prevVariable = v;
        cmap = getDefaultCmap(v);
        const range = getDefaultRange(v);
        if (range) { vmin = range[0]; vmax = range[1]; }
        tfData = buildTfData(cmap, !!styleFor(v)?.categories);
        uploadTransferFn();
      }
      packVmin = vmin;
      packVmax = vmax;
    } else if (applicableSet.size !== list.length) {
      // The sweep list changed under us (more sweeps announced): slot layout must follow.
      if (layout) {
        applicableSet = new Set(list.map((s) => s.index));
        relayoutAll();
      }
    }
    applicableSet = new Set(list.map((s) => s.index));

    // Lowest-elevation sweeps first — they cover the most ground area. Only the lowest is
    // 'visible'; the rest queue at background priority behind anything the user is looking at.
    let first = true;
    for (const s of list) {
      const isLowest = first;
      first = false;
      if (loadedSweeps.has(s.index) || outstanding.has(s.index) || failed.has(s.index)) continue;
      const cached = getCachedSweep(v, s.index);
      if (cached) { ingest(cached); continue; }
      requestSweep(v, s.index, isLowest);
    }
    syncCounts();
    if (outstanding.size === 0 && pendingPack.length === 0) schedulePublish();
  }

  function requestSweep(v: string, index: number, visible: boolean) {
    outstanding.set(index, performance.now());
    wsManager.requestSweepData(v, index, undefined, { priority: visible ? 'visible' : 'background' });
    startTimeoutWatch();
  }

  function startTimeoutWatch() {
    if (timeoutTimer) return;
    timeoutTimer = setInterval(() => {
      const now = performance.now();
      const v = variable;
      for (const [idx, t] of [...outstanding]) {
        // Request deduplicated against an earlier one whose reply we missed: adopt it from the cache.
        const cached = v ? getCachedSweep(v, idx) : undefined;
        if (cached) { outstanding.delete(idx); ingest(cached); continue; }
        if (now - t > SWEEP_TIMEOUT_MS) { outstanding.delete(idx); failed.add(idx); }
      }
      syncCounts();
      if (outstanding.size === 0) {
        if (timeoutTimer) { clearInterval(timeoutTimer); timeoutTimer = null; }
        schedulePublish();
      }
    }, 1000);
  }

  function retryMissing() {
    const v = variable;
    if (!v) return;
    const missing = [...failed];
    failed = new Set();
    for (const idx of missing) {
      const cached = getCachedSweep(v, idx);
      if (cached) ingest(cached);
      else requestSweep(v, idx, false);
    }
    syncCounts();
  }

  function onSweepFailed(m: any) {
    try {
      const v = m?.variable;
      const idx = Number(m?.sweep ?? m?.sweep_index ?? m?.index);
      if (v !== undefined && v !== variable) return;
      if (!Number.isFinite(idx) || !applicableSet.has(idx)) return;
      outstanding.delete(idx);
      if (!loadedSweeps.has(idx)) failed.add(idx);
      syncCounts();
      if (outstanding.size === 0) schedulePublish();
    } catch { /* malformed message: ignore */ }
  }

  // ── Context loss ──────────────────────────────────────────────────────────

  let lostTimer: ReturnType<typeof setTimeout> | null = null;

  function onContextLost(e: Event) {
    e.preventDefault(); // lets 'webglcontextrestored' fire
    pr?.setLost(true);
    gl = null;
    prog = null; quadVAO = null;
    volumeTex = angleIdxTex = sweepMetaTex = tfTex = null;
    texAllocated = false;
    errorMsg = 'Graphics context lost; trying to recover…';
    if (lostTimer) clearTimeout(lostTimer);
    lostTimer = setTimeout(() => {
      if (!gl) fatalMsg = '3D is unavailable because the graphics context was lost.';
    }, 5000);
  }

  function onContextRestored() {
    if (destroyed || !canvas) return;
    if (lostTimer) { clearTimeout(lostTimer); lostTimer = null; }
    // Rebuild all GL objects; the renderer is recreated with the new context.
    pr?.dispose();
    pr = null;
    glInit = null;
    glFailed = false;
    void ensureGl().then((ok) => {
      if (!ok) return;
      errorMsg = '';
      void reuploadAll();
    });
  }

  function retry3d() {
    fatalMsg = '';
    errorMsg = '';
    glFailed = false;
    if (!gl) { pr?.dispose(); pr = null; glInit = null; void ensureGl().then((ok) => ok && void reuploadAll()); }
    else { pr?.resume(); }
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
    interact();
  }
  function onMouseUp() {
    const was = isDragging;
    isDragging = false; isShiftDrag = false;
    // Packing/publishing that waited for the drag can go now.
    if (was && (pendingPack.length > 0 || publishTimer)) schedulePublish();
  }
  function onWheel(e: WheelEvent) {
    e.preventDefault();
    e.stopPropagation();
    camDist = Math.max(10, Math.min(5000, camDist * (1 + e.deltaY * 0.001)));
    camDistTarget = camDist;  // don't fight manual zoom
    interact();
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
    interact();
  }

  function resetCamera() {
    camTheta      = -30; camPhi = 25;
    camDistTarget = volMaxRange * 1.8;
    camTgtTarget  = [0, 0, 0];
    interact();
  }

  // ── Lifecycle ─────────────────────────────────────────────────────────────

  $effect(() => {
    const clip = $selectedBox;
    untrack(() => markDirty());
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
    unsubGov = governor.state.subscribe((s) => { qState = s; });
    unsubData = wsManager.onMessage('sweep_data_ready', (entry: SweepDataEntry) => {
      if (entry.variable !== variable) return;
      outstanding.delete(entry.sweep);
      failed.delete(entry.sweep);
      ingest(entry);
    });
    // Emitted by the sweep scheduler when a request fails permanently; absent on older builds, in
    // which case the 30 s per-sweep timeout above is what ends the wait.
    try { unsubFail = wsManager.onMessage('sweep_request_failed', onSweepFailed); } catch { /* optional */ }
  });

  onDestroy(() => {
    destroyed = true;
    packGen++;
    unsubData?.();
    unsubFail?.();
    unsubGov?.();
    if (timeoutTimer) { clearInterval(timeoutTimer); timeoutTimer = null; }
    if (publishTimer) { clearTimeout(publishTimer); publishTimer = null; }
    if (lostTimer) { clearTimeout(lostTimer); lostTimer = null; }
    pr?.dispose();
    pr = null;
    governor.release();
    resizeObs?.disconnect();
    resizeObs = null;
    canvas?.removeEventListener('webglcontextlost', onContextLost);
    canvas?.removeEventListener('webglcontextrestored', onContextRestored);
    if (gl) {
      [volumeTex, angleIdxTex, sweepMetaTex, tfTex].forEach(t => t && gl!.deleteTexture(t));
      if (prog)    gl.deleteProgram(prog);
      if (quadVAO) gl.deleteVertexArray(quadVAO);
    }
    cpuVol = null;
  });
</script>

<div class="viewer-3d" data-quality={qState.level} data-idle-quality={qState.idleLevel}
     data-quality-mode={qState.mode} data-sweeps-received={receivedCount}
     data-sweeps-total={totalCount} data-sweeps-failed={failedCount}>
  {#if hasData}
    <div class="controls-3d">
      <span class="label-3d">3D Volume</span>
      {#if variable}<span class="var-badge" title={variable}>{variableLabel}</span>{/if}

      <div class="ctrl-group">
        <label for="step-ctrl">Step</label>
        <input id="step-ctrl" type="range" min="0.25" max="3" step="0.25"
               bind:value={stepKm} oninput={interact} />
        <span class="ctrl-val">{stepKm}km</span>
      </div>

      <div class="ctrl-group">
        <label for="opacity-ctrl">Opacity</label>
        <input id="opacity-ctrl" type="range" min="0.1" max="1" step="0.05"
               bind:value={opacityScale} oninput={interact} />
        <span class="ctrl-val">{Math.round(opacityScale * 100)}%</span>
      </div>

      <div class="ctrl-group">
        <label for="quality-ctrl">Quality</label>
        <select id="quality-ctrl" class="quality-sel" value={qState.mode} onchange={onQualityChange}
                title="Auto adapts to your GPU to keep the app responsive. High/Medium/Low set the preferred level.">
          {#each QUALITY_OPTIONS as o (o.value)}
            <option value={o.value}>{o.label}</option>
          {/each}
        </select>
        <span class="q-chip" class:weak={qState.weakRenderer}
              title={`GPU: ${qState.gpu || 'unknown'}\nFrame: ${qState.frameMsEma.toFixed(0)} ms (smoothed)\nRender scale: ${Math.round(qState.renderScale * 100)}%\nCeiling: ${qState.ceiling}`}>
          {qState.idleLevel}{qState.weakRenderer ? ' · software GPU' : ''}
        </span>
      </div>

      <button class="reset-btn" onclick={resetCamera} title="Reset camera (R)">Reset</button>
      <button class="reset-btn" class:active={showTfe} onclick={() => showTfe = !showTfe} title="Transfer function editor">TF</button>

      {#if $selectedBox}
        {@const bw = Math.round(Math.abs($selectedBox.xMax - $selectedBox.xMin) / 1000)}
        {@const bh = Math.round(Math.abs($selectedBox.yMax - $selectedBox.yMin) / 1000)}
        <span class="clip-badge" title="Active clip region">{bw}×{bh} km</span>
      {/if}

      {#if totalCount > 0}
        <span class="sweep-count" class:partial={failedCount > 0}
              title={packedCount < receivedCount ? 'Sweeps received / sweeps carrying this variable (volume still being built)' : 'Sweeps received / sweeps carrying this variable'}>
          {receivedCount}/{totalCount} sweeps
        </span>
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
        {#if categories}
          <div class="colorbar-wrap cat" role="list" aria-label="{variableLabel} categories">
            <span class="cat-title">{variableLabel}</span>
            {#each [...categories].reverse() as c (c.value)}
              <span class="cat-item" role="listitem">
                <span class="cat-swatch" style="background: {c.color}"></span>{c.label}
              </span>
            {/each}
          </div>
        {:else}
          <div class="colorbar-wrap">
            <span class="cb-label">{vmax.toFixed(1)}</span>
            <div class="cb-bar" style="background: {cmapGradient}"></div>
            <span class="cb-label">{vmin.toFixed(1)}</span>
            <span class="cb-var">{variableLabel}</span>
          </div>
        {/if}

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

      {#if isLoading && hasRendered}
        <div class="loading-pill" role="status"><span class="spinner sm"></span>Loading {receivedCount}/{totalCount} sweeps…</div>
      {/if}

      {#if failedCount > 0 && !isLoading}
        <div class="banner missing" role="status">
          <span>
            {#if receivedCount > 0}
              {failedCount} of {totalCount} sweep{totalCount === 1 ? '' : 's'} did not load; showing the {receivedCount} that did.
            {:else}
              No sweeps could be loaded.
            {/if}
          </span>
          <button class="banner-btn" onclick={retryMissing}>Retry missing</button>
        </div>
      {/if}

      {#if qState.hint && !fatalMsg}
        <div class="banner quality" role="status">
          <span>Reduced 3D quality to keep the app responsive</span>
          <button class="banner-btn" onclick={() => governor.keepMySetting()}>Keep my setting</button>
          <button class="banner-x" aria-label="Dismiss" onclick={() => governor.dismissHint()}>×</button>
        </div>
      {/if}

      {#if fatalMsg}
        <div class="overlay error fatal">
          <p>{fatalMsg}</p>
          <p class="sub">Switch to the 2D view to keep working, or try again.</p>
          <button class="banner-btn" onclick={retry3d}>Try again</button>
        </div>
      {:else if isLoading && !hasRendered}
        <div class="overlay">
          <div class="spinner"></div>
          <p>Loading {receivedCount}/{totalCount} sweeps…</p>
        </div>
      {:else if errorMsg}
        <div class="overlay error"><p>{errorMsg}</p></div>
      {:else if !hasRendered && receivedCount > 0}
        <div class="overlay">
          <div class="spinner"></div>
          <p>Building 3D volume…</p>
        </div>
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
  .colorbar-wrap.cat {
    align-items: flex-start;
    gap: 3px;
    padding: 6px 8px;
    background: rgba(15, 15, 26, 0.78);
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 4px;
    max-width: 190px;
  }
  .cat-title {
    font-size: 10px;
    font-weight: 600;
    color: rgba(255,255,255,0.9);
    margin-bottom: 2px;
  }
  .cat-item {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-size: 10px;
    color: rgba(255,255,255,0.85);
    white-space: nowrap;
  }
  .cat-swatch {
    flex: none;
    width: 11px;
    height: 11px;
    border-radius: 2px;
    border: 1px solid rgba(255,255,255,0.25);
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

  .quality-sel {
    font-size: 10px;
    padding: 1px 4px;
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    background: var(--bg-tertiary);
    color: var(--text-secondary);
  }
  .q-chip {
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--text-muted);
    padding: 1px 6px;
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    white-space: nowrap;
  }
  .q-chip.weak { color: var(--accent-warning, #d9a441); border-color: rgba(217,164,65,0.4); }
  .sweep-count.partial { color: var(--accent-warning, #d9a441); }

  .banner {
    position: absolute;
    left: 50%;
    transform: translateX(-50%);
    display: flex;
    align-items: center;
    gap: 8px;
    max-width: calc(100% - 24px);
    padding: 5px 10px;
    font-size: 11px;
    color: var(--text-primary, #fff);
    background: rgba(20, 22, 38, 0.92);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    z-index: 6;
    pointer-events: auto;
  }
  .loading-pill {
    position: absolute; top: 10px; left: 10px; z-index: 6;
    display: flex; align-items: center; gap: 6px;
    padding: 3px 8px; font-size: 10px;
    color: var(--text-secondary);
    background: rgba(20, 22, 38, 0.85);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    pointer-events: none;
  }
  .spinner.sm { width: 10px; height: 10px; border-width: 2px; }
  .banner.missing { top: 10px; }
  .banner.quality { bottom: 12px; }
  .banner-btn {
    font-size: 10px;
    padding: 2px 8px;
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    background: var(--bg-tertiary);
    color: var(--text-secondary);
    cursor: pointer;
    white-space: nowrap;
  }
  .banner-btn:hover { background: var(--accent-primary); color: white; border-color: var(--accent-primary); }
  .banner-x {
    border: none; background: none; color: var(--text-muted);
    font-size: 14px; line-height: 1; cursor: pointer; padding: 0 2px;
  }
  .overlay.fatal { pointer-events: auto; background: rgba(10, 10, 20, 0.7); text-align: center; }
  .overlay .sub { font-size: 11px; color: var(--text-muted); margin: 0; }
</style>
