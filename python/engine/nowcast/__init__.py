"""Radar nowcasting: motion, extrapolation, ensembles and an honest verification.

A nowcast here is **advection-based extrapolation**: the motion of the echo pattern is estimated
from the last few gridded frames (:mod:`engine.nowcast.motion`), the latest frame is moved along
that motion to future times (:mod:`engine.nowcast.extrapolate`), and the uncertainty of the motion
is turned into an ensemble and probabilities (:mod:`engine.nowcast.ensemble`). Growth, decay and
initiation are **not** predicted: that is the method's first limit, not a detail.

Every output is labelled ``forecast`` (:mod:`engine.layer_provenance`): experimental guidance, not
an official forecast. How good it is, is measured and not assumed: :mod:`engine.nowcast.verify`
scores forecasts against what was observed later, always next to Eulerian persistence, with
confidence intervals from a block bootstrap over events (``docs/NOWCAST.md``).

Entry points: :func:`engine.nowcast.run.nowcast` (arrays in, :class:`~engine.nowcast.run.Nowcast`
out), ``rxd nowcast`` (:mod:`engine.nowcast.cli`) and the ``nowcast_run`` WebSocket message
(:mod:`engine.nowcast.ws`).
"""

from __future__ import annotations

from engine.nowcast.frames import FrameSequence
from engine.nowcast.motion import MotionField, estimate_motion
from engine.nowcast.run import Nowcast, NowcastError, nowcast

__all__ = [
    "FrameSequence",
    "MotionField",
    "Nowcast",
    "NowcastError",
    "estimate_motion",
    "nowcast",
]
