# Reporting an issue

radarx-desktop has one way to tell the project about a bug, an idea or a question from inside the app, and a
matching form on the website. Both end up in the same place: a private table that only the owner can read, and an
e-mail to the owner. This page has the user's view first, then what is stored, then the exact interface (for the
website), then the owner's set-up.

## For users: how to report

Open **Report an issue** from the About dialog, from the command palette (`Ctrl/Cmd+K`, type "report"), or from the
Account dialog. Choose what it is (something is wrong, an idea, a question), give it a title and say what happened.

Two things are sent every time, because a report is hard to use without them: **the radarx version** and **the name
of your system** (for example "macOS arm (desktop)"). Your signed-in account is how the report reaches us, so we know
who wrote it; we may reply to the e-mail address of the account.

**Diagnostics are off.** If you tick *Include diagnostics* the report also carries the engine version (when the
engine reports one; today it does not) and the last warnings and errors the engine gave the app in this session (at
most 200 lines, kept in memory only, never on disk). Before anything is shown or sent, those lines are scrubbed:

| Removed or masked | Shown as |
|---|---|
| paths inside your home folder start with `~`; any other absolute path keeps only its last two folders | `~/.../engine/reader.py` |
| the name of a data file (`.nc`, `.gz`, a NEXRAD name such as `KTLX20130520_201643_V06`, ...) | `[file].gz` |
| e-mail addresses and `user@host` | `[email]` |
| IP addresses (v4 and v6) | `[ip]` |
| JWT-like strings, `Bearer ...`, `token=`, `password=`, `api_key=` values, well-known key prefixes, long hex and long base64-like strings | `[token]`, `[removed]`, `[hex]` |
| your user name and the machine name when the app knows them, and names ending `.local`, `.lan`, `.home`, `.corp` | `[user]`, `[host]` |

**Look before you send.** *Show exactly what will be sent* opens the complete message, including the scrubbed
diagnostics, and it is the same object that is sent. The scrubbing is pattern based and cannot be perfect: a place
name in a folder name, or something unusual in free text, can get through, which is why the preview exists. What you
type in the title and description is sent as you wrote it: leave out anything private. The app itself sends no radar files and no
locations; file names it recognises in the diagnostics are masked (table above).

If sending fails (offline, a slow service, a limit) the dialog stays open with everything still typed, and says what
happened in a sentence. Closing the dialog keeps the draft until the report has been sent or the app is closed (memory
only). When it works you see a reference number; keep it if you want to ask about the report.

A signed-in account may send **10 reports in 24 hours**.

## What is stored, who sees it, how long

One row in `issue_reports` per report: the time, the kind, the title and description, the app version, the system
name, the engine version and diagnostics (if you included them), your account id (app reports only) and, for website
reports, the contact e-mail the visitor typed. See {doc}`PRIVACY` for the notice.

- **Who sees it:** the project owner, through the database dashboard and the notification e-mail (sent with Resend, or
  Mailjet when Resend fails; the same mailers as the sign-up codes). If the owner switches on the optional GitHub
  copy, the report text also goes to a **private** repository (see below); the contact e-mail and the diagnostics
  never go to GitHub. A signed-in user can read their own reports through the API (`select` on their own rows only).
- **How long:** 24 months, then `purge_issue_reports(730)` deletes them. The owner must schedule it (below).
- **Deletion:** write to the project owner and ask; the owner deletes the row. There is no self-service delete.
- **If you delete your account:** your reports stay but are **no longer linked to you** (`user_id` becomes empty); they
  keep whatever you wrote in them. Ask for them to be deleted by e-mail if you want them gone.
- **The visitor's IP address** (website form) is never stored. The rate limiter keeps a salted SHA-256 of it per hour,
  deleted after three days.

## The interface (the website form builds to this)

`POST https://bfyimkgvnqiqedcwprpi.supabase.co/functions/v1/report-issue`, deployed with `--no-verify-jwt`,
`Content-Type: application/json`:

```text
{
  "source": "app" | "web",
  "kind": "bug" | "idea" | "question",
  "title": "3 to 140 characters",
  "description": "10 to 5000 characters",
  "contact_email": "optional, at most 200",
  "app_version": "optional, at most 40",
  "os": "optional, at most 60",
  "engine_version": "optional, at most 40",
  "diagnostics": "optional, at most 20000",
  "turnstile_token": "web only: the token from the Turnstile widget",
  "website": "web only: the honeypot, an input people never see; it must be empty or absent"
}
```

- **`source: "app"`** needs `Authorization: Bearer <the user's Supabase access token>`. The function asks Supabase's
  auth API who the token belongs to (with the project's publishable key) and uses **that** user id; nothing in the body
  can name a user. No Turnstile. At most 10 reports per account per rolling 24 hours (a safety net of 1000 a day over
  all accounts).
- **`source: "web"`** is anonymous and needs `turnstile_token`, verified on the server with Cloudflare Turnstile
  (`siteverify`, with the visitor's address as `remoteip` when known). The check **fails closed**: if Cloudflare is
  unreachable the answer is a `server_error`, never a pass. At most **5 per address per hour** and **200 per day** over
  all addresses. A filled honeypot gets the normal success answer and nothing is stored or sent.
- **Success:** HTTP 200 `{"id": "<uuid>"}`.
- **Errors:** `{"error": {"code": "...", "message": "a short sentence", "field": "title"}}` (`field` only for
  `invalid_input` and `turnstile_failed`):

| HTTP | code | when |
|---|---|---|
| 400 | `invalid_input` | wrong content type, not JSON, too large, or a field out of range (the field is named) |
| 400 | `turnstile_failed` | web: no token, or Cloudflare says the token is not valid |
| 401 | `unauthorized` | app: no token, or Supabase does not accept it |
| 429 | `rate_limited` | a limit above; `Retry-After` says when to try again |
| 500 | `server_error` | the database, Supabase auth or Turnstile could not be reached, or the function is not configured |

(A 405 `invalid_input` answers anything but POST and OPTIONS. Messages never repeat what was sent and never carry
internals.)

- **Text rules:** NUL characters are rejected; other control characters (not `\n`, `\t`) are removed; line endings
  are normalised to `\n`; invisible and direction-changing characters are removed; text is trimmed; long runs of
  spaces and blank lines are collapsed. Lengths are counted in code points after cleaning. `kind` and `source` are
  allow-listed. Nothing the visitor wrote is ever rendered as HTML: the notification e-mail has a plain-text part and
  an HTML part in which everything is escaped, and its subject is a fixed prefix plus the title on one line
  (control characters removed, 100 characters at most).
- **CORS:** `OPTIONS` is answered. Allowed origins: the list in the secret `REPORT_ALLOWED_ORIGINS` (default
  `https://radarx.labshope.com`), plus `tauri://localhost`, `http://tauri.localhost` and `https://tauri.localhost` for
  `source: "app"`, plus `http://localhost:*` and `http://127.0.0.1:*` only when `REPORT_ALLOW_LOCALHOST=1`. Allowed request
  headers: `Authorization`, `Content-Type`, `apikey`. Never `*`, never credentials. The web build of the app, if hosted
  on another origin, must be added to `REPORT_ALLOWED_ORIGINS`.

## For the owner: set-up

You do these steps; nothing here is done for you, and no secret belongs in this repository or in a chat.

1. **Apply the migration** `supabase/migrations/20261024000000_issue_reports.sql` to the project (the same way as the
   earlier ones, `supabase db push`). It creates `issue_reports`, `issue_report_limits`, three service-role-only
   functions (`take_issue_limit`, `insert_issue_report`, `purge_issue_reports`) and the view `issue_reports_triage`.
2. **Create the Turnstile widget** in the Cloudflare dashboard: *Turnstile > Add widget*, name it "radarx reports", add
   the hostname `radarx.labshope.com`, mode **Managed**. Cloudflare shows a **site key** (public: it goes in the
   website's configuration) and a **secret key** (private: it goes only into the Supabase secret below).
3. **Set the secrets** (you type the values; `REPORT_IP_SALT` is any long random string, for example from
   `openssl rand -hex 32`, kept secret):
   ```bash
   supabase secrets set \
     TURNSTILE_SECRET_KEY='<the secret key from step 2>' \
     REPORT_NOTIFY_TO='you@example.org' \
     REPORT_ALLOWED_ORIGINS='https://radarx.labshope.com' \
     REPORT_IP_SALT="$(openssl rand -hex 32)"
   ```
   The mail secrets from the sign-up e-mails (`MAIL_FROM`, `RESEND_API_KEY`, `MAILJET_API_KEY`, `MAILJET_SECRET_KEY`,
   optional `MAIL_ORDER`, see `supabase/functions/send-auth-email/README.md`) are reused for the notification. Without
   them, reports are still stored and the function logs that no notification was sent. `SUPABASE_URL`,
   `SUPABASE_ANON_KEY` and `SUPABASE_SERVICE_ROLE_KEY` are provided to every function by Supabase.
4. **Deploy:**
   ```bash
   supabase functions deploy report-issue --no-verify-jwt
   supabase functions deploy send-auth-email --no-verify-jwt   # it now imports ../_shared/mail.ts: redeploy it too
   ```
5. **Put the site key in the website's configuration** (the form's widget and the endpoint URL above).
6. **Schedule the retention clean-up** (SQL editor), for example weekly with `pg_cron`:
   ```text
   select cron.schedule('purge-issue-reports', '17 3 * * 0', $$select public.purge_issue_reports(730)$$);
   ```
   `purge_issue_reports(days)` deletes reports older than `days` and expired rate-limit buckets; the policy is 24 months.
7. **Check it** with a real report from the app and one from the website, and read *Edge Functions > report-issue >
   Logs*: one line per request with an outcome and a code, never text, addresses, tokens or secrets.

Optional secrets: `REPORT_ALLOW_LOCALHOST=1` (local development only), and the GitHub copy below.

### Reading the reports

- **Dashboard:** *Table editor > issue_reports*, or in the SQL editor `select * from public.issue_reports_triage;` (newest
  first, with a 300-character excerpt; the full text and diagnostics are in `issue_reports`). Set `status` to `triaged` or
  `closed` as you go.
- **E-mail:** each stored report sends one message to `REPORT_NOTIFY_TO`; the Reply-To is the contact address when one
  was given. If both mail providers fail, nothing is lost: the report is stored first and the e-mail is only a notice.
- **GitHub (optional, off by default):** set `REPORT_GITHUB_ISSUES=1`, `GITHUB_REPORT_TOKEN` (a fine-grained token with
  *Issues: write* on one repository) and `GITHUB_REPORT_REPO=owner/name`. **The repository must be private**: the function
  asks GitHub and sends nothing unless `private` is true. An issue holds the report id, the kind and source and the text
  in a fenced code block (so nothing renders: no links, images or mentions), with labels from a fixed list (`bug`,
  `idea`, `question`, `from-app`, `from-web`; create them in the repository first or GitHub creates them). The contact
  e-mail and the diagnostics are not copied.

## What is not covered (abuse paths that remain)

- **A determined spammer with many sources.** The website form is protected by Turnstile, a per-address hourly limit and
  a daily global limit. A botnet that solves Turnstile (or pays people to) from many addresses can reach 200 reports a
  day, which is the cap, and fill the owner's inbox up to it. The cap is a safety net, not a defence.
- **The client address header.** The per-address limit uses `cf-connecting-ip`, then `x-real-ip`, then the first
  `x-forwarded-for` entry. If the platform does not overwrite the last two, a client could forge them to dodge the
  per-address limit (it still needs a valid Turnstile token per report and the global cap still applies). Addresses of the
  same IPv6 /64 share one allowance. When no address can be determined all such requests share one bucket.
- **Accounts.** An app report needs a signed-in account (an e-mail the sender controls). Someone can create accounts and
  send 10 reports each per day, up to a global safety net of 1000 a day. Sign-up has its own protections
  ({doc}`ACCOUNTS`).
- **A fixed daily window for the global caps** (UTC days) and a fixed hourly window per address: a burst across a window
  boundary can double for that moment. The per-account limit is a rolling 24 hours.
- **Failed Turnstile attempts are not counted.** Each costs one call to Cloudflare and one function invocation; the
  Supabase and Cloudflare platform limits are the bound.
- **Content.** The text is cleaned and escaped but not judged: a report can be rude, false or contain someone's personal
  data. The owner reads it as untrusted text and deletes what should not be kept.
- **The honeypot** only stops bots that fill every field.
- **Scrubbing is pattern based** (see above). Free text can hold anything; that is what the preview is for.
- **The notification e-mail** goes through the sending domain's reputation; a flood of reports could be marked as spam by
  the mailbox provider. The global cap bounds it.

## Not verified here

This was built and tested without Deno, a Supabase project, a real Turnstile widget or real mail accounts: the logic runs
under Node against fakes (`supabase/functions/report-issue/*.test.mjs`), the database under an in-process Postgres
(`supabase/tests/issue_reports.sql`), and the dialog in a browser with a stubbed endpoint. `index.ts` (the Deno wiring)
is smoke-tested against stand-ins only. First real runs to watch: Turnstile verification with the real secret, the Supabase
auth call with the real publishable key, CORS from the real site, `EdgeRuntime.waitUntil`, the e-mail delivery, and the
app on Windows and Linux.
