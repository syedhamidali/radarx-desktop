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

## Cleaning data (QC)

Cleaning means deciding, gate by gate, whether an echo is weather and hiding what is not. A
single-variable filter cannot do that well (e.g. "hide reflectivity where RhoHV, SNR or ZDR texture say
it is noise"), so the **QC** modules combine several fields and output cleaned copies plus a *QC flags*
map that shows why each gate was removed.

* **Clean radar data (radarx QC)** (`radarx_clean`, recommended): radarx's fuzzy-logic echo mask (RhoHV,
  ZDR, and the along-ray *texture* of ZDR / PhiDP / DBZH: weather is smooth, clutter and noise are not)
  plus an SNR gate and speckle removal. Keeps the interior of heavy rain that fixed thresholds punch holes
  in. ~50-100 ms per sweep. Optional noise-bias correction of ZDR / RhoHV (only for moments that still
  contain the noise). Needs `radarx`.
* **Clean radar data (quick thresholds)** (`qc_clean`): no-dependency fallback with fixed SNR / NCP / RhoHV /
  clutter / speckle thresholds.

**Every step is optional** (SNR gate, echo mask, speckle, gap fill, smoothing: each has its own switch;
with all of them off nothing is removed). **Gap filling** interpolates removed patches that are well
surrounded by clean data (normalised Gaussian, distance-weighted) and only where enough of the
neighbourhood is clean, so large noisy regions stay empty. Filled values are separate *Gap-filled ...*
outputs and are marked in the flag map; the plain *Cleaned ...* fields are never altered, and velocity is
never interpolated. The fill radius sets the largest patch it will bridge: ~0.6 km closes tiny holes,
2-3 km also bridges narrow blocked radials; larger values start to grow echo edges. Filled gates are
estimates, not measurements.

**Apply the mask to** (default *all fields*): the same flag map hides the gates in *every* polar field of the
sweep (e.g. KDP, PhiDP, SNR, WIDTH, the V-channel and "_F" variants), each as its own *Cleaned <name>*
output. Gap filling and smoothing stay on the main fields (reflectivity, ZDR, RhoHV); velocity and phase
fields are never interpolated. Cost: a file with ~25 fields adds ~25 derived variables per sweep (a 7-sweep
X-band volume is ~700 MB of derived data, the budget is 1 GB), so choose *main fields only* when you
just want the principal moments (~190 MB).

Outputs: *Cleaned / Gap-filled / Smoothed* reflectivity, ZDR and RhoHV, *Cleaned velocity*, a *Cleaned <name>*
for every other field (all-fields mode) and *QC flags* (categorical). Press **All sweeps -> 3D** to get them as variables for every sweep. Tune per radar.

Neither fixes partial beam blockage (radial streaks / dark wedges with ZDR spikes; use radarx
`beam_blockage` with a terrain model), ZDR calibration offsets, attenuation or range folding.

## Optional dependencies

Declare packages a module needs with `needs=["radarx"]` and import them **inside** the function.
If a package is missing the module is still listed, but greyed out with a `pip install` hint, and
refuses to run.

## radarx modules

With [radarx](https://github.com/syedhamidali/radarx) installed (`pip install "radarx>=0.5.1"`,
Python 3.12+; or `pip install -e ".[radarx]"` from `python/`) the app gains `radarx_*` modules:

| Module | Product |
|---|---|
| `radarx_clean` | cleaned reflectivity / ZDR / velocity / RhoHV + QC flag map (echo mask + SNR gate) |
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
```

## Notes

* A built-in module that imports `modules/_radar_utils.py` relies on that file being next
  to it. A single-file personal module should copy the few helpers it needs.
* Heavy math should be vectorized NumPy/SciPy. If a kernel is still slow after that, it is
  the right candidate for a native (Rust/PyO3) implementation behind the same module API.
