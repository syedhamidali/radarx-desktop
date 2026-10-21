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
| `dealias` | VRADH (required) | radarx `region` or `unravel`; `builtin` numpy unfolding; Nyquist from the file, the parameter, or the largest valid velocity |
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
* The clutter and biological defaults are conservative starting points tuned on S-band NEXRAD
  data; check the preview ("Removed by") before trusting them on other radars.
