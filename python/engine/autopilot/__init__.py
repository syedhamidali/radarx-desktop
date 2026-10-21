"""Autopilot ("Analyst"): given any set of inputs, decide what to do with them and how.

Four explicit stages, each a plain function returning JSON-able dicts:

1. :func:`inspect_inputs` (``inspect.py``): cheap inventory, metadata only (formats, radars, times,
   scan kinds, fields, dual-pol, size on disk, time gaps, overlap between radars) and the engine's
   data check on one representative file per radar.
2. :func:`classify` + the rule table (``rules.py``): the situation (single snapshot, time series,
   multi-radar network, ...) and the opportunities / problems, as rules with their evidence. Not a
   black box: every rule says why it fired or did not.
3. :func:`make_plan` (``plan.py``) with the resource governor (``govern.py``): the ranked, explained
   steps with estimates for THIS data, refusals with what is missing, and every reduction stated.
4. :func:`run_plan` (``run.py``): executes the approved steps with the existing engine functions,
   writes a run record and a one-page summary. Nothing heavy runs without consent.

Reachable from ``rxd auto`` (``cli.py``) and the app (``ws.py``: ``auto_inspect``, ``auto_plan``,
``auto_run``, ``auto_cancel``).
"""

from __future__ import annotations

from engine.autopilot.inspect import AutopilotError, inspect_inputs
from engine.autopilot.plan import make_plan
from engine.autopilot.rules import RULES, classify, evaluate_rules
from engine.autopilot.run import run_plan

__all__ = [
    "RULES",
    "AutopilotError",
    "classify",
    "evaluate_rules",
    "inspect_inputs",
    "make_plan",
    "run_plan",
]
