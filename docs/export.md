# Exporting

```{image} images/ui/export-panel.png
:alt: The export panel
:width: 35%
:align: right
```

**Quick export** saves the current view as PNG, SVG or PDF with a colour bar, range rings and a title (options for
the DPI and a dark background). **Batch export** writes a figure for every chosen variable and sweep. **Data
export** writes the volume as NetCDF (CfRadial1), CfRadial2, Zarr or CSV; derived variables (cleaned velocity,
classifications, ...) are included next to the measured ones if you tick *include derived fields*. **Animation**
exports a GIF through the sweeps.

The desktop app asks where to save (the last folder is remembered); without a choice, files go to
`~/Downloads/xradar-exports`. In the web version exports are browser downloads. Mosaics export as an image with a
world file (`.pgw`) and projection (`.prj`) so they open georeferenced in GIS software, or as a NetCDF with all
gridded fields ({doc}`MOSAIC`).
