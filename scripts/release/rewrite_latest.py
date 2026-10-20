#!/usr/bin/env python3
"""Prepare the version folder that is uploaded to the download host.

tauri-action writes a `latest.json` whose asset urls point at the GitHub release. This script

1. checks that manifest (Tauri v2 shape: version, notes, pub_date, platforms with an https url and
   a signature each; the version must equal the tag),
2. rewrites every `url` to https://downloads.labshope.com/radarx/v<version>/<file name>
   and leaves every signature byte for byte as it was (a signature is never recomputed),
3. checks the folder: every referenced file is there, and each `<file>.sig` equals the manifest's
   signature for it,
4. writes `latest.json` (the CANDIDATE manifest) and `SHA256SUMS` into the folder.

Usage:  python scripts/release/rewrite_latest.py --tag v0.3.0 --dir staging

It refuses (exit code 1, nothing written) on a missing signature, a non-https url, a version that
does not match the tag, a missing file or a signature that differs from its .sig file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from release_common import (
    BOOKKEEPING,
    CANDIDATE_NAME,
    DEFAULT_BASE_URL,
    DEFAULT_REQUIRED_PLATFORMS,
    SUMS_NAME,
    ReleaseError,
    check_file_name,
    dump_json,
    file_name_of_url,
    format_sums,
    rewrite_manifest,
    sha256_bytes,
    sha256_file,
    validate_manifest,
    version_of,
)


def prepare_folder(
    folder: Path,
    tag: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    source: Path | None = None,
    require: tuple[str, ...] = DEFAULT_REQUIRED_PLATFORMS,
) -> dict[str, str]:
    """Validate, rewrite and checksum `folder`; return the SHA256SUMS content as {name: hex}."""
    version = version_of(tag)
    src = source or folder / CANDIDATE_NAME
    try:
        manifest = json.loads(src.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReleaseError(f"no manifest at {src}") from exc
    except ValueError as exc:
        raise ReleaseError(f"{src} is not valid JSON") from exc

    validate_manifest(manifest, version=version, require=require)
    rewritten = rewrite_manifest(manifest, version, base_url)
    validate_manifest(rewritten, version=version, require=require)

    # the files the manifest points at must be in the folder, with their signatures
    for key, entry in rewritten["platforms"].items():
        name = file_name_of_url(entry["url"])
        if not (folder / name).is_file():
            raise ReleaseError(f"platform {key}: {name} is not in {folder}")
        sig_file = folder / (name + ".sig")
        if not sig_file.is_file():
            raise ReleaseError(f"platform {key}: {name}.sig is not in {folder}")
        if sig_file.read_text(encoding="utf-8").strip() != entry["signature"].strip():
            raise ReleaseError(f"platform {key}: {name}.sig differs from the manifest signature")

    (folder / CANDIDATE_NAME).write_bytes(dump_json(rewritten))

    sums: dict[str, str] = {}
    for p in sorted(folder.iterdir()):
        if p.name in BOOKKEEPING:
            continue
        if not p.is_file():
            raise ReleaseError(f"unexpected folder in the release: {p.name}")
        sums[check_file_name(p.name)] = (
            sha256_bytes(p.read_bytes()) if p.name == CANDIDATE_NAME else sha256_file(p)
        )
    (folder / SUMS_NAME).write_text(format_sums(sums), encoding="utf-8")
    return sums


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--tag", required=True, help="release tag, e.g. v0.3.0")
    ap.add_argument("--dir", required=True, type=Path, help="folder with the downloaded assets")
    ap.add_argument("--input", type=Path, help="manifest to read (default DIR/latest.json)")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument(
        "--require",
        default=",".join(DEFAULT_REQUIRED_PLATFORMS),
        help="comma-separated platform keys the manifest must have ('' for none)",
    )
    args = ap.parse_args(argv)
    require = tuple(p for p in args.require.split(",") if p)
    try:
        sums = prepare_folder(
            args.dir, args.tag, base_url=args.base_url, source=args.input, require=require
        )
    except ReleaseError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(
        f"{args.tag}: {len(sums)} files checksummed; {CANDIDATE_NAME} rewritten to {args.base_url}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
