/**
 * Browser -> engine file uploads (web mode), over the same WebSocket as everything else.
 *
 * Protocol (JSON text frames):
 *   client  upload_start  {upload_id, filename, size}      -> server upload_ready {upload_id}
 *                                                            | upload_error {upload_id, message}
 *   client  upload_chunk  {upload_id, seq, data: base64}   -> server upload_progress {upload_id, received}
 *           (<= 1 MiB raw bytes, one at a time: the next chunk waits for the progress reply)
 *   client  upload_end    {upload_id}                      -> server upload_complete {upload_id, path}
 *
 * The first half of this file is pure (no DOM, no stores): size checks, base64, chunking and the
 * transport-agnostic {@link runUpload} state machine, so it can be tested with a fake transport.
 * The second half ({@link uploadFiles}) wires it to the real socket, the operations tray and toasts.
 */
import { get } from 'svelte/store';
import { wsManager } from './websocket';
import { beginOp, type OpHandle } from './requestScheduler';
import { addToast } from '../stores/toasts';
import { currentLimits } from '../stores/webInfo';
import { openFiles } from '../stores/fileManager';

// ── Pure helpers ────────────────────────────────────────────────────────────

/** Raw bytes per chunk (contract: at most 1 MiB). */
export const CHUNK_BYTES = 1024 * 1024;
const MIB = 1024 * 1024;

export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return '?';
  if (n < 1024) return `${n} B`;
  if (n < MIB) return `${(n / 1024).toFixed(0)} KB`;
  if (n < 1024 * MIB) return `${(n / MIB).toFixed(1)} MB`;
  return `${(n / (1024 * MIB)).toFixed(2)} GB`;
}

export interface SizeCheck {
  ok: boolean;
  /** Friendly explanation when not ok. */
  message?: string;
}

/** Client-side upload limit check (`maxMb` 0 / unknown = no client-side limit). */
export function checkUploadSize(name: string, size: number, maxMb: number): SizeCheck {
  if (!Number.isFinite(size) || size <= 0) {
    return { ok: false, message: `${name} is empty, so there is nothing to upload.` };
  }
  if (maxMb > 0 && size > maxMb * MIB) {
    return {
      ok: false,
      message:
        `${name} is ${formatBytes(size)}, but this server accepts files up to ${maxMb} MB. ` +
        `Try a smaller file, or open a scan from the cloud instead.`,
    };
  }
  return { ok: true };
}

/** Number of chunks needed for `size` bytes. */
export function chunkCount(size: number, chunkBytes = CHUNK_BYTES): number {
  return size <= 0 ? 0 : Math.ceil(size / chunkBytes);
}

/** Byte range [start, end) of chunk `seq`. */
export function chunkRange(seq: number, size: number, chunkBytes = CHUNK_BYTES): [number, number] {
  const start = seq * chunkBytes;
  return [start, Math.min(size, start + chunkBytes)];
}

/** Base64 of raw bytes (works for large buffers: no argument-count blowup). */
export function bytesToBase64(bytes: Uint8Array): string {
  let bin = '';
  const STEP = 0x8000;
  for (let i = 0; i < bytes.length; i += STEP) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + STEP) as unknown as number[]);
  }
  return btoa(bin);
}

export function makeUploadId(): string {
  const c = (globalThis as any).crypto;
  if (c?.randomUUID) return c.randomUUID();
  return `u${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
}

/** Readable text for the operations tray. */
export function progressText(sent: number, total: number): string {
  const pct = total > 0 ? Math.min(100, Math.floor((sent / total) * 100)) : 0;
  return `${pct}% (${formatBytes(sent)} of ${formatBytes(total)})`;
}

// ── Transport-agnostic upload ───────────────────────────────────────────────

/** The slice of the socket {@link runUpload} needs. */
export interface UploadTransport {
  send(msg: Record<string, any>): Promise<void> | void;
  /** Subscribe to server messages of one type; returns the unsubscribe function. */
  on(type: string, handler: (msg: any) => void): () => void;
  /** Optional: called when the connection drops mid-upload. */
  onDisconnect?(handler: () => void): () => void;
}

/** The slice of a `File` {@link runUpload} needs. */
export interface UploadSource {
  name: string;
  size: number;
  slice(start: number, end: number): { arrayBuffer(): Promise<ArrayBuffer> };
}

export interface RunUploadOptions {
  signal?: AbortSignal;
  /** Called with (bytes the server has received, total bytes). */
  onProgress?: (received: number, total: number) => void;
  uploadId?: string;
  chunkBytes?: number;
  /** Max wait for any single server reply. Default 60 s. */
  stepTimeoutMs?: number;
}

export class UploadError extends Error {
  constructor(message: string, readonly cancelled = false) {
    super(message);
    this.name = 'UploadError';
  }
}

const REPLY_TYPES = ['upload_ready', 'upload_progress', 'upload_complete', 'upload_error'] as const;

/**
 * Upload one file. Resolves with the server-side path from `upload_complete`.
 * Rejects with {@link UploadError} (message is user-presentable; `cancelled` when aborted).
 */
export async function runUpload(
  file: UploadSource,
  transport: UploadTransport,
  opts: RunUploadOptions = {},
): Promise<string> {
  const uploadId = opts.uploadId ?? makeUploadId();
  const chunkBytes = opts.chunkBytes ?? CHUNK_BYTES;
  const stepTimeout = opts.stepTimeoutMs ?? 60_000;
  const { signal } = opts;
  const total = file.size;

  // Mailbox: replies are queued so one that arrives before we await it is never lost.
  const inbox: Array<{ type: string; msg: any }> = [];
  let wake: (() => void) | null = null;
  let fatal: UploadError | null = null;
  const poke = () => { const w = wake; wake = null; w?.(); };
  const unsubs: Array<() => void> = REPLY_TYPES.map((t) =>
    transport.on(t, (msg) => {
      if (msg?.upload_id !== uploadId) return;
      inbox.push({ type: t, msg });
      poke();
    }),
  );
  if (transport.onDisconnect) {
    unsubs.push(transport.onDisconnect(() => { fatal ??= new UploadError('Connection to the engine was lost during the upload.'); poke(); }));
  }
  const onAbort = () => { fatal ??= new UploadError('Cancelled', true); poke(); };
  if (signal) {
    if (signal.aborted) onAbort();
    else signal.addEventListener('abort', onAbort, { once: true });
  }

  /** Wait for the next reply of type `want`; an `upload_error` (or fatal condition) rejects. */
  const expect = async (want: (typeof REPLY_TYPES)[number]): Promise<any> => {
    const deadline = Date.now() + stepTimeout;
    for (;;) {
      if (fatal) throw fatal;
      const m = inbox.shift();
      if (m) {
        if (m.type === 'upload_error') {
          throw new UploadError(String(m.msg.message ?? 'The server rejected the upload.'));
        }
        if (m.type === want) return m.msg;
        continue; // stray reply of another type (e.g. a duplicate progress): ignore
      }
      const left = deadline - Date.now();
      if (left <= 0) throw new UploadError('The engine did not answer in time.');
      await new Promise<void>((resolve) => {
        const t = setTimeout(() => { wake = null; resolve(); }, left);
        wake = () => { clearTimeout(t); resolve(); };
      });
    }
  };

  try {
    if (fatal) throw fatal;
    await transport.send({ type: 'upload_start', upload_id: uploadId, filename: file.name, size: total });
    await expect('upload_ready');

    const n = chunkCount(total, chunkBytes);
    let sent = 0;
    for (let seq = 0; seq < n; seq++) {
      if (fatal) throw fatal;
      const [start, end] = chunkRange(seq, total, chunkBytes);
      const buf = new Uint8Array(await file.slice(start, end).arrayBuffer());
      if (fatal) throw fatal;
      await transport.send({ type: 'upload_chunk', upload_id: uploadId, seq, data: bytesToBase64(buf) });
      sent = end;
      const p = await expect('upload_progress');
      const received = typeof p.received === 'number' ? p.received : sent;
      opts.onProgress?.(received, total);
    }

    await transport.send({ type: 'upload_end', upload_id: uploadId });
    const done = await expect('upload_complete');
    if (typeof done.path !== 'string' || !done.path) throw new UploadError('The server did not return a file path.');
    return done.path;
  } finally {
    for (const u of unsubs) u();
    signal?.removeEventListener('abort', onAbort);
  }
}

// ── Wiring to the real socket, operations tray and toasts ───────────────────

export interface UploadHooks {
  /** A file arrived on the server; open it (path is the server-side path). */
  onUploaded: (path: string, filename: string) => void;
}

const socketTransport: UploadTransport = {
  async send(msg) {
    if (!wsManager.isConnected) throw new UploadError('Not connected to the engine.');
    await wsManager.send(msg);
  },
  on: (type, handler) => wsManager.onMessage(type, handler),
  onDisconnect: (handler) => wsManager.onDisconnect(handler),
};

/** Uploads run strictly one after the other (the socket carries one at a time). */
let chain: Promise<void> = Promise.resolve();

/**
 * Validate and queue files for upload. Each accepted file gets an entry in the operations tray
 * (queued -> uploading with a progress text -> done), cancellable; files over the server limit are
 * rejected here with a friendly toast. Returns how many files were queued.
 */
export function uploadFiles(files: File[] | FileList, hooks: UploadHooks): number {
  const list = Array.from(files as ArrayLike<File>);
  if (list.length === 0) return 0;
  if (!wsManager.isConnected) {
    addToast('error', 'The engine is not connected yet. Please wait a moment and try again.');
    return 0;
  }
  const limits = currentLimits();
  let queued = 0;
  let slots = limits.max_open_files > 0 ? limits.max_open_files - get(openFiles).length - pendingUploads : Infinity;

  for (const file of list) {
    const check = checkUploadSize(file.name, file.size, limits.max_upload_mb);
    if (!check.ok) { addToast('error', check.message!, 9000); continue; }
    if (slots <= 0) {
      addToast('error', `This server keeps at most ${limits.max_open_files} files open per session. Close a tab first.`, 9000);
      break;
    }
    slots--;
    queued++;
    pendingUploads++;
    const op: OpHandle = beginOp({
      kind: 'upload',
      label: `Uploading ${file.name}`,
      queued: true,
      slowMs: 0,
      timeoutMs: 0,
    });
    chain = chain.then(() => uploadOne(file, op, hooks)).catch(() => { /* reported in uploadOne */ });
  }
  return queued;
}

let pendingUploads = 0;

async function uploadOne(file: File, op: OpHandle, hooks: UploadHooks): Promise<void> {
  try {
    if (op.signal.aborted) return; // cancelled while waiting in the queue
    op.start();
    op.update(progressText(0, file.size));
    const path = await runUpload(file, socketTransport, {
      signal: op.signal,
      onProgress: (got, total) => op.update(progressText(got, total)),
    });
    op.done();
    hooks.onUploaded(path, file.name);
  } catch (e) {
    const err = e as UploadError;
    if (err.cancelled || op.signal.aborted) return; // cancelOp already finished the op
    const msg = err?.message || 'Upload failed';
    op.fail(msg);
    addToast('error', `Could not upload ${file.name}: ${msg}`, 9000);
  } finally {
    pendingUploads = Math.max(0, pendingUploads - 1);
  }
}
