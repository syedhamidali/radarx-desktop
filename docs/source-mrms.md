# MRMS: radar mosaics, rainfall, hail and rotation

MRMS (Multi-Radar/Multi-Sensor) is NOAA's system that merges the US radar network into one seamless **1 km grid every 2
minutes**: 3-D and composite reflectivity, rainfall rate and accumulations, precipitation type, hail size, rotation
tracks, echo tops and more. It is the natural base layer for a United States event and for animations. Its products are
**retrieved** from the radars' measurements (class `RETR`), not measurements themselves, and **which ingredients go into
each one is stated in the layer**: radar only, radar with a model-assisted classification, radar adjusted to rain gauges,
or a numerical-model field.

## What you can get

| Name | What it is | Ingredients |
| --- | --- | --- |
| `reflectivity_composite`, `reflectivity_lowest` | quality-controlled composite and lowest-altitude reflectivity | radar only |
| `reflectivity_3d` | reflectivity at 33 heights from 0.5 to 19 km above mean sea level (you choose the heights) | radar only |
| `precip_rate`, `qpe_radar_15m`, `qpe_radar_1h`, `qpe_radar_24h` | rainfall rate and radar-only accumulations | radar, with the precipitation type from model temperature |
| `qpe_gauge_1h`, `qpe_gauge_24h` | gauge-adjusted accumulations (second pass) | radar adjusted to hourly gauges |
| `precip_type` | surface precipitation type flag (categories as in the file) | radar, with model temperature |
| `mesh`, `posh`, `shi` | maximum expected hail size, probability of severe hail, severe hail index | radar only |
| `vil`, `echotop_18`, `echotop_30` | vertically integrated liquid, echo-top heights | radar only |
| `rotation_30min`, `rotation_60min`, `azshear_0_2km`, `azshear_3_6km` | rotation tracks and azimuthal shear, in 0.001 per second, on a grid twice as fine (0.005 degree) | radar only |
| `freezing_level` | height of the 0 degC level | a numerical-model field (class `MODEL`); large file |

Any other product folder of the archive can be named as it is written on the bucket (`MergedReflectivityQC_05.00`,
`FLASH_QPE_ARI01H_00.00`, ...); a name that is not there is an error and nothing is guessed.

## What is downloaded

One file per product and time (about 50 kB to 2 MB each, 7.6 MB for the freezing level), found by listing the archive
folder; the dialog shows the exact total. These files cannot be read in parts, so the area is cropped **after** reading
and a small area does not shrink the download. Choose several heights of `reflectivity_3d` with
`-P 'heights_km=[1,3,5]'` (or `'heights_km="all"'`: 33 files).

## Which time you get

The **nearest file** to the time you asked for, within 30 minutes, is used. Its real time and the offset are shown ("file of
03:02:39 UTC, +9 s from the time asked for"), and nothing is interpolated. With an end time (`--time-end`) every file of the
window is fetched as a time series, with its own time axis.

## Reading the numbers

* Values are as in the file. The files mark "no data" and "no coverage" with values below the physical range (for example
  -999, -99, -3, -1, depending on the product). They are **not** replaced or hidden: mask them before computing with a field
  (the mean of a reflectivity field that includes -99 is meaningless). The NSSL documentation gives the meaning per product.
* The file does not say the unit of every product; where it does not, the unit written in the dataset comes from the NSSL
  product documentation, and the dataset says so (`units_source`).
* Products on different grids (the rotation tracks) keep their own latitude and longitude axes in the dataset.
* Reflectivity near the ground is limited by beam height, blockage and range; MRMS fills the country but is poor in mountains
  and far from radars. Rainfall rates depend on the relation between reflectivity and rain; the gauge-adjusted accumulation
  also depends on how many gauges there are.
* Gauge-adjusted and radar-only accumulations are different products; never compare them as if they were the same.

## On the command line

```console
$ rxd env estimate mrms --time 2021-12-11T03:00Z -V mesh -V rotation_30min -V reflectivity_composite
$ rxd env show mrms --time 2021-12-11T03:00Z --bbox -92.5,34,-85,39 -V mesh -V rotation_30min -o mrms.nc
$ rxd env show mrms --time 2021-12-11T03:00Z --bbox -92.5,34,-85,39 -V reflectivity_3d -P 'heights_km=[1,3,5]'
```

`estimate` downloads only a few kB of listing; `show` asks first (or needs `--yes`, which approves exactly the printed
estimate). Reading the files needs the optional ecCodes package (`pip install "radarx-desktop-engine[grib]"`); the files
stay in the cache until it is installed.

## Limits, honestly

* Contiguous United States only (the archive has other regions that are not offered here).
* The archive starts in October 2020 and not every product exists for every date.
* Reading is done one field at a time and cropped while decoding, but the finest products are large: decoding one rotation-track
  file (98 million values) takes about 1.2 GB of memory and 3 s; the 1 km fields take a few hundred MB.
* These are retrievals on a grid, not a replacement for the individual radar's data: a mosaic has merged different radars'
  scans taken minutes apart.
* Display in the 2D and 3D viewers, combination with single-radar data, and tracking on MRMS fields are not part of this
  release: the fields come out as NetCDF (`rxd env show -o`) with their provenance.

## Credit and licence

Multi-Radar/Multi-Sensor (MRMS) system, NOAA National Severe Storms Laboratory and National Weather Service, distributed
through the NOAA Open Data Dissemination programme on Amazon Web Services (<https://registry.opendata.aws/noaa-mrms-pds/>).
NOAA data are free to use and redistribute; credit NOAA/NSSL/NWS.

## References

* Smith, T. M., et al., 2016: Multi-Radar Multi-Sensor (MRMS) severe weather and aviation products: Initial operating
  capabilities. *Bulletin of the American Meteorological Society*, 97, 1617-1630.
* Zhang, J., et al., 2016: Multi-Radar Multi-Sensor (MRMS) quantitative precipitation estimation: Initial operating
  capabilities. *Bulletin of the American Meteorological Society*, 97, 621-638.
* Witt, A., et al., 1998: An enhanced hail detection algorithm for the WSR-88D. *Weather and Forecasting*, 13, 286-303.
