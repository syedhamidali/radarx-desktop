# The `xrd` command line

Everything the desktop app does can be done from a terminal, a shell script or a cron job, with
the same engine and the same results: look at a file, clean it, run an algorithm, merge radars,
save the outcome. No window, no server.

```text
xrd                       # a friendly overview of everything below
xrd examples              # a guided tour of copy-paste commands
xrd COMMAND --help        # purpose, options, real examples
```

## Quick start

The six commands most people need (replace the file name with yours):

```bash
xrd info -i KTLX20130520_201643_V06.gz                       # 1. what is in the file?
xrd plot -i KTLX20130520_201643_V06.gz                       # 2. a picture -> KTLX..._DBZH_sweep0.png
xrd run radarx_hid -i KTLX20130520_201643_V06.gz -p hid.png  # 3. an algorithm (hydrometeor classes)
xrd pipeline "Doppler velocity" -i KTLX20130520_201643_V06.gz -p before_after.png   # 4. clean the data
xrd mosaic -i KHPX..._V06 KNQA..._V06 KPAH..._V06            # 5. several radars on one map
xrd export -i KTLX20130520_201643_V06.gz -o ktlx.zarr        # 6. save it (NetCDF, Zarr, CSV, PNG)
```

Without `-o`/`-p` a sensible file name is chosen, written to the current directory and printed,
and everything that was chosen for you is said in one line (`using ...`, `wrote ...`).

## Install

```bash
pip install -e python/            # the engine and the `xrd` command   (python -m engine works too)
pip install -U radarx             # optional: mosaics, winds and the radarx_* algorithms (Python >= 3.12)
xrd --version
```

`pip install -e` (an editable install) is what makes the shipped algorithms in `python/modules/`
visible; add your own folder with `export XRADAR_MODULES_DIR=~/my_modules`.

Tab completion: `xrd completion bash > ~/.local/share/bash-completion/completions/xrd`,
`source <(xrd completion zsh)` or `xrd completion fish > ~/.config/fish/completions/xrd.fish`.

## The same options everywhere

| Option | Meaning |
|---|---|
| `-i`, `--input FILE [FILE ...]` | input file(s), globs, folders or `-` (a list on stdin); repeatable; bare `FILE` arguments work too |
| `-o`, `--output PATH` | the result; the **extension** says what is written (`.nc` `.zarr` `.csv` `.json` `.png`); a folder gets one file per format |
| `-p`, `--plot PATH` | also write a figure (`.png` `.pdf` `.svg`) of the result |
| `-P`, `--param KEY=VALUE` | a module / pipeline-step / mosaic / winds parameter (repeatable) |
| `-V`, `--variable NAME` | a variable, any spelling the variable mapping knows (`velocity`, `VRADH`, `VEL` ...) |
| `-s`, `--sweep N` | sweep index; default: **the lowest sweep that has what is needed** |
| `-f`, `--format FMT` | only when the `-o` extension is not enough |
| `-v`, `-q`, `--json` | progress and details (`-vv`: engine log and tracebacks) / only errors / machine-readable output |
| `--config FILE`, `--print-command` | keep an analysis in a file / print the equivalent full command |

The Python API uses the same names: `files` (`-i`), `out` (`-o`), `plot` (`-p`), `variable` (`-V`),
`sweep` (`-s`), keyword parameters (`-P`); see [API.md](API.md).

## The commands, in the order you work

| Step | Command | Purpose |
|---|---|---|
| Look | `xrd info` | format, radar, time, sweeps, variables, Nyquist velocity |
| | `xrd plot` | a PPI picture of one variable |
| Clean | `xrd pipeline` | QC recipes: `list`, `show`, `run` (default), `script` |
| Analyse | `xrd run MODULE` | an algorithm on one sweep or all sweeps (same as `xrd modules run`) |
| | `xrd modules` | `list` (default) or `show ID` the algorithms |
| | `xrd cells` | storm cells; several files are tracked in time |
| Combine | `xrd mosaic` | several radars on one map |
| | `xrd winds` | 3-D winds from two or more radars |
| | `xrd animate` | a storm-centred 3D animation (MP4) from many volumes |
| Save | `xrd export` | NetCDF, CfRadial2, Zarr, CSV or an image |
| Help | `xrd examples`, `xrd completion` | a tour of commands; shell completion |

### Look: `info` and `plot`

```console
$ xrd info -i KTLX20130520_201643_V06.gz
KTLX20130520_201643_V06.gz
  format     nexrad_level2
  radar      KTLX at 35.3331, -97.2775, 388 m
  time       2013-05-20T20:16:43Z
  size       9.5 MB on disk, 331.5 MB decoded
  variables  DBZH PHIDP RHOHV VRADH WRADH ZDR
  sweeps     17
    idx   elev  mode          rays  gates  range_km  nyquist  variables
    ---  -----  ------------  ----  -----  --------  -------  --------------------------------
      0   0.48  surveillance   720   1832       460  -        DBZH PHIDP RHOHV ZDR
      1   0.48  surveillance   720   1192       300  ~26.0    DBZH VRADH WRADH
      2   0.88  surveillance   720   1832       460  -        DBZH PHIDP RHOHV ZDR
    ...
    (~ = estimated from the largest velocity in the sweep: the file has no Nyquist value)
```

Note how the NEXRAD split cut shows: velocity (`VRADH`) lives on the odd sweeps. You rarely need
to remember that: `xrd plot -V velocity` and `xrd run radarx_shear` pick the right sweep for you.

```console
$ xrd plot -i KTLX20130520_201643_V06.gz
using variable DBZH (the first reflectivity field; -V NAME to change)
wrote KTLX20130520_201643_V06_DBZH_sweep0.png

$ xrd plot -i KTLX20130520_201643_V06.gz -V velocity
using sweep 1 (the lowest sweep with velocity; choose with -s N)
wrote KTLX20130520_201643_V06_VRADH_sweep1.png
```

`xrd plot` uses the publication renderer of the app (range rings, colour scales per variable, no-echo
codes masked): `--theme dark`, `--dpi 300`, `--vmin/--vmax`, `-p figure.pdf` for a vector file.

### Analyse: `modules`, `run`, `cells`

```console
$ xrd modules --category Dual-pol
  id                    category  scope   requires          title
  --------------------  --------  ------  ----------------  -----------------------------------
  radarx_hid            Dual-pol  sweep   DBZH, ZDR         Hydrometeor Classification (radarx)
  radarx_kdp            Dual-pol  sweep   PHIDP             KDP Estimation (radarx)
  radarx_melting_layer  Dual-pol  volume  DBZH, ZDR, RHOHV  Melting Layer (radarx)
  radarx_qvp            Dual-pol  volume  DBZH, ZDR, RHOHV  Quasi-Vertical Profile (radarx)
  zdr_arc               Dual-pol  sweep   DBZH, ZDR         ZDR Arc Candidates (heuristic)

$ xrd modules show radarx_hid          # purpose, requirements and every parameter with its default
```

```console
$ xrd run radarx_hid -i KTLX20130520_201643_V06.gz -p cli-hid.png
radarx_hid  Hydrometeor Classification (radarx)  |  KTLX20130520_201643_V06.gz  |  sweep 0  |  1.3 s
  summary
    Classified gates: 132634
    Most common class: Light and moderate rain
    Method / band: park / S
    ...
  table: Class statistics
                      class  code  gates  share
    Light and moderate rain     6  47934   36.1
               Ice crystals     3  31875   24.0
                   Dry snow     1  22606   17.0
    ...
  field: Hydrometeor class (radarx) [class]  720x1832, 10.1% valid
wrote cli-hid.png
```

![Hydrometeor classes of KTLX sweep 0](images/cli-hid.png)

A module result can be saved by extension: `-o cells.csv` (a table), `-o summary.json`,
`-o fields.nc` (the gridded fields), `-o picture.png`, or `-o results/` (a folder with json, csv and nc;
`-f json,csv,nc,png` chooses). Parameters are `-P name=value`; a typo is caught with the fix:

```console
$ xrd run radarx_hid -i KTLX20130520_201643_V06.gz -P meth=park
error: Module 'radarx_hid' has no parameter 'meth'. Did you mean 'method'? Parameters: method, band, ...
```

**All sweeps.** `--all-sweeps` runs the module on every sweep and stores ONE derived variable per
output field in the file it writes (without `-o`: `<file>_<module>_all.nc`). Sweeps that lack an
input (here the Doppler cuts have no ZDR) are skipped, with the reason:

```console
$ xrd run radarx_hid -i KTLX20130520_201643_V06.gz --all-sweeps -o hid_all.nc -v
[  2%] Hydrometeor Classification: sweep 1/17
...
radarx_hid  Hydrometeor Classification (radarx)  |  KTLX20130520_201643_V06.gz  |  all sweeps  |  8.4 s
  derived variable(s): radarx_hid:hydrometeor_class
  processed 14 sweep(s), skipped 3
    skipped sweep 1: No differential reflectivity (ZDR) field found on sweep 1. Variables on sweep 1: DBZH, VRADH, WRADH.
  per-sweep summary:
 sweep  Classified gates       Most common class Method / band Melting layer Echo mask radarx
     0            132634 Light and moderate rain      park / S     not given        on  0.5.1
 ...
wrote hid_all.nc
```

**Storm cells** (`xrd cells`): one file lists the cells, several files (of one radar) are tracked in time order.

```console
$ xrd cells -i 'mayfield/KHPX*' --threshold-dbz 45 -o cells_all.csv --tracks tracks.csv -p tracks.png
21 track(s)  |  4 scans  |  50 cells  |  backend numpy
track_id  lifetime  speed_mps  direction_deg    status  max_dbz
    T001         3       5.72          132.3     ended     66.5
    T005         4      35.26          217.8 continued     58.0
    ...
wrote cells_all.csv
wrote tracks.csv
wrote tracks.png
```

### Clean: `pipeline`

A recipe is an ordered list of steps (SNR gate, clutter filter, biological-echo filter,
de-aliasing, speckle removal ...). `pipeline list` shows them, `pipeline show` the steps with every
setting, `pipeline script` prints the equivalent Python.

```console
$ xrd pipeline list
  recipe                                            target  kind      steps
  ------------------------------------------------  ------  --------  -----------------------------------------------------------------
  Doppler velocity: dealias + clutter + bio filter  VRADH   built-in  snr_gate > clutter_filter > biological_filter > dealias > speckle
  Reflectivity QC                                   DBZH    built-in  snr_gate > echo_mask > clutter_filter > speckle > gap_fill
  Dual-pol QC                                       ZDR     built-in  snr_gate > rhohv_gate > echo_mask > biological_filter > speckle

$ xrd pipeline "Doppler velocity" -i KTLX20130520_201643_V06.gz --target VRADH -p cli-pipeline.png -o vel_qc.nc
Doppler velocity: dealias + clutter + bio filter  |  KTLX20130520_201643_V06.gz  |  target VRADH  |  14 sweep(s)  |  8.0 s
  #  step                    status   removed  modified  filled  valid%  note
  -  ----------------------  -------  -------  --------  ------  ------  --------------------------
  1  SNR gate                skipped        0         0       0   100.0  no SNR field on this sweep
  2  Ground clutter filter   ok          5242         0       0    99.6
  3  Biological echo filter  ok        173512         0       0    86.8
  4  Dealias velocity        ok             0     98334       0    86.8
  5  Remove speckle          ok         28329         0       0    84.7
  derived variable: pipeline:doppler_velocity_dealias_clutter_bio_fil_vradh
wrote vel_qc.nc
wrote cli-pipeline.png
```

![Velocity before and after the pipeline](images/cli-pipeline.png)

A unique part of the recipe name is enough (`"Doppler velocity"`), and a path to a recipe JSON saved from
the app works too. Change a setting with `-P STEP.PARAMETER=VALUE` (`-P snr_gate.min_snr=5`) or switch a step
off with `-P gap_fill=off`. The result lives in the file that is written (`-o`, default
`<file>_<recipe>.nc`) because a derived variable only exists inside one run.

### Combine: `mosaic` and `winds` (need radarx)

Origin, map box and grid are chosen from the radar positions and printed; override them with
`--extent`, `--origin`, `--resolution-m`, `--dz-m` ... Without `-o`/`-p` the results are named after the product and time.

```console
$ xrd mosaic -i mayfield/KHPX20211211_015930_V06 mayfield/KNQA20211211_015611_V06 \
             mayfield/KPAH20211211_015935_V06 mayfield/KVWX20211211_015522_V06 -v
[  8%] Preparing radar volumes
[ 15%] Gridding radars onto 723 x 796 cells (band 1/7)
...
[ 92%] Rendering PNG
[14.7 s]
composite of DBZH  |  4 radar(s)  |  686 x 788 cells @ 1000.0 m
  extent  W -92.403  S 33.271  E -84.711  N 40.332  (degrees)  (chosen to cover all radars; --extent W,S,E,N to change)
  values  valid 22% of cells  min -20.0  max 63.9  mean 21.1 dBZ
  radar     lat      lon  volume time           coverage%  share%
  -----  ------  -------  --------------------  ---------  ------
  KHPX   36.737  -87.286  2021-12-11T02:01:56Z         31      15
  KNQA   35.345  -89.873  2021-12-11T01:58:41Z         31      28
  KPAH   37.068  -88.772  2021-12-11T02:02:27Z         31      22
  KVWX   38.260  -87.725  2021-12-11T01:57:27Z         31      34
using mosaic_composite_DBZH_20211211T0202.nc and mosaic_composite_DBZH_20211211T0202_map.png as the outputs (add -o / -p to choose)
wrote mosaic_composite_DBZH_20211211T0202.nc
wrote mosaic_composite_DBZH_20211211T0202_map.png
```

![Composite reflectivity of four radars](images/cli-mosaic.png)

`-o mosaic.nc` is the gridded NetCDF, `-o mosaic.png` the colour image **plus** its world file
(`.pgw`) and projection (`.prj`), so QGIS/GDAL place it; `-p map.png` is a map figure with the radar
sites. `--product cappi --height-m 2000` or `--product echo_top` change the product.

```console
$ xrd winds -i mayfield/KHPX20211211_015930_V06 mayfield/KNQA20211211_015611_V06 \
            mayfield/KPAH20211211_015935_V06 mayfield/KVWX20211211_015522_V06 \
            --extent 200 --max-range-km 200 --dx-m 2000 --zmin-m 500 --zmax-m 6000 --dz-m 500 \
            -P min_crossing_angle=20 -o winds.nc -p winds.png --level-km 2
analytic winds  |  4 radar(s)  |  grid 201 x 201 x 12 (dx 2000 m)
  origin  36.8568, -88.4233  (centre of the radars; --origin LAT,LON to change)
  levels  0.50 to 6.00 km above sea level (12)
  radar   KHPX  2021-12-11T02:02:24Z
  ...
  warning: The volumes were observed 5 min apart; the storm moves between scans. Give a storm motion to advect them to a common time.
wrote winds.nc
wrote winds.png
```

![Winds at 2 km](images/cli-winds.png)

`--method variational`, `--dealias auto|always|never` and any further engine option (`-P key=value`)
are passed to the same code the app uses; warnings of the engine are shown, not hidden. The
vectors are drawn where at least two radars give a usable crossing angle.

### Animate: `animate` (needs radarx; rendering needs the `video` extra)

Many volumes of one or more radars over time become an MP4: the strongest storm in 3D, a camera that flies
around it while it travels across the map, a UTC clock, the storm's track and labels that say which frames
are real volumes and which are interpolated. The files are grouped by radar and time, every time step takes
each radar's nearest volume within a tolerance, the rest is listed with the reason, the radars of one step are
gridded at a time (bounded memory) and cached on disk, an interrupted job resumes, and the MP4 is written
atomically at the end. [The full page](ANIMATE.md) has the camera file, the look, the report and the limits.

```console
$ xrd animate -i mayfield --no-render                    # the plan: steps, offsets, storm track, frames (no browser)
$ xrd animate -i mayfield -o mayfield.mp4                # the folder with one sub-folder per radar
$ xrd animate -i mayfield -o top.mp4 --camera top-down --duration 30
$ xrd animate -i mayfield -o custom.mp4 --camera camera.toml --realistic --look-preset "Golden hour" --set exposure=1.4
$ xrd animate -i mayfield -p still.png --still-at 12      # one frame, to try a camera or a look
```

```text
pip install -e 'python/[video]'      # Playwright (drives the system Chrome) and PyAV (writes the MP4); never a hard dependency
npm ci && npm run build               # once, in a checkout: dist/, the app that draws (or --app-dist / XRADAR_APP_DIST)
```

Without PyAV the frames are written as numbered PNG files and the `ffmpeg` line that encodes them is printed.
The terminal report (or `--json`) gives the track, displacement and speed, the time offsets of every radar,
the share of interpolated frames and the frame count, so a result can be checked without watching the video.

### Save: `export`

```bash
xrd export -i KTLX20130520_201643_V06.gz -o ktlx.nc                  # CfRadial1 NetCDF (the default)
xrd export -i KTLX20130520_201643_V06.gz -o ktlx.zarr                # Zarr
xrd export -i KTLX20130520_201643_V06.gz -o dbz_sweep0.csv -V DBZH   # azimuth, range, value
xrd export -i KTLX20130520_201643_V06.gz -f cfradial2 -o ktlx_cf2.nc
xrd export -i KTLX20130520_201643_V06.gz -o qc.nc --pipeline "Reflectivity QC" --run radarx_hid --include-derived
```

Derived variables only exist inside one run, so `export` can compute them first (`--pipeline RECIPE`,
`--run MODULE`, repeatable, all sweeps) and write them into the file with `--include-derived`.

## Input: files, globs, folders, stdin

```bash
xrd info -i KHPX20211211_015930_V06 KNQA20211211_015611_V06      # several files after one -i
xrd info -i 'mayfield/*/*_V06'                                  # a glob (quote it: xrd expands it)
xrd info -i mayfield/KHPX                                       # a folder = the radar files inside
ls mayfield/KHPX/*_V06 | xrd info -i -                          # a list on stdin
xrd info KTLX20130520_201643_V06.gz                             # bare file names work too
```

A pattern that matches nothing, an empty folder or a missing file is an input error (exit code 3)
that says what was looked for. Files from the Level-II archive on S3 are not fetched by the command line
(it never downloads anything on its own); download them first or use the app's cloud browser.

## Keep an analysis: `--config` and `--print-command`

A config file holds the same options (long names; `_` or `-`). Options on the command line win.

```toml
# run.toml
command = "run"
module = "storm_detection"
input = ["KTLX20130520_201643_V06.gz"]
output = "cells.csv"

[param]
threshold_dbz = 45
```

```console
$ xrd --config run.toml --print-command
storm_detection  Storm Cell Detection  |  KTLX20130520_201643_V06.gz  |  sweep 0  |  1.7 s
  summary
    Cells: 19
    ...
wrote cells.csv
command: xrd run storm_detection --input KTLX20130520_201643_V06.gz --output cells.csv --param threshold_dbz=45
```

`--print-command` also writes out what was chosen for you (for example `-s 1`), so the printed line
reproduces the run exactly. TOML needs Python 3.11+; JSON always works, YAML needs PyYAML.

## When something is wrong

Errors say what went wrong **and** the fix; there is never a Python traceback unless you ask (`-vv`).

```console
$ xrd plot -i KTLX20130520_201643_V06.gz -V velocity -s 0
error: No velocity field found on sweep 0. Variables on sweep 0: DBZH, ZDR, PHIDP, RHOHV. Sweeps that have it: 1, 3, 5, 6, 7, 8, 9, 10 (+6 more). Use sweep=1 in Python or -s 1 on the command line.

$ xrd run radarx_hid -i KTLX20130520_201643_V06.gz -s 1
error: Hydrometeor Classification (radarx) needs DBZH, ZDR, but sweep 1 has no ZDR (it has: DBZH, VRADH, WRADH). Sweeps that have all of them: 0, 2, 4, 6, 7, 8, 9, 10. Use sweep=0 in Python or -s 0 on the command line (without a sweep the right one is chosen for you).

$ xrd mosiac -i KTLX20130520_201643_V06.gz
error: unknown value 'mosiac'. Did you mean 'mosaic'?

$ xrd winds -i mayfield/KHPX20211211_015930_V06
error: winds needs at least two radar files: give them with -i (for example: xrd winds -i FILE1 FILE2 [FILE3 ...]) - only 1 given

$ xrd info -i missing.nc ; echo $?
error: File not found: missing.nc (looked for /.../missing.nc). Check the path and the spelling.
3
```

| Exit code | Meaning |
|---|---|
| 0 | success |
| 1 | the processing failed (the algorithm could not run on this data) |
| 2 | the command was used wrongly: unknown option, module, recipe, variable, bad `-P`, sweep out of range |
| 3 | an input file is missing, is not a file, or cannot be read |
| 4 | an optional Python package is missing; the message says how to install it (`pip install -U radarx`) |
| 70 | an unexpected internal error (re-run with `-vv` for the traceback and report it) |
| 130 | interrupted |

## Scripting

`--json` prints one JSON document on stdout (results, parameters, the files written) and nothing else;
messages go to stderr; `-q` silences everything but errors. `NO_COLOR=1` (or `--no-color`) turns colour off.

```bash
for f in data/*_V06; do
  xrd run storm_detection -i "$f" -P threshold_dbz=45 --json -o "cells_$(basename "$f").csv" | jq '.scalars.Cells'
done
xrd info -i data --json | jq -r '.[] | [.radar, .time] | @tsv'
```

Real runs for this page used the KTLX example volume (20 May 2013) and four radars of the Mayfield
case (11 Dec 2021: KHPX, KNQA, KPAH, KVWX); `xrd animate` was also run on the Mayfield and Hurricane Laura cases.
