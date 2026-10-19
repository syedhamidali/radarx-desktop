#!/usr/bin/env python3
# ruff: noqa: E501
"""Collect the licences of everything radarx-desktop ships and write ``THIRD_PARTY_NOTICES.md``.

What is collected
-----------------
* **Python** - the dependencies of ``python/pyproject.toml`` (plus the ``radarx`` extra and the packages
  the PyInstaller spec collects when present) and everything they require, read from the *installed*
  package metadata of the interpreter that runs this script. Run it with the engine's environment
  (``pip install -e "python[radarx]"``).
* **npm** - the production dependencies of ``package.json`` (and the runtime of Svelte, a dev dependency
  whose runtime is compiled into the bundle), resolved through ``package-lock.json``. No ``npm install`` needed.
* **Rust** - the normal (not build/dev) dependency closure of ``src-tauri`` from ``cargo metadata``.
* **Static entries** - fonts, colour maps, map/terrain/imagery services, public data and native libraries
  that wheels bundle, from ``scripts/licenses/static.toml``.

Policy
------
``scripts/licenses/allowed.toml`` says which licence identifiers are accepted. A dependency passes when
its (SPDX) expression has an alternative that is allowed; weak or strong copyleft, non-commercial,
unknown or unrecognised licences fail unless the package is listed under ``[reviewed]`` with a written
reason. The reviewed ones are repeated in a "needs attention" table at the top of the notices.

Usage
-----
::

    python scripts/licenses/generate_notices.py             # write THIRD_PARTY_NOTICES.md
    python scripts/licenses/generate_notices.py --check     # exit 1 on a licence the policy rejects
    python scripts/licenses/generate_notices.py --verify    # exit 1 if the committed file is stale
                                                            # (npm, Rust and static sections only:
                                                            #  Python versions depend on the platform)

This is an engineering inventory, not legal advice. See docs/LICENSES.md.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from importlib import metadata
from pathlib import Path

import tomllib

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
OUTPUT = ROOT / "THIRD_PARTY_NOTICES.md"

# category order: a larger number is a worse (more restrictive) outcome
PERMISSIVE, WEAK, STRONG, NONCOMMERCIAL, UNKNOWN = 0, 1, 2, 3, 4
CATEGORY_NAME = {
    PERMISSIVE: "permissive",
    WEAK: "weak copyleft",
    STRONG: "strong copyleft",
    NONCOMMERCIAL: "non-commercial",
    UNKNOWN: "unknown",
}

# ---------------------------------------------------------------------------------------------
# licence names found in package metadata -> SPDX identifiers
# ---------------------------------------------------------------------------------------------
_CLASSIFIER_TO_SPDX = {
    "mit license": "MIT",
    "mit no attribution license (mit-0)": "MIT-0",
    "bsd license": "BSD-3-Clause",
    "apache software license": "Apache-2.0",
    "isc license (iscl)": "ISC",
    "python software foundation license": "PSF-2.0",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "gnu lesser general public license v2 or later (lgplv2+)": "LGPL-2.1-or-later",
    "gnu lesser general public license v3 or later (lgplv3+)": "LGPL-3.0-or-later",
    "gnu lesser general public license v3 (lgplv3)": "LGPL-3.0-only",
    "gnu library or lesser general public license (lgpl)": "LGPL-2.1-or-later",
    "gnu general public license v2 (gplv2)": "GPL-2.0-only",
    "gnu general public license v2 or later (gplv2+)": "GPL-2.0-or-later",
    "gnu general public license v3 (gplv3)": "GPL-3.0-only",
    "gnu general public license v3 or later (gplv3+)": "GPL-3.0-or-later",
    "gnu affero general public license v3": "AGPL-3.0-only",
    "the unlicense (unlicense)": "Unlicense",
    "zlib/libpng license": "Zlib",
    "public domain": "LicenseRef-PublicDomain",
    "creative commons zero v1.0 universal": "CC0-1.0",
    "boost software license 1.0 (bsl-1.0)": "BSL-1.0",
    "european union public licence 1.2 (eupl 1.2)": "EUPL-1.2",
    "historical permission notice and disclaimer (hpnd)": "HPND",
}
_NAME_TO_SPDX = {
    "mit": "MIT",
    "mit license": "MIT",
    "the mit license": "MIT",
    "bsd": "BSD-3-Clause",
    "bsd license": "BSD-3-Clause",
    "bsd 3-clause": "BSD-3-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "3-clause bsd": "BSD-3-Clause",
    "new bsd": "BSD-3-Clause",
    "bsd 2-clause": "BSD-2-Clause",
    "bsd-2-clause": "BSD-2-Clause",
    "simplified bsd": "BSD-2-Clause",
    "apache": "Apache-2.0",
    "apache 2": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache-2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "apache software license 2.0": "Apache-2.0",
    "isc": "ISC",
    "isc license": "ISC",
    "psf": "PSF-2.0",
    "psf-2.0": "PSF-2.0",
    "python software foundation license": "PSF-2.0",
    "mpl-2.0": "MPL-2.0",
    "mozilla public license 2.0": "MPL-2.0",
    "lgpl": "LGPL-2.1-or-later",
    "gpl": "GPL-3.0-or-later",
    "unlicense": "Unlicense",
    "cc0": "CC0-1.0",
    "cc0-1.0": "CC0-1.0",
    "zlib": "Zlib",
    "public domain": "LicenseRef-PublicDomain",
    "hpnd": "HPND",
    "cc-by-4.0": "CC-BY-4.0",
}
# phrases that identify a licence text when the metadata field holds the whole text
_TEXT_SIGNATURES = [
    (r"apache license\s+version 2\.0", "Apache-2.0"),
    (r"permission is hereby granted, free of charge, to any person obtaining a copy", "MIT"),
    (r"redistribution and use in source and binary forms", "BSD-3-Clause"),
    (
        r"permission to use, copy, modify, and/or distribute this software for any purpose with or without fee",
        "ISC",
    ),
    (r"mozilla public license,? (version|v\.?) 2\.0", "MPL-2.0"),
    (r"this is free and unencumbered software released into the public domain", "Unlicense"),
    (r"gnu lesser general public license", "LGPL-2.1-or-later"),
    (r"gnu affero general public license", "AGPL-3.0-only"),
    (r"gnu general public license", "GPL-3.0-or-later"),
    (r"python software foundation license", "PSF-2.0"),
]


def _norm_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def python_license(dist: metadata.Distribution) -> tuple[str, str]:
    """Return ``(spdx expression or '', how it was found)`` for an installed distribution."""
    md = dist.metadata
    expr = md.get("License-Expression")
    if expr:
        return expr.strip(), "License-Expression"
    ids: list[str] = []
    for c in md.get_all("Classifier") or []:
        if c.startswith("License ::"):
            name = _norm_key(c.split("::")[-1])
            if name in _CLASSIFIER_TO_SPDX:
                ids.append(_CLASSIFIER_TO_SPDX[name])
            elif name not in ("osi approved",):
                ids.append("LicenseRef-" + re.sub(r"\W+", "-", name).strip("-"))
    if ids:
        ids = sorted(set(ids))
        # several classifiers usually mean "either" (dual licensed)
        return " OR ".join(ids), "classifier"
    lic = (md.get("License") or "").strip()
    if lic and lic.upper() != "UNKNOWN":
        key = _norm_key(lic)
        if key in _NAME_TO_SPDX:
            return _NAME_TO_SPDX[key], "License field"
        if len(lic) < 80 and re.fullmatch(r"[A-Za-z0-9.+\- ()]+", lic) and _looks_spdx(lic):
            return lic, "License field"
        low = _norm_key(lic)
        for pattern, spdx in _TEXT_SIGNATURES:
            if re.search(pattern, low):
                return spdx, "licence text (detected)"
    # last resort: a licence file shipped in the distribution
    for f in dist.files or []:
        name = f.name.lower()
        if name.startswith(("license", "licence", "copying", "notice")):
            try:
                text = _norm_key(f.read_text())
            except Exception:
                continue
            for pattern, spdx in _TEXT_SIGNATURES:
                if re.search(pattern, text):
                    return spdx, f"licence file {f.name} (detected)"
    return "", "none found"


def _looks_spdx(text: str) -> bool:
    return bool(
        re.fullmatch(
            r"[A-Za-z0-9.+\-]+(\s+(AND|OR|WITH)\s+[A-Za-z0-9.+\-]+|\s*[()])*", text.strip()
        )
    )


# ---------------------------------------------------------------------------------------------
# SPDX expression evaluation
# ---------------------------------------------------------------------------------------------
_TOKEN = re.compile(r"\(|\)|[^\s()]+")


def _tokens(expr: str) -> list[str]:
    expr = re.sub(r"\s*/\s*", " OR ", expr)  # old Cargo style "MIT/Apache-2.0"
    return _TOKEN.findall(expr)


class Policy:
    def __init__(self, data: dict):
        self.permissive = set(data["categories"]["permissive"])
        self.weak = set(data["categories"]["weak_copyleft"])
        self.strong = set(data["categories"]["strong_copyleft"])
        self.noncommercial = set(data["categories"]["non_commercial"])
        self.exceptions = set(data["categories"].get("exceptions", []))
        self.aliases = data.get("aliases", {})
        self.reviewed = data.get("reviewed", {})
        self.overrides = data.get("overrides", {})
        self.npm_runtime_roots = data.get("npm_runtime_roots", [])

    def classify_id(self, ident: str) -> int:
        ident = self.aliases.get(ident, ident)
        ident = (
            ident.rstrip("+")
            if ident.rstrip("+") in self.permissive | self.weak | self.strong
            else ident
        )
        if ident in self.permissive:
            return PERMISSIVE
        if ident in self.weak:
            return WEAK
        if ident in self.strong:
            return STRONG
        if ident in self.noncommercial:
            return NONCOMMERCIAL
        return UNKNOWN

    def evaluate(self, expr: str) -> tuple[int, str]:
        """Best reachable category of an SPDX expression and a note on the choice.

        ``A OR B`` takes the better alternative, ``A AND B`` the worse requirement.
        ``X WITH exception`` is judged as ``X`` when the exception is known.
        """
        toks = _tokens(expr)
        pos = 0

        def peek() -> str | None:
            return toks[pos] if pos < len(toks) else None

        def parse_or() -> int:
            nonlocal pos
            best = parse_and()
            while peek() is not None and peek().upper() == "OR":
                pos += 1
                best = min(best, parse_and())
            return best

        def parse_and() -> int:
            nonlocal pos
            worst = parse_atom()
            while peek() is not None and peek().upper() == "AND":
                pos += 1
                worst = max(worst, parse_atom())
            return worst

        def parse_atom() -> int:
            nonlocal pos
            tok = peek()
            if tok is None:
                return UNKNOWN
            if tok == "(":
                pos += 1
                val = parse_or()
                if peek() == ")":
                    pos += 1
                return val
            pos += 1
            cat = self.classify_id(tok)
            if peek() is not None and peek().upper() == "WITH":
                pos += 1
                exc = peek()
                pos += 1
                if exc not in self.exceptions:
                    return max(cat, UNKNOWN)
            return cat

        try:
            cat = parse_or()
            if pos != len(toks):  # trailing garbage: do not trust it
                return UNKNOWN, "unparsed expression"
        except Exception:
            return UNKNOWN, "unparsed expression"
        return cat, ""


# ---------------------------------------------------------------------------------------------
# collectors
# ---------------------------------------------------------------------------------------------
class Entry(dict):
    """name, version, license (SPDX-ish), source (how found), url, ecosystem."""


def _canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def collect_python(policy: Policy) -> list[Entry]:
    from packaging.requirements import Requirement  # imported here: only this collector needs it

    pyproject = tomllib.loads((ROOT / "python" / "pyproject.toml").read_text())
    project = pyproject["project"]
    roots = [Requirement(r) for r in project["dependencies"]]
    roots += [Requirement(r) for r in project.get("optional-dependencies", {}).get("radarx", [])]
    # packages the PyInstaller spec collects when they are installed
    extra_roots = ["fsspec", "s3fs", "h5netcdf", "netCDF4", "numcodecs", "cmweather", "colorcet"]
    dists = {_canon(d.metadata["Name"]): d for d in metadata.distributions() if d.metadata["Name"]}

    seen: dict[str, metadata.Distribution] = {}
    missing: list[str] = []
    stack = [_canon(r.name) for r in roots if (not r.marker or r.marker.evaluate({"extra": ""}))]
    stack += [r for r in map(_canon, extra_roots) if r in dists]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        dist = dists.get(name)
        if dist is None:
            missing.append(name)
            continue
        seen[name] = dist
        for req_text in dist.requires or []:
            req = Requirement(req_text)
            if req.marker and not req.marker.evaluate({"extra": ""}):
                continue
            stack.append(_canon(req.name))
    if missing:
        print(
            "warning: not installed (their licences are not listed): "
            + ", ".join(sorted(set(missing))),
            file=sys.stderr,
        )

    out: list[Entry] = []
    for name, dist in sorted(seen.items()):
        md = dist.metadata
        key = f"python:{name}"
        raw, how = python_license(dist)
        if key in policy.overrides:
            raw, how = (
                policy.overrides[key]["license"],
                "override: " + policy.overrides[key]["reason"],
            )
        url = md.get("Home-page") or ""
        if not url:
            for item in md.get_all("Project-URL") or []:
                label, _, link = item.partition(",")
                if label.strip().lower() in (
                    "homepage",
                    "home",
                    "source",
                    "repository",
                    "documentation",
                ):
                    url = link.strip()
                    break
        out.append(
            Entry(
                ecosystem="python",
                name=md["Name"],
                version=md["Version"],
                license=raw,
                source=how,
                url=url,
            )
        )
    return out


def collect_npm(policy: Policy) -> list[Entry]:
    lock = json.loads((ROOT / "package-lock.json").read_text())
    pkg = json.loads((ROOT / "package.json").read_text())
    packages: dict[str, dict] = lock["packages"]
    roots = list(pkg.get("dependencies", {})) + list(policy.npm_runtime_roots)

    def resolve(parent_path: str, dep: str) -> str | None:
        """Node resolution: look in parent/node_modules, then walk up."""
        base = parent_path
        while True:
            cand = (base + "/" if base else "") + "node_modules/" + dep
            if cand in packages:
                return cand
            if not base:
                return None
            base = base.rsplit("/node_modules/", 1)[0] if "/node_modules/" in base else ""

    seen: dict[str, dict] = {}
    stack = [p for p in (resolve("", r) for r in roots) if p]
    while stack:
        path = stack.pop()
        if path in seen:
            continue
        info = packages[path]
        seen[path] = info
        for dep in list(info.get("dependencies", {})) + list(info.get("optionalDependencies", {})):
            nxt = resolve(path, dep)
            if nxt:
                stack.append(nxt)
    out: list[Entry] = []
    for path, info in sorted(seen.items()):
        name = path.split("node_modules/")[-1]
        key = f"npm:{name}"
        lic = info.get("license", "")
        if isinstance(lic, dict):
            lic = lic.get("type", "")
        how = "package-lock"
        if key in policy.overrides:
            lic, how = (
                policy.overrides[key]["license"],
                "override: " + policy.overrides[key]["reason"],
            )
        out.append(
            Entry(
                ecosystem="npm",
                name=name,
                version=info.get("version", ""),
                license=lic or "",
                source=how if lic else "none found",
                url=f"https://www.npmjs.com/package/{name}",
            )
        )
    return out


def collect_rust(policy: Policy) -> list[Entry]:
    cmd = ["cargo", "metadata", "--format-version", "1", "--locked"]
    try:
        res = subprocess.run(
            cmd, cwd=ROOT / "src-tauri", capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError:
        res = subprocess.run(
            [*cmd, "--offline"], cwd=ROOT / "src-tauri", capture_output=True, text=True, check=True
        )
    data = json.loads(res.stdout)
    by_id = {p["id"]: p for p in data["packages"]}
    nodes = {n["id"]: n for n in data["resolve"]["nodes"]}
    root = data["resolve"]["root"]
    seen: set[str] = set()
    stack = [root]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for dep in nodes[cur]["deps"]:
            if any(k["kind"] is None for k in dep["dep_kinds"]):  # normal (shipped) dependency
                stack.append(dep["pkg"])
    seen.discard(root)
    out: list[Entry] = []
    for pid in sorted(seen, key=lambda i: (by_id[i]["name"], by_id[i]["version"])):
        p = by_id[pid]
        key = f"rust:{p['name']}"
        lic = p.get("license") or ""
        how = "Cargo.toml"
        if not lic and p.get("license_file"):
            how = "license file only"
        if key in policy.overrides:
            lic, how = (
                policy.overrides[key]["license"],
                "override: " + policy.overrides[key]["reason"],
            )
        out.append(
            Entry(
                ecosystem="rust",
                name=p["name"],
                version=p["version"],
                license=lic,
                source=how if lic else "none found",
                url=p.get("repository")
                or p.get("homepage")
                or f"https://crates.io/crates/{p['name']}",
            )
        )
    return out


def collect_static() -> list[dict]:
    return tomllib.loads((HERE / "static.toml").read_text()).get("entry", [])


# ---------------------------------------------------------------------------------------------
# evaluation and rendering
# ---------------------------------------------------------------------------------------------
def judge(entries: list[Entry], policy: Policy) -> None:
    """Add ``category`` and ``status`` ('ok', 'reviewed' or 'FAIL') to every entry."""
    for e in entries:
        expr = e["license"]
        if not expr:
            cat, note = UNKNOWN, "no licence information"
        else:
            cat, note = policy.evaluate(expr)
        key = f"{e['ecosystem']}:{_canon(e['name']) if e['ecosystem'] == 'python' else e['name']}"
        e["category"] = cat
        if cat == PERMISSIVE:
            e["status"], e["note"] = "ok", ""
        elif key in policy.reviewed:
            e["status"], e["note"] = "reviewed", policy.reviewed[key]["note"]
        else:
            e["status"] = "FAIL"
            e["note"] = note or f"{CATEGORY_NAME[cat]} licence not in the allow-list"


def esc(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def table(rows: list[list[str]], header: list[str]) -> str:
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    lines += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def render(
    py: list[Entry], npm: list[Entry], rust: list[Entry], static: list[dict], policy: Policy
) -> str:
    out: list[str] = []
    w = out.append
    w("# Third-party notices\n")
    w(
        "<!-- generated by scripts/licenses/generate_notices.py - do not edit by hand; "
        "edit scripts/licenses/static.toml or allowed.toml and run it again -->\n"
    )
    w(
        "radarx-desktop is licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE). It builds on, bundles "
        "or loads at run time the third-party software, fonts, colour maps and map/terrain/imagery services listed "
        "below, each under its own terms. Those terms are not changed by the project licence.\n"
    )
    w(
        "**This is an engineering inventory, not legal advice.** Licence identifiers come from each package's own "
        "metadata (SPDX where given); entries that had none were identified by a script or by hand and are marked. "
        "The complete licence texts and copyright lines of the packages are in the packages themselves "
        "(the installers keep the `LICENSE`/`NOTICE` files that the packages ship; reproducing every text in this "
        "file is not done yet). **The wording and the completeness of this file are to be reviewed by a lawyer "
        "before wider redistribution.** How it is made and checked: [docs/LICENSES.md](docs/LICENSES.md).\n"
    )

    # --- needs attention -------------------------------------------------------------------
    flagged = [e for e in py + npm + rust if e["status"] != "ok"]
    w("## Needs attention\n")
    w(
        "Components under a licence that is not simply permissive, and the non-commercial imagery. Each has a "
        "written reason in `scripts/licenses/allowed.toml` (or `static.toml`) for why it is accepted and how it "
        "is used. Anything here that changes with a release must be re-read.\n"
    )
    rows: list[list[str]] = []
    for e in flagged:
        rows.append(
            [
                e["ecosystem"],
                f"{e['name']} {e['version']}",
                e["license"] or "(none)",
                CATEGORY_NAME[e["category"]],
                e["note"],
            ]
        )
    for s in static:
        if s.get("flag"):
            rows.append([s["kind"], s["name"], s["license"], s["flag"], s.get("note", "")])
    w(table(rows, ["Kind", "Component", "Licence", "Class", "How it is used / why accepted"]))
    w("")

    # --- static ----------------------------------------------------------------------------
    kinds = [
        ("font", "Fonts"),
        ("colormap", "Colour maps"),
        ("imagery", "Map, terrain and imagery services (fetched at run time on request)"),
        ("data", "Data the app can open or fetch (not redistributed with the installers)"),
        ("native", "Native libraries bundled by Python wheels and the Python runtime"),
    ]
    for kind, title in kinds:
        items = [s for s in static if s["kind"] == kind]
        if not items:
            continue
        w(f"## {title}\n")
        if kind == "native":
            w(
                "Wheels such as h5py, netCDF4, pyproj, Pillow, scipy and numpy ship shared libraries inside them, and "
                "PyInstaller copies them into the engine. Their licences come from the upstream projects and are **not** "
                "read from the built engine here; re-scan each release build with "
                "`python scripts/licenses/generate_notices.py --scan-engine python/dist/xradar-engine` "
                "(lists the shared libraries actually shipped) and compare.\n"
            )
        rows = [
            [
                s["name"],
                s["license"],
                s.get("where", ""),
                s.get("attribution", ""),
                s.get("url", ""),
            ]
            for s in items
        ]
        w(table(rows, ["Name", "Licence", "Where used", "Attribution / terms", "Link"]))
        w("")

    # --- package tables --------------------------------------------------------------------
    def pkg_table(entries: list[Entry], title: str, intro: str) -> None:
        w(f"## {title}\n")
        w(intro + "\n")
        counts = Counter(e["license"] or "(none)" for e in entries)
        w(
            f"{len(entries)} packages. Licences: "
            + ", ".join(f"{k} ({v})" for k, v in counts.most_common())
            + ".\n"
        )
        rows = [
            [
                e["name"],
                e["version"],
                e["license"] or "(none)",
                e["source"] if e["source"] != "License-Expression" else "",
                e["url"],
            ]
            for e in entries
        ]
        w(table(rows, ["Package", "Version", "Licence", "Identified by", "Link"]))
        w("")

    pkg_table(
        py,
        "Python engine",
        "Resolved from `python/pyproject.toml` (dependencies and the `radarx` extra) and the packages the "
        "PyInstaller spec collects, with everything they require, in the environment the file was generated in. "
        "Versions of platform-specific wheels can differ in a release build.",
    )
    pkg_table(
        npm,
        "Frontend (npm)",
        "The production dependencies of `package.json` and the runtime of Svelte (a dev dependency whose runtime is "
        "compiled into the bundle), resolved through `package-lock.json`. Build tools (Vite, TypeScript, ...) are not "
        "shipped and are not listed.",
    )
    pkg_table(
        rust,
        "Desktop shell (Rust crates)",
        "The normal (non-build, non-dev) dependency closure of `src-tauri`, all target platforms, from "
        "`cargo metadata --locked`. Crates used only for another operating system are listed too.",
    )
    return "\n".join(out).rstrip() + "\n"


def sections(text: str) -> dict[str, str]:
    """Split a notices file into its ``## `` sections (used by --verify)."""
    parts = re.split(r"^## ", text, flags=re.M)
    return {p.split("\n", 1)[0].strip(): p for p in parts[1:]}


def scan_engine(dist: Path) -> None:
    libs = sorted(
        {p.name for p in dist.rglob("*") if re.search(r"\.(dylib|so(\.\d+)*|dll)$", p.name)}
    )
    infos = sorted({p.name for p in dist.rglob("*.dist-info")})
    print(f"{len(libs)} shared libraries and {len(infos)} dist-info folders under {dist}\n")
    print("Shared libraries:")
    print("\n".join("  " + n for n in libs))
    print("\ndist-info:")
    print("\n".join("  " + n for n in infos))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--check", action="store_true", help="exit 1 if a dependency fails the allow-list"
    )
    ap.add_argument(
        "--verify", action="store_true", help="exit 1 if THIRD_PARTY_NOTICES.md is stale"
    )
    ap.add_argument("--output", type=Path, default=OUTPUT)
    ap.add_argument(
        "--skip", default="", help="comma list of python,npm,rust to skip (check/verify only)"
    )
    ap.add_argument(
        "--scan-engine",
        type=Path,
        help="list the shared libraries of a built engine folder and exit",
    )
    args = ap.parse_args(argv)

    if args.scan_engine:
        scan_engine(args.scan_engine)
        return 0

    policy = Policy(tomllib.loads((HERE / "allowed.toml").read_text()))
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    py = [] if "python" in skip else collect_python(policy)
    npm = [] if "npm" in skip else collect_npm(policy)
    rust = [] if "rust" in skip else collect_rust(policy)
    static = collect_static()
    for group in (py, npm, rust):
        judge(group, policy)

    failures = [e for e in py + npm + rust if e["status"] == "FAIL"]
    for e in failures:
        print(
            f"FAIL {e['ecosystem']}:{e['name']} {e['version']}: {e['license'] or '(none)'} - {e['note']}",
            file=sys.stderr,
        )

    if args.check:
        total = len(py) + len(npm) + len(rust)
        print(
            f"checked {total} dependencies ({len(py)} python, {len(npm)} npm, {len(rust)} rust): {len(failures)} rejected"
        )
        for e in py + npm + rust:
            if e["status"] == "reviewed":
                print(
                    f"  reviewed {e['ecosystem']}:{e['name']} {e['version']} ({e['license']}): {e['note']}"
                )
        return 1 if failures else 0

    text = render(py, npm, rust, static, policy)
    if args.verify:
        if not args.output.exists():
            print(f"{args.output} does not exist", file=sys.stderr)
            return 1
        have, want = sections(args.output.read_text()), sections(text)
        stale = []
        for title in want:
            if title.startswith("Python engine") or title == "Needs attention":
                continue  # depends on the platform / the Python environment
            if have.get(title) != want[title]:
                stale.append(title)
        if stale:
            print("THIRD_PARTY_NOTICES.md is out of date in: " + ", ".join(stale), file=sys.stderr)
            print("run: python scripts/licenses/generate_notices.py", file=sys.stderr)
            return 1
        print("THIRD_PARTY_NOTICES.md is up to date (npm, Rust and static sections)")
        return 1 if failures else 0

    if failures:
        print(
            f"{len(failures)} dependencies fail the policy; the file is written anyway",
            file=sys.stderr,
        )
    args.output.write_text(text)
    print(
        f"wrote {args.output.relative_to(ROOT) if args.output.is_relative_to(ROOT) else args.output}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
