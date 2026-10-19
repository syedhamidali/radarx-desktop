<script lang="ts">
  /**
   * GeoOverlay — SVG overlay for geographic annotations on the radar PPI.
   *
   * Renders:
   * - Radar site marker (crosshair + label)
   * - Cardinal direction labels (N/S/E/W) at edges
   * - Range ring labels in km
   * - Nearby city/location markers
   */
  import { latLonToPixel } from '../utils/geoProjection';

  let {
    radarLat,
    radarLon,
    radarName = '',
    maxRange,
    scale,
    translateX,
    translateY,
    containerWidth,
    containerHeight,
  }: {
    radarLat: number;
    radarLon: number;
    radarName?: string;
    maxRange: number;
    scale: number;
    translateX: number;
    translateY: number;
    containerWidth: number;
    containerHeight: number;
  } = $props();

  // ── Major US cities (lat, lon, name) ──────────────────────────────────────
  const CITIES: { name: string; lat: number; lon: number }[] = [
    { name: 'New York', lat: 40.7128, lon: -74.006 },
    { name: 'Los Angeles', lat: 34.0522, lon: -118.2437 },
    { name: 'Chicago', lat: 41.8781, lon: -87.6298 },
    { name: 'Houston', lat: 29.7604, lon: -95.3698 },
    { name: 'Phoenix', lat: 33.4484, lon: -112.074 },
    { name: 'Philadelphia', lat: 39.9526, lon: -75.1652 },
    { name: 'San Antonio', lat: 29.4241, lon: -98.4936 },
    { name: 'San Diego', lat: 32.7157, lon: -117.1611 },
    { name: 'Dallas', lat: 32.7767, lon: -96.797 },
    { name: 'Austin', lat: 30.2672, lon: -97.7431 },
    { name: 'Jacksonville', lat: 30.3322, lon: -81.6557 },
    { name: 'San Francisco', lat: 37.7749, lon: -122.4194 },
    { name: 'Columbus', lat: 39.9612, lon: -82.9988 },
    { name: 'Indianapolis', lat: 39.7684, lon: -86.1581 },
    { name: 'Seattle', lat: 47.6062, lon: -122.3321 },
    { name: 'Denver', lat: 39.7392, lon: -104.9903 },
    { name: 'Washington DC', lat: 38.9072, lon: -77.0369 },
    { name: 'Nashville', lat: 36.1627, lon: -86.7816 },
    { name: 'Oklahoma City', lat: 35.4676, lon: -97.5164 },
    { name: 'Boston', lat: 42.3601, lon: -71.0589 },
    { name: 'Portland', lat: 45.5152, lon: -122.6784 },
    { name: 'Memphis', lat: 35.1495, lon: -90.049 },
    { name: 'Louisville', lat: 38.2527, lon: -85.7585 },
    { name: 'Baltimore', lat: 39.2904, lon: -76.6122 },
    { name: 'Milwaukee', lat: 43.0389, lon: -87.9065 },
    { name: 'Albuquerque', lat: 35.0844, lon: -106.6504 },
    { name: 'Tucson', lat: 32.2226, lon: -110.9747 },
    { name: 'Atlanta', lat: 33.749, lon: -84.388 },
    { name: 'Miami', lat: 25.7617, lon: -80.1918 },
    { name: 'Tampa', lat: 27.9506, lon: -82.4572 },
    { name: 'New Orleans', lat: 29.9511, lon: -90.0715 },
    { name: 'Kansas City', lat: 39.0997, lon: -94.5786 },
    { name: 'St. Louis', lat: 38.627, lon: -90.1994 },
    { name: 'Minneapolis', lat: 44.9778, lon: -93.265 },
    { name: 'Detroit', lat: 42.3314, lon: -83.0458 },
    { name: 'Cleveland', lat: 41.4993, lon: -81.6944 },
    { name: 'Pittsburgh', lat: 40.4406, lon: -79.9959 },
    { name: 'Salt Lake City', lat: 40.7608, lon: -111.891 },
    { name: 'Las Vegas', lat: 36.1699, lon: -115.1398 },
    { name: 'Charlotte', lat: 35.2271, lon: -80.8431 },
    { name: 'Raleigh', lat: 35.7796, lon: -78.6382 },
    { name: 'Orlando', lat: 28.5383, lon: -81.3792 },
    { name: 'Cincinnati', lat: 39.1031, lon: -84.512 },
    { name: 'Richmond', lat: 37.5407, lon: -77.436 },
    { name: 'Omaha', lat: 41.2565, lon: -95.9345 },
    { name: 'Tulsa', lat: 36.154, lon: -95.9928 },
    { name: 'Des Moines', lat: 41.5868, lon: -93.625 },
    { name: 'Little Rock', lat: 34.7465, lon: -92.2896 },
    { name: 'Wichita', lat: 37.6872, lon: -97.3301 },
    { name: 'El Paso', lat: 31.7619, lon: -106.485 },
  ];

  // ── Computed screen positions ─────────────────────────────────────────────

  // Radar center on screen
  const radarScreenPos = $derived(project(radarLat, radarLon));

  // Nearby cities within radar range * 1.2
  const nearbyCities = $derived(CITIES.filter((c) => {
    const dLat = c.lat - radarLat;
    const dLon = (c.lon - radarLon) * Math.cos(radarLat * (Math.PI / 180));
    const distM = Math.sqrt(dLat * dLat + dLon * dLon) * (Math.PI / 180) * 6371000;
    return distM < maxRange * 1.2;
  }).map((c) => {
    const [px, py] = project(c.lat, c.lon);
    return { ...c, px, py };
  }));

  // Cardinal directions — placed at 95% of maxRange from radar center
  const cardinals = $derived((() => {
    const dist = maxRange * 0.95;
    // Keep each label on screen: zoomed in, the 95%-range spot is far outside the viewport.
    const clampX = (v: number) => Math.min(Math.max(v, 16), Math.max(16, containerWidth - 16));
    const clampY = (v: number) => Math.min(Math.max(v, 16), Math.max(16, containerHeight - 16));
    return [
      { label: 'N', az: 0 },
      { label: 'E', az: 90 },
      { label: 'S', az: 180 },
      { label: 'W', az: 270 },
    ].map((d) => {
      const azRad = d.az * (Math.PI / 180);
      // data-space: x = dist*sin(az), y = dist*cos(az)
      const dataX = dist * Math.sin(azRad);
      const dataY = dist * Math.cos(azRad);
      // Project through NDC -> pixel (replicating the ppiRenderer transform)
      const aspect = containerWidth / containerHeight;
      const sx = (aspect >= 1 ? scale / aspect : scale) / maxRange;
      const sy = (aspect >= 1 ? scale : scale * aspect) / maxRange;
      const ndcX = (dataX + translateX) * sx;
      const ndcY = (dataY + translateY) * sy;
      const px = ((ndcX + 1) / 2) * containerWidth;
      const py = ((1 - ndcY) / 2) * containerHeight;
      return { ...d, px: clampX(px), py: clampY(py) };
    });
  })());

  // Range ring labels
  const ringLabels = $derived((() => {
    const ringSpacingKm = maxRange > 300000 ? 100 : maxRange > 100000 ? 50 : maxRange > 30000 ? 25 : 10;
    const ringSpacing = ringSpacingKm * 1000;
    const numRings = Math.floor(maxRange / ringSpacing);
    const labels: { km: number; px: number; py: number }[] = [];

    for (let ri = 1; ri <= numRings; ri++) {
      const r = ri * ringSpacing;
      // Place label at 45-degree azimuth (northeast)
      const az45 = 45 * (Math.PI / 180);
      const dataX = r * Math.sin(az45);
      const dataY = r * Math.cos(az45);
      const aspect = containerWidth / containerHeight;
      const sx = (aspect >= 1 ? scale / aspect : scale) / maxRange;
      const sy = (aspect >= 1 ? scale : scale * aspect) / maxRange;
      const ndcX = (dataX + translateX) * sx;
      const ndcY = (dataY + translateY) * sy;
      const px = ((ndcX + 1) / 2) * containerWidth;
      const py = ((1 - ndcY) / 2) * containerHeight;
      labels.push({ km: ri * ringSpacingKm, px, py });
    }
    return labels;
  })());

  function project(lat: number, lon: number): [number, number] {
    return latLonToPixel(
      lat, lon, radarLat, radarLon,
      maxRange, scale, translateX, translateY,
      containerWidth, containerHeight,
    );
  }

  function inBounds(px: number, py: number, margin: number = 20): boolean {
    return px >= -margin && px <= containerWidth + margin &&
           py >= -margin && py <= containerHeight + margin;
  }
</script>

<div class="geo-overlay" aria-hidden="true">
  <svg
    class="geo-svg"
    viewBox="0 0 {containerWidth} {containerHeight}"
    xmlns="http://www.w3.org/2000/svg"
  >
    <!-- Radar site crosshair -->
    {#if inBounds(radarScreenPos[0], radarScreenPos[1])}
      <line
        x1={radarScreenPos[0] - 10} y1={radarScreenPos[1]}
        x2={radarScreenPos[0] + 10} y2={radarScreenPos[1]}
        class="radar-crosshair"
      />
      <line
        x1={radarScreenPos[0]} y1={radarScreenPos[1] - 10}
        x2={radarScreenPos[0]} y2={radarScreenPos[1] + 10}
        class="radar-crosshair"
      />
      <circle
        cx={radarScreenPos[0]} cy={radarScreenPos[1]} r="4"
        class="radar-dot"
      />
      <text
        x={radarScreenPos[0] + 12} y={radarScreenPos[1] - 8}
        class="radar-label"
      >
        {radarName || 'RADAR'}
      </text>
    {/if}

    <!-- Cardinal direction labels -->
    {#each cardinals as cd}
      {#if inBounds(cd.px, cd.py)}
        <text x={cd.px} y={cd.py} class="cardinal-label" text-anchor="middle" dominant-baseline="central">
          {cd.label}
        </text>
      {/if}
    {/each}

    <!-- Range ring labels -->
    {#each ringLabels as rl}
      {#if inBounds(rl.px, rl.py)}
        <text x={rl.px + 4} y={rl.py - 4} class="ring-label">
          {rl.km} km
        </text>
      {/if}
    {/each}

    <!-- City markers -->
    {#each nearbyCities as city}
      {#if inBounds(city.px, city.py)}
        <circle cx={city.px} cy={city.py} r="3" class="city-dot" />
        <text x={city.px + 6} y={city.py + 4} class="city-label">
          {city.name}
        </text>
      {/if}
    {/each}
  </svg>
</div>

<style>
  .geo-overlay {
    position: absolute;
    inset: 0;
    pointer-events: none;
    z-index: 8;
    overflow: hidden;
  }

  .geo-overlay {
    --halo: rgba(var(--c-surface-0-rgb), 0.8);
  }

  .geo-svg {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
  }

  /* Radar site */
  .radar-crosshair {
    stroke: var(--accent-primary);
    stroke-width: 2;
    stroke-opacity: 0.9;
  }

  .radar-dot {
    fill: var(--accent-primary);
    fill-opacity: 0.9;
    stroke: var(--text-primary);
    stroke-width: 1;
  }

  .radar-label {
    fill: var(--accent-primary);
    font-family: var(--font-mono, monospace);
    font-size: 11px;
    font-weight: 700;
        paint-order: stroke;
    stroke: var(--halo);
    stroke-width: 3px;
  }

  /* Cardinals */
  .cardinal-label {
    fill: var(--text-muted);
    font-family: var(--font-sans, sans-serif);
    font-size: 14px;
    font-weight: 700;
    letter-spacing: 0.05em;
    paint-order: stroke;
    stroke: var(--halo);
    stroke-width: 3px;
  }

  /* Range ring labels */
  .ring-label {
    fill: var(--text-muted);
    font-family: var(--font-mono, monospace);
    font-size: 9px;
    font-weight: 500;
    opacity: 0.7;
    paint-order: stroke;
    stroke: var(--halo);
    stroke-width: 2px;
  }

  /* City markers */
  .city-dot {
    fill: var(--text-secondary);
    fill-opacity: 0.8;
    stroke: rgba(var(--c-surface-0-rgb), 0.6);
    stroke-width: 1;
  }

  .city-label {
    fill: var(--text-secondary);
    font-family: var(--font-sans, sans-serif);
    font-size: 10px;
    font-weight: 500;
    opacity: 0.85;
    paint-order: stroke;
    stroke: var(--halo);
    stroke-width: 2.5px;
  }
</style>
