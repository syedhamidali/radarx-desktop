# Opening data

## Ways to open a file

- **Open** button (or `Ctrl/⌘ + O`): pick one or several files. There is no file-type filter, because NEXRAD
  Level II files usually have no extension.
- **Drag and drop** a file onto the window.
- **Open With** from the file manager: right-click a radar file → *Open With → Radarx*, or double-click it
  once the app is the default for that type. This works for NetCDF/CfRadial, HDF5/ODIM, gzip files, numbered
  copies such as `file.nc.1` and, on macOS, for files with no extension.
- **Cloud**: the *Cloud data* panel opens the latest NEXRAD Level II scan of a station from the public AWS archive,
  browses the archive by date, or opens ARCO / icechunk data. Click a radar marker on the map for the same
  actions per site ({doc}`RADAR_SITES`).
- **Live**: the live map shows the newest scans of your default radars as one updating composite ({doc}`LIVE`).

```{image} images/ui/welcome.png
:alt: The empty window
:width: 90%
:align: center
```

The format is detected from the file contents (NEXRAD Level II, ODIM H5, CfRadial 1 and 2, IRIS/Sigmet, Rainbow,
Furuno, IMD, UF, ...), so the name does not matter: `KTLX20130520_201643_V06`, `volume.nc.3` and
`KTLX20130520_201643_V06.gz` all open the same way.

## Several files and several radars

Every open file gets a tab. The files of several radars can be combined into a mosaic ({doc}`MOSAIC`) or a wind
analysis ({doc}`MULTIDOPPLER`) from the panels of the same name.

Three or more files, or folders, dropped together open the {doc}`Autopilot <AUTOPILOT>` instead: it inspects
them, plans what the data support and runs only what you approve. The tutorial {doc}`tutorial-bulk-data`
takes a folder of 44 volumes from four radars through it.

## Cloud data

```{image} images/ui/cloud-panel.png
:alt: The cloud data panel
:width: 40%
:align: right
```

*Real-time* opens the newest scan of the chosen station; *Archive* lists scans by date; *ARCO* opens analysis-ready
cloud-optimised volumes; *Network* opens the scans of every radar of a region at once, ready for a mosaic. Files
are read from the public `unidata-nexrad-level2` bucket, so no AWS account or credentials are needed (your free Radarx account is still required to use the app).

## Looking inside a file

*Table View* shows the values of a sweep as a table; *Metadata* lists the whole data tree with its attributes:

```{image} images/ui/metadata.png
:alt: The metadata dialog
:width: 90%
:align: center
```
