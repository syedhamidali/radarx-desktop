# Processing pipelines (recipes)

A **pipeline** chains several cleaning / processing steps on one variable and returns **one**
result variable, instead of one derived variable per step.

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
| `gap_fill` | - | smooth interpolation into small removed patches (reuses `modules/_gapfill.py`) |
| `noise_correction` | SNR; target ZDR or RhoHV | radarx `correct_zdr_noise` / `correct_rhohv_noise`; only for moments that still contain the noise |

Parameters are declared with the same `param(...)` helper as modules (`number`, `int`, `bool`,
`select`, `string`; validated and clamped), and the panel builds the forms from them.

## Results and provenance

* **Preview** (`preview_pipeline`) runs one sweep and stores nothing. It shows per-step gates
  removed and the % of valid gates left, and three grids on the PPI: **Before**, **After** and
  **Removed by** (which step removed each gate).
* **Run** (`run_pipeline`) streams progress, can be cancelled (`cancel_pipeline`, stops after the
  current step) and stores exactly one derived variable named
  `pipeline:<recipe_slug>_<target>` (a derived name allows one colon). Running again replaces it.
  The option *apply the mask to all loaded fields* additionally stores a masked copy of every
  other field the recipe loaded (`pipeline:<slug>_<role>`).
* The recipe (name, target, steps and parameters) is the variable's `params`; when exported it is
  written to the `comment` attribute of the NetCDF/Zarr variable.

## Using the result downstream

`run_module` accepts `inputs`, a map from a role to a derived variable:

```json
{"type": "run_module", "module": "some_wind_module", "sweep": 1,
 "inputs": {"velocity": "pipeline:doppler_velocity_dealias_clutter_bio_filter_vradh"}}
```

Wherever the module asks for `VRADH` (through `ctx.field`, `ctx.find` or by reading
`ctx.dataset`) it receives the pipeline output. Keys are canonical names or aliases
(`velocity`, `reflectivity`, `ZDR`, ...). Works for single-sweep and `all_sweeps` runs; a sweep
the derived variable does not cover is skipped with a reason, it is never silently replaced by
raw data.

Implementation (kept small on purpose): `ModuleContext(overrides=...)` in `engine/plugins.py`
maps a canonical role to `fn(sweep) -> 2-D array`; the context applies it when it loads the sweep
dataset (`_apply_overrides`). `engine.pipeline.module_overrides(store, inputs)` builds the map.

**Variable mapping.** Roles are resolved in exactly one function,
`engine.pipeline.find_variable(ds, role)`. When the user-editable mapping (`engine/varmap.py`)
lands, make that function call it; the override map is keyed by canonical role, so it keeps
working with the mapping unchanged (the override replaces whichever file variable the mapping
picks for the role).

## Saving, loading, sharing

* Recipes are JSON files in `~/.xradar-desktop/recipes/` (`$XRADAR_RECIPES_DIR` overrides it):
  `{"version": 1, "name", "description", "target", "apply_to_all", "steps": [{"step_id",
  "params", "enabled"}]}`. Files that fail to parse are listed in the panel, never fatal.
* Three recipes ship built in (read-only, marked `*`): **Doppler velocity: dealias + clutter + bio
  filter**, **Reflectivity QC**, **Dual-pol QC**. Save a modified copy under another name.
* **Export script** writes a standalone Python file (numpy, scipy, xarray, xradar; radarx
  optional) with `process_sweep(ds)` and the exact kernels the app uses, for reproducibility:
  `python recipe.py input_file output.nc`.

## Protocol summary

| Request | Response |
|---|---|
| `list_pipeline_steps` | `pipeline_steps {steps}` |
| `list_recipes` | `recipes_list {recipes, errors, dir}` |
| `save_recipe {recipe}` / `delete_recipe {name}` | `recipe_saved` / `recipe_deleted`, then `recipes_list` |
| `export_recipe_script {recipe}` | `recipe_script {filename, script}` |
| `preview_pipeline {file_id, sweep, recipe}` | `pipeline_preview {steps, blocks, notes, ...}` + 3 `module_field` binary payloads (module `pipeline`) |
| `run_pipeline {file_id, recipe, all_sweeps, sweep}` | progress, `pipeline_result {name, derived, steps, skipped, ...}`, `derived_variables` |
| `cancel_pipeline {file_id}` | the run ends with `pipeline_error {cancelled: true}` |
| any failure | `pipeline_error {action, message}` |

## Limits

* Gap-filled and smoothed values are estimates, not measurements.
* The built-in `dealias` is weaker than radarx's; prefer radarx for strongly sheared flow.
* The clutter and biological defaults are conservative starting points tuned on S-band NEXRAD
  data; check the preview ("Removed by") before trusting them on other radars.
* Code: `python/engine/pipeline.py` (engine, recipes, persistence, script export),
  `python/engine/pipeline_steps.py` (catalog), `python/tests/test_pipeline*.py`.
