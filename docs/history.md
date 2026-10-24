# What's new

```{include} changes/unreleased.md
```

## 0.6.0

- **New Lightning layer (observed).** Open GOES GLM Level 2 files or Lightning Mapping Array source files from your
  disk and see the flashes over the radar, in 2D and in 3D. Dots are drawn at their measured time (and, for the
  array, height), joined in time order by flash, brighter for stronger and fading with age; the time window follows
  the volume time. A panel gives the flash rate per minute and the number of sources, hatches the time the files do
  not cover (no data is never drawn as no lightning), and shows the GLM parallax as an uncertainty (or corrects it to
  a cloud-top height you choose, an assumption it states). LMA sources are linked into flashes with the space-time
  metric of Fuchs et al. (2016), thresholds in the panel. The same is available as `rxd lightning`. See {doc}`LIGHTNING`.
- **Lightning: LMA source heights and lightning jumps.** For Lightning Mapping Array files the Lightning panel shows
  how many sources and flash initiations lie in each height bin (km above mean sea level, with the median and the
  most common height) and marks lightning jumps (the "2 sigma" algorithm of Schultz et al. 2009) on the flash-rate
  chart; both are computed by the radarx library. It says that a distribution of located sources depends on the
  range from the array, that the jump needs 14 minutes of data, and that charge layers are not inferred.
- **Lightning sound in the viewer.** The Lightning panel can replay the flashes of the time window as sound
  ("Synthesised from the data, not a recording"): thunder that arrives after distance / 343 m/s (divided by the
  replay speed), quieter, duller and longer the farther the flash, heard from the radar or from the 3D camera with
  binaural-style direction, plus an optional tick at the time of each flash. A small mixer has a master level, a
  level and mute per layer and a limiter ceiling; the panel lists which instrument drives each layer and says that
  the timbre is made up. Nothing plays until you press Listen. There is no wind, rain, hail or tornado sound.
- **HRRR model data and `rxd env`.** The 3 km HRRR model (CAPE, helicity, shear, reflectivity, temperature, wind and
  hydrometeor fields, and a model column read as a sounding) can be fetched for a time you choose. Only the fields
  you pick are downloaded, the exact size is shown and approved first, the nearest hourly time is used and named
  (never interpolated), and everything is labelled as a model. `rxd env` does the same from the terminal
  ({doc}`environment-sources`). Reading the files needs the optional ecCodes package
  (`pip install "radarx-desktop-engine[grib]"`).
- **ERA5 reanalysis data.** Hourly global fields and profiles from the public ERA5 copy on Google Cloud, for any date
  since 1940 and any place. Only the levels and latitudes you ask for are downloaded (a seven-level profile is about
  17 MB instead of 526 MB), the exact size is shown and approved first, the nearest hour is used and named (never
  interpolated), and everything is labelled as a model. Available as `rxd env ... era5` ({doc}`source-era5`).
  ERA5-Land is not offered.
- **MRMS radar products.** The NOAA 1 km, 2-minute mosaics (3-D and composite reflectivity, rain rate, radar-only and
  gauge-adjusted accumulations, precipitation type, hail size, rotation tracks, echo tops) for a time you choose. The
  exact size is shown and approved first, the nearest file is used and named (never interpolated), and each layer
  says whether it is radar only, model-assisted, gauge-adjusted or a model field. Available as `rxd env ... mrms`
  ({doc}`source-mrms`); reading the files needs the optional ecCodes package.
- **Environment diagnostics.** `diagnose()` now computes lapse rates (0-3 km and 700-500 hPa) alongside
  CAPE/CIN/LCL/LFC/EL, shear, SRH and precipitable water; bulk shear, Bunkers storm motion and SRH use
  `radarx.retrieve.wind_profile` when radarx is installed and fall back to local implementations otherwise. See
  {doc}`source-diagnostics`.
- **Rotation tracks.** A new module, *Rotation Tracks (radarx)*, follows low-level rotation over a sequence of
  volumes of one radar: a swath of the largest azimuthal shear, with the rotation couplets linked into tracks, the
  gaps between volumes and the limits of range-dependent resolution shown with the result. It is a retrieval, never
  a tornado warning or a damage estimate, and its detection skill has not been measured. The existing *Azimuthal
  Shear* and *Rotation Couplets* modules (with *Tilts needed* for vertical continuity) are described on the same
  page ({doc}`ROTATION`).
- **Event report.** `rxd report` writes one self-contained HTML document of a rainfall event: summary numbers with
  units and class, accumulation map, basin table, map and series, gauge validation, methods with references, the run
  record and the credits; every figure and number is labelled as an estimate with its uncertainty where known.
  `--record` saves the run record and `--from-record` makes the same report again from it. See {doc}`HYDROLOGY`.
- **Probabilistic rainfall.** `--ensemble N` (and the *Probabilistic rainfall* block of the Rainfall panel) builds an
  ensemble of the rainfall estimate under an assumed multiplicative, spatially correlated error model and gives the
  mean, the spread, percentiles and the probability of reaching chosen depths, per cell and per basin. The error
  parameters come from your gauges when you give some; otherwise they come from the estimate's own uncertainty and
  from defaults that are marked as assumed. Seeded and reproducible, with the run record in the output, and always
  labelled as an ensemble under an assumed model; with 30 or more gauges the probabilities are checked against them
  (Brier skill and reliability slope), otherwise the output says they are not validated. See {doc}`HYDROLOGY`.
- **Flash-flood indicators.** `rxd indicators` compares the rainfall of a period with flash-flood guidance,
  precipitation-frequency grids (return period) and the cell's own climatology, from files you supply (NetCDF,
  GeoTIFF or CSV; nothing is downloaded). The layers are labelled as indicators of the class of the rain they come
  from, carry their inputs, vintage and limits, and say that they are experimental guidance, not an official
  warning. The relative-to-climatology rule is the reference rule: a 36-predictor machine-learning model did not
  beat it in our test, and the rule itself is weak. See {doc}`HYDROLOGY`.
- **Dealiasing is now checked, always.** The pipeline step, the QC panel's velocity dealiasing and the *Velocity
  Dealiasing* module test every result for a sweep that sits in the wrong multiple of two Nyquist velocities (a
  range-ring test with a uniform-wind VAD fit, Browning and Wexler 1968), write the verdict into the run record, and
  warn when the whole sweep is off or when parts of it sit in different intervals. The data are not changed and the
  default *Fold reference* stays `none`: on 133 Doppler cuts of 12 NEXRAD volumes no whole-sweep offset was found in
  the QC panel's method, so the evidence did not justify changing it. Velocity dealiasing in the QC panel can also use
  radarx when it is installed (new *Unfolding engine* option: auto, builtin, radarx), exact where the built-in method
  can be one interval off; and a gate beyond 150 m/s (or five Nyquist velocities) after dealiasing is set to missing,
  counted in the run record and warned about, so a rare unfolding fault never reaches your results. See {doc}`PIPELINES`.
- **A stuck or runaway analysis is easier to get out of.** Cancelling a module now really stops it (and an export,
  batch or animation stops at its next step and leaves no half-written file); a module that grows past 75 % of the
  computer's memory is stopped with a message (`XRADAR_MODULE_MAX_MB`); cloud listings and downloads give up after
  five minutes with a clear message (`XRADAR_CLOUD_TIMEOUT_S`); and an engine whose event loop is stuck for a minute
  restarts itself, while the app warns after 30 s of silence.
- **The engine starts about 30 % faster** (ready after about 0.5 s instead of 0.7 s), because file-format and image
  libraries are now loaded in the background after start-up instead of before it. New opt-in setting
  `XRADAR_FLOAT32=1` keeps decoded Level II style moments as 32-bit numbers: half the memory for them, values within
  6e-8 of their size (written files say so in `numeric_precision`). Slowdowns of several times are now caught by an
  optional benchmark check run by the maintainers.
- **Sign-in tokens leave the plain-text file.** The app and `rxd` keep your access and refresh tokens in the system
  credential store (Credential Manager on Windows, Keychain on macOS, Secret Service on Linux) instead of a readable
  `session.json`. An existing sign-in moves there on its own and you stay signed in. Without a credential store (no
  desktop session, a cluster login node) the tokens stay in a file that only your user can read, and on Windows its
  access list now names only you. {doc}`ACCOUNTS` explains what this does and does not protect, and
  `RADARX_CREDENTIAL_STORE` chooses.
- **The app's engine refuses web pages.** A page open in your browser can no longer open a connection to the engine
  that runs on your computer. Any page other than the app itself, and any request that names another computer, gets
  "403 Forbidden" before a connection is made; before, an engine started by hand (without the app's per-run key)
  accepted every page, and one started by the app closed the connection only after it was open. {doc}`ACCOUNTS` says
  what this does and does not protect.
- **Accessibility.** An automated audit of the main screens in the light and dark themes (axe-core in headless
  Chrome) found headings, landmarks and one scroll area that keyboard and screen-reader users could not use
  properly; they are fixed, and three dialogs (colormap editor, data table, metadata viewer) now keep keyboard focus
  inside while open and give it back when closed. A first step towards translations is in place (a message catalogue
  helper and one translated-ready dialog); no language was added yet.
- **Smaller fixes:** the engine says plainly when its port is already in use (one line, exit code 75) and keeps its
  crash report instead of deleting it when the log rotates; `rxd login` explains itself and exits cleanly (code 5)
  when there is no terminal to type a password into; a mistyped request to the engine is answered "Unknown message
  type" instead of "not signed in"; and the engine reports its own version with `--version`.

## 0.5.0

- **Python 3.12 or newer is now required for the command-line package** (`rxd`): it uses the radar-reading library
  xradar 0.13, which needs Python 3.12. The installers are not affected (they carry their own Python). The new
  library reads some files more completely and more correctly, so exported files can differ from earlier ones in the
  data, not in layout or units: a NEXRAD sweep that lost radials is whole (720 instead of 718 rays in our example),
  ODIM total power is `DBTH` in dBZ instead of an unlabelled `TH`, IRIS gates without data are empty instead of
  values such as -32 dBZ, HPL lidar sweeps keep their last ray, and a CfRadial1 export keeps rays that share a time
  stamp ({doc}`OUTPUT_STANDARDS`).
- **CfRadial1 export is faster and lighter:** a 19-sweep NEXRAD volume exports with about 3 GB of memory instead of
  4.5 GB, and a CfRadial1 file made by the app always goes through the app's clean-up (units, fill values, time axis,
  standard names).
- **Cite and repeat.** The Export panel can copy a methods text for what you did in a session (one paragraph per step, every parameter, software and library versions, radarx named with its version where radarx computed the step), save the run record as JSON, and save and open project files (the open files, what each shows, the pipeline; no data is copied, and a moved or changed file is reported, never replaced). `rxd methods` and `rxd project` do the same from the command line. The pipeline editor has Undo and Redo.
- **Basins on the map, with exports you can keep.** The Rainfall panel draws the basins you load as outlines on the map, filled with the basin-mean rainfall on the colour scale of the layer; hover for area, mean, maximum, uncertainty and coverage, click a basin to open its series. New exports: basin outlines with their statistics as GeoJSON for a GIS (`-o basins.geojson`), the series figure as PNG, a map with each basin filled by its mean (`rxd basin --map map.png`), and CSV files whose first lines give the units, method, uncertainty and sources. Return periods can use an areal reduction factor that you supply in the frequency table. Also corrected: without a factor, comparing a basin mean with point depths understates the return period (the earlier text called it an upper bound), and the output now says so. See {doc}`HYDROLOGY`.
- **Closing a file frees its disk copy at once:** a compressed (.gz) radar file is unpacked to a temporary copy while it is open; closing the file now deletes that copy immediately instead of whenever the system next tidies memory, so repeated opening and closing no longer piles up copies on macOS and Windows.
- **`rxd pipeline` works after `pip install`:** the command-line package failed with a file-not-found error on its first `rxd pipeline` run because it looked for its built-in helper files in the wrong folder. Found by a Windows test of the installed build.
- **Everything in 0.4.1** (the radar globe, the live map's open, view and save actions, German and airport radars on
  the live map, `rxd serve`): 0.4.1 was built but not made available, so 0.5.0 is the next version to download.

## 0.4.1

- **The radar network map is a globe:** it opens as a globe you can turn with the mouse, a finger or the keyboard
  (drag, wheel or pinch to zoom, double-click to centre, a compass button and **N** to point north up again, **Home**
  to start over), with a *Globe | Flat* switch that remembers your choice and falls back to the flat map where the
  graphics driver has no WebGL2. Same radars, filters, search, legend and card in both views; zoomed in far enough it
  offers *Open in flat map here*. With the basemap *None* it sends nothing at all ({doc}`RADAR_MAP`).
- **The live map's data are yours to use:** *Open these volumes* (and *Open this volume* on each radar) opens the
  volumes behind the picture as normal files, so the 2D and 3D viewers, Mosaic, Multi-Doppler, cells, modules and
  export work on them, with no new download; *View in 3D* and *Multi-Doppler winds from these radars* do the common
  cases in one click, and *Save the composite* writes NetCDF or a georeferenced PNG with the credit and the analysis
  time. At most 8 volumes are kept, and closing a file gives the disk back ({doc}`LIVE`).
- **The live map looks better and says what it draws:** echo below 5 dBZ is no longer painted (a visible setting,
  *Hide echo below N dBZ*; data and exports are not changed), a 1 km grid for up to four radars, a smoothed picture,
  place names and boundaries above the echo, thin optional range rings, a view that opens on the radars' footprint and
  a left pane that lists the radars and the volumes held.
- **European and airport radars on the live map:** the live map works with NEXRAD, the 17 German DWD radars (German
  Weather Service Open Data, CC BY 4.0, mixed with NEXRAD if you like) and the 47 US airport radars (TDWR). The
  dialogs name the right server and the exact sizes, and the credit and licence travel into the label and the saved
  files. A live map of far-away radars is now centred on the radars it shows, so Germany, Alaska and Hawaii are no
  longer turned sideways. Other networks (EUMETNET OPERA, FMI, KNMI, DMI, SMHI, Météo-France, South Africa) are not
  live yet; {doc}`LIVE` says why.
- **More radars on the radar network map:** the 47 US Terminal Doppler Weather Radars and 82 Australian radars of the
  Bureau of Meteorology (with band, Doppler and dual-polarisation flags, dates and status); the Australian archive's
  licence is now shown correctly as CC BY 4.0. The app still cannot download Australian volumes itself.
- **The app's screen on data that stays on a cluster:** the new command `rxd serve` starts the engine on a cluster,
  listening on that computer only, with a new random token for every run; you forward its port with `ssh -L` and
  connect the app in *Settings > Engine*. The status bar says **REMOTE ENGINE** while it is in use. The token is
  never saved or logged, only one app can connect at a time, and the engine stops after 120 minutes without use
  ({doc}`cluster`).

## 0.4.0

- **Nowcast (experimental):** a new **Nowcast** panel and `rxd nowcast` extrapolate radar frames 5 to 120 minutes
  ahead (dense optical-flow motion, no growth, decay or initiation), with an ensemble from the measured motion error,
  probabilities of exceeding thresholds and a position uncertainty for every lead. Every frame, picture and file
  carries the **FCST** chip (a new provenance class, **FORECAST**), the origin and valid time, the lead, a measured
  skill statement and the words "experimental guidance, not an official forecast". The method was checked on eight
  public NEXRAD cases that were not used to tune it, against "nothing moves" persistence, with intervals: a small
  gain out to about 20 minutes, about equal at 30 to 45 minutes, none beyond an hour; rotating and stratiform rain
  and an almost empty frame are not forecast better than persistence ({doc}`NOWCAST`, {doc}`SCIENCE`).
- **Live map and default radars:** choose up to eight NEXRAD radars (Settings › Data › Live map, or the new **Live**
  button) and see their newest volumes merged into one composite that updates every 2 to 15 minutes, with a status line
  that says each radar's scan time and age or why it did not answer, and a loop of the last 12 composites. Downloads
  are asked for twice (the listing, then the exact sizes) ({doc}`LIVE`).
- **Radar network map:** the Cloud data panel's new *Map* tab shows every weather radar in a bundled catalogue of 1005
  sites, each with the source and licence of its position and what the app can do with its data (loadable, open data,
  key required, restricted, unknown). *Set as default radar* on a site's card adds it to the live map radars (NEXRAD
  only). **DWD Open Data** (17 German radars, the last two days) and the **FMI open-data sample** (ten Finnish radars)
  now load directly, with the licence and credit travelling into every export ({doc}`RADAR_MAP`).
- **Everything from the terminal, and servers and clusters:** two new pages. {doc}`cli-guide` goes through the whole
  command line by task, with a table of which app feature has which command and which has none yet. {doc}`cluster`
  covers installing without a window, putting `rxd` on the path, signing in on a machine with no browser, proxies and
  firewalls, batch jobs, what needs a display or Chrome, and moving results back. It says plainly that the app cannot
  yet connect to an engine running on a cluster. {doc}`installation` has a short "Put `rxd` on your PATH" section.
- **Command line reference:** `rxd check --fix-plan -o recipe.json` is described as it behaves (one recipe file per
  variable), and the exit-code tables say that an empty file gives 1, not 3 ({doc}`CLI`, {doc}`BULK`).
- **Memory:** reading many radar volumes in a row levels off at about 1 GB instead of creeping up (the engine hands
  freed memory back after each frame, about 0.3 GB less on a Mac), and parallel gridding is limited by the memory
  budget of the computer.

## 0.3.3

- **Autopilot:** dropping a single folder opens it (also folders made of links), the memory budget says where it comes
  from (for example `RAM 17.2 GB, available 3.0 GB -> budget 2.7 GB`), *Cancel* keeps and shows the results written
  before it and *Run again* resumes, a mosaic or other map grid is no longer opened as an empty radar view, and mosaic
  and wind time steps get their own names again (two could overwrite each other and hide the results gallery)
  ({doc}`AUTOPILOT`).
- **Tutorial:** *A whole event in the app (bulk data)* follows a folder of 44 volumes from four radars from the first
  look to the results, the memory budget, cancelling and resuming, tracks, bulk export and what changes with hundreds
  of volumes ({doc}`tutorial-bulk-data`).
- **Python package:** the command-line package stays on the 0.12 line of the radar-reading library it depends on until
  the next line has been checked (the 0.13 line changes some exported unit strings).

## 0.3.2

- **Movies in the installed app:** the Movie Studio can now fetch its movie components on request. They are an
  optional download of about 42 MB (Playwright, which drives your own Google Chrome), shown with what, where from and
  how big before anything is downloaded, kept in your app data folder and removable in Settings. The film itself is
  encoded by Chrome (H.264 and AAC in an MP4; WebM when Chrome has no H.264), so no video library with restrictive
  licences is shipped; `rxd animate --encoder` chooses ({doc}`ANIMATE`, {doc}`settings`).
- **Sound:** the rain is now heard (it read the echo at one height where nothing exists far from the radars; it now
  uses the lowest gridded level up to 2.5 km, and a film whose rain stays silent says so), the screen says "33 flashes",
  the cloud-to-ground or in-cloud type of observed flashes is marked synthetic (GLM does not report it), thunder
  arrival follows the film clock, and the wind label comes from what was actually used.
- **Provenance in the read-out:** the value under the pointer carries the class of the layer it comes from (OBS, RETR,
  ADJ, MODEL, SIM) in the 2D view, the map views, the cross-sections and RHI ({doc}`SCIENCE`).
- **Dealiasing:** a new optional *Fold reference* (`zero` or `vad`) for the dealias step fixes a whole-sweep error of a
  multiple of two Nyquist intervals when the wind is strong and the Nyquist velocity low; the default is unchanged,
  and the option is refused where its assumption fails (sector scans, one-sided echo) ({doc}`PIPELINES`).
- **Rainfall and reflectivity:** averaging reflectivity over time is done in linear units (20 and 40 dBZ average to
  37 dBZ), and summing decibel values is refused ({doc}`HYDROLOGY`).
- **Files:** the app can reopen the Zarr folders it exported (drop the folder on the window) ({doc}`OUTPUT_STANDARDS`).
- **Stability:** a desktop connection holds at most 32 requests in flight (ping, sign-in and every cancel always get
  through).
- **Smaller:** importing the radar library no longer changes the app's logging, `rxd --help` lists every command
  including `auto`, and the unused map-provider hosts were removed from the content-security policy.

## 0.3.1

- **Name:** the app is called **Radarx** under its icon, in the Start menu, in Applications and in the window title;
  the installers are named `Radarx_<version>_...`. The About window keeps its three buttons inside the dialog on Windows
  and macOS.
- **Movie Studio** (desktop app): make a storm movie without a terminal. Choose the files, see the plan (steps, storm
  track, observed and interpolated frames) before anything is drawn, zoom on the strongest storm or show the whole scene,
  preview a frame, pick the scientific or the stylised realistic look, optionally add synthesised sound, render with
  progress and cancel, and play the result in the app ({doc}`ANIMATE`). The installed app does not include the movie
  components yet: the Movie Studio says so and points to the Python package.
- **Command Studio:** every `rxd` command as a form in the app, with files already open offered as one click, live
  "Copy as command", progress, cancel and the produced files ({doc}`command-studio`).
- **Autopilot:** drop any set of files and the app inspects them, explains what makes sense for exactly this data (and
  what does not, and why), estimates time and memory, and runs the steps you approve, recording every decision; the same
  from the command line as `rxd auto` ({doc}`AUTOPILOT`).
- **Big data:** a memory and time budget for mosaics, winds and exports. When a job would not fit, it reduces the work
  in a stated, recorded way or refuses with options instead of failing; `rxd budget` shows the plan and `--budget-gb`
  sets the budget ({doc}`CLI`).
- **Sound for storm movies:** `rxd animate --sound` adds a SYNTHESISED sound track (wind from the retrieved winds,
  rain from the reflectivity, thunder above them, timed to observed GOES GLM flashes, your own flash table or modelled
  flashes), and `--draw-flashes` shows the flashes at their time and place ({doc}`ANIMATE`).
- **Command line:** `rxd` can be installed on its own from a Python package that comes with every release
  ({doc}`installation`); `rxd sounding` labels model and reanalysis profiles as model data, `rxd animate` accepts
  relative paths, plots show `m s-1` units, `rxd check --json` always gives a list, and movie records no longer carry your
  folder names unless you ask.
- **Account:** the Account dialog links to your account on the Radarx website, where you can also sign in, change your
  profile, see your computers and delete the account.
- **Documentation:** a User Guide with topic groups, an installation page with checked download commands, and nothing
  but user documentation.

## 0.3.0

- **Updates:** the app can find, verify and install new versions by itself (signed updates from the download server;
  you stay in control: check on launch, ask first, never during a running job; see {doc}`settings`).
- **Command line:** `rxd` (info, plot, run, pipeline, mosaic, winds, cells, export, check, sounding, qpe,
  basin, animate), with bulk-job recipes ({doc}`CLI`, {doc}`BULK`).
- **3D:** mosaics and multi-Doppler winds in the 3D view, wind-flow particles that follow only the retrieved winds,
  realistic ground (terrain and satellite imagery), a stylised realistic storm look with about 50 colour controls and
  presets, storm clips (orbit, ground-level window, hurricane flight) with interpolation along the observed winds,
  modelled lightning and synthesised sound, all clearly labelled ({doc}`MOSAIC`, {doc}`REALISTIC_3D`, {doc}`STORM_CLIP`).
- **RHI** scans are drawn as range-height sections with sensible height limits.
- **Hydrology:** rain rate and accumulation with uncertainty, gauge adjustment and validation, basin averages
  ({doc}`HYDROLOGY`).
- **Environment and provenance:** a data-provider framework (always asks before any download), soundings with a
  Skew-T and hodograph, and a provenance chip on every layer: observed, model, retrieved, simulated or stylised
  ({doc}`ENVIRONMENT`, {doc}`SCIENCE`).
- **Data check** when a file opens: aliasing, noise, gaps, clutter, blocked beams and ZDR calibration offset, with
  evidence and a suggested recipe that you apply yourself ({doc}`DATA_CHECK`).
- **Map and interface:** a hide-map toggle in 2D and 3D, a radar-sites layer (click a site to open its latest scan,
  after you agree), one consistent look in the dark and light themes, and a value mask that behaves the same
  everywhere, including exports.
- **Files:** NetCDF/CfRadial/Zarr/CSV/PNG outputs follow the CF conventions (checked with the CF checker) and
  carry their provenance ({doc}`OUTPUT_STANDARDS`).
- **Faster and smaller:** NetCDF export of a NEXRAD volume 52 s to 2.6 s, running an algorithm on every sweep about
  5 times faster with far less memory, first paint about 2 times faster, installers about 25 % smaller.
- **More reliable:** a native crash that could stop the engine when opening or reading NetCDF-based files is fixed,
  and 15 more problems found by fault-injection testing are fixed.
- **Accounts:** radarx needs a **free account**, in the app and in the terminal (`rxd login`, `rxd logout`,
  `rxd whoami`; commands exit with code 5 when nobody is signed in).
  The first start asks for name, e-mail, affiliation and what you use it for; later starts open straight in, and
  a sign-in keeps working offline for 30 days after the server last checked it. Codes arrive by e-mail (sent with
  Resend, and with Mailjet if Resend fails). The Account dialog lists your computers and the activity your account
  has sent (feature-level only: never file names, places or contents), and can delete the account. Google, Apple
  and Facebook sign-in appear once they are set up. See {doc}`ACCOUNTS` and the privacy notice
  (a draft, still to be reviewed by a lawyer).
- **Sharing:** after saving an image or movie, a bar offers prefilled posts for X, Facebook, LinkedIn and Bluesky
  (Instagram: the caption is copied and the file shown). radarx never posts for you.
- **Command name:** the command is now `rxd`; `xrd` keeps working as a deprecated alias so existing scripts
  still run ({doc}`CLI`).
- **Name:** the app calls itself Radarx.
- **Fixed (engine):** CfRadial2 files written by xradar work with every module and open in under a second instead
  of about 18 s; the metadata tree no longer keeps the whole file in memory; the difference and accumulation of
  scans on different ray or gate grids match by angle and distance, not by index; URLs and UTC offsets are handled
  correctly in provenance and animation plans; figure titles show the earliest ray's time.
- **Fixed (interface):** the Cloud Data panel works when it opens after the engine connected; Windows paths show
  only the file name; the saved panel order keeps the Rainfall and Pipelines panels; impossible colour-bar limits
  are refused; the browser build loads the realistic ground's tiles; stale "not connected" rows are cleared after
  an engine restart; sidebar splitters and panel headers work from the keyboard; the first load is smaller.
- **Licence:** provided under the PolyForm Noncommercial License 1.0.0; third-party notices are listed in
  {doc}`LICENSES`.

## 0.2.1

- **Open With:** right-click a radar file → *Open With* the app, including numbered copies
  (`file.nc.1`) and, on macOS, files without an extension.
- **Web version:** the engine runs as a secure web service and the app as a static site.
- **Fixed** the 2D azimuth jump on sector scans and rays with gaps; the hover probe no longer reports values
  where nothing is drawn; mosaics no longer show flat plates where no radar saw an echo.

## 0.2.0

- Pipelines, variable mapping, multi-Doppler winds, unified storm-cell tracking, seven more radarx modules,
  reliability work (timeouts, memory budget, no-echo areas in exports).

## 0.1.x

- First installers for macOS, Windows and Linux with the engine bundled; export of cleaned data;
  radarx-based quality control; mosaics; 3D view.
