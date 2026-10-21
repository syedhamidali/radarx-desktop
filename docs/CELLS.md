# Storm cells: identification, tracking and statistics

One cell engine serves the **Storm Cell Detection** module, the **Rank Storm Cells** module and the Storm panel.
There is one algorithm, with two interchangeable implementations:

| Backend | When | Identification | Tracking |
|---|---|---|---|
| **radarx** | `radarx` installed (needs Python >= 3.12 and a version of radarx with cell identification, tracking and statistics) | multi-threshold features + watershed (tobac style) | overlap of storm-motion-advected cells (PyFLEXTRKR style), **split / merge** detection, distance link for cells that do not overlap |
| **numpy** | always (the fallback) | 8-connected components on the polar grid, 0/360 seam merged | greedy nearest-neighbour on centroids, no split / merge |

`backend: "auto"` (the default) uses radarx when it is available and falls back to numpy when it is
not installed, is too old, or raises. The module result and the Storm panel say which one
ran (`Algorithm` in the module summary).
Requesting `radarx` when it is missing still works and the note says so.

## How the radarx backend is applied to polar data

radarx works on regular x/y grids. A sweep is resampled (nearest neighbour) onto a radar-centred grid
(0.5 km by default, coarser for very long ranges so the grid stays below about 1000 x 1000), radarx
labels the cells there, and the labels are mapped **back to the native polar gates**. Area, mean / peak
reflectivity, centroid and outline are then computed on the original gates, exactly like the numpy
backend. Cells touching the 0/360 seam need no special handling on the grid.

Tracking rasterises each scan's cell masks (or, for plain `list[CellInfo]`, the cell outlines) onto one
common grid and tracks them with radarx. Without a given motion it runs twice: a first pass
to measure the median step velocity of the cells that kept their size and neither split nor merged, a
second pass with the cells advected by that motion. Extra per-cell fields (VIL, echo top) are summarised
with radarx's cell statistics.

The two backends do not give identical cells: the grid resampling changes connectivity of thin or
nearly touching echoes. On the KTLX example volume (35 dBZ, 10 km2) both find the same strongest cell
and total area within a few percent, but radarx returns somewhat fewer (merged) small cells.

## Statistics

| Column | Meaning |
|---|---|
| Area, Max, Mean | area of the gates >= threshold (km2), peak and mean reflectivity (dBZ) |
| Azimuth, Range | area-weighted centroid (circular mean across the seam) |
| Lat, Lon | centroid position (needs the radar position in the file) |
| Echo top (sweep) | highest **beam-centre height** (4/3 Earth radius) among the cell's gates on this sweep. This is a lower bound of the real echo top, not a measurement of it |
| VIL max / mean, Echo top | from the whole volume with radarx (`vil`, `echo_top` at 18 dBZ); only for multi-sweep volumes with radarx installed, otherwise the columns are absent |
| Speed, From | storm motion: the manual motion parameter, or the tracked velocity |
| Scans, Track | length and id of the track the cell belongs to |

Columns that have no data for a file are not shown.

## Storm Cell Detection module

Parameters (all old ones keep their meaning and defaults): `threshold_dbz`, `min_size_km2`; new:
`split_cells` (separate touching cores 10 / 20 dBZ above the threshold, radarx), `with_vil`
(VIL / echo-top columns, default on, skipped without radarx or for single-sweep files),
`motion_speed` / `motion_from` / `arrow_minutes` (a manual storm motion that fills the speed columns
and draws a motion arrow on every cell; 0 = off), `backend`.

Outputs: the summary (cell count, largest cell, peak, threshold, **Algorithm**), a sortable table, and
an overlay with a contour and a `C001 · 55 dBZ` label per cell plus, with a motion, an arrow
(`<id>:motion`) to where the cell will be after `arrow_minutes`.

## Limits

* Split / merge flags need the radarx backend; the numpy backend never reports them.
* The estimated motion is one vector for the whole scan (the median of steady cells); storms moving
  differently from each other link by overlap or the `max_distance_km` fallback only.
* One sweep is one level: the echo-top proxy, area and overlap are those of that elevation.
