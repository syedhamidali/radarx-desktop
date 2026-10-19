# Multi-Doppler wind retrieval (dual / multi-radar)

Retrieve the 3-D wind (`u`, `v`, `w`) from the radial velocities of two or more Doppler radars that
scan the same weather. The retrieval is done by [radarx](https://github.com/syedhamidali/radarx)
(`radarx.retrieve.multidoppler`); the radars are cone-gridded onto one Cartesian grid with
`multi_doppler_input` (which uses `grid_radars`, the same gridder as the mosaic). Needs
`pip install radarx` (Python 3.12+).

```{image} images/ui/wind-panel.png
:alt: The wind panel
:width: 30%
:align: right
```

The *Wind* panel lists the open radars, sets the analysis grid (spacing, top height, maximum range), the method
(analytic or variational), the minimum crossing angle and the background wind, and runs the retrieval. It needs
the files of at least two radars that scan the same weather.


## Use it
1. **Open the radars.** Open the files of two or more radars (or fetch them from Cloud Data). Aliased
   (folded) velocities are unfolded automatically before the retrieval (option *Dealias*), so you do
   not need to run a dealiasing module first. Volumes should be taken within a few minutes of each
   other (the panel flags larger gaps).
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
| Dealias | **Auto** (default): every Doppler sweep up to 12 degrees elevation that looks aliased is unfolded (radarx region-based unfolding) before gridding; sweeps that do not look aliased are left alone. **Always**: unfold all of them. **Never**: use the velocities as stored (the result then warns when they look aliased). A sweep "looks aliased" when more than 0.3 % of its gate-to-gate velocity steps exceed 1.2 x the Nyquist velocity (a fold line is a step of about 2 x Nyquist). The Nyquist velocity comes from the file when it is stored, otherwise it is estimated as the largest valid `abs(V)` of the sweep. Aliased sweeps above 12 degrees are left out of the analysis. The opened file is never modified. |
| Vertical boundary | **Auto** (default): integrate `w` **upward** (`w = 0` at the lowest level) when at least 60 % of the columns that have data are covered by two radars at the lowest two levels, otherwise **downward** (`w = 0` at the top of the grid; for the variational method at one level above each column's echo top). Also downward for the variational method when the lowest level is more than 200 m above the highest radar (radarx warns that `w = 0` there is wrong). The choice and its reason are in the notes. You can force **upward**, **downward**, or upward with the **O'Brien** adjustment so that `w = 0` at the top. |
| Output checks | Impossible values are set to missing (NaN) after the retrieval and before the derived fields: cells with a horizontal speed above `max_speed` (default 100 m/s) lose `u`, `v`, `w`; `w` beyond `max_abs_w` (default 40 m/s) is removed; optionally `max_uncertainty` (m/s) removes cells with a larger wind error. `null` switches a check off. The counts and percentages are in the summary (`qc`) and in the warnings. `keep_raw: true` also keeps the unchecked `u`, `v`, `w` (`WindRecord.raw`, written as `u_raw`, `v_raw`, `w_raw` in the NetCDF). |
| Fall speed | Remove the hydrometeor fall speed from the velocities (needs reflectivity from every radar). With no known freezing level the rain relation is used at all heights and a warning says so; give the freezing level to use the snow relation above it. |
| Background wind | Used by the variational method only: none, fitted from the radars' own velocities (layer-wise VVP-style least squares), uniform `(u, v)`, or a manual `height, u, v` profile. The analytic solution ignores it (the result says so). |
| Storm motion | Advect each volume to the latest volume time before gridding. |
| Uncertainty | Analytic: always (Gauss-Markov error of `u`, `v` for an assumed 1 m/s radial-velocity error). Variational: opt-in, slow (a dozen extra solves). |

**Grid.** Horizontal spacing 0.5-5 km, vertical 250 m-1 km, top height, a maximum range per radar.
By default the area is the bounding box of the region seen by two radars at the minimum crossing
angle. The lowest level is the first multiple of the vertical spacing at which **at least 5 % of the
analysis columns** have two radars whose lowest-tilt beams reach that height and cross at the minimum
angle (the beam geometry decides, not the echo). It used to be the first level every column is seen
at, which left no usable levels for far-apart radars over a large box; the level actually chosen, the
share of columns there and one level lower are in the notes. A manual extent is given in km
east/north of the mean radar position. `u`/`v` are along the grid axes (east and
north to within a degree over the domain).

## Reading the result
- **Notes** (Wind panel, "What was chosen automatically"; `notes` in the result): the lowest level and
  why, the vertical boundary and why, and per radar what the dealiasing did (sweeps unfolded, Nyquist
  velocity and its source, gate-to-gate jumps before and after).
- **Coverage by height** (Wind panel table; `level_coverage`): for every analysis height, the share of
  all grid cells of that level that are seen by 2 or more radars (`dual_pct`), have a retrieved
  horizontal wind (`wind_pct`) and a vertical velocity (`w_pct`). Read it before judging a map: a
  level that is empty is usually empty because the radars cannot see it or their beams cross too
  flat, not because the retrieval failed. With radars 100-190 km away the 2+ radar share was 0.2 % at
  1.5 km, 10 % at 3.5 km and 26 % at 6.5 km (Mayfield case). `w_pct` is lower than `wind_pct` because
  `w` needs an unbroken column to integrate along; with the downward boundary it needs data up to the
  top. Click a row to show that height on the map.
- **Quality control** (`qc`): how many cells the output checks removed. A large share means the input
  is still aliased or the geometry is poor; a handful at the edge of the area is normal.
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

## Figures
`engine/windfig.py`: `plot_wind(record_or_dataset, level_km, out_path=None, theme="dark"|"light",
kind="diagnostic"|"publication")`. *diagnostic* is six panels (reflectivity, speed with vectors,
crossing angle, radars used with range rings, `w`, vorticity); *publication* is three clean panels
(reflectivity with vectors, speed, `w`; choose others with `panels=[...]`). The input is a `WindRecord`
or the Dataset of `to_dataset` / an exported NetCDF. Style rules: wind arrows are **white on dark
backgrounds and dark on light ones, each with an edge of the opposite colour** (`pivot="mid"`, edge
width 0.5), so they read over any colour map; reflectivity uses the `cmweather` `ChaseSpectral` map;
vorticity is smoothed by one grid cell for display. The in-app arrow overlay follows the same rule
from the basemap theme (`arrowColors` in `utils/wind.ts`).

Examples (real NEXRAD, `docs/images/winds/`): `mayfield_before_raw_aliased.png` vs
`mayfield_after_analytic.png` / `mayfield_after_variational.png` / `mayfield_publication_light.png`;
`elreno_before_raw_aliased.png` vs `elreno_after_analytic.png` / `elreno_after_variational.png` /
`elreno_publication_dark.png`.

## Real cases
Mayfield KY 2021-12-11 ~02:04 UTC (KHPX, KNQA, KPAH, KVWX) and El Reno OK 2013-05-31 ~23:15 UTC (KFDR,
KTLX, KVNX; KOUN has no file), domain +-100 km, 1 km x 500 m, max range 230 km, defaults.
Before (velocities as stored, lowest level 500 m, upward boundary, analytic): speeds up to 106 m/s
(Mayfield) and 112 m/s (El Reno), `w` valid in 0 % of the wind cells, a wind field that is
visibly random. After (dealias auto, auto level and boundary, checks on): speeds up to 100 m/s (a
few edge cells; mean 37 and 24 m/s), plausible SW flow, `w` within +-40 m/s. The variational method
gives `w` everywhere in the wind area with realistic values (Mayfield -33..+23 m/s, El Reno
-23..+27 m/s; before up to -258..+104). What stays poor: with radars 100-190 km away the low levels
(below about 3 km) have almost no two-radar coverage; the analytic `w` is valid in only 36 % (Mayfield)
to 84 % (El Reno) of the wind cells and is noisy along the edge where the crossing angle is close to
the minimum; both methods still show edge artefacts in `w` and vorticity at the border of the dual
area; the unfolding assumes a zero mean radial velocity when the azimuth coverage of a sweep is
partial (radarx warns), so it is checked against the other radars' wind profile (sweeps off by whole
Nyquist intervals are shifted) but a sweep can still be wrong; the winds are not independently
validated (no sounding or profiler comparison).

## Notes and limits
- Radarx 0.5.1 (the version in the sidecar venv when this was written) has no `retrieve.vvp`, so
  the "fitted from the radars" background is a small local least-squares fit per height layer.
- The local gridding is fast (tens of ms); typical total time for two volumes at 1 km, 20 levels:
  1-3 s analytic, a few seconds variational.
- Cancelling is cooperative: the run stops between stages (volume preparation, gridding, retrieval);
  a stage already running finishes first.
- Map positions of the grid use the same spherical formulas as the PPI viewer, so cells can differ
  from the WGS84 gridding by up to about 0.5 % of the distance from the grid origin.
- Not included: terrain-following levels, the vertical-vorticity constraint.

## The wind in 3D: flow, arrows, animated field
**View in 3D** (Wind panel) opens the 3D tab on the wind. The toolbar has two toggle buttons, both
fading in and out over about 0.4 s, and the wind row has a third:

- **Flow** (on by default, key `F`): thin, light, translucent trails (colour by speed, w or none)
  that flow along the retrieved wind, like windy / earth.nullschool. Density, opacity, width, trail
  length, visual speed and colour are sliders; pause/play freezes the picture exactly. On a dark
  scene the trails glow (additive blending, pale blue for calm air to pink for strong wind); on a
  light scene they are darker saturated colours with a thin light halo.
- **Animate field** (off by default): the radar echo drifts subtly along the retrieved wind, on
  its own or together with the flow. Only the echo is moved; where there is no wind it stays.
- **Arrows** (off by default): the 3D arrows of the analysis grid.

![Four frames of the flow over the mosaic volume (Mayfield, KY, 2021-12-11 02:08 UTC)](images/wind3d-flow-strip.png)

![Flow of the analysis, animated](images/wind3d-flow.gif)

Flow with *Animate field* on (left: the echo drifts a little along the wind as well), and on a
light scene (right: darker saturated trails with a thin light halo):

![Flow and animated field](images/wind3d-flow-and-field.png)
![Flow on a light scene](images/wind3d-flow-light.png)

Of the two, the flow is the one that reads: it shows direction, convergence and speed at a
glance and leaves the echo untouched; the animated field alone is subtle (the cells wobble along
the wind and the changing volume is drawn at reduced quality), which is why it is off by default and
meant as an addition to the flow, not a replacement.

### What the flow animation shows and does not show
The animation is a picture of the retrieved field and nothing else. These are guarantees of the
code (the CPU reference `src/lib/utils/windFlow.ts` is pinned by unit tests; the GPU code in
`flowRenderer.ts` does the same and was checked against it on the Mayfield case: positions and trails
agree to 1e-4 km, no particle was ever found without wind):

1. **Only where the analysis has a wind.** A cell has wind if u and v are finite (and w when the
   analysis retrieved w), at least two radars saw it, its beam-crossing angle is at least the minimum
   and its error is at most the limit ("Hide error >"). The arrows use the same mask. If the
   analysis did not retrieve w at all, the flow is horizontal only and the readout says so.
2. **No extrapolation.** The wind is sampled by trilinear interpolation; if any of the eight cells
   around a point has no wind, the sample is invalid. A particle that reaches an invalid sample
   stops, fades out and is reborn inside a cell all of whose corners have wind. Nothing is filled
   in, held, smoothed, or perturbed: there is no noise, turbulence or curl in the animation.
3. **Real motion in physical space.** Particles move in metres with u, v **and** w, by a midpoint
   (RK2) integrator with sub-steps of at most half a cell. Time is the frame time times ONE visible
   factor, the *visual speed* (default: a 30 m/s wind crosses the domain in about 10 s; shown as
   "×1250 (1 s = 21 min of wind motion)"). The vertical exaggeration is applied only when drawing.
4. **Frozen, not a forecast.** The analysis is one instant: the trails are streamlines of the
   field at the analysis time (shown above the scene), drawn as moving dashes. Particle positions
   mean nothing as time; they only show the direction and speed of the wind at that instant.
5. **Confidence is visible.** Trails get fainter where the beam-crossing angle is close to the minimum
   or the error is large, when those fields exist. A trail is a piece of the streamline through the
   particle (the field integrated backwards), and it is cut where the path leaves the wind.
6. **A read-out for the field that is drawn:** maximum speed, maximum |w|, share of cells with wind,
   mean divergence, and a warning badge if the QC limits (`max_speed`, `max_abs_w`) removed cells or
   were hit, if the field looks noisy (very strong winds: usually aliased velocities), or if w was
   not retrieved. Do not over-trust a field with a warning.
7. **Animate field is display only.** The echo is sampled where each cell was a moment ago, using
   the retrieved wind at that cell, in two phases half a cycle apart that are cross-faded (no seam);
   the amplitude ("Drift", km at the strong end) is a display choice, it is not a forecast of the
   storm's motion. Cells without wind do not move.

Not drawn: wind at heights or places without a retrieved wind, uncertainty-free "smooth" fields,
anything outside the analysis box. The particles are depth-tested against the arrows and each other
only; the ray-marched volume does not occlude them (they are drawn over it, translucent, so the echo
stays visible). With the camera below the ground plane the ground does not hide them.

Performance (Apple M1 Pro, headless Chrome, 4-radar mosaic volume under the flow): 60 fps with the
default 5k particles and with 40k; about 45 fps while orbiting with 40k (the volume is drawn on
every frame then); 42 fps with 60k at rest. Flow with Animate field on: 42 fps; Animate field alone
53 fps (the changing volume is drawn on every frame). While the flow plays and the camera is still,
the volume and ground are drawn once and re-used, which is why the flow is cheap. The quality
governor lowers the number of particles drawn when the GPU is slow; the particle buffers are freed
when the flow is switched off (after the fade), when the tab is hidden or when the source changes.
With "prefers-reduced-motion" the animation starts paused.

## Tests
`python/tests/test_multidoppler.py` builds **synthetic** radar volumes (two sites 70 km apart, radial
velocities forward-simulated from an imposed wind: uniform plus shear, a non-divergent vortex, a
convergent anelastic flow with a known `w`, and a fall-speed case) and checks that the recovered
`u`/`v`/`w` match the imposed wind in the well-observed region, plus the error cases and the
protocol. `python/tests/test_multidoppler_usability.py` adds: a wind field whose radial velocities were folded
(Nyquist 12 m/s against winds of 25-35 m/s) is recovered by *Dealias auto* and not by *never*; the
Nyquist estimate; the automatic lowest level for far-apart radars and the automatic boundary; the
output checks (counts, masks, thresholds, `keep_raw`); the per-level coverage table; the figures
(arrow fill/edge colours per theme, panel counts, `cmweather` colour map); and smoke tests on the two
real cases above that skip when the NEXRAD files are not present (path in `XRADAR_SEVERE_CASES`).
`src/lib/utils/windFlow.test.mjs` (`npm run test:unit`) tests the flow: uniform flow is a straight line at
the right speed, solid-body rotation is a circle of the right radius and period, a vertical wind
raises the particle at w, a particle never enters a cell without wind, a sample with any invalid
corner is invalid, trails are cut where the wind ends, seeding is only in valid cells, the mask,
the confidence, the fades and the read-out.

## Protocol
```
→ {type:"run_multidoppler", request_id, file_ids,
   grid:{dx_m, dz_m, z_min_m?, z_max_m, max_range_km, origin?:{lat,lon}, extent?:{x_min_km,x_max_km,y_min_km,y_max_km}},
   options:{method:"analytic"|"variational", min_crossing_angle, boundary:"auto"|"upward"|"downward"|"obrien",
            dealias:"auto"|"always"|"never", dealias_max_elevation?, max_speed?|null, max_abs_w?|null,
            max_uncertainty?|null, keep_raw?, fall_speed_correction,
            freezing_level:"auto"|"rain"|metres, melting_depth_m,
            background:{mode:"none"|"vvp"|"uniform"|"manual", u?, v?, profile?:[{height_m,u,v}]},
            storm_motion?:[u,v], uncertainty?, obs_error?, w_bottom?}}
← progress…, {type:"multidoppler_result", request_id, wind_id, fields:[{name,units,long_name}…],
   shape:[n_fields,nz,ny,nx], grid:{nx,ny,nz,x,y,dx,dy,z[],origin_lat,origin_lon,west,south,east,north},
   radars, coverage_pct, dual_coverage_pct, w_valid_pct, crossing_angle:{min,mean,median,max},
   field_stats, level_coverage:[{z_m,dual_pct,wind_pct,w_pct}…], qc, dealias:{mode,radars:[…]}, notes:[…],
   boundary, boundary_requested, background, warnings, timing, elapsed_ms, byte_length, chunks}
   + `chunks` binary frames: one float32 array of `shape`, C order
   (fields: u v w speed divergence vorticity uncertainty crossing_angle n_radars dbz)
← {type:"multidoppler_error", request_id, message}   | {type:"multidoppler_cancelled", request_id}
→ {type:"multidoppler_cancel", request_id}
→ {type:"export_multidoppler", wind_id, path, overwrite?}   ← {type:"file_saved", path}
```
