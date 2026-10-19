"""WebSocket messages of the QPE panel (the server only forwards them here).

Protocol (all JSON; a request carries ``request_id`` that the reply echoes)::

    -> {type: "qpe_run", request_id, file_ids: [...], config: {method, relation, band, precip,
        freezing_level_m, z_cap, attenuation, ...}, options: {start, end, resolution_m, extent,
        max_range_km, composite, advect, step_s, max_gap_s, hold_edges_s},
        basins: {path, id_field?, crs?, thresholds?, frequency?}, gauges: {path, adjust?, units?,
        tz?, interval_s?, stations?, format?}, field?}
    <- progress ..., {type: "qpe_result", request_id, qpe_id, summary, fields, layer}
    <- {type: "qpe_error", request_id, message}

    -> {type: "qpe_info", request_id}      <- {type: "qpe_info_result", relations, methods, fields}

    -> {type: "qpe_layer", request_id, qpe_id, field, vmin?, vmax?}
    <- {type: "qpe_layer_result", request_id, qpe_id, layer}

    -> {type: "qpe_adjust", request_id, qpe_id, gauges: {...}}        (re-adjust, no recomputation)
    <- {type: "qpe_adjust_result", request_id, qpe_id, summary, validation, layer}

    -> {type: "qpe_series", request_id, qpe_id, basin: <index or id>, bin_minutes?}
    <- {type: "qpe_series_result", request_id, qpe_id, basin, rows, hyetograph, stats, provenance}

    -> {type: "qpe_export", request_id, qpe_id, format: "nc"|"png"|"json"|"csv", path, field?}
    <- {type: "file_saved", path}

``layer`` is what the map needs (see :func:`layer_payload`): the PNG as base64, its four corners,
the colour range and legend, and a ``mosaic_id`` under which the float grid is registered in the
mosaic cache so that the map probe and PNG export of the mosaic work unchanged.
"""

from __future__ import annotations

import base64
import threading
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

from engine.hydro import products
from engine.hydro import qpe as hqpe

MAX_RESULTS = 3


class QpeStore:
    """The last few results (their grids are tens of MB), newest kept."""

    def __init__(self, maxsize: int = MAX_RESULTS) -> None:
        self._items: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()
        self.maxsize = maxsize

    def put(self, result: Any) -> str:
        qid = uuid.uuid4().hex[:12]
        with self._lock:
            self._items[qid] = result
            while len(self._items) > self.maxsize:
                self._items.popitem(last=False)
        return qid

    def get(self, qid: Any) -> Any:
        with self._lock:
            if not isinstance(qid, str) or qid not in self._items:
                raise hqpe.QpeError("Unknown or expired QPE result: run the estimate again")
            self._items.move_to_end(qid)
            return self._items[qid]

    def discard(self, qid: str) -> None:
        with self._lock:
            self._items.pop(qid, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


STORE = QpeStore()


def _obj(msg: dict[str, Any], key: str) -> dict[str, Any]:
    val = msg.get(key)
    if val is None:
        return {}
    if not isinstance(val, dict):
        raise hqpe.QpeError(f"qpe: '{key}' must be an object")
    return val


def _path(spec: dict[str, Any], what: str) -> str | None:
    p = spec.get("path")
    if not p:
        return None
    if not isinstance(p, str):
        raise hqpe.QpeError(f"qpe: the {what} path must be text")
    return str(Path(p).expanduser())


def layer_payload(result: Any, field: str, vmin: Any = None, vmax: Any = None) -> dict[str, Any]:
    """The map layer of one field; registers its float grid in the mosaic cache for the probe."""
    from engine import mosaic

    if field not in products.FIELDS:
        raise hqpe.QpeError(
            f"qpe: unknown field {field!r}; choose one of {', '.join(products.FIELDS)}"
        )
    products.field_array(result, field)  # clear error when the result lacks it
    kw: dict[str, Any] = {}
    if vmin is not None:
        kw["vmin"] = float(vmin)
    if vmax is not None:
        kw["vmax"] = float(vmax)
    rec = products.mosaic_record(result, field, **kw)
    mosaic.MOSAICS.put(rec)
    head = rec.header()
    head.update(
        field=field,
        label=products.FIELDS[field][0],
        provenance_type="ADJUSTED" if field.endswith("adjusted") else "RETRIEVED",
        png_b64=base64.b64encode(rec.png).decode("ascii"),
        png_bytes=len(rec.png),
    )
    return head


def _fields_available(result: Any) -> list[dict[str, str]]:
    out = []
    for name, (label, units, _, _, _) in products.FIELDS.items():
        try:
            products.field_array(result, name)
        except hqpe.QpeError:
            continue
        out.append({"name": name, "label": label, "units": units})
    return out


def summary(result: Any, qid: str) -> dict[str, Any]:
    d = products._jsonable(result.to_dict())
    d["qpe_id"] = qid
    d["fields"] = _fields_available(result)
    d["default_field"] = products.default_field(result)
    return d


def run(
    msg: dict[str, Any], readers: list[Any], progress: Any, cancel: Any = None
) -> dict[str, Any]:
    """Execute ``qpe_run``; returns the ``qpe_result`` message (without ``type``/``request_id``)."""
    from engine.hydro import run as hydro_run

    config = _obj(msg, "config")
    options = _obj(msg, "options")
    basins = _obj(msg, "basins")
    gauges = _obj(msg, "gauges")
    cfg_opts, acc_opts = hydro_run.split_options({**config, **options})
    extent = acc_opts.get("extent")
    if extent is not None:
        if isinstance(extent, dict):
            extent = (extent["west"], extent["south"], extent["east"], extent["north"])
        acc_opts["extent"] = tuple(float(v) for v in extent)
    thresholds = basins.get("thresholds")
    gauge_options = {
        k: v
        for k, v in (
            ("fmt", gauges.get("format")),
            ("units", gauges.get("units")),
            ("tz", gauges.get("tz")),
            ("stations", gauges.get("stations")),
            ("interval_s", gauges.get("interval_s")),
            ("time_convention", gauges.get("time_convention")),
        )
        if v not in (None, "")
    }
    basin_options = {
        k: v for k, v in (("id_field", basins.get("id_field")), ("crs", basins.get("crs"))) if v
    }
    adjust_options = {}
    if gauges.get("min_amount") is not None:
        adjust_options["min_amount"] = float(gauges["min_amount"])
    if gauges.get("qc") is False:
        adjust_options["qc"] = False
    result = hydro_run.run_qpe(
        readers,
        config=cfg_opts,
        basins=_path(basins, "basin file"),
        basin_options=basin_options,
        thresholds=thresholds,
        frequency=basins.get("frequency") or None,
        gauges=_path(gauges, "gauge file"),
        gauge_options=gauge_options,
        adjust=gauges.get("adjust") or None,
        adjust_options=adjust_options,
        dem=_obj(msg, "terrain").get("path") or None,
        progress=progress,
        cancel=cancel,
        **acc_opts,
    )
    qid = STORE.put(result)
    field = msg.get("field") or products.default_field(result)
    if field not in products.FIELDS or (field == "accumulation" and result.duration_s == 0):
        field = "rate_last" if result.duration_s == 0 else "accumulation"
    return {
        "qpe_id": qid,
        "summary": summary(result, qid),
        "layer": layer_payload(result, field),
    }


def info(msg: dict[str, Any]) -> dict[str, Any]:
    """What the panel offers: Z-R relations (with equations and references), methods, fields."""
    return {
        "relations": [
            {
                "key": r.key,
                "label": r.label,
                "kind": r.kind,
                "equation": r.text(),
                "reference": r.reference,
                "note": r.note,
            }
            for r in hqpe.ZR_RELATIONS.values()
        ],
        "default_relation": {"rain": hqpe.DEFAULT_ZR, "snow": hqpe.DEFAULT_SNOW},
        "methods": list(hqpe.METHODS),
        "fields": [{"name": n, "label": f[0], "units": f[1]} for n, f in products.FIELDS.items()],
    }


def layer(msg: dict[str, Any]) -> dict[str, Any]:
    result = STORE.get(msg.get("qpe_id"))
    return {
        "qpe_id": msg["qpe_id"],
        "layer": layer_payload(
            result, str(msg.get("field") or "accumulation"), msg.get("vmin"), msg.get("vmax")
        ),
    }


def adjust(msg: dict[str, Any]) -> dict[str, Any]:
    """Re-adjust an existing result to a (new) gauge file without recomputing the accumulation."""
    from engine.hydro import gauges as hg

    result = STORE.get(msg.get("qpe_id"))
    spec = _obj(msg, "gauges")
    path = _path(spec, "gauge file")
    if not path:
        raise hqpe.QpeError("qpe_adjust: give the gauge file path")
    if result.duration_s <= 0:
        raise hqpe.QpeError("gauge adjustment needs an accumulation, not a single volume")
    gset = hg.read_gauges(
        path,
        **{
            k: v
            for k, v in (
                ("fmt", spec.get("format")),
                ("units", spec.get("units")),
                ("tz", spec.get("tz")),
                ("stations", spec.get("stations")),
                ("interval_s", spec.get("interval_s")),
                ("time_convention", spec.get("time_convention")),
            )
            if v not in (None, "")
        },
    )
    opts = {}
    if spec.get("min_amount") is not None:
        opts["min_amount"] = float(spec["min_amount"])
    if spec.get("qc") is False:
        opts["qc"] = False
    result.validation = hg.adjust(result, gset, method=spec.get("adjust") or "mfb", **opts)
    return {
        "qpe_id": msg["qpe_id"],
        "summary": summary(result, msg["qpe_id"]),
        "validation": products._jsonable(result.validation),
        "layer": layer_payload(result, "accumulation_adjusted"),
    }


def series(msg: dict[str, Any]) -> dict[str, Any]:
    result = STORE.get(msg.get("qpe_id"))
    bs = result.basin_series
    if bs is None:
        raise hqpe.QpeError("This result has no basins: give a basin file and run again")
    which = msg.get("basin", 0)
    ids = [w.basin.id for w in bs.weights]
    k = ids.index(which) if which in ids else int(which)
    if not 0 <= k < len(ids):
        raise hqpe.QpeError(f"basin {which!r} is not in this result ({', '.join(ids[:10])})")
    bins = float(msg.get("bin_minutes") or 60.0)
    return products._jsonable(
        {
            "qpe_id": msg["qpe_id"],
            "basin": ids[k],
            "basins": ids,
            "rows": bs.rows(k),
            "hyetograph": bs.hyetograph(k, bins),
            "stats": bs.stats[k],
            "exceedance": next((e for e in (result.exceedance or []) if e["id"] == ids[k]), None),
            "provenance_type": "ADJUSTED" if bs.adjusted else "RETRIEVED",
            "exact_adjustment": bs.exact_adjustment,
        }
    )


def export(msg: dict[str, Any]) -> Path:
    result = STORE.get(msg.get("qpe_id"))
    path = msg.get("path")
    if not isinstance(path, str) or not path:
        raise hqpe.QpeError("qpe_export: 'path' is required")
    target = Path(path).expanduser()
    if target.exists() and not msg.get("overwrite"):
        raise hqpe.QpeError(f"{target.name} already exists")
    written = result.save(target, field=msg.get("field"))
    return written[0]
