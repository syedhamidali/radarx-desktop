"""The run record of a processing session, and the methods text written from it.

A *run record* is a small JSON document that says what was done to which data, with which
software and which settings, precisely enough to repeat it and to cite it (docs/SCIENCE.md
2.6)::

    {"schema": "radarx-run-record/1", "created": "...Z", "software": "radarx-desktop",
     "software_version": "0.2.x", "libraries": {"xradar": "0.13.0", ...},
     "inputs": [{"name": "KTLX20130520_201643_V06"}],
     "steps": [{"kind": "module", "id": "radarx_dealias", "title": "...", "version": "1.0",
                "sweep": 0, "input": "KTLX...", "params": {...}, "defaults": {...},
                "data_class": "RETRIEVED", "references": [...], "time": "...Z"}, ...]}

Every parameter is in the step *including the defaults that were applied* (``defaults`` holds
what the default would have been, so the text can say which values the user changed).
Nothing in a record is a path: only file *names* (``provenance.source_names``).

:func:`methods_paragraphs` / :func:`methods_text` turn a record into plain-language methods: one
paragraph per step with every parameter and the software version, preceded by one paragraph
about the software, followed by the references. :class:`RunLog` collects the steps of a running
session (the engine keeps one per connection); the ``rxd methods`` command prints the text of a
saved record.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from engine import provenance

SCHEMA = "radarx-run-record/1"

#: The kinds of step a record knows, with the verb the methods text uses.
KINDS = {
    "module": "was applied",
    "pipeline": "was applied",
    "export": "was written",
    "mosaic": "was computed",
    "winds": "was computed",
    "other": "was done",
}

_CLASS_WORDS = {
    "OBSERVED": "an observation",
    "MODEL-ANALYSIS": "a model analysis",
    "RETRIEVED": "a retrieved (derived) quantity",
    "SIMULATED-STYLISED": "a simulated or stylised product",
}


def _jsonable(value: Any) -> Any:
    """Plain JSON values (numpy scalars and arrays, paths, sets become their plain forms)."""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return provenance.source_names([value])[0] if str(value) else ""
    if hasattr(value, "tolist"):
        return _jsonable(value.tolist())
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def make_step(
    kind: str,
    step_id: str,
    title: str,
    params: Mapping[str, Any] | None = None,
    *,
    defaults: Mapping[str, Any] | None = None,
    version: str | None = None,
    sweep: int | None = None,
    input_name: str | None = None,
    output_name: str | None = None,
    data_class: str | None = None,
    description: str = "",
    references: Iterable[str] = (),
    elapsed_ms: float | None = None,
    when: str | None = None,
    uses: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    """One processing step of a record (JSON-safe)."""
    step: dict[str, Any] = {
        "kind": kind if kind in KINDS else "other",
        "id": str(step_id),
        "title": str(title or step_id),
        "params": _jsonable(dict(params or {})),
        "time": when or provenance.utc_stamp(),
    }
    if defaults:
        step["defaults"] = _jsonable(dict(defaults))
    if version:
        step["version"] = str(version)
    if sweep is not None:
        step["sweep"] = int(sweep)
    if input_name:
        step["input"] = provenance.source_names([input_name])[0]
    if output_name:
        step["output"] = provenance.source_names([output_name])[0]
    if data_class:
        step["data_class"] = str(data_class).upper()
    if description:
        step["description"] = str(description)
    refs = [str(r) for r in references if str(r).strip()]
    if refs:
        step["references"] = refs
    used = {str(k): str(v) for k, v in (uses or {}).items() if v}
    if used:
        step["uses"] = used  # algorithm libraries behind the step, e.g. {"radarx": "0.5.1"}
    if elapsed_ms is not None:
        step["elapsed_ms"] = round(float(elapsed_ms), 1)
    return step


def new_record(
    steps: Iterable[Mapping[str, Any]] = (), inputs: Iterable[Any] = ()
) -> dict[str, Any]:
    """A record of *steps* (as made by :func:`make_step`) on *inputs* (file names or paths)."""
    record: dict[str, Any] = {
        "schema": SCHEMA,
        "created": provenance.utc_stamp(),
        "software": provenance.SOFTWARE_NAME,
        "software_version": provenance.software_version(),
        "libraries": provenance.library_versions(),
        "inputs": [{"name": n} for n in provenance.source_names(list(inputs))],
        "steps": [dict(s) for s in steps],
    }
    # (the note exists once the opt-in float32 decoding is in: PR for issue #97)
    note = getattr(provenance, "numeric_precision_note", None)
    precision = note() if note else None
    if precision:
        record["numeric_precision"] = precision
    return record


class RunLog:
    """The steps of one session, appended as they finish (thread-safe, bounded)."""

    MAX_STEPS = 500

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._steps: list[dict[str, Any]] = []
        self._inputs: list[str] = []

    def add(self, step: Mapping[str, Any]) -> None:
        with self._lock:
            self._steps.append(dict(step))
            del self._steps[: -self.MAX_STEPS]
            name = step.get("input")
            if name and name not in self._inputs:
                self._inputs.append(str(name))

    def add_input(self, name: Any) -> None:
        for n in provenance.source_names([name]):
            with self._lock:
                if n not in self._inputs:
                    self._inputs.append(n)

    def clear(self) -> None:
        with self._lock:
            self._steps.clear()
            self._inputs.clear()

    def __len__(self) -> int:
        return len(self._steps)

    def record(self) -> dict[str, Any]:
        with self._lock:
            return new_record(self._steps, self._inputs)


# ---------------------------------------------------------------------------
# Reading and writing
# ---------------------------------------------------------------------------


class RunRecordError(ValueError):
    """A file is not a run record this version can read."""


def validate(record: Any) -> dict[str, Any]:
    """Check *record* is a run record; return it. Raises :class:`RunRecordError` in words."""
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        raise RunRecordError(
            f"Not a radarx run record (expected schema '{SCHEMA}'). Save one from the app's "
            "Export panel or with rxd ... --record FILE."
        )
    steps = record.get("steps")
    if not isinstance(steps, list) or not all(isinstance(s, dict) for s in steps):
        raise RunRecordError("The run record has no valid list of steps.")
    for s in steps:
        if not s.get("id") or not isinstance(s.get("params", {}), dict):
            raise RunRecordError(
                "A step of the run record has no id or its parameters are invalid."
            )
    return record


def write_json_atomic(path: str | os.PathLike[str], document: Mapping[str, Any]) -> str:
    """Write *document* as indented JSON next to *path* and move it into place (no half files)."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".radarx-", suffix=".json", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(document, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return str(target)


def save_record(record: Mapping[str, Any], path: str | os.PathLike[str]) -> str:
    return write_json_atomic(path, validate(dict(record)))


def load_record(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        return validate(json.loads(Path(path).expanduser().read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise RunRecordError(f"{Path(path).name} is not valid JSON ({exc.msg}).") from exc


# ---------------------------------------------------------------------------
# Methods text
# ---------------------------------------------------------------------------


def _value(v: Any) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {_value(x)}" for k, x in v.items()) + "}"
    if v is None:
        return "none"
    return str(v)


def _params_sentence(step: Mapping[str, Any]) -> str:
    params = step.get("params") or {}
    if not params:
        return "It has no adjustable parameters."
    defaults = step.get("defaults") or {}
    parts = []
    for name, value in params.items():
        note = " (the default)" if name in defaults and defaults[name] == value else ""
        parts.append(f"{name} = {_value(value)}{note}")
    return "Parameters: " + "; ".join(parts) + "."


def _lib_phrase(libraries: Mapping[str, str]) -> str:
    return ", ".join(f"{name} {ver}" for name, ver in libraries.items())


def step_paragraph(step: Mapping[str, Any], index: int, total: int) -> str:
    """The methods paragraph of one step: what, on which data, every parameter, the version."""
    title = step.get("title") or step.get("id")
    kind = step.get("kind", "other")
    verb = KINDS.get(kind, "was done")
    sid = step.get("id")
    what = f"{title}"
    if sid and str(sid) != str(title):
        what += f" ({'module' if kind == 'module' else 'step'} {sid}"
        what += f", version {step['version']})" if step.get("version") else ")"
    elif step.get("version"):
        what += f" (version {step['version']})"
    text = f"Step {index} of {total}. {what} {verb}"
    if step.get("sweep") is not None:
        text += f" to sweep {step['sweep']}"
    if step.get("input"):
        text += f" of {step['input']}"
    if step.get("output"):
        text += f", writing {step['output']}"
    text += f" on {step['time']}." if step.get("time") else "."
    text += " " + _params_sentence(step)
    if step.get("description"):
        desc = str(step["description"]).strip()
        text += " " + (desc if desc.endswith((".", "!", "?")) else desc + ".")
    uses = step.get("uses") or {}
    if uses:
        text += (
            " The algorithm comes from "
            + " and ".join(f"{name} {ver}" for name, ver in uses.items())
            + "."
        )
    cls = str(step.get("data_class", "")).upper()
    if cls in _CLASS_WORDS:
        text += f" The result is {_CLASS_WORDS[cls]} ({cls})."
    return text


def methods_paragraphs(record: Mapping[str, Any]) -> list[str]:
    """Plain-language methods: a software paragraph, one paragraph per step, the references."""
    validate(dict(record))
    steps = list(record["steps"])
    created = str(record.get("created", ""))
    inputs = [i.get("name", "") for i in record.get("inputs", []) if i.get("name")]
    libs = record.get("libraries") or {}
    head = (
        f"The radar data{' (' + ', '.join(inputs) + ')' if inputs else ''} were processed with "
        f"{record.get('software', 'radarx-desktop')} {record.get('software_version', '')}".rstrip()
    )
    if libs:
        head += f", which uses {_lib_phrase(libs)}"
    head += f". The record was made on {created}." if created else "."
    if record.get("numeric_precision"):
        head += " Note: " + str(record["numeric_precision"]).rstrip(".") + "."
    paragraphs = [head]
    if not steps:
        paragraphs.append("No processing step has been recorded yet.")
    for i, step in enumerate(steps, 1):
        paragraphs.append(step_paragraph(step, i, len(steps)))
    refs: list[str] = []
    for step in steps:
        for ref in step.get("references", []) or []:
            if ref not in refs:
                refs.append(ref)
    if refs:
        paragraphs.append("References:\n" + "\n".join(f"- {r}" for r in refs))
    return paragraphs


def methods_text(record: Mapping[str, Any]) -> str:
    return "\n\n".join(methods_paragraphs(record)) + "\n"
