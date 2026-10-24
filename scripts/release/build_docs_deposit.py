#!/usr/bin/env python3
"""Build the files of the Zenodo deposit of the user documentation (see docs/zenodo/README.md).

Writes into ``--out``:

    radarx-desktop-documentation.pdf                      the manual (Sphinx LaTeX, XeLaTeX)
    radarx-desktop-documentation-html.zip                 the web pages, built from the same sources
    zenodo_metadata.json                                  the record metadata (date filled in; no version)
    SHA256SUMS                                            checksums of the files above

Both builds use SOURCE_DATE_EPOCH = the time of the current commit, so the dates printed in the documentation
and the zip entries come from the sources, not from the moment of the build. The HTML zip is deterministic
(sorted entries, fixed timestamps, no Sphinx caches).

Intermediate build folders go to ``<out>/_build`` and are deleted at the end (``--keep-build`` keeps them).
The build stops when the disk holding ``--out`` has less than ``--min-free-gb`` free, and refuses a deposit
larger than ``--max-mb``.

Usage:  python scripts/release/build_docs_deposit.py --out ~/Downloads/radarx_docs_doi [--skip-pdf]
        (with the Python that has the docs requirements: pip install -e "python[docs]" or docs/requirements)
"""

# ruff: noqa: E501
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
NAME = "radarx-desktop-documentation"
# The earliest date a zip entry can hold: the same bytes on every build.
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
SKIP_HTML = {".buildinfo", ".doctrees", "_sources", ".nojekyll"}


def free_gb(path: Path) -> float:
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free / 1e9


def check_disk(out: Path, minimum: float, step: str) -> None:
    free = free_gb(out)
    print(f"[deposit] {step}: {free:.1f} GB free")
    if free < minimum:
        sys.exit(f"[deposit] stopped before {step}: only {free:.1f} GB free (minimum {minimum} GB)")


def run(cmd: list[str], env: dict[str, str], cwd: Path | None = None) -> None:
    print("[deposit] $", " ".join(cmd))
    subprocess.run(cmd, check=True, env=env, cwd=cwd)


def commit_epoch() -> int:
    out = subprocess.run(["git", "log", "-1", "--format=%ct"], cwd=ROOT, check=True, capture_output=True, text=True)
    return int(out.stdout.strip())


def zip_html(html: Path, target: Path, top: str) -> None:
    files = sorted(
        p for p in html.rglob("*") if p.is_file() and not (set(p.relative_to(html).parts) & SKIP_HTML)
    )
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in files:
            info = zipfile.ZipInfo(f"{top}/{p.relative_to(html).as_posix()}", ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, p.read_bytes())


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True, help="folder for the deposit files")
    ap.add_argument("--skip-pdf", action="store_true", help="build only the HTML zip")
    ap.add_argument("--keep-build", action="store_true", help="keep <out>/_build (intermediate files)")
    ap.add_argument("--min-free-gb", type=float, default=15.0, help="stop below this much free disk (default 15)")
    ap.add_argument("--max-mb", type=float, default=300.0, help="largest allowed deposit (default 300 MB)")
    args = ap.parse_args()

    out = args.out.expanduser().resolve()
    build = out / "_build"
    out.mkdir(parents=True, exist_ok=True)
    check_disk(out, args.min_free_gb, "start")

    epoch = commit_epoch()
    date = datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d")
    # RADARX_DEPOSIT: the PDF title page and the web pages carry no software version (docs/conf.py)
    env = {**os.environ, "SOURCE_DATE_EPOCH": str(epoch), "RADARX_DEPOSIT": "1"}
    for key in ("READTHEDOCS", "READTHEDOCS_VERSION_TYPE", "READTHEDOCS_GIT_IDENTIFIER"):
        env.pop(key, None)  # the version comes from package.json, as in a local build
    sphinx = [sys.executable, "-m", "sphinx", "-q", "-E"]

    produced: list[Path] = []
    try:
        # HTML: the web pages, zipped
        run([*sphinx, "-b", "html", str(DOCS), str(build / "html")], env)
        check_disk(out, args.min_free_gb, "zip")
        zipped = out / f"{NAME}-html.zip"
        zip_html(build / "html", zipped, NAME)
        produced.append(zipped)

        # PDF: Sphinx LaTeX, then latexmk with XeLaTeX (latex_engine in docs/conf.py)
        if not args.skip_pdf:
            check_disk(out, args.min_free_gb, "pdf")
            latex = build / "latex"
            run([*sphinx, "-b", "latex", str(DOCS), str(latex)], env)
            run(["latexmk", "-xelatex", "-interaction=nonstopmode", "-halt-on-error", f"{NAME}.tex"], env, cwd=latex)
            pdf = out / f"{NAME}.pdf"
            shutil.copyfile(latex / f"{NAME}.pdf", pdf)
            produced.append(pdf)
    finally:
        if not args.keep_build:
            shutil.rmtree(build, ignore_errors=True)

    # Record metadata: the publication date from the sources, no version; DOIs are assigned by Zenodo, never here
    meta = json.loads((DOCS / "zenodo" / "zenodo_metadata.json").read_text(encoding="utf-8"))
    meta["metadata"].pop("version", None)
    meta["metadata"]["publication_date"] = date
    meta_path = out / "zenodo_metadata.json"
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    produced.append(meta_path)

    sums = out / "SHA256SUMS"
    sums.write_text("".join(f"{sha256(p)}  {p.name}\n" for p in produced), encoding="utf-8")

    total = sum(p.stat().st_size for p in produced) / 1e6
    for p in produced:
        print(f"[deposit] {p.name}: {p.stat().st_size / 1e6:.1f} MB")
    print(f"[deposit] total {total:.1f} MB, date {date}")
    if total > args.max_mb:
        sys.exit(f"[deposit] the deposit is {total:.1f} MB, more than {args.max_mb} MB")


if __name__ == "__main__":
    main()
