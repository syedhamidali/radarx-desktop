<script lang="ts" module>
export interface GlobeFocus {
  seq: number;
  lon: number;
  lat: number;
  /** Zoom in at least this far (scale 1 = the whole globe fits). */
  minScale?: number;
}
export interface GlobePerf {
  markers: number;
  facing: number;
  frameMs: number;
  fps: number;
}
</script>

<script lang="ts">
  /**
   * RadarGlobe - the globe view of the radar network map: the same catalogue, filters, markers and card as
   * the flat map, on a sphere you can turn. WebGL2 draws the sphere (globeRenderer.ts); a 2D canvas over it
   * draws the coastlines/graticule, the markers and the selection (the same shapes as the flat map: shape and
   * text for the access class, never colour alone).
   *
   * Nothing here contacts a server by itself: the sphere shows the bundled outlines, or - only when the
   * basemap setting names a tile service - the 64 tiles of level 3 through the shared tile cache. With the
   * basemap "None" nothing is sent at all.
   *
   * Pointer: drag turns the globe (the point under the pointer stays under it, with a little inertia unless
   * the system asks for reduced motion), Shift+drag spins it about the view axis, wheel and pinch zoom about
   * the pointer, double click centres. Keyboard: arrows turn, Shift+arrows left/right spin, + and - zoom,
   * Home resets, N points north up.
   */
  import { onMount, onDestroy } from 'svelte';
  import { clusterSites, hitCluster, type CatalogueSite, type Cluster } from '../utils/radarCatalogue';
  import { drawMarker } from '../utils/radarMarkers';
  import type { ResolvedBasemap } from '../utils/basemaps';
  import type { SlippyTiles } from '../utils/slippyTiles';
  import { GlobeRenderer } from '../utils/globeRenderer';
  import { GlobeSurface } from '../utils/globeSurface';
  import { loadGlobeGeo, buildGlobeLines, type GlobeGeo, type GlobeLines } from '../utils/globeGeo';
  import {
    type GlobeView,
    HANDOVER_SCALE,
    clampScale,
    dragView,
    zoomAt,
    unproject,
    viewBasis,
    lonLatToVec,
    projectVec,
    traceVisible,
    fitRadius,
    globeToFlatZoom,
    slerpLonLat,
    rollDelta,
    easeInOut,
    keyStepDeg,
    wrapLon,
    MAX_CENTRE_LAT,
    DEG,
  } from '../utils/globeMath';
  import { themeRevision, cssVar, text, textDim, muted, accent, surface, ink, cssRgb01 } from '../utils/themeColors';

  let {
    sites,
    selected = null,
    basemap,
    tiles,
    initial,
    focus = null,
    onselect,
    onclear,
    onflat,
    onsettle,
    onperf,
    onfail,
  }: {
    sites: readonly CatalogueSite[];
    selected?: CatalogueSite | null;
    basemap: ResolvedBasemap;
    tiles: SlippyTiles;
    initial: { lon: number; lat: number; scale: number; roll?: number };
    focus?: GlobeFocus | null;
    onselect: (s: CatalogueSite) => void;
    onclear: () => void;
    onflat: (lon: number, lat: number, zoom: number) => void;
    onsettle?: (v: { lon: number; lat: number; scale: number; roll: number }) => void;
    onperf?: (p: GlobePerf) => void;
    onfail?: (message: string) => void;
  } = $props();

  const HOME = { lon: 10, lat: 25, roll: 0, scale: 1 };

  let el: HTMLDivElement;
  let glCanvas: HTMLCanvasElement;
  let ovCanvas: HTMLCanvasElement;
  let width = $state(800);
  let height = $state(520);
  let view: GlobeView = { lon: HOME.lon, lat: HOME.lat, roll: 0 };
  let scale = 1;
  let renderer: GlobeRenderer | null = null;
  let surface_: GlobeSurface | null = null;
  let geo: GlobeGeo | null = null;
  let lines: GlobeLines | null = null;
  let raf = 0;
  let disposed = false;
  let clusters: Cluster[] = [];
  let ro: ResizeObserver | null = null;
  let settleTimer: ReturnType<typeof setTimeout> | null = null;
  let perfTimer: ReturnType<typeof setTimeout> | null = null;
  let lastSeq = -1;

  // UI state that the template reads.
  let scaleUi = $state(1);
  let rollUi = $state(0);
  let hover = $state<{ x: number; y: number; text: string } | null>(null);
  let lostUi = $state(false);
  let spin = $state(false);
  let reduceMotion = $state(false);
  let tileStatus = $state({ kind: 'plain' as 'plain' | 'tiles', loaded: 0, total: 0 });
  let status = $state('');
  let grabbing = $state(false);

  const radius = () => fitRadius(width, height) * scale;

  // ── Animation: fly-to, inertia, spin ──────────────────────────────────────────────────────────
  interface Fly { from: GlobeView & { scale: number }; to: GlobeView & { scale: number }; t0: number; dur: number }
  let fly: Fly | null = null;
  let inertia: { vlon: number; vlat: number } | null = null;
  let lastFrameT = 0;
  const frameTimes: number[] = [];
  let lastFrameMs = 0;
  let lastMarkers = 0;
  let lastFacing = 0;

  function flyTo(to: Partial<GlobeView> & { scale?: number }, durMs = 700) {
    const target = {
      lon: to.lon ?? view.lon,
      lat: Math.max(-MAX_CENTRE_LAT, Math.min(MAX_CENTRE_LAT, to.lat ?? view.lat)),
      roll: to.roll ?? view.roll,
      scale: clampScale(to.scale ?? scale),
    };
    inertia = null;
    if (reduceMotion || durMs <= 0) {
      view = { lon: target.lon, lat: target.lat, roll: target.roll };
      scale = target.scale;
      fly = null;
      touched();
      return;
    }
    fly = { from: { ...view, scale }, to: target, t0: performance.now(), dur: durMs };
    request();
  }

  function stepAnimations(now: number): boolean {
    const dt = Math.min(0.1, Math.max(0, (now - lastFrameT) / 1000));
    let active = false;
    if (fly) {
      const t = Math.min(1, (now - fly.t0) / fly.dur);
      const e = easeInOut(t);
      const [lon, lat] = slerpLonLat(fly.from.lon, fly.from.lat, fly.to.lon, fly.to.lat, e);
      view = { lon, lat, roll: fly.from.roll + rollDelta(fly.from.roll, fly.to.roll) * e };
      scale = Math.exp(Math.log(fly.from.scale) + (Math.log(fly.to.scale) - Math.log(fly.from.scale)) * e);
      if (t >= 1) { view.roll = fly.to.roll; fly = null; } else active = true;
    } else if (inertia) {
      const k = Math.exp(-dt / 0.45);
      view = { ...view, lon: wrapLon(view.lon + inertia.vlon * dt), lat: Math.max(-MAX_CENTRE_LAT, Math.min(MAX_CENTRE_LAT, view.lat + inertia.vlat * dt)) };
      inertia = { vlon: inertia.vlon * k, vlat: inertia.vlat * k };
      if (Math.hypot(inertia.vlon, inertia.vlat) < 2 || Math.abs(view.lat) >= MAX_CENTRE_LAT) inertia = null; else active = true;
    } else if (spin && !reduceMotion && !drag && pointers.size === 0) {
      view = { ...view, lon: wrapLon(view.lon - 7 * dt) };
      active = true;
    }
    return active;
  }

  // ── Frame loop ───────────────────────────────────────────────────────────────────────────────
  function request() {
    if (raf || disposed) return;
    raf = requestAnimationFrame(frame);
  }
  /** The view changed by an interaction: draw and tell the page once it settles. */
  function touched() {
    request();
    if (settleTimer) clearTimeout(settleTimer);
    settleTimer = setTimeout(() => {
      settleTimer = null;
      scaleUi = scale;
      rollUi = view.roll;
      onsettle?.({ lon: view.lon, lat: view.lat, scale, roll: view.roll });
      status = describe();
    }, 250);
  }

  function frame(now: number) {
    raf = 0;
    if (disposed) return;
    const active = stepAnimations(now);
    lastFrameT = now;
    draw();
    if (active) {
      frameTimes.push(now);
      while (frameTimes.length > 90) frameTimes.shift();
      scaleUi = scale;
      rollUi = view.roll;
      if (settleTimer) clearTimeout(settleTimer);
      settleTimer = setTimeout(() => { settleTimer = null; touched(); }, 250);
      request();
    } else {
      frameTimes.length = 0;
    }
    reportPerf();
  }

  function reportPerf() {
    if (!onperf || perfTimer) return;
    perfTimer = setTimeout(() => {
      perfTimer = null;
      let fps = 0;
      if (frameTimes.length > 5) fps = (1000 * (frameTimes.length - 1)) / (frameTimes[frameTimes.length - 1] - frameTimes[0]);
      onperf?.({ markers: lastMarkers, facing: lastFacing, frameMs: lastFrameMs, fps });
    }, 250);
  }

  // ── Colours (tokens; recomputed when the theme changes) ──────────────────────────────────────
  let cols = { ink: '#fff', dim: '#aaa', muted: '#888', accent: '#0cf', bg: '#000', line: '', lineFaint: '', rim: '' };
  function readColours() {
    cols = {
      ink: text(), dim: textDim(), muted: muted(), accent: accent(), bg: surface(1),
      line: textDim(), lineFaint: ink(0.16), rim: ink(0.35),
    };
  }

  function applyBasemap() {
    if (!surface_) return;
    readColours();
    surface_.setBasemap(basemap, { ocean: surface(1), land: ink(0.16) }, geo, view.lon, view.lat);
    renderer?.setFlatColour(cssRgb01('--c-surface-1', [0.03, 0.05, 0.09]));
    tileStatus = { ...surface_.status };
    request();
  }

  // ── Drawing ─────────────────────────────────────────────────────────────────────────────────
  function draw() {
    if (!renderer || !ovCanvas) return;
    const t0 = performance.now();
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    renderer.resize(width, height, dpr);
    const R = radius();
    const cx = width / 2;
    const cy = height / 2;
    renderer.render({ view, radius: R, cx, cy });

    const w = Math.round(width * dpr);
    const h = Math.round(height * dpr);
    if (ovCanvas.width !== w || ovCanvas.height !== h) { ovCanvas.width = w; ovCanvas.height = h; }
    const ctx = ovCanvas.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const b = viewBasis(view);

    // Lines.
    const stroke = (set: Float32Array[], closed: boolean, style: string, lw: number) => {
      ctx.beginPath();
      for (const v of set) traceVisible(v, closed, b, R, cx, cy, (x, y) => ctx.moveTo(x, y), (x, y) => ctx.lineTo(x, y));
      ctx.strokeStyle = style;
      ctx.lineWidth = lw;
      ctx.stroke();
    };
    const plain = !basemap.provider.url;
    if (lines) {
      stroke(lines.graticule, false, plain ? cols.lineFaint : cols.lineFaint, 0.8);
      if (plain || tileStatus.loaded === 0) {
        stroke(lines.borders, false, cols.muted, 0.7);
        stroke(lines.coast, false, cols.dim, 1.1);
      }
    }
    // The rim of the sphere.
    ctx.beginPath();
    ctx.arc(cx, cy, R, 0, Math.PI * 2);
    ctx.strokeStyle = cols.rim;
    ctx.lineWidth = 1;
    ctx.stroke();

    // Markers: front side only, grid-clustered at low zoom.
    const project = (lat: number, lon: number): [number, number] => {
      const p = projectVec(b, lonLatToVec(lon, lat), R, cx, cy);
      return p.depth > 0.02 ? [p.x, p.y] : [NaN, NaN];
    };
    const cell = scale >= 3.5 ? 1 : scale >= 2 ? 14 : 26;
    clusters = clusterSites(sites, project, width, height, cell);
    lastMarkers = clusters.length;
    lastFacing = clusters.reduce((n, c) => n + c.sites.length, 0);
    const ink_ = { ink: cols.ink, dim: cols.dim, accent: cols.accent, bg: cols.bg };
    ctx.lineWidth = 1.6;
    ctx.font = `600 10px ${cssVar('--font-mono', 'monospace')}`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    for (const c of clusters) {
      const many = c.sites.length > 1;
      const sel = !many && c.sites[0].id === selected?.id;
      const r = many ? Math.min(14, 6 + Math.sqrt(c.sites.length) * 1.6) : 5.5;
      drawMarker(ctx, c.access, c.x, c.y, r, ink_, sel);
      if (many) {
        ctx.fillStyle = cols.ink;
        ctx.fillText(String(c.sites.length), c.x, c.y + 0.5);
      }
    }
    if (selected) {
      const p = projectVec(b, lonLatToVec(selected.lon, selected.lat), R, cx, cy);
      if (p.depth > 0.02) {
        ctx.strokeStyle = cols.accent;
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(p.x, p.y, 11, 0, Math.PI * 2);
        ctx.stroke();
      }
    }
    lastFrameMs = performance.now() - t0;
  }

  // ── Pointer interaction ──────────────────────────────────────────────────────────────────────
  interface Drag {
    x: number; y: number;
    start: GlobeView;
    grab: [number, number] | null;
    rollMode: boolean;
    a0: number;
    moved: boolean;
    samples: Array<{ t: number; lon: number; lat: number }>;
  }
  let drag: Drag | null = null;
  const pointers = new Map<number, { x: number; y: number }>();
  let pinch: { d0: number; scale0: number } | null = null;

  function local(e: { clientX: number; clientY: number }): [number, number] {
    const r = el.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  }

  function onPointerDown(e: PointerEvent) {
    if ((e.target as HTMLElement).closest('button, a')) return;
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    el.focus({ preventScroll: true });
    try { el.setPointerCapture(e.pointerId); } catch { /* not capturable */ }
    const [px, py] = local(e);
    pointers.set(e.pointerId, { x: px, y: py });
    fly = null;
    inertia = null;
    if (pointers.size === 2) {
      const [p, q] = [...pointers.values()];
      pinch = { d0: Math.max(10, Math.hypot(p.x - q.x, p.y - q.y)), scale0: scale };
      drag = null;
      return;
    }
    const g = unproject(view, px, py, radius(), width / 2, height / 2);
    drag = {
      x: px, y: py, start: { ...view }, grab: g, rollMode: e.shiftKey, a0: Math.atan2(py - height / 2, px - width / 2),
      moved: false, samples: [{ t: performance.now(), lon: view.lon, lat: view.lat }],
    };
    grabbing = true;
  }

  function onPointerMove(e: PointerEvent) {
    const [px, py] = local(e);
    if (pointers.has(e.pointerId)) pointers.set(e.pointerId, { x: px, y: py });
    if (pinch && pointers.size >= 2) {
      const [p, q] = [...pointers.values()];
      const d = Math.hypot(p.x - q.x, p.y - q.y);
      zoomTo(clampScale(pinch.scale0 * (d / pinch.d0)), (p.x + q.x) / 2, (p.y + q.y) / 2);
      return;
    }
    if (drag) {
      if (Math.abs(px - drag.x) + Math.abs(py - drag.y) > 3) drag.moved = true;
      if (!drag.moved) return;
      hover = null;
      if (drag.rollMode) {
        const a = Math.atan2(py - height / 2, px - width / 2);
        view = { ...drag.start, roll: drag.start.roll + (a - drag.a0) / DEG };
      } else if (drag.grab) {
        view = dragView(drag.start, drag.grab[0], drag.grab[1], (px - width / 2) / radius(), (height / 2 - py) / radius());
      }
      const t = performance.now();
      drag.samples.push({ t, lon: view.lon, lat: view.lat });
      while (drag.samples.length > 2 && t - drag.samples[0].t > 120) drag.samples.shift();
      touched();
      return;
    }
    if (e.pointerType === 'touch') return;
    const c = hitCluster(clusters, px, py, 10);
    hover = c
      ? { x: c.x, y: c.y, text: c.sites.length === 1 ? `${c.sites[0].id} ${c.sites[0].name}, ${c.sites[0].country}` : `${c.sites.length} radars (click to zoom in)` }
      : null;
  }

  function onPointerUp(e: PointerEvent) {
    pointers.delete(e.pointerId);
    if (pinch) {
      if (pointers.size < 2) pinch = null;
      drag = null;
      grabbing = false;
      return;
    }
    const d = drag;
    drag = null;
    grabbing = false;
    if (!d) return;
    const [px, py] = local(e);
    if (!d.moved) {
      const c = hitCluster(clusters, px, py, 10);
      if (!c) { onclear(); return; }
      if (c.sites.length === 1) { onselect(c.sites[0]); return; }
      const g = unproject(view, c.x, c.y, radius(), width / 2, height / 2);
      if (g) flyTo({ lon: g[0], lat: g[1], scale: scale * 2.2 });
      return;
    }
    // Inertia from the last ~100 ms of the drag (not when the pointer rested before letting go).
    const s = d.samples;
    const last = s[s.length - 1];
    if (!reduceMotion && !d.rollMode && s.length >= 2 && performance.now() - last.t < 60) {
      const first = s[0];
      const dt = (last.t - first.t) / 1000;
      if (dt > 0.02) {
        const vlon = wrapLon(last.lon - first.lon) / dt;
        const vlat = (last.lat - first.lat) / dt;
        if (Math.hypot(vlon, vlat) > 12) inertia = { vlon: Math.max(-240, Math.min(240, vlon)), vlat: Math.max(-240, Math.min(240, vlat)) };
      }
    }
    touched();
  }

  function zoomTo(next: number, px: number, py: number) {
    const s = clampScale(next);
    if (s === scale) return;
    const rOld = radius();
    const rNew = fitRadius(width, height) * s;
    view = zoomAt(view, px, py, width / 2, height / 2, rOld, rNew);
    scale = s;
    touched();
  }

  function onWheel(e: WheelEvent) {
    e.preventDefault();
    fly = null;
    inertia = null;
    const [px, py] = local(e);
    const unit = e.deltaMode === 1 ? 0.05 : e.deltaMode === 2 ? 0.5 : 0.0016;
    const k = e.ctrlKey ? 5 : 1; // a trackpad pinch arrives as ctrl+wheel with small deltas
    zoomTo(scale * Math.exp(-e.deltaY * unit * k), px, py);
  }

  function onDblClick(e: MouseEvent) {
    if ((e.target as HTMLElement).closest('button, a')) return;
    const [px, py] = local(e);
    const g = unproject(view, px, py, radius(), width / 2, height / 2);
    if (g) flyTo({ lon: g[0], lat: g[1], scale: scale * 1.6 });
  }

  // ── Keyboard and buttons ─────────────────────────────────────────────────────────────────────
  /** Move the centre towards a screen direction (+1 right / up) by `deg` degrees, in the screen frame. */
  function turn(dx: number, dy: number, deg: number) {
    const a = Math.sin(deg * DEG);
    const R = radius();
    const g = unproject(view, width / 2 + dx * a * R, height / 2 - dy * a * R, R, width / 2, height / 2);
    if (!g) return;
    const next = dragView(view, g[0], g[1], 0, 0);
    flyTo({ lon: next.lon, lat: next.lat, roll: next.roll }, 180);
  }
  function zoomBy(f: number) {
    flyTo({ scale: scale * f }, 220);
  }
  function resetNorth() {
    flyTo({ roll: 0 }, 350);
  }
  function home() {
    flyTo({ lon: HOME.lon, lat: HOME.lat, roll: 0, scale: HOME.scale }, 700);
  }

  /** Called by the dialog for keys that are not typed into a field. Returns true when the key was used. */
  export function handleKey(e: KeyboardEvent): boolean {
    if (e.ctrlKey || e.metaKey || e.altKey) return false;
    const step = keyStepDeg(scale);
    switch (e.key) {
      case 'ArrowLeft': if (e.shiftKey) flyTo({ roll: view.roll - 10 }, 120); else turn(-1, 0, step); break;
      case 'ArrowRight': if (e.shiftKey) flyTo({ roll: view.roll + 10 }, 120); else turn(1, 0, step); break;
      case 'ArrowUp': turn(0, 1, step); break;
      case 'ArrowDown': turn(0, -1, step); break;
      case '+': case '=': zoomBy(1.4); break;
      case '-': case '_': zoomBy(1 / 1.4); break;
      case 'Home': home(); break;
      case 'n': case 'N': resetNorth(); break;
      default: return false;
    }
    e.preventDefault();
    return true;
  }

  export function getView(): { lon: number; lat: number; scale: number; roll: number; radius: number } {
    return { lon: view.lon, lat: view.lat, scale, roll: view.roll, radius: radius() };
  }

  /** The tile cache has something new (called by the dialog's tile loader). */
  export function tilesChanged(): void {
    surface_?.syncTiles();
    if (surface_) tileStatus = { ...surface_.status };
    request();
  }

  function toFlat() {
    onflat(view.lon, view.lat, globeToFlatZoom(radius()));
  }

  function fmtPos(lon: number, lat: number): string {
    return `${Math.abs(lat).toFixed(1)}°${lat >= 0 ? 'N' : 'S'}, ${Math.abs(wrapLon(lon)).toFixed(1)}°${wrapLon(lon) >= 0 ? 'E' : 'W'}`;
  }
  function describe(): string {
    return `Globe centred near ${fmtPos(view.lon, view.lat)}, zoom ${scale.toFixed(1)} times; ${lastFacing} of ${sites.length} radars are on the visible side.`;
  }

  // ── Wiring ───────────────────────────────────────────────────────────────────────────────────
  $effect(() => {
    // Anything that changes what is drawn.
    void sites; void selected; void $themeRevision;
    request();
  });
  $effect(() => {
    void basemap; void $themeRevision; void geo;
    applyBasemap();
  });
  $effect(() => {
    const f = focus;
    if (!f || f.seq === lastSeq) return;
    lastSeq = f.seq;
    if (!renderer) return;
    // Recentre unless the site is already comfortably in view at a good zoom.
    const min = f.minScale ?? scale;
    const p = projectVec(viewBasis(view), lonLatToVec(f.lon, f.lat), radius(), width / 2, height / 2);
    const inView = p.depth > 0.3 && Math.hypot(p.x - width / 2, p.y - height / 2) < 0.35 * Math.min(width, height) && scale >= min;
    if (!inView) flyTo({ lon: f.lon, lat: f.lat, scale: Math.max(scale, min) });
  });

  let mql: MediaQueryList | null = null;
  const onMq = () => { reduceMotion = !!mql?.matches; if (reduceMotion) { spin = false; inertia = null; } };

  onMount(() => {
    mql = window.matchMedia?.('(prefers-reduced-motion: reduce)') ?? null;
    reduceMotion = !!mql?.matches;
    mql?.addEventListener?.('change', onMq);
    const r0 = el.getBoundingClientRect();
    width = Math.max(200, Math.round(r0.width));
    height = Math.max(200, Math.round(r0.height));
    view = { lon: initial.lon, lat: Math.max(-MAX_CENTRE_LAT, Math.min(MAX_CENTRE_LAT, initial.lat)), roll: initial.roll ?? 0 };
    scale = clampScale(initial.scale);
    if (focus) {
      lastSeq = focus.seq;
      view = { lon: focus.lon, lat: focus.lat, roll: view.roll };
      scale = clampScale(Math.max(scale, focus.minScale ?? scale));
    }
    scaleUi = scale;
    rollUi = view.roll;
    try {
      renderer = new GlobeRenderer(glCanvas, {
        onLost: () => { lostUi = true; },
        onRestored: () => { lostUi = false; request(); },
      });
    } catch (err) {
      onfail?.(err instanceof Error ? err.message : String(err));
      return;
    }
    surface_ = new GlobeSurface(renderer, tiles, () => { if (surface_) tileStatus = { ...surface_.status }; request(); });
    applyBasemap();
    void loadGlobeGeo().then((g) => {
      if (disposed) return;
      geo = g;
      lines = buildGlobeLines(g);
      request();
    });
    ro = new ResizeObserver(() => {
      const r = el.getBoundingClientRect();
      width = Math.max(200, Math.round(r.width));
      height = Math.max(200, Math.round(r.height));
      request();
    });
    ro.observe(el);
    status = describe();
    request();
  });

  onDestroy(() => {
    disposed = true;
    if (raf) cancelAnimationFrame(raf);
    if (settleTimer) clearTimeout(settleTimer);
    if (perfTimer) clearTimeout(perfTimer);
    ro?.disconnect();
    mql?.removeEventListener?.('change', onMq);
    surface_?.dispose();
    renderer?.dispose();
    renderer = null;
    surface_ = null;
  });
</script>

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<div
  class="globe"
  class:grabbing
  bind:this={el}
  tabindex="0"
  role="application"
  aria-label="Interactive globe of radar sites. Drag to turn, scroll or pinch to zoom, arrow keys turn, plus and minus zoom, Home resets, N points north up. The search box and the list on the left do the same without the globe."
  onpointerdown={onPointerDown}
  onpointermove={onPointerMove}
  onpointerup={onPointerUp}
  onpointercancel={onPointerUp}
  onwheel={onWheel}
  ondblclick={onDblClick}
  onpointerleave={() => (hover = null)}
  data-testid="radar-globe"
>
  <canvas class="gl" bind:this={glCanvas} style:width="{width}px" style:height="{height}px" aria-hidden="true"></canvas>
  <canvas class="ov" bind:this={ovCanvas} style:width="{width}px" style:height="{height}px" aria-hidden="true"></canvas>

  {#if hover}
    <div class="tip" style:left="{Math.min(Math.max(hover.x, 80), width - 80)}px" style:top="{hover.y - 14}px" role="tooltip">{hover.text}</div>
  {/if}

  <div class="ctl" role="group" aria-label="Globe controls">
    <button type="button" aria-label="Zoom in" title="Zoom in (+)" onclick={() => zoomBy(1.4)}>+</button>
    <button type="button" aria-label="Zoom out" title="Zoom out (-)" onclick={() => zoomBy(1 / 1.4)}>&minus;</button>
    <button type="button" class="compass" aria-label={Math.abs(rollUi) < 0.5 ? 'North is up. Reset north' : `Reset north (the globe is turned ${Math.round(rollUi)} degrees)`} title="Reset north (N)" onclick={resetNorth}>
      <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" style:transform="rotate({rollUi}deg)">
        <path d="M12 3 L16 13 L12 11.5 L8 13 Z" fill="currentColor" />
        <path d="M12 21 L16 13 L12 11.5 L8 13 Z" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round" />
      </svg>
    </button>
    <button type="button" aria-label="Reset the view" title="Reset the view (Home)" onclick={home}>&#8962;</button>
    <button type="button" aria-label="Turn the globe slowly" aria-pressed={spin} disabled={reduceMotion} title={reduceMotion ? 'Off: your system asks for reduced motion' : 'Turn slowly (off by default)'} onclick={() => { spin = !spin; request(); }}>&#8635;</button>
  </div>

  {#if scaleUi >= HANDOVER_SCALE}
    <button type="button" class="handover" onclick={toFlat}>Open in flat map here</button>
  {/if}

  <div class="foot">
    {#if tileStatus.kind === 'tiles' && tileStatus.loaded < tileStatus.total}
      <span class="tstat">Map tiles {tileStatus.loaded} of {tileStatus.total}</span>
    {/if}
    {#if basemap.provider.attribution}
      <a class="attr" href={basemap.provider.attributionUrl} target="_blank" rel="noopener">{basemap.provider.attribution}</a>
    {/if}
  </div>

  {#if lostUi}
    <div class="lost" role="status">The graphics connection was lost. The globe returns by itself, or switch to the flat map.</div>
  {/if}
  <div class="sr" aria-live="polite">{status}</div>
</div>

<style>
  .globe { position: absolute; inset: 0; overflow: hidden; cursor: grab; touch-action: none; background: var(--c-surface-0); }
  .globe.grabbing { cursor: grabbing; }
  .globe:focus-visible { outline: 2px solid var(--c-accent); outline-offset: -2px; }
  canvas { position: absolute; inset: 0; display: block; }
  .ov { pointer-events: none; }
  .tip { position: absolute; transform: translate(-50%, -100%); padding: 3px 7px; border-radius: var(--radius-sm); background: var(--c-overlay); border: 1px solid var(--c-border-strong); font-size: var(--fs-xs); white-space: nowrap; pointer-events: none; box-shadow: var(--shadow-2); z-index: 2; }
  .ctl { position: absolute; right: 10px; top: 10px; display: flex; flex-direction: column; gap: 4px; z-index: 2; }
  .ctl button { width: 28px; height: 28px; min-width: 0; padding: 0; font-size: var(--fs-md); display: grid; place-items: center; }
  .compass svg { transition: transform 0.15s linear; }
  @media (prefers-reduced-motion: reduce) { .compass svg { transition: none; } }
  .handover { position: absolute; left: 50%; bottom: 14px; transform: translateX(-50%); z-index: 2; background: var(--c-overlay); border-color: var(--c-accent); color: var(--c-accent); }
  .foot { position: absolute; right: 6px; bottom: 4px; display: flex; gap: var(--space-2); align-items: center; z-index: 1; }
  .attr, .tstat { font-size: var(--fs-2xs); color: var(--c-text-dim); background: rgba(var(--c-surface-0-rgb), 0.7); padding: 1px 5px; border-radius: var(--radius-sm); }
  .lost { position: absolute; left: 50%; top: 14px; transform: translateX(-50%); z-index: 3; padding: 4px 10px; background: var(--c-overlay); border: 1px solid var(--c-caution); border-radius: var(--radius-sm); font-size: var(--fs-xs); }
  .sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
</style>
