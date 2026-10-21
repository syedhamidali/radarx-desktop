# Rainfall and hydrology

From radar volumes to **rain rate, rainfall accumulation and basin-average rainfall**, with the
uncertainty and the beam quality shown next to every number, optional adjustment to rain gauges, and
a CF NetCDF that says how it was made. Built on [radarx](https://radarx.readthedocs.io/en/latest/)
(rain-rate relations, KDP, attenuation, hydrometeor classification, beam blockage, advection) and the
mosaic grid of the app. Works from the app (the **Rainfall** panel), and the command line (`rxd qpe`,
`rxd basin`), with the same numbers.

```{admonition} An estimate, never an observation
:class: warning
Every radar rainfall number is a **retrieval**: a model of the drop size distribution turns what
the radar measured (reflectivity, differential reflectivity, differential phase) into rain at the
beam height. Outputs are labelled `RETRIEVED`; after a gauge adjustment `ADJUSTED` (the gauge set,
the method and its parameters are stored). Nothing is ever labelled as observed. The uncertainty
fields below say how large the *known* error sources are; they do not include everything that can
go wrong (see [Limits](#limits-what-this-does-not-do)).
```

## Quick start

```bash
# an accumulation from a folder of volumes of one radar (needs radarx)
rxd qpe -i 'mayfield/KHPX/*_V06' -o rain.nc -p rain.png

# the instantaneous rate of a single volume, tropical Z-R, with a known freezing level
rxd qpe -i KHPX20211211_020457_V06 --relation tropical --freezing-level-m 3800 -o rate.nc

# four radars, quality-weighted, on a 2 km grid
rxd qpe -i 'mayfield/*/*_V06' --resolution-m 2000 -o network.nc

# adjust to gauges, summarise basins, return period from a table you supply
rxd qpe -i 'mayfield/KHPX/*_V06' --gauges gauges.csv --adjust ked \
        --basins watersheds.geojson --frequency atlas14_6h.json -o adjusted.nc
rxd basin -i 'mayfield/KHPX/*_V06' -b watersheds.geojson --thresholds 10,25,50 -p series.png
```

In the app: open the volumes, expand **Rainfall**, choose the relation, press **Estimate rainfall**.
The accumulation is drawn on the map with the mosaic layer (opacity, legend, hover values); the panel
switches the map between accumulation, uncertainty, time observed, quality, beam height and range,
and shows gauge scatter and basin tables.

```{image} images/ui/rainfall-map.png
:alt: Gauge-adjusted rainfall accumulation of the KHPX radar, Mayfield storm, 11 December 2021
:width: 100%
```

Gauge-adjusted 17 minute accumulation of the KHPX volumes (Mayfield storm). The label in the legend says
`ADJUSTED`; the radar-only field is the layer above it in the panel. The gauges of this example are
**synthetic** (the radar times 1.3 with noise, to show the chain: nothing is downloaded).

```{image} images/ui/rainfall-validation.png
:alt: The Rainfall panel: result, gauge comparison table and scatter
:width: 30%
```
```{image} images/ui/rainfall-basins.png
:alt: The Rainfall panel: basin table and series
:width: 30%
```

## How the rain rate is estimated

Notation: *R* rain rate (mm h⁻¹), *Z* reflectivity factor (mm⁶ m⁻³) from the horizontal
reflectivity in dBZ, *zdr* = 10^(ZDR/10), *K*<sub>DP</sub> in ° km⁻¹, *A* specific attenuation in dB km⁻¹.
All relations are power laws fitted to drop size distributions; **no coefficient is fitted here**.

| Method | Relation | Where it comes from |
|---|---|---|
| `z` | *R* = *a* *Z*<sup>*b*</sup>, default **Z = 300 R<sup>1.4</sup>** (*R* = 0.0170 *Z*<sup>0.714</sup>) | the operational WSR-88D relation (Fulton et al. 1998) |
| `kdp` | *R* = 44.0 \|*K*<sub>DP</sub>\|<sup>0.822</sup> (S), 30.5 · ^0.85 (C), 16.9 · ^0.801 (X) | Giangrande & Ryzhkov (2008) at S; Bringi & Chandrasekar (2001, Eq. 8.14) at C; Ryzhkov et al. (2014) at X |
| `z_zdr` | *R* = 1.42·10⁻² *Z*<sup>0.770</sup> *zdr*<sup>−1.67</sup> (S); 5.8·10⁻³ *Z*<sup>0.91</sup> *zdr*<sup>−2.09</sup> (C); 3.9·10⁻³ *Z*<sup>1.07</sup> *zdr*<sup>−5.97</sup> (X) | Giangrande & Ryzhkov (2008); Bringi & Chandrasekar (2001, Table 8.1) |
| `a` | *R* = *a*(*T*) *A*<sup>*b*</sup>(*T*), e.g. 4.12·10³ *A*<sup>1.03</sup> at S and 20 °C | Ryzhkov et al. (2014, Table 1); *A* = α *K*<sub>DP</sub>, α from Bringi et al. (1990) |
| `hybrid` | per gate, see below | Ryzhkov et al. (2005); Giangrande & Ryzhkov (2008); Park et al. (2009) |
| `auto` | `hybrid` if ZDR and phase exist, else `z_zdr`, else `z` | |

Each is evaluated by the compiled radarx kernels (a NEXRAD sweep in a few milliseconds).

### Z-R relations you can select

`rxd qpe --list-relations` prints them. *Z* = *A* *R*<sup>β</sup>:

| key | *A* | β | use |
|---|---|---|---|
| `wsr88d` (= `convective`) | 300 | 1.4 | default of the US network, deep convection (Fulton et al. 1998) |
| `marshall_palmer` (= `stratiform`) | 200 | 1.6 | widespread rain (Marshall & Palmer 1948) |
| `tropical` | 250 | 1.2 | many small drops: tropical / maritime rain (Rosenfeld et al. 1993; WSR-88D option) |
| `stratiform_east`, `stratiform_west` | 130, 75 | 2.0 | the WSR-88D cool-season stratiform options |
| `snow_sekhon_srivastava` | 1780 | 2.21 | snow, liquid-equivalent *S* (Sekhon & Srivastava 1970), `--precip snow` |
| `snow_cool_stratiform` | 75 | 2.0 | the WSR-88D cool-season pair applied to snow: a practical choice, not a snow fit |

A custom pair: `-P zr_a=200 -P zr_b=1.6`. The relation depends strongly on the drop size
distribution (the same reflectivity means 5 mm h⁻¹ with one relation and 8 with another): choose it for
the rain regime, do not treat the default as truth. Snow Z-S relations are uncertain by a factor of two
or more; the engine assumes σ(ln *S*) = 0.7 for them (an assumption, not a fit).

### The hybrid, gate by gate

1. Gates with RhoHV < 0.80 are not weather (rate 0, flagged). Reflectivity is raised to `z_cap`
   (53 dBZ) before R(Z) and R(Z,ZDR) to limit hail contamination (Giangrande & Ryzhkov 2008).
2. **Rain regime** by R(Z): below `r_light` (6 mm h⁻¹) R(Z); up to `r_heavy` (50 mm h⁻¹) R(Z,ZDR); above
   it R(*K*<sub>DP</sub>) where *K*<sub>DP</sub> ≥ 0.3 ° km⁻¹ (radarx: R(KDP) is not useful below that), else R(Z,ZDR).
   The structure follows the synthetic algorithm of Ryzhkov et al. (2005); **the two thresholds are
   tunable parameters** (the values are ours, chosen so each estimator is used where it is the more
   reliable one: R(Z,ZDR) improves on R(Z) when rain is moderate, R(KDP) is immune to calibration,
   attenuation and hail).
3. **Hydrometeor class** (radarx `hid`, Park et al. 2009 at S band, Dolan et al. at C/X): rain classes
   use the regime above; **rain–hail mixture, hail and graupel** use R(*K*<sub>DP</sub>) when usable, else the capped
   R(Z) (`hail_policy`); wet snow, dry snow and ice crystals:
   * with a **freezing level** given (`--freezing-level-m`), these gates and every beam inside or above the
     melting layer (beam half-width included) get **no estimate** (`ml_policy`; `flag` estimates and flags),
   * **without** a freezing level the classification cannot be trusted (it needs the temperature: on a
     warm-sector storm about half of the echo was labelled snow or ice), so the gate is estimated as rain
     and flagged `phase_unverified` (`phase_policy`: `auto` | `exclude` | `rain`).
4. Observed no-echo gates and echo below `min_dbz` (5 dBZ) are rain **0**; a gate with echo but no valid
   relation is **no estimate** (NaN), never a silent zero.

Prepare the inputs: correct reflectivity and ZDR for attenuation (`attenuation auto` applies the radarx
ZPHI correction at C and X band, and only flags at S band), remove the ZDR calibration offset
(`zdr_offset_db`) and reflectivity bias (`z_offset_db`); KDP is estimated from PHIDP with radarx when the
file has none. A 1 dB bias in *Z* changes R(Z) by about 18 %; 0.2 dB in ZDR changes R(Z,ZDR) by about 7 %.

## Quality and uncertainty fields

Per gate, then averaged over the gates of the serving beam in each cell:

| field | meaning |
|---|---|
| `beam_height_agl` | beam-centre height (m): above ground with a DEM (`--dem`), else above the radar (= above ground only on flat terrain) |
| `range_to_radar` | ground distance to the serving radar (km) |
| `snr` | time-mean signal-to-noise ratio (dB) of the serving beam; **only present when the files carry an `SNR` field** (never derived from Z and range). `min_snr_db` (off by default) treats gates below it as non-weather |
| `quality_index` | *Q* = exp(−*h*/3 km) · exp(−(*s*/150 km)²) · (1 − CBB), in 0–1; an index, **not a probability**; the scales are ours |
| `quality_flags` | bit field set where ≥ 25 % of the gates carry it: melting layer / ice, hail–graupel, attenuation (PIA ≥ 1 dB), beam blockage (CBB ≥ 0.1), Z capped, non-meteorological, no valid relation, phase unverified |
| `n_radars` | most radars with an estimate in the cell |
| `valid_time_fraction` | share of the period with an estimate; time without data is **not** counted as zero |
| `accumulation_uncertainty` | 1σ of the accumulation in mm (below) |

Beam geometry is the effective-Earth-radius model (Doviak & Zrnic 1993, *k*<sub>e</sub> = 4/3):
ρ = √(*r*² + *R′*² + 2 *r* *R′* sin θ), *h* = ρ − *R′*, *s* = *R′* arcsin(*r* cos θ / ρ). With `--dem`
(NetCDF terrain, never downloaded) radarx computes the cumulative beam blockage CBB (Bech et al. 2003);
reflectivity can be corrected by −10 log₁₀(1 − CBB) for CBB ≤ 0.5 (`correct_blockage`), KDP and A
need no correction.

**Uncertainty** is a lognormal 1σ of *R*, σ(ln *R*):

* R(Z): √(σ<sub>DSD</sub>² + (*b* ln10/10 · σ<sub>Z</sub>)²) with σ<sub>Z</sub> = 1 dB;
* R(Z,ZDR): √(σ<sub>DSD</sub>² + (*b* ln10/10 σ<sub>Z</sub>)² + (*c* ln10/10 σ<sub>ZDR</sub>)²), σ<sub>ZDR</sub> = 0.2 dB;
* R(KDP): √(σ<sub>DSD</sub>² + (*b* σ<sub>KDP</sub>/*K*<sub>DP</sub>)²), σ<sub>KDP</sub> from the radarx KDP processing;
* R(A): adds the variability of α.

σ<sub>DSD</sub> is the scatter of each relation over the radarx drop-size-distribution prior
(`relation_scatter`; S band R(Z) 0.46, R(KDP) 0.27, R(Z,ZDR) 0.10, R(A) 0.13; C band 0.60, 0.26, 0.13, 0.42;
X band 0.59, 0.22, 0.10, 0.28). Calibration values are options. For an **accumulation** the
errors are taken as fully correlated in time (σ<sub>acc</sub> = Σ σ<sub>k</sub> *R*<sub>k</sub> Δ*t*, an upper bound), and
the composite 1σ of several radars is their quality-weighted mean (radar errors are not assumed independent,
so averaging does not shrink it).

```{admonition} What the uncertainty does not contain
:class: caution
Vertical profile of reflectivity, the bright band, beam overshoot of shallow rain at long range,
partial blockage without a DEM, wet radome, ZDR/Z calibration beyond the stated σ, errors of the
advection, and any bias common to all relations. Read the quality fields next to it: a small σ at
150 km range with a beam 3 km above the ground is *not* a small uncertainty.
```

## Compositing radars

The grid is the azimuthal-equidistant grid of the mosaic engine (square cells, true cell area is
cell² divided by the grid's area distortion, 0.02 % at 230 km). Each sweep is **averaged** onto the cells
(a summed-area table over the polar array: a cell is the mean of its gates, not a nearest-gate sample), so
the rain volume does not depend on how the gates fall. Per radar the lowest beams are merged *best beam*:
the highest quality index wins, so the lowest beam is used unless it is blocked (hybrid-scan idea of the
operational networks, Fulton et al. 1998). Radars are then combined by `weighted` (default: mean with
weights *Q*², the others reduce the noise) or `best` (the highest *Q* only).

## Accumulation over a period

A volume is the rate at its scan time (mean ray time of the lowest sweep). The accumulation is the integral

    A = Σ_k  ½ (R_{k-1} + R_k) (t_k − t_{k-1}) / 3600        (mm, R in mm/h, t in s)

over **nodes** that include every scan time and a regular spacing (`--step-s`, 60 s). What is added up is the
rain rate in mm/h, a linear quantity; reflectivity in dBZ is never summed or averaged (it is a logarithm). Where
reflectivity itself is averaged over scans, for example the mean of a series of files, the average is taken of the
linear Z in mm6 m-3 and converted back: 20 and 40 dBZ average to 37.0 dBZ, not 30. A sum of dBZ is refused. Between two scans of a
radar the rate at a node is the **advection-corrected interpolation** of radarx `interpolate_time`
(Gal-Chen 1982 frame of reference, Staniforth & Côté 1991 departure points): the earlier field is advected
forward, the later one backward, and they are blended with weights (1 − *f*) and *f*. The storm motion is the
FFT cross-correlation of the two fields (Rinehart & Garvey 1978) and is reported per radar. Without usable
motion (or `--no-advect`) the interpolation is linear in time.

* **Sampling interval**: *rate × dt*, with the real time between scans; a constant rate gives *R T* exactly.
* **Gaps**: a gap longer than `--max-gap-min` (20) is **not** bridged: the time is missing and
  `valid_time_fraction` < 1. A period that starts before the first scan or ends after the last is **not
  extrapolated** (`--hold-edges-s` holds the first/last scan for a stated time, e.g. 300 s so that an hour
  boundary is covered when the first scan is at 20 s).
* **One volume per radar** gives the instantaneous rate (`rate_last`), the accumulation is 0 mm over 0 s.
* **Long series**: each file is opened, used and closed; for more than 24 scans the per-scan grids are cropped
  to the radar footprint, stored (float16 for the derived fields) in a temporary folder and read back when
  needed, so memory does not grow with the number of scans. Without `--extent` every file is opened once more
  first to read the radar site; give `--extent W,S,E,N` for a long series to decode each file only once.

## Outputs and provenance

`-o rain.nc` writes a CF-1.10 NetCDF grid (azimuthal-equidistant `x`/`y` in m with 2-D `lat`/`lon`, `crs`
grid mapping, `time` = period end with `time_bounds`): `accumulation` (`lwe_thickness_of_precipitation_amount`,
mm, `cell_methods = "time: sum"`), `accumulation_uncertainty`, `rain_rate_max` and `rain_rate_last`
(`lwe_precipitation_rate`), `valid_time_fraction`, `quality_index`, `beam_height`, `range_to_radar`, `snr` (if the files have an SNR field),
`quality_flags` (`flag_masks`/`flag_meanings`), `n_radars`, and per radar the site and scan count. With gauges:
`accumulation_adjusted` and `adjustment_factor`. **Every field carries** `provenance_type`
(`RETRIEVED` / `ADJUSTED`), `retrieval_method`, `retrieval_parameters` (JSON) and `uncertainty_model`; the
global attribute `provenance` holds the complete record (inputs, method, parameters, period, software). The
other outputs: `.png` (colour image plus `.pgw`/`.prj` so GIS software places it), `.json` (the summary:
everything above plus gauge validation, basins and return periods), `.csv` (basin table and one series file
per basin), `-p` a map figure whose title says `[RETRIEVED: an estimate, not an observation]`.

## Rain gauges

Local files only; **nothing is downloaded** (a provider will feed the same structures later).

| input | how |
|---|---|
| generic CSV / JSON / GeoJSON points | columns found by name (`station`, `lat`/`latitude`, `lon`, `time`/`datetime`/`date`, `precip`/`prcp`/`rain`/`mm`/`precip_in`), or map them (`mapping=`) |
| NOAA hourly (HPD) | `HPCP` in inches (`--gauge-units 0.01in` for the older files), hour *ending* at `DATE`; `T` trace counted as 0 (said); missing sentinels dropped; measurement flags `[`…`]` (accumulated or deleted periods) mark records that are **not** used as hourly values |
| NOAA daily (GHCN-Daily) | `PRCP` in mm (or `--gauge-units in`); the observation day, not aligned to UTC: use only for whole-day periods |
| METAR / ASOS text | `Pnnnn` (hundredths of an inch since the previous hour), rounded to the hour; a routine report without a P group is 0; a station table with coordinates is required |

**Time handling**: a record is the depth over the interval *ending* at its time, in UTC. Naive local
times are converted with `--gauge-tz America/Chicago`; `--gauge-time start|centre` converts other
conventions; the interval (`--gauge-interval-s`) is read from the file or inferred from the spacing. A gauge
total over the radar period sums only the records wholly inside it (± `tolerance_s`, default 300 s) and
requires 90 % coverage; the mismatch between the gauge and radar windows is reported, never hidden.

**Quality control** (flags kept with the reason, flagged records never used): missing / sentinel, negative,
`duplicate` (identical records: one kept), `duplicate_conflict` (all excluded), `implausible` (> 300 mm h⁻¹),
`stuck` (≥ 6 identical positive values in a row).

**Adjustment** pairs each gauge with the mean radar accumulation of the 3 × 3 cells around it and fits:

| method | what it does |
|---|---|
| `mfb` | mean-field bias, *F* = Σ*G* / Σ*R* over gauges with rain (Smith & Krajewski 1991); 90 % bootstrap interval reported |
| `brandes` | spatially varying: ln *F*(**x**) is the Gaussian-weighted mean of ln(*G*/*R*) of nearby gauges, shrunk to the mean-field bias (in the spirit of Brandes 1975); length scale from the gauge spacing |
| `ked` | kriging with external drift: the gauge field interpolated with the radar accumulation as the drift (Wackernagel 1998; Goudenhoofd & Delobbe 2009), exponential variogram with nugget fitted to the residuals; exact at the gauges; the kriging σ is returned |

An adjustment never creates rain where the radar saw none (< 0.01 mm stays 0). The adjusted field is
`ADJUSTED` and the radar field is kept. **Validation**: bias (mm and relative), MAE, RMSE, correlation and the
Kling–Gupta efficiency KGE = 1 − √((*r*−1)² + (α−1)² + (β−1)²) (Gupta et al. 2009), overall and by gauge
amount and by range from the radar, with the scatter and the residual map; the honest skill is
**leave-one-gauge-out** (each gauge predicted from the others), the in-sample fit of an interpolator
is meaningless. Gauges are point measurements with errors of their own (wind undercatch, siting, snow).

## Basins (watersheds)

Polygons from **GeoJSON, ESRI shapefile (`.shp` + `.dbf` + `.prj`) or GeoPackage** are read with the
standard library, NumPy and pyproj only (no GDAL, Fiona or GeoPandas: the installer does not grow) and
reprojected to WGS 84 (`--basin-crs` when the file has none). The cell fractions inside the polygon are
computed **exactly**: for a polygon and a cell the covered area is Σ over edges *s*<sub>e</sub> ∫ clamp(*x*<sub>e</sub>(*y*), *x*₀, *x*₁) d*y*
(closed form for straight edges, holes subtract), and the weight of a cell is that fraction times the
**true** cell area. Statistics are area-weighted: mean = Σ*w v* / Σ*w* over cells with a value, `coverage`
the share of the basin with an estimate, area above thresholds Σ*w*[*v* ≥ *t*]; also max, minimum,
weighted standard deviation, median and 90th percentile. The grid area is compared with the geodesic area of
the polygon (reported; ≤ 0.02 % in the tests). Per basin you get the basin-mean rate series, the maximum
cell rate, the cumulative mean depth, the hyetograph in any bin (`--bin-minutes`), and, after a gauge
adjustment, the same series re-derived with the adjustment (exactly, from the kept per-cell rates).

**Return period**: give a precipitation-frequency table you obtained yourself (e.g. NOAA Atlas 14 point
depths) as JSON, `{"duration_hours": 6, "depth_mm": {"2": 50, "10": 72, "100": 108}}` (or a `"basins"` map
with a table per basin id); the largest basin-mean depth over any window of that duration is interpolated
log–log (`<2` / `>100` are flagged, never extrapolated). Those tables are *point* depths and no areal
reduction factor is applied, so the return period of a large basin is an **upper bound**.

## Accuracy against a known truth

These numbers come from the validation runs of the method; the DSD runs use the radarx forward
model (T-matrix scattering) on gamma drop size distributions (μ = 3, D<sub>m</sub> 0.9–2.6 mm, N<sub>w</sub>
10³–10⁴·² mm⁻¹ m⁻³; 2400 gates, S band), the truth being the rain rate of the distribution itself.

| method | rain rate of the DSD | bias | normalised MAE |
|---|---|---|---|
| R(Z) | 0.5–5 mm/h | +1.9 % | 22.8 % |
| | 5–25 | +14.5 % | 29.7 % |
| | ≥ 25 | −4.6 % | 13.7 % |
| R(Z,ZDR) | 0.5–5 | −4.0 % | 17.1 % |
| | 5–25 | −1.8 % | 15.6 % |
| | ≥ 25 | −19.3 % | 20.0 % |
| R(KDP) | 5–25 | −5.9 % | 16.8 % |
| | ≥ 25 | −14.4 % | 15.4 % |
| hybrid | 0.5–5 | +1.0 % | 21.9 % |
| | 5–25 | −1.9 % | 15.7 % |
| | ≥ 25 | −14.6 % | 15.5 % |

Read it honestly: the polarimetric relations roughly **halve the scatter in moderate rain** (15.7 % vs 29.7 %
for R(Z)), but the published S-band relations (fitted to Oklahoma data) are 10–20 % low in heavy rain on this
synthetic ensemble, and over the whole range R(Z) is not worse. None is "right": the ranking changes with
the drop size distribution, which is why the uncertainty carries the DSD scatter.

Other known-answer tests: a **constant rate** of 10 mm/h over irregular scan spacings (3, 7, 5, 6 min) gives
3.5000 mm in 21 min to 6 digits; the **sampling interval** (30 s vs 300 s nodes) changes nothing; a **Gaussian
storm moving at 15 m/s** between two scans 10 min apart: the advected accumulation differs from the exact
integral by 0.002 mm (0.04 % of the 4.7 mm peak) against 0.034 mm (0.7 %) for linear time interpolation, the rain volume is
conserved to 0.1 % and the retrieved motion is 14.98 m/s; a **basin polygon** of 2870 km² is recovered with a
relative error of 0.02 % against the geodesic area, a hole is subtracted exactly, and the area-weighted
mean of a ramp over a triangle matches the centroid; **gauges** with a known bias of 1.5 give a
mean-field bias of 1.500 (within 6 % with 15 % lognormal gauge noise); with a spatially varying bias
(1.0 to 2.0) the spatial methods cut the leave-one-out RMSE to below 0.7 of the mean-field one.

**Real data** (Mayfield tornado outbreak, 11 Dec 2021, KHPX/KNQA/KPAH/KVWX, no gauges): the four KHPX volumes
(17 min, 2 km grid) give a maximum accumulation of 14 mm with the hybrid (22 mm with R(Z), 32 mm with R(Z,ZDR)) in
the supercell track; the retrieved storm motion is 21 m/s east and 17 m/s north (the line moved to the north-east);
advected and linear time stepping give the same rain volume to 0.3 % with a pattern correlation of 0.98; the
hybrid gives 91 % of the rain volume of R(Z) (median ratio per cell 0.90, 10th to 90th percentile 0.72–1.14);
R(KDP) alone gives 1.6 times that of R(Z) in the cores (and no estimate below 0.3 ° km⁻¹); the tropical relation
about twice the accumulation of the default where more than 1 mm fell. There is no truth for a real storm
here: these are consistency checks. (An earlier version dropped half of the echo of this warm-sector storm as
"snow" because the hydrometeor classification needs the temperature: hence `phase_policy`.)

**Speed and memory** (Apple M-series laptop, NEXRAD Level II with 19 sweeps, 1 km grid, radarx compiled
kernels): decoding a volume takes 2.5–3 s (the file reader, not the rain-rate code); the rain rate of one volume
(two lowest beams, KDP, hybrid, gridding) 0.8–1.1 s; four beams with a 500 m grid 1.9 s; the time
integration of 20 nodes on a 4-radar 796 × 723 grid 0.8 s per radar pair; 16 volumes of 4 radars: 20 s with
open volumes. Peak memory is one decoded volume plus the working set, about 1–1.6 GB for NEXRAD.

## Limits: what this does not do

* **Beam overshoot and vertical profile**: at long range the lowest beam is high above the ground; shallow
  rain, evaporation below the beam or orographic enhancement are missed. There is no VPR correction.
* **Bright band / melting layer**: without a freezing level the engine cannot tell rain from the melting layer
  except through the hydrometeor class, which needs the temperature. Give `--freezing-level-m`.
* **Attenuation**: at C and X band and in heavy rain at S band reflectivity is biased low behind the core;
  the ZPHI correction needs a good PHIDP. Check the `attenuation` flag.
* **Z-R dependence**: one relation cannot be right for convective and stratiform rain; see the table of
  relations and the DSD scatter in the uncertainty.
* **Calibration**: a 1 dB reflectivity bias is 18 % in R(Z); ZDR offsets matter for R(Z,ZDR). NEXRAD ZDR
  is not calibrated here (`zdr_offset_db`).
* **Hail** raises Z: capped at 53 dBZ (R(Z) ≤ about 100 mm/h); R(KDP) is used in rain–hail mixtures.
* **Partial beam blockage** is only evaluated with a DEM; without one the blockage flag is absent and the
  panel/CLI says so.
* **Time**: scans are minutes apart; the advection interpolation follows a single smooth motion per scan pair
  (optionally tiles via `-P tile_km=50`), not growth or decay. Radars of a network scanned minutes apart are
  advected only when you give a period with several volumes.
* **Gauges** are point values with their own errors; a mean-field bias hides a spatial bias; KED needs
  at least 8 gauges with rain and is only as good as the variogram that 8–40 points allow.
* **Basins**: the return period compares an area mean with point depths.
* **Snow**: the Z-S relations are uncertain by a factor of two or more and gauges under-catch snow.

## Command line and app reference

`rxd qpe` options: `-i` volumes (files, globs, folders), `-o` (`.nc .png .json .csv`), `-p` figure, `-P KEY=VALUE`
(any estimator option: `zr_a`, `zr_b`, `sigma_z_db`, `phase_policy`, `ml_policy`, `hail_policy`, `kdp_min`,
`r_light`, `r_heavy`, `z_offset_db`, `zdr_offset_db`, `block_max`, `tile_km` ...), `--method`, `--relation`,
`--band`, `--precip`, `--freezing-level-m`, `--z-cap/--no-z-cap`, `--attenuation`, `--dem`, `--start/--end`,
`--resolution-m`, `--extent`, `--max-range-km`, `--composite`, `--no-advect`, `--step-s`, `--max-gap-min`,
`--hold-edges-s`, `--gauges` (+ `--adjust`, `--gauge-format/-units/-tz/-stations/-interval-s/-time`,
`--min-gauge-mm`, `--no-gauge-qc`), `-b/--basins` (+ `--basin-id`, `--basin-name`, `--basin-crs`, `--thresholds`,
`--frequency`), `--field`, `--list-relations`, and the shared `-v -q --json --config --print-command`.
`rxd basin` is the same pipeline with the basins required and the table / hyetograph as the default outputs.
Exit codes as in {doc}`CLI`: a wrong option or relation is 2, a missing file 3, a processing failure 1.

Modules (Modules panel, category *Hydrology*): `hydro_qpe` (rain rate of one sweep with uncertainty, beam quality
and the relation used, as polar fields; "All sweeps → 3D" works) and `hydro_basin_rate` (area-weighted rate over
basins from one volume).

App: the **Rainfall** panel (radar volumes shared with the Mosaic panel, estimate, gauges, basins, run; result:
legend and map layer switcher, summary, method text, notes, gauge scatter with the 1:1 line, validation tables
by range and amount, residual map, basin table and series chart, export to NetCDF/PNG/JSON/CSV in the desktop
app). The map layer is the mosaic layer, so the Mosaic panel shows the
same image while it is on the map.

## References

* Bech, J., et al., 2003: The sensitivity of single polarization weather radar beam blockage correction to
  variability in the vertical refractivity gradient. *J. Atmos. Oceanic Technol.*, **20**, 845–855.
* Bringi, V. N., et al., 1990: An examination of propagation effects in rainfall on radar measurements at
  microwave frequencies. *J. Atmos. Oceanic Technol.*, **7**, 829–840.
* Bringi, V. N., and V. Chandrasekar, 2001: *Polarimetric Doppler Weather Radar*. Cambridge University Press.
* Brandes, E. A., 1975: Optimizing rainfall estimates with the aid of radar. *J. Appl. Meteor.*, **14**, 1339–1345.
* Doviak, R. J., and D. S. Zrnić, 1993: *Doppler Radar and Weather Observations*. Academic Press.
* Fulton, R. A., et al., 1998: The WSR-88D rainfall algorithm. *Wea. Forecasting*, **13**, 377–395.
* Gal-Chen, T., 1982: Errors in fixed and moving frame of references: Applications for conventional and
  Doppler radar analysis. *J. Atmos. Sci.*, **39**, 2279–2300.
* Giangrande, S. E., and A. V. Ryzhkov, 2008: Estimation of rainfall based on the results of polarimetric echo
  classification. *J. Appl. Meteor. Climatol.*, **47**, 2445–2462.
* Goudenhoofd, E., and L. Delobbe, 2009: Evaluation of radar-gauge merging methods for quantitative
  precipitation estimates. *Hydrol. Earth Syst. Sci.*, **13**, 195–203.
* Gupta, H. V., et al., 2009: Decomposition of the mean squared error and NSE performance criteria:
  Implications for improving hydrological modelling. *J. Hydrol.*, **377**, 80–91.
* Marshall, J. S., and W. M. Palmer, 1948: The distribution of raindrops with size. *J. Meteor.*, **5**, 165–166.
* Park, H. S., et al., 2009: The hydrometeor classification algorithm for the polarimetric WSR-88D. *Wea.
  Forecasting*, **24**, 730–748.
* Rinehart, R. E., and E. T. Garvey, 1978: Three-dimensional storm motion detection by conventional weather
  radar. *Nature*, **273**, 287–289.
* Rosenfeld, D., D. B. Wolff, and D. Atlas, 1993: General probability-matched relations between radar
  reflectivity and rain rate. *J. Appl. Meteor.*, **32**, 50–72.
* Ryzhkov, A., M. Diederich, P. Zhang, and C. Simmer, 2014: Potential utilization of specific attenuation for
  rainfall estimation, mitigation of partial beam blockage, and radar networking. *J. Atmos. Oceanic Technol.*, **31**, 599–619.
* Ryzhkov, A. V., S. E. Giangrande, and T. J. Schuur, 2005: Rainfall estimation with a polarimetric prototype
  of WSR-88D. *J. Appl. Meteor.*, **44**, 502–515.
* Sekhon, R. S., and R. C. Srivastava, 1970: Snow size spectra and radar reflectivity. *J. Atmos. Sci.*, **27**, 299–307.
* Smith, J. A., and W. F. Krajewski, 1991: Estimation of the mean field bias of radar rainfall estimates.
  *J. Appl. Meteor.*, **30**, 397–412.
* Staniforth, A., and J. Côté, 1991: Semi-Lagrangian integration schemes for atmospheric models: A review.
  *Mon. Wea. Rev.*, **119**, 2206–2223.
* Wackernagel, H., 1998: *Multivariate Geostatistics*. Springer.
* Zhang, J., et al., 2016: Multi-Radar Multi-Sensor (MRMS) quantitative precipitation estimation: Initial
  operating capabilities. *Bull. Amer. Meteor. Soc.*, **97**, 621–638.

## Protocol (for tooling)

```
→ {type:"qpe_info"}                                  ← {type:"qpe_info_result", relations, default_relation, methods, fields}
→ {type:"qpe_run", request_id, file_ids, config:{method, relation, precip, band, freezing_level_m, z_cap, attenuation, ...},
   options:{start, end, resolution_m, extent, max_range_km, composite, advect, step_s, max_gap_s, hold_edges_s},
   basins?:{path, id_field?, crs?, thresholds?, frequency?}, gauges?:{path, adjust, units, tz, interval_s, stations}, field?}
                                                     ← progress…, {type:"qpe_result", request_id, qpe_id, summary, layer}
                                                     ← {type:"qpe_error", request_id, kind, message}
→ {type:"qpe_layer", qpe_id, field, vmin?, vmax?}    ← {type:"qpe_layer_result", qpe_id, layer}
→ {type:"qpe_adjust", qpe_id, gauges:{...}}          ← {type:"qpe_adjust_result", summary, validation, layer}
→ {type:"qpe_series", qpe_id, basin, bin_minutes?}   ← {type:"qpe_series_result", rows, hyetograph, stats, exceedance, provenance_type}
→ {type:"qpe_export", qpe_id, format:"nc"|"png"|"json"|"csv", path, field?, overwrite?}   ← {type:"file_saved", path}
```

`layer` carries the PNG as base64, its corners, the colour range and legend and a `mosaic_id`: the float grid
is registered in the mosaic cache, so `mosaic_probe` answers for it. The messages are refused in the web service
(they read local gauge and basin files).
