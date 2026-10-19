import type { Component } from 'svelte';

/**
 * A component whose code is fetched on demand (Vite turns the `import()` into a separate chunk).
 *
 *   const palette = lazy(() => import('./CommandPalette.svelte'));
 *   $effect(() => { if (wanted) palette.load(); });
 *   {#if palette.component}<palette.component bind:visible />{/if}
 *
 * `load()` is idempotent and returns the same promise; `component` is null until the chunk has
 * arrived, then stays set (the component is never unmounted by this helper).
 */
export class Lazy<P extends Record<string, any> = any> {
  component = $state.raw<Component<P> | null>(null);
  failed = $state(false);
  private promise: Promise<void> | null = null;

  constructor(private readonly loader: () => Promise<{ default: Component<P> | any }>) {}

  load(): Promise<void> {
    if (!this.promise) {
      this.promise = this.loader().then(
        (m) => {
          this.component = m.default;
        },
        (err) => {
          // A failed chunk fetch (flaky network on the web build) may be retried by the next load().
          console.error('[lazy] could not load a component chunk:', err);
          this.failed = true;
          this.promise = null;
        },
      );
    }
    return this.promise;
  }
}

export const lazy = <P extends Record<string, any> = any>(
  loader: () => Promise<{ default: Component<P> | any }>,
) => new Lazy<P>(loader);

/** Run `fn` once the browser is idle (or after `timeout` ms at the latest). */
export function whenIdle(fn: () => void, timeout = 1500): void {
  const ric = (globalThis as any).requestIdleCallback as
    | ((cb: () => void, opts?: { timeout: number }) => number)
    | undefined;
  if (ric) ric(fn, { timeout });
  else setTimeout(fn, 200);
}
