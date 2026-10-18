# Multi-Doppler wind retrieval (dual / multi-radar)

Retrieve the 3-D wind (`u`, `v`, `w`) from the radial velocities of two or more Doppler radars that
scan the same weather. The retrieval is done by [radarx](https://github.com/syedhamidali/radarx)
(`radarx.retrieve.multidoppler`); the radars are cone-gridded onto one Cartesian grid with
`multi_doppler_input` (which uses `grid_radars`, the same gridder as the mosaic). Needs
`pip install radarx` (Python 3.12+).

## Use it
1. **Open the radars.** Open the files of two or more radars (or fetch them from Cloud Data). Their
   radial velocity must be **dealiased**; run a dealiasing module first if the velocities fold.
   Volumes should be taken within a few minutes of each other (the panel flags larger gaps).
2. **Wind panel** (right sidebar): tick the radars (those without velocity are disabled), check the
   grid and options, press **Retrieve wind**. Progress streams; **Cancel** stops the run at its next
   stage.
3. The result is drawn on the 2-D map at the chosen height: the selected field as colours (speed by
   default) with wind arrows. A height slider, field selector, opacity, a *minimum crossing angle*
   filter (and, with uncertainty, a *maximum error* filter) change the map instantly, without
   re-running. A vertical cross-section (default `w`, W-E or S-N slice) is drawn in the panel.
4. **NetCDF** export (desktop app) writes all fields on `(z, y, x)` with 2-D `lon`/`lat`.

## Options
| Option | Meaning |
|---|---|
| Method | **Analytic**: the classical dual-Doppler solution, a least-squares fit for 3 or more radars; fast; follows the data where beams cross well. **Variational**: weak-constraint analysis (mass continuity, smoothness, background) that fills small gaps; slower. |
| Min crossing angle | Cells where no pair of beams crosses at least this angle (default 30 degrees) are masked. |
| Vertical boundary | `w = 0` at the lowest level and integrate **upward** (default), at the top and integrate **downward**, or upward with the **O'Brien** adjustment so that `w = 0` at the top. |
| Fall speed | Remove the hydrometeor fall speed from the velocities (needs reflectivity from every radar). With no known freezing level the rain relation is used at all heights and a warning says so; give the freezing level to use the snow relation above it. |
| Background wind | Used by the variational method only: none, fitted from the radars' own velocities (layer-wise VVP-style least squares), uniform `(u, v)`, or a manual `height, u, v` profile. The analytic solution ignores it (the result says so). |
| Storm motion | Advect each volume to the latest volume time before gridding. |
| Uncertainty | Analytic: always (Gauss-Markov error of `u`, `v` for an assumed 1 m/s radial-velocity error). Variational: opt-in, slow (a dozen extra solves). |

**Grid.** Horizontal spacing 0.5-5 km, vertical 250 m-1 km, top height, a maximum range per radar.
By default the area is the bounding box of the region seen by two radars at the minimum crossing
angle and the lowest level is the first multiple of the vertical spacing above the lowest-tilt beams
of both radars over 90 % of that region (so the `w = 0` boundary lies where there are data). A manual
extent is given in km east/north of the mean radar position. `u`/`v` are along the grid axes (east and
north to within a degree over the domain).

## Reading the result
- `coverage_pct`: share of grid cells with a valid wind; `w_valid_pct`: share of those with `w`.
  `w` is integrated up the column and is NaN above the first level with missing divergence, so it is
  patchier than `u`/`v`.
- `crossing_angle` (statistics and field): the larger the better; the error of the horizontal wind
  grows as `1/sin(angle)`. Between the radars on the baseline the beams are collinear (angle 0):
  no wind can be retrieved there.
- `uncertainty` = `sqrt(u_err^2 + v_err^2)`. It covers radial-velocity noise only, not errors from
  gridding, fall speed, dealiasing, or time differences between the radars.
- Divergence and vorticity are in 1e-3 s-1.

Errors are reported in plain language: fewer than two radars, radars at the same site (< 5 km),
no overlapping coverage, beams never crossing at the minimum angle, no radial velocity in a file,
grid too large or too small, missing radarx.

## Notes and limits
- Radarx 0.5.1 (the version in the sidecar venv when this was written) has no `retrieve.vvp`, so
  the "fitted from the radars" background is a small local least-squares fit per height layer.
- The local gridding is fast (tens of ms); typical total time for two volumes at 1 km, 20 levels:
  1-3 s analytic, a few seconds variational.
- Cancelling is cooperative: the run stops between stages (volume preparation, gridding, retrieval);
  a stage already running finishes first.
- Map positions of the grid use the same spherical formulas as the PPI viewer, so cells can differ
  from the WGS84 gridding by up to about 0.5 % of the distance from the grid origin.
- Not included: terrain-following levels, the vertical-vorticity constraint, a 3-D vector layer.

## Tests
`python/tests/test_multidoppler.py` builds **synthetic** radar volumes (two sites 70 km apart, radial
velocities forward-simulated from an imposed wind: uniform plus shear, a non-divergent vortex, a
convergent anelastic flow with a known `w`, and a fall-speed case) and checks that the recovered
`u`/`v`/`w` match the imposed wind in the well-observed region, plus the error cases and the
protocol. No real two-radar data set is used; the end-to-end flow with real radar files has only
been exercised with one real volume duplicated at a shifted site (plumbing, not physics).

## Protocol
```
→ {type:"run_multidoppler", request_id, file_ids,
   grid:{dx_m, dz_m, z_min_m?, z_max_m, max_range_km, origin?:{lat,lon}, extent?:{x_min_km,x_max_km,y_min_km,y_max_km}},
   options:{method:"analytic"|"variational", min_crossing_angle, boundary, fall_speed_correction,
            freezing_level:"auto"|"rain"|metres, melting_depth_m,
            background:{mode:"none"|"vvp"|"uniform"|"manual", u?, v?, profile?:[{height_m,u,v}]},
            storm_motion?:[u,v], uncertainty?, obs_error?, w_bottom?}}
← progress…, {type:"multidoppler_result", request_id, wind_id, fields:[{name,units,long_name}…],
   shape:[n_fields,nz,ny,nx], grid:{nx,ny,nz,x,y,dx,dy,z[],origin_lat,origin_lon,west,south,east,north},
   radars, coverage_pct, dual_coverage_pct, w_valid_pct, crossing_angle:{min,mean,median,max},
   field_stats, background, warnings, timing, elapsed_ms, byte_length, chunks}
   + `chunks` binary frames: one float32 array of `shape`, C order
   (fields: u v w speed divergence vorticity uncertainty crossing_angle n_radars dbz)
← {type:"multidoppler_error", request_id, message}   | {type:"multidoppler_cancelled", request_id}
→ {type:"multidoppler_cancel", request_id}
→ {type:"export_multidoppler", wind_id, path, overwrite?}   ← {type:"file_saved", path}
```
