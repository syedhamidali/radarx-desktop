/**
 * The logic of the "Send Email" auth hook, free of any Deno or Supabase API so Node can test it
 * (supabase/functions/send-auth-email/mail.test.mjs).
 *
 * Supabase calls the hook instead of sending the e-mail itself. We turn its payload into a short
 * message carrying the 6-digit code and send it with Resend; if Resend fails or times out, Mailjet
 * sends it. Nothing here ever logs or returns an address, a code or a key.
 */

export type HookPayload = {
  user: { email: string };
  email_data: { token: string; email_action_type: string };
};

export type Message = { to: string; subject: string; html: string; text: string };

export type Attempt = { provider: string; ok: boolean; status?: number; reason?: string };

export type Provider = {
  name: string;
  send: (m: Message, signal: AbortSignal) => Promise<void>;
};

/** The e-mail kinds the radarx app uses. Anything else is refused instead of guessed. */
const SUBJECTS: Record<string, { subject: string; lead: string }> = {
  signup: { subject: 'Your radarx code', lead: 'Enter this code in radarx to finish creating your account:' },
  recovery: { subject: 'Reset your radarx password', lead: 'Enter this code in radarx, then choose a new password:' },
  magiclink: { subject: 'Your radarx sign-in code', lead: 'Enter this code in radarx to sign in:' },
  reauthentication: { subject: 'Your radarx confirmation code', lead: 'Enter this code in radarx to confirm it is you:' },
};

export class MailError extends Error {
  attempts: Attempt[];
  constructor(message: string, attempts: Attempt[] = []) {
    super(message);
    this.name = 'MailError';
    this.attempts = attempts;
  }
}

const EMAIL = /^[^\s@<>"]+@[^\s@<>"]+\.[^\s@<>"]{2,}$/;

export function buildMessage(p: HookPayload): Message {
  const kind = p?.email_data?.email_action_type;
  const spec = SUBJECTS[kind];
  if (!spec) throw new MailError(`unsupported e-mail type: ${String(kind).slice(0, 40)}`);
  const to = p?.user?.email;
  if (typeof to !== 'string' || !EMAIL.test(to)) throw new MailError('invalid recipient');
  const code = p.email_data.token;
  if (typeof code !== 'string' || !/^\d{4,10}$/.test(code)) throw new MailError('invalid code');
  const tail = 'It expires in one hour. If you did not ask for it, ignore this message.';
  return {
    to,
    subject: spec.subject,
    text: `${spec.lead}\n\n${code}\n\n${tail}\n`,
    html:
      `<h2>${spec.subject}</h2><p>${spec.lead}</p>` +
      `<p style="font-size:28px;letter-spacing:6px;font-family:monospace"><strong>${code}</strong></p><p>${tail}</p>`,
  };
}

/** `Name <addr@host>` or `addr@host` into its parts. */
export function parseFrom(from: string): { name: string; email: string } {
  const m = /^\s*(?:"?([^"<]*?)"?\s*)?<([^<>\s]+@[^<>\s]+)>\s*$/.exec(from);
  if (m) return { name: (m[1] ?? '').trim(), email: m[2] };
  const bare = from.trim();
  if (!EMAIL.test(bare)) throw new MailError('MAIL_FROM is not a valid address');
  return { name: '', email: bare };
}

type Fetch = (url: string, init: { method: string; headers: Record<string, string>; body: string; signal: AbortSignal }) => Promise<{ ok: boolean; status: number; json: () => Promise<unknown> }>;

export function resendProvider(apiKey: string, from: string, fetchFn: Fetch): Provider {
  return {
    name: 'resend',
    async send(m, signal) {
      const res = await fetchFn('https://api.resend.com/emails', {
        method: 'POST',
        headers: { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ from, to: [m.to], subject: m.subject, html: m.html, text: m.text }),
        signal,
      });
      if (!res.ok) throw new HttpFailure(res.status);
    },
  };
}

export function mailjetProvider(apiKey: string, secretKey: string, from: string, fetchFn: Fetch): Provider {
  const sender = parseFrom(from);
  // Basic auth without Buffer (not in the Deno runtime): btoa is in every runtime we use.
  const basic = btoa(`${apiKey}:${secretKey}`);
  return {
    name: 'mailjet',
    async send(m, signal) {
      const res = await fetchFn('https://api.mailjet.com/v3.1/send', {
        method: 'POST',
        headers: { Authorization: `Basic ${basic}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({
          Messages: [
            {
              From: { Email: sender.email, ...(sender.name ? { Name: sender.name } : {}) },
              To: [{ Email: m.to }],
              Subject: m.subject,
              TextPart: m.text,
              HTMLPart: m.html,
            },
          ],
        }),
        signal,
      });
      if (!res.ok) throw new HttpFailure(res.status);
      // Mailjet answers 200 with a per-message status; only "success" counts.
      const body = (await res.json().catch(() => null)) as { Messages?: { Status?: string }[] } | null;
      if (body?.Messages?.[0]?.Status !== 'success') throw new HttpFailure(res.status, 'not accepted');
    },
  };
}

class HttpFailure extends Error {
  status: number;
  constructor(status: number, why = 'http error') {
    super(`${why} ${status}`);
    this.status = status;
  }
}

/** Providers in the order they should be tried, from the function's environment. Unknown names are ignored. */
export function providersFromEnv(env: Record<string, string | undefined>, fetchFn: Fetch): Provider[] {
  const from = env.MAIL_FROM?.trim();
  if (!from) throw new MailError('MAIL_FROM is not set');
  parseFrom(from); // fail early on a malformed sender
  const order = (env.MAIL_ORDER ?? 'resend,mailjet').split(',').map((s) => s.trim().toLowerCase()).filter(Boolean);
  const out: Provider[] = [];
  for (const name of order) {
    if (name === 'resend' && env.RESEND_API_KEY) out.push(resendProvider(env.RESEND_API_KEY, from, fetchFn));
    if (name === 'mailjet' && env.MAILJET_API_KEY && env.MAILJET_SECRET_KEY) {
      out.push(mailjetProvider(env.MAILJET_API_KEY, env.MAILJET_SECRET_KEY, from, fetchFn));
    }
  }
  if (out.length === 0) throw new MailError('no mail provider is configured');
  return out;
}

/**
 * Try each provider in turn, each with its own time limit. Returns the provider that sent it and the
 * attempts made (names, status codes and short reasons only). Throws a MailError listing the attempts
 * when none succeeded.
 */
export async function sendWithFailover(
  providers: Provider[],
  message: Message,
  options: { timeoutMs?: number } = {},
): Promise<{ provider: string; attempts: Attempt[] }> {
  const timeoutMs = options.timeoutMs ?? 8000;
  const attempts: Attempt[] = [];
  for (const p of providers) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    try {
      await p.send(message, ctl.signal);
      attempts.push({ provider: p.name, ok: true });
      return { provider: p.name, attempts };
    } catch (e) {
      const status = e instanceof HttpFailure ? e.status : undefined;
      const reason = ctl.signal.aborted ? 'timeout' : status !== undefined ? 'refused' : 'network';
      attempts.push({ provider: p.name, ok: false, status, reason });
    } finally {
      clearTimeout(timer);
    }
  }
  throw new MailError('every mail provider failed', attempts);
}
