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
The scientific colour scale maps dBZ to colours. The realistic look maps it to what the eye sees in
a daylight storm photograph:

| reflectivity | look |
|---|---|
| light echo, high tops (below about 25 dBZ) | thin white haze and cloud |
| moderate rain (25 to 40 dBZ) | dense, bright white cloud body |
| heavy cores (45 dBZ and above) | darker blue-grey, translucent grey rain curtains below the base |
| strongest cores (about 60 dBZ and above) | a faint green-grey tint only (a hail hint) |

There is no purple, violet or pink in the storm (a unit test checks every dBZ of every palette).

How it is lit (`realisticShader.ts`): a sun with a soft self-shadow (a short secondary march) and a
multiple-scattering floor, so shadows stay blue-grey instead of black; Henyey-Greenstein forward
(silver lining toward the sun) and back lobes; powder darkening in dense cores (thin edges stay
bright); a blue fill from the sky above and a green-grey bounce from the ground below; the base is
darker than the top; sun-lit edges are crisper (shading by the slope of the echo toward the light);
haze and aerial perspective bluish with distance; a saturated blue sky with a lighter horizon and a
sun glow. Cloud shadows fall on the terrain.

**Light.** The real sun is used when it is at least 6 degrees up. Otherwise there are two labelled
options (`light` in the clip settings, `--light daylight|dusk`): **Daylight (assumed)**, the default
for the realistic clip, a daytime sun at the azimuth the real sun has at 14:30 local solar time on
that date and place, raised to 36 to 50 degrees of elevation, labelled on screen *Stylised daylight;
the real sun was 39 degrees below the horizon at this time (not a photograph)*; and **Dusk**, a low
warm sun at the sunset azimuth with a dusk sky. The Mayfield scan (11 Dec 2021, 01:50 to 02:08 UTC,
about 19:50 local time) was 37 to 40 degrees below the horizon: the honest options are those two.
`--sun-azimuth` / `--sun-elevation` choose a direction for the shot.

**Stylised detail.** Four octaves of deterministic fractal noise (riding with the storm) brighten and
darken the shading and thin the FAINT parts of the echo into wisps and billows. Opacity is only
ever reduced, and only where it is low: it never adds echo where the radar has none and never moves
or weakens the cores.

**What radar does not see.** Radar sees precipitation-size particles (rain, graupel, hail, snow), not
cloud droplets. The cloud above and around the echo (the anvil, the cloud base, clear-air cloud) is
NOT observed. The realistic look draws the radar echo as if it were cloud, so its extent is the
echo's, and the screen says so ("radar sees rain and ice, not cloud droplets: cloud look and fine
detail are stylised, echo position and strength are radar data"). An observed alternative for a
later version is GOES ABI cloud imagery or the cloud water of a model such as HRRR.

Three variants were rendered on the same frames of the Mayfield storm and of Hurricane Laura
(`images/storm-palettes.png`): **white** (chosen: brightest, most cloud-like), **soft** (a little
less white) and **stormy** (grey-white body, darker cores: heavier but flatter). `palette` in the
clip settings selects one. An earlier grey palette was judged too grey and replaced.

![Three variants on the same frames: Mayfield storm and Hurricane Laura](images/storm-palettes.png)

What still looks fake (honest list): the cloud is the radar's 500 m smooth field, stretched x4
vertically, so it reads as soft vertical rain shafts and a hazy body, not as a cumulonimbus with a
hard anvil, cauliflower tops and a defined base; the ground imagery is a summer-biased composite at
10 m resolution that blurs at ground level; the cloud is only where the echo is.
