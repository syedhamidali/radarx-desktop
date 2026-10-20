# Privacy notice (draft: still needs a lawyer's review before the first public release)

radarx is a desktop program, with a command line (`rxd`), a Python library (`import rxd`) and an optional web
version. **An account is required to use any of them.** This notice covers that account and what radarx records
about its use. It is a plain-language draft and has **not** been reviewed by a lawyer; it must be before a
public release.

**What we collect.** E-mail address, a password (stored only as a salted hash by our provider, Supabase), or the
identity returned by Google, Apple or Facebook if you use them (name, e-mail, picture). Your name, affiliation and
what you use radarx for, as you enter them. A random id, the name, platform and radarx version of each computer you
sign in from, and when it was last used.

**Activity: which features you use.** While you are signed in, radarx records short feature-level events so the
owner knows who uses it and which functions matter. Each event is one of: the app was started, you signed in or
out, an `rxd` command was run (its name only, for example `run`), an algorithm was run (its name, for example
`radarx_hid`; a plug-in you wrote yourself is recorded only as `custom`), a file was opened (the format *family*,
for example `NEXRAD`), a file was exported (the format), a mosaic, wind analysis, animation or rainfall estimate was
run (that it happened, with the kind of run), or an error occurred (the error class only). Every value comes
from a fixed list of words radarx knows (its own commands, formats, products, algorithms and error classes); anything
else, such as an algorithm you wrote yourself or an error class from your own plug-in, is recorded only as `other` or
`custom`. Each event also carries the time, where it came
from (app, terminal, Python or engine), a random install id, the radarx version number and your operating-system family
(macOS, Windows, Linux, or Web for the browser version). An event is recorded only for the account that caused it. The program and the database both refuse anything else. **You can see every entry:**
*Account > Activity* lists the last 100 events your account has sent. Activity cannot be switched off while you use an
account, because it is the reason for the account being required; the list above is all of it.

**Access tokens (only if you create one).** For batch jobs, cron, CI and clusters you may create *access tokens* in
*Account > Access tokens*. We store the name you give it, a one-way fingerprint (SHA-256) of the token (never the token
itself: it is shown to you once), the first four characters as a hint, when it was created, when it expires, when it
was last used and when you revoked it. A job using a token sends the same feature-level activity events as above. Tokens
are removed with the account; revoked and expired ones stay on record for audit until the owner clears them.
*(Adding this did not change the terms version: it collects nothing unless you create a token. The owner may bump it.)*

**What we do not collect.** Radar files and their contents, **file names or paths**, locations you look at
(coordinates or place names), radar site identifiers, results, anything you type, or the content of your work. They
stay on your computer. (The public web version necessarily receives the files you upload to process them; it keeps
them only for your session and deletes them when it ends, see the web documentation.)

**Why.** To let you sign in on several computers, keep what you choose to sync, understand which communities use
radarx and which features matter so we build the right things, know who is using it, and contact you about your
account. We do not sell personal data and we do not show your data to other users.

**How long.** Activity events are kept for up to one year; the owner removes older ones periodically. Account data
is kept until you delete the account. *(The owner must schedule the clean-up; see the account documentation.)*

**Who processes it.** Supabase (database and sign-in, region Mumbai) and, if you choose them, Google, Apple or
Facebook as sign-in providers. Sharing a picture or movie opens the social network yourself; radarx sends them
nothing.

**Your rights.** Open *Account* and edit your details, remove computers, or delete the account: the account and every
row above, **including the activity**, are removed. For anything else, write to the project owner (see the
repository).

**Offline.** After you have signed in, radarx works for up to 30 days without contacting the server. Activity events
are then kept on your computer (at most 500 small events) and sent when the connection returns.

Version 2026-10-21 (previous version 2026-10, which said nothing about activity: everyone is asked to accept again).
The rules above were tightened after that version was drafted (fixed word lists, no free text in the version or system
fields); they only narrow what is recorded, so the version number is unchanged.
