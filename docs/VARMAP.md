# Variable mapping

Radar files name the same field differently (`DBZH`, `DBZ`, `reflectivity`,
`corrected_reflectivity_horizontal`, `DBZHCC` ...). The **variable mapping layer**
is the one place that knows which file variable plays
which radar **role**. Modules, the dual-pol tools, retrievals, derived-variable runs
and the mosaic all resolve fields through it; there are no other alias tables.

## Roles

| Role (key) | Legacy name | CF `standard_name` (examples) | Unit classes |
|---|---|---|---|
| `reflectivity` | `DBZH` | `radar_equivalent_reflectivity_factor_h` | dBZ, dB |
| `zdr` | `ZDR` | `radar_differential_reflectivity_hv` | dB |
| `phidp` | `PHIDP` | `radar_differential_phase_hv` | deg |
| `kdp` | `KDP` | `radar_specific_differential_phase_hv` | deg/km |
| `rhohv` | `RHOHV` | `radar_correlation_coefficient_hv` | unitless |
| `velocity` | `VRADH` | `radial_velocity_of_scatterers_away_from_instrument_h` | m/s |
| `width` | `WRADH` | `radar_doppler_spectrum_width_h` | m/s |
| `snr` | `SNR` | `signal_to_noise_ratio` | dB |
| `ncp` | `NCP` | `normalized_coherent_power` | unitless |

Each role also has an ordered list of name **aliases**, a list of **regular
expressions** (full match, case-insensitive) and a list of **corrected** variants
(clutter-corrected, qc'd or dealiased names such as `DBZHCC`, `ZDRC`, `VRADDH`).

## Resolution order

For a role on one sweep dataset, the first hit wins:

1. **override** - the user's mapping for this file (confidence 1.0)
2. **cf** - `standard_name` match, and `units` of a compatible class (0.95)
3. **alias** - exact variable name from the alias list, then case-insensitive (0.9)
4. **pattern** - regular expression on the variable name (0.6)

Rules inside steps 2-4:

* Raw names are tried before corrected variants. `DBZH` beats `DBZHCC` when both
  exist; `DBZHCC` is used when it is all there is (as before). A module can ask for the corrected variant to be preferred instead.
* A variable whose own `standard_name` belongs to a *different* role is never
  matched by name (a variable called `V` that says it is spectrum width is not velocity).
* Flag/mask companions (`DBZHC_F`, `*_FLAG`, `*_MASK`, `*_QC`) never match a pattern.
* Unknown or empty `units` never veto a CF match; a known, different unit class does.

The result says which variable, how it matched (override, cf, alias or pattern), the confidence, the sweep and whether it is a corrected variant.

## Cross-sweep (paired sweep) resolution

NEXRAD split cuts keep velocity on a separate Doppler sweep at the same elevation: in
a KTLX volume sweep 0 has `DBZH/ZDR/PHIDP/RHOHV` and sweep 1 has `DBZH/VRADH/WRADH`.

The mapping looks for the role on the sweep with the
nearest fixed angle (else the mean elevation) within a tolerance
(default 0.3 deg). Ties go to the closest sweep index. That field is then resampled onto the requested sweep's `(azimuth, range)` grid (nearest ray with wrap
around 360 deg, nearest gate, NaN outside the source's coverage).

## In modules

* A module asks for a **role** (`velocity`), a legacy name (`VRADH`) or any alias; they all resolve the same way.
  Legacy behaviour is unchanged when the variable is on the sweep.
* A module that can use velocity from the paired sweep (`radarx_dealias`, `radarx_shear`) falls back to it and gets the
  data already aligned to this sweep's grid. This also applies to the "missing required field" check and to the
  all-sweeps run.
* Whenever data came from another sweep, the result says so, for example *"velocity taken from sweep 1 (VRADH, 0.00 deg
  from this sweep's elevation)"*, in a "Data sources" text block.

## User overrides

A blank value (or "auto") clears a role. Role keys and legacy names (`VRADH`) are both accepted; an unknown role is an
error. The resolved variable, how it matched (and the paired sweep, if any) are listed per role.

Overrides are kept per open file and remembered per file path in
`~/.xradar-desktop/variable_maps.json`, so reopening the file restores them. An override naming a variable that is not
on a given sweep falls through to automatic matching for that sweep. They apply to modules, the dual-pol dashboard
(scatter, histogram, region stats), retrievals and "run on all sweeps".

In the app: **Data inspector > Variable mapping** lists every role, the resolved variable, how it matched (and the
paired sweep, if any) and a dropdown to override it. The Modules panel uses the same resolution, so a module is no
longer greyed out when its inputs can be resolved (CF name, pattern, override, paired sweep).

## Error text

When nothing resolves the error names the role, what was searched and what exists:

> No velocity field found on sweep 0 (searched CF standard_name
> radial_velocity_of_scatterers_away_from_instrument_h, names VRADH, velocity, V,
> radial_velocity, VEL, VRAD, ... (+1) and name patterns). Variables on sweep 0: DBZH,
> ZDR, PHIDP, RHOHV. Sweep 1 (same elevation, within 0.3 deg) has VRADH: select that
> sweep, or use a module that accepts paired sweeps. You can map a variable to this role
> under 'Variable mapping'.

## Limits

* Lazily loaded ARCO sweeps expose no variable list until they are loaded, so the paired
  lookup only sees sweeps that are already in memory.
* Mosaics resolve fields per volume without user overrides.
