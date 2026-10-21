"""Tests of the sound of ``rxd animate --sound`` (engine/animate_sound.py and its wiring): option parsing,
the flash table (CSV) and the thinning of GOES GLM flashes, the thunder-source rules, the labels, the
record written next to the film, the failure when something needed is missing, and, only when Chrome and
the built app are available, one end-to-end film with an AAC track (decoded and checked)."""

# ruff: noqa: E501

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from engine import animate
from engine import animate_data as data
from engine import animate_render as render
from engine import animate_sound as snd
from engine.animate_plan import AnimateConfigError
from tests.test_animate import SyntheticGridder, _chrome_ready, _e2e_options, run_cli, series

UTC = timezone.utc


@pytest.fixture
def case(tmp_path) -> Path:
    root = tmp_path / "case"
    series(root, {"KAAA": 0, "KBBB": 50}, 4)
    return root


@pytest.fixture
def synthetic(monkeypatch):
    """The engine's gridder replaced by a synthetic storm moving at a known speed."""
    monkeypatch.setattr(data, "EngineGridder", lambda log=None: SyntheticGridder())


def write_flash_csv(path: Path, rows: list[tuple[str, float, float, str, str]]) -> Path:
    head = "time_utc,lat,lon,type,energy\n"
    path.write_text(head + "".join(",".join(str(v) for v in r) + "\n" for r in rows))
    return path


def write_glm(
    path: Path, product_time: datetime, flashes: list[tuple[float, float, float, float, int]]
) -> Path:
    """A GLM L2 LCFA-like netCDF: ``flashes`` are (offset s, lat, lon, energy, quality flag). The time
    offsets are stored as "seconds since the product time", so xarray decodes them to absolute times,
    as it does for the real files."""
    import xarray as xr

    t0 = product_time.strftime("%Y-%m-%d %H:%M:%S")
    off = np.array([f[0] for f in flashes], dtype="f8")
    ds = xr.Dataset(
        {
            "flash_time_offset_of_first_event": (
                "number_of_flashes",
                off,
                {"units": f"seconds since {t0}"},
            ),
            "flash_lat": ("number_of_flashes", np.array([f[1] for f in flashes], dtype="f4")),
            "flash_lon": ("number_of_flashes", np.array([f[2] for f in flashes], dtype="f4")),
            "flash_energy": ("number_of_flashes", np.array([f[3] for f in flashes], dtype="f4")),
            "flash_quality_flag": (
                "number_of_flashes",
                np.array([f[4] for f in flashes], dtype="i2"),
            ),
            "product_time": ((), np.datetime64(product_time.replace(tzinfo=None), "ns")),
        },
        attrs={"time_coverage_start": product_time.strftime("%Y-%m-%dT%H:%M:%S.0Z")},
    )
    ds.to_netcdf(path)
    return path


def plan_json(capsys, case, tmp_path, *extra) -> dict:
    code, out, err = run_cli(
        capsys,
        "animate",
        "-i",
        str(case),
        "--no-render",
        "--json",
        "--cache-dir",
        str(tmp_path / "c"),
        *extra,
    )
    assert code == 0, err
    return json.loads(out)


# ---------------------------------------------------------------------------
# options and defaults
# ---------------------------------------------------------------------------


class TestOptions:
    def test_listener(self):
        assert snd.parse_listener("10,-4") == (10.0, -4.0)
        assert snd.parse_listener(" 0 , 2.5 ") == (0.0, 2.5)
        for bad in ("10", "a,b", "1,2,3", "", "nan,1"):
            with pytest.raises(AnimateConfigError, match="--sound-listener"):
                snd.parse_listener(bad)

    def test_default_compress_is_the_timelapse_factor(self):
        assert snd.auto_compress(24.0) == 24.0
        assert snd.auto_compress(102.34) == 102.3
        assert snd.auto_compress(0.4) == 1.0  # never slower than the real physics

    def test_default_thinning_bin_is_about_one_thunderclap_per_film_second(self):
        assert snd.auto_bin_s(102) == 102.0
        assert snd.auto_bin_s(2) == 10.0 and snd.auto_bin_s(5000) == 300.0

    def test_help_lists_the_sound_options(self, capsys):
        code, out, _ = run_cli(capsys, "animate", "--help")
        assert code == 0
        for opt in (
            "--sound",
            "--sound-listener",
            "--sound-compress",
            "--sound-seed",
            "--flashes",
            "--flashes-from-glm",
            "--glm-radius-km",
            "--glm-bin-s",
        ):
            assert opt in out
        assert "not a recording" in out

    def test_sound_options_need_sound(self, capsys, case, tmp_path):
        code, _, err = run_cli(
            capsys, "animate", "-i", str(case), "--no-render", "--sound-listener", "5,5"
        )
        assert code == 2 and "--sound-listener" in err and "add --sound" in err

    def test_one_thunder_source_only(self, capsys, case, tmp_path):
        f = write_flash_csv(tmp_path / "f.csv", [("2021-12-11T02:05:00Z", 36.0, -89.0, "", "")])
        code, _, err = run_cli(
            capsys,
            "animate",
            "-i",
            str(case),
            "--no-render",
            "--sound",
            "--flashes",
            str(f),
            "--lightning-model",
        )
        assert code == 2 and "MODELLED" in err and "OBSERVED" in err
        code, _, err = run_cli(
            capsys,
            "animate",
            "-i",
            str(case),
            "--no-render",
            "--sound",
            "--flashes",
            str(f),
            "--flashes-from-glm",
            str(tmp_path),
        )
        assert code == 2 and "not both" in err

    def test_sound_needs_an_mp4_not_a_folder(self, capsys, case, tmp_path):
        code, _, err = run_cli(
            capsys,
            "animate",
            "-i",
            str(case),
            "--no-render",
            "--sound",
            "-o",
            str(tmp_path / "frames") + "/",
        )
        assert code == 2 and "MP4" in err

    def test_numbers_are_checked(self, capsys, case):
        for args, needle in (
            (("--sound-compress", "0"), "--sound-compress"),
            (("--sound-seed", "-1"), "--sound-seed"),
            (("--glm-bin-s", "5"), "go with --flashes-from-glm"),
        ):
            code, _, err = run_cli(
                capsys, "animate", "-i", str(case), "--no-render", "--sound", *args
            )
            assert code == 2 and needle in err, args

    def test_no_sound_means_no_sound_block_and_an_unchanged_page(
        self, capsys, case, tmp_path, synthetic
    ):
        doc = plan_json(capsys, case, tmp_path)
        assert "sound" not in doc
        o = animate.AnimateOptions(files=[])
        cp = animate.cam.resolve_camera(animate.cam.load_camera(None, None), 36.0, 0.0)
        sp = animate.plan_inputs(
            animate.AnimateOptions(files=[str(p) for p in sorted(case.glob("*/K*"))])
        )
        s = animate.page_settings(o, cp, sp, 30, 36.0)
        assert s["audio"] is False and "sound" not in s


# ---------------------------------------------------------------------------
# flash tables
# ---------------------------------------------------------------------------


class TestFlashTable:
    def test_read_with_all_columns(self, tmp_path):
        f = write_flash_csv(
            tmp_path / "f.csv",
            [
                ("2021-12-11T02:05:10.5Z", 36.1, -88.9, "cg", "4"),
                ("2021-12-11T02:05:00Z", 36.2, -88.8, "IC", "1"),
                ("2021-12-11 02:06:00", 36.3, -88.7, "", ""),
            ],
        )
        fl = snd.read_flash_table(f)
        assert [x.kind for x in fl] == ["IC", "CG", None]  # sorted by time; type upper-cased
        assert fl[0].time == datetime(2021, 12, 11, 2, 5, 0, tzinfo=UTC)
        assert fl[1].time == datetime(2021, 12, 11, 2, 5, 10, 500000, tzinfo=UTC)
        assert fl[2].time.tzinfo is not None  # no zone means UTC
        assert fl[1].energy > fl[0].energy  # the stronger one is stronger (relative, 0.2 to 1)

    def test_header_may_be_in_any_case_and_extra_columns_are_ignored(self, tmp_path):
        p = tmp_path / "f.csv"
        p.write_text("Time_UTC,LAT,Lon,note\n2021-12-11T02:05:00Z,36,-89,hello\n")
        assert len(snd.read_flash_table(p)) == 1

    @pytest.mark.parametrize(
        "text,needle",
        [
            ("time,lat,lon\n2021-12-11T02:00:00Z,36,-89\n", "time_utc"),
            ("time_utc,lat,lon\nyesterday,36,-89\n", "not a UTC time"),
            ("time_utc,lat,lon\n2021-12-11T02:00:00Z,abc,-89\n", "must be numbers"),
            ("time_utc,lat,lon\n2021-12-11T02:00:00Z,136,-89\n", "not a place on Earth"),
            ("time_utc,lat,lon,type\n2021-12-11T02:00:00Z,36,-89,XX\n", "CG or IC"),
            ("time_utc,lat,lon\n", "no flashes"),
        ],
    )
    def test_errors_name_the_problem(self, tmp_path, text, needle):
        p = tmp_path / "f.csv"
        p.write_text(text)
        with pytest.raises(AnimateConfigError, match=needle):
            snd.read_flash_table(p)

    def test_missing_file(self, tmp_path):
        with pytest.raises(AnimateConfigError, match="file not found"):
            snd.read_flash_table(tmp_path / "nope.csv")

    def test_write_then_read_round_trip(self, tmp_path):
        from engine.lightning_obs import ObservedFlash

        fl = [
            ObservedFlash(
                datetime(2021, 12, 11, 2, 5, 0, 250000, tzinfo=UTC),
                36.1234,
                -88.9876,
                None,
                0.7,
                kind="CG",
            ),
            ObservedFlash(datetime(2021, 12, 11, 2, 6, 0, tzinfo=UTC), 36.5, -88.5, None, 0.3),
        ]
        snd.write_flash_table(tmp_path / "o.csv", fl, [3.2, 8.0])
        text = (tmp_path / "o.csv").read_text().splitlines()
        assert text[0] == "time_utc,lat,lon,type,energy,distance_to_storm_km"
        back = snd.read_flash_table(tmp_path / "o.csv")
        assert [b.kind for b in back] == ["CG", None]
        assert back[0].time == fl[0].time and back[0].lat == pytest.approx(36.1234)


class TestThinning:
    def test_strongest_of_every_bin_in_time_order(self):
        t = np.array([5, 20, 95, 101, 150, 199, 250, 260.0])
        e = np.array([1, 9, 3, 2, 8, 7, 4, 6.0])
        assert snd.thin_to_strongest(t, e, bin_s=100) == [1, 4, 7]  # bins 0-100, 100-200, 200-300

    def test_a_tie_goes_to_the_earlier_flash_and_unordered_input_is_fine(self):
        t = np.array([30.0, 10.0, 20.0])
        e = np.array([5.0, 5.0, 5.0])
        assert snd.thin_to_strongest(t, e, bin_s=100) == [1]

    def test_missing_energy_loses(self):
        t = np.array([10.0, 20.0])
        assert snd.thin_to_strongest(t, np.array([np.nan, 0.1]), bin_s=100) == [1]

    def test_bins_start_at_the_start_time(self):
        t = np.array([10.0, 60.0, 130.0])
        assert snd.thin_to_strongest(t, np.array([1.0, 2.0, 3.0]), bin_s=100, t_start=50.0) == [
            0,
            2,
        ]  # bins -1 | 0 (60 s and 130 s: the stronger)

    def test_rejects_a_bad_bin(self):
        with pytest.raises(AnimateConfigError):
            snd.thin_to_strongest(np.array([1.0]), np.array([1.0]), bin_s=0)


class TestGlm:
    def test_reads_decoded_times_quality_and_energy(self, tmp_path):
        import xarray as xr

        from engine.lightning_obs import flashes_from_glm_dataset

        p = write_glm(
            tmp_path / "OR_GLM-L2-LCFA_G16_s20213450225000_x.nc",
            datetime(2021, 12, 11, 2, 25, 0, tzinfo=UTC),
            [(0.5, 36.4, -89.3, 1e-13, 0), (12.25, 36.5, -89.2, 5e-13, 1)],
        )
        with xr.open_dataset(p) as ds:
            assert np.issubdtype(
                ds["flash_time_offset_of_first_event"].dtype, np.datetime64
            )  # as the real files decode
            fl = flashes_from_glm_dataset(ds)
        assert fl[0].time == datetime(2021, 12, 11, 2, 25, 0, 500000, tzinfo=UTC)
        assert fl[1].time == datetime(2021, 12, 11, 2, 25, 12, 250000, tzinfo=UTC)
        assert [f.quality for f in fl] == [0, 1]
        assert fl[1].energy_raw == pytest.approx(5e-13, rel=1e-4) and fl[1].energy > fl[0].energy

    def test_float_second_offsets_still_work(self):
        import xarray as xr

        from engine.lightning_obs import flashes_from_glm_dataset

        ds = xr.Dataset(
            {
                "flash_time_offset_of_first_event": ("n", np.array([1.5, 3.0])),
                "flash_lat": ("n", np.array([36.0, 36.1])),
                "flash_lon": ("n", np.array([-89.0, -89.1])),
                "product_time": ((), np.datetime64("2021-12-11T02:00:00", "ns")),
            }
        )
        fl = flashes_from_glm_dataset(ds)
        assert fl[0].time == datetime(2021, 12, 11, 2, 0, 1, 500000, tzinfo=UTC)

    def test_files_from_a_folder_a_file_and_a_pattern(self, tmp_path):
        d = tmp_path / "glm" / "day"
        d.mkdir(parents=True)
        (d / "a.nc").write_bytes(b"x")
        (d / "b.nc").write_bytes(b"x")
        (d / "notes.txt").write_text("x")
        assert [p.name for p in snd.glm_files([str(tmp_path / "glm")])] == ["a.nc", "b.nc"]
        assert [p.name for p in snd.glm_files([str(d / "a.nc"), str(d / "*.nc")])] == [
            "a.nc",
            "b.nc",
        ]
        with pytest.raises(AnimateConfigError, match="nothing is downloaded"):
            snd.glm_files([str(tmp_path / "empty")])

    def test_the_thinning_keeps_the_strongest_good_flash_near_the_storm(
        self, capsys, case, tmp_path, synthetic
    ):
        base = plan_json(capsys, case, tmp_path)
        track = base["storm"]["track"]
        t_first = datetime.strptime(track[0]["time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)

        def at(i, dlat=0.0, dlon=0.0):
            r = track[i]
            return r["lat"] + dlat, r["lon"] + dlon

        step_s = [
            (
                datetime.strptime(r["time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) - t_first
            ).total_seconds()
            for r in track
        ]
        glm = tmp_path / "glm"
        glm.mkdir()
        # (offset from the first step in s, lat, lon, energy, quality)
        la0, lo0 = at(0)
        la1, lo1 = at(1, 0.05, 0.0)  # about 5.5 km from the storm centre of step 1
        flashes = [
            (step_s[0] + 5, la0, lo0, 1.0, 0),  # near, weak
            (step_s[0] + 50, la0 + 0.02, lo0, 9.0, 0),  # near, the strongest of its bin
            (step_s[0] + 60, la0, lo0, 99.0, 1),  # near but a bad quality flag: never used
            (step_s[0] + 70, la0 + 2.0, lo0, 99.0, 0),  # 220 km away: outside the radius
            (step_s[1] + 5, la1, lo1, 4.0, 0),  # the next bin
            (step_s[-1] + 5000, la0, lo0, 50.0, 0),  # after the film
        ]
        write_glm(glm / "OR_GLM-L2-LCFA_G16_s1.nc", t_first, [(f[0], *f[1:]) for f in flashes])
        doc = plan_json(
            capsys,
            case,
            tmp_path,
            "--sound",
            "--flashes-from-glm",
            str(glm),
            "--glm-bin-s",
            str(step_s[1] - 1),
        )
        sel = doc["sound"]["thunder"]["selection"]
        assert sel["flashes_read"] == 6 and sel["good_quality"] == 5 and sel["in_time_window"] == 4
        assert sel["within_radius"] == 3 and sel["radius_km"] == 30.0
        assert sel["bin_s"] == step_s[1] - 1 and sel["selected"] == 2
        assert (
            doc["sound"]["thunder"]["source"] == "observed_glm"
            and doc["sound"]["thunder"]["flashes"] == 2
        )
        assert "OBSERVED GLM flashes" in doc["sound"]["label"][1]

    def test_no_flash_near_the_storm_is_an_error_with_the_fix(
        self, capsys, case, tmp_path, synthetic
    ):
        glm = tmp_path / "glm"
        glm.mkdir()
        t0 = datetime(2021, 12, 11, 2, 0, 0, tzinfo=UTC)
        write_glm(glm / "g.nc", t0, [(10.0, 50.0, 10.0, 1.0, 0)])
        code, _, err = run_cli(
            capsys,
            "animate",
            "-i",
            str(case),
            "--no-render",
            "--cache-dir",
            str(tmp_path / "c"),
            "--sound",
            "--flashes-from-glm",
            str(glm),
        )
        assert code == 2 and "no good GLM flash within 30 km" in err and "--glm-radius-km" in err


# ---------------------------------------------------------------------------
# the plan: sources, compression, labels
# ---------------------------------------------------------------------------


class TestPlan:
    def test_thunder_none_by_default_and_the_label_says_so(self, capsys, case, tmp_path, synthetic):
        doc = plan_json(capsys, case, tmp_path, "--sound")
        s = doc["sound"]
        assert s["thunder"]["source"] == "none" and s["thunder"]["flashes"] == 0
        assert s["label"][1].startswith("no thunder") and "virtual" in s["label"][1]
        assert s["label"][0].startswith("Sound is SYNTHESISED, not a recording:")
        assert s["synthesised"] is True and s["recording"] is False
        assert s["listener"]["default"] is True and s["seed"] == 7

    def test_default_compress_follows_the_timelapse_and_can_be_set(
        self, capsys, case, tmp_path, synthetic
    ):
        doc = plan_json(capsys, case, tmp_path, "--sound")
        lapse = doc["frames"]["timelapse"]
        assert doc["sound"]["compress"] == pytest.approx(round(lapse, 1))
        assert "automatic" in doc["sound"]["compress_source"]
        doc2 = plan_json(
            capsys,
            case,
            tmp_path,
            "--sound",
            "--sound-compress",
            "8",
            "--sound-seed",
            "3",
            "--sound-listener",
            "10,-4",
        )
        s = doc2["sound"]
        assert (
            s["compress"] == 8.0 and s["compress_source"] == "--sound-compress" and s["seed"] == 3
        )
        assert s["listener"]["requested_km_east_north_of_track_middle"] == [10.0, -4.0]

    def test_modelled_flashes_label(self, capsys, case, tmp_path, synthetic):
        doc = plan_json(capsys, case, tmp_path, "--sound", "--lightning-model")
        assert doc["sound"]["thunder"]["source"] == "modelled"
        assert "MODELLED flashes (not observed)" in doc["sound"]["label"][1]

    def test_flash_file_source_is_not_called_observed_glm(self, capsys, case, tmp_path, synthetic):
        base = plan_json(capsys, case, tmp_path)
        r0, r1 = base["storm"]["track"][0], base["storm"]["track"][1]
        f = write_flash_csv(
            tmp_path / "my_flashes.csv",
            [
                (r0["time"], r0["lat"], r0["lon"], "CG", "2"),
                (r1["time"], r1["lat"], r1["lon"], "", "5"),
                ("2030-01-01T00:00:00Z", 36.0, -89.0, "", ""),
            ],
        )
        doc = plan_json(capsys, case, tmp_path, "--sound", "--flashes", str(f))
        s = doc["sound"]
        assert s["thunder"]["source"] == "observed_file" and s["thunder"]["flashes"] == 2
        assert (
            s["thunder"]["selection"]["read"] == 3
            and s["thunder"]["selection"]["in_time_window"] == 2
        )
        assert "my_flashes.csv" in s["label"][1] and "OBSERVED GLM" not in s["label"][1]

    def test_a_file_whose_flashes_are_all_outside_the_film_is_an_error(
        self, capsys, case, tmp_path, synthetic
    ):
        f = write_flash_csv(tmp_path / "late.csv", [("2030-01-01T00:00:00Z", 36.0, -89.0, "", "")])
        code, _, err = run_cli(
            capsys,
            "animate",
            "-i",
            str(case),
            "--no-render",
            "--cache-dir",
            str(tmp_path / "c"),
            "--sound",
            "--flashes",
            str(f),
        )
        assert code == 2 and "falls within the film" in err

    def test_dense_flashes_get_a_warning(self, capsys, case, tmp_path, synthetic):
        base = plan_json(capsys, case, tmp_path)
        r0 = base["storm"]["track"][0]
        t0 = datetime.strptime(r0["time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        rows = [
            (
                (t0 + timedelta(seconds=2 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                r0["lat"],
                r0["lon"],
                "",
                "",
            )
            for i in range(200)
        ]
        doc = plan_json(
            capsys,
            case,
            tmp_path,
            "--sound",
            "--flashes",
            str(write_flash_csv(tmp_path / "d.csv", rows)),
        )
        assert any("continuous roar" in w for w in doc["warnings"])

    def test_the_label_never_calls_modelled_flashes_observed(self):
        for wind in (True, False):
            for th in ("observed_glm", "observed_file", "modelled", "none"):
                rows = snd.label_rows(wind, th, "x.csv")
                text = " ".join(rows)
                assert (
                    "SYNTHESISED, not a recording" in text
                    and "listener position is virtual" in text
                )
                assert ("MODELLED" in text) == (th == "modelled")
                assert ("OBSERVED" in text) == (th == "observed_glm")
                assert ("PROXY" in text) == (not wind)
        # the drawn flashes are said to be drawn
        assert "(drawn)" in snd.label_rows(True, "observed_glm", drawn=True)[1]
        assert "(not drawn)" in snd.label_rows(True, "observed_glm", drawn=False)[1]
        assert (
            "(drawn, not checked here)" in snd.label_rows(True, "observed_file", "x.csv", True)[1]
        )

    def test_drawn_flashes_are_labelled_for_what_they_are(self):
        glm = snd.SoundPlan("observed_glm", None, 10.0, "x", 7, 10.0, [], draw_flashes=True)
        assert "OBSERVED GLM flash (position ~10 km, optical, not a stroke)" in glm.flash_label
        assert (
            glm.drawn
            and glm.page_settings()["drawFlashes"]
            and glm.page_settings()["flashKind"] == "glm"
        )
        file = snd.SoundPlan(
            "observed_file", None, 10.0, "x", 7, 10.0, [], draw_flashes=True, flash_file="my.csv"
        )
        assert (
            "flash from my.csv" in file.flash_label and file.page_settings()["flashKind"] == "file"
        )
        assert "OBSERVED" not in file.flash_label
        quiet = snd.SoundPlan("observed_glm", None, 10.0, "x", 7, 10.0, [])
        assert not quiet.drawn and "drawFlashes" not in quiet.page_settings()
        model = snd.SoundPlan("modelled", None, 10.0, "x", 7, 10.0, [])
        assert model.drawn and model.flash_label is None  # the picture's own MODELLED label

    def test_draw_flashes_needs_a_flash_source_and_sound(self, capsys, case, tmp_path):
        code, _, err = run_cli(
            capsys, "animate", "-i", str(case), "--no-render", "--sound", "--draw-flashes"
        )
        assert code == 2 and "--draw-flashes" in err and "--flashes" in err
        code, _, err = run_cli(capsys, "animate", "-i", str(case), "--no-render", "--draw-flashes")
        assert code == 2 and "add --sound" in err

    def test_the_default_mix_puts_the_thunder_above_the_bed(self):
        plan = snd.SoundPlan("observed_glm", None, 10.0, "x", 7, 10.0, [])
        ps = plan.page_settings()
        assert ps["thunderDb"] == snd.THUNDER_DB > 0 and ps["bedDb"] == snd.BED_DB < 0
        assert ps["thunderDb"] - ps["bedDb"] >= 10  # thunder raised, bed lowered
        assert (
            ps["ceiling"] == snd.CEILING and 20 * math.log10(snd.CEILING) < -1
        )  # room for the AAC encoder
        louder = snd.SoundPlan("observed_glm", None, 10.0, "x", 7, 10.0, [], thunder_db_offset=3.0)
        assert (
            louder.page_settings()["thunderDb"] == snd.THUNDER_DB + 3.0
            and louder.summary()["mix"]["thunder_db_offset"] == 3.0
        )

    def test_thunder_db_is_checked(self, capsys, case):
        code, _, err = run_cli(
            capsys, "animate", "-i", str(case), "--no-render", "--sound", "--sound-thunder-db", "40"
        )
        assert code == 2 and "--sound-thunder-db" in err
        code, _, err = run_cli(
            capsys, "animate", "-i", str(case), "--no-render", "--sound-thunder-db", "3"
        )
        assert code == 2 and "add --sound" in err

    def test_page_settings_carry_the_sound(self, capsys, case, tmp_path, synthetic):
        base = plan_json(capsys, case, tmp_path)
        r0 = base["storm"]["track"][0]
        f = write_flash_csv(tmp_path / "f.csv", [(r0["time"], r0["lat"], r0["lon"], "IC", "")])
        from engine import cli

        # the plan as the command builds it, through the real option parser
        args = cli.build_parser().parse_args(
            [
                "animate",
                "-i",
                str(case),
                "--sound",
                "--flashes",
                str(f),
                "--sound-listener",
                "3,4",
                "--sound-seed",
                "5",
                "--no-render",
            ]
        )
        from engine import animate_cli

        o = animate_cli.options_from_args(args, [str(p) for p in sorted(case.glob("*/K*"))])
        assert (
            o.sound and o.sound_listener == (3.0, 4.0) and o.sound_seed == 5 and o.flashes == str(f)
        )
        res = animate.run(o)
        ps = res.sound_plan.page_settings()
        assert ps["thunder"] == "observed" and ps["listener"] == [3.0, 4.0] and ps["seed"] == 5
        assert ps["flashes"][0]["kind"] == "IC" and ps["flashes"][0]["t_s"] == pytest.approx(
            0.0, abs=1.0
        )
        assert ps["label"] == res.sound_plan.label


# ---------------------------------------------------------------------------
# the record next to the film
# ---------------------------------------------------------------------------


class TestRecord:
    def fake_audio(self, plan):
        return {
            "sampleRate": 48000,
            "frames": 48000 * 4,
            "peak": 0.5,
            "levelsDb": {"thunder": 10.0, "bed": -4.0},
            "ceiling": 0.8,
            "compress": plan.compress,
            "seed": plan.seed,
            "windSource": "retrieved",
            "windMs": {"min": 12.0, "max": 41.25},
            "windProxyShare": 0.1,
            "rainLevel": {"min": 0.0, "max": 0.8},
            "listener": {
                "mode": "virtual",
                "x": 5.0,
                "y": -2.0,
                "lat": 36.5,
                "lon": -88.9,
                "eyeM": 10,
            },
            "thunders": [
                {
                    "flash": 1,
                    "tFlash": 2.0,
                    "arrival": 2.75,
                    "delay": 0.75,
                    "distKm": 12.34,
                    "kind": "CG",
                    "source": "observed",
                    "tDataS": 60.0,
                    "lat": 36.1,
                    "lon": -88.8,
                },
                {
                    "flash": 0,
                    "tFlash": 1.0,
                    "arrival": 1.5,
                    "delay": 0.5,
                    "distKm": 8.0,
                    "kind": "IC",
                    "source": "observed",
                    "tDataS": 30.0,
                    "lat": 36.2,
                    "lon": -88.7,
                },
            ],
        }

    def test_the_sidecar_block_has_everything_needed_to_check_the_sound(
        self, capsys, case, tmp_path, synthetic
    ):
        base = plan_json(capsys, case, tmp_path)
        r0 = base["storm"]["track"][0]
        f = write_flash_csv(tmp_path / "f.csv", [(r0["time"], r0["lat"], r0["lon"], "", "")])
        o = animate.AnimateOptions(
            files=[str(p) for p in sorted(case.glob("*/K*"))],
            no_render=True,
            cache_dir=str(tmp_path / "c"),
            sound=True,
            flashes=str(f),
            sound_compress=20.0,
        )
        res = animate.run(o)
        t0 = res.steps.steps[0].time
        rec = snd.record(res.sound_plan, self.fake_audio(res.sound_plan), "ab" * 32, t0)
        assert (
            rec["listener"]["mode"] == "virtual"
            and rec["listener"]["lat"] == 36.5
            and rec["listener"]["ear_height_m"] == 10
        )
        assert rec["compress"] == 20.0 and rec["seed"] == 7
        w = rec["wind"]
        assert (
            w["source"] == "retrieved"
            and w["min_ms"] == 12.0
            and w["max_ms"] == pytest.approx(41.25, abs=0.06)
        )
        assert w["share_of_time_using_proxy"] == 0.1 and w["ground_factor"] == 0.65
        assert (
            rec["mix"]["thunder_db"] == 10.0
            and rec["mix"]["bed_db"] == -4.0
            and rec["mix"]["ceiling"] == 0.8
        )
        assert rec["thunder"]["drawn"] is False
        ev = rec["thunder"]["events"]
        assert [e["flash_clip_s"] for e in ev] == [1.0, 2.0]  # in clip-time order
        assert (
            ev[0]["thunder_arrives_clip_s"] == 1.5
            and ev[0]["delay_clip_s"] == 0.5
            and ev[0]["delay_real_s"] == 10.0
        )
        assert (
            ev[1]["distance_km"] == 12.3
            and ev[1]["type"] == "CG"
            and ev[1]["flash_time_utc"].endswith("Z")
        )
        assert (
            rec["audio"]["codec"] == "aac"
            and rec["audio"]["duration_s"] == 4.0
            and rec["audio"]["pcm_sha256"] == "ab" * 32
        )
        assert rec["audio"]["peak_dbfs"] == pytest.approx(20 * math.log10(0.5), abs=0.01)
        assert rec["synthesised"] is True and rec["recording"] is False and rec["limits"]
        assert any("not a recording" in s for s in rec["limits"]) and any(
            "tornado" in s for s in rec["limits"]
        )
        json.dumps(rec)  # serialisable

    def test_the_provenance_file_includes_the_sound_and_the_parameters(
        self, capsys, case, tmp_path, synthetic
    ):
        o = animate.AnimateOptions(
            files=[str(p) for p in sorted(case.glob("*/K*"))],
            no_render=True,
            cache_dir=str(tmp_path / "c"),
            sound=True,
            sound_listener=(1.0, 2.0),
            sound_seed=9,
        )
        res = animate.run(o)
        doc = animate.provenance(res, {}, None)
        assert doc["sound"]["seed"] == 9
        assert doc["parameters"]["sound"] is True and doc["parameters"]["sound_listener"] == (
            1.0,
            2.0,
        )
        assert doc["parameters"]["sound_seed"] == 9

    def test_mp4_tags_say_the_sound_is_synthesised(self, capsys, case, tmp_path, synthetic):
        o = animate.AnimateOptions(
            files=[str(p) for p in sorted(case.glob("*/K*"))],
            no_render=True,
            cache_dir=str(tmp_path / "c"),
            sound=True,
        )
        res = animate.run(o)
        comment = animate.mp4_metadata(res, tmp_path / "x.mp4")["comment"]
        assert "SYNTHESISED, not a recording" in comment
        o2 = animate.AnimateOptions(files=o.files, no_render=True, cache_dir=str(tmp_path / "c"))
        assert (
            "SYNTHESISED"
            not in animate.mp4_metadata(animate.run(o2), tmp_path / "x.mp4")["comment"]
        )

    def test_the_report_line_says_what_the_sound_is(self, capsys, case, tmp_path, synthetic):
        code, out, err = run_cli(
            capsys,
            "animate",
            "-i",
            str(case),
            "--no-render",
            "--cache-dir",
            str(tmp_path / "c"),
            "--sound",
            "--sound-listener",
            "3,4",
        )
        assert code == 0, err
        line = next(ln for ln in out.splitlines() if ln.strip().startswith("sound "))
        assert "SYNTHESISED, not a recording" in line and "no thunder" in line and "3,4 km" in line


# ---------------------------------------------------------------------------
# missing things fail clearly (never a silent film when sound was asked)
# ---------------------------------------------------------------------------


class _NoEncoderRenderer:
    """A browser whose page cannot encode (no WebCodecs), for the checks before any gridding."""

    def __init__(self, *_a, **_k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        pass

    def check_look(self, _settings):
        return {}

    def encoder_support(self):
        return {"choice": None, "notes": ["WebCodecs is not available in this page"]}


class TestMissing:
    def test_without_any_encoder_the_command_stops_with_the_hint_and_exit_code_4(
        self, capsys, case, tmp_path, synthetic, monkeypatch
    ):
        # neither Chrome's encoder nor the user's PyAV: a film with sound cannot be written
        monkeypatch.setattr(render, "have_module", lambda name: name != "av")
        monkeypatch.setattr(render, "Renderer", _NoEncoderRenderer)
        monkeypatch.setattr(render, "find_frontend", lambda explicit=None: tmp_path)
        code, _, err = run_cli(
            capsys,
            "animate",
            "-i",
            str(case),
            "-o",
            str(tmp_path / "x.mp4"),
            "--cache-dir",
            str(tmp_path / "c"),
            "--sound",
        )
        assert code == 4 and "pip install av" in err and "--sound" in err and "WebCodecs" in err
        assert not (tmp_path / "x.mp4").exists() and not list(tmp_path.glob("x_frames"))

    def test_encoder_pyav_asked_for_without_pyav_stops_before_anything(
        self, capsys, case, tmp_path, synthetic, monkeypatch
    ):
        monkeypatch.setattr(render, "have_module", lambda name: name != "av")
        code, _, err = run_cli(
            capsys,
            "animate",
            "-i",
            str(case),
            "-o",
            str(tmp_path / "x.mp4"),
            "--cache-dir",
            str(tmp_path / "c"),
            "--encoder",
            "pyav",
        )
        assert code == 4 and "pip install av" in err and "PyAV" in err
        assert "GPL" in err  # why it is the user's own choice

    def test_without_playwright_the_command_stops_with_exit_code_4(
        self, capsys, case, tmp_path, synthetic, monkeypatch
    ):
        monkeypatch.setattr(render, "have_module", lambda name: name != "playwright")
        code, _, err = run_cli(
            capsys,
            "animate",
            "-i",
            str(case),
            "-o",
            str(tmp_path / "x.mp4"),
            "--cache-dir",
            str(tmp_path / "c"),
            "--sound",
        )
        assert code == 4 and "Playwright" in err

    def test_a_chrome_that_cannot_make_sound_is_a_dependency_error(self):
        class Page:
            def evaluate(self, *a):
                raise RuntimeError(
                    "Page.evaluate: ReferenceError: OfflineAudioContext is not defined"
                )

        r = render.Renderer.__new__(render.Renderer)
        r.page = Page()
        with pytest.raises(Exception, match="update Google Chrome") as e:
            r.render_audio()
        assert type(e.value).__name__ == "XrdDependencyError"

    def test_pcm_is_cut_or_padded_to_the_video_length(self):
        import base64

        pcm = (np.arange(2 * 100) % 7 - 3).astype("<i2")
        page = {"sampleRate": 48000, "pcm": base64.b64encode(pcm.tobytes()).decode()}
        n = 48000 // 4  # one video frame at 4 fps is 12000 samples
        track = render.pcm_from_page(page, 1, 4)
        assert (
            track.pcm.shape == (n, 2)
            and track.pcm[:100].tolist() == pcm.reshape(-1, 2).tolist()
            and not track.pcm[100:].any()
        )
        track = render.pcm_from_page(
            {**page, "pcm": base64.b64encode(np.zeros(2 * 30000, "<i2").tobytes()).decode()}, 1, 4
        )
        assert track.pcm.shape == (n, 2)


# ---------------------------------------------------------------------------
# end to end with a real Chrome (skipped when it cannot run)
# ---------------------------------------------------------------------------


@pytest.fixture
def chrome():
    reason = _chrome_ready()
    if reason:
        pytest.skip(reason)
    pytest.importorskip("av")


def test_end_to_end_mp4_has_an_aac_track_as_long_as_the_video(tmp_path, chrome):
    import av

    gridder = SyntheticGridder(fine_shape=(16, 64, 64))
    base = animate.run(
        _e2e_options(tmp_path, str(tmp_path / "base.mp4"), no_render=True), gridder=gridder
    )
    row = base.storm_rows()[1]
    flashes = write_flash_csv(
        tmp_path / "f.csv",
        [
            (row["time"], row["lat"], row["lon"], "CG", "3"),
            (
                base.storm_rows()[0]["time"],
                base.storm_rows()[0]["lat"],
                base.storm_rows()[0]["lon"],
                "IC",
                "1",
            ),
        ],
    )
    target = tmp_path / "sound.mp4"
    res = animate.run(
        _e2e_options(
            tmp_path,
            str(target),
            sound=True,
            flashes=str(flashes),
            sound_compress=5.0,
            sound_listener=(8.0, -3.0),
        ),
        gridder=SyntheticGridder(fine_shape=(16, 64, 64)),
    )
    with av.open(str(target)) as c:
        v, a = c.streams.video[0], c.streams.audio[0]
        assert a.codec_context.name == "aac" and a.rate == 48000 and a.layout.name == "stereo"
        assert abs(float(a.duration * a.time_base) - float(v.duration * v.time_base)) < 0.1
        assert "SYNTHESISED" in c.metadata.get("comment", "")
    with av.open(str(target)) as c:
        pcm = np.concatenate([f.to_ndarray() for f in c.decode(audio=0)], axis=1)
    assert (
        np.abs(pcm).max() < 10 ** (-1 / 20) and np.sqrt((pcm**2).mean()) > 1e-3
    )  # below -1 dBFS, not silent
    rec = json.loads(target.with_suffix(".animate.json").read_text())["sound"]
    assert (
        rec["listener"]["requested_km_east_north_of_track_middle"] == [8.0, -3.0]
        and rec["listener"]["mode"] == "virtual"
    )
    assert (
        rec["compress"] == 5.0
        and len(rec["thunder"]["events"]) == 2
        and rec["audio"]["duration_s"] == pytest.approx(4.0, abs=0.01)
    )
    assert {e["type"] for e in rec["thunder"]["events"]} == {"CG", "IC"}
    assert all(e["distance_km"] > 0 and e["delay_clip_s"] > 0 for e in rec["thunder"]["events"])
    assert res.sound is not None and Path(res.sound["flashes_file"]).is_file()
    assert (tmp_path / "sound.flashes.csv").is_file()


def test_end_to_end_the_same_seed_gives_the_same_samples(tmp_path, chrome):
    """Two renders of the same settings in two fresh pages give the same PCM, bit for bit."""
    gridder = SyntheticGridder(fine_shape=(16, 64, 64))
    base = animate.run(
        _e2e_options(tmp_path, str(tmp_path / "b.mp4"), no_render=True), gridder=gridder
    )
    r0 = base.storm_rows()[1]
    f = write_flash_csv(tmp_path / "f.csv", [(r0["time"], r0["lat"], r0["lon"], "CG", "3")])
    hashes = []
    for k in range(2):
        res = animate.run(
            _e2e_options(
                tmp_path,
                str(tmp_path / f"s{k}.mp4"),
                sound=True,
                flashes=str(f),
                sound_seed=11,
                sound_compress=5.0,
                fresh=True,
            ),
            gridder=SyntheticGridder(fine_shape=(16, 64, 64)),
        )
        hashes.append(res.sound["audio"]["pcm_sha256"])
    assert hashes[0] == hashes[1]
    other = animate.run(
        _e2e_options(
            tmp_path,
            str(tmp_path / "s2.mp4"),
            sound=True,
            flashes=str(f),
            sound_seed=12,
            sound_compress=5.0,
        ),
        gridder=SyntheticGridder(fine_shape=(16, 64, 64)),
    )
    assert other.sound["audio"]["pcm_sha256"] != hashes[0]  # another seed, another noise


def test_end_to_end_a_film_without_sound_has_no_audio_stream(tmp_path, chrome):
    import av

    target = tmp_path / "quiet.mp4"
    animate.run(
        _e2e_options(tmp_path, str(target)), gridder=SyntheticGridder(fine_shape=(16, 64, 64))
    )
    with av.open(str(target)) as c:
        assert not c.streams.audio
    assert "sound" not in json.loads(target.with_suffix(".animate.json").read_text())


def test_end_to_end_drawn_flashes_follow_the_thunder_and_the_thunder_is_above_the_bed(
    tmp_path, chrome
):
    """The flashes of the thunder source are drawn (one flash time for light and thunder), and the thunder
    stands above the wind and rain bed: the same film without thunder is the bed."""
    import av

    base = animate.run(
        _e2e_options(tmp_path, str(tmp_path / "b.mp4"), no_render=True),
        gridder=SyntheticGridder(fine_shape=(16, 64, 64)),
    )
    rows = base.storm_rows()
    flashes = write_flash_csv(
        tmp_path / "f.csv", [(r["time"], r["lat"], r["lon"], "CG", "3") for r in rows[:3]]
    )
    common = {"sound": True, "sound_listener": (6.0, 0.0), "sound_compress": 20.0, "sound_seed": 4}
    loud = animate.run(
        _e2e_options(
            tmp_path, str(tmp_path / "t.mp4"), flashes=str(flashes), draw_flashes=True, **common
        ),
        gridder=SyntheticGridder(fine_shape=(16, 64, 64)),
    )
    bed = animate.run(
        _e2e_options(tmp_path, str(tmp_path / "bed.mp4"), **common),
        gridder=SyntheticGridder(fine_shape=(16, 64, 64)),
    )
    # drawn: the page reports the observed flashes it draws, and the record says so
    assert loud.output["page_report"]["lightning"]["source"] == "observed"
    assert loud.output["page_report"]["lightning"]["flashes"] == 3
    assert loud.sound["thunder"]["drawn"] is True and "drawn" in loud.sound["label"][1]
    assert bed.output["page_report"]["lightning"]["source"] == "off"

    def decode(path):
        with av.open(str(path)) as c:
            return np.concatenate([f.to_ndarray() for f in c.decode(audio=0)], axis=1).mean(axis=0)

    x, xb = decode(tmp_path / "t.mp4"), decode(tmp_path / "bed.mp4")
    sr, hop = 48000, 12000
    n = min(len(x), len(xb)) // hop
    e_full = (x[: n * hop].reshape(n, hop) ** 2).mean(axis=1)
    e_bed = (xb[: n * hop].reshape(n, hop) ** 2).mean(axis=1)
    e_thunder = np.maximum(e_full - e_bed, 0)
    marks = []
    for ev in loud.sound["thunder"]["events"]:
        k0 = int(ev["thunder_arrives_clip_s"] * sr / hop)
        k1 = min(n, k0 + 4)
        if k1 - k0 >= 3:
            marks.append(
                10 * np.log10(e_thunder[k0:k1].mean() + 1e-12)
                - 10 * np.log10(e_bed[k0:k1].mean() + 1e-12)
            )
    assert marks, "no thunder arrival with a window after it"
    assert np.median(marks) >= 6.0, f"thunder minus bed (dB) at the arrivals: {marks}"
    assert np.abs(decode(tmp_path / "t.mp4")).max() < 10 ** (-1 / 20)
