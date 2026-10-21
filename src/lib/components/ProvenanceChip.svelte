<script lang="ts">
  /**
   * A small colour-coded chip that says what kind of numbers a layer holds (OBS, MODEL, RETR, SIM,
   * STYLE) and opens a popover with the details: definition of the class, source, valid and run
   * time, offset to the active radar scan, resolution, method, parameters, uncertainty, references,
   * attribution and licence. The class is always spelled out, never colour alone. A layer without
   * provenance shows "?" and says that nothing is known.
   *
   * Hover or focus shows the popover, a click pins it, Escape closes it.
   */
  import '../styles/provenance.css';
  import { portal } from '../utils/portal';
  import {
    KIND_INFO,
    formatOffset,
    formatUtc,
    formatValue,
    type Provenance,
  } from '../utils/provenance';

  let {
    provenance,
    offset = undefined,
    compact = false,
    title = '',
  }: {
    provenance: Provenance | null;
    /** Seconds from the active radar scan to the layer's valid time (undefined = do not show). */
    offset?: number | null;
    /** Chip only, no popover arrow text (for tight rows). */
    compact?: boolean;
    /** Layer name shown at the top of the popover. */
    title?: string;
  } = $props();

  const kind = $derived(provenance?.kind ?? 'unknown');
  const info = $derived(provenance ? KIND_INFO[provenance.kind] : null);
  const uid = `prov-${Math.random().toString(36).slice(2, 8)}`;

  let btn: HTMLButtonElement | undefined = $state();
  let pinned = $state(false);
  let hovered = $state(false);
  let pos = $state({ left: 0, top: 0, maxHeight: 360, above: false });
  let timer: ReturnType<typeof setTimeout> | null = null;
  const open = $derived(pinned || hovered);

  const PANEL_W = 340;

  function place() {
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const width = Math.min(PANEL_W, vw - 16);
    const left = Math.max(8, Math.min(r.left, vw - width - 8));
    const below = vh - r.bottom - 12;
    const above = r.top - 12;
    const useAbove = below < 220 && above > below;
    pos = {
      left,
      top: useAbove ? Math.max(8, r.top - 6) : r.bottom + 6,
      maxHeight: Math.max(160, Math.min(420, useAbove ? above : below)),
      above: useAbove,
    };
  }

  function show() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      place();
      hovered = true;
    }, 140);
  }
  function hide() {
    if (timer) clearTimeout(timer);
    timer = null;
    hovered = false;
  }
  function toggle() {
    if (timer) clearTimeout(timer);
    place();
    pinned = !pinned;
    hovered = false;
  }

  $effect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        pinned = false;
        hovered = false;
      }
    };
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (btn?.contains(t) || document.getElementById(uid)?.contains(t)) return;
      pinned = false;
    };
    // The popover follows its chip when the page scrolls or resizes, and closes once the chip is
    // out of view. Scrolling inside the popover itself leaves it alone.
    const follow = (e?: Event) => {
      if (e?.target instanceof Node && document.getElementById(uid)?.contains(e.target)) return;
      const r = btn?.getBoundingClientRect();
      if (!r || r.bottom < 0 || r.top > window.innerHeight || r.right < 0 || r.left > window.innerWidth) {
        pinned = false;
        hovered = false;
        return;
      }
      place();
    };
    window.addEventListener('keydown', onKey, true);
    window.addEventListener('mousedown', onDown, true);
    window.addEventListener('resize', follow);
    window.addEventListener('scroll', follow, true);
    return () => {
      window.removeEventListener('keydown', onKey, true);
      window.removeEventListener('mousedown', onDown, true);
      window.removeEventListener('resize', follow);
      window.removeEventListener('scroll', follow, true);
    };
  });

  const rows = $derived.by(() => {
    const p = provenance;
    if (!p) return [] as { k: string; v: string; mono?: boolean }[];
    const out: { k: string; v: string; mono?: boolean }[] = [{ k: 'Source', v: p.source }];
    out.push({ k: 'Valid', v: p.valid_end ? `${formatUtc(p.valid_time)} to ${formatUtc(p.valid_end)}` : formatUtc(p.valid_time), mono: true });
    if (p.run_time) out.push({ k: 'Run / made', v: formatUtc(p.run_time), mono: true });
    if (offset !== undefined) out.push({ k: 'Radar scan', v: formatOffset(offset ?? null) + (offset === null ? ' (a time is missing)' : ''), mono: true });
    if (p.resolution) out.push({ k: 'Resolution', v: p.resolution });
    if (p.method) out.push({ k: 'Method', v: p.method });
    if (p.parameters) out.push({ k: 'Parameters', v: Object.entries(p.parameters).map(([k, v]) => `${k} = ${formatValue(v)}`).join('\n'), mono: true });
    if (p.inputs?.length) out.push({ k: 'Made from', v: p.inputs.join(', ') });
    if (p.uncertainty) out.push({ k: 'Uncertainty', v: p.uncertainty });
    if (p.attribution) out.push({ k: 'Credit', v: p.attribution });
    if (p.licence) out.push({ k: 'Licence', v: p.licence });
    return out;
  });
</script>

<span class="prov-wrap">
  <button
    bind:this={btn}
    type="button"
    class="prov-chip kind-{kind}"
    class:compact
    aria-haspopup="dialog"
    aria-expanded={open}
    aria-controls={open ? uid : undefined}
    aria-label={provenance ? `${info?.label} data: details` : 'Provenance unknown: details'}
    data-kind={kind}
    onmouseenter={show}
    onmouseleave={hide}
    onfocus={show}
    onblur={hide}
    onclick={toggle}
  >
    <span class="dot" aria-hidden="true"></span>{provenance ? info?.short : '?'}
  </button>
</span>

{#if open}
  <div
    id={uid}
    class="prov-pop"
    class:above={pos.above}
    role="dialog"
    tabindex="-1"
    aria-label="Provenance"
    use:portal
    style="left:{pos.left}px; top:{pos.top}px; max-height:{pos.maxHeight}px; width:{Math.min(PANEL_W, (typeof window !== 'undefined' ? window.innerWidth : PANEL_W) - 16)}px"
    onmouseenter={() => (hovered = true)}
    onmouseleave={hide}
  >
    <header>
      <span class="prov-chip static kind-{kind}"><span class="dot" aria-hidden="true"></span>{provenance ? info?.label.toUpperCase() : 'UNKNOWN'}</span>
      {#if title}<strong class="ttl">{title}</strong>{/if}
    </header>
    {#if provenance && info}
      <p class="def">{info.definition}</p>
      <dl>
        {#each rows as r (r.k)}
          <dt>{r.k}</dt>
          <dd class:mono={r.mono}>{r.v}</dd>
        {/each}
      </dl>
      {#if provenance.references?.length}
        <h4>References</h4>
        <ul>
          {#each provenance.references as ref (ref)}<li>{ref}</li>{/each}
        </ul>
      {/if}
    {:else}
      <p class="def">The engine sent no provenance for this layer, so its origin is not known. Treat it as unverified.</p>
    {/if}
  </div>
{/if}

<style>
  .prov-wrap { display: inline-flex; }
  .prov-chip {
    --k: var(--c-prov-unknown);
    --k-rgb: var(--c-prov-unknown-rgb);
    display: inline-flex;
    align-items: center;
    gap: 5px;
    height: 18px;
    padding: 0 6px;
    font-family: var(--font-mono);
    font-size: 9.5px;
    font-weight: 500;
    letter-spacing: 0.08em;
    line-height: 1;
    color: var(--c-text);
    background: rgba(var(--k-rgb), 0.14);
    border: 1px solid rgba(var(--k-rgb), 0.55);
    border-radius: var(--radius-sm);
    cursor: pointer;
    white-space: nowrap;
  }
  .prov-chip.compact { padding: 0 5px; }
  .prov-chip.static { cursor: default; height: 20px; }
  .prov-chip:hover { background: rgba(var(--k-rgb), 0.24); }
  .prov-chip:focus-visible { outline: none; box-shadow: var(--focus-ring); }
  .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--k); flex: none; }

  .kind-observed { --k: var(--c-prov-observed); --k-rgb: var(--c-prov-observed-rgb); }
  .kind-model { --k: var(--c-prov-model); --k-rgb: var(--c-prov-model-rgb); }
  .kind-retrieved { --k: var(--c-prov-retrieved); --k-rgb: var(--c-prov-retrieved-rgb); }
  .kind-simulated { --k: var(--c-prov-simulated); --k-rgb: var(--c-prov-simulated-rgb); }
  .kind-stylised { --k: var(--c-prov-stylised); --k-rgb: var(--c-prov-stylised-rgb); border-style: dashed; }
  .kind-forecast { --k: var(--c-prov-forecast); --k-rgb: var(--c-prov-forecast-rgb); border-style: double; }
  .kind-unknown { border-style: dashed; color: var(--c-muted); }

  .prov-pop {
    position: fixed;
    z-index: 970;
    box-sizing: border-box;
    overflow-y: auto;
    padding: var(--space-3);
    background: var(--c-overlay);
    border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm);
    box-shadow: var(--shadow-3);
    color: var(--c-text);
    font-size: var(--fs-xs);
    line-height: 1.45;
  }
  .prov-pop.above { transform: translateY(-100%); }
  header { display: flex; align-items: center; gap: var(--space-2); margin-bottom: var(--space-2); }
  .ttl { font-size: var(--fs-sm); font-weight: 600; overflow-wrap: anywhere; }
  .def { margin: 0 0 var(--space-2); color: var(--c-text-dim); }
  dl { display: grid; grid-template-columns: max-content 1fr; gap: 3px var(--space-3); margin: 0; }
  dt { color: var(--c-muted); text-transform: uppercase; letter-spacing: var(--ls-label); font-size: 10px; padding-top: 1px; }
  dd { margin: 0; overflow-wrap: anywhere; white-space: pre-line; }
  dd.mono { font-family: var(--font-mono); font-size: 11px; }
  h4 { margin: var(--space-2) 0 2px; font-size: 10px; text-transform: uppercase; letter-spacing: var(--ls-label); color: var(--c-muted); font-weight: 500; }
  ul { margin: 0; padding-left: 16px; color: var(--c-text-dim); }
  li { margin: 2px 0; overflow-wrap: anywhere; }
</style>
