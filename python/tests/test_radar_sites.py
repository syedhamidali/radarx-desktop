"""Integrity of the bundled radar-site catalogue (src/lib/data/nexradSites.json)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import engine.cloud as cloud

CATALOGUE = Path(__file__).resolve().parents[2] / "src" / "lib" / "data" / "nexradSites.json"


@pytest.fixture(scope="module")
def doc() -> dict:
    return json.loads(CATALOGUE.read_text(encoding="utf-8"))


def test_header_cites_source_date_and_licence(doc: dict) -> None:
    assert "ncei.noaa.gov" in doc["_comment"]
    assert "public domain" in doc["_comment"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", doc["retrieved"])
    assert doc["retrieved"] in doc["_comment"]


def test_ids_unique_valid_and_sorted(doc: dict) -> None:
    ids = [s["id"] for s in doc["sites"]]
    assert len(ids) == len(set(ids))
    assert ids == sorted(ids)
    assert all(re.fullmatch(r"[A-Z0-9]{4}", i) for i in ids)


def test_count_matches_the_engine_station_list(doc: dict) -> None:
    """Every engine station has a marker and every marker can be opened."""
    assert {s["id"] for s in doc["sites"]} == set(cloud._NEXRAD_STATIONS)
    assert len(doc["sites"]) == 156


def test_fields_and_ranges(doc: dict) -> None:
    for s in doc["sites"]:
        assert set(s) == {"id", "name", "state", "lat", "lon", "elev_m"}, s["id"]
        assert s["name"].strip() and len(s["name"]) <= 40
        assert re.fullmatch(r"[A-Z]{2}", s["state"]), s["id"]
        assert -90 <= s["lat"] <= 90 and -180 <= s["lon"] <= 180, s["id"]
        assert -100 <= s["elev_m"] <= 4000, s["id"]  # no WSR-88D sits above ~3.3 km


def test_agrees_with_the_engine_coordinate_table(doc: dict) -> None:
    """Two independent transcriptions of the NCEI list must agree."""
    for s in doc["sites"]:
        lat, lon, elev = cloud._NEXRAD_SITES[s["id"]]
        assert abs(s["lat"] - lat) < 0.001, s["id"]
        assert abs(s["lon"] - lon) < 0.001, s["id"]
        assert abs(s["elev_m"] - elev) <= 1, s["id"]


def test_known_sites(doc: dict) -> None:
    by = {s["id"]: s for s in doc["sites"]}
    assert by["KVWX"]["name"] == "Evansville" and by["KVWX"]["state"] == "IN"
    assert by["KTLX"]["state"] == "OK"
    assert by["TJUA"]["state"] == "PR" and by["PGUA"]["state"] == "GU"
