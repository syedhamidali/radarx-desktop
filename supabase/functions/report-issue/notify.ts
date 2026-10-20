/**
 * The e-mail to the owner when a report has been stored. Plain text plus an HTML part in which EVERYTHING that came
 * from the reporter is escaped. The subject is a fixed prefix plus the title with control characters and newlines
 * removed and shortened, so it cannot carry an injected header.
 *
 * Sending uses the same Resend-then-Mailjet failover as the sign-up e-mails (../_shared/mail.ts).
 */
import { EMAIL, providersFromEnv, sendWithFailover } from '../_shared/mail.ts';
import type { Message, Attempt } from '../_shared/mail.ts';
import { cleanSingleLine, escapeHtml, subjectText, truncate, cpLength } from './sanitise.ts';
import type { FetchLike } from './types.ts';
import type { Report } from './validate.ts';

export const SUBJECT_PREFIX = '[radarx report] ';
const DIAGNOSTICS_IN_MAIL = 4000;

export type Stored = { id: string; createdAt: string; userId: string | null };

type Row = [label: string, value: string];

function rows(r: Report, s: Stored): Row[] {
  const out: Row[] = [
    ['Id', s.id],
    ['Received', s.createdAt],
    ['Source', r.source],
    ['Kind', r.kind],
    ['Account', s.userId ?? '(none)'],
    ['Contact', r.contact_email ?? '(none)'],
    ['App version', r.app_version ?? '(not given)'],
    ['System', r.os ?? '(not given)'],
    ['Engine', r.engine_version ?? '(not given)'],
  ];
  return out;
}

/** The message for the owner. `to` is the (already validated) REPORT_NOTIFY_TO address. */
export function buildNotification(to: string, r: Report, s: Stored): Message {
  const diag = r.diagnostics === null ? null : truncate(r.diagnostics, DIAGNOSTICS_IN_MAIL);
  const more = r.diagnostics !== null && cpLength(r.diagnostics) > DIAGNOSTICS_IN_MAIL
    ? `\n[... ${cpLength(r.diagnostics) - DIAGNOSTICS_IN_MAIL} more characters are in the database]`
    : '';
  const facts = rows(r, s);
  const text =
    'A new radarx report was stored.\n\n' +
    facts.map(([k, v]) => `${k}: ${cleanSingleLine(v)}`).join('\n') +
    `\n\nTitle:\n${r.title}\n\nDescription:\n${r.description}\n` +
    (diag !== null ? `\nDiagnostics:\n${diag}${more}\n` : '');
  const html =
    '<h2>A new radarx report was stored</h2>' +
    '<table cellpadding="4" style="border-collapse:collapse">' +
    facts.map(([k, v]) => `<tr><td><strong>${escapeHtml(k)}</strong></td><td>${escapeHtml(cleanSingleLine(v))}</td></tr>`).join('') +
    '</table>' +
    `<h3>Title</h3><p>${escapeHtml(r.title)}</p>` +
    `<h3>Description</h3><pre style="white-space:pre-wrap;font-family:inherit">${escapeHtml(r.description)}</pre>` +
    (diag !== null ? `<h3>Diagnostics</h3><pre style="white-space:pre-wrap">${escapeHtml(diag + more)}</pre>` : '');
  const message: Message = {
    to,
    subject: `${SUBJECT_PREFIX}${r.kind}: ${subjectText(r.title)}`,
    text,
    html,
  };
  // Reply goes to the reporter when they gave an address (it passed the address-shape check: no spaces, no angle brackets).
  if (r.contact_email) message.replyTo = r.contact_email;
  return message;
}

export type NotifyResult = { sent: boolean; provider?: string; attempts: Attempt[]; reason?: string };

/** Sends the notification. Never throws: the report is already stored, a failure here is only reported back. */
export async function notifyOwner(opts: {
  env: (name: string) => string | undefined;
  fetchFn: FetchLike;
  report: Report;
  stored: Stored;
  timeoutMs?: number;
}): Promise<NotifyResult> {
  const to = opts.env('REPORT_NOTIFY_TO')?.trim();
  if (!to || !EMAIL.test(to)) return { sent: false, attempts: [], reason: 'REPORT_NOTIFY_TO is not set or not an address' };
  try {
    const envObject: Record<string, string | undefined> = {};
    for (const k of ['MAIL_FROM', 'MAIL_ORDER', 'RESEND_API_KEY', 'MAILJET_API_KEY', 'MAILJET_SECRET_KEY']) envObject[k] = opts.env(k);
    const providers = providersFromEnv(envObject, opts.fetchFn as any);
    const done = await sendWithFailover(providers, buildNotification(to, opts.report, opts.stored), { timeoutMs: opts.timeoutMs ?? 5000 });
    return { sent: true, provider: done.provider, attempts: done.attempts };
  } catch (e) {
    const attempts = (e as { attempts?: Attempt[] })?.attempts ?? [];
    return { sent: false, attempts, reason: attempts.length ? 'every provider failed' : 'mail is not configured' };
  }
}
