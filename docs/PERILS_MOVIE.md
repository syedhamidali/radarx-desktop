# The PERiLS IOP2 movie

A 120-second, 1920x1080, 30 fps film of the squall line (QLCS) and the EF2 tornado of PERiLS IOP2, the
evening of 30 March 2022 (local), 31 March 01:04 to 01:20 UTC over Mississippi and Alabama, made with the
app's own 3-D renderer from REAL data: the merged volume of six radars, real lightning-mapping-array
(LMA) sources, disdrometers and surface probes, radiosondes, MRMS, the tornado survey and the products
derived from the radar (rotation, hydrometeor classes, multi-Doppler winds). Everything on the screen
says what kind of number it is. This page says how it was made, what each label means, how to make it
again, and what is weak.

The data side is [PERILS_BUNDLE.md](PERILS_BUNDLE.md) (branch `feat/perils-data`), the sound is
[PERILS_AUDIO.md](PERILS_AUDIO.md) (`feat/perils-audio`); this page is the renderer and the assembly
(`tools/perils_movie/render/`, `src/lib/perils/`).

## The labels (provenance)

Six classes, always written next to the colour (chips at the bottom of the screen and next to every
panel; the end card repeats them):

| chip | meaning | examples in the film |
|---|---|---|
| **OBSERVED** | measured by an instrument | the radar volume of an observed step, LMA sources, station and disdrometer series, soundings, MRMS reflectivity, the tornado survey |
| **RETRIEVED** | derived from observations by an algorithm | rotation (azimuthal shear, Vrot), debris-signature cells, hydrometeor classes (hail), ZDR columns, multi-Doppler winds, the MRMS azimuthal-shear swath |
| **ANALYSIS** | an objective analysis of observations | the surface temperature layer and the cold-pool edge (Barnes analysis of the station data), the environment panel |
| **MODEL** | numerical model output | reserved; this film has no model field (see the limits) |
| **INTERPOLATED** | carried between observed volumes | every frame that is not on an observed time; the tornado position between the surveyed begin and end |
| **STYLISED** | drawn for appearance | the cloud look and the lighting, the funnel, the rain streaks, the falling hail streaks, the vertical exaggeration |

The radar volume is the truth for the echo: the look never changes data, it only decides how the echo
is drawn (and says so on screen).

## What is in the picture

* **The echo**: the merged 3-D reflectivity of the six radars on a 500 m grid (the data bundle's
  `volumes/step_NNNN.npz`), drawn by the clip renderer (`utils/stormClip.ts`, the same ray-marched volume as the
  3D tab and `xrd animate`) in the stylised Realistic look. Steps are one minute apart; a frame on an observed
  step says **OBSERVED VOLUME** and lists the radars that contributed with their time offsets (e.g.
  `KGWX +10 s`); every other frame says **INTERPOLATED** and between which two observed times.
* **Interpolation** between two steps: the volumes are carried along the echo motion and the multi-Doppler
  wind where it exists (`morphPass.ts`), then cross-faded. The motion is the translation of the echo
  pattern. It is estimated by phase correlation of the column-maximum reflectivity of pairs of steps three to
  eight minutes apart, with what does not move taken out; because consecutive merged steps share radar
  volumes, what has not changed between them dominates the correlation and the estimate is only used when it
  agrees with the **pattern motion of the data bundle**, a long baseline between two volumes of one radar
  (10.6 m/s toward 90 degrees). The screen says which motion was used. It is interpolation, not new
  observations, and it never extends beyond the observed times.
* **Lightning** (`lmaFx.ts`): every VHF source of the lightning mapping array is drawn at its measured
  position and time (no jitter, no invented branches, no synthetic strokes). Consecutive sources of a flash are
  joined by a thin line only when they are within 3.5 km and 0.4 s of each other. A source flares when it
  appears and fades over 0.55 s of clip time (afterglow); colour by position in the flash (white-hot start,
  cooling to blue), optionally by power. The brightest flashes light the cloud, the ground and the sky through the
  renderer's flash light. Panel: flashes in the last minute and the flash-rate series, all counted from the real
  flashes. Because one clip second is about eight data seconds, a flash (about a second of data) lasts a few
  frames and its afterglow is longer than the flash itself.
* **Stations**: standing on the ground with a pole, a ring that grows with the rain rate, a wind arrow along
  the ground, the live read-out and a pulse when the disdrometer counts a hail candidate; the station nearest the
  camera is highlighted and has the card at the lower left (the audio uses the same one). Strip charts of its
  pressure, temperature and dew point, and wind and gust, with the gust front visible as a step; its drop-size
  spectrum with Dm; the fall-speed-versus-diameter evidence of large particles against the raindrop curve (a
  reference curve).
* **Soundings**: sites with the balloon's drift track, the freezing level and the -10 / -20 degC heights as
  ticks and as thin rings around the storm, and the app's own Skew-T and hodograph of the sounding nearest in time
  (launch time and its offset from the clip time on the inset; there is no sounding inside the 16 minutes, the
  nearest were launched two hours before), with the surface-based parcel path computed from the same profile.
* **Ground**: a schematic map from Natural Earth (public domain) and the Census county lines, no imagery and
  no terrain (flat ground; there are no cached tiles for this region and the build has no network). MRMS
  composite reflectivity outside the radar box (inside it the 3-D volume is the truth), the surface temperature
  analysis (cool to warm) with the cold-pool edge, and the rotation swath so far (MRMS azimuthal shear). A key at the
  left says what the colours mean.
* **Tornado**: the surveyed track on the ground with its start and end times and rating, a marker at the position
  of the clip time (the survey gives the begin and end points only; the path between them is a straight line and
  says so: INTERPOLATED), the radar evidence in 3-D (a glowing column at each low-level rotation track point, the
  swath, and the cells flagged as a debris signature near a vortex, RETRIEVED), a callout card (rating, Vrot, the
  radar and its resolution, debris signature yes / no) and, STYLISED and switchable, a condensation funnel and
  debris ball at the surveyed position, as wide at the ground as the surveyed path width. It is drawn only while a
  survey exists for that time.
* **Hail and convection**: the cells classified as hail (RETRIEVED) as warm sprites with short falling streaks
  (STYLISED), ZDR columns standing on the melting level, multi-Doppler storm-relative wind arrows at 1.5 km (inflow,
  rear inflow) and the strongest vertical motion cells (updraft warm, downdraft cool); where the retrieval is
  masked nothing is drawn.
* **Captions**: the science event of the moment (`science_events.json` of the bundle) with its own numbers
  and one plain-language sentence of the physics, written here from standard references
  (`perilsCaptions.ts`), with its provenance chip.
* **End card**: every data source and instrument of the manifest with its credit and class, and the legend.

## The clock and the camera

`choreography.py` is the single authority; the page and the sound read its three files
(`timeline.json`, `camera_path.json`, `events.json`, also copied to the export folder).

* **Timeline**: 960 s of data (01:04:00 to 01:20:00 UTC) in 120 s, monotone, never backwards. The speed is the
  same everywhere except for a slow-down to 0.35 of the base speed around the tornado shot (the shot's data
  time is solved so that its middle is the middle of the tornado's life). The speed-up is written on the screen
  (mean x8, between x3 and x9). The file has the dense mapping (one entry per frame) and the speed-up per frame.
* **Camera** (one entry per frame: latitude, longitude, altitude, heading, pitch, plus the scene position and
  the vertical exaggeration): an establishing wide oblique view of the whole line; a line-parallel low flyby with
  the line to one side; the tornado shot, a safe distance ahead of the tornado with the line behind it, in a slow
  orbit; the ground-level window camera at a station looking at the approaching line (the audio listener is the
  camera), which turns to watch the line leave; a rise behind the line; a top-down finale with the motion of the
  line toward the top of the frame. Every shot is defined in the frame of the line (centre, motion, along-line
  direction at the data time of the frame), so the camera rides with the storm. Consecutive shots crossfade
  by smoothstep over five seconds, so the position and its velocity are continuous (the tests bound the step, the
  acceleration and the heading change per frame).
* **Vertical exaggeration** is a camera channel: x2.4 in the wide shots, x1 (true scale) at the ground. The screen
  states it.
* **Events** (`events.json`): flashes (clip time of the first source and position), the tornado (clip times and a
  position per second), station hail candidates, radar hail cores, heavy-rain onsets, the data's science events,
  the shot list and the station nearest the camera through the clip.

## Making it again

```
# 1. the bundle (feat/perils-data) is read only; convert it and plan the film (engine environment: numpy, scipy, pyproj, matplotlib)
python/.venv/bin/python tools/perils_movie/render/render_movie.py --bundle <bundle> --work <scratch> \
    --export-dir <folder for timeline/camera/events> --counties <cb_2014_us_county_20m.shp> --convert-only
# 2. the app (the renderer serves dist/ itself)
npm run build
# 3. frames and video: a scratch environment OUTSIDE the repository with playwright, av and pillow; Google Chrome
<clipenv>/bin/python tools/perils_movie/render/render_movie.py --bundle <bundle> --work <scratch> \
    [--preview] [--audio <file>] --out movie.mp4
```

`--preview` renders four segments of the film (20 s); `--at 8,22,70` draws single frames; frames are PNG files
in `<scratch>/frames` (resumable, shared by preview and final); `--layers hail=0,flow=1` switches layers for the
whole film; `--set key=value` changes the look (the film uses the `stormy` palette, `lowDbzCutoff=24`,
`rampMidDbz=42`, `densityScale=1.5`). A frame takes about 1 s at 1080p. `synth_bundle.py` writes a synthetic
bundle in the same layout for development; nothing made from it is data. `make_ground.py` draws the schematic
ground; `convert_bundle.py` / `convert_extras.py` turn the bundle into the files the page reads.

## Tests

`python/tests/test_perils_movie.py`: conversion, the echo motion (phase correlation, long baselines, a fixed strong
feature ignored, fallbacks), the timeline (monotone, covers the window, slows down, inverse), the camera (no jumps,
no jerks, bounded heading and pitch, the ground-level shot stands at a station and looks at the approaching line),
the station choice, the events in clip time, and a Chrome end-to-end render (skipped without Chrome or the build).
`src/lib/perils/*.test.mjs` (`npm run test:unit`): the director (clock, pose, nearest station), the data lookups
(series gaps, soundings, balloon, tornado, interpolation weights), the real-LMA maths (a source appears at its
time and not before, links break over long steps, only measured positions), layer relevance, captions, the
parcel path, the surface analysis and ground layers, the insets' helpers.

## Honest limits

* The cloud is the radar echo drawn as cloud: soft, smooth, vertically stretched; radar does not see cloud
  droplets. Looks and lighting are stylised (the real scene was at night).
* One minute between steps is not one minute between independent observations: the merged volume of a step
  contains each radar's nearest volume, and several radars update every few minutes, so consecutive steps share
  data. Frames between steps are interpolation.
* The tornado path is a straight line between the surveyed begin and end points. The funnel is a symbol.
* The ground is flat and schematic: no terrain, no imagery (no tiles and no network). Station and radar heights
  are drawn on a flat ground at sea level.
* No model (WRF / MPAS / ERA5) field and no lidar exist in the data bundle for this window, so the MODEL class is
  in the legend but nothing is drawn with it; the environment panel is the analysis the bundle has.
* The multi-Doppler winds are the least reliable of the retrievals (w most of all); the retrieval is masked
  where it is poorly constrained and only the masked-in cells are drawn.
* The camera paths over the line and the speed-up are choreography, not data.
