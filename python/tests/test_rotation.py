"""Rotation products (issue 84), against known answers.

Known answers: a Rankine vortex (solid-body core of radius R, peak tangential speed V, so the
azimuthal shear in the core is the angular velocity V / R) seen by a radar at several ranges, with
noise; a vortex that moves at a known speed through a sequence of volumes; the running maximum of a
shear field. radarx does the per-volume science (LLSD shear, couplets), so those tests need it.
"""

from __future__ import annotations

import importlib.util
import math
from typing import ClassVar

import numpy as np
import pytest
import xarray as xr

from engine import plugins, rotation
from engine.plugins import run_module

from . import radar_data

HAS_RADARX = importlib.util.find_spec("radarx") is not None
needs_radarx = pytest.mark.skipif(not HAS_RADARX, reason="radarx not installed")

AZ = np.arange(0, 360, 0.5) + 0.25
RNG = 250.0 * np.arange(1, 401)  # 0.25 .. 100 km
SITE = {"latitude": 37.07, "longitude": -88.77, "altitude": 120.0}
V_MAX, CORE = (
    30.0,
    3000.0,
)  # peak tangential speed (m/s), core radius (m): shear V_MAX / CORE = 0.01 1/s
OMEGA = V_MAX / CORE


def rankine(
    x0: float,
    y0: float,
    v_max: float = V_MAX,
    core: float = CORE,
    noise: float = 0.0,
    seed: int = 0,
    n_gates: int = 400,
) -> np.ndarray:
    """Radial velocity (m/s, positive away) of a counter-clockwise Rankine vortex at (x0, y0) m."""
    a = np.radians(AZ)[:, None]
    rng = 250.0 * np.arange(1, n_gates + 1)
    x = rng[None, :] * np.sin(a)
    y = rng[None, :] * np.cos(a)
    dx, dy = x - x0, y - y0
    r = np.maximum(np.hypot(dx, dy), 1e-9)
    vt = np.where(r < core, v_max * r / core, v_max * core / r)
    out = (-vt * dy / r) * np.sin(a) + (vt * dx / r) * np.cos(a)
    if noise:
        out = out + np.random.default_rng(seed).normal(0.0, noise, out.shape)
    return out


def as_ds(vel: np.ndarray, t0: str = "2021-12-11T03:00:00") -> xr.Dataset:
    return xr.Dataset(
        {
            "VRADH": (("azimuth", "range"), vel.astype(np.float32)),
            "DBZH": (("azimuth", "range"), np.full(vel.shape, 30.0, dtype=np.float32)),
        },
        coords={
            "azimuth": AZ,
            "range": 250.0 * np.arange(1, vel.shape[1] + 1),
            "elevation": ("azimuth", np.full(AZ.size, 0.5)),
            "time": (
                "azimuth",
                np.datetime64(t0) + (np.arange(AZ.size) * 8).astype("timedelta64[ms]"),
            ),
        },
    )


def volume(vel: np.ndarray, t0: str, site: dict | None = None) -> xr.DataTree:
    ds = as_ds(vel, t0)
    ds["sweep_fixed_angle"] = ((), 0.5)
    ds["sweep_mode"] = ((), "azimuth_surveillance")
    ds["nyquist_velocity"] = ((), 40.0)
    root = xr.Dataset(coords={k: np.float64(v) for k, v in (site or SITE).items()})
    return xr.DataTree.from_dict({"/": root, "/sweep_0": ds})


# ---------------------------------------------------------------------------
# radarx: the azimuthal shear of a Rankine vortex at several ranges, with and without noise
# ---------------------------------------------------------------------------


@needs_radarx
class TestRankineShear:
    @staticmethod
    def shear(vel: np.ndarray) -> np.ndarray:
        import radarx.retrieve as rt

        out = rt.llsd(as_ds(vel), "VRADH")
        return out["azimuthal_shear"].transpose("azimuth", "range").values

    @pytest.mark.parametrize("range_km", [30.0, 60.0, 90.0, 120.0])
    def test_core_shear_is_the_angular_velocity(self, range_km: float) -> None:
        """The peak shear is the angular velocity V/R of the core to within 10 % and sits on the
        vortex, whatever the range (a core of 6 km diameter is wider than the window to 120 km)."""
        x0, y0 = 0.6 * range_km * 1e3, 0.8 * range_km * 1e3
        rng = 250.0 * np.arange(1, 641)  # 160 km
        sh = self.shear(rankine(x0, y0, n_gates=640))
        i, j = np.unravel_index(np.nanargmax(sh), sh.shape)
        peak = float(np.nanmax(sh))
        px, py = rng[j] * math.sin(math.radians(AZ[i])), rng[j] * math.cos(math.radians(AZ[i]))
        assert 0.93 * OMEGA < peak < 1.10 * OMEGA
        assert math.hypot(px - x0, py - y0) < CORE + 500.0  # the peak lies in the core

    def test_close_to_the_radar_the_shear_reads_high(self) -> None:
        """At 15 km the window spans a large angle and the plane fit is biased: about 20 % high,
        stated in the docs. The test pins the size of the bias so a change is noticed."""
        sh = self.shear(rankine(0.6 * 15e3, 0.8 * 15e3))
        assert 1.05 * OMEGA < float(np.nanmax(sh)) < 1.30 * OMEGA

    @pytest.mark.parametrize("range_km", [30.0, 60.0])
    def test_noise(self, range_km: float) -> None:
        """1 m/s of velocity noise: the peak stays within 25 % of V/R, and a field of noise alone
        stays far below the couplet threshold of the module (0.012 1/s)."""
        x0, y0 = 0.6 * range_km * 1e3, 0.8 * range_km * 1e3
        sh = self.shear(rankine(x0, y0, noise=1.0, seed=2))
        assert 0.8 * OMEGA < float(np.nanmax(sh)) < 1.25 * OMEGA
        quiet = self.shear(rankine(0.0, 0.0, v_max=0.0, noise=1.0, seed=3))
        assert float(np.nanmax(np.abs(quiet))) < 0.012

    def test_anticyclonic_vortex_is_negative(self) -> None:
        sh = self.shear(rankine(30e3, 30e3, v_max=-V_MAX))
        # the flow outside the core (v ~ 1/r) has shear of the opposite sign, weaker than the core's
        assert float(np.nanmin(sh)) < -0.9 * OMEGA and float(np.nanmax(sh)) < 0.6 * OMEGA

    def test_a_uniform_wind_shows_as_shear_near_the_radar(self) -> None:
        """Known answer: a uniform wind U has radial velocity -U cos(az - from), whose azimuthal
        shear is U sin(az - from) / r. It is not rotation, falls with range, and is why the
        module leaves out the gates closer than 5 km. Beyond 30 km it is below 0.0007 1/s here."""
        a = np.radians(AZ)[:, None]
        sh = self.shear(-20.0 * np.cos(a - np.radians(250.0)) + np.zeros((1, RNG.size)))
        for r_km in (10.0, 30.0, 60.0):
            j = int(r_km * 4) - 1
            assert float(np.nanmax(sh[:, j])) == pytest.approx(20.0 / (r_km * 1e3), rel=0.25)
        assert float(np.nanmax(np.abs(sh[:, 119:]))) < 20.0 / 30e3 * 1.3


# ---------------------------------------------------------------------------
# The swath and the track link
# ---------------------------------------------------------------------------


class TestSwath:
    def polar_bump(self, x0: float, y0: float, peak: float) -> np.ndarray:
        a = np.radians(AZ)[:, None]
        x = RNG[None, :] * np.sin(a)
        y = RNG[None, :] * np.cos(a)
        return peak * np.exp(-((x - x0) ** 2 + (y - y0) ** 2) / (2 * 1500.0**2))

    def test_running_maximum(self) -> None:
        axes = rotation.swath_grid(50000.0, 500.0)
        swath = np.full((axes[1].size, axes[0].size), np.nan)
        for x0, peak in ((10e3, 0.02), (15e3, 0.03), (10e3, 0.01)):
            rotation.accumulate_swath(swath, self.polar_bump(x0, 5e3, peak), AZ, RNG, 0.0, axes)
        ix = int(np.argmin(np.abs(axes[0] - 10e3)))
        iy = int(np.argmin(np.abs(axes[1] - 5e3)))
        assert swath[iy, ix] == pytest.approx(
            0.02, rel=0.02
        )  # the later, weaker 0.01 did not lower it
        jx = int(np.argmin(np.abs(axes[0] - 15e3)))
        assert swath[iy, jx] == pytest.approx(0.03, rel=0.02)
        far = int(np.argmin(np.abs(axes[0] - 40e3)))
        assert swath[iy, far] < 1e-4  # nothing was there

    def test_cells_outside_the_swept_area_stay_empty(self) -> None:
        axes = rotation.swath_grid(150000.0, 1000.0)  # grid larger than the 100 km sweep
        swath = np.full((axes[1].size, axes[0].size), np.nan)
        rotation.accumulate_swath(swath, self.polar_bump(0, 20e3, 0.02), AZ, RNG, 0.0, axes)
        i = int(np.argmin(np.abs(axes[1] - 140e3)))
        assert np.isnan(swath[i, len(axes[0]) // 2])

    def test_sign_selection(self) -> None:
        axes = rotation.swath_grid(50000.0, 500.0)
        bump = -self.polar_bump(10e3, 10e3, 0.02)
        for sign in ("cyclonic", "anticyclonic", "both"):
            swath = np.full((axes[1].size, axes[0].size), np.nan)
            rotation.accumulate_swath(swath, bump, AZ, RNG, 0.0, axes, sign)
            peak = float(np.nanmax(swath))
            assert (peak > 0.019) == (sign != "cyclonic"), sign
        with pytest.raises(ValueError):
            rotation.accumulate_swath(swath, bump, AZ, RNG, 0.0, axes, "sideways")


def moving(
    t_s: float, speed: float = 15.0, x0: float = 20e3, y0: float = 30e3, heading: float = 90.0
):
    """(x, y) of a vortex moving toward ``heading`` degrees at ``speed`` m/s."""
    h = math.radians(heading)
    return x0 + speed * t_s * math.sin(h), y0 + speed * t_s * math.cos(h)


def couplet(x: float, y: float, shear: float = 0.02) -> rotation.Couplet:
    return rotation.Couplet(
        x, y, math.degrees(math.atan2(x, y)) % 360, math.hypot(x, y), shear, 40.0, 2.0
    )


class TestLinkTracks:
    def test_one_vortex_one_track_with_its_motion(self) -> None:
        frames = [(t, [couplet(*moving(t))]) for t in (0, 300, 600, 900, 1200)]
        (tr,) = rotation.link_tracks(frames)
        speed, heading = tr.motion()
        assert len(tr.couplets) == 5 and tr.gaps_s == [300.0] * 4
        assert speed == pytest.approx(15.0, rel=0.01) and heading == pytest.approx(90.0, abs=0.5)
        assert tr.length_km() == pytest.approx(18.0, rel=0.01)

    def test_two_vortices_stay_apart(self) -> None:
        frames = [
            (t, [couplet(*moving(t)), couplet(*moving(t, x0=-40e3, y0=-20e3, heading=45.0), 0.03)])
            for t in (0, 300, 600, 900)
        ]
        tracks = rotation.link_tracks(frames)
        assert len(tracks) == 2 and all(len(t.couplets) == 4 for t in tracks)
        assert tracks[0].peak.shear == pytest.approx(0.03)  # strongest first

    def test_a_missing_volume_is_a_gap_not_a_break(self) -> None:
        frames = [(t, [couplet(*moving(t))]) for t in (0, 300, 900, 1200)]  # 600 missing
        (tr,) = rotation.link_tracks(frames)
        assert tr.gaps_s == [300.0, 600.0, 300.0]

    def test_a_jump_beyond_the_speed_limit_is_not_linked(self) -> None:
        frames = [(0, [couplet(20e3, 30e3)]), (300, [couplet(20e3 + 30e3, 30e3)])]  # 100 m/s
        assert rotation.link_tracks(frames, max_speed_ms=40.0) == []
        assert len(rotation.link_tracks(frames, max_speed_ms=120.0)) == 1

    def test_min_observations(self) -> None:
        frames = [(t, [couplet(*moving(t))]) for t in (0, 300)]
        assert len(rotation.link_tracks(frames, min_obs=2)) == 1
        assert rotation.link_tracks(frames, min_obs=3) == []


# ---------------------------------------------------------------------------
# The module on synthetic volumes
# ---------------------------------------------------------------------------


@pytest.fixture
def discovered():
    saved = (dict(plugins.MODULES), list(plugins.LOAD_ERRORS))
    plugins.discover_modules()
    yield
    plugins.MODULES.clear()
    plugins.MODULES.update(saved[0])
    plugins.LOAD_ERRORS[:] = saved[1]


class FakeReader:
    """Stands in for RadarReader: hands out the synthetic volumes by file name."""

    trees: ClassVar[dict[str, xr.DataTree]] = {}

    def __init__(self) -> None:
        self.datatree = None

    def open_file(self, path: str) -> dict:
        name = path.rsplit("/", 1)[-1]
        if name not in self.trees:
            raise ValueError(f"cannot open {name}")
        self.datatree = self.trees[name]
        return {}

    def get_sweep_dataset(self, i: int, *, mask: bool = True, only=None) -> xr.Dataset:
        return self.datatree[f"sweep_{i}"].to_dataset()

    def close(self) -> None:
        self.datatree = None


def run_track(tmp_path, monkeypatch, trees: dict[str, xr.DataTree], **params):
    """Run the module on the first volume with the others listed as files."""
    import engine.reader as reader_mod

    names = list(trees)
    FakeReader.trees = trees
    monkeypatch.setattr(reader_mod, "RadarReader", FakeReader)
    for name in names[1:]:
        (tmp_path / name).write_text("x")  # the module only checks that the file exists
    first = trees[names[0]]
    return run_module(
        "radarx_rotation_track",
        first,
        0,
        {"files": str(tmp_path), **params},
        get_sweep_dataset=lambda i: first[f"sweep_{i}"].to_dataset(),
        return_fields=True,
    )


def block(payload: dict, kind: str, title: str | None = None) -> dict:
    found = [b for b in payload["blocks"] if b["type"] == kind]
    if title is not None:
        found = [b for b in found if title in b.get("title", "")]
    assert found, [b["type"] for b in payload["blocks"]]
    return found[0]


def scalar(payload: dict, label: str):
    return next(i["value"] for i in block(payload, "scalars")["items"] if i["label"] == label)


@needs_radarx
class TestModule:
    TIMES: ClassVar[list[int]] = [0, 300, 600, 900, 1200]

    def trees(self, **kw) -> dict[str, xr.DataTree]:
        out = {}
        for k, t in enumerate(self.TIMES):
            x, y = moving(t, **kw)
            stamp = np.datetime64("2021-12-11T03:00:00") + np.timedelta64(t, "s")
            out[f"V{k}"] = volume(rankine(x, y), str(stamp))
        return out

    def test_moving_vortex_gives_one_track_with_known_motion_and_a_swath(
        self, tmp_path, discovered, monkeypatch
    ) -> None:
        payload, fields = run_track(tmp_path, monkeypatch, self.trees(), threshold=0.008)
        assert scalar(payload, "Volumes used") == 5
        assert scalar(payload, "Tracks (3+ volumes)") == 1
        (row,) = block(payload, "table", "Rotation tracks")["rows"]
        assert row["obs"] == 5
        assert row["speed"] == pytest.approx(15.0, abs=1.5)
        assert row["heading"] == pytest.approx(90, abs=8)
        assert row["peak_shear"] == pytest.approx(OMEGA, rel=0.25)
        assert row["max_gap_min"] == pytest.approx(5.0)
        assert scalar(payload, "Longest gap between volumes") == pytest.approx(5.0)
        assert 0.8 * OMEGA < scalar(payload, "Swath peak shear") < 1.3 * OMEGA
        # the swath on the sweep grid: high along the path, empty-ish elsewhere
        sw = fields["Rotation track swath (RETRIEVED)"]
        a, g = np.radians(AZ)[:, None], RNG[None, :]
        x, y = g * np.sin(a), g * np.cos(a)
        on_path = (np.abs(y - 30e3) < 1000) & (x > 20e3) & (x < 38e3)
        off_path = (np.abs(y - 30e3) > 12e3) | (x < 5e3)
        assert np.nanmean(sw[on_path]) > 0.6 * OMEGA
        assert np.nanmax(np.abs(sw[off_path & np.isfinite(sw)])) < 0.5 * OMEGA

    def test_provenance_and_limits_are_stated(self, tmp_path, discovered, monkeypatch) -> None:
        payload, _ = run_track(tmp_path, monkeypatch, self.trees(), threshold=0.008)
        assert "RETRIEVED" in scalar(payload, "Provenance")
        text = block(payload, "text", "About this product")["body"]
        for phrase in (
            "not a tornado warning",
            "Sampling gaps",
            "Range-dependent resolution",
            "Aliasing",
            "has not been measured",
        ):
            assert phrase in text, phrase
        assert "Rotation tracks" in block(payload, "overlay")["title"]

    def test_a_noise_only_sequence_makes_no_track(self, tmp_path, discovered, monkeypatch) -> None:
        trees = {}
        for k, t in enumerate(self.TIMES):
            stamp = str(np.datetime64("2021-12-11T03:00:00") + np.timedelta64(t, "s"))
            trees[f"V{k}"] = volume(rankine(0, 0, v_max=0.0, noise=1.0, seed=k), stamp)
        payload, _ = run_track(tmp_path, monkeypatch, trees)
        assert scalar(payload, "Tracks (3+ volumes)") == 0
        assert (scalar(payload, "Swath peak shear") or 0.0) < 0.012

    def test_needs_other_volumes_and_one_radar(self, tmp_path, discovered, monkeypatch) -> None:
        trees = self.trees()
        with pytest.raises(Exception, match=r"Other volumes|at least two"):
            run_module(
                "radarx_rotation_track",
                trees["V0"],
                0,
                {"files": ""},
                get_sweep_dataset=lambda i: trees["V0"][f"sweep_{i}"].to_dataset(),
            )
        # a volume of another radar and a repeat of a scan time are skipped, and said so
        other = {"latitude": 36.0, "longitude": -80.0, "altitude": 100.0}
        trees["V1"] = volume(rankine(*moving(300)), "2021-12-11T03:05:00", site=other)
        trees["V2"] = volume(rankine(*moving(600)), "2021-12-11T03:00:00")  # same time as V0
        payload, _ = run_track(tmp_path, monkeypatch, trees)
        text = block(payload, "text", "About this product")["body"]
        assert "different radar position" in text and "same scan time" in text
        assert scalar(payload, "Volumes used") == 3


@needs_radarx
def test_module_is_listed_and_documented(discovered) -> None:
    spec = plugins.MODULES["radarx_rotation_track"]
    assert spec.scope == "volume" and spec.category == "Hazards"
    names = {p["name"] for p in spec.params}
    assert {"files", "threshold", "window_across_m", "min_track_volumes", "max_speed_ms"} <= names
    assert "tornado" in spec.description.lower() and "never a tornado warning" in spec.description


# ---------------------------------------------------------------------------
# Real data: the 2021-12-11 Mayfield volumes (a smoke test, no skill claimed)
# ---------------------------------------------------------------------------


@needs_radarx
def test_mayfield_kpah_volumes_run_end_to_end(discovered) -> None:
    """Five KPAH volumes (03:05-03:30 UTC) go through the whole module: it runs, reports its
    sampling gaps, labels the result RETRIEVED and gives a swath. No claim is made about whether
    the tracks are tornadic: the detection skill of this product has not been measured."""
    from engine.reader import RadarReader

    names = ["030505", "031104", "031759", "032349", "032939"]
    paths = [radar_data.event_volume("KPAH", n) for n in names]
    if any(p is None for p in paths):
        pytest.skip("the tutorial KPAH volumes are not available (tests/radar_data.py)")
    reader = RadarReader()
    reader.open_file(str(paths[0]))
    try:
        payload, fields = run_module(
            "radarx_rotation_track",
            reader.datatree,
            1,
            {"files": ",".join(str(p) for p in paths[1:])},
            get_sweep_dataset=reader.get_sweep_dataset,
            return_fields=True,
        )
    finally:
        reader.close()
    assert scalar(payload, "Volumes used") == 5
    assert "RETRIEVED" in scalar(payload, "Provenance")
    assert 5.0 < scalar(payload, "Longest gap between volumes") < 8.0
    sw = fields["Rotation track swath (RETRIEVED)"]
    assert sw.ndim == 2 and np.isfinite(sw).mean() > 0.2
    assert 0.0 < float(np.nanmax(sw)) < 0.2  # a physical size, not garbage
    assert "Sampling gaps" in block(payload, "text", "About this product")["body"]
