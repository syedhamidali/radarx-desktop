/**
 * WebGL2 Volume Ray Marching Renderer for 3D radar data visualization.
 *
 * Renders volumetric data using GPU-accelerated ray marching through a 3D texture.
 * Supports customizable transfer functions (value -> RGBA), interactive orbit camera
 * (drag to rotate, scroll to zoom, shift+drag to pan), and front-to-back compositing
 * with early ray termination.
 *
 * Architecture:
 *  - Unit cube geometry serves as the bounding box for the volume
 *  - Vertex shader passes world-space positions to the fragment shader
 *  - Fragment shader marches rays through a 3D texture, sampling and compositing
 *  - Transfer function encoded as a 256x1 RGBA lookup texture
 *  - Wireframe bounding box overlay for spatial reference
 *  - On-demand requestAnimationFrame scheduling: no frame loop while idle; coarser
 *    ray steps while the camera is being dragged, full quality once it settles
 *  - WebGL context-loss recovery (GPU state is rebuilt from CPU-side copies)
 *  - ResizeObserver for responsive canvas sizing
 */

// ─── Types ──────────────────────────────────────────────────────────────────

/** Spatial bounds of the volume in world coordinates. */
export interface VolumeBounds {
  xMin: number;
  xMax: number;
  yMin: number;
  yMax: number;
  zMin: number;
  zMax: number;
}

// ─── Shader Sources ─────────────────────────────────────────────────────────

const VOLUME_VERT_SRC = `#version 300 es
precision highp float;

in vec3 a_position;

uniform mat4 u_mvp;
uniform vec3 u_cameraPos;

out vec3 v_rayOrigin;
out vec3 v_position;

void main() {
  v_position = a_position;
  v_rayOrigin = u_cameraPos;
  gl_Position = u_mvp * vec4(a_position, 1.0);
}
`;

const VOLUME_FRAG_SRC = `#version 300 es
precision highp float;
precision highp sampler3D;

in vec3 v_rayOrigin;
in vec3 v_position;

uniform sampler3D u_volume;
uniform sampler2D u_transferFn;
uniform float u_stepSize;
uniform float u_alphaScale;

out vec4 fragColor;

/**
 * Ray-AABB intersection for the unit cube [0,1]^3.
 * Returns (tNear, tFar); if tNear > tFar the ray misses the box.
 */
vec2 intersectBox(vec3 orig, vec3 dir) {
  vec3 invDir = 1.0 / dir;
  vec3 tMin = -orig * invDir;
  vec3 tMax = (1.0 - orig) * invDir;
  vec3 t1 = min(tMin, tMax);
  vec3 t2 = max(tMin, tMax);
  float tNear = max(max(t1.x, t1.y), t1.z);
  float tFar  = min(min(t2.x, t2.y), t2.z);
  return vec2(tNear, tFar);
}

void main() {
  vec3 rayDir = normalize(v_position - v_rayOrigin);
  vec2 tHit = intersectBox(v_rayOrigin, rayDir);

  if (tHit.x > tHit.y) discard;

  tHit.x = max(tHit.x, 0.0);

  vec3 pos = v_rayOrigin + rayDir * tHit.x;
  vec4 color = vec4(0.0);

  for (float t = tHit.x; t < tHit.y; t += u_stepSize) {
    float val = texture(u_volume, pos).r;

    if (val > 0.001) {
      vec4 tfColor = texture(u_transferFn, vec2(val, 0.5));
      tfColor.a *= u_alphaScale * u_stepSize * 100.0;

      // Front-to-back compositing
      color.rgb += (1.0 - color.a) * tfColor.rgb * tfColor.a;
      color.a   += (1.0 - color.a) * tfColor.a;

      if (color.a > 0.99) break;
    }

    pos += rayDir * u_stepSize;
  }

  fragColor = color;
}
`;

const WIREFRAME_VERT_SRC = `#version 300 es
precision highp float;

in vec3 a_position;
uniform mat4 u_mvp;

void main() {
  gl_Position = u_mvp * vec4(a_position, 1.0);
}
`;

const WIREFRAME_FRAG_SRC = `#version 300 es
precision highp float;

uniform vec4 u_color;
out vec4 fragColor;

void main() {
  fragColor = u_color;
}
`;

// ─── Math Helpers ───────────────────────────────────────────────────────────

/** 4x4 matrix stored in column-major order (WebGL convention). */
type Mat4 = Float32Array;

function mat4Identity(): Mat4 {
  const m = new Float32Array(16);
  m[0] = m[5] = m[10] = m[15] = 1;
  return m;
}

function mat4Perspective(fovY: number, aspect: number, near: number, far: number): Mat4 {
  const m = new Float32Array(16);
  const f = 1.0 / Math.tan(fovY / 2);
  const rangeInv = 1.0 / (near - far);
  m[0] = f / aspect;
  m[5] = f;
  m[10] = (near + far) * rangeInv;
  m[11] = -1;
  m[14] = 2 * near * far * rangeInv;
  return m;
}

function mat4LookAt(eye: [number, number, number], center: [number, number, number], up: [number, number, number]): Mat4 {
  const zx = eye[0] - center[0], zy = eye[1] - center[1], zz = eye[2] - center[2];
  let len = Math.hypot(zx, zy, zz);
  const fz = [zx / len, zy / len, zz / len];

  // right = up x forward
  let rx = up[1] * fz[2] - up[2] * fz[1];
  let ry = up[2] * fz[0] - up[0] * fz[2];
  let rz = up[0] * fz[1] - up[1] * fz[0];
  len = Math.hypot(rx, ry, rz);
  rx /= len; ry /= len; rz /= len;

  // true up = forward x right
  const ux = fz[1] * rz - fz[2] * ry;
  const uy = fz[2] * rx - fz[0] * rz;
  const uz = fz[0] * ry - fz[1] * rx;

  const m = new Float32Array(16);
  m[0] = rx;  m[1] = ux;  m[2] = fz[0]; m[3] = 0;
  m[4] = ry;  m[5] = uy;  m[6] = fz[1]; m[7] = 0;
  m[8] = rz;  m[9] = uz;  m[10] = fz[2]; m[11] = 0;
  m[12] = -(rx * eye[0] + ry * eye[1] + rz * eye[2]);
  m[13] = -(ux * eye[0] + uy * eye[1] + uz * eye[2]);
  m[14] = -(fz[0] * eye[0] + fz[1] * eye[1] + fz[2] * eye[2]);
  m[15] = 1;
  return m;
}

function mat4Multiply(a: Mat4, b: Mat4): Mat4 {
  const out = new Float32Array(16);
  for (let col = 0; col < 4; col++) {
    for (let row = 0; row < 4; row++) {
      out[col * 4 + row] =
        a[row]      * b[col * 4] +
        a[4 + row]  * b[col * 4 + 1] +
        a[8 + row]  * b[col * 4 + 2] +
        a[12 + row] * b[col * 4 + 3];
    }
  }
  return out;
}

// ─── Geometry ───────────────────────────────────────────────────────────────

/** Unit cube [0,1]^3 with 36 vertices (12 triangles, 6 faces). */
function createCubeGeometry(): { vertices: Float32Array; indices: Uint16Array } {
  // 8 unique corner vertices
  const vertices = new Float32Array([
    0, 0, 0,  // 0
    1, 0, 0,  // 1
    1, 1, 0,  // 2
    0, 1, 0,  // 3
    0, 0, 1,  // 4
    1, 0, 1,  // 5
    1, 1, 1,  // 6
    0, 1, 1,  // 7
  ]);

  // 12 triangles (2 per face), winding so normals point inward
  // (we render back faces so the camera inside the cube sees the volume)
  const indices = new Uint16Array([
    // -Z face
    0, 2, 1,  0, 3, 2,
    // +Z face
    4, 5, 6,  4, 6, 7,
    // -Y face
    0, 1, 5,  0, 5, 4,
    // +Y face
    3, 6, 2,  3, 7, 6,
    // -X face
    0, 4, 7,  0, 7, 3,
    // +X face
    1, 2, 6,  1, 6, 5,
  ]);

  return { vertices, indices };
}

/** Line segments for the 12 edges of the unit cube. */
function createWireframeCubeGeometry(): { vertices: Float32Array; indices: Uint16Array } {
  const vertices = new Float32Array([
    0, 0, 0,  1, 0, 0,  1, 1, 0,  0, 1, 0,
    0, 0, 1,  1, 0, 1,  1, 1, 1,  0, 1, 1,
  ]);

  const indices = new Uint16Array([
    0, 1,  1, 2,  2, 3,  3, 0,  // bottom face
    4, 5,  5, 6,  6, 7,  7, 4,  // top face
    0, 4,  1, 5,  2, 6,  3, 7,  // vertical edges
  ]);

  return { vertices, indices };
}

// ─── Default Transfer Function ──────────────────────────────────────────────

/**
 * Generate a default radar-style transfer function (256 RGBA entries).
 *
 * Maps normalized data values to colors mimicking a reflectivity colormap:
 *  - < 0.1:   transparent (skip weak/no-data values)
 *  - 0.1-0.3: blue-green, low opacity (light precipitation)
 *  - 0.3-0.6: yellow-orange, medium opacity (moderate precipitation)
 *  - 0.6-1.0: red, high opacity (intense precipitation / hail)
 */
function createDefaultTransferFunction(): Uint8Array {
  const rgba = new Uint8Array(256 * 4);

  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    const offset = i * 4;

    if (t < 0.1) {
      // Transparent for weak/no-data values
      rgba[offset] = 0;
      rgba[offset + 1] = 0;
      rgba[offset + 2] = 0;
      rgba[offset + 3] = 0;
    } else if (t < 0.2) {
      // Blue-green fade in
      const s = (t - 0.1) / 0.1;
      rgba[offset] = 0;
      rgba[offset + 1] = Math.round(100 + 80 * s);
      rgba[offset + 2] = Math.round(200 - 50 * s);
      rgba[offset + 3] = Math.round(40 + 60 * s);
    } else if (t < 0.3) {
      // Green
      const s = (t - 0.2) / 0.1;
      rgba[offset] = Math.round(50 * s);
      rgba[offset + 1] = Math.round(180 + 40 * s);
      rgba[offset + 2] = Math.round(150 - 100 * s);
      rgba[offset + 3] = Math.round(100 + 30 * s);
    } else if (t < 0.45) {
      // Yellow
      const s = (t - 0.3) / 0.15;
      rgba[offset] = Math.round(50 + 205 * s);
      rgba[offset + 1] = Math.round(220 + 35 * s);
      rgba[offset + 2] = Math.round(50 - 50 * s);
      rgba[offset + 3] = Math.round(130 + 40 * s);
    } else if (t < 0.6) {
      // Orange
      const s = (t - 0.45) / 0.15;
      rgba[offset] = 255;
      rgba[offset + 1] = Math.round(255 - 130 * s);
      rgba[offset + 2] = 0;
      rgba[offset + 3] = Math.round(170 + 30 * s);
    } else if (t < 0.8) {
      // Red
      const s = (t - 0.6) / 0.2;
      rgba[offset] = Math.round(255 - 55 * s);
      rgba[offset + 1] = Math.round(125 - 125 * s);
      rgba[offset + 2] = Math.round(30 * s);
      rgba[offset + 3] = Math.round(200 + 30 * s);
    } else {
      // Deep red / magenta for extreme values
      const s = (t - 0.8) / 0.2;
      rgba[offset] = Math.round(200 + 55 * s);
      rgba[offset + 1] = 0;
      rgba[offset + 2] = Math.round(30 + 120 * s);
      rgba[offset + 3] = Math.round(230 + 25 * s);
    }
  }

  return rgba;
}

// ─── WebGL Helpers ──────────────────────────────────────────────────────────

function compileShader(gl: WebGL2RenderingContext, type: number, source: string): WebGLShader {
  const shader = gl.createShader(type);
  if (!shader) throw new Error('Failed to create shader');
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const info = gl.getShaderInfoLog(shader);
    gl.deleteShader(shader);
    throw new Error(`Shader compile error: ${info}`);
  }
  return shader;
}

function createProgram(gl: WebGL2RenderingContext, vertSrc: string, fragSrc: string): WebGLProgram {
  const vert = compileShader(gl, gl.VERTEX_SHADER, vertSrc);
  const frag = compileShader(gl, gl.FRAGMENT_SHADER, fragSrc);
  const program = gl.createProgram();
  if (!program) throw new Error('Failed to create program');
  gl.attachShader(program, vert);
  gl.attachShader(program, frag);
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    const info = gl.getProgramInfoLog(program);
    gl.deleteProgram(program);
    throw new Error(`Program link error: ${info}`);
  }
  // Shaders can be detached after linking
  gl.detachShader(program, vert);
  gl.detachShader(program, frag);
  gl.deleteShader(vert);
  gl.deleteShader(frag);
  return program;
}

// ─── VolumeRenderer ─────────────────────────────────────────────────────────

export class VolumeRenderer {
  // DOM
  private container: HTMLElement;
  private canvas: HTMLCanvasElement;
  private gl: WebGL2RenderingContext;

  // Programs
  private volumeProgram!: WebGLProgram;
  private wireframeProgram!: WebGLProgram;

  // Volume program uniforms
  private uMvpLoc: WebGLUniformLocation | null = null;
  private uCameraPosLoc: WebGLUniformLocation | null = null;
  private uStepSizeLoc: WebGLUniformLocation | null = null;
  private uAlphaScaleLoc: WebGLUniformLocation | null = null;
  private uVolumeLoc: WebGLUniformLocation | null = null;
  private uTransferFnLoc: WebGLUniformLocation | null = null;

  // Wireframe program uniforms
  private uWireMvpLoc: WebGLUniformLocation | null = null;
  private uWireColorLoc: WebGLUniformLocation | null = null;

  // Geometry
  private cubeVAO: WebGLVertexArrayObject | null = null;
  private cubeIndexCount = 0;
  private wireVAO: WebGLVertexArrayObject | null = null;
  private wireIndexCount = 0;

  // GL buffers (kept so they can be released on dispose)
  private glBuffers: WebGLBuffer[] = [];

  // Textures
  private volumeTexture: WebGLTexture | null = null;
  private transferTexture: WebGLTexture | null = null;

  // CPU-side copies used to rebuild GPU state after a context loss
  private volumeU8: Uint8Array | null = null;
  private transferRGBA: Uint8Array = createDefaultTransferFunction();
  /** Dimensions of the texture currently allocated on the GPU (to allow in-place updates). */
  private texDims: [number, number, number] = [0, 0, 0];
  private contextLost = false;

  // Volume metadata
  private volumeDims: [number, number, number] = [0, 0, 0];
  private bounds: VolumeBounds = { xMin: 0, xMax: 1, yMin: 0, yMax: 1, zMin: 0, zMax: 1 };
  private vmin = 0;
  private vmax = 1;
  private alphaScale = 1.0;
  private stepSize = 0.005;
  private hasVolume = false;

  // Camera (orbit in spherical coordinates)
  private camTheta = Math.PI / 4;   // azimuthal angle
  private camPhi = Math.PI / 3;     // polar angle from +Y
  private camRadius = 2.5;
  private camTarget: [number, number, number] = [0.5, 0.5, 0.5]; // orbit center in volume space

  // Interaction state
  private isDragging = false;
  private isShiftDrag = false;
  private lastMouseX = 0;
  private lastMouseY = 0;

  // Render scheduling
  private dirty = true;
  private animFrameId = 0;
  private resizeObserver: ResizeObserver | null = null;
  private disposed = false;
  /** True while the user is dragging / zooming: ray steps are coarsened for responsiveness. */
  private interacting = false;
  private settleTimer: ReturnType<typeof setTimeout> | null = null;

  // Bound event handlers (stored for removal on dispose)
  private onMouseDownBound: (e: MouseEvent) => void;
  private onMouseMoveBound: (e: MouseEvent) => void;
  private onMouseUpBound: (e: MouseEvent) => void;
  private onWheelBound: (e: WheelEvent) => void;
  private onContextMenuBound: (e: Event) => void;
  private onContextLostBound: (e: Event) => void;
  private onContextRestoredBound: () => void;

  constructor(container: HTMLElement) {
    this.container = container;

    // Create canvas
    this.canvas = document.createElement('canvas');
    this.canvas.style.width = '100%';
    this.canvas.style.height = '100%';
    this.canvas.style.display = 'block';
    this.container.appendChild(this.canvas);

    // Initialize WebGL2
    const gl = this.canvas.getContext('webgl2', {
      alpha: true,
      premultipliedAlpha: false,
      antialias: true,
      preserveDrawingBuffer: false,
    });
    if (!gl) {
      this.canvas.remove();
      throw new Error('WebGL2 is not supported in this browser');
    }
    this.gl = gl;

    this.setupGL();

    // Size the canvas to its container
    this.resizeCanvas();

    // Observe container resizes
    this.resizeObserver = new ResizeObserver(() => {
      if (this.resizeCanvas()) {
        // Resizing cleared the canvas, and this callback runs after the frame's
        // rAF phase: draw right away so no blank frame is presented.
        this.dirty = false;
        this.render();
      }
    });
    this.resizeObserver.observe(this.container);

    // Bind event listeners
    this.onMouseDownBound = this.onMouseDown.bind(this);
    this.onMouseMoveBound = this.onMouseMove.bind(this);
    this.onMouseUpBound = this.onMouseUp.bind(this);
    this.onWheelBound = this.onWheel.bind(this);
    this.onContextMenuBound = (e: Event) => e.preventDefault();
    this.onContextLostBound = (e: Event) => {
      e.preventDefault(); // allows 'webglcontextrestored' to fire
      this.contextLost = true;
      if (this.animFrameId) { cancelAnimationFrame(this.animFrameId); this.animFrameId = 0; }
    };
    this.onContextRestoredBound = () => {
      if (this.disposed) return;
      this.contextLost = false;
      this.setupGL();
      this.resizeCanvas(true);
      this.restoreGpuData();
      this.markDirty();
    };

    this.canvas.addEventListener('mousedown', this.onMouseDownBound);
    window.addEventListener('mousemove', this.onMouseMoveBound);
    window.addEventListener('mouseup', this.onMouseUpBound);
    this.canvas.addEventListener('wheel', this.onWheelBound, { passive: false });
    this.canvas.addEventListener('contextmenu', this.onContextMenuBound);
    this.canvas.addEventListener('webglcontextlost', this.onContextLostBound);
    this.canvas.addEventListener('webglcontextrestored', this.onContextRestoredBound);

    // First frame
    this.markDirty();
  }

  // ── GL resources ────────────────────────────────────────────────────────

  /** Compile programs and create static geometry / transfer texture (also used after context restore). */
  private setupGL(): void {
    const gl = this.gl;
    this.glBuffers = [];
    this.volumeTexture = null;
    this.texDims = [0, 0, 0];

    this.volumeProgram = createProgram(gl, VOLUME_VERT_SRC, VOLUME_FRAG_SRC);
    this.wireframeProgram = createProgram(gl, WIREFRAME_VERT_SRC, WIREFRAME_FRAG_SRC);
    this.cacheUniformLocations();
    this.initCubeGeometry();
    this.initWireframeGeometry();
    this.initTransferTexture();
  }

  /** Re-upload the volume and transfer function from CPU copies after a context restore. */
  private restoreGpuData(): void {
    if (this.volumeU8 && this.hasVolume) {
      this.uploadVolumeTexture(this.volumeU8, this.volumeDims[0], this.volumeDims[1], this.volumeDims[2]);
    }
  }

  // ── Uniform Locations ───────────────────────────────────────────────────

  private cacheUniformLocations(): void {
    const gl = this.gl;

    gl.useProgram(this.volumeProgram);
    this.uMvpLoc = gl.getUniformLocation(this.volumeProgram, 'u_mvp');
    this.uCameraPosLoc = gl.getUniformLocation(this.volumeProgram, 'u_cameraPos');
    this.uStepSizeLoc = gl.getUniformLocation(this.volumeProgram, 'u_stepSize');
    this.uAlphaScaleLoc = gl.getUniformLocation(this.volumeProgram, 'u_alphaScale');
    this.uVolumeLoc = gl.getUniformLocation(this.volumeProgram, 'u_volume');
    this.uTransferFnLoc = gl.getUniformLocation(this.volumeProgram, 'u_transferFn');

    gl.useProgram(this.wireframeProgram);
    this.uWireMvpLoc = gl.getUniformLocation(this.wireframeProgram, 'u_mvp');
    this.uWireColorLoc = gl.getUniformLocation(this.wireframeProgram, 'u_color');
  }

  // ── Geometry Setup ──────────────────────────────────────────────────────

  private initCubeGeometry(): void {
    const gl = this.gl;
    const { vertices, indices } = createCubeGeometry();

    this.cubeVAO = gl.createVertexArray();
    gl.bindVertexArray(this.cubeVAO);

    const vbo = gl.createBuffer();
    if (vbo) this.glBuffers.push(vbo);
    gl.bindBuffer(gl.ARRAY_BUFFER, vbo);
    gl.bufferData(gl.ARRAY_BUFFER, vertices, gl.STATIC_DRAW);

    const posLoc = gl.getAttribLocation(this.volumeProgram, 'a_position');
    gl.enableVertexAttribArray(posLoc);
    gl.vertexAttribPointer(posLoc, 3, gl.FLOAT, false, 0, 0);

    const ibo = gl.createBuffer();
    if (ibo) this.glBuffers.push(ibo);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ibo);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, indices, gl.STATIC_DRAW);

    this.cubeIndexCount = indices.length;
    gl.bindVertexArray(null);
  }

  private initWireframeGeometry(): void {
    const gl = this.gl;
    const { vertices, indices } = createWireframeCubeGeometry();

    this.wireVAO = gl.createVertexArray();
    gl.bindVertexArray(this.wireVAO);

    const vbo = gl.createBuffer();
    if (vbo) this.glBuffers.push(vbo);
    gl.bindBuffer(gl.ARRAY_BUFFER, vbo);
    gl.bufferData(gl.ARRAY_BUFFER, vertices, gl.STATIC_DRAW);

    const posLoc = gl.getAttribLocation(this.wireframeProgram, 'a_position');
    gl.enableVertexAttribArray(posLoc);
    gl.vertexAttribPointer(posLoc, 3, gl.FLOAT, false, 0, 0);

    const ibo = gl.createBuffer();
    if (ibo) this.glBuffers.push(ibo);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ibo);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, indices, gl.STATIC_DRAW);

    this.wireIndexCount = indices.length;
    gl.bindVertexArray(null);
  }

  // ── Texture Setup ─────────────────────────────────────────────────────

  private initTransferTexture(): void {
    const gl = this.gl;

    this.transferTexture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.transferTexture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);

    // Upload the current transfer function (the default one initially)
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, this.transferRGBA);
    gl.bindTexture(gl.TEXTURE_2D, null);
  }

  // ── Canvas Sizing ─────────────────────────────────────────────────────

  /** Resize the backing store to the container. Returns true if the size changed. */
  private resizeCanvas(force = false): boolean {
    // Ray marching cost scales with pixel count: cap the pixel ratio at 2.
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(1, Math.round(this.container.clientWidth * dpr));
    const height = Math.max(1, Math.round(this.container.clientHeight * dpr));

    // Assigning width/height clears the canvas even if the value is unchanged.
    if (!force && width === this.canvas.width && height === this.canvas.height) return false;
    this.canvas.width = width;
    this.canvas.height = height;
    if (!this.contextLost) this.gl.viewport(0, 0, width, height);
    return true;
  }

  // ── Public API ────────────────────────────────────────────────────────

  /**
   * Upload a 3D volume data grid. The flat Float32Array is indexed as
   * data[x + y * nx + z * nx * ny] (x varies fastest).
   */
  setVolumeData(
    data: Float32Array,
    nx: number,
    ny: number,
    nz: number,
    bounds: VolumeBounds,
  ): void {
    const gl = this.gl;
    const expectedLen = nx * ny * nz;
    if (data.length !== expectedLen) {
      throw new Error(
        `Volume data length (${data.length}) does not match dimensions ${nx}x${ny}x${nz} = ${expectedLen}`,
      );
    }

    this.volumeDims = [nx, ny, nz];
    this.bounds = { ...bounds };

    // Normalize to 8 bits: the transfer function is a 256-entry LUT, so 8 bits lose
    // nothing visible, the texture is 4x smaller than R32F, and R8 is always
    // linearly filterable in WebGL2 (R32F needs OES_texture_float_linear, which
    // was never requested here, so trilinear sampling could fail on some GPUs).
    const normalized = new Uint8Array(expectedLen);
    const range = this.vmax - this.vmin;
    const scale = range !== 0 ? 255 / range : 0;
    const vmin = this.vmin;

    for (let i = 0; i < expectedLen; i++) {
      const v = (data[i] - vmin) * scale;
      // NaN fails both comparisons -> stays 0 (no data); +/-Infinity clamp.
      normalized[i] = v >= 255 ? 255 : v > 0 ? (v + 0.5) | 0 : 0;
    }

    this.volumeU8 = normalized;
    this.uploadVolumeTexture(normalized, nx, ny, nz);

    // Compute an appropriate step size (~1/200 of the volume diagonal)
    const dx = bounds.xMax - bounds.xMin;
    const dy = bounds.yMax - bounds.yMin;
    const dz = bounds.zMax - bounds.zMin;
    const diagonal = Math.sqrt(dx * dx + dy * dy + dz * dz);
    // Step in normalized [0,1]^3 space, so divide by largest dimension extent
    const maxExtent = Math.max(dx, dy, dz);
    this.stepSize = (diagonal / maxExtent) / 200;

    this.hasVolume = true;
    this.markDirty();
  }

  /** Create / update the 3D texture from normalized 8-bit voxels. */
  private uploadVolumeTexture(voxels: Uint8Array, nx: number, ny: number, nz: number): void {
    if (this.contextLost) return;
    const gl = this.gl;

    const sameShape = this.texDims[0] === nx && this.texDims[1] === ny && this.texDims[2] === nz;
    if (!this.volumeTexture) this.volumeTexture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_3D, this.volumeTexture);

    // Tightly packed rows (R8 rows are not 4-byte aligned in general).
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    if (sameShape) {
      // Same size: update in place instead of reallocating GPU storage.
      gl.texSubImage3D(gl.TEXTURE_3D, 0, 0, 0, 0, nx, ny, nz, gl.RED, gl.UNSIGNED_BYTE, voxels);
    } else {
      gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_R, gl.CLAMP_TO_EDGE);
      gl.texImage3D(gl.TEXTURE_3D, 0, gl.R8, nx, ny, nz, 0, gl.RED, gl.UNSIGNED_BYTE, voxels);
      this.texDims = [nx, ny, nz];
    }
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
    gl.bindTexture(gl.TEXTURE_3D, null);
  }

  /**
   * Upload a custom transfer function. Must be exactly 1024 bytes (256 RGBA entries).
   * Each group of 4 bytes is [R, G, B, A] for the value at index/255.
   */
  setTransferFunction(rgba: Uint8Array): void {
    if (rgba.length !== 1024) {
      throw new Error(`Transfer function must be 256 * 4 = 1024 bytes, got ${rgba.length}`);
    }

    this.transferRGBA = rgba.slice(); // keep a copy for context-restore
    if (!this.contextLost) {
      const gl = this.gl;
      gl.bindTexture(gl.TEXTURE_2D, this.transferTexture);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, rgba);
      gl.bindTexture(gl.TEXTURE_2D, null);
    }

    this.markDirty();
  }

  /**
   * Set the data value range used for normalization.
   * Values outside [vmin, vmax] are clamped to 0 and 1 respectively.
   * Call this before setVolumeData if you want custom normalization.
   */
  setValueRange(vmin: number, vmax: number): void {
    if (vmin === this.vmin && vmax === this.vmax) return;
    this.vmin = vmin;
    this.vmax = vmax;
    this.markDirty();
  }

  /** Set the global opacity multiplier (default 1.0). */
  setAlphaScale(scale: number): void {
    const next = Math.max(0, scale);
    if (next === this.alphaScale) return;
    this.alphaScale = next;
    this.markDirty();
  }

  /** Reset the camera to the default orbit position. */
  resetCamera(): void {
    this.camTheta = Math.PI / 4;
    this.camPhi = Math.PI / 3;
    this.camRadius = 2.5;
    this.camTarget = [0.5, 0.5, 0.5];
    this.markDirty();
  }

  /** Trigger a single render pass (call automatically via dirty flag, but available publicly). */
  render(): void {
    if (this.disposed || this.contextLost) return;

    const gl = this.gl;
    const width = this.canvas.width;
    const height = this.canvas.height;
    if (width === 0 || height === 0) return;

    gl.viewport(0, 0, width, height);
    gl.clearColor(0.05, 0.05, 0.1, 1.0);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);

    // Build camera matrices
    const aspect = width / height;
    const proj = mat4Perspective(Math.PI / 4, aspect, 0.01, 100.0);

    const eyeX = this.camTarget[0] + this.camRadius * Math.sin(this.camPhi) * Math.cos(this.camTheta);
    const eyeY = this.camTarget[1] + this.camRadius * Math.cos(this.camPhi);
    const eyeZ = this.camTarget[2] + this.camRadius * Math.sin(this.camPhi) * Math.sin(this.camTheta);
    const eye: [number, number, number] = [eyeX, eyeY, eyeZ];

    // Flip up vector when camera is below the orbit equator to prevent gimbal lock
    const up: [number, number, number] = [0, this.camPhi < Math.PI / 2 ? 1 : -1, 0];
    const view = mat4LookAt(eye, this.camTarget, up);
    const mvp = mat4Multiply(proj, view);

    // Camera position in volume space [0,1]^3 (already in that space since cube is [0,1]^3)
    const camPos: [number, number, number] = [eyeX, eyeY, eyeZ];

    // Draw wireframe bounding box
    this.renderWireframe(mvp);

    // Draw volume (if data is loaded)
    if (this.hasVolume) {
      this.renderVolume(mvp, camPos);
    }
  }

  /** Clean up all WebGL resources and remove event listeners. */
  dispose(): void {
    this.disposed = true;

    if (this.animFrameId) {
      cancelAnimationFrame(this.animFrameId);
      this.animFrameId = 0;
    }

    if (this.settleTimer) {
      clearTimeout(this.settleTimer);
      this.settleTimer = null;
    }

    if (this.resizeObserver) {
      this.resizeObserver.disconnect();
      this.resizeObserver = null;
    }

    // Remove event listeners
    this.canvas.removeEventListener('mousedown', this.onMouseDownBound);
    window.removeEventListener('mousemove', this.onMouseMoveBound);
    window.removeEventListener('mouseup', this.onMouseUpBound);
    this.canvas.removeEventListener('wheel', this.onWheelBound);
    this.canvas.removeEventListener('contextmenu', this.onContextMenuBound);
    this.canvas.removeEventListener('webglcontextlost', this.onContextLostBound);
    this.canvas.removeEventListener('webglcontextrestored', this.onContextRestoredBound);

    const gl = this.gl;

    if (!this.contextLost) {
      // Delete textures
      if (this.volumeTexture) gl.deleteTexture(this.volumeTexture);
      if (this.transferTexture) gl.deleteTexture(this.transferTexture);

      // Delete VAOs and their buffers
      if (this.cubeVAO) gl.deleteVertexArray(this.cubeVAO);
      if (this.wireVAO) gl.deleteVertexArray(this.wireVAO);
      for (const b of this.glBuffers) gl.deleteBuffer(b);

      // Delete programs
      gl.deleteProgram(this.volumeProgram);
      gl.deleteProgram(this.wireframeProgram);
    }
    this.glBuffers = [];
    this.volumeU8 = null;

    // Remove canvas from DOM
    if (this.canvas.parentElement) {
      this.canvas.parentElement.removeChild(this.canvas);
    }
  }

  // ── Rendering ─────────────────────────────────────────────────────────

  private renderVolume(mvp: Mat4, camPos: [number, number, number]): void {
    const gl = this.gl;

    gl.useProgram(this.volumeProgram);

    // Enable blending for compositing
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

    // Cull back faces — renders front faces (the ones facing the camera) from any angle.
    // The fragment shader's intersectBox handles ray entry/exit regardless of which face runs.
    gl.enable(gl.CULL_FACE);
    gl.cullFace(gl.BACK);

    // Disable depth writing (volume compositing handles ordering)
    gl.depthMask(false);

    // Set uniforms
    gl.uniformMatrix4fv(this.uMvpLoc, false, mvp);
    gl.uniform3f(this.uCameraPosLoc, camPos[0], camPos[1], camPos[2]);
    // Coarser ray steps while orbiting/zooming: ~2.5x fewer samples per pixel.
    // (Opacity is step-size compensated in the shader, so the look stays the same.)
    gl.uniform1f(this.uStepSizeLoc, this.interacting ? this.stepSize * 2.5 : this.stepSize);
    gl.uniform1f(this.uAlphaScaleLoc, this.alphaScale);

    // Bind 3D volume texture to unit 0
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_3D, this.volumeTexture);
    gl.uniform1i(this.uVolumeLoc, 0);

    // Bind transfer function texture to unit 1
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, this.transferTexture);
    gl.uniform1i(this.uTransferFnLoc, 1);

    // Draw the cube
    gl.bindVertexArray(this.cubeVAO);
    gl.drawElements(gl.TRIANGLES, this.cubeIndexCount, gl.UNSIGNED_SHORT, 0);
    gl.bindVertexArray(null);

    // Restore state
    gl.depthMask(true);
    gl.disable(gl.CULL_FACE);
    gl.disable(gl.BLEND);
  }

  private renderWireframe(mvp: Mat4): void {
    const gl = this.gl;

    gl.useProgram(this.wireframeProgram);
    gl.uniformMatrix4fv(this.uWireMvpLoc, false, mvp);
    gl.uniform4f(this.uWireColorLoc, 0.5, 0.5, 0.5, 1.0);

    gl.bindVertexArray(this.wireVAO);
    gl.drawElements(gl.LINES, this.wireIndexCount, gl.UNSIGNED_SHORT, 0);
    gl.bindVertexArray(null);
  }

  // ── Render Loop ───────────────────────────────────────────────────────

  /** Request a redraw; at most one rAF is ever pending and nothing runs while idle. */
  private markDirty(): void {
    this.dirty = true;
    if (this.animFrameId || this.disposed || this.contextLost) return;
    this.animFrameId = requestAnimationFrame(this.renderLoop);
  }

  private renderLoop = (): void => {
    this.animFrameId = 0;
    if (this.disposed) return;

    if (this.dirty) {
      this.dirty = false;
      this.render();
    }
  };

  /** Mark the camera as moving; a full-quality frame follows once input pauses. */
  private beginInteraction(): void {
    this.interacting = true;
    if (this.settleTimer) clearTimeout(this.settleTimer);
    this.settleTimer = setTimeout(() => {
      this.settleTimer = null;
      this.interacting = false;
      this.markDirty();
    }, 150);
  }

  // ── Input Handlers ────────────────────────────────────────────────────

  private onMouseDown(e: MouseEvent): void {
    if (e.button === 0) {
      this.isDragging = true;
      this.isShiftDrag = e.shiftKey;
      this.lastMouseX = e.clientX;
      this.lastMouseY = e.clientY;
      e.preventDefault();
    }
  }

  private onMouseMove(e: MouseEvent): void {
    if (!this.isDragging) return;

    const dx = e.clientX - this.lastMouseX;
    const dy = e.clientY - this.lastMouseY;
    this.lastMouseX = e.clientX;
    this.lastMouseY = e.clientY;

    if (this.isShiftDrag || e.shiftKey) {
      // Pan: move the orbit target
      const panSpeed = 0.002 * this.camRadius;

      // Compute right and up vectors in world space from camera orientation
      const sinPhi = Math.sin(this.camPhi);
      const cosPhi = Math.cos(this.camPhi);
      const sinTheta = Math.sin(this.camTheta);
      const cosTheta = Math.cos(this.camTheta);

      // Right vector (perpendicular to view in XZ plane)
      const rx = sinTheta;
      const rz = -cosTheta;

      // Up vector (perpendicular to both view and right)
      const ux = -cosPhi * cosTheta;
      const uy = sinPhi;
      const uz = -cosPhi * sinTheta;

      this.camTarget[0] -= (dx * rx + dy * ux) * panSpeed;
      this.camTarget[1] -= dy * uy * panSpeed;
      this.camTarget[2] -= (dx * rz + dy * uz) * panSpeed;
    } else {
      // Orbit: rotate theta/phi
      const rotateSpeed = 0.005;
      this.camTheta -= dx * rotateSpeed;
      this.camPhi -= dy * rotateSpeed;

      // Clamp phi — leave a small margin at each pole to avoid degenerate up vector
      this.camPhi = Math.max(0.01, Math.min(Math.PI - 0.01, this.camPhi));
    }

    this.beginInteraction();
    this.markDirty();
  }

  private onMouseUp(e: MouseEvent): void {
    if (e.button === 0) {
      this.isDragging = false;
      this.isShiftDrag = false;
    }
  }

  private onWheel(e: WheelEvent): void {
    e.preventDefault();

    const zoomSpeed = 0.001;
    this.camRadius *= 1.0 + e.deltaY * zoomSpeed;
    this.camRadius = Math.max(0.3, Math.min(20.0, this.camRadius));

    this.beginInteraction();
    this.markDirty();
  }
}
