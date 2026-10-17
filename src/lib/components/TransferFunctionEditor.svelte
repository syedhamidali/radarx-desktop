<script lang="ts">
  /**
   * TransferFunctionEditor — visual editor for volume rendering transfer functions.
   *
   * Displays a gradient preview bar, an opacity curve editor with draggable
   * control points, and preset buttons for common radar variable mappings.
   */
  import { onMount, onDestroy } from 'svelte';

  let {
    transferFunction = $bindable(new Uint8Array(256 * 4)),
    onchange,
  }: {
    transferFunction?: Uint8Array;
    onchange?: (detail: { transferFunction: Uint8Array }) => void;
  } = $props();

  const SVG_WIDTH = 300;
  const SVG_HEIGHT = 80;

  // Control points: { x: 0-255, y: 0-255 } for alpha mapping
  interface ControlPoint {
    x: number;
    y: number;
  }

  let controlPoints = $state<ControlPoint[]>([
    { x: 0, y: 0 },
    { x: 64, y: 40 },
    { x: 128, y: 128 },
    { x: 192, y: 200 },
    { x: 255, y: 255 },
  ]);

  let dragIndex = $state<number>(-1);
  let gradientCanvas = $state<HTMLCanvasElement>(undefined as any);
  let curveSvg = $state<SVGSVGElement>(undefined as any);

  /**
   * Pointer position in the SVG's own 300x80 units. The SVG is CSS-sized (100% wide), so it is
   * usually scaled and letterboxed: map through the screen CTM instead of subtracting the rect.
   */
  function toSvgUnits(e: MouseEvent): { x: number; y: number } | null {
    const ctm = curveSvg?.getScreenCTM();
    if (!ctm) return null;
    const p = new DOMPoint(e.clientX, e.clientY).matrixTransform(ctm.inverse());
    return { x: Math.max(0, Math.min(SVG_WIDTH, p.x)), y: Math.max(0, Math.min(SVG_HEIGHT, p.y)) };
  }

  // Build the alpha curve from control points (linear interpolation)
  function buildAlphaCurve(): Uint8Array {
    const alpha = new Uint8Array(256);
    const sorted = [...controlPoints].sort((a, b) => a.x - b.x);

    for (let i = 0; i < 256; i++) {
      // Find surrounding control points
      let lo = sorted[0];
      let hi = sorted[sorted.length - 1];
      for (let j = 0; j < sorted.length - 1; j++) {
        if (sorted[j].x <= i && sorted[j + 1].x >= i) {
          lo = sorted[j];
          hi = sorted[j + 1];
          break;
        }
      }
      if (lo.x === hi.x) {
        alpha[i] = lo.y;
      } else {
        const t = (i - lo.x) / (hi.x - lo.x);
        alpha[i] = Math.round(lo.y + t * (hi.y - lo.y));
      }
    }
    return alpha;
  }

  // Apply the alpha curve to the transfer function and dispatch
  function applyAlpha() {
    const alpha = buildAlphaCurve();
    const tf = new Uint8Array(transferFunction);
    for (let i = 0; i < 256; i++) {
      tf[i * 4 + 3] = alpha[i];
    }
    transferFunction = tf;
    onchange?.({ transferFunction: tf });
  }

  // Render the gradient preview
  function renderGradient() {
    if (!gradientCanvas) return;
    const ctx = gradientCanvas.getContext('2d');
    if (!ctx) return;
    const w = gradientCanvas.width;
    const h = gradientCanvas.height;

    // Draw checker pattern for transparency visualization
    const checkerSize = 4;
    for (let cy = 0; cy < h; cy += checkerSize) {
      for (let cx = 0; cx < w; cx += checkerSize) {
        const even = ((cx / checkerSize + cy / checkerSize) % 2) === 0;
        ctx.fillStyle = even ? '#1a1a2e' : '#0f0f1a';
        ctx.fillRect(cx, cy, checkerSize, checkerSize);
      }
    }

    // Draw the transfer function colors
    const imgData = ctx.createImageData(w, 1);
    for (let x = 0; x < w; x++) {
      const idx = Math.floor((x / w) * 255);
      const tfIdx = idx * 4;
      const px = x * 4;
      imgData.data[px + 0] = transferFunction[tfIdx + 0];
      imgData.data[px + 1] = transferFunction[tfIdx + 1];
      imgData.data[px + 2] = transferFunction[tfIdx + 2];
      imgData.data[px + 3] = transferFunction[tfIdx + 3];
    }
    for (let y = 0; y < h; y++) {
      ctx.putImageData(imgData, 0, y);
    }
  }

  // Reactively render gradient when transfer function changes
  $effect(() => {
    if (gradientCanvas && transferFunction) {
      renderGradient();
    }
  });

  // SVG coordinate helpers
  function cpToSvgX(cp: ControlPoint): number {
    return (cp.x / 255) * SVG_WIDTH;
  }

  function cpToSvgY(cp: ControlPoint): number {
    return SVG_HEIGHT - (cp.y / 255) * SVG_HEIGHT;
  }

  // Build SVG path for the alpha curve fill
  const alphaCurvePath = $derived((() => {
    const sorted = [...controlPoints].sort((a, b) => a.x - b.x);
    if (sorted.length === 0) return '';
    let d = `M 0 ${SVG_HEIGHT}`;
    d += ` L ${cpToSvgX(sorted[0])} ${cpToSvgY(sorted[0])}`;
    for (let i = 1; i < sorted.length; i++) {
      d += ` L ${cpToSvgX(sorted[i])} ${cpToSvgY(sorted[i])}`;
    }
    d += ` L ${SVG_WIDTH} ${SVG_HEIGHT} Z`;
    return d;
  })());

  // Build SVG path for the alpha curve line
  const alphaCurveLinePath = $derived((() => {
    const sorted = [...controlPoints].sort((a, b) => a.x - b.x);
    if (sorted.length === 0) return '';
    let d = `M ${cpToSvgX(sorted[0])} ${cpToSvgY(sorted[0])}`;
    for (let i = 1; i < sorted.length; i++) {
      d += ` L ${cpToSvgX(sorted[i])} ${cpToSvgY(sorted[i])}`;
    }
    return d;
  })());

  // Drag handling
  function onPointDown(e: MouseEvent, index: number) {
    e.preventDefault();
    e.stopPropagation();
    dragIndex = index;
    window.addEventListener('mousemove', onDragMove);
    window.addEventListener('mouseup', onDragEnd);
  }

  function onDragMove(e: MouseEvent) {
    if (dragIndex < 0) return;
    const pt = toSvgUnits(e);
    if (!pt) return;
    const svgX = pt.x;
    const svgY = pt.y;

    const newX = Math.round((svgX / SVG_WIDTH) * 255);
    const newY = Math.round((1 - svgY / SVG_HEIGHT) * 255);

    // Don't allow moving first/last point's X
    if (dragIndex === 0) {
      controlPoints[0] = { x: 0, y: Math.max(0, Math.min(255, newY)) };
    } else if (dragIndex === controlPoints.length - 1) {
      controlPoints[controlPoints.length - 1] = { x: 255, y: Math.max(0, Math.min(255, newY)) };
    } else {
      controlPoints[dragIndex] = {
        x: Math.max(1, Math.min(254, newX)),
        y: Math.max(0, Math.min(255, newY)),
      };
    }
    controlPoints = controlPoints; // trigger reactivity
    applyAlpha();
  }

  function onDragEnd() {
    dragIndex = -1;
    window.removeEventListener('mousemove', onDragMove);
    window.removeEventListener('mouseup', onDragEnd);
  }

  // Click on SVG background to add new control point
  function onSvgClick(e: MouseEvent) {
    if (dragIndex >= 0) return;
    const pt = toSvgUnits(e);
    if (!pt) return;
    const svgX = pt.x;
    const svgY = pt.y;

    const newX = Math.round((svgX / SVG_WIDTH) * 255);
    const newY = Math.round((1 - svgY / SVG_HEIGHT) * 255);

    controlPoints = [...controlPoints, { x: Math.max(1, Math.min(254, newX)), y: Math.max(0, Math.min(255, newY)) }];
    applyAlpha();
  }

  // Remove an interior control point (the two end points always stay).
  function removePoint(index: number) {
    if (index <= 0 || index >= controlPoints.length - 1) return;
    controlPoints = controlPoints.filter((_, i) => i !== index);
    applyAlpha();
  }

  function onPointKey(e: KeyboardEvent, index: number) {
    const big = e.shiftKey ? 10 : 2;
    const last = controlPoints.length - 1;
    const cp = controlPoints[index];
    let next: ControlPoint | null = null;
    if (e.key === 'ArrowUp') next = { x: cp.x, y: Math.min(255, cp.y + big) };
    else if (e.key === 'ArrowDown') next = { x: cp.x, y: Math.max(0, cp.y - big) };
    else if (e.key === 'ArrowLeft' && index > 0 && index < last) next = { x: Math.max(1, cp.x - big), y: cp.y };
    else if (e.key === 'ArrowRight' && index > 0 && index < last) next = { x: Math.min(254, cp.x + big), y: cp.y };
    else if (e.key === 'Delete' || e.key === 'Backspace') {
      e.preventDefault();
      removePoint(index);
      return;
    }
    if (!next) return;
    e.preventDefault();
    controlPoints[index] = next;
    controlPoints = controlPoints;
    applyAlpha();
  }

  // ---- Presets ----

  function setColorsRGB(tf: Uint8Array, index: number, r: number, g: number, b: number) {
    tf[index * 4 + 0] = r;
    tf[index * 4 + 1] = g;
    tf[index * 4 + 2] = b;
  }

  function lerp(a: number, b: number, t: number): number {
    return Math.round(a + (b - a) * t);
  }

  function applyPresetReflectivity() {
    const tf = new Uint8Array(256 * 4);
    for (let i = 0; i < 256; i++) {
      const t = i / 255;
      let r: number, g: number, b: number, a: number;
      if (t < 0.15) {
        // Below detection — transparent
        r = 20; g = 20; b = 40; a = 0;
      } else if (t < 0.35) {
        // Low (greens, ~20-35 dBZ) — semi-transparent
        const lt = (t - 0.15) / 0.2;
        r = lerp(0, 50, lt); g = lerp(100, 200, lt); b = lerp(0, 50, lt);
        a = lerp(30, 120, lt);
      } else if (t < 0.55) {
        // Mid (yellows/oranges, ~35-50 dBZ) — opaque
        const lt = (t - 0.35) / 0.2;
        r = lerp(200, 255, lt); g = lerp(200, 140, lt); b = lerp(0, 0, lt);
        a = lerp(160, 230, lt);
      } else {
        // High (reds, 50+ dBZ) — fully opaque
        const lt = (t - 0.55) / 0.45;
        r = lerp(255, 180, lt); g = lerp(50, 0, lt); b = lerp(0, 40, lt);
        a = lerp(230, 255, lt);
      }
      tf[i * 4 + 0] = r; tf[i * 4 + 1] = g; tf[i * 4 + 2] = b; tf[i * 4 + 3] = a;
    }
    transferFunction = tf;
    controlPoints = [
      { x: 0, y: 0 }, { x: 38, y: 0 }, { x: 64, y: 30 },
      { x: 128, y: 120 }, { x: 180, y: 230 }, { x: 255, y: 255 },
    ];
    onchange?.({ transferFunction: tf });
  }

  function applyPresetVelocity() {
    const tf = new Uint8Array(256 * 4);
    for (let i = 0; i < 256; i++) {
      const t = i / 255;
      let r: number, g: number, b: number;
      if (t < 0.5) {
        // Negative — blue to white
        const lt = t / 0.5;
        r = lerp(0, 240, lt); g = lerp(60, 240, lt); b = lerp(220, 255, lt);
      } else {
        // Positive — white to red
        const lt = (t - 0.5) / 0.5;
        r = lerp(240, 220, lt); g = lerp(240, 30, lt); b = lerp(255, 30, lt);
      }
      const a = 140; // semi-transparent throughout
      tf[i * 4 + 0] = r; tf[i * 4 + 1] = g; tf[i * 4 + 2] = b; tf[i * 4 + 3] = a;
    }
    transferFunction = tf;
    controlPoints = [
      { x: 0, y: 140 }, { x: 64, y: 140 }, { x: 128, y: 140 },
      { x: 192, y: 140 }, { x: 255, y: 140 },
    ];
    onchange?.({ transferFunction: tf });
  }

  function applyPresetFullOpacity() {
    const tf = new Uint8Array(transferFunction);
    // Keep existing colors, set alpha to 255 for anything above index 10
    for (let i = 0; i < 256; i++) {
      tf[i * 4 + 3] = i < 10 ? 0 : 255;
    }
    transferFunction = tf;
    controlPoints = [
      { x: 0, y: 0 }, { x: 10, y: 0 }, { x: 11, y: 255 }, { x: 255, y: 255 },
    ];
    onchange?.({ transferFunction: tf });
  }

  function applyPresetTransparentCore() {
    const tf = new Uint8Array(transferFunction);
    // Invert: high values transparent, low values opaque
    for (let i = 0; i < 256; i++) {
      tf[i * 4 + 3] = i < 10 ? 0 : Math.round(255 * (1 - (i - 10) / 245));
    }
    transferFunction = tf;
    controlPoints = [
      { x: 0, y: 0 }, { x: 10, y: 255 }, { x: 128, y: 128 },
      { x: 200, y: 30 }, { x: 255, y: 0 },
    ];
    onchange?.({ transferFunction: tf });
  }

  onMount(() => {
    renderGradient();
  });

  // Do not leave window listeners behind if the editor unmounts mid-drag.
  onDestroy(() => {
    window.removeEventListener('mousemove', onDragMove);
    window.removeEventListener('mouseup', onDragEnd);
  });
</script>

<div class="tfe-root">
  <!-- Gradient preview bar -->
  <div class="tfe-gradient-wrapper">
    <canvas
      class="tfe-gradient"
      bind:this={gradientCanvas}
      width={SVG_WIDTH}
      height="20"
    ></canvas>
  </div>

  <!-- Opacity curve editor -->
  <div class="tfe-curve-wrapper">
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <svg
      class="tfe-curve-svg"
      bind:this={curveSvg}
      width={SVG_WIDTH}
      height={SVG_HEIGHT}
      viewBox="0 0 {SVG_WIDTH} {SVG_HEIGHT}"
      onclick={onSvgClick}
    >
      <!-- Grid lines -->
      {#each [0.25, 0.5, 0.75] as frac}
        <line
          x1="0" y1={SVG_HEIGHT * frac}
          x2={SVG_WIDTH} y2={SVG_HEIGHT * frac}
          class="tfe-grid-line"
        />
        <line
          x1={SVG_WIDTH * frac} y1="0"
          x2={SVG_WIDTH * frac} y2={SVG_HEIGHT}
          class="tfe-grid-line"
        />
      {/each}

      <!-- Alpha fill -->
      <path d={alphaCurvePath} class="tfe-alpha-fill" />

      <!-- Alpha line -->
      <path d={alphaCurveLinePath} class="tfe-alpha-line" />

      <!-- Control points -->
      {#each controlPoints as cp, i}
        <!-- svelte-ignore a11y_no_static_element_interactions -->
        <circle
          cx={cpToSvgX(cp)}
          cy={cpToSvgY(cp)}
          r="5"
          class="tfe-control-point"
          class:dragging={dragIndex === i}
          role="slider"
          tabindex="0"
          aria-label="Opacity point {i + 1}: value {cp.x}, opacity {cp.y}. Arrow keys to move, Delete to remove."
          aria-valuenow={cp.y}
          aria-valuemin="0"
          aria-valuemax="255"
          onmousedown={(e) => onPointDown(e, i)}
          onclick={(e) => e.stopPropagation()}
          ondblclick={(e) => { e.stopPropagation(); removePoint(i); }}
          oncontextmenu={(e) => { e.preventDefault(); removePoint(i); }}
          onkeydown={(e) => onPointKey(e, i)}
        />
      {/each}
    </svg>
    <div class="tfe-curve-labels">
      <span>0</span>
      <span>255</span>
    </div>
  </div>

  <!-- Presets -->
  <div class="tfe-presets">
    <span class="tfe-presets-label">Presets</span>
    <div class="tfe-preset-btns">
      <button class="tfe-preset" onclick={applyPresetReflectivity}>Reflectivity</button>
      <button class="tfe-preset" onclick={applyPresetVelocity}>Velocity</button>
      <button class="tfe-preset" onclick={applyPresetFullOpacity}>Full Opacity</button>
      <button class="tfe-preset" onclick={applyPresetTransparentCore}>Transparent Core</button>
    </div>
  </div>
</div>

<style>
  .tfe-root {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm, 6px);
  }

  /* Gradient bar */
  .tfe-gradient-wrapper {
    border: 1px solid var(--border-color, rgba(255,255,255,0.1));
    border-radius: var(--radius-sm, 4px);
    overflow: hidden;
    line-height: 0;
  }

  .tfe-gradient {
    display: block;
    width: 100%;
    height: 20px;
  }

  /* Curve editor */
  .tfe-curve-wrapper {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .tfe-curve-svg {
    width: 100%;
    height: 80px;
    background: var(--bg-primary, #0f0f1a);
    border: 1px solid var(--border-color, rgba(255,255,255,0.1));
    border-radius: var(--radius-sm, 4px);
    cursor: crosshair;
    overflow: visible; /* end-point handles sit on the border: keep them whole and clickable */
  }

  .tfe-grid-line {
    stroke: rgba(255, 255, 255, 0.04);
    stroke-width: 0.5;
  }

  .tfe-alpha-fill {
    fill: rgba(0, 220, 255, 0.12);
  }

  .tfe-alpha-line {
    fill: none;
    stroke: rgba(0, 220, 255, 0.6);
    stroke-width: 1.5;
    stroke-linejoin: round;
  }

  .tfe-control-point:focus-visible {
    outline: none;
    stroke: var(--accent-primary, #5b6cf7);
    stroke-width: 3;
  }

  .tfe-control-point {
    fill: rgba(0, 220, 255, 0.85);
    stroke: rgba(0, 220, 255, 0.4);
    stroke-width: 2;
    cursor: grab;
    transition: r 100ms ease;
  }

  .tfe-control-point:hover,
  .tfe-control-point.dragging {
    fill: #fff;
    stroke: rgba(0, 220, 255, 0.9);
    r: 6;
  }

  .tfe-control-point.dragging {
    cursor: grabbing;
  }

  .tfe-curve-labels {
    display: flex;
    justify-content: space-between;
    font-size: 9px;
    font-family: var(--font-mono, monospace);
    color: var(--text-muted, rgba(255,255,255,0.3));
    padding: 0 2px;
  }

  /* Presets */
  .tfe-presets {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs, 4px);
  }

  .tfe-presets-label {
    font-size: 10px;
    font-weight: 600;
    color: var(--text-muted, rgba(255,255,255,0.4));
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .tfe-preset-btns {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }

  .tfe-preset {
    padding: 3px 8px;
    font-size: 10px;
    font-weight: 600;
    border: 1px solid var(--border-color, rgba(255,255,255,0.1));
    border-radius: var(--radius-sm, 4px);
    background: rgba(15, 15, 26, 0.6);
    color: var(--text-secondary, rgba(255,255,255,0.6));
    cursor: pointer;
    transition: all 150ms ease;
    white-space: nowrap;
  }

  .tfe-preset:hover {
    background: rgba(91, 108, 247, 0.1);
    border-color: rgba(91, 108, 247, 0.25);
    color: var(--text-primary, #fff);
  }
</style>
