<script lang="ts">
  /**
   * ModuleOverlay — draws plugin-module overlay features (polygons, lines,
   * points; polar or xy frames) as SVG over the PPI canvas.
   *
   * Uses the same polar -> screen transform as StormOverlay, which mirrors
   * the WebGL vertex transform in ppiRenderer. Screen paths are computed in a
   * $derived that only depends on the overlay layers and the view parameters,
   * so selection changes never re-project geometry.
   */
  import {
    overlayLayers,
    selectedFeature,
    selectFeature,
    type OverlayFeature,
  } from '../stores/modules';

  interface Props {
    scale?: number;
    translateX?: number;
    translateY?: number;
    maxRange?: number;
    containerWidth?: number;
    containerHeight?: number;
  }

  let {
    scale = 1,
    translateX = 0,
    translateY = 0,
    maxRange = 1,
    containerWidth = 800,
    containerHeight = 600,
  }: Props = $props();

  interface DrawItem {
    key: string;
    module: string;
    id: string;
    kind: OverlayFeature['kind'];
    color: string;
    fill: number;
    d: string;
    points: { x: number; y: number }[];
    label: string | null;
    lx: number;
    ly: number;
  }

  /** Data-space metres (x east, y north) -> pixel, identical to StormOverlay.polarToScreen. */
  const project = $derived.by(() => {
    const aspect = containerWidth / containerHeight;
    const sx = (aspect >= 1 ? scale / aspect : scale) / maxRange;
    const sy = (aspect >= 1 ? scale : scale * aspect) / maxRange;
    const w = containerWidth;
    const h = containerHeight;
    const tx = translateX;
    const ty = translateY;
    return (dataX: number, dataY: number): { x: number; y: number } => {
      const ndcX = (dataX + tx) * sx;
      const ndcY = (dataY + ty) * sy;
      return { x: ((ndcX + 1) / 2) * w, y: ((1 - ndcY) / 2) * h };
    };
  });

  function toScreen(
    proj: (x: number, y: number) => { x: number; y: number },
    frame: OverlayFeature['frame'],
    a: number,
    b: number,
  ): { x: number; y: number } {
    if (frame === 'xy') return proj(a, b);
    const azRad = (a * Math.PI) / 180;
    return proj(b * Math.sin(azRad), b * Math.cos(azRad));
  }

  const items = $derived.by((): DrawItem[] => {
    const proj = project;
    const out: DrawItem[] = [];
    for (const layer of $overlayLayers) {
      for (const f of layer.features) {
        const pts = f.coords.map(([a, b]) => toScreen(proj, f.frame, a, b));
        if (pts.length === 0) continue;

        let d = '';
        if (f.kind === 'polygon' && pts.length >= 3) {
          d = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ') + ' Z';
        } else if (f.kind === 'line' && pts.length >= 2) {
          d = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ');
        } else if (f.kind !== 'point') {
          continue; // degenerate polygon / line
        }

        // Label anchor: point -> itself, line -> middle vertex, polygon -> vertex mean.
        let lx: number;
        let ly: number;
        if (f.kind === 'polygon') {
          lx = pts.reduce((s, p) => s + p.x, 0) / pts.length;
          ly = pts.reduce((s, p) => s + p.y, 0) / pts.length;
        } else {
          const p = pts[Math.floor(pts.length / 2)];
          lx = p.x;
          ly = p.y;
        }

        out.push({
          key: `${layer.module}\u0000${f.id}`,
          module: layer.module,
          id: f.id,
          kind: f.kind,
          color: f.color ?? layer.color,
          fill: f.fill ?? 0.12,
          d,
          points: f.kind === 'point' ? pts : [],
          label: f.label,
          lx,
          ly,
        });
      }
    }
    return out;
  });

  function onSelect(e: Event, item: DrawItem) {
    e.stopPropagation();
    selectFeature(item.module, item.id);
  }

  function isSelected(item: DrawItem, sel: { module: string; id: string } | null): boolean {
    return !!sel && sel.module === item.module && sel.id === item.id;
  }
</script>

{#if items.length > 0}
  <div class="module-overlay" aria-hidden="true">
    <svg class="module-svg" viewBox="0 0 {containerWidth} {containerHeight}">
      {#each items as item (item.key)}
        {@const selected = isSelected(item, $selectedFeature)}
        {#if item.kind === 'polygon'}
          <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
          <path
            d={item.d}
            class="feat polygon"
            class:selected
            style="stroke: {item.color}; fill: {item.color}; fill-opacity: {selected ? Math.min(1, item.fill + 0.15) : item.fill}"
            onclick={(e) => onSelect(e, item)}
          />
        {:else if item.kind === 'line'}
          <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
          <path
            d={item.d}
            class="feat line"
            class:selected
            style="stroke: {item.color}"
            onclick={(e) => onSelect(e, item)}
          />
        {:else}
          {#each item.points as p}
            <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
            <circle
              cx={p.x}
              cy={p.y}
              r={selected ? 6 : 4}
              class="feat point"
              class:selected
              style="fill: {item.color}; stroke: {item.color}"
              onclick={(e) => onSelect(e, item)}
            />
          {/each}
        {/if}
      {/each}

      {#each items as item (item.key)}
        {#if item.label}
          {@const selected = isSelected(item, $selectedFeature)}
          <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
          <text
            x={item.lx}
            y={item.ly}
            class="feat-label"
            class:selected
            text-anchor="middle"
            dominant-baseline="central"
            style="fill: {item.color}"
            onclick={(e) => onSelect(e, item)}
          >{item.label}</text>
        {/if}
      {/each}
    </svg>
  </div>
{/if}

<style>
  .module-overlay {
    position: absolute;
    inset: 0;
    pointer-events: none;
    z-index: 20;
    overflow: hidden;
  }

  .module-svg {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
  }

  .feat {
    stroke-width: 1.5;
    stroke-opacity: 0.8;
    transition: stroke-opacity 150ms ease, stroke-width 150ms ease;
  }

  /* Only the outline of polygons / lines is clickable so the PPI stays pannable. */
  .feat.polygon {
    pointer-events: stroke;
    cursor: pointer;
    stroke-linejoin: round;
  }

  .feat.line {
    fill: none;
    pointer-events: stroke;
    cursor: pointer;
    stroke-linecap: round;
    stroke-linejoin: round;
  }

  .feat.point {
    pointer-events: auto;
    cursor: pointer;
    fill-opacity: 0.85;
    stroke: rgba(15, 15, 26, 0.9) !important;
    stroke-width: 1;
    stroke-opacity: 1;
  }

  .feat.selected {
    stroke-width: 3;
    stroke-opacity: 1;
  }

  .feat.point.selected {
    stroke: #fff !important;
    stroke-width: 2;
  }

  .feat-label {
    font-family: var(--font-mono, monospace);
    font-size: 10px;
    font-weight: 700;
    paint-order: stroke;
    stroke: rgba(8, 10, 20, 0.85);
    stroke-width: 3px;
    stroke-linejoin: round;
    pointer-events: auto;
    cursor: pointer;
    user-select: none;
  }

  .feat-label.selected {
    font-size: 12px;
  }
</style>
