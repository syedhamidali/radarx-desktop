---
myst:
  html_meta:
    "description lang=en": |
      Radarx Desktop: a desktop and web app for looking at weather radar data, cleaning it, running
      algorithms on it (hydrometeor classification, storm cells, multi-Doppler winds, mosaics,
      rainfall accumulation with uncertainty)
      and exporting the results, with a command line for everything.
html_theme.sidebar_secondary.remove: true
---

# Radarx Desktop

{{release_line}}

**Date:** {{today}}

A desktop app (and web app) for weather radar data, built on [xradar](https://docs.openradarscience.org/projects/xradar/en/stable/),
[xarray](https://xarray.dev/) and [radarx](https://radarx.readthedocs.io/en/latest/). Open a file, see it in 2D
and 3D, clean it, run algorithms, combine several radars into mosaics and wind analyses, and export the
result. Everything the app does is also available from the command line (`rxd`).

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

installation
user-guide
science-and-trust
history
cite
```

::::{only} html
```{rubric} Where to go
```

- {doc}`installation`: download and install it, set up the command line, create your free account.
- {doc}`user-guide`: opening and viewing data, modules and pipelines, mosaics, winds and rainfall, 3D and
  animation, the `rxd` command line.
- {doc}`cli-guide` and {doc}`cluster`: everything from the terminal, and servers and clusters with no screen.
- {doc}`tutorial-bulk-data`: a whole event, four radars and 44 volumes, from a dropped folder to results and
  exports in the app.
- {doc}`science-and-trust`: the science policy, output standards, licences, privacy and reporting.
- {doc}`history`: what changed in each version.
- {doc}`cite`: how to cite this documentation (its DOI) and the software.
::::
