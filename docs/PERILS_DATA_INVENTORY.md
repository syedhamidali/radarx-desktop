# PERiLS IOP2 (30 March 2022): data inventory and the chosen window

The maintainer's PERiLS data folder (`~/Downloads/MULTIDOPPLER`, 85 GB, read only; plus `~/aies_stage/perils` for the
tornado tables and `~/MRMSTEST` for MRMS and GOES samples) holds everything that was measured of the
30 March 2022 quasi-linear convective system (QLCS) in Mississippi and Alabama. This page lists what is
there, what can be used for a movie, how the sources overlap in time and why the window
**2022-03-31 01:04:00 to 01:20:00 UTC (16 min)** was chosen. All times are UTC (the event is the evening of 30 March
local time). Nothing was downloaded; nothing was written to the data folder.

How the bundle is built from these sources is in [PERILS_BUNDLE.md](PERILS_BUNDLE.md).

## 1. Sources

"Usable" means usable for the movie bundle in the 22 to 02 UTC time range.

| Source | What | Time coverage | Resolution | Format | Usable? |
| --- | --- | --- | --- | --- | --- |
| `GRID/<radar>` COW1, DOW7, DOW8, KBMX, KGWX, NOXP, SMARTR2, UAH | Py-ART grids of eight radars (S: KGWX, KBMX; C: COW1, SMART-R 2; X: DOW7, DOW8, NOXP, UAH MAX), all on one grid centred at 33.5759 N, 88.5485 W | 22:00 to 01:30 UTC (KGWX to 01:32; KBMX to 00:32; DOW8 to 00:34); 90 / 107 / 332 / 29 / 33 / 106 / 104 / 103 files | 500 m, 361 x 361 x 41 levels, 0 to 10 km every 250 m (above the radar's own site); a volume every 1.5 to 6 min (DOW8 sector scans every 27 s) | netCDF4, fields REF, VEL (dealiased by the owners), ZDR, RHO, KDP, PHIDP, ROI, NCP, CREF, DBZC per radar | **Yes**, the core. Quirks: z = 0 is the radar site altitude, not sea level (78 to 330 m); ZDR is biased by up to 4 dB at NOXP and SMART-R 2; SMART-R 2 `REF` has unmasked fill values (use `DBZC`) and its `KDP` is garbage; DOW8 has no dual-pol |
| `CFRAD/COW1`, `CFRAD/KGWX` | polar CfRadial of COW1 (18 files) and KGWX (32 files) | 22:45 to about 01:15 | native | CfRadial netCDF | Not needed: the gridded files carry the same volumes |
| `GRID/MRMS/MergedRef`, `AzShear` | MRMS merged base reflectivity (QC, 500 m above MSL) and the 0 to 2 km azimuthal shear | 00:00 30 Mar to about 01:40 31 Mar, 2 min | 0.01 deg, CONUS | GRIB2 (about 2000 files) | **Yes** with `wgrib2` (found at /opt/local/bin; no GRIB reader is in the Python environment). No MESH product in the folder |
| `NSSL_LMA/syed41331583/datagPIuma/20220330` | NALMA (14 stations) + NSSL deployable LMA (8 stations) VHF sources, analysed | 10 min files from 29 Mar 19:30 to 31 Mar 14:40 | source time, lat, lon, alt, chi2, power, station mask | `LYLOUT_*_0600.dat.gz` (0.5 to 1.1 million sources per 10 min) | **Yes** |
| `NSSL_LMA/Gridded` (and the same files in the zip) | pyxlma gridded products of the same data: flash-extent density, flash area, event counts, 500 m, 5 min | 30 Mar 00:00 to 31 Mar 05:00 | 5 min, 1001 x 1001 | netCDF | Not used (raw sources used); useful as a cross-check of the flash count |
| `PIPS_data/IOP2_033022` | 4 PIPS (1A, 1B, 2A, 3B): Parsivel2 DSD, rain rate, reflectivity, Dm, wind, T, Td, p; owner's QC | 23:40 to 01:25 (1A, 1B), 23:59 to 01:25 (2A, 3B) | 10 s and 60 s, 32 x 32 size-velocity matrix | netCDF, csv | **Yes** |
| `UAH_MAPNet_RaDAPS_Surface_610.017` | Vaisala WXT536 surface station incl. rain and hail sensor at the RaDAPS site (33.5956 N, 88.9879 W from the logbook) | 14:30 30 Mar to 02:14 31 Mar | 1 s | text | **Yes** (rain intensity, hail sensor, T, RH, p, wind) |
| `UAH_MAPNet_Parsivel_610.012` | Parsivel at the RaDAPS site | 19:05 30 Mar to 01:33 31 Mar | 10 s | Parsivel text telegrams | Not parsed (the WXT rain sensor and the PIPS cover the rain) |
| `UAH_MRR` (610.018) | Micro Rain Radar at the RaDAPS site: Z, fall velocity, rain rate, LWC, DSD per gate | 14:42 30 Mar to 01:34 31 Mar | about 30 s, 31 gates of 150 m | netCDF | **Yes** (profiles) |
| `UAH_MRR` (610.015) | 915 MHz radar wind profiler at the RaDAPS site (6 beams) | 15:00 30 Mar to 01:40 31 Mar | 5 and 60 min, 140 gates | netCDF | **Yes** (profiles) |
| `PSL_Parsivel_610.037` | NOAA PSL Parsivel at Courtland, AL | 30 Mar to 31 Mar | 1 min | text | Outside the domain (about 116 km east of the grid origin) |
| `TTU_StickNet`, `ttu/Sticknet_10s` | 24 StickNet probes (T, RH, p, wind, gust) with locations | 29 Mar to 31 Mar | 10 s (level 1 is 1 s) | txt / csv | **Yes** (24 stations) |
| `UIUC_FARM_SCOUTS` SCOUT1 to 3, `soundings/non-radar_QC_Illinois/.../DOW7, DOW8` | vehicle mesonets (1 Hz, with GPS position), Parsivels on the scouts, pods | 30 Mar daytime to 31 Mar 02 | 1 s, 10 s | csv (L1 QC), xlsx | **Yes** (mesonets, 3 Parsivel records) |
| Soundings, UIUC FARM (5 sites, 35 launches 20 to 02 UTC) | GRAW DFM-09, 1 s, positions, QC level 1 | launches 20:04 to 02:08 | 1 s | EOL sounding format | **Yes**, including launches inside the window |
| Soundings, UAH (Brooksville, Amory, Maben), ULM (Sturgis) | iMet / ULM sondes | 15:00 to 22:55 | 3 to 5 s | text | Yes (pre-storm; they end at 4 to 6 km) |
| Soundings, NWS operational (`WYO_SOUNDING`) | BMX, JAN, LZK, ... 00 UTC 31 Mar and others | 00 UTC cycle, balloon left about 23:15 | mandatory + significant levels | csv | Yes (BMX is the full-depth profile used for the freezing level) |
| `Reanalyses/ERA5` | pressure-level fields (u, v, T, q, z, cloud and rain water) | 30 and 31 March hourly | 0.25 deg | netCDF (HDF5) | **No**: the file covers 25 to 65 N and **80 to 64 W** (the north-east), the storm was at 88.5 W |
| `Reanalyses/MERRA2*`, IMERG average | MERRA-2 surface fluxes; a time-averaged IMERG map | day | 0.5 x 0.625 deg; averages | netCDF | No (no 3-D fields; the IMERG map is a time average) |
| `wrf_iop2`, `mpas_iop2` | Logs, scripts, figures and summary tables of six WRF 1 km runs (Thompson, WSM6, Morrison, NSSL 2/3-moment, P3; HRRR forced, 18 UTC start) and MPAS test runs | 18 to 03 UTC | 1 km | text, png, csv (the model output stayed on the cluster) | **No**: no gridded model output is on disk; the owner's evaluation says the simulated line is displaced from the observed one by -18 to +87 km (-33 to +155 min) |
| `wind_synthesis/wind_synthesis_2127UTC.nc` | variational multi-Doppler winds (3 radars) | 21:27 only | 141 x 141 x 48, 1 km | netCDF | Not for this window (3.6 h earlier); kept as a reference |
| `TRAJECTORIES/tobac_Save_20220330` | tobac tracks and segmentation of KGWX echo (15 dBZ) | 21:58 to 01:32, 6 min | KGWX grid | netCDF | **Yes** (the tracked QLCS centroid, used as an independent motion estimate); the file tracks one object |
| `TRAJECTORIES/KGWX`, `COLUMNS` | KGWX 0.5 deg DSD retrievals, columns above PIPS sites, evaporation tables | 23:30 to 01:15 | 60 s | netCDF, csv | Not used (retrievals of the owner's thesis) |
| `~/MRMSTEST/GOES16` | one GOES-16 ABI RadC C16 file of December 2024 | not the event | | netCDF | No (wrong date); no GOES-16 for 30 March in the folders |
| Lidar | none: only a notebook `ITAP/.../NSSL_Lidar.ipynb` mentions it | | | | **Not available** |
| ASOS / AWOS | none in the folder | | | | **Not available** |
| Photos and video | `UIUC_FARM_SCOUTS/*/media` and `soundings/non-radar_QC_Illinois/*/media`: 8 to 60 HEIC / jpg per scout or site for IOP2, `GURU-Loops/IOP02-L.mp4`, `PIPS_data/.../*_anim.mp4`; the scout photos are field photographs of the sites (file names IMG_xxxx, no time / place list) | | | HEIC, jpg, mp4 | Not copied (personal). If any is a storm photograph the maintainer can decide; none was opened |
| Tornado tables (`~/aies_stage/perils`) | `perils_tornadoes_nsed.csv` (NWS damage-survey events), `perils_spc_tornadoes.csv` (SPC), `perils_modes.csv` (QLCS mode of each tornado's parent system) | the IOP2 day | begin / end time and point, rating, width | csv | **Yes** |
| SPC / NWS / PERiLS hail reports | none in the folder (`Tornado_Reports.ipynb` fetches them from the network) | | | | **Not available** |

## 2. The tornadoes of the day inside the radar domain

Survey events (NWS, in the NSED table) whose begin point lies within 200 km of the grid origin and that occur between
20:00 UTC 30 Mar and 05 UTC 31 Mar, with the position in km east / north of the origin:

| Event | Rating | Begin to end (UTC) | Begin x, y (km) | In the 180 km box | Near the radars |
| --- | --- | --- | --- | --- | --- |
| 1035753 / 1035752 | EF1 / EF2 | 22:32 to 22:43 | -95, -77 | at the edge | KGWX 89 km |
| 1013817 | EF1 | 22:24 to 22:32 | -77, 53 | edge | KGWX 95 km |
| **1013785** | EF0 | **00:16 to 00:26** | 2, 25 | inside | KGWX 15 km (the survey says a tornadic debris signature); COW1 22 km |
| **1035605** | **EF2** | **01:08 to 01:20** | 7, -53 | inside | **NOXP 3.5 km**, SMART-R 2 25 km, StickNet 0102A 6 km |
| 1035607 / 1035766 | EF1 | 01:07 to 01:35 | -14..1, -150..-137 | no (south) | no |
| 1018790 | EF1 | 01:51 to 02:02 | 26, -114 | no | no |

## 3. Overlap analysis and the chosen window

Two windows have the radars, the lightning, the stations and a tornado at the same time. The numbers:

| | A: 00:14 to 00:30 | **B: 01:04 to 01:20 (chosen)** |
| --- | --- | --- |
| Radar volumes inside the window | 8 radars: COW1 1, DOW7 8, DOW8 33, KBMX 3, KGWX 2, NOXP 8, SMART-R 2 8, UAH 8 | 6 radars: COW1 7, DOW7 8, KGWX 2, NOXP 8, SMART-R 2 8, UAH 8 (DOW8 and KBMX have stopped) |
| Volume mid-times (UTC), the mobile radars | every 1.8 to 2.2 min | every 1.8 to 2.4 min (NOXP 1.2 to 2.5 min) |
| KGWX volumes (mid) | 00:16:07, 00:23:43 | 01:08:40, 01:14:56 (6.3 min cycle) |
| Tornado (survey) | EF0, 10 min, 12 km long, near KGWX / COW1 | **EF2, 12 min, 17.6 km long, 400 yd wide**, NOXP and SMART-R 2 within 3.5 and 25 km |
| Visible rotation at the 500 m grid | none above the shear noise | broad low-level shear at the surveyed position; a rotation candidate within a few km (see PERILS_BUNDLE) |
| LMA sources per minute (chi2 <= 1, >= 6 stations, inside the box) | 12 500 to 21 500 (mean 17 200) | 11 700 to 16 000 (mean 14 200) |
| Rain at the PIPS (60 s) | the line passes: peaks 155 mm/h (1A, 00:29), 156 (2A, 00:31), 142 (3B, 00:30), 131 (1B, 00:40) | trailing stratiform: 6 to 8 mm/h mean, 10 to 14 mm/h at most |
| StickNet / mesonet | the PIPS cluster (10 StickNets) sees the gust front at 00:05 to 00:23 | 0102A (6 km from the tornado start) gust 15.2 m/s at 01:07:20; 0218A gust at 01:16; 0224A at 01:13; SCOUT1 (33.105 N, 88.561 W) sits 9 km from the tornado start |
| Soundings within +-90 min | UAH Amory 22:55 (79 min before), 00 UTC BMX/JAN (balloon about 23:15), UIUC 23:30, 00:17, 00:32 | **UIUC SONDE5 01:03, SONDE1 01:06, SONDE2 01:07, SONDE3 01:08 (SONDE5 one minute before the window, the others inside), SONDE4 00:32 and 01:51, SONDE2 01:58, SONDE3/5 02:00, SONDE1 02:08** (post-storm cold-pool profiles); drift tracks in the bundle |
| Multi-Doppler geometry | DOW8 + UAH + COW1 + SMART-R 2 + NOXP + KGWX + DOW7 + KBMX | NOXP, SMART-R 2, COW1, DOW7 and UAH cross over the tornado path; KGWX adds the north |

**Choice: B.** It contains the whole life of the one tornado of the day that is rated EF2 within the dense network, five
mobile radars scanning every 2 min around it (two within 25 km), lightning at 14 000 sources per minute, StickNet and a vehicle mesonet at
the tornado, and radiosondes launched inside the window (UIUC SONDE1 01:06, SONDE2 01:07, SONDE3 01:08; SONDE5 one minute before, 01:03) and 31 to 56 min after it (SONDE4 01:51,
SONDE2 01:58, SONDE3 and SONDE5 02:00, SONDE1 02:08: the cold pool behind the line). It does **not** contain the PIPS rain-rate peaks
(those are 00:22 to 00:42, window A); the PIPS are in the trailing stratiform rain. The operational 00 UTC soundings are 105 to 110 min
before the start and the UAH / ULM sondes end at 22:55. Window A has the rain and the gust front at the PIPS cluster, eight radars
(DOW8 every 27 s) and the weak EF0 tornado; the builder can make it with `--window 2022-03-31T00:14:00Z 2022-03-31T00:30:00Z`.

The window is 16 min (960 s) with steps every 60 s (17 steps): about 2 min of video at 8x.

## 4. What the data cannot give, honestly

- The tornado vortex itself (about 400 m wide at EF2) is below the resolution of the gridded data (500 m grid, radius of influence 0.5 to 3.5 km):
  rotation and TDS products are evidence at the scale of the grid, not a resolved vortex.
- No lidar, no ASOS, no hail reports, no gridded model fields, ERA5 in the wrong region, GOES only for another date, MRMS without MESH.
- Radar calibration: reflectivity is not corrected for X-band attenuation; ZDR has biases up to 4 dB (corrected with a rain-based estimate, see PERILS_BUNDLE).
- The nearest sounding within the storm environment is a profile ending at 4 to 7 km height for most launches; temperatures above are from the 00 UTC BMX sounding, which was launched 150 km east and about 110 min earlier.
