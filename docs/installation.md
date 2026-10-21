# Installation

## Download

Get the installer for your system from the [download page](https://radarx.labshope.com/#download) (one file;
everything the app needs is included). Or fetch it from a terminal, as below. You
need a free Radarx account to use the app: see {doc}`ACCOUNTS`.

| System | File |
|---|---|
| Mac with Apple silicon (M1 and later) | `Radarx_<version>_aarch64.dmg` |
| Mac with an Intel chip | `Radarx_<version>_x64.dmg` |
| Windows 10 / 11 | `Radarx_<version>_x64-setup.exe` |
| Ubuntu / Debian | `Radarx_<version>_amd64.deb` |
| Fedora / Red Hat | `Radarx-<version>-1.x86_64.rpm` |

### Download with curl

Every release is a folder on the download host, `https://downloads.labshope.com/radarx/v<version>/`, and
`https://downloads.labshope.com/radarx/releases.json` lists the released versions, newest first, with the
address, size and SHA-256 checksum of each file. To fetch the newest installer for your system (macOS and Linux;
needs `curl` and `jq`):

```bash
OS=macos ARCH=aarch64 KIND=dmg     # macos|windows|linux   aarch64|x86_64   dmg|exe|deb|rpm
URL=$(curl -fsS https://downloads.labshope.com/radarx/releases.json |
      jq -r --arg o "$OS" --arg a "$ARCH" --arg k "$KIND" \
        '.releases[0].assets[] | select(.os == $o and .arch == $a and .kind == $k) | .url')
echo "$URL"
curl -fLO "$URL"
```

Typical values: `macos aarch64 dmg` (Apple silicon), `macos x86_64 dmg` (Intel Mac), `linux x86_64 deb`,
`linux x86_64 rpm`, `windows x86_64 exe`. If you know the version, the address is built from it:

```bash
V=0.3.0
curl -fLO https://downloads.labshope.com/radarx/v$V/Radarx_${V}_aarch64.dmg
```

**Check the download.** The folder has a `SHA256SUMS` file in the usual format. A file that does not match must
not be installed:

```bash
curl -fsS "$(dirname "$URL")/SHA256SUMS" | grep " $(basename "$URL")\$" | shasum -a 256 -c -     # macOS
curl -fsS "$(dirname "$URL")/SHA256SUMS" | grep " $(basename "$URL")\$" | sha256sum -c -        # Linux
```

On **Windows** (PowerShell), where `curl.exe` and `Invoke-WebRequest` are built in:

```powershell
$r = (Invoke-RestMethod https://downloads.labshope.com/radarx/releases.json).releases[0]
$a = $r.assets | Where-Object { $_.os -eq "windows" -and $_.kind -eq "exe" } | Select-Object -First 1
Invoke-WebRequest $a.url -OutFile $a.name
(Get-FileHash $a.name -Algorithm SHA256).Hash.ToLower() -eq $a.sha256     # must print True
```

## Install

The builds are not code-signed yet, so the first launch shows a warning.

**macOS**

```bash
open Radarx_*_aarch64.dmg          # drag the app onto Applications
open /Applications/Radarx.app
```

If macOS says the app cannot be opened or is "damaged", open *System Settings → Privacy & Security* and click
*Open Anyway*, or run `xattr -cr /Applications/Radarx.app` once and open it again.

**Windows:** run the downloaded `Radarx_<version>_x64-setup.exe` (or `.\Radarx_*_x64-setup.exe`
in PowerShell). If SmartScreen warns, click *More info → Run anyway*.

**Linux**

```bash
sudo apt install ./Radarx_*_amd64.deb         # Ubuntu / Debian
sudo dnf install ./Radarx-*.x86_64.rpm        # Fedora / Red Hat
radarx-desktop                                          # or start it from the application menu
```

The first launch can take up to a minute while the system scans the app; after that it starts in about a
second. The window then asks you to create your free account or sign in (a code is sent to your e-mail; see
{doc}`ACCOUNTS`). Installed apps check for updates themselves (*Settings → Updates*, see {doc}`settings`); to move to a new version by
hand, download and install it the same way over the old one.

## Open files from the file manager

Right-click a radar file and choose *Open With → Radarx*. NetCDF/CfRadial, HDF5/ODIM, gzip files and
numbered copies such as `file.nc.1` are registered by extension. On macOS the app is also offered for files with
no extension, like NEXRAD Level II (`KTLX20130520_201643_V06`).

## The command line (`rxd`)

The command line comes as a Python package, a wheel that is part of every release, next to the installers. It needs Python 3.10 or newer (3.12 or newer for the optional radarx
algorithms) and works on macOS, Windows and Linux. Use a virtual environment so it stays apart from your other
Python packages.

**macOS and Linux**

```bash
python3 -m venv ~/radarx-env && source ~/radarx-env/bin/activate
V=$(curl -fsS https://downloads.labshope.com/radarx/releases.json | jq -r .latest)
pip install "https://downloads.labshope.com/radarx/v$V/radarx_desktop_engine-$V-py3-none-any.whl"
rxd --version
rxd login                                      # the same free account as the app
rxd examples                                   # a guided tour of copy-paste commands
```

**Windows** (PowerShell)

```powershell
py -m venv $HOME\radarx-env; & $HOME\radarx-env\Scripts\Activate.ps1
$V = (Invoke-RestMethod https://downloads.labshope.com/radarx/releases.json).latest
pip install "https://downloads.labshope.com/radarx/v$V/radarx_desktop_engine-$V-py3-none-any.whl"
rxd --version
```

With [pipx](https://pipx.pypa.io/) the command is on your `PATH` without activating anything:
`pipx install "<the wheel address above>"`. To add the optional algorithms of the
[radarx](https://radarx.readthedocs.io/en/latest/) library (mosaics, winds, `radarx_*` modules, Python 3.12 or newer):
`pip install -U radarx`. To update, run the same `pip install` with the new version number; to check the file,
compare it with the line for the wheel in the release's `SHA256SUMS` (see above).

Tab completion: `source <(rxd completion zsh)`, or
`rxd completion bash > ~/.local/share/bash-completion/completions/rxd`. Every command, with examples and the exit
codes, is in {doc}`CLI`. It needs a signed-in account like the app ({doc}`ACCOUNTS`).
