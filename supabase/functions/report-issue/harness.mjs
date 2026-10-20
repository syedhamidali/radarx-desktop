// Test helpers for handler.test.mjs and index.test.mjs: a fake database with the same rules as the SQL functions,
// a fake network, and a request builder. Not part of the deployed function.
import { handleRequest } from './handler.ts';

export const JWT = 'aaaaaaaa.bbbbbbbb.cccccccc';
export const USER_ID = '3f2b6c1e-8a4d-4b7e-9f10-0123456789ab';
export const OTHER_USER_ID = '99999999-8a4d-4b7e-9f10-0123456789ab';

export const SECRETS = {
  SUPABASE_URL: 'https://proj.supabase.co',
  SUPABASE_ANON_KEY: 'ANON_KEY_SENTINEL',
  TURNSTILE_SECRET_KEY: 'TS_SECRET_SENTINEL',
  REPORT_IP_SALT: 'IP_SALT_SENTINEL',
  REPORT_NOTIFY_TO: 'owner@example.org',
  MAIL_FROM: 'Radarx <no-reply@radarx.labshope.com>',
  RESEND_API_KEY: 'RESEND_KEY_SENTINEL',
  MAILJET_API_KEY: 'MJ_KEY_SENTINEL',
  MAILJET_SECRET_KEY: 'MJ_SECRET_SENTINEL',
};

/** Same rules as supabase/migrations/20261024000000_issue_reports.sql, in memory. */
export function fakeDb() {
  const db = {
    rows: [],
    limits: new Map(),
    failNext: null,
    calls: [],
    async takeLimit(key, bucket, max) {
      db.calls.push(['takeLimit', key, bucket, max]);
      if (db.failNext === 'limits') throw Object.assign(new Error('boom: SENTINEL_DESC details'), { name: 'PostgrestError' });
      const k = `${key}|${bucket}`;
      const n = db.limits.get(k) ?? 0;
      if (n >= max) return false;
      db.limits.set(k, n + 1);
      return true;
    },
    async insertReport(row, caps) {
      db.calls.push(['insertReport', row, caps]);
      if (db.failNext === 'insert') throw Object.assign(new Error(`db exploded while storing ${row.description}`), { name: 'PostgrestError' });
      if (row.user_id) {
        const mine = db.rows.filter((r) => r.user_id === row.user_id).length;
        if (mine >= caps.userCap) return { limited: 'user' };
      }
      if (caps.globalKey) {
        const k = `${caps.globalKey}|day`;
        const n = db.limits.get(k) ?? 0;
        if (n >= caps.globalMax) return { limited: 'global' };
        db.limits.set(k, n + 1);
      }
      const id = `00000000-0000-4000-8000-${String(db.rows.length + 1).padStart(12, '0')}`;
      db.rows.push({ id, ...row });
      return { id, createdAt: '2026-10-24T10:00:00.000Z' };
    },
  };
  return db;
}

const j = (json, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => json });

/** A fake network. `net.on[host]` can be replaced per test. Every call is recorded (without being logged anywhere). */
export function fakeNet(opts = {}) {
  const net = {
    calls: [],
    turnstile: (form) => (form.get('response') === 'good-token' ? j({ success: true }) : form.get('response') === 'down' ? 'throw' : form.get('response') === 'hang' ? 'hang' : j({ success: false, 'error-codes': ['invalid-input-response'] })),
    auth: (token) => (token === JWT ? j({ id: USER_ID, email: 'person@example.org' }) : j({}, 401)),
    resend: () => j({ id: 'x' }),
    mailjet: () => j({ Messages: [{ Status: 'success' }] }),
    github: (method, url, body) => (method === 'GET' ? j({ private: true }) : j({ number: 7 })),
    ...opts,
  };
  net.fetch = async (url, init) => {
    const u = new URL(url);
    net.calls.push({ host: u.host, path: u.pathname, method: init.method, headers: init.headers, body: init.body });
    let r;
    if (u.host === 'challenges.cloudflare.com') r = net.turnstile(new URLSearchParams(init.body));
    else if (u.host === 'proj.supabase.co') r = net.auth((init.headers.Authorization ?? '').replace('Bearer ', ''));
    else if (u.host === 'api.resend.com') r = net.resend(init);
    else if (u.host === 'api.mailjet.com') r = net.mailjet(init);
    else if (u.host === 'api.github.com') r = net.github(init.method, url, init.body);
    else throw new Error(`unexpected host ${u.host}`);
    if (r === 'throw') throw new TypeError('fetch failed');
    if (r === 'hang') return new Promise((_, rej) => init.signal.addEventListener('abort', () => rej(new Error('aborted'))));
    return r;
  };
  net.to = (host) => net.calls.filter((c) => c.host === host);
  return net;
}

export function setup({ env = {}, net = fakeNet(), db = fakeDb(), defer, timeoutMs = 200 } = {}) {
  const logs = [];
  const full = { ...SECRETS, ...env };
  const deps = {
    env: (n) => full[n],
    fetchFn: net.fetch,
    now: () => new Date('2026-10-24T10:30:00.000Z'),
    db,
    log: (e) => logs.push(JSON.parse(JSON.stringify(e))),
    randomUUID: () => 'ffffffff-ffff-4fff-8fff-ffffffffffff',
    defer,
    timeoutMs,
  };
  const call = (body, { headers = {}, method = 'POST', raw } = {}) =>
    handleRequest(
      new Request('https://proj.supabase.co/functions/v1/report-issue', {
        method,
        headers: { 'content-type': 'application/json', ...headers },
        body: method === 'GET' || method === 'OPTIONS' ? undefined : (raw ?? JSON.stringify(body)),
      }),
      deps,
    );
  return { deps, logs, net, db, call, env: full };
}

export const appReport = (over = {}) => ({ source: 'app', kind: 'bug', title: 'Crash on open', description: 'The app closes when I open a file.', app_version: '0.3.0', os: 'macOS', ...over });
export const webReport = (over = {}) => ({ source: 'web', kind: 'question', title: 'How do I install', description: 'Where is the Windows installer?', turnstile_token: 'good-token', ...over });
export const asApp = { authorization: `Bearer ${JWT}` };
export const fromIp = (ip) => ({ 'cf-connecting-ip': ip });
