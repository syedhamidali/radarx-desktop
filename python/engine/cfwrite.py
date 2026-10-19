"""CF / CfRadial helpers shared by every NetCDF and Zarr writer of the engine.

What this module fixes compared with writing a radar tree straight to disk with xarray/xradar:

* the CfRadial writers of xradar leave ``"None"`` strings in ``title``/``institution``/... and
  ``Conventions`` (CfRadial2: the attributes never reach the file at all); here every file gets a
  complete, honest set of global attributes (:func:`engine.provenance.standard_global_attrs`);
* units are normalised to UDUNITS (``unitless`` -> ``1``, ``meters per seconds`` -> ``m s-1``);
* ``standard_name`` is written only when it is in the CF table (``equivalent_reflectivity_factor``,
  ``radial_velocity_of_scatterers_away_from_instrument``); CfRadial-only names such as
  ``radar_equivalent_reflectivity_factor_h`` and the non-CF ``axis`` values are dropped (they make a
  CF checker fail) - ``long_name`` carries the polarisation;
* every variable gets ``long_name``; dangling ``grid_mapping`` / ``coordinates`` references of the
  input are removed; flag variables get integer ``flag_values`` of the variable's own type;
* moments are written as ``float32`` with ``_FillValue = NaN`` (the gates below the detection
  threshold are *masked*, not written as -33 dBZ), zlib-compressed and chunked;
* ``time`` is ``float64`` ``seconds since <volume start, ISO 8601 UTC>`` (``calendar = standard``);
* strings are fixed-width character arrays (NetCDF-3 style, readable by every CF tool), not
  variable-length strings.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

from engine import hdf5_guard, layer_provenance, provenance
from engine.varmap import role_for_name, unit_class

logger = logging.getLogger(__name__)

CF_CONVENTIONS = "CF-1.8"
CFRADIAL1_CONVENTIONS = "CF-1.8"
CFRADIAL1_VERSION = "CF-Radial-1.4"
CFRADIAL1_SUB_CONVENTIONS = "CF-Radial instrument_parameters radar_parameters radar_calibration"
CFRADIAL2_CONVENTIONS = "CF-1.8, WMO CF-1.0, ACDD-1.3"
CFRADIAL2_VERSION = "2.0"
REFERENCES = (
    "CF Conventions 1.8 https://cfconventions.org/; "
    "CfRadial 1.4 https://ncar.github.io/CfRadial/; "
    "xradar https://docs.openradarscience.org/projects/xradar"
)
#: width of the character arrays that replace variable-length strings
STRING_WIDTH = 32
STRING_DIM = "string_length"

#: CF standard names (all present in the CF standard-name table) the engine assigns to radar roles.
ROLE_STANDARD_NAME: dict[str, str] = {
    "reflectivity": "equivalent_reflectivity_factor",
    "velocity": "radial_velocity_of_scatterers_away_from_instrument",
}
#: standard names that may be kept when a file already carries them (all in the CF table)
KEEP_STANDARD_NAMES = frozenset(
    set(ROLE_STANDARD_NAME.values())
    | {
        "time",
        "latitude",
        "longitude",
        "altitude",
        "projection_x_coordinate",
        "projection_y_coordinate",
        "height_above_mean_sea_level",
        "eastward_wind",
        "northward_wind",
        "upward_air_velocity",
        "wind_speed",
        "divergence_of_wind",
        "atmosphere_upward_relative_vorticity",
    }
)
_ROLE_UNITS = {
    "reflectivity": "dBZ",
    "zdr": "dB",
    "phidp": "degrees",
    "kdp": "degrees km-1",
    "rhohv": "1",
    "velocity": "m s-1",
    "width": "m s-1",
    "snr": "dB",
    "ncp": "1",
}
_NAME_UNITS = {
    "fixed_angle": "degrees",
    "sweep_fixed_angle": "degrees",
    "azimuth": "degrees",
    "elevation": "degrees",
    "range": "m",
}
#: ``axis`` values CF allows
_CF_AXIS = frozenset({"X", "Y", "Z", "T"})
_CLASS_UNITS = {
    "dbz": "dBZ",
    "db": "dB",
    "deg/km": "degrees km-1",
    "speed": "m s-1",
    "none": "1",
}
_UNIT_WORDS = {
    "meters": "m",
    "meter": "m",
    "metres": "m",
    "metre": "m",
    "m": "m",
    "seconds": "s",
    "second": "s",
    "sec": "s",
    "s": "s",
    "hz": "Hz",
    "hertz": "Hz",
    "kilometers": "km",
    "kilometer": "km",
    "km": "km",
    "dbm": "dBm",
    "dbw": "dBW",
    "percent": "%",
    "%": "%",
    "degreesperkm": "degrees km-1",
    "degreesperkilometer": "degrees km-1",
    "degreespersecond": "degrees s-1",
    "degreepersecond": "degrees s-1",
    "degpersec": "degrees s-1",
    "deg/s": "degrees s-1",
    "degrees/s": "degrees s-1",
    "m/s2": "m s-2",
}

#: physical bounds (valid_min, valid_max) that are written when the data respect them
_VALID_RANGE = {
    "rhohv": (0.0, 1.05),
    "ncp": (0.0, 1.0),
    "width": (0.0, None),
}


# ---------------------------------------------------------------------------
# Units and attributes
# ---------------------------------------------------------------------------


def normalize_units(units: Any) -> str:
    """UDUNITS spelling of a free-text ``units`` attribute (``""`` stays ``""``).

    ``unitless`` / ``none`` -> ``1``, ``m/s`` / ``meters per seconds`` -> ``m s-1``, ``deg/km`` ->
    ``degrees km-1``, ``meters`` -> ``m``. Anything not recognised is returned unchanged.
    """
    raw = provenance.clean_text(units)
    if not raw:
        return ""
    if re.match(r"^\w+ since ", raw):  # time reference: leave
        return raw
    compact = re.sub(r"\s+", "", raw).lower().replace("_", "")
    if compact in _UNIT_WORDS:
        return _UNIT_WORDS[compact]
    if raw.lower().startswith(("degrees_", "degree_")):  # degrees_north / degrees_east
        return raw
    cls = unit_class(raw)
    if cls in _CLASS_UNITS:
        return _CLASS_UNITS[cls]
    if cls == "deg":
        return "degrees"
    return raw


def long_name_for(name: str, attrs: Mapping[str, Any]) -> str:
    """The ``long_name`` to write: the file's own if any, else a readable one from the role/name."""
    existing = provenance.clean_text(attrs.get("long_name"))
    if existing:
        return existing.replace("_", " ") if "_" in existing and " " not in existing else existing
    role = role_for_name(name)
    if role is not None:
        return role.label
    return name.replace("__", " / ").replace("_", " ")


def finalize_variable_attrs(
    name: str,
    attrs: Mapping[str, Any],
    *,
    is_float_field: bool,
    existing_names: Iterable[str] = (),
) -> dict[str, Any]:
    """Standards-clean ``attrs`` of one variable (returns a new dict).

    ``is_float_field``: a measured/derived 2-D float field (units ``1`` when it has none).
    """
    out = {k: v for k, v in attrs.items() if v is not None}
    names = set(existing_names)
    # --- units
    units = normalize_units(out.get("units", out.get("unit")))
    out.pop("unit", None)
    if not units and name in _NAME_UNITS:
        units = _NAME_UNITS[name]
    if not units and is_float_field:
        role = role_for_name(name)
        units = _ROLE_UNITS.get(role.key, "1") if role else "1"
    if units:
        out["units"] = units
    else:
        out.pop("units", None)
    # --- standard_name: only names of the CF table
    std = provenance.clean_text(out.get("standard_name"))
    role = role_for_name(name) if is_float_field else None
    if role is not None and role.key in ROLE_STANDARD_NAME:
        std_role = ROLE_STANDARD_NAME[role.key]
        if (role.key == "reflectivity" and units in ("dBZ", "dB")) or role.key == "velocity":
            std = std_role
    if std and std not in KEEP_STANDARD_NAMES:
        std = ""
    if std:
        out["standard_name"] = std
    else:
        out.pop("standard_name", None)
    # --- long_name
    out["long_name"] = long_name_for(name, out)
    # --- non-CF axis values (CfRadial has radial_range_coordinate ...)
    if "axis" in out and str(out["axis"]) not in _CF_AXIS:
        out.pop("axis")
    # --- dangling references
    for key in ("grid_mapping", "bounds", "climatology"):
        if key in out and str(out[key]) not in names:
            out.pop(key)
    if "ancillary_variables" in out:
        kept = [n for n in str(out["ancillary_variables"]).split() if n in names]
        if kept:
            out["ancillary_variables"] = " ".join(kept)
        else:
            out.pop("ancillary_variables")
    return out


def flag_attrs(categories: Iterable[Mapping[str, Any]]) -> tuple[np.ndarray, str]:
    """CF ``flag_values`` (int) and ``flag_meanings`` (blank-separated unique words)."""
    values: list[int] = []
    words: list[str] = []
    for c in categories:
        value = int(c["value"])
        word = (
            re.sub(r"[^A-Za-z0-9_]+", "_", str(c.get("label", ""))).strip("_") or f"class_{value}"
        )
        base, n = word, 2
        while word in words:
            word = f"{base}_{n}"
            n += 1
        values.append(value)
        words.append(word)
    dtype = np.int8 if values and min(values) >= -120 and max(values) <= 120 else np.int16
    return np.asarray(values, dtype=dtype), " ".join(words)


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def _chunks_for(shape: tuple[int, ...]) -> tuple[int, ...]:
    if len(shape) == 1:
        return (min(shape[0], 65536),)
    if len(shape) == 2:
        return (min(shape[0], 512), min(shape[1], 1024))
    return tuple(min(s, 64) for s in shape)


def compression_encoding(shape: tuple[int, ...], *, level: int = 4) -> dict[str, Any]:
    """zlib + shuffle with a sensible chunk shape (only for variables worth compressing)."""
    if int(np.prod(shape)) < 1024:
        return {}
    return {"zlib": True, "complevel": level, "shuffle": True, "chunksizes": _chunks_for(shape)}


def field_encoding(shape: tuple[int, ...], flag_values: np.ndarray | None = None) -> dict[str, Any]:
    """Encoding of a measured/derived field: float32 + NaN fill (integer + fill when flagged)."""
    enc = compression_encoding(shape)
    if flag_values is not None and flag_values.size:
        enc.update(dtype=np.dtype(flag_values.dtype).name, _FillValue=int(flag_values.min()) - 1)
        return enc
    enc.update(dtype="float32", _FillValue=np.float32(np.nan))
    return enc


def to_char(values: Any, width: int = STRING_WIDTH) -> np.ndarray:
    """Fixed-width bytes (``S<width>``) for a str/bytes scalar or array (UTF-8)."""
    arr = np.asarray(values)
    flat = [
        v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
        for v in arr.ravel().tolist()
    ]
    need = max([len(s.encode("utf-8")) for s in flat] + [1])
    out = np.array([s.encode("utf-8") for s in flat], dtype=f"S{max(width, need)}")
    return out.reshape(arr.shape)


def set_char_coord(ds: xr.Dataset, name: str, dim: str, values: Any, long_name: str) -> xr.Dataset:
    """Add the labels *values* (one per entry of *dim*) as a character-array coordinate *name*."""
    da = xr.DataArray(
        to_char(list(values)), dims=(dim,), attrs={"long_name": long_name, "_Encoding": "utf-8"}
    )
    da.encoding = {"char_dim_name": STRING_DIM}
    return ds.assign_coords({name: da})


def is_text(var: xr.Variable) -> bool:
    """A string variable (unicode, bytes or object array of str)."""
    if var.dtype.kind in ("U", "S"):
        return True
    if var.dtype.kind == "O":
        sample = var.values.ravel()[:4].tolist()
        return bool(sample) and all(isinstance(v, (str, bytes)) for v in sample)
    return False


# ---------------------------------------------------------------------------
# Whole-dataset finalisation
# ---------------------------------------------------------------------------

_SITE = {
    "latitude": ("latitude", "degrees_north", "latitude of the instrument"),
    "longitude": ("longitude", "degrees_east", "longitude of the instrument"),
    "altitude": ("altitude", "m", "altitude of the instrument above mean sea level"),
}


def time_reference(ds: xr.Dataset) -> np.datetime64 | None:
    """The earliest valid ``time`` of *ds* (the epoch of ``seconds since ...``), or ``None``."""
    if "time" not in ds.variables or ds["time"].dtype.kind != "M":
        return None
    t = ds["time"].values
    t = t[~np.isnat(t)]
    return t.min() if t.size else None


def _within(values: np.ndarray, lo: float | None, hi: float | None) -> bool:
    finite = values[np.isfinite(values)]
    if not finite.size:
        return False
    return (lo is None or float(finite.min()) >= lo) and (hi is None or float(finite.max()) <= hi)


def finalize_dataset(
    ds: xr.Dataset,
    *,
    reference: np.datetime64 | None = None,
    char_arrays: bool = True,
    valid_range: bool = True,
) -> xr.Dataset:
    """A copy of *ds* whose variable attributes and encodings follow CF / CfRadial.

    Covers units, standard/long names, dangling references, ``flag_*``, ``valid_*``, character
    arrays (``char_arrays``), ``time`` as ``seconds since <ISO>``, float32 + NaN-fill + zlib for
    2-D fields, no fill value on coordinates. Global attributes are the caller's business.
    """
    ds = ds.copy(deep=False)
    names = set(ds.variables)
    ref = reference if reference is not None else time_reference(ds)
    text_names = [n for n in ds.variables if is_text(ds.variables[n])]
    width = STRING_WIDTH
    for n in text_names:
        width = max(width, to_char(ds.variables[n].values).dtype.itemsize)
    replacements: dict[str, xr.DataArray] = {}
    for name in list(ds.variables):
        var = ds.variables[name]
        attrs = dict(var.attrs)
        is_field = (
            var.ndim >= 2
            and var.dtype.kind == "f"
            and name not in ("lat", "lon")
            and not str(name).endswith("_bnds")
        )
        new = finalize_variable_attrs(name, attrs, is_float_field=is_field, existing_names=names)
        enc: dict[str, Any] = {}
        if name in text_names:
            new.pop("units", None)
            if char_arrays:
                da = xr.DataArray(to_char(var.values, width), dims=var.dims, attrs=new)
                da.encoding = {"char_dim_name": STRING_DIM}
                replacements[name] = da
                continue
        elif name == "time" and var.dtype.kind == "M":
            if ref is not None:
                enc = {
                    "units": f"seconds since {provenance.iso_utc(ref)}",
                    "calendar": "standard",
                    "dtype": "float64",
                    "_FillValue": None,
                }
            new.pop("units", None)
            new["standard_name"] = "time"
            new["long_name"] = "time of each ray (UTC)"
        elif var.dtype.kind == "M":
            enc = {"_FillValue": None}
            new.pop("units", None)
        elif name in _SITE and var.ndim == 0:
            std, units, long_name = _SITE[name]
            new.update(standard_name=std, units=units, long_name=long_name)
            new.pop("positive", None)
            if name == "altitude":
                new["positive"] = "up"
            enc = {"_FillValue": None}
        elif is_field:
            flags = new.get("flag_values")
            fv = None
            if flags is not None:
                arr = np.asarray(flags)
                if arr.dtype.kind in "iuf" and arr.size:
                    ints = arr.astype(np.int64)
                    fv = ints.astype(
                        np.int8 if ints.min() >= -120 and ints.max() <= 120 else np.int16
                    )
                    new["flag_values"] = fv
                    new.pop("units", None)
            enc = field_encoding(var.shape, fv)
            if fv is None and valid_range:
                role = role_for_name(name)
                rng = _VALID_RANGE.get(role.key) if role else None
                if rng and "valid_min" not in new and "valid_range" not in new:
                    lo, hi = rng
                    if _within(np.asarray(var.values, dtype="float64"), lo, hi):
                        if lo is not None:
                            new["valid_min"] = np.float32(lo)
                        if hi is not None:
                            new["valid_max"] = np.float32(hi)
        elif var.dtype.kind == "f":
            enc = (
                {"_FillValue": None}
                if (name in ds.dims)
                else {"_FillValue": var.dtype.type(np.nan)}
            )
            enc.update(compression_encoding(var.shape))
        elif var.dtype.kind in "iu":
            enc = compression_encoding(var.shape)
        var.attrs = new
        var.encoding = enc
    for name, da in replacements.items():
        ds[name] = da
    return ds


def drop_placeholders(ds: xr.Dataset) -> xr.Dataset:
    """Remove all-NaN float variables xradar creates for information the file did not have."""
    drop = [
        n
        for n in ("polarization_mode",)
        if n in ds.variables
        and ds[n].dtype.kind == "f"
        and not np.isfinite(np.asarray(ds[n].values, dtype="float64")).any()
    ]
    return ds.drop_vars(drop) if drop else ds


# ---------------------------------------------------------------------------
# Global attributes of a radar volume
# ---------------------------------------------------------------------------

#: written by the engine; any other attribute of the source volume is passed through
_MANAGED_ATTRS = frozenset(
    {
        "Conventions",
        "Sub_conventions",
        "version",
        "title",
        "institution",
        "references",
        "source",
        "history",
        "comment",
        "wmo__cf_profile",
    }
)
_XRADAR_COMMENT = "im/exported using xradar"
MASK_NOTE = (
    "Gates below the detection threshold or range-folded are not measurements and are stored "
    "as _FillValue (NaN)."
)


def _iso_or_empty(value: Any) -> str:
    return provenance.iso_utc(value)


def radar_global_attrs(
    src: Mapping[str, Any],
    *,
    kind: str,
    t_start: Any = None,
    t_end: Any = None,
    source_files: Any = None,
    action: str,
    params: Mapping[str, Any] | None = None,
    masked: bool = True,
    ray_times_increase: bool | None = None,
) -> dict[str, Any]:
    """Global attributes of a CfRadial1 (``kind="cfradial1"``) or CfRadial2/Zarr volume.

    The source volume's own attributes (instrument/site/scan names, VCP details ...) are kept;
    ``"None"`` placeholders are replaced by real text; ``history`` gets the engine's line appended.
    """
    names = provenance.source_names(source_files)
    instrument = provenance.clean_text(src.get("instrument_name"))
    scan = provenance.clean_text(src.get("scan_name"))
    start, end = _iso_or_empty(t_start), _iso_or_empty(t_end)
    title = provenance.clean_text(src.get("title")) or " ".join(
        x for x in (instrument, scan, "radar volume", start) if x
    )
    comment_parts = [
        c for c in (provenance.clean_text(src.get("comment")),) if c and c != _XRADAR_COMMENT
    ]
    if masked:
        comment_parts.append(MASK_NOTE)
    refs = provenance.clean_text(src.get("references"))
    if REFERENCES not in refs:
        refs = f"{refs}; {REFERENCES}" if refs else REFERENCES
    src_text = provenance.clean_text(src.get("source")) or (
        f"radar observations read from {', '.join(names)}" if names else "radar observations"
    )
    if kind == "cfradial1":
        conventions, version = CFRADIAL1_CONVENTIONS, CFRADIAL1_VERSION
    else:
        conventions, version = CFRADIAL2_CONVENTIONS, CFRADIAL2_VERSION
    attrs = provenance.standard_global_attrs(
        title=title,
        source_files=names,
        action=action,
        params=params,
        institution=src.get("institution"),
        references=refs,
        comment=" ".join(comment_parts),
        source=src_text,
        conventions=conventions,
        history=src.get("history"),
        layer=layer_provenance.observed_radar(
            radar=instrument,
            scan_time=start,
            fmt=kind,
            simulated=provenance.clean_text(src.get("simulated_data")).lower() == "true",
        ).to_dict(),
    )
    attrs["version"] = version
    if kind == "cfradial1":
        attrs["Sub_conventions"] = CFRADIAL1_SUB_CONVENTIONS
    attrs["instrument_name"] = instrument or "unknown"
    attrs["site_name"] = provenance.clean_text(src.get("site_name")) or instrument
    if scan:
        attrs["scan_name"] = scan
    attrs["scan_id"] = provenance.clean_text(src.get("scan_id")) or "0"
    attrs["platform_is_mobile"] = provenance.clean_text(src.get("platform_is_mobile")) or "false"
    if ray_times_increase is None:
        attrs["ray_times_increase"] = provenance.clean_text(src.get("ray_times_increase")) or "true"
    else:
        attrs["ray_times_increase"] = "true" if ray_times_increase else "false"
    attrs["simulated_data"] = provenance.clean_text(src.get("simulated_data")) or "false"
    if start:
        attrs["time_coverage_start"] = start
    if end:
        attrs["time_coverage_end"] = end
    for k, v in src.items():  # pass-through (VCP details, dynamic_scan_type, ...)
        if k in _MANAGED_ATTRS or k in attrs:
            continue
        attrs[k] = v
    return {k: v for k, v in attrs.items() if v is not None}


def _scalar_text(value: str, long_name: str) -> xr.DataArray:
    da = xr.DataArray(value, attrs={"long_name": long_name})
    return da


def _ray_times_increase(times: np.ndarray) -> bool:
    t = times[~np.isnat(times)]
    return bool(t.size < 2 or np.all(np.diff(t.astype("datetime64[ns]").astype("int64")) >= 0))


def _source_of(dtree: xr.DataTree, source_files: Any) -> list[str]:
    if source_files:
        return provenance.source_names(source_files)
    enc = getattr(dtree, "encoding", {}) or {}
    return provenance.source_names(enc.get("source_path") or enc.get("source"))


# ---------------------------------------------------------------------------
# CfRadial1
# ---------------------------------------------------------------------------


def cfradial1_dataset(dtree: xr.DataTree) -> xr.Dataset:
    """The flat CfRadial1 dataset of *dtree* (what ``xradar.to_cfradial1`` writes, minus I/O)."""
    from xradar.io.export import cfradial1 as c1

    from engine.reader import sorted_sweep_names

    sweeps = sorted_sweep_names(dtree)
    dim0 = _ray_dim(dtree[sweeps[0]].to_dataset()) if sweeps else None
    dataset = c1._variable_mapper(dtree, dim0=dim0)
    if "radar_calibration" in dtree.children:
        dataset.update(c1._calib_mapper(dtree["radar_calibration"].to_dataset()))
    for name in ("radar_parameters", "georeferencing_correction"):
        if name in dtree.children:
            dataset.update(dtree[name].to_dataset().reset_coords())
    if "sweep_mode" in dataset.variables:
        dataset["sweep_mode"] = dataset["sweep_mode"].astype("S")
    return dataset


def _write_cfradial1_with_xradar(
    dtree: xr.DataTree,
    path: str | Path,
    source_files: Any,
    params: Mapping[str, Any] | None,
    masked: bool,
) -> None:
    """Fallback: xradar's public writer, then the standard global attributes patched in place.

    The variable-level clean-up (units, standard names, float32/compression) is not applied here.
    """
    import netCDF4
    from xradar.io.export.cfradial1 import to_cfradial1

    to_cfradial1(dtree=dtree, filename=str(path))
    times = [
        dtree[n].to_dataset()["time"].values
        for n in dtree.children
        if n.startswith("sweep_") and "time" in dtree[n].to_dataset()
    ]
    flat = np.concatenate(times) if times else np.array([], "datetime64[ns]")
    flat = flat[~np.isnat(flat)] if flat.size else flat
    attrs = radar_global_attrs(
        dtree.attrs,
        kind="cfradial1",
        t_start=flat.min() if flat.size else None,
        t_end=flat.max() if flat.size else None,
        source_files=_source_of(dtree, source_files),
        action="export CfRadial1 NetCDF (xradar writer)",
        params=params,
        masked=masked,
    )
    with netCDF4.Dataset(str(path), "a") as nc:
        for key, value in attrs.items():
            nc.setncattr(key, value)


@hdf5_guard.guarded
def write_cfradial1(
    dtree: xr.DataTree,
    path: str | Path,
    *,
    source_files: Any = None,
    params: Mapping[str, Any] | None = None,
    masked: bool = True,
) -> None:
    """Write *dtree* (already NetCDF-safe, see ``exporter._netcdf_safe_tree``) as CfRadial 1.4."""
    try:
        ds = drop_placeholders(cfradial1_dataset(dtree))
    except (ImportError, AttributeError) as exc:  # xradar renamed its private helpers
        logger.warning("xradar's CfRadial1 helpers are unavailable (%s); using its writer", exc)
        _write_cfradial1_with_xradar(dtree, path, source_files, params, masked)
        return
    ref = time_reference(ds)
    times = ds["time"].values if "time" in ds.variables else np.array([], "datetime64[ns]")
    valid = times[~np.isnat(times)] if times.size else times
    start = valid.min() if valid.size else None
    end = valid.max() if valid.size else None
    attrs = radar_global_attrs(
        dtree.attrs,
        kind="cfradial1",
        t_start=start,
        t_end=end,
        source_files=_source_of(dtree, source_files),
        action="export CfRadial1 NetCDF",
        params=params,
        masked=masked,
        ray_times_increase=_ray_times_increase(times) if times.size else None,
    )
    for name, value, long_name in (
        ("time_coverage_start", attrs.get("time_coverage_start", ""), "UTC time of first ray"),
        ("time_coverage_end", attrs.get("time_coverage_end", ""), "UTC time of last ray"),
    ):
        ds[name] = _scalar_text(value, long_name)
    for name, default in (("platform_type", "fixed"), ("instrument_type", "radar")):
        if name not in ds.variables:
            ds[name] = _scalar_text(default, name.replace("_", " ").capitalize())
    if "volume_number" not in ds.variables:
        ds["volume_number"] = xr.DataArray(np.int32(0), attrs={"long_name": "volume number"})
    ds = finalize_dataset(ds, reference=ref)
    ds.attrs = attrs
    ds.encoding = {}
    ds.to_netcdf(str(path), format="NETCDF4", engine="netcdf4")


# ---------------------------------------------------------------------------
# CfRadial2 / FM-301 tree (NetCDF groups or Zarr)
# ---------------------------------------------------------------------------


def _ray_dim(ds: xr.Dataset) -> str:
    """The dimension that indexes the rays of a sweep (``azimuth`` for PPIs; RHI files differ)."""
    if "time" in ds.variables and ds["time"].ndim == 1:
        return str(ds["time"].dims[0])
    return "time"


def _conform_sweep(ds: xr.Dataset) -> xr.Dataset:
    """xradar's CfRadial2 sweep layout, keeping the optional per-ray metadata and sweep attrs."""
    from xradar import model

    dim0 = _ray_dim(ds)
    keep = model.determine_cfradial2_sweep_variables(ds, True, dim0)
    remove = (set(ds.data_vars) | set(ds.coords)) - keep
    out = ds.drop_vars(remove)
    out.attrs = dict(ds.attrs)
    if "time" not in ds.variables:  # a sweep without ray times (synthetic or stripped): keep as is
        return out
    if dim0 != "time" and dim0 in ds.dims:
        out = out.swap_dims({dim0: "time"})
    return out.sortby("time")


def cfradial2_tree(
    dtree: xr.DataTree,
    *,
    source_files: Any = None,
    params: Mapping[str, Any] | None = None,
    masked: bool = True,
    action: str = "export CfRadial2 NetCDF",
    char_arrays: bool = True,
) -> xr.DataTree:
    """A standards-clean CfRadial2 / FM-301 style tree (root metadata + ``sweep_N`` groups)."""
    from engine.reader import sorted_sweep_names

    sweeps: dict[str, xr.Dataset] = {}
    for name in sorted_sweep_names(dtree):
        ds = dtree[name].to_dataset(inherit="all_coords")
        sweeps[name] = _conform_sweep(ds)
    refs = [time_reference(s) for s in sweeps.values()]
    refs = [r for r in refs if r is not None]
    ref = min(refs) if refs else None
    ends = [
        s["time"].values[~np.isnat(s["time"].values)].max()
        for s in sweeps.values()
        if "time" in s and s["time"].dtype.kind == "M" and (~np.isnat(s["time"].values)).any()
    ]
    end = max(ends) if ends else None
    sweep_times = [s["time"].values for s in sweeps.values() if "time" in s]
    all_times = np.concatenate(sweep_times) if sweep_times else np.array([], "datetime64[ns]")
    root = dtree.to_dataset().drop_vars(["sweep_group_name", "sweep_fixed_angle"], errors="ignore")
    root.attrs = radar_global_attrs(
        dtree.attrs,
        kind="cfradial2",
        t_start=ref,
        t_end=end,
        source_files=_source_of(dtree, source_files),
        action=action,
        params=params,
        masked=masked,
        ray_times_increase=_ray_times_increase(all_times) if all_times.size else None,
    )
    group_names = list(sweeps)
    root["sweep_group_name"] = xr.DataArray(
        np.array(group_names, dtype=object),
        dims="sweep",
        attrs={"long_name": "group names for each sweep"},
    )
    root["sweep_fixed_angle"] = xr.DataArray(
        np.array(
            [
                float(np.asarray(s["sweep_fixed_angle"].values))
                if "sweep_fixed_angle" in s
                else np.nan
                for s in sweeps.values()
            ]
        ),
        dims="sweep",
        attrs={"long_name": "target angle for sweep", "units": "degrees"},
    )
    for name, key, long_name in (
        ("time_coverage_start", "time_coverage_start", "UTC time of first ray"),
        ("time_coverage_end", "time_coverage_end", "UTC time of last ray"),
    ):
        root[name] = _scalar_text(root.attrs.get(key, ""), long_name)
    for name, default in (("platform_type", "fixed"), ("instrument_type", "radar")):
        if name not in root.variables:
            root[name] = _scalar_text(default, name.replace("_", " ").capitalize())
    if "volume_number" not in root.variables:
        root["volume_number"] = xr.DataArray(np.int32(0), attrs={"long_name": "volume number"})
    attrs = root.attrs
    root = finalize_dataset(root, reference=ref, char_arrays=char_arrays)
    root.attrs = attrs
    nodes: dict[str, xr.Dataset] = {"/": root}
    for name, ds in sweeps.items():
        sweep_attrs = dict(ds.attrs)
        fin = finalize_dataset(ds, reference=ref, char_arrays=char_arrays)
        fin.attrs = sweep_attrs
        nodes[f"/{name}"] = fin
    for child in dtree.children:  # radar_parameters, radar_calibration, ... pass through
        if child.startswith("sweep"):
            continue
        sub = dtree[child].to_dataset()
        sub_attrs = dict(sub.attrs)
        fin = finalize_dataset(sub, char_arrays=char_arrays)
        fin.attrs = sub_attrs
        nodes[f"/{child}"] = fin
    return xr.DataTree.from_dict(nodes)


@hdf5_guard.guarded
def write_cfradial2(
    dtree: xr.DataTree,
    path: str | Path,
    *,
    source_files: Any = None,
    params: Mapping[str, Any] | None = None,
    masked: bool = True,
) -> None:
    """Write *dtree* (already NetCDF-safe) as a CfRadial2 / FM-301 NetCDF4 file."""
    tree = cfradial2_tree(
        dtree,
        source_files=source_files,
        params=params,
        masked=masked,
        action="export CfRadial2 NetCDF",
    )
    tree.to_netcdf(str(path), engine="netcdf4", format="NETCDF4")


def _zarr_encoding(var: xr.Variable) -> dict[str, Any]:
    enc = var.encoding
    out: dict[str, Any] = {}
    for key in ("dtype", "_FillValue", "units", "calendar"):
        if key in enc:
            out[key] = enc[key]
    if "chunksizes" in enc:
        out["chunks"] = tuple(enc["chunksizes"])
    return out


def write_zarr(
    dtree: xr.DataTree,
    path: str | Path,
    *,
    source_files: Any = None,
    params: Mapping[str, Any] | None = None,
    masked: bool = True,
    zarr_format: int = 2,
) -> None:
    """Write *dtree* as a Zarr store with the CfRadial2 layout and CF attributes.

    Zarr format 2 (default) with consolidated metadata (``.zmetadata``: one read opens the whole
    tree; readable by every Zarr implementation); ``zarr_format=3`` writes a v3 store (the
    consolidated-metadata extension is not part of the v3 specification, so it is not written).
    """
    tree = cfradial2_tree(
        dtree,
        source_files=source_files,
        params=params,
        masked=masked,
        action="export Zarr",
        char_arrays=False,
    )
    for node in tree.subtree:
        ds = node.to_dataset()
        for var in ds.variables.values():
            var.encoding = _zarr_encoding(var)
        node.dataset = ds
    tree.to_zarr(
        str(path),
        mode="w",
        zarr_format=zarr_format,
        consolidated=zarr_format == 2,
    )
