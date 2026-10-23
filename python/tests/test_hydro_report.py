"""The event report: a golden file for a synthetic case, escaping, and the run record."""

# ruff: noqa: E501
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("radarx")

from engine import cli
from engine.hydro import report
from engine.reader import RadarReader
from tests.hydro_synth import make_tree

GOLDEN = Path(__file__).parent / "golden" / "event_report.html"
# a 1 x 1 PNG: the figures of the golden model (real figures depend on the matplotlib build)
PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAHnOcQAAAAABJRU5ErkJggg=="
)


def synthetic_model() -> dict:
    """A fixed model with every section, numbers worked out by hand for a uniform 10 mm/h for 20
    min."""
    basin_row = {
        "id": "H1",
        "name": "Test Creek",
        "area_km2": 1200.0,
        "coverage": 1.0,
        "accumulation_mean_mm": 3.3333,
        "accumulation_adjusted_mean_mm": None,
        "accumulation_max_mm": 3.34,
        "accumulation_sigma_mm": 1.1,
        "peak_mean_rate_mm_h": 10.0,
        "peak_mean_rate_time": "2024-05-01T20:00:00Z",
        "max_cell_rate_mm_h": 10.0,
    }
    pix = {"png": PIXEL}
    return {
        "title": "Synthetic event",
        "created": "2000-01-01T00:00:00Z",
        "software": "radarx-desktop test",
        "label": "RETRIEVED",
        "period": ["2024-05-01T20:00:00Z", "2024-05-01T20:20:00Z"],
        "duration_min": 20.0,
        "inputs": ["v0.hyd", "v1.hyd", "v2.hyd"],
        "radars": [
            {
                "name": "SYN",
                "latitude": 35.0,
                "longitude": -97.0,
                "scans": 3,
                "first": "2024-05-01T20:00:00Z",
                "last": "2024-05-01T20:20:00Z",
            }
        ],
        "method": "hybrid: Z = 300 R^1.4 test text <b>not html</b>",
        "parameters": {},
        "uncertainty": {"kind": "1-sigma, mm", "model": "DSD variability and calibration."},
        "notes": ["No DEM: beam blockage is not evaluated."],
        "grid": {"cell_m": 2000.0, "ny": 100, "nx": 100},
        "stats": {
            "area_km2": 31000.0,
            "max_mm": 3.34,
            "mean_mm": 3.3333,
            "wet_area_km2": 30000.0,
            "max_rate_mm_h": 10.0,
            "mean_valid_fraction": 1.0,
            "uncertainty_median_rel": 0.33,
        },
        "record": {
            "record_version": 1,
            "type": "event_report",
            "inputs": ["v0.hyd"],
            "qpe": {"basins": "b.geojson"},
            "report": {"title": "Synthetic event", "bin_minutes": 60.0},
            "software": "radarx-desktop test",
        },
        "guidance_text": report.GUIDANCE_TEXT,
        "figures": {
            "accumulation": {"caption": "Accumulation map.", **pix},
            "basin_map": {"caption": "Basins filled by their mean.", **pix},
            "basin_series": {"caption": "Series.", **pix},
            "ensemble": {"caption": "Ensemble map.", **pix},
            "indicator_ffg_ratio_1h": {"caption": "Indicator map.", **pix},
        },
        "references": list(report.REFERENCES.values())[:2],
        "basins": {
            "adjusted": False,
            "rows": [basin_row],
            "thresholds": [1.0, 5.0],
            "areas": [{"1": 1200.0, "5": 0.0}],
            "exceedance": [
                {
                    "id": "H1",
                    "duration_h": 0.25,
                    "max_depth_mm": 3.3,
                    "return_period_years": 6.5,
                    "flag": "",
                    "note": "no areal reduction applied: understated for an area mean (a lower bound)",
                }
            ],
        },
        "gauges": {
            "pairs": 6,
            "method": "mean-field bias",
            "before": {
                "n": 6,
                "relative_bias": -0.33,
                "rmse_mm": 1.67,
                "correlation": 0.99,
                "kge": 0.6,
            },
            "after_in_sample": {
                "n": 6,
                "relative_bias": 0.0,
                "rmse_mm": 0.1,
                "correlation": 0.99,
                "kge": 0.99,
            },
            "cross_validation": None,
            "window_mismatch_s": 0.0,
        },
        "ensemble": {
            "disclaimer": "An ensemble under an ASSUMED error model, not a forecast.",
            "n_members": 30,
            "seed": 0,
            "error_model": {
                "sigma_ln": 0.3,
                "corr_length_km": 25.0,
                "shares": {"common": 0.3, "spatial": 0.5, "cell_to_cell": 0.2},
                "sources": {
                    "sigma_ln": "the engine's own uncertainty model",
                    "shares": "ASSUMED DEFAULT",
                },
                "notes": [],
            },
            "thresholds_mm": [1.0, 5.0],
            "basins": [
                {
                    "id": "H1",
                    "basin_mean_mm": {"mean": 3.3, "p05": 2.5, "p95": 4.2},
                    "prob_basin_mean_ge": {"1": 1.0, "5": 0.02},
                }
            ],
            "reliability": None,
        },
        "indicators": {
            "label": "RETRIEVED INDICATOR",
            "product_text": report.GUIDANCE_TEXT,
            "not_modelled": "Antecedent soil moisture, snow, urban drainage and terrain are not modelled.",
            "notes": ["The rainfall period is shorter than the guidance duration: lower bounds."],
            "layers": {
                "ffg_ratio_1h": {
                    "label": "Rain / guidance, 1 h",
                    "classes": {
                        "below 50 % of guidance": {"cells": 10, "area_km2": 40.0},
                        "50 to 75 %": {"cells": 7500, "area_km2": 30000.0},
                    },
                    "provenance": {
                        "guidance": {
                            "file": "ffg.nc",
                            "vintage": "test vintage",
                            "units_in_file": "in",
                        }
                    },
                }
            },
        },
    }


def test_golden_file_of_the_synthetic_model():
    html = report.render_html(synthetic_model())
    if os.environ.get("RADARX_UPDATE_GOLDEN") == "1":  # to regenerate after an intended change
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(html, encoding="utf-8")
    assert html == GOLDEN.read_text(encoding="utf-8")


def test_the_golden_report_states_what_it_must():
    html = report.render_html(synthetic_model())
    assert "Estimates, not observations" in html and "not an official warning" in html
    assert "NaN" not in html and "None" not in html
    assert "ASSUMED" in html and "not validated" in html  # the ensemble without gauges
    assert "lower bound" in html  # the return period without an areal reduction
    assert "6.5" in html
    # self-contained: no external resource is loaded when it is opened
    assert not re.search(r'(src|href)="https?://', html)
    assert html.count("data:image/png;base64,") == 5


def test_text_from_data_is_escaped():
    m = synthetic_model()
    m["title"] = "<script>alert(1)</script>"
    m["radars"][0]["name"] = '"><img src=x onerror=1>'
    html = report.render_html(m)
    assert "<script>alert" not in html and "&lt;script&gt;" in html
    assert "onerror=1>" not in html.replace("&gt;", "")


def test_missing_numbers_are_words_not_nan():
    m = synthetic_model()
    m["stats"].update(max_mm=None, mean_mm=float("nan"), uncertainty_median_rel=None)
    m["basins"]["rows"][0]["accumulation_sigma_mm"] = None
    html = report.render_html(m)
    assert "not available" in html and "NaN" not in html


def test_record_paths_are_reduced_to_names_but_the_record_file_keeps_them():
    rec = report.make_record(
        ["/home/u/data/a_V06", "/home/u/data/b_V06"],
        {"basins": "/home/u/w/b.geojson", "gauges": "/home/u/g.csv", "relation": "tropical"},
        "T",
        60.0,
    )
    assert rec["inputs"][0] == "/home/u/data/a_V06"
    red = report.redact_record(rec)
    assert red["inputs"] == ["a_V06", "b_V06"]
    assert red["qpe"] == {"basins": "b.geojson", "gauges": "g.csv", "relation": "tropical"}
    assert "/home/u" not in json.dumps(red)
    assert report.make_record(["x"], {"a": 1}, "T", 60.0) == report.make_record(
        ["x"], {"a": 1}, "T", 60.0
    )


# ---------------------------------------------------------------------------
# End to end on synthetic volumes
# ---------------------------------------------------------------------------

LAT, LON = 35.0, -97.0


@pytest.fixture()
def volumes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    real_open = RadarReader.open_file

    def fake_open(self, path: str):
        if not path.endswith(".hyd"):
            return real_open(self, path)
        when, rate = Path(path).read_text().split()
        r = float(rate)
        tree = make_tree(
            LAT, LON, lambda x, y: np.full(np.broadcast(x, y).shape, r), when, name="SYN"
        )
        self._commit(tree, path, "synthetic")
        return self.get_schema()

    monkeypatch.setattr(RadarReader, "open_file", fake_open)
    out = []
    for k, t in enumerate(("20:00:00", "20:10:00", "20:20:00")):
        p = tmp_path / f"v{k}.hyd"
        p.write_text(f"2024-05-01T{t} 10.0")
        out.append(p)
    gj = tmp_path / "b.geojson"
    ring = [[-97.2, 34.9], [-96.8, 34.9], [-96.8, 35.2], [-97.2, 35.2], [-97.2, 34.9]]
    gj.write_text(
        json.dumps(
            {
                "type": "Feature",
                "properties": {"huc12": "H1"},
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        )
    )
    return out


def run(capsys, *argv):
    code = cli.main([str(a) for a in argv])
    c = capsys.readouterr()
    return code, c.out, c.err


def normalise(html: str) -> str:
    """Drop what changes with the clock: the creation time."""
    html = re.sub(r"Made \d{4}-\d\d-\d\dT[\d:]+Z", "Made T", html)
    return html


BASE = ("-P", "min_dbz=-30", "--resolution-m", "2000", "--max-range-km", "100")


def test_report_from_volumes_and_again_from_its_record(capsys, volumes, tmp_path):
    code, _, _ = run(
        capsys, "report", "-i", *volumes, *BASE, "-b", "b.geojson", "--title", "Synthetic <event>",
        "-o", "r1.html", "--record", "rec.json", "-q",
    )  # fmt: skip
    assert code == 0
    html = Path("r1.html").read_text(encoding="utf-8")
    assert "<title>Synthetic &lt;event&gt;</title>" in html
    assert "RETRIEVED" in html and "Estimates, not observations" in html
    assert "3.3 mm" in html  # 10 mm/h for 20 min, the largest accumulation
    assert "H1" in html and "<h2>Basins</h2>" in html
    assert html.count("data:image/png;base64,") >= 3
    assert "NaN" not in html
    assert str(tmp_path) not in html  # paths are names in the document
    rec = json.loads(Path("rec.json").read_text())
    assert rec["type"] == "event_report" and str(volumes[0]) in rec["inputs"]
    assert Path(rec["qpe"]["basins"]).name == "b.geojson"

    code, _, _ = run(capsys, "report", "--from-record", "rec.json", "-o", "r2.html", "-q")
    assert code == 0
    again = normalise(Path("r2.html").read_text(encoding="utf-8"))
    first = normalise(html)
    if again != first:
        a, b = first.splitlines(), again.splitlines()
        diff = [(i, x[:120], y[:120]) for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y]
        raise AssertionError(f"{len(a)} vs {len(b)} lines; first differences: {diff[:3]}")


def test_report_without_basins_or_figures(capsys, volumes):
    code, _, _ = run(capsys, "report", "-i", *volumes, *BASE, "--no-figures", "-o", "t.html", "-q")
    assert code == 0
    html = Path("t.html").read_text(encoding="utf-8")
    assert "data:image" not in html and "<h2>Basins</h2>" not in html and "Event summary" in html


def test_report_usage_errors(capsys, volumes):
    code, _, err = run(capsys, "report", "--from-record", "nope.json")
    assert code == 2 and "cannot read the run record" in err
    Path("bad.json").write_text('{"type": "other"}')
    code, _, err = run(capsys, "report", "--from-record", "bad.json")
    assert code == 2 and "not a report run record" in err
    code, _, err = run(capsys, "report", "-i", *volumes, "-o", "x.pdf")
    assert code == 2 and ".html" in err
