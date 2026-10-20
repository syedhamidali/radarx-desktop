// Small HTTP helpers shared by the access-token functions. Plain TypeScript with only syntax Node can
// strip, so http.test.mjs runs without Deno.

export class BodyTooLarge extends Error {
  constructor() {
    super('body too large');
  }
}

/**
 * The request body as text, read from the STREAM with a byte cap: the Content-Length header is a claim
 * (missing, wrong, NaN or absent for a chunked body), so only the bytes actually read count. Throws
 * BodyTooLarge as soon as the cap is passed and stops reading; the caller answers 413.
 */
export async function readLimitedText(
  req: { body: ReadableStream<Uint8Array> | null; headers: { get(name: string): string | null } },
  maxBytes: number,
): Promise<string> {
  const claimed = Number(req.headers.get('content-length'));
  if (Number.isFinite(claimed) && claimed > maxBytes) throw new BodyTooLarge();
  if (req.body === null) return '';
  const reader = req.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > maxBytes) {
        await reader.cancel().catch(() => {});
        throw new BodyTooLarge();
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock?.();
  }
  const all = new Uint8Array(total);
  let at = 0;
  for (const c of chunks) {
    all.set(c, at);
    at += c.byteLength;
  }
  return new TextDecoder('utf-8', { fatal: true }).decode(all); // invalid UTF-8 throws: not JSON either
}

/** Parsed JSON, or null for anything that is not (callers answer with their own generic refusal). */
export function parseJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

const IPV4 = /^(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}$/;
const IPV6 = /^[0-9a-fA-F:]{2,45}$/;

/**
 * The client address, from the ONE header the platform's edge sets and overwrites itself:
 * `cf-connecting-ip` (the functions run behind Cloudflare). `x-forwarded-for` is NOT used: its first
 * entries are whatever the caller wrote. A missing or malformed value gives null, and a caller with no
 * trustworthy address must skip any per-address limit rather than share one bucket among everybody.
 * (Release gate: check on the deployed function that the header is present and that a forged one
 * sent by a caller is replaced; see docs/ACCOUNTS.md.)
 */
export function trustedAddress(headers: { get(name: string): string | null }): string | null {
  const value = (headers.get('cf-connecting-ip') ?? '').trim();
  if (!value || value.length > 45) return null;
  return IPV4.test(value) || (value.includes(':') && IPV6.test(value)) ? value : null;
}
