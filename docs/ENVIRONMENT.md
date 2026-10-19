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
* [Writing a provider](#writing-a-provider)
* [Protocol](#protocol-for-tooling)
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
4. **Polite to servers.** An honest `User-Agent` (`xradar-desktop/<version> (+https://github.com/syedhamidali/xradar-desktop)`),
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

Environment data is a desktop feature. In the public web service the `env_*` and `sounding_open`
messages are refused (a service must not download on a visitor's behalf or read its own disk).

## Provenance: what kind of numbers is this?

Every layer the engine sends carries a `provenance` object, and the **Layers** panel lists every
layer the app holds with a colour-coded **chip**. The class is always written out next to the colour.

| Chip | Class | Meaning | Colour token |
|---|---|---|---|
| `OBS` | **observed** | Measured by an instrument: radar moments, a radiosonde, a gauge. | `--c-prov-observed` = `--c-nominal` (green) |
| `MODEL` | **model** | Output of a numerical weather prediction or reanalysis (HRRR, ERA5): an estimate on a grid, not a measurement. | `--c-prov-model` = `--c-accent` (cyan) |
| `RETR` | **retrieved** | Derived from observations by an algorithm or physical model: hydrometeor class, rain rate, multi-Doppler winds, storm cells, a gridded composite. | `--c-prov-retrieved` = `--c-derived` (violet) |
| `SIM` | **simulated** | Produced by a forward model or synthetic generator: not a real observation. | `--c-prov-simulated` = `--c-caution` (amber) |
| `STYLE` | **stylised** | Drawn for looks (clouds, shading, exaggeration): not a data value. The realistic 3D look is listed as stylised. | `--c-prov-stylised` (neutral, dashed outline) |

The tokens are aliases of the design tokens (`src/lib/styles/provenance.css`), so both themes follow.

Hover or focus a chip (click to pin, Escape to close) to see: the class definition, **source**,
**valid time** and run time (UTC), the **time offset to the active radar scan**, resolution, **method**,
parameters, what it was made from, **uncertainty**, references, credit and licence. A layer without
provenance shows a dashed `?` chip and says that nothing is known.

### The provenance object

```json
{"schema": 1, "kind": "retrieved", "source": "xradar-desktop module radarx_hid (radarx 0.5.1)",
 "title": "Hydrometeor class", "run_time": "2026-10-19T11:21:00Z", "valid_time": "2013-05-20T20:16:43Z",
 "method": "Computed by the 'radarx_hid' module from the radar moments of the open file.",
 "parameters": {"freezing_level_m": 4000.0}, "uncertainty": "Inherits the measurement errors ...",
 "references": ["..."], "inputs": ["DBZH", "ZDR"]}
```

Only `kind` and `source` are mandatory; empty fields are left out. Times are UTC ISO-8601 with a `Z`.
Python: `engine.layer_provenance` (`LayerProvenance`, one builder per kind of layer, `attrs()` for exports).
Frontend: `src/lib/utils/provenance.ts` (vocabulary, tolerant parser), `utils/layerRegistry.ts` (pure rules),
`stores/layers.ts`, `ProvenanceChip.svelte`, `LayersPanel.svelte`.

Where it is attached: `file_opened` (and the `sweep_data` header), `derived_variables`, `module_field`,
`mosaic_result`, `multidoppler_result`, `wind_volume`, `cells_identified`, `cross_section_result`,
`cross_section_3d_data`, `volume_data`, `sounding_result`, `env_fetch_result`.

### In exported files

Files carry the same information, so a figure or dataset passed on still says what it is:
NetCDF/Zarr global attributes `data_class`, `data_source`, `data_valid_time`, `data_run_time`,
`data_resolution`, `data_method`, `data_parameters`, `data_uncertainty`, `data_references`, `data_inputs`,
`data_attribution`, `data_licence` and `data_class_definition`; derived variables carry them per
variable; CSV exports get the same as `# data_class: ...` header lines; figures get `data class: ...`
in their comment. Built by `engine.provenance.standard_global_attrs(layer=...)`,
`csv_comment_block(layer=...)` and `figure_metadata(layer=...)`.

## Using environment data

In the **Sounding** panel, *Environment data settings* shows the cache (used / limit, folder), the
offline switch, the list of sources with their credit and licence, and **Clear the cache**.

| Setting | Default | Notes |
|---|---|---|
| Cache folder | `~/.xradar-desktop/environment-cache` | any absolute folder |
| Cache size limit | 2048 MB | 16 MB .. 4 TB; lowering it evicts the least recently used files at once |
| Offline mode | off | |

Settings are saved in `~/.xradar-desktop/environment.json` (`$XRADAR_ENV_SETTINGS` for another file).
Environment variables win over the file and lock the control in the UI: `XRADAR_OFFLINE=1`,
`XRADAR_ENV_CACHE_DIR`, `XRADAR_ENV_CACHE_MAX_MB`.

What the approval dialog looks like (the numbers are an example):

```text
Download data?                                   [MODEL] Fake model (recorded sample)
From      data.fake.invalid
Download  4 files, 800 KB
Cached    1 of 5 files already in the cache (200 KB), not downloaded again
Saved to  /Users/you/.xradar-desktop/environment-cache
Credit    Synthetic test data generated by xradar-desktop; not real weather.
Licence   CC0 (generated)
Nothing is downloaded until you press Download. Closing this window downloads nothing.
                                                   [ Download ] [ Cancel ]
```

If some file sizes are not known in advance the dialog says so ("at least 3 MB; a limit of ... applies")
and the transfer is stopped at that limit. If the request is larger than the cache limit, or offline mode is
on and files are missing, the dialog says why and offers no Download button.

## Soundings

**Sounding** panel (right-hand side) and `xrd sounding`: a radiosonde profile as a **Skew-T log-p diagram**
and a **hodograph**, with the parameters below. The diagram is drawn by the app (SVG, no extra
dependency, themed, lazy-loaded).

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
| CAPE, CIN (J/kg) | integral of the virtual-temperature buoyancy of a lifted, pseudo-adiabatic parcel above / below its level of free convection | Bolton (1980); Doswell and Rasmussen (1994) via `radarx.io.parcel` |
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
resolution). The parcel's path is **not** drawn on the Skew-T (the engine does not return it, and the
numbers must come from one source); the LCL, LFC and EL of the selected parcel are marked at their pressure.

### How it was checked

`python/tests/test_sounding.py`:

* **Precipitable water** of a real sounding (BMX, Birmingham AL, 2022-03-30 00 UTC) against the value the
  University of Wyoming archive stores for it (22.26 mm): within 3 %. The fixture
  `python/tests/data/BMX_20220330_00Z.csv` holds the public NWS radiosonde levels as archived there.
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

### Command line and Python

```bash
xrd sounding -i BMX_20220330_00Z.csv --wind-units kt          # labelled table
xrd sounding -i BMX_20220330_00Z.csv --wind-units kt -v       # + definitions and references
xrd sounding -i sonde.csv --json                              # everything, machine-readable
xrd sounding -i sonde.csv -o result.json -o profile.csv       # full result / profile levels with a provenance header
```

```python
import xrd
d = xrd.sounding("BMX_20220330_00Z.csv", wind_units="kt")
d["diagnostics"]["parcels"]["most-unstable"]["cape"], d["provenance"]["kind"]
```

Same options as every command: `-i`, `-o`, `-f/--format`, `--json`, `-v`, `-q`. Exit codes: 3 for an unreadable
file, 4 when radarx is missing (the parcel thermodynamics need `pip install radarx`).

## Writing a provider

A provider describes a source and says which files a request needs; the framework does the rest
(estimate, approval, cache, checksums, retries, offline, cancel, progress).

```python
from typing import ClassVar
from engine.environment import FileSpec, InventoryItem, Provider, Request, register_provider

class HrrrSubset(Provider):
    id: ClassVar[str] = "hrrr"
    name: ClassVar[str] = "HRRR (NOAA, on AWS)"
    kind: ClassVar[str] = "model"                      # a provenance class
    attribution: ClassVar[str] = "NOAA / NCEP HRRR, via the NOAA Open Data Dissemination programme."
    licence: ClassVar[str] = "..."                     # the source's own words, with a link
    homepage: ClassVar[str] = "https://registry.opendata.aws/noaa-hrrr-pds/"
    unknown_size_cap: ClassVar[int] = 200 * 1024 * 1024   # per file whose size is not known

    def inventory(self, query=None):
        return [InventoryItem(product="sfc", title="Surface fields", variables=("t2m", "dpt2m"),
                              resolution="3 km", cadence="hourly")]

    def plan(self, request: Request, env) -> list[FileSpec]:
        # Ask the server for the SUBSET (area, time, variables), never for more than needed.
        # May make small listing requests through env.http (polite, offline-aware).
        return [FileSpec(key=f"{url}#{request.digest()[:8]}", name=..., url=url, size=exact_bytes_or_None,
                         sha256=published_checksum_or_None) for url in self._urls(request)]

register_provider(HrrrSubset())   # at import time of the module that defines it
```

* `FileSpec.key` is the cache identity: make it unique per *content* (include the subset).
  `size` is a promise: set it only when exact, otherwise leave `None`.
* Override `download_file(spec, dest, env, *, max_bytes, progress, cancel)` for byte-range or
  server-side subsetting; the default is a plain HTTP GET through the polite client.
* Override `provenance(request, specs)` to give the method text precisely; the default uses the class,
  name, attribution and licence.
* A source of **local** files sets `requires_network = False` and `local_path(spec)` (see
  `engine/environment/providers/local_files.py`): no download, no approval.
* `engine.environment.subset_dataset(ds, request)` cuts an opened dataset to the area, time window and
  variables with the same semantics everywhere (0..360 or -180..180 longitudes handled).
* From the UI: `const result = await requestEnvData({provider: 'hrrr', ...})` (`src/lib/stores/environment.ts`)
  runs estimate -> approval dialog -> fetch and resolves with the files and their provenance, or `null`.

### Testing a provider (no network, ever)

```python
from engine.environment import Environment, EnvSettings, HttpClient, Request
from engine.environment.providers.fake import FakeServer, FakeProvider, make_catalogue

server = FakeServer(make_catalogue(), fail_first={"fake_t2m_202203300001.json": 2})  # scripted 503s
env = Environment(EnvSettings(cache_dir=str(tmp_path / "cache")))
env.http = HttpClient(opener=server.opener, offline=lambda: env.offline, min_interval_s=0)
env.registry.register(FakeProvider(server))
est = env.estimate(Request(provider="fake", variables=("t2m",)))   # nothing is downloaded yet
env.fetch(est.request, est.approve())
```

`FakeServer` is an in-memory transport for the **real** HTTP client, so retries, backoff, size caps, offline
mode and cancellation run for real. It can fail the first N requests of a file, truncate, corrupt or
over-deliver. Tests that need the real service are marked and skipped unless `XRADAR_NETWORK_TESTS` is set
(only an opt-in CI job sets it). `XRADAR_ENV_FAKE=1` registers the fake provider in a running engine, to try
the approval dialog by hand (`scripts/perf/smoke_environment.py` does).

## Protocol (for tooling)

```
-> {type:"env_status"}                                   <- {type:"env_status", providers:[{id,name,kind,attribution,licence,
                                                              homepage,requires_network,inventory}], settings, cache}
-> {type:"env_estimate", request_id, request:{provider, product?, bbox?, time_start?, time_end?, variables?, params?}}
                                                         <- {type:"env_estimate_result", request_id, estimate:{files, total_bytes,
                                                              network_bytes, size_exact, destination, attribution, licence, source,
                                                              requires_approval, can_fetch, reason, notes}}
-> {type:"env_fetch", request_id, approve:true}          <- {type:"env_progress", request_id, percent, message, file, bytes_done, bytes_total}
                                                            ... {type:"env_fetch_result", request_id, result:{paths, from_cache,
                                                              bytes_downloaded, attribution, licence, provenance, checksums}}
                                                            | {type:"env_cancelled", request_id}
                                                            | {type:"env_error", request_id, code, message}
-> {type:"env_cancel", request_id}
-> {type:"env_set_settings", settings:{cache_dir?, cache_max_mb?, offline?, max_concurrent?}}   <- env_status
-> {type:"env_clear_cache"}                                                                     <- env_status (+ freed_bytes)
-> {type:"sounding_open", request_id, path, format?, wind_units?} | {..., example:true}
                                                         <- {type:"sounding_result", request_id, sounding} | {type:"sounding_error", request_id, message}
```

`env_fetch` is refused (`code: "approval_required"`) unless an `env_estimate` with the same `request_id` came
first and `approve` is `true`; the approval is the one made from *that* estimate. Error codes: `approval_required`,
`offline`, `too_large`, `checksum`, `failed`, `invalid`, `unknown_provider`, `unavailable` (web mode), `internal`.

## Limits, honestly

* **No model, reanalysis, satellite or gauge provider ships yet.** The framework, the local-files provider,
  one observed-sounding archive (IEM) and the test harness do. The IEM request has been exercised only against
  recorded samples and the fake server in the automated tests; the one test that calls the real service is
  opt-in (`XRADAR_NETWORK_TESTS`).
* The IEM terms of use are linked in the provider's licence text, not reproduced; read them before relying on it.
* A sounding is not one place and one time: the balloon rises at about 5 m/s and drifts with the wind. The panel
  says so and, where the file has the track, gives the drift.
* Parcel numbers are those of the pseudo-adiabatic, virtual-temperature parcel of Bolton (1980) as implemented in
  radarx; other software (SHARPpy, MetPy, Wyoming) uses other options and gets different CAPE for the same
  profile. `--no-virtual-temperature` shows how much one choice matters.
* The Skew-T background curves are computed in the app from textbook formulas for reference; they are not data.
* Environment data and local sounding files are not available in the public web service.
* A cache entry is verified by size on every use and by checksum on request; a file damaged *and* resized to the
  same length by another program would only be caught by a checksum check.
