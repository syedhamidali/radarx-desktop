# Realistic ground and Realistic (stylised) storm

Two options of the 3D tab for gridded scenes (the merged mosaic volume), next to the flat map and
the scientific colour scale, which stay the defaults:

- **Realistic ground**: terrain with satellite imagery draped on it.
- **Storm: Realistic (stylised)**: reflectivity drawn like precipitation shafts, with sun light and
  soft shadows.

Both are choices of the **Ground** and **Storm** controls of the scene bar and are remembered
(`realGround`, `stormStyle`). The offline storm clip (`docs/STORM_CLIP.md`) uses the same renderer
(`--realistic`). The polar single-file 3D view is unchanged.

![Storm clip, realistic look: low orbit, 02:03 UTC](images/storm3d-realistic-low.png)
![Top-down: satellite imagery, the storm and its shadow](images/storm3d-realistic-topdown.png)

## Realistic ground
- **Elevation**: Terrain Tiles on AWS, Terrarium PNG (no key),
  `https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png`. A pixel stores
  `metres = R * 256 + G + B / 256 - 32768`. The tiles are resampled to a 1024 x 1024 height map
  (km above sea level, sea level is the floor) registered in the same azimuthal-equidistant frame
  as the radar volume and the imagery (`src/lib/utils/terrain.ts`).
- **Imagery**: Sentinel-2 cloudless 2021 by EOX IT Services GmbH (10 m), WMTS tiles
  `https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2021_3857/default/g/{z}/{y}/{x}.jpg`.
- **How it is drawn**: the terrain is a height field ray-marched in the same shader pass as the
  volume (not a triangle mesh). A ray stops at the first terrain hit, so the cloud is hidden below
  hills by construction, and heights are metres above sea level, the same datum as the volume's
  levels, stretched by the same **Vertical** control (the relief is exaggerated like the storm,
  and the screen says so). A height field has no skirts and no level of detail to manage: the
  imagery and the height map are sampled at the level the screen needs. Past the edge of the data
  the ground goes on, mirrored, and fades into haze, so the scene has no visible edge. Relief shading
  uses the slope of the height map; the cloud casts a soft shadow on the ground (a short march
  toward the light).
- Where relief is small (the Mayfield, KY case is Mississippi lowland: about 60 to 150 m in the
  middle, hills up to 560 m at the west edge of the 230 km scene) it is hardly visible even at x4;
  raise the **Vertical** control to see it. The other cases of the test collection (El Reno, Laura,
  Michael, Milton) are flat country or coast as well.

### Terms and attribution
- **EOX Sentinel-2 cloudless** is released under the Creative Commons Attribution-NonCommercial-
  ShareAlike 4.0 licence (EOX capabilities document, `ows:Abstract` of the layer): free for
  non-commercial use with attribution, commercial use needs a licence from EOX
  (<https://cloudless.eox.at>). Derived images and videos carry the same ShareAlike terms.
  Attribution shown on screen: *Imagery: Sentinel-2 cloudless (c) EOX IT Services GmbH (contains
  modified Copernicus Sentinel data 2021), CC BY-NC-SA 4.0*.
- **Terrain Tiles** (Mapzen / Tilezen Joerd; USGS 3DEP and NED, SRTM, GMTED2010 and others) are
  free to use with attribution (<https://github.com/tilezen/joerd/blob/master/docs/attribution.md>),
  shown on screen too.
- **Not used**: Google Maps / Google Earth tiles (their terms forbid it), and Esri World Imagery
  (usable for display with attribution, but its terms restrict caching and offline use, which this
  feature depends on). NASA GIBS Blue Marble and MODIS are too coarse (250 m to 500 m) for a
  120 km storm scene.

### Tile policy, cache, offline
Tiles are fetched once, nearest first, with at most 4 requests at once. In the app a tile is kept
in the browser's Cache API (it survives restarts and works offline); where that is not available
it is fetched as a plain image. The storm-clip renderer goes through a local proxy
(`tools/storm_clip/tilecache.py`) that keeps tiles in `~/.cache/xradar-desktop/tiles`
(`XRADAR_TILE_CACHE` or `--tile-cache` to change), asks one tile at a time with 0.12 s between
requests and a `User-Agent` that names the tool, never forwards a source or zoom that is not
listed, and reports the hosts that failed (`--offline` uses the cache only). This is for
interactive and research use; it is not a bulk downloader.

If a source cannot be reached the viewer says which one ("Could not load: satellite imagery
(tiles.maps.eox.at)") and falls back to what is available, ending at the flat map. Imagery is
never invented. The desktop app's content security policy lists the three tile hosts
(`src-tauri/tauri.conf.json`).

## Realistic (stylised) storm
The scientific colour scale maps dBZ to colours. The realistic look maps it to what the eye sees
in precipitation:

| reflectivity | look |
|---|---|
| light echo (below 25 dBZ) | thin, pale, translucent haze |
| moderate rain (25 to 40 dBZ) | grey-white |
| heavy cores (45 dBZ and above) | dense, dark grey |
| strongest cores (60 dBZ and above) | a subtle warm tint |

Extinction follows `Z^(1/1.4)` (opacity per **true** kilometre along the ray, so the vertical
stretch does not make the column look denser from above), and the volume fades out softly where it
leaves the shown box. Lighting: a sun direction, an ambient sky term that is brighter higher up,
soft self-shadowing by a short secondary march toward the sun (a few jittered steps, fewer while
the camera moves), a forward-scattering lobe (silver lining on back-lit edges) and a little
multiple scattering so dense cores stay lit. The quality governor keeps the frame rate; the
sampling noise changes every frame instead of sticking to the screen.

**Sun**: computed from the scan time and place (NOAA solar position, `sunPosition.ts`). If the sun is
at least 6 degrees above the horizon it is used. Otherwise the light is an artistic dusk light: a
low (9 degrees), warm sun at the azimuth where the sun set that day, with a dusk sky. The Mayfield
scan (11 Dec 2021, 01:50 to 02:08 UTC, about 19:50 local time) was 37 to 40 degrees below the
horizon, so it gets the dusk light, and the screen says: *Stylised lighting, not a photograph: dusk
light at the sunset azimuth (the real sun was 37 degrees below the horizon)*. The style is labelled
"stylised" in the menu and on screen. It is a rendering aid, not a simulation of light in
precipitation or an image of what an observer saw.
