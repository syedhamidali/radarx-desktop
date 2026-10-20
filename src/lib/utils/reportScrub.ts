/**
 * Scrubbing of the optional diagnostics that "Report an issue" can attach (docs/REPORTING.md).
 *
 * The diagnostics are the app version, the system name, the engine version and the last lines of what the engine
 * told the app this session. Before anything is shown in the preview or sent, every line goes through
 * `scrubLine`: it removes or masks what could identify a person, a machine, an account or a file. Pure functions,
 * no DOM, no network (tested in reportScrub.test.mjs).
 *
 * What it does:
 *   - secrets: JWT-like strings, "Bearer ..." values, key=value pairs named token/password/secret/key, well-known
 *     key prefixes, long hex strings and long base64-like strings  ->  [token] / [removed] / [hex]
 *   - e-mail addresses and user@host forms  ->  [email]
 *   - IP addresses (v4 and v6)  ->  [ip]
 *   - paths: under the home folder they start with "~"; any absolute path keeps only its last two segments (and a "..."
 *     for the rest); the name of a data file is replaced by [file] (a source or log file name stays, it helps)
 *   - the user name and machine name when the caller knows them, and names ending in .local / .lan / .home ...
 *
 * What it cannot do: it does not know the machine name unless told, and free text can contain anything. That is why
 * the dialog shows exactly what will be sent before it is sent.
 */

export interface ScrubContext {
  /** The home folder, when known (Tauri: homeDir()). */
  home?: string | null;
  /** The account name on the computer (usually the last segment of home). */
  user?: string | null;
  /** The machine name, when known. */
  host?: string | null;
}

/** File names with these extensions stay in a path (they are program code or logs, useful for a fix). */
const KEEP_EXT = new Set(['py', 'pyc', 'pyd', 'js', 'mjs', 'cjs', 'ts', 'tsx', 'jsx', 'svelte', 'rs', 'so', 'dll', 'dylib', 'c', 'h', 'cpp', 'whl', 'log']);

const MAX_LINE = 300;

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function looksLikeFile(seg: string): boolean {
  return /\.[A-Za-z][A-Za-z0-9]{0,7}$/.test(seg) || (seg.match(/\d/g)?.length ?? 0) >= 6;
}

function maskFileName(seg: string): string {
  const ext = /\.([A-Za-z][A-Za-z0-9]{0,7})$/.exec(seg)?.[1];
  if (ext && KEEP_EXT.has(ext.toLowerCase())) return seg;
  if (!looksLikeFile(seg)) return seg; // a folder name
  return ext ? `[file].${ext}` : '[file]';
}

/** Shortens one absolute path (POSIX, Windows, UNC or ~) as described in the file header. Idempotent. */
export function shortenPath(path: string, ctx: ScrubContext = {}): string {
  const unc = path.startsWith('\\\\');
  const drive = /^([A-Za-z]):[\\/]/.exec(path)?.[1] ?? null;
  const trailing = /[\\/]$/.test(path);
  const full = path.split(/[\\/]+/).filter((x) => x !== '');
  const homeFull = ctx.home ? ctx.home.split(/[\\/]+/).filter((x) => x !== '') : [];
  const fold = (x: string) => (drive || /^[A-Za-z]:$/.test(homeFull[0] ?? '') ? x.toLowerCase() : x);

  let prefix = ''; // '' = filesystem root, '~' = home, 'C:' = a drive, '//[host]' = a network share
  let rest: string[];
  if (path === '~' || path.startsWith('~/')) {
    prefix = '~';
    rest = full.slice(1);
  } else if (unc) {
    prefix = '//[host]';
    rest = full.slice(1);
  } else if (homeFull.length > 0 && full.length >= homeFull.length && homeFull.every((h, i) => fold(h) === fold(full[i]))) {
    prefix = '~';
    rest = full.slice(homeFull.length);
  } else if (drive) {
    if (full[1]?.toLowerCase() === 'users' && full.length >= 3) {
      prefix = '~';
      rest = full.slice(3);
    } else {
      prefix = `${drive}:`;
      rest = full.slice(1);
    }
  } else if ((full[0] === 'Users' || full[0] === 'home') && full.length >= 2) {
    prefix = '~';
    rest = full.slice(2);
  } else if (full[0] === 'root') {
    prefix = '~';
    rest = full.slice(1);
  } else {
    rest = full;
  }

  const parts = rest.slice();
  if (!trailing && parts.length > 0) parts[parts.length - 1] = maskFileName(parts[parts.length - 1]);
  const collapsed = parts.length > 2;
  const kept = parts.slice(-2);
  const pieces = [...(collapsed ? ['...'] : []), ...kept];
  const body = pieces.join('/') + (trailing && pieces.length ? '/' : '');
  if (prefix === '') return '/' + body;
  return pieces.length ? `${prefix}/${body}` : prefix;
}

function secretMasks(line: string): string {
  let t = line;
  t = t.replace(/\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}(?:\.[A-Za-z0-9_-]*)?/g, '[token]');
  t = t.replace(/\b[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b/g, '[token]');
  t = t.replace(/\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}/gi, '$1 [removed]');
  t = t.replace(/\bsb_(?:publishable|secret)_[A-Za-z0-9_-]+/g, '[token]');
  t = t.replace(/\b(?:ghp|gho|ghu|ghs|ghr|github_pat|xox[baprs]|sk)[_-][A-Za-z0-9_-]{16,}/g, '[token]');
  t = t.replace(/\bAKIA[0-9A-Z]{16}\b/g, '[token]');
  t = t.replace(
    /\b(api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|token|secret|password|passwd|pwd|authorization|auth|key)(["']?\s*[=:]\s*)("[^"]*"|'[^']*'|[^\s,;&}]+)/gi,
    '$1$2[removed]',
  );
  t = t.replace(/([?&](?:token|key|sig|signature|access_token|apikey|code)=)[^&\s]+/gi, '$1[removed]');
  t = t.replace(/\b[0-9a-fA-F]{32,}\b/g, '[hex]');
  t = t.replace(/[A-Za-z0-9+_-]{40,}={0,2}/g, (m) => (/\d/.test(m) && /[A-Za-z]/.test(m) ? '[token]' : m));
  return t;
}

const IPV4 = /\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b/g;

function ipMasks(line: string): string {
  let t = line.replace(IPV4, '[ip]');
  // IPv6: a compressed form with "::", or the full eight groups; plain "12:30:45" times are not matched
  t = t.replace(/(?<![0-9A-Za-z:])(?:[0-9a-fA-F]{1,4}:){1,7}:(?:[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{1,4}){0,6})?(?![0-9A-Za-z:])/g, '[ip]');
  t = t.replace(/(?<![0-9A-Za-z:])::(?:[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{1,4}){0,6})(?![0-9A-Za-z:])/g, '[ip]');
  t = t.replace(/(?<![0-9A-Za-z:])(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}(?![0-9A-Za-z:])/g, '[ip]');
  return t;
}

// characters that can be part of a path in free text (a path ends at a space, a quote or a bracket)
const WIN_CHARS = "[^\\s'\"`<>|;,()\\[\\]{}*?]";
const POSIX_CHARS = "[^\\s'\"`<>|;,:()\\[\\]{}*?]";

function pathMasks(line: string, ctx: ScrubContext): string {
  let t = line;
  const short = (m: string) => trimTail(m, (p) => shortenPath(p, ctx));
  // quoted paths may contain spaces
  t = t.replace(/(["'])((?:[A-Za-z]:[\\/]|\\\\|\/|~\/)[^"'\n]*?)\1/g, (_m, q: string, p: string) => `${q}${shortenPath(p, ctx)}${q}`);
  // Windows drive paths and UNC paths
  t = t.replace(new RegExp(`(?<![A-Za-z0-9])[A-Za-z]:[\\\\/]${WIN_CHARS}*`, 'g'), short);
  t = t.replace(new RegExp(`\\\\\\\\[A-Za-z0-9._$-]+(?:\\\\${WIN_CHARS}+)*`, 'g'), short);
  // POSIX absolute paths with at least two segments, and ~/ paths
  t = t.replace(new RegExp(`(?<![\\w:/.~$%@#-])(?:/${POSIX_CHARS}+){2,}/?`, 'g'), short);
  t = t.replace(new RegExp(`(?<![\\w:/.~$%@#-])~(?:/${POSIX_CHARS}+)+/?`, 'g'), short);
  return t;
}

const DATA_EXT = 'nc|nc4|cdf|h5|hdf|hdf5|gz|bz2|xz|zip|tar|tgz|grib|grib2|grb|grb2|uf|ar2v|raw|iris|sigmet|vol|zarr|csv|tif|tiff|png|jpg|jpeg|gif|mp4|mov|webm|pdf|svg|sqlite|parquet|npz|npy|pkl';

/** File names left in free text: after a slash, with a data-file extension, or NEXRAD-style names (KTLX20130520_201643_V06). */
function fileNameMasks(line: string): string {
  let t = line.replace(/[^\s'"`<>()\[\]{},;|]+[\\/][^\s'"`<>()\[\]{},;|\\/]+/g, (tok) => {
    const i = Math.max(tok.lastIndexOf('/'), tok.lastIndexOf('\\'));
    const base = tok.slice(i + 1);
    const masked = maskFileName(base);
    return masked === base ? tok : tok.slice(0, i + 1) + masked;
  });
  t = t.replace(/\b[A-Z]{4}\d{8}_\d{4,6}(?:_V\d{2,3})?(?:_MDM)?\b/g, '[file]');
  t = t.replace(new RegExp(`(?<![\\w\\]])[\\w.+-]+\\.(${DATA_EXT})(?![\\w])`, 'gi'), (_m, ext: string) => `[file].${ext}`);
  return t;
}

/** Applies `f` to a path match without its trailing sentence punctuation. */
function trimTail(match: string, f: (p: string) => string): string {
  const m = /^(.*?)([.:!]+)?$/s.exec(match);
  const core = m ? m[1] : match;
  const tail = m?.[2] ?? '';
  return f(core) + tail;
}

/** Cleans one line. */
export function scrubLine(raw: string, ctx: ScrubContext = {}): string {
  let t = raw.replace(/[\u0000-\u0008\u000B-\u001F\u007F]/g, ' ');
  // the home folder written out, whatever follows it
  if (ctx.home && ctx.home.length > 2) {
    const h = ctx.home.replace(/[\\/]+$/, '');
    const re = new RegExp(escapeRe(h).replace(/\\\\|\//g, '[\\\\/]') + '(?![A-Za-z0-9_-])([\\\\/])?', 'gi');
    t = t.replace(re, (_m, sep: string | undefined) => (sep ? '~/' : '~'));
  }
  t = secretMasks(t);
  t = t.replace(/[\w.+-]+@[\w-]+(?:\.[\w-]+)*/g, '[email]');
  t = ipMasks(t);
  t = pathMasks(t, ctx);
  t = fileNameMasks(t);
  t = t.replace(/\b[\w-]+\.(?:local|lan|home|internal|localdomain|corp|intranet)\b/gi, '[host]');
  for (const name of [ctx.user, ctx.host]) {
    if (name && name.length >= 3) t = t.replace(new RegExp(`(?<![A-Za-z0-9])${escapeRe(name)}(?![A-Za-z0-9])`, 'gi'), name === ctx.host ? '[host]' : '[user]');
  }
  if (t.length > MAX_LINE) t = t.slice(0, MAX_LINE) + ' [cut]';
  return t.trimEnd();
}

/** The last `maxLines` of `lines`, each scrubbed; empty lines dropped. */
export function scrubLines(lines: string[], ctx: ScrubContext = {}, maxLines = 200): string[] {
  return lines
    .slice(-maxLines)
    .flatMap((l) => String(l).split(/\r?\n/))
    .map((l) => scrubLine(l, ctx))
    .filter((l) => l !== '')
    .slice(-maxLines);
}

export interface DiagnosticsInput {
  appVersion: string;
  os: string;
  engineVersion?: string | null;
  engineLines?: string[];
}

export const DIAGNOSTICS_MAX = 20000;

/** The text of the diagnostics (what the preview shows and what is sent). Never longer than DIAGNOSTICS_MAX. */
export function buildDiagnosticsText(input: DiagnosticsInput, ctx: ScrubContext = {}): string {
  const lines = scrubLines(input.engineLines ?? [], ctx, 200);
  const head = [
    `app: ${scrubLine(input.appVersion, ctx)}`,
    `system: ${scrubLine(input.os, ctx)}`,
    `engine: ${input.engineVersion ? scrubLine(input.engineVersion, ctx) : 'version not reported'}`,
    lines.length ? `engine messages this session (last ${lines.length}):` : 'engine messages this session: none',
  ];
  let text = [...head, ...lines].join('\n');
  if (text.length > DIAGNOSTICS_MAX) text = head.join('\n') + '\n[older lines cut]\n' + text.slice(text.length - (DIAGNOSTICS_MAX - head.join('\n').length - 20));
  return text.slice(0, DIAGNOSTICS_MAX);
}
