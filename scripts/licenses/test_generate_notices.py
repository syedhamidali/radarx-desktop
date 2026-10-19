# ruff: noqa: E501
"""Tests of the licence policy logic (known answers, no network): ``python -m pytest scripts/licenses``."""

from __future__ import annotations

import sys
from pathlib import Path

import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_notices as g

POLICY = g.Policy(tomllib.loads((Path(g.HERE) / "allowed.toml").read_text()))


def cat(expr: str) -> int:
    return POLICY.evaluate(expr)[0]


def test_single_permissive():
    assert cat("MIT") == g.PERMISSIVE
    assert cat("Apache-2.0") == g.PERMISSIVE
    assert cat("BSD-3-Clause") == g.PERMISSIVE


def test_or_takes_the_better_alternative():
    assert cat("MIT OR Apache-2.0 OR LGPL-2.1-or-later") == g.PERMISSIVE
    assert cat("GPL-3.0-only OR MIT") == g.PERMISSIVE
    assert cat("MIT/Apache-2.0") == g.PERMISSIVE  # old Cargo spelling


def test_and_takes_the_worse_requirement():
    assert cat("MIT AND LGPL-2.1-or-later") == g.WEAK
    assert cat("(MIT OR Apache-2.0) AND Unicode-3.0") == g.PERMISSIVE
    assert cat("MIT AND GPL-3.0-only") == g.STRONG


def test_categories():
    assert cat("MPL-2.0") == g.WEAK
    assert cat("GPL-3.0-or-later") == g.STRONG
    assert cat("AGPL-3.0-only") == g.STRONG
    assert cat("CC-BY-NC-SA-4.0") == g.NONCOMMERCIAL


def test_unknown_and_garbage_fail():
    assert cat("Frobnicate-1.0") == g.UNKNOWN
    assert cat("") == g.UNKNOWN
    assert cat("MIT OR") == g.UNKNOWN or cat("MIT OR") == g.PERMISSIVE  # never raises
    assert cat("MIT )") == g.UNKNOWN


def test_with_exception():
    assert cat("Apache-2.0 WITH LLVM-exception") == g.PERMISSIVE
    assert cat("Apache-2.0 WITH Mystery-exception") == g.UNKNOWN
    assert cat("Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT") == g.PERMISSIVE


def test_judge_flags_unreviewed_copyleft_and_accepts_reviewed():
    entries = [
        g.Entry(
            ecosystem="rust", name="somecrate", version="1", license="MPL-2.0", source="x", url=""
        ),
        g.Entry(
            ecosystem="rust", name="selectors", version="1", license="MPL-2.0", source="x", url=""
        ),
        g.Entry(
            ecosystem="npm", name="nolic", version="1", license="", source="none found", url=""
        ),
        g.Entry(ecosystem="npm", name="fine", version="1", license="MIT", source="x", url=""),
    ]
    g.judge(entries, POLICY)
    status = {e["name"]: e["status"] for e in entries}
    assert status == {"somecrate": "FAIL", "selectors": "reviewed", "nolic": "FAIL", "fine": "ok"}


def test_every_reviewed_entry_has_a_note():
    for key, val in POLICY.reviewed.items():
        assert val.get("note"), key


def test_committed_policy_and_static_files_parse():
    static = g.collect_static()
    assert static and all({"kind", "name", "license"} <= set(s) for s in static)
    assert {s["kind"] for s in static} <= {"font", "colormap", "imagery", "data", "native"}
    # the non-commercial imagery must be flagged
    eox = [s for s in static if "Sentinel-2" in s["name"]]
    assert eox and "NON-COMMERCIAL" in eox[0]["flag"]
