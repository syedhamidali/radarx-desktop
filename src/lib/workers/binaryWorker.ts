/**
 * Web Worker: binary sweep data reassembly.
 *
 * NOTE: websocket.ts no longer routes sweeps through this worker. Reassembly is
 * a single memcpy per frame into a preallocated buffer, which is cheaper on the
 * main thread than a postMessage round trip, and a worker with a FIFO result
 * queue could desync after an error. The worker is kept as a self-contained
 * utility for callers that want to offload very large payloads; it echoes the
 * caller-supplied `tag` so results can be matched without relying on order.
 *
 * Receives chunked ArrayBuffer data, combines it into ONE contiguous buffer and
 * returns [azimuth | range | values] as zero-copy views over that single buffer,
 * transferring the buffer back (no per-array copies).
 */

export interface BinaryWorkerInput {
  /** Opaque value echoed back in the output (request correlation). */
  tag?: unknown;
  /** Raw binary chunks from the WebSocket */
  chunks: ArrayBuffer[];
  /** Byte length of the azimuth array segment */
  azimuthBytes: number;
  /** Byte length of the range array segment */
  rangeBytes: number;
  /** Metadata forwarded from the sweep_data JSON header */
  header: {
    variable: string;
    sweep: number;
    n_az: number;
    n_range: number;
    max_range: number;
    vmin: number;
    vmax: number;
    units: string;
  };
}

export interface BinaryWorkerOutput {
  tag?: unknown;
  azimuth: Float32Array;
  range: Float32Array;
  values: Float32Array;
  header: BinaryWorkerInput['header'];
  reassemblyTimeMs: number;
}

self.onmessage = (e: MessageEvent<BinaryWorkerInput>) => {
  const t0 = performance.now();
  const { chunks, azimuthBytes, rangeBytes, header, tag } = e.data;

  // A single chunk can be used as-is (no copy); otherwise concatenate once.
  let buf: ArrayBuffer;
  if (chunks.length === 1) {
    buf = chunks[0];
  } else {
    let totalBytes = 0;
    for (const chunk of chunks) totalBytes += chunk.byteLength;
    const combined = new Uint8Array(totalBytes);
    let offset = 0;
    for (const chunk of chunks) {
      combined.set(new Uint8Array(chunk), offset);
      offset += chunk.byteLength;
    }
    buf = combined.buffer;
  }

  // Views over the one buffer; transferring it hands all three to the main thread.
  const azimuth = new Float32Array(buf, 0, azimuthBytes / 4);
  const range = new Float32Array(buf, azimuthBytes, rangeBytes / 4);
  const values = new Float32Array(buf, azimuthBytes + rangeBytes, Math.floor((buf.byteLength - azimuthBytes - rangeBytes) / 4));

  const output: BinaryWorkerOutput = {
    tag,
    azimuth,
    range,
    values,
    header,
    reassemblyTimeMs: performance.now() - t0,
  };

  (self as unknown as Worker).postMessage(output, [buf]);
};
