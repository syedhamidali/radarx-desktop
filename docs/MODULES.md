# Modules

Algorithms are **modules**. Open the **Modules** panel: every module has a parameter form, a **Run** button, and shows
its result as summary values, sortable tables, an overlay on the PPI, a gridded field, or notes. A module whose
input is missing (for example no ZDR in the file) is greyed out and says what it needs; modules that need the
optional [radarx](https://radarx.readthedocs.io/en/latest/) library are listed with an install hint until it is
installed (see {doc}`installation`).

## What a result contains

| Block | Shown as |
|---|---|
| summary values | a grid of numbers |
| table | a sortable table; clicking a row highlights the overlay feature with the same id; a **CSV** button saves it |
| overlay | polygons, points and lines drawn over the PPI; a **GeoJSON** button saves it (needs a known radar site) |
| field | a gridded product on the sweep's azimuth-range grid, shown on the PPI with **Show on PPI**, continuous with a gradient legend or categorical with swatches |
| notes | caveats of the method |

NaN and infinite values are cleaned automatically. Aliases of field names (`DBZ`, `DBZHC`, `differential_reflectivity`,
...) resolve automatically, see {doc}`VARMAP`.

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

## radarx modules

With [radarx](https://radarx.readthedocs.io/en/latest/) installed (`pip install "radarx>=0.5.1"`, Python 3.12+) the
app gains the `radarx_*` modules:

| Module | Product |
|---|---|
| `radarx_clean` | cleaned reflectivity / ZDR / velocity / RhoHV + QC flag map (echo mask + SNR gate) |
| `radarx_echo_mask` | meteorological / non-meteorological / speckle field |
| `radarx_kdp` | KDP, processed PhiDP or uncertainty field |
| `radarx_hid` | hydrometeor classification (categorical field + class counts) |
| `radarx_qpe` | rain-rate field (Z, Z-ZDR, KDP or HID-blended relations) |
| `hydro_qpe` | hydrology: rain rate with a selectable relation, uncertainty, beam quality and the relation used ({doc}`HYDROLOGY`) |
| `hydro_basin_rate` | hydrology: area-weighted rain rate over basin polygons from one volume (GeoJSON / shapefile / GeoPackage) |
| `radarx_dealias` | dealiased velocity / fold-number field |
| `radarx_shear` | azimuthal shear / radial divergence (LLSD) field |
| `radarx_couplets` | rotation couplets: table + overlay |
| `radarx_rotation_track` | maximum low-level azimuthal shear over a sequence of volumes (swath) and linked couplet tracks ({doc}`ROTATION`) |
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
* Whole-volume modules (VIL, hail, ZDR column, ZDR arc) cannot run per sweep.
* Derived data is kept in memory per file (budget 1 GB; least recently used variables are evicted whole). It is dropped when the file is closed, re-opened or processed.
* Derived variables work in the 2D and 3D views and the probe; the cross-section and statistics tools use measured variables.
* Object results (storm cells, couplets, arcs) are not gridded fields and are not shown in 3D.

## Saving cleaned data

Derived variables can be written to disk with the rest of the volume: **Export → Data Export →
NetCDF / CfRadial2 / Zarr**. When the file has derived fields, two checkboxes appear:

- **Include derived fields** adds every derived variable to every sweep as a `(azimuth, range)`
  float32 variable. `:` is not allowed in NetCDF names, so `radarx_clean:cleaned_zdr` is written as
  `radarx_clean__cleaned_zdr`. Sweeps a variable was not computed for are filled with NaN. Categorical
  fields carry `flag_values` / `flag_meanings`; every field carries `long_name`, `units` and a
  `comment` naming the module and its parameters.
- **Include original moments**: untick it to save only the derived fields.

PNG/SVG/PDF/CSV exports and batch/animation exports accept derived names as ordinary variables.
Some files store the site latitude/longitude/altitude per ray, with fill values (NaN, `-9999`) on
some rays. They are exported as the single valid value, since a radar site has one position.

## More radarx modules

| Module | Category | Product | Needs |
|---|---|---|---|
| `radarx_kdp_column` | Storm | KDP columns above the freezing level (van Lier-Walqui et al. 2016): field, object table, outlines; KDP estimated from PHIDP if absent | radarx newer than 0.5.1 |
| `radarx_vvp_winds` | Kinematics | Wind speed/direction/divergence by height from Doppler velocity (VAD or VVP), bulk shear, text summary | VAD: 0.5.1; VVP: newer radarx |
| `radarx_beam_blockage` | QC | Partial / cumulative beam blockage, terrain height, terrain clutter; blocked-sector table | a local DEM file (NetCDF, `.asc`, GeoTIFF with rioxarray/rasterio); never downloads |
| `radarx_tornado_prob` | Hazards | TorNet CNN tornado probability; otherwise a labelled rotation-score heuristic (LLSD shear + couplets) | ML: `onnxruntime` + a local TorNet ONNX model |
| `radarx_melting_layer` | Dual-pol | Melting layer top/bottom (PPI, RD-QVP profile or gradient method); below/in/above field | gradient method: newer radarx |
| `radarx_qvp` | Dual-pol | Quasi-vertical profile (QVP or RD-QVP) of Z, ZDR, RhoHV, KDP: table, bright-band scalars | - |
| `radarx_zdr_calibration` | QC | ZDR bias from light rain, dry snow or vertical pointing, with the bias-corrected ZDR field | - |

Volume-scope modules (everything above except beam blockage and ZDR calibration) use the whole
volume and show their product on the selected sweep. A module whose radarx function is missing
from the installed version stays listed and explains which function it needs.

See also: {doc}`VARMAP` - how field names, CF names, overrides and paired sweeps resolve.
