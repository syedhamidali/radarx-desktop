# `rxd animate`: storm-centred 3D animations from many volumes

`rxd animate` turns a pile of radar volumes (one radar over time, or several radars over time, any
number of files) into an MP4: the strongest storm of the scene in 3D, a camera that flies around it
while the storm travels across the map, a UTC clock, the storm's track on the ground, and labels
that say which frames are real volumes and which are interpolated. It draws with the app's own 3D
renderer (the ray-marched volume, colour scale, map and overlay of the 3D tab) in a headless
Chrome, so the picture matches the app.

```bash
rxd animate -i mayfield -o mayfield.mp4                          # the folder with one sub-folder per radar
rxd animate -i 'mayfield/*/*_V06' --camera top-down -o top.mp4   # a glob, another camera
rxd animate -i mayfield -p still.png --still-at 12               # one frame, to try a camera or a look
rxd animate -i mayfield --no-render                              # the plan only: steps, track, frames (no browser)
rxd animate -i mayfield -o storm.mp4 --winds --sound --flashes-from-glm glm/   # with a synthesised sound track
```

It is the command-line form of the storm clips of the app ({doc}`STORM_CLIP` has the details of the renderer, the
realistic look and the cyclone flight).

## In the app: the Movie Studio

The same thing without a terminal: **Movie** in the toolbar (or "Movie Studio" in the command palette) opens a
dialog with the same six decisions in order. What to film (the files already open, more files, or a folder; a
time window), the plan before anything is drawn (the time steps, the storm's track, how many frames are observed
and how many interpolated, the camera; the same as `--no-render`), the focus and camera (zoom on the strongest storm
or the whole scene, the box size, the presets below, a preview of one frame at a chosen second), the look
(the scientific colour scale or the realistic look, labelled stylised, with its ground, light and palette; title
and subtitle), the sound (synthesised, labelled so, only when the engine has it) and the render, with progress
and a Cancel button in the dialog and in the operations tray. The finished MP4 plays in the dialog, with
"Show in folder", "Export a copy" and the record next to it (which holds the equivalent `rxd animate` command).
Everything the renderer needs is checked first and a missing piece is named with the fix. A stopped render
resumes where it stopped, like the command line.

## Install and what it needs

**In the installed app** the Movie Studio needs the *movie components*, an optional download: Playwright
(it drives your Google Chrome, which draws the frames and encodes the film), about 42 MB to download
and 133 MB on disk. They are not part of the installer, because they would add half again to it and most people
never render a film. When you open the Movie Studio without them, its first box offers the download: it says
what is fetched, from where (`downloads.labshope.com`), how big it is and under which licences, and nothing is
downloaded until you choose *Allow once*. The file is checked against its published size and SHA-256, unpacked
into your data folder (`Library/Application Support/radarx-desktop/movie-pack/<version>` on macOS,
`%APPDATA%\radarx-desktop\movie-pack\<version>` on Windows, `~/.config/radarx-desktop/movie-pack/<version>` on
Linux) and loaded by the app's own engine. Each app version has its own pack (the components must match the
engine exactly); *Settings › Data › Movie components* shows what is installed, its size and a *Remove* button.
Google Chrome stays the one installed on your computer: the app never downloads a browser.

**Who writes the film.** Chrome does: the page that draws the frames encodes each one as soon as it is drawn,
with the browser's own H.264 encoder, adds the AAC sound of `--sound`, and hands the finished MP4 to `rxd`.
No video library is installed or downloaded for this. `--encoder` chooses (`auto` is the default):

| `--encoder` | What happens |
|---|---|
| `auto` | Chrome's encoder when it has one (every current Google Chrome does); otherwise PyAV if you installed it yourself; otherwise PNG frames |
| `browser` | Chrome's encoder, or a clear error if this Chrome cannot encode |
| `pyav` | [PyAV](https://pyav.org/) (`pip install av`), which you install yourself: its wheels bundle an FFmpeg built with GPL parts (x264), so Radarx never ships it; it is a fallback for a Chromium without an H.264 encoder, and the only way to get `--x264-preset` |
| `png` | no film: the numbered frames and the `ffmpeg` line that encodes them |

A Chromium built without H.264 (some Linux distribution packages) still encodes: the film is then VP9 + Opus
in a WebM container, written as `name.webm` with a note in the report; `--encoder pyav` gives an MP4 there.
`--crf` keeps its meaning (18 = visually lossless, higher is smaller): with Chrome it sets the matching target
bit rate (about 13 Mbit/s for 1080p30 at 18), with PyAV it is x264's CRF. Key frames every 2 s, the MP4
starts fast (`moov` first), and the `title` / `comment` tags name the record file either way.

**With the Python package** Playwright comes from the `video` extra:

```bash
pip install -U radarx                       # gridding the radars (Python >= 3.12), needed for every run
pip install "radarx-desktop-engine[video] @ <the wheel address>"   # the optional extra: Playwright (and PyAV, see above)
```

(the wheel address is the one from {doc}`installation`; the `[video]` extra adds Playwright and, as your own
choice, PyAV; Playwright alone is enough with Google Chrome)

| Needs | Why | When it is missing |
|---|---|---|
| `radarx` | merges the radars onto a 3D grid (`rxd mosaic` uses it too) | exit code 4 with the install line |
| `playwright` (extra `video`) | drives Chrome | exit code 4 with the install line |
| Google Chrome | the renderer runs in it and encodes the film (the system Chrome, nothing is downloaded) | exit code 4, says to install Chrome or use `--browser ''` after `python -m playwright install chromium` |
| an encoder | Chrome's own (the normal case), or PyAV if you installed it | the frames are still written as numbered PNG files and the command prints the `ffmpeg` line that makes the MP4; with `--sound` the command stops before drawing (exit code 4 with the fix) rather than write a film without the sound that was asked for |
| the 3D renderer files of the app | the renderer itself | exit code 4 |

Playwright is not needed for the rest of `rxd`. `--no-render` needs none of the four rows below `radarx`.

**Where the renderer is found.** `--app-dist DIR` names the folder with the app's 3D renderer files. A folder is
accepted only if it contains the clip renderer; an older one is refused. Without `--app-dist` the folder named by
the `RADARX_APP_DIST` environment variable is used, then the renderer files of an installed Radarx app (0.3.1 or
later ships them as plain files in its `frontend` resource folder: `Radarx.app/Contents/Resources/frontend` on
macOS, the `frontend` folder of the installation on Windows and Linux), so with the app installed nothing else is
needed. Rendering a clip still needs Google Chrome (the system Chrome) and Playwright; `--no-render` (the plan)
works with every installation. Without any encoder (a Chromium that cannot encode and no PyAV), or with
`--encoder png`:

```console
$ rxd animate -i mayfield -o mayfield.mp4 --encoder png
...
wrote mayfield_frames
to make the MP4 from those frames: ffmpeg -framerate 30 -i 'mayfield_frames/frame_%05d.png' -c:v libx264 -crf 18 -pix_fmt yuv420p -movflags +faststart 'mayfield.mp4'
```

## What it does, step by step

1. **Files to volumes.** Each file gets a radar and a time: from a NEXRAD style name
   (`KHPX20211211_015930_V06`) or, when the name does not say, from the file itself.
2. **Time steps.** The volumes are grouped by radar. The steps follow the volumes of a *reference radar*
   (`--reference`, default: the radar that gives the most steps, then the smallest time differences). For every
   step each other radar contributes its **nearest** volume, if it is within `--tolerance-s` (330 s).
   A step needs two radars (`--min-radars`; one for a single radar) and a step whose volumes are all
   repeats of the previous step is dropped. Everything that is not used is listed with the reason.
3. **Grids, one time step at a time.** The radars of one step are gridded together
   (the same gridding as `rxd mosaic`) at 2 km to find and track the storm, then at 500 m on a box
   around the whole track. Only one step's radar volumes are in memory at once; every result is cached on disk.
4. **The storm.** The strongest convective cell (column maximum of at least `--cell-dbz` 45 dBZ, score =
   maximum dBZ + 2 x echo top + 4 x log10 area) of the first step is followed from step to step
   (`--track cells`, or `linear`: the straight-line fit; or `eye` for a tropical cyclone).
5. **Frames.** The clip runs the data time from the first to the last volume (holding each end for 1.5 s). Between
   two volumes the echo is carried along the storm motion and blended (or along the observed Doppler wind
   with `--winds`): those frames are **interpolated** and say so on the screen.
6. **Render.** Chrome draws frame *i* for the clip time *i* / fps in one full-quality pass (nothing depends
   on the speed of the machine). Frames are saved as they are made; the MP4 is written at the end.

## The time steps

```console
$ rxd animate -i mayfield --no-render
5 time steps from 4 radar(s) (KHPX, KNQA, KPAH, KVWX); reference KVWX
KHPX, KNQA, KPAH, KVWX  11 Dec 2021  |  4 radar(s)  |  5 time steps over 18.1 min  |  1080 frames (36 s at 30 fps, 1920x1080)
  time steps  reference radar KVWX, tolerance 330 s; each radar contributes its nearest volume (offset in s from the reference)
    step  UTC       KHPX  KNQA  KPAH  KVWX
    ----  --------  ----  ----  ----  ----
       0  01:50:44  +193   -26  +171    +0
       1  01:55:22   -85   +49  -107    +0
       2  02:00:00   -30  +108   -25    +0
       3  02:04:18   +39  -150   +89    +0
       4  02:08:52   +99   -88  -185    +0
  offsets     KHPX mean 89 s, max 193 s (5/5 steps); KNQA mean 84 s, max 150 s (5/5 steps); KPAH mean 115 s, max 185 s (5/5 steps); KVWX mean 0 s, max 0 s (5/5 steps)
  storm       followed with the 'cells' tracker: moved 20.9 km toward 55 deg in 18.1 min (mean 19.2 m/s)
    step  UTC          lat      lon  x_km  y_km   m/s  max_dBZ  area_km2  top_km
    ----  --------  ------  -------  ----  ----  ----  -------  --------  ------
       0  01:50:44  36.147  -89.922  -9.3  -5.7     -       62       652    14.0
       1  01:55:22  36.187  -89.858  -3.5  -1.3  26.0       64       616    15.0
       2  02:00:00  36.223  -89.797   1.9   2.7  24.3       65       636    15.0
       3  02:04:18  36.228  -89.788   2.8   3.3   4.0       65       720    14.0
       4  02:08:52  36.257  -89.732   7.8   6.4  21.6       65       616    15.0
  frames      136 observed (13%), 944 interpolated (87%); gaps between volumes 4.3 to 4.6 min; time-lapse x33; interpolation carried along the storm motion only (no wind retrieval)
  camera      orbit from the default orbit, follows 30% of the storm's move; 8 keys, azimuth swept 399 deg, elevation 6 to 86 deg, distance 100 to 215 km
  grids       5 steps at 500 m: 5 computed, 0 from the cache (~/.cache/radarx-desktop/animate); peak memory 1468 MB
nothing was drawn (--no-render): this is the plan; remove --no-render to render it
```

How to read it:

- **offsets**: seconds between the step's reference time and that radar's volume (a positive number means
  the radar scanned later). The mosaic weights the volumes by time, but one step is not an instant:
  up to a few minutes of spread is normal for NEXRAD.
- **storm**: the tracked centre in km from the grid origin and in latitude / longitude, and the speed between
  steps. The per-step speed jumps because the centre of a cell wanders by a few km from scan to scan (step
  3 above): the mean over the whole track is the number to trust. A cyclone is better followed with `--track eye`
  or `linear`.
- **frames**: how many frames sit on a real volume (the two 1.5 s holds, and the moments within 6 s of a volume)
  and how many are interpolated; the gaps between the volumes show how much is carried between them.
  A clip of a few volumes is mostly interpolation, and the screen says so on every such frame.

`--json` prints the same as one document (`inputs`, `steps`, `radar_offsets`, `storm`, `frames`, `camera`,
`look`, `grid`, `output`, `warnings`); `jq '.storm.track[] | [.time, .lat, .lon, .speed_ms_since_previous]'` gives the track.

**Files that are not used** are listed, for example when a radar's volumes are far from every step, when a
file is a repeat, or when a radar cannot be read (the TDWR files of the Laura case):

```console
$ rxd animate -i laura --no-render
  not used    5:
    THOU20200827_045319_V08: could not be read: Could not open THOU20200827_045319_V08: it looks like a NEXRAD Level II file but could not be read: ...
    TIAH20200827_045407_V08: could not be read: site position 30065, -95567 is out of range (a TDWR file with unscaled coordinates)
    ...
```

| Option | Meaning |
|---|---|
| `--reference RADAR` | the radar whose volume times are the steps (default `auto`) |
| `--tolerance-s S` | largest time difference between a step and a radar's volume (330) |
| `--min-radars N` | radars a step needs (2, or 1 for one radar) |
| `--min-step-s S` | thin the steps: none closer together than this (for a long series, `--min-step-s 600`) |
| `--start TIME`, `--end TIME` | use only this window, e.g. `--start 2021-12-11T02:00:00Z` |
| `--max-steps N` | only the first N steps |

A folder is accepted in two forms: a folder of radar files, or a *case folder* with one sub-folder per radar
(only files whose names carry a radar and a time are taken from the sub-folders, so a `results/` folder
beside them is ignored). Globs and `-i -` (names on stdin) work as in every `rxd` command.

## The camera

`--camera` is a preset name or a camera file.

| Preset | What it is |
|---|---|
| `orbit` (default) | a wide oblique view, a slow orbit, a low view along the storm's motion, a top-down view and back (about 36 s) |
| `wide` | a slower, wider orbit for a system a few hundred km across, e.g. a cyclone (`--view-km`) |
| `top-down` | straight down, north up, slowly closing in and out |
| `window` | a camera standing on the ground beside the storm track, true scale (`--observer`, `--observer-dist`, `--observer-side`, `--eye-m`, `--pitch`, `--pan`) |
| `flight` | the cyclone flight: in along a rainband, through the eyewall, into the eye (use with `--track eye`) |

The camera orbits a **target** that follows only part of the storm's move (`--target-lag`, default 0.3; 0 =
fixed on the middle of the track, 1 = locked on the storm), so the storm still travels across the map and past
its landmarks. `--fov DEG` sets the field of view of the whole clip, `--vexag` the vertical exaggeration
(4; the storm is tall and thin, true scale is 1).

### Camera file (TOML or JSON)

```toml
# my_camera.toml
target_lag = 0.35      # optional
fov = 50               # optional: the field of view of every key that has none

[[key]]
t = 0                  # clip time in seconds
azimuth = 230          # alias: theta. Degrees; where the camera is, seen from the target (180 = south of it)
elevation = 32         # alias: phi. Degrees above the horizon (90 = straight down)
distance = 200         # alias: dist. km from the target
target = [0, 0, 4]     # optional [east, north, height] in km, relative to the followed point; or dx / dy / dz
fov = 60               # optional, per key

[[key]]
t = 12
azimuth = 330
elevation = 15
distance = 90
```

Between the keys every value follows a **monotone cubic Hermite curve** (the same as in the
app's storm clips): continuous velocity, no overshoot between keys, zero speed at both ends,
so there are no jerks and no cuts. An azimuth of 505 after 365 is a further 140 degrees of orbit (the
angle is not wrapped). The clip length is `--duration`, else `duration = ` in the file, else the time of the last key; a camera
that ends before the clip stays at its last pose, and keys after the end are reported.

Mistakes are caught before anything is gridded, with the fix in the message:

```console
$ rxd animate -i mayfield --camera bad.toml
error: unknown key "azimut" in keyframe 2 (did you mean "azimuth"?). Known keys: t, azimuth, elevation, distance, target, dx, dy, dz, fov
$ rxd animate -i mayfield --camera topdwn
error: unknown camera preset "topdwn". Did you mean "top-down"? Presets: orbit, wide, top-down, window, flight; or give a camera file (.toml / .json)
```

Other checks: keys must be sorted (`t` increasing), every key needs `t`, `azimuth`, `elevation` and
`distance`, values must be numbers in a sensible range (elevation -5 to 89.9, distance 1 to 3000 km, fov 10
to 120), and at least two keys.

## The look

The look system of {doc}`the realistic 3D page <REALISTIC_3D>` is used as it is: `--look-preset NAME`, then
`--look FILE` (TOML or JSON), then `--set key=value` (highest priority). The scientific dBZ colour scale on
the map is the default; `--realistic` switches to terrain with satellite imagery and the stylised storm
(`--ground`, `--storm-style`, `--palette`, `--light` choose the parts). The look keys are checked **first**, in the
renderer, before any gridding:

```console
$ rxd animate -i mayfield --realistic --set exposur=1.4 -o x.mp4
error: unknown look key "exposur" (did you mean "exposure"?)
```

The imagery and elevation tiles come from the cache of `~/.cache/radarx-desktop/tiles` (`--tile-cache`);
without `--offline` missing tiles are fetched once, politely, as described on the realistic page (and the
EOX imagery carries its attribution and ShareAlike terms, shown on the screen). Modelled lightning is **off**
by default (`--lightning-model` draws flashes labelled "MODELLED from radar echo, NOT observed"). The night
look says it is lit by modelled lightning only when modelled flashes are drawn; without flashes it says "no
lightning is drawn".

## Sound (synthesised, off by default)

`--sound` writes an AAC track (48 kHz, stereo, the same length as the picture) into the MP4. **It is
synthesised from the data; it is not a recording.** Nobody recorded this storm at this place and time, and the
film says so on the screen, in the tags of the MP4 and in the record next to it. Without `--sound` the film has
no audio stream and nothing else changes.

```bash
rxd animate -i mayfield -o storm.mp4 --realistic --light night --winds --sound --flashes-from-glm glm/
rxd animate -i mayfield -o storm.mp4 --winds --sound --flashes-from-glm glm/ --draw-flashes   # the flashes are seen too
rxd animate -i mayfield -o storm.mp4 --winds --sound --lightning-model            # thunder follows MODELLED flashes
rxd animate -i mayfield -o storm.mp4 --winds --sound --sound-listener 12,-4       # where the listener stands
rxd animate -i mayfield --no-render --sound --flashes-from-glm glm/ --json         # the plan: which flashes, which delay
```

**What drives each sound**

| Sound | What drives it | Provenance |
|---|---|---|
| wind | the retrieved multi-Doppler wind at the listener (`--winds`): the lowest level that has a value near the listener, times 0.65 as a stand-in for the ground; the level and the brightness rise with the speed, with gusts. Without `--winds`, or where there is no retrieved wind at the listener, a labelled proxy from the storm motion and the rain is used (the film says "PROXY", and the report says for how much of the time) | retrieved, then synthetic |
| rain | the reflectivity near the ground over the listener: 20 dBZ is silence, 55 dBZ is full | observed, then synthetic |
| thunder | the time and place of each flash of the thunder source (below); the sound of the thunder is synthetic | observed or modelled times, synthetic sound |

**The listener is virtual.** Audio needs a place to listen from. By default it is a point on the ground 10 km
to the right of the storm's track and 4 km upwind of its middle (the place of the window camera, so
`--observer-dist`, `--observer-upwind`, `--observer-side` and `--eye-m` move it), facing the storm. With
`--camera window` or `--camera flight` the listener is the camera itself. `--sound-listener X,Y` puts it at X km
east and Y km north of the middle of the storm's track, whatever the camera is: with the orbit camera the film
shows a view from the sky and the sound is heard from the ground, and the film says "the listener position is
virtual". How far each flash is, and so how loud and how late its thunder is, follows from this choice; it is not
a measurement.

**Thunder** reaches the listener after distance / 343 m/s. A film is a time-lapse, so that delay is divided by
`--sound-compress`; the default is the film's own time-lapse factor (data seconds per film second, printed in the
report), which puts a thunderclap where it belongs in the film's own time: a x24 film divides by 24 (a flash 10 km
away is heard about 1.2 s after it), 1 is the real physics (30 to 150 s, longer than most films). Each part of the
channel arrives at its own time, which gives the rolling; the thunder is louder and brighter the nearer it is, a
close strike has a crack, and it is panned from where it is. The thunder source is one of:

- **none** (the default): wind and rain only. A film with `--sound` and no flash source has no thunder, and says
  "no thunder".
- **`--lightning-model`**: the thunder follows the MODELLED flashes that are then drawn in the picture
  ({doc}`explained here <STORM_CLIP>`); the label says "MODELLED flashes (not observed)".
- **`--flashes FILE`**: a CSV with the columns `time_utc`, `lat` and `lon`, and optionally `type` (`CG` or `IC`)
  and `energy` (only the order matters). The flashes are used as they are: thin them yourself if there are many.
  More than about three a second of film is a continuous roar and the command warns. The film says the flashes are
  those of the file, "not checked here".
- **`--flashes-from-glm DIR`**: OBSERVED flashes of the GOES Geostationary Lightning Mapper, read from Level 2 LCFA
  `.nc` files on disk (a folder, files or a pattern; nothing is downloaded: get them from NOAA's public GOES data
  yourself). The real flash rate near a storm is tens per minute, which would be a continuous roar in a time-lapse,
  so the flashes are thinned: of the good-quality flashes (quality flag 0) within `--glm-radius-km` (default 30) of
  the tracked storm centre at the time of the flash, the strongest by radiant energy of every `--glm-bin-s` seconds of
  data time is kept. The default `--glm-bin-s` is the time-lapse factor (between 10 and 300), which is about one
  thunderclap per second of film. The chosen flashes are written next to the film as `name.flashes.csv`, in the format
  `--flashes` reads, and the report lists how many were read, how many were near and how many were kept.

By default the observed flashes are **not drawn** in the picture: the thunder is heard, not seen. `--draw-flashes`
draws the flashes of the thunder source in the picture (observed GLM flashes, the flashes of a `--flashes` file, or the
modelled ones of `--lightning-model`, which are always drawn), at the place of the flash and at the very flash time the
thunder is timed to, so the viewer sees the flash and hears the thunder arrive after its delay. The screen says what they
are: "OBSERVED GLM flash (position ~10 km, optical, not a stroke)", "flash from FILE (not checked here)" or "MODELLED
from radar echo, NOT observed". Only their time and place are data: the branching channel the light is drawn along and
the thunder rolls along is synthetic, and the type CG or IC of a GLM flash is not known (it is chosen at random, with
a fixed seed). `--sound-seed` (default 7) fixes the noise: the same options give the same samples, bit for bit, on the
same computer; another seed gives other noise.

**The mix: thunder above the bed.** The thunder is mixed clearly above the wind and rain, which stay audible but
secondary and never silent. Compared with the mix of the storm clips of the app, the thunder is raised by 10 dB and the
wind and rain bed lowered by 4 dB (the thunder still gets quieter and duller the farther the flash is), and the loudest
sample is kept below -1.9 dBFS so that the AAC track decodes below -1 dBFS. `--sound-thunder-db DB` moves the thunder
relative to that default (0; from -30 to 20). On the Mayfield case, listener 10 km beside the track, wind 10 to 43 m/s
at the listener, the thunder-only energy in the 1.5 s after each arrival (the film minus the same film without thunder)
was 7.9 dB above the wind and rain bed in the median for the observed GLM flashes and 10.8 dB for the modelled ones; the
loudest thunder samples are about 26 dB above the bed's RMS level. A listener far from the flashes still hears the
thunder fainter, so check the median in the record (`sound` block, `mix`) rather than assuming it.

**What the film and the files say.** On the film two lines in the lower part: "Sound is SYNTHESISED, not a
recording: wind from the retrieved winds, rain from the reflectivity," and "thunder timed to OBSERVED GLM flashes
(not drawn); the listener position is virtual" ("drawn" with `--draw-flashes`; with MODELLED, the file name or "no thunder", and "PROXY" for a wind
that is not retrieved, where that applies). The tags of the MP4 repeat it. In `clip.animate.json` the `sound` block
has the listener (where it stands, as a position and as latitude and longitude), the compression and how it was
chosen, the seed, the range of the wind used, how much of it was a proxy, the selection of the GLM flashes, the
table of the thunder (flash time in UTC and place, the film second of the flash and of the thunder, the delay in the
film and in real time, the distance), the mix (thunder and bed level, the peak ceiling), whether the flashes are drawn,
the audio (codec, length, peak level, a hash of the samples) and the limits.

**Limits of the sound.** It is an illustration. The listener is a virtual point. The wind exists only where two radars
cross well and has the artefacts of every retrieval (see {doc}`MULTIDOPPLER`), and the ground value is a factor, not a
measurement; the roar is a noise bed, not what wind sounds like in a tornadic storm. GLM places a flash to about 10 km,
misses some flashes, and the flashes are thinned. There is no tornado, hail, siren or debris sound: nothing in the data
says anything about them, so do not use the sound to suggest that a tornado was seen or heard. The mix is a choice, not
a measurement: the thunder is raised and the wind lowered so that the thunder is heard; a real storm can be the other way
round. The sound has been
checked by analysing the waveform (its length against the picture, the peak below -1 dBFS, the loudness through the
film, the thunder energy at the arrival of each thunderclap, the same samples twice), not by listening: whether it
sounds natural has not been judged.

## What the screen says

| On screen | Meaning |
|---|---|
| the large clock, `time-lapse x33` | the data time (UTC) of this frame and how fast the data runs against the clip |
| `OBSERVED VOLUME` (yellow dot on the bar) | the frame is within 6 s of a real volume: the radar volumes of that step, nearest volume of each radar |
| `INTERPOLATED between observed volumes ... not new observations` | the echo is the earlier volume carried forward and the later one carried back along the storm motion, blended: nothing here was observed at this moment |
| `carried along the storm motion ... only (no wind retrieval for this interval)` / `... and the observed Doppler wind` | what carries the echo between volumes (`--winds` gives the second) |
| the progress bar with times | every real volume; the white dot is now |
| the line on the ground | the storm's path: solid where it has travelled, dashed ahead, a dot with the UTC time at every volume, a ring at the storm now |
| `storm centre moved 14.6 km since 01:50 UTC` | measured on the track |
| radar masts and `KHPX · 235 km E` | radar sites inside the box; outside it, markers on the edge with name and distance |
| `Stylised daylight; the real sun was 39 degrees below the horizon (not a photograph)` | the light is chosen, not observed; the radar sees rain and ice, not cloud droplets, so the cloud look is stylised while echo position and strength are radar data |
| the credit line | the radars, and the imagery / terrain attribution when those are used (`--credit` replaces the radar part) |
| `Sound is SYNTHESISED, not a recording: ...` (two yellow lines, only with `--sound`) | what drives the wind, the rain and the thunder, whether the thunder follows observed GLM flashes, modelled flashes, a file or none, and that the listener position is virtual |

Nothing is invented: where the radar has no echo none is drawn, and the look controls never add echo.

## Cache, resume and memory

- **Memory is bounded by one time step**, not by the series. The radar files of one step are opened,
  gridded and closed before the next step is touched; what is kept is a column maximum and the cells (coarse
  pass) and the fine grid, which is written to disk at once. Measured on a synthetic series with a 300 MB
  "group" per step (a 4-radar NEXRAD step is about 1.5 GB decoded: the real peak on the Mayfield case was
  1.47 GB, Laura with six radars 2.1 to 2.7 GB), peak memory of the whole process was **446 MB for 5 steps,
  445 MB for 50 and 455 MB for 500 steps**, while the cache on disk grew from 11 MB to 1057 MB.
  The renderer holds at most three grids in the browser.
- **The cache** (`--cache-dir`, default `~/.cache/radarx-desktop/animate`) holds the
  coarse results and the fine grids, keyed by the file names, sizes and times plus the grid settings
  (`--detail-m`, `--cell-dbz`, ...). A **different camera, look, length, size or frame rate does not
  grid again**: the Mayfield plan took 107 s the first time and 0.75 s the second. A changed file gets a new key;
  `--fresh` ignores the cache; deleting the folder is always safe. Entries are written atomically, so a crash
  never leaves a half-written one that looks finished.
- **Resume.** Frames are written to `<cache>/jobs/<job>/frames/` as they are drawn, each atomically. Run the
  same command again after an interruption (Ctrl-C, a crash, a closed laptop) and only the missing frames are
  drawn (`resuming: 61 of 180 frames are already rendered`); the job is identified by the grids, the camera,
  the look, the size, the frame rate and the build of the renderer, so a changed setting starts a new job and never
  mixes frames. Frames are deterministic, so a resumed clip is the same as an uninterrupted one.
- **The MP4 is written at the end**, to `name.mp4.part` and renamed, so a crash never leaves a broken file
  under the final name. Chrome encodes each frame the moment it is drawn and keeps the growing film in its
  memory (the size of the finished file, a few hundred MB at most for a long 1080p clip); `rxd` pulls it in
  4 MB pieces when the last frame is done. The frames are still saved to disk as they are drawn, so a resumed
  job feeds the ones it already has to the encoder and draws only the rest. The working frames are then deleted
  (`--keep-frames` keeps them). They need disk space:
  a 1080p PNG is about 2 to 3 MB with the realistic look, so 720 frames are about 2 GB; the command checks the free space after the first
  frame and stops with the fix (`--frame-format jpg` is about 3 times smaller, a shorter clip, `--size`, or
  `--cache-dir` on a bigger disk).
- **Output** `-o clip.mp4`, or `-o folder/` for a PNG sequence (`frame_00000.png`, ...). With only `-p still.png`
  one frame at `--still-at` seconds is drawn and no video (a fast way to try a camera or a look; the grids come
  from the cache).

Speed (Apple M1 Pro, the work is in the GPU): a 1080p frame with terrain, imagery and soft shadows took 0.51 to
0.59 s, so the 720 frames of the example clip took 7 minutes, plus about a minute to encode; a 960x540 frame
with the flat map takes 0.06 to 0.1 s. The software renderer is several times slower.

## The record of a clip

Next to the result `rxd animate` writes `clip.animate.json` (`folder/animate.json` for a PNG sequence,
`still.animate.json` for a still) and puts a short note in the MP4's tags (`title`, `comment`: the radars, the
time span, that frames between volumes are interpolated, and the name of that file). The record has the
command line, the date, the versions (engine, radarx, xradar, Python, Playwright, Chrome, the encoder that wrote
the film with its codecs, PyAV if present, the build of the renderer), every input volume (radar, volume time,
offset from the step, file name and size, where the time came
from), every parameter, the page settings (the look), the steps, the storm track, the frame plan, the camera,
the files that were not used and the method in words, so a clip can be checked and repeated later. With
`--sound` the record has the `sound` block described above, and the chosen flashes are written next to the film as
`clip.flashes.csv`.
Input files are recorded by name and by their path inside the input folder (`KTLX/KTLX20130520_...`), so the
record can be shared without showing your folders; `--record-paths` also writes each file's full local path
(field `local_path`).

## All options

`rxd animate --help` lists them; the common ones (`-i`, `-o`, `-p`, `-v`, `-q`, `--json`, `--config`,
`--print-command`) are those of {doc}`every rxd command <CLI>`.

| Group | Options |
|---|---|
| clip | `--duration S` or `--timelapse X` (length = data span / X + 3 s), `--fps 30`, `--size 1920x1080`, `--title`, `--subtitle`, `--credit` |
| storm and grids | `--cell-dbz 45`, `--track cells\|linear\|eye`, `--crop-km 60`, `--detail-m 500`, `--winds`, `--cache-dir`, `--fresh`, `--record-paths` |
| camera | `--camera NAME\|FILE`, `--target-lag`, `--fov`, `--view-km`, `--vexag`, and for `window`: `--observer X,Y`, `--observer-dist`, `--observer-upwind`, `--observer-side`, `--eye-m`, `--pan`, `--pitch` |
| look | `--look FILE`, `--look-preset NAME`, `--set KEY=VALUE`, `--realistic`, `--ground`, `--storm-style`, `--palette`, `--light`, `--sea`, `--map-theme`, `--basemap`, `--lightning-model` |
| sound | `--sound`, `--sound-listener X,Y`, `--sound-compress N`, `--sound-seed N`, `--sound-thunder-db DB`, `--flashes FILE`, `--flashes-from-glm DIR`, `--glm-radius-km 30`, `--glm-bin-s S`, `--draw-flashes` |
| render | `--tile-cache DIR`, `--offline`, `--app-dist DIR`, `--browser CHANNEL`, `--software`, `--encoder auto\|browser\|pyav\|png`, `--crf 18`, `--x264-preset medium` (PyAV only), `--frame-format png\|jpg`, `--keep-frames`, `--no-render` |

A config file holds the same names with `_` or `-`; options on the command line win. An option that does
not exist is refused with the nearest one:

```toml
# mayfield.toml
command = "animate"
input = ["mayfield"]
output = "mayfield.mp4"
camera = "my_camera.toml"
realistic = true
look = "my_look.toml"

[set]
exposure = 1.3
```

```console
$ rxd --config mayfield.toml
$ rxd animate -i mayfield --tolerence-s 300
error: unrecognized option(s): --tolerence-s. Did you mean --tolerance-s?
```

Exit codes are those of the other commands: 0 success, 2 a wrong option or setting (a camera or look file,
a size, a duration, a flash file, a sound option without `--sound`), 3 the input files (none found, none usable, fewer than
two time steps), 4 a missing Python package, Chrome, an encoder that was asked for, or the renderer files (also
no encoder at all when `--sound` is given),
1 the processing failed (no storm found: lower `--cell-dbz`; no GLM flash near the storm).

## Examples on the real cases

Two real cases, read only (nothing is written next to the radar files):

```bash
# Mayfield, Kentucky, 11 Dec 2021: four radars, five volumes each over 18 minutes
rxd animate -i mayfield -o mayfield.mp4
rxd animate -i mayfield -o mayfield_window.mp4 --camera window --realistic --timelapse 24
rxd animate -i mayfield -o mayfield_custom.mp4 --camera my_camera.toml \
    --realistic --look my_look.toml --title "Mayfield, KY: the storm in 3D"

# Hurricane Laura, 27 Aug 2020: four readable radars (the two TDWR files are named and left out), four volumes in 15 minutes
rxd animate -i laura -o laura.mp4 --track eye --camera flight --crop-km 140 --detail-m 1000 \
    --realistic --look-preset "Hurricane (stormy)"
```

![The storm at 01:50 UTC (an observed volume), the realistic look, high oblique view](images/animate-example-start.png)
![Low and wide-angle along the inflow side, 02:01 UTC (interpolated between volumes)](images/animate-example-low.png)

## Limits

- **One tracked storm.** The strongest cell of the first step; a merged squall line counts as one cell. The
  track is the centre of reflectivity of that cell, which wanders by a few km between volumes.
- **Interpolation is not observation.** Between two volumes (4 to 5 minutes for NEXRAD) the echo is carried
  along one storm-motion vector (or the retrieved wind where `--winds` has one, about 30% of the cells), so
  parts that move differently (outflow, a second line) are only cross-faded there, and growth or decay between
  volumes is a blend, not what happened. A clip that is almost all interpolation with gaps of more than 10 minutes between volumes gets a warning.
- **The radars' volumes are not simultaneous**: the offsets are printed; a step is not an instant.
- **Time steps follow one reference radar.** Every radar contributes one volume per step; a radar that scans
  more often than the reference has volumes that are not used (they are listed).
- **Needs Chrome, ideally with a GPU**, and the renderer files. Without a working GPU path the command falls back to Chrome's software renderer (`--software` chooses it): it works anywhere, a still took 19 s instead of 7 s here, and a clip is much slower. Resolution is limited by the
  GPU's 3D-texture and canvas limits. Tested on macOS (Apple M1 Pro); Linux and Windows are not tested here.
- **The sound is synthesised, not recorded.** It is off unless `--sound` is given, the listener is virtual, the thunder
  follows only the flashes you name (thinned) and is mixed above the wind on purpose, and there is no tornado, hail or siren sound; see
  the Sound section above.
- **The ground needs tiles.** The flat map's tiles are fetched by Chrome from the basemap server while the
  frames are drawn (`rxd` keeps no cache of them); the satellite imagery and terrain of `--realistic` go through
  the tile cache, and `--offline` uses only that. Without tiles the ground is a grid and the command says so.
- **Radar data are not downloaded.** Give files on disk.
