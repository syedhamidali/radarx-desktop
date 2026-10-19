# Variable mapping

Radar files name the same field differently (`DBZH`, `DBZ`, `reflectivity`,
`corrected_reflectivity_horizontal`, `DBZHCC` ...). The **variable mapping layer**
(`python/engine/varmap.py`) is the one place that knows which file variable plays
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
`varmap.describe_roles()` returns the whole registry; add a role or alias by editing
`ROLES` in `varmap.py` (nothing else).

## Resolution order

For a role on one sweep dataset, the first hit wins:

1. **override** - the user's mapping for this file (confidence 1.0)
2. **cf** - `standard_name` match, and `units` of a compatible class (0.95)
3. **alias** - exact variable name from the alias list, then case-insensitive (0.9)
4. **pattern** - regular expression on the variable name (0.6)

Rules inside steps 2-4:

* Raw names are tried before corrected variants. `DBZH` beats `DBZHCC` when both
  exist; `DBZHCC` is used when it is all there is (as before). Pass
  `prefer_corrected=True` (`ctx.field(role, prefer_corrected=True)`) to flip that.
* A variable whose own `standard_name` belongs to a *different* role is never
  matched by name (a variable called `V` that says it is spectrum width is not velocity).
* Flag/mask companions (`DBZHC_F`, `*_FLAG`, `*_MASK`, `*_QC`) never match a pattern.
* Unknown or empty `units` never veto a CF match; a known, different unit class does.

The result is a `Match(role, variable, kind, confidence, sweep, corrected)`.

## Cross-sweep (paired sweep) resolution

NEXRAD split cuts keep velocity on a separate Doppler sweep at the same elevation: in
a KTLX volume sweep 0 has `DBZH/ZDR/PHIDP/RHOHV` and sweep 1 has `DBZH/VRADH/WRADH`.

`varmap.find_paired_sweep(tree, sweep, role)` looks for the role on the sweep with the
nearest fixed angle (`sweep_fixed_angle`, else the mean elevation) within a tolerance
(default 0.3 deg). Ties go to the closest sweep index. `varmap.align_to_sweep` resamples
that field onto the requested sweep's `(azimuth, range)` grid (nearest ray with wrap
around 360 deg, nearest gate, NaN outside the source's coverage).

## In modules

```python
@module(id="my_shear", title="Shear", requires=["VRADH"], paired=["VRADH"])
def run(ctx):
    v = ctx.field("velocity")            # role key, legacy name (VRADH) or any alias
    z = ctx.field("DBZH")                # still works for every existing module
    w = ctx.find("width", allow_paired_sweep=True)   # explicit opt-in per call
    ...
```

* `ctx.field(name)`, `ctx.find(name)`, `ctx.has(name)` accept role keys, legacy names and
  aliases. Legacy behaviour is unchanged when the variable is on the sweep.
* `allow_paired_sweep=True` falls back to the paired sweep and returns the data already
  aligned to this sweep's grid. `@module(paired=[...])` makes that the default for the
  listed roles (also for the "missing required field" check and the all-sweeps run).
* `ctx.match(name)` returns the `Match` (how it resolved, and from which sweep).
* Whenever data came from another sweep, `ctx.notes` gets e.g. *"velocity taken from
  sweep 1 (VRADH, 0.00 deg from this sweep's elevation)"*. `run_module` adds it to the
  payload as `notes` and as a "Data sources" text block.
* `radarx_dealias` and `radarx_shear` declare `paired=["VRADH"]`, so they run on the
  surveillance cut of a split cut. `modules/_radarx_adapter.prepare_sweep` puts the
  aligned paired field into the sweep dataset under its legacy name.

`engine.plugins.VAR_ALIASES` still exists as a deprecated read-only shim generated from
the registry.

## User overrides

Protocol (WebSocket JSON):

```javascript
// -> sidecar
{"type": "set_variable_mapping", "file_id": "...", "mapping": {"velocity": "VEL_DEALIASED"}}
//    a blank value (or "auto") clears a role; "replace": true replaces the whole mapping
{"type": "get_variable_mapping", "file_id": "...", "sweep": 0}

// <- sidecar (both answer with the same message)
{"type": "variable_mapping", "file_id": "...", "sweep": 0, "overrides": {...},
 "roles": [{"role": "velocity", "legacy": "VRADH", "label": "velocity",
            "override": null, "variable": "VRADH", "kind": "cf", "confidence": 0.95,
            "sweep": 1, "corrected": false}, ...],
 "variables": ["DBZH", "VRADH", ...]}
```

`sweep` in a role entry is `null` when the variable is on the requested sweep and the
paired sweep's index when it was found there. Role keys and legacy names (`VRADH`) are both
accepted; an unknown role is an error.

Overrides are kept per open file and persisted per file path in
`~/.xradar-desktop/variable_maps.json` (`$XRADAR_VARMAP_FILE` overrides the location), so
reopening the file restores them. An override naming a variable that is not on a given
sweep falls through to automatic matching for that sweep. They apply to modules, the
dual-pol dashboard (scatter, histogram, region stats), retrievals and "run on all sweeps".

In the app: **Data inspector > Variable mapping** lists every role, the resolved
variable, how it matched (and the paired sweep, if any) and a dropdown to override it.
The Modules panel uses the same resolution, so a module is no longer greyed out when the
sidecar can resolve its inputs (CF name, pattern, override, paired sweep).

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
* The Modules panel keeps a small client-side alias table as a fallback when the sidecar
  has not answered yet; the sidecar's answer is authoritative.
