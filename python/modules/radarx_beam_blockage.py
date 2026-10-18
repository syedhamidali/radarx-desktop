"""radarx: partial / cumulative beam blockage by terrain from a user-supplied DEM."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import xarray as xr

from engine.plugins import ModuleError, Result, module, param


def _load_extra():
    """Import the sibling ``_radarx_extra.py`` (modules are loaded by path)."""
    name = "xradar_module_radarx_extra"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).with_name("_radarx_extra.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


X = _load_extra()
A = X.A
MAX_SECTORS = 36
DEM_VARIABLES = ("elevation", "dem", "z", "height", "topo", "topography", "altitude", "Band1")

NO_DEM = """\
Beam blockage needs a terrain model (a digital elevation model, DEM) and none was given. \
xradar-desktop never downloads terrain data. Get SRTM or Copernicus GLO-30 tiles that cover \
the radar (about 250 km around it) yourself and save them as a NetCDF file (variable of \
terrain height in metres on latitude/longitude), an ESRI ASCII grid (.asc, degrees) or a \
GeoTIFF (needs the rioxarray or rasterio package), then enter the path in 'DEM file'. \
For example: gdal_translate -of NetCDF dem.tif dem.nc"""

NOTES = """\
Partial beam blockage after Bech et al. (2003) and Lang et al. (2009), \
radarx.retrieve.beam_blockage. The beam centre follows the effective Earth radius model \
(k_e = 4/3 by default); at every gate the terrain height under the beam is interpolated from \
the DEM and the part of the beam cross section (a Gaussian power pattern of the given \
half-power width by default, or the uniform circle of Bech et al.) below the terrain is the \
partial beam blockage PBB (0 = free, 1 = fully blocked). The cumulative beam blockage CBB is \
the maximum of PBB along the ray, i.e. everything behind a blocking ridge is shadowed \
(diffraction is neglected). TERRAIN_CLUTTER flags gates whose beam centre is at or below the \
terrain surface (plus the margin).

Results depend on the DEM resolution (a DEM much coarser than the gate spacing smooths ridges), \
on the beam width (the WSR-88D has 0.95 deg; mobile and C/X-band radars often 1 deg or more) \
and on k_e. The terrain is sampled on the beam axis only; a narrow ridge beside the beam is not \
seen. The result is not applied to any field: use it as a mask, e.g. CBB < 0.5, or as a quality \
field. Gates beyond the DEM are NaN. DEM cells without a value are filled with 0 m (sea) when \
that option is on."""


def load_dem(path: str, variable: str = "", crs: str = "") -> xr.DataArray:
    """Terrain heights (m) from a local file as a 2-D DataArray radarx understands.

    NetCDF/HDF5 (``.nc``), ESRI ASCII grid (``.asc``, degrees unless ``crs`` is given) and, when
    ``rioxarray`` or ``rasterio`` is installed, GeoTIFF. Never touches the network.
    """
    p = Path(path).expanduser()
    if not p.is_file():
        raise ModuleError(
            f"DEM file not found: {p}. Enter the full path of a NetCDF, .asc or GeoTIFF file."
        )
    ext = p.suffix.lower()
    try:
        if ext in (".nc", ".nc4", ".cdf", ".h5", ".hdf5"):
            return _from_netcdf(p, variable)
        if ext == ".asc":
            return _from_ascii(p, crs)
        if ext in (".tif", ".tiff"):
            return _from_geotiff(p)
    except ModuleError:
        raise
    except Exception as exc:
        raise ModuleError(f"Could not read the DEM {p.name}: {type(exc).__name__}: {exc}") from exc
    raise ModuleError(
        f"Unsupported DEM format '{ext}'. Use a NetCDF (.nc), an ESRI ASCII grid (.asc) or a "
        "GeoTIFF (.tif, needs rioxarray or rasterio)."
    )


def _from_netcdf(p: Path, variable: str) -> xr.DataArray:
    ds = xr.open_dataset(p)
    cands = [v for v in ds.data_vars if ds[v].squeeze().ndim == 2]
    if variable:
        if variable not in ds.data_vars:
            raise ModuleError(
                f"DEM variable '{variable}' not in {p.name} (variables: {sorted(ds.data_vars)})"
            )
        da = ds[variable]
    else:
        pick = next((v for v in DEM_VARIABLES if v in cands), None)
        if pick is None and len(cands) == 1:
            pick = cands[0]
        if pick is None:
            raise ModuleError(
                f"Cannot tell which variable of {p.name} is the terrain height "
                f"(2-D variables: {cands}). Enter its name in 'DEM variable'."
            )
        da = ds[pick]
    da = da.squeeze(drop=True)
    rename = {}
    for d in da.dims:
        low = str(d).lower()
        if low in ("lat", "latitude", "y", "northing") and d not in ("latitude", "y", "northing"):
            rename[d] = "latitude" if low.startswith("lat") else "y"
        if low in ("lon", "longitude", "x", "easting") and d not in ("longitude", "x", "easting"):
            rename[d] = "longitude" if low.startswith("lon") else "x"
    return da.rename(rename).load()


def _from_ascii(p: Path, crs: str) -> xr.DataArray:
    header: dict[str, float] = {}
    with p.open() as fh:
        for _ in range(6):
            pos = fh.tell()
            parts = fh.readline().split()
            if len(parts) != 2 or not parts[0].replace("_", "").isalpha():
                fh.seek(pos)
                break
            header[parts[0].lower()] = float(parts[1])
        body = np.loadtxt(fh, dtype=np.float64)
    try:
        n_cols, n_rows = int(header["ncols"]), int(header["nrows"])
        cell = float(header["cellsize"])
    except KeyError as exc:
        raise ModuleError(f"{p.name} is not an ESRI ASCII grid (missing {exc})") from exc
    if body.shape != (n_rows, n_cols):
        raise ModuleError(f"{p.name}: grid shape {body.shape} does not match its header")
    x0 = header.get("xllcorner", header.get("xllcenter", 0.0) - cell / 2) + cell / 2
    y0 = header.get("yllcorner", header.get("yllcenter", 0.0) - cell / 2) + cell / 2
    nodata = header.get("nodata_value", -9999.0)
    body = np.where(body == nodata, np.nan, body)
    xs = x0 + cell * np.arange(n_cols)
    ys = y0 + cell * np.arange(n_rows)[::-1]  # the first row is the northernmost
    if crs:
        da = xr.DataArray(body, dims=("y", "x"), coords={"y": ys, "x": xs})
        da.attrs["crs"] = crs
        return da
    if not (abs(ys).max() <= 90.0 and abs(xs).max() <= 360.0 and cell < 1.0):
        raise ModuleError(
            f"{p.name} does not look like a grid in degrees. If it is in projected metres, "
            "enter its coordinate system in 'DEM CRS' (for example EPSG:32614)."
        )
    return xr.DataArray(
        body, dims=("latitude", "longitude"), coords={"latitude": ys, "longitude": xs}
    )


def _from_geotiff(p: Path) -> xr.DataArray:
    if importlib.util.find_spec("rioxarray") is not None:
        import rioxarray

        da = rioxarray.open_rasterio(p, masked=True).squeeze("band", drop=True)
        return da.load()
    if importlib.util.find_spec("rasterio") is not None:
        import rasterio

        with rasterio.open(p) as src:
            data = src.read(1, masked=True).astype(np.float64).filled(np.nan)
            t = src.transform
            if src.crs is not None and not src.crs.is_geographic:
                crs = src.crs.to_string()
                xs = t.c + t.a * (np.arange(src.width) + 0.5)
                ys = t.f + t.e * (np.arange(src.height) + 0.5)
                da = xr.DataArray(data, dims=("y", "x"), coords={"y": ys, "x": xs})
                da.attrs["crs"] = crs
                return da
            xs = t.c + t.a * (np.arange(src.width) + 0.5)
            ys = t.f + t.e * (np.arange(src.height) + 0.5)
            return xr.DataArray(
                data, dims=("latitude", "longitude"), coords={"latitude": ys, "longitude": xs}
            )
    raise ModuleError(
        "Reading a GeoTIFF needs the rioxarray or rasterio package, which is not installed. "
        "Convert the file to NetCDF (gdal_translate -of NetCDF dem.tif dem.nc) or to an ESRI "
        "ASCII grid (gdal_translate -of AAIGrid dem.tif dem.asc), or pip install rioxarray."
    )


@module(
    id="radarx_beam_blockage",
    title="Beam Blockage (radarx)",
    description=(
        "radarx.retrieve.beam_blockage: partial and cumulative beam blockage and terrain "
        "clutter of every gate from a terrain model you supply (Bech et al. 2003; Lang et al. "
        "2009). Needs a local DEM file (NetCDF, .asc or GeoTIFF); nothing is downloaded."
    ),
    category="QC",
    requires=[],
    optional=["DBZH"],
    needs=A.NEEDS,
    params=[
        param(
            "dem_path",
            "string",
            "",
            "DEM file",
            help="Local terrain-height file (m ASL): NetCDF (.nc), ESRI ASCII (.asc) or "
            "GeoTIFF (.tif, needs rioxarray/rasterio). Must cover ~250 km around the radar",
        ),
        param(
            "dem_variable",
            "string",
            "",
            "DEM variable (NetCDF)",
            help="Name of the terrain variable; empty = detect (elevation, dem, z, ...)",
        ),
        param(
            "dem_crs",
            "string",
            "",
            "DEM CRS (projected DEMs)",
            help="Only for DEMs in metres, e.g. EPSG:32614; empty = latitude/longitude",
        ),
        param(
            "output",
            "select",
            "CBB",
            "Show",
            options=["CBB", "PBB", "TERRAIN_HEIGHT", "TERRAIN_CLUTTER"],
            help="CBB = cumulative blockage (shadowing), PBB = blockage at the gate itself",
        ),
        param(
            "beamwidth_deg",
            "number",
            1.0,
            "Beam width (°, half-power)",
            min=0.1,
            max=5.0,
            step=0.05,
            help="WSR-88D: 0.95; most C/X-band radars: 1-1.5",
        ),
        param(
            "pattern",
            "select",
            "gaussian",
            "Beam pattern",
            options=["gaussian", "uniform"],
            help="Gaussian power pattern (default) or the uniform circle of Bech et al.",
        ),
        param(
            "two_way",
            "bool",
            False,
            "Two-way pattern (Gaussian only)",
            help="Narrower beam (sqrt 2) for the received power of a target; Gaussian only",
        ),
        param(
            "threshold",
            "number",
            0.5,
            "Blocked if CBB ≥",
            min=0.05,
            max=1.0,
            step=0.05,
            help="Used for the gate counts and the blocked-sector table (0.5 = half the beam)",
        ),
        param(
            "clutter_margin_m",
            "number",
            0.0,
            "Terrain clutter margin (m)",
            min=0,
            max=1000,
            step=25,
            help="Flag gates whose beam centre is at most this high above the terrain",
        ),
        param(
            "k_e",
            "number",
            4.0 / 3.0,
            "Effective Earth radius factor",
            min=0.8,
            max=3.0,
            step=0.01,
            help="4/3 standard atmosphere; smaller = sub-, larger = super-refraction",
        ),
        param(
            "fill_sea",
            "bool",
            True,
            "Fill DEM gaps with 0 m (sea)",
            help="Cells without a terrain value (sea, voids) become 0 m instead of NaN",
        ),
    ],
    version="1.0",
)
def run(
    ctx,
    dem_path,
    dem_variable,
    dem_crs,
    output,
    beamwidth_deg,
    pattern,
    two_way,
    threshold,
    clutter_margin_m,
    k_e,
    fill_sea,
):
    rt = A.rx()
    blockage = X.need(rt, "beam_blockage", "Beam blockage")
    if not dem_path.strip():
        raise ModuleError(NO_DEM)
    if two_way and pattern == "uniform":
        raise ModuleError("The two-way option applies to the Gaussian pattern only")

    ds = X.selected_sweep(ctx)
    site = X.site_scalars(ctx.datatree)
    if len(site) < 3:
        raise ModuleError(
            "The radar site (latitude, longitude, altitude) is not in this file, so the beam "
            "cannot be placed on the terrain."
        )
    ctx.progress(10, "Reading the DEM")
    dem = load_dem(dem_path.strip(), dem_variable.strip(), dem_crs.strip())
    if fill_sea:
        dem = dem.fillna(0.0)

    sweep = ds.drop_vars([v for v in ds.data_vars if ds[v].ndim >= 2])  # geometry only
    ctx.progress(35, "Tracing the beam over the terrain")
    with A.captured_warnings() as warns, A.guard("Beam blockage"):
        out = blockage(
            sweep,
            dem,
            beamwidth=float(beamwidth_deg),
            k_e=float(k_e),
            pattern=pattern,
            two_way=bool(two_way),
            clutter_margin=float(clutter_margin_m),
            crs=dem_crs.strip() or None,
            site=(site["latitude"], site["longitude"], site["altitude"]),
        )
    pbb = A.polar_array(out["PBB"], ds, "PBB")
    cbb = A.polar_array(out["CBB"], ds, "CBB")
    terrain = A.polar_array(out["TERRAIN_HEIGHT"], ds, "TERRAIN_HEIGHT")
    clutter = A.polar_array(out["TERRAIN_CLUTTER"].astype(np.float32), ds, "TERRAIN_CLUTTER")
    covered = np.isfinite(cbb)
    if not covered.any():
        raise ModuleError(
            "The DEM does not cover any gate of this sweep. Check that the DEM contains the "
            f"radar site ({site['latitude']:.2f}, {site['longitude']:.2f}) and its surroundings."
        )

    res = Result()
    if output == "CBB":
        res.field(
            "Cumulative beam blockage (radarx)", cbb, units="", vmin=0.0, vmax=1.0, cmap="magma"
        )
    elif output == "PBB":
        res.field("Partial beam blockage (radarx)", pbb, units="", vmin=0.0, vmax=1.0, cmap="magma")
    elif output == "TERRAIN_HEIGHT":
        res.field("Terrain height under the beam (radarx)", terrain, units="m", cmap="viridis")
    else:
        res.field(
            "Terrain clutter (radarx)",
            np.where(covered, clutter, np.nan).astype(np.float32),
            categories=[
                {"value": 0, "label": "Beam above terrain", "color": "#94a3b8"},
                {"value": 1, "label": "Beam in terrain", "color": "#dc2626"},
            ],
            units="flag",
        )

    n_cov = int(covered.sum())
    blocked = covered & (cbb >= threshold)
    azimuth = np.asarray(ds["azimuth"].values, dtype=np.float64)
    rng = np.asarray(ds["range"].values, dtype=np.float64)
    rows = _blocked_sectors(azimuth, rng, cbb, pbb, threshold)
    res.scalars(
        [
            {"label": "Gates with terrain data", "value": n_cov},
            {"label": "DEM coverage of the sweep", "value": 100.0 * n_cov / cbb.size, "unit": "%"},
            {
                "label": f"Gates with CBB ≥ {threshold:g}",
                "value": 100.0 * int(blocked.sum()) / n_cov,
                "unit": "% of covered",
            },
            {
                "label": "Gates with PBB ≥ 0.1",
                "value": 100.0 * int((np.nan_to_num(pbb) >= 0.1).sum()) / n_cov,
                "unit": "% of covered",
            },
            {"label": "Max PBB", "value": float(np.nanmax(pbb))},
            {"label": "Terrain-clutter gates", "value": int((clutter > 0.5).sum())},
            {
                "label": "Highest terrain under the beam",
                "value": float(np.nanmax(terrain)),
                "unit": "m ASL",
            },
            {"label": "Blocked sectors", "value": len(rows)},
            {"label": "Beam width", "value": float(beamwidth_deg), "unit": "°"},
            {"label": "Pattern", "value": pattern + (" (two-way)" if two_way else "")},
            {"label": "DEM", "value": Path(dem_path).name},
            {"label": "radarx", "value": X.version()},
        ]
    )
    res.table(
        f"Sectors with blockage (CBB ≥ {threshold:g}, {A.sweep_elevation(ds):.1f}° sweep)",
        [
            {"key": "sector", "label": "Azimuth"},
            {"key": "first_km", "label": "Blocked from", "unit": "km", "digits": 1},
            {"key": "max_cbb", "label": "Max CBB", "digits": 2},
            {"key": "max_pbb", "label": "Max PBB", "digits": 2},
            {"key": "gates_pct", "label": "Gates blocked", "unit": "%", "digits": 0},
        ],
        rows,
    )
    res.text(NOTES, title="About this method")
    return A.add_warnings(res, warns)


def _blocked_sectors(azimuth, rng, cbb, pbb, threshold):
    """Per 10-degree sector: where blocking starts, strongest values, share of blocked gates."""
    width = 10.0
    rows = []
    for k in range(int(360 // width)):
        lo, hi = k * width, (k + 1) * width
        sel = (azimuth >= lo) & (azimuth < hi)
        if not sel.any():
            continue
        c = cbb[sel]
        valid = np.isfinite(c)
        if not valid.any():
            continue
        hit = valid & (np.nan_to_num(c) >= threshold)
        if not hit.any():
            continue
        first = rng[np.flatnonzero(hit.any(axis=0))[0]] / 1000.0
        rows.append(
            {
                "sector": f"{lo:03.0f}-{hi:03.0f}°",
                "first_km": round(float(first), 1),
                "max_cbb": round(float(np.nanmax(c)), 2),
                "max_pbb": round(float(np.nanmax(pbb[sel])), 2),
                "gates_pct": round(100.0 * float(hit.sum()) / float(valid.sum()), 0),
            }
        )
    rows.sort(key=lambda r: r["first_km"])
    return rows[:MAX_SECTORS]
