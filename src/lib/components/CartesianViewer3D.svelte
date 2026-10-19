<script lang="ts">
  /**
   * CartesianViewer3D: the 3D tab for gridded scenes - the merged mosaic volume and the
   * multi-Doppler wind grids (3D arrows). Same look and controls as the polar radar volume
   * (RadarViewer3D): transfer function editor, "hide below", vertical exaggeration, ground map,
   * lighting, quality governor, orbit/zoom/pan camera - drawn by cartesianRenderer.ts.
   */
  import { onMount, onDestroy, untrack } from 'svelte';
  import { settings, mapHidden, setMapHidden } from '../stores/settings';
  import { getShortcutDisplay } from '../utils/shortcuts';
  import { mosaicResult } from '../stores/mosaic';
  import { windResult } from '../stores/wind';
  import {
    source3d, mosaicVolume, windVolume, volume3dLoading, fetchMosaicVolume, fetchWindVolume,
  } from '../stores/volume3d';
  import { cmapOverrides, effectiveCmap, chooseCmap, isCategoricalVariable } from '../stores/cmapChoice';
  import { COLORMAP_DATA } from '../utils/colormaps';
  import { getDefaultRange } from '../utils/ppiRenderer';
  import { resolveBasemap, IMAGERY_EOX_S2 } from '../utils/basemaps';
  import { buildGroundMap } from '../utils/groundMap';
  import { buildTerrain, TERRAIN_ATTRIBUTION } from '../utils/terrain';
  import { buildRealisticTf, lookFor, lookWith, sampleOptions, type Palette, type LightKind } from '../utils/realisticStyle';
  import { sunLight, daylightAssumed, type SunLight } from '../utils/sunPosition';
  import { defaultLook, validateLook, changedLook, applyPreset, type LookConfig } from '../utils/lookConfig';
  import LookPanel from './LookPanel.svelte';
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
    buildGlyphs, windStats, autoStrides, arrowPalette, flowRamp, lutFromCss, DEFAULT_GLYPH_OPTIONS, type GlyphColorBy, type GlyphSet,
  } from '../utils/windGlyphs';
  import { CartesianRenderer, type CamFrame } from '../utils/cartesianRenderer';
  import {
    buildFlowField, seedCells, levelsUniform, physicsCheck, defaultVisualSpeed, describeVisualSpeed, formatUtc,
    type FlowColor, type PhysicsCheck,
  } from '../utils/windFlow.ts';
  import { drawCartesianOverlay } from '../utils/cartesianOverlay';
  import { yieldToMain, type QualityMode, type GovernorState } from '../utils/qualityGovernor';
  import TransferFunctionEditor from './TransferFunctionEditor.svelte';
  import CmapSelect from './CmapSelect.svelte';
  import MaskPopover from './MaskPopover.svelte';
  import { maskOf, valueMasks } from '../stores/valueMask';
  import { maskVecNorm } from '../utils/valueMaskGl';
  import { maskPredicate } from '../stores/valueMask';
  import { windMaskKey } from '../stores/wind';

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

  /** The camera frames the wind grid when the wind is the subject, else the whole scene. */
  const frameBox = $derived.by((): SceneBox | null => (src === 'wind' && wv && windOn ? translateBox(gridBox(wv), windOffset[0], windOffset[1]) : sceneBox));

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
  /** The shared value mask of this variable in the volume's normalised units (8-bit over [vmin, vmax]). */
  const vmaskVec = $derived(maskVecNorm(maskOf($valueMasks, variable), vmin, vmax));

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
  /** The Ground choice the user made (kept while the map is hidden, so showing it again restores it). */
  let groundChoice = $state<GroundMode>(loadPref<string>('ground', 'map', (x) => x === 'map' || x === 'grid' || x === 'off') as GroundMode);
  /** What is drawn: no ground plane, imagery or terrain while the map is hidden (volume and axes stay). */
  const groundMode = $derived<GroundMode>($mapHidden ? 'off' : groundChoice);
  let showLight = $state<boolean>(loadPref<boolean>('light', false));

  // Realistic ground (terrain + satellite imagery) and the Realistic (stylised) storm look. Both are
  // options next to the flat map and the scientific colour scale, which stay the defaults.
  type StormStyle = 'scientific' | 'realistic';
  let realGround = $state<boolean>(loadPref<boolean>('realGround', false));
  let stormStyle = $state<StormStyle>(loadPref<string>('stormStyle', 'scientific', (x) => x === 'scientific' || x === 'realistic') as StormStyle);
  /** Status of the realistic ground: loading, or what could not be fetched. */
  let realNote = $state('');
  let sunInfo = $state<SunLight | null>(null);
  /** The look of the realistic storm (lookConfig.ts); kept in this browser. */
  let lookCfg = $state<LookConfig>(loadLook());
  let showLook = $state(false);
  function loadLook(): LookConfig {
    try { return validateLook(JSON.parse(localStorage.getItem('xradar.3d.look') ?? '{}')); } catch { return defaultLook(); }
  }
  function onLookChange(next: LookConfig) {
    lookCfg = next;
    try { localStorage.setItem('xradar.3d.look', JSON.stringify(changedLook(next))); } catch { /* storage blocked */ }
    // a preset that is the scientific colour scale switches the style; any other look is the realistic style
    const st: StormStyle = next.style === 'scientific' ? 'scientific' : 'realistic';
    if (st !== stormStyle) { stormStyle = st; savePref('stormStyle', st); }
  }

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

  // Wind display: the flow (streamlines advected by the retrieved wind; on by default), arrows
  // (off by default) and a subtle drift of the radar echo along the wind (off by default) are three
  // independent toggles; each fades in and out.
  let flowOnUser = $state<boolean>(loadPref<boolean>('flowOn', true));
  let arrowsOnUser = $state<boolean>(loadPref<boolean>('arrowsOn', false));
  let animField = $state(false);
  let animAmp = $state(3);
  const showArrows = $derived(windOn && arrowsOnUser);
  const showFlow = $derived(windOn && flowOnUser);
  function toggleFlow() { flowOnUser = !flowOnUser; savePref('flowOn', flowOnUser); }
  function toggleArrows() { arrowsOnUser = !arrowsOnUser; savePref('arrowsOn', arrowsOnUser); }
  const reduceMotion = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
  let flowPlaying = $state(!reduceMotion);
  let flowCount = $state(6000);
  let flowCountTouched = false;
  let flowWidth = $state(1.4);
  let flowTrail = $state(0.8);
  let flowOpacity = $state(0.55);
  let flowSpeedMul = $state(1);
  let flowColor = $state<FlowColor>('speed');
  let uncOn = $state(false);
  let uncVal = $state(5);
  /** Wind error above which cells are hidden (arrows and flow alike); null = no limit. */
  const uncMax = $derived(uncOn && wv?.fields.uncertainty ? uncVal : null);
  const extentKm = $derived(wv ? Math.max(wv.nx * wv.dx, wv.ny * wv.dy) : 0);
  const baseVisualSpeed = $derived(defaultVisualSpeed(extentKm));
  const visualSpeed = $derived(Math.max(1, Math.round(baseVisualSpeed * flowSpeedMul)));
  let flowInfo = $state({ usable: false, seeds: 0, msg: '' });
  let physics = $state<PhysicsCheck | null>(null);
  const analysisUtc = $derived(formatUtc(wv?.analysisTime));
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
    const pose = framePose(frameBox ?? sceneBox, vexag, asp, Math.PI / 4, preset);
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
      state: () => ({ cmap, vexag, groundMode, lightScene, baseVol, windOn, windOffset, flowOnUser, arrowsOnUser, animField, flowPlaying, visualSpeed, flowInfo, physics, glyphInfo, thrValue, hasGround: renderer?.hasGroundMap }),
      cam: () => renderer && { theta: renderer.camTheta, phi: renderer.camPhi, dist: renderer.camDist, tgt: renderer.camTgt },
      flowRead: (n: number) => renderer?.flowReadBack(n) ?? null,
      flowStep: (dt: number) => renderer?.flowStepOnce(dt),
      flowField: () => lastFlowField,
      flowPause: (on: boolean) => { flowPlaying = !on; },
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

  // transfer function (the realistic storm look has its own: opacity and brightness follow dBZ)
  const realisticStorm = $derived(stormStyle === 'realistic' && kind === 'reflectivity' && !!volume);
  $effect(() => {
    const t = tfData;
    const real = realisticStorm;
    const lo = vmin, hi = vmax, st = stepKm;
    if (!ready) return;
    const pal = lookCfg.palette as Palette;
    const opts = sampleOptions(lookCfg);
    untrack(() => renderer?.setTransfer(real ? buildRealisticTf(lo, hi, st, pal, opts) : t));
  });
  // light of the realistic look: the real sun at the scan time and place, or the documented dusk light
  $effect(() => {
    const real = realisticStorm;
    const fr = frame;
    const when = volume?.analysisTime ?? null;
    const cfg = lookCfg;
    if (!ready) return;
    untrack(() => {
      if (!renderer) return;
      if (!real || !fr) { sunInfo = null; renderer.setRealistic(null); return; }
      const d0 = when ? new Date(when) : new Date();
      const d = Number.isNaN(d0.getTime()) ? new Date() : d0;
      const kind = cfg.light as LightKind;
      let sun = sunLight(d, fr.lat, fr.lon);
      if (sun.stylised && (kind === 'daylight' || kind === 'overcast')) sun = daylightAssumed(d, fr.lat, fr.lon);
      sunInfo = sun;
      renderer.setRealistic(lookWith(lookFor(sun, 4, 1, kind === 'overcast' || kind === 'night' ? kind : undefined), cfg));
      renderer.setFov(cfg.fov as number);
    });
  });

  // scalar scene parameters
  $effect(() => {
    const ve = vexag;
    const ts = thrSpec ? thresholdToShader(thrSpec, thrValue, vmin, vmax) : { thr: 0, soft: 0.03, mode: 0 as 0 | 1 };
    const p = {
      vexag: ve, stepKm, opacity: opacityScale, thr: ts.thr, thrSoft: ts.soft, thrMode: ts.mode,
      groundMode, light: showLight, lightScene, showVolume: baseVol !== 'none', vmask: vmaskVec,
      gridStepKm: gridStepFor(sceneBox ? Math.max(sceneBox.xMax - sceneBox.xMin, sceneBox.yMax - sceneBox.yMin) : 100),
    };
    if (!ready) return;
    untrack(() => { renderer?.setParams(p); renderer?.interact(); });
  });

  // scene box + camera framing
  $effect(() => {
    const b = sceneBox;
    const fb = frameBox ?? b;
    const r = ready;
    if (!r) return;
    untrack(() => {
      renderer?.setSceneBox(b);
      if (b && fb) {
        const key = `${fb.xMin.toFixed(1)},${fb.xMax.toFixed(1)},${fb.yMin.toFixed(1)},${fb.yMax.toFixed(1)},${fb.zMax.toFixed(2)}`;
        if (key !== framedKey) { framedKey = key; camTouched = false; resetCamera('oblique', true); }
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
    const on = showArrows;
    const stats = wStats;
    const unc = uncMax;
    const lo = wLevelLo, hi = wLevelHi, sh = wStrideH, sv = wStrideV, len = wLength, col = wColor, minC = wMinCrossing, ve = vexag;
    const off = windOffset;
    // value mask of what the arrows show: the horizontal speed, or w when coloured by w
    const wm = maskOf($valueMasks, windMaskKey(col === 'w' ? 'w' : 'speed'));
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
        colorBy: col, offsetX: off[0], offsetY: off[1], maxUncertainty: unc, hidden: arrowHidden(wm, col === 'w'),
      }, stats);
      glyphInfo = { count: glyphSet.count, strideH: glyphSet.strideH, strideV: glyphSet.strideV, kmPerMs: glyphSet.kmPerMs, colorMax: glyphSet.colorMax };
      renderer.setGlyphs(glyphSet.data, glyphSet.count);
    });
  });

  /** Arrows the value mask hides: by horizontal speed, or by w when the arrows are coloured by w. */
  function arrowHidden(mask: ReturnType<typeof maskOf>, byW: boolean) {
    const test = maskPredicate(mask, { f32: true });
    if (!test) return null;
    return (u: number, v: number, w: number) => test(byW ? w : Math.hypot(u, v));
  }

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

  // ── Wind flow ─────────────────────────────────────────────────────────────
  let flowTimer: ReturnType<typeof setTimeout> | null = null;
  let lastFlowField: ReturnType<typeof buildFlowField> | null = null;
  /** Build the masked wind (same mask as the arrows) and hand it to the GPU; debounced for the sliders. */
  $effect(() => {
    const w = wv;
    const minC = wMinCrossing;
    const unc = uncMax;
    const lo = wLevelLo, hi = wLevelHi;
    const off = windOffset;
    if (!ready) return;
    untrack(() => {
      if (flowTimer) clearTimeout(flowTimer);
      flowTimer = setTimeout(() => {
        flowTimer = null;
        if (!renderer) return;
        if (!w) {
          renderer.setFlowField(null, new Uint32Array(0), [0, 0]);
          physics = null;
          flowInfo = { usable: false, seeds: 0, msg: '' };
          return;
        }
        const f = buildFlowField(w, { minCrossing: minC, maxUncertainty: unc, minRadars: 2 });
        lastFlowField = f;
        physics = physicsCheck(w, f, w.summary?.qc);
        if (w.nx < 2 || w.ny < 2 || w.nz < 2 || !levelsUniform(w.z)) {
          flowInfo = { usable: false, seeds: 0, msg: 'The analysis levels are not evenly spaced: the flow cannot be drawn.' };
          renderer.setFlowField(null, new Uint32Array(0), [0, 0]);
          return;
        }
        const zl = w.z[Math.max(0, Math.min(lo, hi))] - f.dz / 2;
        const zh = w.z[Math.min(w.nz - 1, Math.max(lo, hi))] + f.dz / 2;
        const cells = seedCells(f, zl, zh);
        // a light default density that follows the size of the wind region (the volume stays visible)
        if (!flowCountTouched) flowCount = Math.max(1500, Math.min(20000, Math.round(cells.length * 0.3 / 500) * 500));
        flowInfo = {
          usable: cells.length > 0,
          seeds: cells.length,
          msg: cells.length === 0 ? 'No cell has a retrieved wind at these levels and limits: there is nothing to animate.' : '',
        };
        if (cells.length > 0) renderer.setFlowField(f, cells, off);
        else renderer.setFlowField(null, new Uint32Array(0), [0, 0]);
      }, 120);
    });
  });

  // Flow appearance: light, thin, translucent trails coloured by wind speed. On a dark scene they
  // are blended additively (a soft glow); on a light scene they are dark saturated colours with a
  // thin light halo.
  $effect(() => {
    const light = lightScene;
    const col = flowColor;
    const stats = wStats;
    const p = { requested: flowCount, width: flowWidth, trail: flowTrail, vs: visualSpeed, op: flowOpacity };
    if (!ready) return;
    untrack(() => {
      const pal = arrowPalette(light);
      renderer?.setFlowParams({
        requested: p.requested, lifeSec: 6, visualSpeed: p.vs, trailSec: p.trail, widthPx: p.width,
        haloPx: light ? 0.7 : 0, opacity: light ? Math.min(1, p.op * 1.3) : p.op * 0.45, additive: !light,
        colorMode: col === 'none' ? 0 : col === 'speed' ? 1 : 2,
        colorMax: stats ? (col === 'w' ? stats.absWP98 : stats.speedP98) : 40,
        core: light ? [0.1, 0.12, 0.18] : [0.92, 0.96, 1], halo: light ? [0.97, 0.97, 0.98] : pal.outline,
      });
      if (col !== 'none') renderer?.setFlowLut(flowRamp(col === 'w' ? 'w' : 'speed', light));
    });
  });
  $effect(() => {
    const on = showFlow;
    const playing = flowPlaying;
    if (!ready) return;
    untrack(() => { renderer?.setFlowPlaying(playing); renderer?.setFlowEnabled(on); });
  });
  // subtle drift of the radar echo along the retrieved wind (display only)
  $effect(() => {
    const on = animField && !!wv && baseVol !== 'none' && showWind;
    const amp = animAmp;
    const stats = wStats;
    if (!ready) return;
    untrack(() => renderer?.setFieldAnim(on, { ampKm: amp, period: 6, refSpeed: stats ? stats.speedP95 : 30 }));
  });
  // a hidden tab frees the particle data; it is rebuilt when the tab is shown again
  function onVisibility() {
    if (document.hidden) renderer?.suspendFlow();
    else renderer?.resumeFlow();
  }
  onMount(() => {
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  });
  const flowLegend = $derived(showFlow && flowColor !== 'none');
  const legendKind = $derived<FlowColor | GlyphColorBy>(flowLegend ? flowColor : wColor);
  const legendMax = $derived(flowLegend ? (wStats ? (flowColor === 'w' ? wStats.absWP98 : wStats.speedP98) : 0) : glyphInfo.colorMax);

  const legendGradient = $derived(
    legendKind === 'w'
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
    if (renderer && groundMode !== 'map' && renderer.hasTerrain) renderer.setTerrain(null);
    if (!renderer || groundMode !== 'map' || !b || !fr) return;
    if (realGround) { await refreshRealGround(gen, b, fr); return; }
    if (!bm.provider.url) return;
    if (renderer.hasTerrain) renderer.setTerrain(null);
    renderer.setMapGain(1.5);
    realNote = '';
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
  /** Satellite imagery (EOX Sentinel-2 cloudless, CC BY-NC-SA) draped on Terrarium elevation tiles. */
  async function refreshRealGround(gen: number, b: SceneBox, fr: { lat: number; lon: number }) {
    const half = Math.ceil(halfExtent(b) * 1.02) + 30;
    const key = `real|${fr.lat.toFixed(3)},${fr.lon.toFixed(3)}|${half}`;
    if (key === groundKey && renderer?.hasTerrain) return;
    realNote = 'Loading terrain and satellite imagery…';
    const stale = () => gen !== groundGen || !renderer;
    const [img, dem] = await Promise.all([
      buildGroundMap({ lat: fr.lat, lon: fr.lon, radiusKm: half, size: 2048, provider: IMAGERY_EOX_S2, theme: 'light', maxZoom: 11, maxTiles: 150 }, stale).catch(() => null),
      buildTerrain({ lat: fr.lat, lon: fr.lon, halfKm: half, size: 1024 }, stale).catch(() => null),
    ]);
    if (stale() || !renderer) return;
    renderer.setMapGain(2.1);
    if (img) renderer.setGround(img.size, img.data, half);
    if (dem) renderer.setTerrain({ size: dem.size, heightsKm: dem.heightsKm, minKm: dem.minKm, maxKm: dem.maxKm, half, center: [0, 0] });
    const failed = [!img ? 'satellite imagery (tiles.maps.eox.at)' : '', !dem ? 'elevation (s3.amazonaws.com)' : ''].filter(Boolean);
    realNote = failed.length ? `Could not load: ${failed.join(', ')}. ${img || dem ? 'Showing what is available.' : 'Showing the flat map.'}` : '';
    if (!img && !dem) { realGround = false; groundKey = ''; scheduleGround(0); return; }
    groundKey = key;
  }
  $effect(() => {
    void groundMode; void lightScene; void $settings.mapStyle; void realGround;
    if (!ready) return;
    untrack(() => { groundKey = ''; scheduleGround(0); });
  });

  // ── Misc UI ───────────────────────────────────────────────────────────────
  function setGroundMode(m: GroundMode) { groundChoice = m; savePref('ground', m); }
  function onRealGround() { savePref('realGround', realGround); }
  function onStormStyle(e: Event) {
    stormStyle = (e.target as HTMLSelectElement).value as StormStyle;
    savePref('stormStyle', stormStyle);
    if (stormStyle === 'scientific') lookCfg = { ...lookCfg, style: 'scientific' };
    else if (lookCfg.style === 'scientific') lookCfg = applyPreset('Daylight cumulonimbus');
  }
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
      <MaskPopover variable={variable} units={units} label="Hide values" testid="mask-3d" />
    {/if}
    {#if wv}
      <div class="ctrl-group toggles" role="group" aria-label="Wind animation">
        <button class="reset-btn tog" class:active={flowOnUser} aria-pressed={flowOnUser} onclick={toggleFlow}
                title="Animated streamlines of the retrieved wind (key F). Fades in and out.">≋ Flow</button>
        <button class="reset-btn tog" class:active={animField} aria-pressed={animField} onclick={() => (animField = !animField)}
                disabled={baseVol === 'none'}
                title={baseVol === 'none' ? 'There is no radar volume to animate' : 'Animate field: the radar echo drifts subtly along the retrieved wind (display only, not a forecast). Fades in and out.'}>◌ Animate field</button>
      </div>
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
      <button class="reset-btn" class:active={!$mapHidden} aria-pressed={!$mapHidden} data-testid="map-toggle-3d"
              onclick={() => setMapHidden(!$mapHidden)}
              title="{$mapHidden ? 'Show' : 'Hide'} the map: ground imagery and terrain ({getShortcutDisplay('toggle-map')})">Map</button>
      <label for="ground-ctrl">Ground</label>
      <select id="ground-ctrl" class="quality-sel" value={groundChoice} disabled={$mapHidden}
              title={$mapHidden ? `The map is hidden (${getShortcutDisplay('toggle-map')} shows it again)` : 'Ground under the volume'} onchange={(e) => setGroundMode((e.target as HTMLSelectElement).value as GroundMode)}>
        <option value="map">Map</option>
        <option value="grid">Grid</option>
        <option value="off">Off</option>
      </select>
    </div>
    {#if groundChoice === 'map'}
      <label class="chk" class:dim={$mapHidden} title="{$mapHidden ? 'Hidden while the map is hidden. ' : ''}Terrain from elevation tiles with Sentinel-2 satellite imagery draped on it (needs a network connection; tiles are cached). Heights use the Vertical stretch.">
        <input type="checkbox" bind:checked={realGround} disabled={$mapHidden} onchange={onRealGround} /> Realistic ground
      </label>
    {/if}
    {#if volume && kind === 'reflectivity'}
      <div class="ctrl-group" title="Scientific: the colour scale. Realistic: precipitation drawn as light and dark shafts with sun light and soft shadows (stylised, not a photograph).">
        <label for="style-ctrl">Storm</label>
        <select id="style-ctrl" class="quality-sel" value={stormStyle} onchange={onStormStyle}>
          <option value="scientific">Scientific colours</option>
          <option value="realistic">Realistic (stylised)</option>
        </select>
        {#if stormStyle === 'realistic'}
          <button class="reset-btn" class:active={showLook} aria-pressed={showLook} onclick={() => (showLook = !showLook)} title="Colours, light, haze, finish and presets of the realistic look">Look…</button>
        {/if}
      </div>
    {/if}
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
    <div class="controls-3d row2 windrow" aria-label="Wind">
      <label class="chk" title="Show the retrieved wind in 3D">
        <input type="checkbox" bind:checked={showWind} /> Wind
      </label>
      {#if showWind}
        <button class="reset-btn" class:active={arrowsOnUser} aria-pressed={arrowsOnUser} onclick={toggleArrows}
                title="3D arrows at the analysis grid points">Arrows</button>
        {#if showFlow || animField}
          <button class="reset-btn play-btn" class:active={flowPlaying} onclick={() => (flowPlaying = !flowPlaying)}
                  title={flowPlaying ? 'Pause the animation (the picture stays exactly as it is)' : 'Play the animation'}>{flowPlaying ? '❚❚ Pause' : '▶ Play'}</button>
        {/if}
        <div class="ctrl-group" title="Show only these levels (km above sea level)">
          <label for="wlo-ctrl">Levels</label>
          <input id="wlo-ctrl" type="range" min="0" max={wv.nz - 1} step="1" bind:value={wLevelLo} />
          <input id="whi-ctrl" type="range" min="0" max={wv.nz - 1} step="1" bind:value={wLevelHi} aria-label="Top level" />
          <span class="ctrl-val wide">{wv.z[Math.min(wLevelLo, wLevelHi)].toFixed(1)}–{wv.z[Math.max(wLevelLo, wLevelHi)].toFixed(1)} km</span>
        </div>
        {#if maxCrossing > 0}
          <div class="ctrl-group" title="Show only cells whose beams cross at least this angle (the wind is poorly constrained below ~30°)">
            <label for="wcr-ctrl">Crossing ≥</label>
            <input id="wcr-ctrl" type="range" min="0" max="90" step="5" bind:value={wMinCrossing} />
            <span class="ctrl-val">{wMinCrossing}°</span>
          </div>
        {/if}
        {#if wv.fields.uncertainty}
          <div class="ctrl-group" title="Hide the wind (arrows and flow) where its estimated error is larger than this">
            <label class="chk"><input type="checkbox" bind:checked={uncOn} /> Hide error ></label>
            <input id="wunc-ctrl" type="range" min="1" max="20" step="1" bind:value={uncVal} disabled={!uncOn} aria-label="Largest error shown" />
            <span class="ctrl-val">{uncVal} m/s</span>
          </div>
        {/if}
      {/if}
    </div>
    {#if showWind && showFlow}
      <div class="controls-3d row2 flowrow" aria-label="Wind flow">
        <span class="row-tag">Flow</span>
        <div class="ctrl-group" title="Number of particles (the quality governor draws fewer when the GPU is busy)">
          <label for="fcount-ctrl">Density</label>
          <input id="fcount-ctrl" type="range" min="2000" max="60000" step="1000" bind:value={flowCount} oninput={() => (flowCountTouched = true)} />
          <span class="ctrl-val">{(flowCount / 1000).toFixed(0)}k</span>
        </div>
        <div class="ctrl-group" title="Opacity of the trails">
          <label for="fop-ctrl">Opacity</label>
          <input id="fop-ctrl" type="range" min="0.1" max="1" step="0.05" bind:value={flowOpacity} />
          <span class="ctrl-val">{Math.round(flowOpacity * 100)}%</span>
        </div>
        <div class="ctrl-group">
          <label for="fwid-ctrl">Width</label>
          <input id="fwid-ctrl" type="range" min="0.8" max="4" step="0.2" bind:value={flowWidth} />
        </div>
        <div class="ctrl-group" title="Length of the trails in animation seconds">
          <label for="ftrail-ctrl">Trail</label>
          <input id="ftrail-ctrl" type="range" min="0.2" max="2.5" step="0.1" bind:value={flowTrail} />
          <span class="ctrl-val">{flowTrail.toFixed(1)} s</span>
        </div>
        <div class="ctrl-group" title="Visual speed: simulated seconds of wind per animation second. The default lets a 30 m/s wind cross the domain in about 10 s.">
          <label for="fspeed-ctrl">Speed</label>
          <input id="fspeed-ctrl" type="range" min="0.1" max="4" step="0.05" bind:value={flowSpeedMul} />
          <span class="ctrl-val wide" id="fspeed-val">×{visualSpeed}</span>
        </div>
        <div class="ctrl-group">
          <label for="fcol-ctrl">Colour</label>
          <select id="fcol-ctrl" class="quality-sel" bind:value={flowColor}>
            <option value="none">None</option>
            <option value="speed">Speed</option>
            <option value="w">w (vertical)</option>
          </select>
        </div>
      </div>
    {/if}
    {#if showWind && animField && baseVol !== 'none'}
      <div class="controls-3d row2 animrow" aria-label="Field animation">
        <span class="row-tag">Field</span>
        <div class="ctrl-group" title="How far the echo drifts in one cycle where the wind is at its strong end (a display choice, not a forecast)">
          <label for="fanim-ctrl">Drift</label>
          <input id="fanim-ctrl" type="range" min="0.5" max="8" step="0.5" bind:value={animAmp} />
          <span class="ctrl-val">{animAmp.toFixed(1)} km</span>
        </div>
        <span class="fn-main">The radar echo drifts along the retrieved wind only where there is wind; elsewhere it stays still.</span>
      </div>
    {/if}
    {#if showWind && showArrows}
      <div class="controls-3d row2 arrowrow" aria-label="Wind arrows">
        <span class="row-tag">Arrows</span>
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
        <span class="sweep-count" title="Arrows drawn">{glyphInfo.count.toLocaleString()} arrows</span>
      </div>
    {/if}
    {#if showWind}
      <div class="flow-note" role="note">
        {#if showFlow}
          <span class="fn-main">Streamlines of the analysis{analysisUtc ? ` at ${analysisUtc}` : ''} (frozen wind, not a forecast) · visual speed ×{visualSpeed} ({describeVisualSpeed(visualSpeed)})</span>
        {:else}
          <span class="fn-main">Arrows of the analysis{analysisUtc ? ` at ${analysisUtc}` : ''}</span>
        {/if}
        {#if physics}
          <span class="fn-phys" title="Read-out of the wind that is drawn (cells with a retrieved wind under the current limits)">
            max {physics.maxSpeed.toFixed(0)} m/s{physics.maxAbsW !== null ? ` · max |w| ${physics.maxAbsW.toFixed(1)} m/s` : ''} · wind in {physics.validShare.toFixed(physics.validShare < 10 ? 1 : 0)}% of cells{physics.meanDivergence !== null ? ` · mean div ${physics.meanDivergence.toFixed(2)}×10⁻³/s` : ''}
          </span>
          {#if physics.warnings.length > 0}
            <span class="fn-warn" title={physics.warnings.join('\n')}>⚠ {physics.warnings.length} {physics.warnings.length === 1 ? 'note' : 'notes'}: {physics.warnings[0]}</span>
          {/if}
        {/if}
        {#if showFlow && flowInfo.msg}<span class="fn-warn">{flowInfo.msg}</span>{/if}
      </div>
    {/if}
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

  {#if showLook && stormStyle === 'realistic'}
    <LookPanel look={lookCfg} onchange={onLookChange} />
  {/if}

  <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
  <div class="canvas-wrap" bind:this={container} tabindex="0" role="application" aria-label="3D scene"
       onkeydown={(e) => { if (e.key === 'r' || e.key === 'R') resetCamera(); else if ((e.key === 'f' || e.key === 'F') && wv) toggleFlow(); }}>
    <canvas class="scene-overlay" bind:this={overlayCanvas} aria-hidden="true"></canvas>

    {#if (realGround && groundMode === 'map') || realisticStorm}
      <div class="real-note" aria-label="Realistic look notes">
        {#if realisticStorm}
          <span>{sunInfo?.daylightAssumed ? `Stylised daylight; the real sun was ${Math.round(Math.abs(sunInfo.realElevationDeg))}° below the horizon at this time (not a photograph)` : sunInfo?.stylised ? `Stylised lighting, not a photograph (the real sun was ${Math.round(Math.abs(sunInfo.realElevationDeg))}° ${sunInfo.realElevationDeg < 0 ? 'below' : 'above'} the horizon)` : 'Stylised rendering, not a photograph'}</span>
          <span>Radar sees rain and ice, not cloud droplets: cloud look and fine detail are stylised; echo position and strength are radar data</span>
        {/if}
        {#if realGround && groundMode === 'map'}
          <span>Terrain and storm are stretched ×{vexag} vertically</span>
          <span>{IMAGERY_EOX_S2.attribution}</span>
          <span>{TERRAIN_ATTRIBUTION}</span>
          {#if realNote}<span class="rn-warn">{realNote}</span>{/if}
        {/if}
      </div>
    {/if}
    {#if volume && !realisticStorm}
      <div class="colorbar-wrap">
        <span class="cb-label">{vmax.toFixed(1)}</span>
        <div class="cb-bar" style="background: {cmapGradient}"></div>
        <span class="cb-label">{vmin.toFixed(1)}</span>
        <span class="cb-var">{variable} ({units})</span>
      </div>
    {/if}
    {#if windOn && legendKind !== 'none' && (flowLegend || (showArrows && glyphInfo.count > 0))}
      <div class="arrow-legend">
        <span>{legendKind === 'w' ? `w −${legendMax.toFixed(0)}` : '0'}</span>
        <div class="al-bar" style="background: {legendGradient}"></div>
        <span>{legendKind === 'w' ? `+${legendMax.toFixed(0)}` : legendMax.toFixed(0)} m/s</span>
      </div>
    {/if}

    <div class="compass-wrap">
      <svg viewBox="-20 -20 40 40" width="44" height="44">
        <circle r="18" fill="var(--c-scene-bg)" stroke="var(--c-scene-line)" stroke-width="0.5"/>
        <g transform="rotate({compassAngle})">
          <polygon points="0,-15 -3,0 0,-6 3,0" fill="var(--c-alarm)"/>
          <polygon points="0,15 -3,0 0,6 3,0" fill="var(--c-scene-text-dim)"/>
        </g>
        <text transform="rotate({compassAngle}) translate(0,-22)" text-anchor="middle" dominant-baseline="middle"
              font-size="6.5" font-weight="bold" fill="var(--c-alarm)" font-family="system-ui,sans-serif">N</text>
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
    font-family: var(--font-mono); font-size: 11px; color: var(--accent-primary); background: rgba(var(--c-accent-rgb), 0.1);
    padding: 2px 8px; border-radius: var(--radius-sm); border: 1px solid rgba(var(--c-accent-rgb), 0.2);
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
  .reset-btn:hover, .reset-btn.active { background: var(--accent-primary); color: var(--c-on-accent); border-color: var(--accent-primary); }
  .ctrl-group.views { gap: 3px; }
  .quality-sel {
    font-size: 10px; padding: 1px 4px; border: 1px solid var(--border-color); border-radius: var(--radius-sm);
    background: var(--bg-tertiary); color: var(--text-secondary);
  }
  .q-chip {
    font-family: var(--font-mono); font-size: 10px; color: var(--text-muted); padding: 1px 6px;
    border: 1px solid var(--border-color); border-radius: var(--radius-sm); white-space: nowrap;
  }
  .q-chip.weak { color: var(--accent-warning); border-color: rgba(var(--c-caution-rgb), 0.4); }
  .chk {
    display: inline-flex; align-items: center; gap: 4px; font-size: 10px; font-weight: 600; letter-spacing: 0.03em;
    color: var(--text-muted); text-transform: uppercase; cursor: pointer;
  }
  .chk input { accent-color: var(--accent-primary); }
  .chk.dim { opacity: 0.55; }
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
  .cb-bar { width: 13px; height: 150px; border-radius: 2px; border: 1px solid var(--c-scene-line); }
  .cb-label { font-family: var(--font-mono); font-size: 10px; color: var(--c-scene-text); text-shadow: 0 1px 3px var(--c-scene-shadow); line-height: 1; }
  .cb-var {
    font-family: var(--font-mono); font-size: 9px; color: var(--c-scene-text-dim); text-shadow: 0 1px 3px var(--c-scene-shadow);
    margin-top: 2px; writing-mode: vertical-rl; transform: rotate(180deg);
  }
  .light .cb-label, .light .cb-var { color: var(--c-scene-light-text); text-shadow: 0 1px 3px var(--c-scene-light-shadow); }
  .light .cb-bar { border-color: var(--c-border-strong); }
  .arrow-legend {
    position: absolute; left: 62px; bottom: 12px; display: flex; align-items: center; gap: 6px; z-index: 3;
    padding: 3px 8px; font-family: var(--font-mono); font-size: 10px; color: var(--text-secondary);
    background: rgba(var(--c-surface-1-rgb), 0.8); border: 1px solid var(--border-color); border-radius: var(--radius-sm); pointer-events: none;
  }
  .al-bar { width: 90px; height: 8px; border-radius: 2px; border: 1px solid var(--c-border-strong); }
  .toggles { gap: 4px; }
  .tog { font-weight: 700; }
  .tog:disabled { opacity: 0.5; cursor: not-allowed; }
  .row-tag { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; color: var(--accent-primary); min-width: 40px; }
  .windrow input[type="range"], .flowrow input[type="range"], .arrowrow input[type="range"] { width: 56px; }
  .play-btn { min-width: 74px; }
  .flow-note {
    display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 14px; padding: 3px var(--spacing-md);
    font-size: 10px; color: var(--text-muted); background: var(--bg-secondary); border-bottom: 1px solid var(--border-color);
  }
  .fn-main { color: var(--text-secondary); }
  .fn-phys { font-family: var(--font-mono); }
  .fn-warn { color: var(--accent-warning); }
  .real-note {
    position: absolute; left: 10px; top: 8px; max-width: min(62%, 620px); z-index: 3; pointer-events: none;
    display: flex; flex-direction: column; gap: 1px; font-size: 10px; line-height: 1.25;
    color: var(--c-scene-text); text-shadow: 0 1px 3px var(--c-scene-shadow), 0 0 2px var(--c-scene-shadow);
  }
  .rn-warn { color: var(--c-scene-warn); }
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
  .overlay.fatal { background: rgba(var(--c-surface-1-rgb), 0.7); }
  .overlay .sub { font-size: 11px; color: var(--text-muted); margin: 0; }
  .spinner { width: 32px; height: 32px; border: 3px solid var(--border-color); border-top-color: var(--accent-primary); border-radius: 50%; animation: spin 800ms linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .banner-btn {
    font-size: 11px; padding: 3px 10px; border: 1px solid var(--border-color); border-radius: var(--radius-sm);
    background: var(--bg-tertiary); color: var(--text-secondary); cursor: pointer; white-space: nowrap;
  }
  .banner-btn:hover { background: var(--accent-primary); color: var(--c-on-accent); border-color: var(--accent-primary); }
</style>
