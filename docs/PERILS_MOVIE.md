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

## The film

`~/Downloads/xradar-exports/perils_movie/perils_qlcs_movie.mp4` (not in the repository): 3600 frames, 120.00 s, 1920x1080
at 30 fps, H.264 (crf 17) with AAC stereo 48 kHz, 292 MB, with the 20-second preview `perils_qlcs_preview.mp4`
(five segments of the film, the sound of the same segments). It shows 960 s of data (01:04:00 to 01:20:00 UTC) in
120 s: a mean speed-up of x8, between x1.0 (the real-time core of the tornado shot) and x9.7. The three files that drive
it (`timeline.json`, `camera_path.json`, `events.json`) are in the same folder and, for reference, in
`tools/perils_movie/reference/` with the measurement of the rotation (`rotation_measurement.json`).
Checks made on the encoded film: 3600 decoded frames and 120.00 s, an audio stream (peak 0.37, rms 0.023), no black
frames, stills every 10 s read by eye (storm in frame, labels readable at 1080p), the flashes of the screen
correlated with the clip times of the real LMA sources (a detrended correlation of 0.17 against 0.00 +- 0.04 for the
same series shifted by 1.5 to 5 s; the 40 largest flashes add a third more bright pixels in the 0.4 s after their
first source than in the second before), and the rotation of the funnel measured in the decoded frames.

## The labels (provenance)

Six classes, always written next to the colour (chips at the bottom of the screen and next to every
panel; the end card repeats them):

| chip | meaning | examples in the film |
|---|---|---|
| **OBSERVED** | measured by an instrument | the radar volume of an observed step, LMA sources, station and disdrometer series, soundings and their balloon tracks, MRMS reflectivity, the tornado survey |
| **RETRIEVED** | derived from observations by an algorithm | the radar couplet of the parent circulation (Vrot, width), debris-signature candidates, hydrometeor classes (hail), ZDR columns, multi-Doppler winds, the MRMS azimuthal-shear swath |
| **ANALYSIS** | an objective analysis of observations | the surface temperature layer and the cold-pool edge (Barnes analysis of the station data), the environment panel |
| **MODEL** | numerical model output | reserved; this film has no model field (see the limits) |
| **INTERPOLATED** | carried between observed volumes | every frame that is not on an observed time; the tornado position between the surveyed begin and end |
| **STYLISED** | drawn for appearance | the cloud look and the lighting, the funnel and its debris, the spin rate of the funnel (an ESTIMATE), the vertical exaggeration, the balloon glyph |

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
* **The plan-view inset** ("Where the camera is", every shot, top right; `perilsMinimap.ts`): the merged column-maximum
  reflectivity of the two observed steps around the clip time, cross-faded, in the colours of the scene and over
  the dimmed schematic map, with the OBSERVED VOLUME / INTERPOLATED tag of the frame; the camera and its view cone
  (heading and lens of the frame), the storm's motion arrow, the radar sites with their names, the stations (the one
  nearest the camera ringed), the surveyed tornado path and the marker of the clip time, the lightning sources of
  the last moments, the radiosondes in flight (a tiny dot and a faint trail, nothing written on them), a scale bar,
  a north arrow and the data clock. The view follows the storm in the wide shots and the camera in the close ones
  (6 km across at the tornado, 24 km at the window); it is larger in the top-down finale and small and faint in the
  tornado shot.
* **Lightning** (`lmaFx.ts`): every VHF source of the lightning mapping array is drawn at its measured
  position and time (no jitter, no invented branches, no synthetic strokes). Consecutive sources of a flash are
  joined by a thin line only when they are within 3.5 km and 0.4 s of each other. A source flares when it
  appears and fades over 0.55 s of clip time (afterglow); colour by position in the flash (white-hot start,
  cooling to blue), optionally by power. The brightest flashes light the cloud, the ground and the sky through the
  renderer's flash light. Panel: flashes in the last minute and the flash-rate series, all counted from the real
  flashes. Because one clip second is about eight data seconds, a flash (about a second of data) lasts a few
  frames and its afterglow is longer than the flash itself.
* **Stations** (37 in the bundle: PIPS disdrometers, TTU StickNet, the RaDAPS site, mobile mesonets, FARM
  Parsivels; vehicles are drawn along their tracks and are never the station nearest the camera): standing on the ground with a pole, a ring that grows with the rain rate, a wind arrow along
  the ground, the live read-out and a pulse when the disdrometer counts a hail candidate; the station nearest the
  camera is highlighted and has the card at the lower left (the audio uses the same one). Strip charts of its
  pressure, temperature and dew point, and wind and gust, with the gust front visible as a step; its drop-size
  spectrum with Dm; the fall-speed-versus-diameter evidence of large particles against the raindrop curve (a
  reference curve).
* **Soundings**: the environment is the PRE-storm radiosonde with the most instability that was launched near the
  line (score: its CAPE in units of 100 J/kg minus its distance from the line in units of 40 km), shown with the
  app's own Skew-T and hodograph (a left-hand inset in the establishing and rising shots) and the surface-based
  parcel path computed from the same profile; where the same site flew again after the storm (the cold pool) that
  profile is dashed over it. Launch times and their offset from the moment on screen are written on the inset (the
  pre-storm one was launched about three hours before). The freezing level and the -10 / -20 degC heights of that
  sounding stand as thin rings around the storm (an assumption of a uniform environment, said on screen).
* **Radiosondes in flight**: the balloons of the soundings whose track AND profile are in the bundle fly along their
  measured track (position OBSERVED, glyph STYLISED): a small balloon appears at its real launch time at the launch
  site (or mid-air at the start of the window when the flight began earlier), climbs along the track with a faint thin
  trail and, at the last point of its track (the burst or where the data end), fades out within 1.5 clip seconds; the
  trail follows 3.5 s later. Nothing is extrapolated, and nothing is written on a balloon (no name, time or read-out).
  Four balloons are in the air during the window (UIUC SONDE1, 2, 3 and 5); they are at 1.4 to 3.8 km by its end, and
  none of them reaches the end of its track inside the window (the last points are at 01:24 to 01:42 UTC), so the film
  shows them climbing and never disappearing; the fade is implemented and tested (`balloonFlight`).
* **Ground**: a schematic map from Natural Earth (public domain) and the Census county lines, no imagery and
  no terrain (flat ground; there are no cached tiles for this region and the build has no network). MRMS
  composite reflectivity outside the radar box (inside it the 3-D volume is the truth), the surface temperature
  analysis (cool to warm) with the cold-pool edge, and the rotation swath so far (MRMS azimuthal shear). A key at the
  left says what the colours mean.
* **Hail and convection**: the cells the radar classifies as hail (RETRIEVED, weak evidence: the bundle has no
  hailstone of 8 mm or more measured at the surface and MESH is 11 to 17 mm) as warm sprites with one label; no falling
  streaks and no sizes are drawn. ZDR columns standing on the melting level, multi-Doppler storm-relative wind arrows
  at 1.5 km (inflow, rear inflow) and the strongest vertical motion cells (updraft warm, downdraft cool); where the
  retrieval is masked nothing is drawn. The multi-Doppler synthesis did not fully converge; only what it retrieved
  is drawn and no outflow-speed claim is made.
* **Captions**: the science event of the moment (`science_events.json` of the bundle) with its own numbers
  and one plain-language sentence of the physics, written here from standard references
  (`perilsCaptions.ts`), with its provenance chip.
* **End card**: every data source and instrument of the manifest with its credit and class, and the legend.

## The tornado

What the data say, and what the picture adds (`perilsVortex.ts`, `tools/perils_movie/render/vortex.py`):

* **Observed (survey)**: the begin and end points and times (01:08:00 to 01:20:00 UTC), the rating (EF2) and the damage
  width (366 m). The path between the ends is a straight line at constant speed: INTERPOLATED.
* **Retrieved (radar)**: `context/vortex_kinematics.json` of the bundle. The radars measure the PARENT circulation: a
  couplet 1.1 to 5.9 km wide (median 1.8 km), rotational velocity Vrot 8 to 15 m/s (median 11), cyclonic in 11 of 13
  steps, one turn in about 8 minutes. **No radar resolves the tornado itself**: it is 366 m wide, the finest effective
  resolution is 500 m (`tornado_resolved = false` at every step). The debris-signature flag is weak evidence at X
  band and is worded "possible debris signature (weak evidence)".
* **Estimated, STYLISED**: the funnel. Its position is the surveyed one, its radius half the damage width (183 m);
  its tangential speed at that radius is the middle of the EF2 damage-based wind range (50 to 60 m/s: 55 m/s, an
  assumption, not a measurement), so omega = Vt / r = 0.30 rad/s and one turn takes 21 s (19 to 23 s over the range;
  the data tool's own estimate is the same). It turns counter-clockwise seen from above, like the parent
  circulation. The card and the lower third of the shot say this in words and numbers.
* **The picture** (`ParticleTornado`, behind the `TornadoRenderer` interface so that a physically based tornado, branch
  `feat/tornado-realism`, can replace it through `PerilsOptions.tornado`): a surface of revolution whose texture
  (spiral streaks and billows) is fixed in the rotating frame, so that it turns at omega; a debris ring with the
  Rankine angular rate (rigid inside the core, 1/r^2 outside) lifted by an assumed updraft of 12 m/s, drawn as hard
  specks with the motion blur of 1/12 s; a dust skirt; and a thin circular arrow on the ground whose cyan arrowhead
  sits at the azimuth omega times the data time. The rotation phase is omega times the DATA time: in the time-lapse
  the funnel turns faster by the speed-up of the frame, in the real-time core at nature's pace.
* **Real time for the tornado**: the timeline slows to 0.35 of the base speed around the shot and its middle 10 s
  (clip 52.4 to 63.6 s) run at x1.0 (REAL TIME is written on the screen); the rest of the film speeds up to x9.7 to
  pay for it. 17 degrees of turn per clip second there.
* **The shot** (clip 46 to 70 s): the camera closes in from 2.6 km to 340 m of the vortex and 37 m above the ground,
  ahead of the tornado with the line behind it, orbiting 64 degrees; a 64 degree lens, true vertical scale and a
  hand-held shake (a few decimetres, a third of a degree). The look is darker and denser (the wall cloud of the
  echo), with more rain streaks, nearer haze and a little vignette. The grade removes the arrows, labels, the
  vertical rotation lines, the cards and the panels; what stays is the real flashes, the plan-view inset (small and
  faint) and one lower third (rating, funnel spin as an estimate, what the radars measured, REAL TIME).
* **Measured in the frames**: `tools/perils_movie/render/measure_rotation.py` finds the arrowhead in the decoded frames
  of the real-time core, projects its pixel back to the ground plane with the camera of the frame (render_movie.py
  `--cam-dump` records the camera the scene drew), turns it into an azimuth around the surveyed tornado position and
  fits the rate against the data time. On the rendered film: 170 frames over 11.4 s of data, 0.303 rad/s, period 20.7 s
  against 20.9 s drawn (0.8 % off), counter-clockwise, ring radius 229.1 m against 228.8 m, scatter 0.3 degrees.

## The clock and the camera

`choreography.py` is the single authority; the page and the sound read its three files
(`timeline.json`, `camera_path.json`, `events.json`, also copied to the export folder and, for reference, to
`tools/perils_movie/reference/`).

* **Timeline**: 960 s of data (01:04:00 to 01:20:00 UTC) in 120 s, monotone, never backwards. The speed is the
  same everywhere except for a slow-down to 0.35 of the base speed around the tornado shot with a real-time core
  (above); the shot's data time is solved so that its middle is the middle of the tornado's life. The speed-up is
  written on the screen (mean x8, between x1.0 and x9.7). The file has the dense mapping (one entry per frame) and the
  speed-up per frame.
* **Camera** (one entry per frame: latitude, longitude, altitude, heading, pitch, lens, plus the scene position and
  the vertical exaggeration): an establishing wide oblique view of the whole line; a line-parallel low flyby with
  the line to one side; the tornado shot (above); the ground-level window camera at a station looking at the
  approaching line (the audio listener is the camera), which turns to watch the line leave; a rise behind the line; a
  top-down finale with the motion of the line toward the top of the frame. Every shot is defined in the frame of the
  line (centre, motion, along-line direction at the data time of the frame), so the camera rides with the storm.
  Consecutive shots crossfade by smoothstep over five seconds, the position and the AIM (heading, pitch) blended
  separately so that the view does not whip when a shot arrives; the tests bound the step, the acceleration and the
  heading change per frame. `camera_path.json` is the path the scene is given; the renderer's own eye (the
  `--cam-dump` file) differs from it a little near the ground, which is why the measurement above uses the dump.
* **Vertical exaggeration** is a camera channel: x2.4 in the wide shots, x1 (true scale) at the ground and in the
  tornado shot. The screen states it.
* **Events** (`events.json`): flashes (clip time of the first source and position), the tornado (clip times, a
  position per second and the vortex numbers: radius, speed, period, the real-time window, the parent circulation and
  that the tornado is unresolved), station hail candidates (none: the bundle has no hail particle of 8 mm or more),
  radar hail cores, heavy-rain onsets, the data's science events, the shot list and the station nearest the camera
  through the clip.

## Making it again

```
# 1. the bundle (feat/perils-data) is read only; convert it and plan the film (engine environment: numpy, scipy, pyproj, matplotlib)
python/.venv/bin/python tools/perils_movie/render/render_movie.py --bundle <bundle> --work <scratch> \
    --export-dir <folder for timeline/camera/events> --counties <cb_2014_us_county_20m.shp> --convert-only
# 2. the app (the renderer serves dist/ itself)
npm run build
# 3. frames and video: a scratch environment OUTSIDE the repository with playwright, av and pillow; Google Chrome
<clipenv>/bin/python tools/perils_movie/render/render_movie.py --bundle <bundle> --work <scratch> \
    [--preview] [--audio <file>] [--cam-dump cams.json] --out movie.mp4
# 4. the rotation measured in the frames of the real-time core
<clipenv>/bin/python tools/perils_movie/render/measure_rotation.py --frames <scratch>/frames --camera camera_path.json \
    --timeline timeline.json --events events.json --perils <scratch>/clip_bundle/perils.json --cams cams.json
```

`--preview "0-4,20-24,..."` renders segments of the film (clip seconds; 20 s for the default five); `--at 8,22,70`
draws single frames; frames are JPEG files (quality 95; PNG with `--frame-format png`) in `<scratch>/frames`
(resumable, shared by preview and final); `--layers hail=0,flow=1` switches layers for the whole film; `--set
key=value` changes the look (the film uses the `stormy` palette, `lowDbzCutoff=24`, `rampMidDbz=42`,
`densityScale=1.5`). A frame takes 0.2 to 0.3 s at 1080p on the author's machine. `synth_bundle.py` writes a
synthetic bundle in the same layout for development; nothing made from it is data. `make_ground.py` draws the
schematic ground; `convert_bundle.py` / `convert_extras.py` turn the bundle into the files the page reads
(`perils.json`, the volumes, the plan-view columns `colmax_k.bin`, the winds, the ground).

## Tests

`python/tests/test_perils_movie.py`: conversion, the echo motion (phase correlation, long baselines, a fixed strong
feature ignored, fallbacks), the timeline (monotone, covers the window, slows down, inverse), the camera (no jumps,
no jerks, bounded heading and pitch, the ground-level shot stands at a station and looks at the approaching line),
the station choice, the events in clip time, and a Chrome end-to-end render (skipped without Chrome or the build).
`python/tests/test_perils_tornado_shot.py`: the real-time core of the timeline, the smooth camera keys, the vortex numbers
(survey estimate, parent circulation, unresolved), the close low tornado shot with its lens and bounded shake, the
events, the aim blending, the measurement of the rotation from pixels (a synthetic ring and camera round trip) and the
conversion of the plan-view columns, the sonde track times and the couplet file.
`src/lib/perils/*.test.mjs` (`npm run test:unit`): the director (clock, pose, lens, nearest station), the data lookups
(series gaps, soundings, balloon flight and fade, tornado, interpolation weights), the real-LMA maths (a source appears
at its time and not before, links break over long steps, only measured positions), layer relevance and the immersive
grade, captions, the parcel path, the surface analysis and ground layers, the insets' helpers, the vortex
(spin numbers, Rankine rates, the indicator phase) and the plan-view helpers.

## Honest limits

* The cloud is the radar echo drawn as cloud: soft, smooth, vertically stretched; radar does not see cloud
  droplets. Looks and lighting are stylised (the real scene was at night).
* One minute between steps is not one minute between independent observations: the merged volume of a step
  contains each radar's nearest volume, and several radars update every few minutes, so consecutive steps share
  data. Frames between steps are interpolation.
* **The tornado vortex is not observed.** The radars resolve the parent circulation (Vrot 8 to 15 m/s, about 1.8 km
  wide), not a 366 m wide tornado. The funnel, its debris and its spin rate are an estimate from the survey (wind range
  and width) drawn as a stylised particle vortex on the overlay of the scene, a flat image over the volume: it has no
  depth test against the echo, no shadows and no real condensation physics. It looks like a diagram of a funnel, not a
  photograph; a physically based tornado (`feat/tornado-realism`) is meant to replace it through the
  `TornadoRenderer` interface.
* The tornado path is a straight line between the surveyed begin and end points, so the marker position is interpolated.
* The ground is flat and schematic: no terrain, no imagery (no tiles and no network). Station and radar heights
  are drawn on a flat ground at sea level. The flat dark ground and the empty sky near the tornado are a large part
  of why the film does not look like a photograph.
* No model (WRF / MPAS / ERA5) field and no lidar exist in the data bundle for this window, so the MODEL class is
  in the legend but nothing is drawn with it. Hail: the classification is weak to moderate evidence only; no hail
  particle was measured at the surface.
* The multi-Doppler winds are the least reliable of the retrievals (w most of all) and did not fully converge;
  the retrieval is masked where it is poorly constrained and only the masked-in cells are drawn.
* The radiosondes: only four flew inside the window and none of them was above 3.8 km at its end; the profiles are
  not at one place or one time (the balloon drifts), which the Skew-T inset says.
* The camera paths over the line and the speed-up are choreography, not data. Frames are 4:2:0 H.264 made from JPEG
  frames (quality 95) to fit the disk.
