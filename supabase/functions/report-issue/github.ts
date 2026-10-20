/**
 * Optional: also open an issue in a PRIVATE GitHub repository (behind the flag REPORT_GITHUB_ISSUES=1 and the secrets
 * GITHUB_REPORT_TOKEN and GITHUB_REPORT_REPO). The reporter's text is never rendered as Markdown: it goes in a
 * fenced code block (the quote), so links, images, @mentions and HTML in it stay plain text. The contact e-mail and the
 * diagnostics are NOT copied (they stay in the database). Labels come from a fixed list.
 *
 * Before posting, the repository is checked with the GitHub API and the report is NOT sent unless it is private:
 * a mistyped or public repository can never receive reporters' text.
 */
import { cleanSingleLine, truncate } from './sanitise.ts';
import type { FetchLike, Logger } from './types.ts';
import type { Report } from './validate.ts';

const LABELS: Record<string, string> = { bug: 'bug', idea: 'idea', question: 'question' };
const SOURCE_LABELS: Record<string, string> = { app: 'from-app', web: 'from-web' };
const REPO = /^[A-Za-z0-9_.-]{1,100}\/[A-Za-z0-9_.-]{1,100}$/;
const PRIVATE_CACHE_MS = 10 * 60 * 1000;

/** A fence longer than any run of backticks in `s` (at least three), so the text cannot close it early. */
export function fenceFor(s: string): string {
  let longest = 0;
  for (const m of s.matchAll(/`+/g)) longest = Math.max(longest, m[0].length);
  return '`'.repeat(Math.max(3, longest + 1));
}

export type IssueDraft = { title: string; body: string; labels: string[] };

export function buildIssue(r: Report, id: string): IssueDraft {
  const quoted =
    `kind: ${r.kind}\nsource: ${r.source}\napp_version: ${r.app_version ?? '-'}\nsystem: ${r.os ?? '-'}\n` +
    `engine_version: ${r.engine_version ?? '-'}\n\n${r.title}\n\n${r.description}`;
  const fence = fenceFor(quoted);
  const body =
    `Report \`${id}\` (${r.source}, ${r.kind}). The reporter's text is below, in a code block so it is not rendered.\n\n` +
    `${fence}text\n${quoted}\n${fence}\n\n` +
    (r.diagnostics !== null ? 'Diagnostics were attached: see the database (not copied here).\n' : '');
  return {
    title: truncate(`[${r.kind}] ${cleanSingleLine(r.title)}`, 120),
    body: truncate(body, 60000),
    labels: [LABELS[r.kind], SOURCE_LABELS[r.source]].filter(Boolean),
  };
}

export type GithubResult = { ok: boolean; reason?: string; number?: number };

export function createGithubReporter(opts: { token: string; repo: string; fetchFn: FetchLike; now?: () => number; log?: Logger; timeoutMs?: number }) {
  const now = opts.now ?? (() => Date.now());
  let privateUntil = 0;
  const headers = {
    Authorization: `Bearer ${opts.token}`,
    Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
    'User-Agent': 'radarx-report-issue',
  };

  async function call(method: string, url: string, body?: unknown) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), opts.timeoutMs ?? 6000);
    try {
      return await opts.fetchFn(url, {
        method,
        headers: body === undefined ? headers : { ...headers, 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: ctl.signal,
      });
    } finally {
      clearTimeout(timer);
    }
  }

  return {
    async report(r: Report, id: string): Promise<GithubResult> {
      if (!REPO.test(opts.repo)) return { ok: false, reason: 'GITHUB_REPORT_REPO is not owner/name' };
      try {
        if (now() >= privateUntil) {
          const res = await call('GET', `https://api.github.com/repos/${opts.repo}`);
          if (!res.ok) return { ok: false, reason: `repo check failed (${res.status})` };
          const info = (await res.json()) as { private?: unknown } | null;
          if (!info || info.private !== true) return { ok: false, reason: 'the repository is not private: nothing was sent' };
          privateUntil = now() + PRIVATE_CACHE_MS;
        }
        const draft = buildIssue(r, id);
        const res = await call('POST', `https://api.github.com/repos/${opts.repo}/issues`, draft);
        if (!res.ok) return { ok: false, reason: `issue creation failed (${res.status})` };
        const made = (await res.json().catch(() => null)) as { number?: unknown } | null;
        return { ok: true, number: typeof made?.number === 'number' ? made.number : undefined };
      } catch {
        return { ok: false, reason: 'network error or timeout' };
      }
    },
  };
}
