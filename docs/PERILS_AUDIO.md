# PERiLS movie: the soundtrack

The real-data movie of the PERiLS IOP2 QLCS and tornado (30 March 2022 local, 31 March UTC) has a
soundtrack. **It is synthesised from the data, not a recording.** No sample, no field recording, no
music: filtered noise and short impact kernels, placed in time, level, direction and timbre by the
observations. The movie says so on screen and in `audio_report.json`.

The code is `tools/perils_movie/audio/` (numpy and scipy; no new repository dependency). It
generalises the sound of the earlier storm clips (`src/lib/utils/soundscape.ts`: thunder delay
`distance / 343 m/s / speedup`, per-segment arrivals, the low-pass falling with range, the
crack of a close strike) and keeps their label.

## What is physics and what is art

| Part | Physically grounded (from the data) | Artistic (invented) |
|---|---|---|
| Thunder timing | every LMA VHF source is a channel element at its real position and time; its sound arrives at `t_source + r / 343 m/s`; the onset follows the clip clock (`r / 343 / speedup` for a constant speedup), the later arrivals keep their real spacing | none |
| Thunder pulse | each source radiates an N-wave (sharper and shorter close, longer with a slower front far away), the ISO 9613-1 air absorption of its own distance (minimum phase), a 40 or 80 Hz relaxation pulse, a ground reflection, scattering and turbulence | the N-wave shape laws, the pulse amplitudes, the scattering tail |
| Thunder level | the distance to every source, the source power (relative, within the flash), the source count and density; **compressed for audibility** (see below): 12 dB per decade of range between flashes instead of the 20 of `1/r`, the physical `1/r` inside one flash | the compression itself, the energy proxy |
| Thunder fine structure | none measured | **sub-resolution tortuosity** and the **channel between sources** around an arrival gap are modelled statistically (seeded, labelled in the report) |
| Rain | rain rate (station, on a log curve), drop-size spectrum (number flux per size) when `<id>_dsd.npz` exists, Dm, MRR lowest gate when the listener is over the site, distance weighting | the impact kernels, the curve constants |
| Wind | observed speed, gust, direction of the station nearest the listener; the gust front and pressure jump come out of the data | fine gust structure (amplitude from the observed variability) |
| Hail | station hail events (time, max diameter, counts), the tail of the drop spectrum, events.json, radar hail class: never without evidence | the ring of the impact |
| Tornado roar | existence in time and position from the tornado track / events; level and muffling from the distance to the camera | everything about the timbre: **STYLISED** |
| Convective bed | convective index at the listener (radar volume samples, else the station reflectivity) | the rumble |
| Ambient | none | a very quiet synthetic noise bed (no insects, no room tone, no music) |

## Time-lapse

The clip is a time-lapse (`speedup` data seconds per clip second, from `timeline.json`; its `mapping` clip time to
data time is used, so a varying speed-up is exact). Rain, wind and the like follow the clock. **Thunder does not
follow it inside itself**: a thunder of 5 to 20 s compressed 8 times is a click, so

* the **onset** (the first sound, from the nearest part of the channel) is where the clock puts it: the data time
  `t + r / 343` mapped to clip time, i.e. `r / 343 / speedup` for a constant speed-up, and exact in a real-time
  shot (speedup 1, the tornado shot of the film);
* **every later arrival keeps its real sound time**: the far parts of a channel arrive `(r_i - r_min) / 343` s
  after the onset (plus the real emission spacing of the sources), whatever the clip's speed-up, so a thunder lasts
  as long as it would in real time and rolls with its real character. A thunder is voiced for at most 15 s (the last
  4 s fade): a long flash is cut, not squeezed.

The camera position at the time of the flash gives the distances; the heading at the arrival of each source gives the
panning. Sound speed is the constant 343 m/s (it varies by a few percent with temperature).

## The listener

The listener is the camera: its position at the time of the flash gives the distances, its heading
gives the left / right. A wide shot from 58 km up would hear nothing (the sound of a storm does not
reach that high), so the listener is **the ground track of the camera, at most 500 m above the ground**
(`--max-listener-height`, 0 = the camera's own altitude); the report states the cap and the highest camera
altitude. Stations and the radar echo are taken at the listener's horizontal position; a station
only counts when it is near enough (rain and reflectivity 30 km, other variables 40 km, an MRR 3 km).
Where nothing is near, the layer is silent: the soundtrack is quiet while the camera is far from the
storm, as it would be.

## Layers and priority

Priority (a layer is ducked by the louder layers above it, up to 10 / 6 / 4 / 3 dB): **thunder >
tornado > hail > rain > wind**, then the convective rumble and the ambient bed. Each layer has its own level in
`audio_report.json` (`layers.<name>.rms_dbfs`, `lufs`, `peak_dbfs`, 1 s level trace, maximum ducking).

* **Thunder**, one per real LMA flash, built from thunder acoustics (below); the delay is physical, the level of
  a flash is compressed for audibility, the selection is by audibility score with variety.
  - *Level compressed for audibility; delay is physical.* A strict `1/r` (20 dB per decade of range) puts a flash
    40 km away 20 dB under one at 4 km and a whole storm under the rain. The level of a **flash** is
    `L_ref - 12 dB x log10(r / 5 km)` (no louder than at 1 km; `audibility_gain`), which keeps the order (nearer is
    louder) and puts a 50 km flash 12 dB, not 20 dB, under a 5 km one. **Inside one flash** the parts keep their
    physical `1/r` ratios, so the attack is sharp and the decay natural. The air absorption is applied for half the
    distance (`absorption_scale`, 1 = physical): the same compression for the brightness, so a far thunder is still
    a rumble with some body. `ThunderParams(level_model="physical")` and `absorption_scale=1` restore the strict physics.
  - *Selection by audibility score, with variety.* `score = level at the listener + 6 dB per decade of the number of
    sources (the big flashes) + 4 dB if the flash is within 10 km of the tornado`, minus 3 dB for every voiced flash of
    the same range class (near < 10 km, mid 10 to 30, far) within 6 s: cracks, rolls and rumble are mixed. (1)
    **Guarantee**: in every 8 s of clip time (by the time the sound arrives) the best flash within 80 km is voiced; a
    window with none that near gets the best farther one (to 150 km). (2) The rest by score, up to 3 overlapping
    voices, at most 3 starts within +-1 s, masked only when more than 20 dB below a louder one. Skipped flashes are
    counted with their reason (`skipped_density`, `skipped_masked`, `skipped_voices`, `skipped_out_of_range`,
    `outside_clip`) and every voiced flash says how it was chosen (`selection`). Only real flashes are voiced.
  - *What the report says.* `thunder.selection` (flashes in the bundle, voiced, by rule, not voiced, per 8 s window),
    `thunder.loudest_events` (the 10 loudest) and the full row of every voiced flash: clip time, distances, delay, onset,
    range class, channel extent, level, margins over the bed / rain / tornado, `n_pulses`, `n_reflections`,
    `n_tortuosity_points`, `n_bridge_elements`, `rendered_duration_30db_s`, `spectral_centroid_hz`.
  - *A flash that cannot be heard yet is not voiced*: sound that has not arrived by the end of the clip is
    `outside_clip`; while the camera is 100 km from the line (the wide shot) no flash can have arrived (the nearest flash at 60 km
    needs 175 s of data time, 22 s of clip at 8x): that opening is ambient only, by physics.

* **Rain**. A Poisson stream of drop impacts per size class. With a drop spectrum the rate of each
  class is proportional to its number flux (`N(D) v(D) dD`), the amplitude to the mass^(2/3) of the
  drop (`D^2`), larger drops ring longer and lower with a hard click: heavy rain with big drops is
  fat and low, drizzle is a fine hiss (the hiss share comes from the flux of drops under 1 mm).
  Without a spectrum the classes come from an exponential spectrum with the median volume diameter
  (station Dm, the MRR lowest gate within 3 km, else an assumed `0.9 R^0.21`, reported as
  `assumed_dm_fraction`). Total level follows the rain rate on a perceptual curve (-58 dB at 0,
  +14 dB per decade of `1 + R`) plus +3 dB per doubling of Dm; rain you are not under (nearest station
  far away) fades to a quiet hiss (`1/(1+(d/5 km)^2)`, floor -22 dB). The nearest station dominates
  with a smooth crossfade. **Where no instrument is within 8 km of the listener the rain rate comes from the
  radar echo over the listener** (the lowest echo of the merged volumes, mean over 2 km, `Z = 300 R^1.4`,
  capped at 150 mm/h, interpolated in time between the 60 s volumes; no echo means no rain): a rough estimate,
  labelled `radar_zr` in the report, with an assumed drop size.
* **Wind**. Level ~ speed^1.5 (30 dB per decade), brighter with speed, from the station nearest the
  listener; a high-rate station (sampling <= 7 s) is used as observed (the synthetic gust part drops to a quarter),
  the gust column sets the top of the excursions, the direction (relative to the heading)
  tilts the stereo balance. A **gust front** is whatever the series contains: the sharp rise is
  heard as a swell. Only where a station's own pressure series rises by more than 0.8 hPa in 2
  minutes is a **pressure-wave rumble** (31 to 125 Hz) added; calibration offsets between stations
  are never a jump. Wind aloft (multi-Doppler winds) is not audible and not used.
* **Hail**. Hard impacts (ping with a short ring and a thud, pitch down with size) on top of the
  rain, only where hail evidence is within 5 km of the listener: the PIPS size-velocity hail candidates
  (`hail_events.json`: 10 s samples with particles of 8 mm or more, merged per station in the report),
  a piezo hail sensor reading above 0.5 hits/cm2/h, or the radar hail class (code 10) in the lowest 1.5 km over
  the listener. Rate `6 F^0.6` per second from
  the large-particle flux `F` (event counts / (0.0054 m2 x duration), or the tail of the drop spectrum);
  amplitude from the maximum diameter (mass^(2/3)). Missing fields are assumed and listed per event in
  `assumed`.
* **Tornado**. Roar (40 Hz to 2 kHz, a slow throb) and debris rattle, only while the survey track (and
  the renderer's tornado event interval, when it lists one) says the tornado exists; level
  compressed like the thunder (12 dB per decade, no louder than at 1.5 km: a roar next to the tornado would bury
  every other layer once the mix is normalised), zero beyond 25 km, muffled when far (3.5 kHz close, 120 Hz far).
  Position from the survey; the sound is stylised and labelled so. No siren.

## Thunder acoustics

*Sources in the literature*: Few (1970), Holmes et al. (1971), Hill (1977), Bass (1980), Ribner and Roy (1982). The
model, and the check against it:

| Physics (literature) | In the synthesis (`thunder.py`, `nwave.py`) |
|---|---|
| Thunder is the sum of the sound from every element of the channel; the onset is the nearest point, a long or tilted channel rolls on | every LMA source is an element at its real position and time; arrival `t_i + r_i / 343`; onset = earliest arrival; duration from `(r_max - r_min) / c` |
| Close to the channel the wave is a sharp N-wave (shock steepening: the stronger and nearer, the sharper); crack with energy at 1 to 5 kHz | N-wave of 0.1 ms rise and 2 ms length at 200 m, 1.6 ms and 5 ms at 6 km, 3 ms and 10 ms at 20 km; elements within 1.5 km are boosted (`crack_boost`) |
| Spectral peak of the rumble at 20 to 100 Hz (heated-channel relaxation, tortuous segments) | a damped 40 Hz pulse per source, 80 Hz where the local density of sources is high; nothing below 20 Hz (not reproducible) |
| Air absorption removes the highs with distance (ISO 9613-1) | the minimum-phase filter of each source's own distance (`absorbed`), 12 distance bins; test: within 1 dB of ISO 9613-1 at 1 km and 20 km |
| Tortuosity of the channel (10 to 100 m) makes the crackle | **modelled**: between consecutive sources closer than 8 km, elements at random spacing 1 to 20 m with 3 m lateral scatter, seeded (`sub-resolution tortuosity modelled`) |
| The LMA samples the channel, it does not cover it | **modelled**: arrival gaps longer than 0.4 s are filled along the chord between the sources around them (`n_bridge_elements`) |
| Ground reflection, scattering and turbulence give the long irregular tail | a reflected copy of every source (ear at 1.7 m, coefficient 0.5), a slow amplitude modulation (4 to 30 %, growing with distance) and a decaying sparse-echo tail (0.5 to 2.5 s, 20 to 50 %) |
| Binaural cues | every arrival panned by its azimuth relative to the camera heading at that time (equal-power level difference, Woodworth time difference, cone-of-confusion reduction for elevated sources, head shadow); a long flash is wide |

*Not modelled*: refraction (wind and temperature gradients bend and shadow thunder), terrain and building shadowing,
the individual return strokes (the LMA shows the channel, not the strokes: multiple-stroke detail is absent), the true
acoustic strength of a source (VHF power is not acoustic energy), the Doppler shift of the moving camera, and
real-air nonlinear propagation beyond the two power laws. It remains synthesis, not a recording.

*Verification* (`tools/perils_movie/audio/verify_thunder.py`, `tests/test_perils_audio.py`): the first arrival of
a single source is its distance / 343 / speedup to 1 ms; the spread of the arrivals is `(r_max - r_min) / 343` whatever
the speed-up; the duration grows with the extent; the spectral centroid falls with the distance (800 m, 3, 12, 40
km); a close channel has energy at 0.5 to 5 kHz and a 40 km one peaks below 150 Hz; the N-wave has no net impulse;
the render is deterministic; no clipping. The previous synthesis (a filtered noise per direction sector under a smooth envelope) was diagnosed with the same
tools: a stationary noise bed with a smooth hump of an envelope, a spectral centroid that hardly moved with the
distance (127, 125, 104, 106 Hz for 6, 12, 30, 35 km), no individual arrivals, no structure: that is why it sounded
fake. The new one, for the same four flashes: centroid 81, 55, 45, 42 Hz, an onset on the arrival time, bursts of 10
to 30 dB inside the rumble where the channel's branches arrive, the spectrum's upper edge falling from about 3 kHz
(6 km) to 700 Hz (35 km) (`audio/plots_v4/thunder_isolated_v3_vs_v4.png`).

*Option, not done*: a hybrid with real thunder recordings (CC0 / public-domain: for example freesound.org
sounds marked CC0, or the Wikimedia Commons public-domain thunder files) could add the natural broadband character
and the 1 to 5 kHz detail of a close strike that a model of this kind does not have. It would need a download
(none was made), a licence check per file (CC0 needs no credit; CC-BY needs the credit on the end card; CC-BY-NC
conflicts with the project licence use), and would then be mixed under the same timing (onset, N-wave positions) from
the real flash. Only with the maintainer's agreement.

## Mix

BS.1770 loudness (K-weighting, gated): **-23 LUFS integrated**, look-ahead limiter, **true peak below
-1 dBFS** (4x oversampled), 2 s fade in and 2.5 s out, no clipping, deterministic (seeded). Mono
compatibility is reported (the loss of the mono sum against the stereo RMS, the L/R correlation).
Output 48 kHz stereo WAV (24 or 16 bit with seeded TPDF dither).

**Thunder has the headroom.** The integrated loudness stays at -23 LUFS, but the other layers are trimmed (or raised)
together until the *median voiced thunder stands 12 dB above the rest of the mix* (rms over its first 4 s;
`master.bed_trim_db`, `master.thunder_median_margin_over_bed_db`), and the thunder is then what sets the loudness:
its peaks sit high in the mix, up to the true-peak limit of -1 dBFS (the limiter ceiling is -1.6 dBFS).
Optionally (`--thunder-peak`, off by default) the thunder gain is iterated to put its loudest peak at a given level.

## audio_report.json

`label`, `params` (speedup, seed, thunder parameters), `master` (LUFS, true peak, limiter, ducking,
mono check), `layers` (per layer level and the data that drove it), `thunder` (counts, `selection`, `loudest_events`, the flash table),
`assumptions`, and **`provenance`**: per layer the stations/instruments that were the driver and when
(clip and data seconds, mean distance), the gust fronts and pressure jumps found, the hail evidence rows
(station, time, max diameter, counts, flux), the tornado tracks, the station list with the layers
each drove (for the end card), and the `sources` of the manifest.

## Inputs

What the loaders read (all times are data seconds since the window start of `manifest.json`):

| File | Used for | Fields used |
|---|---|---|
| `manifest.json` | window start, radar grid, steps | `window.start`, `grid` (origin, x_km, y_km), `steps[].time` / `.volume`, `sources` (copied to the report) |
| `lma/flashes.json` | thunder | `t0`, `flashes[]`: `id`, `t_start_s`, `centroid`, `n_sources`, `sources` `[t_s, lat, lon, alt_m, power_dbw]` (power may be null) |
| `stations/stations.json` | rain, wind, pressure rumble, convective fallback, hail sensor, air state | per station: `id`, `name`, `type`, `lat`, `lon`, `alt_m` (null allowed), `credit`, `instrument`, `series` (`rain_rate_mmh`, `dm_mm`, `dbz`, `wind_speed_ms`, `wind_gust_ms`, `wind_dir_deg`, `pressure_hpa`, `temperature_c`, `hail_intensity_hits_cm2h`; nulls are missing data; a `lat` / `lon` series makes a vehicle a moving station) |
| `stations/<id>_dsd.npz` | rain from the drop spectrum, hail tail | `t_s`, `diameter_mm` (+ `diameter_min_mm` / `diameter_max_mm`), `nd_m3mm` (else `counts`); the `fallspeed_ms` axis of a size-velocity matrix is ignored, the Atlas-Ulbrich fall speed is used |
| `stations/hail_events.json` | hail | `station`, `t_s`, `kind` = `hail_candidate`, `max_d_mm`, `n_large` (10 s samples, merged in the report) |
| `profiles/mrr_<site>.npz` | rain drop size over a Micro Rain Radar site | lowest gates 150 to 600 m: `fall_speed_ms`, `rain_rate_mmh` (the site is matched to the station id) |
| `tornadoes/tornadoes.json` | tornado roar | `id`, `rating`, `path` `[[t_s, lat, lon], ...]`, `width_m`, `path_kind`, `source` |
| `volumes/step_NNNN.npz` | convective index and radar hail class at the listener | `conv_strat` (2 = convective), `hid` (code 10 = hail, lowest 1.5 km), `mesh_mm` |
| `timeline.json` (renderer) | clip clock | `data_start`, `clip_duration_s`, `mapping` `{t_clip_s, t_data_s}` (monotone, per frame) or the per-frame `speedup` list or a number |
| `camera_path.json` (renderer) | the listener | per frame `t_clip_s`, `lat`, `lon`, `alt_m`, `heading_deg`, `pitch_deg` |
| `events.json` (renderer) | tornado existence in clip time and position, hail | `tornado` `{t_clip_start_s, t_clip_end_s, positions}`, `hail[]` (repeats the station events: not counted twice) |

Multi-Doppler winds (`winds/`) and the wind profiler are not used: wind aloft is not audible. No file
is required except `manifest.json` and the three the renderer writes; a missing one just means that
layer has no evidence and is silent. `radar/overhead_samples.json` (optional, point samples of
`convective` / `hail`) is accepted when there are no volumes.

## How to render

```
# 1. render (repository venv: numpy, scipy)
python/.venv/bin/python tools/perils_movie/audio/make_audio.py \
    --bundle ~/Downloads/xradar-exports/perils_movie/bundle \
    --timeline ~/Downloads/xradar-exports/perils_movie/timeline.json \
    --camera ~/Downloads/xradar-exports/perils_movie/camera_path.json \
    --out ~/Downloads/xradar-exports/perils_movie/audio/perils_audio.wav

# 2. look at it (waveform, spectrogram, per-layer levels, flash/delay table)
python/.venv/bin/python tools/perils_movie/audio/verify_audio.py --wav ...perils_audio.wav \
    --report ...audio_report.json --out ~/Downloads/xradar-exports/perils_movie/audio/plots

# 3. mux into the silent video as AAC +faststart (scratch venv outside the repository with PyAV;
#    the video stream is copied, the frame count and duration are unchanged)
/tmp/clipenv/bin/python tools/perils_movie/audio/make_audio.py --mux-only \
    --wav perils_audio.wav --video-in silent.mp4 --video-out perils_movie.mp4
```

`--synthetic DIR` renders a made-up storm (never the real data) for a quick test. From Python:
`render_audio(bundle_dir, timeline, camera_path, out_wav)` and `mux_audio(video_in, wav, video_out)`.

## Verification on the real bundle

`tools/perils_movie/audio/verify_audio.py` writes the plots (waveform, spectrogram with the thunder arrivals,
per-layer levels, flash / delay table); they were looked at. Real bundle (PERiLS IOP2, window 2022-03-31
01:04 to 01:20 UTC, 960 s in 120 s, the renderer's `timeline.json` with the tornado slow-down: speed-up 3.3
to 9.4, mean 8; its `camera_path.json` and `events.json`):

* 3236 LMA flashes (244 675 sources), 37 stations (4 with drop spectra, 1 with an MRR), 23 survey
  tornado tracks (one, NSED 1035605 EF2, is near the camera), 17 radar volumes, no hail event and no hail
  sensor hit in the window.
* Loudness -23.0 LUFS integrated, true peak -3.4 dBFS, no clipped sample, mono loss 1.0 dB, deterministic
  (two renders give the same SHA-1).
* Thunder (the `perils_audio_v4` render, thunder built per source, level compressed for audibility): **54 flashes
  voiced** of 3236 (13 by the guarantee, 41 by score): 20 near (< 10 km), 24 mid (10 to 30 km), 10 far (> 30 km).
  6893 channel elements (real LMA sources) rendered with 6890 ground reflections, 34 721 modelled tortuosity points
  and 11 228 modelled bridge elements. First voiced arrival 16.8 s, then never more than 5.6 s apart. Not voiced:
  285 beyond 150 km, 459 arrive after the clip, 2438 heard but dropped (2226 voices, 85 masked, 127 density). Of the
  15 windows of 8 s, 13 have a flash in range and **all 13 are voiced**; the first two (0 to 16 s, the wide shot) have
  no flash that can have arrived (physical delay). Thunder layer: rms -23.9 dBFS, peak -4.0 dBFS; the median voiced
  thunder is 12.0 dB above the rest of the mix. Median spectral centroid by class: near 88 Hz, mid 57 Hz, far 42 Hz.
* Other layers (rms in the final mix): tornado roar -38 dBFS, rain -43, wind -43, convective bed -40,
  ambient -48, hail -56 (only the radar hail class over the listener at 21 to 24 s, with an assumed diameter; no
  station hail event or sensor hit in the window). Rain is from the radar echo for most of the clip (`radar_zr`) and
  silent where nothing is near. Wind (max 8 m/s observed, 12 m/s with the observed gust) from the nearest StickNet and
  mesonet stations.
* The gust fronts the data contain are weak (rises of 5 to 6 m/s in 2 minutes, pressure rises of 0.8 to
  2.8 hPa): the swell is as small as the data say.

## Limits

* Synthesis, not a recording: it conveys the timing, distance, level and direction the data
  imply, not the exact sound of that evening. The timbres are artistic; the tornado roar is stylised.
* The listener is the camera, and the camera is a virtual viewpoint: the sound is what a listener at
  that point would hear *if* the observed rain, wind, hail and lightning were there. The nearest station may be
  kilometres away from the camera; the report says how far.
* No propagation effects other than the distance (level compressed for audibility, see above) and the air
  absorption: no refraction, no ground reflection, no terrain or building shadowing, no Doppler. A flash 100 km
  away is not audible in reality; here it is, as a low rumble, because the level law is compressed; the delay
  alone stays physical.
* LMA source power is relative and not calibrated to acoustic energy: the loudness of a flash is a
  proxy (distance, number of sources, relative power).
* Thunder keeps its real internal time (only the onset follows the clock) and is voiced for at most 15 s: a flash whose
  far parts arrive later than that is cut. The sounds are not pitch-shifted.
* A drop spectrum made from raw counts uses the Parsivel sensor area (0.0054 m2) and the sampling time
  (`from_counts`): the absolute flux is then approximate.
* The thinning of dense bursts drops real flashes (they are counted in the report).
