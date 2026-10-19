import { writable } from 'svelte/store';

/**
 * Why the desktop shell could not start (or restart) the Python engine, as reported by the
 * `sidecar-error` event / `get_sidecar_error` command. Shown by the boot panel next to the live
 * connection state; null while nothing is wrong.
 */
export const sidecarError = writable<string | null>(null);
