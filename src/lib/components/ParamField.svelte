<script lang="ts">
  /**
   * One declared parameter (number / int / bool / select / string) as a form control.
   * Same declaration format as plugin modules, so pipeline steps reuse the module SDK's `param`.
   */
  import { coerceParam, type ModuleParam } from '../stores/modules';

  interface Props {
    p: ModuleParam;
    value: any;
    idPrefix: string;
    onchange: (value: any) => void;
  }
  let { p, value, idPrefix, onchange }: Props = $props();

  const id = $derived(`${idPrefix}-${p.name}`);
  const label = $derived(p.label ?? p.name);

  function optValue(o: any): any {
    return o !== null && typeof o === 'object' && 'value' in o ? o.value : o;
  }
  function optLabel(o: any): string {
    return o !== null && typeof o === 'object' && 'label' in o ? String(o.label) : String(o);
  }
  function num(raw: string): number | null {
    const n = Number(raw);
    return raw.trim() === '' || !Number.isFinite(n) ? null : n;
  }
</script>

<div class="field" class:bool={p.type === 'bool'}>
  {#if p.type === 'bool'}
    <label class="check" for={id} title={p.help}>
      <input {id} type="checkbox" checked={!!value} onchange={(e) => onchange(e.currentTarget.checked)} />
      <span>{label}</span>
    </label>
  {:else}
    <label for={id} title={p.help}>{label}</label>
    {#if p.type === 'select'}
      <select
        {id}
        value={value}
        onchange={(e) => {
          const hit = (p.options ?? []).map(optValue).find((o) => String(o) === e.currentTarget.value);
          onchange(hit ?? e.currentTarget.value);
        }}
      >
        {#each p.options ?? [] as o}
          <option value={optValue(o)}>{optLabel(o)}</option>
        {/each}
      </select>
    {:else if p.type === 'string'}
      <input {id} type="text" value={value ?? ''} oninput={(e) => onchange(e.currentTarget.value)} />
    {:else}
      <input
        {id}
        type="number"
        min={p.min}
        max={p.max}
        step={p.step ?? (p.type === 'int' ? 1 : 'any')}
        value={value ?? ''}
        oninput={(e) => onchange(num(e.currentTarget.value))}
        onchange={(e) => onchange(coerceParam(p, num(e.currentTarget.value)))}
      />
    {/if}
  {/if}
  {#if p.help}<p class="help">{p.help}</p>{/if}
</div>

<style>
  .field {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }
  label {
    font-size: 11px;
    color: var(--text-secondary, #9da2c2);
  }
  .check {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    cursor: pointer;
  }
  input[type='number'],
  input[type='text'],
  select {
    width: 100%;
    box-sizing: border-box;
    font-size: 12px;
    padding: 3px 6px;
    color: var(--text-primary, #e4e6f4);
    background: var(--bg-primary, var(--c-surface-0));
    border: 1px solid var(--border-color, #2a2e4a);
    border-radius: 4px;
  }
  .help {
    margin: 0;
    font-size: 10px;
    color: var(--text-muted, #5c6088);
  }
</style>
