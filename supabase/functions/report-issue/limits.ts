/**
 * Rate-limit helpers: the numbers, the time buckets, and how a visitor's address becomes a key.
 * The address itself is never stored or logged: only a salted SHA-256 of it, per hourly bucket.
 */

export const LIMITS = {
  /** reports per signed-in account per rolling 24 hours (enforced in the database, insert_issue_report) */
  appPerUserDay: 10,
  /** reports per address per hour on the website */
  webPerIpHour: 5,
  /** safety nets over everybody, per UTC day and source */
  webGlobalDay: 200,
  appGlobalDay: 1000,
};

export const GLOBAL_KEY = { web: 'global:web', app: 'global:app' };

/** Start of the UTC hour of `now`, as an ISO string (the bucket id). */
export function hourBucket(now: Date): string {
  const d = new Date(now.getTime());
  d.setUTCMinutes(0, 0, 0);
  return d.toISOString();
}

/** Start of the UTC day of `now`, as an ISO string. */
export function dayBucket(now: Date): string {
  const d = new Date(now.getTime());
  d.setUTCHours(0, 0, 0, 0);
  return d.toISOString();
}

export function secondsToNextHour(now: Date): number {
  return Math.max(1, Math.ceil((new Date(hourBucket(now)).getTime() + 3600_000 - now.getTime()) / 1000));
}

export function secondsToNextDay(now: Date): number {
  return Math.max(1, Math.ceil((new Date(dayBucket(now)).getTime() + 86400_000 - now.getTime()) / 1000));
}

const IPV4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/;

function ipv4(s: string): string | null {
  const m = IPV4.exec(s);
  if (!m) return null;
  const parts = m.slice(1).map(Number);
  if (parts.some((n) => n > 255)) return null;
  return parts.join('.');
}

/**
 * A canonical key for a client address, or null when `raw` is not an address. IPv4 stays as it is. IPv6 is cut to its
 * /64 network (one household or one rented server gets a whole /64, so per-address limits would mean nothing).
 * An IPv4-mapped IPv6 address counts as the IPv4 one.
 */
export function normalizeIp(raw: string): string | null {
  let s = raw.trim().toLowerCase();
  if (s.startsWith('[') && s.includes(']')) s = s.slice(1, s.indexOf(']')); // [::1]:443
  const direct = ipv4(s);
  if (direct) return direct;
  const v4port = /^(\d{1,3}(?:\.\d{1,3}){3}):\d{1,5}$/.exec(s);
  if (v4port) return ipv4(v4port[1]);
  const zone = s.indexOf('%');
  if (zone >= 0) s = s.slice(0, zone);
  if (!/^[0-9a-f:.]+$/.test(s) || !s.includes(':')) return null;
  // an embedded IPv4 tail (::ffff:1.2.3.4) becomes two hex groups
  const tail = /(\d{1,3}(?:\.\d{1,3}){3})$/.exec(s);
  if (tail) {
    const v4 = ipv4(tail[1]);
    if (!v4) return null;
    const [a, b, c, d] = v4.split('.').map(Number);
    s = s.slice(0, s.length - tail[1].length) + ((a << 8) | b).toString(16) + ':' + ((c << 8) | d).toString(16);
  }
  const halves = s.split('::');
  if (halves.length > 2) return null;
  const head = halves[0] === '' ? [] : halves[0].split(':');
  const rest = halves.length === 2 ? (halves[1] === '' ? [] : halves[1].split(':')) : [];
  let groups: string[];
  if (halves.length === 2) {
    if (head.length + rest.length > 7) return null;
    groups = [...head, ...Array(8 - head.length - rest.length).fill('0'), ...rest];
  } else {
    groups = head;
  }
  if (groups.length !== 8 || !groups.every((g) => /^[0-9a-f]{1,4}$/.test(g))) return null;
  const n = groups.map((g) => parseInt(g, 16));
  // ::ffff:a.b.c.d (IPv4-mapped) is that IPv4 address
  if (n.slice(0, 5).every((x) => x === 0) && n[5] === 0xffff) return `${n[6] >> 8}.${n[6] & 255}.${n[7] >> 8}.${n[7] & 255}`;
  return n.slice(0, 4).map((x) => x.toString(16)).join(':') + '::/64';
}

/**
 * The client address as the platform reports it. `cf-connecting-ip` is set by Cloudflare (it overwrites anything the
 * client sent), so it comes first; then `x-real-ip`; then the first `x-forwarded-for` entry. The last two can be
 * forged by a client when no trusted proxy rewrites them: the Turnstile check and the global cap still stand behind
 * the per-address cap (docs/REPORTING.md, "What is not covered").
 */
export function clientIp(headers: { get: (name: string) => string | null }): string | null {
  for (const name of ['cf-connecting-ip', 'x-real-ip']) {
    const v = headers.get(name);
    if (v) {
      const ip = normalizeIp(v);
      if (ip) return ip;
    }
  }
  const xff = headers.get('x-forwarded-for');
  if (xff) return normalizeIp(xff.split(',')[0]);
  return null;
}

/** Salted SHA-256 of the address, as lowercase hex. */
export async function hashIp(ip: string, salt: string): Promise<string> {
  const data = new TextEncoder().encode(`${salt}\n${ip}`);
  const digest = await crypto.subtle.digest('SHA-256', data);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('');
}
