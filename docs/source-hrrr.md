# HRRR: model analyses and forecasts

The High-Resolution Rapid Refresh (HRRR) is NOAA's 3 km convection-allowing model over the contiguous United States,
run every hour. It gives the **environment of a storm** (CAPE, CIN, helicity, shear, the temperature, moisture and wind
profile), the model's own idea of reflectivity and hydrometeors, and, for the hours that have a radar scan, an analysis
that already blends the observations. It is a **model**: every layer from it is labelled `MODEL`, and its numbers are
estimates on a grid, not measurements.

## What you can get

| Product | What it holds | Size of the whole file |
| --- | --- | --- |
| `surface` | 2-D fields: CAPE, CIN, storm-relative helicity, vertical shear, storm motion, model reflectivity, echo top, lightning threat, hail, 2 m and 10 m values, pressure, freezing level, a few pressure levels | about 140 MB |
| `pressure` | 3-D fields on 40 pressure levels, 1000 to 50 hPa, 25 hPa apart: height, temperature, dew point, humidity, wind, vertical velocity | about 390 MB |
| `native` | 3-D fields on the model's 50 native levels: pressure, height, temperature, humidity, wind and the mixing ratios of cloud water, rain, snow, graupel and cloud ice | about 650 MB |
| `profile` | the column at one point, put together from the surface and pressure files and read as a sounding (Skew-T, CAPE, shear, helicity) | see below |

Nobody wants the whole file, so **only the fields you pick are downloaded**. The server publishes a small index of every
file with the position of every field inside it; the app reads that index (10 to 60 kB), finds the fields you asked for
and requests exactly those byte ranges. The size shown in the approval dialog is therefore exact, and a request for
CAPE, helicity and shear is a few megabytes, not 140. Each field is kept in the cache on its own, so a later request that
shares fields with an earlier one downloads only what is new.

The area you choose is cut out **after** the field is read (this file format cannot be read in parts), so a small area
does not make the download smaller; fewer fields and fewer levels do. The approval dialog says so.

## Naming the variables

Use a short name or the file's own words:

* short names: `cape`, `cin`, `srh_0_1km`, `srh_0_3km`, `shear_u_0_6km`, `reflectivity_composite`, `echo_top`, `t2m`, `d2m`,
  `u10`, `v10`, `pwat`, `freezing_level_height`, and more (`rxd env inventory hrrr` lists them);
* sets: `@storm_environment` (CAPE, CIN, helicity, shear, composite reflectivity), `@hydrometeors`;
* the file's own words, `NAME:level`: `TMP:500 mb`, `HLCY:3000-0 m above ground`; add `:step text` when a level appears
  several times (a maximum and an instantaneous value); `TMP:@mb` means every pressure level, `CLMR:@hybrid` every native
  level.

A name that is not in the index of that file is an error that lists what the file does hold; nothing is guessed.

## Which time you get

You give the time you want (for example the time of a radar scan). The app uses the **model analysis of the nearest
hour**: the 03 UTC run's analysis is valid at 03:00. It says which run, which forecast hour and which valid time it
took and how far that is from the time you asked for ("requested 02:50, the nearest hourly valid time is +10 min away").
**Nothing is interpolated in time.** To use a forecast instead, give the forecast hour (`fxx`); the 00, 06, 12 and 18 UTC
runs reach 48 hours, the others 18.

## Winds

The winds in the file are given along the axes of the model grid, not towards east and north. The app rotates them to
east and north when you take both components (`u10` with `v10`; the shear and storm-motion components in pairs). A
single component cannot be rotated, and is marked as grid-relative.

## A profile at a point

`profile` downloads the surface values and the pressure levels needed for a sounding, and reads the column at the grid
cell nearest your point. The lowest level is the model surface (2 m temperature and dew point, 10 m wind, surface
pressure, terrain height); levels at or below the model ground, which the file fills with extrapolated numbers, are
dropped. The column is then analysed like a radiosonde: CAPE, CIN, shear, helicity, storm motion, freezing level and
precipitable water, all labelled as a **model column**. By default the levels are 50 hPa apart (19 levels); `levels_step_hpa`
25 gives finer spacing and `levels` takes your own list. **More levels cost more download** (about 0.8 MB per field and
level), and fewer than about 40 levels make CAPE, CIN and helicity approximate; the sounding says so.

## In the app and on the command line

```console
$ rxd env estimate hrrr --time 2021-12-11T03:00Z -V cape -V srh_0_3km
$ rxd env show hrrr --time 2021-12-11T03:00Z --bbox -92.5,34,-85,39 -V @storm_environment -o environment.nc
$ rxd env profile hrrr --product profile --time 2021-12-11T03:00Z --lat 36.74 --lon -88.64 --yes
```

`estimate` downloads nothing; `show` and `profile` ask first (or need `--yes`, which approves exactly the estimate
that was printed). The NetCDF file carries the class, source, run time, valid time, resolution, method, credit and
licence as global attributes. The Sounding panel draws a model column as a Skew-T with a `MODEL` chip; today a column is requested from the
command line or by other features, there is no button for it in the panel yet.

## Reading the data needs the GRIB reader

Downloading and caching need nothing extra. **Reading** the fields needs the open ecCodes library from ECMWF
(Apache-2.0), which is not part of the installers, to keep them small. Add it to the command-line package with
`pip install "radarx-desktop-engine[grib]"`. Without it the fields stay in the cache and the message says what to install.

## Limits, honestly

* A model field is an estimate, not an observation. At the analysis time the model has assimilated observations, radar
  among them, but near an intense storm the analysis contains the storm itself (its outflow, its rain-cooled air). Look at
  the inflow side for the environment the storm grew in.
* CAPE, CIN, helicity and shear in the file are computed by the model's post-processing with its own choices (parcel,
  storm motion). They are not recomputed from a sounding, and other software gets other values for the same profile.
* 3 km grid cells cannot show boundaries, outflow edges or small-scale features sharper than a few cells.
* Model reflectivity is the model's, not the radar's, and shows where the model put the storms. A value of -10 dBZ means
  no echo.
* The 15-minute sub-hourly files are not offered yet. Only the contiguous US domain is supported. The Alaska domain exists but uses another map projection whose wind
  rotation is not implemented.
* The reader is optional (see above); the viewer in the app shows the profile, not map layers of the fields. The fields
  are available as NetCDF from `rxd env show`.

## How it was checked

* The wind rotation is compared with the local axes of the same projection computed by the PROJ library at four places;
  they agree to better than a millionth of a metre per second, and the speed is unchanged. In the file for 3 UTC on
  11 December 2021 the winds are grid-relative, and the rotation changes the direction at Mayfield, Kentucky by 5.5 degrees.
* Temperature, humidity and dew point conversions have known answers (20 degrees C and 50 % give a dew point of 9.3 degrees C;
  a specific humidity of 10 g/kg at 1000 hPa gives 14.0 degrees C).
* A column of the analytic Weisman and Klemp (1982) sounding is written as HRRR-like files and read back as a profile: its
  surface-based CAPE agrees with the source sounding to within 15 % at 50 hPa spacing.
* A real run for Mayfield, Kentucky, 11 December 2021 03 UTC: the column recomputed from only six pressure levels gives a
  surface-based CAPE of 1030 J/kg, 0-1 km helicity of 520 and 0-3 km helicity of 630 m2/s2 (right mover); the model's own
  fields at that cell are 1430 J/kg, 569 and 662 m2/s2. Helicity agrees to about 10 %; CAPE is 28 % lower because six levels
  are too few to resolve the instability, which is why the sounding calls such a column approximate.

## Credit and licence

High-Resolution Rapid Refresh, NOAA/NCEP/EMC, distributed through the NOAA Open Data Dissemination programme on Amazon
Web Services (<https://registry.opendata.aws/noaa-hrrr-pds/>). NOAA data are free to use and redistribute; credit
NOAA/NCEP.

## References

* Dowell, D. C., et al., 2022: The High-Resolution Rapid Refresh (HRRR): An hourly updating convection-allowing forecast
  model. Part I: Motivation and system description. *Weather and Forecasting*, 37, 1371-1395.
* Benjamin, S. G., et al., 2016: A North American hourly assimilation and model forecast cycle: The Rapid Refresh.
  *Monthly Weather Review*, 144, 1669-1694.
* Bolton, D., 1980: The computation of equivalent potential temperature. *Monthly Weather Review*, 108, 1046-1053
  (saturation vapour pressure and dew point).
* Snyder, J. P., 1987: *Map Projections: A Working Manual*, USGS Professional Paper 1395 (Lambert conformal conic,
  convergence of the meridians).
* Weisman, M. L., and J. B. Klemp, 1982: The dependence of numerically simulated convective storms on vertical wind shear
  and buoyancy. *Monthly Weather Review*, 110, 504-520.
