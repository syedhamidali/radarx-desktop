# Processing pipelines (recipes)

A **pipeline** chains several cleaning / processing steps on one variable and returns **one**
result variable, instead of one derived variable per step.

```{image} images/ui/pipeline-preview.png
:alt: Previewing the Doppler-velocity recipe: the cleaned velocity on the map and the pipeline panel
:width: 100%
```

*Doppler velocity: dealias + clutter + bio filter* previewed on a KTLX Doppler sweep: the map shows the cleaned
velocity (green toward the radar, red away) and the panel lists the steps with the number of gates each removed.


> "Apply de-aliasing, then a clutter filter, then a biological filter, then another filter, and
> then use that result for wind analysis."

Open the **Pipelines** panel, choose the target variable (for example `VRADH`), add the steps in
order, press **Preview** to look at one sweep, then **Run on all sweeps**. A single variable
`pipeline:<recipe>_<target>` appears in the variable list (2D and 3D) and can be used by modules
and exports like any other variable.

## How a recipe works

A step works on a shared **working state** of one sweep: the fields it needs (`DBZH`, `VRADH`,
`ZDR`, `RHOHV`, ...) plus one keep/remove **mask** (`True` = keep).

| Kind | What it does | Steps |
|---|---|---|
| filter | only shrinks the mask | SNR gate, RhoHV gate, echo mask, clutter filter, biological filter, speckle, despike, value / range limits |
| transform | changes values | dealias (velocity), smooth, gap fill (re-admits gates), noise correction (ZDR / RhoHV) |

At the end the mask is applied to the target. **Order matters**:

* clutter / noise filters before `dealias` give the unfolding a cleaner field (dealiasing only
  sees the gates kept so far);
* `gap_fill` only fills gates removed *before* it, and a filter after it can remove the filled
  gates again.

A step that does not apply to a sweep (no SNR field, noise correction on a reflectivity target)
is **skipped and noted**, not an error. A sweep that lacks a *required* role (the target, or the
`needs` of an enabled step such as the velocity for `dealias`) is skipped with a reason, exactly
like "Skipped N sweeps" for modules. If a step fails, the sweep is skipped and the message names
the step.

### Split cuts (NEXRAD)

Doppler cuts have reflectivity and velocity but no RhoHV / ZDR. For filters that can use them,
the fields are taken from the other cut at the same elevation (within 0.1 degrees) and mapped to
the sweep's grid, so the biological filter and the echo mask work on velocity sweeps too. The
preview says where a field came from ("RHOHV taken from sweep 0").

## Step catalog

| id | Needs / uses | Notes |
|---|---|---|
| `snr_gate` | SNR | remove gates below a minimum SNR |
| `rhohv_gate` | RhoHV | remove gates below a minimum correlation |
| `echo_mask` | DBZH; uses ZDR, RhoHV, PhiDP, VRADH, SNR | radarx `echo_mask` (radarx / krause2016 / gourley2007); simple rule if radarx is missing |
| `clutter_filter` | DBZH; uses VRADH | texture (TDBZ) and SPIN of reflectivity (Hubbert et al. 2009, Steiner and Smith 2002); only near-zero velocities when a velocity field exists; `fuzzy` = radarx gourley2007 |
| `biological_filter` | DBZH; uses RhoHV, ZDR, spectrum width | rule based: weak echo with low RhoHV / high ZDR / wide spectrum. radarx's MistNet (`biological_echo`) needs a whole volume and an ONNX model and is **not** used |
| `speckle` | - | small isolated regions (8-connected, joined across 0/360 degrees) |
| `despike` | - | single gates far from the mean *and* median of their neighbours; limit in target units or automatic |
| `range_clip` | - | value interval and/or range window (-1000 / 1000 / 0 = off) |
| `dealias` | VRADH (required) | radarx `region` or `unravel`; `builtin` numpy unfolding; Nyquist from the file, the parameter, or the largest valid velocity; optional fold reference (`none` / `zero` / `vad`, see Limits) |
| `smooth` | - | NaN-aware median or Gaussian on kept gates |
| `gap_fill` | - | smooth interpolation into small removed patches |
| `noise_correction` | SNR; target ZDR or RhoHV | radarx `correct_zdr_noise` / `correct_rhohv_noise`; only for moments that still contain the noise |

Every step has a parameter form in the panel; values are validated and clamped.

## Results and provenance

* **Preview** runs one sweep and stores nothing. It shows per-step gates
  removed and the % of valid gates left, and three grids on the PPI: **Before**, **After** and
  **Removed by** (which step removed each gate).
* **Run** streams progress, can be cancelled (it stops after the
  current step) and stores exactly one derived variable named
  `pipeline:<recipe_slug>_<target>` (a derived name allows one colon). Running again replaces it.
  The option *apply the mask to all loaded fields* additionally stores a masked copy of every
  other field the recipe loaded (`pipeline:<slug>_<role>`).
* The recipe (name, target, steps and parameters) is the variable's `params`; when exported it is
  written to the `comment` attribute of the NetCDF/Zarr variable.

## Using the result downstream

A pipeline result can replace a measured variable as the input of a module: for example the cleaned velocity can be the
velocity that a wind module uses. Wherever the module asks for the velocity (`VRADH`) it receives the pipeline output.
The roles are the canonical names or their aliases (`velocity`, `reflectivity`, `ZDR`, ...). This works for single-sweep
and all-sweeps runs; a sweep the derived variable does not cover is skipped with a reason, it is never silently
replaced by raw data.

## Saving, loading, sharing

* Recipes are JSON files in `~/.xradar-desktop/recipes/`:
  `{"version": 1, "name", "description", "target", "apply_to_all", "steps": [{"step_id",
  "params", "enabled"}]}`. Files that fail to parse are listed in the panel, never fatal.
* Three recipes ship built in (read-only, marked `*`): **Doppler velocity: dealias + clutter + bio
  filter**, **Reflectivity QC**, **Dual-pol QC**. Save a modified copy under another name.
* **Export script** writes a standalone Python file (numpy, scipy, xarray, xradar; radarx
  optional) with the exact kernels the app uses, for reproducibility:
  `python recipe.py input_file output.nc`.

## Limits

* Gap-filled and smoothed values are estimates, not measurements.
* The built-in `dealias` is weaker than radarx's; prefer radarx for strongly sheared flow.
* The default `region` de-aliasing can fail on difficult scans (strong shear, a mesocyclone, much
  missing or noisy velocity): look for patches folded by twice the Nyquist velocity. The alternative is
  UNRAVEL (Louf et al. 2020): Method `unravel` in the app, `-P dealias.method=unravel` on the command
  line. It is slower and can itself leave small suspect clusters of wrongly unfolded gates, so check the
  result in both cases.
* **The whole sweep can be off by twice the Nyquist velocity.** Dealiasing makes neighbouring regions agree,
  but nothing in the folded data says which interval the sweep as a whole belongs to: a 25 m/s wind seen with
  a Nyquist velocity of 10 m/s can come out 20 m/s off at every gate (the region method of the QC panel does
  this on a synthetic uniform wind). The **Fold reference** parameter (default `none`, the sweep as dealiased)
  chooses that whole-sweep multiple from a stated assumption; it shifts every gate by the same amount and
  never changes the region structure:
  * `zero`: the along-beam wind averages to about zero around a full circle. That holds for a wind that is
    uniform or changes linearly across the scan (its radial component is a first harmonic of azimuth,
    Browning and Wexler 1968); divergence and fall speed add a few m/s at most. The same idea as Py-ART's
    `centered` option. It needs velocity echo in at least 33 of 36 10-degree sectors.
  * `vad`: the constant term of a uniform-wind VAD fit (Browning and Wexler 1968) to the 10-degree sector
    means is brought nearest zero. It allows gaps in the echo up to 180 degrees.
  * Both are **refused** for a sector scan or echo on one side of the radar (the step then leaves the sweep as
    dealiased and says so in its notes). They are wrong when the mean along-beam wind of the sweep really is
    more than one Nyquist velocity from zero, for example a strong, compact circulation filling the echo.
  On the command line: `-P dealias.reference=zero` (or `vad`). The same option is in the QC panel's velocity
  dealiasing. The default stays `none` until it can be checked against independent winds (a sounding, a
  model analysis or a second radar).
* **Fold check (always on).** Every dealiasing (the pipeline step, the QC panel's velocity dealiasing and the
  *Velocity Dealiasing* module) tests its own result and says so in its notes and in the run record; it never
  changes the data. Each 10 km range ring with echo around most of the circle gets a uniform-wind VAD fit
  (Browning and Wexler 1968) to the median of each 10-degree sector. In a correctly unfolded sweep the
  constant term of that fit is only the small divergence-like part of the wind, a few m/s; a ring whose
  constant is more than one Nyquist velocity from zero was unfolded to a wrong interval. The verdict is one of:
  * *passed*: under 10 % of the gates of the testable rings are affected.
  * *whole sweep off by k x 2 x Nyquist*: at least 90 % of the gates of the testable rings sit in rings with the
    same multiple, and those rings hold at least half of the sweep's gates. The message gives the shift that
    undoes it, which is what `Fold reference: vad` or `zero` applies. A warning is raised.
  * *inconsistent*: 10 % or more of the testable gates are affected but not as one whole-sweep multiple (or the
    testable rings hold under half of the gates), so parts of the sweep sit in different intervals, or the
    whole sweep is off but cannot be shown to be. No shift is suggested. A warning is raised; do not use that
    sweep for winds, shear or rotation without looking at it, and try UNRAVEL or the other method.
  * *not possible*: too little echo around the radar (sector scans, echo on one side).
  What it cannot see: a minority of wrongly unfolded gates (the median ignores up to about half of each
  sector), gates in rings that are not complete, and an offset that the wind itself really has.
  Measured on 133 Doppler cuts (0.5 to 8 degrees) of 12 NEXRAD volumes, the lowest cuts of the 2021-12-11
  tornado outbreak from four radars and three other days: the QC panel's region method gave no whole-sweep offset
  and one inconsistent cut; radarx's region method (the default of the step when radarx is installed) gave
  no whole-sweep offset and three inconsistent cuts, some of its results holding velocities of hundreds of
  thousands of m/s; the built-in unfolding gave three whole-sweep offsets and twelve inconsistent cuts. Five
  cuts could not be tested. Because no volume showed a whole-sweep offset that a single shift repairs
  (the offset of the issue needs a wind well above the Nyquist velocity, which these low-elevation cuts
  rarely have), the default of *Fold reference* stays `none`; the check is what protects you.
* The clutter and biological defaults are conservative starting points tuned on S-band NEXRAD
  data; check the preview ("Removed by") before trusting them on other radars.
