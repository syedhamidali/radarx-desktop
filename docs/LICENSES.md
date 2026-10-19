# Licences and third-party notices

> This page describes how the project keeps track of licences. It is an engineering process, **not legal
> advice**. The wording of `NOTICE`, `THIRD_PARTY_NOTICES.md` and the contributor terms is **to be reviewed
> by a lawyer** before wider redistribution.

## The project licence

xradar-desktop is source-available under the
[PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0) (`LICENSE`,
with the required notice in `NOTICE`). Commercial use needs a separate written licence from the copyright
holder. That licence is not changed by anything on this page.

## What the installers contain

| Part | Where it comes from | Licences are listed in |
| --- | --- | --- |
| Python engine (xradar, xarray, dask, numpy, scipy, matplotlib, radarx, ...) | `python/pyproject.toml` and everything it requires | `THIRD_PARTY_NOTICES.md`, "Python engine" |
| Native libraries inside the Python wheels (HDF5, netCDF, PROJ, zlib, OpenSSL, ...) | wheels, copied by PyInstaller | "Native libraries" (from upstream terms; re-scan the release build) |
| Frontend (Tauri API, the compiled Svelte runtime) | `package.json`, `package-lock.json` | "Frontend (npm)" |
| Desktop shell (Tauri 2 and its crates) | `src-tauri/Cargo.lock` | "Desktop shell (Rust crates)" |
| Fonts (IBM Plex Sans and Mono) | `src/lib/styles/fonts/` (licence texts beside the files) | "Fonts" |
| Colour maps | `src/lib/utils/colormaps.ts`, colorcet, cmweather | "Colour maps" |
| Map, terrain and imagery services | fetched at run time on request, not shipped | "Map, terrain and imagery services" |
| Radar data | opened by the user, or fetched from NOAA/Unidata on request | "Data" |

## Generating the notices

```bash
# in the engine environment (pip install -e "python[radarx]")
python scripts/licenses/generate_notices.py             # writes THIRD_PARTY_NOTICES.md
python scripts/licenses/generate_notices.py --check     # exit 1 if a dependency fails the allow-list
python scripts/licenses/generate_notices.py --verify    # exit 1 if the committed file is stale
python scripts/licenses/generate_notices.py --scan-engine python/dist/xradar-engine   # shared libraries of a build
python -m pytest scripts/licenses                       # tests of the policy logic
```

* Python licences are read from the **installed** package metadata (SPDX `License-Expression`, else the trove
  classifiers, else the `License` field, else a licence file that ships with the package). Run it in the
  environment the installers are built from; versions of platform-specific wheels differ between systems, so CI
  verifies only the npm, Rust and static sections of the committed file.
* npm licences come from `package-lock.json` (no `npm install` needed), starting at the production dependencies
  and Svelte, whose runtime is compiled into the bundle.
* Rust licences come from `cargo metadata --locked` (normal dependencies only, all target platforms).
* Fonts, colour maps, services, data and native libraries are written by hand in
  `scripts/licenses/static.toml`.

The output has no time stamps: the same inputs give the same file.

## The allow-list (`scripts/licenses/allowed.toml`)

A dependency **passes** when its licence expression has an alternative made only of identifiers listed as
*permissive* (`MIT OR Apache-2.0 OR LGPL-2.1-or-later` passes through MIT; `MIT AND LGPL-2.1-or-later` does not).
It **fails**, and the `Licenses` workflow turns red, when the licence is

* unknown, missing or not recognised,
* weak copyleft (LGPL, MPL, EPL, CDDL ...), strong copyleft (GPL, AGPL ...) or non-commercial,

unless the package is listed under `[reviewed]` with a note that says how it is used (linked statically or
dynamically, modified or not). Reviewed packages are repeated at the top of `THIRD_PARTY_NOTICES.md` under
"Needs attention".

Adding a dependency:

1. Add it, run `generate_notices.py --check`.
2. If it fails, look at the project's own licence. Wrong or missing metadata: add an `[overrides]` entry with the
   reason. A copyleft or non-commercial licence: do not add it without discussing; if accepted, add a `[reviewed]`
   entry that explains how it is used.
3. Run `generate_notices.py` and commit `THIRD_PARTY_NOTICES.md` with the change.

## Findings that need a decision or a lawyer

* **Non-commercial imagery.** Sentinel-2 cloudless (EOX) is CC BY-NC-SA 4.0. It is used only when the user turns
  on *Realistic ground* and is not shipped. It is compatible with a non-commercial project, but a commercial
  licence for xradar-desktop would have to remove that layer or license it from EOX. Images and clips made with it
  inherit ShareAlike terms.
* **Share-alike map data.** OpenStreetMap (ODbL 1.0) and OpenTopoMap (CC BY-SA) tiles need their attribution on
  screen (done) and the OSM tile usage policy applies.
* **MPL-2.0** in the Rust tree (cssparser, selectors, dtoa-short, option-ext: unmodified, statically linked via
  Tauri) and in `certifi` (unmodified data package). File-level copyleft: it concerns modifications of those
  files, which the project does not make.
* **LGPL** shared libraries that wheels can bundle (libquadmath, GNU libiconv): unmodified, separate files in the
  engine folder. Confirm for each release build and platform with `--scan-engine`.
* **Colour tables of unknown origin.** The NWS-style tables (`NWSRef`, `NWSVel`, ...) have no recorded source in
  the repository; the maintainer should record it.
* **Licence texts.** The notices list names, versions and licence identifiers; reproducing every licence text and
  copyright line is not done yet. Whether the installers already carry them (most wheels ship them in
  `*.dist-info`) should be confirmed in a release build.
* **The Content-Security-Policy** in `src-tauri/tauri.conf.json` still allows `api.mapbox.com`, which the app no
  longer uses (no Mapbox code or tiles are present). Remove it when the shell is next touched.
* **Contributions.** Contributor terms (DCO sign-off, rules for generated code) are in {doc}`contributing`; a
  lawyer should confirm whether a DCO is enough for the maintainer's plan to offer commercial licences, or whether
  a contributor licence agreement is needed.
