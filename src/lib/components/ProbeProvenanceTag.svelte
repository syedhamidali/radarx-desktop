<script lang="ts">
  /**
   * The provenance class next to a probed value: a small static tag (OBS, RETR, MODEL, SIM, STYLE,
   * ADJ, "?") in the colours of the layer chips. The class is written out, never colour alone, and
   * the full name is the accessible label. Static on purpose: read-outs follow the pointer and do
   * not take clicks; the details live on the layer's chip in the Layers panel.
   */
  import '../styles/provenance.css';
  import type { ProbeTag } from '../utils/probeProvenance';

  let { tag }: { tag: ProbeTag } = $props();
</script>

<span class="probe-prov kind-{tag.kind}" title={tag.label} data-kind={tag.kind}>
  <span class="dot" aria-hidden="true"></span><span aria-hidden="true">{tag.short}</span><span class="sr">{tag.label}</span>
</span>

<style>
  .probe-prov {
    --k: var(--c-prov-unknown);
    --k-rgb: var(--c-prov-unknown-rgb);
    display: inline-flex;
    align-items: center;
    gap: 4px;
    height: 15px;
    padding: 0 4px;
    margin-left: 6px;
    vertical-align: 1px;
    font-family: var(--font-mono);
    font-size: 9px;
    font-weight: 500;
    letter-spacing: 0.08em;
    line-height: 1;
    color: var(--c-text);
    background: rgba(var(--k-rgb), 0.14);
    border: 1px solid rgba(var(--k-rgb), 0.55);
    border-radius: var(--radius-sm);
    white-space: nowrap;
  }
  .dot { width: 5px; height: 5px; border-radius: 50%; background: var(--k); flex: none; }

  .kind-observed { --k: var(--c-prov-observed); --k-rgb: var(--c-prov-observed-rgb); }
  .kind-model { --k: var(--c-prov-model); --k-rgb: var(--c-prov-model-rgb); }
  .kind-retrieved,
  .kind-adjusted { --k: var(--c-prov-retrieved); --k-rgb: var(--c-prov-retrieved-rgb); }
  .sr {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
  }
  .kind-simulated { --k: var(--c-prov-simulated); --k-rgb: var(--c-prov-simulated-rgb); }
  .kind-stylised { --k: var(--c-prov-stylised); --k-rgb: var(--c-prov-stylised-rgb); border-style: dashed; }
  .kind-unknown { border-style: dashed; color: var(--c-muted); }
</style>
