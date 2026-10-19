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
| Thunder timing | every LMA VHF source is a point emitter; it is heard at `t_source + r / 343 m/s` (data time), mapped to clip time by the timeline: `delay_clip = r / 343 / speedup` for a constant speedup | none |
| Thunder level | `1/r` spreading from the camera to every source (minimum range 300 m, level floor -42 dB re 1 km), source power (relative, within the flash), source count | the energy proxy (amplitude ~ N^0.25), the tail length |
| Thunder colour | ISO 9613-1 air absorption (station temperature and pressure, assumed 80 % humidity): far thunder loses its top | the noise carrier, the crack |
| Direction | azimuth of the sources relative to the camera heading: interaural level and time difference (Woodworth, <= 0.66 ms) and a head-shadow low-pass; no HRTF data | the diffuse spread of the rumble |
| Rain | rain rate (station, on a log curve), drop-size spectrum (number flux per size) when `<id>_dsd.npz` exists, Dm, MRR lowest gate when the listener is over the site, distance weighting | the impact kernels, the curve constants |
| Wind | observed speed, gust, direction of the station nearest the listener; the gust front and pressure jump come out of the data | fine gust structure (amplitude from the observed variability) |
| Hail | station hail events (time, max diameter, counts), the tail of the drop spectrum, events.json, radar hail class: never without evidence | the ring of the impact |
| Tornado roar | existence in time and position from the tornado track / events; level and muffling from the distance to the camera | everything about the timbre: **STYLISED** |
| Convective bed | convective index at the listener (radar volume samples, else the station reflectivity) | the rumble |
| Ambient | none | a very quiet synthetic noise bed (no insects, no room tone, no music) |

## Time-lapse

The clip is a time-lapse (`speedup` data seconds per clip second, from `timeline.json`; constant or
keyframed). Events are moved in time by the clock; the sounds themselves are **not** pitch-shifted
or time-compressed. Sound travels at the real speed in data time, so the delay in the clip is
`distance / 343 / speedup`: at 8x a flash 10 km away is heard 3.6 s after it, not 29 s. A 10 km
long flash is heard from its nearest part and rolls on as the farther parts arrive, compressed by the same
factor (the "rolling" is shorter than in real time; the tail of each part is in clip seconds). The
camera position at the time of the flash is used for the distances; the heading at the arrival of
each direction sector is used for the panning. Sound speed is the constant 343 m/s (it varies by a few
percent with temperature).

## Layers and priority

Priority (a layer is ducked by the louder layers above it, up to 10 / 6 / 4 / 3 dB): **thunder >
tornado > hail > rain > wind**, then the convective rumble and the ambient bed. Each layer has its own level in
`audio_report.json` (`layers.<name>.rms_dbfs`, `lufs`, `peak_dbfs`, 1 s level trace, maximum ducking).

* **Thunder**, one per real LMA flash. Dense bursts are thinned, loudest first, so the mix stays
  readable: at most 3 flashes whose first arrival is within 1.5 s, a flash more than 30 dB below a louder one
  within 3 s is masked, at most 4 thunders at once and thunder sounding for at most 60 % of the clip.
  Every skipped flash is counted with its reason (`skipped_density`, `skipped_masked`,
  `skipped_voices`, `skipped_coverage`, `skipped_out_of_range`, `outside_clip`). A real QLCS
  has far more flashes than a 2 minute clip can show (about a thousand in 16 minutes), so most are skipped: the
  thunder you hear is the loudest (nearest, most energetic) of them. The flash table (distance nearest / farthest / centroid, azimuth, delay, first and last
  arrival, level, status) is in the report.
* **Rain**. A Poisson stream of drop impacts per size class. With a drop spectrum the rate of each
  class is proportional to its number flux (`N(D) v(D) dD`), the amplitude to the mass^(2/3) of the
  drop (`D^2`), larger drops ring longer and lower with a hard click: heavy rain with big drops is
  fat and low, drizzle is a fine hiss (the hiss share comes from the flux of drops under 1 mm).
  Without a spectrum the classes come from an exponential spectrum with the median volume diameter
  (station Dm, the MRR lowest gate within 3 km, else an assumed `0.9 R^0.21`, reported as
  `assumed_dm_fraction`). Total level follows the rain rate on a perceptual curve (-58 dB at 0,
  +14 dB per decade of `1 + R`) plus +3 dB per doubling of Dm; rain you are not under (nearest station
  far away) fades to a quiet hiss (`1/(1+(d/5 km)^2)`, floor -22 dB). The nearest station dominates
  with a smooth crossfade.
* **Wind**. Level ~ speed^1.5 (30 dB per decade), brighter with speed, from the station nearest the
  listener; a high-rate station (sampling <= 7 s) is used as observed (the synthetic gust part drops to a quarter),
  the gust column sets the top of the excursions, the direction (relative to the heading)
  tilts the stereo balance. A **gust front** is whatever the series contains: the sharp rise is
  heard as a swell. Only where a station's own pressure series rises by more than 0.8 hPa in 2
  minutes is a **pressure-wave rumble** (31 to 125 Hz) added; calibration offsets between stations
  are never a jump. Wind aloft (multi-Doppler winds) is not audible and not used.
* **Hail**. Hard impacts (ping with a short ring and a thud, pitch down with size) on top of the
  rain, only where hail evidence is within 5 km of the listener. Rate `6 F^0.6` per second from
  the large-particle flux `F` (event counts / (0.0054 m2 x duration), or the tail of the drop spectrum);
  amplitude from the maximum diameter (mass^(2/3)). Missing fields are assumed and listed per event in
  `assumed`.
* **Tornado**. Roar (40 Hz to 2 kHz, a slow throb) and debris rattle, only while the survey track (and
  the renderer's tornado event interval, when it lists one) says the tornado exists; level
  `1/r` with a 300 m minimum range, zero beyond 25 km, muffled when far (3.5 kHz close, 120 Hz far).
  Position from the survey; the sound is stylised and labelled so. No siren.

## Mix

BS.1770 loudness (K-weighting, gated): **-23 LUFS integrated**, look-ahead limiter, **true peak below
-1 dBFS** (4x oversampled), 2 s fade in and 2.5 s out, no clipping, deterministic (seeded). Mono
compatibility is reported (the loss of the mono sum against the stereo RMS, the L/R correlation).
Output 48 kHz stereo WAV (24 or 16 bit with seeded TPDF dither).

## audio_report.json

`label`, `params` (speedup, seed, thunder parameters), `master` (LUFS, true peak, limiter, ducking,
mono check), `layers` (per layer level and the data that drove it), `thunder` (counts and the flash table),
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

## Limits

* Synthesis, not a recording: it conveys the timing, distance, level and direction the data
  imply, not the exact sound of that evening. The timbres are artistic; the tornado roar is stylised.
* The listener is the camera, and the camera is a virtual viewpoint: the sound is what a listener at
  that point would hear *if* the observed rain, wind, hail and lightning were there. The nearest station may be
  kilometres away from the camera; the report says how far.
* No propagation effects other than 1/r and air absorption: no refraction, no ground reflection, no
  terrain or building shadowing, no Doppler. Thunder from a distant flash is not strengthened by the ducted
  propagation that sometimes carries it.
* LMA source power is relative and not calibrated to acoustic energy: the loudness of a flash is a
  proxy (distance, number of sources, relative power).
* A time-lapse compresses the rolling of long thunder; the sounds are not pitch-shifted.
* A drop spectrum made from raw counts uses the Parsivel sensor area (0.0054 m2) and the sampling time
  (`from_counts`): the absolute flux is then approximate.
* The thinning of dense bursts drops real flashes (they are counted in the report).
