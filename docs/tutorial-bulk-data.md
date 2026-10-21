# Tutorial: a whole event in the app (bulk data)

This tutorial takes a folder with dozens of radar volumes from several radars through the app, with no
terminal: drop the folder, read what the Autopilot found, approve the steps, look at the results, export in
bulk. Everything below was done on the data described in the first section, in the app, and every number
is from those runs (a run on your computer gives other times and, with other data, other counts).

## 1. The data: four radars, one night

The case is the tornado outbreak of the night of 10 to 11 December 2021 around Mayfield, Kentucky. Four
NEXRAD radars saw it: KHPX (Fort Campbell), KNQA (Memphis), KPAH (Paducah) and KVWX (Evansville). The
tutorial folder holds one hour of their Level II volumes, 02:30 to 03:30 UTC, as one sub-folder per radar:

```text
mayfield/
  KHPX/   11 volumes   KHPX20211211_023231_V06 ... KHPX20211211_032739_V06
  KNQA/   10 volumes   KNQA20211211_023037_V06 ... KNQA20211211_032413_V06
  KPAH/   10 volumes   KPAH20211211_023519_V06 ... KPAH20211211_032939_V06
  KVWX/   13 volumes   KVWX20211211_023118_V06 ... KVWX20211211_032725_V06
```

44 files, 457 MB on disk. The files are what the radars recorded (OBSERVED data); nothing in them was
changed.

**Where such data comes from.** NEXRAD Level II volumes are public: NOAA publishes them on the AWS Open Data
archive, and the app's *Cloud data* panel reads that archive directly (the latest scan of a station, or a
scan picked by date, or the scans of a whole region for one time), after you allow the download. The panel
fetches single scans. For a whole hour or day of several radars, download the files first, into one folder
per radar, and work from that folder; {doc}`opening-data` lists the ways to get files into the app. The app
never downloads a file without asking, and nothing in this tutorial downloads anything.

**Credit.** NEXRAD Level II data: NOAA National Weather Service, through the public AWS Open Data archive.
Radarx Desktop by LabShope Technologies.

## 2. Drop the folder: inspection, classification, plan

Drop the four radar folders (or all 44 files) on the window together. Three or more dropped items open the
**Autopilot** instead of opening every file in a tab. One folder dropped alone is treated as one file to
open and refused with a message (*it is a folder, not a radar file*): drop the radar folders or the files
together, or open the command palette (`Ctrl/⌘ + K`, *Autopilot*) and paste the path of the case folder.
Nothing is read in full, nothing is written: the Autopilot inspects, classifies, plans, and waits for your
approval.

```{image} images/tutorial-bulk/inspection-dark.png
:alt: The Autopilot panel after the drop: the inventory of the four radars and the start of the plan
:width: 100%
```

**The inspection** (38 s for these 44 files; 12 of them were opened for their metadata). One tile per radar:

- **KHPX**: 11 volumes, 91 MB, 02:32 to 03:27 UTC, one every 6 min (341 s); **KNQA**: 10 volumes, 96 MB,
  02:30 to 03:24, every 6 min (353 s); **KPAH**: 10 volumes, 137 MB, 02:35 to 03:29, every 6 min (359 s);
  **KVWX**: 13 volumes, 133 MB, 02:31 to 03:27, every 5 min (291 s).
- Every radar is **S band**, has **Doppler velocity** (the `V` chip) and is **dual-pol** (ZDR, RhoHV,
  PhiDP present), 15 to 21 sweeps per volume, 460 km range. There are **no gaps** in any of the four series
  (a gap chip appears when the interval between two volumes is much longer than the cadence).
- **Data check**: one representative volume per radar was checked. Each radar got **2 warnings** (range-folded
  velocity gates; velocity that looks aliased) and 2 or 3 notices (low RhoHV on many ZDR gates, ZDR values
  outside the usual range, biological or clear-air echo). The warnings are listed in full under *Be aware*
  in the plan, with the sweeps, azimuths and ranges they were found in. {doc}`DATA_CHECK` explains each one.
- **Pairs of radars**: distance, whether the coverage overlaps, and the **beam-crossing geometry** for
  dual Doppler: `KHPX-KPAH: 137.2 km, coverage overlaps, beam crossing ok on 88 % of the shared area`;
  KPAH-KVWX 161 km, 82 %; KHPX-KVWX 174 km, 77 %; KNQA-KPAH 216 km, 62 %; KHPX-KNQA 279 km, only 14 %;
  KNQA-KVWX 376 km, 0 % (they overlap, but their beams never cross at a wide enough angle).

The inventory is marked *metadata only*: no sweep data beyond the four checked volumes was loaded. The
inspection itself is governed: for a long series only the first, middle and last volume of each radar are
opened, the others are assumed alike and verified when they are processed; this is stated as a reduction
(`metadata sampling for KHPX: 11 -> 3`) and repeated in the run record.

**The classification** is the panel's title: **Several radars over time**. The other possible answers are
one radar and one volume, a time series of one radar, several radars at one time, RHI scans, or a mix; the
evidence is the list of counts and times above.

**The plan** is a ranked list of steps for exactly this data. Each step shows a rough estimate of time, memory
and the number of volumes it touches (marked ESTIMATE: coefficients scaled by the size of the files, not a
measurement), its provenance class, and when opened, *why* it is proposed, what it *needs*, what it
*produces*, *how* it is processed (how many volumes are in memory at once) and the equivalent `rxd` command.
For this data:

| Step | Estimate | Provenance |
|---|---|---|
| Data check report of every radar | ~6 s, 641 MB, 4 volumes | RETRIEVED |
| Clean-up (QC recipe suggested by the data check) | ~8 min, 1.3 GB, 44 volumes | RETRIEVED |
| Hydrometeor classification | ~14 min, 1.2 GB, 44 volumes | RETRIEVED |
| Storm cells and tracks | ~2 min, 699 MB, 44 volumes | RETRIEVED |
| Rainfall accumulation with uncertainty | ~3 min, 806 MB, 44 volumes | RETRIEVED |
| Storm animation (3D clip) | not run here: planned, command given | OBSERVED + labelled interpolation |
| Reflectivity mosaic (composite) of KHPX, KNQA, KPAH, KVWX | ~6 min, 3.4 GB, 44 volumes | RETRIEVED |
| Multi-Doppler winds from KHPX, KNQA, KPAH, KVWX | ~28 min, 6.4 GB, 44 volumes | RETRIEVED |

The QC step is tied to the data check: *velocity: biological filter, de-alias; reflectivity: biological
filter; ZDR: RhoHV gate, biological filter* for three radars, *velocity: de-alias; ZDR: RhoHV gate* for
KPAH. The hydrometeor classification is proposed because all four radars are dual-pol. The cells step
proposes tracks because each radar is a series ("11 volumes over 55 min: cells can be linked in time").

**Not possible with this data.** The section of that name lists analyses the data invites but cannot
support, each with what is missing: no hydrometeor classification without ZDR, RhoHV and PhiDP; no mosaic
without a second radar overlapping in space and time; no winds without two radars with velocity and enough
beam-crossing area; no accumulation without a series; no animation with fewer than three volumes of a
radar. For this case the section is **empty**: four dual-pol, Doppler radars with overlapping coverage and
a one-hour series support everything the Autopilot knows. The refusals that did appear were of another
kind, below.

### The budget: why a step is refused and what to do

The line above the steps says what the governor works with: `Budget 2.7 GB of 17.2 GB RAM`. The budget is
60 % of the computer's memory, but never more than 90 % of the memory that is free at that moment, and on
this computer, with other programs open, that was 2.7 GB. Two steps did not fit it and were refused, with
their reason inside the step:

```{image} images/tutorial-bulk/refused-dark.png
:alt: The mosaic and winds steps refused under a 2.7 GB budget, each with its reason
:width: 100%
```

- Mosaic: `one time step needs ~3.4 GB (4 volume(s) x 1.6) but the budget is 2.7 GB`.
- Winds: `one time step needs ~6.4 GB (4 volume(s) x 3.0) but the budget is 2.7 GB`.

A mosaic or a wind analysis holds one volume of every radar at the same time, so the memory of one step
grows with the number of radars (the largest volume here decodes to about 540 MB, so four of them are over
2 GB before any processing). A refused step cannot be ticked. Two ways out, both in the panel:

1. **Raise the budget**: the *Memory budget (GB)* field under *Run*; the plan is remade at once. With 4 GB
   (`Budget 4.3 GB of 17.2 GB RAM`) the mosaic fits and is ticked by itself; the winds step still needs
   6.4 GB and stays refused. Give only memory that is really free: the budget is a promise to the computer,
   not a wish.
2. **Reduce the radars**: inspect a smaller set, for example only the pair with the best geometry. Two
   volumes instead of four halve the step's memory. The multi-Doppler run in section 4 does exactly that.

```{image} images/tutorial-bulk/budget-raised-dark.png
:alt: After raising the budget to 4 GB the mosaic step is ticked; the winds step is still refused
:width: 100%
```

(An earlier run of the same plan on this computer, with more free memory, had a 3.8 GB budget: the mosaic
fitted and the winds step was refused with the same 6.4 GB message. The numbers in the refusal depend on the
data; the budget depends on the computer and the moment.)

Under *Show the decision rules and their evidence* the panel lists every rule, whether it fired, and the
sentences of evidence it fired on. There is no hidden score. The panel follows the app's theme; the same
inventory in the light theme:

```{image} images/tutorial-bulk/inspection-light.png
:alt: The same Autopilot inventory and plan in the light theme
:width: 100%
```

## 3. Approve steps and run

Tick what you want and press **Run**. For the walk-through the first run took three steps: the data check,
the storm cells and tracks, and the mosaic, with the budget at 4 GB and the results folder set to
`mayfield/results` (the default is a folder named `autopilot_<date and time>` next to the data). The
button says what it will do: `Run 3 steps (~8 min)`.

```{image} images/tutorial-bulk/running-dark.png
:alt: The run in progress: the bar, the step and the radar it is on, and Cancel
:width: 100%
```

The progress line names the step and the volume (`2/3 Cells KPAH`, `3/3 mosaic 20211211T030142 (KHPX, KNQA,
KPAH, KVWX)`); the status bar shows the same. Measured times of this run (the estimates were ~6 s, ~2 min and
~6 min):

| Step | Measured | Wrote |
|---|---|---|
| Data check report of every radar | 12 s | `check_<radar>_<time>.json`, one per radar |
| Storm cells and tracks | 94 s | `tracks_<radar>.csv` and `tracks_<radar>.png`, one per radar |
| Reflectivity mosaic (composite) | 145 s | `mosaic_<time>.nc` and `mosaic_<time>.png` per time step, 13 steps |

**Where the results are.** When the run ends the panel shows a *Results* list (one line per step with its
status and measured time) and a **gallery** of every file written, with a thumbnail for the pictures and
*open in viewer* under the NetCDF files. Everything is in the results folder, next to two more files:

- `SUMMARY.md`, the one-page summary: the situation and its evidence, each step with its status, time,
  reason, provenance and files, the reductions decided by the governor, and the decision table.
- `run_record.json`, the full record: the inputs, the software and library versions, the inventory, the
  plan, every decision with its reason, every reduction, the exact commands, parameters and timings.

```{image} images/tutorial-bulk/results-dark.png
:alt: The results list and the gallery after the run
:width: 100%
```

The summary of the resumed run (below) reads, for the mosaic step:

```text
- **Reflectivity mosaic (composite) of KHPX, KNQA, KPAH, KVWX** - done (153 s)
  - why: Overlapping radars at the same time: one map of the column maximum,
    with calibration offsets between radars estimated in the overlap.
  - provenance: RETRIEVED (beam-weighted merge on a 1 km grid; offsets
    between radars are estimated, not measured)
  - wrote: mosaic_20211211T023118.nc, mosaic_20211211T023118.png, ...
```

The time in a mosaic's name is the reference radar's volume time (KVWX here, the radar with most volumes).

**The other two steps.** The QC recipe and the hydrometeor classification go through all 44 volumes, one at a
time, and were estimated at ~8 and ~14 minutes. The QC step was started in a second run and cancelled to
show what cancelling does. Cancel is honoured between volumes, so the volume being cleaned finishes first
(here two cleaned volumes, `KHPX_20211211T023231_qc.nc` and `KHPX_20211211T023814_qc.nc`, were on disk
14 s later). The panel then says *Cancelled* and keeps the plan; the record and summary on disk list the step
as `cancelled (14 s)`, and the steps before it keep their status.

```{image} images/tutorial-bulk/cancelled-dark.png
:alt: After Cancel: the panel says Cancelled and keeps the plan
:width: 100%
```

**Resume.** Press *Run* again with the same results folder: a step that finished earlier is reported as
*skipped (done earlier)* with its files, in under a second, and only the unfinished steps run. The results
list in the picture above is such a run: the data check and the cells were skipped, the mosaic step ran
again (153 s). A cancelled step starts again from its first volume. The results folder keeps a small state
file for this; delete it to force everything to run again.

**Open a result in the viewer.** Under each `.nc` in the gallery, *open in viewer* opens the file as a new
tab: a cleaned `_qc.nc` or a classified `_hid.nc` volume behaves like the original file, with the derived
fields in the variable list. A mosaic grid opens too (variable `DBZH_composite`, one level), but it is a map
grid rather than a radar volume and the PPI view has little to show for it: for the mosaics use the PNG next
to each grid, the Mosaic panel for an interactive one (section 4), or GIS software for the grid itself.

**The Layers panel** (right sidebar, *Layers*) lists everything the app holds on screen, each with its
provenance chip, its source and its valid time with the offset to the active scan: `OBS` for the radar
moments of each open volume, `RETR` for a mosaic, a wind field or a classification, and a labelled chip for a
stylised look. Below, two volumes of 03:05 UTC and the mosaic made from them in the Mosaic panel; *What the
chips mean* explains the classes ({doc}`science-and-trust`).

```{image} images/tutorial-bulk/layers-dark.png
:alt: The Layers panel: OBS chips for the two open volumes, a RETR chip for the mosaic made from them
:width: 100%
```

## 4. Look at the whole event

**The mosaic over time.** The mosaic step wrote one composite (column-maximum reflectivity of the four
radars on a 1 km grid) per time step of the reference radar, the one with most volumes: 13 steps, 02:31 to
03:27 UTC, each using the nearest volume of every other radar (never more than 10 minutes away). The
pictures are a first look at the whole line of storms crossing the four radars:

```{image} images/tutorial-bulk/mosaic-frame.png
:alt: Composite reflectivity of KHPX, KNQA, KPAH and KVWX at one time step of the run
:width: 70%
:align: center
```

A column maximum keeps every echo: the clear-air and biological returns the data check flagged around each
radar are in the picture together with the rain, and the cyan and blue speckle near the radars (below
0 dBZ) is noise rather than weather. That is why the QC step comes before the mosaic in the plan: a mosaic
of cleaned volumes is made the same way from the `_qc.nc` files, in the Mosaic panel or with the command
the step shows.

For one time, the Mosaic panel does the same interactively on the files you open. Here the best pair,
KPAH and KHPX at 03:05 UTC, merged on the map (the weighted blend in the overlap; hover gives each radar's
contribution):

```{image} images/tutorial-bulk/mosaic-view-dark.png
:alt: The Mosaic panel: composite reflectivity of KPAH and KHPX at 03:05 UTC on the map
:width: 100%
```

**The tracked storms.** `tracks_<radar>.csv` holds every cell of every scan (reflectivity above 35 dBZ,
the step's fixed threshold) with its area, peak and mean reflectivity, position, an echo-top proxy, speed,
direction, lifetime and track id; `tracks_<radar>.png` draws the tracks. The counts for this hour:

| Radar | Scans | Cells | Tracks | Tracks seen in every scan | Single-scan cells | Peak dBZ |
|---|---|---|---|---|---|---|
| KHPX | 11 | 429 | 119 | 1 | 29 | 68.5 |
| KNQA | 10 | 235 | 82 | 3 | 27 | 72.0 |
| KPAH | 10 | 460 | 141 | 10 | 38 | 69.0 |
| KVWX | 13 | 915 | 258 | 5 | 81 | 73.0 |

Most of these cells are pieces of the wide band of rain, so the tracks are many and short; the storms that
matter are the ones seen in every scan. From KPAH, track `T001` is seen in all 10 scans, 02:35 to 03:29 UTC,
peak 69 dBZ, mean area 1271 km², moving from 240° (from the south-west) at 26 m/s, from 36.5° N, 89.4° W to
36.9° N, 88.5° W. KNQA's track `T001` covers the same place and time from further away (10 scans, 36.5 to
36.8° N, 89.4 to 88.7° W, peak 72 dBZ, from 244° at 22.5 m/s), which suggests it is the same storm. KVWX's longest tracks run through all 13 scans; its track `T009` peaks at
73 dBZ at 03:27 UTC at 37.8° N, 86.4° W. The median speed of the cells that lasted three scans or more is
18 to 26 m/s depending on the radar, from 232 to 249°. A tracked speed is one number per track from the
displacement of the cell between scans (the tracking method is in {doc}`CELLS`); it is a RETRIEVED value,
not a measurement, and cells that split or merge get new ids.

```{image} images/tutorial-bulk/tracks-kpah.png
:alt: The tracks of KPAH's cells over 10 scans, drawn by the run
:width: 60%
:align: center
```

**A cross-section or 3D for one time.** Open the volume of one radar at the time you want (double-click a
file, or *open in viewer* from a cleaned `_qc.nc`), draw a line across the storm in the Cross-Section panel,
or switch to **3D**; {doc}`CROSSSECTION` and {doc}`REALISTIC_3D` show what each view does and does not show.

**Multi-Doppler winds for the best pair.** The Autopilot's pair list says which two radars to use: KHPX
and KPAH, 137 km apart, with beams crossing at 30° or more on 88 % of the area they both see (27,416 km²);
KPAH-KVWX (82 %) and KHPX-KVWX (77 %) are the next candidates, and KNQA-KVWX (0 %) is useless for winds
however well it overlaps. A wide crossing angle is what makes two radial velocities solvable for a
horizontal wind. For the pair, inspect only the two folders (two volumes per step, about 3.2 GB instead of
6.4 under the same rule) or open the two volumes of one time in the Wind panel; the de-aliasing the data
check asked for is applied automatically there. {doc}`MULTIDOPPLER` describes the method, the real-case
check on this same night and the limits (the vertical wind is the weakest part; below about 3 km the two
radars see little in common at these distances).

## 5. Export in bulk

**The Export panel** (right sidebar, *Export*) works on the file in the active tab. *Batch Export* writes
one figure for every ticked **variable** and **sweep** of that volume, as PNG, SVG or PDF at the chosen
DPI, into the folder you choose (the desktop app asks for it; the default is `radarx-exports` in your
Downloads). The *Title* of the quick export takes placeholders, `{variable}`, `{sweep}`, `{elevation}`,
`{time}` and `{radar}`, that are filled in for every figure of a batch. *Data export* writes the volume as
NetCDF (CfRadial1), CfRadial2, Zarr or CSV, with the derived fields (the cleaned velocity, the
classification) next to the measured ones when you tick them. Every file carries the software version, the
creation time, the source file name and the settings ({doc}`OUTPUT_STANDARDS`).

```{image} images/tutorial-bulk/batch-export-dark.png
:alt: The Export panel: batch export of the variables and sweeps of the open volume
:width: 100%
```

**Every volume of the event.** The Autopilot steps are the bulk export of the app: the QC step writes one
cleaned `<radar>_<time>_qc.nc` per volume, the classification one `_hid.nc` and `.png` per volume, the
mosaic one grid and picture per time step, each with its provenance and the recipe in its history. For
anything else, the **Command Studio** (command palette, *Command Studio*) is a form for every `rxd`
command: pick the files (the open ones are one click away; a folder or a pattern such as
`mayfield/KPAH/*_V06` works where the command takes several files), set the options, and read the
equivalent terminal command, which *Copy as command* copies. `rxd export` writes one file per call, so
exporting all 44 volumes is a loop around that command; {doc}`BULK` has the loop ready to paste. A large
NetCDF export is checked against the memory budget and the free disk space before it starts, in the app
and on the command line alike: one that does not fit stops at once with the message and the alternatives
(a disk with more room, or Zarr and CfRadial2, which are compressed) instead of failing half-way, and a
reduction that was needed is written into the file it produced.

```{image} images/tutorial-bulk/command-studio-dark.png
:alt: The Command Studio with the cells command: a form, the live equivalent command, Copy as command
:width: 100%
```

**The Movie Studio** (*Movie* in the toolbar) films the event in 3D: what to film (the open files, more
files or the case folder with one sub-folder per radar, a time window), then **Plan the film** before
anything is drawn (the time steps, the storm's track, how many frames are observed and how many
interpolated, the camera), the focus (zoom on the strongest storm or the whole scene) with **Preview this
frame** for one still at a chosen second, the look, the sound, and the render with progress and Cancel. The
record next to the film holds the equivalent command. In the installed app the Movie Studio needs the *movie
components*, an optional download of about 42 MB that it offers the first time, with the source, the size
and the licences stated, and fetches only when you allow it; it drives the Google Chrome on your computer,
which draws and encodes the frames. The dialog below is the browser version of the app, which cannot render
and says so; the plan and the still are the places to stop and check before spending the minutes a full
render takes ({doc}`ANIMATE`).

```{image} images/tutorial-bulk/movie-studio-dark.png
:alt: The Movie Studio dialog: what to film, the plan, focus and camera; in the browser version it says that rendering needs the desktop app
:width: 100%
```

The Command Studio, too, runs commands only in the desktop app ("Commands run on your own computer: this
needs the desktop app"); in the browser version the form and *Copy as command* still work, so the command
can be pasted into a terminal. The command-line page for whole folders is {doc}`BULK`.

## 6. When the data are bigger

The same panel takes a day of a radar network, hundreds of volumes, with the same rules, and the rules are
visible:

- **Inspection** opens a sample of each long series (first, middle, last) and checks one volume per radar;
  files whose names carry no radar and time are opened to find out, and with very many of them only a sample
  is, and the plan says so.
- **One volume at a time** (one per radar for mosaics and winds): the rest stays on disk and is read
  lazily. A step whose single time step does not fit the budget is refused, as in section 2, never run
  half-way into an out-of-memory stop.
- **Long series are thinned for the heavy per-step products** (mosaic, winds, hydrometeor classification,
  the animation): at most 24 time steps, taken evenly, with the reason stated in the step, for example
  for a day of 5-minute volumes: `Reduced: time steps for mosaic 288 → 24. 288 time steps would take ~N
  min; every 12. volume is used (evenly), the others are listed as skipped in the run record` (the minutes
  are the step's own estimate). The data check, the QC recipe, the cells
  and tracks and the rainfall accumulation keep every volume: they are light, and tracks and accumulations
  need the full cadence.
- **Nothing is silent.** Every reduction has three parts, *what* (`time steps for mosaic`, `metadata sampling
  for KHPX`, `data check`), *from → to* (`44 -> 4`) and *reason*, and appears three times: in the step on
  screen, in the *Reductions decided by the governor* section of `SUMMARY.md`, and in `run_record.json`.
  The products carry it in their provenance. A reduction record that says `every 12. volume` means that 11
  of every 12 volumes were not used for that product; if that is not acceptable, raise the budget, cut the
  period, or run the step from the command line with every volume.

**When to switch to the command line.** The app is right for a case: one event, one results folder, a
plan you read and approve. A nightly feed, hundreds of cases, or a product that must use every one of a
thousand volumes is a job for {doc}`BULK`: the same engine, one command per file in a loop, with exit codes
a script can act on, parallel runs sized by memory, and the same budget (`--budget-gb`) and record. The
Autopilot's steps already show the command for each of them; *Copy as command* in the Command Studio
gives the rest.

## 7. What can go wrong and what to do

| Seen in this data or at this step | What it means | What to do |
|---|---|---|
| *Range-folded gates*: 11 to 38 % of the velocity gates of one sweep (KVWX 38 % on sweep 1) | second-trip echo hides the true velocity in those gates (a NEXRAD velocity sweep at a short unambiguous range) | these gates carry no usable velocity and no de-aliasing recovers them; the finding names the sweep, azimuths and ranges, so prefer the levels and the pair where the fraction is small |
| *Velocity looks aliased*: 4 to 6 % of gate-to-gate steps exceed 1.2 x the Nyquist velocity (24.5 to 33.5 m/s) | the true wind exceeds the radar's unambiguous velocity and folds over | de-alias first (the QC recipe does; the Wind panel does it automatically); never compute shear or winds from folded velocity |
| *Biological / clear-air echo*, *many gates with low RhoHV*: 16 to 18 % of the echo gates on the lowest sweep are weak with the signatures of birds, insects or clear air; 11 to 16 % of the ZDR gates have RhoHV below 0.85 | non-meteorological scatter around the radar; ZDR is meaningless there | the biological filter and the RhoHV gate of the QC recipe; the composite in section 4 shows what happens without them |
| A `gap` chip on a radar tile; `gap of N min after <time>` under *Be aware* | a volume is missing from the series | tracks across the gap are less certain and an accumulation under-counts; the record names the gap; fetch the missing files if the archive has them |
| A radar whose times do not overlap the others | `coverage overlaps but the times are N min apart` in the mosaic evidence: no common time step | the mosaic and winds steps leave that radar out (the list of radars in the step's title tells); drop its folder or download the matching hour |
| *Refused: one time step needs ~X GB but the budget is Y GB* | one step of a mosaic or wind analysis holds one volume per radar and does not fit | raise the budget to memory that is really free, or inspect fewer radars (section 2) |
| An estimate far from the measured time (mosaic ~6 min estimated, 145 s measured) | estimates are coefficients scaled by file size, not measurements of your computer | read the measured times in the results list and the record; plan the next run from those |
| *Cancelled* in the results list | you pressed Cancel; the step stopped after the volume it was on | run again: finished steps are skipped, the cancelled one restarts |
| A `.nc` from the mosaic step opened in the viewer shows little | the file is a map grid, not a radar volume | use the PNG next to it, the Mosaic panel or GIS software; open the `_qc.nc` and `_hid.nc` volumes in the viewer instead |

Everything above is also in the run record of your own runs; when in doubt, read `SUMMARY.md` first.
