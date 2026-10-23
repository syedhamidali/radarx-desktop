<script lang="ts">
  /**
   * BasinOverlay - the basins of the Rainfall panel on the map: outlines, a fill by the basin-mean
   * accumulation on the same colour scale as the rainfall layer (only while that layer is an
   * accumulation), a short label, and a click that opens the basin's rainfall series in the panel.
   *
   * The fill is the area-weighted basin mean from the engine, not something read off the picture.
   * Outlines are simplified for drawing; the statistics use the exact polygons. The label at the
   * top left says what the colours are and that they come from an estimate.
   */
  import { latLonToPixel } from '../utils/geoProjection';
  import { qpeState, qpeSeries, basinOverlayOn, loadSeries } from '../stores/qpe';
  import { mosaicLayer } from '../stores/mosaic';
  import { binMinutesFor, provenanceLabel } from '../utils/qpeView';
  import { basinPath, labelPoint, colorOnLegend, basinMean, isClick, basinTitle, type BasinFeature } from '../utils/basinMap';

  let {
    radarLat,
    radarLon,
    maxRange,
    scale,
    translateX,
    translateY,
    containerWidth,
    containerHeight,
  }: {
    radarLat: number;
    radarLon: number;
    maxRange: number;
    scale: number;
    translateX: number;
    translateY: number;
    containerWidth: number;
    containerHeight: number;
  } = $props();

  const st = $derived($qpeState);
  const fc = $derived(st?.summary.basin_geojson ?? null);
  const adjusted = $derived(st?.summary.provenance.type === 'ADJUSTED');
  const selected = $derived($qpeSeries?.basin ?? null);
  // fill only while the map layer shows an accumulation: the scale of any other field would not fit
  const fillOn = $derived(!!st && (st.layer.field === 'accumulation' || st.layer.field === 'accumulation_adjusted'));
  const shown = $derived(!!fc && $basinOverlayOn && !!$mosaicLayer?.visible);

  // The view is passed in as arguments (a function reading the props inside would not re-run on pan/zoom).
  function drawn(f: BasinFeature, lat: number, lon: number, mr: number, sc: number, tx: number, ty: number, w: number, h: number) {
    const project = (la: number, lo: number) => latLonToPixel(la, lo, lat, lon, mr, sc, tx, ty, w, h);
    const lp = labelPoint(f.geometry);
    const at = lp ? project(lp[1], lp[0]) : null;
    return { d: basinPath(f.geometry, project), at };
  }

  let down: { x: number; y: number } | null = null;
  function onDown(e: MouseEvent) {
    down = { x: e.clientX, y: e.clientY };
  }
  function open(e: MouseEvent, id: string) {
    const ok = isClick(down, { x: e.clientX, y: e.clientY });
    down = null;
    if (!ok || !st) return;
    e.stopPropagation();
    loadSeries(id, binMinutesFor(st.summary.duration_s));
  }
  function onKey(e: KeyboardEvent, id: string) {
    if ((e.key === 'Enter' || e.key === ' ') && st) {
      e.preventDefault();
      loadSeries(id, binMinutesFor(st.summary.duration_s));
    }
  }
</script>

{#if shown && fc && st}
  <div class="basin-overlay">
    <svg class="basin-svg" viewBox="0 0 {containerWidth} {containerHeight}" role="group" aria-label="Basin outlines coloured by basin-mean rainfall">
      {#each fc.features as f (f.properties.id)}
        {@const g = drawn(f, radarLat, radarLon, maxRange, scale, translateX, translateY, containerWidth, containerHeight)}
        {@const fill = fillOn ? colorOnLegend(basinMean(f, adjusted), st.layer.legend) : null}
        {#if g.d}
          <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
          <path
            d={g.d}
            class="basin"
            class:selected={f.properties.id === selected}
            class:filled={!!fill}
            style={fill ? `fill: ${fill}` : ''}
            fill-rule="evenodd"
            role="button"
            tabindex="0"
            aria-label={basinTitle(f, adjusted)}
            onmousedown={onDown}
            onclick={(e) => open(e, f.properties.id)}
            onkeydown={(e) => onKey(e, f.properties.id)}
          ><title>{basinTitle(f, adjusted)}</title></path>
        {/if}
      {/each}
      {#each fc.features as f (f.properties.id)}
        {@const g = drawn(f, radarLat, radarLon, maxRange, scale, translateX, translateY, containerWidth, containerHeight)}
        {#if g.at && g.at[0] > -40 && g.at[0] < containerWidth + 40 && g.at[1] > -20 && g.at[1] < containerHeight + 20}
          {@const m = basinMean(f, adjusted)}
          <text x={g.at[0]} y={g.at[1]} class="basin-label" text-anchor="middle" dominant-baseline="middle">
            {f.properties.id}{m !== null ? ` · ${m.toFixed(m < 10 ? 1 : 0)} mm` : ''}
          </text>
        {/if}
      {/each}
    </svg>
    <div class="basin-note" aria-label="What the basin colours are">
      Basins: {fillOn ? 'mean accumulation (mm)' : 'outlines only (switch the layer to accumulation for the fill)'}, area-weighted ·
      {provenanceLabel(st.summary.provenance.type)}: an estimate, not an observation
    </div>
  </div>
{/if}

<style>
  .basin-overlay { position: absolute; inset: 0; pointer-events: none; z-index: 19; overflow: hidden; }
  .basin-svg { position: absolute; inset: 0; width: 100%; height: 100%; }
  .basin {
    pointer-events: all; cursor: pointer;
    fill: transparent; fill-opacity: 0.5;
    stroke: var(--c-text); stroke-width: 1.4; stroke-opacity: 0.85; stroke-linejoin: round;
    transition: stroke-width 120ms ease, fill-opacity 120ms ease;
  }
  .basin.filled { fill-opacity: 0.55; }
  .basin:hover { stroke-width: 2.2; fill-opacity: 0.7; }
  .basin.selected { stroke: var(--c-accent); stroke-width: 3; stroke-opacity: 1; }
  .basin:focus-visible { outline: 2px solid var(--c-accent); outline-offset: 1px; }
  .basin-label {
    font-family: var(--font-mono); font-size: 10px; fill: var(--c-text); pointer-events: none;
    paint-order: stroke; stroke: rgba(var(--c-surface-1-rgb), 0.9); stroke-width: 3px; stroke-linejoin: round;
  }
  .basin-note {
    position: absolute; left: var(--space-2); bottom: 30px; max-width: min(520px, calc(100% - 2 * var(--space-2)));
    padding: 2px 8px; font-size: var(--fs-2xs); color: var(--c-text-dim);
    background: rgba(var(--c-surface-1-rgb), 0.9); border: 1px solid var(--c-border-strong); border-radius: var(--radius-sm);
  }
</style>
