#!/usr/bin/env python3
"""Check the links inside the repository's Markdown files (no network).

Checks, for the top-level ``*.md`` files, ``docs/*.md`` and ``.github/*.md``:

* relative links and images ``[text](path)``, ``![alt](path)`` point to an existing file or folder,
* ``#anchors`` (own page or another Markdown file) match a heading, with GitHub's slug rules,
* MyST roles ``{doc}`name```, and ``{image}``/``{figure}`` directive paths in ``docs/``,
* every ``docs/*.md`` page is listed in a ``toctree`` of ``docs/index.md`` (Sphinx warns otherwise).

External ``http(s)`` links are only counted; add ``--external`` to also request them (needs network).

    python scripts/check_links.py [--external]

Exit status 1 when something is broken.
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"node_modules", ".venv", "_build", "_api", "target", "dist", "dist-web", ".git", "changes"}
GENERATED = {"docs/changes/unreleased.md"}

LINK = re.compile(r"(!?)\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
MYST_DOC = re.compile(r"\{doc\}`([^`]+)`")
MYST_IMG = re.compile(r"^```\{(?:image|figure)\}\s+(\S+)", re.M)
FENCE = re.compile(r"^(```|~~~).*?^\1", re.M | re.S)
INLINE_CODE = re.compile(r"`[^`\n]*`")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$", re.M)
TOCTREE = re.compile(r"```\{toctree\}\n(.*?)```", re.S)


def md_files() -> list[Path]:
    files: list[Path] = []
    for pattern in ("*.md", "docs/*.md", ".github/*.md"):
        files += [p for p in ROOT.glob(pattern) if p.relative_to(ROOT).as_posix() not in GENERATED]
    return sorted(files)


def slug(heading: str) -> str:
    """GitHub's anchor for a heading."""
    text = re.sub(r"`([^`]*)`", r"\1", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_~]", "", text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text, flags=re.U)
    return text.replace(" ", "-")


_anchor_cache: dict[Path, set[str]] = {}


def anchors(path: Path) -> set[str]:
    if path not in _anchor_cache:
        text = FENCE.sub("", path.read_text())
        seen: dict[str, int] = {}
        out: set[str] = set()
        for _, title in HEADING.findall(text):
            s = slug(title)
            n = seen.get(s, 0)
            seen[s] = n + 1
            out.add(s if n == 0 else f"{s}-{n}")
        # explicit HTML anchors
        out |= set(re.findall(r'<a\s+(?:name|id)="([^"]+)"', text))
        _anchor_cache[path] = out
    return _anchor_cache[path]


def check_external(url: str) -> str | None:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "xradar-desktop-linkcheck"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            return None if resp.status < 400 else f"HTTP {resp.status}"
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__ + ": " + str(exc)[:80]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--external", action="store_true", help="also request http(s) links")
    args = ap.parse_args()

    problems: list[str] = []
    external: set[str] = set()
    n_links = 0

    for md in md_files():
        rel = md.relative_to(ROOT).as_posix()
        text = md.read_text()
        body = INLINE_CODE.sub("", FENCE.sub("", text))
        in_docs = md.parent == ROOT / "docs"

        for _, _, target in LINK.findall(body):
            n_links += 1
            if re.match(r"(https?:|mailto:|tel:)", target):
                if target.startswith("http"):
                    external.add(target)
                continue
            path_part, _, frag = target.partition("#")
            if not path_part:  # own page
                dest = md
            else:
                dest = (md.parent / path_part.split("?")[0]).resolve()
                if not dest.exists():
                    problems.append(f"{rel}: missing target {target}")
                    continue
            if frag and dest.suffix == ".md" and dest.is_file() and frag not in anchors(dest):
                problems.append(f"{rel}: no heading for anchor {target}")

        if in_docs:
            for name in MYST_DOC.findall(body):
                target = name.split("<")[-1].rstrip(">").strip() if "<" in name else name
                target = target.lstrip("/")
                if not (ROOT / "docs" / f"{target}.md").exists() and not target.startswith("_api"):
                    problems.append(f"{rel}: {{doc}} target {target!r} not found")
            for img in MYST_IMG.findall(text):
                if not re.match(r"https?:", img) and not (md.parent / img).exists():
                    problems.append(f"{rel}: image {img} not found")

    index = (ROOT / "docs" / "index.md").read_text()
    listed = {
        line.strip()
        for block in TOCTREE.findall(index)
        for line in block.splitlines()
        if line.strip() and not line.startswith(":")
    }
    for page in sorted((ROOT / "docs").glob("*.md")):
        if page.stem != "index" and page.stem not in listed:
            problems.append(f"docs/{page.name}: not in a toctree of docs/index.md (Sphinx will warn)")
    for name in sorted(listed):
        if not name.startswith("_api") and not (ROOT / "docs" / f"{name}.md").exists():
            problems.append(f"docs/index.md: toctree entry {name!r} has no page")

    if args.external:
        for url in sorted(external):
            err = check_external(url)
            if err:
                problems.append(f"external {url}: {err}")

    for p in problems:
        print(p, file=sys.stderr)
    print(
        f"checked {len(md_files())} Markdown files, {n_links} links "
        f"({len(external)} external{'' if args.external else ', not requested'}): {len(problems)} problems"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
