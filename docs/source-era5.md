# ERA5: reanalysis for any date

ERA5 is the European Centre for Medium-Range Weather Forecasts' global **reanalysis**: for every hour since 1940 a
model state of the atmosphere, corrected by all the observations available at the time. It gives the large-scale
**environment** of an event anywhere in the world, including events before HRRR existed and outside the United States.
It is a model-based estimate on a 0.25 degree grid (the model's own resolution is about 31 km), so every layer from it
is labelled `MODEL` and cannot show storm-scale features: use it for the air mass, fronts, jets and the profile around a
storm, not for the storm itself.

## What you can get

| Product | What it holds |
| --- | --- |
| `single` | one field per variable at the surface or for the column: 2 m temperature and dew point, 10 m wind, surface and sea-level pressure, CAPE, CIN, precipitable water, soil moisture and temperature in four layers, snow depth, and many more (their names are listed on the data store) |
| `pressure` | geopotential, temperature, wind, specific and relative humidity, vertical velocity on 37 pressure levels, 1 to 1000 hPa; you choose the levels |
| `profile` | the column at the grid cell nearest a point (lat, lon), read as a sounding with CAPE, shear and helicity |

Short names: `t2m`, `d2m`, `u10`, `v10`, `sp`, `msl`, `cape`, `cin`, `tcwv`, `orography` for single levels and `z`, `t`,
`u`, `v`, `q`, `r`, `w` for pressure levels; any other variable of the store works by its own name
(`volumetric_soil_water_layer_1`, `snow_depth`, ...). A name that is not in the store is an error.

## Only the part you ask for is downloaded

The public copy of ERA5 on Google Cloud (ARCO-ERA5) stores one **whole globe at one hour** per file: 2 MB for a surface
variable, but **85 MB for a pressure-level variable** (all 37 levels). Downloaded whole, five variables for one profile
would be over 400 MB. The app instead reads the small table at the start of the file, works out which compressed blocks
hold your levels and latitudes and requests exactly those bytes. A profile with seven levels is about 17 MB instead of
526 MB, a regional map of five variables on two levels is 5.6 MB instead of 509 MB. The size shown in the approval dialog
is **exact**, and the dialog says how much the whole files would have been. A block covers about 90 rows of latitude, so
making the area smaller does not shrink the download further; fewer levels and variables do. Longitudes are cut after
reading.

## Which time you get

ERA5 is hourly. The **nearest hour** is used, the offset is named ("requested 02:40, the nearest hourly analysis is +20 min
away"), and nothing is interpolated in time. A reanalysis has no model run or forecast hour: each hour is an analysis. The
store covers 1940 to a few days ago (the newest months are preliminary "ERA5T"); a date outside is an error that says so.

## Reading the numbers

* Units are those of the store, written as in the CF conventions (`m s-1`). Geopotential is in m2 s-2 (divide by 9.80665
  for geopotential height). **CIN is stored as a positive number** here; HRRR's is negative.
* Precipitation, fluxes and similar accumulated variables are forecasts started from the analysis, not analyses.
* In a profile, the lowest level is the model surface (2 m temperature and dew point, 10 m wind, surface pressure,
  orography); levels at or below the model ground, which ERA5 fills with extrapolated numbers, are dropped; dew point above
  the surface is derived from specific humidity and pressure by radarx. The column is the nearest grid cell, not an
  interpolation, and its distance from your point is stated.

## On the command line

```console
$ rxd env estimate era5 --product single --time 2021-12-11T03:00Z --bbox=-92.5,34,-85,39 -V t2m -V cape
$ rxd env show era5 --product pressure --time 2021-12-11T03:00Z --bbox=-92.5,34,-85,39 -V t -V u -V v -P 'levels=[850,500]' -o upper.nc
$ rxd env profile era5 --product profile --time 2021-12-11T03:00Z -P lat=36.74 -P lon=-88.64 --lat 36.74 --lon -88.64
```

`estimate` downloads only the few kilobytes of tables needed to know the size; `show` and `profile` ask first (or need
`--yes`, which approves exactly the printed estimate). Nothing needs an account or a key. Reading the files needs no extra
package.

## Limits, honestly

* ERA5 is an estimate on a grid of about 28 km (0.25 degree): storms, outflow boundaries and the near-storm profile are
  not resolved. Compare with the HRRR analysis where both exist.
* **ERA5-Land** (0.1 degree, land only, for soils and hydrology) is **not offered**: it is not in this public store, and
  the Copernicus service that has it needs your own account and key. The ERA5 soil moisture, soil temperature and snow
  fields of the single-level product (0.25 degree) are available instead.
* A profile is the nearest grid cell and the nearest hour. A different point or time is another download.
* The store's own documentation gives no per-field uncertainty; none is invented here.

## How it was checked

* The pieces are decoded bit for bit like the whole file: for surface pressure over the Mayfield area the 21 latitude
  rows read from a 322 kB piece are identical to the same rows of the 2.4 MB whole chunk.
* The block arithmetic is tested on chunks with blocks in any order and with a short last block.
* A column of the analytic Weisman and Klemp (1982) sounding is written into a store of the real layout and read back as a
  profile: its surface-based CAPE agrees with the source sounding to within 15 %.
* A real run for 11 December 2021 03 UTC at Mayfield, Kentucky (cell 9.9 km away): surface-based CAPE 407 J/kg, 0-6 km shear
  39.6 m/s, helicity 0-1 km 473 and 0-3 km 573 m2/s2 (right mover); the HRRR analysis for the same hour gives 34.5 m/s, 520 and
  630 m2/s2 from a column of six levels, and a 3 km CAPE of 1430 J/kg: ERA5 is smoother and less unstable, as a 28 km
  grid is expected to be.

## Credit and licence

Contains modified Copernicus Climate Change Service information. ERA5 reanalysis by ECMWF for the Copernicus Climate
Change Service (C3S); analysis-ready cloud-optimised copy ARCO-ERA5 by Google LLC. The Copernicus licence allows free use
with this credit; its terms are on <https://apps.ecmwf.int/datasets/licences/copernicus/> (they are not reproduced here:
read them before redistributing). Neither ECMWF nor the European Commission is responsible for any use of the information.

## References

* Hersbach, H., et al., 2020: The ERA5 global reanalysis. *Quarterly Journal of the Royal Meteorological Society*, 146,
  1999-2049.
* Carver, R. W., and A. Merose, 2023: ARCO-ERA5: An analysis-ready cloud-optimized reanalysis dataset. 22nd Conference on
  Artificial Intelligence for Environmental Science, American Meteorological Society.
* Weisman, M. L., and J. B. Klemp, 1982: The dependence of numerically simulated convective storms on vertical wind shear
  and buoyancy. *Monthly Weather Review*, 110, 504-520.
