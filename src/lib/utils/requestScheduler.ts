/**
 * Operation tracking shared by every long-running thing in the app (sweep loads, module and
 * mosaic runs, file opens, exports).
 *
 * Goals: nothing waits forever, stale work is cancelled, the user can always see what is running
 * and cancel or retry it. This file defines the contract; the sweep-request queue
 * (concurrency limit + priorities) is layered on top of it in websocket.ts.
 */
import { writable, get } from 'svelte/store';

export type OpPriority = 'visible' | 'prefetch' | 'background';
export type OpState = 'queued' | 'running' | 'slow' | 'failed' | 'cancelled' | 'done';

export interface Op {
  id: string;
  /** Machine kind, e.g. 'sweep', 'module', 'mosaic', 'open', 'export'. */
  kind: string;
  /** Human label, e.g. "Loading DBZH sweep 3". */
  label: string;
  priority: OpPriority;
  state: OpState;
  startedAt: number;
  /** Latest progress / error text. */
  message?: string;
  cancellable: boolean;
  retryable: boolean;
}

export interface BeginOpOptions {
  kind: string;
  label: string;
  priority?: OpPriority;
  /** Marked 'slow' (UI shows "still working…") after this many ms. Default 5000. */
  slowMs?: number;
  /** Marked 'failed' with a timeout message after this many ms. Default 60000. 0 = never. */
  timeoutMs?: number;
  /** Called when the user (or the app) cancels. Also aborts `signal`. */
  onCancel?: () => void;
  /** Called when the user presses Retry on a failed op; should start a fresh attempt. */
  onRetry?: () => void;
  /** Called when the timeout fires (the op is already marked failed and its signal aborted). */
  onTimeout?: () => void;
  /** Start in the 'queued' state (waiting for a slot) instead of 'running'. */
  queued?: boolean;
}

export interface OpHandle {
  readonly id: string;
  readonly signal: AbortSignal;
  /** Move a queued op to running (restarts the slow/timeout clocks). */
  start(): void;
  update(message: string): void;
  done(): void;
  fail(message: string): void;
}

const MAX_FINISHED = 20;
const _ops = writable<Op[]>([]);
export const operations = { subscribe: _ops.subscribe };

interface Internal {
  controller: AbortController;
  slowTimer?: ReturnType<typeof setTimeout>;
  timeoutTimer?: ReturnType<typeof setTimeout>;
  opts: BeginOpOptions;
}
const internals = new Map<string, Internal>();
let counter = 0;

function patch(id: string, p: Partial<Op>): void {
  _ops.update((list) => list.map((o) => (o.id === id ? { ...o, ...p } : o)));
}
function clearTimers(id: string): void {
  const i = internals.get(id);
  if (!i) return;
  if (i.slowTimer) clearTimeout(i.slowTimer);
  if (i.timeoutTimer) clearTimeout(i.timeoutTimer);
  i.slowTimer = i.timeoutTimer = undefined;
}
function armTimers(id: string): void {
  const i = internals.get(id);
  if (!i) return;
  clearTimers(id);
  const slowMs = i.opts.slowMs ?? 5000;
  const timeoutMs = i.opts.timeoutMs ?? 60000;
  if (slowMs > 0) i.slowTimer = setTimeout(() => patch(id, { state: 'slow' }), slowMs);
  if (timeoutMs > 0) {
    i.timeoutTimer = setTimeout(() => {
      patch(id, { state: 'failed', message: `Timed out after ${Math.round(timeoutMs / 1000)} s` });
      i.controller.abort();
      trim();
      try {
        i.opts.onTimeout?.();
      } catch (err) {
        console.error('[ops] onTimeout handler failed:', err);
      }
    }, timeoutMs);
  }
}
function trim(): void {
  _ops.update((list) => {
    const finished = list.filter((o) => ['done', 'failed', 'cancelled'].includes(o.state));
    if (finished.length <= MAX_FINISHED) return list;
    const drop = new Set(finished.slice(0, finished.length - MAX_FINISHED).map((o) => o.id));
    for (const id of drop) internals.delete(id);
    return list.filter((o) => !drop.has(o.id));
  });
}
function finish(id: string, state: 'done' | 'failed' | 'cancelled', message?: string): void {
  clearTimers(id);
  patch(id, { state, ...(message !== undefined ? { message } : {}) });
  trim();
  // Successful / cancelled ops disappear shortly; failures stay until dismissed or trimmed.
  if (state !== 'failed') setTimeout(() => dismissOp(id), state === 'done' ? 1500 : 3000);
}

export function beginOp(opts: BeginOpOptions): OpHandle {
  const id = `op${++counter}`;
  const controller = new AbortController();
  internals.set(id, { controller, opts });
  const op: Op = {
    id,
    kind: opts.kind,
    label: opts.label,
    priority: opts.priority ?? 'visible',
    state: opts.queued ? 'queued' : 'running',
    startedAt: Date.now(),
    cancellable: true,
    retryable: !!opts.onRetry,
  };
  _ops.update((l) => [...l, op]);
  if (!opts.queued) armTimers(id);
  return {
    id,
    signal: controller.signal,
    start() {
      patch(id, { state: 'running', startedAt: Date.now() });
      armTimers(id);
    },
    update(message) {
      patch(id, { message });
    },
    done() {
      finish(id, 'done');
    },
    fail(message) {
      finish(id, 'failed', message);
    },
  };
}

/** Change an unfinished op's priority (e.g. a prefetch that the user now needs on screen). */
export function setOpPriority(id: string, priority: OpPriority): void {
  const op = get(_ops).find((o) => o.id === id);
  if (!op || ['done', 'failed', 'cancelled'].includes(op.state) || op.priority === priority) return;
  patch(id, { priority });
}

export function cancelOp(id: string): void {
  const i = internals.get(id);
  const op = get(_ops).find((o) => o.id === id);
  if (!i || !op || ['done', 'failed', 'cancelled'].includes(op.state)) return;
  i.controller.abort();
  try {
    i.opts.onCancel?.();
  } finally {
    finish(id, 'cancelled', 'Cancelled');
  }
}

/** Cancel every unfinished op of a kind (optionally only some priorities). */
export function cancelKind(kind: string, priorities?: OpPriority[]): void {
  for (const o of get(_ops)) {
    if (o.kind !== kind || ['done', 'failed', 'cancelled'].includes(o.state)) continue;
    if (priorities && !priorities.includes(o.priority)) continue;
    cancelOp(o.id);
  }
}

export function retryOp(id: string): void {
  const i = internals.get(id);
  const op = get(_ops).find((o) => o.id === id);
  if (!i || !op || op.state !== 'failed') return;
  dismissOp(id);
  i.opts.onRetry?.();
}

export function dismissOp(id: string): void {
  clearTimers(id);
  internals.delete(id);
  _ops.update((l) => l.filter((o) => o.id !== id));
}
