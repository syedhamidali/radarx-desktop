<script lang="ts">
  /**
   * CartesianViewer3D: the 3D tab for gridded scenes - the merged mosaic volume and the
   * multi-Doppler wind grids (3D arrows). Same look and controls as the polar radar volume
   * (RadarViewer3D): transfer function editor, "hide below", vertical exaggeration, ground map,
   * lighting, quality governor, orbit/zoom/pan camera - drawn by cartesianRenderer.ts.
   */
  import { onMount, onDestroy, untrack } from 'svelte';
  import { settings } from '../stores/settings';
  import { mosaicResult } from '../stores/mosaic';
  import { windResult } from '../stores/wind';
  import {
    source3d, mosaicVolume, windVolume, volume3dLoading, fetchMosaicVolume, fetchWindVolume,
  } from '../stores/volume3d';
  import { cmapOverrides, effectiveCmap, chooseCmap, isCategoricalVariable } from '../stores/cmapChoice';
  import { COLORMAP_DATA } from '../utils/colormaps';
  import { getDefaultRange } from '../utils/ppiRenderer';
  import { resolveBasemap } from '../utils/basemaps';
  import { buildGroundMap } from '../utils/groundMap';
  import { scaleLut } from '../utils/wind';
  import {
    variableKind, buildTf, recolorTf, thresholdSpec, thresholdToShader, adaptiveShiftDb,
    type VolumeVarKind,
  } from '../utils/volumeTf';
  import { loadPref, savePref, VEXAG_MIN, VEXAG_MAX, VEXAG_DEFAULT } from '../utils/scene3d';
  import {
    windDbzVolume, gridBox, unionBox, translateBox, halfExtent, aeqdOffsetKm, sceneIsLight, framePose, gridStepFor,
    type CartVolume, type SceneBox, type SceneSite,
  } from '../utils/cartesianVolume';
  import {
    buildGlyphs, windStats, autoStrides, arrowPalette, lutFromCss, DEFAULT_GLYPH_OPTIONS, type GlyphColorBy, type GlyphSet,
  } from '../utils/windGlyphs';
  import { CartesianRenderer, type CamFrame } from '../utils/cartesianRenderer';
  import { drawCartesianOverlay } from '../utils/cartesianOverlay';
  import { yieldToMain, type QualityMode, type GovernorState } from '../utils/qualityGovernor';
  import TransferFunctionEditor from './TransferFunctionEditor.svelte';
  import CmapSelect from './CmapSelect.svelte';

  // ── Sources ───────────────────────────────────────────────────────────────
  const src = $derived($source3d);
  const mv = $derived($mosaicVolume);
  const wv = $derived($windVolume);
  const loading = $derived($volume3dLoading);

  type BaseVol = 'mosaic' | 'wind' | 'none';
  /** What is ray-marched: the mosaic's merged field, the wind grid's own reflectivity, or nothing. */
  let baseVol = $state<BaseVol>('mosaic');
  let showWind = $state(true);

  // Pick sensible defaults when the source changes or data arrives.
  $effect(() => {
    const s = src;
    const hasM = !!mv;
    const hasW = !!wv;
    untrack(() => {
      if (s === 'mosaic') {
        baseVol = hasM ? 'mosaic' : hasW ? 'wind' : 'none';
        showWind = hasW;
      } else {
        baseVol = hasM ? 'mosaic' : hasW ? 'wind' : 'none';
        showWind = true;
      }
    });
  });

  const windDbz = $derived.by(() => (wv ? windDbzVolume(wv) : null));
  const volume = $derived<CartVolume | null>(baseVol === 'mosaic' ? mv : baseVol === 'wind' ? windDbz : null);
  const windOn = $derived(!!wv && showWind);

  /** Origin of the scene frame: the volume's AEQD origin, else the wind grid's. */
  const frame = $derived(
    volume ? { lat: volume.originLat, lon: volume.originLon } : wv ? { lat: wv.originLat, lon: wv.originLon } : mv ? { lat: mv.originLat, lon: mv.originLon } : null,
  );
  const volOffset = $derived<[number, number]>(
    frame && volume ? aeqdOffsetKm(frame.lat, frame.lon, volume.originLat, volume.originLon) : [0, 0],
  );
  const windOffset = $derived<[number, number]>(frame && wv ? aeqdOffsetKm(frame.lat, frame.lon, wv.originLat, wv.originLon) : [0, 0]);

  const sceneBox = $derived.by((): SceneBox | null => {
    let b: SceneBox | null = null;
    if (volume) b = translateBox(gridBox(volume), volOffset[0], volOffset[1]);
    if (wv && windOn) b = unionBox(b, translateBox(gridBox(wv), windOffset[0], windOffset[1]));
    return b;
  });

  const sites = $derived.by((): SceneSite[] => {
    const out: SceneSite[] = [];
    const seen = new Set<string>();
    const add = (list: SceneSite[], off: [number, number]) => {
      for (const s of list) {
        const key = `${s.name}`;
        if (seen.has(key)) continue;
        seen.add(key);
        out.push({ ...s, x: s.x + off[0], y: s.y + off[1] });
      }
    };
    if (volume) add(volume.sites, volOffset);
    if (wv && windOn) add(wv.sites, windOffset);
    return out;
  });

  // ── Scene theme ───────────────────────────────────────────────────────────
  type BgMode = 'auto' | 'dark' | 'light';
  let bgMode = $state<BgMode>(loadPref<string>('cartBg', 'auto', (x) => x === 'auto' || x === 'dark' || x === 'light') as BgMode);
  let systemLight = $state(false);
  const lightScene = $derived(sceneIsLight(bgMode === 'auto' ? $settings.theme.mode : bgMode, systemLight));

  // ── Variable / transfer function ──────────────────────────────────────────
  const variable = $derived(volume?.variable ?? 'DBZH');
  const units = $derived(volume?.units ?? 'dBZ');
  const vmin = $derived(volume?.vmin ?? (getDefaultRange('DBZH')?.[0] ?? -20));
  const vmax = $derived(volume?.vmax ?? (getDefaultRange('DBZH')?.[1] ?? 75));
  const kind = $derived<VolumeVarKind>(variableKind(variable, false));
  const volumeLabel = $derived(volume?.kind === 'mosaic' ? `${variable} mosaic` : volume ? 'Wind-grid reflectivity' : '');

  let cmap = $state('turbo');
  let tfData = $state(buildTf('reflectivity', COLORMAP_DATA['turbo'], -20, 75));
  let tfSyncKey = $state(0);
  let tfShift = $state(0);
  let tfTouched = false;
  let thrTouched = false;
  let showTfe = $state(false);
  let thrValue = $state(15);
  const thrSpec = $derived(thresholdSpec(kind, vmin, vmax, units));

  const cmapGradient = $derived.by(() => {
    const stops: string[] = [];
    for (let i = 0; i <= 12; i++) {
      const idx = Math.min(Math.floor((i / 12) * 255), 255);
      stops.push(`rgb(${tfData[idx * 4]},${tfData[idx * 4 + 1]},${tfData[idx * 4 + 2]}) ${((i / 12) * 100).toFixed(1)}%`);
    }
    return `linear-gradient(to top, ${stops.join(', ')})`;
  });

  let loadedKey = '';
  /** A new volume: colours of its colormap (user's choice), default opacity curve, adapted to its strength. */
  function resetTfFor(v: CartVolume) {
    cmap = effectiveCmap(v.variable);
    const k = variableKind(v.variable, false);
    const rgb = COLORMAP_DATA[cmap] ?? COLORMAP_DATA['turbo'];
    tfTouched = false;
    thrTouched = false;
    let shift = 0;
    if (k === 'reflectivity') {
      const hist = new Float64Array(256);
      for (let i = 0; i < v.data.length; i += 3) hist[v.data[i]]++;
      let sum = 0;
      for (let i = 1; i < 256; i++) sum += hist[i];
      if (sum > 2000) {
        let acc = 0;
        let p995 = v.vmax;
        for (let i = 1; i < 256; i++) {
          acc += hist[i];
          if (acc >= 0.995 * sum) { p995 = v.vmin + ((i - 1) / 254) * (v.vmax - v.vmin); break; }
        }
        shift = adaptiveShiftDb(p995);
      }
    }
    tfShift = shift;
    tfData = buildTf(k, rgb, v.vmin, v.vmax, 'default', shift);
    const spec = thresholdSpec(k, v.vmin, v.vmax, v.units);
    thrValue = spec ? (k === 'reflectivity' ? Math.max(Math.max(spec.min, 5), Math.min(spec.max, 15 - shift)) : spec.value) : 0;
    tfSyncKey++;
  }

  function applyCmap(name: string) {
    if (!(name in COLORMAP_DATA)) return;
    cmap = name;
    tfData = recolorTf(tfData, COLORMAP_DATA[name]);
  }
  function onCmapPick(name: string) {
    chooseCmap(variable, name);
    applyCmap(name);
  }
  // A colormap chosen elsewhere (2D view, polar 3D view) for this variable.
  $effect(() => {
    void $cmapOverrides;
    const v = variable;
    untrack(() => {
      if (!volume) return;
      const want = effectiveCmap(v);
      if (want !== cmap) applyCmap(want);
    });
  });

  // ── Scene controls ────────────────────────────────────────────────────────
  let vexag = $state<number>(loadPref<number>('vexag', VEXAG_DEFAULT, (x) => (x as number) >= VEXAG_MIN && (x as number) <= VEXAG_MAX));
  let stepKm = $state(0.5);
  let opacityScale = $state(0.8);
  type GroundMode = 'map' | 'grid' | 'off';
  let groundMode = $state<GroundMode>(loadPref<string>('ground', 'map', (x) => x === 'map' || x === 'grid' || x === 'off') as GroundMode);
  let showLight = $state<boolean>(loadPref<boolean>('light', false));

  // wind controls
  let wLevelLo = $state(0);
  let wLevelHi = $state(0);
  let wStrideH = $state(DEFAULT_GLYPH_OPTIONS.strideH);
  let wStrideV = $state(DEFAULT_GLYPH_OPTIONS.strideV);
  let wLength = $state(1);
  let wWidth = $state(1);
  let wColor = $state<GlyphColorBy>('none');
  let wMinCrossing = $state(30);
  let glyphInfo = $state({ count: 0, strideH: 3, strideV: 2, kmPerMs: 0, colorMax: 0 });
  const wStats = $derived(wv ? windStats(wv) : null);
  const maxCrossing = $derived(wv?.fields.crossing_angle ? 90 : 0);

  $effect(() => {
    const w = wv;
    untrack(() => {
      if (!w) return;
      wLevelLo = 0;
      wLevelHi = w.nz - 1;
      const st = autoStrides(w);
      wStrideH = st.strideH;
      wStrideV = st.strideV;
    });
  });

  // ── Quality governor ──────────────────────────────────────────────────────
  let qState = $state<GovernorState | null>(null);
  let unsubGov: (() => void) | null = null;
  const QUALITY_OPTIONS: { value: QualityMode; label: string }[] = [
    { value: 'auto', label: 'Auto' }, { value: 'high', label: 'High' }, { value: 'medium', label: 'Medium' }, { value: 'low', label: 'Low' },
  ];
  function onQualityChange(e: Event) {
    renderer?.governor.setMode((e.target as HTMLSelectElement).value as QualityMode);
    // the texture budget follows the quality ceiling
    if (volume) void renderer?.setVolume(volume, volOffset);
    renderer?.markDirty();
  }

  // ── Renderer ──────────────────────────────────────────────────────────────
  let container = $state<HTMLDivElement | null>(null);
  let overlayCanvas = $state<HTMLCanvasElement | null>(null);
  let renderer: CartesianRenderer | null = null;
  let fatalMsg = $state('');
  let ready = $state(false);
  let compassAngle = $state(0);
  let overlayCtx: CanvasRenderingContext2D | null = null;
  let overlayDpr = 1;
  let camTouched = false;
  let camPreset: 'oblique' | 'top' | 'side' = 'oblique';
  let framedKey = '';

  function sizeOverlay() {
    if (!overlayCanvas || !container) return;
    overlayDpr = Math.min(window.devicePixelRatio || 1, 2);
    const rect = container.getBoundingClientRect();
    const w = Math.max(1, Math.round(rect.width * overlayDpr));
    const h = Math.max(1, Math.round(rect.height * overlayDpr));
    if (overlayCanvas.width !== w || overlayCanvas.height !== h) {
      overlayCanvas.width = w;
      overlayCanvas.height = h;
      overlayCanvas.style.width = rect.width + 'px';
      overlayCanvas.style.height = rect.height + 'px';
    }
  }

  function onFrame(f: CamFrame) {
    if (Math.abs(f.compass - compassAngle) > 0.25) compassAngle = f.compass;
    if (!overlayCanvas || !container || !sceneBox) return;
    try {
      if (!overlayCtx) overlayCtx = overlayCanvas.getContext('2d');
      if (!overlayCtx) return;
      sizeOverlay();
      const cssW = overlayCanvas.width / overlayDpr;
      const cssH = overlayCanvas.height / overlayDpr;
      overlayCtx.setTransform(overlayDpr, 0, 0, overlayDpr, 0, 0);
      const note = windOn && glyphInfo.count > 0 && glyphInfo.kmPerMs > 0
        ? `Arrows: 10 m/s = ${(10 * glyphInfo.kmPerMs).toFixed(1)} km` : undefined;
      drawCartesianOverlay(overlayCtx, { eye: f.eye, fwd: f.fwd, right: f.right, up: f.up, tanX: f.tanX, tanY: f.tanY, w: cssW, h: cssH }, {
        box: sceneBox, vexag, sites, lightScene, showFrame: true, note,
      });
    } catch (e) {
      console.warn('[Cartesian3D] overlay failed:', e);
    }
  }

  function resetCamera(preset: 'oblique' | 'top' | 'side' = 'oblique', instant = false) {
    if (!renderer || !sceneBox || !container) return;
    camPreset = preset;
    const rect = container.getBoundingClientRect();
    const asp = rect.height > 0 ? rect.width / rect.height : 1.6;
    const pose = framePose(sceneBox, vexag, asp, Math.PI / 4, preset);
    renderer.setPose(pose, instant);
  }

  onMount(() => {
    systemLight = !!window.matchMedia?.('(prefers-color-scheme: light)').matches;
    try {
      renderer = new CartesianRenderer(container!, {
        onFatal: (m) => { fatalMsg = m; },
        onFrame,
        onUserCamera: () => { camTouched = true; },
      });
    } catch (e) {
      fatalMsg = e instanceof Error ? e.message : String(e);
      return;
    }
    unsubGov = renderer.governor.state.subscribe((s) => { qState = s; });
    (globalThis as any).__xrCart = {
      stats: () => renderer?.stats(),
      state: () => ({ cmap, vexag, groundMode, lightScene, baseVol, windOn, glyphInfo, thrValue, hasGround: renderer?.hasGroundMap }),
      cam: () => renderer && { theta: renderer.camTheta, phi: renderer.camPhi, dist: renderer.camDist, tgt: renderer.camTgt },
      glyphCenter: () => {
        const g = glyphSet;
        if (!g || !g.count) return null;
        let x = 0, y = 0, z = 0;
        for (let i = 0; i < g.count; i++) { x += g.data[i * 8]; y += g.data[i * 8 + 1]; z += g.data[i * 8 + 2]; }
        return [x / g.count, y / g.count, z / g.count];
      },
      setCam: (p: { theta?: number; phi?: number; dist?: number; tgt?: [number, number, number] }) => {
        if (!renderer) return;
        if (p.tgt) renderer.camTgt = p.tgt;
        if (p.theta !== undefined) renderer.camTheta = p.theta;
        if (p.phi !== undefined) renderer.camPhi = p.phi;
        if (p.dist !== undefined) renderer.camDist = p.dist;
        camTouched = true;
        renderer.setPose({ theta: renderer.camTheta, phi: renderer.camPhi, dist: renderer.camDist, target: [...renderer.camTgt] as [number, number, number] }, true);
      },
      reset: (preset?: 'oblique' | 'top' | 'side') => resetCamera(preset ?? 'oblique', true),
    };
    ready = true;
  });

  onDestroy(() => {
    unsubGov?.();
    if (groundTimer) clearTimeout(groundTimer);
    groundGen++;
    renderer?.dispose();
    renderer = null;
    delete (globalThis as any).__xrCart;
  });

  // volume -> GPU (and its colours)
  $effect(() => {
    const r = ready;
    const v = volume;
    const off = volOffset;
    if (!r) return;
    untrack(() => {
      if (!renderer) return;
      if (v) {
        const key = `${v.id}|${v.vmin}|${v.vmax}|${v.variable}`;
        if (key !== loadedKey) { loadedKey = key; resetTfFor(v); }
      } else {
        loadedKey = '';
      }
      void renderer.setVolume(v, off).catch((e) => {
        console.error('[Cartesian3D] volume upload failed:', e);
        fatalMsg = `Could not build the 3D volume (${e instanceof Error ? e.message : String(e)}).`;
      });
    });
  });

  // transfer function
  $effect(() => {
    const t = tfData;
    if (!ready) return;
    untrack(() => renderer?.setTransfer(t));
  });

  // scalar scene parameters
  $effect(() => {
    const ve = vexag;
    const ts = thrSpec ? thresholdToShader(thrSpec, thrValue, vmin, vmax) : { thr: 0, soft: 0.03, mode: 0 as 0 | 1 };
    const p = {
      vexag: ve, stepKm, opacity: opacityScale, thr: ts.thr, thrSoft: ts.soft, thrMode: ts.mode,
      groundMode, light: showLight, lightScene, showVolume: baseVol !== 'none',
      gridStepKm: gridStepFor(sceneBox ? Math.max(sceneBox.xMax - sceneBox.xMin, sceneBox.yMax - sceneBox.yMin) : 100),
    };
    if (!ready) return;
    untrack(() => { renderer?.setParams(p); renderer?.interact(); });
  });

  // scene box + camera framing
  $effect(() => {
    const b = sceneBox;
    const r = ready;
    if (!r) return;
    untrack(() => {
      renderer?.setSceneBox(b);
      if (b) {
        const key = `${b.xMin.toFixed(1)},${b.xMax.toFixed(1)},${b.yMin.toFixed(1)},${b.yMax.toFixed(1)},${b.zMax.toFixed(2)}`;
        if (key !== framedKey) { framedKey = key; if (!camTouched || true) { camTouched = false; resetCamera('oblique', true); } }
        scheduleGround(300);
      }
    });
  });

  function onVexagInput(e: Event) {
    const nv = Math.max(VEXAG_MIN, Math.min(VEXAG_MAX, Number((e.target as HTMLInputElement).value)));
    if (nv === vexag) return;
    renderer?.scaleTargetZ(nv / vexag);
    vexag = nv;
    savePref('vexag', nv);
  }

  // ── Wind arrows ───────────────────────────────────────────────────────────
  let glyphSet: GlyphSet | null = null;
  $effect(() => {
    const w = wv;
    const on = windOn;
    const stats = wStats;
    const lo = wLevelLo, hi = wLevelHi, sh = wStrideH, sv = wStrideV, len = wLength, col = wColor, minC = wMinCrossing, ve = vexag;
    const off = windOffset;
    if (!ready) return;
    untrack(() => {
      if (!renderer) return;
      if (!w || !on || !stats) {
        glyphSet = null;
        glyphInfo = { count: 0, strideH: sh, strideV: sv, kmPerMs: 0, colorMax: 0 };
        renderer.setGlyphs(null, 0);
        return;
      }
      const zl = w.z[Math.max(0, Math.min(lo, hi))];
      const zh = w.z[Math.min(w.nz - 1, Math.max(lo, hi))];
      glyphSet = buildGlyphs(w, {
        ...DEFAULT_GLYPH_OPTIONS,
        zMinKm: zl, zMaxKm: zh, strideH: sh, strideV: sv, minCrossing: minC, lengthScale: len, vexag: ve,
        colorBy: col, offsetX: off[0], offsetY: off[1],
      }, stats);
      glyphInfo = { count: glyphSet.count, strideH: glyphSet.strideH, strideV: glyphSet.strideV, kmPerMs: glyphSet.kmPerMs, colorMax: glyphSet.colorMax };
      renderer.setGlyphs(glyphSet.data, glyphSet.count);
    });
  });

  // arrow colours: white on a dark scene, dark on a light one; the outline is the opposite
  $effect(() => {
    const light = lightScene;
    const col = wColor;
    const width = wWidth;
    if (!ready) return;
    untrack(() => {
      const pal = arrowPalette(light);
      renderer?.setGlyphStyle({
        colorMode: col === 'none' ? 0 : 1,
        lut: col === 'none' ? undefined : lutFromCss(scaleLut(col === 'w' ? 'diverging' : 'sequential')),
        fill: pal.fill, outline: pal.outline, width,
      });
    });
  });

  const legendGradient = $derived(
    wColor === 'w'
      ? 'linear-gradient(90deg, rgb(33,102,172), rgb(103,169,207), rgb(247,247,247), rgb(239,138,98), rgb(178,24,43))'
      : 'linear-gradient(90deg, rgb(68,1,84), rgb(59,82,139), rgb(33,145,140), rgb(94,201,98), rgb(253,231,37))',
  );

  // ── Ground map ────────────────────────────────────────────────────────────
  let groundTimer: ReturnType<typeof setTimeout> | null = null;
  let groundGen = 0;
  let groundKey = '';
  function scheduleGround(delay = 400) {
    if (groundTimer) clearTimeout(groundTimer);
    groundTimer = setTimeout(() => { groundTimer = null; void refreshGround(); }, delay);
  }
  async function refreshGround() {
    const gen = ++groundGen;
    const b = sceneBox;
    const fr = frame;
    const bm = resolveBasemap($settings.mapStyle, $settings.theme.mode);
    if (!renderer || groundMode !== 'map' || !b || !fr || !bm.provider.url) return;
    const half = Math.ceil(halfExtent(b) * 1.02);
    const th = lightScene ? 'light' : 'dark';
    const key = `${fr.lat.toFixed(3)},${fr.lon.toFixed(3)}|${half}|${bm.provider.id}|${th}`;
    if (key === groundKey && renderer.hasGroundMap) return;
    let res = null;
    try {
      res = await buildGroundMap({ lat: fr.lat, lon: fr.lon, radiusKm: half, size: 1024, provider: bm.provider, theme: th }, () => gen !== groundGen || !renderer);
    } catch (e) {
      console.warn('[Cartesian3D] ground map failed:', e);
    }
    if (!res || gen !== groundGen || !renderer) return;
    renderer.setGround(res.size, res.data, half);
    groundKey = key;
  }
  $effect(() => {
    void groundMode; void lightScene; void $settings.mapStyle;
    if (!ready) return;
    untrack(() => { groundKey = ''; scheduleGround(0); });
  });

  // ── Misc UI ───────────────────────────────────────────────────────────────
  function setGroundMode(m: GroundMode) { groundMode = m; savePref('ground', m); }
  function onBgChange(e: Event) {
    bgMode = (e.target as HTMLSelectElement).value as BgMode;
    savePref('cartBg', bgMode);
  }
  async function loadFromCurrent() {
    if (src === 'wind' && $windResult) await fetchWindVolume($windResult.wind_id);
    else if ($mosaicResult) await fetchMosaicVolume($mosaicResult.mosaic_id);
  }
  const canLoad = $derived(src === 'wind' ? !!$windResult : !!$mosaicResult?.has_volume);
  const emptyReason = $derived(
    src === 'wind'
      ? 'No wind analysis loaded. Run the Wind panel and press "View in 3D".'
      : $mosaicResult && !$mosaicResult.has_volume
        ? ($mosaicResult.volume_note ?? 'This mosaic was created without its 3D volume; create it again.')
        : 'No mosaic loaded. Create one in the Mosaic panel and press "View in 3D".',
  );
  const busy = $derived(loading.mosaic || loading.wind);
  void yieldToMain;
</script>

<div class="viewer-3d cart" class:light={lightScene}>
  <div class="controls-3d">
    <span class="label-3d">3D {src === 'wind' ? 'Wind' : 'Mosaic'}</span>
    {#if volume}<span class="var-badge" title={volumeLabel}>{volumeLabel}</span>{/if}

    {#if mv || wv}
      <div class="ctrl-group" title="What is ray-marched underneath (the wind arrows are drawn on top)">
        <label for="basevol-ctrl">Volume</label>
        <select id="basevol-ctrl" class="quality-sel" bind:value={baseVol}>
          {#if mv}<option value="mosaic">Mosaic reflectivity</option>{/if}
          {#if wv && wv.fields.dbz}<option value="wind">Wind-grid reflectivity</option>{/if}
          <option value="none">None</option>
        </select>
      </div>
    {/if}

    <div class="ctrl-group">
      <label for="step-ctrl">Step</label>
      <input id="step-ctrl" type="range" min="0.25" max="3" step="0.25" bind:value={stepKm} />
      <span class="ctrl-val">{stepKm}km</span>
    </div>
    <div class="ctrl-group">
      <label for="opacity-ctrl">Opacity</label>
      <input id="opacity-ctrl" type="range" min="0.1" max="1" step="0.05" bind:value={opacityScale} />
      <span class="ctrl-val">{Math.round(opacityScale * 100)}%</span>
    </div>
    <div class="ctrl-group">
      <label for="quality-ctrl">Quality</label>
      <select id="quality-ctrl" class="quality-sel" value={qState?.mode ?? 'auto'} onchange={onQualityChange}
              title="Auto adapts to your GPU to keep the app responsive.">
        {#each QUALITY_OPTIONS as o (o.value)}<option value={o.value}>{o.label}</option>{/each}
      </select>
      {#if qState}
        <span class="q-chip" class:weak={qState.weakRenderer}
              title={`GPU: ${qState.gpu || 'unknown'}\nFrame: ${qState.frameMsEma.toFixed(0)} ms (smoothed)\nRender scale: ${Math.round(qState.renderScale * 100)}%`}>
          {qState.idleLevel}{qState.weakRenderer ? ' · software GPU' : ''}
        </span>
      {/if}
    </div>
    <div class="ctrl-group views" role="group" aria-label="Camera views">
      <button class="reset-btn" onclick={() => resetCamera('oblique')} title="Reset camera (R)">Reset</button>
      <button class="reset-btn" onclick={() => resetCamera('oblique')} title="Oblique view from the south">Oblique</button>
      <button class="reset-btn" onclick={() => resetCamera('side')} title="Side view: shows the vertical structure">Side</button>
      <button class="reset-btn" onclick={() => resetCamera('top')} title="Top-down view, north up">Top</button>
    </div>
    {#if volume}
      <button class="reset-btn" class:active={showTfe} onclick={() => (showTfe = !showTfe)} title="Transfer function editor">TF</button>
    {/if}
    {#if busy}<span class="sweep-count">loading…</span>{/if}
  </div>

  <div class="controls-3d row2" aria-label="Scene controls">
    {#if volume && thrSpec}
      <div class="ctrl-group" title="Echoes weaker than this are hidden. Raise it to isolate the strong cores.">
        <label for="thr-ctrl">{thrSpec.label}</label>
        <input id="thr-ctrl" type="range" min={thrSpec.min} max={thrSpec.max} step={thrSpec.step}
               bind:value={thrValue} oninput={() => { thrTouched = true; }} />
        <span class="ctrl-val wide">{thrValue}{thrSpec.unit ? ' ' + thrSpec.unit : ''}</span>
      </div>
    {/if}
    <div class="ctrl-group" title="Stretch the vertical axis. A radar volume is ~15 km tall and hundreds of km wide, so it needs stretching to read.">
      <label for="vex-ctrl">Vertical</label>
      <input id="vex-ctrl" type="range" min={VEXAG_MIN} max={VEXAG_MAX} step="1" value={vexag} oninput={onVexagInput} />
      <span class="ctrl-val">{vexag}×</span>
    </div>
    <div class="ctrl-group">
      <label for="ground-ctrl">Ground</label>
      <select id="ground-ctrl" class="quality-sel" value={groundMode} onchange={(e) => setGroundMode((e.target as HTMLSelectElement).value as GroundMode)}>
        <option value="map">Map</option>
        <option value="grid">Grid</option>
        <option value="off">Off</option>
      </select>
    </div>
    <div class="ctrl-group" title="Background of the scene. Auto follows the app theme.">
      <label for="bg-ctrl">Background</label>
      <select id="bg-ctrl" class="quality-sel" value={bgMode} onchange={onBgChange}>
        <option value="auto">Auto</option>
        <option value="dark">Dark</option>
        <option value="light">Light</option>
      </select>
    </div>
    {#if volume}
      <div class="ctrl-group">
        <label>Colormap <CmapSelect value={cmap} disabled={isCategoricalVariable(variable)} onchange={onCmapPick} /></label>
      </div>
      <label class="chk" title="Shade the echoes using the gradient of the field (costs GPU time)">
        <input type="checkbox" bind:checked={showLight} onchange={() => savePref('light', showLight)} /> Lighting
      </label>
    {/if}
  </div>

  {#if wv}
    <div class="controls-3d row2 windrow" aria-label="Wind vectors">
      <label class="chk" title="Show the retrieved wind as 3D arrows">
        <input type="checkbox" bind:checked={showWind} /> Wind vectors
      </label>
      {#if showWind}
        <div class="ctrl-group" title="Show only these levels (km above sea level)">
          <label for="wlo-ctrl">Levels</label>
          <input id="wlo-ctrl" type="range" min="0" max={wv.nz - 1} step="1" bind:value={wLevelLo} />
          <input id="whi-ctrl" type="range" min="0" max={wv.nz - 1} step="1" bind:value={wLevelHi} aria-label="Top level" />
          <span class="ctrl-val wide">{wv.z[Math.min(wLevelLo, wLevelHi)].toFixed(1)}–{wv.z[Math.max(wLevelLo, wLevelHi)].toFixed(1)} km</span>
        </div>
        <div class="ctrl-group" title="Take every n-th grid cell horizontally (1 = every cell)">
          <label for="wsh-ctrl">Density H</label>
          <input id="wsh-ctrl" type="range" min="1" max="16" step="1" bind:value={wStrideH} />
          <span class="ctrl-val">1/{glyphInfo.strideH}</span>
        </div>
        <div class="ctrl-group" title="Take every n-th level (1 = every level)">
          <label for="wsv-ctrl">Density V</label>
          <input id="wsv-ctrl" type="range" min="1" max="8" step="1" bind:value={wStrideV} />
          <span class="ctrl-val">1/{glyphInfo.strideV}</span>
        </div>
        <div class="ctrl-group">
          <label for="wlen-ctrl">Length</label>
          <input id="wlen-ctrl" type="range" min="0.25" max="3" step="0.05" bind:value={wLength} />
          <span class="ctrl-val">{wLength.toFixed(2)}×</span>
        </div>
        <div class="ctrl-group">
          <label for="wwid-ctrl">Width</label>
          <input id="wwid-ctrl" type="range" min="0.5" max="3" step="0.1" bind:value={wWidth} />
        </div>
        <div class="ctrl-group" title="Colour the arrows by speed or vertical velocity; 'None' draws them white on a dark scene, dark on a light one">
          <label for="wcol-ctrl">Colour</label>
          <select id="wcol-ctrl" class="quality-sel" bind:value={wColor}>
            <option value="none">None</option>
            <option value="speed">Speed</option>
            <option value="w">w (vertical)</option>
          </select>
        </div>
        {#if maxCrossing > 0}
          <div class="ctrl-group" title="Show only cells whose beams cross at least this angle (the wind is poorly constrained below ~30°)">
            <label for="wcr-ctrl">Crossing ≥</label>
            <input id="wcr-ctrl" type="range" min="0" max="90" step="5" bind:value={wMinCrossing} />
            <span class="ctrl-val">{wMinCrossing}°</span>
          </div>
        {/if}
        <span class="sweep-count" title="Arrows drawn">{glyphInfo.count.toLocaleString()} arrows</span>
      {/if}
    </div>
  {/if}

  {#if showTfe && volume}
    <div class="tfe-panel">
      <TransferFunctionEditor
        bind:transferFunction={tfData}
        {kind}
        {vmin}
        {vmax}
        keepColors={true}
        syncKey={tfSyncKey}
        shift={tfShift}
        onchange={() => { tfTouched = true; }}
      />
    </div>
  {/if}

  <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
  <div class="canvas-wrap" bind:this={container} tabindex="0" role="application" aria-label="3D scene"
       onkeydown={(e) => { if (e.key === 'r' || e.key === 'R') resetCamera(); }}>
    <canvas class="scene-overlay" bind:this={overlayCanvas} aria-hidden="true"></canvas>

    {#if volume}
      <div class="colorbar-wrap">
        <span class="cb-label">{vmax.toFixed(1)}</span>
        <div class="cb-bar" style="background: {cmapGradient}"></div>
        <span class="cb-label">{vmin.toFixed(1)}</span>
        <span class="cb-var">{variable} ({units})</span>
      </div>
    {/if}
    {#if windOn && wColor !== 'none' && glyphInfo.count > 0}
      <div class="arrow-legend">
        <span>{wColor === 'w' ? `w −${glyphInfo.colorMax.toFixed(0)}` : '0'}</span>
        <div class="al-bar" style="background: {legendGradient}"></div>
        <span>{wColor === 'w' ? `+${glyphInfo.colorMax.toFixed(0)}` : glyphInfo.colorMax.toFixed(0)} m/s</span>
      </div>
    {/if}

    <div class="compass-wrap">
      <svg viewBox="-20 -20 40 40" width="44" height="44">
        <circle r="18" fill="rgba(0,0,0,0.45)" stroke="rgba(255,255,255,0.12)" stroke-width="0.5"/>
        <g transform="rotate({compassAngle})">
          <polygon points="0,-15 -3,0 0,-6 3,0" fill="#e05252"/>
          <polygon points="0,15 -3,0 0,6 3,0" fill="#555"/>
        </g>
        <text transform="rotate({compassAngle}) translate(0,-22)" text-anchor="middle" dominant-baseline="middle"
              font-size="6.5" font-weight="bold" fill="#e05252" font-family="system-ui,sans-serif">N</text>
      </svg>
    </div>

    {#if fatalMsg}
      <div class="overlay error fatal">
        <p>{fatalMsg}</p>
        <p class="sub">Switch to the 2D view to keep working, or try again.</p>
        <button class="banner-btn" onclick={() => { fatalMsg = ''; renderer?.resume(); }}>Try again</button>
      </div>
    {:else if busy}
      <div class="overlay"><div class="spinner"></div><p>Loading 3D data…</p></div>
    {:else if !volume && !wv}
      <div class="overlay empty">
        <p>{emptyReason}</p>
        {#if canLoad}<button class="banner-btn" onclick={loadFromCurrent}>Load the current {src === 'wind' ? 'wind analysis' : 'mosaic'}</button>{/if}
      </div>
    {/if}
  </div>

  <div class="hint-3d">Drag · Scroll to zoom · Shift+drag to pan · Arrow keys · R to reset</div>
</div>

<style>
  .viewer-3d { display: flex; flex-direction: column; height: 100%; background: var(--bg-primary); overflow: hidden; min-height: 0; flex: 1; }
  .controls-3d {
    display: flex; align-items: center; gap: var(--spacing-md); padding: var(--spacing-sm) var(--spacing-md);
    background: var(--bg-secondary); border-bottom: 1px solid var(--border-color); flex-shrink: 0; flex-wrap: wrap;
  }
  .controls-3d.row2 { padding-top: 3px; padding-bottom: 3px; }
  .label-3d { font-size: 12px; font-weight: 600; color: var(--text-secondary); white-space: nowrap; }
  .var-badge {
    font-family: var(--font-mono); font-size: 11px; color: var(--accent-primary); background: rgba(91,108,247,0.1);
    padding: 2px 8px; border-radius: var(--radius-sm); border: 1px solid rgba(91,108,247,0.2);
  }
  .ctrl-group { display: flex; align-items: center; gap: 4px; }
  .ctrl-group label, .ctrl-group :global(label) {
    font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.03em; font-weight: 600; white-space: nowrap;
    display: inline-flex; align-items: center; gap: 4px;
  }
  .ctrl-group input[type="range"] { width: 70px; accent-color: var(--accent-primary); }
  .ctrl-val { font-family: var(--font-mono); font-size: 11px; color: var(--text-secondary); min-width: 36px; }
  .ctrl-val.wide { min-width: 62px; }
  .reset-btn {
    font-size: 10px; padding: 2px 8px; border: 1px solid var(--border-color); border-radius: var(--radius-sm);
    background: var(--bg-tertiary); color: var(--text-secondary); cursor: pointer;
  }
  .reset-btn:hover, .reset-btn.active { background: var(--accent-primary); color: white; border-color: var(--accent-primary); }
  .ctrl-group.views { gap: 3px; }
  .quality-sel {
    font-size: 10px; padding: 1px 4px; border: 1px solid var(--border-color); border-radius: var(--radius-sm);
    background: var(--bg-tertiary); color: var(--text-secondary);
  }
  .q-chip {
    font-family: var(--font-mono); font-size: 10px; color: var(--text-muted); padding: 1px 6px;
    border: 1px solid var(--border-color); border-radius: var(--radius-sm); white-space: nowrap;
  }
  .q-chip.weak { color: var(--accent-warning, #d9a441); border-color: rgba(217,164,65,0.4); }
  .chk {
    display: inline-flex; align-items: center; gap: 4px; font-size: 10px; font-weight: 600; letter-spacing: 0.03em;
    color: var(--text-muted); text-transform: uppercase; cursor: pointer;
  }
  .chk input { accent-color: var(--accent-primary); }
  .sweep-count { font-family: var(--font-mono); font-size: 10px; color: var(--text-muted); margin-left: auto; }
  .tfe-panel { padding: var(--spacing-sm) var(--spacing-md); background: var(--bg-secondary); border-bottom: 1px solid var(--border-color); flex-shrink: 0; }
  .canvas-wrap { flex: 1; position: relative; overflow: hidden; cursor: grab; outline: none; min-height: 0; }
  .canvas-wrap:active { cursor: grabbing; }
  .canvas-wrap :global(canvas) { display: block; }
  .canvas-wrap canvas.scene-overlay { position: absolute; left: 0; top: 0; pointer-events: none; z-index: 2; }
  .colorbar-wrap {
    position: absolute; right: 10px; top: 50%; transform: translateY(-50%); display: flex; flex-direction: column;
    align-items: center; gap: 3px; pointer-events: none; z-index: 3;
  }
  .cb-bar { width: 13px; height: 150px; border-radius: 2px; border: 1px solid rgba(128,128,128,0.4); }
  .cb-label { font-family: var(--font-mono); font-size: 10px; color: rgba(255,255,255,0.9); text-shadow: 0 1px 3px rgba(0,0,0,0.9); line-height: 1; }
  .cb-var {
    font-family: var(--font-mono); font-size: 9px; color: rgba(255,255,255,0.7); text-shadow: 0 1px 3px rgba(0,0,0,0.9);
    margin-top: 2px; writing-mode: vertical-rl; transform: rotate(180deg);
  }
  .light .cb-label, .light .cb-var { color: rgba(20,28,48,0.92); text-shadow: 0 1px 3px rgba(255,255,255,0.9); }
  .arrow-legend {
    position: absolute; left: 62px; bottom: 12px; display: flex; align-items: center; gap: 6px; z-index: 3;
    padding: 3px 8px; font-family: var(--font-mono); font-size: 10px; color: var(--text-secondary);
    background: rgba(20,22,38,0.8); border: 1px solid var(--border-color); border-radius: var(--radius-sm); pointer-events: none;
  }
  .al-bar { width: 90px; height: 8px; border-radius: 2px; border: 1px solid rgba(128,128,128,0.4); }
  .compass-wrap { position: absolute; bottom: 10px; left: 10px; pointer-events: none; z-index: 3; }
  .hint-3d {
    padding: 3px var(--spacing-md); font-size: 10px; color: var(--text-muted); text-align: center;
    background: var(--bg-tertiary); border-top: 1px solid var(--border-color);
  }
  .overlay {
    position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: var(--spacing-md);
    color: var(--text-muted); pointer-events: none; z-index: 5; text-align: center; padding: 0 24px;
  }
  .overlay.error { color: var(--accent-danger); }
  .overlay.empty .banner-btn, .overlay.fatal { pointer-events: auto; }
  .overlay.fatal { background: rgba(10,10,20,0.7); }
  .overlay .sub { font-size: 11px; color: var(--text-muted); margin: 0; }
  .spinner { width: 32px; height: 32px; border: 3px solid var(--border-color); border-top-color: var(--accent-primary); border-radius: 50%; animation: spin 800ms linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .banner-btn {
    font-size: 11px; padding: 3px 10px; border: 1px solid var(--border-color); border-radius: var(--radius-sm);
    background: var(--bg-tertiary); color: var(--text-secondary); cursor: pointer; white-space: nowrap;
  }
  .banner-btn:hover { background: var(--accent-primary); color: white; border-color: var(--accent-primary); }
</style>
