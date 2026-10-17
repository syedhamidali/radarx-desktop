import { writable } from 'svelte/store';

export interface Toast {
  id: number;
  level: 'info' | 'success' | 'warning' | 'error';
  message: string;
}

let nextId = 1;
/** Most toasts shown at once; the oldest is dropped when a new one would exceed this. */
const MAX_VISIBLE = 5;
const timers = new Map<number, ReturnType<typeof setTimeout>>();

export const toasts = writable<Toast[]>([]);

let current: Toast[] = [];
toasts.subscribe((t) => (current = t));

function arm(id: number, durationMs: number): void {
  const old = timers.get(id);
  if (old) clearTimeout(old);
  if (durationMs > 0) timers.set(id, setTimeout(() => dismissToast(id), durationMs));
  else timers.delete(id);
}

/**
 * Show a toast. An identical message that is already on screen is not stacked again: its timer
 * is restarted instead (a repeated action, e.g. clicking Open twice, shows one toast).
 */
export function addToast(level: Toast['level'], message: string, durationMs = 5000): void {
  const dup = current.find((t) => t.level === level && t.message === message);
  if (dup) {
    arm(dup.id, durationMs);
    return;
  }
  const id = nextId++;
  toasts.update((t) => {
    const next = [...t, { id, level, message }];
    for (const drop of next.slice(0, Math.max(0, next.length - MAX_VISIBLE))) arm(drop.id, 0);
    return next.slice(-MAX_VISIBLE);
  });
  arm(id, durationMs);
}

export function dismissToast(id: number): void {
  const t = timers.get(id);
  if (t) clearTimeout(t);
  timers.delete(id);
  toasts.update((list) => list.filter((toast) => toast.id !== id));
}
