# Engine performance

Measured speed and memory of the Python engine (`python/engine`, `python/server.py`,
`python/modules`), what was changed because of the numbers, and how to re-measure.
Nothing here changes a result: every optimisation below is checked to give identical output
(`python/tests/test_perf_equivalence.py`, plus a before/after comparison of 36 outputs on real
volumes, see "Checking outputs").

## How it is measured

`python/benchmarks/bench_engine.py` (not part of the installed package) times each stage and
reports, per stage:

| column | meaning |
|---|---|
| `first ms` | the first call in the process (process-wide caches cold, lazy file data not yet read) |
| `warm ms` | best of the remaining repeats (per-data caches cleared, process-wide ones warm) |
| `cpu ms` | process CPU time of the best run; stays meaningful when the machine is busy |
| `peak MB` | extra memory of one more run: `tracemalloc` peak (Python and NumPy buffers); for `open_file` the RSS high-water mark (needs `psutil`) |
| `rss MB` | process high-water mark so far (cumulative, mostly useful at the end) |

```
cd python
python benchmarks/bench_engine.py                          # synthetic NEXRAD-sized volumes
python benchmarks/bench_engine.py FILE                     # one real volume (read only)
python benchmarks/bench_engine.py KHPX KPAH KVWX KNQA      # first file = main volume, all = mosaic
python benchmarks/bench_engine.py FILE --repeat 5 --json out.json
python benchmarks/bench_engine.py FILE --only open sweeps render   # just some stages
```

Stages: `startup` (cold `import server`, spawn to the `READY` line), `open`, `sweeps` (the wire
format of `get_sweep_data`, sweep switching, Arrow), `render` (PNG tiles), `volume` (3D),
`analysis` (cross-section, vertical profile, profile statistics), `qc`, `modules` (one sweep and
"all sweeps", QC recipes), `mosaic` (2 and 4 radars), `export` (PNG, CSV, NetCDF/CfRadial1,
CfRadial2, Zarr). Files are only read; exports go to a temporary folder that is removed.
With no file the stages run on a synthetic 14-sweep volume (720 x 1832 gates, integer-encoded
moments like Level II) and, for the mosaic, three more synthetic radars.

## Machine and data

| | |
|---|---|
| Machine | Apple M1 Pro, 10 cores, 16 GB, macOS 26.3 (arm64) |
| Software | Python 3.14.2, numpy 2.4.4, xarray 2026.4.0, xradar 0.12.0, scipy 1.17.1, radarx 0.5.1 |
| Data | NEXRAD Level II `KHPX20211211_015357_V06` (6.7 MB, 19 sweeps, split cuts, DBZH/ZDR/PHIDP/RHOHV/CCORH/VRADH/WRADH); mosaic: + `KPAH`, `KVWX`, `KNQA` of the same event |
| Caveat | The machine was shared with other jobs (load average 5 to 10) during the runs, so absolute numbers carry noise of roughly +-15 %. Before and after were run back to back with the same script and arguments; the `cpu ms` column agrees with the wall times. |

## Before and after

Warm ms (best of 3) and extra peak MB, `origin/integration/cli` (before) against `perf/engine`
(after).

| Stage | before ms | after ms | before MB | after MB |
|---|---:|---:|---:|---:|
| `import server` (cold process) | 660 | 385 | | |
| spawn to `READY` line | 758 | 439 | | |
| `open_file` (Level II, 19 sweeps) | 1741 | 1480 | 70 to 130 (RSS, noisy) | same |
| first sweep, wire format (`get_sweep_data`) | 38 (first) | 19 (first) | 25 | 25 |
| switch through 19 sweeps (wire format) | 103 | 94 | 25 | 25 |
| first PPI as PNG (800x600) | 197 (first) | 170 (first) | 25 | 25 |
| switch through 19 sweeps (PNG) | 417 | 353 | 38 | 38 |
| 3D volume, resolution 100 | 643 | 308 | 157 | 150 |
| 3D volume, resolution 200 | 2121 | 1488 | 182 | 182 |
| cross-section (200 points) | 440 | 86 | 74 | 32 |
| vertical profile | 470 | 87 | 74 | 32 |
| area-averaged profile (all sweeps) | 579 | 93 | 91 | 33 |
| QC: noise_removal + despeckle + remove_clutter | 154 | 146 | 152 | 152 |
| module `storm_detection`, sweep 0 | 1085 | 697 | 691 | 287 |
| module `radarx_hid`, sweep 0 | 522 | 150 | 587 | 127 |
| module `radarx_hid`, all sweeps | 8512 | 1494 | 6951 | 152 |
| QC recipe "Reflectivity QC", all sweeps | 10821 | 3034 | 8813 | 234 |
| QC recipe "dual-pol", all sweeps | 7450 | 1359 | 6958 | 200 |
| mosaic CAPPI, 2 radars | 1145 | 333 | 276 | 275 |
| mosaic CAPPI, 4 radars | 2564 | 899 | 577 | 577 |
| export PNG | 592 | 545 | 144 | 144 |
| export CSV | 425 | 406 | 28 | 28 |
| export NetCDF (CfRadial1) | 51782 | 2577 | 12551 | 3789 |
| export CfRadial2 | 466 | 532 | 32 | 32 |
| export Zarr | 2620 | 2886 | 31 | 31 |

Rows within the noise (unchanged on purpose, see "Measured and left alone"): `open_file`,
sweep switching, PNG render, the QC pipeline, PNG, CSV, CfRadial2 and Zarr export. The volume at
resolution 200 gains a fixed ~0.3 s (it no longer decodes the five moments it does not use); its
sampling is unchanged.

The "all sweeps" rows show the memory effect most clearly: the old module context copied every
moment of every sweep (330 MB of float64) for each sweep that was processed, and Python kept the
copies alive until the run ended.

## What changed, and why

Each item was found in a profile first.

1. **CfRadial1 / "NetCDF" export: 52 s to 2.6 s, 12.5 GB to 3.8 GB.**
   `xradar.io.export.to_cfradial1` merges the sweeps with `compat="no_conflicts"` after
   broadcasting the per-sweep scalars (`sweep_mode`, `prt_mode`, ...) along time; the merge then
   spends its time on string arrays that are dropped right afterwards. `_cfradial1_variables`
   in `exporter.py` drops them first (they are removed after the merge anyway) and falls back to
   xradar's own mapper if its private helpers move or anything fails. The written file is
   identical (checked with `Dataset.identical` on the real 997 MB file and in a unit test).
   *Since xradar 0.13 this workaround is gone:* its own sweep merge (`_combine_sweeps`, a
   positional `concat`) takes 1.6 s and 1.6 GB for `KHPX20211211_023231` (19 sweeps, 9720
   rays) against 40 s and 3.4 GB for the 0.12 merge, and the whole export peaks at 3.0 GB
   instead of 4.5 GB, so `exporter.py` uses it as it is.
2. **Mask only what is read.** Level II moments carry reserved "no echo / range folded" codes
   that are turned into NaN (`reader.mask_variable`). Code that reads one moment decoded and
   copied all five or six of every sweep. `get_sweep_dataset(only=[...])` and
   `mask_below_threshold(only=...)` restrict the work; used by the 3D volume, 3D and 2D
   cross-sections, vertical profile, profile statistics, mosaic and multi-Doppler loaders
   (cross-section, profile: 5x faster, half the memory).
3. **Lazy masking for modules.** `ModuleContext.datatree` (volume modules, QC recipes) masked the
   whole volume up front, every run, every sweep (0.7 to 0.85 s and 330 MB each time).
   `reader.lazy_mask_below_threshold` applies exactly the same masking when a moment is first
   read (an xarray lazily indexed array with a memory cache). A module now pays only for the
   moments it uses: all-sweeps module and recipe runs are 3 to 6 times faster and use about
   40 times less memory.
4. **Deferred `scipy.ndimage`.** It cost 130 to 190 ms of the ~650 ms CPU of `import server` and
   is needed only by QC, retrievals, tracking and the 3D volume (`engine/_lazy.py`; the
   background preload thread imports it after `READY`). Spawn to `READY` drops by about 320 ms
   in the final run (about 140 ms of it CPU, the rest is a quieter machine). `import server` no longer loads scipy,
   matplotlib, radarx, cmweather or xradar; pandas and pyarrow come with xarray.

Already lazy before this work and left that way: matplotlib (first export), radarx (first module
or mosaic), xradar and the palette library (preloaded in a thread after `READY`).

## Engine start-up (issue #97)

`python -X importtime` of `import server` (which the shell waits for before the app can talk to
the engine) showed where the rest of the time goes: `xarray` with `pandas` about 300 of 490 ms,
`h5netcdf`/`h5py`/`netCDF4` about 140 ms (imported by the HDF5 guard only to learn which classes
to watch), `numpy` 37 ms, `asyncio` 33 ms, Pillow about 25 ms (imported by the picture code).
Two of these did not need to be paid before `READY`:

1. **The HDF5 guard recognises its file openers by name** (`netCDF4.Dataset`, `h5netcdf.File`)
   instead of importing the libraries, so installing it no longer loads them.
2. **Pillow is imported when the first picture is drawn** (`engine/renderer.py`, the same
   `LazyModule` as for scipy).

Both are imported again by the background pre-import that already follows `READY`
(`server._preload_heavy_imports`), so the first open and the first picture pay nothing extra.

Measured back to back on the same machine, `origin/main` against this change
(`python scripts/perf/engine_startup.py`, 7 starts, medians; Apple M1 Pro, Python 3.14.2, a
loaded machine, so look at the ratios):

| | before | after |
|---|---:|---:|
| `import server`, CPU time (9 runs) | 599 to 622 ms | 418 ms |
| process start to the `READY` line | 723 ms | 517 ms |
| native libraries mapped at `READY` | 192 | 118 |
| resident memory at `READY` | 165 MB | 145 MB |
| libraries / memory 3 s later (after the pre-import) | 276 / 242 MB | 275 / 240 MB |

**What radarx costs (optional extra).** radarx is never imported at start-up (checked by
`test_importing_the_server_does_not_load_the_hdf5_libraries_or_pillow`). When the extra is
installed, the first `import radarx` takes about 790 ms of CPU (885 ms wall; radarx 0.5.1, same
machine), more than the whole engine start-up: `radarx.grid` (630 ms, mostly `scipy.interpolate`
and `scipy.optimize`) and `radarx.retrieve` (270 ms). That cost lands on the first radarx module,
mosaic or wind run of a session, and stays out of the way until then. Importing it in the
background after `READY` would hide it, at the price of CPU and memory for people who never run
a radarx module; not done.

Not changed, and why: `xarray` imports `pandas` itself (a hard dependency of both), and the
engine uses xarray in nearly every module, so about 300 ms remain in `import server`. Where
`pyarrow` is installed, pandas loads it as well (about 55 ms); the installers do not ship it.
The 154 libraries counted in `docs/PACKAGING_AUDIT.md` were 192 here with `pyarrow` and the full
development environment; the count after the pre-import is unchanged because the libraries are
still needed once the first file is opened.

## float32 decoding (opt-in)

`XRADAR_FLOAT32=1` keeps integer-encoded moments (Level II style: `value = offset + code * scale`)
as `float32` once they are decoded. The no-echo and range-folded codes are found on the exact
`float64` values first and only then is the result rounded, so the same gates are masked.
Measured on 6 synthetic Level-II-sized moments (720 x 1832 gates):

| | float64 (default) | float32 |
|---|---:|---:|
| resident decoded data | 63.3 MB | 31.7 MB |
| peak while decoding | 66.0 MB | 39.6 MB |
| time to decode and mask | 28 ms | 23 ms |

So a volume needs about half the memory for its decoded moments and decoding is about 15 %
faster. The end-to-end stages of `bench_engine.py` (PNG render, 3D volume, cross-section) are not
measurably faster (differences within the run-to-run noise), which is why it is not the default.

**Precision effect.** A float32 value differs from the float64 one by at most 6e-8 of its size:
below 5e-6 dB or m/s for any real moment, far below the 0.5 steps the radar stores (for steps of
0.5 or 0.25 the values are exactly the same). A gate lying exactly on a threshold of a module
can fall on the other side of it when its float64 value was a rounding error away from the
threshold. Sums over many gates are done by NumPy with pairwise summation (relative error about
1e-7); histograms and statistics convert to `float64` first. Outputs written while the option is
on carry `numeric_precision` in the file (and in the JSON of recipes and records) so a result
can be reproduced with the same setting. Tests: `python/tests/test_startup_and_float32.py`.

## Benchmark check in CI

`python scripts/perf/bench_check.py` runs about a minute on synthetic volumes (no data, no
network, no account): the median of three engine starts (time to `READY`, libraries mapped) and
the process CPU time of four engine stages. It compares them with
`python/benchmarks/ci_baseline.json` after dividing out the speed of the machine (a fixed
NumPy calibration run by the same interpreter), and fails only when something is more than 2.5
times its calibrated baseline (1.3 times for the library count): it catches "several times
slower", not noise. `--update` rewrites the baseline; do that in the change that upgrades Python
or a library. The optional workflow `.github/workflows/bench.yml` runs it on a pull request that
carries the `bench` label or by hand, allowed to fail, never required. The reference numbers
come from the machine above (Python 3.14, xradar 0.12) while CI uses Python 3.12, so a first CI
run may need a baseline refresh; the factor is deliberately wide for that reason.

## Measured and left alone

| What | Measurement | Why not changed |
|---|---|---|
| `open_file` | 1.5 to 1.7 s | over 95 % is inside xradar (bz2 decompression 0.8 s, record parsing 1.2 s); the engine adds < 2 % |
| first data read of a Level II file | ~1 s extra, once | the same parse, done lazily by xradar on the first moment |
| Zarr export | 2.6 s, hundreds of small files | filesystem-bound inside `zarr`/xarray |
| PNG export | 545 ms | 320 ms is matplotlib `draw_quad_mesh` of 1.3 M cells |
| CSV export | 406 ms | pandas formats 540 k floats with `%.4f`; a hand-written writer risks different line endings or digits for a 5 MB file |
| 3D volume, resolution 200 | 1.5 s | spread over `map_coordinates`, `interp` and chunk arithmetic, already chunked, no single waste |
| `storm_detection` VIL / echo top, `radarx_hid`, mosaic gridding | 0.5 to 0.9 s | radarx native code |
| QC `despeckle` | 125 ms | scipy rank filter + distance transform |
| `mask_variable` arithmetic | 3.1 ms per sweep | best alternative measured 2.5 ms: not worth the risk |
| `engine/arrow_bridge.py` | 6 to 40 ms | not used by the server (sweeps go over the binary wire format) |
| float32 for decoded moments | 330 MB resident per volume | now an opt-in engine setting (`XRADAR_FLOAT32=1`, see above); not the default because it changes values at the 1e-7 level and the speed gain is not visible end to end. An xradar-side option would additionally save the temporary float64 array while decoding |
| NetCDF peak 3.8 GB | | CfRadial1 pads every sweep to one rectangular (time x range) float64 grid (1 GB) and xarray's outer join holds a few copies |

## Checking outputs

* `python/tests/test_perf_equivalence.py` (14 tests): masking with `only=` and lazy masking equal
  the eager masking (values, dtype, attrs, coords, slicing, deep copy, arithmetic); the volume
  extracted with the narrowed loader equals the old one bit for bit; the CfRadial1 dataset
  carries xradar's own sweep merge; `import server` does not import
  `scipy.ndimage` until QC needs it.
* One-off comparison on the real KHPX/KPAH volumes between the old and new code: 3D volumes of
  DBZH and VRADH, cross-section, vertical profile, profile and histogram statistics, a 2-radar
  mosaic (image, per-radar grids, PNG bytes, statistics), all 23 modules on sweep 0 (payload JSON
  and field arrays) and the derived variables of the "Reflectivity QC" and "dual-pol" recipes and
  `radarx_hid` on all sweeps: all 36 items identical, `numpy.array_equal` including NaN
  positions. The 997 MB CfRadial1 file was compared variable by variable with
  `Dataset.identical`.
