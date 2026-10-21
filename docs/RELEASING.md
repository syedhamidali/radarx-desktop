# Releasing

How a version gets from a tag to people's computers. Read this together with [UPDATES.md](UPDATES.md) (what the app
does) and [BUNDLING.md](BUNDLING.md) (how the installers are built).

## The idea in one picture

```
git tag vX.Y.Z  ──▶  Release workflow (4 build jobs)  ──▶  draft GitHub release (working copy)
                                │
                                └─ downloads job (needs the R2 secrets)
                                      │   rewrite_latest.py   check latest.json, point urls at the download host,
                                      │                       keep signatures, write SHA256SUMS
                                      │   upload_version.py   upload to radarx/vX.Y.Z/   (immutable, a CANDIDATE)
                                      ▼   promote.py verify   read it back, check every SHA-256
                          downloads.labshope.com/radarx/vX.Y.Z/…      nothing is live yet
                                │
              you check it, then run  Actions ▶ Promote release ▶ tag vX.Y.Z
                                ▼
          radarx/latest.json   (what installed apps read)      ◀── written only by promote
          radarx/releases.json (what the website reads)        ◀── written only by promote
```

**Publishing a release means running the Promote release workflow.** Building a tag never changes what users see.

## Layout of the bucket

| Key | Content | `Cache-Control` | Written by |
|---|---|---|---|
| `radarx/v<version>/<installers>` | `.dmg`, `-setup.exe`, `.deb`, `.rpm`, the updater payloads (`*.app.tar.gz`) and the `.sig` of each | `public, max-age=31536000, immutable` | the `downloads` job |
| `radarx/v<version>/radarx-movie-pack-<version>-<os>-<arch>.zip` (+ `.manifest.json` sidecar) | the optional movie components the installed app downloads on demand ([BUNDLING.md](BUNDLING.md)); four per release | same, immutable | the `downloads` job |
| `radarx/v<version>/latest.json` | the **candidate** update manifest of that version | same, immutable | the `downloads` job |
| `radarx/v<version>/SHA256SUMS` | `sha256sum`-style list of every file in the folder (including `latest.json`) | same | the `downloads` job |
| `radarx/v<version>/PROMOTION.json` | `{"status": "promoted" \| "withdrawn", "version": "…"}` | same | promote |
| `radarx/latest.json` | the **live** update manifest the app reads | `no-cache` | promote **only** |
| `radarx/releases.json` | the index the website reads | `no-cache` | promote **only** |

Version folders are never edited after promotion and nothing in the bucket is ever deleted by these scripts. The
public address of the bucket is `https://downloads.labshope.com`.

## Releasing a version, step by step

1. Bump the version in `package.json`, `src-tauri/Cargo.toml` and `src-tauri/tauri.conf.json`, merge, and tag:

   ```bash
   git tag -a v0.3.1 -m "What is new in 0.3.1
   - ..."
   git push origin v0.3.1
   ```

   The tag message becomes the release notes in the update dialog and on the website.
2. The **Release** workflow builds the four installers and creates the draft GitHub release, as before; the `wheel` job
   attaches the Python wheel and the four `movie-pack` jobs attach the movie components packs with their `.manifest.json`
   sidecars (same runners and Python as the engine). When the four R2 secrets exist, its last job (**Upload candidate to
   the download host**) uploads the candidate (everything on the draft release: installers, signatures, wheel, packs) and
   verifies it. Without the secrets it prints a notice and succeeds (forks, or before the set-up below).
3. **Check the candidate** (nothing is live yet):

   ```bash
   V=v0.3.1
   curl -fsS https://downloads.labshope.com/radarx/$V/SHA256SUMS
   curl -fsS https://downloads.labshope.com/radarx/$V/latest.json | python3 -m json.tool | head -40
   curl -fLO https://downloads.labshope.com/radarx/$V/Radarx_0.3.1_aarch64.dmg
   shasum -a 256 Radarx_0.3.1_aarch64.dmg        # must equal the line in SHA256SUMS
   ```

   Every `url` in the candidate manifest must start with `https://downloads.labshope.com/radarx/v0.3.1/`; open the
   installers once on a clean machine. `SHA256SUMS` must also list the four `radarx-movie-pack-0.3.1-*.zip` files and
   their `.manifest.json` sidecars (a pack without its sidecar makes the promote refuse). To try the download in the
   installed app before promoting, point the engine at the candidate folder's host with
   `RADARX_MOVIE_PACK_BASE_URL` and a local copy of `releases.json` that lists it, or simply promote and test.
4. **Promote**: Actions ▶ *Promote release* ▶ *Run workflow* ▶ tag `v0.3.1`. It re-checks the candidate (SHA-256 of
   every file, each `.sig` against the manifest, urls under the right folder), then writes the version's `PROMOTION.json`,
   `radarx/releases.json` and, last, `radarx/latest.json`. Finally it fetches `https://downloads.labshope.com/radarx/latest.json`
   and fails if that address does not serve the version. Running it again does nothing new (same bytes).
5. Installed apps now offer the update at their next check; the website's download block shows the new version once it reads
   `releases.json`.
6. Publish or keep the GitHub release as you like; the draft stays as a private archive of the build.

### Rolling back

Run *Promote release* with an **older** tag and **rollback** ticked. `radarx/latest.json` becomes that version's manifest,
every newer version is marked `withdrawn` (it disappears from `releases.json`; its files stay in the bucket) so that the
app and the website agree. Promoting an older version without the rollback tick, or "rolling back" to a newer one, is
refused. To go forward again, promote the newer version normally. A rollback does not downgrade people who already updated
(the updater only moves forward); it stops new installs and updates from receiving the bad version.

### What the scripts refuse

`rewrite_latest.py`, `upload_version.py` and `promote.py` (in `scripts/release/`, standard library only, tested in
`python/tests/test_release_scripts.py`) stop with `REFUSED: …` and change nothing when: a platform has no signature or a
signature that is not base64; a url is not https; the manifest version is not the tag; `notes` or `pub_date` are missing;
a required platform (`darwin-aarch64`, `darwin-x86_64`, `windows-x86_64`) is missing; a referenced file or its `.sig` is
missing, or a `.sig` differs from the manifest's signature; a file is not in `SHA256SUMS` or does not match it (a tampered or
half-uploaded file); an already promoted version is uploaded again; an older version is promoted without rollback.
Signatures are copied exactly as tauri-action wrote them; the scripts never compute or edit one.

## The `releases.json` contract

This is what the website reads (`GET https://downloads.labshope.com/radarx/releases.json`, cross-origin, no cookies). The
file is rebuilt from scratch by every promote, is deterministic (no timestamps; a release's date is the `pub_date` of its
manifest) and has sorted keys. Schema version 1:

```json
{
  "schema": 1,
  "product": "radarx",
  "latest": "0.3.1",
  "releases": [
    {
      "version": "0.3.1",
      "date": "2026-11-02T09:14:07.000Z",
      "notes": "What is new in 0.3.1\n- …",
      "assets": [
        {
          "os": "macos",
          "arch": "aarch64",
          "kind": "dmg",
          "name": "Radarx_0.3.1_aarch64.dmg",
          "size": 118234567,
          "sha256": "5cded49153520ddae78fdb2c49ab87b05fa153ca18a7332f71a9395b9a673f38",
          "url": "https://downloads.labshope.com/radarx/v0.3.1/Radarx_0.3.1_aarch64.dmg"
        }
      ],
      "components": [
        {
          "kind": "movie-pack",
          "name": "radarx-movie-pack-0.3.1-macos-aarch64.zip",
          "os": "macos",
          "arch": "aarch64",
          "python": "cpython-312",
          "size": 60090257,
          "sha256": "…",
          "url": "https://downloads.labshope.com/radarx/v0.3.1/radarx-movie-pack-0.3.1-macos-aarch64.zip",
          "contents": [{"name": "playwright", "version": "1.64.0", "license": "Apache-2.0"}, {"name": "av", "version": "19.0.2", "license": "BSD-3-Clause"}]
        }
      ]
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `schema` | integer, `1`. A reader should ignore a file with a higher number it does not know. |
| `product` | `"radarx"` |
| `latest` | the live version (equals `releases[0].version`), or `null` when nothing has been promoted |
| `releases` | promoted versions, **newest first** (semantic-version order; a pre-release is below its release) |
| `releases[].version` | `X.Y.Z` or `X.Y.Z-pre.release`, without a `v` |
| `releases[].date` | RFC 3339 date of the build (the manifest's `pub_date`) |
| `releases[].notes` | the tag message, markdown text (treat as untrusted text, never as HTML) |
| `assets[].os` | `macos`, `windows` or `linux` |
| `assets[].arch` | `aarch64` (Apple silicon, Windows/Linux ARM) or `x86_64` (Intel/AMD) |
| `assets[].kind` | `dmg`, `exe` (NSIS installer), `msi`, `deb`, `rpm`, `appimage`, `zip` |
| `assets[].name` | file name as stored |
| `assets[].size` | bytes |
| `assets[].sha256` | lowercase hex, equals the line in that version's `SHA256SUMS` (checked at promote time) |
| `assets[].url` | the download address; use it as it is |
| `releases[].components` | the optional components of that release (additive; `[]` when there are none). The website ignores it; the installed app reads it to find its movie components pack: the entry whose `kind` is `movie-pack` and whose `os`, `arch` and `python` (the engine's CPython tag) match. `name`, `size`, `sha256`, `url` as for assets; `contents` lists what is inside with versions and licences (from the pack's sidecar manifest). |

Assets are the installers a person downloads, sorted by `os` (macos, windows, linux), `arch`, `kind`, `name`. The updater
payloads (`*.app.tar.gz`), `.sig` files, `latest.json`, `SHA256SUMS`, the wheel and the movie components packs are not
listed as assets (the packs are under `components`, see above). To show the main download buttons:
take `releases[0]`, then pick the asset with the matching `os`, `arch` and `kind` (`dmg` for macOS, `exe` for Windows,
`deb` / `rpm` for Linux). A client must cope with a missing combination (offer the others) and with the request failing
(show a plain link to the directory of installers or a retry; never block the page). The file is served with
`Cache-Control: no-cache`, so a promote shows up on the next request.

A complete example is built from a synthetic release by `test_index_entries` in `python/tests/test_release_scripts.py`;
`python scripts/release/build_index.py --bucket-dir DIR` prints the index of any bucket directory.

## Owner set-up (once)

These steps need your Cloudflare and GitHub accounts; nothing here is done by the code, and no key ever has to be sent
to anyone. Do them in this order.

1. **Create the R2 bucket.** Cloudflare dashboard ▶ R2 ▶ *Create bucket* ▶ name it (for example `radarx-downloads`),
   location *Automatic*. Keep it private (do **not** enable the `r2.dev` public URL; the custom domain below is the public
   address). Note your **Account ID** (R2 overview page, right-hand side).
2. **Attach the custom domain** `downloads.labshope.com`: the bucket ▶ *Settings* ▶ *Custom Domains* ▶ *Add* ▶
   `downloads.labshope.com`. The `labshope.com` zone must be on Cloudflare. Then **test it** (the LIMS application answers
   every unknown `*.labshope.com` name through a wildcard route, and that must not catch this one):

   ```bash
   # upload a harmless probe with the Cloudflare dashboard (Objects ▶ Upload) as radarx/_probe.txt containing: probe
   curl -si https://downloads.labshope.com/radarx/_probe.txt
   ```

   Expected: `HTTP/2 200`, `content-type: text/plain`, body `probe`, `server: cloudflare`. If you get the LIMS page, a
   redirect, or `404` from LIMS: an exact custom domain should take precedence, but a wildcard **Worker route**
   (`*.labshope.com/*`) runs before it. Fix: Workers & Pages ▶ the LIMS worker ▶ *Settings* ▶ *Domains & Routes*, or the
   zone's *Workers Routes*: add a route `downloads.labshope.com/*` with the Worker set to **None**; more specific routes win.
   A wildcard **DNS record** (`*`) needs no change, because the record R2 adds for `downloads` is exact. If neither works,
   use another host name outside the wildcard and change it in the three places listed in
   [UPDATES.md](UPDATES.md#where-the-addresses-live-the-single-places-to-change). Delete the probe afterwards.
3. **CORS** (the website fetches `releases.json` from another origin): the bucket ▶ *Settings* ▶ *CORS Policy* ▶ *Add* ▶
   JSON:

   ```json
   [
     {
       "AllowedOrigins": ["https://radarx.labshope.com"],
       "AllowedMethods": ["GET", "HEAD"],
       "AllowedHeaders": ["*"],
       "ExposeHeaders": ["ETag"],
       "MaxAgeSeconds": 3600
     }
   ]
   ```

   Test: `curl -si -H 'Origin: https://radarx.labshope.com' https://downloads.labshope.com/radarx/_probe.txt | grep -i access-control`
   shows `access-control-allow-origin: https://radarx.labshope.com`.
4. **Create an API token**: R2 ▶ *Manage R2 API Tokens* ▶ *Create API token* ▶ permission **Object Read & Write** ▶ *Specify
   bucket(s)*: only this bucket ▶ create. Cloudflare shows the **Access Key ID** and **Secret Access Key** once; copy them
   straight into step 5 and nowhere else (not a chat, a ticket or a file).
5. **Add the GitHub Actions secrets** (repository ▶ Settings ▶ Secrets and variables ▶ Actions, or `gh secret set NAME`,
   which asks for the value without putting it in your shell history). Four new secrets, next to the two signing secrets
   that already exist (`TAURI_SIGNING_PRIVATE_KEY`, `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`):

   | Secret | Value |
   |---|---|
   | `R2_ACCOUNT_ID` | the Cloudflare account ID |
   | `R2_ACCESS_KEY_ID` | the token's Access Key ID |
   | `R2_SECRET_ACCESS_KEY` | the token's Secret Access Key |
   | `R2_BUCKET` | the bucket name |

6. **Run a tag build.** The tag `v0.3.0` exists and its release build was cancelled: re-run the *Release* workflow for it
   (Actions ▶ Release ▶ *Re-run all jobs* on that run, or push the next tag). Confirm the **Upload candidate to the download
   host** job is green and the files appear under `radarx/v0.3.0/`. Check it as in step 3 of the procedure above. Nothing is
   live yet, so this is safe at any time. (Run *Promote release* with **dry run** ticked to repeat all the checks without
   writing.)
7. **Promote it** when you are satisfied: *Promote release* ▶ `v0.3.0`. Then `curl -s https://downloads.labshope.com/radarx/latest.json`
   and `…/releases.json` and confirm the version, the urls and the signatures.
8. **Test the app's update check against it**: build an app that believes it is older, and let it update from the live
   address (this is the real path: the real public key verifies the real signature):

   ```bash
   # version 0.0.1 so that anything on the server is "newer"; same endpoint and public key as a release
   echo '{"version":"0.0.1"}' > /tmp/older.json
   CI=true npm run tauri build -- --config src-tauri/tauri.bundle.macos.conf.json --config /tmp/older.json --bundles app
   ```

   Open that app ▶ Settings ▶ Updates ▶ *Check now*: it must offer v0.3.0, and *Install and restart* must succeed. Do it on a
   spare machine, because it really installs. (Seeing what the updater asks without installing:
   [UPDATES.md](UPDATES.md#testing-against-a-local-server).)

## When the repository goes private

The repository stays public for now. When you switch it, do these in order:

- [ ] **Promote the current release first** so that `https://downloads.labshope.com/radarx/latest.json` serves it (the app falls
  back to GitHub only while that address answers 404).
- [ ] **Remove the GitHub fallback endpoint** from `plugins.updater.endpoints` in `src-tauri/tauri.conf.json` (the second line;
  exact edit in [UPDATES.md](UPDATES.md#the-temporary-github-fallback)), and ship that in the next release.
- [ ] **Switch the website's download block** from the GitHub API to `releases.json` on the downloads host (contract above),
  and test it from `https://radarx.labshope.com` (CORS).
- [ ] **Check that no workflow or document needs the public repository**: `.github/workflows/*` (the Release workflow reads its own
  draft with `GITHUB_TOKEN`, which keeps working), issue and PR templates and docs links that point at the repository
  (`docs/conf.py` edit-on-GitHub links, `.github/ISSUE_TEMPLATE/config.yml`), `README.md` badges and links, the
  `supabase/config.toml` `site_url`, and Read the Docs (it needs access to the private repository).
- [ ] **Actions minutes**: private repositories on the free plan get a monthly allowance (see the estimate below) and macOS
  minutes count ten times. Decide before switching.
- [ ] **Codecov** (`ci.yml` uploads coverage with a token): check the private-repository plan and the upload token.
- [ ] **Read the Docs**: re-import or re-authorise the project for a private repository (a paid feature on the public service),
  or host the documentation elsewhere.
- [ ] **Download links in the repository's own files**: `README.md` and `docs/installation.md` link to the GitHub
  "latest release" page and `README.md` has a release badge; point them at `https://radarx.labshope.com/#download`.
- [ ] **The old public releases disappear for the public**: v0.1.x to v0.2.1 on GitHub Releases stop being downloadable by
  anyone without access. Copy any you still want to offer into the bucket (`radarx/v0.2.1/…`, with a `SHA256SUMS`; they have
  no update manifest, so they are not promotable) before switching, or accept that they are gone.
- [ ] **Running copies keep working.** Installed apps do not need the repository. Apps built with only the GitHub address
  can no longer check for updates (the check fails quietly with "Could not check"); those users get the new version from
  the website's download page. v0.3.0 and later also carry the downloads address.
- [ ] **The private draft releases stay** as an archive of every build (GitHub keeps them while the repository exists).
- [ ] **Links in already-published documents** (release notes, social posts) that point at the source host will stop
  working for the public.

## GitHub Actions minutes for a private repository

A private repository on GitHub's free plan gets about **2,000 Actions minutes a month**; a macOS minute counts as **10**
minutes and a Windows minute as **2**. (Public repositories pay nothing, which is why this has not mattered so far.) This is
an estimate from this repository's own history, not a decision; the choice is yours.

**What the workflows run** (from `.github/workflows/`):

| Workflow | When | Jobs and runners |
|---|---|---|
| CI | every push to `main`, every pull-request push | Python on ubuntu and macOS, Frontend on ubuntu and macOS, Rust on ubuntu and macOS, Docker image on ubuntu: 3 macOS + 4 Linux jobs |
| Release | a `v*` tag, a manual run, and pull requests that touch the packaging files | macOS (Apple silicon), macOS (Intel), Linux, Windows installer builds, each with a PyInstaller engine build; plus the new `downloads` job (ubuntu, about a minute) on tags |
| Docs | push to `main`, pull requests | ubuntu |
| Licenses | push, pull request, weekly | ubuntu |

**Measured** with `gh api` over the last 30 days (2026-09-20 to 2026-10-20: 286 runs, a busy development month), each job
rounded up to whole minutes as GitHub bills it, multiplied by 1 (Linux), 2 (Windows) and 10 (macOS):

| Workflow and trigger | Runs | Raw minutes | Billed minutes | Per run |
|---|---|---|---|---|
| CI, pull request | 94 | 830 Linux + 698 macOS | 7,810 | 83 |
| CI, push to `main` | 52 | 458 Linux + 386 macOS | 4,318 | 83 |
| Release, pull request (packaging paths) | 23 | 188 Linux + 528 macOS + 235 Windows | 5,938 | 258 |
| Release, tag push | 7 | 79 Linux + 182 macOS + 98 Windows | 2,095 | 299 |
| Release, manual | 1 | 11 Linux + 27 macOS + 14 Windows | 309 | 309 |
| Docs and Licenses (all triggers) | 109 | 116 Linux | 116 | about 1 |
| **Total** | **286** | | **about 20,600** | |

(Many of those runs were cancelled or failed early, but they are billed too.)

**Is the free allowance enough? No, not at this pace.** The same month would use about ten times the allowance. The cost is
almost all macOS: one CI run is about 83 billed minutes (74 of them from the three macOS jobs), so the whole allowance is
about 24 CI runs a month, fewer than one a day; a full release build is about 300 billed minutes.

**What would fit** (same month, recomputed from the table; each line adds to the one above it):

| Change | Billed minutes in that month |
|---|---|
| none | about 20,600 |
| the Release workflow no longer runs on pull requests (trial builds by hand only) | about 14,600 |
| and CI on pull requests runs Linux only (macOS stays on pushes to `main`) | about 7,700 |
| and CI on `main` runs macOS only nightly or weekly | about 3,800 (a release is about 300 of it) |

Under roughly 2,000 a month means Linux-only CI on pull requests (about 9 minutes a run) plus one or two release builds
and an occasional macOS check.

**Options** (not decided here):

1. **A paid plan** (GitHub Team or Pro): more included minutes (3,000 at the time of writing) and per-minute billing beyond
   that. At this month's volume the overage would be on the order of 100 to 150 US dollars at list prices as I know them, mostly macOS minutes;
   check GitHub's current rates before relying on a figure.
2. **Fewer macOS jobs on pull requests**: keep the three macOS jobs only on `main` and on tags, keep Linux on every pull
   request; run Release by hand or on tags only (remove the `pull_request` trigger, or keep it for `release.yml` edits
   only). Costs some safety on macOS-only breakage until it reaches `main`.
3. **A self-hosted runner** (for example a Mac you own) for the macOS jobs: GitHub counts no minutes for it, but you maintain it,
   it must only run trusted code (a private repository's own branches, never forks), and GitHub has announced a small
   per-minute platform fee for self-hosted runners; check the current terms.
4. Smaller levers: cancel superseded runs (CI already does), cache more, and skip the Docs and Licenses workflows when
   only unrelated files change (they cost about a minute each).

The `downloads` and `promote` jobs added with this channel are Linux and cost about 1 to 3 minutes per release.
