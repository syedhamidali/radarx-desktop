# What's new

```{include} changes/unreleased.md
```

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
