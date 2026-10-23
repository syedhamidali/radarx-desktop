/**
 * Undo / redo for a value that is replaced, never changed in place (the recipe being edited).
 *
 * `push(previous, key)` is called with the value as it was *before* an edit. Edits with the same
 * `key` within `coalesceMs` (typing in a number box, dragging a slider) share one step, so one
 * Undo takes back the whole gesture. Pure and clock-injectable for unit tests (history.test.mjs).
 */
export class History<T> {
  private past: T[] = [];
  private future: T[] = [];
  private lastKey: string | null = null;
  private lastAt = 0;

  private readonly limit: number;
  private readonly coalesceMs: number;

  constructor(limit = 100, coalesceMs = 800) {
    this.limit = limit;
    this.coalesceMs = coalesceMs;
  }

  get canUndo(): boolean {
    return this.past.length > 0;
  }

  get canRedo(): boolean {
    return this.future.length > 0;
  }

  push(previous: T, key: string | null = null, now = Date.now()): void {
    const joins = key !== null && key === this.lastKey && now - this.lastAt <= this.coalesceMs;
    this.lastKey = key;
    this.lastAt = now;
    this.future = [];
    if (joins && this.past.length > 0) return; // same gesture: keep the value before the first edit
    this.past.push(previous);
    if (this.past.length > this.limit) this.past.shift();
  }

  /** The value to go back to (given the current one), or undefined when there is nothing to undo. */
  undo(current: T): T | undefined {
    const value = this.past.pop();
    if (value === undefined) return undefined;
    this.future.push(current);
    this.lastKey = null;
    return value;
  }

  redo(current: T): T | undefined {
    const value = this.future.pop();
    if (value === undefined) return undefined;
    this.past.push(current);
    this.lastKey = null;
    return value;
  }

  clear(): void {
    this.past = [];
    this.future = [];
    this.lastKey = null;
  }
}
