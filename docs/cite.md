# How to cite

This documentation is archived on [Zenodo](https://zenodo.org), a research repository run by CERN, as a PDF and as
a copy of these web pages. The record has a **DOI** (a permanent identifier) so that papers, theses and reports
can cite it.

- **The DOI archives the documentation only**: the user manual you are reading, not the software and not its
  source code.
- **One DOI for all versions.** The *concept DOI* below always leads to the latest version. Each release of the
  documentation is added to the same record as a new version, which also gets a DOI of its own.
- The citations on this page are generated from the same record information as the archive, so the formats
  always agree.

```{include} generated/cite-formats.md
```

## Citing the software

The DOI above is for the documentation. If you used Radarx Desktop itself to make a figure or a result, say so in
your methods and name the version you used (shown in the app under *About*, and by `rxd --version`), for example:

> Radar data were processed with Radarx Desktop, version {{release}} (Radarx Desktop by LabShope Technologies,
> <https://radarx.labshope.com>).

The software is a separate work from this documentation and has its own licence (see {doc}`LICENSES`). Methods
and algorithms that the app applies (hydrometeor classification, multi-Doppler winds, rainfall estimation, ...)
have their own references, given on each page and in the run record written with every result: please cite
those as well.

## Licence of the documentation

The licence of the archived documentation is stated on the Zenodo record. Figures that contain third-party maps,
satellite imagery or radar data keep the licences and credits of those sources, listed in
{doc}`image-credits`.
