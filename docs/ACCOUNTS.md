# Accounts

radarx-desktop needs an account to run. The account is **free**, there is no billing, and **any valid
signed-in account is enough**. A new install asks for one, then opens straight into the app on every later
start. The same account is required everywhere the program can be used:

| Surface | What happens without a signed-in account |
|---|---|
| the desktop app | the sign-in screen; the app does not open |
| the engine (the Python process behind the window, `python/server.py`) | every WebSocket message except `ping` / `get_web_info` / `auth_*` is answered `auth_required`; nothing is processed |
| the command line `rxd` (and the deprecated `xrd`) | a friendly message and **exit code 5**; only `login`, `logout`, `whoami`, `examples`, `completion`, `--help` and `--version` work |
| the Python API `import rxd` | `import rxd` always works; every function that touches data raises `AccountRequired` |

One account and one credential serve all four (see [How the surfaces share one sign-in](#how-the-surfaces-share-one-sign-in)).
Your radar files and results still never leave your machine: the account is a gate, not a cloud.

```{important}
**What this gate does and does not do.** radarx-desktop is source-available software: the check runs on
your computer, so anyone who edits the code can remove it. The gate stops honest use without an account and
supports the licence terms; it is **not** copy protection and not security against a determined user. Real
enforcement of **paid** features later needs server-side features (a hosted service, signed downloads,
computation that runs on our servers). The list of ways around it is in
[Known ways around the gate](#known-ways-around-the-gate); nothing here claims more than that.
```

## What users see

- **First run:** "Create your free account" with e-mail (Google, Apple and Facebook buttons appear once they are set up, see below). The form asks for
  name, e-mail, password, **affiliation** (a university, agency or company, or "Independent") and what
  radarx is mostly used for. People who sign in with a provider are asked for the same two items once.
- **Every later start:** no screen at all. The session is kept and refreshed.
- **No network (ship, aircraft, field laptop):** radarx works for up to **30 days** after the last time the
  server vouched for the account (see [The offline rule](#the-offline-rule)), and the status bar says "offline".
  After that it asks to reconnect.
- **Account dialog** (status bar, click your name): edit name, affiliation and use; see and remove your
  computers; **Activity**: the list of the last 100 events your account has sent, exactly as stored (see
  [The activity log](#the-activity-log)); sign out; delete the account (everything is removed, typed confirmation).
- **Terminal and Python:** `rxd login` once (e-mail, password typed without echo), then every `rxd` command and
  `import rxd` works. `rxd whoami` shows the account, `rxd logout` signs out. Creating the account itself happens
  in the app (or its sign-up screen), where the e-mail code is entered.
- **Sharing:** after an image or movie is saved a "Share" bar appears. It opens X, Facebook, LinkedIn or
  Bluesky with a caption filled in and shows the file so it can be attached; for Instagram it copies the
  caption and shows the file. radarx never posts on anyone's behalf.

## What is stored (Supabase project "Radarx-Desktop")

| Table | Content | Who can read it |
|---|---|---|
| `auth.users` (Supabase) | e-mail, password hash, provider | the user (via the API); the owner |
| `profiles` | name, affiliation, use case, terms version, avatar | the user only |
| `devices` | a random install id, computer name, platform, app version, last seen | the user only |
| `synced_items` | recipes, workspaces, looks, colour maps the user chose to sync | the user only |
| `activity_events` | which **features** the account used (no file names, places or data; see below) | the user (insert and read their own); the owner via a service-role-only view |

Row-level security is on for every table (see `supabase/migrations/`). The app, the engine and `rxd` hold only
the **publishable** key; the database password and the service-role key are never in this repository or on a
user's machine. There is no billing in this version.

## How the surfaces share one sign-in

The Supabase session lives in **one file per user**, in the operating system's user configuration folder:

| System | Path |
|---|---|
| macOS | `~/Library/Application Support/radarx-desktop/session.json` |
| Windows | `%APPDATA%\radarx-desktop\session.json` |
| Linux | `$XDG_CONFIG_HOME/radarx-desktop/session.json` (default `~/.config/radarx-desktop/`) |

`RADARX_CONFIG_DIR` replaces the folder (containers, tests). The file is written with mode `0600` through a
temporary file and an atomic rename, and every read-modify-write holds a lock file, so the app's engine and a
terminal can run at the same moment. It holds tokens and the e-mail, **never the password**. Storing the tokens
in the operating system's keychain (macOS Keychain, Windows Credential Manager, Secret Service) instead of a
file is a follow-up; until then the protection is the file mode and your user account. On Windows the mode
bits do not apply: the file inherits the private permissions of your profile folder.

* **`rxd login`** signs in with e-mail and password (typed without echo, never on the command line, never
  stored) and writes a `cli` session: the access token (one hour) **and** the refresh token, which `rxd` and
  `import rxd` use to renew it.
* **The app** signs in with the Supabase library and hands only the short-lived **access token** to the engine
  (`auth_session`); the engine writes it to the file as an `app` session, **without the refresh token**. The reason
  is the server's refresh-token rotation: if two programs both used one refresh token, the server would see a
  reused token and end the session for both (signing you out of the app). So each sign-in has exactly one
  program that renews it: the app for an `app` session, `rxd` for a `cli` session. When the app is closed and an
  `app` session has expired, `rxd` and `import rxd` **refuse it while the account server is reachable** and say
  "open the radarx-desktop app (it renews its own sign-in) or sign in again" (`rxd login` makes a separate session of
  its own); only when the server cannot be reached does the 30-day offline rule below apply.
* **Signing out** in the app, or `rxd logout`, removes the file for everyone; `rxd logout` also ends the
  session on the server.

Checking a session is the same on every surface (`python/engine/account/`): the access token (a signed JWT) is
verified **offline** against the project's public signing key (ES256). That key is **built into the program**
(`jwks_seed.py`; the same public document anyone can read at `<project>/auth/v1/.well-known/jwks.json`). A token with
a different `kid` makes the program download that document over HTTPS once (kept in memory for that run only, at most
every five minutes). **Nothing is ever read from a file in your folder as a key**: such a file is data you control, and
a session verified against it would prove nothing (an earlier version cached the document on disk; a user could then
mint their own session from data files alone, which the review found and which is fixed). Offline, a token with an
unknown key is simply refused, so after the project rotates its signing key one online run (or an app update that ships
the new key) is needed. Only the signature algorithms ES256 and
RS256 are accepted; issuer, audience and the `authenticated` role must match, and anonymous sessions do not count.
No secret is involved: the project URL and the publishable key are public values, the same as in
`src/lib/auth/config.ts`.

### The offline rule

A stored session is accepted **while the account server cannot be reached**, for **30 days after the last time the
server vouched for the account**: the `iat` (issued-at) claim of the newest token, which is signed and cannot be
edited. What applies when:

| Situation | Result |
|---|---|
| access token not yet expired | accepted (no network needed) |
| expired, `rxd login` session (has a refresh token) | renewed online; if the server **answers with its own refusal** (GoTrue's `invalid_grant`, `refresh_token_not_found`, ... ) the session is removed; if it cannot be reached, or a proxy / portal / outage answers something else (403, 404, 407, 408, 429, 5xx, HTML), the session is **kept** and the 30-day rule applies |
| expired, app session | the app renews its own token and hands it over. If the server is **reachable** (a short probe, at most 2 s) and nobody renewed it: refused ("open the app or sign in again"). If it is **not reachable**: accepted until 30 days after the token's `iat` |
| the app (engine WebSocket, desktop) with an expired token | accepted on the same 30-day rule (a ship has no network); the app renews and hands over a fresh token when it can |
| the public web service | **only an unexpired token** is accepted, and it is re-checked on every message: the service is always online, so no offline allowance applies; the browser renews its token about hourly and presents it again |

**Clocks.** The latest clock reading ever seen with a session is kept in the file (`high_water`) and the later of it and
the current clock is used, so setting the clock **backwards** does not extend the 30 days. Honest limits: (1) a clock that
is simply stopped at an old date (never advanced) cannot be told from a real one without a trusted time source, so it
*does* keep an offline session alive; (2) one wrong reading far in the future (a virtual machine resuming, a dead
clock battery) also refuses an offline session until the server vouches for the account again. Going online fixes it: a
sign-in, a refresh, or the app handing over a token that was issued just now (within 10 minutes of the local clock by the
server's own timestamp) and is newer than the one on file resets the mark. The number is `OFFLINE_GRACE_DAYS` in the
frontend and in `python/engine/account/config.py`; both are 30. At **start-up the app** applies the same rule to the
session in its own storage (`startup.ts`), without asking Supabase to renew it first: opening a laptop offline the next
day opens the app ("offline" in the status bar) and hands the stored token to the engine. Only a **definite** answer from
the server (its own session or user is gone) signs the user out; a 5xx, 429, a portal page or a timeout does not.

### The entitlement hook (for the day charging starts)

Today every valid account may use radarx-desktop. When charging starts, the decision "may this account use it?"
is made in **one function**, `entitlement_allows(claims)` in `python/engine/account/entitlement.py`, which every
surface goes through. Switching to subscriptions means changing the policy there to `subscription_required`: a
billing webhook (a server-side function; it needs the service-role key, which never belongs in this repository)
writes `subscription_status` into the user's `app_metadata`, Supabase copies `app_metadata` into the signed token,
and the check reads it with no extra network call. `python/tests/test_account_entitlement.py` shows the check with
a stand-in claim. **There is no billing, no Stripe and no plans table in this version.** Limits to remember then:
claims are as old as the token (an hour, plus the 30-day offline rule), and a check on the user's machine can
always be removed from source-available code.

## The activity log

The owner needs to know who uses radarx-desktop. Each signed-in account therefore sends a small **feature-level**
log. The rules (enforced in the program **and again in the database**, so a modified client cannot store more):

| Event | Detail (the only thing ever attached) |
|---|---|
| `app_start`, `sign_in`, `sign_out` | none |
| `command` | the name of the `rxd` command (`info`, `run`, ...) |
| `module_run` | the algorithm's name (a built-in one; your own plug-in is logged as `custom`) |
| `file_opened` | the format **family**: `NEXRAD`, `ODIM`, `CfRadial`, ... (never the name) |
| `export` | the format (`netcdf`, `zarr`, `png`, ...) |
| `mosaic_run`, `winds_run`, `animate_run`, `qpe_run` | the kind of run only |
| `error` | the error **class** only (`XrdInputError`), never the message |

Every event also carries the time, the surface (`app`, `cli`, `api` or `engine`), a random install id, the radarx
version and the operating-system family. **Never recorded:** file names or paths, coordinates or place names, radar
site identifiers, file contents, or any text a person typed. A value can only be a short plain word (letters,
digits, space, `_`, `+`, `-`), so a path, a URL or an address cannot fit.

How it is written: **one code path** (`python/engine/account/activity.py`) for `rxd`, the Python API and the
engine's WebSocket handling. Events go to a small local queue file (at most 500 events and 200 KB, oldest dropped)
and a background thread (or one bounded flush when a short command ends) sends them in batches, signed with *your own*
token. It never blocks or fails a command and is silent on network errors (they are retried later). The account
dialog lists the last 100 events your account has sent, read back from the database with your own token.

In the database (`supabase/migrations/20261021000000_activity.sql`): you can insert and read your own rows and
nothing else; nobody but the service role can change or delete them; the event names and detail keys are checked by a
trigger; at most 2000 events per account per 24 hours (the count is made under a per-user advisory lock, so
concurrent inserts cannot slip past it; the lock is tested for presence, its concurrent effect needs a real
multi-connection test that the single-connection test harness cannot do); timestamps are clamped. The accounts schema
is hardened in `20261022000000_accounts_hardening.sql`: the same lock for the synced-items quota, and at most **50
registered computers per account** (a 51st replaces the computer unused for the longest time). Deleting the account deletes its
log. The values are checked against **fixed word lists** before they are sent: a command name that is not an
`rxd` command, an export format, mosaic product or error class that is not one radarx knows, or an algorithm that is not
shipped with radarx is recorded as `other` (algorithms: `custom`), so a plug-in's class name, a product someone invented
or a typo can never identify a person or a project. `app_version` must look like a version number and `os` is one of
`macOS`, `Windows`, `Linux`, `Web`, `Other`. An event is attributed to the account of the connection that caused it
(never to whatever session file is current now), a 400 for one bad row costs only that row (the batch is split), and a
web visitor's events are dropped if their token expired. `purge_old_activity(days)` removes old rows (service role only; **scheduling it is left to the owner**, for
example monthly with `select public.purge_old_activity(365);`), and the view `public.activity_overview` gives the
owner one row per account (e-mail, first and last seen, counts per surface); only the service role can read it.
`supabase/tests/activity_rls.sql` and `accounts_hardening.sql` test this (the checks name the error they expect, so a
typo cannot pass as "refused").

## The web service

The public web engine (`docs/WEB.md`) has the same gate. It is controlled by `RADARX_WEB_REQUIRE_ACCOUNT`, which
**defaults to on**: a visitor's browser signs in with the web build, presents the session on its WebSocket with
`auth_session`, and until then the engine answers only `ping` and `get_web_info`. The web engine serves many users, so
it **never writes a session or an activity queue to disk**: the account lives in that connection's memory only, and
its activity events are sent with that visitor's own token. The operator can switch the gate off with
`RADARX_WEB_REQUIRE_ACCOUNT=0` (for a private deployment): then `auth_session` is answered `auth_ok` with
`bypass: true` and nothing is checked; **build the web app for that deployment with `RADARX_AUTH_DISABLED=1`** so its
screen does not ask for a sign-in either. An installer build ignores the engine variable. A web connection's token must
be unexpired, and the engine re-checks it on every message (see the table above).

## Development bypass

The test suite and developers need to run without an account: `RADARX_AUTH_DISABLED=1` switches the gate off for the
engine, `rxd` and `import rxd` when the code runs from a **source checkout**. An installer build (PyInstaller sets
`sys.frozen`) ignores it, as it ignores `RADARX_SUPABASE_URL` / `RADARX_SUPABASE_KEY` (which point a source checkout at
another project) and `RADARX_WEB_REQUIRE_ACCOUNT`. For the UI, build with `RADARX_AUTH_DISABLED=1 npm run dev` as
well. No activity is recorded while the bypass is on.

## Known ways around the gate

Said plainly, so nobody relies on more than it gives. The gate stops honest use without an account; it is not copy
protection. Everything below is real, including what is not worth fixing:

* **Edit the source** (it is available): remove the check, or `pip install` the engine and call modules such as
  `engine.reader` or `modules/` directly, which are not behind the API gate. This includes tools outside the gate
  (`tools/storm_clip`, `scripts/`).
* **`RADARX_AUTH_DISABLED=1`** in a source or `pip` install (installers ignore it). Likewise `RADARX_SUPABASE_URL` /
  `RADARX_SUPABASE_KEY` there, which point the checks at another project (and its keys).
* **Repackage a frozen engine** from edited source.
* **Talk to the engine without the app** and present a token that is not yours (the sidecar's random connection
  token keeps other programs out of an installed app's engine, but a modified app has it).
* **A stopped clock** keeps an offline session alive (see "Clocks" above); only a trusted time source (the server)
  fixes that, and offline there is none.
* **Within the offline rule**, a revoked or deleted account keeps working for up to 30 days when it cannot reach the
  server (a user can also simply block the account server), and up to an hour (the token's life) when it can.
  Signing out in the app while the engine is unreachable is remembered and delivered later, but until then the shared
  session file survives.
* **Tokens at rest**: the session file holds a one-hour access token (and, for `rxd login`, a refresh token) with mode
  0600 only; anything running as the same user can read it. Keychain storage is a follow-up.
* **The page's script policy keeps `'unsafe-inline'`** (`script-src`) in both the desktop and the web build because
  the UI is built with inline scripts and styles; a content-injection bug in the UI would therefore not be stopped by
  the CSP. `base-uri 'self'` and `form-action 'self'` are set to close the cheaper tricks. Not changed here.
* **Already-open `Volume` objects** keep their data after sign-out, and some read-only properties are not gated.
* **The activity log** is only as honest as the client that sends it, and it cannot be switched off while an account
  is used.
* **The public web service** does not limit work per account (any valid free account passes), and one account can be
  used from many places.

## Setting it up (owner, once)

1. **Supabase CLI**, in this repository, signed in as the owner of the project:

   ```bash
   supabase login
   supabase link --project-ref bfyimkgvnqiqedcwprpi      # asks for the database password
   supabase db push                                       # applies supabase/migrations (accounts, then activity)
   supabase config push                                   # applies supabase/config.toml (auth settings)
   ```

2. **E-mail sending: required before real users sign up.** Supabase's built-in mailer is for testing and sends only a
   handful of e-mails per hour for the whole project, far too few for sign-up and reset codes. Radarx sends its
   account e-mails with **Resend, and with Mailjet when Resend fails**, through Supabase's *Send Email* hook: the function
   in `supabase/functions/send-auth-email/` (its README has the exact commands). Supabase's own SMTP setting accepts only
   one provider, which is why the hook is used. While the hook is on, Supabase sends nothing itself and the templates in
   `supabase/templates/` are not used (the function builds the message; the wording is in `mail.ts`).
   - **Sending domain: `radarx.labshope.com`.** Mail is sent from an address on that subdomain
     (`no-reply@radarx.labshope.com`), which must be verified at **both** providers. In Cloudflare DNS that name has no
     mail records yet and labshope.com has no DMARC record. The exact steps (records, one SPF record per name, a
     monitoring DMARC policy, and why the website name must be a Worker **Custom Domain** and not a hand-made CNAME) are in
     the function's README. Do not touch labshope.com's existing SPF record.
   - **Resend:** create the account, add and verify `radarx.labshope.com` (it shows the DNS records), and create an API
     key with sending access only.
   - **Mailjet:** authenticate the same sending domain there. LIMS's existing sender on labshope.com is a different
     address and domain: leave it alone. Create a **separate** API key pair for Radarx so it can be revoked on its own.
   - **Secrets:** you type them with `supabase secrets set ...` (the README has the list); never paste them into a chat,
     a file in git or an issue.
   - **Simpler alternative with one provider:** *Authentication > Emails > SMTP Settings* with Resend's SMTP
     (host `smtp.resend.com`, user `resend`, the API key as password). Then there is no fallback.
   - Not verified here: the function was tested against mock providers only (no Deno on the build machine, no real
     Resend or Mailjet account, nothing deployed). Send a real test sign-up and read the function's logs
     (they show the provider that sent it, never the address or the code) before announcing anything.
   `site_url` in `config.toml` is deliberately an https address (the project page): `config push` writes it to
   the hosted project, and the e-mails carry codes, not links.

3. **E-mail codes.** The app asks for a 6-digit code (a desktop app has no web page to open a link in).
   `supabase config push` installs the templates in `supabase/templates/`. Without it, edit
   *Authentication > Emails* in the dashboard so both "Confirm sign up" and "Reset password" contain
   `{{ .Token }}`.

4. **Redirect URLs** (*Authentication > URL Configuration*): add `http://127.0.0.1:*/auth/callback`
   (the desktop sign-in return) and the address of the web build. **`supabase config push` replaces the redirect list
   with the one in `config.toml`: URLs you added only in the dashboard are erased**, so keep them in the file.

5. **Social sign-in** (*Authentication > Sign In / Providers*). Supabase's callback address is
   `https://bfyimkgvnqiqedcwprpi.supabase.co/auth/v1/callback`. Each provider needs its own app:

   | Provider | Create at | Notes |
   |---|---|---|
   | Google | Google Cloud Console > APIs & Services > Credentials > OAuth client (Web) | add the callback address above as an authorised redirect URI; configure the consent screen |
   | Facebook | developers.facebook.com > Create app > Facebook Login | add the callback under "Valid OAuth Redirect URIs"; the app must be switched to Live |
   | Apple | developer.apple.com > Identifiers > Services ID + a Sign in with Apple key | needs a paid Apple Developer account; the "secret" is a JWT that expires every 6 months at most |

   Paste each client id and secret into the dashboard (or the environment variables named in
   `supabase/config.toml`, never into git). A provider that is not switched on shows
   "This sign-in method is not switched on yet" and e-mail sign-in keeps working. **Buttons are hidden by
   default**: after a provider works, build with `VITE_OAUTH_PROVIDERS=google,apple,facebook` (only the ones
   that work) to show them.

6. **Activity retention.** Decide how long the activity log is kept (the privacy notice says so) and schedule
   `select public.purge_old_activity(365);` (for example with pg_cron or a monthly reminder). Nothing purges
   it automatically.

7. Rotate the publishable key if it ever leaks; it is public by design but can be revoked.

## For developers

```bash
supabase start -x studio,imgproxy,edge-runtime,logflare,vector,realtime,storage-api,supavisor,postgres-meta
# the local stack uses ports 553xx (see config.toml) so it never collides with other projects
```

`RADARX_AUTH_DISABLED=1 npm run dev` starts the UI without the account gate (UI tests and offline
builds only; release builds never set it); set the same variable for the engine (`RADARX_AUTH_DISABLED=1 npm run
tauri dev` passes it on) and the Python tests set it in `python/tests/conftest.py`. The tests of the gate itself switch
it on with the `gate_on` fixture and use a locally generated key, a mocked key document and a mocked account server:
they never touch the real network or a real account. The gate's decision logic is `src/lib/auth/gate.ts`
(unit-tested); handing the session to the engine is `src/lib/auth/engineSession.ts` (unit-tested) wired by
`engineLink.ts`; sign-in with a provider on desktop is `src/lib/auth/oauth.ts` plus the loopback
listener `src-tauri/src/oauth.rs`. The Python side is the package `python/engine/account/`
(`store`, `gotrue`, `tokens`, `core`, `entitlement`, `activity`, `commands`).

SQL tests (no Docker needed): run `supabase/migrations/*.sql` and `supabase/tests/*.sql` on an in-process Postgres
(PGlite) with a small stub of the Supabase roles, or on a local stack with `psql -f`.
