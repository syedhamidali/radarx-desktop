# What's new

```{include} changes/unreleased.md
```

## 0.3.0

- **Updates:** the app can find, verify and install new versions by itself (signed updates from GitHub Releases;
  you stay in control: check on launch, ask first, never during a running job). See {doc}`UPDATES`.
- **Command line and Python:** `rxd` (info, plot, run, pipeline, mosaic, winds, cells, export, check, sounding, qpe,
  basin, animate) and `from engine import api`, with bulk-job recipes ({doc}`CLI`, {doc}`BULK`, {doc}`API`).
- **3D:** mosaics and multi-Doppler winds in the 3D view, wind-flow particles that follow only the retrieved winds,
  realistic ground (terrain and satellite imagery), a stylised realistic storm look with about 50 colour controls and
  presets, storm clips (orbit, ground-level window, hurricane flight) with interpolation along the observed winds,
  modelled lightning and synthesised sound, all clearly labelled ({doc}`MOSAIC`, {doc}`REALISTIC_3D`, {doc}`STORM_CLIP`).
- **RHI** scans are drawn as range-height sections with sensible height limits.
- **Hydrology:** rain rate and accumulation with uncertainty, gauge adjustment and validation, basin averages
  ({doc}`HYDROLOGY`).
- **Environment and provenance:** a data-provider framework (always asks before any download), soundings with a
  Skew-T and hodograph, and a provenance chip on every layer: observed, model, retrieved, simulated or stylised
  ({doc}`ENVIRONMENT`, {doc}`SCIENCE`).
- **Data check** when a file opens: aliasing, noise, gaps, clutter, blocked beams and ZDR calibration offset, with
  evidence and a suggested recipe that you apply yourself ({doc}`DATA_CHECK`).
- **Map and interface:** a hide-map toggle in 2D and 3D, a radar-sites layer (click a site to open its latest scan,
  after you agree), one consistent look in the dark and light themes, and a value mask that behaves the same
  everywhere, including exports.
- **Files:** NetCDF/CfRadial/Zarr/CSV/PNG outputs follow the CF conventions (checked with the CF checker) and
  carry their provenance ({doc}`OUTPUT_STANDARDS`).
- **Faster and smaller:** NetCDF export of a NEXRAD volume 52 s to 2.6 s, running an algorithm on every sweep about
  5 times faster with far less memory, first paint about 2 times faster, installers about 25 % smaller.
- **More reliable:** a native crash that could stop the engine when opening or reading NetCDF-based files is fixed,
  and 15 more problems found by fault-injection testing are fixed ({doc}`STABILITY`).
- **Licence:** source-available under the PolyForm Noncommercial License 1.0.0; third-party notices are in
  `THIRD_PARTY_NOTICES.md`.

## 0.2.1

- **Open With:** right-click a radar file → *Open With → xradar-desktop*, including numbered copies
  (`file.nc.1`) and, on macOS, files without an extension.
- **Web version:** the engine runs as a secure web service and the app as a static site.
- **Fixed** the 2D azimuth jump on sector scans and rays with gaps; the hover probe no longer reports values
  where nothing is drawn; mosaics no longer show flat plates where no radar saw an echo.

## 0.2.0

- Pipelines, variable mapping, multi-Doppler winds, unified storm-cell tracking, seven more radarx modules,
  reliability work (timeouts, memory budget, no-echo areas in exports).

## 0.1.x

- First installers for macOS, Windows and Linux with the engine bundled; export of cleaned data;
  radarx-based quality control; mosaics; 3D view.
