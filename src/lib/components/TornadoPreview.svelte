<script lang="ts">
  /**
   * Standalone preview of the tornado simulation (open the app with `?tornado=1`): one WebGL canvas, one
   * label canvas, and `window.__tornado` to drive it frame by frame from tools/tornado_sim/render_tornado.py.
   * Lazy: the simulation code is not part of the app's entry chunk.
   */
  import { onMount, onDestroy } from 'svelte';
  import type { TornadoScene } from '../utils/tornado/TornadoScene';
  import type { SeedData } from '../utils/tornado/seed';

  let host = $state<HTMLDivElement | null>(null);
  let gl = $state<HTMLCanvasElement | null>(null);
  let ov = $state<HTMLCanvasElement | null>(null);
  let scene: TornadoScene | null = null;
  let seedInfo: any = null;
  let modules: { cam: any; labels: any } | null = null;

  onMount(() => {
    (window as any).__tornado = {
      init: async (cfg: { seed: SeedData & { epochUtc?: string }; width: number; height: number; ambient?: string; tiles?: boolean; tileBase?: string; shot?: [number, number]; renderScale?: number; debrisCount?: number }) => {
        const [{ TornadoScene }, cam, labels] = await Promise.all([
          import('../utils/tornado/TornadoScene'),
          import('../utils/tornado/cameraModes'),
          import('../utils/tornado/labels'),
        ]);
        modules = { cam, labels };
        const c = gl!;
        c.style.width = `${cfg.width}px`;
        c.style.height = `${cfg.height}px`;
        ov!.width = cfg.width;
        ov!.height = cfg.height;
        ov!.style.width = `${cfg.width}px`;
        ov!.style.height = `${cfg.height}px`;
        scene = new TornadoScene({ canvas: c, width: cfg.width, height: cfg.height, ambient: (cfg.ambient as any) ?? 'dusk', useTiles: cfg.tiles !== false, tileBase: cfg.tileBase ?? '', renderScale: cfg.renderScale, debrisCount: cfg.debrisCount });
        seedInfo = scene.setSeedData(cfg.seed);
        if (cfg.shot) scene.setShot(cfg.shot[0], cfg.shot[1]);
        await scene.loadGround();
        return { info: scene.info(cfg.shot ? cfg.shot[0] : undefined), ground: scene.groundInfo };
      },
      setShot: (t0: number, t1: number) => scene!.setShot(t0, t1),
      /** Clear trees around the observer's positions for a mode and shot (so the camera does not stand in a tree). */
      prepare: (a: { mode: 'chaser' | 'debris' | 'inside'; t0: number; speed: number; duration: number }) => {
        const s = scene!;
        const pts: [number, number][] = [];
        for (let tau = 0; tau <= a.duration; tau += 1) {
          const p = modules!.cam.modePose(a.mode, seedInfo, tau, a.t0 + tau * a.speed, { duration: a.duration, groundZ: (x: number, y: number) => s.groundHeight(x, y) });
          pts.push([p.pos[0], p.pos[1]]);
        }
        s.clearPositions(pts, a.mode === 'debris' ? 40 : a.mode === 'chaser' ? 220 : 30);
      },
      /** Render a frame of a camera mode: tau = seconds into the shot, t = data time. */
      frame: (a: { mode: 'chaser' | 'debris' | 'inside'; tau: number; t: number; duration: number; labels?: boolean; timeLabel?: string }) => {
        const s = scene!;
        s.setTime(a.t);
        const opt = { duration: a.duration, groundZ: (x: number, y: number) => s.groundHeight(x, y) };
        const pose = modules!.cam.modePose(a.mode, seedInfo, a.tau, a.t, opt);
        pose.vel = modules!.cam.poseVelocity(a.mode, seedInfo, a.tau, a.t, opt);
        const st = s.render(pose);
        const ctx = ov!.getContext('2d')!;
        if (a.labels === false) ctx.clearRect(0, 0, ov!.width, ov!.height);
        else {
          const inf = s.info(a.t);
          modules!.labels.drawLabels(ctx, ov!.width, ov!.height, {
            ef: inf.ef, width: inf.width, vt: inf.vt, coreRadius: inf.coreRadius, periodCore: inf.periodCore, periodFunnel: inf.periodFunnel,
            lcl: inf.lcl, cloudBase: inf.cloudBase, speed: inf.speed, headingDeg: inf.headingDeg, ground: inf.ground, ambient: inf.ambient,
            sunLabel: inf.sun.label, mesoVrot: seedInfo.meso ? seedInfo.meso.vt : undefined, attribution: s.groundInfo.attribution,
          }, { mode: a.mode, tau: a.tau, duration: a.duration, timeLabel: a.timeLabel });
        }
        return { stats: st, pose: { pos: pose.pos, target: pose.target, fov: pose.fovDeg, wind: pose.windAtCamera, shakeDeg: pose.shakeDeg, distance: pose.distance } };
      },
      /** Render with an explicit camera (local metres). */
      camera: (cam: any, t: number) => {
        scene!.setTime(t);
        return scene!.render(cam);
      },
      slice: (t: number) => {
        scene!.setTime(t);
        scene!.renderSwirlSlice();
      },
      mask: (m: number) => { scene!.mask = m; },
      tune: (o: { exposure?: number; flash?: number }) => { if (o.exposure) scene!.exposureMul = o.exposure; if (o.flash !== undefined) scene!.flashMul = o.flash; },
      info: (t?: number) => scene!.info(t),
      debris: () => ({ airborne: (scene as any).sim.airborne(), n: (scene as any).sim.n }),
      pixels: () => Array.from(scene!.readPixels().slice(0, 64)),
      hashPixels: () => {
        const p = scene!.readPixels();
        let h = 2166136261 >>> 0;
        for (let i = 0; i < p.length; i += 7) h = Math.imul(h ^ p[i], 16777619) >>> 0;
        return h;
      },
    };
  });
  onDestroy(() => {
    scene?.dispose();
    delete (window as any).__tornado;
  });
</script>

<div class="tornado-host" bind:this={host}>
  <canvas class="tornado-gl" bind:this={gl}></canvas>
  <canvas class="tornado-labels" bind:this={ov} aria-hidden="true"></canvas>
</div>

<style>
  :global(html), :global(body) { margin: 0; background: var(--c-surface-0); overflow: hidden; }
  .tornado-host { position: fixed; inset: 0; background: var(--c-surface-0); }
  .tornado-host :global(canvas) { display: block; position: absolute; left: 0; top: 0; }
  .tornado-labels { pointer-events: none; z-index: 2; }
</style>
