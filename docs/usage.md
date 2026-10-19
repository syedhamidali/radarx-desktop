# A tour of the app

## Open data

*Open* (or drag a file onto the window, or right-click a file → *Open With*) reads NEXRAD Level II, ODIM H5,
CfRadial 1/2, IRIS/Sigmet, Rainbow, Furuno, IMD and UF files; gzip-wrapped files work as they are. The format is
detected from the content, so file names do not matter. The *Cloud data* panel opens the latest NEXRAD scan of
a station straight from the public AWS archive.

```{image} images/app-2d.png
:alt: The 2D view
:width: 100%
```

## 2D and 3D views

Pan and zoom the PPI on a map; switch to *3D* for the GPU ray-marched volume with a transfer-function editor,
a map ground plane and vertical exaggeration.

```{image} images/app-3d.png
:alt: The 3D view
:width: 100%
```

Sector scans and rays with gaps in the azimuth list are drawn as they were measured: the sector stays open and
only moderate gaps are filled.

## Variable mapping

Modules ask for a *role* (reflectivity, velocity, ZDR, ...) rather than a variable name. {doc}`VARMAP`
explains how names are resolved (CF names, aliases, patterns, per-file overrides) and how a velocity that lives
on the paired Doppler cut is found automatically.

## Modules

Algorithms are plug-in modules: hydrometeor classification, KDP, QPE, hail, rotation couplets, shear, ZDR arcs and
columns, KDP columns, VVP winds, beam blockage, tornado probability, melting layer, QVP, ZDR calibration, storm
cell detection and more. A result can be a table, an overlay or a new field that you can view in 2D and 3D and
export. Running a module on all sweeps stores one *derived variable*. See {doc}`MODULES` to write your own.

```{image} images/modules-hid.png
:alt: Hydrometeor classification on every sweep
:width: 100%
```

## Cleaning and pipelines

Chain de-aliasing, clutter and biological filters, SNR gates, despeckle, smoothing and gap filling into one
*recipe* that produces a single result variable, which other modules (wind analysis, ...) can use as input:
{doc}`PIPELINES`.

```{image} images/qc-before-after.png
:alt: Reflectivity and ZDR before and after quality control
:width: 80%
```

## Mosaics, winds and cells

- {doc}`MOSAIC`: merge several radars into a composite, CAPPI or echo-top product with radarx's gridding and
  weighted merge, exported as an image with a world file or as NetCDF.
- {doc}`MULTIDOPPLER`: dual- and multi-Doppler wind retrieval from two or more radars.
- {doc}`CELLS`: storm cell identification and tracking with per-cell statistics.

## Export

PNG, SVG and PDF figures, and NetCDF (CfRadial1), CfRadial2, Zarr and CSV data, including derived fields.
Exports go to the folder you choose (or `~/Downloads/xradar-exports`); in the web version they are browser
downloads.
