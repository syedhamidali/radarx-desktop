"""Provenance of every layer shown in the app: *what kind of number is this, and where from?*

A layer is anything drawn or tabulated: a radar moment, a derived field, a mosaic, a wind
analysis, storm cells, a cross-section, a sounding, a model or reanalysis field. Each gets a small
``provenance`` dict (JSON-able, ``LayerProvenance.to_dict()``) that travels with it to the UI and
into export metadata. The **class** says how far the numbers are from a measurement:

=============  ===============================================================================
``observed``   a direct measurement by an instrument (radar moments, a radiosonde, a gauge)
``model``      output of a numerical weather prediction or reanalysis (HRRR, ERA5)
``retrieved``  derived from observations by an algorithm or a physical model (hydrometeor class,
               rain rate, multi-Doppler winds, storm cells, a gridded composite)
``simulated``  produced by a forward model or a synthetic generator (test data, simulated radar)
``stylised``   drawn for looks: shading, clouds, exaggeration; not a data value
=============  ===============================================================================

Nothing here invents information: a field that is not known stays empty and the UI shows
"unknown", never a guess.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from engine import provenance as _prov

CLASSES: tuple[str, ...] = ("observed", "model", "retrieved", "simulated", "stylised")

#: One-sentence definition per class (shown in the UI legend and in docs/SCIENCE.md).
DEFINITIONS: dict[str, str] = {
    "observed": "Measured by an instrument; the values are what the instrument reported "
    "(after the quality control named in the method).",
    "model": "Output of a numerical weather prediction or reanalysis model; it is an estimate "
    "of the atmosphere on a grid, not a measurement.",
    "retrieved": "Derived from observations by an algorithm or physical model; carries the "
    "algorithm's assumptions and errors on top of the measurement's.",
    "simulated": "Produced by a forward model or synthetic generator; not a real observation.",
    "stylised": "Drawn for appearance (shading, clouds, exaggeration); not a data value.",
}

#: Short chip text.
SHORT: dict[str, str] = {
    "observed": "OBS",
    "model": "MODEL",
    "retrieved": "RETR",
    "simulated": "SIM",
    "stylised": "STYLE",
}

SCHEMA = 1


def _clean(value: Any) -> str:
    return _prov.clean_text(value)


@dataclass
class LayerProvenance:
    """Provenance of one layer. Only ``kind`` and ``source`` are mandatory."""

    kind: str
    source: str
    title: str = ""
    run_time: str = ""  # when the model was initialised / the algorithm was run (UTC ISO)
    valid_time: str = ""  # the time the numbers represent (UTC ISO); start of a window
    valid_end: str = ""  # end of the window, when the layer covers an interval
    resolution: str = ""
    method: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    uncertainty: str = ""
    references: list[str] = field(default_factory=list)
    attribution: str = ""
    licence: str = ""
    inputs: list[str] = field(default_factory=list)  # names of layers this one is made from

    def __post_init__(self) -> None:
        if self.kind not in CLASSES:
            raise ValueError(f"provenance kind must be one of {CLASSES}, not {self.kind!r}")
        self.source = _clean(self.source) or "unknown"
        for name in ("run_time", "valid_time", "valid_end"):
            setattr(self, name, _prov.iso_utc(getattr(self, name)))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"schema": SCHEMA, "kind": self.kind, "source": self.source}
        for name in (
            "title",
            "run_time",
            "valid_time",
            "valid_end",
            "resolution",
            "method",
            "uncertainty",
            "attribution",
            "licence",
        ):
            text = _clean(getattr(self, name))
            if text:
                out[name] = text
        params = {k: _json_safe(v) for k, v in self.parameters.items()}
        params = {k: v for k, v in params.items() if v is not None}
        if params:
            out["parameters"] = params
        if self.references:
            out["references"] = [_clean(r) for r in self.references if _clean(r)]
        if self.inputs:
            out["inputs"] = [_clean(i) for i in self.inputs if _clean(i)]
        return out

    def time_offset_s(self, reference: Any) -> float | None:
        """Seconds from *reference* (e.g. the active radar scan) to this layer's valid time.

        Positive = the layer is valid *after* the radar scan. ``None`` when either time is
        unknown (the UI then says so instead of showing 0).
        """
        return time_offset_s(self.valid_time, reference)


def _json_safe(v: Any) -> Any:
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, Mapping):
        return {str(k): _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    if isinstance(v, (str, int, bool)) or v is None:
        return _prov.redact_path(v) if isinstance(v, str) else v
    try:
        return float(v) if math.isfinite(float(v)) else None
    except (TypeError, ValueError):
        return _prov.redact_path(str(v))


def _parse(value: Any) -> datetime | None:
    text = _prov.iso_utc(value)
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def time_offset_s(valid: Any, reference: Any) -> float | None:
    a, b = _parse(valid), _parse(reference)
    if a is None or b is None:
        return None
    return (a - b).total_seconds()


def format_offset(seconds: float | None) -> str:
    """``"+12 min"``, ``"-1 h 05 min"``, ``"same time"`` or ``"unknown"``."""
    if seconds is None:
        return "unknown"
    sign = "+" if seconds > 0 else "-" if seconds < 0 else ""
    s = abs(round(seconds))
    if s < 30:
        return "same time"
    if s < 3600:
        return f"{sign}{round(s / 60)} min"
    h, m = divmod(round(s / 60), 60)
    if h < 48:
        return f"{sign}{h} h {m:02d} min" if m else f"{sign}{h} h"
    return f"{sign}{round(s / 86400, 1):g} d"


def from_dict(d: Mapping[str, Any]) -> LayerProvenance:
    """Rebuild from ``to_dict()`` output (unknown keys are ignored)."""
    known = {k: d[k] for k in LayerProvenance.__dataclass_fields__ if k in d}
    known.setdefault("source", "unknown")
    return LayerProvenance(**known)


# ---------------------------------------------------------------------------
# Builders for the layers the app makes itself
# ---------------------------------------------------------------------------


def observed_radar(
    *,
    radar: str = "",
    scan_time: Any = "",
    fmt: str = "",
    title: str = "",
    gate_spacing_m: float | None = None,
    n_sweeps: int | None = None,
    nyquist: str = "",
    references: Iterable[str] = (),
    simulated: bool = False,
) -> LayerProvenance:
    """The radar moments of an opened file (the measured data; ``simulated`` for synthetic ones)."""
    res = []
    if gate_spacing_m and math.isfinite(gate_spacing_m):
        res.append(f"gate spacing {gate_spacing_m:g} m")
    if n_sweeps:
        res.append(f"{n_sweeps} sweeps")
    return LayerProvenance(
        kind="simulated" if simulated else "observed",
        source=_clean(radar) or "radar file",
        title=title or "Radar moments",
        valid_time=scan_time,
        resolution=", ".join(res),
        method="Radar moments as stored in the file"
        + (f" ({fmt})" if _clean(fmt) else "")
        + "; no processing applied by radarx-desktop.",
        uncertainty="Measurement uncertainty depends on the radar's calibration, beam "
        "geometry, attenuation and clutter; none of these is corrected unless a QC step "
        "says so." + (f" {nyquist}" if nyquist else ""),
    )


def derived_field(
    *,
    name: str,
    module: str,
    params: Mapping[str, Any] | None = None,
    valid_time: Any = "",
    inputs: Iterable[str] = (),
    description: str = "",
    references: Iterable[str] = (),
    uncertainty: str = "",
) -> LayerProvenance:
    """A field computed by an analysis module / pipeline step from the open radar data."""
    return LayerProvenance(
        kind="retrieved",
        source=f"radarx-desktop module {module}"
        + (
            f" (radarx {_prov.radarx_version()})"
            if module.startswith("radarx") and _prov.radarx_version()
            else ""
        ),
        title=name,
        run_time=_prov.utc_stamp(),
        valid_time=valid_time,
        method=description or f"Computed by the '{module}' module from the radar moments.",
        parameters=dict(params or {}),
        uncertainty=uncertainty
        or "Inherits the measurement errors of its inputs plus the algorithm's assumptions; "
        "see the references for its validation.",
        references=list(references),
        inputs=list(inputs),
    )


def for_variable(
    variable: str, *, radar: str = "", scan_time: Any = "", title: str = ""
) -> LayerProvenance:
    """Provenance of a variable of an opened file: a derived variable is named ``module:field``."""
    if ":" in variable:
        module = variable.split(":", 1)[0]
        return derived_field(name=title or variable, module=module, valid_time=scan_time)
    return observed_radar(radar=radar, scan_time=scan_time, title=title or variable)


def mosaic(
    *, radars: Iterable[str], valid_time: Any, method: str, params: Mapping[str, Any] | None = None
) -> LayerProvenance:
    names = [r for r in (_clean(x) for x in radars) if r]
    return LayerProvenance(
        kind="retrieved",
        source=f"Mosaic of {len(names)} radar(s)" + (f": {', '.join(names[:6])}" if names else ""),
        title="Radar mosaic",
        run_time=_prov.utc_stamp(),
        valid_time=valid_time,
        method=method,
        parameters=dict(params or {}),
        uncertainty="Gridded composite: values between radars are interpolated or "
        "merged; volumes are not observed at the same instant.",
        inputs=names,
    )


def winds(
    *, radars: Iterable[str], valid_time: Any, method: str, params: Mapping[str, Any] | None = None
) -> LayerProvenance:
    names = [r for r in (_clean(x) for x in radars) if r]
    return LayerProvenance(
        kind="retrieved",
        source="Multi-Doppler wind analysis" + (f" ({', '.join(names[:6])})" if names else ""),
        title="Multi-Doppler winds",
        run_time=_prov.utc_stamp(),
        valid_time=valid_time,
        method=method,
        parameters=dict(params or {}),
        uncertainty="Retrieved wind: depends on beam-crossing geometry and the assumed "
        "fall speed / mass-continuity constraint; poorly constrained near the baseline "
        "and at the lowest and highest levels.",
        inputs=names,
    )


def storm_cells(*, valid_time: Any, params: Mapping[str, Any] | None = None) -> LayerProvenance:
    return LayerProvenance(
        kind="retrieved",
        source="radarx-desktop storm-cell identification",
        title="Storm cells",
        run_time=_prov.utc_stamp(),
        valid_time=valid_time,
        method="Connected regions above a reflectivity threshold on one sweep.",
        parameters=dict(params or {}),
        uncertainty="Outlines depend on the threshold and minimum size; they are objects "
        "derived from the field, not measured quantities.",
    )


def cross_section(*, valid_time: Any, variable: str = "", source: str = "") -> LayerProvenance:
    return LayerProvenance(
        kind="observed",
        source=_clean(source) or "radar volume",
        title="Cross-section" + (f" of {variable}" if variable else ""),
        valid_time=valid_time,
        method="Vertical slice sampled from the volume's sweeps along a line; values between "
        "sweeps are interpolated.",
        uncertainty="Sweeps are taken minutes apart and the beam widens with range; "
        "gaps above and below the sweeps are not filled with real data.",
    )


def volume_3d(*, valid_time: Any, source: str = "") -> LayerProvenance:
    return LayerProvenance(
        kind="observed",
        source=_clean(source) or "radar volume",
        title="3D volume",
        valid_time=valid_time,
        method="Gates of all sweeps placed in space with standard beam propagation "
        "(4/3 effective Earth radius).",
        uncertainty="Between-sweep volume is not measured; the cone of silence above the "
        "highest sweep is empty.",
    )


def sounding(
    *,
    station: str,
    launch_time: Any,
    source: str,
    method: str = "",
    attribution: str = "",
    licence: str = "",
    references: Iterable[str] = (),
) -> LayerProvenance:
    return LayerProvenance(
        kind="observed",
        source=_clean(source) or "radiosonde",
        title=f"Sounding {station}".strip(),
        valid_time=launch_time,
        method=method
        or "Radiosonde profile: pressure, temperature, dew point and wind versus height "
        "(geopotential heights converted to geometric).",
        uncertainty="The balloon rises at roughly 5 m/s (about 30 to 35 minutes to 10 km) and "
        "drifts with the wind: the profile is neither at one place nor at one time.",
        attribution=attribution,
        licence=licence,
        references=list(references),
    )


def for_provider(
    *,
    kind: str,
    source: str,
    attribution: str,
    licence: str,
    request: Mapping[str, Any],
    method: str = "",
    run_time: Any = "",
    valid_time: Any = "",
) -> LayerProvenance:
    """Provenance of data fetched through the environment framework (a provider's default)."""
    params = {k: v for k, v in request.items() if k in ("product", "bbox", "variables", "params")}
    return LayerProvenance(
        kind=kind,
        source=source,
        run_time=run_time,
        valid_time=valid_time or request.get("time_start") or "",
        valid_end=request.get("time_end") or "",
        method=method or f"Fetched from {source} and subset as requested.",
        parameters=params,
        attribution=attribution,
        licence=licence,
    )


# ---------------------------------------------------------------------------
# Helpers for the sidecar (read an opened file, wrap a derived-variable record)
# ---------------------------------------------------------------------------


def reader_context(reader: Any) -> dict[str, str]:
    """``{"source", "scan_time"}`` of an opened file, as remembered by :func:`describe_reader`."""
    ctx = getattr(reader, "_layer_ctx", None)
    return dict(ctx) if isinstance(ctx, dict) else {"source": "radar file", "scan_time": ""}


def describe_reader(reader: Any, schema: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Provenance dict of the radar moments of the file in *reader* (also cached on it).

    Reads only the time and range coordinates of the first sweep. Never raises: whatever cannot
    be read stays empty (the UI then shows "unknown").
    """
    schema = schema or {}
    attrs = schema.get("attributes") or {}
    radar = ""
    for key in ("instrument_name", "station", "site_name"):
        radar = _clean(schema.get(key) or attrs.get(key))
        if radar:
            break
    scan_time = _clean(attrs.get("scan_time"))
    spacing = None
    try:
        import numpy as np

        ds = reader.get_sweep_dataset(0, mask=False)
        if not scan_time and "time" in ds.variables:
            t = np.asarray(ds["time"].values).ravel()
            if t.size:
                scan_time = _prov.iso_utc(t.min())
        if "range" in ds.variables and ds["range"].size > 2:
            rng = np.asarray(ds["range"].values, dtype=float)
            spacing = float(np.nanmedian(np.diff(rng)))
    except Exception:
        pass
    n_sweeps = len(schema.get("sweeps") or [])
    prov = observed_radar(
        radar=radar or "radar file",
        scan_time=scan_time,
        fmt=_clean(schema.get("format")),
        gate_spacing_m=spacing,
        n_sweeps=n_sweeps or None,
    ).to_dict()
    with contextlib.suppress(Exception):
        reader._layer_ctx = {"source": prov["source"], "scan_time": prov.get("valid_time", "")}
        reader._layer_prov = prov
    return prov


def _radar_names(radars: Any) -> list[str]:
    names: list[str] = []
    for r in radars or []:
        name = r.get("name") if isinstance(r, Mapping) else r
        if _clean(name):
            names.append(_clean(name))
    return names


def for_mosaic_header(h: Mapping[str, Any]) -> dict[str, Any]:
    """Provenance of a ``mosaic_result`` header (radars, analysis time, merge settings)."""
    merge = h.get("merge") if isinstance(h.get("merge"), Mapping) else {}
    level = f" at {h['height_m']:g} m" if isinstance(h.get("height_m"), (int, float)) else ""
    method = (
        f"{_clean(h.get('product')) or 'Composite'} of {_clean(h.get('variable'))}{level}: "
        "the volumes of every radar are cone-gridded onto one grid and merged with "
        "range-dependent weights (radarx.grid)."
    )
    return mosaic(
        radars=_radar_names(h.get("radars")),
        valid_time=h.get("analysis_time") or "",
        method=method,
        params={"merge": dict(merge), "threshold_dbz": h.get("threshold_dbz")},
    ).to_dict()


def for_winds_header(h: Mapping[str, Any]) -> dict[str, Any]:
    """Provenance of a ``multidoppler_result`` / ``wind_volume`` header."""
    opts = h.get("options") if isinstance(h.get("options"), Mapping) else {}
    bg = h.get("background")
    return winds(
        radars=_radar_names(h.get("radars")),
        valid_time=h.get("analysis_time") or "",
        method=f"Multi-Doppler wind synthesis, method '{_clean(h.get('method')) or 'unknown'}' "
        "(radarx): u, v and w from the radial velocities of two or more radars."
        + (" A background field constrains the retrieval." if bg else ""),
        params={
            "min_crossing_angle": h.get("min_crossing_angle"),
            "fall_speed_correction": h.get("fall_speed_correction"),
            **{k: v for k, v in opts.items() if k != "storm_motion"},
        },
    ).to_dict()


def from_derived_info(info: Mapping[str, Any], valid_time: Any = "") -> LayerProvenance:
    """Provenance of one derived variable from its registry record (module, params, ...)."""
    module = _clean(info.get("module")) or "unknown module"
    title = _clean(info.get("module_title"))
    stamp = info.get("computed_at")
    run_time = ""
    if isinstance(stamp, (int, float)):
        run_time = _prov.utc_stamp(datetime.fromtimestamp(stamp, timezone.utc))
    prov = derived_field(
        name=_clean(info.get("label")) or _clean(info.get("name")) or module,
        module=module,
        params=info.get("params") if isinstance(info.get("params"), Mapping) else None,
        valid_time=valid_time,
        description=f"Computed by the '{title or module}' module from the radar moments of "
        "the open file.",
    )
    if run_time:
        prov.run_time = run_time
    return prov


# ---------------------------------------------------------------------------
# Export metadata
# ---------------------------------------------------------------------------


def attrs(prov: Mapping[str, Any] | LayerProvenance | None) -> dict[str, str]:
    """Flat ``data_*`` strings for NetCDF/Zarr global attributes, CSV headers and figures."""
    if prov is None:
        return {}
    d = prov.to_dict() if isinstance(prov, LayerProvenance) else dict(prov)
    out = {"data_class": str(d.get("kind", "")), "data_source": str(d.get("source", ""))}
    for src, dst in (
        ("valid_time", "data_valid_time"),
        ("run_time", "data_run_time"),
        ("resolution", "data_resolution"),
        ("method", "data_method"),
        ("uncertainty", "data_uncertainty"),
        ("attribution", "data_attribution"),
        ("licence", "data_licence"),
    ):
        if d.get(src):
            out[dst] = str(d[src])
    if d.get("parameters"):
        out["data_parameters"] = _prov.format_params(d["parameters"], limit=1500)
    if d.get("references"):
        out["data_references"] = "; ".join(map(str, d["references"]))
    if d.get("inputs"):
        out["data_inputs"] = ", ".join(map(str, d["inputs"]))
    if out["data_class"] in DEFINITIONS:
        out["data_class_definition"] = DEFINITIONS[out["data_class"]]
    return out
