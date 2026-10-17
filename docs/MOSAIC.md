# Radar mosaic (multi-radar merge)

Merge one or more open radar volumes onto a shared grid and draw the result on the map. The
heavy lifting is done by [radarx](https://github.com/syedhamidali/radarx) (`grid_radars`,
`merge_radars`, `network_bias`), whose compiled kernels run multithreaded. Needs
`pip install "radarx>=0.5.1"` (Python 3.12+).

## Use it
1. **Open the radars.** Either open files normally (they appear in the Mosaic panel), or use
   **Cloud Data → Network**: pick a centre station, radius, radar count and time and it opens
   each neighbour's nearest volume, then starts the mosaic.
2. **Mosaic panel** (right sidebar): tick the radars, choose variable and product, press **Run**.
   - **Products:** CAPPI (height above sea level), Composite (column max), Echo top.
   - **Merge:** distance/beam/time-weighted blend (off = nearest radar wins), optional bias
     calibration against a reference radar, optional advection to a common time.
   - **Extent:** all radars, or the current map view. Resolution 250 m - 5 km.
3. The result is drawn on the map. Hover for the merged value and each radar's contribution
   (value, range, beam elevation). Use opacity, **Mosaic view**, **Fit to mosaic**.
4. **Export** NetCDF (lon/lat grid + per-radar variables, CF attributes) or PNG (+ `.pgw`/`.prj`).

Typical cost for two 16-sweep volumes: ~0.4 s at 1 km, ~1.3-2.2 s at 500 m.

## Notes
- Mixed volume times (> 10 min apart) and duplicate sites are flagged in the file list.
- NEXRAD data uses -33 dBZ as a "no echo" fill, which breaks dB-space averaging, so reflectivity
  is averaged linearly. Cells below the colour-scale minimum are transparent.
- Calibration needs >= 200 matched cells between 15 and 50 dBZ in the overlap; otherwise the
  bias is reported as unavailable.
- Advection needs a storm motion `[u, v]` (m/s); without one it is skipped with a warning.

## Protocol
```
→ {type:"create_mosaic", request_id, file_ids, variable, product, height_m?, threshold_dbz?,
   grid:{resolution_m, extent:"auto"|{west,south,east,north}, z_max_m, z_step_m, max_range_km},
   merge:{enabled, range_scale_km, time_scale_s, beamwidth_deg, calibrate, reference_index, advect}}
← progress…, {type:"mosaic_result", request_id, mosaic_id, corners, vmin, vmax, legend, stats,
   radars:[{name, coverage_fraction, share, bias_db…}], …, png_bytes, chunks} + PNG binary frames
← {type:"mosaic_error", request_id, message}
→ {type:"mosaic_probe", mosaic_id, lon, lat}   ← {type:"mosaic_probe_result", value, radars:[…]}
→ {type:"export_mosaic", mosaic_id, format:"netcdf"|"png", path, overwrite?} ← {type:"file_saved"}
```
