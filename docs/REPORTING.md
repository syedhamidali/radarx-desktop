# Reporting an issue

Radarx Desktop has one way to tell the Radarx team about a bug, an idea or a question from inside the app, and a
matching form on the website (radarx.labshope.com/report). Both end up in the same place: a private store that only the
Radarx team can read, and an e-mail to the team. This page says how to report, what is sent and what is stored.

## How to report

Open **Report an issue** from the About dialog, from the command palette (`Ctrl/Cmd+K`, type "report"), or from the
Account dialog. Choose what it is (something is wrong, an idea, a question), give it a title and say what happened.

Two things are sent every time, because a report is hard to use without them: **the Radarx version** and **the name
of your system** (for example "macOS arm (desktop)"). Your signed-in account is how the report reaches us, so we know
who wrote it; we may reply to the e-mail address of the account.

**Diagnostics are off.** If you tick *Include diagnostics* the report also carries the engine version and the last warnings and errors the engine gave the app in this session (at
most 200 lines, kept in memory only, never on disk). Before anything is shown or sent, those lines are scrubbed:

| Removed or masked | Shown as |
|---|---|
| paths inside your home folder start with `~`; any other absolute path keeps only its last two folders | `~/.../project/notes.txt` |
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

One record per report: the time, the kind, the title and description, the app version, the system name, the engine
version and diagnostics (if you included them), your account id (app reports only) and, for website reports, the contact
e-mail the visitor typed. See {doc}`PRIVACY` for the notice.

- **Who sees it:** the Radarx team, through its database and a notification e-mail (sent with Resend, or Mailjet when
  Resend fails; the same mailers as the sign-up codes). The contact e-mail and the diagnostics are never copied anywhere
  else. A signed-in user can read their own reports.
- **How long:** 24 months, then they are deleted.
- **Deletion:** write to the Radarx team and ask. There is no self-service delete.
- **If you delete your account:** your reports stay but are **no longer linked to you**; they keep whatever you wrote in
  them. Ask for them to be deleted if you want them gone.
- **The visitor's IP address** (website form) is never stored. The website form is protected by Cloudflare Turnstile and
  a rate limit that keeps a salted hash of the address per hour, deleted after three days.
- **Limits:** a signed-in account may send 10 reports in 24 hours; the website form accepts 5 per address per hour. The
  text is cleaned (control and invisible characters removed) and is never rendered as HTML.

## What is not covered

- **Scrubbing is pattern based** (see above). Free text can hold anything; that is what the preview is for.
- **Content.** The text is cleaned but not judged: a report can contain someone's personal data if the sender types it.
  Reports are read as untrusted text, and what should not be kept is deleted.
- **A determined spammer with many sources** can reach the daily cap of the website form; the cap is a safety net, not a
  defence. An app report needs a signed-in account ({doc}`ACCOUNTS`).
