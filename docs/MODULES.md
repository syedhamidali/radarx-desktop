# Writing modules (plugins)

A module is **one Python file**. Drop it in a modules folder and it shows up in the
**Modules** panel with an auto-generated parameter form, a Run button, result
tables and a map overlay — no server or UI changes.

Built-in examples: `python/modules/storm_detection.py`, `cell_size_sort.py`, `zdr_arc.py`.
Start from `python/modules/_template.py`.

## Where modules are loaded from

| Location | Use |
|---|---|
| `python/modules/` | ships with the app |
| `~/.xradar-desktop/modules/` | personal modules |
| `$XRADAR_MODULES_DIR` (`:`-separated) | project/shared folders |

Later locations override earlier ones with the same `id`. A module that fails to
import is skipped and listed under *Load errors* in the panel; it never stops the
sidecar. Press **Reload** in the panel after editing a file.

> Modules run with the privileges of the app. Only install modules you trust.

## Anatomy

```python
from engine.plugins import module, param, Result

@module(id="hot_gates", title="Hot Gates", category="Demo",
        requires=["DBZH"],
        params=[param("threshold", "number", 50.0, "Threshold (dBZ)", min=0, max=80)])
def run(ctx, threshold):
    z = ctx.field("DBZH").values
    return Result().scalars({"Gates above": int((z >= threshold).sum())})
```

* `requires` — canonical names (`DBZH`, `ZDR`, `PHIDP`, `KDP`, `RHOHV`, `VRADH`, `WRADH`).
  Aliases such as `DBZ`, `DBZHC`, `differential_reflectivity` resolve automatically; the
  Run button is disabled with an explanation when a file lacks one.
* `params` — `number`, `int`, `bool`, `select` (needs `options`), `string`. Values are
  validated and clamped before your function sees them.
* `ctx` — `field(name)` (raises a clear error if missing), `find`/`has` for optional
  fields, `polar_coords()` → `(azimuth_deg, range_m)`, `dataset`, `sweep`, `progress(pct, msg)`.

## Results

Build a `Result` from any mix of blocks:

| Call | Renders as |
|---|---|
| `res.scalars({...})` | summary grid |
| `res.table(title, columns, rows, id_key=...)` | sortable table; clicking a row highlights the overlay feature with the same id |
| `res.overlay(title, [polygon(...), point(...), line(...)])` | drawn over the PPI. `frame="polar"` = `[azimuth_deg, range_m]`, `frame="xy"` = `[x_m, y_m]` |
| `res.field(name, array, units=, vmin=, vmax=, cmap=, categories=)` | a gridded product on the sweep's `(azimuth, range)` grid, shown on the PPI via **Show on PPI** (continuous with a gradient legend, or categorical with `categories=[{"value":1,"label":"Rain","color":"#3b82f6"}]`) |
| `res.text(body)` | notes / caveats |

NaN/Inf and numpy types are cleaned automatically. A field must have exactly the selected
sweep's `(n_azimuth, n_range)` shape (the server rejects anything else). In the panel, table
blocks have a **CSV** button and overlay blocks a **GeoJSON** button (needs a known radar site).

## Optional dependencies

Declare packages a module needs with `needs=["radarx"]` and import them **inside** the function.
If a package is missing the module is still listed, but greyed out with a `pip install` hint, and
refuses to run.

## radarx modules

With [radarx](https://github.com/syedhamidali/radarx) installed (`pip install "radarx>=0.5.1"`,
Python 3.12+; or `pip install -e ".[radarx]"` from `python/`) the app gains `radarx_*` modules:

| Module | Product |
|---|---|
| `radarx_echo_mask` | meteorological / non-meteorological / speckle field |
| `radarx_kdp` | KDP, processed PhiDP or uncertainty field |
| `radarx_hid` | hydrometeor classification (categorical field + class counts) |
| `radarx_qpe` | rain-rate field (Z, Z-ZDR, KDP or HID-blended relations) |
| `radarx_dealias` | dealiased velocity / fold-number field |
| `radarx_shear` | azimuthal shear / radial divergence (LLSD) field |
| `radarx_couplets` | rotation couplets: table + overlay |
| `radarx_zdr_arc` | ZDR arc and KDP-ZDR separation per storm (Wilson & Van Den Broeke 2021) |
| `radarx_zdr_column` | ZDR column depth and objects (Snyder et al. 2015) |
| `radarx_vil` | VIL, VIL density or echo top |
| `radarx_hail` | MESH / SHI / POSH / HKE field and hail cells |

Notes: freezing level and the -20 C level are parameters (a file does not carry them); the
Nyquist velocity is estimated when a file lacks it; on NEXRAD split cuts, sweep 0 holds dual-pol
and sweep 1 holds velocity, so velocity and dual-pol modules run on different sweeps.

## Seeing a module in 3D (derived variables)

A module normally runs on **one sweep** and shows as a flat 2D picture. For a per-sweep module that
returns a gridded product, **All sweeps -> 3D** (next to Run) runs it on every sweep that has the
inputs and registers each output field as a **derived variable** of the open file, e.g.
`radarx_hid:hydrometeor_class`. It then works like any variable: the 2D variable list (under
*Derived*), the 3D volume, sweep stepping, the probe and the legend (swatches for categorical
products). The result card has **View in 2D / View in 3D / Remove** buttons.

* Sweeps that lack a required field are skipped with a reason (NEXRAD split cuts: dual-pol is on
  some sweeps, velocity on others); the run only fails if every sweep fails.
* Whole-volume modules (`@module(..., scope="volume")`: VIL, hail, ZDR column, ZDR arc) cannot run
  per sweep; the default is `scope="sweep"`.
* Derived data is kept in memory per file (budget 1 GB, `XRADAR_DERIVED_MAX_MB`; least recently used
  variables are evicted whole). It is dropped when the file is closed, re-opened or processed.
* Only `get_sweep_data` serves derived names (so 2D, 3D and the probe work; the cross-section,
  statistics and export tools use real variables).
* Object results (storm cells, couplets, arcs) are not gridded fields and are not shown in 3D.

## Protocol (for tooling)

```
→ {type:"list_modules", reload?:bool}      ← {type:"modules_list", modules, errors, dirs}
→ {type:"run_module", module, sweep, params, file_id?}
                                           ← {type:"module_result", module, sweep, params, elapsed_ms, blocks}
                                           ← {type:"module_error", module, message}
                                           ← per field block: {type:"module_field", module, name, sweep,
                                             n_az, n_range, azimuth_bytes, range_bytes, values_bytes,
                                             vmin, vmax, units, max_range, chunks, file_id}
                                             followed by `chunks` binary frames [azimuth f32][range f32][values f32]
→ {type:"save_text_file", path, content, overwrite?}   ← {type:"file_saved", path} | {type:"error"}
→ {type:"run_module", module, sweep, params, file_id?, all_sweeps:true}
                                           ← progress ("<module>: sweep 4/16"), then
                                           ← {type:"module_volume_result", module, file_id, params, elapsed_ms,
                                             derived:[{name, label, field, units, vmin, vmax, cmap, categories,
                                             sweeps:[...], skipped:[{sweep, reason}]}], summaries:[{sweep, blocks_scalars}],
                                             evicted:[...]}  then  {type:"derived_variables", file_id, variables:[...]}
→ {type:"list_derived", file_id?} / {type:"clear_derived", file_id?, name?}  ← {type:"derived_variables", ...}
→ get_sweep_data with a derived name: same wire format, header adds derived:true, label, categories, cmap
```

## Notes

* A built-in module that imports `modules/_radar_utils.py` relies on that file being next
  to it. A single-file personal module should copy the few helpers it needs.
* Heavy math should be vectorized NumPy/SciPy. If a kernel is still slow after that, it is
  the right candidate for a native (Rust/PyO3) implementation behind the same module API.
