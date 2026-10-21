# Output standards

Every file the engine writes is meant to be **self-describing and reproducible**: it says what it is
(title, units, standard names), where it came from (source file *names*, software and library
versions), when and how it was made (UTC time, history, module parameters), and it opens in other
tools without surprises. The app and the command line write files through the same code, so they produce the same attributes.

The software version stamped into files is the version of the app or of the `rxd` package that wrote them; library
versions (python, xradar, xarray, numpy, netCDF4, zarr, pyproj, radarx) come from the installed packages.

Nothing written contains an absolute path of the machine that made it (source files are recorded by
name only; paths found in module parameters are reduced to file names).

## What each output follows

| Output | Standard | Notes |
| --- | --- | --- |
| NetCDF `netcdf` | CF-1.8 + CfRadial 1.4 (`version = "CF-Radial-1.4"`, `Sub_conventions`) | float32 moments, `_FillValue = NaN`, zlib, `time` = `seconds since <volume start>`; strings as character arrays |
| NetCDF `cfradial2` | CF-1.8, WMO CF-1.0, ACDD-1.3 (`version = "2.0"`, FM-301 layout) | root metadata + one group per sweep, common time epoch, sweep attributes and per-ray metadata kept |
| Zarr | CfRadial2 layout, CF attributes | Zarr v2 + consolidated metadata (`.zmetadata`) by default; `options.zarr_format = 3` for v3 |
| Derived variables (`qc__echo_class` ...) | CF flag attributes, provenance in `comment` | `flag_values` of the variable's own integer type, unique `flag_meanings`; `long_name`, `units` (`1` if none); `standard_name` only when a CF one exists; module id/title, field, parameters (pipeline recipe steps), radarx version, time in `comment`; line in the global `history` |
| Mosaic NetCDF | CF-1.10 grid | `y`/`x` = Web-Mercator metres (`projection_*_coordinate`), 2-D `lat`/`lon`, `crs` = `mercator` (+ `crs_wkt`), `crs_working_grid` documents the azimuthal-equidistant merge grid, scalar `time` + `time_bounds`, scalar `z` (`positive = "up"`) for a CAPPI, `radar` dimension with sites/volume times, merge parameters as global attributes |
| Mosaic PNG | PNG + world file (`.pgw`) + ESRI `.prj` | `.prj` parses with `pyproj.CRS.from_wkt` and equals EPSG:3857; `tEXt` chunks: Title, Software, Creation Time, Description, Comment (radars, CRS, bounds) |
| Multi-Doppler NetCDF | CF-1.8 grid | `x_wind`/`y_wind` (grid axes), `eastward_wind`/`northward_wind` (true directions), `upward_air_velocity`, `wind_speed`, `divergence_of_wind` and `atmosphere_upward_relative_vorticity` in SI (s-1), `crs` = `azimuthal_equidistant`, 2-D `lat`/`lon`, `z` positive up, `time` + bounds, radars; global attributes: method, boundary, options, background wind, QC threshold and cell counts, radars, software and radarx versions |
| CSV (sweep export) | `# key: value` comment block, then a plain header row | title, software, UTC creation time, source file name, radar position, ISO-8601 UTC `data_time`, column units, how missing values are written; values `%.4f`; read with `pandas.read_csv(path, comment="#")` |
| CSV / GeoJSON of module tables (UI) | same comment block (CSV); RFC 7946 foreign member `metadata` (GeoJSON) | module, sweep, parameters, source file name, units per column |
| PNG / PDF / SVG figures | PNG `tEXt` (Title, Software, Source, Description, Creation Time, Comment), PDF Info (Title, Creator, Subject, Keywords, CreationDate), SVG Dublin Core `<metadata>` | title text states `UTC`; GIF animations carry a comment extension |
| Pipeline recipe JSON | `schema = "xradar-desktop/recipe"`, integer `version`, `created_with` | a newer `version`, or another `schema`, is refused on load |
| Pipeline script | header with software, date, recipe format, library versions | writes a CF-1.8 NetCDF with `title`, `source`, `history`, per-variable `long_name`/`units` |

### Global attributes written by every NetCDF/Zarr

`Conventions`, `title`, `institution`, `source`, `history` (one UTC line per step: derived variables
added, export), `references`, `comment`, `date_created`, `software`, `libraries`, `source_files`,
`provenance_schema`. CfRadial volumes additionally keep the source's own attributes
(`instrument_name`, `scan_name`, VCP details ...) and add `site_name`, `scan_id`, `platform_is_mobile`,
`ray_times_increase`, `simulated_data`, `time_coverage_start/end` (ISO-8601 UTC).

### Missing data

Gates below the detection threshold or range-folded are **not measurements**. NEXRAD-style decoders turn
them into -33 dBZ, -8 dB, 0.2 or -64.5 m/s; the NetCDF/Zarr exports set them to `_FillValue` (NaN) and say so
in `comment` (`options.mask_no_echo = false` keeps the raw values). No other fill value is used: there is no
-9999 anywhere in the outputs; missing values in CSV are simply absent rows or empty fields.

## Findings of the audit and what changed

| Output | Finding (before) | Now |
| --- | --- | --- |
| CfRadial1 | `title/institution/references/source = "None"`, `history = "None: xradar ... export"`, `Conventions = "Cf/Radial"` (the CF Checker rejects it), `version = "1.2"` | real attributes, `Conventions = "CF-1.8"`, `version = "CF-Radial-1.4"`, history line, references |
| CfRadial1 | moments `float64`, uncompressed: **728 MB** for one KTLX volume | `float32` + zlib/shuffle + chunks: **15 MB** |
| CfRadial1/2 | "no echo" codes written as data (-33 dBZ ...) | masked to `_FillValue` (NaN), documented |
| CfRadial2 | `Conventions`, `version`, `history` never reached the file (xradar edits a copy of the root dataset); packed integers with no `_FillValue` (NaN became 0, "saving variable ... without any _FillValue" warnings) | attributes written; float32 + NaN fill |
| CfRadial2 | sweep attributes (`sails_cut` ...) and optional per-ray metadata (`nyquist_velocity`, `scan_rate`, `prt` ...) dropped; every sweep with its own time epoch | kept; one epoch for the volume |
| CfRadial1 | RHI volumes failed to export (`AssertionError` in xradar: ray dimension is not `azimuth`/`elevation`) | ray dimension taken from the `time` variable |
| both | units `unitless`, `meters per seconds`, `meters`, `deg/km` | UDUNITS: `1`, `m s-1`, `m`, `degrees km-1`; every unit parses with UDUNITS-2 |
| both | 7 `standard_name`s that are not in the CF table, `axis = "radial_range_coordinate"` ... | CF-table names only (`equivalent_reflectivity_factor`, `radial_velocity_of_scatterers_away_from_instrument`, `time`, `latitude`, `longitude`, `altitude`) |
| both | variable-length strings (`platform_type`, `prt_mode`, `time_coverage_*` ...), `latitude:positive = "up"`, all-NaN `polarization_mode`, variables without `long_name`, `grid_mapping` naming a variable that is not in the file | character arrays; `positive` only on altitude; placeholder dropped; `long_name` everywhere; dangling references removed |
| both | `time` = `milliseconds since 1970-01-01T00:00:00+00:00` | `seconds since <volume start ISO-8601 UTC>`, `calendar = "standard"`, no `_FillValue` on coordinates |
| derived | `flag_values` `int32` on a `float32` variable, no real provenance | integer variable of the same type with a fill value, provenance in `comment` and `history` |
| Zarr | v3 store, no consolidated metadata, float64, no CF clean-up | v2 + `.zmetadata`, float32, same attributes as CfRadial2 |
| Mosaic NetCDF | `grid_mapping = latitude_longitude` although the cells are uniform in **Web-Mercator** (wrong CRS); 1-D latitude spaced non-uniformly; no time; heights/`radar_volume_time` as free text | `mercator` grid mapping + 2-D lat/lon; `time` + `time_bounds`; numeric `radar_volume_time`; `z` for CAPPI; full merge description; `Conventions = "CF-1.10"` |
| Mosaic PNG | no metadata in the PNG | `tEXt` chunks (the world file and `.prj` were already correct) |
| Multi-Doppler | no `standard_name`s, `units = "1e-3 1/s"`, `"m/s"`, `"deg"`, no grid mapping, no time, almost no global attributes | CF names and SI units, `crs`, `time`, radars, method/options/background/QC counts/versions |
| CSV | no header information, no units, no time | `#` comment block (see above) |
| Figures | matplotlib default metadata only (`Software: Matplotlib`), title time without time zone | software/version, source file name, data time, parameters; title says UTC |
| Recipes | saved JSON had `version` but nothing about who made it | `schema`, `created_with`, newer-version guard |

## Deliberately not followed

* **CfRadial-only names** (`radar_equivalent_reflectivity_factor_h`, `ray_azimuth_angle`,
  `projection_range_coordinate`, `axis = "radial_*_coordinate"`, `index_of_first_ray_in_sweep` ...) are *not*
  in the CF standard-name table, so a CF checker flags them; they are not written. `long_name` and the
  variable name (`DBZH`, `VRADH`, `ZDR` ...) carry the polarisation. The CfRadial *structure* (dimensions,
  variable names, sweep indices, `Sub_conventions`) is unchanged.
* **Strictly monotonic `time` coordinate**: some sources (e.g. the CPOL sector/RHI examples) contain rays with
  identical time stamps. CfRadial requires `time` as the ray dimension; the data are written unchanged, so
  the CF Checker reports "(5): co-ordinate variable not monotonic" for those files. Nothing is invented to
  hide it.
* **`Conventions = "CF-1.10"` for the mosaic**: the CF Checker (4.1) only knows CF up to 1.8; the file is
  validated as CF-1.8 (nothing in it needs 1.9/1.10 features).
* **GeoTIFF mosaic export** is not available: `rasterio`/`rioxarray` are not installed and no dependency was
  added for it. Use the PNG + `.pgw` + `.prj` (opens georeferenced in QGIS/GDAL) or the NetCDF (`crs_wkt` is
  readable by GDAL/rioxarray).
* **CSV comment block** is not RFC 4180 (which has no comments); it is the convention pandas (`comment="#"`),
  R and most analysis tools understand. The column row itself stays plain (`azimuth_deg,range_m,DBZH`) and
  units are in the block.
* **`u`/`v` of the wind file are grid components** (`x_wind`, `y_wind`) because the retrieval works on the
  azimuthal-equidistant grid, whose y axis is true north only on the origin's meridian. True
  `eastward_wind`/`northward_wind` are written as well (rotated by the exact local meridian convergence).
* The mosaic does not write `cell_methods` for the composite/echo-top products: the vertical extent of the
  maximum is not part of the cached result, and a `z: maximum` method needs a `z` coordinate to refer to.

## How to validate

```bash
pip install cfchecker cfunits           # pure Python; needs the UDUNITS-2 library
brew install udunits                    # or: apt install libudunits2-dev
export UDUNITS2_XML_PATH=/opt/homebrew/share/udunits/udunits2.xml    # if the library is not found
mkdir -p ~/.cache/xradar-desktop
curl -L -o ~/.cache/xradar-desktop/cf-standard-name-table.xml \
  https://cfconventions.org/Data/cf-standard-names/current/src/cf-standard-name-table.xml
cfchecks -s ~/.cache/xradar-desktop/cf-standard-name-table.xml -v auto file.nc
```

The outputs were validated this way with the CF Checker, and with a dependency-free strict check of `Conventions`,
units and `long_name`, references, flags, time units, placeholder text and absolute paths, on synthetic volumes and on
the real example files (NEXRAD KTLX, the CPOL sector scan and RHI, the Mayfield radars: mosaic and winds); every file
was then read back and its values, masks, flags and times compared.

Results of the audit run (CF Checker 4.1.0, standard-name table v95): CfRadial1 and CfRadial2 of the KTLX volume,
mosaics (CAPPI, composite, echo top), analytic and variational winds, and every synthetic output: **0
errors**. CPOL sector/RHI: only the duplicate-ray-time message described above.
