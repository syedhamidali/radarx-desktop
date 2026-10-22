# Everything from the terminal

This page is for working with Radarx **without a window**: on a server, in a cluster job, over SSH, or simply because
you like a terminal. It is organised by what you want to do, not by command. For every task you get a command to
copy, what you get back, where the result is written, and the matching place in the app. Every option of every
command is on the {doc}`command line page <CLI>`; installing on a machine without a screen and signing in there
is on {doc}`cluster`.

```{note}
**What was run for this page.** The commands below were run for real on a Mac with small NEXRAD Level II volumes
of the 11 December 2021 case (radars KHPX, KNQA and KPAH, about 8 to 12 MB each), and the output shown is what they
printed (shortened with `...` where it was long). Movie rendering was **not** run (it needs Google Chrome), and nothing
was run on a cluster or on Windows; the pages say so where it matters. Your file names, times and numbers will differ.
```

## Before you start

* You need the `rxd` command on your `PATH` and a signed-in account. If `rxd --version` prints a version and
  `rxd whoami` says who you are, you are ready. If not, see {doc}`installation` and {doc}`cluster`.
* **Give every command the memory it may use** with `--budget-gb`, for example `--budget-gb 4` (or set `RADARX_BUDGET_GB=4`
  once for a whole script). Without it `rxd` chooses a budget from the free memory of the whole computer, which on a
  shared machine can be more than you were given. See [Memory budget](#can-this-computer-do-it-rxd-budget).
* Nothing is downloaded by `rxd` on its own: fetch your files first, then point `-i` at them.
* A file name, a list of names, a quoted pattern (`'data/*_V06'`) and a folder all work after `-i`. Quote patterns so
  that `rxd` expands them, not your shell.
* Results without `-o` are written to the **current folder** with a name made from the input, and `rxd` prints the name.
  In scripts always give `-o`.

The examples use these files (replace them with yours):

```text
data/KHPX/KHPX20211211_023231_V06      data/KHPX/KHPX20211211_023814_V06      data/KHPX/KHPX20211211_024357_V06
data/KNQA/KNQA20211211_023037_V06      data/KPAH/KPAH20211211_023519_V06
```

## Look at one file

```bash
rxd info -i data/KHPX/KHPX20211211_023231_V06
```

```text
KHPX20211211_023231_V06
  format     nexrad_level2
  radar      KHPX at 36.7370, -87.2856, 186 m
  time       2021-12-11T02:32:31Z
  size       7.7 MB on disk, 460.7 MB decoded
  variables  CCORH DBZH PHIDP RHOHV VRADH WRADH ZDR
  sweeps     19
    idx   elev  mode          rays  gates  range_km  nyquist  variables
    ---  -----  ------------  ----  -----  --------  -------  --------------------------------------
      0   0.48  surveillance   720   1832       460  -        CCORH DBZH PHIDP RHOHV ZDR
      1   0.48  surveillance   720   1192       300  ~24.5    DBZH VRADH WRADH
...
```

* **You get:** the format, radar, time, sweeps and variables. "Decoded" is the memory the volume needs once it is read:
  plan your memory with it. `--json` prints the same as one JSON document (for scripts).
* **Where it goes:** the terminal only. Several files, a folder or a pattern give one report each.
* **In the app:** the Data Inspector, which opens with the file.

A picture of one variable:

```bash
rxd plot -i data/KHPX/KHPX20211211_023231_V06                              # reflectivity, lowest sweep
rxd plot -i data/KHPX/KHPX20211211_023231_V06 -V velocity --theme dark -p vel.png
```

```text
using variable DBZH (the first reflectivity field; -V NAME to change)
wrote KHPX20211211_023231_V06_DBZH_sweep0.png
using sweep 1 (the lowest sweep with velocity; choose with -s N)
wrote vel.png
```

* **You get:** a PPI picture with range rings and a colour scale (`.png`, `.svg` or `.pdf` by the extension of `-p`;
  `--dpi`, `--vmin`, `--vmax`, `--title`, `--no-rings`). It is drawn to a file, no window opens.
* **Where it goes:** the file you name, or `<file>_<variable>_sweep<N>.png` in the current folder.
* **In the app:** the PPI view and the Export panel (quick export). A moving or 3D view has no command: see
  [the table at the end](#which-app-feature-has-which-command).

## Check the data quality

```bash
rxd check -i data/KHPX/KHPX20211211_023231_V06 --budget-gb 4
```

```text
data/KHPX/KHPX20211211_023231_V06  (nexrad_level2, 19 sweeps, S band; checked in 1.0 s)
  2 warning, 3 notice, 4 info. Nothing was changed; ESTIMATE values name their method.

  WARNING Range-folded gates   (confidence 0.90; sweeps 1, 3, 5 (+4 more); ...)
    Sweep 6 (Doppler velocity): 10.71 % of the gates with a signal are range-folded ...
...
  WARNING Velocity looks aliased (folded)   (confidence 0.65; ...)
...
```

* **You get:** what looks wrong (folded or aliased velocity, clutter, a ZDR offset, gaps ...), with the evidence and
  where it is. It changes nothing, and the exit code is 0 even when it finds problems (3 only when a file cannot be read).
  Every check is explained on {doc}`DATA_CHECK`.
* **Fix plan:** add `--fix-plan -o recipe.json` to also write the suggested cleaning recipes. Because there is one
  recipe per variable, the files are named `recipe_velocity.json`, `recipe_reflectivity.json` and `recipe_zdr.json`
  (only the ones that are needed). The command prints the `rxd pipeline` line to run each.
* **In the app:** the Data check chip after opening a file.

## Clean the data (pipelines and recipes)

A recipe is an ordered list of cleaning steps (noise gate, echo mask, clutter filter, biological-echo filter,
de-aliasing, speckle removal). List them, look inside one, run one:

```bash
rxd pipeline list
rxd pipeline show "Reflectivity QC"
rxd pipeline "Reflectivity QC" -i data/KHPX/KHPX20211211_023231_V06 -s 0 -o refl_qc.json -p qc.png --budget-gb 4
rxd pipeline recipe_velocity.json -i data/KHPX/KHPX20211211_023231_V06 -s 1 -o vel_qc.json   # a recipe file from rxd check
```

```text
Reflectivity QC  |  KHPX20211211_023231_V06  |  target DBZH  |  1 sweep(s)  |  0.5 s
  #  step                      status   removed  modified  filled  valid%  note
  -  ------------------------  -------  -------  --------  ------  ------  --------------------------
  1  SNR gate                  skipped        0         0       0   100.0  no SNR field on this sweep
  2  Meteorological echo mask  ok         47827         0       0    77.4
  3  Ground clutter filter     ok          1745         0       0    76.6
  4  Remove speckle            ok             2         0       0    76.6
  5  Fill small gaps           ok             0         0    1462    77.2
  derived variable: pipeline:reflectivity_qc_dbzh
```

* **You get:** the table above, plus the files you ask for: `-o x.nc` is the whole volume with the cleaned field added as a
  derived variable, `-o x.json` the statistics, `-p x.png` a before-and-after picture.
* **Where it goes:** the file you name. A derived variable exists only inside one run, so without `-o` the result is saved as
  `<file>_<recipe>.nc` in the current folder; otherwise it would be lost. Without `-s` every sweep is processed.
* **Change a setting:** `-P snr_gate.min_snr=5`, or switch a step off with `-P gap_fill=off`. A recipe saved from the app can be
  used by name or by its file ({doc}`PIPELINES`).
* **If the memory budget is too small** for the file being written, `rxd` stops at the start and says what to change instead of
  failing halfway. A real example (the `.nc` of a whole volume under a 4 GB budget):
  `error: export needs about 4.0 GB; this machine allows 4.0 GB. You can: allow more memory: --budget-gb 4.5 ...; export CfRadial2 or Zarr (written sweep by sweep) instead of NetCDF CfRadial1`.
* **In the app:** the Pipeline panel.

## Run an algorithm

```bash
rxd modules                                   # every algorithm: id, category, what it needs
rxd modules show storm_detection              # its purpose and every parameter with the default
rxd run storm_detection -i data/KHPX/KHPX20211211_023231_V06 -P threshold_dbz=45 -o cells.csv --budget-gb 4
rxd run radarx_hid -i data/KHPX/KHPX20211211_023231_V06 -p hid.png --budget-gb 4
rxd run radarx_hid -i data/KHPX/KHPX20211211_023231_V06 --all-sweeps -o hid_all.nc --budget-gb 4
```

```text
storm_detection  Storm Cell Detection  |  KHPX20211211_023231_V06  |  sweep 0  |  1.1 s
  summary
    Cells: 9
    Largest cell: 538.335
    Max reflectivity: 68.5
    ...
  table: Storm cells (9)
      id  centroid_az  centroid_range_km  area_km2  max_dbz  mean_dbz     lat      lon ...
    C001        260.1              192.0     538.3     68.5      52.7 36.4206 -89.3997 ...
...
wrote cells.csv
```

* **You get:** a summary, tables and gridded fields, printed. `-o` saves them by its extension: `.csv` (a table), `.json` (the
  summary), `.nc` (the fields), `.png` (a picture), or a folder (`-f json,csv,nc,png` chooses). Parameters are `-P name=value`; a
  misspelled name is caught with a suggestion.
* **Where it goes:** the file or folder from `-o`. With `--all-sweeps` and no `-o`: `<file>_<module>_all.nc`.
* **Without `-s`** the lowest sweep that has every field the module needs is used, and `rxd` says which. NEXRAD velocity is on
  sweep 1, not sweep 0.
* **A time limit:** `--timeout 600` stops the module after 600 seconds.
* **In the app:** the Modules panel. {doc}`MODULES` lists the algorithms; algorithms with `radarx` in their name need the
  optional radarx library ([Install](#the-optional-radarx-library)).

## Storm cells and tracks

```bash
rxd cells -i data/KHPX/KHPX20211211_023231_V06 data/KHPX/KHPX20211211_023814_V06 data/KHPX/KHPX20211211_024357_V06 \
    --threshold-dbz 45 -o cells_all.csv --tracks tracks.csv -p tracks.png --budget-gb 4
```

```text
14 track(s)  |  3 scans  |  30 cells  |  backend numpy
track_id  lifetime  speed_mps  direction_deg    status  max_dbz
    T001         3      24.21          237.5 continued     68.5
    T002         3      20.96          228.2 continued     61.5
...
wrote cells_all.csv
wrote tracks.csv
wrote tracks.png
```

* **You get:** one file lists the cells; several files **of one radar** are tracked in time order (speed, direction, lifetime).
* **Where it goes:** the three files named. Without `-o` the table is only printed.
* **In the app:** the Storm panel ({doc}`CELLS`).

## Cross-sections, RHI and soundings

* **Cross-sections** (a vertical slice along a line): the app only. There is **no command for them yet**. The closest things
  in the terminal are the profile algorithms: `rxd run radarx_qvp` (a quasi-vertical profile of reflectivity, ZDR and RhoHV)
  and `rxd run radarx_vvp_winds` (a wind profile). The first was run for this page:

  ```bash
  rxd run radarx_qvp -i data/KHPX/KHPX20211211_023231_V06 -o qvp_out --budget-gb 4
  ```

  It printed a summary (`Max Z`, `Height of max Z`, `Bright-band signature: not found` ...) and a table of height against
  `DBZH ZDR RHOHV count`, and wrote a `.json` and a `.csv` into the folder `qvp_out`.
* **RHI scans** (vertical scans of one azimuth): the viewer is in the app ({doc}`RHI`). `rxd plot` draws an RHI file as a
  range-height figure, as described on that page; that was **not run here** (no RHI file was at hand). Autopilot
  (below) recognises RHI scans when it plans.
* **Soundings** (a radiosonde file you already have): 

  ```bash
  rxd sounding -i sonde.csv --wind-units kt -o sonde.json -o profile.csv
  ```

  ```text
  BMX launch time not in the file  (csv, 80 levels, sonde.csv)
  data class: observed; This file has no per-level positions or times: ...
  surface-based  CAPE 101 J/kg  CIN -3 J/kg  LCL 1635 m AGL  LFC 1808 m AGL  EL 6584 m AGL  LI -1.1 K
  ...
  bulk shear 0-6km 15.8 m/s
  SRH 0-3km right 247 m2/s2  left 66 m2/s2
  freezing level 3520 m AGL   wet-bulb zero 2454 m AGL   -20 degC 6289 m AGL
  precipitable water 22 mm (to 7.0 hPa)
  ```

  **You get** the numbers, each labelled with its parcel, unit and height reference; `-o result.json` writes everything and
  `-o profile.csv` the levels. The Skew-T diagram is drawn by the app only. **In the app:** the Sounding panel
  ({doc}`ENVIRONMENT`).

## Combine radars: mosaics and winds

Both need the radarx library ([Install](#the-optional-radarx-library)). Give the volumes of **one moment** from several radars.

```bash
rxd mosaic -i data/KHPX/KHPX20211211_023231_V06 data/KNQA/KNQA20211211_023037_V06 \
    -o mosaic.nc -p mosaic_map.png --budget-gb 4
```

```text
composite of DBZH  |  2 radar(s)  |  693 x 617 cells @ 1000.0 m
  extent  W -92.403  S 33.271  E -84.711  N 38.809  (degrees)  (chosen to cover all radars; --extent W,S,E,N to change)
  values  valid 19% of cells  min -20.0  max 67.1  mean 22.7 dBZ
  radar     lat      lon  volume time           coverage%  share%
  -----  ------  -------  --------------------  ---------  ------
  KHPX   36.737  -87.286  2021-12-11T02:35:07Z         39      43
  KNQA   35.345  -89.873  2021-12-11T02:33:16Z         39      57
wrote mosaic.nc
wrote mosaic_map.png
```

* **You get:** the merged grid. `-o x.nc` is the gridded NetCDF, `-o x.png` a colour image **plus** its world file (`.pgw`) and
  projection (`.prj`) so GIS software places it, `-p map.png` a map figure with the radar sites. `--product cappi --height-m 2000`
  or `--product echo_top` change the product.
* **In the app:** the Mosaic panel ({doc}`MOSAIC`).

```bash
rxd winds -i data/KHPX/KHPX20211211_023231_V06 data/KPAH/KPAH20211211_023519_V06 \
    --dx-m 2000 --dz-m 1000 --zmax-m 6000 -o winds.nc -p winds.png --level-km 2 --budget-gb 4
```

```text
analytic winds  |  2 radar(s)  |  grid 35 x 71 x 5 (dx 2000 m)
  origin  36.9050, -88.0272  (centre of the radars; --origin LAT,LON to change)
  levels  2.00 to 6.00 km above sea level (5)
  radar   KHPX  2021-12-11T02:35:15Z
  radar   KPAH  2021-12-11T02:37:58Z
  warning: KHPX: dealiasing could not unfold every aliased sweep (0 failed, 1 still show folds); winds there may be wrong.
  warning: No freezing level is known, so the rain fall-speed relation is used at all heights; ...
wrote winds.nc
wrote winds.png
```

* **You get:** a wind analysis from at least two radars, and the warnings, which are real: read them. Two radars that are too far
  apart say so and stop. Here KHPX and KNQA (about 270 km apart) gave
  `error: The selected radars have no overlapping coverage within 100 km of each radar, so no wind can be retrieved. Increase the maximum range or select radars closer together.`;
  the closer pair above worked. `--method variational` and the other options are on {doc}`MULTIDOPPLER`.
* **Where it goes:** `-o` (`.nc` analysis or `.json` description) and `-p` (a map of the level `--level-km`). Without them, files named
  `winds_<time>.nc` and a map are written to the current folder.
* **In the app:** the Wind panel. The 3D flow view has no command.

For a mosaic or winds at every time step of an event, loop over the time stamps: {doc}`BULK`, section 8.

## Rain: QPE and river basins

Both need the radarx library. Several volumes of one radar in time give an accumulation in mm; one volume per radar gives a rate.

```bash
rxd qpe -i data/KHPX/KHPX20211211_023231_V06 data/KHPX/KHPX20211211_023814_V06 data/KHPX/KHPX20211211_024357_V06 \
    --extent -88.5,36,-86,38 --resolution-m 2000 -o rain.nc -p rain.png --budget-gb 4
```

```text
rainfall accumulation  |  1 radar(s), 3 volume(s)  |  2021-12-11T02:32:40Z to 2021-12-11T02:44:06Z (11.4 min)  |  119 x 117 cells @ 2000 m  |  7.6 s
  method   hybrid: Z = 300 R^1.4 ...
  label    RETRIEVED: an estimate, not an observation
  values   max 9.9 mm  mean 0.10 mm over 55,674 km2  |  max rate 103 mm/h  |  area >= 0.1 mm 5,272 km2
  uncertainty  median 33 % (1 sigma: relation scatter, calibration; errors fully correlated in time)
  ...
wrote rain.nc
wrote rain.png
```

* **You get:** the accumulation grid (`.nc`), a picture, and the numbers above, labelled RETRIEVED, or ADJUSTED after gauges
  (`--gauges FILE`, a local file). `-o x.json` writes the full summary. {doc}`HYDROLOGY` has the equations and limits.
* **Giving `--extent W,S,E,N`** keeps a long series cheap: each file is decoded once for that box.
* **In the app:** the Rainfall (QPE) panel.

```bash
rxd basin -i data/KHPX/KHPX20211211_023231_V06 data/KHPX/KHPX20211211_023814_V06 data/KHPX/KHPX20211211_024357_V06 \
    -b basins.geojson --extent -88.5,36,-86,38 --resolution-m 2000 --bin-minutes 5 -o basin.csv -p basin.png --budget-gb 4
```

```text
  basin  area km2  mean mm  max mm  peak mean mm/h  coverage %
  -----  --------  -------  ------  --------------  ----------
  B1     7,882         0.4     9.9             2.1         100
wrote basin.csv
wrote basin_B1_series.csv
wrote basin.png
```

* **You get:** the area-weighted rain over each polygon you supply (`.geojson`, `.shp` or `.gpkg`): a table, one time-series file per
  basin and a hyetograph picture. `--frequency table.json` adds a return period from a table you supply (never downloaded).
* **In the app:** the Rainfall panel; the basin table is in the Command Studio only ({doc}`command-studio`).

## Environment data

Model fields, reanalyses, satellite images and the like are fetched, cached and labelled **in the app**, which asks before
any download ({doc}`ENVIRONMENT`). `rxd` has **no command for them** and never downloads anything. What you can do in the terminal
is use files you already have: a radiosonde file with `rxd sounding` (above), rain-gauge files with `rxd qpe --gauges`, a terrain file
with `rxd qpe --dem`, a basin file with `rxd basin -b`.

## Movies

```bash
rxd animate -i data/KHPX/KHPX20211211_023231_V06 data/KHPX/KHPX20211211_023814_V06 data/KHPX/KHPX20211211_024357_V06 \
    --no-render --detail-m 1000 --budget-gb 4
```

```text
3 time steps from 1 radar(s) (KHPX); reference KHPX
nothing was drawn (--no-render): this is the plan; remove --no-render to render it
KHPX  11 Dec 2021  |  1 radar(s)  |  3 time steps over 11.4 min  |  1080 frames (36 s at 30 fps, 1920x1080)
  time steps  reference radar KHPX, tolerance 330 s; each radar contributes its nearest volume (offset in s from the reference)
...
  storm       followed with the 'cells' tracker: moved 15.2 km toward 56 deg in 11.4 min (mean 22.2 m/s)
...
  frames      132 observed (12%), 948 interpolated (88%); gaps between volumes 5.7 to 5.7 min; ...
  grids       3 steps at 1000 m: 3 computed, 0 from the cache (...); peak memory 1705 MB
```

`--no-render` is the **plan**: which volumes become time steps, the storm's track and how many frames are real or interpolated.
It needs no browser and was run for this page. **The film itself was not run here**; from `rxd animate --help` and
{doc}`ANIMATE`, the real commands are:

```bash
rxd animate -i 'data/*/*_V06' -o storm.mp4                              # the film (needs Chrome and the movie components)
rxd animate -i 'data/*/*_V06' -p still.png --still-at 12                # one frame, to try a camera or a look
rxd animate -i 'data/*/*_V06' -o storm.mp4 --winds --sound --flashes-from-glm glm/   # with a synthesised sound track
```

* **You get:** an MP4 (or numbered frames with `--encoder png`) and a record file next to it. `--sound` adds a **synthesised**
  sound (wind, rain and thunder timed to flashes from local files; it is labelled as not a recording).
* **What it needs:** Google Chrome, the movie components and the radarx library: see [What needs a display or Chrome](cluster.md#what-needs-a-display-or-chrome).
  `--cache-dir DIR` moves the cache of gridded volumes (by default in your home folder, which can be small on a cluster).
* **In the app:** the Movie Studio ({doc}`ANIMATE`, {doc}`STORM_CLIP`). The GIF through the sweeps is an app export without a command.

## Export with standard outputs

```bash
rxd export -i data/KHPX/KHPX20211211_023231_V06 -o khpx.nc               # CfRadial1 NetCDF (the default)
rxd export -i data/KHPX/KHPX20211211_023231_V06 -f cfradial2 -o khpx_cf2.nc
rxd export -i data/KHPX/KHPX20211211_023231_V06 -o khpx.zarr             # Zarr (a folder)
rxd export -i data/KHPX/KHPX20211211_023231_V06 -o dbz0.csv -V DBZH -s 0 # one sweep as a table
rxd export -i data/KHPX/KHPX20211211_023231_V06 -o qc.nc --pipeline "Reflectivity QC" --run radarx_hid --include-derived
```

* **You get:** a file in the format of the extension (`.nc`, `.zarr`, `.csv`, `.png`, `.svg`, `.pdf`; `-f cfradial2` for CfRadial2). A volume
  of 7.7 MB on disk gave a 15 MB NetCDF, a 15.6 MB CfRadial2 file and a 24 MB Zarr folder here. The files follow CF and CfRadial
  conventions and say where they came from: the software version, a `history` line, the input file names and, for derived
  fields, the method ({doc}`OUTPUT_STANDARDS`).
* **Where it goes:** the `-o` path; without it a NetCDF named after the input.
* **Derived variables** (cleaned fields, classes) exist only inside one run, so `--pipeline` and `--run` compute them first and
  `--include-derived` writes them into the file.
* **In the app:** the Export panel ({doc}`export`).

## "I have a pile of data, decide what to do": Autopilot

```bash
rxd auto data/KHPX/KHPX20211211_023231_V06 data/KHPX/KHPX20211211_023814_V06 data/KHPX/KHPX20211211_024357_V06 --plan-only --budget-gb 4
```

```text
Situation: Time series of one radar
  3 file(s), 1 radar(s): KHPX
  KHPX: 3 volume(s) from 2021-12-11T02:32:31+00:00 to 2021-12-11T02:43:57+00:00, cadence 343 s
Budget: RAM 17.2 GB, available 5.0 GB -> budget 4.3 GB (as given); ~1.8 min for the enabled steps (ESTIMATE)

Plan:
  [x] 1. Data check report of every radar  (~1 s, ~0.6 GB, 1 volumes)
       why: Know the problems (aliasing, clutter, noise, calibration offset) before any product is made.
       ...
       rxd: rxd check -i "data/KHPX/KHPX20211211_023231_V06" --json
  [x] 2. Clean-up (QC recipe suggested by the data check)  (~35 s, ~1.2 GB, 3 volumes)
  [x] 3. Hydrometeor classification  ...
  [x] 4. Storm cells and tracks  ...
  [x] 5. Rainfall accumulation with uncertainty  ...
  [m] 6. Storm animation (3D clip)  ...  manual: not run here; use the command

Not possible with this data:
  - mosaic: missing a second radar whose coverage overlaps in space and time
  - multi-Doppler winds: missing a second radar with Doppler velocity 30-150 km away
```

Autopilot looks at your inputs (a file, a folder, hundreds of volumes, several radars), says what the data allows and
refuses what it does not allow, with the reason. `--plan-only` runs nothing and writes nothing. To run, approve the steps:

```bash
rxd auto data/KHPX/KHPX20211211_023231_V06 data/KHPX/KHPX20211211_023814_V06 data/KHPX/KHPX20211211_024357_V06 \
    --steps check,cells -o auto_out --budget-gb 4        # only these steps (this implies your consent)
```

* **You get:** the results of the approved steps, a run record (`run_record.json`), a one-page `SUMMARY.md` that lists every
  decision and its reason, and a small state file; a later run skips the steps that finished. The `--steps check,cells` run above wrote
  `check_KHPX_20211211T023231.json`, `tracks_KHPX.csv`, `tracks_KHPX.png` and those three files.
* **Where it goes:** the folder from `-o` (default `./autopilot_<time>`).
* **Without `--yes` or `--steps`** it waits for your approval on the terminal. In a job give `--steps ...` (only those steps) or `--yes`
  (every enabled step, no questions); `--yes` was not run for this page.
* The animation step is **planned, never rendered** inside a run. {doc}`AUTOPILOT` and {doc}`tutorial-bulk-data` explain the plan.
* **In the app:** Autopilot (drop a folder on the window, or the command palette).

## Can this computer do it? `rxd budget`

```bash
rxd budget -i data/KHPX/KHPX20211211_023231_V06 data/KNQA/KNQA20211211_023037_V06 --budget-gb 4
```

```text
memory  budget 4.0 GB (cli), hard limit 4.0 GB  |  free now 5.3 GB of 16.0 GB
cores   10
disk    54.2 GB free in ...

inputs  2 file(s), 2 radar(s) KHPX, KNQA  |  16 MB on disk, about 1.1 GB decoded  (0 opened, the rest scaled by file size)

read          peak    1.8 GB  time    6.1 s  -> runs as asked
qc            peak    1.3 GB  time     16 s  -> runs as asked
mosaic        peak    939 MB  time    8.0 s  -> runs as asked
...
export NetCDF peak    4.9 GB  time     12 s  -> WILL NOT RUN
              export needs about 4.9 GB; this machine allows 4.0 GB
              - allow more memory: --budget-gb 5.4 (that much is free now)
              - export CfRadial2 or Zarr (written sweep by sweep) instead of NetCDF CfRadial1
export Zarr   peak    1.1 GB  time     13 s  -> runs as asked
```

Nothing is processed: one file per radar is opened to measure it, the others are scaled by their size. Each operation ends as
**runs as asked**, **fits with changes** (each change listed with what it costs scientifically) or **will not run** (with what you can
change). The same decision is taken automatically when `mosaic`, `winds` or a NetCDF `export` runs, so a job that cannot fit stops at
the start. The estimates are approximate (about 25 %): in the run above the plain NetCDF export of one of these volumes
in fact finished within the same 4 GB budget, so treat a "will not run" as a warning to read, not a measurement.

* **Set the budget:** `--budget-gb 4` on any command, or `RADARX_BUDGET_GB=4` in the environment. `RADARX_GOVERNOR=strict` makes `rxd`
  refuse instead of ever reducing data. Without either, the budget is the smaller of 60 % of the computer's memory and 80 % of the
  memory free now. **On a shared machine that is the whole machine's memory, not your share:** always give the number
  ({doc}`cluster`).
* `--max-minutes 10` also thins long time series to fit a time limit; `--ops mosaic winds` plans only those.
* **In the app:** the memory budget of Autopilot, and the reductions listed in the run record.

## Reproduce a run

* **Print the command:** `--print-command` prints, at the end, the full command with everything that was chosen for you (the
  sweep, the output name, the budget). Keep that line in your log.
* **Keep an analysis in a file:** `--config run.toml` reads the same options (long names). Options on the command line win, so a file is
  a template for many inputs. JSON works everywhere; TOML needs Python 3.11 or newer.

  ```toml
  command = "run"
  module = "storm_detection"
  input = ["data/KHPX/KHPX20211211_023231_V06"]
  output = "cells_from_config.csv"

  [param]
  threshold_dbz = 45
  ```

  ```text
  $ rxd --config run.toml --print-command --budget-gb 4
  ...
  wrote cells_from_config.csv
  command: rxd run storm_detection --input data/KHPX/KHPX20211211_023231_V06 --output cells_from_config.csv --param threshold_dbz=45 --budget-gb 4
  ```

* **The result says how it was made.** Written NetCDF files carry the software and library versions, the time, the input file names,
  a `history` line with the method and its settings, and a provenance class (observed, retrieved, ...). For example the history of a
  mosaic lists the radars, the grid size and the merge settings. CSV exports start with comment lines giving the same.
* **Autopilot's run record** (`run_record.json` and `SUMMARY.md`) lists inputs, versions, every decision and its reason, and the commands.
  A movie writes a record next to the film ({doc}`ANIMATE`).
* **Machine-readable output:** `--json` prints one JSON document on stdout and nothing else; messages go to stderr.

## Which app feature has which command

| App feature | Command | Note |
|---|---|---|
| Open a file, Data Inspector | `rxd info` | |
| Data check | `rxd check` | `--fix-plan -o recipe.json` writes the recipes |
| PPI view | `rxd plot` | a picture file, not an interactive view |
| Export panel (figure, data) | `rxd plot`, `rxd export` | |
| Pipeline panel and recipes | `rxd pipeline` | list, show, run, script; recipes saved in the app work by name or file |
| Modules panel | `rxd modules`, `rxd run` | |
| Storm panel | `rxd cells` | |
| Sounding panel | `rxd sounding` | numbers and tables; no Skew-T diagram |
| Mosaic panel | `rxd mosaic` | needs radarx |
| Wind panel | `rxd winds` | needs radarx; the 3D flow view has no command |
| Rainfall (QPE) panel | `rxd qpe`, `rxd basin` | needs radarx |
| Movie Studio, storm clips | `rxd animate` | needs Chrome to draw; `--no-render` gives the plan |
| Autopilot | `rxd auto` | |
| Memory budget | `rxd budget`, `--budget-gb` | |
| Account | `rxd login`, `rxd logout`, `rxd whoami` | |
| Command Studio | the commands themselves | a form for `rxd`, nothing extra |
| RHI viewer | `rxd plot` draws RHI files as a figure | not run here |
| 2D probe, measuring, zoom, 3D volume view | **no command yet** | interactive |
| Cross-section | **no command yet** | `rxd run radarx_qvp` and `radarx_vvp_winds` give profiles |
| Environment data (model, reanalysis, satellite) | **no command yet** | `rxd` never downloads; use your own files |
| Cloud data browser, radar network map, radar sites, live map | **no command yet** | fetch files yourself, then use `-i` |
| GIF through the sweeps | **no command yet** | |
| Settings, workspaces, looks | **no command yet** | options are given per command or in a `--config` file |

## When a command stops

Errors say what went wrong and how to fix it; a traceback appears only with `-vv`. The exit code tells a script what happened:

| Exit code | Meaning |
|---|---|
| 0 | success |
| 1 | the processing failed (the algorithm could not run on this data) |
| 2 | wrong use: an unknown option, module, recipe or variable |
| 3 | an input file is missing or cannot be read |
| 4 | an optional package is missing; the message says how to install it |
| 5 | not signed in: `rxd login` |
| 70 | an unexpected internal error: re-run with `-vv` and report it |
| 130 | interrupted |

Real examples from this page: a missing file printed `error: File not found: missing.nc (looked for ...)` and exited **3**; a misspelled
command (`rxd mosiac`) printed `error: unknown value 'mosiac'. Did you mean 'mosaic'?` and exited **2**; an empty (0-byte) file printed
`error: Could not open badfile_V06: the file is empty (0 bytes). A download or copy probably failed; fetch it again.` and exited **1**
(not 3), so a script should treat 1 and 3 alike for a bad input file. Without a session every command except `login`, `logout`, `whoami`,
`examples`, `completion`, `--help` and `--version` printed how to sign in and exited **5**.

Many files, many jobs, parallel runs and nightly jobs: {doc}`BULK`. Clusters: {doc}`cluster`.

## The optional radarx library

`mosaic`, `winds`, `qpe`, `basin`, `animate`, `sounding` and every algorithm named `radarx_...` need the radarx library, which needs
Python 3.12 or newer:

```bash
pip install -U radarx
```

Without it these commands stop with exit code **4** and a message that says how to install it. `rxd modules --available` hides the
algorithms whose package is missing.
