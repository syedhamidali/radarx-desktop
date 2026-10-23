# Accounts

Radarx Desktop needs an account to run. The account is **free**, there is no billing, and **any valid signed-in
account is enough**. A new install asks for one, then opens straight into the app on every later start. The same
account is required everywhere the program can be used:

| Surface | What happens without a signed-in account |
|---|---|
| the desktop app | the sign-in screen; the app does not open |
| the command line `rxd` (and the deprecated `xrd`) | a friendly message and **exit code 5**; only `login`, `logout`, `whoami`, `examples`, `completion`, `--help` and `--version` work |

One account and one sign-in serve both (see [How the app and the terminal share one sign-in](#how-the-app-and-the-terminal-share-one-sign-in)).
Your radar files and results still never leave your machine: the account is a gate, not a cloud. The gate stops
use without an account and supports the licence terms; it is not a security feature.

## What users see

- **First run:** "Create your free account" with e-mail (Google, Apple and Facebook buttons appear once they are
  set up). The form asks for name, e-mail, password, **affiliation** (a university, agency or company, or
  "Independent") and what Radarx is mostly used for. People who sign in with a provider are asked for the same two
  items once. The app asks for a 6-digit code that is sent to the e-mail address.
- **Every later start:** no screen at all. The session is kept and refreshed.
- **No network (ship, aircraft, field laptop):** Radarx works for up to **30 days** after the last time the
  account server vouched for the account (see [The offline rule](#the-offline-rule)), and the status bar says
  "offline". After that it asks to reconnect.
- **Account dialog** (status bar, click your name): edit name, affiliation and use; see and remove your
  computers; **Activity**: the list of the last 100 events your account has sent, exactly as stored (see
  [The activity log](#the-activity-log)); sign out; delete the account (everything is removed, typed confirmation).
  **Manage your account on the web** opens the account page of the website
  (<https://radarx.labshope.com/account.html>) in your browser, for when you are away from the app.
- **Terminal:** `rxd login` once (e-mail, password typed without echo), then every `rxd` command works.
  `rxd whoami` shows the account, `rxd logout` signs out. Creating the account itself happens in the app (or its
  sign-up screen), where the e-mail code is entered.
- **Sharing:** after an image or movie is saved a "Share" bar appears. It opens X, Facebook, LinkedIn or
  Bluesky with a caption filled in and shows the file so it can be attached; for Instagram it copies the
  caption and shows the file. Radarx never posts on anyone's behalf.

## What is stored

| Content | Who can read it |
|---|---|
| e-mail, password (only as a salted hash), sign-in provider | you; the Radarx team (account management) |
| name, affiliation, what you use Radarx for, terms version | you |
| your computers: a random install id, computer name, platform, app version, last seen | you |
| recipes, workspaces, looks and colour maps you chose to sync | you |
| which **features** the account used (no file names, places or data; see below) | you (your own entries); the Radarx team (per-account counts) |
| a bug, idea or question you chose to send ({doc}`REPORTING`); kept 24 months; unlinked from the account when it is deleted | you (your own, read only); the Radarx team |

Every table is protected so that an account can only read its own rows. The app and `rxd` hold only a public
client key; the database password and administrative keys are never on a user's machine. There is no billing in
this version. The full privacy notice is {doc}`PRIVACY`.

## How the app and the terminal share one sign-in

The session lives in **one file per user**, in the operating system's user configuration folder:

| System | Path |
|---|---|
| macOS | `~/Library/Application Support/radarx-desktop/session.json` |
| Windows | `%APPDATA%\radarx-desktop\session.json` |
| Linux | `$XDG_CONFIG_HOME/radarx-desktop/session.json` (default `~/.config/radarx-desktop/`) |

The file is readable by your user only, is replaced atomically and is locked while it is changed, so the app and a
terminal can run at the same moment. It holds tokens and the e-mail, **never the password**. On Windows the file
inherits the private permissions of your profile folder. Storing the tokens in the operating system's keychain
instead of a file is planned; until then the protection is the file mode and your user account.

* **`rxd login`** signs in with e-mail and password (typed without echo, never on the command line, never
  stored) and writes a terminal session: a one-hour access token **and** a refresh token, which `rxd` uses to
  renew it.
* **The app** signs in itself and hands only the short-lived **access token** to its analysis engine, without the
  refresh token. The reason is the server's refresh-token rotation: if two programs both used one refresh token,
  the server would see a reused token and end the session for both (signing you out of the app). So each sign-in
  has exactly one program that renews it: the app for an app session, `rxd` for a terminal session. When the app is
  closed and an app session has expired, `rxd` **refuses it while the account server is reachable** and says
  "open the radarx-desktop app (it renews its own sign-in) or sign in again" (`rxd login` makes a separate session of
  its own); only when the server cannot be reached does the 30-day offline rule below apply.
* **Signing out** in the app, or `rxd logout`, removes the file for everyone; `rxd logout` also ends the
  session on the server.

A session is checked the same way everywhere: the access token (a signed JWT) is verified **offline** against the
account project's public signing key, which is built into the program. Nothing is ever read from a file in your
folder as a key, because such a file is data you control and a session verified against it would prove nothing.
Only the signature algorithms ES256 and RS256 are accepted; issuer, audience and the `authenticated` role must
match, and anonymous sessions do not count. If the project rotates its signing key, one online run (or an app
update that ships the new key) is needed.

### The offline rule

A stored session is accepted **while the account server cannot be reached**, for **30 days after the last time the
server vouched for the account** (the signed issued-at time of the newest token, which cannot be edited). What
applies when:

| Situation | Result |
|---|---|
| access token not yet expired | accepted (no network needed) |
| expired, terminal session (has a refresh token) | renewed online; if the server **answers with its own refusal** the session is removed; if it cannot be reached, or a proxy / portal / outage answers something else, the session is **kept** and the 30-day rule applies |
| expired, app session | the app renews its own token and hands it over. If the server is **reachable** (a short probe) and nobody renewed it: refused ("open the app or sign in again"). If it is **not reachable**: accepted until 30 days after the token was issued |
| the app with an expired token | accepted on the same 30-day rule (a ship has no network); the app renews and hands over a fresh token when it can |

**What works offline.** Everything in the app and in `rxd` needs a signed-in account, **including opening and
processing local files**: no feature is open without one. Offline means "signed in earlier": the first sign-in on a
computer needs the network, and after it the program keeps working without one for the 30 days above. With no
session at all (never signed in, signed out, or past the 30 days) every request is answered "not signed in" together
with the reason (no sign-in, expired, revoked, offline too long) and what to do. A request the engine does not know
at all, for example a mistyped one, is answered as an unknown request, whether or not anyone is signed in, so a
typing mistake is never mistaken for a sign-in problem.

**Clocks.** The latest clock reading ever seen with a session is kept and the later of it and the current clock is
used, so setting the clock **backwards** does not extend the 30 days. Honest limits: (1) a clock that is simply
stopped at an old date (never advanced) cannot be told from a real one without a trusted time source, so it *does*
keep an offline session alive; (2) one wrong reading far in the future (a virtual machine resuming, a dead clock
battery) also refuses an offline session until the server vouches for the account again. Going online fixes it: a
sign-in, a refresh, or the app handing over a token that was issued just now resets the mark. At start-up the app
applies the same rule to its own stored session, without asking the server to renew it first: opening a laptop
offline the next day opens the app ("offline" in the status bar). Only a **definite** answer from the server (its
own session or user is gone) signs the user out; a server error, a rate limit, a portal page or a timeout does not.

## The activity log

Each signed-in account sends a small **feature-level** log, so the Radarx team knows who uses the program and which
functions matter. The rules are enforced in the program **and again in the database**, so a modified client cannot
store more:

| Event | Detail (the only thing ever attached) |
|---|---|
| `app_start`, `sign_in`, `sign_out` | none |
| `command` | the name of the `rxd` command (`info`, `run`, ...) |
| `module_run` | the algorithm's name (one of Radarx's own; anything else is logged as `custom`) |
| `file_opened` | the format **family**: `NEXRAD`, `ODIM`, `CfRadial`, ... (never the name) |
| `export` | the format (`netcdf`, `zarr`, `png`, ...) |
| `mosaic_run`, `winds_run`, `animate_run`, `qpe_run` | the kind of run only |
| `error` | the error **class** only, never the message |

Every event also carries the time, the surface (the app or the command line), a random install id, the Radarx
version and the operating-system family. **Never recorded:** file names or paths, coordinates or place names, radar
site identifiers, file contents, or any text a person typed. A value can only be a short plain word (letters,
digits, space, `_`, `+`, `-`), so a path, a URL or an address cannot fit.

How it works: events go to a small local queue file (at most 500 events and 200 KB, oldest dropped) and are sent in
batches in the background (or in one bounded flush when a short command ends), signed with *your own* token. It
never blocks or fails a command and is silent on network errors (they are retried later). The account dialog lists
the last 100 events your account has sent, read back from the database with your own token.

In the database you can insert and read your own rows and nothing else; nobody but the service can change or delete
them; the event names and detail keys are checked; at most 2000 events per account per 24 hours are accepted;
timestamps are clamped; and at most **50 registered computers per account** are kept (a 51st replaces the computer
unused for the longest time). Deleting the account deletes its log. The values are checked against **fixed word
lists** before they are sent: a command name that is not an `rxd` command, an export format, mosaic product or error
class that is not one Radarx knows, or an algorithm that is not shipped with Radarx is recorded as `other`
(algorithms: `custom`), so a product someone invented or a typo can never identify a person or a project. The
operating-system family is one of `macOS`, `Windows`, `Linux`, `Web`, `Other`. An event is attributed to the account
of the connection that caused it (never to whatever session file is current now). Old rows are removed periodically.

## Known limits

Said plainly, so nobody relies on more than the account gives. The gate stops honest use without an account; it is
not copy protection and not security against a determined user. In particular:

* A stopped clock keeps an offline session alive (see "Clocks" above); only a trusted time source fixes that, and
  offline there is none.
* Within the offline rule, a revoked or deleted account keeps working for up to 30 days when it cannot reach the
  server, and up to an hour (the token's life) when it can.
* The session file holds a one-hour access token (and, for `rxd login`, a refresh token) protected by file
  permissions only; anything running as the same user can read it.
* The activity log is only as honest as the program that sends it, and it cannot be switched off while an account
  is used.
