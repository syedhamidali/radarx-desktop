// Supabase Auth "Send Email" hook: sends radarx's sign-up and reset codes with Resend, and with Mailjet when
// Resend fails. Logic and tests live in mail.ts / mail.test.mjs; this file only does the Deno and webhook parts.
// Deploy: see README.md (needs `--no-verify-jwt`: Supabase calls it with a signed webhook, not a user token).
// deno-lint-ignore-file no-explicit-any
import { Webhook } from 'npm:standardwebhooks@1.0.0';
import { buildMessage, providersFromEnv, sendWithFailover, MailError } from './mail.ts';

function reply(status: number, message?: string): Response {
  const body = message ? { error: { http_code: status, message } } : {};
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

Deno.serve(async (req: Request) => {
  if (req.method !== 'POST') return reply(405, 'method not allowed');

  const secret = (Deno.env.get('SEND_EMAIL_HOOK_SECRET') ?? '').replace('v1,whsec_', '');
  if (!secret) return reply(500, 'hook secret is not configured');

  const raw = await req.text();
  let payload: any;
  try {
    payload = new Webhook(secret).verify(raw, Object.fromEntries(req.headers));
  } catch {
    return reply(401, 'invalid signature');
  }

  try {
    const message = buildMessage(payload);
    const providers = providersFromEnv(Deno.env.toObject(), fetch as any);
    const done = await sendWithFailover(providers, message);
    // Only provider names and outcomes are logged: never an address, a code or a key.
    console.log(JSON.stringify({ event: 'auth_email', type: payload.email_data.email_action_type, provider: done.provider, attempts: done.attempts }));
    return reply(200);
  } catch (e) {
    const attempts = e instanceof MailError ? e.attempts : [];
    console.error(JSON.stringify({ event: 'auth_email_failed', reason: e instanceof MailError ? e.message : 'unexpected', attempts }));
    return reply(500, 'could not send the e-mail');
  }
});
