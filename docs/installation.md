# Installation

## The installers (nothing else to install)

Download the file for your system from the
[latest release](https://github.com/syedhamidali/radarx-desktop/releases/latest). The installers include
the Python engine, so there is no Python or library to install.

| System | File |
|---|---|
| Mac with Apple silicon (M1 and later) | `radarx-desktop_<version>_aarch64.dmg` |
| Mac with an Intel chip | `radarx-desktop_<version>_x64.dmg` |
| Windows 10 / 11 | `radarx-desktop_<version>_x64-setup.exe` |
| Ubuntu / Debian | `radarx-desktop_<version>_amd64.deb` |
| Fedora / Red Hat | `radarx-desktop-<version>-1.x86_64.rpm` |

The builds are not code-signed, so the first launch shows a warning:

- **macOS:** drag the app to Applications. If macOS says it cannot be opened or is "damaged", open
  *System Settings → Privacy & Security* and click *Open Anyway*, or run
  `xattr -cr /Applications/radarx-desktop.app` once.
- **Windows:** click *More info → Run anyway*.
- **Linux:** `sudo apt install ./radarx-desktop_*.deb` (or `sudo dnf install ./*.rpm`).

The first launch can take up to a minute while the system scans the app; after that it starts in about a
second. See {doc}`BUNDLING` for how the installers are built.

## Open files from the file manager

Right-click a radar file and choose *Open With → radarx-desktop*. NetCDF/CfRadial, HDF5/ODIM, gzip files and
numbered copies such as `file.nc.1` are registered by extension. On macOS the app is also offered for files with
no extension, like NEXRAD Level II (`KTLX20130520_201643_V06`).

## From source (engine only, for scripts and the command line)

```bash
git clone https://github.com/syedhamidali/radarx-desktop.git
cd radarx-desktop/python
python -m venv .venv && source .venv/bin/activate
pip install -e ".[radarx]"        # radarx needs Python >= 3.12
```

This gives the Python API (`from engine import api`) and the `rxd` command. The desktop app itself additionally
needs Node.js 20+ and Rust; see {doc}`contributing`.

## On the web

The same app runs in a browser against an engine in web mode: see {doc}`WEB` and {doc}`RENDER`.
