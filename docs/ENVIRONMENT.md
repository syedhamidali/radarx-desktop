# Environment data, soundings and provenance

Radar data is only half of an analysis. The other half is the environment: a sounding, a model
field, a reanalysis, a satellite image, a gauge. This page describes the framework every such source
plugs into (**consent first, cached, usable offline, attributed**), the **provenance** that labels every
layer, and the first source built on it: **observed soundings** with a Skew-T and thermodynamic
parameters.

* [The rules](#the-rules)
* [Provenance: what kind of numbers is this?](#provenance-what-kind-of-numbers-is-this)
* [Using environment data](#using-environment-data) (what you see)
* [Soundings](#soundings)
* [Limits, honestly](#limits-honestly)

## The rules

1. **Nothing is downloaded without your approval.** Before any transfer the app shows the *source*
   (host names), the *number of files and bytes*, *where they will be saved*, and the *credit and
   licence*. Only the **Download** button starts it; closing the window downloads nothing.
2. **The approval covers exactly what you saw.** If the files grew after the estimate (the server
   sent more than announced), the transfer is stopped and nothing is kept.
3. **Cached data works offline.** Everything fetched once is kept in a size-limited cache (least
   recently used files go first). A repeat of the same request is served from the cache with no
   network and no question. **Offline mode** never touches the network at all.
4. **Polite to servers.** An honest `User-Agent` (`radarx-desktop/<version> (+https://radarx.labshope.com)`),
   at most 2 simultaneous connections, at least 0.1 s between requests to one host, retries with
   exponential backoff for timeouts and HTTP 408/429/5xx (honouring `Retry-After`), hard timeouts
   (30 s for any single network operation, 15 min for a whole transfer) and a byte cap per file.
5. **Integrity.** A file is only kept once complete; if the source publishes a checksum (SHA-256) it
   must match, otherwise the file is discarded (one automatic retry, then an error). The checksum of
   every kept file is recorded.
6. **Credit.** Every provider carries an attribution and a licence text, shown in the approval dialog
   and stored in the provenance of what it returns.
7. **Never invented.** A value that cannot be computed is shown as `n/a`; a time that is not known is
   shown as "unknown".

Environment data is a feature of the desktop app.

## Provenance: what kind of numbers is this?

The policy behind this (what each class must show, how uncertainty is stated) is {doc}`SCIENCE`;
this page is how the app implements it. Every layer carries a provenance record, and the **Layers** panel lists every
layer the app holds with a colour-coded **chip**. The class is always written out next to the colour.

| Chip | Class | Meaning | Colour |
|---|---|---|---|
| `OBS` | **observed** | Measured by an instrument: radar moments, a radiosonde, a gauge. | green |
| `MODEL` | **model** | Output of a numerical weather prediction or reanalysis (HRRR, ERA5): an estimate on a grid, not a measurement. | cyan |
| `RETR` | **retrieved** | Derived from observations by an algorithm or physical model: hydrometeor class, rain rate, multi-Doppler winds, storm cells, a gridded composite. | violet |
| `SIM` | **simulated** | Produced by a forward model or synthetic generator: not a real observation. | amber |
| `STYLE` | **stylised** | Drawn for looks (clouds, shading, exaggeration): not a data value. The realistic 3D look is listed as stylised. | neutral, dashed outline |

Both the dark and the light theme use these colours.

Hover or focus a chip (click to pin, Escape to close) to see: the class definition, **source**,
**valid time** and run time (UTC), the **time offset to the active radar scan**, resolution, **method**,
parameters, what it was made from, **uncertainty**, references, credit and licence. A layer without
provenance shows a dashed `?` chip and says that nothing is known.

### What a provenance record holds

```json
{"schema": 1, "kind": "retrieved", "source": "radarx-desktop module radarx_hid (radarx 0.5.1)",
 "title": "Hydrometeor class", "run_time": "2026-10-19T11:21:00Z", "valid_time": "2013-05-20T20:16:43Z",
 "method": "Computed by the 'radarx_hid' module from the radar moments of the open file.",
 "parameters": {"freezing_level_m": 4000.0}, "uncertainty": "Inherits the measurement errors ...",
 "references": ["..."], "inputs": ["DBZH", "ZDR"]}
```

Only `kind` and `source` are mandatory; empty fields are left out. Times are UTC ISO-8601 with a `Z`. A record is
attached to every kind of layer: an opened file, derived variables, module fields, mosaics, multi-Doppler winds,
storm cells, cross-sections, volumes, soundings and downloaded environment data.

### In exported files

Files carry the same information, so a figure or dataset passed on still says what it is:
NetCDF/Zarr global attributes `data_class`, `data_source`, `data_valid_time`, `data_run_time`,
`data_resolution`, `data_method`, `data_parameters`, `data_uncertainty`, `data_references`, `data_inputs`,
`data_attribution`, `data_licence` and `data_class_definition`; derived variables carry them per
variable; CSV exports get the same as `# data_class: ...` header lines; figures get `data class: ...`
in their comment.

## Using environment data

In the **Sounding** panel, *Environment data settings* shows the cache (used / limit, folder), the
offline switch, the list of sources with their credit and licence, and **Clear the cache**.

| Setting | Default | Notes |
|---|---|---|
| Cache folder | `~/.xradar-desktop/environment-cache` | any absolute folder |
| Cache size limit | 2048 MB | 16 MB .. 4 TB; lowering it evicts the least recently used files at once |
| Offline mode | off | |

Settings are saved in `~/.xradar-desktop/environment.json`.

What the approval dialog looks like (the numbers are an example):

```text
Download data?                                   [MODEL] Fake model (recorded sample)
From      data.fake.invalid
Download  4 files, 800 KB
Cached    1 of 5 files already in the cache (200 KB), not downloaded again
Saved to  /Users/you/.xradar-desktop/environment-cache
Credit    Synthetic test data generated by Radarx Desktop; not real weather.
Licence   CC0 (generated)
Nothing is downloaded until you press Download. Closing this window downloads nothing.
                                                   [ Download ] [ Cancel ]
```

If some file sizes are not known in advance the dialog says so ("at least 3 MB; a limit of ... applies")
and the transfer is stopped at that limit. If the request is larger than the cache limit, or offline mode is
on and files are missing, the dialog says why and offers no Download button.

## Soundings

**Sounding** panel (right-hand side) and `rxd sounding`: a radiosonde profile as a **Skew-T log-p diagram**
and a **hodograph**, with the parameters below. The diagram is drawn by the app (SVG, themed).

Open a file (University of Wyoming text list or CSV, SPC / SHARPpy / NSHARP text, IEM JSON, IGRA2, or a
CSV with named columns), try the synthetic **Example** (labelled *simulated*), or ask the **Archive**
(Iowa Environmental Mesonet radiosonde archive; the approval dialog comes first).

### Formats

| Format | Notes |
|---|---|
| Wyoming text list (`TEXT:LIST`) | fixed 7-character columns `PRES HGHT TEMP DWPT ... DRCT SKNT`; station, time, latitude/longitude from the block below the table |
| Wyoming CSV, generic CSV | header names the columns; units in the header are honoured (`pressure(mb)`, `temp (deg C)`, `wind speed (kts)`, `height (m AGL)`); heights are geopotential unless the header says AGL/MSL/GPS |
| SPC / SHARPpy | `%TITLE%` / `%RAW%` / `%END%`: p (hPa), z (m), T, Td (degC), direction, speed (knots) |
| IEM RAOB JSON | `profiles[].profile[]` |
| IGRA2 | through radarx |

A **wind speed with no unit** in the file is read as m/s **and the sounding says so**, with buttons to
re-read it as knots (`--wind-units kt` on the command line). Dew-point gaps between reported levels are
interpolated in log(p) and, above the last reported humidity, the air is treated as very dry (dew point
60 K below the temperature, negligible for CAPE at those temperatures); neither is drawn, and both are
listed in the notes.

### What is shown, and its definition

Every number says **which parcel**, its **unit** and its **height reference** (m AGL = above the launch
site; the launch height above sea level is stated). Hover a label or open *Definitions and references*.

| Quantity | Definition (short) | Reference |
|---|---|---|
| CAPE, CIN (J/kg) | integral of the virtual-temperature buoyancy of a lifted, pseudo-adiabatic parcel above / below its level of free convection | Bolton (1980); Doswell and Rasmussen (1994) |
| LCL, LFC, EL (m AGL, hPa) | lifting condensation level; first level at or above the LCL where the parcel is warmer than its surroundings; highest level where buoyancy returns to zero (the top of the sounding if still buoyant: shown as such, CAPE then cut off) | Bolton (1980) |
| Lifted index (K) | environment minus parcel temperature at 500 hPa | Galway (1956) |
| **Parcels** | *surface-based*: the lowest level; *mixed-layer*: mean theta and mixing ratio of the lowest 100 hPa; *most-unstable*: greatest theta-e in the lowest 300 hPa | usual definitions |
| Bulk shear 0-1 / 0-3 / 0-6 km (m/s, kt) | magnitude of the vector wind difference between the top and bottom of the layer | |
| SRH 0-1 / 0-3 km (m2/s2) | storm-relative helicity, layer by layer on the sounding's own levels, for the Bunkers right and left mover | Davies-Jones et al. (1990) |
| Storm motion | Bunkers internal-dynamics method: 0-6 km mean wind (height-weighted here) plus 7.5 m/s perpendicular to the 0-6 km shear vector (0-0.5 km to 5.5-6 km mean winds), to the right (right mover) or left | Bunkers et al. (2000) |
| Freezing, -20 degC level (m AGL) | lowest crossing above the ground, linear interpolation | |
| Wet-bulb zero (m AGL) | where the isobaric wet-bulb temperature falls through 0 degC (lowest crossing) | not the pseudo-adiabatic wet bulb of Davies-Jones (2008) |
| Precipitable water (mm) | (1/g) times the integral of specific humidity over pressure, from the surface to the top level with a reported humidity (the top pressure is shown) | |

The launch **time**, the **station** and a **drift note** are always shown: when the file has per-level
positions and times the balloon's drift and the duration of the ascent are stated; otherwise the sounding
says it is treated as a vertical line over the station "although a real balloon drifts with the wind".
A sounding with fewer than 40 levels is flagged as approximate (CAPE, CIN and helicity depend on the vertical
resolution). The parcel's path is **not** drawn on the Skew-T (the numbers must come from one source); the LCL, LFC and EL of the selected parcel are marked at their pressure.

### How it was checked

The sounding calculations were checked against known answers:

* **Precipitable water** of a real sounding (BMX, Birmingham AL, 2022-03-30 00 UTC) against the value the
  University of Wyoming archive stores for it (22.26 mm): within 3 %. The reference profile is the public NWS radiosonde levels as archived there.
* **Helicity** of a circular hodograph arc: exactly R^2 times the angle swept (Davies-Jones et al. 1990),
  opposite sign for backing winds, zero for straight shear.
* **Bunkers motion**: the deviation is 7.5 m/s, perpendicular to the shear vector, to its right; the two
  movers are symmetric about the mean wind.
* **Bulk shear** of linear winds, **freezing level** of a linear temperature profile, a constant-humidity
  **precipitable water** column: exact answers.
* **LCL** against the rule of thumb of 125 m per K of dew-point depression (Lawrence 2005, *BAMS* 86, 225-233):
  within 8 %.
* **CAPE and CIN** of the analytic Weisman and Klemp (1982) sounding against an **independent** numerical
  parcel integration written without radarx (moist-adiabatic lapse rate of Rogers and Yau 1989, 5 m steps):
  CAPE within 15 %, CIN within 15 J/kg. Neglecting the virtual-temperature correction lowers CAPE, as
  Doswell and Rasmussen (1994) describe.
* All formats give the same profile and the same numbers; malformed files give a clear error.

### Command line

```bash
rxd sounding -i BMX_20220330_00Z.csv --wind-units kt          # labelled table
rxd sounding -i BMX_20220330_00Z.csv --wind-units kt -v       # + definitions and references
rxd sounding -i sonde.csv --json                              # everything, machine-readable
rxd sounding -i sonde.csv -o result.json -o profile.csv       # full result / profile levels with a provenance header
```

Same options as every command: `-i`, `-o`, `-f/--format`, `--json`, `-v`, `-q`. Exit codes: 3 for an unreadable
file, 4 when radarx is missing (the parcel thermodynamics need `pip install radarx`).

## Limits, honestly

* **No model, reanalysis, satellite or gauge provider ships yet.** The framework, the local-files provider,
  one observed-sounding archive (IEM) and the test harness do. The IEM request has been exercised only against
  recorded samples and a simulated server; the real service is not part of the automated checks.
* The IEM terms of use are linked in the provider's licence text, not reproduced; read them before relying on it.
* A sounding is not one place and one time: the balloon rises at about 5 m/s and drifts with the wind. The panel
  says so and, where the file has the track, gives the drift.
* Parcel numbers are those of the pseudo-adiabatic, virtual-temperature parcel of Bolton (1980) as implemented in
  radarx; other software (SHARPpy, MetPy, Wyoming) uses other options and gets different CAPE for the same
  profile. `--no-virtual-temperature` shows how much one choice matters.
* The Skew-T background curves are computed in the app from textbook formulas for reference; they are not data.
* A cache entry is verified by size on every use and by checksum on request; a file damaged *and* resized to the
  same length by another program would only be caught by a checksum check.
