---
myst:
  html_meta:
    "description lang=en": |
      xradar-desktop: a desktop and web app for looking at weather radar data, cleaning it, running
      algorithms on it (hydrometeor classification, storm cells, multi-Doppler winds, mosaics)
      and exporting the results, with a Python API and command line for everything.
html_theme.sidebar_secondary.remove: true
---

# xradar-desktop

**Release:** {{release}}\
**Date:** {{today}}

A desktop app (and web app) for weather radar data, built on [xradar](https://github.com/openradar/xradar),
[xarray](https://xarray.dev/) and [radarx](https://github.com/syedhamidali/radarx). Open a file, see it in 2D
and 3D, clean it, run algorithms, combine several radars into mosaics and wind analyses, and export the
result. Everything the app does is also available from Python and from the command line.

It is a research tool, so it follows one rule: everything on screen or in a file is either observed data, a
labelled method applied to it, or a labelled picture, and nothing is invented. The rule and what each kind of
layer must show are in {doc}`SCIENCE`; the licences of everything it ships are in {doc}`LICENSES`.

```{image} images/reflectivity.png
:alt: Reflectivity from the KTLX radar, Oklahoma, 20 May 2013
:width: 70%
:align: center
```

```{toctree}
:maxdepth: 2
:caption: Getting started

installation
usage
```

```{toctree}
:maxdepth: 2
:caption: Using the app

opening-data
viewing
using-modules
RADAR_SITES
RHI
CROSSSECTION
export
settings
```

```{toctree}
:maxdepth: 2
:caption: Command line and Python

CLI
BULK
API
```

```{toctree}
:maxdepth: 2
:caption: User guide

VARMAP
PIPELINES
MODULES
CELLS
MOSAIC
REALISTIC_3D
STORM_CLIP
MULTIDOPPLER
```

```{toctree}
:maxdepth: 2
:caption: Science and trust

SCIENCE
OUTPUT_STANDARDS
ENVIRONMENT
LICENSES
```

```{toctree}
:maxdepth: 2
:caption: Running it your way

WEB
RENDER
BUNDLING
UPDATES
PACKAGING_AUDIT
```

```{toctree}
:maxdepth: 2
:caption: Project

DESIGN
RELIABILITY
RESPONSIVENESS
STABILITY
PERFORMANCE
PERFORMANCE_FRONTEND
contributing
history
_api/index
```

Indices and tables
==================

- {ref}`genindex`
- {ref}`modindex`
- {ref}`search`
