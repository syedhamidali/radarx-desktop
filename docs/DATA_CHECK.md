# Data check (smart open)

When a file opens, the app looks at the data that is already in memory and tells you what is in
it, what looks wrong, where, and what to do next. **It never changes anything by itself**: the
suggested clean-up is a normal {doc}`pipeline recipe <PIPELINES>` that runs only when you click
*Apply* (or run it from the command line).

* **App:** a *Data check* chip appears in the status bar a moment after a file has opened
  (`1 warning, 5 notices`). Click it to open the panel; the `x` hides the chip until the file is
  closed, *Don't show again for this file* remembers that choice for the file path (also after a
  restart), and *Command palette > Data Check* brings it back.
* **Command line:** `rxd check -i FILE [FILE ...] [--json] [--fix-plan]` (see below).

The check is fast (about 1 s for a NEXRAD volume, measured; see *Cost*), uses only the sweeps that
are already loaded (a lazily-loaded cloud scan is never fetched for it) and holds one sweep in
memory at a time (about 50 MB on top of a 330 MB volume).

## How to read a finding

| Field | Meaning |
|---|---|
| severity | `info`: a fact about the file. `notice`: worth knowing. `warning`: act before analysing. |
| evidence | Labelled numbers. **RETRIEVED**: counted or measured directly in the data. **ESTIMATE**: a model- or heuristic-based value; the method is stated in the finding. |
| where | The sweeps, and the azimuth interval and range interval (km) that hold 90 % of the affected gates. |
| confidence | 0 to 1: how sure the *detector* is, not how bad the problem is. |
| method | The technique with its literature reference, and its caveats. |

Severity is a rule of thumb, not a standard: the thresholds below are listed so that you can judge
them. The values are fixed in the program, not settings.

## What is checked

### File and sweep layout

| id | What | Rule | Severity |
|---|---|---|---|
| `scan_type` | PPI or RHI volume; elevation range | `sweep_mode`, else the way azimuth and elevation change along the rays | info |
| `partial_coverage` | Sector scan or missing rays | largest gap in the sorted ray azimuths of a PPI sweep is at least 10 deg | notice; warning when other sweeps of the file are full circles (then rays were probably lost) |
| `time_order` | Ray times not increasing | a few (at most 5 % of the rays) backward steps, or a few repeated times. One backward step in an otherwise increasing series is a rotated ray order (xradar stores NEXRAD rays from azimuth 0) and is not flagged; many backward steps mean the rays are stored by azimuth, not by time, and are not flagged either | warning |
| `time_gap` | Pause in the ray times | a ray interval above max(5 x the median interval, 1 s), judged on the time-sorted series; missing rays are estimated as gap / median interval | notice; warning when more than 5 % of the rays are missing |
| `time_resolution` | Coarse time stamps | more than 5 % of the rays repeat the previous time (whole-second stamps) | info |
| `time_missing`, `sweep_time_order` | Rays without a time; sweeps not in time order | count of NaT; start times against sweep numbers | notice |
| `split_cut` | NEXRAD-style split cuts | a sweep with only velocity and a sweep with only dual-pol moments at the same fixed angle (within 0.3 deg) | info |

### Fields and units

| id | What | Rule | Severity |
|---|---|---|---|
| `no_reflectivity` | No reflectivity resolves through the {doc}`variable mapping <VARMAP>` | role search on every sweep | warning |
| `velocity_without_width`, `width_without_velocity` | Velocity without spectrum width (or the reverse) | role inventory | notice |
| `zdr_without_rhohv` | ZDR but no correlation coefficient | role inventory | warning |
| `phidp_without_rhohv` | PhiDP but no RhoHV | role inventory | notice |
| `empty_field` | A listed variable with no valid gate on a sweep | all gates NaN after decoding | info when a sweep at the same elevation has the field (split-cut padding), else notice |
| `units_<role>` | Values or units that do not fit the field | 0.1 and 99.9 percentiles against the usual range of the role (reflectivity -60..95 dBZ, velocity +-120 m/s, width up to 40 m/s, ZDR -10..14 dB, RhoHV -0.02..1.08, PhiDP 0..360 deg; "hard" bounds make it a warning, RhoHV above 1.2 too); a unit string of another class; PhiDP that never exceeds 2 pi (radians?) | notice / warning |
| `nyquist_mismatch` | Velocities above the Nyquist velocity stated in the file | max abs(V) > 1.05 x the file's Nyquist velocity | warning |

### Doppler velocity

| id | What | Rule |
|---|---|---|
| `velocity_aliasing` | Folded (aliased) velocity | share of gate-to-gate velocity steps (along azimuth and range) above 1.2 Nyquist velocities. This is the detector of the multi-Doppler retrieval (`multidoppler._jump_fraction`), reused unchanged: a folded field jumps by about 2 Nyquist velocities across fold boundaries (Eilts and Smith 1990; Doviak and Zrnic 1993). |
| | thresholds | warning at 0.3 % of the steps, notice from 0.06 %; at least 50 estimated jumps and 500 valid gates are required. |
| | Nyquist velocity | from the file (**RETRIEVED**) or, when the file has none, the largest abs(V) rounded up to 0.5 m/s (**ESTIMATE**; exact for aliased data, too small otherwise, so confidence drops from 0.85 to 0.65). |
| | where | the gates next to a jump (the fold boundaries). |
| `range_folding` | Gates flagged "range folded" | count of the Level II code 1 before it is masked (Doviak and Zrnic 1993; NEXRAD ICD 2620010) as a share of the gates that carry a signal: notice from 2 %, warning from 10 %. Only integer-encoded moments carry the code; other formats are listed under *Not assessed*. Folded gates hold no measurement and cannot be recovered. |

### Sensitivity and echo classes

| id | What | Rule |
|---|---|---|
| `noise_floor` | Minimum detectable reflectivity versus range | 2nd percentile of reflectivity in 16 log-spaced range bins from 10 km; for a noise-limited receiver the weakest detectable echo grows as 20 log10(r) (radar equation, Doviak and Zrnic 1993). Accepted only when the envelope has a slope of 20 +- 10 dB/decade, a scatter below 4 dB and at least 8 bins. Reported at 50 and 100 km as **ESTIMATE**, an upper bound of the true value (the signal processor's own SNR threshold is included). Notice when the envelope at 50 km is above 10 dBZ. Without a clear noise law nothing is reported. |
| `ground_clutter` | Persistent zero-velocity strong echo | gates with abs(V) <= 1 m/s and Z >= 20 dBZ below 100 km range on sweeps up to 3 deg; counted per 5 deg x 5 km cell; cells where at least half of the gates qualify (and at least 15 gates) are persistent. An azimuth bin that is clutter-like at most of its ranges is a zero-isodop line of real weather and is dropped. After Hubbert et al. 2009 and Steiner and Smith 2002. Reported from 0.2 % of the echo gates; warning from 3 %. Calm-wind widespread rain can look like clutter. |
| `beam_blockage` | Azimuthal reflectivity deficit (proxy) | per-degree share of gates with echo between 20 and 150 km on full-circle sweeps up to 6 deg, against the flanks 8-30 deg away on both sides; a wedge 2-45 deg wide with at most a quarter of the flank echo (flanks with at least 20 % echo) is flagged. Never above notice and confidence at most 0.55: a gap in the precipitation looks the same and **no terrain model is used** (Bech et al. 2003 for partial blockage itself). Confirm with the radarx beam-blockage module. |
| `biological_echo` | Birds, insects, clear-air scatter | the rule of the pipeline's `biological_filter` at its defaults: Z <= 25 dBZ with RhoHV < 0.8 or ZDR > 3 dB (Zrnic and Ryzhkov 1998; Gourley et al. 2007). Reported from 2 % of the echo gates, notice from 15 %. An upper bound: noisy weak rain also has low RhoHV. |
| `speckle` | Isolated single gates | a valid reflectivity gate whose 8 neighbours are all invalid; notice from 10 %. |
| `low_snr` | Noise-dominated gates | share of gates with SNR < 3 dB (only when the file has an SNR field); notice from 10 %. |
| `low_rhohv` | Non-rain-like gates in ZDR | share of ZDR gates with RhoHV < 0.85; notice from 10 %. |

### Dual-polarisation calibration and phase

| id | What | Rule |
|---|---|---|
| `zdr_offset` | ZDR calibration offset | **Light-rain method** (Ryzhkov et al. 2005; Bringi and Chandrasekar 2001). Gates with 20 <= Z < 25 dBZ, RhoHV >= 0.98, abs(ZDR) < 3 dB, 10-110 km, beam centre below 2 km (under a typical melting layer), elevation <= 10 deg and smooth ZDR along range have an intrinsic ZDR of about 0.3 dB (+- 0.1 dB systematic, S band). Offset = median of 5-gate block means minus 0.3 dB. Standard error = 1.2533 x 1.4826 MAD / sqrt(blocks); the total 1-sigma adds the 0.1 dB in quadrature. **Refused** (no number is given) below 3000 gates, 300 independent blocks or 30 rays. Warning when abs(offset) >= 0.25 dB and at least twice the uncertainty; notice from 0.1 dB and at least once the uncertainty; otherwise info ("consistent with no offset"). At C and X band differential attenuation and resonance bias the reference. |
| `phidp_noise` | Noisy PhiDP | 1.4826 x MAD of the wrapped gate-to-gate differences / sqrt(2) in rain gates (RhoHV >= 0.9, Z >= 15 dBZ; Hubbert and Bringi 1995); at least 2000 gate pairs. Notice from 8 deg, warning from 15 deg (rules of thumb). |
| `phidp_unfold` | PhiDP wraps around 360 deg | rays with a gate-to-gate step above 180 deg in those gates; notice from 1 % of the rays. |
| `attenuation` | Rain attenuation likely | the 95th percentile over rays of the summed unwrapped PhiDP steps, times 0.08 dB/deg at C band (Bringi et al. 1990; Carey et al. 2000) or 0.28 dB/deg at X band (Park et al. 2005); an **ESTIMATE**, uncertain by about a factor two. Notice from 3 dB, warning from 10 dB. The band comes from the file's frequency or wavelength, or from the format (NEXRAD is S band, where attenuation is negligible and nothing is reported); an unknown band gives no number. |

## The suggestion: recipe and first view

Every finding that motivates a pipeline step (`dealias`, `clutter_filter`, `biological_filter`,
`speckle`, `snr_gate`, `rhohv_gate`) at *notice* or above adds that step, with the finding as the
reason, to a recipe for the matching variable. Steps come out in a safe order (gates are removed
before the velocity is de-aliased). The result is up to three recipes, named *Data check -
Doppler velocity*, *... Reflectivity*, *... ZDR*; they are ordinary recipes (parameters at their
defaults), so *Review in Pipelines* opens them for editing and `rxd pipeline recipe.json` runs
them. Problems no pipeline step fixes (ZDR offset, PhiDP unfolding, attenuation, range folding)
are listed as *Other things to do*.

The first view is the lowest sweep with reflectivity (else the next best variable), with a colour
range from the data (0.1 to 99.9 percentile, rounded to 5 dBZ) or, for velocity, plus and minus
the Nyquist velocity. *Show it* selects that sweep and variable; the colour range is advice and is
not applied.

## Command line

```console
$ rxd check -i KTLX20130520_201643_V06.gz
KTLX20130520_201643_V06.gz  (nexrad_level2, 17 sweeps, S band; checked in 1.0 s)
  1 warning, 5 notice, 3 info. Nothing was changed; ESTIMATE values name their method.

  WARNING Velocity looks aliased (folded)   (confidence 0.65; sweeps 1, 3, 5 (+11 more); azimuth ...)
    sweeps 1, 3, 5 ...: 4.17 % of gate-to-gate velocity steps exceed 1.2 x the Nyquist velocity ...
      - steps above 1.2 x Nyquist 4.17 % [RETRIEVED]
      - Nyquist velocity 26 m/s [ESTIMATE]
  ...
$ rxd check -i FILE --fix-plan -o recipe.json     # also writes the suggested recipe(s) as JSON
$ rxd pipeline recipe_velocity.json -i FILE       # run it yourself
$ rxd check -i A B C --json                       # a JSON list, one report per file (also for one file)
```

Options follow the {doc}`shared options <CLI>`: `-i`, `--json`, `-v` (adds each finding's method and
the facts' evidence), `-q`, `--config`. `-o` is only meaningful with `--fix-plan`. Exit codes: **0
even when problems are found**; 3 when a file cannot be read; 2 for wrong usage. With several files
the others are still checked and the exit code is 3 if any failed.

## Cost and limits

* **Time** (this machine, warm cache): KTLX NEXRAD volume 0.5 s (about 1.0 s the first time the
  lazily decoded arrays are touched), KVWX 0.5-0.9 s, a TDWR volume 0.3 s; a 115 MB CfRadial
  volume took 4 s because reading the file dominates, which is why a wall-clock budget (4 s) skips
  the pixel checks of the remaining sweeps and says so. Pixel checks look only at the lowest sweeps
  (at most 2-6 per check).
* **Memory:** one sweep at a time; about 50 MB above the volume.
* **Single volume:** persistence over time (the classic clutter map) and the melting layer height
  are not known, so the ZDR check assumes a beam below 2 km and the clutter check relies on
  persistence over azimuth.
* **No external data:** no terrain model (blockage is a proxy), no sounding, no network.
* **Thresholds are rules of thumb**, tuned on S-band NEXRAD data; on C/X band and other formats
  treat them as starting points. The report states what could not be assessed under *Not
  assessed* instead of guessing.
* **Synthetic validation:** the check was tested by injecting velocity aliasing, range-folded gates, a missing
  field, a time gap, a partial sector, a clutter ring, a blocked wedge and a +0.5 dB ZDR offset
  into otherwise clean volumes and requiring them to be found where they were put; clean data must
  raise nothing above *info*. A real KTLX volume is checked for the known facts (split cuts,
  velocity on odd sweeps, an estimated Nyquist velocity near 26 m/s).

## References

* Bech, J., Codina, B., Lorente, J., Bebbington, D., 2003: The sensitivity of single polarization weather radar beam blockage correction to variability in the vertical refractivity gradient. J. Atmos. Oceanic Technol. 20, 845-855.
* Bringi, V. N., Chandrasekar, V., Balakrishnan, N., Zrnic, D. S., 1990: An examination of propagation effects in rainfall on radar measurements at microwave frequencies. J. Atmos. Oceanic Technol. 7, 829-840.
* Bringi, V. N., Chandrasekar, V., 2001: Polarimetric Doppler Weather Radar. Cambridge University Press.
* Carey, L. D., Rutledge, S. A., Ahijevych, D. A., Keenan, T. D., 2000: Correcting propagation effects in C-band polarimetric radar observations of tropical convection using differential propagation phase. J. Appl. Meteor. 39, 1405-1433.
* Doviak, R. J., Zrnic, D. S., 1993: Doppler Radar and Weather Observations, 2nd ed. Academic Press.
* Eilts, M. D., Smith, S. D., 1990: Efficient dealiasing of Doppler velocities using local environment constraints. J. Atmos. Oceanic Technol. 7, 118-128.
* Gourley, J. J., Tabary, P., du Chatelet, J. P., 2007: A fuzzy logic algorithm for the separation of precipitating from nonprecipitating echoes using polarimetric radar observations. J. Atmos. Oceanic Technol. 24, 1439-1451.
* Hubbert, J. C., Bringi, V. N., 1995: An iterative filtering technique for the analysis of copolar differential phase and dual-frequency radar measurements. J. Atmos. Oceanic Technol. 12, 643-648.
* Hubbert, J. C., Dixon, M., Ellis, S. M., Meymaris, G., 2009: Weather radar ground clutter. Part I: Identification, modeling, and simulation. J. Atmos. Oceanic Technol. 26, 1165-1180.
* Park, S.-G., Bringi, V. N., Chandrasekar, V., Maki, M., Iwanami, K., 2005: Correction of radar reflectivity and differential reflectivity for rain attenuation at X band. Part I: Theoretical and empirical basis. J. Atmos. Oceanic Technol. 22, 1621-1632.
* Ryzhkov, A. V., Giangrande, S. E., Melnikov, V. M., Schuur, T. J., 2005: Calibration issues of dual-polarization radar measurements. J. Atmos. Oceanic Technol. 22, 1138-1155.
* Steiner, M., Smith, J. A., 2002: Use of three-dimensional reflectivity structure for automated detection and removal of nonprecipitating echoes in radar data. J. Atmos. Oceanic Technol. 19, 673-686.
* Zrnic, D. S., Ryzhkov, A. V., 1998: Observations of insects and birds with polarimetric radar. IEEE Trans. Geosci. Remote Sens. 36, 661-668.
