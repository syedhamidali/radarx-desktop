# Privacy notice (draft: still needs a lawyer's review before the first public release)

Radarx is a desktop program with a command line (`rxd`) and a browser version. **An account is required to use any of them.** This notice covers that account and what radarx records
about its use. It is a plain-language draft and has **not** been reviewed by a lawyer; it must be before a
public release.

**What we collect.** E-mail address, a password (stored only as a salted hash by our provider, Supabase), or the
identity returned by Google, Apple or Facebook if you use them (name, e-mail, picture). Your name, affiliation and
what you use radarx for, as you enter them. A random id, the name, platform and radarx version of each computer you
sign in from, and when it was last used.

**Activity: which features you use.** While you are signed in, radarx records short feature-level events so the
Radarx team knows who uses it and which functions matter. Each event is one of: the app was started, you signed in or
out, an `rxd` command was run (its name only, for example `run`), an algorithm was run (its name, for example
`radarx_hid`; an algorithm that is not one of Radarx's own is recorded only as `custom`), a file was opened (the format *family*,
for example `NEXRAD`), a file was exported (the format), a mosaic, wind analysis, animation or rainfall estimate was
run (that it happened, with the kind of run), or an error occurred (the error class only). Every value comes
from a fixed list of words radarx knows (its own commands, formats, products, algorithms and error classes); anything
else is recorded only as `other` or `custom`. Each event also carries the time, where it came
from (the app or the terminal), a random install id, the radarx version number and your operating-system family
(macOS, Windows, Linux, or Web for the browser version). An event is recorded only for the account that caused it. The program and the database both refuse anything else. **You can see every entry:**
*Account > Activity* lists the last 100 events your account has sent. Activity cannot be switched off while you use an
account, because it is the reason for the account being required; the list above is all of it.

**Reports you choose to send.** *Report an issue* (in the app) and the form on the radarx website send a report **only
when you press send**. A report holds what you typed (kind, title, description), the radarx version and the name of
your system (for example "macOS arm (desktop)"), and: for a report from the app, your account id (so we know who wrote
it; we may answer to your account's e-mail address); for a report from the website, the e-mail address you chose to
give, if any. **Diagnostics are optional and off by default**: when you tick the box the report also carries the last
warnings and errors the engine gave the app in that session (at most 200 lines, never on disk). Before they are shown or
sent, folder paths, the names of data files, e-mail addresses, IP addresses, keys and tokens, and your user and machine
name when known are removed or masked, and the dialog shows the complete message first (the scrubbing is pattern based
and not perfect, which is why you see it). Radar files and locations are never part of it. A report is read by the
Radarx team (database dashboard and an e-mail notification, sent through Resend or Mailjet).
Reports are kept for **24 months**, then deleted. You can ask for a report to be deleted by writing to the Radarx team.
If you delete your account the reports you sent **stay but are no longer linked to you**. The website form is protected by
Cloudflare Turnstile (it receives what the widget sends, under Cloudflare's own notice) and by a rate limit that keeps
only a **salted hash** of your IP address, per hour, for three days; your IP address itself is never stored by radarx.
Details: {doc}`REPORTING`.

**What we do not collect.** Radar files and their contents, **file names or paths**, locations you look at
(coordinates or place names), radar site identifiers, results, anything you type (apart from a report you choose to
send, above), or the content of your work. They stay on your computer. (The public web version necessarily receives the files you upload to process them; it keeps
them only for your session and deletes them when it ends.)

**Why.** To let you sign in on several computers, keep what you choose to sync, understand which communities use
radarx and which features matter so we build the right things, know who is using it, and contact you about your
account. We do not sell personal data and we do not show your data to other users.

**How long.** Activity events are kept for up to one year; older ones are removed periodically. Account data
is kept until you delete the account.

**Who processes it.** Supabase (database, sign-in and the report function, region Mumbai); Resend and Mailjet (sending
sign-up codes and the report notification to the Radarx team); Cloudflare (Turnstile on the website's report form); and, if you
choose them, Google, Apple or Facebook as sign-in providers. Sharing a picture or movie opens the social network yourself; radarx sends them
nothing.

**Your rights.** Open *Account* and edit your details, remove computers, or delete the account: the account and every
row above, **including the activity**, are removed (reports you sent are kept without a link to you, see above; ask for
them to be deleted if you want them gone). For anything else, write to the Radarx team through *Report an issue* in the app or the form at radarx.labshope.com/report.

**Offline.** After you have signed in, radarx works for up to 30 days without contacting the server. Activity events
are then kept on your computer (at most 500 small events) and sent when the connection returns.

Version 2026-10-21 (previous version 2026-10, which said nothing about activity: everyone is asked to accept again).
The rules above were tightened after that version was drafted (fixed word lists, no free text in the version or system
fields); they only narrow what is recorded, so the version number is unchanged.
