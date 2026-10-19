"""Reusable CF-conformance checks for the NetCDF files the engine writes.

Two layers:

* :func:`cfchecker_report` runs the real **CF Checker** (``pip install cfchecker cfunits``, needs
  the UDUNITS-2 library and the CF standard-name table, see :func:`standard_name_table`). Files
  with groups (CfRadial2) are checked group by group. Returns ``None`` when the checker or its
  data are not available (tests then skip).
* :func:`structural_problems` is a dependency-free (netCDF4 only) strict check of the rules this
  project cares about: ``Conventions``, units/long_name on every variable, references that
  resolve, ``flag_values`` consistency, time units, no ``"None"`` text, no absolute home paths,
  legal dimension names.

Usage in a test::

    problems = structural_problems(path)
    assert not problems, problems
    report = cfchecker_report(path)
    if report is not None:
        assert not report.errors, report.errors
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import netCDF4
import numpy as np

_DEFAULT_TABLE = Path.home() / ".cache" / "radarx-desktop" / "cf-standard-name-table.xml"
_UDUNITS_XML = (
    "/opt/homebrew/share/udunits/udunits2.xml",
    "/usr/share/udunits/udunits2.xml",
    "/usr/local/share/udunits/udunits2.xml",
)
_DIM_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.@+\-]*$")
_PATH_RE = re.compile(r"(/Users/|/home/|[A-Za-z]:\\Users\\|/private/var/|/var/folders/)")
_TIME_UNITS_RE = re.compile(
    r"^(seconds?|minutes?|hours?|days?|milliseconds?)\s+since\s+\d{4}-\d{2}-\d{2}"
    r"([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?\s*(Z|[+-]\d{2}:?\d{2})?$"
)
_CALENDARS = {
    "standard",
    "gregorian",
    "proleptic_gregorian",
    "noleap",
    "365_day",
    "all_leap",
    "366_day",
    "360_day",
    "julian",
    "none",
}


@dataclass
class CfReport:
    """Result of the real CF checker over one file (all groups)."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    infos: list[str] = field(default_factory=list)
    version: str = ""

    def filtered(self, allow: tuple[str, ...] = ()) -> list[str]:
        """Errors that do not contain any of the *allow* substrings."""
        return [e for e in self.errors if not any(a in e for a in allow)]


def standard_name_table() -> Path | None:
    """Path of the CF standard-name table XML (``$XRADAR_CF_STANDARD_NAME_TABLE`` or the cache)."""
    env = os.environ.get("XRADAR_CF_STANDARD_NAME_TABLE", "").strip()
    for candidate in (Path(env).expanduser() if env else None, _DEFAULT_TABLE):
        if candidate is not None and candidate.is_file():
            return candidate
    return None


def _ensure_udunits_env() -> None:
    if "UDUNITS2_XML_PATH" in os.environ:
        return
    for p in _UDUNITS_XML:
        if Path(p).is_file():
            os.environ["UDUNITS2_XML_PATH"] = p
            return


def real_checker_available() -> bool:
    """True if ``cfchecker`` (+ ``cfunits``/UDUNITS-2) and the standard-name table are usable."""
    if standard_name_table() is None:
        return False
    _ensure_udunits_env()
    try:
        import cfchecker.cfchecks  # noqa: F401
        import cfunits

        return bool(cfunits.Units("m s-1").isvalid)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# netCDF4 helpers
# ---------------------------------------------------------------------------


def _walk(group: netCDF4.Dataset | netCDF4.Group, path: str = "/"):
    yield path, group
    for name, sub in group.groups.items():
        yield from _walk(sub, f"{path.rstrip('/')}/{name}")


def _attr_dict(obj: Any) -> dict[str, Any]:
    return {a: obj.getncattr(a) for a in obj.ncattrs()}


def _flatten_group(
    root: netCDF4.Dataset, group: netCDF4.Group | netCDF4.Dataset, dest: str
) -> None:
    """Copy *group* (+ the scalar variables of the root it references) into a flat file."""
    out = netCDF4.Dataset(dest, "w", format="NETCDF4")
    try:
        for a, v in _attr_dict(root).items():
            out.setncattr(a, v)
        if group is not root:
            for a, v in _attr_dict(group).items():
                out.setncattr(a, v)
        # dimensions used by the group's variables (own or inherited)
        used = {d for var in group.variables.values() for d in var.dimensions}
        cur: Any = group
        while cur is not None:
            for name, dim in cur.dimensions.items():
                if name in used and name not in out.dimensions:
                    out.createDimension(name, len(dim))
            cur = cur.parent
        wanted = set(group.variables)
        for var in group.variables.values():
            for ref in str(
                var.getncattr("coordinates") if "coordinates" in var.ncattrs() else ""
            ).split():
                wanted.add(ref)
        for name in wanted:
            src = group.variables[name] if name in group.variables else root.variables.get(name)
            if src is None or (name not in group.variables and src.ndim != 0):
                continue
            if not isinstance(src.datatype, np.dtype):  # vlen / compound: not representable
                continue
            attrs = _attr_dict(src)
            fill = attrs.pop("_FillValue", None)
            if any(d not in out.dimensions for d in src.dimensions):
                continue
            dst = out.createVariable(name, src.dtype, src.dimensions, fill_value=fill)
            for a, v in attrs.items():
                dst.setncattr(a, v)
            src.set_auto_maskandscale(False)
            dst.set_auto_maskandscale(False)
            if src.ndim == 0:
                dst[...] = src[...]
            else:
                dst[:] = src[:]
    finally:
        out.close()


def cfchecker_report(
    path: str | Path, version: str = "auto", *, only_groups: bool = False
) -> CfReport | None:
    """Run the CF Checker on *path* (and each group of it). ``None`` if it is not available."""
    if not real_checker_available():
        return None
    _ensure_udunits_env()
    from cfchecker.cfchecks import CFChecker, CFVersion

    table = str(standard_name_table())
    ver: Any = False if version == "auto" else CFVersion(version)
    report = CfReport()
    with tempfile.TemporaryDirectory() as tmp:
        # CF Checker 4.1 knows CF up to 1.8 and rejects "CF-1.9"/"CF-1.10" outright: check a copy
        # whose Conventions says CF-1.8 (nothing the project writes needs 1.9/1.10 features).
        copy = Path(tmp) / "conventions_as_1_8.nc"
        with netCDF4.Dataset(str(path)) as probe:
            conv = probe.getncattr("Conventions") if "Conventions" in probe.ncattrs() else ""
        if re.search(r"CF-1\.(9|10)\b", str(conv)):
            shutil.copyfile(path, copy)
            with netCDF4.Dataset(str(copy), "a") as fix:
                fix.setncattr("Conventions", re.sub(r"CF-1\.(9|10)\b", "CF-1.8", str(conv)))
            path = copy
        return _run_checker(CFChecker, table, ver, path, only_groups, report, tmp)


def _collect(report: CfReport, where: str, label: str, block: dict[str, list[str]]) -> None:
    """Move the messages of one result block (global or one variable) into *report*."""
    for cat, bucket in (
        ("FATAL", report.errors),
        ("ERROR", report.errors),
        ("WARN", report.warnings),
        ("INFO", report.infos),
    ):
        bucket.extend(f"{where}{label}{m}" for m in block.get(cat, []))


def _run_checker(
    checker_cls: Any,
    table: str,
    ver: Any,
    path: str | Path,
    only_groups: bool,
    report: CfReport,
    tmp: str,
) -> CfReport:
    with netCDF4.Dataset(str(path)) as root:
        targets: list[tuple[str, str]] = []
        if (not only_groups or not root.groups) and (root.variables or root.ncattrs()):
            targets.append(("/", str(path)))
        for gpath, grp in _walk(root):
            if gpath == "/":
                continue
            dest = str(Path(tmp) / (gpath.strip("/").replace("/", "__") + ".nc"))
            _flatten_group(root, grp, dest)
            targets.append((gpath, dest))
        for gpath, f in targets:
            checker = checker_cls(
                useFileName="no",
                cfStandardNamesXML=table,
                version=ver,
                silent=True,
                cacheTables=False,
            )
            checker.checker(f)
            res = checker.all_results[
                f if f in checker.all_results else next(iter(checker.all_results))
            ]
            where = "" if gpath == "/" else f"[{gpath}] "
            _collect(report, where, "global: ", res["global"])
            for vname, block in res["variables"].items():
                _collect(report, where, f"{vname}: ", block)
            report.version = str(checker.version)
    return report


# ---------------------------------------------------------------------------
# Strict structural checks (netCDF4 only)
# ---------------------------------------------------------------------------


def _units_valid(units: str) -> bool | None:
    """True/False via cfunits when available, else ``None`` (unknown)."""
    _ensure_udunits_env()
    try:
        import cfunits

        return bool(cfunits.Units(units).isvalid)
    except Exception:
        return None


def structural_problems(
    path: str | Path,
    *,
    require_conventions: bool = True,
    allow_missing_long_name: tuple[str, ...] = (),
    allow_missing_units: tuple[str, ...] = (),
) -> list[str]:
    """Return a list of human-readable problems (empty = fine) of the NetCDF file *path*."""
    problems: list[str] = []
    with netCDF4.Dataset(str(path)) as root:
        gattrs = _attr_dict(root)
        conv = gattrs.get("Conventions")
        if require_conventions and not (isinstance(conv, str) and conv.strip()):
            problems.append("global: missing Conventions attribute")
        if isinstance(conv, str) and conv.strip() == "None":
            problems.append("global: Conventions is the placeholder 'None'")
        for gpath, grp in _walk(root):
            tag = "" if gpath == "/" else f"[{gpath}] "
            # attributes: placeholder text, absolute paths, types
            for owner, attrs in [("global", _attr_dict(grp))] + [
                (n, _attr_dict(v)) for n, v in grp.variables.items()
            ]:
                for a, v in attrs.items():
                    where = f"{tag}{owner}:{a}"
                    if isinstance(v, str):
                        if v.strip() in ("None", "nan"):
                            problems.append(f"{where}: placeholder text {v!r}")
                        if _PATH_RE.search(v):
                            problems.append(f"{where}: embeds an absolute path")
                    elif isinstance(v, np.ndarray) and v.dtype.kind not in "iuf":
                        problems.append(f"{where}: unsupported attribute type {v.dtype}")
            for dname in grp.dimensions:
                if not _DIM_RE.match(dname):
                    problems.append(f"{tag}dimension name {dname!r} is not legal")
            names = set(grp.variables) | set(root.variables)
            for name, var in grp.variables.items():
                attrs = _attr_dict(var)
                vt = f"{tag}{name}"
                if not isinstance(var.datatype, np.dtype):
                    problems.append(f"{vt}: variable-length / compound type {var.datatype}")
                    continue
                is_text = var.dtype.kind in ("S", "U")
                # long_name / standard_name
                if (
                    not is_text
                    and "long_name" not in attrs
                    and "standard_name" not in attrs
                    and name not in allow_missing_long_name
                ):
                    problems.append(f"{vt}: no long_name or standard_name")
                if is_text and "long_name" not in attrs and name not in allow_missing_long_name:
                    problems.append(f"{vt}: text variable without long_name")
                # units
                units = attrs.get("units")
                if units is not None:
                    if not isinstance(units, str):
                        problems.append(f"{vt}: units is not a string")
                    elif units == "unitless" or units.strip() == "":
                        problems.append(f"{vt}: units {units!r} is not a UDUNITS string (use '1')")
                    elif not _TIME_UNITS_RE.match(units):
                        ok = _units_valid(units)
                        if ok is False:
                            problems.append(f"{vt}: units {units!r} do not parse (UDUNITS)")
                elif (
                    var.dtype.kind == "f"
                    and var.ndim >= 2
                    and "flag_values" not in attrs
                    and name not in allow_missing_units
                ):
                    problems.append(f"{vt}: field without units")
                # time
                if units is not None and isinstance(units, str) and " since " in units:
                    if not _TIME_UNITS_RE.match(units):
                        problems.append(f"{vt}: time units {units!r} are not '<unit> since <ISO>'")
                    cal = attrs.get("calendar", "standard")
                    if cal not in _CALENDARS:
                        problems.append(f"{vt}: calendar {cal!r} unknown")
                # references
                for key in ("coordinates", "ancillary_variables"):
                    for ref in str(attrs.get(key, "")).split():
                        if ref not in names:
                            problems.append(f"{vt}: {key} names missing variable {ref!r}")
                gm = attrs.get("grid_mapping")
                if gm is not None:
                    if ":" in str(gm):  # extended form "crs: x y"
                        refs = [t.rstrip(":") for t in str(gm).split() if t.endswith(":")]
                    else:
                        refs = [str(gm)]
                    for ref in refs:
                        if ref not in names:
                            problems.append(f"{vt}: grid_mapping names missing variable {ref!r}")
                for key in ("bounds", "climatology"):
                    if key in attrs and str(attrs[key]) not in names:
                        problems.append(f"{vt}: {key} names missing variable {attrs[key]!r}")
                # flags
                if "flag_values" in attrs or "flag_meanings" in attrs:
                    fv = np.atleast_1d(attrs.get("flag_values", []))
                    fm = str(attrs.get("flag_meanings", "")).split()
                    if len(fv) != len(fm):
                        problems.append(f"{vt}: {len(fv)} flag_values but {len(fm)} flag_meanings")
                    if len(set(fm)) != len(fm):
                        problems.append(f"{vt}: duplicate flag_meanings")
                    if len(set(fv.tolist())) != len(fv):
                        problems.append(f"{vt}: duplicate flag_values")
                    if "flag_values" in attrs and fv.dtype != var.dtype:
                        problems.append(
                            f"{vt}: flag_values type {fv.dtype} != variable type {var.dtype}"
                        )
                # fill value
                fill = attrs.get("_FillValue")
                if fill is not None and np.asarray(fill).dtype != var.dtype and not is_text:
                    problems.append(
                        f"{vt}: _FillValue type {np.asarray(fill).dtype} != {var.dtype}"
                    )
                if (
                    ("scale_factor" in attrs or "add_offset" in attrs)
                    and var.dtype.kind in "iu"
                    and fill is None
                    and "valid_range" not in attrs
                    and "valid_min" not in attrs
                ):
                    problems.append(f"{vt}: packed integer data without _FillValue")
                if name in grp.dimensions and fill is not None:
                    problems.append(f"{vt}: coordinate variable has a _FillValue")
    return problems
