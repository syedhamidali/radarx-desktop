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
`~/Downloads/radarx-exports`. In the web version exports are browser downloads. Mosaics export as an image with a
world file (`.pgw`) and projection (`.prj`) so they open georeferenced in GIS software, or as a NetCDF with all
gridded fields ({doc}`MOSAIC`). Every file records the software version, creation time (UTC), source file *names* and the
settings used, and follows CF / CfRadial conventions: see {doc}`OUTPUT_STANDARDS`.

## Cite and repeat: run record, methods text, project files

The **Export** panel has a *Cite and repeat* section. The app keeps a record of what you did in the session: every
module, pipeline step and export, with every parameter (including the defaults that were applied), the file it was
applied to, and the versions of radarx-desktop and its libraries.

* **Copy methods text** puts a plain-language methods section on the clipboard: one paragraph about the software, one
  paragraph per step (what was done, to which sweep of which file, every parameter and which of them you changed,
  the algorithm's version; steps computed by radarx say "radarx" and its version), then the references.
* **Save run record** writes the same record as a JSON file. `rxd methods run_record.json` prints the methods text
  from it later, without the app ({doc}`CLI`).
* **Save project / Open project** keeps a piece of work: which files are open, which variable and sweep each shows,
  the pipeline being edited, and the run record. A project holds **no data**; each file is recorded by its path and by
  its path relative to the project, with its size and date. When you open a project the app looks for each file
  there, and tells you if one is missing or has changed since (it never silently opens something else).
  `rxd project check` does the same check from the command line.

In the pipeline editor, **Undo** and **Redo** (or Ctrl/Cmd+Z and Ctrl/Cmd+Shift+Z) step back and forth through your
edits; dragging a value counts as one step.
