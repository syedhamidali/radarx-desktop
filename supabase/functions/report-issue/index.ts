// "Report an issue": stores a bug report, idea or question from the radarx app (signed-in users) or the public
// website (Turnstile-checked visitors) and tells the owner. All logic and tests live in the modules next to this
// file (handler.ts and friends); this file only does the Deno parts: serve, read secrets, talk to the database.
// Deploy: see docs/REPORTING.md. Needs `--no-verify-jwt`: website visitors have no Supabase token, and the function
// checks the user's token itself for app reports.
// deno-lint-ignore-file no-explicit-any
import { createClient } from 'npm:@supabase/supabase-js@2.118.0';
import { handleRequest } from './handler.ts';
import type { Db } from './handler.ts';

declare const EdgeRuntime: { waitUntil(p: Promise<unknown>): void } | undefined;

let client: any = null;
function service() {
  if (!client) {
    const url = Deno.env.get('SUPABASE_URL');
    const key = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY');
    if (!url || !key) throw new Error('database is not configured');
    client = createClient(url, key, { auth: { persistSession: false, autoRefreshToken: false } });
  }
  return client;
}

/** A database failure with a code and no message: nothing the database said can reach a log or a response. */
class DbError extends Error {
  constructor(code: string) {
    super(`db ${code}`);
    this.name = 'DbError';
  }
}

const db: Db = {
  async takeLimit(key, bucket, max) {
    const { data, error } = await service().rpc('take_issue_limit', { p_key: key, p_bucket: bucket, p_max: max });
    if (error) throw new DbError(String(error.code ?? 'rpc'));
    return data === true;
  },
  async insertReport(row, caps) {
    const { data, error } = await service().rpc('insert_issue_report', {
      p_user_id: row.user_id,
      p_source: row.source,
      p_kind: row.kind,
      p_title: row.title,
      p_description: row.description,
      p_contact_email: row.contact_email,
      p_app_version: row.app_version,
      p_os: row.os,
      p_engine_version: row.engine_version,
      p_diagnostics: row.diagnostics,
      p_user_cap: caps.userCap,
      p_global_key: caps.globalKey ?? null,
      p_global_max: caps.globalMax ?? null,
    });
    if (error) {
      if (error.code === 'P0001' && String(error.message).includes('report limit')) return { limited: 'user' as const };
      if (error.code === 'P0001' && String(error.message).includes('global limit')) return { limited: 'global' as const };
      throw new DbError(String(error.code ?? 'rpc'));
    }
    if (typeof data !== 'string') throw new DbError('no-id');
    return { id: data, createdAt: new Date().toISOString() };
  },
};

Deno.serve((req: Request) =>
  handleRequest(req, {
    env: (name) => Deno.env.get(name),
    fetchFn: fetch as any,
    now: () => new Date(),
    db,
    // Only the structured events the handler builds (names, outcomes, codes): see handler.ts.
    log: (event) => console.log(JSON.stringify(event)),
    randomUUID: () => crypto.randomUUID(),
    defer: typeof EdgeRuntime !== 'undefined' ? (p) => EdgeRuntime!.waitUntil(p) : undefined,
  }),
);
