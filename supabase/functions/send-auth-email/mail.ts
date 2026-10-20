/**
 * The logic of the "Send Email" auth hook, free of any Deno or Supabase API so Node can test it
 * (supabase/functions/send-auth-email/mail.test.mjs).
 *
 * Supabase calls the hook instead of sending the e-mail itself. We turn its payload into a short
 * message carrying the 6-digit code and send it with Resend; if Resend fails or times out, Mailjet
 * sends it (the providers live in ../_shared/mail.ts). Nothing here ever logs or returns an address, a code or a key.
 */
import { MailError, EMAIL } from '../_shared/mail.ts';
import type { Message } from '../_shared/mail.ts';

// Everything the function used to export from here is still exported from here.
export * from '../_shared/mail.ts';

export type HookPayload = {
  user: { email: string };
  email_data: { token: string; email_action_type: string };
};

/** The e-mail kinds the radarx app uses. Anything else is refused instead of guessed. */
const SUBJECTS: Record<string, { subject: string; lead: string }> = {
  signup: { subject: 'Your radarx code', lead: 'Enter this code in radarx to finish creating your account:' },
  recovery: { subject: 'Reset your radarx password', lead: 'Enter this code in radarx, then choose a new password:' },
  magiclink: { subject: 'Your radarx sign-in code', lead: 'Enter this code in radarx to sign in:' },
  reauthentication: { subject: 'Your radarx confirmation code', lead: 'Enter this code in radarx to confirm it is you:' },
};

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

