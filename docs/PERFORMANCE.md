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
| float32 for decoded moments | 330 MB resident per volume | xradar decodes to float64; float32 would change values at the 1e-7 level, so it needs an xradar-side option |
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
