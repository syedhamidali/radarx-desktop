# Accounts

radarx asks a new install to create a free account, then opens straight into the app on every later
start. An account is free, does not gate any feature, and is the base for features that need to know
who you are (cloud-synced recipes and workspaces, the list of your computers, and later billing).

**What is never behind the account:** the engine, the command line (`xrd`), your radar files and
results. They never leave your machine, and `xrd` needs no sign-in.

## What users see

- **First run:** "Create your free account" with e-mail (Google, Apple and Facebook buttons appear once they are set up, see below). The form asks for
  name, e-mail, password, **affiliation** (a university, agency or company, or "Independent") and what
  radarx is mostly used for. People who sign in with a provider are asked for the same two items once.
- **Every later start:** no screen at all. The session is kept and refreshed.
- **No network (ship, aircraft, field laptop):** the app opens for up to **30 days** after the last time
  the account was verified, and the status bar says "offline". After that it asks to reconnect.
- **Account dialog** (status bar, click your name): edit name, affiliation and use; see and remove your
  computers; sign out; delete the account (everything is removed, typed confirmation).
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

Row-level security is on for every table (see `supabase/migrations/`). The app holds only the
**publishable** key; the database password and the service-role key are never in this repository or the
app. There is no billing in this version.

## Setting it up (owner, once)

1. **Supabase CLI**, in this repository, signed in as the owner of the project:

   ```bash
   supabase login
   supabase link --project-ref bfyimkgvnqiqedcwprpi      # asks for the database password
   supabase db push                                       # applies supabase/migrations
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

5. Rotate the publishable key if it ever leaks; it is public by design but can be revoked.

## For developers

```bash
supabase start -x studio,imgproxy,edge-runtime,logflare,vector,realtime,storage-api,supavisor,postgres-meta
# the local stack uses ports 553xx (see config.toml) so it never collides with other projects
```

`RADARX_AUTH_DISABLED=1 npm run dev` starts the UI without the account gate (UI tests and offline
builds only; release builds never set it). The gate's decision logic is `src/lib/auth/gate.ts`
(unit-tested); sign-in with a provider on desktop is `src/lib/auth/oauth.ts` plus the loopback
listener `src-tauri/src/oauth.rs`.
