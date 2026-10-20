# The Python API

Everything the app and the [`rxd` command line](CLI.md) do is available from a script or a Jupyter
notebook, without the GUI and without the WebSocket server. The names are the same as on the command line.

```python
from engine import api      # or:  import rxd        (rxd is a short alias of engine.api)
help(api)                   # a guide: Look / Clean / Analyse / Combine / Save
```

| Command line | Python | Returns |
|---|---|---|
| `rxd info -i FILE` | `api.info(file)` / `vol.info()` | `dict` |
| `rxd plot -i FILE -V velocity -p out.png` | `api.plot(file, "velocity", out="out.png")` / `vol.plot(...)` | matplotlib `Figure` |
| `rxd modules`, `rxd modules show ID` | `api.modules()`, `api.module(id)` | table of `dict`s |
| `rxd run MODULE -i FILE [-s N] -P k=v` | `api.run(module, file, sweep, k=v)` / `vol.run(...)` | `ModuleResult` |
| `rxd run MODULE -i FILE --all-sweeps` | `vol.run(module, all_sweeps=True)` | `ModuleResult` (derived variable on `vol`) |
| `rxd pipeline RECIPE -i FILE` | `api.pipeline(recipe, file)` / `vol.pipeline(...)` | `PipelineResult` |
| `rxd pipeline list / show` | `api.recipes()`, `api.recipe(name)` | table / `Recipe` |
| `rxd cells -i FILE [FILE ...]` | `api.cells(file)` / `api.track(files)` | `CellsResult` / `TrackingResult` |
| `rxd mosaic -i F1 F2 ... -o out.nc -p map.png` | `api.mosaic([F1, F2], out="out.nc", plot="map.png")` | `Mosaic` |
| `rxd winds -i F1 F2 ... -o w.nc -p w.png` | `api.winds([F1, F2], out="w.nc", plot="w.png")` | `Winds` |
| `rxd export -i FILE -o out.zarr` | `api.export(file, out="out.zarr")` / `vol.export(...)` | `pathlib.Path` |

Argument names match the options: `files` (`-i`), `out` (`-o`), `plot` (`-p`), `variable` (`-V`),
`sweep` (`-s`), keyword parameters (`-P`). Anywhere a volume is expected you may pass a file path or a `Volume`.
`sweep=None` means "the lowest sweep that has what is needed" (velocity is on sweep 1 of a NEXRAD split-cut volume).

## Sign in first

An account is required (free; see {doc}`ACCOUNTS`). `import rxd` always works, but every function that touches data
raises `rxd.AccountRequired` until a user is signed in, in the app or with `rxd login` in a terminal:

```python
import rxd

try:
    vol = rxd.open("KTLX20130520_201643_V06.gz")
except rxd.AccountRequired as exc:
    print(exc)            # "You are not signed in." ... how to sign in
    print(exc.reason)     # "no_session" | "invalid" | "expired" | "offline_expired" | "revoked" | "not_entitled"
```

The check reads the same session file as the app and `rxd` (it is renewed automatically; with no network it holds for
30 days after the last server check) and costs microseconds after the first call. `RADARX_AUTH_DISABLED=1` switches it
off for developers working from a source checkout; installers ignore that variable.

## A session

Run on the KTLX example volume of 20 May 2013 (outputs are real).

```python
from engine import api

vol = api.open("KTLX20130520_201643_V06.gz")
vol
```
```text
<Volume KTLX20130520_201643_V06.gz | nexrad_level2 | 17 sweeps | 0 derived>
```

`vol.tree` is the `xarray.DataTree` as xradar opened it; `vol.sweeps`, `vol.variables()`, `vol.info()`,
`vol.field("velocity", sweep=1)` (a masked `xarray.DataArray`), `vol.dataset(sweep)` read it.
Variable names go through the engine's variable mapping, so `"velocity"`, `"VRADH"` and `"VEL"` all work.

### Find and run an algorithm

```python
api.modules("Dual-pol")        # a table; a list of dicts underneath
```
```text
id                    category  scope   requires          parameters                                             usable
--------------------  --------  ------  ----------------  -----------------------------------------------------  ------
radarx_hid            Dual-pol  sweep   DBZH, ZDR         method, band, freezing_level_m, ml_thickness_m ...     yes
radarx_kdp            Dual-pol  sweep   PHIDP             output, method, band, min_rhohv ...                    yes
radarx_melting_layer  Dual-pol  volume  DBZH, ZDR, RHOHV  method, min_elevation, max_elevation, sector_deg ...   yes
radarx_qvp            Dual-pol  volume  DBZH, ZDR, RHOHV  method, qvp_min_elevation, qvp_elevation, rmax_km ...  yes
zdr_arc               Dual-pol  sweep   DBZH, ZDR         z_min, zdr_min, rhohv_min, min_elongation ...          yes
```

`api.module("radarx_hid")` prints the purpose and every parameter with its default.

```python
res = vol.run("radarx_hid", sweep=0, method="park")     # keyword parameters, checked against the module
res
```
```text
<ModuleResult radarx_hid | sweep 0 | 1179 ms | fields=['Hydrometeor class (radarx)'] | tables=['Class statistics']>
```

A `ModuleResult` gives you the module's output as Python objects:

```python
res.scalars          # {'Classified gates': 132634, 'Most common class': 'Light and moderate rain', ...}
res.table            # a pandas DataFrame (res.tables has every table by title)
```
```text
                     class  code  gates  share
0  Light and moderate rain     6  47934   36.1
1             Ice crystals     3  31875   24.0
2                 Dry snow     1  22606   17.0
...
```
```python
da = res.field()     # an xarray.DataArray on the sweep's (azimuth, range) grid, with units and flag_values
res.plot()           # a PPI figure (categorical fields get a legend)
res.to_dataset()     # all fields as an xarray.Dataset
res.save("hid.nc")   # or "hid.csv", "hid.json", "hid.png", or a folder
```

Wrong calls fail early with the fix:

```python
vol.run("radarx_hid", sweep=1)
# XrdArgumentError: Hydrometeor Classification (radarx) needs DBZH, ZDR, but sweep 1 has no ZDR
#   (it has: DBZH, VRADH, WRADH). Sweeps that have all of them: 0, 2, 4, ... Use sweep=0 ...
vol.run("radarx_hid", method="prak")
# XrdArgumentError: Module 'radarx_hid': parameter 'method': must be one of auto, park, dolan, thompson
```

### Every sweep: derived variables

```python
allres = vol.run("radarx_hid", all_sweeps=True)
allres
```
```text
<ModuleResult radarx_hid | all sweeps | 7262 ms | derived=['radarx_hid:hydrometeor_class'] | 3 sweep(s) skipped>
```

The result is stored as **one derived variable on this volume** (`vol.derived`), not in any global
state; `allres.skipped` lists the sweeps that had no ZDR and why. It is a variable like any other:
`vol.field("radarx_hid:hydrometeor_class", 2)`, `vol.plot("radarx_hid:hydrometeor_class", 2)`,
`vol.to_dataset(1, include_derived=True)`, `vol.export("netcdf", "out.nc", include_derived=True)`.

### Clean with a recipe and feed it on

```python
pipe = vol.pipeline("Doppler velocity", target="VRADH", all_sweeps=True)
pipe.steps[["title", "status", "removed", "modified", "valid_pct"]]
```
```text
                    title    status  removed  modified   valid_pct
0                SNR gate   skipped        0         0  100.000000
1   Ground clutter filter        ok     5242         0   99.613313
2  Biological echo filter        ok   173512         0   86.813827
3        Dealias velocity        ok        0     98334   86.813827
4          Remove speckle        ok    28329         0   84.724078
5         Fill small gaps  disabled        0         0   84.724078
```
```python
pipe.plot(sweep=1)                       # before / after
vol.pipeline("Reflectivity QC", params={"snr_gate.min_snr": 5, "gap_fill": False})   # change steps
shear = vol.run("radarx_shear", all_sweeps=True, inputs={"velocity": pipe.name})   # use the cleaned velocity
```

A recipe is a built-in or saved name (`api.recipes()`), the path of a recipe JSON file, or a dict
`{"name": ..., "target": "DBZH", "steps": [{"step_id": "speckle", "params": {"min_size": 8}}]}`.

### Cells, plots, files

```python
cells = vol.cells(threshold_dbz=40, min_size_km2=50)
cells.to_dataframe()[["id", "lat", "lon", "area_km2", "max_dbz"]].head(4)
```
```text
     id      lat      lon  area_km2  max_dbz
0  C001  35.4628 -97.4257     297.2     69.5
1  C002  36.9812 -96.1584     274.1     65.0
2  C003  34.6921 -97.6748     706.4     63.0
3  C004  34.0992 -98.2535     615.6     63.0
```
```python
vol.plot("velocity", out="velocity.png")                  # publication renderer; sweep 1 chosen for you
vol.export(out="ktlx.zarr", include_derived=True)         # netcdf / cfradial2 / zarr / csv / png / svg / pdf
```

## Several radars

Pass files or `Volume`s; paths are opened (and closed again) for you.

```python
files = ["KHPX20211211_015930_V06", "KNQA20211211_015611_V06", "KPAH20211211_015935_V06", "KVWX20211211_015522_V06"]

mos = api.mosaic(files, product="composite", variable="DBZH", out="mosaic.nc", plot="mosaic.png")
mos
mos.image            # numpy array (ny, nx); NaN where no radar measured an echo
mos.to_dataset()     # CF dataset with per-radar coverage / share / bias
mos.save("mosaic.png")    # PNG + .pgw (world file) + .prj, for QGIS / GDAL
```
```text
<Mosaic composite of DBZH | 686x788 @ 1000 m | 4 radar(s)>
```

```python
w = api.winds(files, method="analytic", extent=200, max_range_km=200, dx_m=2000,
              z_min_m=500, z_max_m=6000, dz_m=500, min_crossing_angle=20)
w
w.fields             # {'u': (z, y, x) array, 'v': ..., 'w': ..., 'speed': ...}
w.plot(level_km=2)   # map of one level with vectors
w.save(["winds.nc", "winds.png"], level_km=2)
```
```text
<Winds analytic | 201x201x12 grid | 4 radar(s)>
```

`extent` is a half-width in km (or `(x_min, x_max, y_min, y_max)`), `origin` is `(lat, lon)`; options that
the engine adds later (for example `dealias=`) pass through. Unknown names raise `XrdArgumentError` with a suggestion.

```python
trk = api.track(["KHPX20211211_015357_V06", "KHPX20211211_015930_V06", "KHPX20211211_020457_V06"], threshold_dbz=40)
trk.to_dataframe().head(3)     # one row per track; trk.cells_dataframe() has every cell; trk.plot()
```

## Rainfall and hydrology

```python
res = api.qpe(["KHPX20211211_015357_V06", "KHPX20211211_015930_V06", "KHPX20211211_020457_V06"], out="rain.nc")
res.stats()                      # max / mean depth, wet area, uncertainty_median_rel
res.accumulation                 # mm on res.grid (NaN where no radar covers); res.accumulation_sigma is its 1 sigma
res.provenance()["type"]         # 'RETRIEVED' (an estimate); 'ADJUSTED' after gauges
res = api.qpe(files, relation="tropical", freezing_level_m=3800, gauges="gauges.csv", adjust="ked",
              basins="watersheds.geojson", thresholds=[10, 25, 50], frequency="atlas14_6h.json")
res.validation["cross_validation"]      # leave-one-gauge-out skill
res.basin_series.summary()              # one dict per basin
vol.qpe()                               # the instantaneous rate of one volume (res.rate_last)
```

Files are passed as paths and opened one at a time. Options are the `rxd qpe` ones; unknown names raise
`XrdArgumentError` with a suggestion. Details, equations and limits: {doc}`HYDROLOGY`.

## Results at a glance

| Class | Main members |
|---|---|
| `QpeResult` | `accumulation`, `accumulation_sigma`, `rate_max`, `rate_last`, `valid_fraction`, `quality`, `flags`, `basin_series`, `validation`, `exceedance`, `stats()`, `provenance()`, `save()`, `plot()`, `to_dataset()` |
| `Volume` | `qpe()`, `tree`, `path`, `format`, `sweeps`, `info()`, `variables()`, `field()`, `dataset()`, `to_dataset()`, `run()`, `pipeline()`, `cells()`, `plot()`, `export()`, `derived`, `add_derived()`, `drop_derived()`, `default_sweep()` |
| `ModuleResult` | `scalars`, `table(s)`, `text`, `overlays`, `fields`, `derived`, `skipped`, `plot()`, `to_dataset()`, `save()`, `to_dict()` |
| `PipelineResult` | `steps` (DataFrame), `name`, `derived`, `sweeps`, `skipped`, `plot()`, `to_dataset()`, `save()` |
| `Mosaic` | `image`, `bbox`, `radars`, `stats`, `to_dataset()`, `save()`, `save_png()`, `save_netcdf()`, `plot()` |
| `Winds` | `fields`, `x`, `y`, `z`, `origin`, `radars`, `summary`, `to_dataset()`, `save()`, `plot()` |
| `CellsResult`, `TrackingResult` | `to_dataframe()`, `cells_dataframe()`, `backend`, `motion`, `save()`, `plot()` |

Every result has `.plot()` (a matplotlib figure, Agg backend, headless safe), `.save(...)` (the extension picks
the format) and `.to_dict()` (JSON-safe), exactly what the command line prints with `--json`.

## Errors

| Exception | Raised when | CLI exit code |
|---|---|---|
| `XrdArgumentError` (a `ValueError`) | unknown module / parameter / variable / recipe, bad value, sweep out of range | 2 |
| `XrdInputError` (an `XrdArgumentError`) | file missing, a folder, unreadable | 3 |
| `XrdDependencyError` | an optional package is missing (`pip install -U radarx`) | 4 |
| `XrdError` and the engine's own errors (`api.user_errors()`) | the processing failed | 1 |
| `AccountRequired` (an `Exception`, not an `XrdError`) | nobody is signed in, the sign-in has expired or lapsed offline, or the account is not allowed | 5 |

## Notes

* **No global state.** A `Volume` owns its derived variables; two volumes never share results. `api.mosaic` and
  `api.winds` do not use the app's result caches either.
* **Install.** `pip install -e python/` (editable) so the shipped modules are found; `pip install -U radarx` for
  the `radarx_*` modules, mosaics and winds. Your own modules: `XRADAR_MODULES_DIR`, then `api.reload_modules()`.
* **Notebooks.** Figures are returned, so the last expression of a cell displays them; `api.modules()` and
  `api.recipes()` render as tables.
