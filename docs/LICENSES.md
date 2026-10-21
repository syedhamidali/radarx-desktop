# Licences and third-party notices

> This page describes how licences are tracked. It is an engineering summary, **not legal advice**.

## The project licence

Radarx Desktop is provided by LabShope Technologies under the
[PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0).
Commercial use needs a separate written licence from the copyright holder. That licence is not changed by anything
on this page.

## What the installers contain

| Part | Licences |
| --- | --- |
| Analysis engine: xradar, xarray, dask, numpy, scipy, matplotlib, radarx and what they require | listed with name, version and licence identifier in the third-party notices |
| Native libraries inside the Python packages (HDF5, netCDF, PROJ, zlib, OpenSSL, ...) | from the upstream terms of each library |
| Interface (the compiled web front end and the Tauri API) | listed in the third-party notices |
| Desktop shell (Tauri 2 and its Rust libraries) | listed in the third-party notices |
| Fonts (IBM Plex Sans and Mono) | SIL Open Font License; the licence texts ship with the fonts |
| Colour maps | colorcet, cmweather and the maps written for the app, as listed in the notices |
| Map, terrain and imagery services | fetched at run time on request, not shipped |
| Radar data | opened by you, or fetched from NOAA / Unidata on request |

## How third-party licences are checked

A list of every third-party component with its version and licence identifier is generated for each release, from
the metadata of the components themselves. The list is produced the same way every time (it has no time stamps).

A component **passes** automatically when its licence expression has an alternative made only of permissive licences
(for example `MIT OR Apache-2.0 OR LGPL-2.1-or-later` passes through MIT; `MIT AND LGPL-2.1-or-later` does not). A
component with an unknown or missing licence, a weak copyleft licence (LGPL, MPL, EPL, CDDL, ...), a strong copyleft
licence (GPL, AGPL, ...) or a non-commercial licence does **not** pass automatically: it is added only after a review
that records how it is used (linked statically or dynamically, modified or not), and reviewed components are repeated
at the top of the notices under "Needs attention".

## Things to know

* **Non-commercial imagery.** Sentinel-2 cloudless (EOX) is CC BY-NC-SA 4.0. It is used only when you turn on
  *Realistic ground* and is not shipped. It is compatible with a non-commercial licence, but a commercial licence
  for Radarx Desktop would have to remove that layer or license it from EOX. Images and clips made with it inherit
  ShareAlike terms.
* **Share-alike map data.** OpenStreetMap (ODbL 1.0) and OpenTopoMap (CC BY-SA) tiles need their attribution on
  screen (shown) and the OSM tile usage policy applies.
* **MPL-2.0** components (some Rust libraries linked statically through Tauri, and the `certifi` data package) are
  used unmodified. The file-level copyleft concerns modifications of those files, which are not made.
* **LGPL** shared libraries that the Python packages can bundle (libquadmath, GNU libiconv) are unmodified, separate
  files in the engine folder.
