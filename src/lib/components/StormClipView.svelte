<script lang="ts">
  /**
   * Full-window view used by tools/storm_clip/render_clip.py (open the app with `?clip=1`): one
   * canvas for the 3D scene, one for the overlay, and `window.__xrClip` to drive it frame by frame.
   * No engine connection is needed: the data comes from a bundle written by clipdata.py.
   */
  import { onMount, onDestroy } from 'svelte';
  import { StormClip } from '../utils/stormClip';
  import { runMorphSelfTest } from '../utils/morphSelfTest';

  let host = $state<HTMLDivElement | null>(null);
  let overlay = $state<HTMLCanvasElement | null>(null);
  let clip: StormClip | null = null;

  onMount(() => {
    const el = host!;
    const ov = overlay!;
    const size = () => {
      ov.width = el.clientWidth;
      ov.height = el.clientHeight;
    };
    size();
    clip = new StormClip(el, ov);
    (window as any).__xrClip = {
      load: (base: string, settings?: any) => clip!.load(base, settings),
      prepare: (provider?: string) => clip!.prepare(provider),
      frameCount: () => clip!.frameCount,
      renderFrame: (i: number) => clip!.renderFrame(i),
      renderAt: (t: number, cam?: any) => clip!.renderAt(t, cam),
      project: (x: number, y: number, z?: number) => clip!.project(x, y, z),
      sites: () => clip!.siteXY(),
      debugScaleTerrain: (k: number) => clip!.debugScaleTerrain(k),
      renderAudio: (sr?: number) => clip!.renderAudio(sr),
      report: () => clip!.report(),
      setLook: (o: any) => clip!.setLook(o),
      flashList: () => clip!.flashList(),
      morphSelfTest: () => runMorphSelfTest(),
      stateAt: (t: number) => clip!.stateAt(t),
      info: () => ({ fatal: clip!.fatal, mapOk: clip!.mapOk, terrain: clip!.terrainInfo, light: clip!.lightInfo, w: ov.width, h: ov.height }),
      resize: size,
    };
  });
  onDestroy(() => {
    clip?.dispose();
    delete (window as any).__xrClip;
  });
</script>

<div class="clip-host" bind:this={host}>
  <canvas class="clip-overlay" bind:this={overlay} aria-hidden="true"></canvas>
</div>

<style>
  :global(html), :global(body) { margin: 0; background: #05070d; overflow: hidden; }
  .clip-host { position: fixed; inset: 0; background: #05070d; }
  .clip-host :global(canvas) { display: block; }
  .clip-overlay { position: absolute; left: 0; top: 0; pointer-events: none; z-index: 2; }
</style>
