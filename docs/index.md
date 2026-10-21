---
myst:
  html_meta:
    "description lang=en": |
      radarx-desktop: a desktop and web app for looking at weather radar data, cleaning it, running
      algorithms on it (hydrometeor classification, storm cells, multi-Doppler winds, mosaics,
      rainfall accumulation with uncertainty)
      and exporting the results, with a Python API and command line for everything.
html_theme.sidebar_secondary.remove: true
---

# radarx-desktop

**Release:** {{release}}\
**Date:** {{today}}

A desktop app (and web app) for weather radar data, built on [xradar](https://docs.openradarscience.org/projects/xradar/en/stable/),
[xarray](https://xarray.dev/) and [radarx](https://radarx.readthedocs.io/en/latest/). Open a file, see it in 2D
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
:hidden:
:maxdepth: 2

getting-started
using-the-app
cli-and-python
user-guide
science-and-trust
project
```

## Where to go

- {doc}`getting-started`: install it, create your free account, a first tour.
- {doc}`using-the-app`: open data, the 2D and 3D views, cross-sections and RHI, modules, settings, export.
- {doc}`cli-and-python`: the `rxd` command, many files at once and the Python API.
- {doc}`user-guide`: pipelines, storm cells, mosaics, rainfall, multi-Doppler winds, animations and the other tools.
- {doc}`science-and-trust`: the science policy, output standards, licences, privacy and reporting.
- {doc}`project`: web service, installers and updates, design, performance and the changelog.

Indices and tables
==================

- {ref}`genindex`
- {ref}`modindex`
- {ref}`search`
