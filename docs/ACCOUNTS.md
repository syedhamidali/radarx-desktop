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
  `app` session has expired, `rxd` says so and asks for `rxd login` (a separate session of its own).
* **Signing out** in the app, or `rxd logout`, removes the file for everyone; `rxd logout` also ends the
  session on the server.

Checking a session is the same on every surface (`python/engine/account/`): the access token (a signed JWT) is
verified **offline** against the project's public signing key (ES256; the key document is
`<project>/auth/v1/.well-known/jwks.json`, cached on disk, a copy ships in the code, and a token signed with an
unknown key triggers one re-download at most every five minutes). Only the signature algorithms ES256 and
RS256 are accepted; issuer, audience and the `authenticated` role must match, and anonymous sessions do not count.
No secret is involved: the project URL and the publishable key are public values, the same as in
`src/lib/auth/config.ts`.

### The offline rule

A stored session is accepted for **30 days after the last time the server vouched for the account**, that is the
`iat` (issued-at) claim of the newest token, which is signed and cannot be edited. An expired token
(they last an hour) is renewed online when that is possible; when the server cannot be reached, the session still
counts until 30 days have passed. A clock set backwards does not help: the latest clock reading ever seen is kept
in the session file and the later of it and the current clock is used; it is trusted again only after the server has
vouched for the account anew (a refresh or a sign-in). The number is `OFFLINE_GRACE_DAYS` in the frontend and in
`python/engine/account/config.py`; both are 30.

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
trigger; at most 2000 events per account per 24 hours; timestamps are clamped. Deleting the account deletes its
log. `purge_old_activity(days)` removes old rows (service role only; **scheduling it is left to the owner**, for
example monthly with `select public.purge_old_activity(365);`), and the view `public.activity_overview` gives the
owner one row per account (e-mail, first and last seen, counts per surface); only the service role can read it.
`supabase/tests/activity_rls.sql` tests all of this.

## The web service

The public web engine (`docs/WEB.md`) has the same gate. It is controlled by `RADARX_WEB_REQUIRE_ACCOUNT`, which
**defaults to on**: a visitor's browser signs in with the web build, presents the session on its WebSocket with
`auth_session`, and until then the engine answers only `ping` and `get_web_info`. The web engine serves many users, so
it **never writes a session or an activity queue to disk**: the account lives in that connection's memory only, and
its activity events are sent with that visitor's own token. The operator can switch the gate off with
`RADARX_WEB_REQUIRE_ACCOUNT=0` (for a private deployment); an installer build ignores that variable.

## Development bypass

The test suite and developers need to run without an account: `RADARX_AUTH_DISABLED=1` switches the gate off for the
engine, `rxd` and `import rxd` when the code runs from a **source checkout**. An installer build (PyInstaller sets
`sys.frozen`) ignores it, as it ignores `RADARX_SUPABASE_URL` / `RADARX_SUPABASE_KEY` (which point a source checkout at
another project) and `RADARX_WEB_REQUIRE_ACCOUNT`. For the UI, build with `RADARX_AUTH_DISABLED=1 npm run dev` as
well. No activity is recorded while the bypass is on.

## Known ways around the gate

Said plainly, so nobody relies on more than it gives:

* Edit the source (it is available): remove the check, or `pip install` the engine and call modules such as
  `engine.reader` directly, which are not behind the API gate.
* Set `RADARX_AUTH_DISABLED=1` in a source or `pip` install (installers ignore it).
* A frozen engine can be repackaged from edited source.
* Run a copy of the engine that never shows the gate, or talk to the engine's WebSocket without the app: the gate is
  per connection, and a modified client could present a stolen token (the sidecar's own random connection token
  already keeps other programs out of an installed app's engine).
* Within the offline rule, a revoked or deleted account keeps working for up to 30 days when it cannot reach the
  server, and up to an hour (the token's life) when it can.
* Already-open `Volume` objects keep their data after sign-out, and some read-only properties are not gated.
* The activity log is only as honest as the client that sends it.

## Setting it up (owner, once)

1. **Supabase CLI**, in this repository, signed in as the owner of the project:

   ```bash
   supabase login
   supabase link --project-ref bfyimkgvnqiqedcwprpi      # asks for the database password
   supabase db push                                       # applies supabase/migrations (accounts, then activity)
   supabase config push                                   # applies supabase/config.toml (auth settings)
   ```

2. **E-mail codes.** The app asks for a 6-digit code (a desktop app has no web page to open a link in).
   `supabase config push` installs the templates in `supabase/templates/`. Without it, edit
   *Authentication > Emails* in the dashboard so both "Confirm sign up" and "Reset password" contain
   `{{ .Token }}`.

3. **Redirect URLs** (*Authentication > URL Configuration*): add `http://127.0.0.1:*/auth/callback`
   (the desktop sign-in return) and the address of the web build.

4. **Social sign-in** (*Authentication > Sign In / Providers*). Supabase's callback address is
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

5. **Activity retention.** Decide how long the activity log is kept (the privacy notice says so) and schedule
   `select public.purge_old_activity(365);` (for example with pg_cron or a monthly reminder). Nothing purges
   it automatically.

6. Rotate the publishable key if it ever leaks; it is public by design but can be revoked.

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
