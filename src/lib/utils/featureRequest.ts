/**
 * One-shot request/response helper for the analysis panels (QC, retrievals, statistics, storm
 * cells, 3D volume, dual-pol region stats ...).
 *
 * Every request is an operation in the operations tray (Cancel / Retry / timeout), settles on the
 * first matching reply or on the server error that answers it, and never leaves its panel spinning:
 * a panel only needs to say what to send, which reply types answer it and what to do with them.
 *
 * Several of the server handlers announce "10 %" progress and never announce completion; the
 * global progress bar would then hang on that message for ever, so a finished request clears it.
 */
import { wsManager } from './websocket';
import { beginOp, cancelOp } from './requestScheduler';
import { processingProgress, connectionStatus } from '../stores/radarData';
import { get } from 'svelte/store';

export type Outcome = 'done' | 'error' | 'cancelled' | 'timeout';

export interface FeatureRequestOptions {
  /** Op kind shown in the tray (e.g. 'qc', 'retrieval', 'stats'). */
  kind: string;
  /** Human label, e.g. "Running quality control". */
  label: string;
  /** The JSON message to send. */
  message: Record<string, any>;
  /** Reply message types that answer the request (the first one that passes `accept` wins). */
  resultTypes: string[];
  /** Optional filter for replies (e.g. match the variable). */
  accept?: (msg: any) => boolean;
  /**
   * Optional filter for server errors, for when several requests of the same type are in flight
   * (e.g. two scatter plots) and an error must be matched to its own request by its text.
   */
  acceptError?: (text: string) => boolean;
  /** Called with the accepted reply. */
  onResult: (msg: any) => void;
  /** Called once with how the request ended (after onResult for 'done'). */
  onSettled?: (outcome: Outcome, error?: string) => void;
  /**
   * Called with a reply that arrives after the request was cancelled or timed out (the server cannot
   * be interrupted, and some results are applied app-wide regardless).
   */
  onLate?: (msg: any) => void;
  /** Re-run the request (enables the tray's Retry button). */
  retry?: () => void;
  /** Mark failed after this many ms (default 60 s, 0 = never). */
  timeoutMs?: number;
}

export interface FeatureRequestHandle {
  cancel(): void;
  readonly active: boolean;
}

/** "get_scatter_data: ..." / "Invalid input for run_qc: ..." -> the request type the error names. */
function errorTarget(text: string): string | null {
  const m = /\b((?:get|run|identify|track|list)_[a-z0-9_]+)/.exec(text);
  return m ? m[1] : null;
}

export function runFeatureRequest(opts: FeatureRequestOptions): FeatureRequestHandle {
  let settled = false;
  let lateListening = false;
  const unsubs: (() => void)[] = [];
  const reqType = String(opts.message.type ?? '');

  const op = beginOp({
    kind: opts.kind,
    label: opts.label,
    timeoutMs: opts.timeoutMs ?? 60000,
    onCancel: () => settle('cancelled'),
    onTimeout: () => settle('timeout'),
    onRetry: opts.retry,
  });

  function clearProgress(): void {
    const p = get(processingProgress);
    if (p && p.percent < 100) processingProgress.set(null);
  }

  function detach(): void {
    for (const u of unsubs.splice(0)) u();
  }

  function settle(outcome: Outcome, error?: string): void {
    if (settled) return;
    settled = true;
    if (outcome === 'done') op.done();
    else if (outcome === 'error') op.fail(error ?? 'Request failed');
    // cancelled / timeout: the op was already moved to its final state by the scheduler.
    if (outcome === 'done' || outcome === 'error') clearProgress();
    if (opts.onLate && (outcome === 'cancelled' || outcome === 'timeout')) {
      // Keep only the result listeners alive (the error listener would resurrect nothing).
      lateListening = true;
      const keep = unsubs.splice(0, opts.resultTypes.length);
      detach();
      unsubs.push(...keep);
    } else {
      detach();
    }
    try {
      opts.onSettled?.(outcome, error);
    } catch (err) {
      console.error('[featureRequest] onSettled failed:', err);
    }
  }

  for (const type of opts.resultTypes) {
    unsubs.push(
      wsManager.onMessage(type, (msg: any) => {
        if (opts.accept && !opts.accept(msg)) return;
        if (settled) {
          if (lateListening) {
            lateListening = false;
            detach();
            try {
              opts.onLate?.(msg);
            } catch (err) {
              console.error('[featureRequest] onLate failed:', err);
            }
          }
          return;
        }
        try {
          opts.onResult(msg);
        } catch (err) {
          console.error('[featureRequest] onResult failed:', err);
        }
        settle('done');
      }),
    );
  }
  unsubs.push(
    wsManager.onMessage('error', (msg: any) => {
      if (settled) return;
      const text = typeof msg?.message === 'string' ? msg.message : 'Request failed';
      const target = errorTarget(text);
      if (target && reqType && target !== reqType) return; // another request's error
      if (opts.acceptError && !opts.acceptError(text)) return;
      settle('error', text);
    }),
  );

  if (get(connectionStatus) !== 'connected') {
    // Nothing would ever answer: fail now instead of after the timeout.
    queueMicrotask(() => settle('error', 'Not connected to the analysis engine'));
  } else {
    void wsManager.send(opts.message);
  }

  return {
    cancel: () => {
      if (!settled) cancelOp(op.id);
    },
    get active() {
      return !settled;
    },
  };
}
