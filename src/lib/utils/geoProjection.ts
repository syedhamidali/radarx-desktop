/**
 * Geographic projection utilities for radar PPI display.
 *
 * Uses an azimuthal equidistant projection centered on the radar site,
 * which is appropriate for radar ranges up to ~300 km.
 */

const EARTH_RADIUS_M = 6371000;
const DEG2RAD = Math.PI / 180;
const RAD2DEG = 180 / Math.PI;

/**
 * Convert radar polar coordinates (range, azimuth) to geographic (lat, lon).
 *
 * @param range_m   - Slant range from radar in meters
 * @param azimuth_deg - Meteorological azimuth in degrees (0 = North, CW)
 * @param radarLat  - Radar site latitude
 * @param radarLon  - Radar site longitude
 * @returns [lat, lon] in degrees
 */
export function radarToLatLon(
  range_m: number,
  azimuth_deg: number,
  radarLat: number,
  radarLon: number,
): [number, number] {
  const angularDist = range_m / EARTH_RADIUS_M; // radians on sphere
  const bearing = azimuth_deg * DEG2RAD;
  const lat1 = radarLat * DEG2RAD;
  const lon1 = radarLon * DEG2RAD;

  const sinLat1 = Math.sin(lat1);
  const cosLat1 = Math.cos(lat1);
  const sinD = Math.sin(angularDist);
  const cosD = Math.cos(angularDist);

  const lat2 = Math.asin(sinLat1 * cosD + cosLat1 * sinD * Math.cos(bearing));
  const lon2 =
    lon1 +
    Math.atan2(
      Math.sin(bearing) * sinD * cosLat1,
      cosD - sinLat1 * Math.sin(lat2),
    );

  return [lat2 * RAD2DEG, lon2 * RAD2DEG];
}

/**
 * Convert geographic (lat, lon) to screen pixel coordinates.
 *
 * Projects through the radar's data-space coordinate system (meters from
 * radar center) and then applies the same NDC transform the WebGL PPI uses.
 *
 * @returns [px, py] in CSS pixels relative to the canvas container
 */
export function latLonToPixel(
  lat: number,
  lon: number,
  radarLat: number,
  radarLon: number,
  maxRange: number,
  scale: number,
  translateX: number,
  translateY: number,
  width: number,
  height: number,
): [number, number] {
  // Geographic to data-space (meters from radar)
  const [dataX, dataY] = latLonToDataSpace(lat, lon, radarLat, radarLon);

  // Data-space to NDC (same transform as ppiRenderer)
  const aspect = width / height;
  const sx = (aspect >= 1 ? scale / aspect : scale) / maxRange;
  const sy = (aspect >= 1 ? scale : scale * aspect) / maxRange;
  const ndcX = (dataX + translateX) * sx;
  const ndcY = (dataY + translateY) * sy;

  // NDC [-1,1] to pixel
  const px = ((ndcX + 1) / 2) * width;
  const py = ((1 - ndcY) / 2) * height;
  return [px, py];
}

/**
 * Convert screen pixel to geographic (lat, lon).
 */
export function pixelToLatLon(
  px: number,
  py: number,
  radarLat: number,
  radarLon: number,
  maxRange: number,
  scale: number,
  translateX: number,
  translateY: number,
  width: number,
  height: number,
): [number, number] {
  // Pixel to NDC
  const ndcX = (px / width) * 2 - 1;
  const ndcY = 1 - (py / height) * 2;

  // NDC to data-space
  const aspect = width / height;
  const sx = (aspect >= 1 ? scale / aspect : scale) / maxRange;
  const sy = (aspect >= 1 ? scale : scale * aspect) / maxRange;
  const dataX = ndcX / sx - translateX;
  const dataY = ndcY / sy - translateY;

  // Data-space (meters) to lat/lon
  return dataSpaceToLatLon(dataX, dataY, radarLat, radarLon);
}

/**
 * Geographic to radar data-space (x = east meters, y = north meters): the spherical azimuthal
 * equidistant projection centred on the radar. This is the projection the PPI itself is drawn
 * in (x = r sin az, y = r cos az), so overlays, tiles and measurements registered through it
 * line up with the echoes at any range. (A flat dLat/dLon approximation drifts by ~1 km at
 * 100 km and ~9 km at 300 km off-axis.)
 */
export function latLonToDataSpace(
  lat: number,
  lon: number,
  radarLat: number,
  radarLon: number,
): [number, number] {
  const phi = lat * DEG2RAD;
  const phi0 = radarLat * DEG2RAD;
  const dLon = (lon - radarLon) * DEG2RAD;
  const sinPhi = Math.sin(phi);
  const cosPhi = Math.cos(phi);
  const sinPhi0 = Math.sin(phi0);
  const cosPhi0 = Math.cos(phi0);
  const cosC = Math.min(1, Math.max(-1, sinPhi0 * sinPhi + cosPhi0 * cosPhi * Math.cos(dLon)));
  const c = Math.acos(cosC);
  const k = c < 1e-9 ? 1 : c / Math.sin(c);
  return [
    EARTH_RADIUS_M * k * cosPhi * Math.sin(dLon),
    EARTH_RADIUS_M * k * (cosPhi0 * sinPhi - sinPhi0 * cosPhi * Math.cos(dLon)),
  ];
}

/**
 * Radar data-space (meters) back to geographic (inverse of latLonToDataSpace).
 */
export function dataSpaceToLatLon(
  dataX: number,
  dataY: number,
  radarLat: number,
  radarLon: number,
): [number, number] {
  const rho = Math.hypot(dataX, dataY);
  if (rho < 1e-6) return [radarLat, radarLon];
  const phi0 = radarLat * DEG2RAD;
  const c = rho / EARTH_RADIUS_M;
  const sinC = Math.sin(c);
  const cosC = Math.cos(c);
  const sinPhi0 = Math.sin(phi0);
  const cosPhi0 = Math.cos(phi0);
  const lat = Math.asin(Math.min(1, Math.max(-1, cosC * sinPhi0 + (dataY * sinC * cosPhi0) / rho)));
  const lon =
    radarLon * DEG2RAD +
    Math.atan2(dataX * sinC, rho * cosPhi0 * cosC - dataY * sinPhi0 * sinC);
  return [lat * RAD2DEG, ((lon * RAD2DEG + 540) % 360) - 180];
}

/**
 * Web Mercator tile math utilities for slippy-map tile rendering.
 */

/** Convert lat/lon to fractional tile coordinates at a given zoom level. */
export function latLonToTile(
  lat: number,
  lon: number,
  zoom: number,
): { tx: number; ty: number } {
  const n = Math.pow(2, zoom);
  const tx = ((lon + 180) / 360) * n;
  const latRad = lat * DEG2RAD;
  const ty = ((1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2) * n;
  return { tx, ty };
}

/** Convert fractional tile coordinates back to lat/lon. */
export function tileToLatLon(
  tx: number,
  ty: number,
  zoom: number,
): [number, number] {
  const n = Math.pow(2, zoom);
  const lon = (tx / n) * 360 - 180;
  const latRad = Math.atan(Math.sinh(Math.PI * (1 - (2 * ty) / n)));
  return [latRad * RAD2DEG, lon];
}

/**
 * Compute the appropriate tile zoom level for a given radar view.
 * Aims for a reasonable number of tiles covering the visible area.
 */
export function computeTileZoom(
  maxRange: number,
  scale: number,
  containerWidth: number,
): number {
  // Visible extent in meters (diameter at current zoom)
  const visibleExtent = (maxRange * 2) / scale;
  // Meters per pixel
  const metersPerPixel = visibleExtent / containerWidth;
  // At zoom z, one tile is ~40075016 / 2^z meters wide, covering 256 pixels
  // So meters per pixel at zoom z = 40075016 / (256 * 2^z)
  // Solve for z: 2^z = 40075016 / (256 * metersPerPixel)
  const z = Math.log2(40075016 / (256 * metersPerPixel));
  return Math.max(1, Math.min(18, Math.round(z)));
}
