"""Data-driven soundtrack of the PERiLS movie: synthesised from the data, not a recording.

The heavy modules (numpy / scipy) are imported on first use, so ``mux_audio`` can be used from a
scratch environment that only has PyAV and numpy.
"""

from __future__ import annotations

LABEL = "Synthesised from the data, not a recording"

__all__ = ["LABEL", "mux_audio", "render_audio"]


def __getattr__(name: str):
    if name == "render_audio":
        from .render import render_audio

        return render_audio
    if name == "mux_audio":
        from .mux import mux_audio

        return mux_audio
    raise AttributeError(name)
