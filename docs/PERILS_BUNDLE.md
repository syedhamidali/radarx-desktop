# PERiLS IOP2 data bundle for the real-data storm movie

The bundle is the data side of a movie of the 30 March 2022 PERiLS IOP2 quasi-linear convective system (QLCS) and its EF2 tornado:
**2022-03-31 01:04:00 to 01:20:00 UTC (16 min = 960 s)**, 17 time steps every 60 s. It holds only measurements and
labelled retrievals (no invented data, [SCIENCE.md](SCIENCE.md)), in the layout of the SPEC v1 that the renderer and the audio agents
code against, plus additive fields. Why this window: [PERILS_DATA_INVENTORY.md](PERILS_DATA_INVENTORY.md).

Build (read only on the data folder, no network, idempotent and resumable, about 25 min on a laptop):

```
python/.venv/bin/python tools/perils_movie/build_bundle.py \
    --window 2022-03-31T01:04:00Z 2022-03-31T01:20:00Z --out ~/Downloads/xradar-exports/perils_movie/bundle
python/.venv/bin/python tools/perils_movie/verify_bundle.py --bundle ~/Downloads/xradar-exports/perils_movie/bundle --out /tmp/perils_verify
python/.venv/bin/python tools/perils_movie/to_clip_bundle.py --bundle ... --out /tmp/perils_clip_bundle   # for tools/storm_clip
```

`--stages`, `--force`, `--no-qc`, `--no-zdr-cal`, `--no-winds`, `--step-s`, `--tol-s` are in `--help`. Another window (for example the
PIPS rain peaks at 00:14 to 00:30) is only a different `--window`.

## 1. Layout

```
bundle/
  README.md                  progress board for the other agents
  manifest.json              SPEC v1 manifest (+ qc, merge method, HID legend, sources, deviations)
  volumes/step_NNNN.npz      merged 3-D radar volume + rotation / debris / hydrometeor class / 2-D diagnostics
  winds/step_NNNN.npz        multi-Doppler u, v, w, vorticity, divergence, storm-relative wind (1 km grid)
  lma/flashes.json           lightning sources grouped into flashes
  stations/stations.json     surface stations and disdrometers; stations/<id>_dsd.npz; stations/hail_events.json
  soundings/index.json       radiosondes with diagnostics; soundings/<id>.csv
  tornadoes/tornadoes.json   damage-survey tornadoes (observed) with an INTERPOLATED path
  context/mrms_NNNN.npz      MRMS merged reflectivity and 0-2 km azimuthal shear (0.01 deg)
  context/surface_NNNN.npz   Barnes analysis of the surface stations (analysis, not a model)
  context/rotation_tracks.json   vortex candidates and tracks, comparison with the survey
  context/tobac_track.json   the data owner's tobac track of the echo (independent motion)
  context/environment.json   sounding diagnostics through time + StickNet medians
  profiles/mrr_RaDAPS.npz, rwp_RaDAPS.npz   Micro Rain Radar and 915 MHz wind profiler
  science_events.json        data-derived events with numbers (captions the renderer may show)
```

Time: `t_s` is always seconds since the window start (`manifest.window.start`). Grid: azimuthal equidistant on a sphere
(R = 6 370 997 m, Py-ART's) centred at 33.575865 N, 88.548519 W; x east, y north; x and y from -90 to +90 km every 500 m (361 points);
**z is height above mean sea level**, 0 to 10 km every 250 m (41 levels). Arrays are `(z, y, x)`.

## 2. SPEC v1 conformance and deviations

Everything of SPEC v1 is there under the same names. Deviations and additions (all additive, none removes a v1 field):

1. `manifest.spec_version` = "1.1"; extra manifest keys: `tornadoes`, `rotation_tracks`, `science_events`, `hail_events`, `profiles`, `hid_legend`, `pattern_motion`, `qc`, `merge`,
   `environment_profile_for_hail`, `deviations_from_spec_v1`; steps carry `files` (source file names), `diagnostics`, and optional `winds`, `winds_summary`, `surface`.
2. `grid.z_km` is **height above sea level**. The source grids put z = 0 at each radar's own site altitude (78 m for COW1, 61 DOW7, 86 DOW8, 231 KBMX, 179 KGWX, 330 NOXP,
   74 SMART-R 2, 98 UAH): the same level index is a different height for every radar. Every radar was resampled to height above sea level (linear in z) before merging.
3. `volumes/step_NNNN.npz` also holds `vel_src`, `pol_src` (1-based index into `manifest.radars`; the radars are listed alphabetically: COW1, DOW7, DOW8, KBMX, KGWX, NOXP, SMARTR2, UAH),
   `rotation` (float16), `tds`, `hid` (uint8) and the 2-D arrays listed below. `vel`, `dbz` are float32; `zdr`, `kdp`, `rho` are **float16** (3 digits; the space they save is 15 %).
4. `winds/` is on a **1 km** grid (`x_km`, `y_km`, `z_km` are in each file), not the 500 m grid, because the retrieval is run at 1 km.
5. The steps list the radars that contributed (`radars`), their `offsets_s` (volume mid-time minus step time) and the source file names.
6. Stations: `series` keys beyond the SPEC (`dewpoint_c`, `theta_e_k`, `mixing_ratio_gkg`, `wind_gust_ms`, `log10_nw`, `lwc_gm3`, `dmax_mm`, `d0_mm`, `rain_rate_parsivel_mmh`, `hail_intensity_hits_cm2h`, lat/lon for moving
   platforms) and per-station `class`, `instrument`, `qc`, `moving`, `x_km`, `y_km`, `rain_accum_window_mm`. The series keep 10 min (600 s) of data before and after the window; moving platforms are
   flagged `moving` and their `lat` / `lon` is the median position.
7. Soundings: the index has `relation_to_window` ("pre-storm", "in-window", "post-storm"), `x_km`, `top_height_asl_m`, parcel paths for the Skew-T and the full engine diagnostics. `drift` is
   `[[t_s, lat, lon, alt_m ASL], ...]` every 15th level (15 s for the UIUC 1 Hz data).
8. `lma/flashes.json` keeps SPEC v1 (`t_start_s`, `t_end_s`, `centroid` [lat, lon, alt m], `n_sources`, `sources` [t_s, lat, lon, alt m, power dBW]) and adds `params`, `files`,
   `sources_per_minute_in_box`, `stations`, `class`, `instrument`.
9. `environment` (manifest) is `context/environment.json`: **there is no model or reanalysis time series**: WRF / MPAS output is not on disk and ERA5 covers another region (section 12).

## 3. Merged radar volume

**Time steps.** A step is every 60 s from 01:04:00 to 01:20:00 (17). Every radar contributes the volume whose **mid-time** (start plus half the volume duration, from the file attributes) is nearest to the step, if it is within
**190 s** (half of the 6.2 min WSR-88D cycle); the signed offset is stored. The merged volumes are *not* advected to the step time: they are observations at slightly different times (offsets in the table of section 11).

**Merge** (`tools/perils_movie/merge.py`):
- *reflectivity*: mean **in linear Z** of the radars with a valid value in the voxel, weights `band_weight / ROI^2` (ROI = Py-ART's radius of influence in km, at least 0.5; a smaller radius means gates closer to the voxel); X band has weight 0.5 because it is
  attenuated in heavy rain and is not corrected here (a choice, not a measurement), S and C weight 1. `nradars` = number of radars with data.
- *radial velocity*: cannot be averaged between radars looking from different directions: the voxel takes the value of the **best radar** (smallest ROI, ties to the higher band weight); `vel_src` says which. Mixed dual-Doppler analysis is the job of `winds/`.
- *polarimetric* `zdr`, `rho`, `kdp` and the hydrometeor class come **together from the best dual-pol radar** (`pol_src`); never averaged between bands.
- NaN = no data. A voxel without echo above 0 dBZ has no velocity or dual-pol value either.

**QC** (minimal, reversible: `--no-qc` switches the gates off, impossible values are still removed; the counts per criterion and radar are in `pgrid.load_grid().qc`):
physical ranges (REF -30..80 dBZ, |VEL| <= 100 m/s, ZDR -5..8 dB, KDP -3..30 deg/km, RHO 0..1.05); voxels with ROI > 3.5 km removed (poorly sampled); reflectivity with RHO < 0.6 removed unless it is at least 35 dBZ
(debris and hail keep their echo); UAH NCP < 0.1 removed; velocity or dual-pol without a reflectivity gate removed; merged reflectivity below 0 dBZ is "no echo". SMART-R 2: `DBZC` is used (its `REF` has unmasked fill values) and its `KDP`
is dropped (garbage, around -327). The velocities in the files are the owners' dealiased "Corrected mean doppler velocity"; the dealiasing was not redone (the per-radar residuals of the wind synthesis are the check, section 6).

**ZDR calibration (visible, switchable, recorded).** The radars' ZDR is not calibrated: NOXP has +3.5 to +4 dB and SMART-R 2 jumps between +0.7 and +3.9 dB from volume to volume (median ZDR of rain at 25 to 40 dBZ). A per-volume bias estimate is subtracted:
median ZDR of voxels with 25 to 40 dBZ, RHO >= 0.97, ROI < 2.5 km, 0.3 to 2.5 km above sea level, minus the typical rain value 0.55 dB; with fewer than 3000 samples the radar's previous estimate is used, and without any the ZDR of that volume is dropped (UAH: always,
98 samples). It is an estimate of +-0.3 dB at best (the real drop size distribution of the day is not the typical one); the log of every volume is in `manifest.qc.zdr_calibration.log`. `--no-zdr-cal` leaves ZDR as delivered.

## 4. Per-step derived products (RETRIEVED from radar)

All are in `volumes/step_NNNN.npz`, summarised per step in `manifest.steps[i].diagnostics`.

| Field | What and how | Limits |
| --- | --- | --- |
| `rotation` (nz, ny, nx), `rotation_ll`, `vrot_ll` | **Azimuthal shear** (s-1, cyclonic > 0): local linear least-squares plane through the radial velocity of one radar in a 2.5 km square window (NaN aware, >= 60 % of the points), projected on that radar's azimuthal direction (Smith and Elmore 2004; Mahalik et al. 2019, as the LLSD of radarx does on polar data); range 3 to 80 km from the radar, ROI <= 2.5 km; the largest value over the radars. `rotation_ll` = maximum below 2 km. `vrot_ll` = largest `(Vr(p+s) - Vr(p-s)) / 2` across an azimuthal baseline of 1, 2 or 3 km below 2 km | the 500 m grid and the radius of influence (0.5 to 3.5 km) smooth a 400 m wide vortex away; gridding seams along a radar's axes make shear ribbons (removed from the vortex list by a length / aspect test) |
| `rotation_tracks.json` | vortex candidates = compact regions (length <= 12 km, aspect <= 4) of `rotation_ll >= 0.012 s-1`, position of the maximum, peak and mean shear, area, `vrot`, radar, position uncertainty (grid cell to ROI plus offset x motion); linked step to step within 5 km | candidates, not detections of tornadoes |
| `tds` (uint8) | debris signature **candidate**: RHO < 0.8, Z >= 30 dBZ, below 2 km (flag 1); flag 2 also within 2 km of shear >= 0.012 s-1 | large drops, hail, X-band resonance and gridding also lower RHO |
| `hid` (uint8) + `manifest.hid_legend` | radarx fuzzy-logic hydrometeor classification per radar (Park et al. 2009 at S, Dolan et al. 2013 at C, Dolan and Rutledge 2009 at X) with the BMX 00 UTC temperature profile, then mapped to ONE legend (1 drizzle, 2 rain, 3 heavy rain, 4 big drops, 5 ice crystals, 6 aggregates / dry snow, 7 wet snow, 8 graupel, 9 high-density graupel / small hail, 10 hail (C) or rain-hail mixture (S), 11 vertically aligned ice) | X band has no hail class; the temperature profile is 150 km away and 110 min old |
| `shi`, `mesh_mm`, `posh_pct` | Witt et al. (1998) from radarx with the 0 and -20 degC levels of the BMX 00 UTC sounding (3659 and 6877 m ASL) | grid ends at 10 km, calibrated in Oklahoma and Florida; MESH is an upper estimate of the largest stone |
| `vil` | Greene and Clark (1972), radarx, capped at 56 dBZ | no layer below the lowest beam; truncated at 10 km |
| `echotop18_km`, `30`, `40` | Lakshmanan et al. (2013), radarx | heights above sea level; `top_truncated` marks columns that reach the 10 km top |
| `zdr_column_km` | Snyder et al. (2015), radarx: depth above the freezing level with ZDR >= 1 dB, RHO >= 0.8, Z >= 30 dBZ | depends on the ZDR bias correction |
| `conv_strat` (0 none, 1 stratiform, 2 convective) | Steiner, Houze and Yuter (1995) at 2 km: convective if >= 40 dBZ or more than `10 - bg^2/180` dB above the mean of the echo within 11 km, then a radius of 1, 2, 3, 4 or 5 km (background < 25, 25-30, 30-35, 35-40, >= 40 dBZ) | the radius table and the peakedness function are the author's recollection of the paper, not checked against it |
| `bwer` | bounded weak echo region candidate: column minimum 1 to 4 km at least 20 dB below the 4.5 to 8 km maximum (>= 35 dBZ) and ringed (3 to 6 km) by echo at least 15 dB stronger | thresholds are the author's; a candidate list |
| `cmax` | column-maximum reflectivity | |

## 5. Hail, rain and size sorting

- **Radar hail evidence**: `hid` classes 9 and 10, `mesh_mm`, `posh_pct`, `shi`, `zdr_column_km`, `tds`; the strongest MESH and the deepest ZDR column per window are in `science_events.json` (kind `hail_signature`, with the caveats).
- **Hail reports**: there are no SPC / NWS / PERiLS hail reports in the folder (the report notebook fetches them from the network), so none are in the bundle.
- **Disdrometer hail evidence** (`stations/hail_events.json`): per 10 s PIPS sample the number of particles of 8 mm or more (raindrops this large break up) from the owner's QC'd size-velocity matrix; a sample with at least 2 is a `hail_candidate` with the largest
  diameter, their mean fall speed and the raindrop fall speed (Atlas et al. 1973) of that size. The RaDAPS WXT hail sensor (`hail_intensity_hits_cm2h`) is in its series. In this window the PIPS saw no particle of 8 mm or more (largest 4.2 to 5.5 mm).
- **Rain** (`stations/<id>_dsd.npz`, series): for each PIPS the N(D) of the owner's QC (m-3 mm-1) on the 32 Parsivel diameter bins, the size-velocity counts, Dm, D0, log10 Nw, LWC, Dmax, rain rate (owner's derived and Parsivel's own), reflectivity;
  accumulation over the window. Dm and Nw follow the standard moments (Testud et al. 2001; `stations.dsd_moments`, tested on an exponential distribution whose answer is known).
- **Size sorting**: the quantities that show it are all in the bundle: low-level `zdr`, `kdp`, `rho`, `dbz` on the grid, the PIPS Dm and Nw against rain rate (in `science_events.json` kind `rain_dsd`: the correlation of Dm with rain rate and the change from the first to the last third of the window).
  radarx `zdr_arc` is a **supercell** algorithm (forward-flank downdraft direction) and was not applied to this QLCS; `rain_trajectories` / `size_sorting` need an analysed wind field of the right quality at the surface and were not run. The window is the trailing stratiform rain at the PIPS
  (the leading-edge sorting happened at 00:15 to 00:40), so what the PIPS show here is the trailing-stratiform DSD.

## 6. Multi-Doppler winds (RETRIEVED)

`winds/step_NNNN.npz` (`tools/perils_movie/winds.py`): per step the dealiased radial velocities of the radars of that step are moved to the step time by the pattern motion times each radar's time offset (translation, linear interpolation), averaged horizontally
(3 x 3 NaN-aware) and sampled every 1 km, and given to `radarx.retrieve.multi_doppler` (variational analysis with the cost structure of Gao et al. 1999: observations of every radar, anelastic mass continuity, smoothness; fall speed removed from w with the radarx relation and the 3659 m freezing level; `w = 0` at the bottom
and top; observation error 2 m/s; no background wind; three grid levels, 300 iterations each). Fields on `(41, 181, 181)`: `u`, `v`, `w` (m/s), `vorticity`, `divergence` (s-1, centred differences at 1 km), `u_sr`, `v_sr` (relative to the pattern motion), `uv_error` (m/s,
the standard deviation estimated by the analytic dual-Doppler solution `dual_doppler_analytic` on the same input), `crossing_deg` (largest horizontal angle between two beams), `nradars`. Cells with fewer than two radars or a crossing angle below 30 degrees, or a speed above 70 m/s, are NaN.

Limits: the solver stops at its iteration limit (`converged` = 0 in every step: it did not converge fully); `w` is the least reliable component (boundary conditions, 1 km smoothing, gridding artefacts: Collis et al. 2010; the maps show arc-shaped banding) and is not
an estimate of the strongest updraft; the lowest 500 m and the top levels are weakly constrained; no attenuation correction of X-band velocity; the storm motion used to move the data is that of the echo pattern.

## 7. Lightning (OBSERVED)

`lma/flashes.json`: the 10 min `LYLOUT_*_0600.dat.gz` source files (NALMA, 14 stations in north Alabama, plus the NSSL deployable LMA, 8 sites in Mississippi, v1.1) read with `radarx.io.read_lma` (chi2 <= 1, at least 6 stations, 0 to 20 km altitude; Fuchs et al. 2016)
and the sources inside 32.2 to 35.0 N, 87 to 90 W grouped into flashes with `radarx.retrieve.cluster_flashes` (single linkage in space-time, distance 6 km (the value Fuchs et al. use for the Alabama network), time 150 ms, at least 2 points, flashes cut at 3 s). Flashes with at least 2 sources are kept. Parameters,
station counts and the files are in the JSON. LMA detects VHF breakdown (in-cloud channels), not ground strike points, with a detection efficiency that falls with range from the stations.

## 8. Stations and soundings (OBSERVED)

Stations (`stations/stations.json`): 4 PIPS, 24 StickNet, the RaDAPS WXT station (rain, hail sensor, T, RH, p, wind), 5 vehicle mesonets (DOW7, DOW8, SCOUT1 to 3) and 3 FARM Parsivels; each with `credit`, `qc`,
dew point and theta-e derived with Bolton (1980) (`thermo.py`). **Gust fronts** (`science_events.json`, kind `gust_front_passage`): the largest 2-minute temperature fall (>= 1 K) at a fixed station, accepted if the pressure rose >= 0.4 hPa or the wind peak was >= 3 m/s above its pre-front median; the numbers
(temperature fall, pressure rise, wind jump, theta-e change, shift) are the raw differences. The outflow speed is a fit to the passage times at 3 or more stations when there are that many in the data.

Soundings (`soundings/index.json`): all launches from 4 h before to 1.5 h after the window: UIUC FARM (5 sites x 7, GRAW, 1 Hz with positions), UAH and ULM (to 22:55) and the operational 00 UTC ones: 42 profiles, each as a common CSV (height ASL, p, T, Td, wind, lat, lon, seconds since the window start) with
**the engine's own diagnostics** (`engine/sounding.py` -> radarx parcel functions): CAPE and CIN of the surface-based, mixed-layer and most-unstable parcels, LCL, LFC, EL, lifted index, 0 to 1 / 3 / 6 km shear, Bunkers motion, storm-relative helicity, 0, -10, -20 degC heights, precipitable water, plus the parcel paths for
a Skew-T (dry adiabat to the LCL then a liquid-only pseudo-adiabat, drawing aids that differ from the CAPE kernel). Most sondes end at 4 to 7 km: then the 0 to 6 km shear, the Bunkers motion and the parcel above are not computed, the CAPE is cut off (a lower bound; the engine notes say so).
The LZK 00 UTC file has no dew point and is listed without diagnostics.

## 9. Tornado (survey: OBSERVED; radar evidence: RETRIEVED)

`tornadoes/tornadoes.json`: the NWS damage-survey events (NSED) of the day in 30.5 to 35.5 N, 91 to 86 W within one hour of the window: id, EF rating, start and end (UTC), begin and end point, **path = a straight line between the surveyed begin and end points at constant speed (INTERPOLATED)**,
width from the SPC table (400 yd = 366 m), `max_wind_mph` = the upper bound of the EF-scale range for the rating (an estimate from damage, not a measurement; `wind_note`), the first 300 characters of the survey narrative, source and credit. The tornado of the window is **EF2, 01:08 to 01:20 UTC, 33.1008 N 88.4691 W to 33.2105 N 88.3316 W**.
Radar evidence and the offset between the radar vortex candidates and the surveyed track (km and s): `context/rotation_tracks.json`, `survey_comparison`, and section 11.

## 10. Context

- **MRMS** (`context/mrms_NNNN.npz`): GRIB2 read with `wgrib2` (`/opt/local/bin/wgrib2`, found on the PATH; no GRIB reader is in the Python environment, nothing was installed): `mergedref_dbz` (MergedBaseReflectivityQC, 500 m above MSL) and `azshear_s-1` (0 to 2 km layer maximum azimuthal shear) cropped to 32.6 to 34.5 N, 89.7 to 87.4 W on their 0.01 degree grid, `lat`, `lon`; the
  nearest file within 150 s. There is no MRMS MESH in the folder.
- **Surface analysis** (`context/surface_NNNN.npz`, class *analysis*): Barnes single-pass analysis (sigma 15 km, only where at least 2 stations are within 30 km) of T, Td, theta-e, p, mixing ratio and the wind of the fixed stations at the step time, on a 2 km grid. An interpolation of observations, not a model.
- **tobac** (`context/tobac_track.json`): the data owner's tracked echo (KGWX, 15 dBZ): centroid, area, maximum reflectivity; an independent motion estimate.
- **Profiles** (`profiles/`): the RaDAPS Micro Rain Radar (`t_s`, `height_m` above the radar, `dbz`, `fall_speed_ms`, `rain_rate_mmh`, `lwc_gm3`, `pia_db`) and the 915 MHz profiler (`u`, `v`, `w`, `snr_db`, `qcTag`) at 33.5956 N, 88.9879 W, 41 km west of the grid origin, 5 to 25 min around the window (in trailing stratiform rain).

## 11. Verification (numbers and figures)

(filled in below from `verify_bundle.py`)

## 12. What could not be used, and why

| Source | Why not |
| --- | --- |
| ERA5 (`Reanalyses/ERA5`) | covers 25 to 65 N and 80 to 64 W: the north-east; the storm is at 88.5 W |
| WRF (6 microphysics members) and MPAS | only logs, scripts, figures and summary tables are on disk; the model output stayed on the cluster; the owner's evaluation says the simulated line is displaced by -18 to +87 km |
| `wind_synthesis_2127UTC.nc` | 21:27 UTC, 3.6 h before the window |
| Lidar | no lidar data files (only a notebook) |
| ASOS / AWOS, hail reports | not in the folder |
| GOES | one file of December 2024 (`MRMSTEST/GOES16`); nothing for 30 March |
| MRMS MESH | not in the folder (the radar MESH of the volume is used) |
| UAH MAPNet Parsivel `.dat` | Parsivel telegrams not parsed (the WXT rain sensor and the PIPS cover the rain at and near the site) |
| PSL Parsivel (Courtland AL) | outside the grid (116 km east) |
| Photos / video | personal field photographs and animations; not copied, not opened |
| Gridded LMA (`NSSL_LMA/Gridded`) | the raw sources are used; the gridded product can cross-check the flash counts |

## 13. Honest limits

- The tornado vortex (400 m wide) is not resolved by 500 m gridded data with a radius of influence of up to 3.5 km; what the bundle shows is low-level shear and a debris-like flag at the grid's scale.
- The merged volume is a mosaic of observations up to 190 s apart; the radars are not calibrated against each other (X-band attenuation is not corrected, ZDR is bias-corrected by an estimate).
- The temperature profile for the hail products and the ZDR column is the 00 UTC BMX sounding (balloon about 23:15 UTC, 150 km east), not the storm environment at the time; most in-window sondes end at 4 to 6 km.
- The retrieved winds did not converge fully and `w` carries gridding artefacts; the vorticity is at 1 km.
- The size-sorting leading edge (00:15 to 00:40) is outside the window; the window has the trailing rain.
- Window time offsets, the line motion and the vortex positions are estimates with the uncertainties stated where they are given.

## 14. Reading the files (numpy)

```python
import numpy as np, json
b = "bundle/"
man = json.load(open(b + "manifest.json"))
v = np.load(b + man["steps"][8]["volume"])          # dbz (41, 361, 361) float32 NaN = no data; vel, zdr, kdp, rho; nradars uint8
v["dbz"][6]                                          # 1.5 km above sea level
v["hid"], man["hid_legend"]["codes"]                 # hydrometeor class and its legend
w = np.load(b + man["steps"][8]["winds"])            # u, v, w, vorticity ... on (41, 181, 181); w["x_km"], w["y_km"], w["z_km"]
flashes = json.load(open(b + "lma/flashes.json"))    # sources [t_s, lat, lon, alt_m, power_dBW]
```
