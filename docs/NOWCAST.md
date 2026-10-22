# Nowcast: what it is, how good it is, and what it is not

A **nowcast** here is an extrapolation: the motion of the radar echo pattern is estimated from the last
few gridded frames, and the latest frame is moved along that motion to the next 5 to 120 minutes. An
ensemble of perturbed motions turns the uncertainty of the motion into probabilities. Nothing grows, decays
or forms in it: that is the method's first limit, not a detail. Every output is labelled **FORECAST** and
carries the words *experimental guidance, not an official forecast* ({doc}`SCIENCE`).

```{admonition} How good it is was measured, not assumed
:class: warning
The skill numbers on this page come from a verification of this method on public NEXRAD cases that
were never used to tune it, always next to **Eulerian persistence** (the latest frame held still), with
confidence intervals from a bootstrap over events. Where the nowcast does not beat persistence, the page
says so.
```

## Quick start

```bash
# the next hour, every 5 minutes, from the volumes of one radar (composite reflectivity, 1 km grid)
rxd nowcast -i 'storm/KTLX/*_V06' --lead 60 --step 5 -o nowcast/

# 20 members with perturbed motion: probabilities of >= 20 / 35 / 45 dBZ and 0.5 / 2 / 10 mm/h
rxd nowcast -i 'storm/KTLX/*_V06' --ensemble 20 -o nowcast.nc -p nowcast.png

# from gridded frames the engine wrote earlier (any CF grid with x, y in metres and a time axis)
rxd nowcast -i frames.nc --lead 90 --origin 2021-12-11T02:00 -o out/

# how good would it have been on this sequence? every origin, scored against the later frames
rxd nowcast -i 'storm/KTLX/*_V06' --verify --bootstrap 0 -o scores.json
```

A folder output gets `nowcast_<origin>.nc`, `.json` (the run record) and `.png` (the overview figure
below). Several radars scanned within `--group-s` seconds (150) of each other form one frame (their
mosaic); the grid is fixed from the first frame.

```{image} images/nowcast/example_kpah.png
:alt: Observed composite reflectivity of KPAH at 02:02 UTC on 11 December 2021 with motion vectors, the nowcast at 15, 30 and 60 minutes, the ensemble spread at 60 minutes and the probability of 35 dBZ at the three leads
:width: 100%
```

The overview figure (KPAH, 11 December 2021, 02:02 UTC; a tuning case of the verification below, so
its skill is not claimed from this picture): the latest observed frame with the motion field, the
deterministic nowcast at three leads, the ensemble spread, and the probability of exceeding a
threshold. Every forecast panel says `FORECAST`; the light grey outside the circle is "not observed
or not forecastable", never "no rain".

## How it works

Notation: a frame is a grid of composite reflectivity *Z* (dBZ) or rain rate *R* (mm h⁻¹) with
square cells of *d* metres; NaN is a cell no radar observed (the coverage mask tells it from a dry cell).

### 1. Motion

**Dense Lucas–Kanade optical flow** (Lucas and Kanade 1981), coarse to fine on a 3-level image
pyramid with iterative warping (Bouguet 2001): for every cell, the translation that best explains the
change between two frames is the least-squares solution of *I*<sub>x</sub> *u* + *I*<sub>y</sub> *v* = −*I*<sub>t</sub>
over a 9 × 9 window. The tracking image is the frame with no-echo cells set to a floor (10 dBZ) and a
1-cell Gaussian smoothing, so that gradients at echo edges are well posed; the data that is extrapolated
is never smoothed. Cells whose window has too little texture (smallest eigenvalue of the structure tensor
below 0.05) or no echo get no local solution; the field is completed by **normalised convolution** (a
Gaussian-weighted mean of the reliable vectors, Knutsson and Westin 1993), falling back to the
domain-wide vector where nothing reliable is near. Gross outliers (farther than three median deviations
from the median vector) are dropped before the completion.

**Global translation**: the peak of the FFT cross-correlation of the two frames (Rinehart and Garvey
1978), refined to sub-cell precision with a parabolic fit. It is the fallback when the dense estimate is
reliable in too few cells, the baseline "one storm motion" of the verification (`--motion global`), and a
quality gate: two frames whose patterns do not correlate at any shift (peak correlation below 0.2:
noise, or a field that changed completely) carry no motion information, and the motion is then zero and
says so.

The last three frames (`--n-frames`) give two pair estimates that are averaged in m/s, so an irregular
scan cadence (4.5 then 6 minutes) is handled by each pair's own time step. Vectors faster than 60 m/s are
clipped and counted in the notes. The motion of the *pattern* is a retrieval; it is neither the wind nor
the motion of individual cells (Germann and Zawadzki 2002).

### 2. Extrapolation

Backward **semi-Lagrangian** advection (Germann and Zawadzki 2002): the value at a cell and lead *t* is
the latest frame read at the point the air came from, *F*(**x**, *t*) = *Z*₀(**x** − **D**(**x**, *t*)).
The displacement **D** follows the frozen motion field step by step with the mid-point fixed-point
iteration of Staniforth and Côté (1991), three iterations per 5-minute step; the original frame is read
once per lead with bilinear interpolation, so interpolation errors do not compound. Reflectivity is
interpolated in dBZ and converted to rain rate afterwards with a named Z–R relation of the engine
(`--relation`, default the WSR-88D *Z* = 300 *R*<sup>1.4</sup>; see {doc}`HYDROLOGY`); below 5 dBZ the rate
is 0.

A cell whose departure point lies outside the observed area (or outside the radar coverage) is **not
forecastable** and stays NaN: no echo is invented upstream of the domain. Growth and decay are a
named interface with one documented default, **Lagrangian persistence** (the intensity is kept); an
optional exponential decay (`--decay exponential:<seconds>`) exists for experiments and is off by default.

### 3. Uncertainty: the ensemble

The dominant error of a short extrapolation is *where* the echo ends up: the error of the motion
(Bowler et al. 2006 built the STEPS velocity perturbations on this). Each member perturbs the motion field
by one vector, drawn in the frame of the mean motion (a component along it and one across it) with a
standard deviation that is the **verified motion error of this method**: the difference between the
motion used at the forecast time and the motion the echo really had over the following lead, measured on
the *tuning* events of the verification below (never on the events that are scored) and shipped as a
table per lead time. One draw per member is scaled with the lead-dependent standard deviation, so a member
is a consistent trajectory; the seed is in the record. Member 0 is the unperturbed motion.

From the members: the **ensemble mean rain rate** (a mean of rates, never of dBZ), the **probability of
exceeding** 20 / 35 / 45 dBZ and 0.5 / 2 / 10 mm h⁻¹ (the member fraction, then a Gaussian neighbourhood
whose width is the position uncertainty *lead × σ*<sub>motion</sub> implied by the same table: a
neighbourhood probability, Theis et al. 2005), the **spread** (standard deviation of the rate) and the
**position-uncertainty radius** per lead. Without an ensemble the probabilities are the neighbourhood
probability of the single member, and the record says so.

What the ensemble does not contain: growth and decay, initiation, local (cell-by-cell) motion errors,
radar errors. The spread/skill ratio in the verification shows the under-dispersion that follows; it is
reported, not hidden.

## How good it is

The method was scored on **eight public NEXRAD Level II cases** (NOAA Big Data Program, three hours
each) chosen for their storm modes and seasons *before* anything was run, and never used to tune it.
Every origin every 10 minutes was extrapolated 5 to 90 minutes ahead (composite reflectivity, 1 km
cells, 200 km range, 8 members) and scored against the frame observed nearest the valid time (within
half a lead step) next to **Eulerian persistence** (the origin frame held still) and a **one-vector
nowcast** (the global translation alone). Scores: POD / FAR / CSI at 20, 35 and 45 dBZ, the fractions
skill score FSS (Roberts and Lean 2008) at 1 to 33 km, MAE of the rain rate (WSR-88D *Z–R*), Brier
score, reliability and ROC of the ensemble probabilities, CRPS (Hersbach 2000) and the spread. Intervals
are a 95 % **paired block bootstrap over events** (1000 resamples; the events are the independent units,
the frames within an event are not; Wilks 2011). Two further cases (T1 KPAH, 11 December 2021, Mayfield
outbreak; T2 KFWS, 21 October 2019, Dallas tornado) served only to measure the motion error that the
ensemble perturbs with; they are not scored.

### What the numbers say

* **Where it rains at all (≥ 20 dBZ)** the nowcast beats persistence by +0.04 CSI at 5 to 20 min
  (the interval excludes zero), the gain is gone at 30 to 45 min, and **beyond 60 min persistence is
  better** (−0.06 at 90 min; the interval includes zero). Both scores are high (0.85 at 5 min, 0.6 at
  90 min) because the rain areas are large.
* **Where the cores are (≥ 35 and ≥ 45 dBZ)** the gain is larger and lasts longer: +0.12 and +0.17
  CSI at 5 to 10 min, +0.09 and +0.10 at 30 min (intervals above or touching zero), nothing at 60 min
  and slightly below persistence at 90 min. The ≥ 45 dBZ CSI itself falls from 0.60 to 0.09 in an hour:
  cores move with the pattern but do not keep their intensity.
* **Rain rate**: MAE 2.5 mm h⁻¹ against 3.9 for persistence at 5 min, 5.0 against 6.7 at 30 min
  (intervals exclude zero), equal from 60 min.
* **Dense optical flow beats the single translation** at every lead and threshold: the cases have
  differential motion (a hurricane, a derecho with a bowing line, supercells that deviate from the mean).
* **The ensemble adds information and is under-dispersive, as predicted.** The CRPS of the 8 members
  is below the MAE of the unperturbed member at every lead (3.6 against 5.0 mm h⁻¹ at 30 min), the ROC
  area for ≥ 20 dBZ is 0.94 at 15 min, 0.86 at 30 min and 0.82 at 60 min, and the Brier skill score
  against the sample climatology falls from 0.68 (5 min) to 0.34 (30 min), 0.12 (60 min) and zero at
  90 min. The reliability curve is close to the diagonal between 0.15 and 0.85 and over-confident at
  both ends (forecast 0.02, observed 0.19; forecast 0.98, observed 0.93). The spread (1.1 to 4.9
  mm h⁻¹) stays below the CRPS at every lead: the members carry the uncertainty of the mean motion
  only, not growth, decay or local motion errors.
* **Storm mode decides.** The supercell cases (V1 Moore, V7 Dodge City) and the derecho (V2) keep a
  nowcast advantage at every lead to 90 min. The tropical rain bands of Harvey (V3) and Ian (V4) are
  matched by persistence at 15 to 30 min and beaten by it afterwards: bands that rotate about the centre
  and regenerate in place are carried away by a frozen translation. The quasi-stationary lake-effect
  band (V5) and the stratiform atmospheric-river rain (V6) lose to persistence after 20 to 25 min. The
  quiet case (V8, 0.0 % of the area ≥ 20 dBZ) is a warning rather than a score: what little echo
  there is (ground clutter, clear-air returns) does not move, the nowcast moves it, and persistence
  wins at every lead. **A near-empty frame must not be extrapolated.** The engine now follows this: when fewer
  than 1 % of the covered cells in the latest frame reach 10 dBZ (the quiet case had about 0.3 %; every storm case
  had at least 5 %), the motion is set to zero, so the result is persistence, and the run record says so. The 1 %
  limit was chosen after seeing the quiet case, so that case is not independent evidence for it, and the verification
  scores above were not re-run with it.

In one sentence: *use the nowcast for the next 0 to 45 minutes of moving convection; expect no gain
over the last frame for stationary or rotating rain patterns, and none beyond an hour anywhere.*

```{image} images/nowcast/skill_vs_lead.png
:alt: CSI at 20, 35 and 45 dBZ and the MAE of the rain rate against lead time, pooled over eight events, for the dense optical-flow nowcast, the one-vector nowcast and Eulerian persistence
:width: 100%
```

```{image} images/nowcast/skill_vs_persistence.png
:alt: The nowcast's CSI minus persistence's and its MAE minus persistence's against lead time with 95 percent bootstrap bands
:width: 100%
```

```{image} images/nowcast/skill_by_event.png
:alt: CSI at about 30 minutes for every event and model, showing the storm-mode dependence
:width: 100%
```

```{image} images/nowcast/probabilistic.png
:alt: Reliability diagram and ROC curves of the probability of 20 dBZ and the CRPS, MAE and spread of the ensemble against lead time
:width: 100%
```

The MAE curves are jagged because the leads are 5 minutes apart and the scan cadences are 4.3 to 7.1
minutes: at some leads the observation is 2 minutes from the valid time, at others nothing is scored
(the dashes in the tables).

### The cases

<!-- generated by the verification run; do not edit by hand -->

| id | radar | window (UTC) | season; storm mode | frames | cadence | echo ≥ 20 dBZ (mean fraction of the covered area) |
|---|---|---|---|---|---|---|
| V1 | KTLX | 2013-05-20 19:05 to 22:00 | spring (May); supercell (Moore tornado) | 42 | 4.3 min | 15.4 % |
| V2 | KDMX | 2020-08-10 15:37 to 18:30 | summer (Aug); squall line / derecho (Iowa) | 27 | 6.3 min | 44.2 % |
| V3 | KHGX | 2017-08-27 06:07 to 08:59 | summer (Aug); tropical rain bands (Harvey, Houston) | 33 | 5.4 min | 59.8 % |
| V4 | KTBW | 2022-09-28 14:06 to 16:58 | autumn (Sep); hurricane eyewall and bands (Ian): rotating motion | 27 | 6.7 min | 69.9 % |
| V5 | KBUF | 2022-11-18 12:07 to 14:59 | late autumn (Nov); lake-effect snow band: quasi-stationary | 28 | 6.3 min | 4.7 % |
| V6 | KDAX | 2017-02-07 12:03 to 14:58 | winter (Feb); atmospheric river, stratiform rain (California) | 30 | 6.0 min | 36.3 % |
| V7 | KDDC | 2016-05-24 22:03 to 00:58 | spring (May); supercells (Dodge City) | 27 | 6.8 min | 18.8 % |
| V8 | KFTG | 2021-01-08 18:05 to 21:02 | winter (Jan); quiet / clear-air (Denver) | 26 | 7.1 min | 0.0 % |

One KDMX volume of V2 (16:01 UTC) could not be read by xradar (mixed 360- and 720-ray sweeps) and was
left out; the run record notes it.

### Scores

**Reflectivity ≥ 20 dBZ, pooled over the verification events** (CSI; FSS at 9 km; the nowcast's CSI
minus persistence's with its 95 % block-bootstrap interval over events):

| lead | CSI nowcast | CSI one-vector | CSI persistence | FSS 9 km nowcast | FSS 9 km persistence | CSI nowcast − persistence [95 % interval] |
|---|---|---|---|---|---|---|
| +5 min | 0.85 | 0.84 | 0.81 | 0.99 | 0.97 | +0.04 [+0.04, +0.06] |
| +10 min | 0.79 | 0.78 | 0.76 | 0.96 | 0.95 | +0.03 [+0.02, +0.06] |
| +15 min | 0.82 | 0.79 | 0.78 | 0.97 | 0.95 | +0.04 [+0.02, +0.08] |
| +20 min | 0.76 | 0.73 | 0.73 | 0.94 | 0.92 | +0.03 [+0.01, +0.06] |
| +30 min | 0.68 | 0.65 | 0.67 | 0.90 | 0.88 | +0.02 [−0.02, +0.06] |
| +45 min | 0.69 | 0.65 | 0.70 | 0.89 | 0.89 | −0.00 [−0.04, +0.05] |
| +60 min | 0.63 | 0.58 | 0.65 | 0.85 | 0.86 | −0.03 [−0.06, +0.04] |
| +90 min | 0.58 | 0.54 | 0.64 | 0.80 | 0.84 | −0.06 [−0.12, +0.04] |

**Reflectivity ≥ 35 dBZ**, pooled:

| lead | CSI nowcast | CSI one-vector | CSI persistence | FSS 9 km nowcast | FSS 9 km persistence | CSI nowcast − persistence [95 % interval] |
|---|---|---|---|---|---|---|
| +5 min | 0.63 | 0.60 | 0.51 | 0.95 | 0.90 | +0.12 [+0.10, +0.14] |
| +10 min | 0.55 | 0.50 | 0.43 | 0.90 | 0.81 | +0.12 [+0.11, +0.15] |
| +15 min | 0.52 | 0.47 | 0.41 | 0.87 | 0.78 | +0.11 [+0.08, +0.17] |
| +20 min | 0.45 | 0.40 | 0.35 | 0.81 | 0.70 | +0.10 [+0.05, +0.18] |
| +30 min | 0.37 | 0.33 | 0.29 | 0.72 | 0.60 | +0.09 [+0.02, +0.18] |
| +45 min | 0.32 | 0.27 | 0.28 | 0.64 | 0.59 | +0.04 [−0.01, +0.14] |
| +60 min | 0.26 | 0.22 | 0.26 | 0.55 | 0.56 | −0.00 [−0.03, +0.07] |
| +90 min | 0.18 | 0.17 | 0.22 | 0.43 | 0.50 | −0.04 [−0.06, +0.04] |

**Reflectivity ≥ 45 dBZ**, pooled:

| lead | CSI nowcast | CSI one-vector | CSI persistence | FSS 9 km nowcast | FSS 9 km persistence | CSI nowcast − persistence [95 % interval] |
|---|---|---|---|---|---|---|
| +5 min | 0.60 | 0.56 | 0.43 | 0.95 | 0.86 | +0.17 [+0.14, +0.19] |
| +10 min | 0.50 | 0.44 | 0.33 | 0.89 | 0.74 | +0.17 [+0.15, +0.23] |
| +15 min | 0.41 | 0.36 | 0.26 | 0.81 | 0.62 | +0.15 [+0.09, +0.22] |
| +20 min | 0.34 | 0.29 | 0.20 | 0.74 | 0.51 | +0.14 [+0.05, +0.24] |
| +30 min | 0.27 | 0.21 | 0.16 | 0.61 | 0.41 | +0.10 [−0.00, +0.24] |
| +45 min | 0.16 | 0.13 | 0.12 | 0.41 | 0.33 | +0.04 [−0.03, +0.12] |
| +60 min | 0.09 | 0.08 | 0.10 | 0.26 | 0.28 | −0.01 [−0.06, +0.04] |
| +90 min | 0.04 | 0.03 | 0.07 | 0.12 | 0.21 | −0.03 [−0.09, +0.02] |

**Rain rate (mm h⁻¹, WSR-88D Z–R) and the ensemble**, pooled (Brier, BSS and ROC for ≥ 20 dBZ;
persistence's "probability" is its 0/1 field):

| lead | MAE nowcast | MAE persistence | MAE difference [95 % interval] | CRPS ensemble | spread | Brier nowcast | Brier persistence | BSS vs climatology | ROC area |
|---|---|---|---|---|---|---|---|---|---|
| +5 min | 2.45 | 3.87 | −1.42 [−2.11, −0.80] | 1.96 | 1.08 | 0.073 | 0.143 | 0.68 | 0.95 |
| +10 min | 3.31 | 5.01 | −1.71 [−3.00, −0.65] | 2.47 | 1.83 | 0.102 | 0.186 | 0.55 | 0.92 |
| +15 min | 4.44 | 6.38 | −1.93 [−3.63, −0.92] | 3.27 | 2.51 | 0.086 | 0.173 | 0.62 | 0.94 |
| +20 min | 4.33 | 6.06 | −1.73 [−3.28, −0.68] | 3.18 | 2.44 | 0.115 | 0.214 | 0.49 | 0.91 |
| +30 min | 4.98 | 6.71 | −1.73 [−3.74, −0.38] | 3.61 | 2.84 | 0.157 | 0.266 | 0.34 | 0.86 |
| +45 min | 7.27 | 8.65 | −1.38 [−3.46, −0.26] | 5.28 | 4.08 | 0.155 | 0.253 | 0.31 | 0.87 |
| +60 min | 7.14 | 7.46 | −0.33 [−2.10, +0.11] | 5.13 | 4.08 | 0.194 | 0.295 | 0.12 | 0.82 |
| +90 min | 7.28 | 7.14 | +0.14 [−2.18, +0.54] | 5.03 | 4.89 | 0.218 | 0.317 | −0.02 | 0.80 |

**By event** (CSI ≥ 20 dBZ, nowcast / persistence; "skill lost after" is the first lead at which the
nowcast no longer beats persistence by at least 0.02 CSI; a dash: no observation within half a step of
that lead at this radar's cadence):

| event | storm mode | +15 min | +30 min | +60 min | +90 min | skill lost after |
|---|---|---|---|---|---|---|
| V1 | supercell (Moore tornado) | 0.67 / 0.58 | 0.58 / 0.50 | 0.47 / 0.40 | 0.41 / 0.30 | >90 min |
| V2 | squall line / derecho (Iowa) | 0.86 / 0.80 | 0.78 / 0.71 | 0.70 / 0.64 | 0.63 / 0.56 | >90 min |
| V3 | tropical rain bands (Harvey, Houston) | 0.87 / 0.86 | 0.82 / 0.84 | 0.75 / 0.83 | 0.67 / 0.82 | 15 min |
| V4 | hurricane eyewall and bands (Ian): rotating motion | 0.88 / 0.84 | 0.83 / 0.80 | 0.76 / 0.79 | 0.71 / 0.78 | 35 min |
| V5 | lake-effect snow band: quasi-stationary | 0.66 / 0.57 | 0.53 / 0.55 | 0.40 / 0.51 | 0.34 / 0.52 | 25 min |
| V6 | atmospheric river, stratiform rain (California) | – | 0.52 / 0.54 | 0.43 / 0.48 | 0.35 / 0.44 | 20 min |
| V7 | supercells (Dodge City) | 0.69 / 0.63 | – | 0.40 / 0.35 | – | >90 min |
| V8 | quiet / clear-air (Denver) | 0.13 / 0.37 | 0.09 / 0.38 | – | 0.01 / 0.31 | 5 min |

### The motion error the ensemble uses

Measured on the tuning events only (T1, T2; 930 origin–lead samples): the standard deviation in m s⁻¹ of
the motion used at the origin minus the motion the echo really had over the lead (the echo-weighted mean
of the frame-pair motions estimated afterwards), along and across the forecast motion.

| lead | +5 | +10 | +15 | +20 | +30 | +45 | +60 | +90 min |
|---|---|---|---|---|---|---|---|---|
| along the motion | 1.67 | 1.67 | 1.53 | 1.53 | 1.67 | 1.77 | 1.78 | 1.80 |
| across the motion | 3.55 | 3.56 | 2.35 | 2.29 | 0.82 | 0.76 | 0.81 | 0.98 |

The across-motion value falls with lead because the "realised" motion over a short lead is one noisy
frame-pair estimate, over a long lead the mean of many. A position uncertainty cannot shrink with lead,
so the engine carries the larger *lead × σ* reached at a shorter lead forward where the table would let
it fall (the across-motion 2.3 m s⁻¹ at 25 min becomes 1.9 at 30 min, 1.3 at 45 min and 0.95 at 60 min;
the position σ, the root mean square of the two components times the lead, is then 1.8 km at 15 min,
3.2 km at 30 min, 5.2 km at 60 min and 7.8 km at 90 min). The table is shipped
as `engine/nowcast/motion_error_default.json` and the record of every nowcast names it. These are the
errors of the *domain-mean* motion on two convective nights; the under-dispersion above shows what
they leave out.

### Reproducing the verification

```bash
cd python
python benchmarks/nowcast_events.py --list                       # the cases (no network)
python benchmarks/nowcast_events.py --download --dir ~/nowcast_events   # ~3.5 GB of Level II volumes, on purpose
python benchmarks/nowcast_verify.py --dir ~/nowcast_events --frames-only --delete-volumes  # ~27 MB of frames
python benchmarks/nowcast_verify.py --dir ~/nowcast_events --members 8 --out ../docs/images/nowcast
```

The volumes are gridded once into a packed CF NetCDF cache per event (composite reflectivity, 1 km,
200 km range), the Level II files can then be deleted (`manifest.json` in the data folder says what is on
disk); the run writes `verification_record.json` (every setting, library version, event fact and score),
the CSV tables, the figures and `verification_tables.txt` (the tables above). The scripts run under a
memory and disk watchdog (`benchmarks/watchdog.py`: the process tree is stopped above 6 GB, below 20 GB
of free disk or above 8 GB of swap); gridding one event takes about 2 GB, the scoring about 1.5 GB.
A volume that xradar cannot read is left out with a note in the record (one KDMX file of V2).

## Limits: what this does not do

* **No initiation, growth or decay.** A storm that forms after the last scan is absent; one that
  intensifies or weakens keeps the intensity it had. This is the main reason skill falls with lead time
  (Germann and Zawadzki 2002), and the reason the squall-line and supercell cases lose skill faster at
  35 and 45 dBZ than at 20 dBZ.
* **Skill falls with lead time** and depends on the storm mode: steady, large patterns (stratiform rain,
  a squall line) keep skill longest; small cells and rotating patterns (hurricane bands) lose it first.
  Beyond the lead at which the nowcast no longer beats persistence the output is a picture of the
  method, not guidance; the table above says where that is for each case.
* **Radar coverage and attenuation.** The frames are what the radars saw: beam overshoot at long range,
  attenuation behind heavy rain, the cone of silence and sector blockage are all in the input and move
  with it. Cells whose air came from outside the observed area are not forecastable (NaN), so the usable
  area shrinks upstream with lead time.
* **The motion is the pattern's, not the wind's.** Propagation (new cells forming on a flank) looks like
  motion; a dense field follows it where the pattern is coherent and the global fallback cannot.
* **One quantity.** Composite reflectivity (or a rain rate) at the surface time of the scan; no vertical
  structure, no hail, no polarimetry.
* **The ensemble is under-dispersive**: it carries only the uncertainty of the mean motion.

## The interface for a hydrological model

The NetCDF written by `-o` (CF-1.10) is the contract a flash-flood or routing model should read:

| variable | dims | units | meaning |
|---|---|---|---|
| `rain_rate` | `lead, y, x` | mm h⁻¹ | deterministic member (member 0) |
| `rain_rate_member` | `member, lead, y, x` | mm h⁻¹ | every member (`--no-members` leaves it out); member 0 is unperturbed |
| `rain_rate_mean`, `rain_rate_spread` | `lead, y, x` | mm h⁻¹ | ensemble mean and standard deviation |
| `prob_rate_ge_<t>` | `lead, y, x` | 1 | probability of rain rate ≥ *t* mm h⁻¹ (0.5, 2, 10) |
| `reflectivity`, `prob_dbz_ge_<t>` | `lead, y, x` | dBZ, 1 | the same for reflectivity input |
| `position_sigma` | `lead` | km | 1σ position uncertainty from the verified motion error |
| `motion_u`, `motion_v` | `y, x` | m s⁻¹ | the motion field used (a retrieval) |
| `covered` | `y, x` | 1 | 1 where a radar observed the cell |
| `lead`, `valid_time` | `lead` | minutes, UTC | lead after the origin; valid time |
| `x`, `y`, `lat`, `lon`, `crs` | | m, degrees | azimuthal-equidistant grid (origin in `crs`), 2-D lat/lon |

NaN means *not forecastable*, never zero rain. Accumulations over a lead interval are the integral of the
rate (mm h⁻¹ × hours); the members are the right input for an ensemble of basin responses, the mean is not
a member. The global attributes carry `data_class = forecast`, the origin time, the method, the skill
statement, the guidance sentence, every parameter, the seed and the full run record as JSON
(`nowcast_record`), so the file can be reproduced from itself.

## Protocol (for tooling)

```
→ {type:"nowcast_run", request_id, file_ids:[...open files in time order...],
   options:{lead_min, step_min, n_members, motion_method, n_frames, origin, resolution_m, max_range_km,
            extent, variable, product, group_s, zr_relation, decay, seed, thresholds, rate_thresholds}}
← progress…, {type:"nowcast_result", request_id, nowcast_id, origin_time, leads_min, valid_times, field,
   units, grid, corners, motion, stats, notes, provenance, record,
   layers:[{lead_min, kind:"deterministic"|"probability", threshold?, png_b64, vmin, vmax, legend, label}]}
← {type:"nowcast_error", request_id, message}     → {type:"nowcast_cancel", request_id}
→ {type:"nowcast_export", request_id, nowcast_id, path, overwrite?}   ← {type:"file_saved", path}
```

The layers are map-ready PNGs on the same image grid as a mosaic (with `corners`), so a panel can draw
them with the existing map layer; `provenance` is the FORECAST layer provenance.

## References

* Bouguet, J.-Y., 2001: Pyramidal implementation of the affine Lucas Kanade feature tracker. Intel
  Corporation, Microprocessor Research Labs.
* Bowler, N. E., C. E. Pierce, and A. W. Seed, 2006: STEPS: A probabilistic precipitation forecasting
  scheme which merges an extrapolation nowcast with downscaled NWP. *Q. J. R. Meteorol. Soc.*, **132**,
  2127–2155.
* Germann, U., and I. Zawadzki, 2002: Scale-dependence of the predictability of precipitation from
  continental radar images. Part I: Description of the methodology. *Mon. Wea. Rev.*, **130**, 2859–2873.
* Gneiting, T., and A. E. Raftery, 2007: Strictly proper scoring rules, prediction, and estimation.
  *J. Amer. Stat. Assoc.*, **102**, 359–378.
* Hersbach, H., 2000: Decomposition of the continuous ranked probability score for ensemble prediction
  systems. *Wea. Forecasting*, **15**, 559–570.
* Jolliffe, I. T., and D. B. Stephenson (eds.), 2012: *Forecast Verification: A Practitioner's Guide in
  Atmospheric Science*, 2nd ed. Wiley.
* Knutsson, H., and C.-F. Westin, 1993: Normalized and differential convolution. *Proc. IEEE CVPR*,
  515–523.
* Lucas, B. D., and T. Kanade, 1981: An iterative image registration technique with an application to
  stereo vision. *Proc. 7th Int. Joint Conf. on Artificial Intelligence*, 674–679.
* Pulkkinen, S., et al., 2019: Pysteps: an open-source Python library for probabilistic precipitation
  nowcasting (v1.0). *Geosci. Model Dev.*, **12**, 4185–4219.
* Rinehart, R. E., and E. T. Garvey, 1978: Three-dimensional storm motion detection by conventional
  weather radar. *Nature*, **273**, 287–289.
* Roberts, N. M., and H. W. Lean, 2008: Scale-selective verification of rainfall accumulations from
  high-resolution forecasts of convective events. *Mon. Wea. Rev.*, **136**, 78–97.
* Staniforth, A., and J. Côté, 1991: Semi-Lagrangian integration schemes for atmospheric models: A
  review. *Mon. Wea. Rev.*, **119**, 2206–2223.
* Theis, S. E., A. Hense, and U. Damrath, 2005: Probabilistic precipitation forecasts from a
  deterministic model: a pragmatic approach. *Meteorol. Appl.*, **12**, 257–268.
* Wilks, D. S., 2011: *Statistical Methods in the Atmospheric Sciences*, 3rd ed. Academic Press.
