"""Sound for ``rxd animate --sound``: what drives it, the flash tables, the labels and the record.

The sound itself is synthesised in the clip page (``src/lib/utils/soundscape.ts``, offline Web Audio
in the same headless Chrome that draws the frames); this module decides and records everything
around it, without a browser:

* the thunder source: none, the MODELLED flashes of ``--lightning-model``, a flash table the user
  gives (``--flashes FILE``: CSV with ``time_utc, lat, lon`` and optionally ``type`` CG / IC and
  ``energy``) or OBSERVED GOES GLM flashes read from local files (``--flashes-from-glm``), thinned to
  the strongest flash of every N seconds within R km of the tracked storm;
* the default light-to-sound delay compression (the film's time-lapse factor, so the thunder keeps
  its place in the film's own time);
* the labels that are drawn on the film and written into the files;
* the record (``.animate.json`` ``sound`` block): listener, compression, seed, wind range, thunder
  table with times, distances and delays.

Everything is a method of the clip, nothing is invented: only the TIMES and PLACES of flashes (and the
wind and reflectivity at the listener) are data; the sound of the wind, rain and thunder is synthetic.
"""

# ruff: noqa: E501
from __future__ import annotations

import csv
import glob
import math
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from engine.animate_plan import AnimateConfigError

UTC = UTC

DEFAULT_SEED = 7
GLM_RADIUS_KM = 30.0
SAMPLE_RATE = 48000
#: scales the lowest retrieved wind to a ground value (the same factor the clip tool uses)
SURFACE_WIND_FACTOR = 0.65
#: more thunderclaps per film second than this are a continuous roar: the user is told to thin
DENSE_PER_FILM_S = 3.0

#: the mix of ``rxd animate``: the thunder is raised and the wind and rain bed lowered relative to the
#: clip tool's mix, so the thunder stands clearly above the bed (measured on the Mayfield films, see
#: docs/ANIMATE.md); the loudest sample stays below this fraction of full scale (-1.9 dBFS) so that the
#: AAC track decodes below -1 dBFS
THUNDER_DB = 10.0
BED_DB = -4.0
CEILING = 0.8

LIMITS = [
    "The sound is synthesised from data; it is not a recording and nobody recorded this storm at this place and time.",
    "The listener is a virtual point on the ground: distances to the flashes are a consequence of that choice, not a measurement.",
    "Wind follows the retrieved multi-Doppler wind at the listener (lowest level times 0.65 as a ground value), which exists only where two radars cross well and has artefacts; where there is none a labelled proxy from the storm motion and the rain is used.",
    "Rain follows the reflectivity at the lowest gridded level that has a value over the listener, searched up to 2.5 km above the ground (far from the radars the grid has nothing nearer the ground), so it is the echo aloft, not the rain at the ground; reflectivity is not a rain rate: the loudness is a linear map of dBZ (20 dBZ none, 55 dBZ full), not a rainfall measurement.",
    "Thunder: only the time and place of each flash are data (GLM: about 10 km, detection efficiency below 100 %); the flashes are thinned for the time-lapse; the real light-to-sound delay is added in data time and mapped through the film's clock (or divided by --sound-compress when given); the sound of the thunder is synthetic.",
    "GLM does not say whether a flash reached the ground: the cloud-to-ground / in-cloud type of a GLM flash (channel shape, close crack) is drawn at random and marked synthetic in the flash table.",
    "There is no tornado, hail, siren or debris sound: nothing in the data says anything about them.",
    "The mix puts the thunder clearly above the wind and rain bed (the bed stays audible, never silent); a distant listener hears fainter thunder because loudness falls with distance.",
]


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


def parse_listener(text: str) -> tuple[float, float]:
    """``--sound-listener X,Y``: km east and north of the middle of the storm's track."""
    try:
        parts = [float(v) for v in str(text).replace(";", ",").split(",")]
        if len(parts) != 2 or not all(math.isfinite(v) for v in parts):
            raise ValueError
    except ValueError:
        raise AnimateConfigError(
            f"--sound-listener {text!r}: use X,Y in km east and north of the middle of the storm's track, e.g. 10,-4"
        ) from None
    return parts[0], parts[1]


def auto_compress(timelapse: float) -> float:
    """Default light-to-sound delay compression: the film's time-lapse factor (data seconds per clip
    second), so a thunderclap keeps its place in the film's own time. 1 is the real physics."""
    return max(1.0, round(float(timelapse), 1))


def auto_bin_s(timelapse: float) -> float:
    """Default thinning bin (data seconds): the time-lapse factor, so about one thunderclap per film
    second at most (the real rate of 20-40 flashes a minute would be a continuous roar)."""
    return float(min(300.0, max(10.0, round(float(timelapse)))))


# ---------------------------------------------------------------------------
# Flash tables
# ---------------------------------------------------------------------------


def _parse_time(text: str, where: str) -> datetime:
    t = str(text).strip()
    if t.endswith(("Z", "z")):
        t = t[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        raise AnimateConfigError(
            f"{where}: time {text!r} is not a UTC time like 2021-12-11T02:32:36Z"
        ) from None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def read_flash_table(path: str | os.PathLike[str]):
    """The flashes of a CSV file: columns ``time_utc, lat, lon`` and optionally ``type`` (CG / IC) and
    ``energy`` (any positive number: only its order within the file is used). Times are UTC."""
    from engine.lightning_obs import ObservedFlash, _relative

    p = Path(path).expanduser()
    if not p.is_file():
        raise AnimateConfigError(f"--flashes: file not found: {p}")
    with open(p, newline="", encoding="utf-8-sig") as fh:
        rd = csv.DictReader(ln for ln in fh if not ln.lstrip().startswith("#"))
        names = {(n or "").strip().lower(): n for n in (rd.fieldnames or [])}
        missing = [c for c in ("time_utc", "lat", "lon") if c not in names]
        if missing:
            raise AnimateConfigError(
                f"--flashes {p.name}: the CSV needs the columns time_utc, lat, lon (optionally type, energy); "
                f"missing: {', '.join(missing)}; found: {', '.join(rd.fieldnames or []) or 'none'}"
            )
        rows = []
        for ln, row in enumerate(rd, start=2):
            where = f"--flashes {p.name} line {ln}"
            if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
                continue
            try:
                lat, lon = float(row[names["lat"]]), float(row[names["lon"]])
            except (TypeError, ValueError):
                raise AnimateConfigError(f"{where}: lat and lon must be numbers") from None
            if not (-90 <= lat <= 90 and -180 <= lon <= 360):
                raise AnimateConfigError(f"{where}: lat {lat} / lon {lon} is not a place on Earth")
            kind = (row.get(names["type"]) or "").strip().upper() if "type" in names else ""
            if (
                "type_source" in names
                and (row.get(names["type_source"]) or "").strip().lower() == SYNTHETIC_TYPE
            ):
                kind = ""  # a type drawn at random by an earlier run is not data
            if kind not in ("", "CG", "IC"):
                raise AnimateConfigError(f"{where}: type {kind!r} must be CG or IC (or empty)")
            en = None
            if "energy" in names and (row.get(names["energy"]) or "").strip():
                try:
                    en = float(row[names["energy"]])
                except ValueError:
                    raise AnimateConfigError(f"{where}: energy must be a number") from None
            rows.append((_parse_time(row[names["time_utc"]], where), lat, lon, kind or None, en))
    if not rows:
        raise AnimateConfigError(f"--flashes {p.name}: no flashes in the file")
    have = any(r[4] is not None for r in rows)
    rel = (
        _relative(np.array([r[4] if r[4] is not None else np.nan for r in rows])) if have else None
    )
    out = [
        ObservedFlash(
            t,
            la,
            lo,
            None,
            float(rel[i]) if rel is not None and rows[i][4] is not None else 0.6,
            kind=k,
        )
        for i, (t, la, lo, k, _e) in enumerate(rows)
    ]
    out.sort(key=lambda f: f.time)
    return out


#: ``type_source`` of a type that was drawn at random (GLM gives no type): never read back as data
SYNTHETIC_TYPE = "synthetic"


def write_flash_table(
    path: Path,
    flashes,
    distance_km: list[float] | None = None,
    chosen_kind: list[str | None] | None = None,
    seed: int | None = None,
) -> None:
    """Write flashes as the CSV that ``--flashes`` reads (plus the distance to the storm when known).

    ``chosen_kind``: the CG / IC type the clip drew at random for flashes without a type (GLM gives
    none); it is written with ``type_source`` ``synthetic`` and a comment line says so, and
    ``read_flash_table`` does not read a synthetic type back as data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    synthetic = [
        not f.kind and bool(chosen_kind and i < len(chosen_kind) and chosen_kind[i])
        for i, f in enumerate(flashes)
    ]
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        if any(synthetic):
            fh.write(
                "# type: where type_source is 'synthetic' the CG / IC type is SYNTHETIC, drawn at random "
                f"(seed {seed if seed is not None else DEFAULT_SEED}) for the channel shape and the thunder sound: "
                "GOES GLM (L2 LCFA) gives the time, place, area, energy and quality of a flash, not whether it "
                "reached the ground. Not an observation.\n"
            )
        w = csv.writer(fh)
        head = ["time_utc", "lat", "lon", "type", "energy"]
        if chosen_kind is not None:
            head.append("type_source")
        if distance_km is not None:
            head.append("distance_to_storm_km")
        w.writerow(head)
        for i, f in enumerate(flashes):
            kind = f.kind or (chosen_kind[i] if synthetic[i] and chosen_kind else "") or ""
            row = [
                f.time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                f"{f.lat:.4f}",
                f"{f.lon:.4f}",
                kind,
                f"{f.energy:.4f}",
            ]
            if chosen_kind is not None:
                row.append(SYNTHETIC_TYPE if synthetic[i] else ("file" if f.kind else ""))
            if distance_km is not None:
                row.append(f"{distance_km[i]:.1f}")
            w.writerow(row)
    tmp.replace(path)


def chosen_kinds(table, thunders: list[dict[str, Any]], t0: datetime) -> list[str | None]:
    """The CG / IC type the page used for every row of ``table`` (matched by data time), None when unknown."""
    if not thunders:
        return [None] * len(table)
    td = np.array([float(th["tDataS"]) for th in thunders])
    out: list[str | None] = []
    for f in table:
        j = int(np.argmin(np.abs(td - (f.time - t0).total_seconds())))
        out.append(
            thunders[j].get("kind") if abs(td[j] - (f.time - t0).total_seconds()) < 1.0 else None
        )
    return out


def glm_files(items: list[str]) -> list[Path]:
    """GLM L2 LCFA netCDF files from folders (searched below), files and globs."""
    out: list[Path] = []
    for raw in items:
        p = Path(os.path.expanduser(raw))
        if p.is_dir():
            out += sorted(q for q in p.rglob("*.nc") if q.is_file())
        elif p.is_file():
            out.append(p)
        else:
            out += sorted(Path(g) for g in glob.glob(os.path.expanduser(raw)) if Path(g).is_file())
    seen: set[Path] = set()
    out = [f for f in out if not (f in seen or seen.add(f))]
    if not out:
        raise AnimateConfigError(
            "--flashes-from-glm: no GLM files (*.nc) found in "
            + ", ".join(items)
            + ". Give a folder, files or a pattern of GOES GLM Level 2 LCFA netCDF files "
            "(OR_GLM-L2-LCFA_G16_s...nc); nothing is downloaded by rxd."
        )
    return out


def thin_to_strongest(
    t_s: np.ndarray, energy: np.ndarray, *, bin_s: float, t_start: float = 0.0
) -> list[int]:
    """Indices of the strongest flash of every ``bin_s`` seconds (bins count from ``t_start``); a tie goes
    to the earlier flash. Returned in time order."""
    if bin_s <= 0:
        raise AnimateConfigError("the thinning interval must be positive (seconds)")
    best: dict[int, int] = {}
    en = np.where(np.isfinite(energy), energy, -np.inf)
    for i in np.argsort(t_s, kind="stable"):
        b = math.floor((t_s[i] - t_start) / bin_s)
        j = best.get(b)
        if j is None or en[i] > en[j]:
            best[b] = int(i)
    return sorted(best.values(), key=lambda i: t_s[i])


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass
class SoundPlan:
    """What the sound of a run is made from (decided without a browser)."""

    thunder: str  # "observed_glm" | "observed_file" | "modelled" | "none"
    listener_request: tuple[float, float] | None
    compress: float
    compress_source: str
    seed: int
    timelapse: float
    label: list[str]
    flashes: list[dict[str, Any]] = field(
        default_factory=list
    )  # bundle records (t_s, x, y, energy, kind)
    flash_times: list[datetime] = field(default_factory=list)
    flash_latlon: list[tuple[float, float]] = field(default_factory=list)
    flash_distance_km: list[float] = field(default_factory=list)
    selection: dict[str, Any] = field(default_factory=dict)
    source_files: list[str] = field(default_factory=list)
    table: list[Any] = field(default_factory=list)  # the ObservedFlash rows that were kept
    warnings: list[str] = field(default_factory=list)
    draw_flashes: bool = False
    thunder_db_offset: float = 0.0  # --sound-thunder-db: dB relative to the default mix
    flash_file: str | None = None

    @property
    def flash_label(self) -> str | None:
        """What the drawn flashes are, on the film (None: the picture's own lightning label)."""
        if self.thunder == "observed_glm":
            return "Lightning: OBSERVED GLM flash (position ~10 km, optical, not a stroke); channel shape synthetic, thinned for the time-lapse"
        if self.thunder == "observed_file":
            return f"Lightning: flash from {self.flash_file or 'the file you gave'} (not checked here); channel shape synthetic"
        return None

    @property
    def drawn(self) -> bool:
        """True when the flashes of the thunder source are shown in the picture."""
        return self.thunder == "modelled" or (self.draw_flashes and self.page_thunder == "observed")

    @property
    def page_thunder(self) -> str:
        return {
            "observed_glm": "observed",
            "observed_file": "observed",
            "modelled": "modelled",
        }.get(self.thunder, "off")

    def page_settings(self) -> dict[str, Any]:
        s: dict[str, Any] = {
            "thunder": self.page_thunder,
            "compress": self.compress,
            "compressAuto": self.compress_source.startswith("automatic"),
            "seed": self.seed,
            "label": self.label,
            "thunderDb": THUNDER_DB + self.thunder_db_offset,
            "bedDb": BED_DB,
            "ceiling": CEILING,
        }
        if self.draw_flashes and self.page_thunder == "observed":
            s["drawFlashes"] = True
            s["flashKind"] = "glm" if self.thunder == "observed_glm" else "file"
            s["flashLabel"] = self.flash_label
        if self.listener_request is not None:
            s["listener"] = [self.listener_request[0], self.listener_request[1]]
        if self.page_thunder == "observed":
            s["flashes"] = self.flashes
        return s

    def summary(self) -> dict[str, Any]:
        """What the plan decided (JSON of ``--no-render --json``); the full record is added after rendering."""
        return {
            "enabled": True,
            "synthesised": True,
            "recording": False,
            "label": self.label,
            "listener": {
                "requested_km_east_north_of_track_middle": list(self.listener_request)
                if self.listener_request
                else None,
                "default": self.listener_request is None,
            },
            "compress": self.compress,
            "compress_source": self.compress_source,
            "timelapse": self.timelapse,
            "seed": self.seed,
            "thunder": {
                "source": self.thunder,
                "files": self.source_files,
                "selection": self.selection,
                "flashes": len(self.flashes),
                "drawn": self.drawn,
            },
            "mix": {
                "thunder_db": THUNDER_DB + self.thunder_db_offset,
                "thunder_db_default": THUNDER_DB,
                "thunder_db_offset": self.thunder_db_offset,
                "bed_db": BED_DB,
                "ceiling": CEILING,
                "rule": "relative to the clip tool's mix; the thunder is raised and the wind and rain bed lowered so that thunder stands above the bed",
            },
        }


def wind_words(wind_retrieved: bool, retrieved_share: float | None = None) -> str:
    """What the wind of the sound is made of. Before rendering only whether any step has retrieved
    winds is known; after it, ``retrieved_share``: the share of film time the sampler found a
    retrieved wind at the listener."""
    if retrieved_share is None:
        return (
            "wind from the retrieved winds where they reach the listener, a proxy elsewhere"
            if wind_retrieved
            else "wind a PROXY from the storm motion and the rain (no retrieved winds)"
        )
    pct = round(100 * float(retrieved_share))
    if retrieved_share >= 0.995:
        return "wind from the retrieved winds"
    if pct <= 0:
        return "wind a PROXY from the storm motion and the rain (no retrieved wind at the listener)"
    return f"wind from the retrieved winds for {pct}% of the film, a proxy otherwise"


def label_rows(
    wind_retrieved: bool,
    thunder: str,
    flash_file: str | None = None,
    drawn: bool = False,
    *,
    retrieved_share: float | None = None,
    rain_heard: bool | None = None,
) -> list[str]:
    """The two lines drawn on the film (and written into its files): what the sound is.

    ``retrieved_share`` and ``rain_heard`` come from the rendered sound (see :func:`final_label`); a
    rain that stayed silent for the whole film is said on the film."""
    winds = wind_words(wind_retrieved, retrieved_share)
    rain = (
        "rain SILENT (no echo of 20 dBZ or more low over the listener)"
        if rain_heard is False
        else "rain from the reflectivity"
    )
    if thunder == "observed_glm":
        th = f"thunder timed to OBSERVED GLM flashes ({'drawn' if drawn else 'not drawn'})"
    elif thunder == "observed_file":
        th = f"thunder timed to the flashes of {flash_file or 'the file you gave'} ({'drawn' if drawn else 'not drawn'}, not checked here)"
    elif thunder == "modelled":
        th = "thunder timed to MODELLED flashes (not observed)"
    else:
        th = "no thunder"
    return [
        f"Sound is SYNTHESISED, not a recording: {winds}, {rain},",
        f"{th}; the listener position is virtual",
    ]


def retrieved_share(audio: dict[str, Any]) -> float:
    """Share of film time with a retrieved wind at the listener, from the rendered sound."""
    if audio.get("windSource") != "retrieved":
        return 0.0
    return max(0.0, min(1.0, 1.0 - float(audio.get("windProxyShare", 0.0))))


def rain_heard(audio: dict[str, Any]) -> bool:
    """True when the rain level rose above zero at some time of the film."""
    return float(audio.get("rainLevel", {}).get("max", 0.0)) > 0.0


def final_label(plan: SoundPlan, audio: dict[str, Any]) -> list[str]:
    """The label from what the sound was actually made of (wind share at the listener, rain heard or not)."""
    return label_rows(
        audio.get("windSource") == "retrieved",
        plan.thunder,
        plan.flash_file,
        plan.draw_flashes,
        retrieved_share=retrieved_share(audio),
        rain_heard=rain_heard(audio),
    )


def audio_warnings(audio: dict[str, Any]) -> list[str]:
    """What the user must be told about the rendered sound."""
    out = []
    proxy = float(audio.get("windProxyShare", 0.0))
    if proxy > 0:
        out.append(
            f"sound: for {100 * proxy:.0f}% of the time there was no retrieved wind at the "
            "listener, so a proxy from the storm motion and the rain was used there"
        )
    if not rain_heard(audio):
        out.append(
            "sound: the rain level stayed at zero for the whole film (no echo of 20 dBZ or more at the lowest "
            "gridded level up to 2.5 km above the ground over the listener): the rain is silent; the film says so"
        )
    return out


def _project(manifest: dict[str, Any]):
    from engine import mosaic

    o = manifest["origin"]
    return mosaic._aeqd(o["lat"], o["lon"])


def plan_sound(
    o: Any,
    manifest: dict[str, Any],
    t0: datetime,
    times_s: list[float],
    duration: float,
    timelapse: float,
    wind_retrieved: bool,
) -> SoundPlan:
    """Decide the thunder source, read and thin the flashes, choose the compression. Raises
    ``AnimateConfigError`` (with the fix in the message) when a source cannot give any flash."""
    sources = [bool(o.flashes), bool(o.flashes_from_glm), bool(o.lightning_model)]
    if sum(sources) > 1:
        raise AnimateConfigError(
            "give one thunder source: --flashes FILE, --flashes-from-glm FOLDER or --lightning-model "
            "(the sound must follow the flashes that are shown or named on the film)"
        )
    seed = DEFAULT_SEED if o.sound_seed is None else int(o.sound_seed)
    if o.sound_compress is not None:
        compress, csrc = float(o.sound_compress), "--sound-compress"
    else:
        compress, csrc = (
            auto_compress(timelapse),
            f"automatic: the film's time-lapse x{timelapse:.1f}",
        )
    plan = SoundPlan(
        draw_flashes=bool(o.draw_flashes),
        thunder_db_offset=float(o.sound_thunder_db or 0.0),
        flash_file=Path(o.flashes).name if o.flashes else None,
        thunder="none",
        listener_request=tuple(o.sound_listener) if o.sound_listener else None,  # type: ignore[arg-type]
        compress=compress,
        compress_source=csrc,
        seed=seed,
        timelapse=float(timelapse),
        label=[],
    )
    span = times_s[-1]
    if o.lightning_model:
        plan.thunder = "modelled"
    elif o.flashes or o.flashes_from_glm:
        proj = _project(manifest)

        def rel0(f: Any) -> float:
            return (f.time - t0).total_seconds()

        if o.flashes:
            plan.thunder = "observed_file"
            plan.source_files = [str(Path(o.flashes).expanduser())]
            flashes = read_flash_table(o.flashes)
            n_read = len(flashes)
            kept = [f for f in flashes if 0.0 <= rel0(f) <= span]
            plan.selection = {
                "read": n_read,
                "in_time_window": len(kept),
                "thinning": "none: the flashes are used as given",
            }
            if not kept:
                raise AnimateConfigError(
                    f"--flashes {Path(o.flashes).name}: none of its {n_read} flashes falls within the film's data time "
                    f"({t0:%Y-%m-%d %H:%M:%S} to {t0 + timedelta(seconds=span):%H:%M:%S} UTC)"
                )
        else:
            plan.thunder = "observed_glm"
            files = glm_files(list(o.flashes_from_glm))
            plan.source_files = (
                [f.name for f in files]
                if len(files) <= 6
                else [files[0].name, "...", files[-1].name]
            )
            all_f: list[Any] = []
            for f in files:
                import xarray as xr

                from engine.lightning_obs import flashes_from_glm_dataset

                try:
                    with xr.open_dataset(f) as ds:
                        all_f += flashes_from_glm_dataset(ds)
                except Exception as exc:
                    raise AnimateConfigError(
                        f"--flashes-from-glm: {f.name} could not be read as a GLM Level 2 LCFA file ({str(exc).splitlines()[0][:120]})"
                    ) from exc
            good = [f for f in all_f if f.quality in (None, 0)]
            window = [f for f in good if 0.0 <= rel0(f) <= span]
            tr_t = np.array(times_s, dtype=float)
            tr_xy = np.array(manifest["storm"]["track_km"], dtype=float)
            xs = np.empty(len(window))
            ys = np.empty(len(window))
            for i, f in enumerate(window):
                x, y = proj(f.lon, f.lat)
                xs[i], ys[i] = x / 1000.0, y / 1000.0
            tt = np.array([rel0(f) for f in window], dtype=float)
            if len(window):
                cx = np.interp(tt, tr_t, tr_xy[:, 0])
                cy = np.interp(tt, tr_t, tr_xy[:, 1])
                dist = np.hypot(xs - cx, ys - cy)
            else:
                dist = np.zeros(0)
            radius = float(o.glm_radius_km) if o.glm_radius_km is not None else GLM_RADIUS_KM
            bin_s = float(o.glm_bin_s) if o.glm_bin_s is not None else auto_bin_s(timelapse)
            near = np.nonzero(dist <= radius)[0]
            energy = np.array(
                [window[i].energy_raw if window[i].energy_raw is not None else np.nan for i in near]
            )
            pick = thin_to_strongest(tt[near], energy, bin_s=bin_s) if len(near) else []
            kept = [window[int(near[j])] for j in pick]
            plan.flash_distance_km = [float(dist[int(near[j])]) for j in pick]
            plan.selection = {
                "files": len(files),
                "flashes_read": len(all_f),
                "good_quality": len(good),
                "in_time_window": len(window),
                "within_radius": len(near),
                "radius_km": radius,
                "bin_s": bin_s,
                "bin_s_source": "--glm-bin-s"
                if o.glm_bin_s is not None
                else f"automatic: the film's time-lapse x{timelapse:.0f}",
                "selected": len(kept),
                "rule": "good-quality flashes (quality flag 0) within the radius of the tracked storm centre, the strongest (radiant energy) of every bin of data time",
            }
            if not kept:
                raise AnimateConfigError(
                    f"--flashes-from-glm: no good GLM flash within {radius:g} km of the tracked storm during the film "
                    f"({len(all_f)} flashes read, {len(window)} in the time window). Check the files cover "
                    f"{t0:%Y-%m-%d %H:%M} to {t0 + timedelta(seconds=span):%H:%M} UTC, or raise --glm-radius-km"
                )
            from engine.lightning_obs import _relative

            rel = _relative(
                np.array([f.energy_raw if f.energy_raw is not None else np.nan for f in kept])
            )
            for f, r in zip(kept, rel, strict=True):
                f.energy = float(r)
        plan.table = kept
        plan.flash_times = [f.time for f in kept]
        plan.flash_latlon = [(f.lat, f.lon) for f in kept]
        recs = []
        for f in kept:
            x, y = proj(f.lon, f.lat)
            rec: dict[str, Any] = {
                "t_s": round(rel0(f), 3),
                "x": float(x) / 1000.0,
                "y": float(y) / 1000.0,
                "energy": float(f.energy),
            }
            if f.kind:
                rec["kind"] = f.kind
            recs.append(rec)
        plan.flashes = recs
        per_s = len(recs) / max(1.0, duration - 3.0)
        plan.selection["thunderclaps_per_film_second"] = round(per_s, 2)
        if per_s > DENSE_PER_FILM_S:
            plan.warnings.append(
                f"{len(recs)} flashes in {duration:g} s of film is {per_s:.1f} thunderclaps a second: a continuous roar. "
                + (
                    "Raise --glm-bin-s to thin them."
                    if o.flashes_from_glm
                    else "Thin the file (for example one flash per few data seconds per film second)."
                )
            )
    plan.label = label_rows(
        wind_retrieved,
        plan.thunder,
        Path(o.flashes).name if o.flashes else None,
        plan.draw_flashes,
    )
    return plan


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def record(plan: SoundPlan, audio: dict[str, Any], pcm_sha256: str, t0: datetime) -> dict[str, Any]:
    """The ``sound`` block of the ``.animate.json`` file, after the audio was rendered."""
    events = []
    flash_rel = [(f.time - t0).total_seconds() for f in plan.table]
    for th in audio.get("thunders", []):
        when = t0 + timedelta(seconds=float(th["tDataS"]))
        lat, lon = float(th["lat"]), float(th["lon"])
        type_source = "modelled" if th["source"] != "observed" else SYNTHETIC_TYPE
        if (
            flash_rel
        ):  # observed: the flash's own coordinates, not the ones recovered from the scene frame
            j = int(np.argmin(np.abs(np.array(flash_rel) - float(th["tDataS"]))))
            lat, lon = plan.flash_latlon[j]
            when = plan.flash_times[j]
            if plan.table[j].kind:
                type_source = "file"
        events.append(
            {
                "flash_time_utc": when.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "lat": round(lat, 4),
                "lon": round(lon, 4),
                "type": th["kind"],
                "type_source": type_source,
                "source": th["source"],
                "flash_clip_s": round(float(th["tFlash"]), 3),
                "thunder_arrives_clip_s": round(float(th["arrival"]), 3),
                "delay_clip_s": round(float(th["delay"]), 3),
                "delay_real_s": round(
                    float(th.get("delayRealS", float(th["delay"]) * audio["compress"])), 1
                ),
                "distance_km": round(float(th["distKm"]), 1),
            }
        )
    events.sort(key=lambda e: e["flash_clip_s"])
    peak = float(audio.get("peak", 0.0))
    lis = audio["listener"]
    out = plan.summary()
    out["listener"] = {
        **out["listener"],
        "mode": lis["mode"],
        "lat": round(float(lis["lat"]), 4),
        "lon": round(float(lis["lon"]), 4),
        "ear_height_m": lis["eyeM"],
        "frame": (
            "the camera's own position and heading"
            if lis["mode"] == "camera"
            else "x km east, y km north of the middle of the storm's track; the default place is 10 km to the right of the track, 4 km upwind of its middle; it faces the storm centre"
        ),
    }
    out["compress"] = audio["compress"]
    out["seed"] = audio["seed"]
    out["mix"]["thunder_db"] = round(float(audio["levelsDb"]["thunder"]), 2)
    out["mix"]["bed_db"] = round(float(audio["levelsDb"]["bed"]), 2)
    out["mix"]["ceiling"] = audio["ceiling"]
    out["wind"] = {
        "source": audio["windSource"],
        "min_ms": round(float(audio["windMs"]["min"]), 1),
        "max_ms": round(float(audio["windMs"]["max"]), 1),
        "share_of_time_using_proxy": round(float(audio["windProxyShare"]), 3),
        "ground_factor": SURFACE_WIND_FACTOR,
    }
    rl = audio["rainLevel"]
    hk = audio.get("rainHeightKm")
    out["wind"]["share_of_time_retrieved"] = round(retrieved_share(audio), 3)
    out["rain"] = {
        "level_min": round(float(rl["min"]), 3),
        "level_max": round(float(rl["max"]), 3),
        "heard": rain_heard(audio),
        "rule": "reflectivity at the lowest gridded level with a value over the listener (up to 2.5 km above the ground), level = (dBZ - 20) / 35 clipped to 0..1 (20 dBZ none, 55 dBZ full); loudness 0.3 x level^1.4; reflectivity is not a rain rate",
    }
    if "mean" in rl:
        out["rain"]["level_mean"] = round(float(rl["mean"]), 3)
        out["rain"]["share_of_time_heard"] = round(float(rl.get("heardShare", 0.0)), 3)
    if hk is not None:
        out["rain"]["echo_height_km"] = {
            "min": round(float(hk["min"]), 2),
            "max": round(float(hk["max"]), 2),
        }
    elif "rainHeightKm" in audio:
        out["rain"]["echo_height_km"] = None
    if not out["rain"]["heard"]:
        out["rain"]["warning"] = (
            "the rain level stayed at zero for the whole film: no echo of 20 dBZ or more over the listener; "
            "the rain is silent and the film's label says so"
        )
    if audio.get("thunderTiming"):
        out["thunder"]["timing"] = (
            "the real light-to-sound delay added in data time and mapped through the film clock"
            if audio["thunderTiming"] == "film clock"
            else f"the real light-to-sound delay divided by {audio['compress']:g}"
        )
    out["thunder"]["events"] = events
    out["audio"] = {
        "codec": "aac",
        "sample_rate_hz": audio["sampleRate"],
        "channels": 2,
        "duration_s": round(audio["frames"] / audio["sampleRate"], 3),
        "peak_dbfs": round(20 * math.log10(max(peak, 1e-9)), 2),
        "pcm_sha256": pcm_sha256,
    }
    out["limits"] = LIMITS
    return out
