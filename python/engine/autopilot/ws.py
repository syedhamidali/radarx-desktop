"""WebSocket messages of the Autopilot panel (``python/server.py`` only forwards them here).

Protocol (JSON; a request carries ``request_id`` that the reply echoes)::

    -> {type: "auto_inspect", request_id, inputs: [paths...]}
    <- progress ..., {type: "auto_inspect_result", request_id, inventory}

    -> {type: "auto_plan", request_id, inventory?, inputs?, budget_gb?, out_dir?}
    <- {type: "auto_plan_result", request_id, inventory, plan}

    -> {type: "auto_run", request_id, inventory, plan, step_ids: [...], out_dir}
    <- progress ..., {type: "auto_run_result", request_id, record, gallery: [{name, path, png?}]}

    -> {type: "auto_cancel", request_id}      <- {type: "auto_cancelled", request_id}
    <- {type: "auto_error", request_id, kind, message}

Consent: ``auto_run`` runs only the ``step_ids`` the user ticked, into the ``out_dir`` they chose.
"""

from __future__ import annotations

import base64
import threading
from pathlib import Path
from typing import Any, Callable

from engine.autopilot import inspect_inputs, make_plan, run_plan
from engine.autopilot.inspect import AutopilotError

_CANCELS: dict[str, threading.Event] = {}
_LOCK = threading.Lock()
MAX_PNG_BYTES = 3 * 1024 * 1024


def cancel_event(request_id: Any) -> threading.Event:
    ev = threading.Event()
    if isinstance(request_id, str) and request_id:
        with _LOCK:
            _CANCELS[request_id] = ev
    return ev


def cancel(request_id: Any) -> bool:
    with _LOCK:
        ev = _CANCELS.get(request_id) if isinstance(request_id, str) else None
    if ev is None:
        return False
    ev.set()
    return True


def forget(request_id: Any) -> None:
    with _LOCK:
        _CANCELS.pop(request_id, None)


def _inputs(msg: dict[str, Any]) -> list[str]:
    inputs = msg.get("inputs")
    if not isinstance(inputs, list) or not inputs or not all(isinstance(p, str) and p for p in inputs):
        raise AutopilotError("'inputs' must be a non-empty list of paths")
    return inputs


def inspect(msg: dict[str, Any], progress: Callable[[int, str], None], cancel_fn: Callable[[], bool]) -> dict[str, Any]:
    return {"inventory": inspect_inputs(_inputs(msg), progress=progress, cancel=cancel_fn)}


def plan(msg: dict[str, Any], progress: Callable[[int, str], None], cancel_fn: Callable[[], bool]) -> dict[str, Any]:
    inv = msg.get("inventory")
    if not isinstance(inv, dict):
        inv = inspect_inputs(_inputs(msg), progress=progress, cancel=cancel_fn)
    budget = msg.get("budget_gb")
    out_dir = msg.get("out_dir")
    return {"inventory": inv, "plan": make_plan(inv, budget_gb=float(budget) if budget else None, out_dir=out_dir if isinstance(out_dir, str) else None)}


def run(msg: dict[str, Any], progress: Callable[[int, str], None], cancel_fn: Callable[[], bool]) -> dict[str, Any]:
    inv, pl = msg.get("inventory"), msg.get("plan")
    if not isinstance(inv, dict) or not isinstance(pl, dict):
        raise AutopilotError("auto_run needs the 'inventory' and 'plan' of auto_plan")
    out_dir = msg.get("out_dir")
    if not isinstance(out_dir, str) or not out_dir:
        raise AutopilotError("auto_run needs 'out_dir' (where results are written)")
    step_ids = msg.get("step_ids")
    if step_ids is not None and (not isinstance(step_ids, list) or not all(isinstance(s, str) for s in step_ids)):
        raise AutopilotError("'step_ids' must be a list of step ids")
    record = run_plan(inv, pl, out_dir=out_dir, step_ids=step_ids, progress=progress, cancel=cancel_fn)
    gallery: list[dict[str, Any]] = []
    for s in record["steps"]:
        for f in s.get("files", []):
            item: dict[str, Any] = {"step": s["id"], "name": Path(f).name, "path": f}
            if f.lower().endswith(".png"):
                try:
                    p = Path(f)
                    if p.stat().st_size <= MAX_PNG_BYTES:
                        item["png"] = base64.b64encode(p.read_bytes()).decode("ascii")
                except OSError:
                    pass
            gallery.append(item)
    return {"record": record, "gallery": gallery}
