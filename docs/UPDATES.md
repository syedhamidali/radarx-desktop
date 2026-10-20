# Updates

The installed desktop app can keep itself up to date. It is quiet and you stay in control: it tells you a new
version exists, shows the release notes, and installs only when you say so. The browser (web) version has no
update feature; it is always the current build.

## What you see

- A small **Update available: vX.Y.Z** chip appears in the status bar when a newer version exists. Click it to read the
  release notes and choose **Install and restart**, **Later** or **Skip this version**.
- **Settings → Updates** shows the current version, the channel (stable releases from the download server), when it last checked,
  a **Check now** button, and two switches:
  - **Check automatically on launch** (on by default): at most one check every 24 hours.
  - **Download in the background** (off by default): off means the app asks first; on means a new version is downloaded
    quietly and you are then asked to restart.
- **Skip this version** hides that exact version. A newer one is offered again. **Offer it again** in Settings undoes it,
  and **Check now** always shows the latest version, skipped or not.

It never interrupts your work. While an export, pipeline, module, mosaic, wind retrieval, file open or upload is
running, the restart waits: the dialog says what it is waiting for, and the prompt comes back by itself when the app is idle.

If you are offline, or the update server does not answer within 10 seconds, nothing pops up. Settings → Updates shows a short
"Could not check" line and that is all.

## What is checked (privacy)

A check is **one HTTPS GET** to the update address in `src-tauri/tauri.conf.json` (`plugins.updater.endpoints`):
`https://downloads.labshope.com/radarx/latest.json`. (While the source repository is still public a second, older
address is listed as a fallback, used only when the first does not answer; see
[the temporary GitHub fallback](#the-temporary-github-fallback).) The request is made by the
app's Rust shell (the updater plugin), not by the web page. Measured on the wire (a local test server):

```
GET /latest.json
accept: application/json
user-agent: tauri-plugin-updater/2.10.1
host: <update host>
```

- The app's own version, your operating system and your architecture are **not sent**: the address has no placeholders, and
  the user agent names the updater plugin and its version, not the app. The app compares versions itself.
- Like any web request, the update server (a Cloudflare R2 bucket behind `downloads.labshope.com`) sees your IP
  address and the time. There are no cookies, identifiers or analytics, and
  nothing else is sent anywhere.
- If you choose to install, the app then downloads the installer for your system from the address listed in
  `latest.json` (a file on the same download server, `https://downloads.labshope.com/radarx/v<version>/...`).
- What stays on your computer: the last-checked time, the two switches and a skipped version, in the app's settings.

**To turn it off:** Settings → Updates → switch off *Check automatically on launch*. The app then contacts the
update server only when you press *Check now*. A build whose public key is not filled in (see below) never checks at
all and says "Updates are not configured in this build".

## Trust: how an update is verified

Every update file is signed with a private key that only the maintainers hold. The matching **public key** is built into
the app (`plugins.updater.pubkey`). Before anything is installed, the app verifies the downloaded file against that key
(minisign signatures); a file that was altered, or signed by another key, is **rejected and nothing is installed**. This
was tested: a one-byte change in the downloaded archive is refused with "The update was rejected because its signature
could not be verified."

An update that cannot be verified never replaces the app. Known limit: the signature covers the downloaded file, not the
version number in `latest.json`, so someone who could publish a forged `latest.json` could offer an older (but
genuine) signed build. Tauri 2.12 adds a switch for this (`requireSignedVersion`); this project is on Tauri 2.10, so set it
when Tauri is upgraded.

## What happens on each system

| System | What an update does |
|---|---|
| macOS (dmg / `.app`) | Downloads the `.app.tar.gz`, verifies it, replaces the app in place, then restarts it. If the app is in a folder you cannot write to, macOS asks for an administrator password. |
| Windows (NSIS installer) | Downloads the installer, verifies it, stops the analysis engine, runs the installer in "passive" mode (a progress bar, no questions) and starts the new version. |
| Linux `.deb` / `.rpm` | **Cannot update itself.** The app still tells you a new version exists and offers **Open the download page** (the website's download section); install the new package the usual way. |
| Linux AppImage | Would update in place, but no AppImage is published yet (the bundled Python libraries do not pass linuxdeploy; see [BUNDLING.md](BUNDLING.md)). |
| Browser (web) | No update section; nothing to update. |

On macOS the app is ad-hoc signed (not notarised). Updating does not change the first-launch warning story: the very
first install from the dmg still needs *Open Anyway* (see the README). The updated copy is written by the app itself,
so it carries no download quarantine flag and **does not trigger the Gatekeeper warning again**; the bundle's code
signature still verifies (`codesign --verify --deep --strict`).

## For maintainers

### One-time setup

1. Generate the signing key pair (do this on your own machine; keep the private key out of the repository):

   ```bash
   npx tauri signer generate -w ~/.tauri/radarx.key
   ```

   Choose a password when asked. This writes the private key to `~/.tauri/radarx.key` and prints the public key (also saved as
   `~/.tauri/radarx.key.pub`).

2. Put the **public** key in `src-tauri/tauri.conf.json`, replacing the placeholder in `plugins.updater.pubkey`:

   ```bash
   cat ~/.tauri/radarx.key.pub        # copy this one line into "pubkey"
   ```

3. Add the **private** key and its password as GitHub repository secrets (Settings → Secrets and variables → Actions), or with the
   GitHub CLI:

   ```bash
   gh secret set TAURI_SIGNING_PRIVATE_KEY < ~/.tauri/radarx.key
   gh secret set TAURI_SIGNING_PRIVATE_KEY_PASSWORD
   ```

   Never commit the private key, paste it into an issue or share it. Back it up somewhere safe (a password manager):
   if it is lost you can no longer ship updates to existing installs (see key rotation).

4. Commit the public key change and merge it. Installers built before that commit have the placeholder and never update.

### Cutting a release

```bash
# bump the version in package.json, src-tauri/Cargo.toml and src-tauri/tauri.conf.json first, then:
git tag -a v0.3.0 -m "What is new in 0.3.0
- ...
- ..."
git push origin v0.3.0
```

The **Release** workflow builds all four installers. Because `TAURI_SIGNING_PRIVATE_KEY` exists, the macOS and
Windows jobs also create and sign the updater files (`*.app.tar.gz` + `.sig`, `*-setup.exe` + `.sig`) and
`tauri-action` writes and merges **`latest.json`** into the **draft** release. The text of the tag message becomes the
release notes shown in the update dialog (markdown is fine; HTML is shown as plain text).

The draft GitHub release is the build's working copy. When the R2 secrets exist, the workflow's **downloads** job then
copies it to the download server as a **candidate** (`radarx/v0.3.0/`): it checks `latest.json`, rewrites the asset
addresses from GitHub to `https://downloads.labshope.com/radarx/v0.3.0/<file>` (the signatures are copied, never
recomputed or edited), writes `SHA256SUMS`, uploads and reads everything back. **Nothing is live yet**: installed apps read
`radarx/latest.json`, which only the **Promote release** workflow writes. Check the candidate, then run
Actions → Promote release with the tag. The whole procedure, including rollback and the owner's one-time set-up, is in
[RELEASING.md](RELEASING.md).

Pull requests and forks build without the secret: the workflow skips signing and does not fail. A tag build without the
secret still produces installers, with a warning in the log that no `latest.json` was created.

To trial a release without publishing: run the workflow by hand (Actions → Release → Run workflow); the installers
and signed updater files are attached as workflow artifacts.

### Building locally with updater files

`tauri.updater.conf.json` turns on `bundle.createUpdaterArtifacts` (it needs the private key in the environment, which is why it
is not in the default config: an ordinary `tauri build` must work without the key):

```bash
export TAURI_SIGNING_PRIVATE_KEY="$(cat ~/.tauri/radarx.key)"
export TAURI_SIGNING_PRIVATE_KEY_PASSWORD='...'
CI=true npm run tauri build -- --config src-tauri/tauri.bundle.macos.conf.json \
  --config src-tauri/tauri.updater.conf.json --bundles app,dmg
```

Only the `app` (macOS), `nsis` / `msi` (Windows) and `appimage` (Linux) bundle types are updater targets; asking for updater
files with only `dmg`, `deb` or `rpm` is an error, which is why the Linux job does not request them.

### Where the addresses live (the single places to change)

| What | Where | Value |
|---|---|---|
| The update address the app asks | `plugins.updater.endpoints` in `src-tauri/tauri.conf.json` (first entry) | `https://downloads.labshope.com/radarx/latest.json` |
| The page the app opens for a manual install ("Open the download page") | `RELEASES_PAGE` in `src-tauri/src/updates.rs` and `DOWNLOAD_PAGE_URL` in `src/lib/utils/brand.ts` | `https://radarx.labshope.com/#download` |
| The host the release scripts write into `latest.json` and `releases.json` | `DEFAULT_BASE_URL` in `scripts/release/release_common.py` | `https://downloads.labshope.com` |

The page the app offers is an explicit constant: it no longer depends on where the update manifest is hosted. A Rust test
(`updates::tests`) reads `tauri.conf.json` and fails if the first endpoint is not the downloads host above.
Installed apps keep the address they were built with, so moving the host later means keeping the old address answering
(or shipping one transition release that carries the new address, signed as usual).

### The temporary GitHub fallback

While the source repository is public, `endpoints` lists two addresses, in this order:

```json
"endpoints": [
  "https://downloads.labshope.com/radarx/latest.json",
  "https://github.com/syedhamidali/radarx-desktop/releases/latest/download/latest.json"
]
```

The second is a **fallback that only works while the repository is public**. It keeps already-built apps (v0.3.0 is built
this way) able to find an update even if the downloads host is not set up yet, and it is not visible to users. How the
updater uses the list (read in `tauri-plugin-updater` 2.10.1, `src/updater.rs`, the version this project locks):
it tries the addresses **in order** and the first one that gives a usable answer wins; an address that cannot be
reached, answers with a non-success status (404, 5xx), or answers 2xx with JSON that is not a manifest, is skipped and the
next one is tried. Two cases do **not** fall through: an address that answers 2xx with a body that is not JSON at all
(an HTML page, for instance) stops the check with an error, and an address that answers successfully with an older
version wins even if a later one has a newer one. So: until the first promote, `radarx/latest.json` must not exist
(it then answers 404 and the app falls through to GitHub); and a wildcard route that answers every unknown path with
an HTML page would break the fallback, which is one more reason to test the custom domain (RELEASING.md).

**When the repository goes private**, delete the second line (and the comma after the first) so the file reads:

```json
"endpoints": [
  "https://downloads.labshope.com/radarx/latest.json"
]
```

Apps built before that edit still carry the GitHub address; once the repository is private it answers 404 for them, so they
simply fall through to the first address, which is the one that works. Nothing else in the code mentions GitHub. The full
checklist is in [RELEASING.md](RELEASING.md#when-the-repository-goes-private).

### Rotating the key

Existing installs only trust the public key they were built with, so a new key cannot be introduced by a normal update that is signed with
the new key (they would reject it). To rotate:

1. Generate a new key pair. Keep the old private key.
2. Build and publish **one transition release** signed with the **old** key whose app contains the **new** public key.
3. Switch the GitHub secrets to the new private key for every release after that.

Installs that update through the transition release trust the new key from then on. Installs that skip the transition
release (still on the old key) cannot be updated automatically any more and must download the installer from the download page once.
The same applies if the private key is lost or leaked without a transition release: users have to reinstall by hand,
which is why the key should be backed up. If a key leaks, rotate at once, and treat releases signed in the meantime as suspect.

### Testing against a local server

The endpoint can be redirected for a test build without touching code, by merging a config on the command line:

```json
{ "version": "0.2.2",
  "plugins": { "updater": { "pubkey": "<a throwaway PUBLIC key>",
                            "endpoints": ["http://127.0.0.1:18700/latest.json"],
                            "dangerousInsecureTransportProtocol": true } } }
```

(`tauri build --config that.json ...`). Use a throwaway key from `npx tauri signer generate`, never the real one, and
never ship a build with `dangerousInsecureTransportProtocol`. The shipped app has no environment variable or hidden
switch that changes the update address.

## The `releases.json` index (for the website)

`https://downloads.labshope.com/radarx/releases.json` lists every promoted version with its installers, newest first. It is
written only by the Promote release workflow and served with `Cache-Control: no-cache`. The website reads it with a
cross-origin GET (the bucket's CORS rule allows `https://radarx.labshope.com`). The schema is the contract between the
release scripts and the website; it is documented, with an example, in [RELEASING.md](RELEASING.md#the-releasesjson-contract).

## How it is built

- Rust: `tauri-plugin-updater` does the check, download, signature verification and install (the web page never
  contacts the update host, so the content security policy is unchanged). `src-tauri/src/updates.rs` decides whether
  updating is configured (public key and endpoint present, not the placeholder) and registers the plugin only then.
  Three small commands (`updater_status`, `stop_engine_for_update`, `relaunch_app`) serve the UI; the restart goes
  through the normal exit path so the analysis engine is stopped first.
- Permissions (`src-tauri/capabilities/default.json`): `updater:allow-check`, `updater:allow-download`,
  `updater:allow-install` (not the combined `default` set, and no process plugin).
- Frontend: `src/lib/utils/updateLogic.ts` (pure rules, unit-tested in `updateLogic.test.mjs`), `updater.ts` (service),
  `stores/updater.ts`, and the chip, dialog and Settings section in `src/lib/components/Update*.svelte`.
