<script lang="ts">
  /**
   * Full-window view used by tools/storm_clip/render_clip.py and `rxd animate` (open the app with
   * `?clip=1`): one canvas for the 3D scene, one for the overlay, and `window.__xrClip` to drive it
   * frame by frame. No engine connection is needed: the data comes from a bundle written by clipdata.py.
   *
   * The `encoder*` entries encode the film in this page (clipEncoder.ts: WebCodecs + an in-memory
   * muxer), so the engine needs no video library: `encoderStart`, then `encodeFrame(i)` (draws and
   * encodes frame i) or `encodeImage(i, url)` (a frame already on disk, for a resumed job),
   * `encodeAudio(n)` (the PCM of the last `renderAudio`), `encoderFinish`, `encoderRead(offset, length)`.
   */
  import { onMount, onDestroy } from 'svelte';
  import { StormClip } from '../utils/stormClip';
  import { runMorphSelfTest } from '../utils/morphSelfTest';
  import { ClipEncoder, pickCodecs, type EncoderSupport } from '../utils/clipEncoder';

  let host = $state<HTMLDivElement | null>(null);
  let overlay = $state<HTMLCanvasElement | null>(null);
  let clip: StormClip | null = null;
  let encoder: ClipEncoder | null = null;
  let composite: OffscreenCanvas | null = null;

  onMount(() => {
    const el = host!;
    const ov = overlay!;
    const size = () => {
      ov.width = el.clientWidth;
      ov.height = el.clientHeight;
    };
    size();
    clip = new StormClip(el, ov);

    /** The picture as the screenshot shows it: page background, the WebGL scene, the overlay, at the viewport size. */
    const compose = (): OffscreenCanvas => {
      const w = ov.width, h = ov.height;
      if (!composite || composite.width !== w || composite.height !== h) composite = new OffscreenCanvas(w, h);
      const ctx = composite.getContext('2d')!;
      ctx.fillStyle = getComputedStyle(el).backgroundColor; // the page background (a theme token)
      ctx.fillRect(0, 0, w, h);
      const scene = el.querySelector('canvas:not(.clip-overlay)') as HTMLCanvasElement | null;
      if (scene) ctx.drawImage(scene, 0, 0, w, h);
      ctx.drawImage(ov, 0, 0, w, h);
      return composite;
    };
    const enc = (): ClipEncoder => {
      if (!encoder) throw new Error('encoderStart first');
      return encoder;
    };

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
      checkLook: (s: any) => clip!.checkLook(s),
      flashList: () => clip!.flashList(),
      morphSelfTest: () => runMorphSelfTest(),
      stateAt: (t: number) => clip!.stateAt(t),
      info: () => ({ fatal: clip!.fatal, mapOk: clip!.mapOk, terrain: clip!.terrainInfo, light: clip!.lightInfo, w: ov.width, h: ov.height }),
      resize: size,
      // --- the in-page encoder ---
      encoderSupport: async (): Promise<EncoderSupport & { choice: ReturnType<typeof pickCodecs> }> => {
        const s = await ClipEncoder.support();
        return { ...s, choice: pickCodecs(s) };
      },
      encoderStart: async (o: { crf?: number; keyframeS?: number; sound?: boolean; sampleRate?: number; fps?: number; force?: 'webm' }) => {
        encoder?.dispose();
        encoder = null;
        const support = await ClipEncoder.support();
        // `force: 'webm'` takes the VP9 + Opus path on a browser that has H.264 (the engine's tests of that fallback)
        const choice = pickCodecs(o.force === 'webm' ? { ...support, avc: false } : support);
        if (!choice) throw new Error('this browser cannot encode video (no WebCodecs H.264 or VP9 encoder): ' + support.notes.join('; '));
        encoder = new ClipEncoder({
          width: ov.width, height: ov.height, fps: o.fps ?? clip!.settings.fps, crf: o.crf ?? 18, keyframeS: o.keyframeS,
          sound: o.sound ? { sampleRate: o.sampleRate ?? 48000 } : null, choice,
        });
        return { ...encoder.describe(), width: ov.width, height: ov.height, support };
      },
      encodeFrame: async (i: number) => {
        await clip!.renderFrame(i);
        return enc().addFrame(compose(), i);
      },
      encodeImage: async (i: number, url: string) => {
        const r = await fetch(url);
        if (!r.ok) throw new Error(`frame ${i}: ${url} gave ${r.status}`);
        const bmp = await createImageBitmap(await r.blob());
        try {
          if (bmp.width !== ov.width || bmp.height !== ov.height) {
            // a frame of another size (never for one job; drawn to the film size rather than refused)
            const c = new OffscreenCanvas(ov.width, ov.height);
            c.getContext('2d')!.drawImage(bmp, 0, 0, ov.width, ov.height);
            return await enc().addFrame(c, i);
          }
          return await enc().addFrame(bmp, i);
        } finally {
          bmp.close();
        }
      },
      encodeAudio: (nFrames: number) => {
        const a = clip!.lastAudio;
        if (!a) throw new Error('renderAudio first');
        return enc().addAudio(a.pcm, a.sampleRate, nFrames);
      },
      encoderFinish: () => enc().finish(),
      encoderRead: (offset: number, length: number) => enc().read(offset, length),
      encoderSize: () => enc().size(),
      encoderDispose: () => {
        encoder?.dispose();
        encoder = null;
      },
    };
  });
  onDestroy(() => {
    encoder?.dispose();
    clip?.dispose();
    delete (window as any).__xrClip;
  });
</script>

<div class="clip-host" bind:this={host}>
  <canvas class="clip-overlay" bind:this={overlay} aria-hidden="true"></canvas>
</div>

<style>
  :global(html), :global(body) { margin: 0; background: var(--c-surface-0); overflow: hidden; }
  .clip-host { position: fixed; inset: 0; background: var(--c-surface-0); }
  .clip-host :global(canvas) { display: block; }
  .clip-overlay { position: absolute; left: 0; top: 0; pointer-events: none; z-index: 2; }
</style>
