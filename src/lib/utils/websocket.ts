import { beginOp, cancelOp, dismissOp, setOpPriority, type OpHandle, type OpPriority } from './requestScheduler';
import { pickDefaultVariable, firstSweepWith } from './variables';
import { get } from 'svelte/store';
import {
  connectionStatus,
  radarData,
  selectedVariable,
  selectedSweep,
  renderResult,
  processingProgress,
  exportNotification,
} from '../stores/radarData';
import { addToast } from '../stores/toasts';
import { cacheSweepData, clearSweepCache, getCachedSweep, type SweepDataEntry } from './ppiRenderer';
import { addFile, getActiveFile, saveActiveSelection, type FileEntry } from '../stores/fileManager';
import { perfStats } from '../stores/perfStats';
import { registerVariableStyle, styleFor } from './variableStyles';

type MessageHandler = (data: any) => void;

/** A gridded module product assembled from a `module_field` binary payload. */
export interface ModuleFieldEntry {
  module: string;
  name: string;
  sweep: number;
  fileId?: string;
  azimuth: Float32Array;
  range: Float32Array;
  values: Float32Array;
  nAz: number;
  nRange: number;
  maxRange: number;
  vmin: number;
  vmax: number;
  units: string;
}

/** State of one binary payload being reassembled (JSON header + N binary frames). */
interface InflightBinary {
  header: any;
  /** Number of binary frames announced by the header (-1 = unknown, use byte count). */
  expectedChunks: number;
  /** Total payload size in bytes derived from the header (-1 = unknown). */
  expectedBytes: number;
  chunksSeen: number;
  received: number;
  /** Preallocated destination when the total size is known. */
  buf: Uint8Array | null;
  /** Fallback accumulation when the total size is unknown. */
  parts: Uint8Array[];
  /** Zero-copy fast path: the single frame itself. */
  single: ArrayBuffer | null;
  startedAt: number;
}

const MAX_RETRY_DELAY_MS = 5000;
/** Messages queued while the socket is still CONNECTING (flushed on open). */
const MAX_QUEUED_MESSAGES = 64;

// ── Sweep request scheduling ─────────────────────────────────────────────────
/** At most this many `get_sweep_data` requests are on the wire at once. */
const SWEEP_MAX_CONCURRENT = 3;
/** An in-flight sweep request that has not been answered after this long is failed (Retry offered). */
const SWEEP_TIMEOUT_MS = 25000;
/** After this long an in-flight sweep op is flagged 'slow' ("still working..."). */
const SWEEP_SLOW_MS = 4000;
/** Replies for cancelled / timed-out requests are still cached if they show up within this window. */
const ORPHAN_TTL_MS = 120000;
const PRIORITY_RANK: Record<OpPriority, number> = { visible: 0, prefetch: 1, background: 2 };

/** Options for {@link WebSocketManager.requestSweepData}. */
export interface SweepRequestOptions {
  /** 'visible' (default): needed on screen now. 'prefetch': cache warm-up. 'background': bulk (3D volume). */
  priority?: OpPriority;
  /**
   * Owner tag. Grouped requests are managed by their owner (see `cancelGroup`) and are NOT
   * dropped by `setViewContext` pruning (the timeline uses this for frames ahead).
   */
  group?: string;
  /** Called once when this request settles (every de-duplicated caller gets its own call). */
  onSettled?: (result: 'done' | 'failed' | 'cancelled', info?: { reason?: SweepFailReason; message?: string }) => void;
}

export type SweepFailReason = 'unavailable' | 'timeout' | 'error';

/** What the viewer is currently showing; used to drop stale queued requests. */
export interface ViewContext {
  fileId?: string | null;
  variable: string | null;
  sweep: number | null;
}

interface SweepJob {
  key: string;
  seq: number;
  variable: string;
  sweep: number;
  /** Normalised file id ('' = server default). */
  fileId: string;
  priority: OpPriority;
  group?: string;
  /** Reply should be announced as `sweep_data_ready` (false = cache warm-up only). */
  broadcast: boolean;
  state: 'queued' | 'inflight' | 'retired';
  handle: OpHandle;
  waiters: NonNullable<SweepRequestOptions['onSettled']>[];
}

class WebSocketManager {
  private ws: WebSocket | null = null;
  private port: number | null = null;
  private retryCount = 0;
  private maxRetries = 60;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private messageHandlers: Map<string, MessageHandler[]> = new Map();
  private baseDelay = 500; // ms
  private authToken: string | null = null;
  private authTokenPromise: Promise<string | null> | null = null;
  /** Messages submitted while the socket is CONNECTING. */
  private sendQueue: Record<string, any>[] = [];

  // ── Sweep request queue ──
  /** Every outstanding (queued or in-flight) sweep request by `variable:sweep:fileId` (de-duplication). */
  private sweepJobs = new Map<string, SweepJob>();
  /** Requests on the wire, oldest first. */
  private sweepInflight: SweepJob[] = [];
  private sweepSeq = 0;
  private maxConcurrentSweeps = SWEEP_MAX_CONCURRENT;
  private drainSuspended = 0;
  private viewCtx: { fileId: string; variable: string | null; sweep: number | null } | null = null;
  /** Requests that were abandoned while on the wire: their reply may still arrive (cache-only). */
  private orphans = new Map<string, { fileId: string; t: number }[]>();
  /** Last failed op per request key, so a fresh request can clear the stale failure. */
  private failedSweepOps = new Map<string, string>();
  /** `fileId|variable:sweep` the server said does not exist. */
  private unavailableSweeps = new Set<string>();

  // Timers for transient notifications (so a new message cancels the old timer)
  private progressClearTimer: ReturnType<typeof setTimeout> | null = null;
  private exportClearTimer: ReturnType<typeof setTimeout> | null = null;

  /**
   * Connect to the Python sidecar WebSocket server.
   */
  connect(port: number): void {
    // Skip if already connected to this port
    if (this.port === port && this.ws?.readyState === WebSocket.OPEN) {
      return;
    }
    // Skip if already connecting to this port
    if (this.port === port && this.ws?.readyState === WebSocket.CONNECTING) {
      return;
    }
    this.port = port;
    this.retryCount = 0;
    // Cancel any pending reconnect timer
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    this.doConnect();
  }

  /** Manually restart the connection (e.g. after retries were exhausted). */
  reconnect(): void {
    if (this.port == null) return;
    if (this.ws?.readyState === WebSocket.OPEN) return;
    this.retryCount = 0;
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    this.doConnect();
  }

  /** Drop all per-connection protocol state (called on connect, close, disconnect). */
  private resetConnectionState(): void {
    this.inflight = null;
    this.sendQueue.length = 0;
    // Nothing sent on the old connection will be answered any more.
    this.failAllSweepJobs('Connection lost');
    this.orphans.clear();
  }

  private doConnect(): void {
    if (!this.port) return;

    // Clean up any existing connection
    if (this.ws) {
      const old = this.ws;
      old.onopen = null;
      old.onclose = null;
      old.onmessage = null;
      old.onerror = null;
      if (old.readyState === WebSocket.OPEN || old.readyState === WebSocket.CONNECTING) {
        try { old.close(); } catch { /* ignore */ }
      }
      this.ws = null;
    }
    this.resetConnectionState();

    connectionStatus.set('connecting');
    const url = `ws://localhost:${this.port}`;

    let ws: WebSocket;
    try {
      ws = new WebSocket(url);
      ws.binaryType = 'arraybuffer';
    } catch (err) {
      console.error('[WS] Failed to create WebSocket:', err);
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;

    ws.onopen = () => {
      if (this.ws !== ws) return;
      console.log(`[WS] Connected to ${url}`);
      connectionStatus.set('connected');
      this.retryCount = 0;
      this.flushSendQueue();
    };

    ws.onclose = (event) => {
      if (this.ws !== ws) return;
      console.log(`[WS] Disconnected (code: ${event.code}, reason: ${event.reason})`);
      // Anything half-received on this connection is unusable now.
      this.resetConnectionState();
      connectionStatus.set('disconnected');
      this.scheduleReconnect();
    };

    ws.onerror = (event) => {
      console.error('[WS] Error:', event);
    };

    ws.onmessage = (event) => {
      if (this.ws !== ws) return;
      try {
        this.handleMessage(event.data);
      } catch (err) {
        // Never let a bad message kill the socket's message pump.
        console.error('[WS] Unhandled error while processing message:', err);
      }
    };
  }

  private authResolved = false;

  private async getAuthToken(): Promise<string | null> {
    if (this.authResolved) return this.authToken;
    if (this.authTokenPromise) return this.authTokenPromise;

    // Outside Tauri there is no token to fetch: settle once, forever.
    if (typeof window !== 'undefined' && !(window as any).__TAURI_INTERNALS__) {
      this.authResolved = true;
      this.authToken = null;
      return null;
    }

    this.authTokenPromise = (async () => {
      try {
        const { invoke } = await import('@tauri-apps/api/core');
        const token = await invoke<string>('get_sidecar_token');
        this.authToken = typeof token === 'string' && token.length > 0 ? token : null;
        // Only a real token settles the lookup; a failure (sidecar not up yet) is retried.
        this.authResolved = this.authToken !== null;
      } catch {
        this.authToken = null;
      } finally {
        this.authTokenPromise = null;
      }
      return this.authToken;
    })();

    return this.authTokenPromise;
  }

  private scheduleReconnect(): void {
    if (this.retryCount >= this.maxRetries) {
      console.warn(`[WS] Max retries (${this.maxRetries}) reached. Giving up.`);
      connectionStatus.set('disconnected');
      return;
    }

    // Exponential backoff, capped: the sidecar monitor restarts the server on
    // the same port within seconds, so waiting minutes between attempts is useless.
    const delay = Math.min(this.baseDelay * Math.pow(2, this.retryCount), MAX_RETRY_DELAY_MS);
    this.retryCount++;
    console.log(`[WS] Reconnecting in ${delay}ms (attempt ${this.retryCount}/${this.maxRetries})...`);

    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
    }

    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this.doConnect();
    }, delay);
  }

  /** Binary payload currently being reassembled (the server sends frames strictly in order). */
  private inflight: InflightBinary | null = null;

  /** Path of the file currently being opened (set before send, consumed by file_opened). */
  private _pendingOpenPath: string | null = null;

  /** Types whose JSON header is followed by binary frames. */
  private static readonly BINARY_TYPES = new Set([
    'sweep_data',
    'module_field',
    'qc_result',
    'difference_result',
    'retrieval_data',
    'volume_data',
    'cross_section_3d_data',
    'mosaic_result',
  ]);

  /** Start tracking a binary payload announced by `header`. */
  private beginBinary(header: any): void {
    if (this.inflight) {
      console.warn(
        `[WS] New '${header.type}' header while '${this.inflight.header.type}' was incomplete ` +
        `(${this.inflight.chunksSeen}/${this.inflight.expectedChunks} frames) — dropping the stale payload`,
      );
      this.failSweepForHeader(this.inflight.header, 'Incomplete reply from the server');
    }

    // Total payload size, derived from whichever byte counts the header carries.
    let expectedBytes = -1;
    if (typeof header.azimuth_bytes === 'number' && typeof header.range_bytes === 'number') {
      const valBytes =
        typeof header.values_bytes === 'number' ? header.values_bytes
        : typeof header.diff_bytes === 'number' ? header.diff_bytes
        : (typeof header.n_az === 'number' && typeof header.n_range === 'number')
          ? header.n_az * header.n_range * 4
          : -1;
      if (valBytes >= 0) expectedBytes = header.azimuth_bytes + header.range_bytes + valBytes;
    } else if (typeof header.byte_length === 'number') {
      expectedBytes = header.byte_length;
    } else if (typeof header.png_bytes === 'number') {
      // mosaic_result: one PNG image split over `chunks` frames
      expectedBytes = header.png_bytes;
    }

    // Frame count: trust the header; `difference_result` omits it, so fall back to bytes.
    let expectedChunks = -1;
    if (typeof header.chunks === 'number' && header.chunks >= 0) expectedChunks = header.chunks;
    else if (expectedBytes < 0) expectedChunks = 1;

    this.inflight = {
      header,
      expectedChunks,
      expectedBytes,
      chunksSeen: 0,
      received: 0,
      buf: null,
      parts: [],
      single: null,
      startedAt: performance.now(),
    };

    // A payload with zero frames (empty array) completes immediately.
    if (expectedChunks === 0 || expectedBytes === 0) {
      this.completeBinary(new ArrayBuffer(0));
    }
  }

  /** Append one binary frame to the in-flight payload; finalise when complete. */
  private onBinaryFrame(raw: ArrayBuffer): void {
    const st = this.inflight;
    if (!st) {
      console.warn(`[WS] Dropping unexpected binary frame (${raw.byteLength} bytes) — no header pending`);
      return;
    }

    st.chunksSeen++;
    const len = raw.byteLength;

    if (st.expectedBytes >= 0 && st.received + len > st.expectedBytes) {
      console.error(
        `[WS] '${st.header.type}' payload overflow (${st.received + len} > ${st.expectedBytes} bytes) — discarding`,
      );
      this.abortInflight();
      return;
    }

    if (st.chunksSeen === 1 && len === st.expectedBytes) {
      // Whole payload arrived in one frame: use it directly, no copy at all.
      st.single = raw;
    } else if (st.expectedBytes >= 0) {
      // Known size: copy each frame once into a single preallocated buffer.
      if (!st.buf) st.buf = new Uint8Array(st.expectedBytes);
      st.buf.set(new Uint8Array(raw), st.received);
    } else {
      st.parts.push(new Uint8Array(raw));
    }
    st.received += len;

    const done = st.expectedChunks >= 0
      ? st.chunksSeen >= st.expectedChunks
      : st.received >= st.expectedBytes;
    if (!done) return;

    let buffer: ArrayBuffer;
    if (st.single) {
      buffer = st.single;
    } else if (st.buf) {
      buffer = st.buf.buffer as ArrayBuffer;
    } else if (st.parts.length === 1) {
      const p = st.parts[0];
      buffer = (p.byteOffset === 0 && p.byteLength === p.buffer.byteLength
        ? p.buffer
        : p.slice().buffer) as ArrayBuffer;
    } else {
      const out = new Uint8Array(st.received);
      let off = 0;
      for (const p of st.parts) { out.set(p, off); off += p.byteLength; }
      buffer = out.buffer as ArrayBuffer;
    }

    if (st.expectedBytes >= 0 && st.received !== st.expectedBytes) {
      console.error(
        `[WS] '${st.header.type}' payload truncated (${st.received}/${st.expectedBytes} bytes) — discarding`,
      );
      this.abortInflight();
      return;
    }
    this.completeBinary(buffer);
  }

  /** Dispatch a fully reassembled payload and clear the in-flight slot. */
  private completeBinary(buffer: ArrayBuffer): void {
    const st = this.inflight;
    if (!st) return;
    this.inflight = null;

    const header = st.header;
    const transferMs = performance.now() - st.startedAt;
    perfStats.update((s) => ({ ...s, dataTransferTimeMs: transferMs }));

    const type: string = header.type;
    if (type === 'volume_data' || type === 'cross_section_3d_data' || type === 'retrieval_data') {
      // Raw float32 grids, not azimuth+range+values
      this.handleRawBinaryData(header, buffer);
    } else if (type === 'module_field') {
      this.handleModuleField(header, buffer);
    } else if (type === 'mosaic_result') {
      // PNG bytes + the JSON header; the mosaic store builds the object URL.
      this._dispatchHandlers('mosaic_result_ready', { header, png: new Uint8Array(buffer) });
    } else {
      this.handleBinarySweepData(header, buffer);
    }
  }

  private abortInflight(): void {
    const st = this.inflight;
    this.inflight = null;
    if (st) this.failSweepForHeader(st.header, 'Corrupted reply from the server');
  }

  /** A sweep payload could not be used: fail the request it answered so nothing waits for it. */
  private failSweepForHeader(header: any, message: string): void {
    if (!header || header.type !== 'sweep_data' || typeof header.variable !== 'string') return;
    const job = this.sweepInflight.find((j) => j.variable === header.variable && j.sweep === header.sweep);
    if (job) this.failJob(job, message, 'error');
  }

  private handleMessage(raw: string | ArrayBuffer): void {
    // Binary message → chunk of data following a JSON header
    if (typeof raw !== 'string') {
      this.onBinaryFrame(raw as ArrayBuffer);
      return;
    }

    let msg: any;
    try {
      msg = JSON.parse(raw);
    } catch (err) {
      console.error('[WS] Failed to parse message:', raw.length > 200 ? raw.slice(0, 200) + '…' : raw);
      return;
    }

    const type: string = msg.type;
    if (!type) {
      console.warn('[WS] Message has no type field:', msg);
      return;
    }

    // Dispatch to built-in store handlers
    switch (type) {
      case 'file_opened': {
        try {
          const vars: string[] = msg.data?.variables ?? [];
          const swps = msg.data?.sweeps ?? [];
          const dims = msg.data?.dimensions ?? {};
          const attrs = msg.data?.attributes ?? {};
          const fileId: string | undefined = msg.file_id;
          const openedPath = this._pendingOpenPath ?? msg.data?.source ?? '';
          this._pendingOpenPath = null;

          // Store the outgoing file's selection before the stores are reset below
          // (addFile would otherwise save the already-reset values into it).
          saveActiveSelection();

          processingProgress.set(null);
          clearSweepCache();
          // Queued requests issued against the previous file are moot.
          this.cancelQueuedSweeps((j) => !j.group);
          this.unavailableSweeps.clear();

          const firstVar = pickDefaultVariable(vars);
          const firstSweep = firstSweepWith(swps, firstVar);

          // Clear variable first so no stale render fires while stores are updating
          selectedVariable.set(null);
          selectedSweep.set(firstSweep);

          radarData.update((state) => ({
            ...state,
            variables: vars,
            dimensions: dims,
            attributes: attrs,
            sweeps: swps,
            filePath: openedPath || state.filePath,
          }));

          // Register in the multi-file manager if we got a file_id
          if (fileId && openedPath) {
            const filename = openedPath.split('/').pop() ?? openedPath;
            const entry: FileEntry = {
              id: fileId,
              path: openedPath,
              filename,
              variables: vars,
              sweeps: swps,
              dimensions: dims,
              attributes: attrs,
              selectedVariable: firstVar,
              selectedSweep: firstSweep,
            };
            addFile(entry, { skipSaveOutgoing: true });
          }

          if (firstVar) {
            // Set variable last — unsubVarStore fires here with sweep already at firstSweep
            selectedVariable.set(firstVar);
            addToast('success', `Loaded ${vars.length} variables, ${swps.length} sweeps`);
            this.requestSweepData(firstVar, firstSweep);
          } else {
            console.warn('[WS] file_opened: no variables in schema', msg.data);
          }
        } catch (err) {
          console.error('[WS] file_opened handler error:', err);
          addToast('error', `Failed to process opened file: ${err}`);
        }
        break;
      }

      case 'sweep_data':
      case 'module_field':
      case 'qc_result':
      case 'difference_result':
      case 'retrieval_data':
      case 'volume_data':
      case 'cross_section_3d_data':
      case 'mosaic_result':
        // JSON header for incoming binary data — wait for the chunk frames
        this.beginBinary(msg);
        break;

      case 'render_result':
        renderResult.set({
          image: msg.image ?? null,
          metadata: msg.metadata ?? null,
        });
        break;

      case 'progress':
        processingProgress.set({
          percent: msg.percent ?? 0,
          message: msg.message ?? '',
        });
        // Clear progress when complete (a newer progress message cancels the pending clear)
        if (this.progressClearTimer) {
          clearTimeout(this.progressClearTimer);
          this.progressClearTimer = null;
        }
        if (msg.percent >= 100) {
          this.progressClearTimer = setTimeout(() => {
            this.progressClearTimer = null;
            processingProgress.set(null);
          }, 2000);
        }
        break;

      case 'export_complete':
        this.showExportNotification(msg.path ?? 'Export complete');
        break;

      case 'batch_export_complete':
        this.showExportNotification(
          `Batch export complete: ${msg.count ?? 0} files in ${msg.output_dir ?? 'temp'}`
        );
        break;

      case 'animation_export_complete':
        this.showExportNotification(msg.path ?? 'Animation export complete');
        break;

      case 'mosaic_error':
      case 'mosaic_probe_result':
        // Handled by custom handlers registered in the mosaic store
        break;

      case 'cross_section_result':
      case 'vertical_profile_result':
        // Handled by custom handlers registered in CrossSectionPanel
        break;

      case 'module_volume_result':
      case 'derived_variables':
        // Handled by custom handlers registered in the modules / derived stores
        break;

      case 'data_table_result':
      case 'metadata_tree_result':
        // Handled by custom handlers registered in DataTable / MetadataViewer
        break;

      case 'error': {
        const text: string = typeof msg.message === 'string' ? msg.message : 'Unknown error from server';
        // Errors that answer a queued sweep request fail that request (the viewer reacts to
        // `sweep_request_failed`); "variable not in this sweep" is expected on split-cut volumes
        // and stays silent.
        const sweepErr = this.handleSweepError(text);
        // Errors attributed to a sweep request are reported through `sweep_request_failed`. They must
        // NOT reach the generic 'error' handlers (those fail running module/export/open/mosaic ops)
        // nor clear the global progress bar of some unrelated task.
        if (sweepErr === 'quiet') {
          console.warn('[WS] Sweep unavailable:', text);
          return;
        }
        if (sweepErr === 'handled') {
          console.error('[WS] Sweep request failed:', text);
          this.notifyError(text);
          return;
        }
        console.error('[WS] Server error:', text);
        processingProgress.set(null);
        this.notifyError(text);
        break;
      }

      default:
        console.log('[WS] Unhandled message type:', type, msg);
    }

    // Dispatch to custom handlers
    const handlers = this.messageHandlers.get(type);
    if (handlers) {
      for (const handler of handlers) {
        try {
          handler(msg);
        } catch (err) {
          console.error(`[WS] Handler error for type '${type}':`, err);
        }
      }
    }
  }

  private showExportNotification(text: string): void {
    exportNotification.set(text);
    if (this.exportClearTimer) clearTimeout(this.exportClearTimer);
    this.exportClearTimer = setTimeout(() => {
      this.exportClearTimer = null;
      exportNotification.set(null);
    }, 5000);
  }

  private notifyError(message: string): void {
    addToast('error', message, 8000);
  }

  /**
   * Register a custom handler for a specific message type.
   */
  onMessage(type: string, handler: MessageHandler): () => void {
    // Copy-on-write: dispatch iterates the array it grabbed, so a handler that
    // (un)subscribes during dispatch cannot cause others to be skipped.
    const current = this.messageHandlers.get(type);
    this.messageHandlers.set(type, current ? [...current, handler] : [handler]);

    // Return an unsubscribe function (idempotent)
    return () => {
      const handlers = this.messageHandlers.get(type);
      if (!handlers) return;
      const idx = handlers.indexOf(handler);
      if (idx < 0) return;
      if (handlers.length === 1) {
        this.messageHandlers.delete(type);
      } else {
        this.messageHandlers.set(type, [...handlers.slice(0, idx), ...handlers.slice(idx + 1)]);
      }
    };
  }

  /**
   * Handle binary sweep data received after a sweep_data JSON header.
   *
   * The reassembled buffer is laid out as [azimuth][range][values] (float32).
   * The three arrays are zero-copy views over that single buffer.
   */
  private handleBinarySweepData(header: any, buffer: ArrayBuffer): void {
    const azBytes = header.azimuth_bytes as number;
    const rngBytes = header.range_bytes as number;
    const msgType: string = header.type ?? 'sweep_data';
    const isQc = msgType === 'qc_result';
    const key = `${header.variable}:${header.sweep}`;

    const nValues = Math.floor((buffer.byteLength - azBytes - rngBytes) / 4);
    if (
      !Number.isInteger(azBytes) || !Number.isInteger(rngBytes) ||
      azBytes < 0 || rngBytes < 0 || azBytes % 4 !== 0 || rngBytes % 4 !== 0 ||
      nValues < 0 || nValues < header.n_az * header.n_range
    ) {
      console.error(
        `[WS] Malformed '${msgType}' payload for ${key}: ${buffer.byteLength} bytes, ` +
        `az=${azBytes}, range=${rngBytes}, grid=${header.n_az}x${header.n_range}`,
      );
      if (msgType === 'sweep_data') this.failSweepForHeader(header, 'Malformed reply from the server');
      return;
    }

    const entry: SweepDataEntry = {
      variable: header.variable,
      sweep: header.sweep,
      azimuth: new Float32Array(buffer, 0, azBytes / 4),
      range: new Float32Array(buffer, azBytes, rngBytes / 4),
      values: new Float32Array(buffer, azBytes + rngBytes, nValues),
      nAz: header.n_az,
      nRange: header.n_range,
      maxRange: header.max_range,
      vmin: header.vmin,
      vmax: header.vmax,
      units: header.units,
    };
    if (header.derived === true) {
      // A derived variable (plugin module output) carries its own presentation. The registry normally
      // knows it already (derived_variables); register from the header when it does not, so the sweep
      // still renders with the right colours and legend.
      entry.derived = true;
      if (typeof header.label === 'string') entry.label = header.label;
      entry.categories = Array.isArray(header.categories) && header.categories.length > 0 ? header.categories : null;
      entry.cmap = typeof header.cmap === 'string' ? header.cmap : null;
      if (!styleFor(entry.variable)) {
        registerVariableStyle(entry.variable, {
          label: entry.label,
          units: entry.units,
          vmin: entry.vmin,
          vmax: entry.vmax,
          cmap: entry.cmap,
          categories: entry.categories,
        });
      }
    }

    this.finalizeSweepEntry(entry, isQc, msgType);
  }

  /**
   * Handle a gridded module product (`module_field`): same [azimuth][range][values] layout
   * as sweep data, but it is NOT cached or broadcast as `sweep_data_ready` — it must never
   * replace the displayed variable. Listeners get `module_field_ready` with the entry.
   */
  private handleModuleField(header: any, buffer: ArrayBuffer): void {
    const azBytes = header.azimuth_bytes as number;
    const rngBytes = header.range_bytes as number;
    const nValues = Math.floor((buffer.byteLength - azBytes - rngBytes) / 4);
    if (
      !Number.isInteger(azBytes) || !Number.isInteger(rngBytes) ||
      azBytes < 0 || rngBytes < 0 || azBytes % 4 !== 0 || rngBytes % 4 !== 0 ||
      nValues < 0 || nValues < header.n_az * header.n_range
    ) {
      console.error(
        `[WS] Malformed 'module_field' payload for ${header.module}/${header.name}: ` +
        `${buffer.byteLength} bytes, az=${azBytes}, range=${rngBytes}, grid=${header.n_az}x${header.n_range}`,
      );
      return;
    }
    const entry: ModuleFieldEntry = {
      module: header.module,
      name: header.name,
      sweep: header.sweep,
      fileId: header.file_id,
      azimuth: new Float32Array(buffer, 0, azBytes / 4),
      range: new Float32Array(buffer, azBytes, rngBytes / 4),
      values: new Float32Array(buffer, azBytes + rngBytes, nValues),
      nAz: header.n_az,
      nRange: header.n_range,
      maxRange: header.max_range,
      vmin: header.vmin,
      vmax: header.vmax,
      units: header.units ?? '',
    };
    this._dispatchHandlers('module_field_ready', entry);
  }

  /**
   * Handle raw binary grid data (volume_data, cross_section_3d_data, retrieval_data).
   * These are flat Float32Arrays without azimuth/range preamble.
   */
  private handleRawBinaryData(header: any, buffer: ArrayBuffer): void {
    if (buffer.byteLength % 4 !== 0) {
      console.error(`[WS] '${header.type}' payload is not float32-aligned (${buffer.byteLength} bytes)`);
      return;
    }
    const data = new Float32Array(buffer);
    const payload = { ...header, data };
    this._dispatchHandlers(header.type, payload);
    if (header.type === 'retrieval_data') {
      this._dispatchHandlers('retrieval_data_ready', payload);
    }
  }

  /**
   * Common path after binary data has been parsed.
   * Caches the entry, notifies listeners, and triggers prefetch.
   */
  private finalizeSweepEntry(entry: SweepDataEntry, isQc: boolean, msgType = 'sweep_data'): void {
    if (msgType === 'difference_result') {
      // A difference is not the sweep it is named after: it must neither replace that sweep in the
      // cache nor be announced as sweep data (the main PPI would show the difference disk).
      this._dispatchHandlers('difference_data_ready', entry);
      return;
    }
    if (msgType !== 'sweep_data') {
      // QC results are not scheduled requests; they intentionally replace the displayed data.
      cacheSweepData(entry);
      if (isQc) this._dispatchHandlers('qc_data_ready', entry);
      this._dispatchHandlers('sweep_data_ready', entry);
      return;
    }

    const key = `${entry.variable}:${entry.sweep}`;
    let broadcast = true;
    let cache = true;
    const job = this.sweepInflight.find((j) => j.variable === entry.variable && j.sweep === entry.sweep);
    if (job) {
      this.retireJob(job);
      this.clearFailedOp(job.key);
      job.handle.done();
      // A prefetched neighbour only warms the cache: broadcasting it as sweep_data_ready would make
      // the viewer show it instead of the sweep the user selected. It is announced if it turns out
      // to be the sweep on screen (the user got there while it was loading).
      broadcast = job.broadcast || this.matchesContext(job.variable, job.sweep, job.fileId);
      // The cache is not file-aware: another file's lookahead must not poison the shown file's entries.
      cache = broadcast || this.sameAsContextFile(job.fileId);
      this.settleWaiters(job, 'done');
    } else {
      const orphan = this.takeOrphan(key);
      if (orphan) {
        // The request was cancelled / timed out meanwhile: keep the data, never repaint the view.
        broadcast = false;
        cache = this.sameAsContextFile(orphan.fileId);
      }
      // else: unsolicited reply — legacy behaviour (cache + announce).
    }

    if (cache) cacheSweepData(entry);
    if (broadcast) this._dispatchHandlers('sweep_data_ready', entry);

    // Warm the neighbours of the sweep on screen.
    if (broadcast && !isQc) {
      const c = this.viewCtx;
      if (!c || c.variable === null || (c.variable === entry.variable && c.sweep === entry.sweep)) {
        this.prefetchAdjacentSweeps(entry.variable, entry.sweep);
      }
    }
    this.drainSweepQueue();
  }

  /** Fire all registered handlers for a given event type. */
  private _dispatchHandlers(eventType: string, data: any): void {
    const handlers = this.messageHandlers.get(eventType);
    if (handlers) {
      for (const handler of handlers) {
        try { handler(data); } catch (err) {
          console.error(`[WS] ${eventType} handler error:`, err);
        }
      }
    }
  }

  // ── Sweep request queue ──────────────────────────────────────────────────

  /**
   * Prefetch sweep N-1 and N+1 in the background so switching is instant.
   * Skips sweeps that are cached, already requested, or known not to carry the variable.
   */
  private prefetchAdjacentSweeps(variable: string, currentSweep: number): void {
    const sweepList = get(radarData).sweeps;
    const totalSweeps = sweepList.length;
    const fileId = this.viewCtx?.fileId ?? getActiveFile()?.id ?? '';
    for (const s of [currentSweep - 1, currentSweep + 1]) {
      if (s < 0 || s >= totalSweeps) continue;

      // Skip if this sweep's variable list is known and doesn't include the variable
      const sweepInfo = sweepList.find((sw) => sw.index === s);
      if (sweepInfo?.variables && sweepInfo.variables.length > 0 && !sweepInfo.variables.includes(variable)) continue;
      if (this.isUnavailable(variable, s, fileId)) continue;
      if (getCachedSweep(variable, s)) continue;
      if (this.sweepJobs.has(this.jobKey(variable, s, fileId))) continue;

      this.requestSweepData(variable, s, fileId || undefined, { priority: 'prefetch' });
    }
  }

  private jobKey(variable: string, sweep: number, fileId: string): string {
    return `${variable}:${sweep}:${fileId}`;
  }

  /** Tell the scheduler what the user is looking at so stale queued work can be dropped. */
  setViewContext(ctx: ViewContext): void {
    this.viewCtx = {
      fileId: ctx.fileId ?? getActiveFile()?.id ?? '',
      variable: ctx.variable,
      sweep: ctx.sweep,
    };
    this.cancelQueuedSweeps((j) => !j.group && !this.isRelevant(j));
    // Showing a cached sweep never goes through a reply, so warm its neighbours here.
    const { variable, sweep } = this.viewCtx;
    if (variable !== null && sweep !== null && getCachedSweep(variable, sweep)) {
      this.prefetchAdjacentSweeps(variable, sweep);
    }
  }

  /** Whether a queued request still matters for the current view. */
  private isRelevant(j: SweepJob): boolean {
    const c = this.viewCtx;
    if (!c || c.variable === null || c.sweep === null) return true;
    if (c.fileId && j.fileId && c.fileId !== j.fileId) return false;
    switch (j.priority) {
      case 'visible':
        return j.variable === c.variable && j.sweep === c.sweep;
      case 'prefetch':
        return j.variable === c.variable && Math.abs(j.sweep - c.sweep) <= 1;
      default:
        return j.variable === c.variable;
    }
  }

  private matchesContext(variable: string, sweep: number, fileId: string): boolean {
    const c = this.viewCtx;
    if (!c || c.variable === null) return false;
    if (c.variable !== variable || c.sweep !== sweep) return false;
    return !c.fileId || !fileId || c.fileId === fileId;
  }

  private sameAsContextFile(fileId: string): boolean {
    const c = this.viewCtx;
    return !c || !c.fileId || !fileId || c.fileId === fileId;
  }

  /** Cancel queued (never in-flight) sweep requests matching `pred`. */
  private cancelQueuedSweeps(pred: (j: SweepJob) => boolean): void {
    for (const j of [...this.sweepJobs.values()]) {
      if (j.state === 'queued' && pred(j)) cancelOp(j.handle.id);
    }
  }

  /** Cancel the queued requests that were issued under `group`. */
  cancelGroup(group: string): void {
    this.cancelQueuedSweeps((j) => j.group === group);
  }

  /** True if the server already said `variable` does not exist in `sweep` (for that file). */
  isUnavailable(variable: string, sweep: number, fileId?: string): boolean {
    const fid = fileId ?? this.viewCtx?.fileId ?? getActiveFile()?.id ?? '';
    return this.unavailableSweeps.has(`${fid}|${variable}:${sweep}`);
  }

  /** Forget "not in this sweep" answers for a variable (a derived variable was recomputed). */
  clearUnavailable(variable: string): void {
    const needle = `|${variable}:`;
    for (const k of [...this.unavailableSweeps]) {
      if (k.includes(needle)) this.unavailableSweeps.delete(k);
    }
  }

  /** Number of sweep requests queued / on the wire (diagnostics and tests). */
  get sweepQueueStats(): { queued: number; inflight: number; max: number } {
    return {
      queued: this.sweepJobs.size - this.sweepInflight.length,
      inflight: this.sweepInflight.length,
      max: this.maxConcurrentSweeps,
    };
  }

  /** Change the in-flight cap (default 3). */
  setMaxConcurrentSweeps(n: number): void {
    this.maxConcurrentSweeps = Math.max(1, Math.floor(n));
    this.drainSweepQueue();
  }

  /**
   * Request raw sweep data for client-side rendering.
   * Optionally target a specific open file by its server-assigned id.
   *
   * Each request is an Op (see requestScheduler) waiting in a priority queue: at most
   * {@link SWEEP_MAX_CONCURRENT} are on the wire; visible first, then prefetch, then
   * background; FIFO within a priority. Identical outstanding requests share one reply.
   * Returns the op id.
   */
  requestSweepData(
    variable: string,
    sweep: number,
    fileId?: string,
    opts?: SweepRequestOptions,
  ): string {
    const fid = fileId ?? getActiveFile()?.id ?? '';
    const priority: OpPriority = opts?.priority ?? 'visible';
    const key = this.jobKey(variable, sweep, fid);

    // Identical request already outstanding (viewer + file_opened handler both asking for the
    // first sweep, a prefetch the user now needs...): share it; every caller is notified.
    const existing = this.sweepJobs.get(key);
    if (existing) {
      if (PRIORITY_RANK[priority] < PRIORITY_RANK[existing.priority]) {
        existing.priority = priority;
        setOpPriority(existing.handle.id, priority);
      }
      if (priority !== 'prefetch') existing.broadcast = true;
      if (existing.group !== opts?.group) existing.group = undefined; // shared: no single owner any more
      if (opts?.onSettled) existing.waiters.push(opts.onSettled);
      if (existing.state === 'queued') this.drainSweepQueue();
      return existing.handle.id;
    }

    this.clearFailedOp(key, true);

    const group = opts?.group;
    const job: SweepJob = {
      key,
      seq: ++this.sweepSeq,
      variable,
      sweep,
      fileId: fid,
      priority,
      group,
      broadcast: priority !== 'prefetch',
      state: 'queued',
      handle: undefined as unknown as OpHandle,
      waiters: opts?.onSettled ? [opts.onSettled] : [],
    };
    job.handle = beginOp({
      kind: 'sweep',
      label: `Loading ${variable} sweep ${sweep}`,
      priority,
      queued: true,
      slowMs: SWEEP_SLOW_MS,
      timeoutMs: SWEEP_TIMEOUT_MS,
      onCancel: () => this.onJobCancelled(job),
      onTimeout: () => this.onJobTimeout(job),
      onRetry: () => {
        // The original owner is gone; retrying is the user asking for this sweep. A request that
        // targeted "the active file" re-resolves it (the file may have been re-opened since).
        this.requestSweepData(variable, sweep, fileId, { priority, group });
      },
    });
    this.sweepJobs.set(key, job);
    this.drainSweepQueue();
    return job.handle.id;
  }

  /** Start as many queued requests as the concurrency limit allows. */
  private drainSweepQueue(): void {
    if (this.drainSuspended > 0) return;
    for (;;) {
      const inflight = this.sweepInflight.length;
      if (inflight >= this.maxConcurrentSweeps) return;
      // Bulk / prefetch work may not take the last slot: a visible request must never wait for it.
      const lowerCap = this.maxConcurrentSweeps > 1 ? this.maxConcurrentSweeps - 1 : 1;

      let next: SweepJob | null = null;
      for (const j of this.sweepJobs.values()) {
        if (j.state !== 'queued') continue;
        // One request per variable:sweep at a time: replies carry no file id, so they could not be told apart.
        if (this.sweepInflight.some((f) => f.variable === j.variable && f.sweep === j.sweep)) continue;
        if (PRIORITY_RANK[j.priority] > 0 && inflight >= lowerCap) continue;
        if (
          !next ||
          PRIORITY_RANK[j.priority] < PRIORITY_RANK[next.priority] ||
          (j.priority === next.priority && j.seq < next.seq)
        ) {
          next = j;
        }
      }
      if (!next) return;

      const rs = this.ws?.readyState;
      if (rs !== WebSocket.OPEN && rs !== WebSocket.CONNECTING) {
        this.failJob(next, 'Not connected to the server', 'error');
        continue;
      }
      next.state = 'inflight';
      this.sweepInflight.push(next);
      next.handle.start();
      this.sendSweepRequest(next.variable, next.sweep, next.fileId || undefined);
    }
  }

  private sendSweepRequest(variable: string, sweep: number, fileId?: string): void {
    const msg: Record<string, any> = { type: 'get_sweep_data', variable, sweep };
    if (fileId) msg.file_id = fileId;
    void this.send(msg);
  }

  /** Take a request out of the queue / wire bookkeeping (idempotent). */
  private retireJob(job: SweepJob): void {
    if (job.state === 'retired') return;
    const wasInflight = job.state === 'inflight';
    job.state = 'retired';
    if (this.sweepJobs.get(job.key) === job) this.sweepJobs.delete(job.key);
    if (wasInflight) {
      const i = this.sweepInflight.indexOf(job);
      if (i >= 0) this.sweepInflight.splice(i, 1);
    }
  }

  private addOrphan(job: SweepJob): void {
    const k = `${job.variable}:${job.sweep}`;
    const now = performance.now();
    const list = (this.orphans.get(k) ?? []).filter((o) => now - o.t < ORPHAN_TTL_MS);
    list.push({ fileId: job.fileId, t: now });
    this.orphans.set(k, list);
  }

  private takeOrphan(key: string): { fileId: string; t: number } | undefined {
    const list = this.orphans.get(key);
    if (!list) return undefined;
    const now = performance.now();
    while (list.length > 0) {
      const o = list.shift()!;
      if (now - o.t < ORPHAN_TTL_MS) {
        if (list.length === 0) this.orphans.delete(key);
        return o;
      }
    }
    this.orphans.delete(key);
    return undefined;
  }

  private settleWaiters(
    job: SweepJob,
    result: 'done' | 'failed' | 'cancelled',
    info?: { reason?: SweepFailReason; message?: string },
  ): void {
    const waiters = job.waiters.splice(0);
    for (const w of waiters) {
      try { w(result, info); } catch (err) { console.error('[WS] sweep onSettled handler error:', err); }
    }
  }

  private clearFailedOp(key: string, dismiss = false): void {
    const id = this.failedSweepOps.get(key);
    if (id === undefined) return;
    this.failedSweepOps.delete(key);
    if (dismiss) dismissOp(id);
  }

  private emitSweepFailed(job: SweepJob, reason: SweepFailReason, message: string): void {
    this._dispatchHandlers('sweep_request_failed', {
      variable: job.variable,
      sweep: job.sweep,
      fileId: job.fileId,
      reason,
      message,
      opId: job.handle.id,
    });
  }

  /** The request was cancelled (user, stale-pruning, ...): free its slot. */
  private onJobCancelled(job: SweepJob): void {
    if (job.state === 'retired') return;
    if (job.state === 'inflight') this.addOrphan(job); // the reply may still come: cache it, don't repaint
    this.retireJob(job);
    this.settleWaiters(job, 'cancelled');
    this.drainSweepQueue();
  }

  /** The scheduler's deadline fired (op already marked failed): free the slot, never wedge the queue. */
  private onJobTimeout(job: SweepJob): void {
    if (job.state === 'retired') return;
    this.addOrphan(job);
    this.retireJob(job);
    this.failedSweepOps.set(job.key, job.handle.id);
    const message = `Timed out after ${Math.round(SWEEP_TIMEOUT_MS / 1000)} s`;
    this.settleWaiters(job, 'failed', { reason: 'timeout', message });
    this.emitSweepFailed(job, 'timeout', message);
    this.drainSweepQueue();
  }

  /** Mark a request failed, release its slot and start the next one. */
  private failJob(job: SweepJob, message: string, reason: SweepFailReason): void {
    if (job.state === 'retired') return;
    this.retireJob(job);
    if (reason === 'unavailable') {
      this.unavailableSweeps.add(`${job.fileId}|${job.variable}:${job.sweep}`);
      job.handle.fail(message);
      dismissOp(job.handle.id); // expected on split-cut volumes: no failed row to clutter the tray
    } else {
      job.handle.fail(message);
      this.failedSweepOps.set(job.key, job.handle.id);
    }
    this.settleWaiters(job, 'failed', { reason, message });
    this.emitSweepFailed(job, reason, message);
    this.drainSweepQueue();
  }

  /** Fail everything outstanding (connection dropped). Retry works once the socket is back. */
  private failAllSweepJobs(message: string): void {
    const jobs = [...this.sweepJobs.values()];
    if (jobs.length === 0) return;
    this.drainSuspended++;
    try {
      for (const j of jobs) this.failJob(j, message, 'error');
    } finally {
      this.drainSuspended--;
    }
  }

  /**
   * An `error` message arrived. If it answers a sweep request, fail that request.
   * Returns 'quiet' for expected "variable not in sweep" errors, 'handled' for other sweep errors
   * (still toasted) and 'other' for everything else.
   */
  private handleSweepError(text: string): 'quiet' | 'handled' | 'other' {
    const m = /Variable '([^']+)' not in sweep (\d+)/.exec(text);
    if (m) {
      const variable = m[1];
      const sweep = parseInt(m[2], 10);
      const job = this.sweepInflight.find((j) => j.variable === variable && j.sweep === sweep);
      const short = `${variable} is not available in sweep ${sweep}`;
      if (job) this.failJob(job, short, 'unavailable');
      else this.takeOrphan(`${variable}:${sweep}`);
      return 'quiet';
    }
    if (/get_sweep_data/.test(text) && this.sweepInflight.length > 0) {
      // Errors that name the request but not the sweep: the oldest request on the wire is the best guess.
      this.failJob(this.sweepInflight[0], text, 'error');
      return 'handled';
    }
    return 'other';
  }

  /**
   * Open a file on the server (multi-file aware).
   * Stores the pending path so the file_opened handler can build a FileEntry.
   */
  openFile(path: string): void {
    this._pendingOpenPath = path;
    this.send({ type: 'open_file', path });
  }

  /**
   * Close a file on the server and release its resources.
   */
  closeFile(fileId: string): void {
    this.send({ type: 'close_file', file_id: fileId });
  }

  /**
   * Send a JSON message to the server.
   */
  async send(message: Record<string, any>): Promise<void> {
    if (this.ws && this.ws.readyState === WebSocket.CONNECTING) {
      // Not open yet: hold the message and flush it from onopen (bounded queue).
      if (this.sendQueue.length >= MAX_QUEUED_MESSAGES) this.sendQueue.shift();
      this.sendQueue.push(message);
      return;
    }
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      console.warn('[WS] Cannot send, not connected. Message type:', message.type);
      return;
    }

    const payloadMessage = { ...message };
    // Once the token lookup has settled, stay synchronous so messages keep call order.
    const token = this.authResolved ? this.authToken : await this.getAuthToken();
    if (token) {
      payloadMessage.token = token;
    }

    // Re-check after async gap — connection may have changed
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      console.warn('[WS] WebSocket closed while preparing send. Message type:', message.type);
      return;
    }

    const activeFile = getActiveFile();
    const needsActiveFile =
      activeFile &&
      !payloadMessage.file_id &&
      !payloadMessage.file_id_1 &&
      !payloadMessage.file_ids &&
      payloadMessage.type !== 'open_file' &&
      payloadMessage.type !== 'close_file' &&
      payloadMessage.type !== 'ping' &&
      payloadMessage.type !== 'get_time_series' &&
      payloadMessage.type !== 'get_accumulation';
    if (needsActiveFile) {
      payloadMessage.file_id = activeFile.id;
    }

    const payload = JSON.stringify(payloadMessage);
    try {
      this.ws.send(payload);
    } catch (err) {
      console.error('[WS] send() failed for type', message.type, ':', err);
    }
  }

  private flushSendQueue(): void {
    if (this.sendQueue.length === 0) return;
    const queued = this.sendQueue.splice(0);
    for (const msg of queued) void this.send(msg);
  }

  /**
   * Disconnect from the server and stop reconnection attempts.
   */
  disconnect(): void {
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    this.retryCount = this.maxRetries; // Prevent reconnects

    if (this.ws) {
      // Detach every handler so nothing fires after an intentional close.
      this.ws.onopen = null;
      this.ws.onclose = null;
      this.ws.onmessage = null;
      this.ws.onerror = null;
      try { this.ws.close(); } catch { /* ignore */ }
      this.ws = null;
    }
    this.resetConnectionState();

    if (this.progressClearTimer) { clearTimeout(this.progressClearTimer); this.progressClearTimer = null; }
    if (this.exportClearTimer) { clearTimeout(this.exportClearTimer); this.exportClearTimer = null; }

    connectionStatus.set('disconnected');
  }

  /**
   * Whether the WebSocket is currently connected.
   */
  get isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }
}

// Export a singleton instance
export const wsManager = new WebSocketManager();

// Auto-connect: discover sidecar port and connect.
//
// Primary strategy: call the Rust `get_sidecar_port` command via Tauri invoke.
// This directly asks the Rust backend for the port — no file polling, no event
// timing, no race conditions. Falls back to file polling for non-Tauri contexts.
(function autoConnect() {
  let attempts = 0;

  async function tryTauriInvoke(): Promise<number | null> {
    try {
      const { invoke } = await import('@tauri-apps/api/core');
      const port = await invoke<number>('get_sidecar_port');
      return port > 0 ? port : null;
    } catch {
      return null;
    }
  }

  async function tryPortFile(): Promise<number | null> {
    try {
      const r = await fetch('/.sidecar-port');
      if (!r.ok) return null;
      const text = await r.text();
      const port = parseInt(text.trim(), 10);
      return port > 0 && port < 65536 ? port : null;
    } catch {
      return null;
    }
  }

  async function tryOnce() {
    if (wsManager.isConnected) return; // done
    attempts++;

    // Strategy 1: Tauri invoke (most reliable inside Tauri)
    let port = await tryTauriInvoke();

    // Strategy 2: check global set by bootstrap script
    if (!port) {
      const globalPort = (window as any).__XRADAR_PORT__;
      if (typeof globalPort === 'number' && globalPort > 0) {
        port = globalPort;
      }
    }

    // Strategy 3: port file fallback (for non-Tauri or edge cases)
    if (!port) {
      port = await tryPortFile();
    }

    if (port && !wsManager.isConnected) {
      console.log(`[WS] autoConnect: discovered port ${port} (attempt ${attempts})`);
      (window as any).__XRADAR_PORT__ = port;
      wsManager.connect(port);
    }

    // Keep polling until connected (up to 120 attempts = ~60 seconds)
    if (!wsManager.isConnected && attempts < 120) {
      setTimeout(tryOnce, 500);
    }
  }

  // Also listen for the Tauri event as a belt-and-suspenders approach
  (async () => {
    try {
      const { listen } = await import('@tauri-apps/api/event');
      const unlisten = await listen<number>('sidecar-ready', (event) => {
        const port = event.payload;
        if (port > 0 && !wsManager.isConnected) {
          console.log(`[WS] autoConnect via Tauri event: port ${port}`);
          (window as any).__XRADAR_PORT__ = port;
          wsManager.connect(port);
        }
        unlisten();
      });
    } catch {
      // Not running inside Tauri — ignore
    }
  })();

  // Start polling after a short delay to let the page settle
  setTimeout(tryOnce, 300);
})();
