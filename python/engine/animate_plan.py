"""Planning of ``xrd animate``: which volumes, which time steps, which frame shows which moment.

Pure Python (standard library only, no radar data is decoded here), so everything in this module is
fast and testable without a browser or radarx:

1. :func:`scan_files` finds the radar and the time of each file (from the NEXRAD style name, or from
   the file itself when the name does not say).
2. :func:`choose_steps` groups the volumes by radar and picks, for each time step, the volume of every
   radar that is nearest in time, within a tolerance. Everything that is not used is listed in
   :class:`StepPlan.skipped` with the reason, so nothing disappears silently.
3. :func:`plan_frames` maps every frame of the clip to a data time, the pair of observed volumes
   around it and the position ``s`` between them; a frame is *observed* when it sits on a real
   volume and *interpolated* otherwise (the screen labels it). The maths mirrors
   ``src/lib/utils/clipCamera.ts`` (``dataTimeAt``, ``locate``, ``morphShifts``) and is checked
   against it in the tests.
"""

# ruff: noqa: E501

from __future__ import annotations

import bisect
import json
import math
import os
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc

#: Largest distance in time (s) between a time step and the volume taken for it (5.5 min: one NEXRAD
#: volume is 4.5 to 6 min, so a radar one scan away still counts as "the same moment" for a storm).
TOLERANCE_S = 330.0
#: The clip holds the first and the last observed volume this long (s): it starts and ends on real data.
HOLD_S = 1.5
#: A frame is "observed" when its data time is within this many seconds of an observed volume.
OBSERVED_TOL_S = 6.0

_NEXRAD = re.compile(r"^(?P<site>[A-Z0-9]{4})(?P<date>\d{8})_(?P<time>\d{6})")
_GENERIC = re.compile(r"^(?P<site>[A-Za-z0-9]{3,8})[_-](?P<date>\d{8})[_T-](?P<time>\d{6})(?!\d)")


class AnimatePlanError(ValueError):
    """A problem with the input volumes that the user can fix (exit code 3)."""


class AnimateConfigError(ValueError):
    """A wrong option or setting in a config, camera or look file (exit code 2); the text says how to fix it."""


# ---------------------------------------------------------------------------
# Files -> (radar, time)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VolumeRef:
    """One radar volume file: which radar, when, and where it came from."""

    radar: str
    time: datetime  # UTC, timezone-aware
    path: Path
    source: str = "name"  # "name": read from the file name, "file": read from the file itself

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class Skip:
    """A file (or a whole time step) that is not used, with the reason in words."""

    what: str
    reason: str
    radar: str | None = None
    time: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"what": self.what, "radar": self.radar, "time": self.time, "reason": self.reason}


def parse_volume_name(path: str | Path) -> tuple[str, datetime] | None:
    """``(radar, UTC time)`` from a NEXRAD style name such as ``KHPX20211211_020457_V06``, else None."""
    name = Path(path).name
    for pat in (_NEXRAD, _GENERIC):
        m = pat.match(name)
        if m:
            try:
                t = datetime.strptime(m["date"] + m["time"], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
            except ValueError:
                continue
            return m["site"].upper(), t
    return None


def parse_time(text: str) -> datetime:
    """An ISO time (``2021-12-11T02:00:00Z``, ``2021-12-11 02:00``, ``20211211T0200``) as UTC."""
    raw = text.strip().replace("Z", "+00:00")
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d_%H%M%S", "%Y%m%d_%H%M"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
        except ValueError:
            pass
    try:
        t = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise AnimatePlanError(
            f"cannot read the time {text!r}: use for example 2021-12-11T02:00:00Z"
        ) from exc
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def radar_label(attrs: dict[str, Any], lat: float | None, lon: float | None, fallback: str) -> str:
    """A name for the radar of a file: its ``instrument_name``, else its position, else ``fallback``.

    The file name is not used when the file says better: a series of files of one radar must group
    together, and a series named by time (``cfrad.20211011_201557.nc``) would otherwise be one
    "radar" per file.
    """
    for key in ("instrument_name", "station", "site_name", "platform_name"):
        val = attrs.get(key)
        if val is not None and str(val).strip() not in ("", "None", "nan"):
            return str(val).strip().upper()
    if lat is not None and lon is not None and math.isfinite(lat) and math.isfinite(lon):
        return f"SITE{lat:+.2f}{lon:+.2f}"
    return fallback.upper()


def read_file_meta(path: Path) -> tuple[str, datetime]:
    """Radar and volume time from the file itself (slow: the file is opened)."""
    from engine import api

    with api.open(str(path)) as vol:
        info = vol.info()
        attrs: dict[str, Any] = {}
        try:
            tree = vol.tree
            attrs = {**dict(getattr(tree, "attrs", {})), **dict(tree.root.to_dataset().attrs)}
        except Exception:  # a format without root attributes
            attrs = {}
    when = info.get("time")
    if not when:
        raise AnimatePlanError(f"{path.name}: the file has no volume time")
    radar = radar_label(attrs, info.get("latitude"), info.get("longitude"), path.stem)
    return radar, parse_time(str(when))


class MetaCache:
    """Radar and time of files that were read once (``path, size, mtime`` -> radar, time), in a JSON
    file, so a large set of files whose names carry no time is opened only the first time."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.data: dict[str, list[str]] = {}
        self.dirty = False
        if path and path.is_file():
            try:
                self.data = json.loads(path.read_text())
            except (OSError, ValueError):
                self.data = {}

    @staticmethod
    def _key(p: Path) -> str:
        st = p.stat()
        return f"{p.resolve()}|{st.st_size}|{st.st_mtime_ns}"

    def wrap(
        self, meta: Callable[[Path], tuple[str, datetime]]
    ) -> Callable[[Path], tuple[str, datetime]]:
        def cached(p: Path) -> tuple[str, datetime]:
            key = self._key(p)
            if key in self.data:
                radar, when = self.data[key]
                return radar, parse_time(when)
            radar, when_dt = meta(p)
            self.data[key] = [radar, when_dt.isoformat()]
            self.dirty = True
            return radar, when_dt

        return cached

    def save(self) -> None:
        if not (self.path and self.dirty):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f".part{os.getpid()}")
        tmp.write_text(json.dumps(self.data))
        tmp.replace(self.path)
        self.dirty = False


def scan_files(
    paths: Iterable[str | Path],
    meta: Callable[[Path], tuple[str, datetime]] | None = None,
) -> tuple[list[VolumeRef], list[Skip]]:
    """Radar and time of each file: from the name if it says, otherwise from the file (``meta``).

    Files whose radar or time cannot be determined are returned as :class:`Skip` entries.
    """
    meta = meta or read_file_meta
    refs: list[VolumeRef] = []
    skipped: list[Skip] = []
    seen: set[Path] = set()
    for raw in paths:
        p = Path(raw)
        key = p.resolve() if p.exists() else p
        if key in seen:
            continue
        seen.add(key)
        parsed = parse_volume_name(p)
        if parsed:
            refs.append(VolumeRef(parsed[0], parsed[1], p, "name"))
            continue
        try:
            radar, when = meta(p)
        except Exception as exc:  # unreadable or no time: say so and go on
            skipped.append(Skip(p.name, f"cannot tell the radar and time: {str(exc)[:120]}"))
            continue
        refs.append(VolumeRef(radar, when, p, "file"))
    return refs, skipped


def group_by_radar(refs: Iterable[VolumeRef]) -> dict[str, list[VolumeRef]]:
    """``{radar: [volumes sorted by time]}``; two files of one radar at the same second keep one."""
    out: dict[str, list[VolumeRef]] = {}
    for r in refs:
        out.setdefault(r.radar, []).append(r)
    for radar, vols in out.items():
        vols.sort(key=lambda v: (v.time, v.name))
        dedup: list[VolumeRef] = []
        for v in vols:
            if dedup and dedup[-1].time == v.time:
                continue
            dedup.append(v)
        out[radar] = dedup
    return out


# ---------------------------------------------------------------------------
# Time steps
# ---------------------------------------------------------------------------


@dataclass
class Step:
    """One time step: the reference time and the volume each radar contributes (with its offset)."""

    time: datetime  # time of the reference radar's volume
    files: dict[str, tuple[VolumeRef, float]]  # radar -> (volume, offset in s from ``time``)

    def offsets(self) -> dict[str, float]:
        return {r: off for r, (_v, off) in self.files.items()}


@dataclass
class StepPlan:
    reference: str
    steps: list[Step]
    skipped: list[Skip] = field(default_factory=list)
    tolerance_s: float = TOLERANCE_S
    radars: list[str] = field(default_factory=list)  # every radar that had at least one volume

    @property
    def times_s(self) -> list[float]:
        t0 = self.steps[0].time
        return [(s.time - t0).total_seconds() for s in self.steps]

    @property
    def used_radars(self) -> list[str]:
        return sorted({r for s in self.steps for r in s.files})


def _nearest(vols: Sequence[VolumeRef], times: Sequence[datetime], t: datetime) -> VolumeRef:
    """The volume nearest in time to ``t`` (``vols`` sorted, ``times`` their times)."""
    i = bisect.bisect_left(times, t)
    cands = [vols[j] for j in (i - 1, i) if 0 <= j < len(vols)]
    return min(cands, key=lambda v: abs((v.time - t).total_seconds()))


def _steps_for(
    groups: dict[str, list[VolumeRef]],
    reference: str,
    tolerance_s: float,
    min_radars: int,
    min_changed_fraction: float,
    dropped: list[Skip] | None = None,
) -> list[Step]:
    times = {r: [v.time for v in vols] for r, vols in groups.items()}
    steps: list[Step] = []
    for ref_vol in groups[reference]:
        files: dict[str, tuple[VolumeRef, float]] = {}
        for radar, vols in groups.items():
            v = _nearest(vols, times[radar], ref_vol.time)
            off = (v.time - ref_vol.time).total_seconds()
            if abs(off) <= tolerance_s:
                files[radar] = (v, off)
        if len(files) >= min_radars:
            steps.append(Step(ref_vol.time, files))
        elif dropped is not None:
            dropped.append(
                Skip(
                    ref_vol.name,
                    f"time step {ref_vol.time:%H:%M:%S} UTC dropped: only {len(files)} radar(s) "
                    f"have a volume within {tolerance_s:.0f} s (need {min_radars})",
                    reference,
                    ref_vol.time.isoformat(),
                )
            )
    # A step only adds information when most radars have a NEW volume in it.
    kept: list[Step] = []
    for st in steps:
        if kept:
            prev = kept[-1]
            shared = [r for r in st.files if r in prev.files]
            changed = sum(1 for r in shared if st.files[r][0].path != prev.files[r][0].path)
            if shared and changed < min_changed_fraction * len(shared):
                if dropped is not None:
                    dropped.append(
                        Skip(
                            st.files[reference][0].name,
                            f"time step {st.time:%H:%M:%S} UTC dropped: it repeats the volumes of "
                            "the previous step (no new data)",
                            reference,
                            st.time.isoformat(),
                        )
                    )
                continue
        kept.append(st)
    return kept


def choose_steps(
    groups: dict[str, list[VolumeRef]],
    reference: str | None = None,
    *,
    tolerance_s: float = TOLERANCE_S,
    min_radars: int | None = None,
    min_changed_fraction: float = 0.5,
    min_step_s: float | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    max_steps: int | None = None,
) -> StepPlan:
    """Time steps along a reference radar; every other radar contributes its nearest volume.

    ``reference`` None or ``"auto"``: the radar whose volumes give the most steps (then the smallest
    time offsets of the others). A step needs ``min_radars`` radars within ``tolerance_s`` (default:
    2 when the input has two or more radars, otherwise 1). ``start`` / ``end`` cut the time window,
    ``min_step_s`` thins steps closer together than that, ``max_steps`` keeps the first N.

    Raises :class:`AnimatePlanError` when fewer than two time steps remain. The result lists every
    file that was not used with the reason.
    """
    if not groups:
        raise AnimatePlanError("no radar volumes found in the input")
    all_refs = [v for vols in groups.values() for v in vols]
    window: dict[str, list[VolumeRef]] = {}
    outside: list[Skip] = []
    for radar, vols in groups.items():
        keep = [
            v for v in vols if (start is None or v.time >= start) and (end is None or v.time <= end)
        ]
        outside += [
            Skip(v.name, "outside the --start / --end window", radar, v.time.isoformat())
            for v in vols
            if v not in keep
        ]
        if keep:
            window[radar] = keep
    if not window:
        raise AnimatePlanError("no radar volume lies inside the --start / --end window")
    need = min_radars if min_radars is not None else (2 if len(window) >= 2 else 1)
    if need > len(window):
        raise AnimatePlanError(
            f"--min-radars {need} but the input has volumes of only {len(window)} radar(s): "
            f"{', '.join(sorted(window))}"
        )

    dropped: list[Skip] = []
    if reference in (None, "auto"):
        best: tuple[tuple[int, float], str, list[Step]] | None = None
        for radar in sorted(window):
            trial = _steps_for(window, radar, tolerance_s, need, min_changed_fraction)
            if len(trial) < 2:
                continue
            offs = [abs(off) for st in trial for _, off in st.files.values()]
            key = (len(trial), -sum(offs) / max(1, len(offs)))
            if best is None or key > best[0]:
                best = (key, radar, trial)
        if best is None:
            raise AnimatePlanError(_too_few_steps(window, tolerance_s, need))
        reference = best[1]
    elif reference not in window:
        import difflib

        close = difflib.get_close_matches(reference.upper(), list(window), n=1)
        hint = f" Did you mean {close[0]}?" if close else ""
        raise AnimatePlanError(
            f"--reference {reference!r}: no volumes of that radar in the input.{hint} "
            f"Radars: {', '.join(sorted(window))}"
        )
    steps = _steps_for(window, reference, tolerance_s, need, min_changed_fraction, dropped)

    if min_step_s:
        thinned: list[Step] = []
        for st in steps:
            if thinned and (st.time - thinned[-1].time).total_seconds() < min_step_s:
                dropped.append(
                    Skip(
                        st.files[reference][0].name,
                        f"time step {st.time:%H:%M:%S} UTC dropped: less than --min-step "
                        f"{min_step_s:.0f} s after the previous step",
                        reference,
                        st.time.isoformat(),
                    )
                )
                continue
            thinned.append(st)
        steps = thinned
    if max_steps is not None and len(steps) > max_steps:
        for st in steps[max_steps:]:
            dropped.append(
                Skip(
                    st.files[reference][0].name,
                    f"time step {st.time:%H:%M:%S} UTC dropped: beyond --max-steps {max_steps}",
                    reference,
                    st.time.isoformat(),
                )
            )
        steps = steps[:max_steps]
    if len(steps) < 2:
        raise AnimatePlanError(_too_few_steps(window, tolerance_s, need))

    used = {v.path for st in steps for v, _off in st.files.values()}
    skipped = list(outside) + dropped
    mentioned = {s.what for s in skipped}
    step_times = [st.time for st in steps]
    for v in sorted(all_refs, key=lambda x: (x.radar, x.time)):
        if v.path in used or v.name in mentioned:
            continue
        gap = min(abs((v.time - t).total_seconds()) for t in step_times)
        if gap <= tolerance_s:
            reason = f"not the nearest {v.radar} volume to any time step (a closer one was used)"
        else:
            reason = (
                f"{gap:.0f} s from the nearest time step (tolerance {tolerance_s:.0f} s): "
                "no step uses it"
            )
        skipped.append(Skip(v.name, reason, v.radar, v.time.isoformat()))
    return StepPlan(reference, steps, skipped, tolerance_s, sorted(groups))


def _too_few_steps(window: dict[str, list[VolumeRef]], tol: float, need: int) -> str:
    counts = ", ".join(f"{r} {len(v)}" for r, v in sorted(window.items()))
    return (
        f"fewer than two usable time steps (volumes per radar: {counts}). An animation needs at "
        f"least two moments with {need} radar(s) within {tol:.0f} s of each other. Give more "
        "volumes, or widen --tolerance-s, or lower --min-radars."
    )


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------


def data_time_at(t: float, duration: float, t0: float, t1: float, hold: float = HOLD_S) -> float:
    """Data time (s) shown at clip time ``t`` (s): the first volume is held for ``hold`` s, then the
    data time runs from ``t0`` to ``t1`` (a little ease, mostly constant speed), then the last volume
    is held. Mirrors ``dataTimeAt`` of ``clipCamera.ts``."""
    a = max(0.0, min(1.0, (t - hold) / max(1e-9, duration - 2 * hold)))
    e = a * a * (3 - 2 * a) * 0.35 + a * 0.65
    return t0 + (t1 - t0) * e


def clip_time_of(td: float, duration: float, t0: float, t1: float, hold: float = HOLD_S) -> float:
    """The clip time (s) at which data time ``td`` is shown (inverse of :func:`data_time_at`)."""
    target = max(0.0, min(1.0, (td - t0) / max(1e-9, t1 - t0)))
    lo, hi = 0.0, 1.0
    for _ in range(40):
        m = (lo + hi) / 2
        e = m * m * (3 - 2 * m) * 0.35 + m * 0.65
        if e < target:
            lo = m
        else:
            hi = m
    return hold + ((lo + hi) / 2) * max(1e-9, duration - 2 * hold)


def locate(ts: Sequence[float], t: float) -> tuple[int, float]:
    """The interval ``i`` of the observed times that contains ``t`` and the position ``s`` in [0, 1]."""
    if len(ts) < 2 or t <= ts[0]:
        return 0, 0.0
    if t >= ts[-1]:
        return len(ts) - 2, 1.0
    i = 0
    while i < len(ts) - 2 and t > ts[i + 1]:
        i += 1
    return i, (t - ts[i]) / (ts[i + 1] - ts[i])


def interpolation_weights(s: float) -> tuple[float, float]:
    """Weights of the earlier volume A and the later volume B at position ``s`` between them.

    Each is first carried along the storm motion to the moment shown (:func:`morph_shifts`), then
    they are blended with these weights: A fades out as B fades in, and at ``s`` = 0 or 1 the
    picture is exactly one observed volume.
    """
    s = max(0.0, min(1.0, s))
    return 1.0 - s, s


def morph_shifts(
    motion_km_s: tuple[float, float], dt: float, s: float
) -> tuple[tuple[float, float], tuple[float, float]]:
    """How far (km east, north) volume A is carried forward and volume B back for position ``s``."""
    mx, my = motion_km_s
    return (mx * s * dt, my * s * dt), (-mx * (1 - s) * dt, -my * (1 - s) * dt)


@dataclass
class Frame:
    index: int
    t: float  # clip time, s
    td: float  # data time, s after the first volume
    k: int  # interval (volume k to volume k + 1)
    s: float  # position inside the interval, 0..1
    observed: bool  # on an observed volume (within the tolerance); otherwise interpolated
    nearest: int  # index of the observed volume nearest in time


def frame_count(duration: float, fps: float) -> int:
    return max(1, round(duration * fps))


def plan_frames(
    duration: float,
    fps: float,
    ts: Sequence[float],
    observed_tol_s: float = OBSERVED_TOL_S,
    hold: float = HOLD_S,
) -> list[Frame]:
    """Every frame of the clip: its data time, interval, position and whether it is observed.

    ``ts`` are the data times (s after the first volume) of the observed volumes.
    """
    if len(ts) < 2:
        raise AnimatePlanError("a frame plan needs at least two observed volumes")
    if duration <= 2 * hold + 0.5:
        raise AnimatePlanError(
            f"the clip is too short ({duration:g} s): it holds the first and last volume for "
            f"{hold:g} s each, so it must be longer than {2 * hold + 0.5:g} s"
        )
    frames: list[Frame] = []
    for i in range(frame_count(duration, fps)):
        t = i / fps
        td = data_time_at(t, duration, ts[0], ts[-1], hold)
        k, s = locate(ts, td)
        near = min(range(len(ts)), key=lambda j: abs(ts[j] - td))
        frames.append(Frame(i, t, td, k, s, abs(ts[near] - td) < observed_tol_s, near))
    return frames


def summarise_frames(frames: Sequence[Frame], ts: Sequence[float]) -> dict[str, Any]:
    """Counts for the report: how many frames are observed or interpolated, and the gaps between volumes."""
    n = len(frames)
    n_obs = sum(1 for f in frames if f.observed)
    per_interval = [
        {
            "from": k,
            "to": k + 1,
            "gap_s": ts[k + 1] - ts[k],
            "frames": sum(1 for f in frames if f.k == k),
            "interpolated_frames": sum(1 for f in frames if f.k == k and not f.observed),
        }
        for k in range(len(ts) - 1)
    ]
    gaps = [ts[k + 1] - ts[k] for k in range(len(ts) - 1)]
    return {
        "frames": n,
        "observed_frames": n_obs,
        "interpolated_frames": n - n_obs,
        "interpolated_share": (n - n_obs) / n if n else 0.0,
        "data_span_s": ts[-1] - ts[0],
        "longest_gap_s": max(gaps),
        "shortest_gap_s": min(gaps),
        "intervals": per_interval,
    }


def duration_for_timelapse(span_s: float, timelapse: float, hold: float = HOLD_S) -> float:
    """Clip length (s) for a time-lapse factor: the data span divided by it, plus the two holds."""
    if timelapse <= 0:
        raise AnimatePlanError("--timelapse must be a positive number")
    return round(span_s / timelapse + 2 * hold, 1)


def timelapse_of(span_s: float, duration: float, hold: float = HOLD_S) -> float:
    """The time-lapse factor of a clip (data seconds per clip second while the data time runs)."""
    return span_s / max(1.0, duration - 2 * hold)


def step_table(plan: StepPlan) -> list[dict[str, Any]]:
    """Rows for the report: index, time, and each radar's volume time offset."""
    rows = []
    t0 = plan.steps[0].time
    for i, st in enumerate(plan.steps):
        rows.append(
            {
                "index": i,
                "time": st.time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "t_s": (st.time - t0).total_seconds(),
                "offsets_s": {r: off for r, (_v, off) in sorted(st.files.items())},
                "files": {r: v.name for r, (v, _off) in sorted(st.files.items())},
            }
        )
    return rows


def offset_summary(plan: StepPlan) -> dict[str, dict[str, float]]:
    """Per radar: how many steps it is in and the mean / largest absolute time offset (s)."""
    out: dict[str, dict[str, float]] = {}
    for radar in plan.used_radars:
        offs = [abs(st.files[radar][1]) for st in plan.steps if radar in st.files]
        out[radar] = {
            "steps": len(offs),
            "mean_abs_offset_s": sum(offs) / len(offs),
            "max_abs_offset_s": max(offs),
        }
    return out
