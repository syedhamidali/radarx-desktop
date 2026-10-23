"""The project file: what is open, how it is shown, and how it was processed. No data inside.

A project is a small JSON document (``.radarxproj``) so that a piece of work can be closed and
picked up again, handed to a colleague, or attached to a paper::

    {"schema": "radarx-project/1", "name": "Oklahoma 2013-05-20", "saved": "...Z",
     "created_with": {...software and library versions...},
     "files": [{"name": "KTLX...", "path": "/data/KTLX...", "relative": "../data/KTLX...",
                "size_bytes": 7032000, "modified": "...Z"}],
     "view": {...what the app was showing: active file, variable, sweep, zoom, mode...},
     "recipe": {...the pipeline recipe being edited...},
     "run_record": {...engine.runrecord...}}

Radar data are never copied into it: each file is recorded by its path **and** its path
relative to the project file, with size and modification time. On loading, a file is looked
for at the recorded path first, then at the relative path, then next to the project file; the
answer says for each file whether it was found, where, and whether it changed since the project
was saved (size or modification time), so a project never silently shows other data.

``view`` and ``recipe`` are kept as the app wrote them (the engine only checks that they are
plain JSON objects of a sane size), so newer apps can add to them.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from engine import provenance, runrecord

SCHEMA = "radarx-project/1"
EXTENSION = ".radarxproj"
MAX_FILES = 200
MAX_VIEW_BYTES = 256 * 1024


class ProjectError(ValueError):
    """A project file cannot be written or read; the message says what to do."""


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _relative(path: Path, base: Path) -> str | None:
    try:
        return os.path.relpath(path, base)
    except ValueError:  # another drive on Windows: there is no relative path
        return None


def describe_file(path: str | os.PathLike[str], project_dir: Path) -> dict[str, Any]:
    """The entry of one data file: name, absolute path, path relative to the project, size, time."""
    p = Path(path).expanduser()
    entry: dict[str, Any] = {"name": p.name, "path": str(p)}
    rel = _relative(p, project_dir)
    if rel is not None:
        entry["relative"] = rel.replace(os.sep, "/")
    try:
        st = p.stat()
        entry["size_bytes"] = int(st.st_size)
        entry["modified"] = _iso(st.st_mtime)
    except OSError:
        pass  # a file that is gone is still recorded by name and path
    return entry


def _plain_object(value: Any, what: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProjectError(f"The project's {what} must be an object.")
    plain = json.loads(json.dumps(value, default=str))  # JSON-safe copy
    if len(json.dumps(plain)) > MAX_VIEW_BYTES:
        raise ProjectError(f"The project's {what} is too large ({MAX_VIEW_BYTES // 1024} KB max).")
    return plain


def build_project(
    path: str | os.PathLike[str],
    *,
    name: str = "",
    files: list[str] | None = None,
    view: Mapping[str, Any] | None = None,
    recipe: Mapping[str, Any] | None = None,
    run_record: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The project document for the file that will be written at *path* (nothing is written)."""
    target = Path(path).expanduser()
    if len(files or []) > MAX_FILES:
        raise ProjectError(f"A project can list at most {MAX_FILES} files.")
    base = target.resolve().parent
    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "name": (name or target.stem)[:200],
        "saved": provenance.utc_stamp(),
        "created_with": {
            "software": provenance.SOFTWARE_NAME,
            "software_version": provenance.software_version(),
            "libraries": provenance.library_versions(),
        },
        "files": [describe_file(f, base) for f in files or []],
        "view": _plain_object(view, "view"),
        "recipe": _plain_object(recipe, "recipe") or None,
        "run_record": runrecord.validate(dict(run_record)) if run_record else None,
    }
    precision = provenance.numeric_precision_note()
    if precision:
        doc["numeric_precision"] = precision
    return doc


def save_project(path: str | os.PathLike[str], **parts: Any) -> str:
    """Write the project file atomically; returns the path written."""
    target = Path(path).expanduser()
    if not target.suffix:
        target = target.with_suffix(EXTENSION)
    return runrecord.write_json_atomic(target, build_project(target, **parts))


def read_project(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read and check a project file (no files are opened)."""
    p = Path(path).expanduser()
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProjectError(f"Project file not found: {p.name}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectError(f"{p.name} is not a radarx project (it is not valid JSON).") from exc
    if not isinstance(doc, dict) or not str(doc.get("schema", "")).startswith("radarx-project/"):
        raise ProjectError(f"{p.name} is not a radarx project file.")
    if doc["schema"] != SCHEMA:
        raise ProjectError(
            f"{p.name} was written by a newer version of radarx-desktop ({doc['schema']}); "
            "update the app to open it."
        )
    if not isinstance(doc.get("files", []), list):
        raise ProjectError(f"{p.name}: the list of files is damaged.")
    return doc


def resolve_files(doc: Mapping[str, Any], path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Where each recorded file is now: ``status`` is ``found``, ``changed`` or ``missing``.

    Looks at the recorded path, then the path relative to the project file, then the file name
    next to the project. ``changed`` means the size or modification time differs from when the
    project was saved: the file opens, but it may not be the data the work was done on.
    """
    base = Path(path).expanduser().resolve().parent
    out = []
    for entry in doc.get("files", []):
        if not isinstance(entry, Mapping) or not entry.get("name"):
            continue
        candidates = []
        if entry.get("path"):
            candidates.append(Path(str(entry["path"])).expanduser())
        if entry.get("relative"):
            candidates.append((base / str(entry["relative"])).resolve())
        candidates.append(base / str(entry["name"]))
        found = next((c for c in candidates if c.is_file() or c.is_dir()), None)
        result: dict[str, Any] = {"name": entry["name"], "status": "missing", "path": None}
        if found is not None:
            result["path"] = str(found)
            result["status"] = "found"
            try:
                st = found.stat()
                if (
                    entry.get("size_bytes") is not None
                    and found.is_file()
                    and int(entry["size_bytes"]) != st.st_size
                ) or (entry.get("modified") and _iso(st.st_mtime) != entry["modified"]):
                    result["status"] = "changed"
            except OSError:
                pass
        out.append(result)
    return out


def load_project(path: str | os.PathLike[str]) -> dict[str, Any]:
    """The project document plus ``resolved``: where each file is now (:func:`resolve_files`)."""
    doc = read_project(path)
    return {**doc, "resolved": resolve_files(doc, path)}


def summary(loaded: Mapping[str, Any]) -> str:
    """A few plain lines about a loaded project (``rxd project show``)."""
    lines = [f"Project: {loaded.get('name', '')}  (saved {loaded.get('saved', '?')})"]
    cw = loaded.get("created_with") or {}
    if cw:
        lines.append(f"Made with {cw.get('software', '')} {cw.get('software_version', '')}")
    for item in loaded.get("resolved", []):
        where = f" -> {item['path']}" if item.get("path") else ""
        lines.append(f"  {item['status']:8s} {item['name']}{where}")
    recipe = loaded.get("recipe")
    if recipe:
        lines.append(
            f"Recipe: {recipe.get('name', '(unnamed)')}, {len(recipe.get('steps', []))} steps"
        )
    rec = loaded.get("run_record")
    if rec:
        lines.append(f"Run record: {len(rec.get('steps', []))} processing steps")
    return "\n".join(lines)
