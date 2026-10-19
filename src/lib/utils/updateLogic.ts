/**
 * Pure rules of the self-updater: version comparison, the 24 h throttle, skip-this-version, "is a
 * job running", error wording, and a safe release-notes renderer. No Tauri, no DOM, no stores, so
 * all of it is unit-tested (updateLogic.test.mjs). The service that uses it is updater.ts.
 */

// --- versions ----------------------------------------------------------------------------------

interface Semver {
  core: [number, number, number];
  pre: (string | number)[];
}

/** Parse "1.2.3", "v1.2.3", "1.2.3-rc.1", "1.2.3+build". Null when it is not a version. */
export function parseVersion(raw: unknown): Semver | null {
  if (typeof raw !== 'string') return null;
  const m = /^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$/.exec(raw.trim());
  if (!m) return null;
  const pre = m[4] ? m[4].split('.').map((p) => (/^\d+$/.test(p) ? Number(p) : p)) : [];
  return { core: [Number(m[1]), Number(m[2]), Number(m[3])], pre };
}

/** -1 / 0 / 1 by semver precedence (a pre-release is older than its release). Null if either is invalid. */
export function compareVersions(a: string, b: string): -1 | 0 | 1 | null {
  const x = parseVersion(a);
  const y = parseVersion(b);
  if (!x || !y) return null;
  for (let i = 0; i < 3; i++) {
    if (x.core[i] !== y.core[i]) return x.core[i] < y.core[i] ? -1 : 1;
  }
  if (x.pre.length === 0 && y.pre.length === 0) return 0;
  if (x.pre.length === 0) return 1;
  if (y.pre.length === 0) return -1;
  for (let i = 0; i < Math.max(x.pre.length, y.pre.length); i++) {
    const p = x.pre[i];
    const q = y.pre[i];
    if (p === undefined) return -1;
    if (q === undefined) return 1;
    if (p === q) continue;
    if (typeof p === 'number' && typeof q === 'number') return p < q ? -1 : 1;
    if (typeof p === 'number') return -1; // numeric identifiers sort before alphanumeric ones
    if (typeof q === 'number') return 1;
    return p < q ? -1 : 1;
  }
  return 0;
}

/** True only for a valid version that is strictly newer than the running one. */
export function isNewer(current: string, candidate: string): boolean {
  return compareVersions(current, candidate) === -1;
}

/** "v1.2.3" -> "1.2.3" (the form stored and compared). */
export function normalizeVersion(v: string): string {
  return String(v ?? '').trim().replace(/^v/i, '');
}

// --- skip, throttle ----------------------------------------------------------------------------

/** Skipping a version hides exactly that version; a newer one is offered again. */
export function isSkipped(skippedVersion: string, version: string): boolean {
  const s = normalizeVersion(skippedVersion);
  return s !== '' && s === normalizeVersion(version);
}

export const CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000;

/**
 * Should the app check by itself now? At most once per interval; never when switched off. A stamp
 * in the future (the clock was set back) is not trusted, so it can never block checks for good.
 */
export function autoCheckDue(o: {
  enabled: boolean;
  lastCheckedAt: number;
  now: number;
  intervalMs?: number;
}): boolean {
  if (!o.enabled) return false;
  const last = o.lastCheckedAt;
  if (!Number.isFinite(last) || last <= 0) return true;
  if (last > o.now + 60_000) return true;
  return o.now - last >= (o.intervalMs ?? CHECK_INTERVAL_MS);
}

// --- never interrupt a job ---------------------------------------------------------------------

export interface BusyInputs {
  operations: { kind: string; state: string }[];
  pipelineBusy: string | null;
  mosaicRunning: boolean;
  windRunning: boolean;
  /** Number of modules currently running. */
  modulesRunning: number;
}

/** Operation kinds that are real jobs. Sweep loads (display, prefetch, animation) are not. */
const JOB_KINDS: Record<string, string> = {
  export: 'an export',
  module: 'a module run',
  mosaic: 'a mosaic',
  wind: 'a wind retrieval',
  open: 'a file open',
  upload: 'a file upload',
  progress: 'a processing job',
};
const ACTIVE_STATES = ['queued', 'running', 'slow'];

/** Is a job running, and which one (for the "waiting for ..." message)? */
export function busyState(i: BusyInputs): { busy: boolean; reason: string | null } {
  const reasons: string[] = [];
  const ops = new Set<string>();
  for (const o of i.operations) {
    if (ACTIVE_STATES.includes(o.state) && JOB_KINDS[o.kind]) ops.add(o.kind);
  }
  if (ops.has('export')) reasons.push(JOB_KINDS.export);
  if (i.pipelineBusy) reasons.push('a pipeline');
  if (i.modulesRunning > 0 || ops.has('module')) reasons.push(JOB_KINDS.module);
  if (i.mosaicRunning || ops.has('mosaic')) reasons.push(JOB_KINDS.mosaic);
  if (i.windRunning || ops.has('wind')) reasons.push(JOB_KINDS.wind);
  for (const k of ['open', 'upload', 'progress']) if (ops.has(k)) reasons.push(JOB_KINDS[k]);
  return { busy: reasons.length > 0, reason: reasons.length ? reasons[0] : null };
}

/**
 * What to do about the restart prompt: it is shown only when the update is downloaded, somebody
 * wants it (`promptPending`), and nothing is running. While a job runs it waits.
 */
export function restartGate(o: {
  ready: boolean;
  promptPending: boolean;
  busy: boolean;
}): 'show' | 'wait' | 'none' {
  if (!o.ready || !o.promptPending) return 'none';
  return o.busy ? 'wait' : 'show';
}

// --- errors ------------------------------------------------------------------------------------

export type ErrorKind = 'timeout' | 'offline' | 'unavailable' | 'no-build' | 'signature' | 'other';

export type ErrorContext = 'check' | 'download' | 'install';

/** Turn whatever the updater threw into a short, honest, human sentence. */
export function describeUpdateError(
  err: unknown,
  ctx: ErrorContext = 'check',
): { kind: ErrorKind; message: string } {
  const raw = typeof err === 'string' ? err : err instanceof Error ? err.message : JSON.stringify(err ?? '');
  const t = String(raw).toLowerCase();
  const verb = ctx === 'check' ? 'check' : ctx === 'download' ? 'download' : 'install';
  const lead = ctx === 'check' ? 'Could not check' : `Could not ${verb} the update`;
  if (t === '__timeout__' || /timed out|timeout|deadline/.test(t)) {
    return { kind: 'timeout', message: `${lead}: the server did not answer in time.` };
  }
  if (/signature|minisign/.test(t)) {
    return {
      kind: 'signature',
      message: 'The update was rejected because its signature could not be verified. Nothing was installed.',
    };
  }
  if (/fallback platforms|platform .* was not found|targets? not found/.test(t)) {
    return { kind: 'no-build', message: 'The latest release has no build for this system.' };
  }
  if (/error sending request|dns|connect|network|offline|unreachable|resolve|tls|ssl|certificate|broken pipe|reset by peer/.test(t)) {
    return { kind: 'offline', message: `${lead}: the update server could not be reached.` };
  }
  if (/\b(404|403|410|5\d\d)\b|status code|not found|valid release json|error decoding response|expected value|invalid (json|response)/.test(t)) {
    return { kind: 'unavailable', message: `${lead}: the update information is not available right now.` };
  }
  const short = String(raw).replace(/\s+/g, ' ').trim().slice(0, 140);
  return { kind: 'other', message: short ? `${lead}: ${short}` : `${lead}.` };
}

// --- numbers -----------------------------------------------------------------------------------

export function progressPercent(received: number, total: number | null | undefined): number | null {
  if (!total || total <= 0 || !Number.isFinite(received)) return null;
  return Math.max(0, Math.min(100, Math.round((received / total) * 100)));
}

export function formatBytes(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n) || n < 0) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

// --- release notes: markdown to a safe tree ----------------------------------------------------
//
// The release body comes from the network, so it is untrusted. It is never turned into HTML: it is
// parsed into a small tree of text nodes (headings, paragraphs, lists, code, bold, italic, links)
// that Svelte renders as text. Raw HTML in the notes stays visible as plain text. A link survives
// only if it is http(s); images become their alt text.

export type Inline =
  | { t: 'text'; s: string }
  | { t: 'code'; s: string }
  | { t: 'strong'; c: Inline[] }
  | { t: 'em'; c: Inline[] }
  | { t: 'link'; href: string; c: Inline[] };

export type Block =
  | { t: 'h'; level: 1 | 2 | 3; c: Inline[] }
  | { t: 'p'; c: Inline[] }
  | { t: 'ul'; items: Inline[][] }
  | { t: 'ol'; items: Inline[][] }
  | { t: 'code'; s: string }
  | { t: 'hr' };

const MAX_NOTES_CHARS = 20_000;
const MAX_BLOCKS = 400;
const MAX_DEPTH = 4;

/** http(s) URLs only (normalised); everything else (javascript:, data:, file:, ...) gives null. */
export function safeHref(raw: string): string | null {
  const s = String(raw ?? '').trim();
  if (!s || s.length > 2048) return null;
  try {
    const u = new URL(s);
    return u.protocol === 'https:' || u.protocol === 'http:' ? u.href : null;
  } catch {
    return null;
  }
}

function parseInline(src: string, depth = 0): Inline[] {
  const out: Inline[] = [];
  let buf = '';
  const flush = () => {
    if (buf) {
      out.push({ t: 'text', s: buf });
      buf = '';
    }
  };
  let i = 0;
  while (i < src.length) {
    const ch = src[i];
    if (ch === '\\' && i + 1 < src.length && /[\\`*_{}[\]()#+\-.!<>]/.test(src[i + 1])) {
      buf += src[i + 1];
      i += 2;
      continue;
    }
    if (ch === '`') {
      const j = src.indexOf('`', i + 1);
      if (j > i + 1) {
        flush();
        out.push({ t: 'code', s: src.slice(i + 1, j) });
        i = j + 1;
        continue;
      }
    }
    if (ch === '[' || (ch === '!' && src[i + 1] === '[')) {
      const image = ch === '!';
      const from = image ? i + 1 : i;
      const m = /^\[([^\]]*)\]\(\s*<?([^)\s>]*)>?(?:\s+"[^"]*")?\s*\)/.exec(src.slice(from));
      if (m) {
        flush();
        const inner = depth < MAX_DEPTH ? parseInline(m[1], depth + 1) : [{ t: 'text', s: m[1] } as Inline];
        const href = image ? null : safeHref(m[2]);
        if (href && inner.length) out.push({ t: 'link', href, c: inner });
        else out.push(...inner);
        i = from + m[0].length;
        continue;
      }
    }
    if ((ch === '*' || ch === '_') && src[i + 1] === ch) {
      const d = ch + ch;
      const j = src.indexOf(d, i + 2);
      if (j > i + 2 && depth < MAX_DEPTH) {
        flush();
        out.push({ t: 'strong', c: parseInline(src.slice(i + 2, j), depth + 1) });
        i = j + 2;
        continue;
      }
    }
    if (ch === '*' || (ch === '_' && (i === 0 || !/[A-Za-z0-9]/.test(src[i - 1])))) {
      const j = src.indexOf(ch, i + 1);
      const closes = j > i + 1 && (ch === '*' || !/[A-Za-z0-9]/.test(src[j + 1] ?? ' '));
      if (closes && src[i + 1] !== ' ' && depth < MAX_DEPTH) {
        flush();
        out.push({ t: 'em', c: parseInline(src.slice(i + 1, j), depth + 1) });
        i = j + 1;
        continue;
      }
    }
    buf += ch;
    i++;
  }
  flush();
  return out;
}

/** Parse release-notes markdown into a safe tree. Always returns; never throws. */
export function parseReleaseNotes(markdown: unknown): Block[] {
  if (typeof markdown !== 'string') return [];
  const text = markdown.replace(/\r\n?/g, '\n').slice(0, MAX_NOTES_CHARS);
  const lines = text.split('\n');
  const blocks: Block[] = [];
  let para: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  const endPara = () => {
    if (para.length) blocks.push({ t: 'p', c: parseInline(para.join(' ')) });
    para = [];
  };
  const endList = () => {
    if (list) {
      const items = list.items.map((s) => parseInline(s));
      blocks.push(list.ordered ? { t: 'ol', items } : { t: 'ul', items });
    }
    list = null;
  };
  for (let n = 0; n < lines.length && blocks.length < MAX_BLOCKS; n++) {
    const line = lines[n];
    const fence = /^\s*(```|~~~)/.exec(line);
    if (fence) {
      endPara();
      endList();
      const code: string[] = [];
      n++;
      while (n < lines.length && !new RegExp(`^\\s*${fence[1]}`).test(lines[n])) code.push(lines[n++]);
      blocks.push({ t: 'code', s: code.join('\n') });
      continue;
    }
    if (/^\s*$/.test(line)) {
      endPara();
      endList();
      continue;
    }
    const h = /^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line);
    if (h) {
      endPara();
      endList();
      const level = Math.min(3, h[1].length) as 1 | 2 | 3;
      blocks.push({ t: 'h', level, c: parseInline(h[2]) });
      continue;
    }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      endPara();
      endList();
      blocks.push({ t: 'hr' });
      continue;
    }
    const li = /^\s*(?:([-*+])|(\d+)[.)])\s+(.*)$/.exec(line);
    if (li) {
      endPara();
      const ordered = li[2] !== undefined;
      if (list && list.ordered !== ordered) endList();
      if (!list) list = { ordered, items: [] };
      list.items.push(li[3]);
      continue;
    }
    if (list && /^\s{2,}\S/.test(line)) {
      // A wrapped list item continues on the next, indented line.
      list.items[list.items.length - 1] += ' ' + line.trim();
      continue;
    }
    endList();
    para.push(line.replace(/^\s*>\s?/, '').trim());
  }
  endPara();
  endList();
  return blocks.slice(0, MAX_BLOCKS);
}
