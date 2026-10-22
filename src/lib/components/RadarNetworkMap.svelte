<script lang="ts">
  /**
   * RadarNetworkMap - a world map of every radar in the bundled catalogue: which radars exist and
   * which data the app can load.  Markers are drawn on a canvas (a thousand sites at 60 Hz) over
   * slippy-map tiles in Web Mercator; when zoomed out they are grid-clustered.  The marker shape
   * says the access class (shape and text, never colour alone); the legend spells it out.
   *
   * Nothing here contacts a server by itself: the catalogue ships in the app, map tiles follow the
   * basemap setting (choose "None" to send nothing), and every listing or download of radar data
   * goes through the consent dialog first.
   *
   * Two views of the same catalogue, filters, markers and card: the globe (RadarGlobe.svelte, WebGL2, the
   * default where it exists) and this flat Web Mercator map. Selecting a site in one selects it in the other.
   *
   * Keyboard (flat): arrows pan, + / - zoom; the globe takes the same keys (see RadarGlobe.svelte).
   * Escape closes the card, then the dialog.
   */
  import { onMount, onDestroy } from 'svelte';
  import { portal } from '../utils/portal';
  import { focusTrap } from '../utils/focusTrap';
  import { settings, setNetworkView } from '../stores/settings';
  import { resolveBasemap } from '../utils/basemaps';
  import { SlippyTiles, type TileWant } from '../utils/slippyTiles';
  import { drawMarker } from '../utils/radarMarkers';
  import { webgl2Available } from '../utils/globeRenderer';
  import { angularDistanceDeg, globeToFlatZoom, flatZoomToGlobeRadius, fitRadius, clampScale } from '../utils/globeMath';
  import {
    loadCatalogue,
    clusterSites,
    hitCluster,
    lonLatToWorld,
    worldToLonLat,
    ACCESS_ORDER,
    ACCESS_INFO,
    networkGroup,
    type RadarCatalogue,
    type CatalogueSite,
    type DataAccess,
    type Cluster,
  } from '../utils/radarCatalogue';
  import { consentPrompt as nexradPrompt } from '../stores/radarSites';
  import { consentPrompt as networkPrompt, loadProviders } from '../stores/radarNetworks';
  import { connectionStatus } from '../stores/radarData';
  import { cssVar, text, textDim, accent, surface } from '../utils/themeColors';
  import RadarNetworkCard from './RadarNetworkCard.svelte';
  import RadarGlobe, { type GlobeFocus, type GlobePerf } from './RadarGlobe.svelte';
  import RadarSiteConsent from './RadarSiteConsent.svelte';

  let { onclose, initialSiteId = null }: { onclose: () => void; initialSiteId?: string | null } = $props();

  // ── Catalogue and filters ──────────────────────────────────────────────────────────────────
  let cat = $state<RadarCatalogue | null>(null);
  let query = $state('');
  let access = $state<DataAccess[]>([...ACCESS_ORDER]);
  let network = $state('');
  let country = $state('');
  let selectedId = $state<string | null>(null); // initialSiteId is applied once the catalogue is in
  let now = $state(Date.now());
  let timer: ReturnType<typeof setInterval> | null = null;

  const filtered = $derived.by(() => {
    if (!cat) return [] as CatalogueSite[];
    const base = cat.filter({ access, networks: network ? [network] : undefined, countries: country ? [country] : undefined });
    return query.trim() ? cat.search(query, 100000, base) : base;
  });
  const results = $derived(query.trim() ? filtered.slice(0, 60) : []);
  const selected = $derived(cat && selectedId ? cat.byId(selectedId) : null);
  const counts = $derived(cat ? cat.countByAccess(filtered) : null);

  // ── View: centre (lon, lat) and fractional zoom; integer tiles scaled between levels ─────────
  const TILE = 256;
  let mapEl: HTMLDivElement;
  let tileCanvas: HTMLCanvasElement;
  let markCanvas: HTMLCanvasElement;
  let width = $state(800);
  let height = $state(520);
  let lon = $state(10);
  let lat = $state(30);
  let zoom = $state(1.6);
  const MIN_Z = 1;
  const MAX_Z = 12;
  let clusters: Cluster[] = [];
  let markerCount = $state(0);
  let frameMs = $state(0);
  let hover = $state<{ x: number; y: number; text: string } | null>(null);

  const basemap = $derived(resolveBasemap($settings.mapStyle, $settings.theme.mode));

  // ── Globe or flat ───────────────────────────────────────────────────────────────────────────
  const glOk = webgl2Available();
  let globeFailed = $state<string | null>(null);
  /** The saved choice: 'auto' (and 'globe') mean the globe wherever it can run. */
  const wantGlobe = $derived($settings.display.networkView !== 'flat');
  const viewMode = $derived<'globe' | 'flat'>(wantGlobe && glOk && !globeFailed ? 'globe' : 'flat');
  const globeNote = $derived(
    !glOk
      ? 'The globe needs WebGL2, which this browser or graphics driver does not provide, so the flat map is shown.'
      : globeFailed
        ? 'The globe could not start on this graphics setup, so the flat map is shown.'
        : '',
  );
  let globeRef: ReturnType<typeof RadarGlobe> | undefined = $state();
  let globeFocus = $state<GlobeFocus | null>(null);
  let focusSeq = 0;
  /** Where the globe starts when it is (re)opened: kept up to date while it runs. */
  let globeStart = $state({ lon: 10, lat: 25, scale: 1, roll: 0 });
  let centre = $state<{ lon: number; lat: number } | null>(null);
  let globePerf = $state<GlobePerf | null>(null);

  /** Radars nearest the centre of the globe: a plain list for the keyboard and for screen readers. */
  const nearList = $derived.by(() => {
    if (viewMode !== 'globe' || !centre || !filtered.length) return [] as CatalogueSite[];
    const c = centre;
    return filtered
      .map((s) => ({ s, d: angularDistanceDeg(c.lat, c.lon, s.lat, s.lon) }))
      .sort((a, b) => a.d - b.d)
      .slice(0, 8)
      .map((x) => x.s);
  });

  function chooseView(mode: 'globe' | 'flat') {
    if (mode === viewMode) return;
    if (mode === 'flat') {
      const g = globeRef?.getView();
      if (g) {
        lon = g.lon;
        lat = g.lat;
        zoom = Math.max(1.6, globeToFlatZoom(g.radius, MIN_Z, MAX_Z));
        globeStart = { lon: g.lon, lat: g.lat, scale: g.scale, roll: g.roll };
        clampView();
      }
    } else {
      globeStart = {
        lon, lat, roll: 0,
        scale: Math.max(1, clampScale(flatZoomToGlobeRadius(zoom) / fitRadius(width, height))),
      };
      globeFailed = null;
    }
    setNetworkView(mode);
  }
  /** The globe's "Open in flat map here". */
  function toFlat(glon: number, glat: number, gzoom: number) {
    lon = glon;
    lat = glat;
    zoom = gzoom;
    clampView();
    globeStart = { ...globeStart, lon: glon, lat: glat };
    setNetworkView('flat');
  }

  function project(plat: number, plon: number): [number, number] {
    const [wx, wy] = lonLatToWorld(plon, plat, zoom);
    const [cx, cy] = lonLatToWorld(lon, lat, zoom);
    return [wx - cx + width / 2, wy - cy + height / 2];
  }
  function unproject(x: number, y: number): [number, number] {
    const [cx, cy] = lonLatToWorld(lon, lat, zoom);
    return worldToLonLat(cx + x - width / 2, cy + y - height / 2, zoom);
  }

  // ── Tiles: the shared bounded loader (cache, concurrency cap, backoff, offline pause) ─────────
  const tiles = new SlippyTiles({ onChange: () => { schedule(); globeRef?.tilesChanged(); } });

  // ── Rendering ───────────────────────────────────────────────────────────────────────────────
  let raf = 0;
  function schedule() {
    if (raf) return;
    raf = requestAnimationFrame(() => { raf = 0; render(); });
  }

  function render() {
    if (viewMode !== 'flat' || !tileCanvas || !markCanvas) return;
    const t0 = performance.now();
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    for (const c of [tileCanvas, markCanvas]) {
      if (c.width !== Math.round(width * dpr) || c.height !== Math.round(height * dpr)) {
        c.width = Math.round(width * dpr);
        c.height = Math.round(height * dpr);
      }
    }
    const tctx = tileCanvas.getContext('2d');
    const mctx = markCanvas.getContext('2d');
    if (!tctx || !mctx) return;
    tctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    mctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    tctx.fillStyle = '#e9e8ee';
    tctx.fillRect(0, 0, width, height);

    // Tiles at the integer level nearest the zoom, scaled to the fractional one.
    if (basemap.provider.url) {
      const zi = Math.max(basemap.provider.minZoom, Math.min(basemap.provider.maxZoom, Math.round(zoom)));
      const s = 2 ** (zoom - zi);
      const n = 2 ** zi;
      const [cx, cy] = lonLatToWorld(lon, lat, zi);
      const x0 = cx - width / 2 / s;
      const y0 = cy - height / 2 / s;
      const tx0 = Math.floor(x0 / TILE);
      const ty0 = Math.max(0, Math.floor(y0 / TILE));
      const tx1 = Math.floor((x0 + width / s) / TILE);
      const ty1 = Math.min(n - 1, Math.floor((y0 + height / s) / TILE));
      const wanted: Array<TileWant & { d: number }> = [];
      const mx = width / 2, my = height / 2;
      for (let ty = ty0; ty <= ty1; ty++) {
        for (let tx = tx0; tx <= tx1; tx++) {
          const wx = ((tx % n) + n) % n; // wrap around the antimeridian
          const px = (tx * TILE - x0) * s;
          const py = (ty * TILE - y0) * s;
          const img = tiles.get(basemap.provider, zi, wx, ty);
          if (img) tctx.drawImage(img, px, py, TILE * s + 0.5, TILE * s + 0.5);
          else wanted.push({ z: zi, x: wx, y: ty, d: Math.hypot(px - mx, py - my) });
        }
      }
      wanted.sort((a, b) => a.d - b.d);
      tiles.want(basemap.provider, wanted);
    } else {
      tiles.want(basemap.provider, []);
    }

    // Markers.
    mctx.clearRect(0, 0, width, height);
    const cell = zoom >= 7.5 ? 1 : 26;
    clusters = clusterSites(filtered, project, width, height, cell);
    markerCount = clusters.length;
    const ink = text();
    const dim = textDim();
    const accentCol = accent();
    const bg = surface(1);
    mctx.lineWidth = 1.6;
    mctx.font = `600 10px ${cssVar('--font-mono', 'monospace')}`;
    mctx.textAlign = 'center';
    mctx.textBaseline = 'middle';
    for (const c of clusters) {
      const many = c.sites.length > 1;
      const sel = !many && c.sites[0].id === selectedId;
      const r = many ? Math.min(14, 6 + Math.sqrt(c.sites.length) * 1.6) : 5.5;
      drawMarker(mctx, c.access, c.x, c.y, r, { ink, dim, accent: accentCol, bg }, sel);
      if (many) {
        mctx.fillStyle = ink;
        mctx.fillText(String(c.sites.length), c.x, c.y + 0.5);
      }
    }
    if (selected) {
      const [x, y] = project(selected.lat, selected.lon);
      mctx.strokeStyle = accentCol;
      mctx.lineWidth = 2;
      mctx.beginPath();
      mctx.arc(x, y, 11, 0, Math.PI * 2);
      mctx.stroke();
    }
    frameMs = performance.now() - t0;
  }

  // ── Interaction ────────────────────────────────────────────────────────────────────────────
  let drag: { x: number; y: number; lon: number; lat: number; moved: boolean } | null = null;

  function clampView() {
    zoom = Math.max(MIN_Z, Math.min(MAX_Z, zoom));
    lat = Math.max(-80, Math.min(84, lat));
    lon = ((lon + 540) % 360) - 180;
  }
  function onPointerDown(e: PointerEvent) {
    if (e.button !== 0) return;
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    drag = { x: e.clientX, y: e.clientY, lon, lat, moved: false };
  }
  function onPointerMove(e: PointerEvent) {
    const rect = mapEl.getBoundingClientRect();
    const px = e.clientX - rect.left, py = e.clientY - rect.top;
    if (drag) {
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
      const [cx, cy] = lonLatToWorld(drag.lon, drag.lat, zoom);
      [lon, lat] = worldToLonLat(cx - dx, cy - dy, zoom);
      clampView();
      schedule();
      return;
    }
    const c = hitCluster(clusters, px, py, 10);
    hover = c ? { x: c.x, y: c.y, text: c.sites.length === 1 ? `${c.sites[0].id} ${c.sites[0].name}, ${c.sites[0].country}` : `${c.sites.length} radars (click to zoom in)` } : null;
  }
  function onPointerUp(e: PointerEvent) {
    const d = drag;
    drag = null;
    if (!d || d.moved) return;
    const rect = mapEl.getBoundingClientRect();
    const c = hitCluster(clusters, e.clientX - rect.left, e.clientY - rect.top, 10);
    if (!c) { selectedId = null; schedule(); return; }
    if (c.sites.length === 1) { select(c.sites[0]); return; }
    [lon, lat] = unproject(c.x, c.y);
    zoom = Math.min(MAX_Z, zoom + 2);
    clampView();
    schedule();
  }
  function onWheel(e: WheelEvent) {
    e.preventDefault();
    const rect = mapEl.getBoundingClientRect();
    const px = e.clientX - rect.left, py = e.clientY - rect.top;
    const [plon, plat] = unproject(px, py);
    zoom = Math.max(MIN_Z, Math.min(MAX_Z, zoom - Math.sign(e.deltaY) * 0.5));
    // Keep the point under the cursor fixed.
    const [wx, wy] = lonLatToWorld(plon, plat, zoom);
    [lon, lat] = worldToLonLat(wx - (px - width / 2), wy - (py - height / 2), zoom);
    clampView();
    schedule();
  }
  function onDblClick(e: MouseEvent) {
    const rect = mapEl.getBoundingClientRect();
    [lon, lat] = unproject(e.clientX - rect.left, e.clientY - rect.top);
    zoom = Math.min(MAX_Z, zoom + 1);
    clampView();
    schedule();
  }
  function onKey(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      if (selectedId) selectedId = null; else onclose();
      schedule();
      return;
    }
    const target = e.target as HTMLElement | null;
    if (target && (target.tagName === 'INPUT' || target.tagName === 'SELECT' || target.tagName === 'TEXTAREA')) return;
    if (viewMode === 'globe') {
      globeRef?.handleKey(e);
      return;
    }
    const step = 80 / 2 ** zoom; // degrees-ish: slower when zoomed in
    if (e.key === 'ArrowLeft') lon -= step;
    else if (e.key === 'ArrowRight') lon += step;
    else if (e.key === 'ArrowUp') lat += step * 0.6;
    else if (e.key === 'ArrowDown') lat -= step * 0.6;
    else if (e.key === '+' || e.key === '=') zoom += 1;
    else if (e.key === '-') zoom -= 1;
    else return;
    e.preventDefault();
    clampView();
    schedule();
  }

  function select(s: CatalogueSite) {
    selectedId = s.id;
    globeFocus = { seq: ++focusSeq, lon: s.lon, lat: s.lat, minScale: 2.2 };
    if (zoom < 5) zoom = 5;
    // Centre on the site unless it is already comfortably in view at the new zoom.
    const [x, y] = project(s.lat, s.lon);
    if (x < 60 || y < 60 || x > width - 60 || y > height - 60) { lon = s.lon; lat = s.lat; }
    clampView();
    schedule();
  }

  function toggleAccess(a: DataAccess) {
    access = access.includes(a) ? access.filter((x) => x !== a) : [...access, a];
  }

  // Re-render on every change of the inputs (reading them here registers the dependencies).
  $effect(() => {
    const deps = [filtered, selectedId, width, height, lon, lat, zoom, basemap, viewMode];
    if (deps.length) schedule();
  });

  let ro: ResizeObserver | null = null;
  onMount(() => {
    void loadCatalogue().then((c) => {
      cat = c;
      if (initialSiteId) { const s = c.byId(initialSiteId); if (s) select(s); }
    });
    if ($connectionStatus === 'connected') void loadProviders().catch(() => {});
    ro = new ResizeObserver(() => {
      const r = mapEl.getBoundingClientRect();
      width = Math.max(200, Math.round(r.width));
      height = Math.max(200, Math.round(r.height));
    });
    ro.observe(mapEl);
    timer = setInterval(() => (now = Date.now()), 10_000);
  });
  onDestroy(() => {
    ro?.disconnect();
    if (timer) clearInterval(timer);
    if (raf) cancelAnimationFrame(raf);
    tiles.dispose();
    $nexradPrompt?.answer('cancel');
    $networkPrompt?.answer('cancel');
  });
</script>

<svelte:window onkeydowncapture={(e) => { if (!$nexradPrompt && !$networkPrompt) onKey(e); }} />

<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
<div class="rnm-scrim" use:portal onclick={(e) => { if (e.target === e.currentTarget) onclose(); }}>
  <div class="rnm brackets-4" role="dialog" aria-modal="true" aria-labelledby="rnm-title" use:focusTrap={{ initial: '.rnm-search' }} data-testid="radar-network-map">
    <header>
      <div>
        <h3 id="rnm-title">Radar network map</h3>
        <p class="sub">Which radars exist and which data you can load. {cat ? `${cat.sites.length} sites, catalogue of ${cat.retrieved}.` : 'Loading the catalogue...'} Nothing is fetched without asking you first.</p>
        {#if globeNote}<p class="sub note" data-testid="rnm-globe-note">{globeNote}</p>{/if}
      </div>
      <div class="viewsw seg" role="group" aria-label="Map view">
        <button type="button" aria-pressed={viewMode === 'globe'} disabled={!glOk} title={glOk ? 'A globe you can turn' : 'The globe needs WebGL2, which is not available here'} onclick={() => chooseView('globe')} data-testid="rnm-view-globe">Globe</button>
        <button type="button" aria-pressed={viewMode === 'flat'} title="A flat Web Mercator map" onclick={() => chooseView('flat')} data-testid="rnm-view-flat">Flat</button>
      </div>
      <button type="button" class="x" aria-label="Close" onclick={onclose}>&times;</button>
    </header>

    <div class="body">
      <aside class="side">
        <input class="rnm-search" type="search" placeholder="Search id, name, country, network" bind:value={query} aria-label="Search radars" />
        <div class="filters">
          <span class="hud-label">Access</span>
          {#each ACCESS_ORDER as a (a)}
            <label class="chk" data-access={a}>
              <input type="checkbox" checked={access.includes(a)} onchange={() => toggleAccess(a)} />
              <span class="glyph" aria-hidden="true">{ACCESS_INFO[a].glyph}</span>
              <span>{ACCESS_INFO[a].label}</span>
              <span class="count mono">{counts ? counts[a] : ''}</span>
            </label>
          {/each}
          <label class="sel">
            <span class="hud-label">Network</span>
            <select bind:value={network}>
              <option value="">All networks</option>
              {#each cat?.networks() ?? [] as n (n.id)}<option value={n.id}>{n.id} ({n.count})</option>{/each}
            </select>
          </label>
          <label class="sel">
            <span class="hud-label">Country</span>
            <select bind:value={country}>
              <option value="">All countries</option>
              {#each cat?.countries() ?? [] as c (c)}<option value={c}>{c}</option>{/each}
            </select>
          </label>
        </div>

        {#if results.length > 0}
          <ul class="results" aria-label="Search results">
            {#each results as s (s.id)}
              <li>
                <button type="button" class="res" class:sel={s.id === selectedId} onclick={() => select(s)}>
                  <span class="glyph" aria-hidden="true">{ACCESS_INFO[s.data_access].glyph}</span>
                  <span class="mono id">{s.id}</span>
                  <span class="nm">{s.name}, {s.country}</span>
                </button>
              </li>
            {/each}
          </ul>
          {#if filtered.length > results.length}<p class="fine">{filtered.length - results.length} more match; narrow the search.</p>{/if}
        {:else if query.trim() && cat}
          <p class="fine">No radar matches.</p>
        {:else if nearList.length > 0}
          <section class="near" aria-label="Radars nearest the centre of the globe">
            <span class="hud-label">Nearest the centre of the globe</span>
            <ul class="results">
              {#each nearList as s (s.id)}
                <li>
                  <button type="button" class="res" class:sel={s.id === selectedId} onclick={() => select(s)}>
                    <span class="glyph" aria-hidden="true">{ACCESS_INFO[s.data_access].glyph}</span>
                    <span class="mono id">{s.id}</span>
                    <span class="nm">{s.name}, {s.country}</span>
                  </button>
                </li>
              {/each}
            </ul>
          </section>
        {/if}

        <section class="legend" aria-label="Legend">
          <span class="hud-label">Legend</span>
          {#each ACCESS_ORDER as a (a)}
            <div class="lg"><span class="glyph" aria-hidden="true">{ACCESS_INFO[a].glyph}</span><b>{ACCESS_INFO[a].label}</b>: {ACCESS_INFO[a].meaning}</div>
          {/each}
          <div class="lg">A number in a marker is a group of radars; click to zoom in.</div>
        </section>
        {#if viewMode === 'globe'}
          <p class="fine perf" data-testid="rnm-perf">{globePerf ? `${globePerf.markers} markers (${globePerf.facing} radars facing you) drawn in ${globePerf.frameMs.toFixed(1)} ms${globePerf.fps ? `, ${globePerf.fps.toFixed(0)} frames per second while turning` : ''}.` : 'Globe: drawing.'}</p>
        {:else}
          <p class="fine perf" data-testid="rnm-perf">{markerCount} markers drawn in {frameMs.toFixed(1)} ms, zoom {zoom.toFixed(1)}.</p>
        {/if}
      </aside>

      <div class="stage">
        {#if viewMode === 'globe'}
          <RadarGlobe
            bind:this={globeRef}
            sites={filtered}
            {selected}
            {basemap}
            {tiles}
            initial={globeStart}
            focus={globeFocus}
            onselect={select}
            onclear={() => { selectedId = null; schedule(); }}
            onflat={toFlat}
            onsettle={(v) => { globeStart = { lon: v.lon, lat: v.lat, scale: v.scale, roll: v.roll }; centre = { lon: v.lon, lat: v.lat }; }}
            onperf={(p) => (globePerf = p)}
            onfail={(m) => { globeFailed = m; }}
          />
        {/if}
        <div class="map" class:off={viewMode === 'globe'} bind:this={mapEl} onpointerdown={onPointerDown} onpointermove={onPointerMove} onpointerup={onPointerUp} onpointercancel={() => (drag = null)} onwheel={onWheel} ondblclick={onDblClick} role="application" aria-label="World map of radar sites; drag to pan, wheel to zoom, click a marker for its card; arrow keys pan and + or - zoom">
          <canvas class="tiles" bind:this={tileCanvas} style:filter={basemap.provider.url ? basemap.filter : 'none'} style:width="{width}px" style:height="{height}px"></canvas>
          <canvas class="marks" bind:this={markCanvas} style:width="{width}px" style:height="{height}px"></canvas>
          {#if hover}
            <div class="tip" style:left="{Math.min(Math.max(hover.x, 80), width - 80)}px" style:top="{hover.y - 14}px" role="tooltip">{hover.text}</div>
          {/if}
          {#if basemap.provider.attribution}
            <a class="attr" href={basemap.provider.attributionUrl} target="_blank" rel="noopener">{basemap.provider.attribution}</a>
          {/if}
          <div class="zoomctl">
            <button type="button" aria-label="Zoom in" onclick={() => { zoom += 1; clampView(); schedule(); }}>+</button>
            <button type="button" aria-label="Zoom out" onclick={() => { zoom -= 1; clampView(); schedule(); }}>&minus;</button>
          </div>
        </div>
        {#if selected && cat}
          <RadarNetworkCard site={selected} catalogue={cat} {now} onclose={() => { selectedId = null; schedule(); }} />
        {/if}
      </div>
    </div>
  </div>
</div>

{#if $nexradPrompt}<RadarSiteConsent prompt={$nexradPrompt} />{/if}
{#if $networkPrompt}<RadarSiteConsent prompt={$networkPrompt} />{/if}

<style>
  .rnm-scrim {
    position: fixed;
    inset: 0;
    z-index: 1500;
    display: grid;
    place-items: center;
    background: var(--c-scrim);
    padding: var(--space-4);
  }
  .rnm {
    width: min(1180px, 100%);
    height: min(780px, 100%);
    display: flex;
    flex-direction: column;
    background: var(--c-overlay);
    border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-3);
    color: var(--c-text);
  }
  header { display: flex; justify-content: space-between; align-items: flex-start; gap: var(--space-3); padding: var(--space-3) var(--space-4) var(--space-2); }
  h3 { margin: 0; font-size: var(--fs-lg); }
  .sub { margin: 2px 0 0; color: var(--c-text-dim); font-size: var(--fs-xs); }
  .x { min-width: 28px; height: 28px; padding: 0; font-size: var(--fs-lg); background: transparent; border-color: transparent; color: var(--c-text-dim); }
  .x:hover { color: var(--c-text); border-color: var(--c-border); }
  .body { flex: 1; display: flex; min-height: 0; border-top: 1px solid var(--c-border); }
  .side { width: 272px; flex: 0 0 auto; display: flex; flex-direction: column; gap: var(--space-2); padding: var(--space-3); border-right: 1px solid var(--c-border); overflow: auto; font-size: var(--fs-xs); }
  .filters { display: flex; flex-direction: column; gap: 3px; }
  .chk { display: flex; align-items: center; gap: var(--space-2); cursor: pointer; }
  .chk input { margin: 0; width: auto; }
  .count { margin-left: auto; color: var(--c-muted); }
  .sel { display: flex; flex-direction: column; gap: 2px; margin-top: var(--space-1); }
  .glyph { display: inline-block; width: 14px; text-align: center; color: var(--c-text-dim); }
  .chk[data-access='open-loadable'] .glyph, .res .glyph { color: var(--c-accent); }
  .results { list-style: none; margin: 0; padding: 0; max-height: 220px; overflow: auto; border: 1px solid var(--c-border); border-radius: var(--radius-sm); }
  .res { display: flex; gap: var(--space-2); align-items: center; width: 100%; height: auto; padding: 3px 6px; text-align: left; background: transparent; border: 0; border-radius: 0; }
  .res:hover, .res.sel { background: rgba(var(--c-accent-rgb), 0.12); }
  .res .id { min-width: 44px; }
  .res .nm { color: var(--c-text-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .legend { display: flex; flex-direction: column; gap: 2px; margin-top: auto; padding-top: var(--space-2); border-top: 1px solid var(--c-border); color: var(--c-text-dim); font-size: var(--fs-2xs); }
  .lg b { color: var(--c-text); }
  .fine { margin: 0; color: var(--c-muted); font-size: var(--fs-2xs); }
  .stage { position: relative; flex: 1; min-width: 0; overflow: hidden; }
  .map { position: absolute; inset: 0; overflow: hidden; cursor: grab; touch-action: none; }
  .map.off { display: none; }
  .map:active { cursor: grabbing; }
  .viewsw { align-self: center; margin-left: auto; margin-right: var(--space-2); }
  .viewsw button:disabled { opacity: 0.45; cursor: not-allowed; }
  .sub.note { color: var(--c-caution); }
  canvas { position: absolute; inset: 0; display: block; }
  .marks { pointer-events: none; }
  .tip { position: absolute; transform: translate(-50%, -100%); padding: 3px 7px; border-radius: var(--radius-sm); background: var(--c-overlay); border: 1px solid var(--c-border-strong); font-size: var(--fs-xs); white-space: nowrap; pointer-events: none; box-shadow: var(--shadow-2); }
  .attr { position: absolute; right: 6px; bottom: 4px; font-size: var(--fs-2xs); color: var(--c-text-dim); background: rgba(var(--c-surface-0-rgb), 0.7); padding: 1px 5px; border-radius: var(--radius-sm); }
  .zoomctl { position: absolute; right: 10px; top: 10px; display: flex; flex-direction: column; gap: 4px; }
  .zoomctl button { width: 28px; height: 28px; min-width: 0; padding: 0; font-size: var(--fs-md); }
  .mono { font-family: var(--font-mono); }
  @media (max-width: 760px) {
    .body { flex-direction: column; }
    .side { width: auto; max-height: 40%; border-right: 0; border-bottom: 1px solid var(--c-border); }
  }
</style>
