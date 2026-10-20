"""One allow-list, three places: the client (engine/account/activity.py), the table's trigger
(supabase/migrations/20261021000000_activity.sql) and the log-activity Edge Function
(supabase/functions/log-activity/allowlist.json). This fails when they drift apart, so the token
path can never accept more than the database path."""

from __future__ import annotations

import json
import re
from pathlib import Path

from engine.account import activity

REPO = Path(__file__).resolve().parents[2]
ALLOWLIST = json.loads((REPO / "supabase/functions/log-activity/allowlist.json").read_text())
SQL = (REPO / "supabase/migrations/20261021000000_activity.sql").read_text()


def sql_events() -> dict[str, list[str]]:
    found = {}
    for name, keys in re.findall(r"when '(\w+)'\s+then array\[([^\]]*)\]", SQL):
        found[name] = re.findall(r"'(\w+)'", keys)
    return found


def test_the_event_lists_are_identical():
    python = {name: list(keys) for name, keys in activity.EVENTS.items()}
    assert ALLOWLIST["events"] == python
    assert sql_events() == python


def test_surfaces_match():
    assert ALLOWLIST["surfaces"] == list(activity.SURFACES)
    match = re.search(r"surface\s+text not null check \(surface in \(([^)]*)\)\)", SQL)
    assert re.findall(r"'(\w+)'", match.group(1)) == list(activity.SURFACES)


def test_the_operating_system_words_match():
    match = re.search(r"new\.os not in \(([^)]*)\)", SQL)
    assert re.findall(r"'(\w+)'", match.group(1)) == ALLOWLIST["os"]
    for family in ("macOS", "Windows", "Linux", "Other"):  # what the client sends
        assert family in ALLOWLIST["os"]


def norm(pattern: str) -> str:
    """The same character class however it is spelled (an escaped hyphen, anchors)."""
    return pattern.replace("\\-", "-")


def test_the_value_pattern_matches():
    assert norm(activity._VALUE.pattern) == norm(ALLOWLIST["value_pattern"])
    match = re.search(r"\(v #>> '\{\}'\) !~ '([^']*)'", SQL)
    assert norm(match.group(1)) == norm(ALLOWLIST["value_pattern"])


def test_the_device_and_version_patterns_match():
    match = re.search(r"new\.device_id !~ '([^']*)'", SQL)
    assert match.group(1) == ALLOWLIST["device_pattern"]
    match = re.search(r"new\.app_version !~ '([^']*)'", SQL)
    assert match.group(1) == ALLOWLIST["version_pattern"]
    assert "^" + activity._VERSION.pattern + "$" == ALLOWLIST["version_pattern"]
    assert ALLOWLIST["version_unknown"] == "unknown"
    assert f"<> '{ALLOWLIST['version_unknown']}'" in SQL


def test_the_size_limits_match():
    assert f"octet_length(new.detail::text) > {ALLOWLIST['max_detail_bytes']}" in SQL
    assert f"char_length(event) <= {ALLOWLIST['max_event_name']}" in SQL
    assert ALLOWLIST["max_batch"] == activity.BATCH_SIZE


def test_the_patterns_behave_the_same_on_awkward_values():
    """The same strings pass or fail in Python and in the JavaScript-compatible pattern."""
    value = re.compile(ALLOWLIST["value_pattern"])
    for text in (
        "info",
        "rain rate",
        "a+b",
        "x_y-z",
        "",
        "a/b",
        "a.b",
        "a@b",
        "a\\b",
        "x" * 65,
        "ok1",
    ):
        assert bool(activity._VALUE.fullmatch(text)) == bool(value.fullmatch(text)), text
