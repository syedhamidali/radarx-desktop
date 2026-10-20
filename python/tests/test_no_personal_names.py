"""The engine writes provenance into exported files and sends a user agent on the network. Neither
may carry a personal name or a source-hosting account (docs/RELEASING.md, the brand module in
``src/lib/utils/brand.ts``). The maker is credited as "Radarx Desktop by LabShope Technologies"."""

from __future__ import annotations

import re
from pathlib import Path

PYTHON = Path(__file__).resolve().parents[1]
ROOT = PYTHON.parent
FORBIDDEN = re.compile(r"hamid|syed|github\.com/[A-Za-z0-9_-]+/(radarx|xradar)-desktop", re.I)


def test_engine_sources_name_no_person_and_no_repository() -> None:
    files = [
        *sorted((PYTHON / "engine").rglob("*.py")),
        *sorted((PYTHON / "modules").rglob("*.py")),
    ]
    files += [PYTHON / "server.py", PYTHON / "pyproject.toml"]
    assert len(files) > 20
    hits = [
        f"{f.relative_to(ROOT)}:{n}: {line.strip()[:80]}"
        for f in files
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1)
        if FORBIDDEN.search(line)
    ]
    assert hits == []


def test_the_network_user_agents_point_at_the_website() -> None:
    from engine.animate_tiles import USER_AGENT
    from engine.environment.net import default_user_agent

    assert "radarx.labshope.com" in USER_AGENT
    assert "radarx.labshope.com" in default_user_agent()
    assert not FORBIDDEN.search(USER_AGENT + default_user_agent())


def test_the_bundle_config_credits_the_company() -> None:
    import json

    conf = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    credit = "Radarx Desktop by LabShope Technologies"
    assert conf["bundle"]["publisher"] == credit
    assert conf["bundle"]["copyright"] == f"Copyright (c) 2024-2026 {credit}"
    assert f'authors = ["{credit}"]' in (ROOT / "src-tauri" / "Cargo.toml").read_text()
    assert f'authors = [{{ name = "{credit}" }}]' in (PYTHON / "pyproject.toml").read_text()
