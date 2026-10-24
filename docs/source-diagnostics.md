# Environment Diagnostics

Thermodynamic and kinematic parameters computed from a vertical profile (radiosonde, HRRR
column, ERA5 column or any file supported by `rxd sounding`).

## What is computed

Every number carries its definition, assumptions and references.

### Parcels (surface-based, mixed-layer 100 hPa, most-unstable 300 hPa)

| Field | Definition |
|-------|-----------|
| CAPE (J/kg) | Buoyancy energy from the LFC to the EL; virtual-temperature correction (Doswell and Rasmussen 1994) |
| CIN (J/kg) | Negative buoyancy from the parcel start to the LFC (zero or negative) |
| LCL (m AGL) | Lifting condensation level (Bolton 1980, eq. 15) |
| LFC (m AGL) | Level of free convection; `None` if the parcel never becomes buoyant |
| EL (m AGL) | Equilibrium level; shown as the sounding top if the parcel is still buoyant there (CAPE is then a lower bound) |
| Lifted Index (K) | Environment minus parcel temperature at 500 hPa (Galway 1956); negative = unstable |

Thermodynamics: radarx `io.parcel.parcel_diagnostics` (Bolton 1980 equations, compiled C++ kernel).

### Kinematics

| Field | Definition |
|-------|-----------|
| Bulk shear 0-1/0-3/0-6 km (m/s) | Vector wind difference between layer top and bottom, linearly interpolated |
| Bunkers storm motion (m/s) | Right and left movers: 0-6 km mean wind displaced 7.5 m/s perpendicular to the shear vector (Bunkers et al. 2000) |
| SRH 0-1/0-3 km (m2/s2) | Storm-relative helicity for the right and left Bunkers movers (Davies-Jones 1984, exact for linear winds between levels) |

Kinematics: radarx `retrieve.wind_profile` when available; the local implementations are identical and serve as the fall-back.

### Levels

| Field | Definition |
|-------|-----------|
| Freezing level (m AGL) | Height of the 0 degC isotherm (lowest crossing; linear interpolation) |
| -10 degC / -20 degC level (m AGL) | Same for colder isotherms |
| Wet-bulb zero (m AGL) | Height where the isobaric wet-bulb temperature falls through 0 degC |

### Lapse rates

| Field | Definition |
|-------|-----------|
| 0-3 km (K/km) | Mean environmental temperature gradient in the lowest 3 km AGL; relevant for convective initiation |
| 700-500 hPa (K/km) | Mid-level lapse rate; relevant for conditional instability (values near or above 7-8 K/km are steep) |

Reference: dry adiabatic lapse rate = 9.8 K/km. Layers exceeding this are super-adiabatic
(absolutely unstable) and are flagged in the notes. The standard atmosphere mid-tropospheric
value is about 6.5 K/km.

### Moisture

| Field | Definition |
|-------|-----------|
| Precipitable water (mm) | (1/g) times the integral of specific humidity over pressure from the surface to the top level with a reported dew point; only levels with a reported (not filled) dew point count |

Specific humidity from dew point: radarx `io.sounding.specific_humidity_from_dewpoint`.

## Provenance

Every call to `diagnose()` or `rxd sounding` attaches a provenance record:
- `kind`: `"observed"` for radiosondes, `"model"` for HRRR / ERA5 columns
- `source`, `method`, `uncertainty`: text visible in the UI provenance chips
- `library.radarx`: the version of radarx that computed the thermodynamics

## Limits

- A sounding with fewer than 40 levels is flagged as approximate.
- CAPE, CIN, LCL, LFC and EL need radarx (`pip install radarx`); shear and lapse rates do not.
- Wet-bulb zero needs a complete humidity column.
- Lapse rates are `None` when the layer is not spanned by the valid levels.
- 0-6 km shear and Bunkers motion are `None` when winds end below 6 km AGL.
- Model columns have no balloon drift; a drift note is shown for radiosondes.

## References

- Bolton (1980): parcel thermodynamics, doi:10.1175/1520-0493(1980)108<1046:TCOEPT>2.0.CO;2
- Doswell and Rasmussen (1994): virtual-temperature correction, doi:10.1175/1520-0434(1994)009<0625:TEONTV>2.0.CO;2
- Galway (1956): lifted index, doi:10.1175/1520-0477-37.10.528
- Bunkers et al. (2000): storm motion, doi:10.1175/1520-0434(2000)015<0061:PSMUAN>2.0.CO;2
- Davies-Jones (1984): SRH, doi:10.1175/1520-0469(1984)041<2991:SVTOOU>2.0.CO;2
