"""One place that describes *who wrote a file, when, from what and with which settings*.

Every writer of the engine (CfRadial NetCDF, mosaic and wind grids, Zarr, CSV, figures, recipe
files and scripts) builds its global attributes / metadata block / history line here, so all
outputs carry the same self-describing information and none of them embeds an absolute path of the
user's machine.

Public API (all pure functions, no server state, safe to call from a CLI)::

    software_version()                     # "0.2.1"
    software_string()                      # "radarx-desktop 0.2.1"
    library_versions()                     # {"python": ..., "xarray": ..., "xradar": ..., ...}
    utc_stamp()                            # "2026-10-19T11:21:00Z"
    history_line("export netcdf", {...})   # "2026-...Z: radarx-desktop 0.2.1: export netcdf (...)"
    append_history(existing, line)
    standard_global_attrs(...)             # title/institution/source/history/... (strings only)
    csv_comment_block(...)                 # "# key: value" lines for the head of a CSV
    figure_metadata(...)                   # key -> text for PNG text chunks / PDF info / SVG
    redact_path(text)                      # "/Users/me/data/x.nc" -> "x.nc" ; "~" for the home dir
"""

from __future__ import annotations

import json
import os
import platform
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

SOFTWARE_NAME = "radarx-desktop"
#: Version of the layout of the provenance attributes written by this module.
PROVENANCE_SCHEMA = 1
#: Packages whose versions are recorded (name on PyPI -> key in the output).
_LIBRARIES = ("xradar", "xarray", "numpy", "netCDF4", "zarr", "pyproj", "radarx")

_ENV_VERSION = "XRADAR_DESKTOP_VERSION"
_OFFSET_TIME = re.compile(
    r"(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?"
    r"(?P<sign>[+-])(?P<oh>\d{2}):?(?P<om>\d{2})"
)
_PLACEHOLDERS = {"none", "nan", "null", "n/a", "unknown"}


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


def _package_json_version() -> str | None:
    """App version from the ``package.json`` of a source checkout (engine/ -> python/ -> root)."""
    here = Path(__file__).resolve()
    for parent in list(here.parents)[:4]:
        candidate = parent / "package.json"
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            if data.get("name") == SOFTWARE_NAME and data.get("version"):
                return str(data["version"])
            return None
    return None


@lru_cache(maxsize=1)
def software_version() -> str:
    """The app version: ``$XRADAR_DESKTOP_VERSION``, else ``package.json``, else package metadata.

    Falls back to ``"unknown"`` (never raises) so a writer can always stamp its output.
    """
    env = os.environ.get(_ENV_VERSION, "").strip()
    if env:
        return env
    version = _package_json_version()
    if version:
        return version
    try:
        from importlib import metadata

        for name in (
            "radarx-desktop-engine",
            "xradar-desktop-engine",
        ):  # the old name: older installs
            try:
                return metadata.version(name)
            except metadata.PackageNotFoundError:
                continue
        return "unknown"
    except Exception:
        return "unknown"


def software_string() -> str:
    """``"radarx-desktop 0.2.1"``."""
    return f"{SOFTWARE_NAME} {software_version()}"


def _module_version(name: str) -> str | None:
    try:
        from importlib import metadata

        return metadata.version(name)
    except Exception:
        return None


@lru_cache(maxsize=1)
def _library_versions() -> tuple[tuple[str, str], ...]:
    out = [("python", platform.python_version())]
    for name in _LIBRARIES:
        v = _module_version(name)
        if v is not None:
            out.append((name, v))
    return tuple(out)


def library_versions() -> dict[str, str]:
    """``{"python": "3.14.2", "xradar": "0.12.0", ...}`` (only the packages that are installed)."""
    return dict(_library_versions())


def libraries_string() -> str:
    """``"python 3.14.2; xradar 0.12.0; xarray 2026.4.0; ..."``."""
    return "; ".join(f"{k} {v}" for k, v in _library_versions())


def radarx_version() -> str | None:
    """Installed radarx version, or ``None`` when the optional package is absent."""
    return library_versions().get("radarx")


# ---------------------------------------------------------------------------
# Time, paths, text
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def utc_stamp(when: datetime | None = None) -> str:
    """ISO-8601 UTC timestamp with a ``Z`` suffix: ``2026-10-19T11:21:00Z``."""
    when = (when or utc_now()).astimezone(UTC)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_utc(value: Any) -> str:
    """Best-effort ISO-8601 UTC (``...Z``) text of a datetime64 / datetime / string; else ""."""
    if value is None:
        return ""
    try:
        import numpy as np

        if isinstance(value, np.datetime64):
            if np.isnat(value):
                return ""
            return str(np.datetime_as_string(value, unit="s")) + "Z"
        if isinstance(value, datetime):
            return utc_stamp(value if value.tzinfo else value.replace(tzinfo=UTC))
        text = str(value).strip()
        if not text or text.lower() in _PLACEHOLDERS:
            return ""
        text = text.replace(" ", "T")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?", text):
            return text.split(".")[0] + "Z"
        if text.endswith("+00:00"):
            return text[: -len("+00:00")].split(".")[0] + "Z"
        offset = _OFFSET_TIME.fullmatch(text)
        if offset:  # a local time with its UTC offset: convert, do not pass it off as UTC
            sign = -1 if offset["sign"] == "-" else 1
            delta = timedelta(hours=int(offset["oh"]), minutes=int(offset["om"]))
            local = datetime.strptime(offset["stamp"], "%Y-%m-%dT%H:%M:%S")
            return utc_stamp((local - sign * delta).replace(tzinfo=UTC))
        return text
    except Exception:
        return ""


def clean_text(value: Any) -> str:
    """``None`` / ``"None"`` / ``"nan"`` become ``""``; anything else is returned as text."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = str(value).strip()
    return "" if text.lower() in {"none", "nan", "null"} else text


_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s'\"<>]+")
_URL_PARTS = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://)(?:[^/@?#]*@)?([^?#]*)")


def _redact_url(match: re.Match[str]) -> str:
    """A URL as it may be written into a file: no credentials, query or fragment (tokens)."""
    url = match.group(0)
    tail = re.search(r"[).,;:\]]+$", url)
    trailing = tail.group(0) if tail else ""
    parts = _URL_PARTS.match(url[: len(url) - len(trailing)])
    if parts is None:
        return url
    scheme, rest = parts.groups()
    if scheme.lower() == "file://":  # a local file: the path rules below
        return redact_path(rest) + trailing
    return scheme + rest + trailing


def redact_path(text: Any) -> str:
    """*text* without absolute paths: a path becomes its file name, the home folder becomes ``~``.

    Used for everything that is written into a file's metadata, so a figure or dataset passed
    on to a colleague does not reveal the user name or folder layout of the machine that made it.
    A URL (``s3://bucket/key``, ``https://host/file``) is kept, without credentials, query
    and fragment.
    """
    s = str(text)
    if _URL.search(s):
        out, pos = [], 0
        for m in _URL.finditer(s):
            out.append(_redact_local_paths(s[pos : m.start()]))
            out.append(_redact_url(m))
            pos = m.end()
        out.append(_redact_local_paths(s[pos:]))
        return "".join(out)
    return _redact_local_paths(s)


def _redact_local_paths(s: str) -> str:
    """The file-path part of :func:`redact_path` (no URLs in *s*)."""
    # a lone absolute path (POSIX or Windows) -> its file name
    if re.fullmatch(r"(/|[A-Za-z]:[\\/]|~[\\/]).*", s) and "\n" not in s and " " not in s:
        return re.split(r"[\\/]", s.rstrip("\\/"))[-1]
    # absolute paths embedded in a longer sentence
    s = re.sub(r"(?<![\w.~])(?:[A-Za-z]:\\|/)(?:[^\s'\";,)]*[\\/])+([^\s'\";,)\\/]+)", r"\1", s)
    s = re.sub(r"~[\\/](?:[^\s'\";,)]*[\\/])*([^\s'\";,)\\/]+)", r"\1", s)
    home = str(Path.home())
    if home and home != "/" and home in s:  # e.g. the bare home folder
        s = s.replace(home, "~")
    return s


def source_names(paths: Iterable[Any] | Any | None) -> list[str]:
    """File names (never folders) of one or several source files, in order, without duplicates."""
    if paths is None:
        return []
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    out: list[str] = []
    for p in paths:
        text = clean_text(p)
        if not text:
            continue
        name = re.split(r"[\\/]", text.rstrip("\\/"))[-1]
        if name and name not in out:
            out.append(name)
    return out


def _format_value(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_format_value(x) for x in v) + "]"
    if isinstance(v, Mapping):
        return "{" + ", ".join(f"{k}={_format_value(x)}" for k, x in v.items()) + "}"
    return redact_path(v)


def format_params(params: Mapping[str, Any] | None, *, limit: int = 600) -> str:
    """``key=value, key=value`` (stable order, paths redacted, truncated to *limit* characters)."""
    if not params:
        return ""
    text = ", ".join(f"{k}={_format_value(v)}" for k, v in params.items() if v is not None)
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ---------------------------------------------------------------------------
# history / global attributes
# ---------------------------------------------------------------------------


def history_line(
    action: str, params: Mapping[str, Any] | None = None, *, when: str | None = None
) -> str:
    """One CF-style history entry: ``"<UTC time>: <software>: <action> (<parameters>)"``."""
    line = f"{when or utc_stamp()}: {software_string()}: {action}"
    detail = format_params(params)
    return f"{line} ({detail})" if detail else line


def append_history(existing: Any, line: str) -> str:
    """Append *line* to a history attribute (newline separated, CF style); ``"None"`` is empty."""
    prior = clean_text(existing)
    prior = re.sub(r"^None:\s*", "", prior)  # xradar writes "None: xradar v... export" on empty
    return f"{prior}\n{line}" if prior else line


def numeric_precision_note() -> str | None:
    """Words for the record when the opt-in float32 decoding is on, else None."""
    flag = os.environ.get("XRADAR_FLOAT32", "").strip().lower()
    on = flag in ("1", "true", "yes", "on")
    return (
        "integer-encoded moments were decoded to float32 (XRADAR_FLOAT32=1): values differ from "
        "the default float64 decoding by at most 6e-8 of their size"
        if on
        else None
    )


def standard_global_attrs(
    *,
    title: str,
    source_files: Iterable[Any] | Any | None = None,
    action: str = "export",
    params: Mapping[str, Any] | None = None,
    institution: Any = "",
    references: Any = "",
    comment: Any = "",
    summary: str = "",
    source: str = "",
    conventions: str | None = None,
    history: Any = "",
    extra: Mapping[str, Any] | None = None,
    layer: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The global attributes every NetCDF/Zarr written by the engine carries (all plain strings).

    ``title``, ``institution``, ``source``, ``history``, ``references`` and ``comment`` are the CF
    (section 2.6.2) / CfRadial ones; ``date_created`` is ACDD; ``software`` / ``libraries`` /
    ``provenance_schema`` identify the writer; ``source_files`` lists the input file *names*
    (never folders). ``history`` is the incoming history with a new line appended. *layer* (a
    layer-provenance
    dict, see :mod:`engine.layer_provenance`) adds the ``data_class`` / ``data_source`` /
    ``data_valid_time`` ... attributes that say what kind of numbers the file holds.
    """
    names = source_names(source_files)
    attrs: dict[str, Any] = {}
    if conventions:
        attrs["Conventions"] = conventions
    attrs["title"] = clean_text(title)
    if summary:
        attrs["summary"] = summary
    attrs["institution"] = clean_text(institution)
    attrs["source"] = clean_text(source) or (
        f"derived from {', '.join(names)}" if names else "radar observations"
    )
    attrs["history"] = append_history(history, history_line(action, params))
    attrs["references"] = clean_text(references)
    attrs["comment"] = clean_text(comment)
    attrs["date_created"] = utc_stamp()
    attrs["software"] = software_string()
    attrs["libraries"] = libraries_string()
    attrs["provenance_schema"] = PROVENANCE_SCHEMA
    precision = numeric_precision_note()
    if precision:
        attrs["numeric_precision"] = precision
    if names:
        attrs["source_files"] = ", ".join(names)
    if layer:
        from engine import layer_provenance

        attrs.update(layer_provenance.attrs(layer))
    if extra:
        for k, v in extra.items():
            if v is not None:
                attrs[k] = v
    return attrs


# ---------------------------------------------------------------------------
# CSV / figures / JSON
# ---------------------------------------------------------------------------


def csv_comment_block(
    *,
    title: str,
    columns: Mapping[str, str] | None = None,
    source_files: Iterable[Any] | Any | None = None,
    fill: str = "",
    params: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
    layer: Mapping[str, Any] | None = None,
) -> str:
    """A ``# key: value`` header for a CSV (pandas: ``comment="#"``; R: ``comment.char="#"``).

    *columns* maps each column name to its unit (``""`` = dimensionless); *fill* documents how a
    missing value is written.
    """
    lines = [f"# title: {clean_text(title)}"]
    lines.append(f"# software: {software_string()} ({libraries_string()})")
    lines.append(f"# created: {utc_stamp()}")
    names = source_names(source_files)
    if names:
        lines.append(f"# source_files: {', '.join(names)}")
    if params:
        lines.append(f"# parameters: {format_params(params)}")
    if layer:
        from engine import layer_provenance

        for k, v in layer_provenance.attrs(layer).items():
            lines.append(f"# {k}: {redact_path(v)}")
    if extra:
        for k, v in extra.items():
            if v is not None and clean_text(v):
                lines.append(f"# {k}: {redact_path(v)}")
    if columns:
        lines.append(
            "# columns: " + "; ".join(f"{k} [{u}]" if u else f"{k} [1]" for k, u in columns.items())
        )
    lines.append(f"# missing_values: {fill or 'empty field'}")
    lines.append("# time_standard: UTC (ISO 8601)")
    return "\n".join(lines) + "\n"


def figure_metadata(
    *,
    title: str = "",
    description: str = "",
    source_files: Iterable[Any] | Any | None = None,
    variable: str = "",
    sweep: int | None = None,
    data_time: str = "",
    params: Mapping[str, Any] | None = None,
    layer: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Key/value text embedded in a PNG (tEXt), PDF (Info) or SVG (``<metadata>``) figure.

    Keys follow the PNG specification's predefined keywords (Title, Author, Description, Software,
    Creation Time, Source, Comment) so viewers show them. No absolute paths, no user name.
    """
    meta: dict[str, str] = {}
    if title:
        meta["Title"] = redact_path(title)
    meta["Software"] = software_string()
    meta["Creation Time"] = utc_stamp()
    names = source_names(source_files)
    if names:
        meta["Source"] = ", ".join(names)
    desc = description or " ".join(
        x
        for x in (
            f"variable {variable}" if variable else "",
            f"sweep {sweep}" if sweep is not None else "",
            f"data time {data_time}" if data_time else "",
        )
        if x
    )
    if desc:
        meta["Description"] = desc
    comment = [f"libraries: {libraries_string()}"]
    if layer:
        comment.append(f"data class: {layer.get('kind', '')} ({layer.get('source', '')})")
    if params:
        comment.append(f"parameters: {format_params(params)}")
    meta["Comment"] = "; ".join(comment)
    return meta


def json_provenance(
    *, action: str, params: Mapping[str, Any] | None = None, source_files: Any = None
) -> dict[str, Any]:
    """A ``created_with`` object for JSON documents (recipes, sidecar files)."""
    out: dict[str, Any] = {
        "software": SOFTWARE_NAME,
        "software_version": software_version(),
        "libraries": library_versions(),
        "created": utc_stamp(),
        "action": action,
    }
    precision = numeric_precision_note()
    if precision:
        out["numeric_precision"] = precision
    if params:
        out["parameters"] = {k: v for k, v in params.items() if v is not None}
    names = source_names(source_files)
    if names:
        out["source_files"] = names
    return out
