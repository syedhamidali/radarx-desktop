/**
 * Which long jobs are running, reported by the stores that run them.
 *
 * The updater must not restart the app while a pipeline, module, mosaic or wind run is going. It used
 * to import those four stores to find out, which dragged them (and the WebGL renderer, the wind
 * maths ...) into the code that runs before the first paint. Instead each store registers a small
 * "how many jobs are running" store here when it loads; a store that has not loaded cannot be running
 * anything.
 */
import { writable, derived, type Readable } from 'svelte/store';

export type JobSource = 'pipeline' | 'modules' | 'mosaic' | 'wind' | 'movie' | 'live';

const sources = writable<Partial<Record<JobSource, Readable<number>>>>({});

/** Register (or replace) the running-job counter of one store. */
export function registerJobSource(name: JobSource, running: Readable<number>): void {
  sources.update((s) => ({ ...s, [name]: running }));
}

/** Running jobs per source; a source that is not loaded yet is simply absent (= nothing running). */
export const jobCounts: Readable<Partial<Record<JobSource, number>>> = derived(
  sources,
  ($sources, set) => {
    const names = Object.keys($sources) as JobSource[];
    const counts: Partial<Record<JobSource, number>> = {};
    if (names.length === 0) {
      set(counts);
      return;
    }
    const unsubs = names.map((n) =>
      $sources[n]!.subscribe((v) => {
        counts[n] = v;
        set({ ...counts });
      }),
    );
    return () => unsubs.forEach((u) => u());
  },
  {} as Partial<Record<JobSource, number>>,
);
