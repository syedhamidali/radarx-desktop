"""What the rendered sound of ``rxd animate --sound`` was made of, written where the viewer sees it: the
wind label from the share of film time with retrieved wind at the listener, a silent rain warned about on
the film, in the report and in the record, the real thunder delay from the page, and the CG / IC type of
GLM flashes written to the flash table as SYNTHETIC (GLM does not give it)."""

# ruff: noqa: E501

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from engine import animate
from engine import animate_sound as snd
from tests import test_animate_sound as _tas
from tests.test_animate_sound import case, synthetic  # noqa: F401  (fixtures)

UTC = UTC


def plan():
    return snd.SoundPlan("observed_glm", None, 102.0, "automatic: x", 7, 102.0, [])


def audio(proxy=0.0, rain_max=0.4, source="retrieved"):
    return {
        "windSource": source,
        "windProxyShare": proxy,
        "rainLevel": {"min": 0.0, "max": rain_max},
    }


def test_the_wind_label_follows_the_share_of_film_time_with_retrieved_wind():
    p = plan()
    assert "wind from the retrieved winds," in snd.final_label(p, audio(0.0))[0]
    mixed = snd.final_label(p, audio(0.6))[0]
    assert "retrieved winds for 40% of the film, a proxy otherwise" in mixed
    none = snd.final_label(p, audio(1.0))[0]
    assert "PROXY" in none and "retrieved winds for" not in none
    # no retrieved winds at all: the page uses the proxy everywhere
    assert "PROXY" in snd.final_label(p, audio(1.0, source="none"))[0]
    assert snd.retrieved_share(audio(0.25)) == pytest.approx(0.75)
    # before rendering the plan does not claim retrieved winds everywhere
    assert "where they reach the listener" in snd.label_rows(True, "observed_glm")[0]


def test_a_silent_rain_is_said_on_the_film_and_in_the_warnings():
    p = plan()
    rows = snd.final_label(p, audio(rain_max=0.0))
    assert "rain SILENT" in rows[0] and "rain from the reflectivity" not in rows[0]
    assert "rain from the reflectivity" in snd.final_label(p, audio(rain_max=0.3))[0]
    w = snd.audio_warnings(audio(rain_max=0.0))
    assert any("rain level stayed at zero for the whole film" in x for x in w)
    assert not any("rain" in x for x in snd.audio_warnings(audio(rain_max=0.2)))


def test_the_record_says_whether_the_rain_was_heard_and_from_which_height(
    case,  # noqa: F811
    tmp_path,
    synthetic,  # noqa: F811
):
    o = animate.AnimateOptions(
        files=[str(p) for p in sorted(case.glob("*/K*"))],
        no_render=True,
        cache_dir=str(tmp_path / "c"),
        sound=True,
    )
    res = animate.run(o)
    assert res.sound_plan.page_settings()["compressAuto"] is True
    a = _tas.TestRecord().fake_audio(res.sound_plan)
    a["rainLevel"] = {"min": 0.0, "max": 0.0, "mean": 0.0, "heardShare": 0.0}
    a["rainHeightKm"] = None
    a["thunderTiming"] = "film clock"
    a["thunders"][0]["delayRealS"] = 36.0
    rec = snd.record(res.sound_plan, a, "ab" * 32, res.steps.steps[0].time)
    assert rec["rain"]["heard"] is False and "warning" in rec["rain"]
    assert rec["rain"]["echo_height_km"] is None and "not a rain rate" in rec["rain"]["rule"]
    assert "film clock" in rec["thunder"]["timing"]
    # the real delay comes from the page (through the clock), not from delay x compress
    ev = {e["flash_clip_s"]: e for e in rec["thunder"]["events"]}
    assert ev[2.0]["delay_real_s"] == 36.0
    assert ev[1.0]["delay_real_s"] == pytest.approx(0.5 * a["compress"], abs=0.1)  # old records
    a["rainLevel"] = {"min": 0.0, "max": 0.6, "mean": 0.3, "heardShare": 0.8}
    a["rainHeightKm"] = {"min": 1.0, "max": 2.0}
    rec = snd.record(res.sound_plan, a, "ab" * 32, res.steps.steps[0].time)
    assert rec["rain"]["heard"] is True and "warning" not in rec["rain"]
    assert rec["rain"]["echo_height_km"] == {"min": 1.0, "max": 2.0}
    assert rec["rain"]["share_of_time_heard"] == 0.8
    assert rec["wind"]["share_of_time_retrieved"] == 0.9


def test_the_record_marks_a_drawn_type_of_an_untyped_flash_synthetic():
    p = plan()
    t0 = datetime(2021, 12, 11, 2, 0, 0, tzinfo=UTC)
    from engine.lightning_obs import ObservedFlash

    p.table = [ObservedFlash(t0 + timedelta(seconds=30), 36.1, -88.9, None, 0.7)]
    p.flash_times = [p.table[0].time]
    p.flash_latlon = [(36.1, -88.9)]
    a = _tas.TestRecord().fake_audio(p)
    a["thunders"] = a["thunders"][1:]  # the one at data time 30 s
    rec = snd.record(p, a, "ab" * 32, t0)
    assert rec["thunder"]["events"][0]["type"] == "IC"
    assert rec["thunder"]["events"][0]["type_source"] == "synthetic"
    assert any("GLM does not say whether a flash reached the ground" in s for s in rec["limits"])


def test_the_flash_table_writes_the_drawn_type_and_marks_it_synthetic(tmp_path):
    from engine.lightning_obs import ObservedFlash

    t0 = datetime(2021, 12, 11, 2, 0, 0, tzinfo=UTC)
    fl = [
        ObservedFlash(t0 + timedelta(seconds=30), 36.1, -88.9, None, 0.7),  # GLM: no type
        ObservedFlash(t0 + timedelta(seconds=60), 36.2, -88.8, None, 0.3, kind="IC"),
    ]
    thunders = [{"tDataS": 60.0, "kind": "IC"}, {"tDataS": 30.0, "kind": "CG"}]
    kinds = snd.chosen_kinds(fl, thunders, t0)
    assert kinds == ["CG", "IC"]
    snd.write_flash_table(tmp_path / "g.csv", fl, [3.0, 4.0], chosen_kind=kinds, seed=7)
    lines = (tmp_path / "g.csv").read_text().splitlines()
    assert lines[0].startswith("# type:") and "SYNTHETIC" in lines[0] and "seed 7" in lines[0]
    assert "GLM" in lines[0] and "Not an observation" in lines[0]
    assert lines[1] == "time_utc,lat,lon,type,energy,type_source,distance_to_storm_km"
    assert lines[2].split(",")[3] == "CG" and lines[2].split(",")[5] == "synthetic"
    assert lines[3].split(",")[3] == "IC" and lines[3].split(",")[5] == "file"
    # read back with --flashes: the synthetic type is not taken as data, the given one is
    back = snd.read_flash_table(tmp_path / "g.csv")
    assert [b.kind for b in back] == [None, "IC"]
    # no type drawn: no comment line, the old layout
    snd.write_flash_table(tmp_path / "h.csv", fl[1:])
    assert (tmp_path / "h.csv").read_text().splitlines()[0] == "time_utc,lat,lon,type,energy"
